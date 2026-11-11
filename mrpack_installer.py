#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mrpack_installer.py —— Modrinth 整合包 (.mrpack) 安装模块
=========================================================
独立、可复用：把官方 Modrinth 的 .mrpack 包解压并安装到一个 Minecraft 实例里。

流程：
  1. 解析：zipfile 打开 .mrpack，读根目录的 modrinth.index.json
  2. 提取基础信息：依赖的 minecraft 版本、modLoaders（如 fabric-loader-xxx）
  3. 下载文件：遍历 index["files"]，每个文件按 downloads[0] 下载，
     并按路径分目录落盘（mods/ -> mods 目录，config/ -> 配置目录，
     shaderpacks/ -> 光影目录，其余按相对路径落到游戏根目录）
  4. 解压覆盖（Overrides）：把包内 overrides/ 完整解压并覆盖到游戏根目录
  5. 进度回调：progress_cb(cur, total, note) 实时汇报
     （note 形如 “当前正在下载：xxx Mod (进度 3/100)”）

依赖：mc_launcher（提供 core.Net.download）。不改动任何底层网络逻辑。
"""

from __future__ import annotations

import concurrent.futures
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Callable, Optional

import mc_launcher as core


# ---------------------------------------------------------------------------
# 常量 / 小工具
# ---------------------------------------------------------------------------
# 这些一级子目录要落到游戏根目录下的对应子目录（严格版本隔离同款约定）
_KNOWN_DIRS = ("mods", "config", "shaderpacks", "resourcepacks",
               "kubejs", "scripts", "datapacks")


def _noop_progress(cur: int, total: int, note: str) -> None:
    """默认进度回调：什么都不做（可在 UI 里覆盖）。"""
    pass


def _noop_log(msg: str) -> None:
    """默认日志回调：打印到 stdout。"""
    print(msg, flush=True)


def _safe_join(root: Path, relative: str) -> Path:
    """把包内相对路径安全拼到 root 下，防止 ../ 越权写出根目录。"""
    rel = Path(relative.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"非法路径（拒绝越权写出）：{relative!r}")
    return root / rel


# ---------------------------------------------------------------------------
# 1) 解析 .mrpack
# ---------------------------------------------------------------------------
def parse_mrpack(mrpack: Path | str) -> dict:
    """打开 .mrpack，读取根目录的 modrinth.index.json，返回解析后的 dict。

    抛错：文件不存在、不是合法 zip、缺少 modrinth.index.json。
    """
    mrpack = Path(mrpack)
    if not mrpack.is_file():
        raise FileNotFoundError(f".mrpack 不存在: {mrpack}")
    try:
        with zipfile.ZipFile(mrpack, "r") as z:
            try:
                info = z.getinfo("modrinth.index.json")
            except KeyError:
                raise ValueError(
                    f"缺少 modrinth.index.json，不是有效的整合包: {mrpack.name}")
            data = z.read(info).decode("utf-8")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"不是有效的 zip/.mrpack 文件: {mrpack.name}") from exc
    try:
        return __import__("json").loads(data)
    except Exception as exc:
        raise ValueError(f"modrinth.index.json 解析失败: {exc}") from exc


def get_minecraft_version(index: dict) -> str:
    """读取整合包依赖的 MC 版本（dependencies.minecraft）。"""
    deps = index.get("dependencies") or {}
    return deps.get("minecraft") or ""


def get_mod_loaders(index: dict) -> list:
    """读取整合包依赖的加载器（dependencies 里非 minecraft 的键）。

    例：{"minecraft":"1.21.1","fabric-loader":"0.16.10"} ->
        ["fabric-loader-0.16.10"]；{"neoforge":"21.1.52"} -> ["neoforge-21.1.52"]
    """
    deps = index.get("dependencies") or {}
    out = []
    for key, val in deps.items():
        if key == "minecraft" or not val:
            continue
        out.append(f"{key}-{val}")
    return out


def list_files(index: dict) -> list:
    """返回 index["files"] 列表（每个元素含 path / hashes / env / downloads）。"""
    return index.get("files") or []


# ---------------------------------------------------------------------------
# 2) 下载文件（按路径分目录落盘）
# ---------------------------------------------------------------------------
def install_files(mrpack: Path | str, game_dir: Path | str, mc_version: str,
                  mods_dir: Optional[Path | str] = None,
                  progress_cb: Optional[Callable[[int, int, str], None]] = None,
                  log_cb: Optional[Callable[[str], None]] = None,
                  dry_run: bool = False) -> int:
    """下载 index["files"] 里的所有文件到对应目录。

    参数
    ----
    mrpack        : .mrpack 文件路径
    game_dir      : 游戏根目录（.minecraft）
    mc_version    : 要安装到的目标 MC 版本（用于把文件落进该版本的隔离目录；
                    若为空则落到 game_dir 根下的 mods/config）
    mods_dir      : 可选，mods 目录显式指定（优先于 mc_version 推断）
    progress_cb   : progress_cb(cur, total, note)  —— 文件级进度
    log_cb        : log_cb(msg)
    dry_run       : 只打印不下载（用于预览）

    返回下载成功的文件数。
    """
    progress_cb = progress_cb or _noop_progress
    log_cb = log_cb or _noop_log
    game_dir = Path(game_dir)

    # 目标 mods 目录：显式指定 > 版本隔离 > 游戏根目录
    if mods_dir:
        target_mods = Path(mods_dir)
    elif mc_version:
        target_mods = core._ensure_resource_dir(mc_version, "mods")
    else:
        target_mods = game_dir / "mods"

    index = parse_mrpack(mrpack)
    files = list_files(index)
    # [FIX-mrpack] 过滤掉整合包作者误打包的本地测试文件/异常路径（垃圾文件）
    #   - local/ 开头：作者本地测试残留，服务器上没有对应资源
    #   - 以 . 开头：隐藏文件（.gitkeep / .cache 等）
    #   - 含 _mod.jar：误打包的本地修改件
    def _is_junk(path_norm: str) -> bool:
        p = path_norm.lstrip("/")
        return (p.startswith("local/") or p.startswith(".")
                or "_mod.jar" in p)

    filtered = []
    skipped = 0
    for f in files:
        path = (f.get("path") or "").replace("\\", "/")
        if _is_junk(path):
            skipped += 1
            log_cb(f"[跳过] 发现无效或测试文件，已忽略: {path}")
            continue
        filtered.append(f)
    total = len(filtered)
    log_cb(f"开始安装整合包，共发现 {total} 个文件，已过滤 {skipped} 个异常路径，"
           f"mods 目标目录 {target_mods}")

    # ------------------------------------------------------------------
    # [FIX-mrpack] 多线程并发下载：max_workers=8 + 单请求 timeout=15
    #   - 8 并发：避免榨干带宽 / 触发 Modrinth/ForgeCDN 429 限流
    #   - 每个文件独立 try/except：个别失败（超时/WinError 5 等）不中断队列
    #   - 线程安全计数器（Lock）：每处理完 1 个文件（成功或跳过）
    #     调用 progress_cb(completed, total)，日志带 [completed/total] 进度
    #   - UI 层收到进度/日志后经 pyqtSignal 排队投递，多线程刷新不冲突
    # ------------------------------------------------------------------
    MAX_WORKERS = 8
    DOWNLOAD_TIMEOUT = 15
    completed = 0
    lock = threading.Lock()

    # 无下载地址的文件：直接在主线程记日志（不进线程池）
    valid_files = []
    no_url_skipped = 0
    for f in filtered:
        if f.get("downloads"):
            valid_files.append(f)
        else:
            no_url_skipped += 1
            p = (f.get("path") or "").replace("\\", "/")
            log_cb(f"跳过（无下载地址）: {p}")
    total = len(valid_files)

    if not valid_files:
        log_cb("无需下载的文件（全部跳过/无地址）")
        return 0

    ok = 0

    def download_single_file(file_info: dict) -> bool:
        """[worker 线程] 下载并写入单个文件；任何异常都隔离处理，返回成功标记。"""
        nonlocal completed, ok
        path = (file_info.get("path") or "").replace("\\", "/")
        name = Path(path).name or file_info.get("filename") or "file"
        urls = file_info.get("downloads") or []
        success = False
        if not urls:
            # [FIX-mrpack] 无下载地址：算处理过（不算失败），直接计数跳过
            with lock:
                completed += 1
                log_cb(f"[跳过] 无下载地址: {path}")
            return True
        try:
            dest = _route_dest(path, target_mods, game_dir)
            if dry_run:
                log_cb(f"[dry] 将下载 {name} -> {dest}")
                success = True  # 预览模式：算已处理
            else:
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    log_cb(f"正在下载: {name} -> {dest}")
                    # chunk 级进度回调：Net.download 传的是 chunk 数（非文件序号），
                    # 直接透传给上层 progress_cb（UI 层排队投递，多线程刷新不冲突）
                    _url = urls[0]
                    _name = name

                    def _file_prog(c, t, _name=_name):
                        progress_cb(c, t, f"下载 {_name} ...")

                    core.Net.download(_url, dest, progress_cb=_file_prog,
                                      timeout=DOWNLOAD_TIMEOUT)
                    success = True
                except Exception as exc:
                    # [FIX-mrpack] 单文件失败不中断队列：打错误日志后 continue
                    log_cb(f"[错误] 下载 {path} 失败: {exc}，已跳过该文件")
                    success = False
        except Exception as exc:
            # 路径越权（_safe_join）等结构性错误：同样隔离，不拖垮整个队列
            log_cb(f"[错误] 处理 {path} 失败: {exc}，已跳过该文件")
            success = False
        finally:
            # 线程安全地更新进度：每处理完 1 个文件（成功/失败/跳过）+1
            with lock:
                completed += 1
                if success:
                    ok += 1
                mark = "完成" if success else "失败"
                log_cb(f"[{completed}/{total}] {mark}: {path}")
                progress_cb(completed, total,
                           f"整合包文件 {completed}/{total} 已处理")
        return success

    log_cb(f"开始并发下载 {total} 个文件（最大并发 {MAX_WORKERS}，"
           f"单请求超时 {DOWNLOAD_TIMEOUT}s）...")
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=MAX_WORKERS,
            thread_name_prefix="mrpack-dl") as executor:
        list(executor.map(download_single_file, valid_files))
    log_cb("所有文件下载任务结束。")
    log_cb(f"文件下载完成：成功 {ok}/{total}，过滤 {skipped} 个，"
           f"跳过无下载地址 {no_url_skipped} 个")
    return ok


def _route_dest(path: str, target_mods: Path, game_dir: Path) -> Path:
    """按文件路径决定落盘位置。

    - mods/xxx.jar      -> target_mods/xxx.jar
    - config/xxx        -> <对应目录>/config/xxx
    - shaderpacks/...   -> <对应目录>/shaderpacks/...
    - 其它已知一级目录   -> <对应目录>/<原相对路径>
    - 其余任意路径      -> 按相对路径落到游戏根目录（保留层级）
    """
    norm = path.replace("\\", "/").lstrip("/")
    if not norm:
        raise ValueError("空文件路径")
    rel = Path(norm)
    top = rel.parts[0].lower() if rel.parts else ""

    # mods 特殊：统一进目标 mods 目录（版本隔离）
    if top == "mods":
        return target_mods / rel.name
    # 其它已知一级目录：优先落到版本隔离目录（若存在）
    if top in _KNOWN_DIRS:
        base = target_mods.parent  # target_mods 的父级（即版本目录或 game_dir）
        return base / rel
    # 未知路径：落到游戏根目录，保留相对层级
    return _safe_join(game_dir, norm)


# ---------------------------------------------------------------------------
# 3) 解压覆盖（Overrides）
# ---------------------------------------------------------------------------
def _open_zip_safe(mrpack: Path) -> zipfile.ZipFile:
    """安全打开 zip；强制读一次中央目录，0 字节/截断/损坏的包会立即抛错。"""
    z = zipfile.ZipFile(mrpack, "r")
    z.namelist()  # 触发 central directory 解析
    return z


def extract_overrides(mrpack: Path | str, game_dir: Path | str, mc_version: str,
                      log_cb: Optional[Callable[[str], None]] = None,
                      progress_cb: Optional[Callable[[int, int, str], None]] = None,
                      dry_run: bool = False) -> int:
    """把 .mrpack 里的 overrides/ 目录完整解压并覆盖到目标根目录。

    - 目标根：有 mc_version 时优先用 versions/<mc_version>/ 作为根
      （严格版本隔离），否则用 game_dir 本身。
    - 覆盖规则：同名文件直接覆盖（不询问），这正是整合包 Overrides 的语义。
    - 性能：整个循环只开一次 zip（旧实现每个文件重开一次，几千个
      overrides 文件时 O(n²) IO，表现为「进度满后卡死」）；
      每个文件写盘前打日志，卡住时能立刻看出卡在哪一个文件。
    返回解压的文件数。
    """
    log_cb = log_cb or _noop_log
    progress_cb = progress_cb or _noop_progress
    game_dir = Path(game_dir)

    # Overrides 落盘根：版本隔离优先
    if mc_version:
        root = Path(core.VERSIONS_DIR) / mc_version
    else:
        root = game_dir

    mrpack = Path(mrpack)
    prefix = "overrides/"
    entries = []
    try:
        with zipfile.ZipFile(mrpack, "r") as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                if not info.filename.startswith(prefix):
                    continue
                entries.append((info.filename, info.file_size))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"不是有效的 zip/.mrpack 文件: {mrpack.name}") from exc

    total = len(entries)
    count = 0
    log_cb(f"开始解压 Overrides：共 {total} 个文件，目标根目录 {root}")
    if not total:
        log_cb("Overrides 为空，跳过")
        return 0

    # 只开一次 zip，逐文件读 + 写盘
    z = _open_zip_safe(mrpack)
    try:
        for i, (zname, size) in enumerate(entries, 1):
            rel = zname[len(prefix):]
            try:
                dest = _safe_join(root, rel)
            except ValueError as exc:
                log_cb(f"跳过非法覆盖路径: {exc}")
                continue
            note = f"解压覆盖：{rel} (进度 {i}/{total})"
            progress_cb(i - 1, total, note)
            if dry_run:
                log_cb(f"[dry] 覆盖 {dest}")
                continue
            # 写盘前打日志：卡住时最后一条日志就是正在写的那个文件
            log_cb(f"解压 overrides [{i}/{total}]: {rel} ({size} 字节)")
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(zname) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
            except Exception as exc:
                log_cb(f"覆盖失败 {zname}: {exc!r}")
                continue
            count += 1
            progress_cb(i, total, note)
    finally:
        z.close()
    log_cb(f"Overrides 解压完成：{count}/{total} 个文件 -> {root}")
    return count


# ---------------------------------------------------------------------------
# 4) 一键安装：解析 -> 下载文件 -> 解压 Overrides
# ---------------------------------------------------------------------------
def install_mrpack(mrpack: Path | str, game_dir: Path | str,
                   mc_version: str = "",
                   mods_dir: Optional[Path | str] = None,
                   progress_cb: Optional[Callable[[int, int, str], None]] = None,
                   log_cb: Optional[Callable[[str], None]] = None,
                   dry_run: bool = False) -> dict:
    """完整安装一个 .mrpack 到指定 MC 实例/游戏目录。

    返回汇总 dict：
      {"minecraft": <MC 版本>, "loaders": [...], "files_ok": int,
       "overrides_ok": int}
    """
    progress_cb = progress_cb or _noop_progress
    log_cb = log_cb or _noop_log
    game_dir = Path(game_dir)

    index = parse_mrpack(mrpack)
    mc = get_minecraft_version(index)
    loaders = get_mod_loaders(index)
    log_cb(f"整合包依赖：Minecraft={mc or '未知'}，加载器={loaders or '无'}")

    # 没显式给 mc_version 时，用整合包声明的 MC 版本
    if not mc_version:
        mc_version = mc

    log_cb("=== 开始下载文件 ===")
    files_ok = install_files(mrpack, game_dir, mc_version, mods_dir,
                             progress_cb, log_cb, dry_run)
    log_cb("=== 开始解压 Overrides ===")
    overrides_ok = extract_overrides(mrpack, game_dir, mc_version,
                                     log_cb, progress_cb, dry_run)

    return {"minecraft": mc, "loaders": loaders,
            "files_ok": files_ok, "overrides_ok": overrides_ok}


# ---------------------------------------------------------------------------
# 独立命令行入口（便于测试）
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="安装 Modrinth .mrpack 整合包")
    ap.add_argument("mrpack", help=".mrpack 文件路径")
    ap.add_argument("--game-dir", help="游戏根目录（默认 .minecraft）", default=str(core.DEFAULT_MINECRAFT_DIR))
    ap.add_argument("--mc-version", default="", help="目标 MC 版本（版本隔离目录）")
    ap.add_argument("--mods-dir", default=None, help="显式指定 mods 目录")
    ap.add_argument("--dry-run", action="store_true", help="只预览不下载")
    args = ap.parse_args()

    try:
        result = install_mrpack(args.mrpack, args.game_dir, args.mc_version,
                                args.mods_dir, dry_run=args.dry_run)
        print("安装完成：", result)
    except Exception as exc:
        print(f"安装失败: {exc}", file=sys.stderr)
        sys.exit(1)