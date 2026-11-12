#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mc_launcher_qt.py —— PyQt6 版 Minecraft 启动器 UI
================================================
设计原则
--------
1. 底层逻辑 100% 复用 mc_launcher.py（Net / mcl_* / download_* / modrinth_* 等
   一行都不改），本文件只重写 UI 层。旧代码里定义在 LauncherApp 内部的实例方法
   （如 _clone_version_instance）不是模块级函数，这里在本文件内重写等价版本。
2. 后台线程 -> UI 的刷新全部走 UiBridge 的 pyqtSignal（跨线程自动排队），
   不再有 self.after_safe，也不会出现「后台下载卡死主界面」。
3. 列表统一用 QTableWidget（SelectRows + SingleSelection），选中项靠
   「行号 -> 数据对象」映射获取，不存在 Tkinter Treeview 的焦点/选中串台问题。

运行
----
    pip install PyQt6
    python mc_launcher_qt.py
"""
from __future__ import annotations
import subprocess
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDesktopServices, QIcon, QPixmap, QTextCursor
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QRadioButton, QScrollArea, QSizePolicy, QSpinBox, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import mc_launcher as core
import mrpack_installer  # [FIX-免登弹窗] 本地整合包导入（顶层 import 防 PyInstaller 漏收）
import requests          # [FIX-开服工具] MSL API（顶层 import，确保 PyInstaller 收集 requests/urllib3/certifi 进包）

# ---------------------------------------------------------------------------
# 常量 / 小工具
# ---------------------------------------------------------------------------
LOG_BUFFER = collections.deque(maxlen=3000)

# ---------------------------------------------------------------------------
# 版本号 / 自动更新
# ---------------------------------------------------------------------------
CURRENT_VERSION = "1.0.2"
GITHUB_REPO = "xp12-mcmin/PyMC-Launcher"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_RELEASES_PROXY = \
    f"https://ghproxy.net/https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
UPDATE_UA = "PyMC-Launcher/1.0"


def _compare_versions(v1: str, v2: str) -> int:
    """健壮的版本号比较：正确判定 1.0.10 > 1.0.2。

    - 忽略大小写与可选的 v 前缀；逐段按数字比较。
    - v1 > v2 返回 1；v1 < v2 返回 -1；相等返回 0。
    """
    parts1 = [int(x) for x in v1.lower().replace('v', '').split('.') if x.isdigit()]
    parts2 = [int(x) for x in v2.lower().replace('v', '').split('.') if x.isdigit()]
    length = max(len(parts1), len(parts2))
    parts1 += [0] * (length - len(parts1))
    parts2 += [0] * (length - len(parts2))
    for p1, p2 in zip(parts1, parts2):
        if p1 > p2:
            return 1
        if p1 < p2:
            return -1
    return 0


def open_url_safe(url: str):
    """使用最可靠的系统唤起方式打开网页链接。

    Windows 下完美复刻终端 `start` 命令，拉起默认浏览器；
    macOS / Linux 分别用 open / xdg-open。
    """
    try:
        if sys.platform.startswith("win"):
            # 完美复刻终端 start 命令，拉起默认浏览器
            subprocess.Popen(
                ["cmd", "/c", "start", "", url],
                shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url])
        else:
            subprocess.Popen(["xdg-open", url])
    except Exception as e:
        print(f"[打开网页异常] {e}")

def open_browser_as_normal(url):
    try:
        # 强制使用资源管理器(普通权限)去拉起浏览器，避开高权限拦截
        subprocess.Popen(["explorer.exe", url])
    except Exception as e:
        print(f"降权打开失败: {e}")
        # 备用方案：如果 explorer 抽风，退回 QDesktopServices
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl(url))
def launcher_dir() -> Path:
    """启动器自身所在的目录（兼容 PyInstaller 单文件/单目录两种模式）。

    - PyInstaller 打包（--onefile / --onedir）：外部资源（icon.png、
      authlib-injector.jar 等）必须放在 exe 所在目录，而不是 C 盘临时解压目录。
      单文件模式下 __file__ 指向 %TEMP%\\_MEIxxxx，必须改用 sys.executable。
    - 普通 .py 运行：取 __file__ 所在目录（= 脚本目录，行为不变）。
    """
    if getattr(sys, "frozen", False):
        # PyInstaller 打包：exe 所在目录（onedir 时为 _internal 旁的 exe 目录）
        exe_dir = Path(os.path.dirname(sys.executable))
        return exe_dir
    return Path(__file__).resolve().parent


def run_as_admin(cmd_str: str, wait: bool = False) -> int:
    """以管理员身份静默拉起命令（触发 UAC 弹窗）。

    [一键修复权限] 普通用户权限下 takeown/icacls 必然失败，必须用
    ctypes.windll.shell32.ShellExecuteW 的 "runas" 动词触发 UAC 提权。
    路径在 cmd_str 里用双引号包裹，空格/特殊字符交给 cmd 解析。

    参数：
      cmd_str: 完整命令字符串（例如 'takeown /F "C:\\..." & icacls "C:\\..." /grant ...'）
      wait:    True 时等待子命令跑完再返回（SW_SHOWNORMAL 可见窗口）；
               False 时立即返回（窗口后台跑完即关，nShowCmd=0 SW_HIDE 隐藏）。

    返回 ShellExecuteW 的句柄值：> 32 表示成功（用户点了"是"），
    <= 32 表示被拒绝或出错（用户点"否" / 无 shell / 参数错）。
    若被拒绝则抛 RuntimeError，调用方据此弹「取消」提示。
    """
    import ctypes
    handle = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "cmd.exe", f"/c {cmd_str}", None, 0 if not wait else 1)
    if handle <= 32:
        raise RuntimeError(f"UAC 提权被拒绝或失败，ShellExecuteW 返回码: {handle}")
    return handle


# [FIX-免责弹窗重复] 本地覆盖配置文件（启动器目录下）
# %APPDATA%\.pymc 在很多机器上被杀软/OneDrive 锁定不可写，
# save_config 会静默失败 -> 免责状态永远写不进 -> 每次下载都重复弹窗。
# 因此把「同意类」标记同时写进启动器目录的 local_cfg.json，
# 读取时两边合并，确保同意状态一定能持久化。
def _local_cfg_path() -> Path:
    return launcher_dir() / "local_cfg.json"


def _persist_local_override(key: str, value):
    """把单个配置项写进启动器目录的 local_cfg.json（失败静默）。"""
    try:
        p = _local_cfg_path()
        data = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
        data[key] = value
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                     encoding="utf-8")
        return True
    except Exception:
        return False


def _merge_local_cfg(cfg: dict) -> dict:
    """把 local_cfg.json 里的覆盖项合并进 cfg（启动器目录版优先）。"""
    try:
        p = _local_cfg_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    cfg[k] = v
    except Exception:
        pass
    return cfg


def resource_search_dirs() -> list[Path]:
    """外部资源搜索目录（按优先级）。

    onedir 模式下 PyInstaller 把 datas 文件放到 _internal，运行时 sys.executable
    是 exe；所以先查 exe 同级，再查 exe 下的 _internal（打包数据目录），
    最后回退到 BASE_DIR。onfile 模式下 _MEIPASS 等价于 _internal。
    """
    dirs = [launcher_dir()]
    if getattr(sys, "frozen", False):
        # onedir：datas 落在 _internal；onfile：落在 _MEIPASS
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            dirs.append(Path(meipass))
        else:
            dirs.append(launcher_dir() / "_internal")
    return dirs

LANGUAGES = [("简体中文", "zh_CN"), ("English", "en_US"), ("Deutsch", "de_DE"),
             ("Français", "fr_FR"), ("Español", "es_ES"), ("日本語", "ja_JP")]
LOADER_KINDS = [("forge", "Forge"), ("fabric", "Fabric"), ("neoforge", "NeoForge")]
MOD_LOADERS = ["all", "forge", "fabric", "neoforge", "quilt"]
SHADER_LOADERS = ["all", "iris", "optifine"]
LOADER_MC_PRESETS = ["1.12.2", "1.16.5", "1.17.1", "1.18.2", "1.19.2", "1.19.4",
                     "1.20.1", "1.20.2", "1.20.4", "1.20.6", "1.21.1", "1.21.4"]

# ---------------------------------------------------------------------------
# 外置登录（Yggdrasil 协议 / Authlib-Injector）
# ---------------------------------------------------------------------------
# LittleSkin 等皮肤站「拖拽到启动器」生成的字符串就长这样：
#   authlib-injector:yggdrasil-server:https%3A%2F%2Flittleskin.cn%2Fapi%2Fyggdrasil
# 冒号后的部分 urllib.parse.unquote() 解出来就是纯正的 Yggdrasil API 根地址。
AUTHLIB_URI_PREFIX = "authlib-injector:yggdrasil-server:"

# authlib-injector 元数据（latest.json）源，按顺序尝试：
#   官方根本不提供固定名字的 jar 直链，必须先读 latest.json 拿 download_url。
#   BMCLAPI 镜像放前面（国内快），官方 yushi.moe 兜底。
AUTHLIB_INJECTOR_META_URLS = [
    "https://bmclapi2.bangbang93.com/mirrors/authlib-injector/artifact/latest.json",
    "https://authlib-injector.yushi.moe/artifact/latest.json",
]
# 元数据全部失败时的硬编码回退（GitHub 特定版本，文件名必须带版本号）
AUTHLIB_INJECTOR_FALLBACK_URLS = [
    "https://github.com/yushijinhun/authlib-injector/releases/download/"
    "v1.2.7/authlib-injector-1.2.7.jar",
    "https://github.com/yushijinhun/authlib-injector/releases/download/"
    "v1.2.6/authlib-injector-1.2.6.jar",
]
# 环境变量显式指定优先（方便离线部署 / 内网镜像）
_ENV_INJECTOR = os.environ.get("AUTHLIB_INJECTOR_URL", "")
AUTHLIB_INJECTOR_URLS = ([_ENV_INJECTOR] if _ENV_INJECTOR else []) \
    + AUTHLIB_INJECTOR_FALLBACK_URLS
# 兼容旧名字（其它地方可能引用）
AUTHLIB_INJECTOR_URL = AUTHLIB_INJECTOR_URLS[0]
AUTHLIB_INJECTOR_NAME = "authlib-injector.jar"


def authlib_injector_path() -> Path:
    """authlib-injector.jar 的【首选】本地路径（BASE_DIR 根目录下）。

    注意：BASE_DIR 可能不可写（例如被系统策略保护），实际落盘位置请用
    authlib_injector_dest()，它会挑一个真正可写的目录。
    """
    return Path(core.BASE_DIR) / AUTHLIB_INJECTOR_NAME


def authlib_injector_search_paths() -> list[Path]:
    """authlib-injector.jar 的所有可能位置（按优先级）。

    顺序：BASE_DIR 根 -> 脚本所在目录 -> BASE_DIR/authlib_injector 旧缓存
          -> 脚本目录/authlib_injector -> 系统临时目录
    这样无论玩家把 jar 放在启动器目录还是 AppData 里，都能被找到并复用。
    """
    base = Path(core.BASE_DIR)
    # [FIX-截断恢复] PyInstaller 兼容：外部资源（authlib-injector.jar 等）
    # 指向 exe 所在目录（sys.executable），而不是 __file__（单文件时是 %TEMP% 解压目录）
    import tempfile
    return [
        base / AUTHLIB_INJECTOR_NAME,
        launcher_dir() / AUTHLIB_INJECTOR_NAME,
        *(d / AUTHLIB_INJECTOR_NAME for d in resource_search_dirs()),
        base / "authlib_injector" / AUTHLIB_INJECTOR_NAME,
        launcher_dir() / "authlib_injector" / AUTHLIB_INJECTOR_NAME,
        Path(tempfile.gettempdir()) / "pymc_authlib" / AUTHLIB_INJECTOR_NAME,
    ]


def authlib_injector_dest() -> Path:
    """挑一个【真正可写】的目录作为 jar 落盘位置。

    优先 BASE_DIR；不可写就退到脚本所在目录（用户看得见、好手动替换）；
    再不行退系统临时目录。返回完整文件路径（父目录已创建并验证可写）。
    """
    import tempfile
    base = Path(core.BASE_DIR)
    # [FIX-截断恢复] PyInstaller 兼容：优先 exe 所在目录（用户看得见、好手动替换），
    # 再查 _internal/_MEIPASS 数据目录
    script = launcher_dir()
    cand_dirs = [base, script, *(resource_search_dirs()),
                 Path(tempfile.gettempdir()) / "pymc_authlib"]
    for cand_dir in cand_dirs:
        try:
            cand_dir.mkdir(parents=True, exist_ok=True)
            probe = cand_dir / ".write_test_authlib"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            return cand_dir / AUTHLIB_INJECTOR_NAME
        except Exception:
            continue
    # 最后兜底：当前工作目录
    cwd = Path.cwd()
    return cwd / AUTHLIB_INJECTOR_NAME


def fetch_authlib_injector_url(log_cb=None) -> str:
    """读 latest.json 元数据，返回真正的 jar 下载地址。

    返回的 JSON 形如：
      {"build_number": 56, "version": "1.2.8",
       "download_url": "https://authlib-injector.yushi.moe/artifact/56/authlib-injector-1.2.8.jar",
       "checksums": {"sha256": "..."}}

    依次尝试 AUTHLIB_INJECTOR_META_URLS；全部失败后回退到
    AUTHLIB_INJECTOR_FALLBACK_URLS 里的 GitHub 固定版本直链。
    都拿不到返回 ""。
    """
    log_cb = log_cb or (lambda m: None)
    for meta_url in AUTHLIB_INJECTOR_META_URLS:
        try:
            log_cb(f"读取 authlib-injector 元数据: {meta_url}")
            data = core.Net.get_json(meta_url, timeout=20)
            url = (data.get("download_url") or "").strip()
            if not url:
                log_cb(f"  元数据缺少 download_url，跳过")
                continue
            ver = data.get("version") or "?"
            sha = ((data.get("checksums") or {}).get("sha256") or "")[:16]
            log_cb(f"  最新版本 {ver}（build {data.get('build_number', '?')}"
                   + (f", sha256 {sha}..." if sha else "") + "）")
            log_cb(f"  download_url = {url}")
            return url
        except Exception as exc:
            log_cb(f"  元数据获取失败: {exc}")
    if _ENV_INJECTOR:
        return _ENV_INJECTOR
    if AUTHLIB_INJECTOR_FALLBACK_URLS:
        log_cb(f"元数据全部不可用，回退 GitHub 固定版本: "
               f"{AUTHLIB_INJECTOR_FALLBACK_URLS[0]}")
        return AUTHLIB_INJECTOR_FALLBACK_URLS[0]
    return ""


def parse_authlib_uri(text: str) -> str:
    """从拖拽/粘贴的字符串里解析出 Yggdrasil API 根地址。

    支持三种输入：
      1. authlib-injector:yggdrasil-server:https%3A%2F%2Flittleskin.cn%2Fapi%2Fyggdrasil
         -> https://littleskin.cn/api/yggdrasil
      2. 已经是 https://littleskin.cn/api/yggdrasil（原样返回，去掉尾部斜杠）
      3. 纯域名 littleskin.cn -> 补成 https://littleskin.cn/api/yggdrasil
    解析不出返回 ""。
    """
    from urllib.parse import unquote, urlparse

    s = (text or "").strip()
    if not s:
        return ""
    # 1) 标准 authlib-injector URI（前面可能混了说明文字，取最后一次出现处）
    if AUTHLIB_URI_PREFIX in s:
        idx = s.rfind(AUTHLIB_URI_PREFIX)
        s = unquote(s[idx + len(AUTHLIB_URI_PREFIX):]).strip()
    # 去掉尾部粘连的说明文字 / 换行
    s = s.split()[0] if s.split() else ""
    if not s:
        return ""
    if not s.startswith(("http://", "https://")):
        # 纯域名或"域名/路径"：补协议；只有域名时补标准 api 路径
        host = s.strip("/")
        if "/" not in host:
            return f"https://{host}/api/yggdrasil"
        return f"https://{host}"
    if not urlparse(s).netloc:
        return ""
    return s.rstrip("/")


def yggdrasil_authenticate(server_url: str, username: str, password: str,
                           client_token: str = "", timeout: int = 20) -> dict:
    """按标准 Yggdrasil 协议向 {URL}/authserver/authenticate 换取 Token。

    请求体（严格按协议）：
      {
        "agent": {"name": "Minecraft", "version": 1},
        "username": "<邮箱或玩家名>",
        "password": "<密码>",
        "requestUser": True
      }
    Headers: Content-Type: application/json

    返回：
      {
        "accessToken":  str,   # 真实 accessToken
        "clientToken":  str,   # 服务器返回或沿用传入的 clientToken
        "uuid":         str,   # selectedProfile.id（已去掉横杠）
        "uuid_dashed":  str,   # selectedProfile.id（保留横杠，备用）
        "username":     str,   # selectedProfile.name（真实玩家名）
        "server":       str,   # 规范化后的 API 根地址
        "profiles":     list,  # availableProfiles -> [{"id": 无横杠uuid, "name": 角色名}]
        "selected":     str,   # selectedProfile.name（= 默认选中角色）
      }
    失败抛 RuntimeError（带服务器错误原文，便于定位 401/403）。
    """
    import requests
    from urllib.parse import urlparse

    base = parse_authlib_uri(server_url) or (server_url or "").strip().rstrip("/")
    if not base or not urlparse(base).netloc:
        raise RuntimeError(f"认证服务器地址无效：{server_url!r}")
    url = f"{base}/authserver/authenticate"
    payload = {
        "agent": {"name": "Minecraft", "version": 1},
        "username": username,
        "password": password,
        "requestUser": True,
    }
    if client_token:
        payload["clientToken"] = client_token

    print(f"[Yggdrasil] POST {url} (username={username})", flush=True)
    try:
        resp = requests.post(url, json=payload,
                             headers={"Content-Type": "application/json"},
                             timeout=timeout)
    except Exception as exc:
        raise RuntimeError(f"无法连接认证服务器 {url}：{exc}") from exc

    if resp.status_code != 200:
        # Yggdrasil 错误体：{"error": "...", "errorMessage": "..."}
        detail = resp.text[:600]
        try:
            j = resp.json()
            detail = f"{j.get('error', '')} {j.get('errorMessage', '')}".strip() or detail
        except Exception:
            pass
        if resp.status_code == 403:
            raise RuntimeError(
                f"认证被拒绝（403）：{detail}\n\n"
                f"常见原因：账号密码错误 / 该账号未在皮肤站创建角色 / 需要用邮箱登录。")
        raise RuntimeError(f"认证失败（HTTP {resp.status_code}）：{detail}")

    data = resp.json()
    access_token = data.get("accessToken") or data.get("access_token") or ""
    out_client_token = data.get("clientToken") or data.get("client_token") or client_token
    profile = data.get("selectedProfile") or {}

    if not access_token:
        raise RuntimeError(f"服务器未返回 accessToken，原始返回：{str(data)[:400]}")

    def _norm_uuid(raw: str) -> tuple[str, str]:
        """把 UUID 规范成 (无横杠小写, 带横杠) 两种形式。"""
        nod = (raw or "").replace("-", "").lower()
        if len(nod) == 32:
            return nod, (f"{nod[0:8]}-{nod[8:12]}-{nod[12:16]}-"
                         f"{nod[16:20]}-{nod[20:32]}")
        return (raw or "").replace("-", ""), raw or ""

    # ---- 解析 availableProfiles：该账号下的全部角色（name + id）----
    # Yggdrasil 标准返回形如：
    #   "availableProfiles": [{"id": "8位8位8位8位32hex", "name": "角色名"}, ...]
    # 有些皮肤站（或未选角色的账号）不返回它，或返回空数组 —— 用 selectedProfile 兜底。
    profiles: list[dict] = []
    for p in (data.get("availableProfiles") or []):
        if not isinstance(p, dict):
            continue
        pname = (p.get("name") or "").strip()
        pid, pid_dashed = _norm_uuid(p.get("id") or "")
        if not pname:
            continue
        profiles.append({"id": pid or pid_dashed, "name": pname})

    # ---- [FIX-登录态恢复] 选中角色：selectedProfile 优先；若为 null 则默认第一个 ----
    # 皮肤站常返回 selectedProfile=null 但 availableProfiles 有值。此时必须默认选中
    # 第一个角色，否则存进 config 的 uuid/username 落空，重启后登录态无法恢复。
    sel_id = (profile.get("id") or "").strip()
    sel_name = (profile.get("name") or "").strip()
    if not sel_id and not sel_name:
        if profiles:
            sel_id = profiles[0].get("id") or ""
            sel_name = profiles[0].get("name") or ""
        # 再兜底：顶层 uuid / name / 登录用户名
        if not sel_id:
            sel_id = data.get("uuid") or ""
        if not sel_name:
            sel_name = data.get("name") or username
    uuid_no_dash, uuid_dashed = _norm_uuid(sel_id)
    name = sel_name

    # 没有可用角色、但有选中名字：补一个 profile，保证下拉框有东西可显示
    if not profiles and name:
        profiles = [{"id": uuid_no_dash, "name": name}]

    print(f"[Yggdrasil] 登录成功: {name} uuid={uuid_no_dash or '(空)'} "
          f"角色数={len(profiles)}: "
          + ", ".join(f"{p['name']}({p['id'][:8]})" for p in profiles[:6]),
          flush=True)
    return {
        "accessToken": access_token,
        "clientToken": out_client_token,
        "uuid": uuid_no_dash,
        "uuid_dashed": uuid_dashed,
        "username": name,
        "server": base,
        "profiles": profiles,
        "selected": name,
    }


def yggdrasil_validate_token(server_url: str, access_token: str,
                             client_token: str = "", timeout: int = 12) -> dict:
    """校验 accessToken 是否还有效（启动前静默调用）。

    优先走 Yggdrasil 标准接口 POST {URL}/authserver/validate：
        body  {"accessToken": ..., "clientToken": ...}
        204 / 200 -> 有效；403 -> 失效
    若皮肤站没实现 validate（返回 404/405），退回 GET {URL}/authserver/validate
    或 GET {URL}/api/user/profile（带 Authorization: Bearer）。

    【容错策略（关键）】只有明确的 401/403 才判 valid=False；
    404 / 405 / 5xx / 超时 / 网络异常 / 接口未实现 一律 valid=None（无法判断，按有效放行）。
    皮肤站的 /validate 接口经常损坏或乱报 404/500，绝不能因为校验失败就清空玩家登录态。

    返回 {"valid": bool|None, "status": int, "message": str}
      valid=True  -> 确定有效
      valid=False -> 确定过期/无效（仅当 401/403 时）
      valid=None  -> 网络问题/无法判断（调用方应放行，不要误伤玩家）
    """
    import requests
    from urllib.parse import urlparse

    base = parse_authlib_uri(server_url) or (server_url or "").strip().rstrip("/")
    if not base or not urlparse(base).netloc:
        return {"valid": None, "status": 0, "message": f"服务器地址无效: {server_url!r}"}
    if not access_token:
        return {"valid": False, "status": 0, "message": "本地没有 accessToken"}

    # [FIX-登录态恢复] 只有 401/403 才算「明确失效」；其余（404/405/5xx/超时/异常）
    # 一律视为「无法判断」(valid=None)，让调用方放行。皮肤站的 /validate 经常是坏接口。
    def _classify(status_code: int, response_body: str, endpoint: str):
        """返回 None 表示「继续尝试下一个接口」；返回 dict 表示最终结论。"""
        if status_code in (200, 204):
            return {"valid": True, "status": status_code,
                    "message": f"Token 有效（{endpoint} 200）"}
        if status_code in (401, 403):
            return {"valid": False, "status": status_code,
                    "message": f"Token 已失效（{endpoint} HTTP {status_code}），"
                               f"返回内容: {response_body[:300]}"}
        # 404/405/5xx：接口不可用/服务端错误，无法判断 -> 继续下一接口
        return None

    # ---- 1) 标准：POST /authserver/validate ----
    try:
        r = requests.post(f"{base}/authserver/validate",
                          json={"accessToken": access_token,
                                "clientToken": client_token or ""},
                          headers={"Content-Type": "application/json"},
                          timeout=timeout)
        print(f"[Yggdrasil] validate POST -> HTTP {r.status_code}", flush=True)
        verdict = _classify(r.status_code, r.text, "POST /authserver/validate")
        if verdict:
            return verdict
        # 404/405/5xx：接口不可用，继续退回下一个接口
    except Exception as exc:
        print(f"[Yggdrasil] validate POST 异常: {exc}", flush=True)
        # 网络异常：不再直接返回，继续尝试 GET / profile

    # ---- 2) 退回 GET /authserver/validate ----
    try:
        r = requests.get(f"{base}/authserver/validate",
                         headers={"Authorization": f"Bearer {access_token}"},
                         timeout=timeout)
        print(f"[Yggdrasil] validate GET -> HTTP {r.status_code}", flush=True)
        verdict = _classify(r.status_code, r.text, "GET /authserver/validate")
        if verdict:
            return verdict
    except Exception as exc:
        print(f"[Yggdrasil] validate GET 异常: {exc}", flush=True)

    # ---- 3) 再退：GET /api/user/profile（带 Bearer）----
    try:
        r = requests.get(f"{base}/api/user/profile",
                         headers={"Authorization": f"Bearer {access_token}"},
                         timeout=timeout)
        print(f"[Yggdrasil] profile GET -> HTTP {r.status_code}", flush=True)
        verdict = _classify(r.status_code, r.text, "GET /api/user/profile")
        if verdict:
            return verdict
    except Exception as exc:
        print(f"[Yggdrasil] profile GET 异常: {exc}", flush=True)

    # 所有接口都失败（404/5xx/超时/网络异常）：无法判断 -> 按有效处理，放行
    return {"valid": None, "status": 0,
            "message": "所有校验接口不可用（皮肤站可能未实现 /validate，或网络问题），按有效处理"}


def _verify_jar(path: Path) -> bool:
    """粗校验：是不是一个可读的 zip/jar，且不是 404 的 HTML 页面。"""
    try:
        if not path.is_file() or path.stat().st_size < 100 * 1024:
            return False
        with zipfile.ZipFile(path, "r") as z:
            names = z.namelist()
        return "META-INF/MANIFEST.MF" in names
    except Exception:
        return False


def ensure_authlib_injector(progress_cb=None, log_cb=None,
                            status_cb=None) -> Path:
    """确保 authlib-injector.jar 已就位，返回【绝对路径】。

    查找顺序（任一处已有合法 jar 就直接复用，不联网）：
      <BASE_DIR>/authlib-injector.jar
      <脚本目录>/authlib-injector.jar
      <BASE_DIR>/authlib_injector/authlib-injector.jar（旧缓存兼容）
      <脚本目录>/authlib_injector/authlib-injector.jar
      系统临时目录/pymc_authlib/authlib-injector.jar

    下载流程（官方没有固定名字的直链，必须先读元数据）：
      1. 读 latest.json 元数据（BMCLAPI 镜像 -> 官方 yushi.moe）
      2. 从中取 download_url（形如 .../artifact/56/authlib-injector-1.2.8.jar）
      3. 下载该 URL，保存为 authlib-injector.jar
      4. 元数据全挂 -> 回退 GitHub 固定版本（文件名带版本号，如 v1.2.7）
      5. 下完做 zip 校验，防止把 404 HTML 当成 jar 存下来

    落盘目录用 authlib_injector_dest() 挑选真正可写的一个
    （BASE_DIR 在部分系统上不可写，否则会 ERRNO 13）。

    status_cb(msg) 用于刷 UI 状态栏（"正在下载 authlib-injector.jar..."）。
    """
    log_cb = log_cb or (lambda m: None)
    status_cb = status_cb or (lambda m: None)

    # ---- 0) 先找现成的（任何一处合法 jar 都直接复用）----
    for p in authlib_injector_search_paths():
        if _verify_jar(p):
            log_cb(f"authlib-injector 已就位: {p}（{p.stat().st_size // 1024} KB）")
            return p.resolve()

    dest = authlib_injector_dest()
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        log_cb(f"创建目录失败（继续尝试）: {exc}")
    log_cb(f"authlib-injector 落盘位置: {dest}")
    status_cb("正在获取 authlib-injector 版本信息 ...")

    # ---- 1) 元数据 -> download_url ----
    urls = []
    meta_url = fetch_authlib_injector_url(log_cb)
    if meta_url:
        urls.append(meta_url)
    # ---- 4) 回退：GitHub 固定版本（去重）----
    for u in AUTHLIB_INJECTOR_URLS:
        if u and u not in urls:
            urls.append(u)
    if not urls:
        raise RuntimeError(
            f"无法获取 authlib-injector 下载地址（元数据全部失败）\n"
            f"可手动下载后放到：{dest}")

    last = None
    for i, url in enumerate(urls, 1):
        try:
            log_cb(f"下载 authlib-injector ({i}/{len(urls)}): {url}")
            status_cb(f"正在下载 authlib-injector.jar ...（{i}/{len(urls)}）")
            # 注意：core.Net.download 内部已经写 .part 再原子替换，
            # 这里不能再给 dest 加 .part 后缀（否则会变成 xxx.jar.part.part）。
            core.Net.download(url, dest, progress_cb=progress_cb, timeout=180)
            if _verify_jar(dest):
                log_cb(f"authlib-injector 下载完成: {dest} "
                       f"（{dest.stat().st_size // 1024} KB）")
                status_cb("authlib-injector.jar 下载完成")
                return dest.resolve()
            size = dest.stat().st_size if dest.is_file() else 0
            log_cb(f"下载内容不是合法 jar（{size} 字节，可能是 404 页面），换下一个源")
            try:
                dest.unlink()
            except Exception:
                pass
        except Exception as exc:
            last = exc
            log_cb(f"源{i} 下载失败: {exc}")

    status_cb("authlib-injector.jar 下载失败")
    raise RuntimeError(
        f"authlib-injector 下载失败（{len(urls)} 个地址全都不行）：{last}\n\n"
        f"手动方式：\n"
        f"  1. 打开 https://authlib-injector.yushi.moe/artifact/latest.json\n"
        f"  2. 复制里面的 download_url 到浏览器下载\n"
        f"  3. 把下到的 jar 改名为 authlib-injector.jar，放到：\n     {dest}")


# ---------------------------------------------------------------------------
# 自定义实例克隆
# ---------------------------------------------------------------------------
# 注意：旧代码里 _clone_version_instance 是 LauncherApp 的【实例方法】，
# 不是 mc_launcher 的模块级函数，所以 core._clone_version_instance 不存在。
# 这里按原逻辑重写一份全局版本，UI 层统一调用它。
def _clone_version_instance(src_id: str, new_id: str) -> bool:
    """把 versions/<src_id>/ 完整克隆为 versions/<new_id>/。

    - 复制整个目录（含 jar / json / natives / mods 等）
    - 目标目录里的 <src_id>.json 改名为 <new_id>.json
    - 改写 json 内的 "id" 字段
    源目录保留不动；目标已存在或源不存在时返回 False。
    """
    src = core.VERSIONS_DIR / src_id
    dst = core.VERSIONS_DIR / new_id
    if not src.is_dir():
        print(f"[克隆实例] 源版本目录不存在: {src}", flush=True)
        return False
    if dst.exists():
        print(f"[克隆实例] 目标实例已存在: {dst}", flush=True)
        return False
    try:
        shutil.copytree(str(src), str(dst))
    except Exception as exc:
        print(f"[克隆实例] 复制失败: {exc}", flush=True)
        return False

    old_jf = dst / f"{src_id}.json"
    new_jf = dst / f"{new_id}.json"
    if old_jf.is_file():
        try:
            data = json.loads(old_jf.read_text(encoding="utf-8"))
            data["id"] = new_id
            new_jf.write_text(json.dumps(data, indent=2), encoding="utf-8")
            old_jf.unlink()
            print(f"[克隆实例] 已改写 {new_jf.name}", flush=True)
        except Exception as exc:
            print(f"[克隆实例] 改写 json 失败（实例仍可用）: {exc}", flush=True)
    else:
        print(f"[克隆实例] 未找到 {old_jf.name}，跳过 id 改写", flush=True)
    return True


def run_bg(fn, *args, **kwargs) -> threading.Thread:
    """启动 daemon 线程（所有耗时工作都从这里进）。"""
    t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# NeoForge / Forge 安装器调用（不经过 core，避免它把 stderr 吞掉）
# ---------------------------------------------------------------------------
NEOFORGE_MAVEN = "https://maven.neoforged.net/releases/net/neoforged/neoforge"
# maven.neoforged.net 在国内经常 connect timeout，安装器内部把下载地址写死在
# version.json / install_profile.json 里，CLI 的 --mirror 并不管这两个文件，
# 所以正确做法是「把安装器 jar 里的域名改掉」，见 patch_neoforge_installer()。
NEOFORGE_CDN = "neoforged.forgecdn.net/releases"
NEOFORGE_MAVEN_MIRRORS = [
    f"https://{NEOFORGE_CDN}/net/neoforged/neoforge",
    "https://bmclapi2.bangbang93.com/maven/net/neoforged/neoforge",
    "https://repo.huaweicloud.com/maven2/net/neoforged/neoforge",
]
# 安装器里这些文本资源才可能写死下载域名
_PATCHABLE_SUFFIXES = (".json", ".properties", ".txt", ".xml")


def ensure_launcher_profiles(mc_dir: Path) -> None:
    """安装器要求游戏目录里有 launcher_profiles.json，缺了就补一个空的。

    NeoForge/Forge 安装器开场就会检查这个文件，没有会直接报
    "There is no minecraft launcher profile in ... you need to run the launcher first!"
    然后 exit 1 —— 这是最常见、也最容易被误判成「参数不对」的失败。
    游戏目录本身不存在时也一并创建（安装器不会自己建）。
    """
    mc_dir = Path(mc_dir)
    path = mc_dir / "launcher_profiles.json"
    if path.is_file():
        return
    try:
        mc_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"profiles": {}, "settings": {}, "version": 3},
                                   ensure_ascii=False, indent=4), encoding="utf-8")
        print(f"[安装器] 已补建 {path}", flush=True)
    except Exception as exc:
        print(f"[安装器] 补建 launcher_profiles.json 失败: {exc}", flush=True)


def installer_temp_dir(preferred: Path | None = None) -> Path:
    """安装器补丁用的临时目录：优先 BASE_DIR，不可写就退到系统临时目录。"""
    import tempfile
    for cand in ([preferred] if preferred else []) + [
            Path(core.BASE_DIR) / "installer_tmp",
            Path(tempfile.gettempdir()) / "pymc-installer"]:
        if cand is None:
            continue
        try:
            Path(cand).mkdir(parents=True, exist_ok=True)
            probe = Path(cand) / ".write_test"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            return Path(cand)
        except Exception:
            continue
    # 兜底：退到进程工作目录下的相对目录（一定可写）
    fallback = Path(".") / "pymc-installer"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def patch_neoforge_installer(original_jar, output_jar, log=None) -> int:
    """把 NeoForge 安装器 jar 里写死的 maven.neoforged.net 换成 ForgeCDN。

    安装器运行时完全按 version.json / install_profile.json 里的 url 去下载
    （里面写死了 maven.neoforged.net/releases），国内直连会 SocketTimeout，
    最终以 exit 1 结束。CLI 的 --mirror 只影响极少数路径，靠它救不回来。

    做法就是**物理换文件**：读出原 jar 的所有条目，把文本资源里的域名改写，
    再写出一个全新的 patched jar（条目顺序、其余字节一律不动，保证
    `java -jar` 能正常启动）。返回替换次数；0 表示没有可替换的地址。
    """
    original_jar = Path(original_jar)
    output_jar = Path(output_jar)
    output_jar.parent.mkdir(parents=True, exist_ok=True)
    if output_jar.exists() and original_jar.resolve() == output_jar.resolve():
        raise ValueError("输出 jar 不能覆盖原始安装器")

    new_data = []
    hits = 0
    with zipfile.ZipFile(original_jar, "r") as zin:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.endswith(_PATCHABLE_SUFFIXES):
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    pass
                else:
                    if "maven.neoforged.net" in text:
                        hits += text.count("maven.neoforged.net")
                        text = text.replace("maven.neoforged.net/releases",
                                            NEOFORGE_CDN)
                        data = text.encode("utf-8")
            new_data.append((item.filename, data))

    # 写新文件：先写 .part 再原子替换，避免半成品 jar 被拿去执行
    tmp_out = output_jar.with_suffix(output_jar.suffix + ".part")
    with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in new_data:
            zout.writestr(name, data)
    os.replace(tmp_out, output_jar)

    # 立刻自检：能打开、MANIFEST 还在、官方域名已清零
    with zipfile.ZipFile(output_jar) as z:
        names = z.namelist()
        if "META-INF/MANIFEST.MF" not in names:
            raise RuntimeError(f"补丁后的 {output_jar.name} 丢失 MANIFEST，拒绝使用")
        for probe in ("version.json", "install_profile.json"):
            if probe in names:
                txt = z.read(probe).decode("utf-8", "replace")
                if "maven.neoforged.net" in txt:
                    raise RuntimeError(
                        f"补丁后的 {output_jar.name} 里 {probe} 仍有官方域名，拒绝使用")

    if log:
        log(f"[安装器补丁] {original_jar.name} -> {output_jar.name}: "
            f"替换 {hits} 处 maven.neoforged.net -> {NEOFORGE_CDN}")
    return hits


def download_installer_jar(loader_ver: str, dest: Path, log, progress_cb=None) -> Path:
    """下载 neoforge-<ver>-installer.jar：优先 ForgeCDN（官方源国内必超时）。"""
    fname = f"neoforge-{loader_ver}-installer.jar"
    if dest.is_file() and dest.stat().st_size > 1024 * 100:
        log(f"安装器已存在，复用: {dest}")
        return dest
    urls = [f"{u}/{loader_ver}/{fname}" for u in NEOFORGE_MAVEN_MIRRORS + [NEOFORGE_MAVEN]]
    last = None
    for i, url in enumerate(urls, 1):
        try:
            log(f"下载安装器 (源{i}): {url}")
            core.Net.download(url, dest, progress_cb, timeout=180)
            log(f"安装器下载完成: {dest.name}（{dest.stat().st_size // 1024} KB）")
            return dest
        except Exception as exc:
            last = exc
            log(f"源{i} 下载失败: {exc}")
    raise RuntimeError(f"安装器下载失败（全部源均不可用）: {last}")


def install_neoforge_patched(mc_version: str, mc_dir: Path, loader_ver: str,
                             java, log, progress_cb=None) -> str:
    """NeoForge 安装：物理打补丁后**直接执行补丁版安装器**。

    严格按「下载 -> 用 zipfile 打补丁成新文件 -> subprocess 跑新文件」执行，
    不再走 minecraft_launcher_lib 的 NeoForge 安装分支（它会把官方原版
    neoforge-installer.jar 直接拿去跑，内部请求必然 SocketTimeout）。

    流程（与 lib 官方实现等价，但换掉了安装器本体）：
      1. 补 launcher_profiles.json（安装器硬性要求）
      2. 确认原版 MC 本体已安装
      3. 下载原版安装器 -> patch_neoforge_installer() 生成 patched jar
      4. `java -jar <patched> --install-client <mc_dir>`（cwd 用临时目录）
      5. 按 neoforge-<ver> 版本 id 补全 libraries / assets
    返回最终安装的版本 id（neoforge-<loader_ver>）。
    """
    mc_dir = Path(mc_dir)
    ensure_launcher_profiles(mc_dir)
    tempdir = installer_temp_dir()
    installed_id = f"neoforge-{loader_ver}"

    def cb_status(msg):
        log(msg)
        if progress_cb:
            progress_cb(0, 0, msg)

    # 1) 本体
    cb_status(f"检查 Minecraft {mc_version} 本体...")
    core.mcl_install_minecraft(mc_version, mc_dir,
                               core._make_mcl_callback(progress_cb=progress_cb))

    # 2) 下载原版安装器
    original = tempdir / f"neoforge-{loader_ver}-installer.jar"
    download_installer_jar(loader_ver, original, log, progress_cb)

    # 3) 物理打补丁 -> 全新文件
    patched = tempdir / f"neoforge-{loader_ver}-installer-patched.jar"
    hits = patch_neoforge_installer(original, patched, log)
    if not hits:
        log("[安装器补丁] 未发现需要替换的地址，将直接使用原版安装器")
        patched = original
    else:
        log(f"[安装器补丁] 已生成补丁安装器: {patched}")

    # 4) 直接执行补丁版安装器（这一步是真正生效的地方）
    cb_status("运行补丁版 NeoForge 安装器...")
    run_installer_jar_qt(java, patched, ["--install-client", str(mc_dir)],
                          tempdir, log)
    log("[安装器补丁] 安装器执行成功")

    # 5) 补全该版本的 libraries / assets
    cb_status(f"补全 {installed_id} 的库文件...")
    core.mcl_install_minecraft(installed_id, mc_dir,
                               core._make_mcl_callback(progress_cb=progress_cb))
    log(f"[安装器补丁] {installed_id} 安装完成")
    return installed_id


def run_installer_jar_qt(java: str, jar: Path, args: list, cwd: Path,
                         log, timeout: int = 1800) -> int:
    """运行 Forge/NeoForge 安装器，实时把 stdout+stderr 转发到日志。

    关键点（旧实现踩的坑）：
      1. NeoForge 安装器把进度和错误全打到 **stdout**，旧代码只截 stderr[:200]，
         所以永远看到一句没用的「安装失败」。
      2. `java` 必须是用户选中的那个 Java（安装器要用 Java 跑 installertools，
         Java 版本过低会在 processor 阶段 exit 1）。
      3. 出错时要把最近若干行 + installer.log 一起回传，否则无法定位。
    """
    from collections import deque

    ensure_launcher_profiles(cwd)
    tail = deque(maxlen=120)
    cmd = [str(java), "-jar", str(jar)] + [str(a) for a in args]
    log("执行: " + " ".join(cmd))
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    started = time.time()
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        log("  " + line)
        if time.time() - started > timeout:
            proc.kill()
            raise RuntimeError(f"安装器超过 {timeout}s 未完成，已终止")
    proc.wait()

    if proc.returncode != 0:
        # 安装器自己会把详细堆栈写进 cwd/installer.log（也可能叫 xxx-installer.jar.log）
        extra = []
        log_name = ""
        for name in ("installer.log", f"{Path(jar).name}.log", "installer.jar.log"):
            p = Path(cwd) / name
            if p.is_file():
                try:
                    extra = p.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
                    log_name = name
                except Exception:
                    pass
                break
        detail = "\n".join(list(tail)[-40:]) or "（安装器没有输出）"
        if extra:
            detail += f"\n\n--- {log_name}（末尾）---\n" + "\n".join(extra)
        raise RuntimeError(
            f"安装器返回 {proc.returncode}\n\n--- 安装器输出（末尾）---\n{detail}"
        )
    return proc.returncode


class _SubprocessTee:
    """把 minecraft_launcher_lib 内部的 subprocess.run 换成「带日志 + 带错误输出」的版本。

    库源码在这些地方写成 subprocess.run(..., stdout=PIPE, stderr=PIPE, check=True)，
    出错时只抛出 CalledProcessError，真正的报错信息被丢掉了（NeoForge 安装器把
    错误全打在 stdout，所以只看 stderr 永远是空的）。
    """

    def __init__(self, real_module, log):
        self._real = real_module
        self._log = log

    def __getattr__(self, item):
        return getattr(self._real, item)

    def run(self, cmd, **kwargs):
        # 只接管「跑 java 子进程」的调用，其它调用保持原样
        try:
            first = os.path.basename(str(cmd[0])).lower()
        except Exception:
            return self._real.run(cmd, **kwargs)
        if first not in ("java", "java.exe", "javaw", "javaw.exe"):
            return self._real.run(cmd, **kwargs)

        check = kwargs.pop("check", False)
        timeout = kwargs.pop("timeout", None)
        kwargs.pop("capture_output", None)
        kwargs.pop("stdout", None)
        kwargs.pop("stderr", None)
        kwargs.pop("text", None)
        kwargs.pop("encoding", None)
        kwargs.pop("errors", None)
        self._log("执行: " + " ".join(str(c) for c in cmd))
        proc = subprocess.Popen(
            list(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            **kwargs,
        )
        tail = []
        timed_out = False
        started = time.time()
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                tail.append(line)
                self._log("  " + line)
                if len(tail) > 200:
                    del tail[:100]
            if timeout and time.time() - started > timeout:
                timed_out = True
                proc.kill()
                break
        proc.wait()
        if timed_out:
            raise subprocess.TimeoutExpired(cmd, timeout, output="\n".join(tail))
        if proc.returncode != 0:
            detail = "\n".join(tail[-30:]) or "（安装器没有输出）"
            if check:
                raise subprocess.CalledProcessError(
                    proc.returncode, cmd,
                    output=detail,
                    stderr=detail,
                )
            self._log(f"[警告] 子进程返回 {proc.returncode}:\n{detail}")
        return subprocess.CompletedProcess(list(cmd), proc.returncode,
                                           stdout="\n".join(tail), stderr="")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def install_mod_loader_captured(kind: str, mc_version: str, mc_dir: Path,
                                loader_ver: str, java, log, progress_cb=None,
                                tmp_dir: Path | None = None) -> str:
    """安装 Forge / NeoForge / Fabric，并保证安装器输出不丢。

    - NeoForge：**完全不经过 lib 的 NeoForge 安装分支**，改走
      install_neoforge_patched()：下载原版安装器 -> zipfile 打补丁成新文件 ->
      直接 subprocess 执行那个新文件。这样安装器内部请求走 ForgeCDN，不会超时。
    - Forge / Fabric：临时接管库内部 `subprocess.run`，把安装器 stdout+stderr
      实时转发到 UI 日志，失败时抛出带「安装器末尾输出」的 RuntimeError。
    - 安装前自动补 launcher_profiles.json（安装器硬性要求）。
    """
    ensure_launcher_profiles(Path(mc_dir))

    if kind == "neoforge":
        return install_neoforge_patched(mc_version, Path(mc_dir), loader_ver,
                                        java, log, progress_cb)

    saved = _tee_mod_loader_subprocess(log)
    try:
        return core.mcl_install_loader(kind, mc_version, mc_dir,
                                       java_path=java,
                                       loader_version=loader_ver,
                                       progress_cb=progress_cb)
    except subprocess.CalledProcessError as exc:
        detail = (exc.output or exc.stderr or "").strip()
        raise RuntimeError(
            f"{kind} 安装器执行失败（退出码 {exc.returncode}）\n\n"
            f"--- 安装器输出（末尾）---\n{detail or '（无输出）'}\n\n"
            f"常见原因：\n"
            f"  1. 游戏目录缺少 launcher_profiles.json（已自动补建）\n"
            f"  2. Java 版本过低 —— NeoForge 21.x 需要 Java 21+\n"
            f"  3. 网络无法访问安装器所需下载源\n"
            f"  4. 游戏目录被占用（先关闭正在运行的 Minecraft）"
        ) from exc
    finally:
        for mod, real in saved:
            mod.subprocess = real


def _tee_mod_loader_subprocess(log):
    """接管 lib 里 Forge/NeoForge 模块的 subprocess，捕获安装器输出。"""
    mods = []
    for name in ("_neoforge", "_forge"):
        try:
            mod = __import__(f"minecraft_launcher_lib.mod_loader.{name}",
                             fromlist=["subprocess"])
            mods.append(mod)
        except Exception:
            continue
    saved = []
    for mod in mods:
        saved.append((mod, mod.subprocess))
        mod.subprocess = _SubprocessTee(mod.subprocess, log)
    return saved


def elide_middle(text: str, limit: int = 48) -> str:
    """把过长的文字中间截断（保留盘符/开头和文件名结尾），避免撑破控件。"""
    text = str(text)
    if len(text) <= limit:
        return text
    keep = max(8, limit - 3)
    head = keep // 2
    tail = keep - head
    return f"{text[:head]}…{text[-tail:]}"


# ---------------------------------------------------------------------------
# 后台 -> 主线程 的唯一通道
# ---------------------------------------------------------------------------
class UiBridge(QObject):
    """所有后台线程只允许 emit 这里的信号，绝不直接碰控件。"""

    log = pyqtSignal(str)                 # 通用日志
    status = pyqtSignal(str)              # 底部状态文字
    progress = pyqtSignal(int, int, str)  # cur, total, note；total<=0 表示不定进度
    progressReset = pyqtSignal()
    busy = pyqtSignal(bool)

    javaReady = pyqtSignal(list)          # [(版本, 路径)]
    versionsReady = pyqtSignal(list)      # [版本 id]
    onlineReady = pyqtSignal(list)        # [(id, type, url)]
    mcVersionsReady = pyqtSignal(object, list)  # combo, [官方原版版本号]

    loadersReady = pyqtSignal(str, list)  # kind, [(ver, ...)]
    # hits 信号：(hits, page) —— 每次返回「某一页」的结果 + 该页页号。
    # 渲染器永远整页替换（不清空累积），并按页缓存：命中缓存时直接渲染，零网络。
    modHits = pyqtSignal(list, int)
    shaderHits = pyqtSignal(list, int)
    modpackHits = pyqtSignal(list, int)
    modResolved = pyqtSignal(str, object)       # project_id, vinfo
    shaderResolved = pyqtSignal(str, object)
    modpackResolved = pyqtSignal(str, object)
    # 资源包（resourcepack）：无前置依赖，逻辑与光影一致
    respackHits = pyqtSignal(list, int)
    respackResolved = pyqtSignal(str, object)
    iconReady = pyqtSignal(str)                 # 图标缓存文件路径

    gameStarted = pyqtSignal(str)               # version_id
    gameLog = pyqtSignal(str, str)              # version_id, text
    gameExited = pyqtSignal(str, int)           # version_id, returncode
    launchBusy = pyqtSignal(bool)               # 启动专用忙碌（只控「启动」按钮，不碰下载）

    # 外置登录结果（后台线程 -> 主线程）：
    # payload = {"ok": bool, "session": dict|None, "profiles": [...], "error": str}
    # ★ 必须走信号！Qt 控件只能在主线程操作，后台线程直接 addItem 会静默失效，
    #   这正是「登录成功但下拉框里没有角色」的根因。
    authReady = pyqtSignal(object)
    # Token 过期（启动前校验失败）-> 主线程弹提示并把 UI 切回未登录
    tokenInvalid = pyqtSignal(str)

    # ---- 【开服工具】服务端实时日志 / 退出信号（后台读线程 -> 主线程）----
    srvConsole = pyqtSignal(str)                # 服务端 stdout 一行文本
    srvExited = pyqtSignal(int)                # 服务端进程退出码

    # ---- 【开服工具】MSL 请求结果信号（后台 -> 主线程，绝不在后台碰 UI）----
    srvTypesReady = pyqtSignal(list)             # 服务端类型列表
    srvVersionsReady = pyqtSignal(list, str)     # (版本列表, 描述文本)
    srvDownloadReady = pyqtSignal(str, str)      # (直接下载的URL, 保存的完整路径)

    # ---- 【系统维护】垃圾清理 / 内存释放 信号（后台 -> 主线程，绝不在后台碰 UI）----
    # 统计完垃圾：files=文件数, total_bytes=总字节数。主线程槽里弹确认框。
    junkScanned = pyqtSignal(int, int)
    # 删除完：freed_bytes=释放字节, skipped=被占用跳过的个数, total=原统计文件数。
    junkCleaned = pyqtSignal(int, int, int)
    # 内存释放完：before/after 系统内存占用百分比。主线程槽里刷新 Label + 弹窗。
    memCleared = pyqtSignal(int, int)

    # ---- 【Java 下载失败】后台下载全候选失败 -> 主线程槽弹询问框 ----
    # 信号参数：目标文件名, 错误摘要, 候选数, 官方下载页 URL
    javaDownloadFailed = pyqtSignal(str, str, int, str)
    # ---- 【一键修复权限完成】后台 takeown/icacls 结束 -> 主线程复位按钮 ----
    permsFixedDone = pyqtSignal()

    error = pyqtSignal(str, str)                # 标题, 内容（QMessageBox.critical）
    warn = pyqtSignal(str, str)                 # 标题, 内容（QMessageBox.warning）
    info = pyqtSignal(str, str)                 # 标题, 内容（QMessageBox.information）
    # 发现新版本：主线程弹「是否前往 GitHub」确认框，确认则打开 html_url
    ask_open_url = pyqtSignal(str, str)         # remote_tag, html_url


# ---------------------------------------------------------------------------
# 「下载类型」对话框：原版 / Forge / Fabric / NeoForge + 自定义实例名
# ---------------------------------------------------------------------------
class LoaderPickDialog(QDialog):
    """对应原 Tk 的 _pick_loader_dialog()，返回 (kind, loader_ver, custom_name)。"""

    loadersLoaded = pyqtSignal(str, list)   # kind, [(ver, ...), ...]

    def __init__(self, parent, version_id: str, is_detail: bool = True):
        super().__init__(parent)
        self.version_id = version_id
        self.mc_ver, _ = core.parse_mc_version(version_id)
        self.result_value = None
        self._kind = "none"
        self._items: list = []

        # 标题/提示一律显示「纯原版号」（self.mc_ver），绝不显示实例名（如 1.20.1-Forge_47.4.18）
        display_ver = self.mc_ver or version_id
        self.setWindowTitle(f"{display_ver} — 选择下载类型" if is_detail
                            else f"下载 {display_ver} 完成 — 选择 Mod 加载器")
        self.setModal(True)
        self.setMinimumWidth(460)

        root = QVBoxLayout(self)
        head = QLabel(
            f"版本 {display_ver}。\n"
            f"请选择下载类型：原版（不装加载器）或 Mod 加载器\n"
            f"原版=纯客户端；选加载器会先下载本体再安装加载器。"
        )
        head.setWordWrap(True)
        root.addWidget(head)

        grid = QGridLayout()
        self.kind_group = QButtonGroup(self)
        self.kind_group.setExclusive(True)   # 单选：四选一，绝不多选
        self.rb_none = QRadioButton("原版（不装加载器）")
        self.rb_none.setChecked(True)
        self.rb_loaders = {}
        for i, (kind, label) in enumerate(LOADER_KINDS):
            rb = QRadioButton(label)
            self.kind_group.addButton(rb)
            self.rb_loaders[kind] = rb
            rb.toggled.connect(
                lambda checked, k=kind: checked and self._refresh_items(k))
            grid.addWidget(rb, i + 1, 0)
        grid.addWidget(self.rb_none, 0, 0)
        # 关键修复：把“原版”也加进同一组，才能和 Forge/Fabric/NeoForge 互斥单选
        self.kind_group.addButton(self.rb_none)
        self.rb_none.toggled.connect(
            lambda checked: checked and self._refresh_items("none"))

        self.ver_combo = QComboBox()
        self.ver_combo.setMinimumWidth(220)
        self.ver_combo.setEnabled(False)
        self.ver_combo.addItem("(原版无需加载器版本)")
        # 排版修复：把「加载器版本」标签 + 下拉框放进第 2 列、跨全部 4 行，
        # 并上下居中，与左侧 4 个单选按钮对齐，不再错位。
        ver_col = QVBoxLayout()
        ver_col.addStretch(1)
        ver_row = QHBoxLayout()
        ver_row.addWidget(QLabel("加载器版本"))
        ver_row.addWidget(self.ver_combo, 1)
        ver_col.addLayout(ver_row)
        ver_col.addStretch(1)
        grid.addLayout(ver_col, 0, 1, 4, 1)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("自定义实例名（可选）"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("留空 = 使用原版本名；重名会提示换名")
        name_row.addWidget(self.name_edit, 1)
        root.addLayout(name_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("下一步")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("关闭")
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.loadersLoaded.connect(self._on_loaders_loaded)

    # -- 拉取加载器版本（后台） --
    def _refresh_items(self, kind: str):
        self._kind = kind
        self._items = []
        if kind == "none":
            self.ver_combo.clear()
            self.ver_combo.addItem("(原版无需加载器版本)")
            self.ver_combo.setEnabled(False)
            return
        self.ver_combo.clear()
        self.ver_combo.addItem("加载中...")
        self.ver_combo.setEnabled(False)

        def work():
            try:
                items = core.mcl_loader_versions(kind, self.mc_ver)
            except Exception as exc:
                print(f"[加载器列表] {kind} (MC {self.mc_ver}) 获取失败: {exc}", flush=True)
                items = []
            try:
                self.loadersLoaded.emit(kind, list(items))
            except Exception:
                pass  # 对话框已销毁

        run_bg(work)

    @pyqtSlot(str, list)
    def _on_loaders_loaded(self, kind: str, items: list):
        if kind != self._kind:
            return
        self._items = items
        self.ver_combo.clear()
        if not items:
            self.ver_combo.addItem("(未获取到，可先装原版)")
            self.ver_combo.setEnabled(False)
            return
        self.ver_combo.addItems([it[0] for it in items])
        self.ver_combo.setCurrentIndex(0)
        self.ver_combo.setEnabled(True)

    def _on_ok(self):
        custom = self.name_edit.text().strip()
        if self._kind == "none" or not self._items:
            self.result_value = (None, None, custom)
        else:
            loader_ver = self.ver_combo.currentText() or self._items[0][0]
            self.result_value = (self._kind, loader_ver, custom)
        self.accept()

    def exec_and_get(self):
        self.exec()
        return self.result_value


# ---------------------------------------------------------------------------
# Java 大版本选择对话框
# ---------------------------------------------------------------------------
class JavaPickDialog(QDialog):
    MAJORS = [8, 11, 16, 17, 21, 25]

    def __init__(self, parent, recommended: int):
        super().__init__(parent)
        self.setWindowTitle("选择要下载的 Java 大版本")
        self.setModal(True)
        self.result_value = None
        root = QVBoxLayout(self)
        root.addWidget(QLabel("选择要下载的 Java 大版本（Adoptium Temurin JRE）："))
        self.group = QButtonGroup(self)
        for mj in self.MAJORS:
            note = "（当前版本推荐）" if mj == recommended else ""
            rb = QRadioButton(f"Java {mj}{note}")
            rb.setProperty("major", mj)
            self.group.addButton(rb)
            root.addWidget(rb)
            if mj == (recommended if recommended in self.MAJORS else 17):
                rb.setChecked(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("下载")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _on_ok(self):
        btn = self.group.checkedButton()
        self.result_value = int(btn.property("major")) if btn else None
        self.accept()

    def exec_and_get(self):
        self.exec()
        return self.result_value


# ---------------------------------------------------------------------------
# 版本 Mod 管理器
# ---------------------------------------------------------------------------
class ModManagerDialog(QDialog):
    """管理某个已安装版本的专属 mods 目录（versions/<版本>/mods/）。

    功能：
    - 列出该版本 mods 目录下所有 .jar
    - 【添加 Mod】：文件选择框选 jar -> 复制进 mods 目录 -> 刷新
    - 【删除选中 Mod】：删除选中的 jar -> 刷新
    - 【打开 mods 文件夹】：用系统文件管理器打开该目录
    """

    def __init__(self, parent, version_id: str):
        super().__init__(parent)
        self.version_id = version_id
        self.setWindowTitle(f"管理 Mod - {version_id}")
        self.setModal(True)
        self.resize(620, 460)
        self.setMinimumSize(520, 380)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)

        head = QLabel(f"版本专属 mods 目录：\n{self._mods_dir()}")
        head.setWordWrap(True)
        head.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(head)

        self.table = QTableWidget(0, 1, self)
        self.table.setHorizontalHeaderLabels(["Mod 文件 (.jar)"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        root.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.btn_add = QPushButton("添加 Mod…")
        self.btn_add.clicked.connect(self._add_mod)
        self.btn_del = QPushButton("删除选中 Mod")
        self.btn_del.clicked.connect(self._delete_selected)
        self.btn_open = QPushButton("打开该版本 mods 文件夹")
        self.btn_open.clicked.connect(self._open_folder)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.accept)
        for b in (self.btn_add, self.btn_del, self.btn_open, self.btn_close):
            row.addWidget(b)
        row.addStretch(1)
        root.addLayout(row)

        self.refresh()

    def _mods_dir(self) -> Path:
        return Path(core.VERSIONS_DIR) / self.version_id / "mods"

    def _jar_files(self) -> list:
        d = self._mods_dir()
        if not d.is_dir():
            return []
        return sorted([f for f in d.iterdir()
                       if f.is_file() and f.suffix.lower() == ".jar"],
                      key=lambda p: p.name.lower())

    def refresh(self):
        """重新列出 mods 目录里的 .jar（保留当前选中文件名以便恢复）。"""
        prev = self._selected_name()
        d = self._mods_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self.table.setRowCount(0)
        files = self._jar_files()
        for i, f in enumerate(files):
            self.table.insertRow(i)
            item = QTableWidgetItem(f.name)
            item.setData(Qt.ItemDataRole.UserRole, str(f))
            item.setToolTip(str(f))
            self.table.setItem(i, 0, item)
            if f.name == prev:
                self.table.selectRow(i)
        self.table.resizeColumnToContents(0)
        return files

    def _selected_name(self) -> str:
        row = self.table.currentRow()
        if row < 0 or row >= self.table.rowCount():
            return ""
        item = self.table.item(row, 0)
        return item.text() if item else ""

    def _add_mod(self):
        initial = str(self._mods_dir().parent if self._mods_dir().parent.is_dir()
                      else str(core.MINECRAFT_DIR))
        path, _ = QFileDialog.getOpenFileName(
            self, "选择要添加的 Mod 文件", initial, "Minecraft Mod (*.jar)")
        if not path:
            return
        src = Path(path)
        d = self._mods_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
            dst = d / src.name
            if dst.exists():
                # 已存在同名 -> 询问覆盖
                ret = QMessageBox.question(
                    self, "覆盖确认", f"mods 目录已存在同名文件：\n{dst.name}\n\n是否覆盖？")
                if ret != QMessageBox.StandardButton.Yes:
                    return
            shutil.copy2(str(src), str(dst))
            self._log(f"已添加 Mod: {src.name} -> {d}")
            self.refresh()
        except Exception as exc:
            QMessageBox.critical(self, "添加失败", f"复制失败：\n{exc}")

    def _delete_selected(self):
        row = self.table.currentRow()
        if row < 0 or row >= self.table.rowCount():
            QMessageBox.information(self, "提示", "请先在列表里选中要删除的 Mod")
            return
        item = self.table.item(row, 0)
        name = item.text() if item else ""
        path = Path(item.data(Qt.ItemDataRole.UserRole)) if item else None
        if not path or not path.is_file():
            return
        ret = QMessageBox.question(
            self, "删除确认", f"确定删除 Mod：\n{name}\n\n（文件会被移除，不可恢复）？")
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            path.unlink()
            self._log(f"已删除 Mod: {name}")
            self.refresh()
        except Exception as exc:
            QMessageBox.critical(self, "删除失败", f"删除失败：\n{exc}")

    def _open_folder(self):
        d = self._mods_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
            os.startfile(str(d))
            self._log(f"已打开 mods 文件夹: {d}")
        except Exception as exc:
            QMessageBox.critical(self, "打开失败", f"无法打开目录：\n{exc}")

    def _log(self, msg: str):
        try:
            print(msg, flush=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 陶瓦联机端口扫描线程
# ---------------------------------------------------------------------------
class TerracottaScannerThread(QThread):
    """后台扫描陶瓦动态端口的工作线程。

    职责：
      1. 用 subprocess.Popen 启动陶瓦 exe（列表参数 + CREATE_NO_WINDOW 防黑框，
         并注入 env["BROWSER"]="echo" 拦截陶瓦自身唤起浏览器，避免 Edge 高权限弹窗）。
      2. 用 psutil 记录启动前本地 LISTEN 端口基线，随后轮询找出新增的、真正
         在监听的"陌生"端口（陶瓦动态端口）。
      3. 找到端口发 port_found(int)；陶瓦退出 / 超时 / 启动失败发 scan_failed(str)。

    信号：
      port_found      -> 扫描到的新端口号
      scan_failed     -> 失败原因（用于主线程弹 QMessageBox）
      process_started -> 陶瓦进程 PID（主线程记录，便于退出时清理）
    """
    port_found = pyqtSignal(int)
    scan_failed = pyqtSignal(str)
    process_started = pyqtSignal(int)

    def __init__(self, exe_path, timeout=60.0, interval=0.3, parent=None):
        super().__init__(parent)
        self.exe_path = str(exe_path)
        self.timeout = timeout
        self.interval = interval
        self.proc = None            # 陶瓦 subprocess 句柄

    def _list_local_listen_ports(self) -> set:
        """返回系统所有本地 TCP LISTEN 端口（去重）的集合。"""
        try:
            import psutil
        except ImportError:
            raise RuntimeError("缺少依赖 psutil，请先执行 pip install psutil")
        ports = set()
        try:
            conns = psutil.net_connections(kind="tcp")
        except Exception:
            conns = []
        for conn in conns:
            if conn.status == "LISTEN":
                try:
                    ports.add(conn.laddr.port)
                except Exception:
                    continue
        return ports

    def _is_port_usable(self, port: int) -> bool:
        """端口当前是否可被重新绑定（用于过滤瞬时端口，排除监听口）。"""
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False

    def run(self):
        # 1) 记录基线端口
        try:
            base_ports = self._list_local_listen_ports()
        except Exception as e:
            self.scan_failed.emit(f"扫描端口失败：{e}")
            return

        # 2) 启动陶瓦：拦截它自己唤起浏览器，并用 CREATE_NO_WINDOW 防黑框
        env = os.environ.copy()
        env["BROWSER"] = "echo"   # 屏蔽陶瓦(Rust)自动调起浏览器 -> 避免 Edge 高权限弹窗
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.proc = subprocess.Popen(
                [self.exe_path],
                env=env,
                creationflags=flags,
            )
        except Exception as e:
            self.scan_failed.emit(f"启动陶瓦失败：{e}")
            return
        try:
            self.process_started.emit(self.proc.pid)
        except Exception:
            pass

        # 3) 轮询扫描新增的 LISTEN 端口
        import time
        start = time.time()
        found = None
        while time.time() - start < self.timeout:
            if self.isInterruptionRequested():
                return
            ret = self.proc.poll()
            if ret is not None:
                self.scan_failed.emit(f"陶瓦提前退出，退出码 {ret}")
                return
            now = self._list_local_listen_ports()
            new_ports = now - base_ports
            candidates = {p for p in new_ports if not self._is_port_usable(p)}
            if candidates:
                found = sorted(candidates)[0]
                break
            time.sleep(self.interval)

        if found:
            self.port_found.emit(found)
        else:
            self.scan_failed.emit(
                "等待陶瓦端口超时，未发现新增的本地 LISTEN 端口。")

    def stop(self):
        """请求线程尽快停止扫描（等下一次轮询跳出）。"""
        self.requestInterruption()


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{core.APP_NAME} - v{CURRENT_VERSION}")
        self.resize(1000, 740)
        self.setMinimumSize(860, 620)

        core.BASE_DIR.mkdir(parents=True, exist_ok=True)
        self.cfg = core.load_config()
        # [FIX-免责弹窗重复] 合并启动器目录的 local_cfg.json 覆盖项
        self.cfg = _merge_local_cfg(self.cfg)
        core.apply_paths(self.cfg)

        # ---- 运行状态（只在主线程读写）----
        self.installed = []
        self.online_versions = []
        self.detected_java = []
        self.busy = False              # 全局忙碌（游戏启动/其它非下载任务）
        self.is_downloading = False    # 下载专用锁：与游戏运行状态完全解耦，
                                       # 让玩家边玩游戏边在「下载管理」页下新版本
        self._downloading_count = 0   # 并发下载任务计数（Mod/光影/整合包可多任务并行）
        # Yggdrasil 登录凭证：{server, accessToken, uuid, username, profiles}；None = 离线模式
        self._auth_session = None
        # 外置登录角色列表：[{"id": 无横杠uuid, "name": 角色名}, ...]
        self._auth_profiles: list[dict] = []
        # 当前在「玩家名」下拉框里选中的角色的真实 UUID（离线模式为空）
        self._selected_uuid = ""
        # 登录态是否来自本次启动的本地恢复（用于状态栏显示「自动恢复」）
        self._restored_auth = False
        self.game_dirs = [d for d in self.cfg.get("game_dirs", []) if d] \
            or [str(core.DEFAULT_MINECRAFT_DIR)]
        self.current_dir_idx = 0

        self._action_buttons = []
        self._icon_wait: dict[str, list] = {}      # 缓存路径 -> [(table, row, pid)]
        self._icon_jobs: set[str] = set()
        self._loading_dirs = False
        self._versions_loaded_once = False
        # 【开服工具】服务端进程句柄 + 当前 jar 路径（只在主线程改，停止时清理）
        self._srv_proc = None
        self._srv_jar_path: str = ""
        self._srv_stdout_thread: threading.Thread | None = None
        self._srv_input_lock = threading.Lock()    # 写 proc.stdin 前加锁，防并发写

        # 【开服工具】服务端进程句柄 + 当前 jar 路径（只在主线程改，停止时清理）
        self._srv_proc = None
        self._srv_jar_path: str = ""
        self._srv_stdout_thread: threading.Thread | None = None
        self._srv_input_lock = threading.Lock()    # 写 proc.stdin 前加锁，防并发写

        # 【陶瓦联机】Terracotta 进程句柄（仅主线程读写；关闭时用 taskkill 强杀进程树）
        self._terracotta_proc: subprocess.Popen | None = None
        self._terracotta_cache_dir = Path(core.BASE_DIR) / "terracotta"
        # 【陶瓦联机】端口扫描线程（QThread，后台跑，找到端口发 port_found）
        self._terracotta_scanner: TerracottaScannerThread | None = None
        # 【陶瓦联机】已启动的陶瓦进程 PID（用于退出时递归杀进程树）
        self._terracotta_pid: int | None = None

        # 各列表的 行/数据 映射
        self._loader_items = []                    # [(kind, ver, url), ...]
        self._dl_item_map = {}                     # 下载表 行号 -> (id, type, url)（纯原版号，绝不装实例名）
        self._mod_rows = {}                        # pid -> row
        self._mod_hits = {}                        # pid -> hit
        self._mod_versions = {}                    # pid -> vinfo
        self._shader_rows = {}
        self._shader_hits = {}
        self._shader_versions = {}
        self._modpack_rows = {}
        self._modpack_hits = {}
        self._modpack_versions = {}
        self._respack_rows = {}
        self._respack_hits = {}
        self._respack_versions = {}
        self._dep_names = {}                        # 依赖 project_id -> 已知显示名（会话内缓存）

        # ---- 分页搜索状态（每个 tab 一份） ----
        self._mod_page = 0
        self._mod_query = None                     # 当前搜索词（翻页时复用，不重新查 MC百科）
        self._mod_ver = None                        # 搜索时使用的 MC 版本
        self._mod_loader = None                     # 搜索时使用的加载器
        self._mod_exhausted = False
        self._shader_page = 0
        self._shader_query = None
        self._shader_ver = None
        self._shader_loader = None
        self._shader_exhausted = False
        self._modpack_page = 0
        self._modpack_query = None
        self._modpack_ver = None
        self._modpack_loader = None
        self._modpack_exhausted = False
        self._respack_page = 0
        self._respack_query = None
        self._respack_ver = None
        self._respack_loader = None
        self._respack_exhausted = False

        # ---- 按页缓存（每个 tab 一份）：page_number -> 该页的 hits 列表 ----
        # 翻到已缓存的页：直接用缓存渲染，零网络请求；只有未缓存才发 API。
        # 新搜索/换搜索词/换加载器/换版本时整体清空，从第 1 页重来。
        self._mod_cached_pages = {}
        self._shader_cached_pages = {}
        self._modpack_cached_pages = {}
        self._respack_cached_pages = {}

        self._running_proc = {}
        self._user_killed = {}          # version_id -> True（玩家主动结束进程，退出时不再弹崩溃警告）
        self._log_windows = {}
        self._global_log_dialog = None

        self.bridge = UiBridge(self)
        self._build_ui()
        self._connect_bridge()
        self._load_cfg_into_ui()
        self._refresh_dir_combo()

        # 启用拖拽支持：用户可从资源管理器拖入认证 .json / authlib-injector.jar
        self.setAcceptDrops(True)

        QTimer.singleShot(200, self.refresh_all)

        # 界面美化：应用自定义背景图（未配置时使用默认深色主题 QSS）
        self.apply_background_image()

        # 系统维护：QTimer 每 5s 刷新一次内存占用 Label（主线程，轻量）
        self._mem_timer = QTimer(self)
        self._mem_timer.setInterval(5000)
        self._mem_timer.timeout.connect(self._refresh_mem_label)
        self._mem_timer.start()
        self._refresh_mem_label()

        # 自动更新检查：启动 3 秒后静默检查一次（有新版才弹窗，失败不打扰）
        QTimer.singleShot(3000, lambda: self._check_update(silent=True))

    # ==================================================================
    # 界面美化：自定义背景图
    # ==================================================================
    def _pick_background_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择背景图", "",
            "图片 (*.png *.jpg *.jpeg *.bmp *.webp)")
        if not path:
            return
        self._bg_image_path = path
        self.cfg["background_image"] = path
        core.save_config(self.cfg)
        self.bg_image_label.setText(path)
        self._log(f"背景图已设置: {path}")
        self.apply_background_image()

    def _clear_background_image(self):
        self._bg_image_path = ""
        self.cfg["background_image"] = ""
        core.save_config(self.cfg)
        self.bg_image_label.setText("未设置")
        self._log("背景图已清除，恢复默认主题")
        self.apply_background_image()

    def _select_bg_theme(self, dark: bool):
        """设置主题下拉框：True=深色，False=浅色。"""
        idx = 0 if dark else 1
        if self.bg_theme_combo.currentIndex() != idx:
            self.bg_theme_combo.setCurrentIndex(idx)

    @property
    def _bg_is_dark(self) -> bool:
        """当前主题：0=深色（默认），1=浅色。"""
        return self.bg_theme_combo.currentIndex() == 0

    def apply_background_image(self):
        """给 MainWindow 应用主题 QSS。

        三种来源（优先级从高到低）：
          1. 自定义背景图（cfg['background_image']，文件存在时）
          2. 深色默认主题（cfg['bg_theme'] == 0，默认）
          3. 浅色默认主题（cfg['bg_theme'] == 1）
        所有内部控件都套对应主题色的半透明遮罩，保证背景图/主题色不挡文字。
        """
        p = getattr(self, "_bg_image_path", "") or ""
        theme_dark = self._bg_is_dark

        if p and os.path.isfile(p):
            # 自定义背景图：铺满窗口
            abs_path = os.path.abspath(p).replace("\\", "/")
            main_qss = (
                f"QMainWindow {{ "
                f"  background-image: url('{abs_path}'); "
                f"  background-position: center; "
                f"  background-repeat: no-repeat; "
                f"  background-size: cover; "
                f"}}"
            )
            bg_src = "背景图"
        elif theme_dark:
            abs_path = ""
            main_qss = (
                "QMainWindow {"
                "  background-color: #2b2b2b; "
                "}"
            )
            bg_src = "深色默认主题"
        else:
            abs_path = ""
            main_qss = (
                "QMainWindow {"
                "  background-color: #f5f5f5; "
                "}"
            )
            bg_src = "浅色默认主题"

        # 内部控件遮罩：深色主题 -> 深灰半透明 + 白字；浅色主题 -> 浅灰半透明 + 深字
        if theme_dark:
            widget_qss = (
                "QTabWidget, QTabWidget > QStackedWidget > QWidget {"
                "  background-color: rgba(45, 45, 45, 180); color: white; }"
                "QTabBar::tab {"
                "  background-color: rgba(45, 45, 45, 200); color: white; "
                "  padding: 6px 12px; border-radius: 4px; }"
                "QTabBar::tab:selected {"
                "  background-color: rgba(70, 130, 200, 200); }"
                "QTableWidget, QTableView {"
                "  background-color: rgba(45, 45, 45, 180); color: white; "
                "  gridline-color: rgba(120, 120, 120, 100); }"
                "QHeaderView::section {"
                "  background-color: rgba(70, 70, 70, 200); color: white; padding: 4px; }"
                "QGroupBox {"
                "  background-color: rgba(45, 45, 45, 180); color: white; "
                "  border: 1px solid rgba(100, 100, 100, 120); "
                "  border-radius: 6px; margin-top: 12px; padding-top: 8px; }"
                "QGroupBox::title {"
                "  subcontrol-origin: margin; left: 12px; padding: 0 4px; "
                "  color: #ddd; }"
                "QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit, QComboBox {"
                "  background-color: rgba(35, 35, 35, 220); color: white; "
                "  border: 1px solid rgba(90, 90, 90, 160); border-radius: 4px; "
                "  padding: 4px 6px; }"
                "QComboBox::drop-down, QSpinBox::up-button, QSpinBox::down-button {"
                "  background-color: rgba(60, 60, 60, 200); border: none; width: 18px; }"
                "QComboBox QAbstractItemView {"
                "  background-color: rgba(40, 40, 40, 255); color: white; "
                "  selection-background-color: #3a6ea5; }"
                "QPushButton {"
                "  background-color: rgba(60, 60, 60, 200); color: white; "
                "  border: 1px solid rgba(100, 100, 100, 140); border-radius: 4px; "
                "  padding: 5px 14px; }"
                "QPushButton:hover { background-color: rgba(80, 110, 160, 200); }"
                "QPushButton:disabled { color: rgba(160, 160, 160, 160); "
                "  background-color: rgba(40, 40, 40, 160); }"
                "QLabel { color: #eee; }"
                "QCheckBox, QRadioButton { color: #ddd; }"
                "QProgressBar {"
                "  background-color: rgba(40, 40, 40, 200); color: white; "
                "  border: 1px solid rgba(90, 90, 90, 140); border-radius: 3px; "
                "  text-align: center; }"
                "QProgressBar::chunk { background-color: #3a6ea5; }"
                "QScrollBar:vertical { background: rgba(40,40,40,120); width: 10px; }"
                "QScrollBar::handle:vertical { background: rgba(100,100,100,180); "
                "  min-height: 30px; border-radius: 4px; }"
                "QScrollBar:horizontal { background: rgba(40,40,40,120); height: 10px; }"
                "QScrollBar::handle:horizontal { background: rgba(100,100,100,180); "
                "  min-width: 30px; border-radius: 4px; }"
                # QScrollArea（设置页签包裹层）：透明背景 + 可见滚动条
                "QScrollArea { background-color: transparent; }"
                "QScrollArea::viewport { background-color: transparent; }"
            )
        else:
            # 浅色主题：白底深字，控件半透明浅色遮罩
            widget_qss = (
                "QTabWidget, QTabWidget > QStackedWidget > QWidget {"
                "  background-color: rgba(245, 245, 245, 180); color: #222; }"
                "QTabBar::tab {"
                "  background-color: rgba(230, 230, 230, 220); color: #333; "
                "  padding: 6px 12px; border-radius: 4px; }"
                "QTabBar::tab:selected {"
                "  background-color: rgba(100, 150, 220, 200); color: white; }"
                "QTableWidget, QTableView {"
                "  background-color: rgba(250, 250, 250, 200); color: #222; "
                "  gridline-color: rgba(180, 180, 180, 120); }"
                "QHeaderView::section {"
                "  background-color: rgba(220, 220, 220, 220); color: #333; padding: 4px; }"
                "QGroupBox {"
                "  background-color: rgba(245, 245, 245, 180); color: #222; "
                "  border: 1px solid rgba(180, 180, 180, 140); "
                "  border-radius: 6px; margin-top: 12px; padding-top: 8px; }"
                "QGroupBox::title {"
                "  subcontrol-origin: margin; left: 12px; padding: 0 4px; "
                "  color: #444; }"
                "QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit, QComboBox {"
                "  background-color: rgba(255, 255, 255, 230); color: #222; "
                "  border: 1px solid rgba(170, 170, 170, 160); border-radius: 4px; "
                "  padding: 4px 6px; }"
                "QComboBox::drop-down, QSpinBox::up-button, QSpinBox::down-button {"
                "  background-color: rgba(235, 235, 235, 220); border: none; width: 18px; }"
                "QComboBox QAbstractItemView {"
                "  background-color: rgba(250, 250, 250, 255); color: #222; "
                "  selection-background-color: #a5c8ec; }"
                "QPushButton {"
                "  background-color: rgba(230, 230, 230, 220); color: #222; "
                "  border: 1px solid rgba(180, 180, 180, 160); border-radius: 4px; "
                "  padding: 5px 14px; }"
                "QPushButton:hover { background-color: rgba(200, 220, 250, 220); }"
                "QPushButton:disabled { color: rgba(150, 150, 150, 180); "
                "  background-color: rgba(235, 235, 235, 180); }"
                "QLabel { color: #333; }"
                "QCheckBox, QRadioButton { color: #333; }"
                "QProgressBar {"
                "  background-color: rgba(240, 240, 240, 220); color: #222; "
                "  border: 1px solid rgba(180, 180, 180, 160); border-radius: 3px; "
                "  text-align: center; }"
                "QProgressBar::chunk { background-color: #6ea5d5; }"
                "QScrollBar:vertical { background: rgba(235,235,235,160); width: 10px; }"
                "QScrollBar::handle:vertical { background: rgba(180,180,180,200); "
                "  min-height: 30px; border-radius: 4px; }"
                "QScrollBar:horizontal { background: rgba(235,235,235,160); height: 10px; }"
                "QScrollBar::handle:horizontal { background: rgba(180,180,180,200); "
                "  min-width: 30px; border-radius: 4px; }"
                # QScrollArea（设置页签包裹层）：透明背景 + 可见滚动条
                "QScrollArea { background-color: transparent; }"
                "QScrollArea::viewport { background-color: transparent; }"
            )
        # 主窗口自身先套，再套到所有内部控件
        self.setStyleSheet(main_qss + widget_qss)
        if abs_path:
            self._log(f"已应用{bg_src}: {abs_path}（内部控件套半透明遮罩）")
        else:
            self._log(f"使用{bg_src}")

    # ==================================================================
    # 信号槽接线
    # ==================================================================
    def _connect_bridge(self):
        b = self.bridge
        b.log.connect(self.on_log)
        b.status.connect(self.on_status)
        b.progress.connect(self.on_progress)
        b.progressReset.connect(self.on_progress_reset)
        b.busy.connect(self.on_busy)
        b.javaReady.connect(self.on_java_ready)
        b.versionsReady.connect(self.on_versions_ready)
        b.onlineReady.connect(self.on_online_ready)
        b.mcVersionsReady.connect(self.on_mc_versions_ready)
        b.loadersReady.connect(self.on_loaders_ready)
        b.modHits.connect(self.on_mod_hits)
        b.shaderHits.connect(self.on_shader_hits)
        b.modpackHits.connect(self.on_modpack_hits)
        b.respackHits.connect(self.on_respack_hits)
        b.modResolved.connect(self.on_mod_resolved)
        b.shaderResolved.connect(self.on_shader_resolved)
        b.modpackResolved.connect(self.on_modpack_resolved)
        b.respackResolved.connect(self.on_respack_resolved)
        b.iconReady.connect(self.on_icon_ready)
        b.gameStarted.connect(self.on_game_started)
        b.gameLog.connect(self.on_game_log)
        b.gameExited.connect(self.on_game_exited)
        b.launchBusy.connect(self.on_launch_busy)
        b.authReady.connect(self.on_auth_ready)
        b.tokenInvalid.connect(self.on_token_invalid)
        # 【开服工具】服务端日志/退出
        b.srvConsole.connect(self.on_srv_console)
        b.srvExited.connect(self.on_srv_exited)
        # 【系统维护】垃圾清理 / 内存释放（后台 -> 主线程）
        b.junkScanned.connect(self.on_junk_scanned)
        b.junkCleaned.connect(self.on_junk_cleaned)
        b.memCleared.connect(self.on_mem_cleared)
        # 【Java 下载失败】询问式弹窗（后台 -> 主线程槽）
        b.javaDownloadFailed.connect(self.on_java_download_failed)
        # 【一键修复权限完成】复位按钮（后台 -> 主线程槽）
        b.permsFixedDone.connect(self._on_perms_fixed_done)
        # 【开服工具 MSL】后台请求 -> 主线程槽渲染下拉框（绝不在后台碰 UI）
        b.srvTypesReady.connect(self.on_srv_types_ready)
        b.srvVersionsReady.connect(self.on_srv_versions_ready)
        b.srvDownloadReady.connect(self.on_srv_download_ready)
        b.error.connect(lambda t, m: QMessageBox.critical(self, t, m))
        b.warn.connect(lambda t, m: QMessageBox.warning(self, t, m))
        b.info.connect(lambda t, m: QMessageBox.information(self, t, m))
        # 自动更新：发现新版本 -> 弹「是否前往 GitHub」，确认则浏览器打开 html_url
        b.ask_open_url.connect(self._on_update_open_url)

    # ==================================================================
    # UI 构建
    # ==================================================================
    def _build_ui(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 8)
        root.setSpacing(8)

        root.addWidget(self._build_account_box())

        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._build_version_tab(), "版本列表")
        self.tabs.addTab(self._build_download_tab(), "下载管理")
        self.tabs.addTab(self._build_resource_tab(), "Mod资源")
        self.tabs.addTab(self._build_modpack_tab(), "整合包")
        # self.tabs.addTab(self._build_server_tab(), "开服工具")  # [已切除] 开服工具不再展示
        self.tabs.addTab(self._build_terracotta_tab(), "陶瓦联机")
        self.tabs.addTab(self._build_settings_tab(), "设置")
        root.addWidget(self.tabs, 1)
        # 切到「Mod资源」或「整合包」页签时，若该页从未搜过，
        # 自动发一次空查询（relevance 排序 = 热门项目）渲染到列表里。
        self._mod_auto_loaded = False
        self._shader_auto_loaded = False
        self._modpack_auto_loaded = False
        self._respack_auto_loaded = False
        self.tabs.currentChanged.connect(self._on_main_tab_changed)
        # 光影子页签切换时也自动加载一次（同样只加载一次）
        self.res_tabs.currentChanged.connect(self._on_res_subtab_changed)

        # 全局 Java / 目录 / 内存 已抽到【设置】页签，底部只留状态栏
        # 状态框 stretch=0 + Fixed 高度策略：始终贴底、紧凑，不被上方 tab 内容拉高
        root.addWidget(self._build_status_box(), 0)

    # ------------- 陶瓦联机管理页签 -------------
    def _build_terracotta_tab(self) -> QWidget:
        """独立的「陶瓦联机管理」页面。

        包含：状态指示灯/标签、[启动联机]/[结束联机] 两个联动按钮、
        一个只读日志框（实时显示陶瓦启动/控制台信息）。
        """
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        # ---- 状态区 ----
        box = QGroupBox("联机状态")
        bf = QFormLayout(box)
        bf.setContentsMargins(10, 10, 10, 10)
        bf.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.terra_status_label = QLabel("未启动")
        self.terra_status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.terra_status_label.setStyleSheet(
            "font-size: 12pt; font-weight: bold; color: #999999;")
        bf.addRow("状态", self.terra_status_label)
        lay.addWidget(box)

        # ---- 操作按钮区 ----
        btn_box = QGroupBox("操作")
        bb = QHBoxLayout(btn_box)
        bb.setContentsMargins(10, 10, 10, 10)
        self.btn_terra_start = self._mk_button("🚀 启动联机", self._start_terracotta,
                                               primary=True)
        self.btn_terra_stop = self._mk_button("🛑 结束联机", self._stop_terracotta)
        self.btn_terra_stop.setEnabled(False)   # 初始未启动，禁止结束
        bb.addWidget(self.btn_terra_start)
        bb.addWidget(self.btn_terra_stop)
        bb.addStretch(1)
        lay.addWidget(btn_box)

        # ---- 日志输出区 ----
        log_box = QGroupBox("陶瓦联机日志")
        lb = QVBoxLayout(log_box)
        lb.setContentsMargins(10, 10, 10, 10)
        self.terra_log = QPlainTextEdit()
        self.terra_log.setReadOnly(True)
        self.terra_log.setMaximumBlockCount(2000)
        self.terra_log.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 9pt;")
        self.terra_log.setPlaceholderText("陶瓦联机控制台输出将显示在这里...")
        lb.addWidget(self.terra_log, 1)
        lay.addWidget(log_box, 1)

        return w

    def _set_terracotta_state(self, state: str, port: int | None = None):
        """【主线程】统一刷新陶瓦联机页面的状态标签与按钮联动。

        state 取值：unstarted / starting / ready / stopped
        """
        color = {
            "unstarted": "#999999",   # 灰
            "starting": "#ffaa00",    # 橙
            "ready": "#33cc33",       # 绿
            "stopped": "#888888",     # 深灰
        }.get(state, "#999999")
        if state == "unstarted":
            text = "未启动"
        elif state == "starting":
            text = "启动中..."
        elif state == "ready":
            text = f"联机就绪 (后台运行中)"
        elif state == "stopped":
            text = "已停止"
        else:
            text = state
        self.terra_status_label.setText(text)
        self.terra_status_label.setStyleSheet(
            f"font-size: 12pt; font-weight: bold; color: {color};")
        # 按钮联动：启动中/就绪时禁用「启动」，启用「结束」；否则反之
        running = state in ("starting", "ready")
        self.btn_terra_start.setEnabled(not running)
        self.btn_terra_stop.setEnabled(running)

    def _terra_append_log(self, line: str):
        """【主线程】往陶瓦日志框追加一行（带时间戳）。"""
        if getattr(self, "terra_log", None) is None:
            return
        ts = time.strftime("%H:%M:%S")
        self.terra_log.appendPlainText(f"[{ts}] {line}")

    def _stop_terracotta(self):
        """【主线程】结束陶瓦联机：psutil 递归杀进程树，状态置「已停止」。"""
        self.bridge.log.emit("正在结束陶瓦联机...")
        self._terra_append_log("正在结束陶瓦联机...")
        self._kill_terracotta()
        self._set_terracotta_state("stopped")
        self.bridge.status.emit("陶瓦联机已停止")
        self.bridge.log.emit("陶瓦联机已停止")

    # ------------- 顶部：账号与设置 -------------
    def _build_account_box(self) -> QGroupBox:
        box = QGroupBox("账号与设置", self)
        grid = QGridLayout(box)
        grid.setContentsMargins(10, 8, 10, 8)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        # 「玩家名」改成可编辑下拉框：
        #   离线模式  -> 玩家可以手输任意名字
        #   外置登录  -> 自动填入 availableProfiles 的全部角色供选择
        # 选中角色的真实 UUID 存在 item 的 UserRole 里，启动时直接取用。
        self.name_combo = QComboBox()
        self.name_combo.setEditable(True)
        self.name_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.name_combo.setMinimumWidth(220)
        self.name_combo.setToolTip(
            "离线模式：直接输入玩家名\n"
            "外置登录：从下拉框选择皮肤站账号下的角色")
        self.name_combo.currentIndexChanged.connect(self._on_name_combo_changed)
        if self.name_combo.lineEdit() is not None:
            self.name_combo.lineEdit().editingFinished.connect(self._save_settings)
        self.name_combo.setEditText("Steve")

        self.offline_check = QCheckBox("离线模式")
        self.offline_check.setChecked(True)
        self.offline_check.toggled.connect(self._on_offline_toggled)

        self.lang_combo = QComboBox()
        self.lang_combo.addItems([label for label, _ in LANGUAGES])
        self.lang_combo.currentIndexChanged.connect(lambda _: self._save_settings())

        self.jvm_edit = QLineEdit()
        self.jvm_edit.setPlaceholderText("附加 JVM 参数，如 -XX:+UseG1GC")
        self.jvm_edit.editingFinished.connect(self._save_settings)

        grid.addWidget(QLabel("玩家名"), 0, 0)
        grid.addWidget(self.name_combo, 0, 1)
        grid.addWidget(self.offline_check, 0, 2)
        grid.addWidget(QLabel("游戏语言"), 1, 0)
        grid.addWidget(self.lang_combo, 1, 1)
        grid.addWidget(QLabel("JVM 参数"), 2, 0)
        grid.addWidget(self.jvm_edit, 2, 1, 1, 2)
        grid.setColumnStretch(1, 1)
        return box

    # ---- 玩家名下拉框 / 离线勾选 的联动 ----
    def _on_offline_toggled(self, offline: bool):
        """离线勾选变化：外置登录有角色时禁用手输（只能选皮肤站角色）。"""
        self._refresh_name_combo_editability()
        self._save_settings()

    def _refresh_name_combo_editability(self):
        """有外置登录角色时，玩家名下拉框锁成「只选」；否则允许手输。"""
        has_profiles = bool(getattr(self, "_auth_profiles", None))
        self.name_combo.setEditable(not has_profiles)
        if has_profiles:
            self.name_combo.setToolTip("外置登录：请从下拉框选择皮肤站角色（不可手输）")
        else:
            self.name_combo.setToolTip("离线模式：直接输入玩家名")

    def _on_name_combo_changed(self, _idx: int):
        """切换选中角色：把该角色的真实 UUID 记到 self._selected_uuid。"""
        self._selected_uuid = self.name_combo.currentData() or ""
        self._save_settings()

    def _fill_name_combo_from_profiles(self, profiles: list, selected: str = ""):
        """把 availableProfiles 灌进玩家名下拉框，并选中默认角色。"""
        self._auth_profiles = list(profiles or [])
        self.name_combo.blockSignals(True)
        self.name_combo.clear()
        for prof in self._auth_profiles:
            pname = prof.get("name") or ""
            puuid = prof.get("id") or ""
            if not pname:
                continue
            self.name_combo.addItem(pname, puuid)   # UserRole = 真实 UUID（无横杠）
        if not self._auth_profiles:
            # 没有角色信息：回退成可手输，保留原名字
            cur = self.name_combo.currentText() or "Steve"
            self.name_combo.setEditText(cur)
        else:
            # 优先选中 selectedProfile 对应的角色，否则第一个
            idx = 0
            for i in range(self.name_combo.count()):
                if self.name_combo.itemText(i) == selected:
                    idx = i
                    break
            self.name_combo.setCurrentIndex(idx)
        self.name_combo.blockSignals(False)
        self._selected_uuid = self.name_combo.currentData() or ""
        self._refresh_name_combo_editability()
        self._log(f"[外置登录] 已载入 {len(self._auth_profiles)} 个角色到玩家名下拉框："
                  + ", ".join(p.get("name", "")
                              for p in self._auth_profiles[:8]))

    def _current_player(self) -> tuple[str, str]:
        """返回 (玩家名, 真实UUID)。离线模式下 UUID 为空字符串。"""
        name = (self.name_combo.currentText() or "").strip() or "Steve"
        uid = self.name_combo.currentData() or ""
        return name, str(uid)

    # ------------- 通用表格 -------------
    def _make_table(self, headers, widths=None) -> QTableWidget:
        t = QTableWidget(0, len(headers), self)
        t.setHorizontalHeaderLabels(headers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setAlternatingRowColors(True)
        t.setWordWrap(False)
        t.setShowGrid(False)
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setDefaultSectionSize(26)
        t.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        header = t.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        if widths:
            for i, w in enumerate(widths):
                t.setColumnWidth(i, w)
        return t

    def _mk_button(self, text, slot, primary=False) -> QPushButton:
        """只造按钮（由调用方决定放进哪个布局），并登记到「忙碌时禁用」列表。"""
        btn = QPushButton(text)
        btn.clicked.connect(slot)
        if primary:
            btn.setDefault(True)
        self._action_buttons.append(btn)
        return btn

    # ------------- Java 下拉框 -------------
    @staticmethod
    def _java_display_name(version: str, path: str, index: int) -> str:
        """生成 Java 下拉框里的短标签：只显示版本号，绝不放绝对路径。

        例：'Java 25.0.4'、'Java 8（1.8.0_392）'；同版本重名时加 (2)/(3) 区分。
        """
        v = str(version or "?").strip() or "?"
        parts = v.split(".")
        # 1.8.0_392 这种老式版本号，主版本其实是 8，但完整号也有用，附在括号里
        if len(parts) >= 2 and parts[0] == "1":
            label = f"Java {parts[1]}（{v}）"
        else:
            label = f"Java {v}"
        if index > 0:
            label = f"{label} ({index + 1})"
        return label

    def _make_java_combo(self) -> QComboBox:
        """构造 Java 下拉框：短标签 + 完整路径 tooltip + 不被长文本顶宽。"""
        combo = QComboBox()
        # 允许被压缩，同时给出合理下限，避免按钮把下拉框挤成一条缝
        combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        combo.setMinimumWidth(180)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(18)   # 只按 ~18 个字符算自然宽度
        combo.setMaxVisibleItems(12)
        combo.setToolTip("Java 运行时（完整路径见悬停提示）")
        combo.view().setTextElideMode(Qt.TextElideMode.ElideMiddle)
        combo.currentIndexChanged.connect(self._on_java_combo_changed)
        return combo

    def _make_mc_ver_combo(self) -> QComboBox:
        """构造【目标 MC 版本】下拉框：可编辑 + 带“All/不限制”占位首项。

        值=纯原版版本号（1.20.1、1.21.1…），绝不放实例全名（如 1.21.1-NeoForge），
        这样传给 Modrinth API 时不会被“NeoForge 版本号”污染。
        """
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        combo.setMinimumWidth(150)
        combo.setMinimumContentsLength(10)
        combo.addItem("All（不限制）")
        combo.setCurrentIndex(0)
        combo.setToolTip("目标原版 MC 版本（如 1.21.1）。选 All 则不过滤版本。")
        return combo

    def _make_loader_combo(self, for_shader=False) -> QComboBox:
        """构造【加载器】下拉框：All / Forge / Fabric / NeoForge。

        for_shader=True 时写死为光影专用三选项：All / Iris / OptiFine
        （光影只认 Iris 和 OptiFine，forge/fabric/neoforge 对光影无意义）。
        """
        combo = QComboBox()
        combo.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        combo.setMinimumWidth(120)
        combo.setMinimumContentsLength(8)
        if for_shader:
            combo.addItems(["all", "iris", "optifine"])
        else:
            combo.addItems(["all", "forge", "fabric", "neoforge"])
        combo.setCurrentIndex(0)
        combo.setToolTip("加载器类型。选 All 则不过滤加载器。"
                         + ("（光影只有 Iris / OptiFine）" if for_shader else ""))
        return combo

    def _add_button(self, layout, text, slot, primary=False):
        btn = self._mk_button(text, slot, primary)
        layout.addWidget(btn)
        return btn

    def _cell(self, text, user_data=None, align=None) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        if align is not None:
            item.setTextAlignment(align)
        if user_data is not None:
            item.setData(Qt.ItemDataRole.UserRole, user_data)
        return item

    def _make_pager_row(self, key: str) -> QHBoxLayout:
        """造一排【上一页 | 第 N 页 | 下一页】，绑定到 key（mod/shader/modpack）。

        key 决定：
          - 页码标签 self.<key>_page_label
          - 上一页按钮 self.<key>_prev_btn
          - 下一页按钮 self.<key>_next_btn
        """
        row = QHBoxLayout()
        prev_btn = QPushButton("上一页")
        prev_btn.clicked.connect(lambda: self._search_prev_page(key))
        page_label = QLabel("第 0 页")
        next_btn = QPushButton("下一页")
        next_btn.clicked.connect(lambda: self._search_next_page(key))
        row.addWidget(prev_btn)
        row.addWidget(page_label)
        row.addWidget(next_btn)
        row.addStretch(1)
        setattr(self, f"{key}_prev_btn", prev_btn)
        setattr(self, f"{key}_page_label", page_label)
        setattr(self, f"{key}_next_btn", next_btn)
        self._action_buttons.append(prev_btn)
        self._action_buttons.append(next_btn)
        return row

    # ------------- Tab1：版本列表 -------------
    def _build_version_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)

        self.ver_table = self._make_table(["版本号", "类型", "状态"], [300, 130, 160])
        self.ver_table.cellDoubleClicked.connect(lambda *_: self.launch())
        self.ver_table.itemSelectionChanged.connect(self._on_ver_selected)
        lay.addWidget(self.ver_table, 1)

        row = QHBoxLayout()
        row.addWidget(QLabel("已安装版本（双击启动）"))
        row.addStretch(1)
        self._add_button(row, "刷新列表", self._refresh_versions_bg)
        self.btn_launch = self._mk_button("启动所选版本", self.launch, primary=True)
        row.addWidget(self.btn_launch)
        self._add_button(row, "校验/补全所选版本", self._repair_selected)
        self._add_button(row, "管理 Mod", self._open_mod_manager)
        lay.addLayout(row)
        return w

    # ------------- Tab2：下载管理 -------------
    def _build_download_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)

        top = QHBoxLayout()
        top.addWidget(QLabel("可下载版本"))
        self.btn_dl_refresh = self._mk_button("刷新列表", self._refresh_dl_list)
        top.addWidget(self.btn_dl_refresh)
        top.addWidget(QLabel("筛选："))
        self.filter_group = QButtonGroup(self)
        self.rb_release = QRadioButton("Release")
        self.rb_release.setChecked(True)
        self.rb_snapshot = QRadioButton("Snapshot")
        for rb in (self.rb_release, self.rb_snapshot):
            self.filter_group.addButton(rb)
            top.addWidget(rb)
            rb.toggled.connect(lambda checked: checked and self._render_online())
        top.addStretch(1)
        lay.addLayout(top)

        self.dl_table = self._make_table(["版本号", "类型", "状态"], [300, 130, 160])
        self.dl_table.cellDoubleClicked.connect(lambda *_: self.download_version())
        lay.addWidget(self.dl_table, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self._add_button(row, "下载所选版本", self.download_version, primary=True)
        self._add_button(row, "校验/补全所选版本", self._repair_selected)
        self._add_button(row, "批量补全本地版本", self._download_all_release)
        lay.addLayout(row)
        return w

    # ------------- Tab3：Mod资源 -------------
    def _build_resource_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)

        top = QHBoxLayout()
        top.addWidget(QLabel("目标版本："))
        self.res_ver_combo = QComboBox()
        self.res_ver_combo.setMinimumWidth(260)
        top.addWidget(self.res_ver_combo)
        self._add_button(top, "刷新", self._refresh_res_version_list)
        self._add_button(top, "打开 mods 目录", lambda: self._open_res_dir("mods"))
        self._add_button(top, "打开光影目录", lambda: self._open_res_dir("shaderpacks"))
        self._add_button(top, "打开资源包目录", lambda: self._open_res_dir("resourcepacks"))
        top.addStretch(1)
        lay.addLayout(top)

        self.res_tabs = QTabWidget()
        self.res_tabs.addTab(self._build_loader_subtab(), "Mod加载器")
        self.res_tabs.addTab(self._build_mod_subtab(), "Mod")
        self.res_tabs.addTab(self._build_shader_subtab(), "光影")
        self.res_tabs.addTab(self._build_resourcepack_subtab(), "资源包")
        lay.addWidget(self.res_tabs, 1)
        return w

    def _build_loader_subtab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 6)

        top = QHBoxLayout()
        top.addWidget(QLabel("MC 版本："))
        self.loader_mc_combo = QComboBox()
        self.loader_mc_combo.setEditable(True)
        self.loader_mc_combo.addItems(LOADER_MC_PRESETS)
        self.loader_mc_combo.setCurrentText("1.20.1")
        self.loader_mc_combo.setMinimumWidth(110)
        top.addWidget(self.loader_mc_combo)
        top.addWidget(QLabel("加载器："))
        self.loader_kind_group = QButtonGroup(self)
        for i, (kind, label) in enumerate(LOADER_KINDS):
            rb = QRadioButton(label)
            rb.setChecked(kind == "forge")
            self.loader_kind_group.addButton(rb, i)
            top.addWidget(rb)
            rb.toggled.connect(lambda checked: checked and self._refresh_loader_list())
        top.addStretch(1)
        self._add_button(top, "刷新列表", self._refresh_loader_list)
        lay.addLayout(top)

        self.loader_table = self._make_table(["加载器版本", "类型", "状态"], [260, 130, 120])
        lay.addWidget(self.loader_table, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self._add_button(row, "下载并安装所选加载器", self._install_selected_loader, primary=True)
        lay.addLayout(row)
        return w

    def _build_mod_subtab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 6)

        top = QHBoxLayout()
        top.addWidget(QLabel("目标 MC 版本："))
        self.mod_mc_combo = self._make_mc_ver_combo()
        top.addWidget(self.mod_mc_combo)
        top.addWidget(QLabel("加载器："))
        self.mod_loader_combo = self._make_loader_combo()
        top.addWidget(self.mod_loader_combo)
        self.mod_search_edit = QLineEdit()
        self.mod_search_edit.setPlaceholderText("输入 Mod 名称搜索；留空直接点搜索 = 查看当前热门 Mod")
        self.mod_search_edit.returnPressed.connect(self._search_mods)
        top.addWidget(self.mod_search_edit, 1)
        self._add_button(top, "搜索 Mod", self._search_mods, primary=True)
        lay.addLayout(top)

        self.mod_table = self._make_table(["图标", "Mod 名称", "加载器", "游戏版本"],
                                          [48, 360, 130, 130])
        self.mod_table.itemSelectionChanged.connect(self._on_mod_selected)
        lay.addWidget(self.mod_table, 1)

        lay.addWidget(QLabel("选中的 Mod（简介 / 前置依赖）"))
        self.mod_info = QPlainTextEdit()
        self.mod_info.setReadOnly(True)
        self.mod_info.setMaximumHeight(120)
        lay.addWidget(self.mod_info)

        # 分页导航行（上一页 | 第 N 页 | 下一页）
        lay.addLayout(self._make_pager_row("mod"))

        row = QHBoxLayout()
        row.addWidget(QLabel("（按上方 MC 版本 + 加载器搜索；下载时自动放进匹配的已装实例）"))
        row.addStretch(1)
        self._add_button(row, "下载此 Mod（含前置）",
                         self._download_selected_mod, primary=True)
        lay.addLayout(row)
        return w

    def _build_shader_subtab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 6)

        top = QHBoxLayout()
        top.addWidget(QLabel("目标 MC 版本："))
        self.shader_mc_combo = self._make_mc_ver_combo()
        top.addWidget(self.shader_mc_combo)
        top.addWidget(QLabel("加载器："))
        self.shader_loader_combo = self._make_loader_combo(for_shader=True)
        top.addWidget(self.shader_loader_combo)
        self.shader_search_edit = QLineEdit()
        self.shader_search_edit.setPlaceholderText("输入光影名称搜索；留空直接点搜索 = 查看当前热门光影")
        self.shader_search_edit.returnPressed.connect(self._search_shaders)
        top.addWidget(self.shader_search_edit, 1)
        self._add_button(top, "搜索光影", self._search_shaders, primary=True)
        lay.addLayout(top)

        self.shader_table = self._make_table(["图标", "光影名称", "加载器", "游戏版本"],
                                             [48, 360, 130, 130])
        self.shader_table.itemSelectionChanged.connect(self._on_shader_selected)
        lay.addWidget(self.shader_table, 1)

        lay.addWidget(QLabel("选中的光影（简介 / 前置依赖）"))
        self.shader_info = QPlainTextEdit()
        self.shader_info.setReadOnly(True)
        self.shader_info.setMaximumHeight(120)
        lay.addWidget(self.shader_info)

        # 分页导航行（上一页 | 第 N 页 | 下一页）
        lay.addLayout(self._make_pager_row("shader"))

        row = QHBoxLayout()
        row.addWidget(QLabel("（自动匹配当前版本 + 加载器，并把必要前置一并下好）"))
        row.addStretch(1)
        self._add_button(row, "下载此光影（含前置）到当前版本",
                         self._download_selected_shader, primary=True)
        lay.addLayout(row)
        return w

    def _build_resourcepack_subtab(self) -> QWidget:
        """资源包子页签：UI 与光影完全一致，但搜索 project_type=resourcepack，
        下载目录为 resourcepacks，且无前置依赖。"""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 6)

        top = QHBoxLayout()
        top.addWidget(QLabel("目标 MC 版本："))
        self.respack_mc_combo = self._make_mc_ver_combo()
        top.addWidget(self.respack_mc_combo)
        top.addWidget(QLabel("加载器："))
        self.respack_loader_combo = self._make_loader_combo()
        top.addWidget(self.respack_loader_combo)
        self.respack_search_edit = QLineEdit()
        self.respack_search_edit.setPlaceholderText("输入资源包名称搜索；留空直接点搜索 = 查看当前热门资源包")
        self.respack_search_edit.returnPressed.connect(self._search_resourcepacks)
        top.addWidget(self.respack_search_edit, 1)
        self._add_button(top, "搜索资源包", self._search_resourcepacks, primary=True)
        lay.addLayout(top)

        self.respack_table = self._make_table(["图标", "资源包名称", "加载器", "游戏版本"],
                                               [48, 360, 130, 130])
        self.respack_table.itemSelectionChanged.connect(self._on_respack_selected)
        lay.addWidget(self.respack_table, 1)

        lay.addWidget(QLabel("选中的资源包（简介 / 版本）"))
        self.respack_info = QPlainTextEdit()
        self.respack_info.setReadOnly(True)
        self.respack_info.setMaximumHeight(120)
        lay.addWidget(self.respack_info)

        # 分页导航行（上一页 | 第 N 页 | 下一页）
        lay.addLayout(self._make_pager_row("respack"))

        row = QHBoxLayout()
        row.addWidget(QLabel("（自动匹配当前版本，直接下到 resourcepacks 目录，无前置依赖）"))
        row.addStretch(1)
        self._add_button(row, "下载此资源包到当前版本",
                         self._download_selected_respack, primary=True)
        lay.addLayout(row)
        return w

    # ------------- Tab4：整合包 -------------
    def _build_modpack_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)

        top = QHBoxLayout()
        top.addWidget(QLabel("目标 MC 版本："))
        self.modpack_mc_combo = self._make_mc_ver_combo()
        top.addWidget(self.modpack_mc_combo)
        top.addWidget(QLabel("加载器："))
        self.modpack_loader_combo = self._make_loader_combo()
        top.addWidget(self.modpack_loader_combo)
        self.modpack_search_edit = QLineEdit()
        self.modpack_search_edit.setPlaceholderText("输入整合包名称搜索；留空直接点搜索 = 查看当前热门整合包")
        self.modpack_search_edit.returnPressed.connect(self._search_modpacks)
        top.addWidget(self.modpack_search_edit, 1)
        self._add_button(top, "搜索整合包", self._search_modpacks, primary=True)
        lay.addLayout(top)

        self.modpack_table = self._make_table(["图标", "整合包名称", "加载器", "游戏版本"],
                                              [48, 360, 130, 130])
        self.modpack_table.itemSelectionChanged.connect(self._on_modpack_selected)
        lay.addWidget(self.modpack_table, 1)

        lay.addWidget(QLabel("选中的整合包（简介 / 版本）"))
        self.modpack_info = QPlainTextEdit()
        self.modpack_info.setReadOnly(True)
        self.modpack_info.setMaximumHeight(120)
        lay.addWidget(self.modpack_info)

        # 分页导航行（上一页 | 第 N 页 | 下一页）
        lay.addLayout(self._make_pager_row("modpack"))

        row = QHBoxLayout()
        row.addWidget(QLabel("（将创建独立新实例：自动装原版+加载器，并把整合包 Mod 装进该实例）"))
        row.addStretch(1)
        self._add_button(row, "下载并安装整合包（创建新实例）",
                         self._download_selected_modpack, primary=True)
        # [FIX-截断恢复] 新增「导入本地整合包」按钮：选择本机 .mrpack 直接安装
        self._add_button(row, "导入本地整合包...",
                         self._import_local_mrpack)
        lay.addLayout(row)
        return w

    # ------------- Tab：开服工具（服务端下载 + 一键开服 + 实时控制台）-------------
    def _build_server_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)

        # ---- 顶部选择区：服务端类型 + MC 版本 + 描述 + 下载 ----
        top = QHBoxLayout()
        top.addWidget(QLabel("服务端类型："))
        self.combo_srv_type = QComboBox()
        self.combo_srv_type.setMinimumWidth(160)
        self.combo_srv_type.setToolTip("MSL 镜像提供的服务端类型（paper / forge / fabric ...）")
        # [FIX-版本下拉框为空] 用 currentIndexChanged + lambda 忽略 index 参数，
        # 比 activated 更可靠（activated 只在键盘/鼠标手动选择时触发，
        # 程序 setCurrentIndex / 首次填充不触发）。
        self.combo_srv_type.currentIndexChanged.connect(
            lambda _: self._srv_on_type_changed())
        top.addWidget(self.combo_srv_type)
        top.addWidget(QLabel("MC 版本："))
        self.combo_srv_ver = QComboBox()
        self.combo_srv_ver.setMinimumWidth(120)
        self.combo_srv_ver.setEditable(True)
        self.combo_srv_ver.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        top.addWidget(self.combo_srv_ver)
        top.addStretch(1)
        self.btn_download_srv = self._mk_button(
            "⬇ 下载服务端", self._download_srv, primary=True)
        top.addWidget(self.btn_download_srv)
        lay.addLayout(top)

        # 服务端 description 文本
        self.lbl_srv_desc = QLabel("（请先选择服务端类型以加载版本列表）")
        self.lbl_srv_desc.setWordWrap(True)
        self.lbl_srv_desc.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_srv_desc.setStyleSheet("color:#9ac;")
        lay.addWidget(self.lbl_srv_desc)

        # ---- 操作区：启动 / 停止 ----
        op = QHBoxLayout()
        self.btn_start_server = self._mk_button(
            "▶️ 一键启动服务端", self._start_server, primary=True)
        op.addWidget(self.btn_start_server)
        self.btn_stop_server = self._mk_button(
            "⏹ 停止服务端", self._stop_server)
        op.addWidget(self.btn_stop_server)
        op.addStretch(1)
        lay.addLayout(op)

        # 警告小字
        warn = QLabel(
            "* 开服前请确保遵守 Minecraft EULA 协议，"
            "严禁用于非法盈利或诱导未成年人充值。*")
        warn.setStyleSheet("color:#e57; font-size:11px;")
        warn.setWordWrap(True)
        lay.addWidget(warn)

        # ---- 底部控制台 + 指令输入 ----
        lay.addWidget(QLabel("服务端控制台："))
        self.server_console = QPlainTextEdit()
        self.server_console.setReadOnly(True)
        self.server_console.setMaximumBlockCount(5000)
        self.server_console.setStyleSheet(
            "QPlainTextEdit { background:#0d1117; color:#c9d1d9; "
            "font-family: Consolas, 'Cascadia Mono', monospace; }")
        lay.addWidget(self.server_console, 1)

        cmd_row = QHBoxLayout()
        self.edit_cmd_input = QLineEdit()
        self.edit_cmd_input.setPlaceholderText(
            "向服务端发送指令（如 stop / op 玩家名 / say hi），回车发送")
        self.edit_cmd_input.returnPressed.connect(self._srv_send_cmd)
        cmd_row.addWidget(self.edit_cmd_input, 1)
        self._add_button(cmd_row, "发送", self._srv_send_cmd)
        lay.addLayout(cmd_row)

        # ---- [MSL UI 合规] 底部服务署名 + 官网跳转（官方强制要求）----
        self.lbl_msl_footer = QLabel(
            '<span style="color:#8ab4f8; text-decoration: underline;">'
            '本服务由 MSL 开服器提供 | 官网地址：https://www.mslmc.cn</span>')
        self.lbl_msl_footer.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_msl_footer.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.lbl_msl_footer.setOpenExternalLinks(False)
        self.lbl_msl_footer.linkActivated.connect(self._open_msl_site)
        lay.addWidget(self.lbl_msl_footer)

        # 初始化：后台拉取服务端类型列表
        run_bg(self._srv_get_types)
        return w

    def _open_msl_site(self, _url: str = ""):
        """[MSL UI 合规] 点击底部署名 -> 打开 MSL 官网。"""
        try:
            open_url_safe(self.MSL_SITE)
            self._log(f"[开服] 已打开 MSL 官网: {self.MSL_SITE}")
        except Exception as exc:
            self._log(f"[开服] 打开 MSL 官网失败: {exc}")

    def _build_settings_tab(self) -> QWidget:
        """独立【设置】页签：单列垂直布局 + QScrollArea 包裹，窗口再小也能往下滚。

        设计原则（避免控件被挤压/透明/消失）：
          1. 每个 QGroupBox 内部用 QFormLayout（左 Label、右控件），
             输入框/下拉框统一 setMinimumWidth(250)，保证有足够显示空间。
          2. 所有 groupbox 纵向排进一个 QVBoxLayout，再放进 QScrollArea。
          3. 控件背景/文字颜色由 apply_background_image() 的全局 QSS 控制，
             这里不单独 setStyleSheet 白底白字，防止和主题 QSS 冲突。
        """
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- QScrollArea 包裹：窗口小也能往下滚，不挤压 ----
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)      # 关键：内容随视口宽自适应
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)   # 横向不滚，纵向滚
        # 滚动区域背景色由 apply_background_image() 的 QSS 控制，这里不单独设白底
        outer.addWidget(scroll)

        content = QWidget()
        content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        clay = QVBoxLayout(content)
        clay.setContentsMargins(8, 8, 8, 8)
        clay.setSpacing(10)
        scroll.setWidget(content)

        # 控件默认宽度：保证 form field 有足够空间
        MINW = 250

        # ---- 1) Java（运行时）----
        java_box = QGroupBox("Java（运行时）")
        jf = QFormLayout(java_box)
        jf.setContentsMargins(10, 10, 10, 10)
        jf.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.java_combo = self._make_java_combo()
        self.java_combo.setMinimumWidth(MINW)
        self.java_status = QLabel("检测中...")
        self.java_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.java_status.setWordWrap(True)
        self.btn_java_rescan = self._mk_button("重新检测", self.refresh_java)
        self.btn_dl_java = self._mk_button("下载 Java", self.download_java)
        jf.addRow("Java 版本", self.java_combo)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_java_rescan)
        btn_row.addWidget(self.btn_dl_java)
        btn_row.addStretch(1)
        jf.addRow(btn_row)
        jf.addRow("检测结果", self.java_status)
        clay.addWidget(java_box)

        # ---- 2) 游戏目录 ----
        dir_box = QGroupBox("游戏目录")
        df = QFormLayout(dir_box)
        df.setContentsMargins(10, 10, 10, 10)
        df.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.dir_combo = QComboBox()
        self.dir_combo.setMinimumWidth(MINW)
        self.dir_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.dir_combo.setMinimumContentsLength(40)
        self.dir_combo.view().setTextElideMode(Qt.TextElideMode.ElideNone)
        self.dir_combo.setToolTip("当前游戏目录（显示完整路径）")
        self.dir_combo.activated.connect(self._switch_dir)
        df.addRow("当前目录", self.dir_combo)
        self.btn_dir_add = self._mk_button("添加目录…", self._add_game_dir)
        self.btn_dir_del = self._mk_button("移除所选", self._remove_game_dir)
        dir_btn_row = QHBoxLayout()
        dir_btn_row.addWidget(self.btn_dir_add)
        dir_btn_row.addWidget(self.btn_dir_del)
        dir_btn_row.addStretch(1)
        df.addRow(dir_btn_row)
        # [FIX-一键修复] 「一键修复游戏目录权限」按钮：生成提权 .bat，夺回所有权
        self.btn_fix_perms = self._mk_button("🔧 一键修复游戏目录权限",
                                             self._fix_dir_perms)
        self.btn_fix_perms.setToolTip(
            f"自动申请管理员权限，对当前游戏目录 {core.MINECRAFT_DIR} "
            "执行 takeown + icacls 完全控制。\n"
            "弹出黑框窗口后请勿关闭，等待完成。")
        fix_row = QHBoxLayout()
        fix_row.addWidget(self.btn_fix_perms)
        fix_row.addStretch(1)
        df.addRow(fix_row)
        clay.addWidget(dir_box)

        # ---- 3) Java 下载目录 ----
        jdir_box = QGroupBox("Java 下载 / 存放目录")
        jd = QFormLayout(jdir_box)
        jd.setContentsMargins(10, 10, 10, 10)
        jd.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.java_dir_edit = QLineEdit()
        self.java_dir_edit.setMinimumWidth(MINW)
        self.java_dir_edit.setClearButtonEnabled(True)
        self.java_dir_edit.editingFinished.connect(self._on_java_dir_edited)
        self.java_dir_edit.textChanged.connect(
            lambda t: self.java_dir_edit.setToolTip(t or "Java 下载/存放目录"))
        jd.addRow("目录", self.java_dir_edit)
        self.btn_java_dir = self._mk_button("浏览…", self._pick_java_dir)
        jd.addRow(self.btn_java_dir)
        clay.addWidget(jdir_box)

        # ---- 4) 运行设置（内存）----
        mem_box = QGroupBox("运行设置（内存）")
        mf = QFormLayout(mem_box)
        mf.setContentsMargins(10, 10, 10, 10)
        mf.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.mem_spin = QSpinBox()
        self.mem_spin.setRange(512, 65536)
        self.mem_spin.setSingleStep(256)
        self.mem_spin.setSuffix(" MB")
        self.mem_spin.setMinimumWidth(180)
        self.mem_spin.valueChanged.connect(lambda _: self._save_settings())
        mf.addRow("内存", self.mem_spin)
        self.auto_mem_check = QCheckBox("自动（按系统内存分配）")
        self.auto_mem_check.setChecked(True)
        self.auto_mem_check.toggled.connect(self._apply_auto_mem)
        mf.addRow(self.auto_mem_check)
        self.sys_mem_label = QLabel(f"（系统 {core.auto_memory_mb()} MB）")
        mf.addRow(self.sys_mem_label)
        clay.addWidget(mem_box)

        # ---- 5) 日志系统 ----
        log_box = QGroupBox("日志系统")
        lf = QFormLayout(log_box)
        lf.setContentsMargins(10, 10, 10, 10)
        lf.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.warn_on_exit_check = QCheckBox(
            "游戏异常退出时弹窗警告（取消勾选 = 彻底关掉“错误码 1”提示）")
        self.warn_on_exit_check.setChecked(True)
        self.warn_on_exit_check.toggled.connect(lambda _: self._save_settings())
        lf.addRow(self.warn_on_exit_check)
        hint = QLabel("勾选：非正常退出（返回码非 0）且不是手动结束时会弹警告框。\n"
                      "取消：一律不弹窗，只在日志里记录退出码。手动结束进程永不弹窗。")
        hint.setWordWrap(True)
        lf.addRow(hint)
        clay.addWidget(log_box)

        # ---- 6) 外置登录（Yggdrasil / Authlib-Injector）----
        skin_box = QGroupBox("皮肤站配置（外置登录 / Yggdrasil）")
        sf = QFormLayout(skin_box)
        sf.setContentsMargins(10, 10, 10, 10)
        sf.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.auth_server_combo = QComboBox()
        self.auth_server_combo.setEditable(True)
        self.auth_server_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.auth_server_combo.setMinimumWidth(MINW)
        self.auth_server_combo.addItems([
            "https://pvp.wiki.mcmod.cn/auth",
            "https://authserver.your-domain.com",
        ])
        self.auth_server_combo.setToolTip("Yggdrasil 认证服务器地址，可编辑/自定义")
        sf.addRow("认证服务器", self.auth_server_combo)

        self.auth_username_edit = QLineEdit()
        self.auth_username_edit.setPlaceholderText("用户名（玩家名）")
        self.auth_username_edit.setMinimumWidth(MINW)
        sf.addRow("用户名", self.auth_username_edit)

        self.auth_password_edit = QLineEdit()
        self.auth_password_edit.setPlaceholderText("密码")
        self.auth_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.auth_password_edit.setMinimumWidth(MINW)
        sf.addRow("密码", self.auth_password_edit)

        self.auth_token_edit = QLineEdit()
        self.auth_token_edit.setPlaceholderText("令牌 / Token（部分皮肤站要求）")
        self.auth_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.auth_token_edit.setMinimumWidth(MINW)
        sf.addRow("令牌", self.auth_token_edit)

        self.btn_auth_login = self._mk_button("登录（Yggdrasil）", self._auth_login, primary=True)
        self.btn_auth_offline = self._mk_button("离线模式（不登录）", self._auth_offline)
        auth_btn_row = QHBoxLayout()
        auth_btn_row.addWidget(self.btn_auth_login)
        auth_btn_row.addWidget(self.btn_auth_offline)
        auth_btn_row.addStretch(1)
        sf.addRow(auth_btn_row)

        self.auth_status_label = QLabel("未登录（离线模式）")
        self.auth_status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.auth_status_label.setWordWrap(True)
        sf.addRow("状态", self.auth_status_label)
        clay.addWidget(skin_box)

        # ---- 7) 系统维护（一键清理垃圾 / 一键释放内存）----
        sys_box = QGroupBox("系统维护")
        sysf = QFormLayout(sys_box)
        sysf.setContentsMargins(10, 10, 10, 10)
        sysf.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # -- 陶瓦联机（P2P）--
        terra_hint = QLabel("基于开源项目 Terracotta 提供 P2P 穿透，房主退出即断开，严禁商业开服")
        terra_hint.setWordWrap(True)
        sysf.addRow(terra_hint)
        self.btn_terracotta = self._mk_button("🌐 陶瓦联机 (P2P)", self._start_terracotta)
        sysf.addRow(self.btn_terracotta)
        # -- 一键清理垃圾 --
        clean_hint = QLabel("扫描 versions/*/logs、crash-reports 及 installer_tmp 的垃圾文件并清理")
        clean_hint.setWordWrap(True)
        sysf.addRow(clean_hint)
        self.btn_clean_junk = self._mk_button("🧹 一键清理垃圾",
                                               self._clean_junk, primary=True)
        sysf.addRow(self.btn_clean_junk)

        # -- 一键释放内存 --
        self.lbl_mem_status = QLabel("当前系统内存占用：计算中...")
        self.lbl_mem_status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        sysf.addRow("内存", self.lbl_mem_status)
        self.btn_clear_mem = self._mk_button("💥 释放系统内存",
                                              self._clear_mem, primary=True)
        sysf.addRow(self.btn_clear_mem)
        self.chk_clear_mem_before_launch = QCheckBox(
            "启动游戏前自动释放内存")
        self.chk_clear_mem_before_launch.setChecked(
            bool(self.cfg.get("clear_mem_before_launch", False)))
        self.chk_clear_mem_before_launch.toggled.connect(
            lambda _: self._save_settings())
        sysf.addRow(self.chk_clear_mem_before_launch)
        clay.addWidget(sys_box)

        # ---- 版本号与自动更新 ----
        ver_layout = QHBoxLayout()
        ver_layout.addWidget(QLabel(f"当前版本：v{CURRENT_VERSION}"))
        self.btn_check_update = self._mk_button(
            "🔄 检查更新", lambda: self._check_update(silent=False))
        ver_layout.addWidget(self.btn_check_update)
        ver_layout.addStretch(1)
        clay.addLayout(ver_layout)

        # ---- 8) 界面美化：默认主题（深/浅） + 自定义背景图 ----
        bg_box = QGroupBox("界面美化")
        bf = QFormLayout(bg_box)
        bf.setContentsMargins(10, 10, 10, 10)
        bf.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.bg_theme_combo = QComboBox()
        self.bg_theme_combo.addItems(["深色", "浅色"])
        self.bg_theme_combo.setCurrentIndex(0)
        self.bg_theme_combo.setMinimumWidth(MINW)
        self.bg_theme_combo.setToolTip("无背景图时的默认主题（深色 / 浅色）")
        bf.addRow("默认主题", self.bg_theme_combo)

        self.bg_image_label = QLabel("未设置")
        self.bg_image_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        bf.addRow("背景图", self.bg_image_label)

        self.btn_bg_pick = self._mk_button("选择背景图…", self._pick_background_image)
        self.btn_bg_clear = self._mk_button("清除", self._clear_background_image)
        bg_btn_row = QHBoxLayout()
        bg_btn_row.addWidget(self.btn_bg_pick)
        bg_btn_row.addWidget(self.btn_bg_clear)
        bg_btn_row.addStretch(1)
        bf.addRow(bg_btn_row)

        # 主题切换 / 背景图变化即时生效
        self.bg_theme_combo.currentIndexChanged.connect(
            lambda _: (self._save_settings(), self.apply_background_image()))
        clay.addWidget(bg_box)

        # ---- MSL 官网入口按钮 ----
        portal_row = QHBoxLayout()
        self.btn_portal = self._mk_button("🌐 打开 MSL 官网",
                                          lambda: open_url_safe("https://www.mslmc.cn"))
        portal_row.addWidget(self.btn_portal)
        portal_row.addStretch(1)
        clay.addLayout(portal_row)

        clay.addStretch(1)   # 底部弹簧，内容贴顶
        return w

    def _build_status_box(self) -> QWidget:
        """底部「操作状态」框：固定最小高度，不被中间 tab 内容拉伸。

        用 QSizePolicy.Fixed(高度) 保证状态栏始终贴底、紧凑，
        设置页/其他页空白再多也不会把进度条拉得很长。
        """
        box = QGroupBox("操作状态", self)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(10, 6, 10, 8)
        lay.setSpacing(4)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFixedHeight(20)
        lay.addWidget(self.progress_bar)

        row = QHBoxLayout()
        self.status_label = QLabel("就绪（详细日志请查看控制台或「查看日志」）")
        self.status_label.setStyleSheet("color:#333;")
        self.status_label.setSizePolicy(QSizePolicy.Policy.Expanding,
                                        QSizePolicy.Policy.Fixed)
        row.addWidget(self.status_label, 1)
        self.btn_show_log = QPushButton("查看日志")
        self.btn_show_log.clicked.connect(self._show_log_dialog)
        row.addWidget(self.btn_show_log)
        lay.addLayout(row)

        # 整个状态框高度固定：不被外层 QVBoxLayout 的 stretch 拉高
        box.setSizePolicy(QSizePolicy.Policy.Expanding,
                         QSizePolicy.Policy.Fixed)
        return box

    # ==================================================================
    # 配置读写 / 目录管理
    # ==================================================================
    def _lang_code(self) -> str:
        idx = self.lang_combo.currentIndex()
        return LANGUAGES[idx][1] if 0 <= idx < len(LANGUAGES) else "zh_CN"

    def _load_cfg_into_ui(self):
        self.name_combo.setEditText(self.cfg.get("username", "Steve"))
        self.jvm_edit.setText(self.cfg.get("jvm", ""))
        lang = self.cfg.get("language", "zh_CN")
        for i, (_, code) in enumerate(LANGUAGES):
            if code == lang:
                self.lang_combo.setCurrentIndex(i)
                break
        self.java_dir_edit.setText(str(core.JAVA_DIR))
        self.mem_spin.setValue(int(self.cfg.get("mem", core.auto_memory_mb()) or 2048))
        self.auto_mem_check.setChecked(True)
        self.mem_spin.setEnabled(False)
        self.mem_spin.setValue(core.auto_memory_mb())
        # 日志系统：游戏异常退出时是否弹窗（默认开）
        self.warn_on_exit_check.setChecked(
            bool(self.cfg.get("warn_on_exit", True)))
        # 外置登录（Yggdrasil）：回填服务器 / 用户名 / 密码 / 令牌
        srv = self.cfg.get("auth_server", "")
        if srv:
            i = self.auth_server_combo.findText(srv)
            if i >= 0:
                self.auth_server_combo.setCurrentIndex(i)
            else:
                self.auth_server_combo.setEditText(srv)
        self.auth_username_edit.setText(self.cfg.get("auth_username", ""))
        # 安全：绝不从配置恢复密码（历史版本可能写过，这里也一并忽略）
        self.auth_password_edit.clear()
        self.cfg.pop("auth_password", None)
        self.auth_token_edit.setText(self.cfg.get("auth_token", ""))
        # ★ 自动恢复登录态（有 auth_session 就还原 UI，不联网）
        self._restored_auth = self._load_auth_session()
        self._update_auth_status_label()
        # 背景图 + 默认主题（0=深色，1=浅色）
        self._bg_image_path = self.cfg.get("background_image", "")
        if self._bg_image_path:
            self.bg_image_label.setText(self._bg_image_path)
        self._select_bg_theme(dark=bool(self.cfg.get("bg_theme", 0)))

    def _update_auth_status_label(self):
        """刷新【皮肤站配置】状态文本：区分「离线 / 拖拽待命 / 已换 Token」。"""
        srv = self.auth_server_combo.currentText().strip()
        user = self.auth_username_edit.text().strip()
        sess = self._auth_session
        if sess and sess.get("server"):
            mode = sess.get("mode") or ("token" if sess.get("accessToken") else "drag")
            name = sess.get("username") or user or "（未填用户名）"
            profiles = sess.get("profiles") or []
            restored = bool(sess.get("restored"))
            suffix = "（自动恢复）" if restored else ""
            if mode == "token":
                lines = [
                    f"✔ 外置登录已就绪{suffix}（已换取 Token）",
                    f"服务器：{sess['server']}",
                    f"账号：{name}",
                ]
                if profiles:
                    lines.append(f"角色（{len(profiles)} 个）："
                                 + "、".join(p.get("name", "") for p in profiles[:6])
                                 + ("…" if len(profiles) > 6 else ""))
                    cur_name = (self.name_combo.currentText() or "").strip()
                    cur_uuid = self.name_combo.currentData() or ""
                    if cur_name:
                        lines.append(f"当前启动角色：{cur_name}")
                    if cur_uuid:
                        lines.append(f"UUID：{cur_uuid}")
                else:
                    lines.append(f"UUID：{sess.get('uuid') or '（启动时生成）'}")
                self.auth_status_label.setText("\n".join(lines))
                self.auth_status_label.setStyleSheet("color:#2a7;")
            else:
                self.auth_status_label.setText(
                    f"✔ 外置登录已就绪{suffix}（拖拽模式，由注入器接管验证）\n"
                    f"服务器：{sess['server']}\n"
                    f"玩家：{name}（启动后以皮肤站账号为准）")
                self.auth_status_label.setStyleSheet("color:#27a;")
        elif srv and user:
            self.auth_status_label.setText(
                f"已配置认证服务器（未登录）：{srv}\n用户：{user}\n"
                f"点「登录（Yggdrasil）」换取 Token，或直接启动由注入器接管。")
            self.auth_status_label.setStyleSheet("color:#a80;")
        elif srv:
            self.auth_status_label.setText(
                f"已配置认证服务器（未登录）：{srv}\n"
                f"点「登录（Yggdrasil）」换取 Token，或直接启动由注入器接管。")
            self.auth_status_label.setStyleSheet("color:#a80;")
        else:
            self.auth_status_label.setText("未登录（离线模式）")
            self.auth_status_label.setStyleSheet("color:#888;")

    def _save_settings(self):
        """保存全部设置。★ 绝不写入密码（auth_password 已被清除）。"""
        self.cfg.update({
            "game_dirs": list(self.game_dirs),
            "java_dir": self.java_dir_edit.text().strip() or str(core.DEFAULT_JAVA_DIR),
            "username": (self.name_combo.currentText() or "").strip() or "Steve",
            "mem": int(self.mem_spin.value()),
            "jvm": self.jvm_edit.text().strip(),
            "language": self._lang_code(),
            "warn_on_exit": self.warn_on_exit_check.isChecked(),
            "auth_server": self.auth_server_combo.currentText().strip(),
            "auth_username": self.auth_username_edit.text().strip(),
            "auth_token": self.auth_token_edit.text(),
            "auth_selected_uuid": self.name_combo.currentData() or "",
            "background_image": getattr(self, "_bg_image_path", ""),
            "bg_theme": self.bg_theme_combo.currentIndex(),
            "clear_mem_before_launch": self.chk_clear_mem_before_launch.isChecked(),
        })
        # 安全：旧版本曾经把密码写进 config.json，这里主动抹掉
        for k in ("auth_password",):
            if k in self.cfg:
                self.cfg.pop(k, None)
        ok = core.save_config(self.cfg)
        # [FIX-免责弹窗重复] 免责类标记同步镜像到启动器目录，主配置写不了也能持久化
        for flag in ("agreed_to_disclaimer", "agreed_to_server_disclaimer"):
            if self.cfg.get(flag, False):
                _persist_local_override(flag, True)
        if not ok:
            self._log(f"[设置] 主配置写入失败（%APPDATA% 被锁/杀软拦截），"
                      f"免责类状态已镜像到启动器目录 local_cfg.json")

    # ==================================================================
    # 外置登录会话的本地持久化（config.json，绝不存密码）
    # ==================================================================
    def _save_auth_session(self, session: dict | None = None):
        """把登录态写进 config.json。

        保存字段（全部为非敏感信息，**不含密码**）：
          auth_session = {
            "server":       皮肤站 Yggdrasil API 根地址,
            "accessToken":  真实 accessToken（用于下次启动直接复用）,
            "clientToken":  clientToken,
            "uuid":         当前选中角色的真实 UUID（无横杠）,
            "uuid_dashed":  带横杠版本（备用）,
            "username":     当前选中角色的名字,
            "profiles":     [{"id","name"}, ...] 全部角色（离线也能还原下拉框）,
            "selected":     当前选中角色名,
            "mode":         "token" / "drag",
            "saved_at":     保存时间戳,
          }
        session 为 None 时取 self._auth_session。
        """
        sess = session if session is not None else self._auth_session
        if not sess or not sess.get("server"):
            # 没有登录态 -> 清掉本地记录
            self.cfg.pop("auth_session", None)
            core.save_config(self.cfg)
            return
        name, uid = self._current_player()
        record = {
            "server": sess.get("server") or sess.get("authserver") or "",
            "accessToken": sess.get("accessToken") or sess.get("access_token") or "",
            "clientToken": sess.get("clientToken") or "",
            "uuid": uid or sess.get("uuid") or "",
            "uuid_dashed": sess.get("uuid_dashed") or "",
            "username": name or sess.get("username") or "",
            "profiles": list(sess.get("profiles") or []),
            "selected": name or sess.get("selected") or "",
            "mode": sess.get("mode") or ("token" if sess.get("accessToken") else "drag"),
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        # 显式确认不含密码字段
        record.pop("password", None)
        self.cfg["auth_session"] = record
        core.save_config(self.cfg)
        self._log(f"[外置登录] 登录态已保存到配置：{record['username']} "
                  f"uuid={record['uuid']} @ {record['server']}（不含密码）")

    def _load_auth_session(self) -> bool:
        """启动器初始化时读取本地登录态，恢复 UI。

        返回 True 表示「已自动恢复登录」。恢复后不会立即联网校验
        （避免启动时卡网络），真正的过期检查放在 launch() 之前。
        """
        rec = self.cfg.get("auth_session")
        if not isinstance(rec, dict) or not rec.get("server"):
            return False
        # [FIX-脏数据防护] 旧配置可能存了非字符串的 server（None/数字/嵌套结构），
        # 直接 return False，防止后续字符串操作报错导致启动闪退。
        srv_check = rec.get("server")
        if not isinstance(srv_check, str) or not srv_check.strip():
            self._log(f"[外置登录] 本地登录态 server 字段非法（{type(srv_check).__name__}），忽略旧记录")
            return False
        token = rec.get("accessToken") or ""
        profiles = rec.get("profiles") or []
        # [FIX-登录态恢复] 只要 accessToken + server 齐全，哪怕 profiles 空、uuid 空，
        # 也必须判定为「可恢复」（皮肤站选角后 availableProfiles 常缺失，但 token 仍有效）。
        # 另外：有角色列表、或纯拖拽模式（无 token）也算有效登录态。
        if not (
            (token and rec.get("server"))
            or profiles
            or rec.get("mode") == "drag"
        ):
            self._log("[外置登录] 本地登录态缺少 token/角色，忽略")
            return False

        self._auth_session = {
            "server": rec.get("server", ""),
            "authserver": rec.get("server", ""),
            "accessToken": token,
            "access_token": token,
            "clientToken": rec.get("clientToken", ""),
            "uuid": rec.get("uuid", ""),
            "uuid_dashed": rec.get("uuid_dashed", ""),
            "username": rec.get("username", ""),
            "password": "",                       # 永远不从配置恢复密码
            "profiles": profiles,
            "selected": rec.get("selected", ""),
            "mode": rec.get("mode") or ("token" if token else "drag"),
            "restored": True,                     # 标记：来自本地恢复，未经联网校验
        }
        # 回填设置页的服务器/用户名（密码留空，等玩家自己输）
        srv = rec.get("server", "")
        if srv:
            i = self.auth_server_combo.findText(srv)
            if i >= 0:
                self.auth_server_combo.setCurrentIndex(i)
            else:
                self.auth_server_combo.setEditText(srv)
        if rec.get("username"):
            self.auth_username_edit.setText(rec["username"])
        self.auth_password_edit.clear()
        # 回填角色下拉框（离线也能用上次的角色列表）
        self.offline_check.blockSignals(True)
        self.offline_check.setChecked(False)
        self.offline_check.blockSignals(False)
        # [FIX-登录态恢复] profiles 为空但 username 有值：下拉框显示该 username（可手输）
        if profiles:
            self._fill_name_combo_from_profiles(
                profiles, rec.get("selected") or rec.get("username") or "")
        else:
            uname = (rec.get("username") or "").strip()
            self._auth_profiles = []
            self.name_combo.blockSignals(True)
            self.name_combo.clear()
            if uname:
                self.name_combo.addItem(uname, rec.get("uuid") or "")
                self.name_combo.setCurrentIndex(0)
            else:
                self.name_combo.setEditText("Steve")
            self.name_combo.blockSignals(False)
            self._selected_uuid = self.name_combo.currentData() or rec.get("uuid") or ""
            self._refresh_name_combo_editability()
        self._update_auth_status_label()
        self._log(f"[外置登录] 已从配置恢复登录态（自动恢复）："
                  f"{rec.get('username')} @ {srv}"
                  f"（保存于 {rec.get('saved_at', '未知时间')}）")
        return True

    def _apply_auto_mem(self, checked: bool):
        self.mem_spin.setEnabled(not checked)
        if checked:
            self.mem_spin.setValue(core.auto_memory_mb())
        self._save_settings()

    def _refresh_dir_combo(self):
        self._loading_dirs = True
        self.dir_combo.clear()
        for i, d in enumerate(self.game_dirs):
            self.dir_combo.addItem(f"[{i + 1}] {d}")
            self.dir_combo.setItemData(i, d, Qt.ItemDataRole.ToolTipRole)
        if self.game_dirs:
            self.dir_combo.setCurrentIndex(min(self.current_dir_idx, len(self.game_dirs) - 1))
        self._loading_dirs = False

    def _switch_dir(self, idx: int):
        if self._loading_dirs or idx < 0 or idx >= len(self.game_dirs):
            return
        selected = self.game_dirs.pop(idx)
        self.game_dirs.insert(0, selected)
        self.current_dir_idx = 0
        self._save_settings()
        core.apply_paths(self.cfg)
        self._refresh_dir_combo()
        self._log(f"切换目录: {selected}")
        self._refresh_versions_bg()

    def _add_game_dir(self):
        initial = self.game_dirs[self.current_dir_idx] if self.game_dirs else str(core.DEFAULT_MINECRAFT_DIR)
        d = QFileDialog.getExistingDirectory(self, "选择 Minecraft 游戏目录", initial)
        if not d:
            self._log("已取消选择")
            return
        if d in self.game_dirs:
            QMessageBox.information(self, "提示", f"目录已存在：\n{d}")
            return
        self.game_dirs.append(d)
        self._save_settings()
        self._refresh_dir_combo()
        self._log(f"已添加: {d}")

    # ==================================================================
    # [一键修复] 游戏目录权限修复（takeown + icacls，subprocess 列表传参）
    # ==================================================================
    def _fix_dir_perms(self):
        """一键修复当前游戏目录的所有权与完全控制权限。

        [重写] 废弃旧版「生成 UAC 提权 .bat + ctypes 复杂逻辑」（会报
        WinError 87 参数错误），改为最标准的 subprocess.run 列表传参：
          1. 弹窗确认（主线程）。
          2. run_bg 后台线程里直接 subprocess.run 调 takeown / icacls，
             列表传参（路径不带任何引号，自动处理空格/特殊字符），
             CREATE_NO_WINDOW 静默执行，运行中 UI 显示「正在修复权限...」。
          3. 完成/失败通过 bridge.info / bridge.warn 回主线程弹窗提示。
        """
        target_dir = str(core.MINECRAFT_DIR)
        ans = QMessageBox.question(
            self, "一键修复游戏目录权限",
            "即将修复当前游戏目录的所有权与完全控制权限（takeown + icacls）。\n"
            f"目标目录：{target_dir}\n\n"
            "该操作可能在后台运行数十秒，是否需要继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if ans != QMessageBox.StandardButton.Yes:
            self._log("[一键修复] 已取消")
            return
        self.btn_fix_perms.setEnabled(False)
        self.btn_fix_perms.setText("正在修复权限...")
        self._log(f"[一键修复] 开始修复 {target_dir} ...")
        self.bridge.status.emit("正在修复游戏目录权限...")
        run_bg(self._fix_dir_perms_bg, target_dir)

    def _fix_dir_perms_bg(self, target_dir: str):
        """[后台线程] 触发 UAC 提权并修复权限（takeown + icacls）。

        [重写] 普通用户权限下直接 subprocess.run takeown/icacls 必然失败
        （无所有权），因此改用 run_as_admin（ShellExecuteW "runas" 动词）
        触发 Windows UAC 确认弹窗，以管理员身份静默执行。
        路径在命令字符串里用双引号包裹，空格/特殊字符交给 cmd 解析。
        """
        try:
            # 拼接命令：先 takeown 夺所有权，再 icacls 赋完全控制
            # & 串联（前一条成功/失败都执行下一条，icacls 的 /C 会跳过被占用文件）
            cmd_str = (
                f'takeown /F "{target_dir}" /R /D Y & '
                f'icacls "{target_dir}" /grant Users:(OI)(CI)F /T /C /Q'
            )
            self._log(f"[一键修复] 发起 UAC 提权请求（管理员执行 takeown + icacls）...")
            # 会弹出 Windows UAC 确认框；用户点"是"即以后台静默执行
            run_as_admin(cmd_str, wait=False)
            self._log(f"[一键修复] 提权命令已执行（黑框一闪而过即完成）")
            self.bridge.status.emit("权限修复命令已执行")
            self.bridge.info.emit(
                "权限修复已发起",
                f"已弹出管理员权限请求（UAC）。\n"
                f"如果黑框一闪而过，说明修复完成。\n\n"
                f"目标目录：{target_dir}\n\n"
                "您可以尝试重新启动游戏，或右键该文件夹 → 属性 → 安全"
                "查看是否已获得完全控制权限。")
        except Exception as e:
            self._log(f"[一键修复] 提权失败或被取消: {e}")
            self.bridge.status.emit("权限修复取消")
            self.bridge.warn.emit(
                "权限修复取消",
                f"未能获取管理员权限（UAC 弹窗被取消或发生错误）：\n{e}\n\n"
                f"如确认需要以管理员身份运行：\n"
                f"右键启动器 → 以管理员身份运行，再点「一键修复」。")
        finally:
            # 恢复按钮（主线程经信号排队）
            self.bridge.permsFixedDone.emit()

    def _on_perms_fixed_done(self):
        """permsFixedDone 信号槽（主线程）：复位一键修复按钮。"""
        self._reset_fix_perms_btn()

    def _reset_fix_perms_btn(self):
        self.btn_fix_perms.setEnabled(True)
        self.btn_fix_perms.setText("🔧 一键修复游戏目录权限")

    def _remove_game_dir(self):
        if len(self.game_dirs) <= 1:
            QMessageBox.warning(self, "提示", "至少需要保留一个游戏目录")
            return
        idx = self.dir_combo.currentIndex()
        if idx < 0:
            idx = self.current_dir_idx
        del self.game_dirs[idx]
        if idx >= len(self.game_dirs):
            idx = len(self.game_dirs) - 1
        self.current_dir_idx = idx
        self._save_settings()
        core.apply_paths(self.cfg)
        self._refresh_dir_combo()
        self._refresh_versions_bg()

    def _pick_java_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择 Java 下载/存放目录",
                                             self.java_dir_edit.text())
        if d:
            self.java_dir_edit.setText(d)
            self._on_java_dir_edited()

    def _on_java_dir_edited(self):
        self._save_settings()
        core.apply_paths(self.cfg)
        self.refresh_java()

    # ==================================================================
    # 系统维护：一键清理垃圾 + 一键释放内存（耗时操作全部走 run_bg 后台线程）
    # ==================================================================
    def _clean_junk(self):
        """点「🧹 一键清理垃圾」：后台统计 -> 信号回主线程确认 -> 后台删除 -> 信号回主线程弹窗。

        严格 PyQt6 线程规范：后台线程只做文件遍历与 os.remove，绝不碰任何
        Qt 控件 / 弹窗（直接碰会触发 QObject::setParent / QBackingStore 报错）。
        所有 UI 交互都经 self.bridge 信号排队回主线程槽函数执行。
        """
        self.btn_clean_junk.setEnabled(False)
        self.btn_clean_junk.setText("统计中...")
        self._log("[清理] 开始统计垃圾文件 ...")

        def _scan():
            """[后台] 仅做文件遍历 + 计算，完成后发 junkScanned 信号回主线程。"""
            files = []  # (Path, size_bytes)
            try:
                versions_dir = core.MINECRAFT_DIR / "versions"
                if versions_dir.is_dir():
                    for vdir in versions_dir.iterdir():
                        for sub in ("logs", "crash-reports"):
                            d = vdir / sub
                            if d.is_dir():
                                for f in d.rglob("*"):
                                    if f.is_file():
                                        try:
                                            files.append((f, f.stat().st_size))
                                        except OSError:
                                            pass
                tmp_dir = core.MINECRAFT_DIR / "installer_tmp"
                if tmp_dir.is_dir():
                    for f in tmp_dir.iterdir():
                        if f.is_file():
                            try:
                                files.append((f, f.stat().st_size))
                            except OSError:
                                pass
            except Exception as exc:
                # 统计本身出错：发 error 信号（主线程弹窗），并复位按钮
                self._log(f"[清理] 统计出错: {exc}")
                self.bridge.error.emit("清理失败", f"统计垃圾文件时出错：{exc}")
                self.bridge.junkScanned.emit(-1, 0)   # -1 标记「出错」
                return
            # 缓存本次统计结果（含路径），供删除线程使用；同时发信号回主线程
            self._junk_files = files
            n = len(files)
            total = sum(s for _, s in files)
            self._log(f"[清理] 统计到 {n} 个垃圾文件，共 {total/1048576:.1f} MB")
            self.bridge.junkScanned.emit(n, total)   # -> on_junk_scanned（主线程）

        run_bg(_scan)

    def on_junk_scanned(self, n: int, total_bytes: int):
        """junkScanned 信号槽（主线程）：弹确认框，玩家同意后启动删除线程。"""
        if n == -1:
            # 统计出错，仅复位按钮（error 弹窗已由 bridge 处理）
            self._reset_clean_junk_btn()
            return
        if n == 0:
            self.bridge.info.emit("无需清理", "没有发现垃圾文件，目录很干净 ✅")
            self._reset_clean_junk_btn()
            return
        total_mb = total_bytes / 1048576
        # 主线程弹窗（此处合法：在事件循环里）
        ans = QMessageBox.question(
            self, "一键清理垃圾",
            f"发现 {n} 个垃圾文件，共占用 {total_mb:.1f} MB，是否清理？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans == QMessageBox.StandardButton.Yes:
            self.btn_clean_junk.setEnabled(False)
            self.btn_clean_junk.setText("清理中...")
            self._log("[清理] 玩家确认，开始后台删除 ...")
            run_bg(self._clean_junk_delete_bg)
        else:
            self._reset_clean_junk_btn()

    def _clean_junk_delete_bg(self):
        """[后台] 执行 os.remove 删除；完成后发 junkCleaned 信号回主线程弹窗。"""
        freed = 0
        skipped = 0
        for f, size in getattr(self, "_junk_files", []):
            try:
                f.unlink()
                freed += size
            except PermissionError:
                skipped += 1
                self._log(f"[清理] 跳过（被占用）: {f}")
            except OSError as exc:
                skipped += 1
                self._log(f"[清理] 跳过: {f} ({exc})")
        # 发信号回主线程（绝不在此弹窗 / 碰控件）
        self.bridge.junkCleaned.emit(freed, skipped, len(getattr(self, "_junk_files", [])))

    def on_junk_cleaned(self, freed_bytes: int, skipped: int, total: int):
        """junkCleaned 信号槽（主线程）：弹窗「清理完毕」+ 复位按钮。"""
        freed_mb = freed_bytes / 1048576
        msg = f"✅ 清理完毕！释放了 {freed_mb:.2f} MB 空间。"
        if skipped:
            msg += f"\n（{skipped} 个文件被占用，已跳过）"
        self._log(f"[清理] 删除 {total - skipped} 个，跳过 {skipped} 个，释放 {freed_mb:.2f} MB")
        self.bridge.info.emit("清理完成", msg)
        self._reset_clean_junk_btn()

    def _reset_clean_junk_btn(self):
        self.btn_clean_junk.setEnabled(True)
        self.btn_clean_junk.setText("🧹 一键清理垃圾")

    def _clear_mem(self):
        """点「💥 释放系统内存」：后台 EmptyWorkingSet，完成弹窗。"""
        self.btn_clear_mem.setEnabled(False)
        self.btn_clear_mem.setText("清理中...")
        self._log("[内存] 开始释放系统内存 ...")

        def _do_clear():
            """[后台] 仅用 psapi.EmptyWorkingSet 释放内存，完成后发 memCleared 信号。

            严格线程规范：后台只做 ctypes / psutil 调用，绝不碰 Qt 控件；
            Label 刷新与弹窗全部交给主线程槽 on_mem_cleared。
            """
            import ctypes
            try:
                import psutil
                mem_before = int(psutil.virtual_memory().percent)
                for proc in psutil.process_iter(['pid']):
                    try:
                        p_handle = ctypes.windll.kernel32.OpenProcess(
                            0x1F0FFF, False, proc.info['pid'])
                        if p_handle:
                            ctypes.windll.psapi.EmptyWorkingSet(p_handle)
                            ctypes.windll.kernel32.CloseHandle(p_handle)
                    except Exception:
                        pass
                mem_after = int(psutil.virtual_memory().percent)
                self._log(f"[内存] 释放前 {mem_before}% -> 释放后 {mem_after}%")
                # 发信号回主线程（成功路径；含 -1 标记也走这里，主线程据 before==-1 区分）
                self.bridge.memCleared.emit(mem_before, mem_after)
            except Exception as exc:
                self._log(f"[内存] 释放失败: {exc}")
                # 失败路径：before=-1 表示出错，主线程槽里弹 warn + 复位按钮
                self.bridge.memCleared.emit(-1, -1)

        run_bg(_do_clear)

    def on_mem_cleared(self, before: int, after: int):
        """memCleared 信号槽（主线程）：刷新 Label + 弹窗 + 复位按钮。"""
        self.btn_clear_mem.setEnabled(True)
        self.btn_clear_mem.setText("💥 释放系统内存")
        self._refresh_mem_label()
        if before < 0:
            # 后台释放失败：主线程弹 warn（弹窗必须在主线程）
            self.bridge.warn.emit(
                "内存清理失败",
                "释放系统内存时出错。\n\n"
                "若提示权限不足，请以管理员身份运行启动器后重试。")
            return
        drop = before - after
        if drop < 1:
            self.bridge.info.emit(
                "内存清理完毕",
                f"✅ 内存清理完毕！系统内存占用从 {before}% 降至 {after}%。\n"
                f"当前内存非常健康，无需清理。")
        else:
            self.bridge.info.emit(
                "内存清理完毕",
                f"✅ 内存清理完毕！系统内存占用从 {before}% 降至 {after}%。")

    def _refresh_mem_label(self):
        """主线程：刷新内存占用 Label（QTimer 每 5s 调一次）。"""
        try:
            import psutil
            pct = psutil.virtual_memory().percent
            self.lbl_mem_status.setText(f"当前系统内存占用：{pct}%")
        except Exception:
            self.lbl_mem_status.setText("当前系统内存占用：未知")

    def _silent_clear_mem_before_launch(self, log):
        """launch() 启动前：若勾选「启动前自动释放内存」，后台静默清理一次。"""
        if not self.chk_clear_mem_before_launch.isChecked():
            return
        log("[启动] 已勾选「启动前自动释放内存」，后台执行一次 ...")

        def _bg():
            try:
                import ctypes
                import psutil
                before = psutil.virtual_memory().percent
                for proc in psutil.process_iter(['pid']):
                    try:
                        h = ctypes.windll.kernel32.OpenProcess(0x1F0FFF, False, proc.info['pid'])
                        if h:
                            ctypes.windll.psapi.EmptyWorkingSet(h)
                            ctypes.windll.kernel32.CloseHandle(h)
                    except Exception:
                        pass
                after = psutil.virtual_memory().percent
                log(f"[启动] 内存释放前 {before}% -> 后 {after}%")
            except Exception as exc:
                log(f"[启动] 内存自动释放失败（忽略）: {exc}")
        run_bg(_bg)

    # ==================================================================
    # 【开服工具】服务端下载（MSL 官方 API v4）+ 一键开服 + 实时控制台
    #   - 所有耗时操作走 run_bg 后台线程，UI 一律经 self.bridge 信号回主线程
    #   - MSL 官方接口规范（见 https://www.mslmc.cn 文档）：
    #       GET {MSL_API}/mirrors?view=list          -> data: list[str]
    #       GET {MSL_API}/mirrors/{server}           -> data: {description, versions}
    #       GET {MSL_API}/download/server/{s}/{v}    -> data: {url, sha256?}
    #     所有请求必须带 User-Agent: PyMC-Launcher/1.0（官方统计用）
    #     防滥用限制：下载接口 1小时30次 / 1天60次，失败不自动重试。
    # ==================================================================
    MSL_API = "https://api.mslmc.cn/v4"
    MSL_UA = {"User-Agent": "PyMC-Launcher/1.0"}
    MSL_SITE = "https://www.mslmc.cn"

    @staticmethod
    def _msl_get(url: str, timeout: int = 15):
        """[MSL 官方规范] GET MSL API 返回 json。

        严格带 Header User-Agent: PyMC-Launcher/1.0；先判断 code==200 再取
        data；请求/解析失败抛带 status_code + 响应片段的异常。
        所有调用处必须 try/except 并记日志，失败后不自动重试（防刷）。
        """
        headers = dict(MainWindow.MSL_UA)
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
        except Exception as exc:
            raise RuntimeError(f"MSL 请求异常: {exc}") from exc
        if r.status_code != 200:
            raise RuntimeError(
                f"MSL HTTP {r.status_code}，响应片段: {r.text[:200]}")
        try:
            data = r.json()
        except Exception as exc:
            raise RuntimeError(
                f"MSL 响应非 JSON（HTTP {r.status_code}），片段: "
                f"{r.text[:200]}") from exc
        if not isinstance(data, dict) or data.get("code") != 200:
            raise RuntimeError(
                f"MSL 业务码非 200: code={data.get('code') if isinstance(data, dict) else None} "
                f"message={data.get('message') if isinstance(data, dict) else ''} "
                f"片段: {str(data)[:200]}")
        return data

    def _srv_get_types(self):
        """[后台线程] 拉取 MSL 服务端类型列表，emit 信号回主线程填充下拉框。

        官方接口：GET {MSL_API}/mirrors?view=list，data 为数组。
        跨线程安全：本函数只在后台线程跑，绝不直接操作 UI 控件（会触发
        QObject::setParent/QBackingStore 报错）。结果一律经 self.bridge.srvTypesReady
        信号排队回主线程槽 on_srv_types_ready 里填充。
        失败仅记日志 + 提示，不自动重试（防刷）。
        """
        try:
            # [MSL 官方规范] 类型列表：/mirrors?view=list（data 为 list）
            data = self._msl_get(f"{self.MSL_API}/mirrors?view=list")
            server_list = data.get("data")
            if not isinstance(server_list, list):
                raise RuntimeError(
                    f"接口 data 非 list（实际 {type(server_list).__name__}）: "
                    f"{str(server_list)[:200]}")
            # 过滤非字符串项，防 addItems 崩
            server_list = [t for t in server_list if isinstance(t, str)]
            # 跨线程：只 emit，不在后台碰 UI
            self.bridge.srvTypesReady.emit(server_list)
        except Exception as exc:
            # 防刷：只记日志，不自动重试
            self._log(f"[开服] 获取服务端类型失败（不自动重试）: {exc}")
            self.bridge.error.emit(
                "获取服务端类型失败",
                f"MSL 接口请求失败：{exc}\n\n"
                f"受防滥用限制，请勿频繁重试。请检查网络后稍后再试。")

    def _srv_on_type_changed(self):
        """类型切换（主线程）：后台拉取该类型的版本列表。

        [FIX-版本下拉框为空] 被 currentIndexChanged + on_srv_types_ready
        两处显式调用；函数体不接收额外参数（lambda 已过滤 index）。
        """
        srv = (self.combo_srv_type.currentText() or "").strip()
        if not srv:
            return
        self.combo_srv_ver.blockSignals(True)
        self.combo_srv_ver.clear()
        self.combo_srv_ver.blockSignals(False)
        self.lbl_srv_desc.setText(f"正在加载 {srv} 的版本列表...")
        self.btn_download_srv.setEnabled(False)
        self._log(f"[开服] 准备请求 {srv} 的版本列表")
        run_bg(self._srv_get_versions, srv)

    def _srv_get_versions(self, srv: str):
        """[后台线程] 拉取指定服务端类型的版本列表 + description。

        官方接口：GET {MSL_API}/mirrors/{server}（不带 ?view=classify），
        data 为 dict，含 description + versions 数组。
        跨线程安全：本函数只在后台线程跑，绝不直接操作 UI 控件。结果经
        self.bridge.srvVersionsReady 信号排队回主线程槽 on_srv_versions_ready 里渲染。
        [修复] 加死日志，绝不静默报错——每一步都打印，方便定位断点。
        """
        self._log(f"[开服] 开始请求 /mirrors/{srv} ...")
        try:
            data = self._msl_get(f"{self.MSL_API}/mirrors/{srv}")
            self._log(f"[开服] 版本接口 HTTP 成功，data 类型={type(data.get('data')).__name__}")
            d = data.get("data")
            if not isinstance(d, dict):
                raise RuntimeError(f"data 非 dict，原始: {str(d)[:200]}")
            desc = (d.get("description") or "").strip()
            versions = d.get("versions", [])
            if not isinstance(versions, list):
                raise RuntimeError(f"versions 字段不是列表，实际={type(versions).__name__}: {str(versions)[:200]}")
            versions = [str(v) for v in versions]
            self._log(f"[开服] 成功获取 {srv} 的 {len(versions)} 个版本")
            # 跨线程：只 emit，不在后台碰 UI
            self.bridge.srvVersionsReady.emit(versions, desc)
        except Exception as exc:
            self._log(f"[开服] 获取 {srv} 版本列表失败: {exc}")
            self.bridge.error.emit("获取版本失败",
                                   f"获取 {srv} 版本列表失败：\n{exc}\n\n"
                                   f"受 MSL 防滥用限制，请勿频繁重试。")
            # 跨线程安全：错误也经信号回主线程显示在 lbl_srv_desc
            self.bridge.srvVersionsReady.emit([], f"加载 {srv} 版本失败：{exc}")

    def _download_srv(self):
        """点「⬇ 下载服务端」：免责拦截 -> 后台拉直链并下载 -> 弹 EULA 询问。"""
        srv = (self.combo_srv_type.currentText() or "").strip()
        ver = (self.combo_srv_ver.currentText() or "").strip()
        if not srv or not ver:
            QMessageBox.warning(self, "提示", "请先选择服务端类型和 MC 版本")
            return
        # ---- 免责拦截 1：首次下载前必须确认版权与镜像来源 ----
        if not self.cfg.get("agreed_to_server_disclaimer", False):
            ans = QMessageBox.question(
                self, "开服工具免责声明",
                "关于「开服工具」的版权与镜像来源声明：\n\n"
                "1. 本启动器通过第三方镜像（MSL API）下载 Minecraft 服务端文件，"
                "该镜像为社区提供，非 Mojang 官方渠道。\n"
                "2. 下载的服务端 jar 受 Mojang / 原作者版权保护，"
                "仅限个人学习、测试与非商业用途。\n"
                "3. 作者不对镜像文件的完整性、合法性或可用性做任何保证；"
                "因下载/运行服务端产生的任何损失由使用者自行承担。\n\n"
                "点击「同意并下载」代表你已知晓并同意上述条款。",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if ans != QMessageBox.StandardButton.Ok:
                self._log("[开服] 用户拒绝免责条款，取消下载")
                return
            self.cfg["agreed_to_server_disclaimer"] = True
            ok = core.save_config(self.cfg)
            self._log(f"[开服] 保存免责状态: {'成功' if ok else '失败'}"
                      f"（路径 {core.CONFIG_FILE}）")
            if not ok:
                # AppData 写不了时，把同意状态写进启动器目录的本地覆盖文件，
                # 保证下次启动即使主配置没写成也能读到，弹窗不再反复出现
                self._persist_local_override("agreed_to_server_disclaimer", True)
        # 后台下载
        self.btn_download_srv.setEnabled(False)
        self._log(f"[开服] 开始下载 {srv} {ver} ...")
        run_bg(self._srv_download_bg, srv, ver)

    # ---------------- MSL 请求结果信号槽（全部主线程渲染 UI）----------------
    @pyqtSlot(list)
    def on_srv_types_ready(self, server_list: list):
        """srvTypesReady 信号槽（主线程）：填充 combo_srv_type。

        后台线程 _srv_get_types 只做请求 + emit，UI 一律在这个主线程槽里渲染。
        """
        self._srv_types_loaded = list(server_list)
        self.combo_srv_type.blockSignals(True)
        self.combo_srv_type.clear()
        self.combo_srv_type.addItems([str(t) for t in server_list])
        self.combo_srv_type.blockSignals(False)
        # [FIX-版本下拉框为空] 主动选第一项并触发版本请求。
        # 由于 _build_server_tab 里已把 currentIndexChanged 连到 _srv_on_type_changed，
        # setCurrentIndex(0) 若从空态变为有内容会自发信号触发；但若信号已被
        # blockSignals 拦掉，这里再显式调一次保证版本请求一定会发。
        if self.combo_srv_type.count() > 0:
            self.combo_srv_type.setCurrentIndex(0)
            self._srv_on_type_changed()
        self._log(f"[开服] 已加载 {len(server_list)} 种服务端类型")

    @pyqtSlot(list, str)
    def on_srv_versions_ready(self, versions: list, desc: str):
        """srvVersionsReady 信号槽（主线程）：填充 combo_srv_ver + 描述。

        后台线程 _srv_get_versions 只做请求 + emit，UI 在这个主线程槽里渲染。
        失败时 versions 为空列表、desc 含错误原因，同样走此槽。
        """
        self.combo_srv_ver.blockSignals(True)
        self.combo_srv_ver.clear()
        self.combo_srv_ver.addItems(versions)
        self.combo_srv_ver.blockSignals(False)
        self.lbl_srv_desc.setText(desc or "无描述")
        if versions:
            self._srv_versions_loaded = True
        self.btn_download_srv.setEnabled(bool(versions))
        self._log(f"[开服] 版本列表已更新，共 {len(versions)} 个版本")

    @pyqtSlot(str, str)
    def on_srv_download_ready(self, url: str, dest_path: str):
        """srvDownloadReady 信号槽（主线程）：下载直链 + 目标路径。

        后台 _srv_download_bg 拉到直链并算出保存路径后 emit，
        主线程槽里再触发真正的文件下载（避免跨线程跑大文件传输）。
        """
        self.bridge.status.emit(f"开始下载 {Path(dest_path).name} ...")

        def _prog(cur, total):
            self.bridge.progress.emit(cur, total, Path(dest_path).name)

        try:
            self.bridge.progress.emit(0, 0, "正在连接...")
            try:
                core.Net.download(url, Path(dest_path),
                                   progress_cb=_prog, timeout=300)
            finally:
                self.bridge.progressReset.emit()
            self._log(f"[开服] 下载完成: {dest_path}")
            # EULA 询问
            srv_ver = Path(dest_path).name.rsplit(".jar", 1)[0]
            ans = QMessageBox.question(
                self, "EULA 协议",
                f"已下载 {Path(dest_path).name}。\n\n"
                "是否自动同意 EULA 协议并生成 eula.txt？\n"
                "（同意后将在 jar 同级目录写入 eula.txt：eula=true，"
                "以便首次开服时自动接受协议）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if ans == QMessageBox.StandardButton.Yes:
                try:
                    eula = Path(dest_path).parent / "eula.txt"
                    eula.write_text("eula=true\n", encoding="utf-8")
                    self._log(f"[开服] 已生成 {eula}")
                except Exception as exc:
                    self._log(f"[开服] 写 eula.txt 失败: {exc}")
            self.bridge.info.emit(
                "服务端下载完成",
                f"{srv_ver} 已下载到：\n{dest_path}\n\n"
                f"可点击下方「▶️ 一键启动服务端」开始开服。")
        except Exception as exc:
            self._log(f"[开服] 下载失败: {exc}")
            # 防刷：直接弹窗报错，不自动重试（下载接口 1小时30次 / 1天60次）
            self.bridge.error.emit(
                "服务端下载失败",
                f"{Path(dest_path).name} 下载出错：\n{exc}\n\n"
                f"受 MSL 防滥用限制（1小时30次 / 1天60次），"
                f"请勿频繁重试。请检查网络或稍后再试。")
        finally:
            self.btn_download_srv.setEnabled(True)

    def _srv_download_bg(self, srv: str, ver: str):
        """[后台线程] 拉取 MSL 下载直链 -> emit 信号回主线程执行下载。

        跨线程安全：本函数只在后台线程跑，只做 API 请求 + 计算保存路径，
        绝不碰 UI / QMessageBox。直链 + 路径通过 self.bridge.srvDownloadReady
        信号排队回主线程槽 on_srv_download_ready 里执行真正的文件传输。
        失败不自动重试（防刷）。
        """
        try:
            self.bridge.status.emit(f"解析 {srv} {ver} 下载直链...")
            # [MSL 官方规范] 下载直链：/download/server/{server}/{version}
            # （不带 ?build=latest），data 含 url + 可选 sha256
            data = self._msl_get(
                f"{self.MSL_API}/download/server/{srv}/{ver}")
            d = data.get("data")
            if not isinstance(d, dict):
                raise RuntimeError(
                    f"接口 data 非 dict（实际 {type(d).__name__}）: {str(d)[:200]}")
            url = (d.get("url") or "").strip()
            if not url:
                raise RuntimeError(f"接口未返回下载地址: {str(d)[:200]}")
            dest_dir = core.MINECRAFT_DIR / "server_downloads"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{srv}-{ver}.jar"
            # 跨线程：把「直链 + 保存路径」emit 回主线程，由主线程槽执行下载
            self.bridge.srvDownloadReady.emit(url, str(dest))
        except Exception as exc:
            self._log(f"[开服] 获取 {srv} {ver} 下载直链失败（不自动重试）: {exc}")
            self.bridge.error.emit(
                "服务端下载失败",
                f"获取 {srv} {ver} 下载直链出错：\n{exc}\n\n"
                f"受 MSL 防滥用限制，请勿频繁重试。请检查网络后稍后再试。")
            self.btn_download_srv.setEnabled(True)

    # ---------------- 服务端启动 / 停止 / 指令 / 控制台 ----------------
    def _start_server(self):
        """点「▶️ 一键启动服务端」：每次必弹免责声明 -> 选 jar -> 后台 Popen。"""
        # 已有一个服务端在跑就不重复起
        if self._srv_proc is not None and self._srv_proc.poll() is None:
            QMessageBox.information(self, "提示",
                                    "已有一个服务端在运行，请先停止再启动新的。")
            return
        # 找最新下载的服务端 jar（server_downloads 下最近修改的）
        srv_dir = core.MINECRAFT_DIR / "server_downloads"
        jars = [p for p in srv_dir.glob("*.jar")] if srv_dir.is_dir() else []
        if not jars:
            QMessageBox.warning(self, "无服务端",
                                "尚未下载任何服务端 jar。\n"
                                "请先点击「⬇ 下载服务端」。")
            return
        jar = max(jars, key=lambda p: p.stat().st_mtime)
        java = self._selected_java()
        if not java:
            QMessageBox.warning(self, "无 Java",
                                "未找到 Java，请先在【设置】里检测/下载 Java。")
            return
        # ---- 免责拦截 2：每次启动都必须确认法律风险 ----
        ans = QMessageBox.warning(
            self, "重要法律风险提示与免责声明",
            "启动 Minecraft 服务端前请确认：\n\n"
            "• 严禁商业盈利\n"
            "• 严禁诱导未成年人充值\n"
            "• 遵守 Mojang EULA 协议\n"
            "• 违规后果自负\n\n"
            "点击「我已阅读并承诺不违规」继续启动；"
            "点击「取消」将中止。",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok)
        if ans != QMessageBox.StandardButton.Ok:
            self._log("[开服] 用户取消启动（免责拦截）")
            return
        self._srv_jar_path = str(jar)
        # 内存参数：直接 Java Popen 启动（不走 .bat），内存跟随启动器「运行设置」
        mem = int(self.mem_spin.value())            # 最大堆 MB（-Xmx）
        min_mem = max(512, min(2048, mem // 2))     # 最小堆 MB（-Xms）
        # 启动按钮锁定到服务端真正退出（on_srv_exited 恢复）
        self.btn_start_server.setEnabled(False)
        self.btn_stop_server.setEnabled(True)
        run_bg(self._start_server_bg, jar, java, mem, min_mem)

    def _start_server_bg(self, jar: Path, java: str,
                         mem: int = 4096, min_mem: int = 2048):
        """[后台] 直接用 subprocess.Popen 调 Java 启动服务端（绝不走 .bat）。

        这样能：① 捕获 stdout/stderr 进内嵌控制台；② 通过 stdin 发指令；
        ③ 精准结束进程。Windows 下 Java 控制台默认 GBK，用 text+encoding
        读取，errors='replace' 防脏字节崩溃。
        """
        try:
            cmd = [java, f"-Xmx{mem}M", f"-Xms{min_mem}M",
                   "-jar", str(jar), "nogui"]
            self.bridge.srvConsole.emit("$ " + " ".join(cmd) + "\n")
            self._log(f"[开服] 启动命令: {' '.join(cmd)}")
            # 关键：cwd 设为 jar 所在父目录，否则 world/ 等会生成在启动器根目录
            proc = subprocess.Popen(
                cmd, cwd=str(jar.parent),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="gbk",
                errors="replace",
            )
            # 主线程接管句柄
            self._srv_proc = proc
            self._srv_jar_path = str(jar)

            # ---- 独立读线程：逐行读 stdout（text 模式）-> bridge.srvConsole ----
            def _reader():
                try:
                    for line in iter(proc.stdout.readline, ""):
                        self.bridge.srvConsole.emit(line)
                except Exception:
                    pass
                finally:
                    ret = proc.wait()
                    if self._srv_proc is proc:
                        self._srv_proc = None
                    self.bridge.srvExited.emit(ret)
            self._srv_stdout_thread = run_bg(_reader)
            # 顺带为小白生成 start_server.bat（仅供参考，Python 不走它）
            self._generate_start_bat(jar, java, mem, min_mem)
            self.bridge.status.emit(f"服务端已启动: {jar.name}")
            # 提示用户 eula.txt 是否就绪（首次开服必须同意，否则 Java 起来会直接退）
            eula = jar.parent / "eula.txt"
            if not eula.is_file():
                self.bridge.srvConsole.emit(
                    "\n[开服工具] 未找到 eula.txt。若服务端立即退出（rc!=0），"
                    "请点「⬇ 下载服务端」重新下载并勾选「生成 eula.txt」，"
                    "或手动在 jar 同级目录创建 eula.txt 并写入 eula=true。\n")
        except Exception as exc:
            self._log(f"[开服] 启动失败: {exc}")
            self.bridge.error.emit("服务端启动失败",
                                   f"Popen 出错：{exc}\n\n"
                                   f"请确认 Java 可用且 jar 完整。")
            self.bridge.srvConsole.emit(f"启动失败: {exc}\n")

    def _generate_start_bat(self, jar: Path, java: str,
                            mem: int, min_mem: int):
        """生成 start_server.bat（双击即用，带 pause 防闪退）。仅供参考，
        启动器自身用 Popen 直连 Java，不依赖此 .bat。"""
        try:
            dest = jar.parent / "start_server.bat"
            # 绝对路径包引号，防路径带空格
            bat = (f'@echo off\r\n'
                   f'chcp 65001 >nul\r\n'
                   f'title {jar.name} - MC Server\r\n'
                   f'"{java}" -Xmx{mem}M -Xms{min_mem}M -jar "{jar.name}" nogui\r\n'
                   f'pause\r\n')
            dest.write_text(bat, encoding="utf-8")
            self._log(f"[开服] 已生成 {dest.name}（双击可用，需同目录有 eula.txt）")
        except Exception as exc:
            self._log(f"[开服] 生成 start_server.bat 失败: {exc}")

    def _stop_server(self, is_exit: bool = False):
        """停止服务端（干净利落，不留孤儿进程）。

        正常停止（点「⏹ 停止」）：
          1. 往 stdin 写 "stop\\n" 并 flush，让 Java 优雅关闭；
          2. 等 3s；若还没退出，用 taskkill /F /T 强杀整个进程树。
        退出停止（closeEvent，is_exit=True）：直接 taskkill /F /T，快速清理。
        """
        proc = self._srv_proc
        if proc is None:
            if not is_exit:
                self._log("[开服] 没有运行中的服务端可停止")
            self.btn_start_server.setEnabled(True)
            self.btn_stop_server.setEnabled(False)
            return
        if proc.poll() is not None:
            # 已自行退出
            if self._srv_proc is proc:
                self._srv_proc = None
            self.btn_start_server.setEnabled(True)
            self.btn_stop_server.setEnabled(False)
            if not is_exit:
                self._log("[开服] 服务端已退出")
            return

        def _taskkill_tree(pid: int):
            """强杀整个进程树（防残留子进程）。"""
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception as exc:
                self._log(f"[开服] taskkill 失败: {exc}，回退 proc.kill()")
                try:
                    proc.kill()
                except Exception:
                    pass

        def _reset_btns():
            if self._srv_proc is proc:
                self._srv_proc = None
            self.btn_start_server.setEnabled(True)
            self.btn_stop_server.setEnabled(False)

        if is_exit:
            # 关闭程序：直接强杀，不阻塞、不发 stop
            _taskkill_tree(proc.pid)
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            _reset_btns()
            self._log("[开服] 关闭程序，已强杀服务端进程树")
            return

        # 正常停止：先优雅 stop，等 3s，没退就 taskkill /F /T
        try:
            with self._srv_input_lock:
                proc.stdin.write("stop\n")
                proc.stdin.flush()
        except Exception:
            pass
        self._log("[开服] 已发送 stop，等待服务端优雅关闭（3s）...")
        # 后台等待 3s，不冻结 UI；到期若仍未退出再 taskkill /F /T 强杀
        def _watch_stop():
            time.sleep(3)
            if proc.poll() is None:
                self._log("[开服] 服务端未在 3s 内退出，taskkill /F /T 强杀进程树")
                _taskkill_tree(proc.pid)
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
            _reset_btns()
            self._log("[开服] 服务端进程已清理")
        run_bg(_watch_stop)

    def _srv_send_cmd(self):
        """指令输入框回车：把文本写入 proc.stdin 并 flush。"""
        proc = self._srv_proc
        if proc is None or proc.poll() is not None:
            self._log("[开服] 无运行中的服务端，指令未发送")
            return
        text = self.edit_cmd_input.text()
        if not text:
            return
        try:
            with self._srv_input_lock:
                proc.stdin.write(text + "\n")
                proc.stdin.flush()
            self.server_console.appendPlainText(f"> {text}")
            self.edit_cmd_input.clear()
        except Exception as exc:
            self._log(f"[开服] 发送指令失败: {exc}")
            self.bridge.srvConsole.emit(f"指令发送失败: {exc}\n")

    def on_srv_console(self, text: str):
        """srvConsole 信号槽（主线程）：追加到 server_console。"""
        # text 模式下 readline 已做 GBK→str 解码，脏字节被 replace 吞掉；
        # 仅过滤纯回车行，其余原样追加。
        if not text:
            return
        line = text.rstrip("\r\n")
        if not line:
            return
        self.server_console.appendPlainText(line)
        sb = self.server_console.verticalScrollBar()
        sb.setValue(sb.maximum())

    def on_srv_exited(self, ret: int):
        """srvExited 信号槽（主线程）：服务端退出，复位按钮。"""
        self._log(f"[开服] 服务端已退出，返回码={ret}")
        self.btn_start_server.setEnabled(True)
        self.btn_stop_server.setEnabled(False)
        self.server_console.appendPlainText(
            f"\n[开服工具] 服务端已停止 (rc={ret})\n")
        # 诊断：非正常退出（常见原因 eula 未同意 / Java 缺失 / 内存不足）
        if ret not in (0, None):
            hints = []
            srv_dir = Path(self._srv_jar_path).parent if self._srv_jar_path else None
            if srv_dir and not (srv_dir / "eula.txt").is_file():
                hints.append("• eula.txt 缺失：服务端子进程启动即退。"
                             "请重新「⬇ 下载服务端」并勾选生成 eula.txt，"
                             "或手动在 jar 同级创建 eula.txt（内容：eula=true）。")
            if not self._selected_java():
                hints.append("• 未检测到 Java：请在【设置】里检测/下载 Java。")
            if hints:
                self.bridge.warn.emit(
                    "服务端异常退出",
                    f"服务端进程返回码 {ret}，可能原因：\n" + "\n".join(hints))
            else:
                self.bridge.warn.emit(
                    "服务端异常退出",
                    f"服务端返回码 {ret}，请在上方控制台查看最后几行日志定位原因。")

    # ==================================================================
    # 外置登录（Yggdrasil / Authlib-Injector）
    # ==================================================================
    def _auth_login(self):
        """点「登录（Yggdrasil）」：按标准 Yggdrasil 协议换 Token。

        成功后：
          1. 从 availableProfiles 里提取全部角色（name + id）
          2. 灌进顶部「玩家名」下拉框，玩家可以选用哪个角色启动
          3. self._auth_session 存 accessToken / server / 全部角色
          4. 启动时按选中角色的真实 name + 真实 UUID 填入启动参数
        """
        srv_raw = self.auth_server_combo.currentText().strip()
        user = self.auth_username_edit.text().strip()
        pwd = self.auth_password_edit.text()
        client_token = self.auth_token_edit.text().strip()
        srv = parse_authlib_uri(srv_raw) or srv_raw
        if not srv or not user:
            QMessageBox.warning(self, "外置登录", "请填写认证服务器地址和用户名")
            return
        self._save_settings()
        if not self._begin_busy():
            return
        self.bridge.status.emit("Yggdrasil 登录中...")

        def work():
            """后台线程：只做网络请求，绝不动任何 Qt 控件。

            结果统一通过 bridge.authReady 信号送回主线程，由 on_auth_ready 刷新 UI。
            """
            try:
                self.bridge.log.emit(f"[Yggdrasil] 服务器={srv} 用户名={user}")
                sess = yggdrasil_authenticate(srv, user, pwd, client_token)
                profiles = sess.get("profiles") or []
                session = {
                    "server": sess["server"],
                    "authserver": sess["server"],
                    "accessToken": sess["accessToken"],
                    "access_token": sess["accessToken"],
                    "clientToken": sess.get("clientToken", ""),
                    "uuid": sess["uuid"],
                    "uuid_dashed": sess.get("uuid_dashed", ""),
                    "username": sess["username"] or user,
                    "password": pwd,
                    "profiles": profiles,             # ← 全部角色
                    "selected": sess.get("selected", ""),
                    "mode": "token",
                }
                self.bridge.log.emit(
                    f"[Yggdrasil] 登录成功: {session['username']} "
                    f"uuid={session['uuid']}"
                    + (f" 角色数={len(profiles)}: "
                       + ", ".join(p.get("name", "") for p in profiles[:6])
                       if profiles else " 角色数=0"))
                # ★ 交给主线程：填下拉框 + 刷状态栏
                self.bridge.authReady.emit({
                    "ok": True, "session": session,
                    "profiles": profiles, "error": "",
                })
            except Exception as exc:
                self.bridge.log.emit(f"Yggdrasil 登录失败: {exc}")
                self.bridge.authReady.emit({
                    "ok": False, "session": None,
                    "profiles": [], "error": str(exc),
                })
            finally:
                self.busy = False
                for b in self._action_buttons:
                    try:
                        b.setEnabled(True)
                    except Exception:
                        pass

        run_bg(work)

    @pyqtSlot(object)
    def on_auth_ready(self, payload):
        """【主线程】处理 Yggdrasil 登录结果：填角色下拉框 + 刷状态。

        这一步必须在主线程做：QComboBox.addItem 在非 GUI 线程调用不会报错，
        但界面完全不刷新 —— 之前「登录成功却没有角色」就是踩了这个坑。
        """
        payload = payload or {}
        if not payload.get("ok"):
            self._auth_session = None
            self._auth_profiles = []
            self._refresh_name_combo_editability()
            self._update_auth_status_label()
            err = payload.get("error") or "未知错误"
            self.bridge.error.emit("Yggdrasil 登录失败", err)
            return

        session = payload.get("session") or {}
        profiles = payload.get("profiles") or []
        self._auth_session = session
        # 登录成功 -> 取消离线勾选（否则启动时仍是离线语义）
        self.offline_check.blockSignals(True)
        self.offline_check.setChecked(False)
        self.offline_check.blockSignals(False)
        # ★ 把 availableProfiles 灌进顶部「玩家名」下拉框（主线程）
        self._fill_name_combo_from_profiles(
            profiles, session.get("selected", ""))
        self._restored_auth = False
        self._update_auth_status_label()
        self._save_settings()
        # ★ 登录态落盘（不含密码），下次启动自动恢复
        self._save_auth_session()
        self.bridge.status.emit(f"登录成功: {session.get('username', '')}")
        self._log(f"已保存外置登录会话：{session.get('username')} "
                  f"({session.get('uuid')}) @ {session.get('server')}"
                  f"（{len(profiles)} 个角色）")
        if not profiles:
            self.bridge.warn.emit(
                "登录成功但未取到角色",
                "服务器没有返回 availableProfiles（该账号可能还没创建角色）。\n\n"
                "请先到皮肤站网页端创建/绑定一个游戏角色，然后重新登录。\n"
                "在此之前启动游戏会使用 selectedProfile 的信息。")

    def _auth_offline(self):
        """离线模式：清掉登录凭证，恢复玩家名手输，启动时不注入 authlib-injector。"""
        self._auth_session = None
        self._auth_profiles = []
        self._restored_auth = False
        self.name_combo.blockSignals(True)
        cur = (self.name_combo.currentText() or "Steve").strip() or "Steve"
        self.name_combo.clear()
        self.name_combo.setEditText(cur)
        self.name_combo.blockSignals(False)
        self._selected_uuid = ""
        self._refresh_name_combo_editability()
        self.offline_check.setChecked(True)
        self._update_auth_status_label()
        self._save_settings()
        # 清掉本地保存的登录态，否则重启又会「自动恢复」成已登录
        self._save_auth_session(None)
        self._log("已切换到离线模式（下次启动游戏不再注入 authlib-injector）")

    def _clear_auth_session(self, reason: str = ""):
        """Token 过期 / 登录失效：清掉会话与本地记录，把 UI 切回「未登录」可重输状态。"""
        self._auth_session = None
        self._auth_profiles = []
        self._restored_auth = False
        self._selected_uuid = ""
        self.name_combo.blockSignals(True)
        cur = (self.name_combo.currentText() or "Steve").strip() or "Steve"
        self.name_combo.clear()
        self.name_combo.setEditText(cur)
        self.name_combo.blockSignals(False)
        self._refresh_name_combo_editability()
        self.offline_check.blockSignals(True)
        self.offline_check.setChecked(True)
        self.offline_check.blockSignals(False)
        # 密码框留空，等玩家重新输入；服务器/用户名保留方便重登
        self.auth_password_edit.clear()
        self._update_auth_status_label()
        if reason:
            self.auth_status_label.setText(
                f"✖ 外置登录已过期，请重新输入密码登录\n{reason}")
            self.auth_status_label.setStyleSheet("color:#c0392b;")
        self._save_settings()
        self._save_auth_session(None)      # 抹掉本地 token，避免反复提示
        self._log(f"[外置登录] 登录态已清除：{reason or '手动清除'}")

    # ==================================================================
    # 外置登录「拖拽」支持（LittleSkin 等皮肤站的拖拽字符串 + 认证文件）
    # ==================================================================
    # 皮肤站页面上的「拖拽到启动器」按钮给出的文本就是：
    #   authlib-injector:yggdrasil-server:https%3A%2F%2Flittleskin.cn%2Fapi%2Fyggdrasil
    # 拖进窗口后解析出 URL，直接启用外置登录（不需要密码，由注入器接管验证）。
    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasUrls() or md.hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        md = event.mimeData()
        if md.hasUrls() or md.hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        md = event.mimeData()
        if not (md.hasUrls() or md.hasText()):
            event.ignore()
            return
        event.acceptProposedAction()

        # ---- 1) 纯文本拖入：优先按 authlib-injector URI 解析 ----
        if md.hasText():
            text = md.text().strip()
            uri = parse_authlib_uri(text)
            if uri:
                self._apply_authlib_uri(uri, raw=text)
                return

        # ---- 2) 文件拖入：jar / json ----
        for u in md.urls():
            p = u.toLocalFile()
            if not p:
                continue
            self._import_auth_file(p)

    def _apply_authlib_uri(self, api_root: str, raw: str = ""):
        """把解析出的 Yggdrasil API 根地址写进【皮肤站配置】，并进入外置登录待命状态。

        纯拖拽模式没有密码 / Token：
          - 配置里只保存 server（供下次启动复用）
          - self._auth_session 标记为「待注入」状态（token 为空）
          - 启动时依然强制挂 -javaagent:<jar>=<api_root>，由注入器接管验证
        """
        self.auth_server_combo.setEditText(api_root)
        self._save_settings()
        self._auth_session = {
            "server": api_root,
            "authserver": api_root,
            "accessToken": "",          # 纯拖拽模式没有 token，注入器接管
            "access_token": "",
            "clientToken": "",
            "uuid": "",
            "uuid_dashed": "",
            "username": self.auth_username_edit.text().strip(),
            "password": "",
            "mode": "drag",             # drag = 只挂注入器；password = 已换取真实 Token
        }
        self._update_auth_status_label()
        self.cfg["auth_server"] = api_root
        core.save_config(self.cfg)
        self._log(f"已识别外置登录配置（拖拽模式）: {api_root}")
        if raw and raw != api_root:
            self._log(f"  原始拖拽内容: {raw[:120]}")
        QMessageBox.information(
            self, "已识别外置登录配置",
            f"已从拖拽内容解析出 Yggdrasil API 地址：\n{api_root}\n\n"
            f"已填入【设置 → 皮肤站配置】。\n"
            f"下次启动游戏时会自动挂上：\n"
            f"  -javaagent:authlib-injector.jar={api_root}\n\n"
            f"提示：这种方式不需要密码，authlib-injector 会接管验证。\n"
            f"若想直接换取真实 Token，可再填用户名/密码并点「登录（Yggdrasil）」。")

    def _import_auth_file(self, path: str):
        """尝试把拖入的文件识别为外置登录认证配置。

        支持三种常见格式（都是皮肤站 / authlib-injector 生态）：
          1. 认证服务器说明 json（如 pvp.wiki.mcmod.cn 发的 auth.json）：
             顶层或嵌套含 url/server + token，可能附带 username。
          2. 账号 json（authlib-injector 的 accounts 格式）：
             数组或对象，每项含 username / token / uuid / authServer。
          3. .jar（authlib-injector 本体）：直接拷进缓存目录，跳过解析。
        识别成功：填进设置页【皮肤站配置】并提示「已识别外置登录配置」。
        """
        p = Path(path)
        if not p.is_file():
            self._log(f"拖入文件不存在，忽略: {path}")
            return

        # ---- 情况 3：authlib-injector.jar 本体，直接进可写目录 ----
        if p.suffix.lower() == ".jar":
            target = authlib_injector_dest()
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(str(p), str(target))
                self._log(f"已识别 authlib-injector.jar：{p.name} -> {target}")
                QMessageBox.information(
                    self, "已识别外置登录配置",
                    f"已把 {p.name} 放入：\n{target}")
            except Exception as exc:
                QMessageBox.warning(self, "导入失败", f"复制 {p.name} 失败：\n{exc}")
            return

        # ---- 情况 1/2：json 认证配置 ----
        if p.suffix.lower() != ".json":
            self._log(f"拖入文件不是 .json/.jar，忽略: {path}")
            return

        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            QMessageBox.warning(self, "导入失败", f"无法解析 {p.name}：\n{exc}")
            return

        # 提取：把 data 规范化成列表（账号 json 可能是数组）
        items = []
        def _flatten(obj):
            """递归收集 dict/列表 里的所有 dict（含顶层）。"""
            if isinstance(obj, dict):
                items.append(obj)
                for v in obj.values():
                    _flatten(v)
            elif isinstance(obj, list):
                for v in obj:
                    _flatten(v)
        _flatten(data)

        def dig(obj, *keys):
            """在 dict 里按候选 key 找值（兼容 url/server/authServer 等命名）。"""
            if not isinstance(obj, dict):
                return ""
            for k in keys:
                v = obj.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return ""

        # 打分挑最像「一个完整账号」的那一项：
        #   有认证服务器 +10（最重要，没有它无法登录）
        #   有 access token +5、有用户名 +3
        # 这样 authenticationDatabase 里嵌套的 acc1 会胜过只有 clientToken 的顶层。
        best = None
        best_score = 0
        for it in items:
            server = dig(it, "url", "server", "authServer", "auth_server",
                         "yggdrasil_server", "domain")
            token = dig(it, "token", "access_token", "accessToken", "key", "secret")
            username = dig(it, "username", "name", "playerName", "account")
            score = (10 if server else 0) + (5 if token else 0) + (3 if username else 0)
            if score > best_score:
                best_score = score
                best = (server, token, username)

        if best is None or best_score < 5:
            # 兜底：整份文件里找 clientToken 之类的顶层令牌（可能是启动器自身配置）
            client_token = ""
            for it in items:
                client_token = dig(it, "clientToken") or client_token
            if client_token:
                QMessageBox.information(
                    self, "未识别",
                    f"{p.name} 里只找到了 clientToken（启动器标识），"
                    f"没有认证服务器地址与 access token，\n"
                    f"无法识别为外置登录配置。")
            else:
                QMessageBox.information(
                    self, "未识别",
                    f"{p.name} 里没有找到 url/server/token 字段，"
                    f"无法识别为外置登录配置。")
            return

        server, token, username = best
        # 规范化：server 可能也是 authlib-injector URI 或纯域名
        server = parse_authlib_uri(server) or server
        # 填进【皮肤站配置】
        if server:
            self.auth_server_combo.setEditText(server)
        if username:
            self.auth_username_edit.setText(username)
        if token:
            self.auth_token_edit.setText(token)
        self._save_settings()
        # 标记为外置登录待命（有 token 就是能直接用的会话，没有就靠注入器接管）
        self._auth_session = {
            "server": server,
            "authserver": server,
            "accessToken": token,
            "access_token": token,
            "clientToken": "",
            "uuid": "",
            "uuid_dashed": "",
            "username": username,
            "password": "",
            "mode": "token" if token else "drag",
        }
        self._update_auth_status_label()
        shown_token = (token[:12] + "...") if len(token) > 12 else token
        self._log(f"已识别外置登录配置: {p.name}（服务器={server or '未指定'}，"
                  f"用户={username or '未指定'}，token={shown_token or '无'}）")
        QMessageBox.information(
            self, "已识别外置登录配置",
            f"已从 {p.name} 读取认证配置并填入【设置 → 皮肤站配置】：\n"
            f"· 认证服务器：{server or '（未指定）'}\n"
            f"· 用户名：{username or '（未指定）'}\n"
            f"· 令牌：{token[:24] + '...' if len(token) > 24 else (token or '（无）')}\n\n"
            f"启动游戏时会自动挂上 -javaagent:authlib-injector.jar={server or ''}。")

    # ==================================================================
    # 通用 UI 槽（全部在主线程执行）
    # ==================================================================
    @pyqtSlot(str)
    def on_log(self, msg: str):
        self._log(msg)

    def _log(self, msg: str):
        try:
            print(msg, flush=True)
        except Exception:
            pass
        LOG_BUFFER.append(str(msg))
        dlg = self._global_log_dialog
        if dlg is not None:
            dlg.append_text(str(msg))

    @pyqtSlot(str)
    def on_status(self, msg: str):
        self.status_label.setText(msg)

    @pyqtSlot(int, int, str)
    def on_progress(self, cur: int, total: int, note: str):
        try:
            if total and total > 0:
                if self.progress_bar.maximum() == 0:
                    self.progress_bar.setRange(0, 100)
                self.progress_bar.setRange(0, total)
                self.progress_bar.setValue(min(cur, total))
                pct = int(cur * 100 / total)
                self.status_label.setText(f"[{pct}%] {note}" if note else f"[{pct}%]")
            else:
                if self.progress_bar.maximum() != 0:
                    self.progress_bar.setRange(0, 0)
                self.status_label.setText(note or "处理中...")
        except Exception:
            pass

    @pyqtSlot()
    def on_progress_reset(self):
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

    @pyqtSlot(bool)
    def on_busy(self, busy: bool):
        """忙碌状态：只控「非启动」类按钮（刷新/搜索/下载/翻页…），
        绝不碰 btn_launch（启动按钮由 launchBusy 信号单独控制）。

        关键修复：游戏启动产生的 busy 绝不再全局禁用下载按钮——
        启动走的是 bridge.launchBusy，与此处 self.busy 完全分离。
        """
        self.busy = busy
        self.btn_show_log.setEnabled(True)      # 「查看日志」任何时候都能点
        for b in self._action_buttons:
            try:
                if b is getattr(self, "btn_launch", None):
                    continue  # 启动按钮不归 busy 管
                b.setEnabled(not busy)
            except Exception:
                pass
        if busy:
            self.status_label.setText(self.status_label.text() or "任务进行中...")

    def _begin_busy(self) -> bool:
        """同步置忙（不经信号队列），立刻挡住同一次事件循环里的二次点击。

        只禁用「非启动」类按钮；启动按钮（btn_launch）由 launchBusy 单独控制，
        绝不被下载/校验忙碌误伤。
        返回 True = 成功进入忙态；False = 已在忙（调用方应直接 return）。
        """
        if self.busy:
            return False
        self.busy = True
        for b in self._action_buttons:
            try:
                if b is getattr(self, "btn_launch", None):
                    continue
                b.setEnabled(False)
            except Exception:
                pass
        return True

    # ---------------- 下载专用锁（与游戏运行状态解耦） ----------------
    def _begin_download(self) -> bool:
        """登记一个下载任务开始（允许 Mod/光影/整合包多任务并行，只计数不清空）。

        绝不检查游戏是否在运行——玩家一边玩游戏一边下载新版本，是合法且鼓励的操作。
        仅用于防止「同一入口被连点」造成重复弹窗，不阻塞其它下载任务。
        返回 True 表示成功登记（调用方继续）；已并发下载时仍返回 True（放行并行）。
        """
        self._downloading_count += 1
        self.is_downloading = self._downloading_count > 0
        return True

    def _end_download(self):
        """登记一个下载任务结束。最后一个任务结束时才真正解除 is_downloading。"""
        if self._downloading_count > 0:
            self._downloading_count -= 1
        if self._downloading_count <= 0:
            self._downloading_count = 0
            self.is_downloading = False

    # ---------------- Java ----------------
    def refresh_all(self):
        run_bg(self._refresh_java_bg)
        run_bg(self._refresh_versions_bg)

    def refresh_java(self):
        run_bg(self._refresh_java_bg)

    def _refresh_java_bg(self):
        self.bridge.status.emit("检测 Java 中...")
        try:
            det = core.detect_java()
        except Exception as exc:
            self.bridge.log.emit(f"Java 检测失败: {exc}")
            det = []
        self.bridge.javaReady.emit(list(det))

    @pyqtSlot(list)
    def on_java_ready(self, det: list):
        self.detected_java = det
        prev_path = self._current_java_path()

        self.java_combo.blockSignals(True)
        self.java_combo.clear()
        for i, (ver, path) in enumerate(det):
            self.java_combo.addItem(self._java_display_name(ver, path, i))
            # 完整路径只放在 tooltip / UserRole 里，不参与显示与宽度计算
            full = f"{path}"
            self.java_combo.setItemData(i, full, Qt.ItemDataRole.ToolTipRole)
            self.java_combo.setItemData(i, full, Qt.ItemDataRole.UserRole)

        if det:
            self.java_status.setText(f"检测到 {len(det)} 个 Java")
            self.java_status.setStyleSheet("color:#2a7a2a; background-color: rgba(60,140,60,200); "
                                           "border-radius: 4px; padding: 2px 6px;")
            paths = [p for _, p in det]
            idx = paths.index(prev_path) if prev_path in paths else 0
            self.java_combo.setCurrentIndex(idx)
            self.java_combo.setToolTip(f"Java {det[idx][0]}\n{det[idx][1]}")
        else:
            self.java_status.setText("未检测到 Java，请下载或安装")
            self.java_status.setStyleSheet("color:#e74c3c; background-color: rgba(200,80,80,200); "
                                           "border-radius: 4px; padding: 2px 6px;")
            self.java_combo.setToolTip("未检测到 Java")
        self.java_combo.blockSignals(False)

    def _current_java_path(self):
        """当前选中 Java 的完整路径（从 UserRole 取，不解析显示文字）。"""
        idx = self.java_combo.currentIndex()
        if idx < 0:
            return None
        path = self.java_combo.itemData(idx, Qt.ItemDataRole.UserRole)
        if path:
            return str(path)
        if idx < len(self.detected_java):
            return self.detected_java[idx][1]
        return None

    def _on_java_combo_changed(self, idx: int):
        """切换 Java 时更新 tooltip，让整条路径在悬停时才出现。"""
        if idx < 0 or idx >= len(self.detected_java):
            return
        ver, path = self.detected_java[idx]
        self.java_combo.setToolTip(f"Java {ver}\n{path}")

    def _selected_java(self):
        path = self._current_java_path()
        if path:
            return path
        if self.detected_java:
            return self.detected_java[0][1]
        return None

    def download_java(self):
        if self.is_downloading:
            return
        vid = self._selected_version()
        recommended = core.mc_to_java_major(vid) if vid else 17
        choice = JavaPickDialog(self, recommended).exec_and_get()
        if choice is None:
            return
        self._begin_download()
        self._download_java_bg(choice)

    def _download_java_bg(self, java_major: int):
        def work():
            self._begin_busy()
            self.bridge.status.emit("解析 Java 下载地址...")
            # [FIX-Java下载超时] get_java_download_url 现返回 (urls_list, fname)
            # urls_list 为候选直链（清华 + 华为云双镜像），依次尝试自动切换
            # 获取候选直链（清华 + 华为云双镜像兜底）
            urls, fname = core.get_java_download_url(java_major)
            if isinstance(urls, str):
                urls = [urls]
            if not urls:
                # 无候选直链：同样走「询问式」弹窗（官网手动下载）
                self._log(f"[Java 下载] 无可用候选直链，询问是否跳官网手动下载")
                official_url = ("https://adoptium.net/zh-CN/temurin/releases/"
                                f"?os=windows&arch=x64&package=jdk")
                self.bridge.javaDownloadFailed.emit(
                    fname or "Java 安装包", "无候选直链", 0, official_url)
                self.bridge.progressReset.emit()
                self._end_download()
                self.bridge.busy.emit(False)
                return

            dest = core.JAVA_DIR / fname
            self.bridge.log.emit(f"下载 {fname}")

            def cb(cur, tot):
                self.bridge.progress.emit(cur, tot, f"下载 Java {java_major}...")

            # 依次尝试每个候选直链（清华 -> 华为云 -> 官方），任一成功即止
            last_err = None
            for i, url in enumerate(urls, 1):
                try:
                    self.bridge.log.emit(f"下载 {fname}（候选 {i}/{len(urls)}）...")
                    core.Net.download(url, dest, cb, timeout=30)
                    self.bridge.log.emit(f"下载完成: {dest.name}")
                    break
                except Exception as exc:
                    last_err = exc
                    # 清理残留 .part，避免下次候选直链写入冲突
                    try:
                        part = dest.with_suffix(dest.suffix + ".part")
                        if part.is_file():
                            part.unlink()
                    except OSError:
                        pass
                    self.bridge.log.emit(f"候选 {i} 下载失败，尝试下一个: {exc}")
                    continue
            else:
                # 所有候选都失败：发信号回主线程，弹「询问式」对话框
                # （不再冷冰冰弹 error 框；由主线程槽弹「去官网手动下载 / 取消」）
                err_summary = (f"{last_err!r}" if last_err else "无可用直链")
                official_url = ("https://adoptium.net/zh-CN/temurin/releases/"
                                f"?os=windows&arch=x64&package=jdk")
                self._log(f"[Java 下载] {len(urls)} 个候选均失败，询问是否跳官网手动下载")
                self.bridge.javaDownloadFailed.emit(
                    fname, err_summary, len(urls), official_url)
                self.bridge.progressReset.emit()
                self._end_download()
                self.bridge.busy.emit(False)
                return

            self.bridge.status.emit("准备安装 Java...")
            # 安装（zip 解压 / msi 静默）
            lower = str(dest).lower()
            if lower.endswith(".zip"):
                try:
                    import zipfile
                    self.bridge.log.emit("解压 Java...")
                    extract_dir = core.JAVA_DIR / (dest.stem + "-extracted")
                    extract_dir.mkdir(parents=True, exist_ok=True)
                    with zipfile.ZipFile(dest) as z:
                        z.extractall(extract_dir)
                    self.bridge.log.emit(f"解压完成: {extract_dir}")
                    self.bridge.status.emit("解压完成，正在刷新检测...")
                except Exception as exc:
                    self.bridge.info.emit("解压失败",
                                          f"Java 解压失败：{exc}\n\n"
                                          f"请确认 {dest.name} 是否完整，"
                                          f"或手动解压后点「重新检测」。")
            elif lower.endswith(".msi"):
                try:
                    subprocess.Popen(["msiexec", "/i", str(dest), "/qn"],
                                     cwd=str(core.JAVA_DIR))
                    self.bridge.log.emit("已启动静默安装，安装后点击「重新检测」")
                except Exception as exc:
                    self.bridge.info.emit("安装失败",
                                          f"Java 静默安装启动失败：{exc}\n\n"
                                          f"请手动运行 {dest.name} 安装后"
                                          f"点「重新检测」。")
            else:
                self.bridge.info.emit("下载完成",
                                      f"安装包已保存到：\n{dest}\n"
                                      f"请手动安装后点击「重新检测」")

            self.bridge.progressReset.emit()
            self._end_download()
            self.bridge.busy.emit(False)
            time.sleep(1.5)
            self.bridge.status.emit("就绪")
            run_bg(self._refresh_java_bg)

        run_bg(work)

    def on_java_download_failed(self, fname: str, err_summary: str,
                                candidate_count: int, official_url: str):
        """javaDownloadFailed 信号槽（主线程）：弹「询问式」对话框。

        所有候选直链下载失败（超时 / WinError 10060 / 网络错误）后，
        由后台线程发信号到这里，在主线程弹「⚠️ 自动下载失败」询问框：
          - 「去官网手动下载」→ open_url_safe 跳 Adoptium 官方下载页，
            并在日志 + 状态栏提示「请在网页下载后，回到启动器点击【重新检测】」
          - 「取消」→ 仅在日志记录「用户取消下载 Java」
        """
        msg = (f"无法连接到 Java 下载服务器（可能是网络超时或被拦截）。\n\n"
               f"已尝试 {candidate_count} 个候选直链均失败：{err_summary}\n\n"
               f"是否为您自动打开 Java 官方下载网页，进行手动下载？")
        # 用独立 QMessageBox 便于自定义按钮文字（「去官网手动下载」/「取消」）
        box = QMessageBox(self)
        box.setWindowTitle("⚠️ 自动下载失败")
        box.setText(msg)
        box.setIcon(QMessageBox.Icon.Warning)
        btn_go = box.addButton("去官网手动下载", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is btn_go:
            try:
                open_url_safe(official_url)
                self._log(f"[Java 下载] 已打开官网下载页：{official_url}")
                self.status_label.setText(
                    "请在网页下载后，回到启动器点击【重新检测】")
                self.bridge.status.emit(
                    "请在网页下载后，回到启动器点击【重新检测】")
            except Exception as exc:
                self._log(f"[Java 下载] 打开官网失败：{exc}")
                self.bridge.info.emit(
                    "手动下载 Java",
                    f"无法自动打开浏览器，请手动访问以下链接下载 Java：\n\n"
                    f"{official_url}\n\n"
                    f"下载后放入 Java 目录（{core.JAVA_DIR}），再点「重新检测」。")
        else:
            self._log("[Java 下载] 用户取消下载 Java")

    # ---------------- 版本列表 ----------------
    def _refresh_versions_bg(self):
        self.bridge.status.emit("扫描本地版本...")

        def work():
            try:
                inst = core.installed_versions()
            except Exception as exc:
                self.bridge.log.emit(f"扫描版本失败: {exc!r}")
                inst = []
            self.bridge.log.emit(f"扫描到 {len(inst)} 个版本")
            self.bridge.versionsReady.emit(list(inst))

        run_bg(work)

    @pyqtSlot(list)
    def on_versions_ready(self, versions: list):
        self.installed = list(versions)
        self.ver_table.setRowCount(0)
        for v in versions:
            r = self.ver_table.rowCount()
            self.ver_table.insertRow(r)
            self.ver_table.setItem(r, 0, self._cell(v, user_data=v))
            self.ver_table.setItem(r, 1, self._cell("本地"))
            self.ver_table.setItem(r, 2, self._cell("已下载"))
        if versions:
            self.ver_table.selectRow(0)
        self.ver_table.resizeColumnToContents(0)
        self._refresh_res_version_list(versions)
        # 刷新三个搜索子页签的「目标 MC 版本」下拉框（官方全量清单）
        self._refresh_all_mc_ver_combos()
        self.status_label.setText(f"就绪（共 {len(versions)} 个版本）")
        if not self._versions_loaded_once:
            self._versions_loaded_once = True
            if not self.online_versions:
                self._refresh_dl_list()

    def _on_ver_selected(self):
        vid = self._selected_version()
        if vid:
            self._log(f"选中的版本: {vid}")
            self._auto_match_java(vid)

    def _selected_row(self, table: QTableWidget) -> int:
        """返回表格真正被「选中」的行号，没有选中返回 -1。

        刻意不用 currentRow()：当前项（焦点）会因为键盘/焦点变化而漂移，
        Tkinter Treeview 串台的根源就是拿焦点当选中。这里只认 selectionModel
        里的高亮行，选中多行时取最后一个（即用户最后点的那一行）。
        """
        sm = table.selectionModel()
        if sm is not None:
            rows = [i.row() for i in sm.selectedRows()]
            if rows:
                return rows[-1]
        return -1

    def _selected_version(self):
        """版本列表当前选中的版本 id（没有返回 None）。"""
        row = self._selected_row(self.ver_table)
        if row < 0 or row >= self.ver_table.rowCount():
            return None
        item = self.ver_table.item(row, 0)
        if item is not None:
            vid = item.data(Qt.ItemDataRole.UserRole)
            if vid:
                return str(vid)
        if row < len(self.installed):
            return self.installed[row]
        return None

    def _selected_dl_version(self):
        """下载列表当前选中项 -> (id, type, url)。

        唯一数据源是 _dl_item_map（行号 -> 官方纯原版号），
        绝不从版本列表（self._selected_version()）拿本地实例名。
        """
        row = self._selected_row(self.dl_table)
        if row < 0:
            return None
        item = self._dl_item_map.get(row)
        if item:
            return tuple(item)
        # 兜底：极少数情况下 map 未建（如用户手快），从单元格 UserRole 取行号
        cell = self.dl_table.item(row, 0)
        r = cell.data(Qt.ItemDataRole.UserRole) if cell else None
        if r is not None:
            return tuple(self._dl_item_map.get(int(r), ()))
        return None

    def _auto_match_java(self, version_id: str, required=None):
        if not self.detected_java:
            return None
        if required is None:
            required = core._required_java_major(version_id)

        def to_major(v):
            try:
                if isinstance(v, int):
                    return v
                s = str(v)
                head = s.split(".")[0]
                if head == "1":
                    return int(s.split(".")[1]) if "." in s else 8
                return int(head)
            except Exception:
                return 0

        required = to_major(required)
        best, best_major = None, 999
        for ver, path in self.detected_java:
            vmaj = to_major(ver)
            if vmaj >= required and vmaj < best_major:
                best, best_major = path, vmaj
        if best:
            for i, (ver, path) in enumerate(self.detected_java):
                if path == best:
                    self.java_combo.setCurrentIndex(i)
                    break
            self._log(f"版本 {version_id} 需要 Java {required}+，自动匹配 Java {best_major}")
            return best
        self._log(f"未找到 Java {required}+，使用列表中的最新版本")
        return None

    def _best_java_for(self, version_id: str):
        """给安装器挑一个满足该版本要求的 Java 可执行文件（不改变下拉框选择）。"""
        required = core._required_java_major(version_id)
        fallback = None
        best, best_major = None, 999
        for ver, path in self.detected_java:
            major = self._java_major_of(path)
            if major >= best_major:
                continue
            if major >= required:
                best, best_major = path, major
            elif best is None and major > (self._java_major_of(fallback) if fallback else 0):
                fallback = path
        chosen = best or fallback or self._selected_java()
        self._log(f"[安装器] {version_id} 需要 Java {required}+，"
                  f"使用: {chosen or 'java'}")
        return chosen or "java"

    def _java_major_of(self, java_path) -> int:
        p = str(java_path)
        m = re.search(r"jdk[-_]?(\d+)", p, re.IGNORECASE)
        if m:
            v = int(m.group(1))
            return 8 if v == 9 else v
        m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?(?:_[\w]+)?", p)
        if m:
            v = int(m.group(1))
            minor = int(m.group(2))
            if v == 1:
                return 8 if minor >= 8 else minor
            return 8 if v <= 8 else v
        try:
            out = subprocess.run([p, "-version"], capture_output=True, text=True, timeout=5)
            txt = out.stderr + out.stdout
            m = re.search(r'version "(\d+)(?:\.(\d+))?([^"]*)"', txt)
            if m:
                a = int(m.group(1))
                b = m.group(2)
                if a == 1:
                    return 8 if (b and int(b) >= 8) else int(b or 8)
                return 8 if a <= 8 else a
        except Exception:
            pass
        return 0

    def _check_java_for_version(self, version_id, java_path):
        required = core._required_java_major(version_id)
        cur_major = self._java_major_of(java_path)
        self._log(f"[Java 检查] {version_id} 需要 Java>={required}，当前 {cur_major}")
        if cur_major >= required:
            return java_path
        matched = self._auto_match_java(version_id, required)
        if matched:
            return matched
        self._log(f"[Java 警告] {version_id} 需要 Java>={required}，仍继续启动")
        return java_path

    # ---------------- 下载列表 ----------------
    def _refresh_dl_list(self):
        self.btn_dl_refresh.setEnabled(False)
        self.bridge.status.emit("获取在线版本列表...")

        def work():
            try:
                online = core.mcl_version_list()
            except Exception as exc:
                self.bridge.log.emit(f"获取版本列表失败: {exc!r}")
                online = []
            self.bridge.onlineReady.emit(list(online))

        run_bg(work)

    @pyqtSlot(list)
    def on_online_ready(self, online: list):
        self.online_versions = list(online)
        self.btn_dl_refresh.setEnabled(True)
        self._render_online()

    def _render_online(self):
        if not self.online_versions:
            return
        want = "release" if self.rb_release.isChecked() else "snapshot"
        self.dl_table.setRowCount(0)
        self._dl_item_map.clear()
        for vid, vtype, url in self.online_versions:
            if vtype != want:
                continue
            r = self.dl_table.rowCount()
            self.dl_table.insertRow(r)
            # 行号 -> 纯原版号 (id, type, url)。这是下载的唯一数据源，
            # 绝不从版本列表里取实例名（如 1.20.1-Forge_47.4.18）。
            self._dl_item_map[r] = (vid, vtype, url)
            installed = vid in self.installed
            self.dl_table.setItem(r, 0, self._cell(vid, user_data=r))
            self.dl_table.setItem(r, 1, self._cell(vtype))
            self.dl_table.setItem(r, 2, self._cell("已安装" if installed else "未下载"))
        self.dl_table.resizeColumnToContents(0)

    # ---------------- 校验 / 补全 ----------------
    def _repair_selected(self):
        if self.is_downloading:
            return
        # 关键修复：本按钮位于【下载管理】页，优先用下载列表选中的「纯原版号」
        # （来自 _dl_item_map），绝不先拿版本列表里的本地实例名（如 1.20.1-Forge_47.4.18）
        # 去请求官方 API。只有下载列表没选中时，才回退到版本列表的实例名。
        dl = self._selected_dl_version()
        vid = (dl[0] if dl else None) or self._selected_version()
        if not vid:
            QMessageBox.warning(self, "提示", "请先在下载列表或版本列表里选择一个版本")
            return
        self._begin_download()
        self._save_settings()
        core.apply_paths(self.cfg)
        self._begin_busy()
        run_bg(self._repair_version_bg, vid)

    def _repair_version_bg(self, version_id: str):
        def log(m):
            self.bridge.log.emit(m)

        def cb(cur, tot, note=None):
            self.bridge.progress.emit(cur, tot,
                                      f"校验/补全 {version_id}" + (f" ({note})" if note else ""))

        # 用 _instance_base_version 解析真正拥有核心 jar 的版本：
        # 克隆实例（测试3）通过 json.inheritsFrom -> 1.20.1，补全时下 1.20.1 本体。
        base = core._instance_base_version(version_id) or version_id
        pure = core.mcl_minecraft_version(base) or base
        try:
            if pure == version_id:
                log(f"校验 {version_id} 本体 ...")
                core.mcl_install_minecraft(version_id, core.MINECRAFT_DIR,
                                           core._make_mcl_callback(progress_cb=cb))
            else:
                log(f"校验 {version_id}：先补本体 {pure} ...")
                core.mcl_install_minecraft(pure, core.MINECRAFT_DIR,
                                           core._make_mcl_callback(progress_cb=cb))
                n = version_id.lower()
                if "fabric" in n:
                    lk = "fabric"
                elif "neoforge" in n or "neo" in n:
                    lk = "neoforge"
                elif "forge" in n:
                    lk = "forge"
                else:
                    lk = None
                if lk:
                    log(f"补全 {lk} 加载器 ...")
                    install_mod_loader_captured(
                        lk, pure, core.MINECRAFT_DIR, None,
                        self._best_java_for(pure), log, progress_cb=cb)
            self.bridge.progressReset.emit()
            self.bridge.status.emit(f"{version_id} 校验/补全完成")
            self._end_download()
            self.bridge.busy.emit(False)
            run_bg(self._refresh_versions_bg)
        except Exception as exc:
            log(f"校验 {version_id} 失败: {exc}")
            self.bridge.progressReset.emit()
            self.bridge.status.emit(f"{version_id} 校验失败")
            self.bridge.error.emit("校验失败", str(exc))
            self._end_download()
            self.bridge.busy.emit(False)

    def _download_all_release(self):
        if not self.installed:
            QMessageBox.information(self, "提示", "没有本地版本需要补全")
            return
        if self.is_downloading:
            return
        ret = QMessageBox.question(self, "确认",
                                   f"将补全 {len(self.installed)} 个版本的文件，是否继续？")
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._begin_download()
        self._save_settings()
        core.apply_paths(self.cfg)
        self._begin_busy()
        run_bg(self._batch_download_bg, list(self.installed))

    def _batch_download_bg(self, versions: list):
        for version_id in versions:
            try:
                self.bridge.status.emit(f"补全 {version_id}...")
                core.mcl_install_minecraft(
                    core.mcl_minecraft_version(version_id), core.MINECRAFT_DIR,
                    core._make_mcl_callback(progress_cb=(
                        lambda c, t, n=None, _vid=version_id:
                        self.bridge.progress.emit(
                            c, t, f"补全 {_vid}" + (f" ({n})" if n else "")))))
            except Exception as exc:
                self.bridge.log.emit(f"补全 {version_id} 失败: {exc}")
        self.bridge.progressReset.emit()
        self._end_download()
        self.bridge.busy.emit(False)
        run_bg(self._refresh_versions_bg)

    # ---------------- 下载新版本 ----------------
    def download_version(self):
        if self.is_downloading:
            return
        v = self._selected_dl_version()
        if not v:
            QMessageBox.warning(self, "提示", "请先在下载列表里选择一个版本")
            return
        version_id = v[0]
        QMessageBox.information(self, "版本确认", f"即将下载的版本：\n\n{version_id}")
        self._download_new_with_loader_choice(version_id, None)

    def _download_new_with_loader_choice(self, version_id: str, url):
        dlg = LoaderPickDialog(self, version_id, is_detail=True)
        choice = dlg.exec_and_get()
        if choice is None:
            return
        loader_kind, loader_ver, custom_name = choice
        # custom_name 是用户显式输入的实例名；留空则 None。
        # want_clone = True 表示用户给定了自定义名，下载完成后要克隆 src -> 该名。
        # want_clone = False 表示用户留空，直接用底层生成的名字（src_id）。
        final_name = (custom_name or "").strip()
        if final_name:
            if re.search(r'[\\/:*?"<>|]', final_name):
                QMessageBox.warning(self, "实例名无效",
                                    "实例名包含非法字符（\\ / : * ? \" < > |），请换个名字")
                return
            if final_name in self.installed or (core.VERSIONS_DIR / final_name).is_dir():
                QMessageBox.warning(self, "实例名已存在", f"实例名 {final_name} 已存在，请换个名字")
                return
        want_clone = bool(final_name)
        self._begin_download()
        self._save_settings()
        core.apply_paths(self.cfg)
        self._begin_busy()
        run_bg(self._download_new_bg, version_id, url, loader_kind, loader_ver,
               final_name, want_clone)

    def _download_new_bg(self, version_id, url, loader_kind, loader_ver,
                         final_name, want_clone):
        def log(m):
            self.bridge.log.emit(m)

        def cb(cur, tot, note=None):
            self.bridge.progress.emit(cur, tot,
                                      f"下载 {version_id}" + (f" ({note})" if note else ""))

        self.bridge.status.emit(f"下载 {version_id} 本体...")
        self.bridge.progress.emit(0, 0, f"下载 {version_id} 本体...")
        try:
            if core.MCL_OK:
                core.mcl_install_minecraft(version_id, core.MINECRAFT_DIR,
                                           core._make_mcl_callback(progress_cb=cb))
            elif url:
                core.download_minecraft_version(version_id, url, cb, log)
            else:
                raise RuntimeError("无法获取下载地址，且 minecraft_launcher_lib 不可用")
            log(f"本体 {version_id} 安装完成")

            src_id = version_id
            if loader_kind:
                # install_mod_loader_captured 返回底层生成的默认名字，
                # 例如 1.21.1-neoforge-21.1.264 —— 它才是「真正装好的版本」，
                # 之后所有克隆都必须以它作为源目录。
                src_id = install_mod_loader_captured(
                    loader_kind, version_id, core.MINECRAFT_DIR, loader_ver,
                    self._best_java_for(version_id), log, progress_cb=cb)
                log(f"{loader_kind} 加载器已装: {src_id}")
                if loader_kind == "fabric":
                    core._download_fabric_api_mc(src_id, log, cb)

            if want_clone:
                # 用户显式输入了自定义名 -> 把 src_id 克隆成 final_name，
                # 并强制把新实例 .json 里的 id 字段改成 final_name。
                if not _clone_version_instance(src_id, final_name):
                    raise RuntimeError(f"无法创建实例 {final_name}（可能已存在）")
                self._force_instance_id(final_name, final_name, log)
                log(f"已创建自定义实例: {final_name}（源目录 {src_id}）")
            else:
                # 用户留空 -> 直接使用底层生成的 src_id 作为实例名
                log(f"未填写自定义名，使用加载器生成的实例名: {src_id}")

            self.bridge.progressReset.emit()
            self.bridge.status.emit(f"{version_id} 下载完成")
            self._end_download()
            self.bridge.busy.emit(False)
            run_bg(self._refresh_versions_bg)
        except Exception as exc:
            log(f"下载失败: {exc}")
            self.bridge.progressReset.emit()
            self.bridge.status.emit("下载失败")
            self.bridge.error.emit("下载失败", str(exc))
            self._end_download()
            self.bridge.busy.emit(False)

    def _force_instance_id(self, instance_name: str, new_id: str, log) -> None:
        """强制把某个已存在实例的 .json 里 id 字段改成 new_id。

        这是克隆的「最后保险」：_clone_version_instance 在复制目录时会顺带改写
        <实例名>.json 里的 id，但如果源目录里的 json 文件名与目录名不一致、
        或改写被异常跳过，这里再兜底确认一次，保证实例 id 一定是用户要的名字。
        """
        vdir = core.VERSIONS_DIR / instance_name
        if not vdir.is_dir():
            log(f"[实例id] 目录不存在，跳过: {vdir}")
            return
        # 目标 json 文件名优先按 new_id 找，找不到再扫目录里的任意 *.json
        candidates = [vdir / f"{new_id}.json"] + \
            sorted(vdir.glob("*.json"))
        for jf in candidates:
            if not jf.is_file():
                continue
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                if data.get("id") != new_id:
                    data["id"] = new_id
                    jf.write_text(json.dumps(data, indent=2), encoding="utf-8")
                log(f"[实例id] {jf.name} 的 id 已确认为 {new_id}")
                return
            except Exception as exc:
                log(f"[实例id] 改写 {jf.name} 失败: {exc}")
                continue
        log(f"[实例id] 未找到 {instance_name} 的 json，跳过 id 改写")

    # ---------------- 版本 Mod 管理 ----------------
    def _is_mod_loader_version(self, version_id: str) -> bool:
        """判断某版本是否带 Mod 加载器（forge/fabric/neoforge/quilt）。

        检测顺序：
          1. 版本目录下 .json 内容里是否出现加载器关键字（最可靠，能命中
             被重命名的克隆实例，其 json 里通常含加载器 id）
          2. 版本名关键字（forge/fabric/neoforge/quilt/neo）
        纯原版（无加载器）返回 False。
        """
        v = (version_id or "").lower()
        # 1) json 内容检测
        try:
            vdir = core.VERSIONS_DIR / version_id
            for jf in sorted(vdir.glob("*.json")):
                txt = jf.read_text(encoding="utf-8", errors="replace").lower()
                if any(k in txt for k in ("forge", "fabric", "neoforge", "quilt",
                                          "mod_loader", "modloader")):
                    return True
        except Exception:
            pass
        # 2) 版本名关键字兜底
        return any(k in v for k in ("forge", "fabric", "neoforge", "quilt", "neo"))

    def _open_mod_manager(self):
        """在【版本列表】页打开当前选中版本的 Mod 管理器窗口。

        仅允许带 Mod 加载器的版本进入；纯原版直接拦截并提示。
        """
        version_id = self._selected_version()
        if not version_id:
            QMessageBox.warning(self, "提示", "请先在版本列表里选择一个版本")
            return
        if version_id not in self.installed:
            QMessageBox.warning(self, "提示", "该版本尚未下载")
            return
        # 拦截：纯原版不支持加载 Mod
        if not self._is_mod_loader_version(version_id):
            QMessageBox.warning(
                self, "版本无 Mod 加载器",
                f"版本「{version_id}」为纯原版，不支持加载 Mod。\n\n"
                f"请先安装 Forge / Fabric / NeoForge 后再来管理 Mod。")
            return
        dlg = ModManagerDialog(self, version_id)
        dlg.exec()

    # ---------------- 启动 ----------------
    def launch(self):
        # 启动只受「已有游戏在跑 / 正在启动」限制，绝不看 self.busy：
        # 下载任务与游戏运行完全解耦，玩家可以在下载的同时启动游戏。
        if self.is_downloading:
            QMessageBox.information(self, "正在下载", "有下载任务进行中，稍等再启动？（可继续下载）")
        version_id = self._selected_version()
        if not version_id:
            QMessageBox.warning(self, "提示", "请选择一个版本")
            return
        if version_id not in self.installed:
            QMessageBox.warning(self, "提示", "该版本尚未下载")
            return
        ok, missing = core.check_version_complete(version_id)
        if not ok:
            QMessageBox.warning(self, "版本不完整",
                                f"版本 {version_id} 缺少：\n- " + "\n- ".join(missing)
                                + "\n\n点击「校验/补全所选版本」可自动补全。")
            return
        java = self._selected_java()
        if not java:
            QMessageBox.warning(self, "提示", "未找到 Java，请先下载/安装 Java")
            return
        username, uuid_sel = self._current_player()
        mem = self.mem_spin.value()
        offline = self.offline_check.isChecked()
        language = self._lang_code()
        jvm_extra = self.jvm_edit.text().strip() or None
        self._save_settings()
        java = self._check_java_for_version(version_id, java)

        # 外置登录：把当前选中角色的真实 name / uuid 一并带进启动线程
        if self._auth_session:
            self._log(f"[启动] 外置登录角色: {username} (uuid={uuid_sel or '未取到'})")
        else:
            self._log(f"[启动] 离线模式玩家名: {username}")

        # ★ 启动前校验 Token 放在后台线程 _launch_bg 的最前面做（不冻结 UI）
        # 启动专用置忙：只禁「启动所选版本」按钮（防止重复启动），
        # 绝不禁用下载/补全/加载器按钮，绝不 set 全局 self.busy 阻塞下载。
        self._set_launch_busy(True)
        # [新增] 启动前自动释放内存（若玩家勾选了「启动游戏前自动释放内存」）
        # 静默在后台执行，日志打一行，不阻断启动流程。
        self._silent_clear_mem_before_launch(self._log)
        run_bg(self._launch_bg, version_id, java, username, mem, offline, language,
               jvm_extra, core.MINECRAFT_DIR, uuid_sel)

    # ---------------- 启动前 Token 校验（在后台线程里跑） ----------------
    def _preflight_token_check(self) -> bool:
        """校验 accessToken 是否还有效（在 _launch_bg 后台线程里调用，不冻结 UI）。

        返回 True  = 可以继续启动（离线模式 / Token 有效 / 网络失败无法判断时都放行）
        返回 False = Token 明确失效，已通过 tokenInvalid 信号通知主线程
        """
        sess = self._auth_session
        if not sess:
            return True
        mode = sess.get("mode") or ("token" if sess.get("accessToken") else "drag")
        token = (sess.get("accessToken") or "").strip()
        if mode != "token" or not token:
            # 纯拖拽模式：没有 token，注入器接管验证，无需校验
            self.bridge.log.emit("[外置登录] 拖拽模式（无 Token），跳过过期校验")
            return True

        server = sess.get("server") or sess.get("authserver") or ""
        self.bridge.status.emit("正在校验外置登录状态 ...")
        try:
            res = yggdrasil_validate_token(
                server, token, sess.get("clientToken") or "")
        except Exception as exc:
            self.bridge.log.emit(f"[外置登录] 校验异常（放行）：{exc}")
            return True

        valid = res.get("valid")
        self.bridge.log.emit(f"[外置登录] 校验结果: valid={valid} "
                             f"status={res.get('status')} {res.get('message')}")
        # [FIX-登录态恢复] 容错做到极致：valid=False / valid=None 都不清空登录态、不阻断启动。
        # 皮肤站的 /validate 接口经常损坏（乱报 404/401/403），误判风险极高。
        # 游戏真正连到主界面后 authlib-injector 自己报「无效的会话」才是真失效。
        # 所以：无论 valid=False 还是 valid=None，都放行，只打日志警告。
        if valid is False:
            reason = res.get("message") or "Token 可能失效"
            self.bridge.log.emit(
                f"[外置登录] 校验失败（可能是皮肤站不支持 /validate 或网络问题）：{reason}\n"
                f"尝试强行启动游戏；若游戏内提示「无效的会话」，请重新登录。")
        elif valid is None:
            self.bridge.log.emit("[外置登录] 无法判断 Token 状态（网络问题/接口不可用），按有效放行")
        else:
            self.bridge.status.emit("外置登录状态正常")
        return True

    @pyqtSlot(str)
    def on_token_invalid(self, reason: str):
        """【主线程】Token 校验失败提示。

        [FIX-登录态恢复] 皮肤站 /validate 接口经常损坏（乱报 404/401/403），
        这里只弹「非阻断」提示，绝不清空本地登录态。玩家如果游戏内真报
        「无效的会话」，可手动去设置页重新登录；重启前自动恢复的登录态保留。
        """
        # [FIX-登录态恢复] 不再清空本地登录态 —— 保留，避免误伤
        self._set_launch_busy(False)
        self.bridge.status.emit("外置登录校验失败（未清空登录态）")
        QMessageBox.warning(
            self, "外置登录校验提醒",
            f"外置登录校验接口返回异常，可能皮肤站不支持 /validate 或网络问题。\n\n"
            f"详情：{reason}\n\n"
            f"已保留本地登录态并继续启动。若游戏内提示「无效的会话」，"
            f"请到【设置 → 皮肤站配置】重新登录。")

    def _set_launch_busy(self, busy: bool):
        """只控制「启动所选版本」按钮可用性，不影响任何下载按钮。"""
        if getattr(self, "btn_launch", None) is not None:
            self.btn_launch.setEnabled(not busy)

    # ---------------- 版本隔离（物理隔离：--gameDir 指向版本目录） ----------------
    # 隔离的「存档 / 配置」子项：这些全都要按版本独立，否则两个版本会互相覆盖
    ISOLATED_SUBDIRS = ("mods", "shaderpacks", "resourcepacks",
                        "saves", "config", "logs", "screenshots",
                        "replay_recordings", "crash-reports")
    ISOLATED_FILES = ("options.txt", "optionsof.txt", "servers.dat",
                      "usercache.json", "realms_persistence.json")

    def apply_version_isolation(self, version_id: str, mc_dir: Path, log) -> Path:
        """PCL2 级物理版本隔离：为版本准备独立的游戏工作目录，并返回它。

        核心思路（最彻底、最不会串台的方案）：
          启动参数里的 --gameDir 不再指向全局 .minecraft，而是指向
              <mc_dir>/versions/<version_id>
          于是游戏自动在这个目录下创建/读取自己的
              saves（存档）、config（配置）、options.txt（按键/视频设置）
          两个版本各玩各的，永远不会互相覆盖。

        本函数负责：
          1. 确保版本目录下存在全部隔离子目录（mods / saves / config / ...）
          2. 首次使用某个版本时，从全局目录「一次性播种」：
             - 全局 options.txt / servers.dat -> 拷进版本目录（省得玩家重设按键）
             - 全局 saves 下的存档 -> 拷进版本目录（老玩家存档不丢）
             - 全局 config -> 拷进版本目录
             之后再也不会覆盖（只补缺失，绝不删除玩家已有数据）
          3. 把 versions/<id>/mods、shaderpacks 就地当作本版本资源目录：
             因为是同一个目录，无需再往全局复制 —— 这本身就消除了串台。
        返回：应该传给 --gameDir 的路径。
        """
        mc_dir = Path(mc_dir)
        vdir = mc_dir / "versions" / version_id
        game_dir = vdir          # ← 关键：版本目录即游戏工作目录
        log(f"[版本隔离] 版本工作目录(--gameDir): {game_dir}")
        if not vdir.is_dir():
            log(f"[版本隔离] 警告：版本目录不存在 {vdir}，仍按该路径启动")
        try:
            vdir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            log(f"[版本隔离] 创建版本目录失败: {exc}，回退全局目录")
            return mc_dir

        # 1) 建立全部隔离子目录
        for sub in self.ISOLATED_SUBDIRS:
            d = vdir / sub
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                log(f"[版本隔离] 创建 {d.name}/ 失败: {exc}")

        # 2) 首次播种：只补缺失，绝不覆盖
        # [FIX-版本隔离] shutil.copy2 内部用 open() 读源 + 写目标，函数返回时句柄已自动释放。
        # 这里【没有】留任何未 close() 的文件句柄 —— Java 启动后写 config/logs 不会
        # 被 Python 线程占用。若以后改用 open() 手动读，务必 with 包裹、用完即关。
        seeded = []
        for fname in self.ISOLATED_FILES:
            src = mc_dir / fname
            dst = vdir / fname
            if src.is_file() and not dst.exists():
                try:
                    shutil.copy2(str(src), str(dst))
                    seeded.append(fname)
                except Exception as exc:
                    log(f"[版本隔离] 播种 {fname} 失败: {exc}")

        # 全局 saves / config 首次播种（只在版本目录为空时）
        for sub in ("saves", "config"):
            src = mc_dir / sub
            dst = vdir / sub
            if not src.is_dir():
                continue
            try:
                has_any = dst.is_dir() and any(dst.iterdir())
            except Exception:
                has_any = False
            if has_any:
                continue          # 版本已有自己的数据，绝不动
            n = 0
            try:
                for item in src.iterdir():
                    t = dst / item.name
                    if t.exists():
                        continue
                    if item.is_dir():
                        shutil.copytree(str(item), str(t), dirs_exist_ok=True)
                    else:
                        shutil.copy2(str(item), str(t))
                    n += 1
            except Exception as exc:
                log(f"[版本隔离] 播种 {sub} 失败: {exc}")
            if n:
                seeded.append(f"{sub}/({n} 项)")

        if seeded:
            log(f"[版本隔离] 首次使用，已从全局目录播种: {', '.join(seeded)}")
        else:
            log("[版本隔离] 版本目录已有独立数据，无需播种")

        # 3) 统计各隔离目录内容，方便核对隔离是否真的生效
        for sub in ("mods", "shaderpacks", "saves", "config"):
            d = vdir / sub
            try:
                n = len([f for f in d.iterdir()]) if d.is_dir() else 0
            except Exception:
                n = 0
            log(f"[版本隔离]   {sub}/: {n} 项（独立于其它版本）")
        log(f"[版本隔离] 完成：本版本存档/配置全部落在 {game_dir}")
        return game_dir

    def _launch_bg(self, version_id, java, username, mem, offline, language,
                   jvm_extra, mc_dir, selected_uuid=""):
        self.bridge.status.emit("启动中...")
        self.bridge.progressReset.emit()

        def log(m):
            self.bridge.log.emit(m)

        # ---- ★ 启动前先校验 Token（后台线程，不冻结 UI）----
        # [FIX-登录态恢复] 校验不再阻断启动：无论返回 True/False 都继续往下走。
        # 皮肤站 /validate 经常是坏接口，误判风险极高；游戏内 authlib-injector
        # 连到主界面后若真失效会自己弹「无效的会话」，启动器不做致命拦截。
        if not self._preflight_token_check():
            log("[外置登录] 校验失败（可能是皮肤站不支持或网络问题），尝试强行启动...")

        # ---- 外置登录：准备 authlib-injector（-javaagent:<jar>=<API根地址>）----
        # auth 为 None 表示纯离线模式（JVM 不挂注入器）。
        auth = None
        if self._auth_session:
            sess = self._auth_session
            server = (sess.get("server") or sess.get("authserver") or "").strip()
            log(f"[外置登录] 会话: server={server} "
                f"user={sess.get('username')} uuid={sess.get('uuid')}")
            # ★ 优先取「玩家名」下拉框里选中的角色的真实 UUID / 名字
            chosen_username = (self.name_combo.currentText() or "").strip() or username
            chosen_uuid = (self.name_combo.currentData()
                           or selected_uuid or sess.get("uuid") or "")
            try:
                # 自动检测 + 自动下载：BASE_DIR/authlib-injector.jar 不存在就后台拉
                self.bridge.status.emit("检查 authlib-injector.jar ...")
                inj_path = ensure_authlib_injector(
                    log_cb=log,
                    status_cb=lambda m: self.bridge.status.emit(m))
                # 缺 token 的「纯拖拽模式」也要挂上 -javaagent，让注入器接管验证
                token = (sess.get("accessToken") or sess.get("access_token") or "").strip()
                token_for_game = token or "0"
                auth = {
                    "injector": str(inj_path),
                    "server": server,
                    "access_token": token_for_game,
                    "uuid": chosen_uuid,
                    "username": chosen_username,
                }
                log(f"[外置登录] injector={inj_path}")
                log(f"[外置登录] -javaagent:{inj_path}"
                    + (f"={server}" if server else ""))
                log(f"[外置登录] username={auth['username']} "
                    f"uuid={auth['uuid'] or '(未登录, 将用离线UUID)'} "
                    f"token={token[:12] + '...' if token else '(纯拖拽模式, 由注入器接管)'}")
            except Exception as exc:
                log(f"[外置登录] authlib-injector 准备失败: {exc}")
                # 注入器是外置登录的命脉，拿不到就不要假装登录成功
                self.bridge.status.emit("启动失败")
                self.bridge.error.emit(
                    "authlib-injector 不可用",
                    f"无法获取 authlib-injector.jar，外置登录无法工作：\n{exc}\n\n"
                    f"可手动下载后放到：\n{authlib_injector_dest()}")
                self.bridge.launchBusy.emit(False)
                return

        # ---- 版本隔离：物理隔离 --gameDir 到 versions/<版本名> ----
        # 这样 saves / config / options.txt 全部按版本独立，互不覆盖。
        try:
            game_dir = self.apply_version_isolation(version_id, Path(mc_dir), log)
        except Exception as exc:
            log(f"[版本隔离] 失败，回退全局目录: {exc}")
            game_dir = Path(mc_dir)

        try:
            core.mcl_extract_natives(version_id)
            cmd = core.mcl_build_launch_cmd(
                version_id, java, username, mem, jvm_extra,
                auth=auth, game_dir=game_dir)
            log(f"启动命令已生成（{len(cmd)} 个参数）")
            log(" ".join(cmd))
            # 自检：外置登录时必须能看到 -javaagent，且不含全 0 uuid
            if auth:
                if not any(str(a).startswith("-javaagent:") for a in cmd):
                    raise RuntimeError("启动命令里缺少 -javaagent（authlib-injector 未注入）")
                if "00000000-0000-0000-0000-000000000000" in cmd:
                    raise RuntimeError("启动命令里仍然是全 0 的假 UUID，注入失败")
                log("[外置登录] 自检通过：-javaagent 已注入，uuid/token 已替换为真实值")
            # 自检：--gameDir 必须指向版本目录
            if "--gameDir" in cmd:
                gi = cmd.index("--gameDir")
                if gi + 1 < len(cmd):
                    log(f"[版本隔离] 自检通过：--gameDir = {cmd[gi + 1]}")
        except Exception as exc:
            self.bridge.status.emit("启动失败")
            self.bridge.error.emit("启动失败",
                                   f"生成启动命令失败: {exc}\n\n版本: {version_id}\n"
                                   f"如需补全文件，请先点击「校验/补全所选版本」。")
            self.bridge.launchBusy.emit(False)
            return

        self.bridge.gameStarted.emit(version_id)

        def run():
            try:
                # [FIX-版本隔离] 启动前写入探测：若版本目录不可写（杀软拦截 / 权限不足），
                # 提前暴露，别等 Java 起来后才发现 config/logs 打不开。
                try:
                    _probe = Path(game_dir) / ".write_test_launch"
                    _probe.write_text("test", encoding="utf-8")
                    _probe.unlink()
                    log(f"[启动前探测] 目录可写: {game_dir}")
                except Exception as exc:
                    log(f"[启动前探测] 严重错误，目录不可写: {exc}")
                    self.bridge.error.emit(
                        "权限错误",
                        f"无法写入游戏目录 {game_dir}，请检查是否有杀毒软件拦截，"
                        f"或尝试以管理员身份运行。\n\n详情：{exc}")
                    self.bridge.launchBusy.emit(False)
                    return

                # [FIX-版本隔离] 若仍有后台下载/校验任务在写版本目录（_refresh_versions_bg、
                # _begin_download 等），先等它结束再拉起 Java，避免 Java 写 config/logs 时
                # 被 Python 线程抢目录锁。最多等 10s，超时则强制继续（Java 会自己重试）。
                if self.busy:
                    log("[版本隔离] 检测到后台下载/校验任务仍在写目录，等待其结束...")
                    waited = 0
                    while self.busy and waited < 10:
                        time.sleep(0.5)
                        waited += 0.5
                    if self.busy:
                        log(f"[版本隔离] 等待 {waited}s 后台任务仍未结束，强制继续启动")

                # [FIX-版本隔离] cwd 必须指向版本隔离目录（= --gameDir 的目标），
                # NeoForge / 加载器启动极度依赖当前工作目录，传错会导致
                # "Access denied .../config" / "FileNotFoundException logs/latest.log"。
                proc = subprocess.Popen(
                    cmd, cwd=str(game_dir),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                self._running_proc[version_id] = proc
                self.bridge.gameLog.emit(version_id, " ".join(cmd) + "\n")

                def reader():
                    buf, last = [], time.time()
                    # 游戏进程 stdout 的字节码在中文 Windows 上通常是 GBK（cp936），
                    # 但混入的英文/进度条又多是 UTF-8。逐行做「UTF-8 优先、GBK 兜底」
                    # 的容错解码，绝不再一刀切 utf-8 把中文全变成「锘」/「□」乱码。
                    def decode_line(raw: bytes) -> str:
                        if not raw:
                            return ""
                        # 1) 先按 UTF-8 严格解码；成功（合法 UTF-8）就直接用
                        try:
                            return raw.decode("utf-8")
                        except UnicodeDecodeError:
                            pass
                        # 2) 不是合法 UTF-8 -> 按 Windows 系统默认编码（mbcs，中文系统=GBK）
                        #    再用 replace 兜底，保证任何脏字节都不会抛异常中断读取。
                        try:
                            return raw.decode("mbcs", errors="replace")
                        except Exception:
                            return raw.decode("gbk", errors="replace")

                    try:
                        for line in iter(proc.stdout.readline, b""):
                            buf.append(decode_line(line))
                            if time.time() - last >= 0.15:
                                self.bridge.gameLog.emit(version_id, "".join(buf))
                                buf.clear()
                                last = time.time()
                    except Exception:
                        pass
                    finally:
                        if buf:
                            try:
                                self.bridge.gameLog.emit(version_id, "".join(buf))
                            except Exception:
                                pass

                run_bg(reader)
                ret = proc.wait()
                if self._running_proc.get(version_id) is proc:
                    self._running_proc[version_id] = None
                self.bridge.gameExited.emit(version_id, ret)
            except Exception as exc:
                self.bridge.gameLog.emit(version_id, f"启动失败: {exc}\n")
                self.bridge.error.emit("启动失败", str(exc))
                self.bridge.launchBusy.emit(False)

        run_bg(run)
        self.bridge.status.emit("游戏启动中...（日志见独立窗口）")

    @pyqtSlot(str)
    def on_game_started(self, version_id: str):
        self._open_game_log_window(version_id)

    @pyqtSlot(str, str)
    def on_game_log(self, version_id: str, text: str):
        dlg = self._log_windows.get(version_id)
        if dlg is not None:
            dlg.append_text(text)

    @pyqtSlot(bool)
    def on_launch_busy(self, busy: bool):
        """启动专用忙碌：只控制「启动所选版本」按钮，绝不影响下载类按钮。"""
        self._set_launch_busy(busy)

    @pyqtSlot(str, int)
    def on_game_exited(self, version_id: str, ret: int):
        # 游戏退出：只解除「启动」按钮的禁用，绝不碰全局 self.busy，
        # 这样即使启动过游戏，下载按钮也从未被锁死。
        self.bridge.launchBusy.emit(False)
        user_killed = bool(self._user_killed.pop(version_id, False))
        # 玩家手动点「结束进程」杀掉的：只记日志、关窗口，绝不弹崩溃警告
        if user_killed:
            self._log(f"[游戏退出] 已手动结束游戏进程: {version_id}")
            dlg = self._log_windows.get(version_id)
            if dlg is not None:
                dlg.append_text("=== 已手动结束游戏进程 ===\n")
                QTimer.singleShot(3000, dlg.close)
            return

        dlg = self._log_windows.get(version_id)
        if ret != 0:
            if dlg is not None:
                dlg.append_text(f"=== 游戏异常退出（代码 {ret}）===\n")
            # 是否弹警告由【设置->日志系统】开关决定（默认开）
            if getattr(self, "warn_on_exit_check", None) is None \
                    or self.warn_on_exit_check.isChecked():
                self.bridge.warn.emit(
                    "游戏退出",
                    f"游戏以代码 {ret} 退出\n\n请查看「游戏日志」窗口，"
                    f"可能是 Java 版本不兼容或版本文件缺失。")
            else:
                self._log(f"[游戏退出] 异常退出（代码 {ret}），已按设置跳过警告")
        else:
            if dlg is not None:
                dlg.append_text("=== 游戏已正常退出 ===\n")
        if dlg is not None:
            QTimer.singleShot(10000, dlg.close)

    def _open_game_log_window(self, version_id: str):
        if version_id in self._log_windows:
            self._log_windows[version_id].show()
            self._log_windows[version_id].raise_()
            self._log_windows[version_id].activateWindow()
            return
        # parent 传 None：日志是独立顶层窗口，不依附主窗口、不随主窗口最小化
        dlg = GameLogWindow(None, version_id,
                            kill_cb=lambda: self._kill_game_process(version_id))
        dlg.destroyed.connect(lambda *_: self._log_windows.pop(version_id, None))
        self._log_windows[version_id] = dlg
        dlg.show()

    def _kill_game_process(self, version_id: str):
        proc = self._running_proc.get(version_id)
        if proc is None or proc.poll() is not None:
            QMessageBox.information(self, "结束进程", "该版本没有正在运行的进程（可能已退出）")
            return
        # 先标记为「玩家主动结束」，on_game_exited 据此跳过崩溃警告
        self._user_killed[version_id] = True
        try:
            proc.terminate()
            if proc.poll() is None:
                proc.kill()
            self._log(f"[结束进程] 已结束 {version_id} 的游戏进程")
        except Exception as exc:
            self._user_killed.pop(version_id, None)
            QMessageBox.critical(self, "结束进程", f"结束进程失败: {exc}")

    # ---------------- 日志窗口 ----------------
    def _show_log_dialog(self):
        if self._global_log_dialog is None:
            self._global_log_dialog = TextDialog(self, "运行日志", "".join(LOG_BUFFER))
            self._global_log_dialog.finished.connect(
                lambda _: setattr(self, "_global_log_dialog", None))
        else:
            self._global_log_dialog.raise_()
        self._global_log_dialog.show()

    # ==================================================================
    # Mod 加载器
    # ==================================================================
    def _refresh_res_version_list(self, versions=None):
        if versions is None:
            try:
                versions = core.installed_versions()
            except Exception:
                versions = self.installed or []
        cur = self.res_ver_combo.currentText()
        self.res_ver_combo.blockSignals(True)
        self.res_ver_combo.clear()
        self.res_ver_combo.addItems(list(versions))
        if cur in versions:
            self.res_ver_combo.setCurrentText(cur)
        self.res_ver_combo.blockSignals(False)

    def _selected_res_version(self) -> str:
        v = self.res_ver_combo.currentText().strip()
        if v:
            return v
        return self.installed[0] if self.installed else ""

    def _open_res_dir(self, sub: str):
        try:
            d = core._ensure_resource_dir(self._selected_res_version(), sub)
            os.startfile(str(d))
            self._log(f"已打开: {d}")
        except Exception as exc:
            self._log(f"打开失败: {exc}")

    # ---------------- 搜索用：MC 版本 / 加载器 下拉框的取值 ----------------
    def _pure_installed_versions(self) -> list:
        """从已安装实例里提取「纯原版 MC 版本号」（去重、按版本号倒序）。

        关键：MC 主版本一律以 "1." 开头（1.8 ~ 1.21.x），
        而 NeoForge(21.x) / Forge(47.x) 等加载器版本号是大数字开头的，
        用 "1." 加数字的前缀判断可同时做到：
          - 不放过 1.20.1 / 1.19.2 这类正常原版版本
          - 排除 neoforge-21.1.264 里被误提取的 "21.1" / "21.1.264"
          - 排除快照/预发布（1.20.1-rc1）里被截断的 "1.20.1"
        """
        seen, out = set(), []
        for vid in self.installed:
            pure = self._instance_game_version(vid)
            if not pure or pure in seen:
                continue
            # 只收 "1.xx(.yy)?" 形式的真正 MC 主版本
            if re.fullmatch(r"1\.\d+(?:\.\d+)?", pure):
                seen.add(pure)
                out.append(pure)
        out.sort(key=lambda v: [int(x) for x in v.split(".")] or [0], reverse=True)
        return out

    def _instance_game_version(self, version_id: str) -> str:
        """读取某个已装实例真正的游戏 MC 版本。

        优先级：
          1. versions/<id>/*.json 的 inheritsFrom 字段（加载器实例指向父级原版）
          2. versions/<id>/*.json 的 id 字段
          3. 回退 core.mcl_minecraft_version(version_id)（正则从目录名提取）
        最后统一只保留 "1.xx" 形式的 MC 主版本，避免加载器版本号混入。
        """
        vdir = core.VERSIONS_DIR / version_id
        try:
            for jf in sorted(vdir.glob("*.json")):
                data = json.loads(jf.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                # 加载器实例的 json 通常有 inheritsFrom 指向父级原版（如 "1.20.1"）
                for key in ("inheritsFrom", "id"):
                    val = data.get(key)
                    if val and re.fullmatch(r"1\.\d+(?:\.\d+)?", str(val)):
                        return str(val)
        except Exception:
            pass
        # 兜底：从目录名正则提取，同样只要 "1.xx" 形式
        pure = core.mcl_minecraft_version(version_id) or ""
        if re.fullmatch(r"1\.\d+(?:\.\d+)?", pure):
            return pure
        return ""

    def _refresh_all_mc_ver_combos(self):
        """一次性拉官方全量清单，填满三个【目标 MC 版本】下拉框。

        复用 on_online_ready 已拉好的 online_versions（release+snapshot），
        没有则后台拉一次，再统一发给三个下拉框，避免重复网络请求。
        """
        combos = [c for c in (getattr(self, "mod_mc_combo", None),
                              getattr(self, "shader_mc_combo", None),
                              getattr(self, "modpack_mc_combo", None))
                  if c is not None]
        if not combos:
            return
        # 先把各自已选值占位，避免空白；清单回来后统一填充
        for combo in combos:
            cur = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("All（不限制）")
            if cur and cur != "All（不限制）":
                combo.addItem(cur)
            combo.blockSignals(False)

        def work():
            try:
                items = core.mcl_version_list()   # [(id, type, url)] release 在前、新->旧
            except Exception as exc:
                self.bridge.log.emit(f"[版本清单] 获取失败: {exc!r}")
                items = []
            all_vers = [vid for vid, vtype, _u in items
                        if vtype in ("release", "snapshot")]
            # 官方清单拿不到（离线/被墙）时，回退到本地已装的原版版本，保证下拉框仍可用
            if not all_vers:
                all_vers = self._pure_installed_versions()
                if all_vers:
                    self.bridge.log.emit(
                        "[版本清单] 官方清单不可用，已回退到本地已装版本")
            for combo in combos:
                self.bridge.mcVersionsReady.emit(combo, all_vers)

        run_bg(work)

    def _refresh_mc_ver_combo(self, combo: QComboBox, keep_current: bool = True):
        """用官方 Mojang 全量清单灌进【目标 MC 版本】下拉框（可编辑）。

        不再读本地 versions 文件夹做正则匹配，而是直接调 core.mcl_version_list()
        拿官方 release（+snapshot）全量版本号，从新到旧 addItems。
        三个下拉框共用同一份清单，这里统一走 _refresh_all_mc_ver_combos
        （拉一次、三个一起填），避免重复网络请求。
        """
        self._refresh_all_mc_ver_combos()
        return combo

    @pyqtSlot(object, list)
    def on_mc_versions_ready(self, combo: QComboBox, versions: list):
        """把官方全量版本号（release+snapshot，新->旧）灌进目标下拉框。"""
        if combo is None:
            return
        cur = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("All（不限制）")
        combo.addItems(versions)
        # 恢复之前的选择（用户可能正停在某个版本上）
        idx = 0
        if cur and cur != "All（不限制）":
            found = combo.findText(cur)
            if found >= 0:
                idx = found
        combo.setCurrentIndex(idx)
        combo.blockSignals(False)

    def _selected_mc_ver(self, combo: QComboBox) -> str:
        """读取【目标 MC 版本】下拉框：选 All 或留空则返回 ''（不传版本过滤）。"""
        v = combo.currentText().strip()
        if not v or v.startswith("All"):
            return ""
        return v

    def _selected_loader_val(self, combo: QComboBox) -> str:
        """读取【加载器】下拉框：选 all 则返回 ''（不传加载器过滤）。"""
        v = combo.currentText().strip()
        return "" if not v or v == "all" else v

    def _resolve_target_instance(self, mc_ver: str, loader: str, fallback: str = "") -> str:
        """下载资源时，根据 纯 MC 版本 + 加载器 挑一个本地实例目录名。

        匹配优先级（HMCL 式）：
          1. 实例真实 MC 版本 == mc_ver 且 加载器名匹配 -> 精确命中
          2. 实例真实 MC 版本 == mc_ver -> 版本命中（任取其一）
          3. 都没有 -> 返回 fallback（如资源页当前选中的实例），没有再空串
        返回的是 versions/ 下的实例目录名（可传给 core._ensure_resource_dir）。
        """
        if not mc_ver:
            return fallback
        loader = (loader or "").lower()
        exact, any_ver = [], []
        for vid in self.installed:
            if self._instance_game_version(vid) != mc_ver:
                continue
            any_ver.append(vid)
            low = vid.lower()
            if loader and loader in low:
                exact.append(vid)
        if exact:
            return exact[0]
        if any_ver:
            return any_ver[0]
        return fallback

    def _loader_kind(self) -> str:
        btn = self.loader_kind_group.checkedButton()
        if btn is None:
            return "forge"
        idx = self.loader_kind_group.id(btn)
        return LOADER_KINDS[max(0, idx)][0]

    def _refresh_loader_list(self):
        kind = self._loader_kind()
        mc = self.loader_mc_combo.currentText().strip()
        self.loader_table.setRowCount(0)
        self._loader_items = []
        self._log(f"获取 {kind} 加载器列表 (MC {mc})...")
        self.bridge.status.emit(f"获取 {kind} 列表 (MC {mc})...")

        def work():
            try:
                if kind == "fabric":
                    versions = core.get_fabric_versions(mc)
                elif kind == "forge":
                    versions = core.get_forge_versions(mc)
                else:
                    versions = core._get_neoforge_versions(mc, timeout=8)
            except Exception as exc:
                self.bridge.log.emit(f"获取加载器列表失败: {exc!r}")
                versions = []
            self.bridge.loadersReady.emit(kind, list(versions))

        run_bg(work)

    @pyqtSlot(str, list)
    def on_loaders_ready(self, kind: str, versions: list):
        self.loader_table.setRowCount(0)
        self._loader_items = []
        label = {"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge"}.get(kind, kind)
        for entry in versions:
            r = self.loader_table.rowCount()
            self.loader_table.insertRow(r)
            if kind == "fabric":
                ver, url = entry
                item = ("fabric", ver, url)
            elif kind == "forge":
                ver, url = entry
                item = ("forge", ver, None)
            else:
                ver, url = entry
                item = ("neoforge", ver, url)
            self._loader_items.append(item)
            self.loader_table.setItem(r, 0, self._cell(ver))
            self.loader_table.setItem(r, 1, self._cell(label))
            self.loader_table.setItem(r, 2, self._cell("可下载"))
        if not versions:
            self._log(f"{kind} (MC) 未找到加载器版本")
            self.bridge.status.emit(f"{kind} 无可用版本")
        else:
            self.bridge.status.emit(f"{kind} 列表已更新（{len(versions)} 项）")

    def _install_selected_loader(self):
        if self.is_downloading:
            return
        row = self._selected_row(self.loader_table)
        if row < 0 or row >= len(self._loader_items):
            QMessageBox.warning(self, "提示", "请先选择一个加载器版本")
            return
        item = self._loader_items[row]
        kind = item[0]
        ver_id = self._selected_res_version() or self.loader_mc_combo.currentText().strip()
        self._begin_download()
        self._save_settings()
        core.apply_paths(self.cfg)
        self._begin_busy()
        run_bg(self._install_loader_bg, kind, item, ver_id)

    def _install_loader_bg(self, kind: str, item, ver_id: str):
        def log(m):
            self.bridge.log.emit(m)

        def cb(cur, tot, note=None):
            self.bridge.progress.emit(cur, tot,
                                      f"安装 {kind} 加载器..." + (f" ({note})" if note else ""))

        pure = core.mcl_minecraft_version(ver_id) or ver_id
        java = self._best_java_for(pure)
        try:
            if kind == "fabric":
                _, loader_ver, url = item
                if core.MCL_OK:
                    installed = install_mod_loader_captured(
                        "fabric", pure, core.MINECRAFT_DIR, loader_ver, java, log,
                        progress_cb=cb)
                    log(f"Fabric 加载器已装: {installed}")
                    core._download_fabric_api_mc(installed or ver_id, log, cb)
                else:
                    core.download_fabric(ver_id, loader_ver, url, progress_cb=cb, log_cb=log)
                    core._download_fabric_api_mc(ver_id, log, cb)
            elif kind in ("forge", "neoforge"):
                loader_ver = item[1]
                # UI 列表里的 Forge 版本是 "1.20.1-47.4.18" 形式，
                # 而 mcl_install_loader 只接受加载器自身的版本号 "47.4.18"。
                norm_ver = loader_ver
                if norm_ver.startswith(pure + "-"):
                    norm_ver = norm_ver.split("-", 1)[1]
                if core.MCL_OK:
                    # 统一走 lib + 捕获安装器输出，失败时把安装器最后几十行带出来
                    installed = install_mod_loader_captured(
                        kind, pure, core.MINECRAFT_DIR, norm_ver, java, log,
                        progress_cb=cb)
                    log(f"{kind} 加载器已装: {installed}")
                elif kind == "forge":
                    core.download_forge(ver_id, loader_ver, None, None,
                                        progress_cb=cb, log_cb=log)
                else:
                    core._download_neoforge(ver_id, loader_ver, item[2],
                                            progress_cb=cb, log_cb=log)
            log("加载器安装完成")
            self.bridge.progressReset.emit()
            self.bridge.status.emit(f"{ver_id} 加载器安装完成")
        except Exception as exc:
            log(f"加载器安装失败: {exc}")
            self.bridge.error.emit("加载器安装失败", str(exc))
        finally:
            self._end_download()
            self.bridge.busy.emit(False)
            run_bg(self._refresh_versions_bg)

    # ==================================================================
    # Mod 搜索 / 下载（分页）
    # ==================================================================
    # 每页固定 20 条
    PAGE_SIZE = 20

    def _search_loader_value(self, combo: QComboBox):
        v = combo.currentText()
        return "" if not v or v == "all" else v

    def _search_mods(self, page: int = 0):
        """搜索 Mod（统一入口：手动搜索 / 回车 / 切页签自动热门加载都走这里）。

        - query 允许为空字符串：空查询 = 向 Modrinth 请求当前热门项目（index=relevance）。
        - page>0 视为翻页：先查按页缓存 self._mod_cached_pages，命中则直接渲染（零网络），
          未命中才发 API，回来存进缓存再渲染。
        """
        loader = self._selected_loader_val(self.mod_loader_combo)
        ver = self._selected_mc_ver(self.mod_mc_combo) or None
        query = self.mod_search_edit.text().strip()

        if page == 0:
            # 第 0 页：如果是重新搜索（query/ver/loader 变了）就清缓存重来；
            # 但「切页签自动加载热门」也走 page=0，同样清缓存（重新拿最新热门）。
            self._reset_search_context("mod", query, ver, loader)

        # 翻页路径：先查按页缓存，命中零网络，未命中发 API 存缓存
        if page > 0:
            # [FIX-截断恢复] 方法名统一为 _goto_page_cached（原 _goto_cached_page 不存在）
            self._goto_page_cached("mod", page)
            return

        # 第 0 页（新搜索）：解析英文词后发 API
        q_en = self._mod_query_en

        def work():
            try:
                hits = core.modrinth_sdk.modrinth_search_sdk(
                    q_en, ver, loader, limit=self.PAGE_SIZE, offset=0,
                    index="relevance")
                self.bridge.modHits.emit(hits, 0)
            except Exception as exc:
                self.bridge.log.emit(f"搜索失败: {exc!r}")
                self.bridge.status.emit("搜索失败")

        self._log(f"搜索 mod: {query or '（空查询=热门）'} (loader={loader or 'all'}, mc={ver or 'all'}, 第1页)")
        self.bridge.status.emit(f"搜索 mod: {query or '热门'} ...")
        run_bg(work)

    def _pick_first_english(self, query: str) -> str:
        """若为中文词，走 MC百科查英文名并只取最核心的第一个；否则原样返回。"""
        if not re.search(r"[\u4e00-\u9fff]", query):
            return query
        try:
            en_list = core.modrinth_sdk.mcmod_chinese_lookup(query, limit=1)
            if en_list and en_list[0].get("english"):
                return en_list[0]["english"]
        except Exception:
            pass
        return query

    @pyqtSlot(list)
    def _hit_project_id(self, hit) -> str:
        """从搜索结果里稳健提取项目 ID（project_id 优先，slug 兜底）。

        这是整个 Mod 数据流的“锚点”：渲染、解析、下载都必须用同一个 id，
        避免因为字段缺失（部分搜索接口只回 slug）导致 pid 为 None 而串台。
        """
        if not hit:
            return ""
        return str(hit.get("project_id") or hit.get("slug") or "").strip()

    @pyqtSlot(list, int)
    def on_mod_hits(self, hits: list, page: int):
        """收到某一页的结果：先存进按页缓存，再整页渲染。

        page = 该页页号（0 起）。命中缓存路径不会走这里，只有「未缓存才发 API」
        回来时走这里，把结果写进 self._mod_cached_pages[page] 供后续翻页复用。
        """
        # 1) 存入按页缓存：之后翻回这一页绝不会再发网络请求
        self._mod_cached_pages[page] = hits
        # 2) 整页渲染（表格只展示当前这一页，不累积）
        self._render_mod_page(page)

    def _render_mod_page(self, page: int):
        """把第 page 页（取自按页缓存）渲染到 mod_table。

        - 表格整页替换：先清空再插入该页的行。
        - 已解析过版本的 pid 不重复发解析请求（查 self._mod_versions）。
        - 不足整页时标记 _mod_exhausted，「下一页」按钮自动禁用。
        """
        hits = self._mod_cached_pages.get(page, [])
        ver = self._mod_ver
        loader = self._mod_loader

        self.mod_table.setRowCount(0)
        self._mod_rows.clear()

        rows = []
        for h in hits:
            pid = self._hit_project_id(h)
            if not pid:
                continue  # 没有 id/slug 的结果无法定位，跳过（不占行）
            rows.append({
                "pid": pid,
                "title": h.get("name") or h.get("title") or "",
                "loader": ", ".join(h.get("loaders") or [])[:16] or "-",
                "ver": ", ".join((h.get("game_versions") or [])[-2:])[:16] or "-",
                "icon": h.get("icon_url"),
            })
            self._mod_hits[pid] = h
            # 已解析过的版本不重复请求（按页缓存命中后重渲染也省钱）
            if pid not in self._mod_versions:
                run_bg(self._resolve_mod_bg, h, pid, ver, loader)

        self._append_project_rows(self.mod_table, rows)
        self._mod_page = page
        # 本页不足整页 = 已到最后；否则保持「还能翻」
        self._mod_exhausted = (len(hits) < self.PAGE_SIZE)
        self._update_pager("mod")
        self.bridge.status.emit(f"Mod 第{page + 1}页（{len(rows)} 项）")

    def _append_project_rows(self, table: QTableWidget, rows: list):
        """把新行追加到 table 末尾（不清空已加载的页），并登记 pid->row 映射。"""
        for row_data in rows:
            r = table.rowCount()
            table.insertRow(r)
            pid = row_data["pid"]
            icon_item = self._cell("", user_data=pid, align=Qt.AlignmentFlag.AlignCenter)
            table.setItem(r, 0, icon_item)
            title_item = self._cell(row_data["title"], user_data=pid)
            table.setItem(r, 1, title_item)
            loader_item = self._cell(row_data["loader"], user_data=pid)
            table.setItem(r, 2, loader_item)
            ver_item = self._cell(row_data["ver"], user_data=pid)
            table.setItem(r, 3, ver_item)
            if row_data.get("icon"):
                self._wire_icon(table, r, pid, row_data["icon"])
            if table is self.mod_table:
                self._mod_rows[pid] = r
            elif table is self.shader_table:
                self._shader_rows[pid] = r
            elif table is self.modpack_table:
                self._modpack_rows[pid] = r
            elif table is self.respack_table:
                self._respack_rows[pid] = r
        table.resizeColumnToContents(0)
        table.resizeColumnToContents(1)

    def _reset_search_context(self, key: str, query: str, ver, loader):
        """全新搜索时调用：清空表格、缓存页、seen 集合、保存搜索上下文。

        用户改了搜索词 / 换了加载器 / 换了 MC 版本，都会走这里，
        按页缓存整体作废，从第 1 页重新开始。
        """
        table = getattr(self, f"{key}_table")
        info = getattr(self, f"{key}_info")
        rows = getattr(self, f"_{key}_rows")
        hits = getattr(self, f"_{key}_hits")
        versions = getattr(self, f"_{key}_versions")
        cached = getattr(self, f"_{key}_cached_pages")

        table.setRowCount(0)
        rows.clear()
        hits.clear()
        versions.clear()
        cached.clear()
        info.clear()
        self._seen_project_ids = set()

        setattr(self, f"_{key}_page", 0)
        setattr(self, f"_{key}_query", query)
        setattr(self, f"_{key}_ver", ver)
        setattr(self, f"_{key}_loader", loader)
        setattr(self, f"_{key}_exhausted", False)
        setattr(self, f"_{key}_query_en", self._pick_first_english(query))

    def _goto_page_cached(self, key: str, target: int):
        """翻到第 target 页（0 起）：按页缓存优先，命中零网络，未命中才发 API。

        严格实现用户要求的「按页缓存」机制：
          1. if target in cached_pages: 直接拿缓存数据更新 UI，绝不发网络请求
          2. 否则请求 API，回来先 cached_pages[target] = results，再渲染
        """
        cached = getattr(self, f"_{key}_cached_pages")
        if target in cached:
            self._log(f"{key} 第{target + 1}页命中按页缓存，直接渲染（零网络请求）")
            getattr(self, f"_render_{key}_page")(target)
            return

        q_en = getattr(self, f"_{key}_query_en") or ""
        ver = getattr(self, f"_{key}_ver")
        loader = getattr(self, f"_{key}_loader")
        api = self._search_api(key)
        offset = target * self.PAGE_SIZE
        self._log(f"{key} 第{target + 1}页无缓存，请求 API ...")
        self.bridge.status.emit(f"加载 {key} 第{target + 1}页 ...")

        def work():
            try:
                results = api(q_en, ver, loader, offset)
                cached[target] = results          # 先存进按页缓存（后续翻回此页零网络）
                getattr(self, f"on_{key}_hits")(results, target)
            except Exception as exc:
                self.bridge.log.emit(f"搜索失败: {exc!r}")
                self.bridge.status.emit("搜索失败")

        run_bg(work)

    def _search_api(self, key: str):
        """返回某个 tab 的搜索 API 闭包：(q_en, ver, loader, offset) -> hits。"""
        if key == "mod":
            def api(q_en, ver, loader, offset):
                return core.modrinth_sdk.modrinth_search_sdk(
                    q_en, ver, loader, limit=self.PAGE_SIZE, offset=offset,
                    index="relevance")
            return api
        if key == "shader":
            def api(q_en, ver, loader, offset):
                return core.modrinth_search(
                    q_en, project_type="shader", loader=loader, game_version=ver,
                    limit=self.PAGE_SIZE, offset=offset, index="relevance")
            return api
        if key == "respack":
            def api(q_en, ver, loader, offset):
                return core.modrinth_search(
                    q_en, project_type="resourcepack", loader=loader, game_version=ver,
                    limit=self.PAGE_SIZE, offset=offset, index="relevance")
            return api
        def api(q_en, ver, loader, offset):
            return core.modrinth_search(
                q_en, project_type="modpack", loader=loader, game_version=ver,
                limit=self.PAGE_SIZE, offset=offset, index="relevance")
        return api

    def _update_pager(self, key: str):
        """刷新某 tab 的页码标签与上/下一页按钮可用性。"""
        page = getattr(self, f"_{key}_page", 0)
        exhausted = getattr(self, f"_{key}_exhausted", False)
        getattr(self, f"{key}_page_label").setText(f"第 {page + 1} 页")
        getattr(self, f"{key}_prev_btn").setEnabled(page > 0)
        getattr(self, f"{key}_next_btn").setEnabled(not exhausted)

    def _search_next_page(self, key: str):
        """下一页：目标页 = 当前显示页 + 1，走按页缓存（命中零网络，未命中发 API）。"""
        target = getattr(self, f"_{key}_page", 0) + 1
        self._goto_page_cached(key, target)

    def _search_prev_page(self, key: str):
        """上一页：目标页 = 当前显示页 - 1，必然在按页缓存里，直接渲染，零网络。"""
        target = getattr(self, f"_{key}_page", 0) - 1
        if target < 0:
            return
        self._goto_page_cached(key, target)

    def _on_main_tab_changed(self, index: int):
        """切到某个主页签时，若该资源页从未加载过，自动发一次空查询（热门项目）。

        - 页签索引 2 = Mod资源（内含 光影 子页签，见 _build_resource_tab）
        - 页签索引 3 = 整合包
        空查询 = Modrinth 返回当前最热门项目（index=relevance），渲染进列表，
        让玩家打开页面就知道能搜什么、有哪些热门资源。每个页签只自动加载一次，
        玩家手动搜索后该标志置位，切走再切回不再重复发请求。
        """
        if index == 2 and not self._mod_auto_loaded:
            self._mod_auto_loaded = True
            # 若玩家还没输入任何关键字，走「空查询=热门」
            if not self.mod_search_edit.text().strip():
                self._search_mods(0)
        elif index == 3 and not self._modpack_auto_loaded:
            self._modpack_auto_loaded = True
            if not self.modpack_search_edit.text().strip():
                self._search_modpacks(0)

    def _on_res_subtab_changed(self, index: int):
        """Mod资源页签内的子页签切换：

        - index=2「光影」/ index=3「资源包」：各自只自动热门加载一次（首次进入时）。
        - 若玩家已经输入了关键字就不再自动发请求，避免冲掉他的搜索意图。
        """
        if index == 2 and not self._shader_auto_loaded:
            self._shader_auto_loaded = True
            if not self.shader_search_edit.text().strip():
                self._search_shaders(0)
        elif index == 3 and not self._respack_auto_loaded:
            self._respack_auto_loaded = True
            if not self.respack_search_edit.text().strip():
                self._search_resourcepacks(0)

    def _resolve_mod_bg(self, hit, pid, ver, loader):
        try:
            # 用已解析好的 pid（而不是 hit["project_id"]，避免 KeyError / None）
            vinfo = core.modrinth_sdk.resolve_version_sdk(pid, ver, loader)
        except Exception as exc:
            self.bridge.log.emit(f"[解析版本] {pid} 失败: {exc!r}")
            vinfo = None
        self.bridge.modResolved.emit(pid, vinfo)

    @pyqtSlot(str, object)
    def on_mod_resolved(self, pid: str, vinfo):
        row = self._mod_rows.get(pid)
        if row is None or row >= self.mod_table.rowCount():
            return
        item = self.mod_table.item(row, 0)
        if item is None or item.data(Qt.ItemDataRole.UserRole) != pid:
            return
        self._mod_versions[pid] = vinfo
        if vinfo:
            self.mod_table.setItem(row, 2, self._cell(
                ", ".join(vinfo.get("loaders") or [])[:16] or "-"))
            self.mod_table.setItem(row, 3, self._cell(
                ", ".join((vinfo.get("game_versions") or [])[-2:])[:16] or "-"))

    def _selected_mod_pid(self):
        row = self._selected_row(self.mod_table)
        if row < 0:
            return None
        # 优先读列 0，若为空（理论上不会，渲染时每列都存了 pid）则扫其它列兜底
        for col in range(self.mod_table.columnCount()):
            item = self.mod_table.item(row, col)
            if item is None:
                continue
            pid = item.data(Qt.ItemDataRole.UserRole)
            if pid:
                return str(pid)
        return None

    def _dep_display(self, d: dict, hits: dict = None) -> str:
        """把前置依赖渲染成玩家能看懂的文本，绝不裸甩 project_id。

        解析顺序：
          1. 会话内名称缓存 self._dep_names（之前查过就复用）
          2. 缓存的搜索结果 hits（刚搜过的项目里有 name/slug，零网络）
          3. 兜底：[未知名前置] (ID)
        后台下载前置前会对「未知」的 ID 触发一次 API 查询（_lookup_dep_name_bg），
        查到后写回缓存，下次再选中同名 mod 直接命中。
        """
        dep_pid = str(d.get("project_id") or "").strip()
        if not dep_pid:
            return "[无效依赖]"
        if dep_pid in self._dep_names:
            return self._dep_names[dep_pid]
        if hits is None:
            hits = self._mod_hits
        h = hits.get(dep_pid) or {}
        name = h.get("name") or h.get("title") or ""
        slug = h.get("slug") or ""
        if name:
            label = f"{name} ({slug})" if slug else name
            self._dep_names[dep_pid] = label
            return label
        # 还没缓存到：后台查一次（不阻塞 UI），当前先显示未知名占位
        if not any(k == dep_pid for k in getattr(self, "_dep_name_pending", set())):
            if not hasattr(self, "_dep_name_pending"):
                self._dep_name_pending = set()
            self._dep_name_pending.add(dep_pid)
            run_bg(self._lookup_dep_name_bg, dep_pid)
        return f"[未知名前置] ({dep_pid})"

    def _lookup_dep_name_bg(self, dep_pid: str):
        """后台按 project_id 查 Modrinth 名称，写回缓存；失败保持占位。"""
        try:
            r = core.Net.get_json(f"https://api.modrinth.com/v2/project/{dep_pid}",
                                  timeout=15)
            name = (r.get("name") or "").strip()
            slug = (r.get("slug") or "").strip()
            if name:
                label = f"{name} ({slug})" if slug else name
                self._dep_names[dep_pid] = label
                self.bridge.log.emit(f"[前置] {dep_pid} -> {label}")
        except Exception as exc:
            self.bridge.log.emit(f"[前置] 查名失败 {dep_pid}: {exc}")
        finally:
            try:
                self._dep_name_pending.discard(dep_pid)
            except Exception:
                pass

    def _on_mod_selected(self):
        pid = self._selected_mod_pid()
        if not pid:
            return
        hit = self._mod_hits.get(pid, {})
        lines = []
        desc = hit.get("desc") or ""
        if desc:
            lines.append(f"【简介】{desc}")
        vinfo = self._mod_versions.get(pid)
        if vinfo:
            lines.append(f"【版本】{vinfo.get('version_number', '?')} "
                         f"({','.join(vinfo.get('game_versions') or [])} / "
                         f"{','.join(vinfo.get('loaders') or [])})")
            deps = core.modrinth_sdk.resolve_required_deps(vinfo) if core.modrinth_sdk else []
            if deps:
                lines.append("【前置依赖】")
                lines.extend(f"  • {self._dep_display(d, self._mod_hits)}" for d in deps)
            else:
                lines.append("【前置依赖】无")
        else:
            lines.append("（选中后自动解析版本与前置依赖）")
        self.mod_info.setPlainText("\n".join(lines))

    def _download_selected_mod(self):
        # 允许多个 Mod 同时下载：去掉全局 busy 拦截。
        # 同一项目绝不重复下（并发写同一 .part/.jar 才是真正撞车的原因）：
        pid = self._selected_mod_pid()
        if not pid:
            QMessageBox.warning(self, "提示", "请先在列表中选中一个 Mod")
            return
        if pid in getattr(self, "_mod_dl_inflight", set()):
            QMessageBox.information(self, "提示", f"「{self._mod_hits.get(pid, {}).get('name', pid)}」正在下载中，请勿重复点击")
            return
        hit = self._mod_hits.get(pid)
        if not hit:
            return
        if core.modrinth_sdk is None:
            QMessageBox.warning(self, "库未安装", "请先运行: pip install modrinth")
            return
        # 用 hit 里的 id/slug 兜底再确认一次，保证本体 ID 是准的
        pid = self._hit_project_id(hit) or pid
        # 下载目标：强制使用资源页当前选中的实例（如 测试3），
        # 绝不按 MC 版本+加载器去匹配别的已装实例（会把 mod 塞进
        # versions/1.20.1-Forge_xxx/mods 而不是 versions/测试3/mods）。
        # 上方的 MC 版本/加载器下拉框只用于搜索过滤，不决定下载位置。
        loader = self._selected_loader_val(self.mod_loader_combo)
        ver_id = self._selected_res_version()
        if not ver_id:
            QMessageBox.warning(self, "提示",
                                "请先在资源页顶部选择目标实例。")
            return
        # 登记在途任务（同一 pid 不重复下载）；允许多个不同 mod 并发并行下载，
        # 绝不检查游戏是否在运行——玩家一边玩游戏一边下 mod 是合法操作。
        if not hasattr(self, "_mod_dl_inflight"):
            self._mod_dl_inflight = set()
        if not hasattr(self, "_mod_dl_active"):
            self._mod_dl_active = 0
        self._mod_dl_inflight.add(pid)
        self._mod_dl_active += 1
        self._begin_download()
        self._save_settings()
        core.apply_paths(self.cfg)
        run_bg(self._download_mod_bg, hit, pid, ver_id, loader,
               self._mod_versions.get(pid))

    def _download_mod_bg(self, hit, pid, ver_id, loader, cached_vinfo):
        # 本体：标题 + 精确 project_id（用 _hit_project_id 兜底，杜绝 None）
        title = hit.get("name") or hit.get("title") or "mod"
        pid = self._hit_project_id(hit) or pid

        def log(m):
            self._log(m)          # 直接进 LOG_BUFFER + 日志窗口，不抢主进度条

        # 并发下载时主进度条不再被每个任务抢占：
        # 进度只写日志（带 mod 名），主进度条留给「当前最前」任务或保持静止。
        def prog(cur, tot):
            if tot and tot > 0:
                pct = int(cur * 100 / tot)
                log(f"[{pct:>3}%] 下载 {title}  {cur} / {tot}")
            else:
                log(f"下载 {title} ...")

        try:
            log(f"解析 {title} 的版本与前置依赖...")
            # 优先用下拉框的纯 MC 版本；没有则回退到目标实例的真实 MC 版本
            ver = self._selected_mc_ver(self.mod_mc_combo)
            if not ver and ver_id:
                ver = self._instance_game_version(ver_id) or ""
            vinfo = cached_vinfo
            if not vinfo:
                vinfo = core.modrinth_sdk.resolve_version_sdk(pid, ver or None, loader or None)
            if not vinfo:
                log(f"未能为 {title} 找到可用版本")
                return
            url, fname = core.modrinth_sdk.version_download(vinfo)
            if not url:
                log(f"{title} 没有可下载文件")
                return
            target = core._ensure_resource_dir(ver_id, "mods")
            deps = core.modrinth_sdk.resolve_required_deps(vinfo)
            log(f"使用版本: {vinfo.get('version_number', '?')}（{len(deps)} 个前置）")
            # 先下依赖（用依赖自己的 project_id），再下本体，ID 绝不混用
            for dep in deps:
                self._download_one_dep_sdk(dep, target, log, prog, ver or None, loader or None)
            log(f"下载本体 {title} (id={pid}) ...")
            core.modrinth_sdk.download_file(url, Path(target) / (fname or f"{pid}.jar"), prog)
            log(f"完成: {title} 已装到 {target}")
        except Exception as exc:
            log(f"下载失败: {exc}")
            self.bridge.error.emit("下载失败", str(exc))
        finally:
            # 移除在途登记；最后一个 mod 任务结束时才解除下载锁
            if hasattr(self, "_mod_dl_inflight"):
                self._mod_dl_inflight.discard(pid)
            if hasattr(self, "_mod_dl_active"):
                self._mod_dl_active -= 1
            self._end_download()

    def _download_one_dep_sdk(self, dep, target, log, prog, ver, loader):
        dep_pid = dep.get("project_id")
        if not dep_pid:
            return
        try:
            dep_ver = core.modrinth_sdk.resolve_version_sdk(dep_pid, ver, loader)
            if not dep_ver:
                log(f"前置 {dep_pid} 无可用版本，跳过")
                return
            url, fname = core.modrinth_sdk.version_download(dep_ver)
            if url:
                core.modrinth_sdk.download_file(url, Path(target) / (fname or "dep.jar"), prog)
                log(f"  前置已下: {dep_pid}")
        except Exception as exc:
            log(f"前置 {dep_pid} 下载失败: {exc}")

    # ==================================================================
    # 光影
    # ==================================================================
    def _search_shaders(self, page: int = 0):
        """搜索光影（统一入口：手动 / 回车 / 切页签自动热门加载）。

        - query 允许为空字符串：空查询 = 向 Modrinth 请求当前热门光影（index=relevance）。
        - page>0 视为翻页：先查按页缓存 self._shader_cached_pages，命中直接渲染（零网络），
          未命中才发 API，回来存进缓存再渲染。
        """
        loader = self._selected_loader_val(self.shader_loader_combo)
        ver = self._selected_mc_ver(self.shader_mc_combo) or None
        query = self.shader_search_edit.text().strip()

        if page == 0:
            # 第 0 页：新搜索，清空按页缓存与上下文，重新拿最新结果
            self._reset_search_context("shader", query, ver, loader)

        if page > 0:
            self._goto_page_cached("shader", page)
            return

        q_en = self._shader_query_en

        def work():
            try:
                hits = core.modrinth_search(q_en, project_type="shader",
                                             loader=loader, game_version=ver,
                                             limit=self.PAGE_SIZE, offset=0,
                                             index="relevance")
                self.bridge.shaderHits.emit(hits, 0)
            except Exception as exc:
                self.bridge.log.emit(f"搜索失败: {exc!r}")
                self.bridge.status.emit("搜索失败")

        self._log(f"搜索光影: {query or '（空查询=热门）'} (loader={loader or 'all'}, mc={ver or 'all'}, 第1页)")
        self.bridge.status.emit(f"搜索光影: {query or '热门'} ...")
        run_bg(work)

    @pyqtSlot(list, int)
    def on_shader_hits(self, hits: list, page: int):
        """收到某一页的光影结果：先存进按页缓存，再整页渲染。"""
        self._shader_cached_pages[page] = hits
        self._render_shader_page(page)

    def _render_shader_page(self, page: int):
        """把第 page 页（取自按页缓存）渲染到 shader_table（整页替换，不累积）。"""
        hits = self._shader_cached_pages.get(page, [])
        ver = self._shader_ver
        loader = self._shader_loader

        self.shader_table.setRowCount(0)
        self._shader_rows.clear()

        rows = []
        for h in hits:
            pid = self._hit_project_id(h)
            if not pid:
                continue
            rows.append({
                "pid": pid,
                "title": h.get("title") or h.get("name") or "",
                "loader": ", ".join(h.get("categories") or h.get("loaders") or [])[:16] or "-",
                "ver": ", ".join((h.get("versions") or h.get("game_versions") or [])[-2:])[:16] or "-",
                "icon": h.get("icon_url"),
            })
            self._shader_hits[pid] = h
            if pid not in self._shader_versions:
                run_bg(self._resolve_shader_bg, h, pid, ver, loader)

        self._append_project_rows(self.shader_table, rows)
        self._shader_page = page
        self._shader_exhausted = (len(hits) < self.PAGE_SIZE)
        self._update_pager("shader")
        self.bridge.status.emit(f"光影第{page + 1}页（{len(rows)} 项）")

    def _pick_project_version(self, pid, ver, loader):
        """拉取项目版本列表，挑一个匹配 游戏版本+加载器 的最新版本。"""
        if not pid:
            return None
        versions = core.modrinth_project_versions(pid)
        if not versions:
            return None

        def ok(v):
            if ver and ver not in (v.get("game_versions") or []):
                return False
            if loader and loader != "all" and loader not in (v.get("loaders") or []):
                return False
            return True

        cands = [v for v in versions if ok(v)] or versions
        cands.sort(key=lambda v: v.get("date_published") or "", reverse=True)
        return cands[0]

    def _resolve_shader_bg(self, hit, pid, ver, loader):
        try:
            vinfo = self._pick_project_version(pid, ver, loader)
        except Exception:
            vinfo = None
        self.bridge.shaderResolved.emit(pid, vinfo)

    @pyqtSlot(str, object)
    def on_shader_resolved(self, pid: str, vinfo):
        row = self._shader_rows.get(pid)
        if row is None or row >= self.shader_table.rowCount():
            return
        item = self.shader_table.item(row, 0)
        if item is None or item.data(Qt.ItemDataRole.UserRole) != pid:
            return
        self._shader_versions[pid] = vinfo
        if vinfo:
            self.shader_table.setItem(row, 2, self._cell(
                ", ".join(vinfo.get("loaders") or [])[:16] or "-"))
            self.shader_table.setItem(row, 3, self._cell(
                ", ".join((vinfo.get("game_versions") or [])[-2:])[:16] or "-"))

    def _selected_shader_pid(self):
        row = self._selected_row(self.shader_table)
        if row < 0:
            return None
        item = self.shader_table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_shader_selected(self):
        pid = self._selected_shader_pid()
        if not pid:
            return
        hit = self._shader_hits.get(pid, {})
        lines = []
        desc = hit.get("description") or ""
        if desc:
            lines.append(f"【简介】{desc}")
        vinfo = self._shader_versions.get(pid)
        if vinfo:
            lines.append(f"【版本】{vinfo.get('version_number', '?')} "
                         f"({','.join(vinfo.get('game_versions') or [])} / "
                         f"{','.join(vinfo.get('loaders') or [])})")
            deps = core.modrinth_resolve_dependencies(vinfo, "shader")
            if deps:
                lines.append("【前置依赖】")
                lines.extend(f"  • {self._dep_display(d, self._shader_hits)}" for d in deps)
            else:
                lines.append("【前置依赖】无")
        else:
            lines.append("（选中后自动解析版本与前置依赖）")
        self.shader_info.setPlainText("\n".join(lines))

    def _download_selected_shader(self):
        # 允许多任务并发下载（去掉全局 busy 拦截），改用下载专用锁
        pid = self._selected_shader_pid()
        if not pid:
            QMessageBox.warning(self, "提示", "请先在列表中选中一个光影")
            return
        hit = self._shader_hits.get(pid)
        if not hit:
            return
        # 下载目标：强制使用资源页当前选中的实例（同 Mod 逻辑）
        loader = self._selected_loader_val(self.shader_loader_combo)
        ver_id = self._selected_res_version()
        if not ver_id:
            QMessageBox.warning(self, "提示",
                                "请先在资源页顶部选择目标实例。")
            return
        # 允许多任务并发下载（去掉全局 busy 拦截），改用下载专用锁
        self._begin_download()
        self._save_settings()
        core.apply_paths(self.cfg)
        run_bg(self._download_shader_bg, hit, ver_id, loader, self._shader_versions.get(pid))

    def _download_shader_bg(self, hit, ver_id, loader, cached_vinfo):
        title = hit.get("title") or "shader"
        pid = hit.get("project_id")

        def log(m):
            self._log(m)   # 进 LOG_BUFFER + 日志窗口，不抢主进度条

        def prog(cur, tot):
            pct = int(cur * 100 / tot) if tot and tot > 0 else 0
            log(f"[{pct:>3}%] 下载光影 {title}  {cur} / {tot}")

        try:
            log(f"解析 {title} 的版本与前置依赖...")
            ver = self._selected_mc_ver(self.shader_mc_combo)
            if not ver and ver_id:
                ver = self._instance_game_version(ver_id) or ""
            vinfo = cached_vinfo or self._pick_project_version(pid, ver or None, loader or None)
            if not vinfo:
                log(f"未能为 {title} 找到可用版本")
                return
            target = core._ensure_resource_dir(ver_id, "shaderpacks")
            deps = core.modrinth_resolve_dependencies(vinfo, "shader")
            log(f"使用版本: {vinfo.get('version_number', '?')}（{len(deps)} 个前置）")
            for dep in deps:
                self._download_one_shader_dep(dep, target, log, prog, ver, loader)
            log(f"下载 {title} ...")
            core.modrinth_download_file(vinfo, target, progress_cb=prog, log_cb=log)
            log(f"完成: {title} 已装到 {target}")
            self.bridge.status.emit(f"{title} 及前置下载完成")
        except Exception as exc:
            log(f"下载失败: {exc}")
            self.bridge.error.emit("下载失败", str(exc))
        finally:
            self._end_download()

    def _download_one_shader_dep(self, dep, target, log, prog, ver, loader):
        dep_pid = dep.get("project_id")
        if not dep_pid:
            return
        try:
            dep_ver = None
            if dep.get("version_id"):
                try:
                    dep_ver = core.modrinth_version(dep["version_id"])
                except Exception:
                    dep_ver = None
            if not dep_ver:
                dep_ver = self._pick_project_version(dep_pid, ver, loader)
            if not dep_ver:
                log(f"前置 {dep_pid} 无可用版本，跳过")
                return
            core.modrinth_download_file(dep_ver, target, progress_cb=prog, log_cb=log)
            log(f"  前置已下: {dep_pid}")
        except Exception as exc:
            log(f"前置 {dep_pid} 下载失败: {exc}")

    # ==================================================================
    # 资源包（resourcepack）—— 逻辑照抄光影，无前置依赖，目录 resourcepacks
    # ==================================================================
    def _search_resourcepacks(self, page: int = 0):
        """搜索资源包（统一入口：手动 / 回车 / 切页签自动热门加载）。

        - query 允许为空字符串：空查询 = 向 Modrinth 请求当前热门资源包（index=relevance）。
        - page>0 视为翻页：先查按页缓存 self._respack_cached_pages，命中直接渲染（零网络），
          未命中才发 API，回来存进缓存再渲染。
        """
        loader = self._selected_loader_val(self.respack_loader_combo)
        ver = self._selected_mc_ver(self.respack_mc_combo) or None
        query = self.respack_search_edit.text().strip()

        if page == 0:
            # 第 0 页：新搜索，清空按页缓存与上下文，重新拿最新结果
            self._reset_search_context("respack", query, ver, loader)

        if page > 0:
            self._goto_page_cached("respack", page)
            return

        q_en = self._respack_query_en

        def work():
            try:
                hits = core.modrinth_search(q_en, project_type="resourcepack",
                                             loader=loader, game_version=ver,
                                             limit=self.PAGE_SIZE, offset=0,
                                             index="relevance")
                self.bridge.respackHits.emit(hits, 0)
            except Exception as exc:
                self.bridge.log.emit(f"搜索失败: {exc!r}")
                self.bridge.status.emit("搜索失败")

        self._log(f"搜索资源包: {query or '（空查询=热门）'} (loader={loader or 'all'}, mc={ver or 'all'}, 第1页)")
        self.bridge.status.emit(f"搜索资源包: {query or '热门'} ...")
        run_bg(work)

    @pyqtSlot(list, int)
    def on_respack_hits(self, hits: list, page: int):
        """收到某一页的资源包结果：先存进按页缓存，再整页渲染。"""
        self._respack_cached_pages[page] = hits
        self._render_respack_page(page)

    def _render_respack_page(self, page: int):
        """把第 page 页（取自按页缓存）渲染到 respack_table（整页替换，不累积）。"""
        hits = self._respack_cached_pages.get(page, [])
        ver = self._respack_ver
        loader = self._respack_loader

        self.respack_table.setRowCount(0)
        self._respack_rows.clear()

        rows = []
        for h in hits:
            pid = self._hit_project_id(h)
            if not pid:
                continue
            rows.append({
                "pid": pid,
                "title": h.get("title") or h.get("name") or "",
                "loader": ", ".join(h.get("categories") or h.get("loaders") or [])[:16] or "-",
                "ver": ", ".join((h.get("versions") or h.get("game_versions") or [])[-2:])[:16] or "-",
                "icon": h.get("icon_url"),
            })
            self._respack_hits[pid] = h
            if pid not in self._respack_versions:
                run_bg(self._resolve_respack_bg, h, pid, ver, loader)

        self._append_project_rows(self.respack_table, rows)
        self._respack_page = page
        self._respack_exhausted = (len(hits) < self.PAGE_SIZE)
        self._update_pager("respack")
        self.bridge.status.emit(f"资源包第{page + 1}页（{len(rows)} 项）")

    def _resolve_respack_bg(self, hit, pid, ver, loader):
        try:
            vinfo = self._pick_project_version(pid, ver, loader)
        except Exception:
            vinfo = None
        self.bridge.respackResolved.emit(pid, vinfo)

    @pyqtSlot(str, object)
    def on_respack_resolved(self, pid: str, vinfo):
        row = self._respack_rows.get(pid)
        if row is None or row >= self.respack_table.rowCount():
            return
        item = self.respack_table.item(row, 0)
        if item is None or item.data(Qt.ItemDataRole.UserRole) != pid:
            return
        self._respack_versions[pid] = vinfo
        if vinfo:
            self.respack_table.setItem(row, 2, self._cell(
                ", ".join(vinfo.get("loaders") or [])[:16] or "-"))
            self.respack_table.setItem(row, 3, self._cell(
                ", ".join((vinfo.get("game_versions") or [])[-2:])[:16] or "-"))

    def _selected_respack_pid(self):
        row = self._selected_row(self.respack_table)
        if row < 0:
            return None
        item = self.respack_table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_respack_selected(self):
        pid = self._selected_respack_pid()
        if not pid:
            return
        hit = self._respack_hits.get(pid, {})
        lines = []
        desc = hit.get("description") or ""
        if desc:
            lines.append(f"【简介】{desc}")
        vinfo = self._respack_versions.get(pid)
        if vinfo:
            lines.append(f"【版本】{vinfo.get('version_number', '?')} "
                         f"({','.join(vinfo.get('game_versions') or [])} / "
                         f"{','.join(vinfo.get('loaders') or [])})")
            # 资源包无前置依赖
            lines.append("【前置依赖】无（资源包不需要前置）")
        else:
            lines.append("（选中后自动解析版本）")
        self.respack_info.setPlainText("\n".join(lines))

    def _download_selected_respack(self):
        pid = self._selected_respack_pid()
        if not pid:
            QMessageBox.warning(self, "提示", "请先在列表中选中一个资源包")
            return
        hit = self._respack_hits.get(pid)
        if not hit:
            return
        loader = self._selected_loader_val(self.respack_loader_combo)
        ver_id = self._selected_res_version()
        if not ver_id:
            QMessageBox.warning(self, "提示",
                                "请先在资源页顶部选择目标实例。")
            return
        self._begin_download()
        self._save_settings()
        core.apply_paths(self.cfg)
        run_bg(self._download_respack_bg, hit, ver_id, loader, self._respack_versions.get(pid))

    def _download_respack_bg(self, hit, ver_id, loader, cached_vinfo):
        title = hit.get("title") or "resourcepack"
        pid = hit.get("project_id")

        def log(m):
            self._log(m)

        def prog(cur, tot):
            pct = int(cur * 100 / tot) if tot and tot > 0 else 0
            log(f"[{pct:>3}%] 下载资源包 {title}  {cur} / {tot}")

        try:
            log(f"解析 {title} 的版本...")
            ver = self._selected_mc_ver(self.respack_mc_combo)
            if not ver and ver_id:
                ver = self._instance_game_version(ver_id) or ""
            vinfo = cached_vinfo or self._pick_project_version(pid, ver or None, loader or None)
            if not vinfo:
                log(f"未能为 {title} 找到可用版本")
                return
            # 资源包专用目录：resourcepacks
            target = core._ensure_resource_dir(ver_id, "resourcepacks")
            log(f"使用版本: {vinfo.get('version_number', '?')}")
            log(f"下载 {title} ...")
            core.modrinth_download_file(vinfo, target, progress_cb=prog, log_cb=log)
            log(f"完成: {title} 已装到 {target}")
            self.bridge.status.emit(f"{title} 下载完成")
        except Exception as exc:
            log(f"下载失败: {exc}")
            self.bridge.error.emit("下载失败", str(exc))
        finally:
            self._end_download()

    # ==================================================================
    # 整合包
    # ==================================================================
    def _search_modpacks(self, page: int = 0):
        """搜索整合包（统一入口：手动 / 回车 / 切页签自动热门加载）。

        - query 允许为空字符串：空查询 = 向 Modrinth 请求当前热门整合包（index=relevance）。
        - page>0 视为翻页：先查按页缓存 self._modpack_cached_pages，命中直接渲染（零网络），
          未命中才发 API，回来存进缓存再渲染。
        """
        loader = self._selected_loader_val(self.modpack_loader_combo)
        ver = self._selected_mc_ver(self.modpack_mc_combo) or None
        query = self.modpack_search_edit.text().strip()

        if page == 0:
            # 第 0 页：新搜索，清空按页缓存与上下文，重新拿最新结果
            self._reset_search_context("modpack", query, ver, loader)

        if page > 0:
            self._goto_page_cached("modpack", page)
            return

        q_en = self._modpack_query_en

        def work():
            try:
                hits = core.modrinth_search(q_en, project_type="modpack",
                                             loader=loader, game_version=ver,
                                             limit=self.PAGE_SIZE, offset=0,
                                             index="relevance")
                self.bridge.modpackHits.emit(hits, 0)
            except Exception as exc:
                self.bridge.log.emit(f"搜索失败: {exc!r}")
                self.bridge.status.emit("搜索失败")

        self._log(f"搜索整合包: {query or '（空查询=热门）'} (loader={loader or 'all'}, mc={ver or 'all'}, 第1页)")
        self.bridge.status.emit(f"搜索整合包: {query or '热门'} ...")
        run_bg(work)

    @pyqtSlot(list, int)
    def on_modpack_hits(self, hits: list, page: int):
        """收到某一页的整合包结果：先存进按页缓存，再整页渲染。"""
        self._modpack_cached_pages[page] = hits
        self._render_modpack_page(page)

    def _render_modpack_page(self, page: int):
        """把第 page 页（取自按页缓存）渲染到 modpack_table（整页替换，不累积）。"""
        hits = self._modpack_cached_pages.get(page, [])
        ver = self._modpack_ver
        loader = self._modpack_loader

        self.modpack_table.setRowCount(0)
        self._modpack_rows.clear()

        rows = []
        for h in hits:
            pid = self._hit_project_id(h)
            if not pid:
                continue
            rows.append({
                "pid": pid,
                "title": h.get("title") or h.get("name") or "",
                "loader": ", ".join(h.get("categories") or h.get("loaders") or [])[:16] or "-",
                "ver": ", ".join((h.get("versions") or h.get("game_versions") or [])[-2:])[:16] or "-",
                "icon": h.get("icon_url"),
            })
            self._modpack_hits[pid] = h
            if pid not in self._modpack_versions:
                run_bg(self._resolve_modpack_bg, h, pid, ver, loader)

        self._append_project_rows(self.modpack_table, rows)
        self._modpack_page = page
        self._modpack_exhausted = (len(hits) < self.PAGE_SIZE)
        self._update_pager("modpack")
        self.bridge.status.emit(f"整合包第{page + 1}页（{len(rows)} 项）")


    def _resolve_modpack_bg(self, hit, pid, ver, loader):
        try:
            vinfo = self._pick_project_version(pid, ver, loader)
        except Exception:
            vinfo = None
        self.bridge.modpackResolved.emit(pid, vinfo)

    @pyqtSlot(str, object)
    def on_modpack_resolved(self, pid: str, vinfo):
        row = self._modpack_rows.get(pid)
        if row is None or row >= self.modpack_table.rowCount():
            return
        item = self.modpack_table.item(row, 0)
        if item is None or item.data(Qt.ItemDataRole.UserRole) != pid:
            return
        self._modpack_versions[pid] = vinfo
        if vinfo:
            self.modpack_table.setItem(row, 2, self._cell(
                ", ".join(vinfo.get("loaders") or [])[:16] or "-"))
            self.modpack_table.setItem(row, 3, self._cell(
                ", ".join((vinfo.get("game_versions") or [])[-2:])[:16] or "-"))

    def _selected_modpack_pid(self):
        row = self._selected_row(self.modpack_table)
        if row < 0:
            return None
        item = self.modpack_table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_modpack_selected(self):
        pid = self._selected_modpack_pid()
        if not pid:
            return
        hit = self._modpack_hits.get(pid, {})
        lines = []
        desc = hit.get("description") or ""
        if desc:
            lines.append(f"【简介】{desc}")
        vinfo = self._modpack_versions.get(pid)
        if vinfo:
            lines.append(f"【版本】{vinfo.get('version_number', '?')} "
                         f"({','.join(vinfo.get('game_versions') or [])} / "
                         f"{','.join(vinfo.get('loaders') or [])})")
            lines.append("【说明】.mrpack 内含全部 mod 文件清单，"
                         "下载后可在其他整合包工具中导入安装。")
        else:
            lines.append("（选中后自动解析版本）")
        self.modpack_info.setPlainText("\n".join(lines))

    def _download_selected_modpack(self):
        # 整合包不能塞进「当前版本」，必须创建全新独立实例。
        # 先解析目标 MC 版本/加载器 -> 弹窗确认实例名 -> 检查重名 -> 后台装。
        # 允许多任务并发下载（去掉全局 busy 拦截）
        pid = self._selected_modpack_pid()
        if not pid:
            QMessageBox.warning(self, "提示", "请先在列表中选中一个整合包")
            return
        hit = self._modpack_hits.get(pid)
        if not hit:
            return
        loader = self._selected_loader_val(self.modpack_loader_combo)
        # 目标 MC 版本：优先搜索下拉框选中的纯 MC 版本；否则尝试从整合包 vinfo 提取
        ver = self._selected_mc_ver(self.modpack_mc_combo)
        if not ver:
            vinfo = self._modpack_versions.get(pid)
            gv = (vinfo or {}).get("game_versions") or []
            ver = gv[-1] if gv else ""
        if not ver:
            QMessageBox.warning(self, "提示", "无法确定整合包目标 MC 版本，请在上方的「目标 MC 版本」下拉框里选一个。")
            return
        # 整合包名（默认实例名）
        title = (hit.get("name") or hit.get("title") or "整合包").strip()
        # 非法实例名字符
        if re.search(r'[\\/:*?"<>|]', title):
            QMessageBox.warning(self, "实例名无效", f"整合包名「{title}」含非法字符，请在下方输入框里改个名字。")
            return

        # 弹窗：确认新实例名（默认用整合包名，可改）
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(
            self, "创建新实例",
            f"将为整合包「{title}」(MC {ver}) 创建一个全新实例并安装，\n"
            f"请输入新实例的名字（将放到 versions/ 目录下）：",
            text=title)
        if not ok or not (name or "").strip():
            return
        name = name.strip()
        if re.search(r'[\\/:*?"<>|]', name):
            QMessageBox.warning(self, "实例名无效", "实例名不能包含 \\ / : * ? \" < > |")
            return
        # 检查重名（已装列表 或 磁盘上已有目录）
        if name in self.installed or (core.VERSIONS_DIR / name).is_dir():
            QMessageBox.warning(self, "实例名已存在",
                                f"versions/ 下已存在「{name}」，请换一个名字。")
            return
        # 校验通过，同步置下载锁
        self._begin_download()
        self._save_settings()
        core.apply_paths(self.cfg)
        run_bg(self._download_modpack_bg, hit, name, ver, loader,
               self._modpack_versions.get(pid))

    # ==================================================================
    # [FIX-截断恢复] 本地 .mrpack 整合包导入（不联网下载整合包本身）
    # ==================================================================
    def _import_local_mrpack(self):
        """[主线程] 选择本机 .mrpack 文件，解析依赖后后台安装到全新实例。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择整合包文件", "",
            "Modrinth 整合包 (*.mrpack);;所有文件 (*)")
        if not path:
            return
        mrpack_file = Path(path)
        if not mrpack_file.is_file():
            QMessageBox.warning(self, "提示", f"文件不存在: {mrpack_file.name}")
            return
        # 主线程先快速解析（本地 zip，毫秒级），失败立即报给用户
        try:
            import mrpack_installer
            index = mrpack_installer.parse_mrpack(mrpack_file)
        except Exception as exc:
            QMessageBox.warning(self, "导入失败",
                                f"解析 {mrpack_file.name} 失败：\n{exc}")
            return
        dep_mc = mrpack_installer.get_minecraft_version(index) or ""
        dep_loaders = mrpack_installer.get_mod_loaders(index) or []
        title = (index.get("name") or mrpack_file.stem.replace(".mrpack", "")
                 or "本地整合包").strip()
        # 校验非法实例名字符
        if re.search(r'[\\/:*?"<>|]', title):
            QMessageBox.warning(self, "实例名无效",
                                f"整合包名「{title}」含非法字符，请重命名文件后重试。")
            return
        # 弹窗：确认新实例名（默认用整合包名，可改）
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(
            self, "导入本地整合包",
            f"将把「{title}」(MC {dep_mc or '未知版本'}) 安装到全新实例，\n"
            f"请输入新实例的名字（将放到 versions/ 目录下）：",
            text=title)
        if not ok or not (name or "").strip():
            return
        name = name.strip()
        if re.search(r'[\\/:*?"<>|]', name):
            QMessageBox.warning(self, "实例名无效",
                                 "实例名不能包含 \\ / : * ? \" < > |")
            return
        if name in self.installed or (core.VERSIONS_DIR / name).is_dir():
            QMessageBox.warning(self, "实例名已存在",
                                 f"versions/ 下已存在「{name}」，请换一个名字。")
            return
        self._log(f"导入本地整合包: {mrpack_file.name} "
                  f"(MC {dep_mc or '?'} / 加载器 {dep_loaders or '无'})")
        self._begin_download()
        self._save_settings()
        core.apply_paths(self.cfg)
        run_bg(self._install_local_mrpack_bg, mrpack_file, index,
               dep_mc, dep_loaders, name)

    def _install_local_mrpack_bg(self, mrpack_file: Path, index: dict,
                                 dep_mc: str, dep_loaders: list,
                                 inst_name: str):
        """[后台] 本地 .mrpack 安装：装原版本体 + 加载器 + 克隆实例 +
        下载包内 Mod + 解压 Overrides。复用现有网络下载流程的第 2-7 步。"""
        title = index.get("name") or mrpack_file.stem

        def log(m):
            self._log(m)

        def prog(cur, tot, note=None):
            self.bridge.progress.emit(cur, tot,
                                      f"本地整合包 {title}"
                                      + (f" ({note})" if note else ""))

        try:
            # 1) 安装纯原版本体（到 dep_mc 版本目录）
            log(f"安装原版 MC 本体 {dep_mc} ...")
            if core.MCL_OK:
                core.mcl_install_minecraft(dep_mc, core.MINECRAFT_DIR,
                                           core._make_mcl_callback(progress_cb=prog))
            else:
                raise RuntimeError(
                    "无法获取下载地址，且 minecraft_launcher_lib 不可用")
            log(f"本体 {dep_mc} 安装完成")

            # 2) 安装加载器（从整合包 index 解析，如 "neoforge-21.1.52"）
            src_id = dep_mc
            if dep_loaders:
                loader_spec = dep_loaders[0]
                lkind, lver = (loader_spec.split("-", 1)
                               if "-" in loader_spec else (loader_spec, ""))
                lkind = lkind.lower()
                if lkind.startswith("neo"):
                    lkind = "neoforge"
                elif lkind.startswith("forge"):
                    lkind = "forge"
                elif lkind.startswith("fabric"):
                    lkind = "fabric"
                else:
                    lkind = ""  # 兜底：未知加载器跳过
                log(f"安装 {lkind} 加载器 {lver} ...")
                src_id = install_mod_loader_captured(
                    lkind, dep_mc, core.MINECRAFT_DIR, lver,
                    self._best_java_for(dep_mc), log, progress_cb=prog)
                if lkind == "fabric":
                    core._download_fabric_api_mc(src_id, log, prog)
                log(f"{lkind} 加载器已装: {src_id}")

            # 3) 把 src_id 克隆成新实例名 inst_name（强制 json id = inst_name）
            if src_id != inst_name:
                log(f"克隆 {src_id} -> {inst_name} ...")
                if not _clone_version_instance(src_id, inst_name):
                    raise RuntimeError(f"无法创建实例 {inst_name}")
                self._force_instance_id(inst_name, inst_name, log)
                log(f"已创建实例: {inst_name}（源 {src_id}）")

            # 4) 下载整合包里的 Mod 到 versions/<inst_name>/mods/
            log("=== 下载整合包 Mod 文件 ===")
            mods_dir = core._ensure_resource_dir(inst_name, "mods")
            import mrpack_installer
            mrpack_installer.install_files(mrpack_file, core.MINECRAFT_DIR,
                                           inst_name, mods_dir=mods_dir,
                                           progress_cb=prog, log_cb=log)

            # 5) 解压 overrides 到 versions/<inst_name>/
            log("=== 解压整合包 Overrides ===")
            mrpack_installer.extract_overrides(mrpack_file, core.MINECRAFT_DIR,
                                                inst_name, log_cb=log,
                                                progress_cb=prog)

            log(f"本地整合包 {title} 导入完成，实例: {inst_name} (MC {dep_mc})")
            self.bridge.status.emit(
                f"本地整合包 {title} 导入完成 -> {inst_name}")
            run_bg(self._refresh_versions_bg)
        except Exception as exc:
            log(f"本地整合包导入失败: {exc}")
            self.bridge.error.emit("本地整合包导入失败", str(exc))
        finally:
            self.bridge.progressReset.emit()
            self._end_download()

    def _download_modpack_bg(self, hit, inst_name, mc_ver, loader, cached_vinfo):
        """整合包安装：创建独立新实例，装原版本体 + 加载器，
        再下载 .mrpack 里的 Mod 和 Overrides。
        """
        title = hit.get("name") or hit.get("title") or "整合包"
        pid = hit.get("project_id") or ""

        def log(m):
            self._log(m)

        def prog(cur, tot, note=None):
            self.bridge.progress.emit(cur, tot,
                                      f"整合包 {title}" + (f" ({note})" if note else ""))

        try:
            # 1) 下载整合包 .mrpack（拿到 mod 清单与 overrides 用）
            log(f"下载整合包 {title} ...")
            vinfo = cached_vinfo
            if not vinfo:
                vinfo = self._pick_project_version(pid, mc_ver, loader or None)
            if not vinfo:
                log(f"未能为 {title} 找到可用版本")
                return
            mrpack_dir = core._ensure_resource_dir("modpack_cache", "mrpacks")
            url, fname = core.modrinth_sdk.version_download(vinfo) if core.modrinth_sdk else (None, None)
            if not url:
                # 回退：用官方 modrinth_download_file
                log(f"下载 {title} (.mrpack) ...")
                _now = __import__("time").time()
                core.modrinth_download_file(vinfo, mrpack_dir, progress_cb=prog, log_cb=log)
                # 找到刚下载的 .mrpack（mtime 最新且晚于本次任务开始）
                import glob
                cands = [p for p in mrpack_dir.glob("*.mrpack") if p.stat().st_mtime >= _now]
                mrpack_file = max(cands, key=lambda p: p.stat().st_mtime, default=None)
                if not mrpack_file:
                    log("未找到下载好的 .mrpack 文件")
                    return
            else:
                # 直接用 modrinth_sdk 下载（走镜像加速）
                import modrinth_sdk
                mrpack_file = Path(mrpack_dir) / (fname or "modpack.mrpack")
                modrinth_sdk.download_file(url, mrpack_file, prog)

            log(f"已下载 .mrpack: {mrpack_file.name}")

            # 2) 解析整合包依赖
            import mrpack_installer
            index = mrpack_installer.parse_mrpack(mrpack_file)
            dep_mc = mrpack_installer.get_minecraft_version(index) or mc_ver
            dep_loaders = mrpack_installer.get_mod_loaders(index) or []
            log(f"整合包依赖：Minecraft={dep_mc}，加载器={dep_loaders or '无'}")

            # 3) 安装纯原版本体（到 inst_name，这样 versions/<inst_name>/ 下会有本体）
            log(f"安装原版 MC 本体 {dep_mc} ...")
            if core.MCL_OK:
                core.mcl_install_minecraft(dep_mc, core.MINECRAFT_DIR,
                                            core._make_mcl_callback(progress_cb=prog))
            else:
                raise RuntimeError("无法获取下载地址，且 minecraft_launcher_lib 不可用")
            log(f"本体 {dep_mc} 安装完成")

            # 4) 安装加载器（从整合包 index 里解析的 dep_loaders，如 "neoforge-21.1.52"）
            src_id = dep_mc
            if dep_loaders:
                # 取第一个加载器（通常整合包只有一个）
                loader_spec = dep_loaders[0]
                # 解析出 loader 类型（fabric/forge/neoforge）和版本
                lkind, lver = (loader_spec.split("-", 1) if "-" in loader_spec else (loader_spec, ""))
                # 规范化：neoforge -> "neoforge"
                lkind = lkind.lower()
                if lkind.startswith("neo"):
                    lkind = "neoforge"
                elif lkind.startswith("forge"):
                    lkind = "forge"
                elif lkind.startswith("fabric"):
                    lkind = "fabric"
                else:
                    lkind = loader or ""  # 兜底用 UI 选的
                log(f"安装 {lkind} 加载器 {lver} ...")
                src_id = install_mod_loader_captured(
                    lkind, dep_mc, core.MINECRAFT_DIR, lver,
                    self._best_java_for(dep_mc), log, progress_cb=prog)
                if lkind == "fabric":
                    core._download_fabric_api_mc(src_id, log, prog)
                log(f"{lkind} 加载器已装: {src_id}")

            # 5) 把 src_id 克隆成新实例名 inst_name（强制 json id = inst_name）
            if src_id != inst_name:
                log(f"克隆 {src_id} -> {inst_name} ...")
                if not _clone_version_instance(src_id, inst_name):
                    raise RuntimeError(f"无法创建实例 {inst_name}")
                self._force_instance_id(inst_name, inst_name, log)
                log(f"已创建实例: {inst_name}（源 {src_id}）")

            # 6) 下载整合包里的 Mod 到 versions/<inst_name>/mods/
            log("=== 下载整合包 Mod 文件 ===")
            mods_dir = core._ensure_resource_dir(inst_name, "mods")
            # 传 inst_name 作 mc_version 参数，让 config/kubejs 等已知目录
            # 也落到 versions/<inst_name>/ 下；mods_dir 显式指定 mods 目标
            mrpack_installer.install_files(mrpack_file, core.MINECRAFT_DIR,
                                           inst_name, mods_dir=mods_dir,
                                           progress_cb=prog, log_cb=log)

            # 7) 解压 overrides 到 versions/<inst_name>/
            log("=== 解压整合包 Overrides ===")
            mrpack_installer.extract_overrides(mrpack_file, core.MINECRAFT_DIR,
                                               inst_name, log_cb=log, progress_cb=prog)

            log(f"整合包 {title} 安装完成，实例: {inst_name} (MC {dep_mc})")
            self.bridge.status.emit(f"整合包 {title} 安装完成 -> {inst_name}")
            run_bg(self._refresh_versions_bg)
        except Exception as exc:
            log(f"整合包安装失败: {exc}")
            self.bridge.error.emit("整合包安装失败", str(exc))
        finally:
            self.bridge.progressReset.emit()
            self._end_download()

    # ==================================================================
    # 图标缓存（后台下载，信号回主线程）
    # ==================================================================
    def _icon_cache_dir(self) -> Path:
        d = Path(core.BASE_DIR) / "mod_icons"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _icon_path(self, url: str) -> Path:
        key = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(url.split("?")[0])) or "icon.png"
        return self._icon_cache_dir() / key

    def _wire_icon(self, table: QTableWidget, row: int, pid: str, url: str):
        path = self._icon_path(url)
        if path.is_file():
            self._set_icon(table, row, pid, path)
            return
        targets = self._icon_wait.setdefault(str(path), [])
        targets.append((table, row, pid))
        if str(path) in self._icon_jobs:
            return
        self._icon_jobs.add(str(path))
        run_bg(self._fetch_icon_bg, url, path)

    def _fetch_icon_bg(self, url: str, path: Path):
        try:
            if not path.exists():
                import urllib.request
                req = urllib.request.Request(url, headers={"User-Agent": "PyMC-Launcher/1.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    path.write_bytes(r.read())
        except Exception:
            self._icon_jobs.discard(str(path))
            return
        try:
            self.bridge.iconReady.emit(str(path))
        except Exception:
            pass

    @pyqtSlot(str)
    def on_icon_ready(self, path_str: str):
        self._icon_jobs.discard(path_str)
        path = Path(path_str)
        targets = self._icon_wait.pop(path_str, [])
        if not targets or not path.is_file():
            return
        pix = QPixmap(str(path))
        if pix.isNull():
            return
        pix = pix.scaled(20, 20, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
        icon = QIcon(pix)
        for table, row, pid in targets:
            try:
                item = table.item(row, 0)
                if item is None or item.data(Qt.ItemDataRole.UserRole) != pid:
                    continue
                item.setIcon(icon)
            except RuntimeError:
                continue

    def _set_icon(self, table: QTableWidget, row: int, pid: str, path: Path):
        pix = QPixmap(str(path))
        if pix.isNull():
            return
        pix = pix.scaled(20, 20, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
        item = table.item(row, 0)
        if item is not None and item.data(Qt.ItemDataRole.UserRole) == pid:
            item.setIcon(QIcon(pix))

    # ==================================================================
    # 关闭
    # ==================================================================
    def closeEvent(self, event):
        # [服务端] 关闭程序前强制结束开服工具里的服务端进程，防止残留 Java
        try:
            self._stop_server(is_exit=True)
        except Exception:
            pass
        # [陶瓦联机] 关闭前强制结束 Terracotta 进程树，绝不留后患
        self._kill_terracotta()
        self._save_settings()
        super().closeEvent(event)

    # ==================================================================
    # 自动更新检查
    # ==================================================================
    def _check_update(self, silent=False):
        """【后台线程】检查 GitHub 是否有新版本。

        - 后台 run_bg 请求，绝不卡 UI
        - 优先官方 API，异常/超时自动用 ghproxy 代理重试
        - 有新版本 -> bridge.info 弹窗；点确认则用浏览器打开 html_url
        - silent=True：请求失败或无新版一律不打扰；silent=False：失败弹「检查更新失败」
        """
        run_bg(self._update_check_bg, silent)

    def _update_check_bg(self, silent: bool):
        """真正在后台线程里请求并比较版本号（绝不碰 UI 控件，只 emit 信号）。"""
        import json as _json

        data = None
        last_err = ""
        for url in (GITHUB_RELEASES_API, GITHUB_RELEASES_PROXY):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": UPDATE_UA})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = _json.loads(r.read().decode("utf-8"))
                break
            except Exception as exc:
                last_err = str(exc)
                continue
        if not isinstance(data, dict) or not data:
            if silent:
                return
            self.bridge.log.emit(f"检查更新失败：{last_err}")
            self.bridge.warn.emit("检查更新",
                                  "检查更新失败，请检查网络")
            return

        remote_tag = str(data.get("tag_name") or "").strip()
        html_url = str(data.get("html_url") or "")
        if not remote_tag:
            if silent:
                return
            self.bridge.warn.emit("检查更新", "无法解析远端版本号")
            return

        if _compare_versions(remote_tag, CURRENT_VERSION) <= 0:
            # 没有新版本
            if not silent:
                self.bridge.info.emit("检查更新",
                                      f"当前已是最新版本 v{CURRENT_VERSION}")
            return

        # 发现新版本 -> 通知主线程弹「是否前往 GitHub」确认框（确认后由槽打开 URL）
        self.bridge.log.emit(f"发现新版本 {remote_tag}（当前 v{CURRENT_VERSION}）")
        self.bridge.ask_open_url.emit(remote_tag, html_url)

    def _on_update_open_url(self, remote_tag: str, html_url: str):
        """【主线程槽】发现新版本：弹「是否前往 GitHub」确认框，确认则打开网页。"""
        ret = QMessageBox.question(
            self, "发现新版本",
            f"发现新版本 {remote_tag}！\n是否前往 GitHub 查看更新详情？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if ret == QMessageBox.StandardButton.Yes:
            open_url_safe(html_url)

    # ==================================================================
    # 陶瓦联机（Terracotta，P2P）
    # ==================================================================
    def _start_terracotta(self):
        """按钮入口：点击「🌐 陶瓦联机 (P2P)」走免责确认 + 启动扫描线程。"""
        self._on_terracotta_clicked()

    def _on_scanner_process_started(self, pid: int):
        """【主线程槽】陶瓦进程已启动：状态置「启动中...」，记录 PID。"""
        self._terracotta_pid = pid
        self._set_terracotta_state("starting")
        self._terra_append_log(f"陶瓦联机进程已启动（PID {pid}），正在扫描端口...")
        self.bridge.log.emit(f"陶瓦联机进程已启动（PID {pid}），正在扫描端口...")
        self.bridge.status.emit("正在扫描陶瓦联机端口...")

    def _on_scanner_port_found(self, port: int):
        """【主线程槽】扫描到陶瓦端口：状态置「联机就绪」，弹提示并拉起浏览器。"""
        url = f"http://127.0.0.1:{port}/"
        self._set_terracotta_state("ready", port)
        self._terra_append_log(f"陶瓦联机端口就绪：{url}")
        self.bridge.log.emit(f"陶瓦联机端口就绪：{url}")
        self.bridge.status.emit(f"陶瓦联机就绪 (端口: {port})")
        
        # 主线程主动打开浏览器：对系统来说是本 Python 进程发起的普通请求
        QMessageBox.information(
            self, "陶瓦联机就绪",
            f"✅ 联机已就绪！\n\n地址：{url}\n\n将为您打开联机页面。")
        
        try:
            import subprocess
            # 1. 先干掉可能残留的高权限 Edge 实例（防止它拦截新实例）
            # 使用 CREATE_NO_WINDOW 防止闪黑框
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            subprocess.Popen("taskkill /f /im msedge.exe", shell=True, 
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, 
                             startupinfo=startupinfo)
            
            # 2. 使用 explorer.exe 强制降权拉起网页（避开 UAC 拦截）
            subprocess.Popen(["explorer.exe", url])
            
        except Exception as exc:
            self.bridge.log.emit(f"降权打开联机页面失败：{exc}，尝试备用方案...")
            # 备用方案：如果 explorer 抽风，退回 QDesktopServices
            try:
                QDesktopServices.openUrl(QUrl(url))
            except Exception as e2:
                self.bridge.log.emit(f"备用方案也失败了：{e2}")

    def _on_scanner_failed(self, reason: str):
        """【主线程槽】扫描/启动失败：状态置「已停止」，弹窗提示。"""
        self.bridge.log.emit(f"陶瓦联机失败：{reason}")
        self.bridge.status.emit("陶瓦联机失败")
        self._terra_append_log(f"陶瓦联机失败：{reason}")
        QMessageBox.critical(self, "陶瓦联机失败", reason)
        # 顺手清理可能的进程
        self._kill_terracotta()
        self._set_terracotta_state("stopped")

    def _on_terracotta_clicked(self):
        """【主线程】点击启动：直接拉起辅助器。"""
        if getattr(self, "_terra_helper_proc", None) is not None:
            QMessageBox.information(self, "提示", "陶瓦联机辅助进程已在运行中。")
            return

        if getattr(sys, "frozen", False):
            base_dir = Path(os.path.dirname(sys.executable))
        else:
            base_dir = launcher_dir()

        # 👇 这里的文件名必须和你打包出来的 exe 名字一模一样！
        helper_exe = base_dir / "PyMC_TerracottaLauncher.exe"
        
        if not helper_exe.exists():
            # 兼容开发环境（直接跑 py 脚本）
            helper_py = base_dir / "terracotta_port_scan.py"
            if not helper_py.exists():
                QMessageBox.warning(self, "错误", f"未找到辅助程序：\n{helper_exe.name}")
                return
            cmd = [sys.executable, str(helper_py)]
        else:
            cmd = [str(helper_exe)]

        self._set_terracotta_state("starting")
        self._terra_append_log("正在拉起底层联机核心...")
        self._terra_append_log("⚠️ 请注意：即将弹出“用户账户控制(UAC)”，请点击“是”以授权联机。")

        try:
            import subprocess
            # 直接用 start 命令拉起带 UAC 清单的程序，完美绕开 WinError 740
            # 第一个空字符串 "" 是窗口标题，防止路径带空格被错认
            cmd = f'start "" "{helper_exe}"'
            subprocess.Popen(cmd, shell=True, cwd=str(base_dir))
            
            self._set_terracotta_state("ready")
            self.bridge.status.emit("陶瓦联机已启动 (等待授权并后台探测中)")
        except Exception as exc:
            self._set_terracotta_state("stopped")
            QMessageBox.critical(self, "启动失败", f"无法启动辅助进程：\n{exc}")



    def _terracotta_is_alive(self) -> bool:
        """Terracotta 进程是否仍存活（只读，主线程调用）。"""
        proc = self._terracotta_proc
        if proc is None:
            return False
        try:
            if proc.poll() is None:
                return True
        except Exception:
            pass
        # 进程已退出，句柄失效
        try:
            self._terracotta_proc = None
        except Exception:
            pass
        return False

    def _terracotta_worker(self):
        """【后台线程】通配符查找本地陶瓦联机核心并拉起进程。

        - 优先在 launcher_dir() 用 glob("terracotta*.exe") 找，找到第一个即启动
        - 没找到再在 self._terracotta_cache_dir 用同样的通配符找一遍
        - 还是没有 -> 弹窗提示「未找到联机核心」；不联网、不下载、不循环重试
        """
        try:
            exe_path = self._find_terracotta_exe_glob(launcher_dir())
            if exe_path is None:
                exe_path = self._find_terracotta_exe_glob(self._terracotta_cache_dir)

            if exe_path is None:
                self.bridge.log.emit("未找到联机核心")
                self.bridge.warn.emit(
                    "陶瓦联机",
                    "未找到联机核心，请确保 terracotta.exe 在启动器目录下")
                return

            self.bridge.log.emit(f"陶瓦联机：检测到本地核心，直接启动 -> {exe_path}")
            self._terracotta_launch(exe_path)
        except Exception as exc:
            self.bridge.log.emit(f"陶瓦联机核心处理异常：{exc}")
            self.bridge.warn.emit(
                "陶瓦联机",
                "未找到联机核心，请确保 terracotta.exe 在启动器目录下")

    def _find_terracotta_exe_glob(self, base_dir) -> Path | None:
        """用通配符 base_dir.glob('terracotta*.exe') 找核心；返回第一个的绝对路径。

        找不到返回 None。launcher_dir() 根目录和缓存目录都直接用这个。
        """
        base_dir = Path(base_dir)
        try:
            hits = list(base_dir.glob("terracotta*.exe"))
        except Exception:
            return None
        for p in hits:
            try:
                if p.is_file():
                    return p.resolve()
            except Exception:
                continue
        return None

    def _terracotta_net_error(self, exc):
        """下载/解析失败：弹窗 + 日志记录详细报错，不重试。"""
        self.bridge.log.emit(f"[陶瓦联机] 详细错误: {exc!r}")
        self.bridge.status.emit("陶瓦联机核心获取失败")
        self.bridge.warn.emit("陶瓦联机", "无法获取联机核心，请检查网络或稍后再试。")

    def _terracotta_launch(self, exe_path: Path):
        """【后台线程】用 CREATE_NEW_PROCESS_GROUP 拉起 Terracotta exe。"""
        try:
            proc = subprocess.Popen(
                [str(exe_path)],
                cwd=str(exe_path.parent),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        except Exception as exc:
            self.bridge.log.emit(f"启动陶瓦联机失败：{exc}")
            self._terracotta_net_error(exc)
            return
        self._terracotta_proc = proc
        self.bridge.log.emit(
            f"陶瓦联机已启动（PID {proc.pid}），请在弹出的窗口进行联机操作。")
        self.bridge.status.emit("陶瓦联机已启动")
        self.bridge.info.emit("陶瓦联机",
                              "陶瓦联机已启动，请在弹出的窗口进行联机操作。")
        
    def _kill_terracotta(self):
        """【主线程】纯 ctypes 内核级强杀，彻底告别残留。"""
        import psutil
        import ctypes

        target_pids = set()
        try:
            for proc in psutil.process_iter(['pid', 'name']):
                pname = proc.info['name']
                if pname and ("terracotta" in pname.lower() or "terra" in pname.lower()):
                    target_pids.add(proc.info['pid'])
        except Exception:
            pass

        if not target_pids:
            self._terra_append_log("未发现存活的陶瓦/辅助器进程。")
            self._terra_helper_proc = None
            self._set_terracotta_state("stopped")
            return

        self._terra_append_log(f"检测到残留进程 PID: {target_pids}，执行内核级清理...")

        kernel32 = ctypes.windll.kernel32
        PROCESS_TERMINATE = 1

        for pid in target_pids:
            h_process = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if h_process:
                success = kernel32.TerminateProcess(h_process, 1)
                kernel32.CloseHandle(h_process)
                if success:
                    self._terra_append_log(f"✅ 成功强杀 PID: {pid}")
                else:
                    self._terra_append_log(f"❌ 强杀 PID: {pid} 失败")
            else:
                self._terra_append_log(f"❌ 无法获取 PID: {pid} 的句柄")

        self._terra_helper_proc = None
        self._set_terracotta_state("stopped")
        self.bridge.status.emit("陶瓦联机已停止")


# ---------------------------------------------------------------------------
# 独立窗口：游戏日志 / 通用文本
# ---------------------------------------------------------------------------
class GameLogWindow(QDialog):
    """游戏日志窗口 —— 独立顶层窗口（parent=None），不依附主窗口。"""

    def __init__(self, parent, version_id: str, kill_cb):
        super().__init__(parent)
        # 顶层独立窗口 + 任务栏独立按钮，主窗口最小化时它仍可见
        self.setWindowFlags(Qt.WindowType.Window
                            | Qt.WindowType.WindowMinMaxButtonsHint
                            | Qt.WindowType.WindowCloseButtonHint)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.version_id = version_id
        self.setWindowTitle(f"游戏日志 - {version_id}")
        self.resize(760, 460)
        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        btn = QPushButton("结束进程")
        btn.clicked.connect(kill_cb)
        bar.addWidget(btn)
        bar.addWidget(QLabel(f"运行中：{version_id}"))
        bar.addStretch(1)
        root.addLayout(bar)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(1000)
        self.text.setStyleSheet("font-family: Consolas, monospace; font-size: 9pt;")
        root.addWidget(self.text, 1)

    def append_text(self, text: str):
        if not text:
            return
        self.text.moveCursor(QTextCursor.MoveOperation.End)
        self.text.insertPlainText(text)

    def closeEvent(self, event):
        # 关闭时只隐藏，进程还在跑也能用「结束进程」再打开
        super().closeEvent(event)


class TextDialog(QDialog):
    def __init__(self, parent, title: str, initial: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 420)
        lay = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(3000)
        self.text.setPlainText(initial)
        lay.addWidget(self.text)

    def append_text(self, text: str):
        self.text.appendPlainText(text)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    core.BASE_DIR.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    app.setApplicationName(core.APP_NAME)
    # [FIX-截断恢复] 图标优先用 exe 同目录的 icon.jpg（PyInstaller 兼容），
    # onedir 时打包数据在 _internal；找不到再回退到 BASE_DIR/icon.png
    for _ic in [p / "icon.jpg" for p in resource_search_dirs()] + [
            p / "icon.png" for p in resource_search_dirs()] + [
            core.BASE_DIR / "icon.png"]:
        if _ic.is_file():
            app.setWindowIcon(QIcon(str(_ic)))
            break

    # [首次启动免责声明] 在创建 MainWindow 之前读取 cfg，若未同意则弹窗
    cfg = core.load_config()
    # [FIX-免责弹窗重复] 合并启动器目录 local_cfg.json，确保同意状态不会因
    # %APPDATA% 不可写而丢失
    cfg = _merge_local_cfg(cfg)
    if not cfg.get("agreed_to_disclaimer", False):
        dlg = QMessageBox()
        dlg.setWindowTitle("欢迎使用本启动器")
        dlg.setText(
            "欢迎使用本启动器！\n\n"
            "1. 本工具为个人非盈利开源项目，仅供学习交流，与 Mojang 无关。\n"
            "2. 启动器包含「一键清理」、「释放内存」及「权限修复」等系统级操作，属正常维护行为。\n"
            "3. 若因操作不当或杀毒软件拦截导致数据丢失，作者不承担任何责任。\n"
            "4. 如 Windows Defender 提示风险，为打包误报，点击信任即可。\n\n"
            "点击「同意」代表您已阅读并同意上述条款。")
        agree_btn = dlg.addButton("同意并启动", QMessageBox.ButtonRole.AcceptRole)
        reject_btn = dlg.addButton("拒绝并退出", QMessageBox.ButtonRole.RejectRole)
        # 置顶
        dlg.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        dlg.exec()
        if dlg.clickedButton() is agree_btn:
            cfg["agreed_to_disclaimer"] = True
            ok = core.save_config(cfg)
            # 无论主配置写没写成，都同步写一份到启动器目录，保证下次能读到
            _persist_local_override("agreed_to_disclaimer", True)
            if not ok:
                # 诊断：主配置路径写失败（杀软/权限锁定）
                print(f"[main] 主配置写入失败，已回退启动器目录 local_cfg.json")
        else:
            sys.exit(0)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
