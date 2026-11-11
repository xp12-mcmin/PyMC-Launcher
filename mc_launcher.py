#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minecraft 启动器 (Python)
=========================
功能：
  1. 识别本机已安装的 Java (自动扫描常见路径 + 环境变量 + 注册表)
  2. 下载 Java (Adoptium/Temurin JRE，自动选择与系统匹配的版本)
  3. 获取并下载 Minecraft 版本 (从官方 version manifest)
  4. 启动 Minecraft (自动下载核心 jar / libraries / assets / natives)
  5. 支持正版账号 (Microsoft 登录) 与离线账号两种模式
  6. 图形界面 (tkinter)

仅依赖 Python 标准库 (tkinter / urllib / json / zipfile / subprocess)。
"""

import json
import os
import platform
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import winreg
import zipfile
import concurrent.futures
from pathlib import Path
from tkinter import (
    Tk, ttk, messagebox, filedialog, StringVar, BooleanVar,
    IntVar, PhotoImage
)
from tkinter import scrolledtext

# 在 import minecraft_launcher_lib 之前打 NeoForge 源切换 patch
# （库升级后删除 neoforge_patch.py 并去掉下面这行即可回退官方源）
try:
    import neoforge_patch  # noqa: F401
except Exception:
    neoforge_patch = None

# 在 import lib 之前打 Mojang 下载镜像回退 patch（被墙时走 BMCLAPI2）
# （库升级后删除 mojang_mirror_patch.py 并去掉下面这行即可回退官方源）
try:
    import mojang_mirror_patch  # noqa: F401
except Exception:
    mojang_mirror_patch = None

# Modrinth 官方 SDK 封装 + MC百科中文搜索（pip install modrinth）
try:
    import modrinth_sdk  # noqa: F401
    modrinth_sdk.patch_sdk()
except Exception:
    modrinth_sdk = None

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
APP_NAME = "PyMC Launcher"
BASE_DIR = Path(os.path.expandvars(r"%APPDATA%\.pymc"))
CONFIG_FILE = BASE_DIR / "config.json"

# 默认路径
DEFAULT_MINECRAFT_DIR = Path(os.path.expandvars(r"%APPDATA%\.minecraft"))
DEFAULT_JAVA_DIR = BASE_DIR / "java"

# 当前生效路径 (由 apply_paths() 根据配置设置，可在 GUI 中更改)
MINECRAFT_DIR = DEFAULT_MINECRAFT_DIR
JAVA_DIR = DEFAULT_JAVA_DIR
VERSIONS_DIR = MINECRAFT_DIR / "versions"
LIBRARIES_DIR = MINECRAFT_DIR / "libraries"
ASSETS_DIR = MINECRAFT_DIR / "assets"
NATIVES_DIR = MINECRAFT_DIR / "natives"
LOGIN_FILE = BASE_DIR / "login.json"


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------
def load_config() -> dict:
    """读取启动器配置。返回 {game_dirs, java_dir, username, mem, jvm, language}。"""
    cfg = {
        "game_dirs": [str(DEFAULT_MINECRAFT_DIR)],
        "java_dir": str(DEFAULT_JAVA_DIR),
        "username": "Steve",
        "mem": auto_memory_mb(),
        "jvm": "",
        "language": "zh_CN",
    }
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            # 兼容旧版 game_dir
            if "game_dir" in data and "game_dirs" not in data:
                raw = data["game_dir"]
                # 如果是 versions 子目录，自动提升到父目录
                if raw.endswith("\\versions") or raw.endswith("/versions"):
                    raw = Path(raw).parent
                data["game_dirs"] = [raw]
                del data["game_dir"]
            # 规范化 game_dirs 中的路径
            if "game_dirs" in data and isinstance(data["game_dirs"], list):
                data["game_dirs"] = [
                    str(Path(d).resolve()) for d in data["game_dirs"]
                    if d and isinstance(d, str)
                ]
            cfg.update(data)
    except Exception:
        pass
    return cfg


def save_config(cfg: dict):
    """保存启动器配置。

    [FIX-静默写失败] 不再 except:pass 吞异常——返回 bool 表明是否真正落盘，
    并打印一行诊断，方便排查「配置改了却不生效 / 免责声明反复弹」这类问题。
    """
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                               encoding="utf-8")
        return True
    except Exception as exc:
        import builtins
        builtins.print(f"[save_config] 写入失败 {CONFIG_FILE}: {exc!r}")
        return False


def save_config_quiet(cfg: dict) -> bool:
    """save_config 的兼容别名：同样返回 bool，但不额外打诊断（供高频调用处用）。"""
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                               encoding="utf-8")
        return True
    except Exception:
        return False


def apply_paths(cfg: dict):
    """根据配置设置当前生效的路径全局变量。"""
    global MINECRAFT_DIR, JAVA_DIR, VERSIONS_DIR, LIBRARIES_DIR, ASSETS_DIR, NATIVES_DIR
    game_dirs = cfg.get("game_dirs", [])
    if game_dirs and isinstance(game_dirs, list):
        game = game_dirs[0] or str(DEFAULT_MINECRAFT_DIR)
    else:
        game = cfg.get("game_dir") or str(DEFAULT_MINECRAFT_DIR)
    jdir = cfg.get("java_dir") or str(DEFAULT_JAVA_DIR)
    MINECRAFT_DIR = Path(game)
    JAVA_DIR = Path(jdir)
    VERSIONS_DIR = MINECRAFT_DIR / "versions"
    LIBRARIES_DIR = MINECRAFT_DIR / "libraries"
    ASSETS_DIR = MINECRAFT_DIR / "assets"
    NATIVES_DIR = MINECRAFT_DIR / "natives"

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
# 备用镜像 (国内用户可用)
MANIFEST_MIRRORS = [
    "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json",
    "https://bmclapi2.bangbang93.com/mc/game/version_manifest_v2.json",
]

ADOPTIUM_API = "https://api.adoptium.net/v3/assets/latest/{feature}/hotspot"
ADOPTIUM_MIRROR = "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/"
# [FIX-Java下载超时] 国内镜像兜底直链（JRE，zip 便携版，免安装）
# 清华 TUNA 的 /Adoptium/ 目录页是 JS 动态渲染、HEAD 被 403，无法程序化列目录，
# 且其 WAF 会拦非浏览器 UA 的直链；因此「清华优先 + 华为云备用」双镜像兜底，
# 下载失败/被拦时自动切另一个；镜像滚动更新时兜底失效，则回退官方 API/GitHub，
# 最终仍失败给出「下载超时，请检查网络或手动下载 Java」的明确提示。
# 注：以下直链在「浏览器手动下载」场景下可用（WAF 放行浏览器），
#     程序化下载时若被 403 拦截会自动切到备用镜像或官方源。
ADOPTIUM_TUNA_FALLBACK = {
    8:  "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/8/jre/x64/8u504b01/OpenJDK8U-jre_x64_windows_hotspot_8u504b01.zip",
    17: "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/17/jre/x64/17.0.20.1_1/OpenJDK17U-jre_x64_windows_hotspot_17.0.20.1_1.zip",
    21: "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/21/jre/x64/21.0.12.1_1/OpenJDK21U-jre_x64_windows_hotspot_21.0.12.1_1.zip",
}
# 清华被 WAF 拦（403）时的备用镜像：华为云（路径结构与清华一致）
ADOPTIUM_HUAWEI_FALLBACK = {
    8:  "https://repo.huaweicloud.com/openjdk/8/jre/x64/8u504b01/OpenJDK8U-jre_x64_windows_hotspot_8u504b01.zip",
    17: "https://repo.huaweicloud.com/openjdk/17/jre/x64/17.0.20.1_1/OpenJDK17U-jre_x64_windows_hotspot_17.0.20.1_1.zip",
    21: "https://repo.huaweicloud.com/openjdk/21/jre/x64/21.0.12.1_1/OpenJDK21U-jre_x64_windows_hotspot_21.0.12.1_1.zip",
}

SYSTEM_ARCH = "x64" if platform.machine().lower() in ("amd64", "x86_64") else "x86"


def auto_memory_mb() -> int:
    """根据系统物理内存自动分配 -Xmx (单位 MB)。留 2GB 给系统，最高 8GB，最低 1024MB。"""
    try:
        import ctypes
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        total_gb = stat.ullTotalPhys / (1024 ** 3)
    except Exception:
        return 2048
    # 给系统留 2GB，其余给游戏，上限 8GB，下限 1GB
    mem_gb = max(1, min(8, int(total_gb) - 2))
    return mem_gb * 1024

# 各 Minecraft 主版本对应的推荐 Java 大版本
MC_JAVA_MAP = [
    (21, 17),   # 1.20.5+  -> Java 21
    (18, 17),   # 1.18+    -> Java 17
    (17, 16),   # 1.17     -> Java 16
    (16, 8),    # 1.16 及以下 -> Java 8
    (8, 8),
    (1, 8),
]


class _CancelLaunch(Exception):
    """用户取消启动时抛出。"""
    pass


def mc_to_java_major(version: str) -> int:
    """根据 Minecraft 版本号推断推荐的 Java 大版本（最低运行要求）。"""
    import re
    # 清理版本名中的非数字前缀 (如 "1.20.1-Fabric 0.19.3" -> "1.20.1")
    clean = re.split(r"[-\s]", version)[0]
    parts = [int(p) for p in clean.split(".") if p.isdigit()]
    if not parts:
        return 17
    major, minor = (parts + [0, 0])[:2]
    # 新命名体系 (25.2 / 26.3 等): major >= 25 -> Java 21
    if major >= 25:
        return 21
    if major == 1:
        if minor <= 16:          # 1.0 - 1.16.5  -> Java 8
            return 8
        if minor <= 18:          # 1.17 - 1.18.x  -> Java 17
            return 17
        if minor == 19:          # 1.19.x -> Java 17
            return 17
        if minor == 20:
            patch = parts[2] if len(parts) >= 3 else 0
            # 1.20.5+ 官方最低 Java 21
            return 21 if patch >= 5 else 17
        # 1.21+ -> Java 21
        return 21
    return 17


# ---------------------------------------------------------------------------
# Mod Loader (Forge / Fabric / NeoForge)
# ---------------------------------------------------------------------------
# 多源镜像：官方站 → 华为云 maven → (Fabric 无镜像回退，但 meta API 官方较稳定)
FORGE_API = "https://files.minecraftforge.net/net/minecraftforge/forge/maven-metadata.json"
FABRIC_API = "https://meta.fabricmc.net/v2/versions/loader"
NEOFORGE_API = "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"


def parse_mc_version(version_id: str) -> tuple:
    """从版本名解析出 Minecraft 主版本号。如 '1.20.1-Fabric 0.18.0' -> ('1.20.1', 'fabric')"""
    import re
    clean = re.split(r"[-\s]", version_id)[0]
    loader = ""
    for part in version_id.split():
        pl = part.lower()
        if "fabric" in pl:
            loader = "fabric"
        elif "forge" in pl or "neoforge" in pl:
            loader = "neoforge" if "neo" in pl else "forge"
        elif "optifine" in pl:
            loader = "optifine"
    return clean, loader


def get_forge_versions(mc_version: str):
    """从 Forge API 获取指定 MC 版本的 Forge 列表。多源回退。

    返回 [(version, url)]，version 形如 "1.20.1-47.4.18"。
    maven-metadata.json 结构为 {"<mc>": ["<mc>-<forge>", ...]}。
    """
    urls = [FORGE_API,
            "https://repo.huaweicloud.com/maven2/net/minecraftforge/forge/maven-metadata.json"]
    for u in urls:
        try:
            data = Net.get_json(u, timeout=15)
            if not isinstance(data, dict):
                continue
            versions = data.get(mc_version, []) or []
            result = []
            for full in versions:
                # full 形如 "1.20.1-47.4.18"
                if full.startswith(mc_version + "-"):
                    result.append((full, None))
            def _key(x):
                tail = x[0].split("-", 1)[1] if "-" in x[0] else x[0]
                return tuple(int(p) for p in tail.split(".") if p.isdigit())
            result.sort(key=_key, reverse=True)
            return result
        except Exception:
            continue
    return []


def get_fabric_versions(mc_version: str):
    """从 Fabric API 获取指定 MC 版本的 Fabric 列表。多源回退。

    返回 [(loader_version, installer_url), ...]
    """
    urls = [FABRIC_API]
    for u in urls:
        try:
            data = Net.get_json(u, timeout=15)
            loaders = data.get("loaders", [])
            mc_versions = data.get("versions", [])
            target = None
            for mv in mc_versions:
                if mv["version"] == mc_version:
                    target = mv
                    break
            if not target:
                return []
            result = []
            for l in loaders:
                if l["type"] == "loader" and l["version"] in target.get("compatibleLoaders", []):
                    inst = next((i for i in target.get("installs", [])
                                 if i.get("profile") == "generic"), None)
                    if inst:
                        result.append((l["version"], inst["url"]))
            result.sort(key=lambda x: tuple(int(p) for p in x[0].split(".")
                                           if p.isdigit()), reverse=True)
            return result
        except Exception:
            continue
    return []


def download_forge(version_id, forge_version, build, url, progress_cb=None, log_cb=None):
    """下载并安装 Forge。多源回退：官方 maven → 华为云镜像。

    forge_version: 完整版本号，形如 "47.4.18" 或 "1.20.1-47.4.18"。
    兼容旧格式：build/url 可为 None，此时自动构建现代 installer URL。
    """
    import re
    mc_ver, _ = parse_mc_version(version_id)
    # 规范化完整版本串
    if forge_version.startswith(mc_ver + "-"):
        full = forge_version
        forge_part = forge_version.split("-", 1)[1]
    else:
        forge_part = forge_version
        full = f"{mc_ver}-{forge_version}"
    # 安装器 URL（现代 Forge，1.17+），多源
    official = (f"https://maven.minecraftforge.net/net/minecraftforge/forge/{full}/"
                f"forge-{full}-installer.jar")
    mirror = (f"https://repo.huaweicloud.com/maven2/net/minecraftforge/forge/{full}/"
              f"forge-{full}-installer.jar")
    fname = f"forge-{full}-installer.jar"
    dest = VERSIONS_DIR / version_id / fname
    log_cb(f"下载 Forge {full}...")
    urls = [url] if url else [official]
    if mirror not in urls:
        urls.append(mirror)
    for i, u in enumerate(urls, 1):
        try:
            Net.download(u, dest, progress_cb, timeout=120)
            log_cb(f"Forge 下载完成 (源{i}): {dest}")
            _run_forge_installer(dest, version_id, log_cb)
            return
        except Exception as e:
            if i == len(urls):
                log_cb(f"Forge 下载失败(全部源): {e}")
            else:
                log_cb(f"源{i} 下载失败，换镜像: {e}")


def _run_forge_installer(jar_path, version_id, log_cb):
    """运行 Forge installer 进行静默安装。"""
    try:
        import subprocess
        install_dir = MINECRAFT_DIR
        log_cb(f"运行 Forge installer，安装到 {install_dir}...")
        result = subprocess.run(
            ["java", "-jar", str(jar_path), "--installClient", str(install_dir)],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            log_cb("Forge 安装成功")
        else:
            log_cb(f"Forge 安装失败: {result.stderr[:200]}")
    except Exception as e:
        log_cb(f"自动安装失败，请手动运行: {jar_path}")


def download_fabric(version_id, fabric_version, installer_url, progress_cb=None, log_cb=None):
    """下载并安装 Fabric Loader。多源回退：官方 → maven 镜像。"""
    import re
    mc_ver, _ = parse_mc_version(version_id)
    fname = f"fabric-installer-{fabric_version}.jar"
    dest = VERSIONS_DIR / version_id / fname
    log_cb(f"下载 Fabric Loader {fabric_version}...")
    urls = [installer_url]
    m = re.search(r"(\d+\.\d+\.\d+)", installer_url or "")
    if m:
        urls.append(
            f"https://maven.fabricmc.net/net/fabricmc/fabric-installer/"
            f"{m.group(1)}/fabric-installer-{m.group(1)}.jar")
    for i, u in enumerate(urls, 1):
        try:
            Net.download(u, dest, progress_cb, timeout=60)
            log_cb(f"Fabric 下载完成 (源{i}): {dest}")
            _run_fabric_installer(dest, version_id, log_cb)
            return
        except Exception as e:
            if i == len(urls):
                log_cb(f"Fabric 下载失败(全部源): {e}")
            else:
                log_cb(f"源{i} 下载失败，换镜像: {e}")


def _run_fabric_installer(jar_path, version_id, log_cb):
    """运行 Fabric installer。"""
    try:
        import subprocess
        log_cb(f"运行 Fabric installer...")
        result = subprocess.run(
            ["java", "-jar", str(jar_path), "client", "--version", version_id, "--download", "minecraft", "mappings", "nw", "loader"],
            capture_output=True, text=True, timeout=120,
            cwd=str(MINECRAFT_DIR)
        )
        if result.returncode == 0:
            log_cb("Fabric 安装成功")
        else:
            log_cb(f"Fabric 安装失败: {result.stderr[:200]}")
    except Exception as e:
        log_cb(f"自动安装失败，请手动运行: {jar_path}")


FABRIC_API_MOD_URL = "https://cdn.modrinth.com/data/AANobbMI/versions"


def _download_fabric_api_mc(version_id, log_cb, progress_cb=None):
    """从 Modrinth 自动下载当前版本对应的 Fabric API 到该版本的 mods 目录。

    version_id: 完整版本目录名（如 "1.20.1-Fabric 0.18.1"），Fabric API 会装到
    该版本的 mods 目录，避免装错版本。
    """
    mc_version, _ = parse_mc_version(version_id)
    url = (f"https://api.modrinth.com/v2/project/AANobbMI"
           f"/version?game_versions={mc_version}&limit=3")
    try:
        data = Net.get_json(url, timeout=20)
        if not data:
            log_cb(f"未找到 {mc_version} 的 Fabric API")
            return False
        files = data[0].get("files", [])
        primary = next((f for f in files if f.get("primary")), files[0])
        dest = _ensure_resource_dir(version_id, "mods") / primary.get("filename", "fabric-api-mod.jar")
        dest.parent.mkdir(parents=True, exist_ok=True)
        log_cb(f"下载 Fabric API {data[0].get('name')} 到 {dest}")
        Net.download(primary["url"], dest, progress_cb, timeout=120)
        log_cb("Fabric API 下载完成")
        return True
    except Exception as e:
        log_cb(f"Fabric API 下载失败: {e}")
        return False


def _get_neoforge_versions(mc_version: str, timeout=8):
    """获取 NeoForge 版本列表。返回 [(neoforge_version, installer_url), ...]。

    多源回退：官方 maven.neoforged.net → 华为云镜像 → Modrinth 项目版本。
    每源 8s 超时（防止界面卡死）。
    """
    import xml.etree.ElementTree as ET
    import re

    def fetch_versions():
        """从多源拉 NeoForge 版本，任一成功即返回 [version, ...]。"""
        # 1) 官方 maven-metadata.xml
        try:
            text = Net.get_text(NEOFORGE_API, timeout=timeout)
            root = ET.fromstring(text)
            return [v.text for v in root.iter("version") if v.text]
        except Exception:
            pass
        # 2) 华为云 NeoForge 镜像
        try:
            mirror = "https://repo.huaweicloud.com/maven2/net/neoforged/neoforge/maven-metadata.xml"
            text = Net.get_text(mirror, timeout=timeout)
            root = ET.fromstring(text)
            return [v.text for v in root.iter("version") if v.text]
        except Exception:
            pass
        # 3) Modrinth NeoForge 官方项目版本（最新 50 个）
        #    NeoForge 在 Modrinth 的项目 slug 是 "neoforge"（loader 类型）
        try:
            data = Net.get_json(
                "https://api.modrinth.com/v2/project/neoforge/version?limit=50",
                timeout=timeout)
            # 版本号从 version_number 取（形如 "21.1.34" 或 "1.21.1-21.1.34"）
            result = []
            for v in data:
                vn = v.get("version_number", "")
                # 取形如 XX.Y.Z 的主版本号
                m2 = re.search(r"(\d+\.\d+\.\d+)", vn)
                if m2:
                    result.append(m2.group(1))
            return result
        except Exception:
            return []

    versions = fetch_versions()
    if not versions:
        return []
    # NeoForge 版本形如 21.1.34 / 20.1.72，对应 MC 1.21.1 / 1.20.1
    prefix = mc_version[2:] + "."  # "1.21.1" -> "21.1."
    # 下载源（按优先级）
    sources = [
        "https://maven.neoforged.net/releases/net/neoforged/neoforge",
        "https://repo.huaweicloud.com/maven2/net/neoforged/neoforge",
    ]
    result = []
    for v in versions:
        if not v.startswith(prefix) or "." not in v[len(prefix):]:
            continue
        url = f"{sources[0]}/{v}/neoforge-{v}-installer.jar"
        result.append((v, url))
    seen = set()
    uniq = []
    for v, u in result:
        if v not in seen:
            seen.add(v)
            uniq.append((v, u))
    uniq.sort(key=lambda x: tuple(int(p) for p in x[0].split(".") if p.isdigit()),
              reverse=True)
    return uniq[:30]


def _download_neoforge(version_id, neoforge_version, url, progress_cb=None, log_cb=None):
    """下载并安装 NeoForge。多源回退：官方 maven → 华为云镜像。"""
    fname = f"neoforge-{neoforge_version}-installer.jar"
    dest = VERSIONS_DIR / version_id / fname
    log_cb(f"下载 NeoForge {neoforge_version}...")
    # 下载源列表：先试 url（默认官方），失败再试华为云
    urls = [url]
    mirror = f"https://repo.huaweicloud.com/maven2/net/neoforged/neoforge/{neoforge_version}/neoforge-{neoforge_version}-installer.jar"
    urls.append(mirror)
    for i, u in enumerate(urls, 1):
        try:
            Net.download(u, dest, progress_cb, timeout=120)
            log_cb(f"NeoForge 下载完成 (源{i}): {dest}")
            _run_neoforge_installer(dest, version_id, log_cb)
            return
        except Exception as e:
            if i == len(urls):
                log_cb(f"NeoForge 下载失败(全部源): {e}")
            else:
                log_cb(f"源{i} 下载失败，换镜像: {e}")


def _run_neoforge_installer(jar_path, version_id, log_cb):
    """运行 NeoForge installer 进行安装。"""
    try:
        import subprocess
        install_dir = MINECRAFT_DIR
        log_cb(f"运行 NeoForge installer，安装到 {install_dir}...")
        result = subprocess.run(
            ["java", "-jar", str(jar_path), "--installClient", str(install_dir)],
            capture_output=True, text=True, timeout=180
        )
        if result.returncode == 0:
            log_cb("NeoForge 安装成功")
        else:
            log_cb(f"NeoForge 安装失败: {result.stderr[:200]}")
    except Exception as e:
        log_cb(f"自动安装失败，请手动运行: {jar_path}")



class Net:
    """带重试和镜像回退的简单下载工具。"""

    @staticmethod
    def _open(url, timeout=30):
        req = urllib.request.Request(
            url, headers={"User-Agent": "PyMC-Launcher/1.0"}
        )
        return urllib.request.urlopen(req, timeout=timeout)

    @staticmethod
    def get_json(url, timeout=30):
        with Net._open(url, timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    @staticmethod
    def get_text(url, timeout=30):
        with Net._open(url, timeout) as r:
            return r.read().decode("utf-8")

    @staticmethod
    def download(url, dest: Path, progress_cb=None, timeout=60):
        """下载文件到 dest。progress_cb(cur, total) 用于进度回调。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        with Net._open(url, timeout) as r:
            total = int(r.headers.get("Content-Length") or 0)
            cur = 0
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
                    cur += len(chunk)
                    if progress_cb:
                        progress_cb(cur, total)
            os.replace(tmp, dest)
        return dest


# ---------------------------------------------------------------------------
# Modrinth 资源下载 (mod / 光影 / mod 加载器)
# 数据来自官方 Modrinth API（公开，无需 API key，含前置依赖信息）
# ---------------------------------------------------------------------------
MODRINTH_API = "https://api.modrinth.com/v2"


def modrinth_search(query, project_type="mod", loader=None, game_version=None,
                    limit=30, facets=None, offset=0, index="relevance"):
    """在 Modrinth 上搜索资源。返回 hit 列表。

    project_type: "mod" / "shader" / "modpack" / "resourcepack" / "datapack"
    loader:  "forge" / "fabric" / "neoforge" / "quilt"
    game_version: 如 "1.20.1"
    offset: 分页偏移量（第一页 0，第二页 limit，…）
    index: 排序方式 relevance(默认/热门) | downloads | updated。
    query 为空字符串时，返回当前最热门的项目列表。
    返回 [{project_id, slug, title, description, author, ...}]
    """
    import urllib.parse
    params = [("query", query or ""), ("limit", str(limit)),
              ("offset", str(offset)),
              ("project_type", project_type),
              ("index", index or "relevance")]
    # Modrinth facets 语法：[["categories:fabric"],["versions:1.20.1"],["project_type:mod"]]
    facet_list = []
    if project_type:
        facet_list.append(["project_type:" + project_type])
    if loader:
        facet_list.append(["categories:" + loader])
    if game_version:
        facet_list.append(["versions:" + game_version])
    if facet_list:
        params.append(("facets", json.dumps(facet_list)))
    qs = urllib.parse.urlencode(params)
    url = f"{MODRINTH_API}/search?{qs}"
    data = Net.get_json(url, timeout=20)
    hits = data.get("hits", [])
    # 客户端二次过滤：确保返回的都是指定 project_type（防整合包混入 mod 搜索）
    if project_type:
        hits = [h for h in hits if (h.get("project_type") or "") == project_type]
    return hits


def modrinth_project_versions(project_id):
    """获取某项目（mod/shader）的版本列表。返回 [{id, name, game_versions, loaders, files, dependencies}]"""
    url = f"{MODRINTH_API}/project/{project_id}/version"
    return Net.get_json(url, timeout=20)


def modrinth_version(version_id):
    """获取某个具体版本的详情（含文件下载地址与依赖）。"""
    url = f"{MODRINTH_API}/version/{version_id}"
    return Net.get_json(url, timeout=20)


def modrinth_loaders():
    """Modrinth 支持的加载器列表（含名称/颜色/图标）。"""
    url = f"{MODRINTH_API}/tag/loader"
    return Net.get_json(url, timeout=20)


def modrinth_game_versions():
    """Modrinth 支持的游戏版本列表。"""
    url = f"{MODRINTH_API}/tag/game_version"
    return Net.get_json(url, timeout=20)


def _pick_game_version(version_id: str):
    """从版本目录名解析出主游戏版本号。如 '1.20.1-Forge_47.4.18' -> '1.20.1'。
    返回标准主版本（去掉预发布/快照标记）或原样。"""
    m = re.match(r"(\d+\.\d+(?:\.\d+)?)", version_id or "")
    if m:
        return m.group(1)
    return version_id or ""


def _ensure_resource_dir(version_id: str, sub: str) -> Path:
    """返回版本对应的 mods/shaderpacks 目录，没有则自动创建。

    sub: "mods" 或 "shaderpacks"
    严格版本隔离（对齐 PCL）：只创建/使用「该版本自己的目录」
    versions/<version_id>/<sub>，绝不回退到游戏根目录（.minecraft/mods），
    避免把全局 mods 共享给所有版本。
    若版本目录不可写则退回系统临时目录。
    """
    sub = sub.strip("/\\")
    if version_id:
        c = VERSIONS_DIR / version_id / sub
        try:
            c.mkdir(parents=True, exist_ok=True)
            probe = c / ".write_test"
            probe.write_text("x")
            probe.unlink()
            return c
        except Exception:
            pass
    # 版本目录不可写 → 退回系统临时目录（保证可用）
    import tempfile
    c = Path(tempfile.gettempdir()) / "pymc" / sub
    c.mkdir(parents=True, exist_ok=True)
    return c


def sync_version_mods(version_id: str) -> None:
    """PCL 式版本隔离：启动前把该版本的 mods/光影 同步到游戏根目录。

    Mojang 标准布局下，游戏只从 <gameDir>/mods 和 <gameDir>/shaderpacks
    加载 mod/光影。PCL 把每个版本的资源存在 versions/<版本>/mods 下，
    启动时再同步。本函数：
    1. 清空 <gameDir>/mods 与 <gameDir>/shaderpacks（避免上次版本的残留）
    2. 把 versions/<版本>/mods、versions/<版本>/shaderpacks 里的文件拷进去
    这样每个版本启动时只加载「自己的」mod，实现版本隔离。
    """
    import shutil
    import time
    vdir = VERSIONS_DIR / version_id
    for sub in ("mods", "shaderpacks"):
        src = vdir / sub
        dst = MINECRAFT_DIR / sub
        dst.mkdir(parents=True, exist_ok=True)
        # 1) 清掉目标目录里的 jar/zip/mrpack（不碰其他文件，如 config）
        if dst.is_dir():
            for f in dst.iterdir():
                if f.is_file() and f.suffix.lower() in (".jar", ".zip", ".mrpack"):
                    try:
                        f.unlink()
                    except Exception:
                        pass
        # 2) 拷贝该版本自己的资源
        if src.is_dir():
            t0 = time.time()
            n = 0
            for f in src.iterdir():
                if f.is_file() and f.suffix.lower() in (".jar", ".zip", ".mrpack"):
                    try:
                        shutil.copy2(str(f), str(dst / f.name))
                        n += 1
                    except Exception:
                        pass
            print(f"[版本隔离] {version_id} 同步 {sub}: {n} 个文件 "
                  f"(耗时 {time.time()-t0:.2f}s)", flush=True)


def modrinth_resolve_dependencies(version_info, project_type="mod"):
    """从版本信息解析出前置依赖（required 的 mod/shader）。

    返回 [{project_id, slug, title, version_id, version_title, kind}]，
    kind 取值: 'required' / 'optional' / 'incompatible' / 'embedded'
    只返回 required（必要前置），供一键下载。
    """
    deps = []
    seen = set()
    for d in version_info.get("dependencies", []) or []:
        dep_type = d.get("dependency_type", "")
        if dep_type != "required":
            continue
        pid = d.get("project_id") or d.get("version_id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        deps.append({
            "project_id": d.get("project_id"),
            "version_id": d.get("version_id"),
            "version_title": d.get("version_title"),
            "kind": dep_type,
        })
    return deps


def modrinth_download_file(version_info, dest_dir, progress_cb=None, log_cb=None):
    """下载某版本的文件到 dest_dir，返回下载后的文件路径。

    version_info 为 modrinth_version()/modrinth_project_versions() 的单个元素。
    选主要文件（primary=True），或第一个文件。
    """
    files = version_info.get("files", []) or []
    if not files:
        raise RuntimeError("该版本没有可用文件")
    primary = next((f for f in files if f.get("primary")), files[0])
    url = primary.get("url")
    fname = primary.get("filename") or Path(url).name
    # 清理文件名中的非法字符
    fname = re.sub(r'[\\/:*?"<>|]', "_", fname)
    dest = Path(dest_dir) / fname
    if dest.exists():
        if log_cb:
            log_cb(f"已存在，跳过: {fname}")
        return dest
    if log_cb:
        log_cb(f"下载 {fname} ...")
    Net.download(url, dest, progress_cb, timeout=120)
    if log_cb:
        log_cb(f"下载完成: {fname}")
    return dest


# ---------------------------------------------------------------------------
# Java 检测
# ---------------------------------------------------------------------------
def java_candidates():
    """返回候选的 java 可执行文件路径列表。"""
    cands = []
    # 1) JAVA_HOME
    jh = os.environ.get("JAVA_HOME")
    if jh:
        cands.append(Path(jh) / "bin" / "java.exe")
    # 2) PATH
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if p:
            cands.append(Path(p) / "java.exe")
    # 3) 常见安装路径
    common = [
        r"C:\Program Files\Java",
        r"C:\Program Files (x86)\Java",
        r"C:\Program Files\Eclipse Adoptium",
        r"C:\Program Files (x86)\Eclipse Adoptium",
        r"C:\Program Files\Microsoft",
        r"C:\Program Files\Zulu",
        r"C:\Program Files\AdoptOpenJDK",
    ]
    for base in common:
        base = Path(base)
        if base.exists():
            for jdk_dir in base.iterdir():
                cands.append(jdk_dir / "bin" / "java.exe")
    # 4) 注册表
    try:
        for hive, access in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                             (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY)):
            try:
                key = winreg.OpenKey(hive, r"SOFTWARE\JavaSoft\Java Runtime Environment", 0, access)
            except OSError:
                continue
            try:
                cur, _ = winreg.QueryValueEx(key, "CurrentVersion")
                sub = winreg.OpenKey(key, cur)
                home, _ = winreg.QueryValueEx(sub, "JavaHome")
                cands.append(Path(home) / "bin" / "java.exe")
            except OSError:
                pass
            finally:
                winreg.CloseKey(key)
    except Exception:
        pass
    # 5) 本启动器自带的 Java
    if JAVA_DIR.exists():
        for jdir in JAVA_DIR.iterdir():
            cands.append(jdir / "bin" / "java.exe")
    return cands


def detect_java():
    """扫描已安装 Java，返回 [(版本号, 路径), ...]，按版本降序。"""
    results = []
    seen = set()
    for cand in java_candidates():
        if not cand.is_file():
            continue
        try:
            real = os.path.realpath(cand)
            if real in seen:
                continue
            seen.add(real)
            out = subprocess.run(
                [str(cand), "-version"],
                capture_output=True, text=True, timeout=15,
            ).stderr
            m = re.search(r'version "([^"]+)"', out)
            ver = m.group(1) if m else "?"
            results.append((ver, real))
        except Exception:
            continue
    # 版本排序 (按数字部分降序)
    def sort_key(item):
        try:
            nums = [int(x) for x in re.findall(r"\d+", item[0])][:2]
            while len(nums) < 2:
                nums.append(0)
            return nums
        except Exception:
            return [0, 0]
    results.sort(key=sort_key, reverse=True)
    return results


def java_version_major(version_str: str) -> int:
    try:
        return int(version_str.split(".")[0])
    except ValueError:
        return 8


# ---------------------------------------------------------------------------
# Java 下载
# ---------------------------------------------------------------------------
def _tuna_fallback_url(java_major: int, official_link: str = "",
                       official_name: str = "", arch: str = "x64",
                       os_name: str = "windows"):
    """[FIX-Java下载超时] 清华/华为镜像兜底直链（双镜像，下载时自动切换）。

    优先清华 TUNA；清华 WAF 拦截（403/被拦）时下载层自动切到华为云。
    写死的常用版本（8/17/21）文件名若与官方 API 返回一致则直接命中；
    未写死的大版本尝试用官方 GitHub tag 拼镜像路径（best-effort）。
    """
    fb_tuna = ADOPTIUM_TUNA_FALLBACK.get(java_major)
    if fb_tuna:
        return fb_tuna
    # 未写死的大版本：尝试用官方 API 给的 GitHub tag 拼镜像直链
    if official_link and official_name:
        m = re.search(r"/download/([^/]+)/", official_link)
        if m:
            tag = m.group(1).replace("%2B", "+")
            return f"{ADOPTIUM_MIRROR}{java_major}/jre/{arch}/{tag}/{official_name}"
    return None


def _mirror_candidate_urls(java_major: int, official_link: str = "",
                           official_name: str = "", arch: str = "x64",
                           os_name: str = "windows"):
    """返回「清华 + 华为」两个候选镜像直链（按优先级排序）。

    下载层会依次尝试，第一个失败/被拦自动切下一个，最后都失败才提示手动下载。
    """
    cands = []
    fb_tuna = ADOPTIUM_TUNA_FALLBACK.get(java_major)
    fb_hw = ADOPTIUM_HUAWEI_FALLBACK.get(java_major)
    if fb_tuna:
        cands.append(fb_tuna)
    if fb_hw:
        cands.append(fb_hw)
    if not cands and official_link and official_name:
        m = re.search(r"/download/([^/]+)/", official_link)
        if m:
            tag = m.group(1).replace("%2B", "+")
            cands.append(f"{ADOPTIUM_MIRROR}{java_major}/jre/{arch}/{tag}/{official_name}")
    # 去重保序
    seen = set()
    out = []
    for u in cands:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def get_java_download_url(java_major: int):
    """获取当前系统 Java 下载链接。返回 (urls, 文件名)。

    [FIX-Java下载超时] 国内网络下 api.adoptium.net / GitHub 频繁 10060 超时，
    因此按「清华 + 华为云镜像兜底 -> 官方 API 改写 -> GitHub 直链 -> 明确提示」
    的顺序给出候选直链列表（urls 为 list，下载层依次尝试、自动切换），
    全程捕获超时，最终失败时给出「下载超时，请检查网络或手动下载 Java」。
    """
    os_name = "windows"
    arch = "x64" if SYSTEM_ARCH == "x64" else "x86"
    # Adoptium 的 x86 只有 JRE 8；16/17/21 一般只有 x64
    if java_major > 8:
        arch = "x64"
    image_type = "jre"
    feature = java_major
    api_url = ADOPTIUM_API.format(feature=feature)

    official_link = ""
    official_name = ""
    api_err = None
    # ---- 1) 官方 API（timeout=30），拿 GitHub 直链与文件名 ----
    try:
        data = Net.get_json(api_url, timeout=30)
        for entry in data:
            binfo = entry.get("binary", {})
            if binfo.get("os") == os_name and binfo.get("architecture") == arch \
                    and binfo.get("image_type") == image_type:
                pkg = binfo.get("package", {}) or {}
                official_link = (pkg.get("link") or "").strip()
                official_name = (pkg.get("name") or "").strip()
                break
    except Exception as exc:
        api_err = exc
        print(f"[Java 下载] 官方 Adoptium API 失败: {exc!r}", flush=True)

    # ---- 2) 镜像兜底候选（清华 + 华为云双镜像，下载层自动切换）----
    mirror_cands = _mirror_candidate_urls(java_major, official_link, official_name,
                                          arch, os_name)
    if mirror_cands:
        fname = official_name or mirror_cands[0].rsplit("/", 1)[-1]
        print(f"[Java 下载] 镜像兜底候选 {len(mirror_cands)} 个，优先: {mirror_cands[0]}",
              flush=True)
        return mirror_cands, fname

    # ---- 3) 官方 API 成功但没拿到文件名：回退官方 GitHub 直链 ----
    if official_link:
        fname = official_name or official_link.rsplit("/", 1)[-1]
        print(f"[Java 下载] 使用官方 GitHub 直链: {official_link}", flush=True)
        return [official_link], fname

    # ---- 4) 无可用 URL：给出明确提示 ----
    if api_err is not None:
        msg = ("下载超时，请检查网络或手动下载 Java "
               f"https://mirrors.tuna.tsinghua.edu.cn/Adoptium/ "
               f"（详情: {api_err!r}）")
        raise RuntimeError(msg) from api_err
    raise RuntimeError(
        f"未找到 Java {java_major} 的 {os_name}/{arch} JRE 下载地址；"
        f"可手动下载 https://mirrors.tuna.tsinghua.edu.cn/Adoptium/")


# ---------------------------------------------------------------------------
# Minecraft 版本清单
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# minecraft-launcher-lib 封装 (pip install minecraft-launcher-lib)
# 版本列表 / 安装 / 加载器安装 / 启动命令 全部走这个库
# ---------------------------------------------------------------------------
try:
    # WindowsApps 的 Python(3.12) 有时不在默认 sys.path 里能看到 local-packages，
    # 手动把已知位置加进 sys.path，确保 lib 可导入
    import site as _site
    for _p in [
        r"C:\Users\xp123\AppData\Local\Packages\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\LocalCache\local-packages\Python312\site-packages",
        str(Path(os.path.expandvars(r"%LOCALAPPDATA%")) / "Local" / "Packages"
            / "PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0"
            / "LocalCache" / "local-packages" / "Python312" / "site-packages"),
    ]:
        if os.path.isdir(_p) and _p not in sys.path:
            sys.path.insert(0, _p)
    import minecraft_launcher_lib as mcl
    from minecraft_launcher_lib.utils import (
        get_available_versions as mcl_get_available_versions,
        get_installed_versions as mcl_get_installed_versions,
    )
    from minecraft_launcher_lib.install import install_minecraft_version as mcl_install_minecraft
    from minecraft_launcher_lib.mod_loader import (
        list_mod_loader as mcl_list_mod_loader,
        get_mod_loader as mcl_get_mod_loader,
    )
    from minecraft_launcher_lib.command import get_minecraft_command as mcl_get_command
    MCL_OK = True
    MCL_ERR = ""
except Exception as _e:
    MCL_OK = False
    MCL_ERR = str(_e)

    def _mcl_missing(*a, **k):
        raise RuntimeError("minecraft_launcher_lib 未安装 (pip install minecraft-launcher-lib)")
    mcl_get_available_versions = mcl_get_installed_versions = _mcl_missing
    mcl_install_minecraft = mcl_get_mod_loader = mcl_list_mod_loader = _mcl_missing
    mcl_get_command = _mcl_missing

# lib 已可用时，再正式应用 Mojang 下载镜像回退补丁（需 lib 在 sys.path）
if MCL_OK:
    try:
        mojang_mirror_patch.apply()
    except Exception:
        pass


def _mc_version_sort_key(vid: str):
    """Minecraft 版本排序键。纯 MC 版本按数字倒序；老 alpha/beta 排在最后。

    '1.21.1' -> (2, 1, 21, 1)；'26.3' -> (2, 26, 3)；老 alpha 'rd-20090515' -> (0, ...)
    返回元组，越大越新。
    """
    import re as _re
    m = _re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", vid)
    if m:
        major = int(m.group(1))
        minor = int(m.group(2))
        patch = int(m.group(3)) if m.group(3) else 0
        # 1.x 和 2x 都按 major,minor,patch 排；但 1.21 比 1.8 新
        # 给 1.x 加个偏置让它排在 2x 前面（现代版），老 alpha 沉底
        return (1, major, minor, patch)
    # 非纯数字（snapshot/老 alpha）：给 0 偏置，沉底
    return (0, 0, 0, 0, vid)


def _bmclapi_version_list():
    """BMCLAPI2 版本清单（launchermeta.mojang.com 被墙时的国内回退）。

    返回与 lib get_available_versions 同结构的列表；每项 url 改写为 BMCLAPI 的
    单版本 json 地址，使后续安装/校验也走镜像。失败抛异常。
    """
    import requests as _rq
    s = _rq.Session()
    s.headers["User-Agent"] = "PyMC-Launcher/1.0"
    s.verify = False
    import urllib3
    urllib3.disable_warnings()
    data = s.get("https://bmclapi2.bangbang93.com/minecraft/java/versions", timeout=30).json()
    base = "https://bmclapi2.bangbang93.com/minecraft/java/versions"
    for item in data:
        item["url"] = f"{base}/{item['id']}.json"
    return data


def mcl_version_list():
    """可下载版本列表。返回 [(version_id, type, url), ...]。

    优先用 lib 的 Mojang 官方清单；失败（被墙）时回退 BMCLAPI2 镜像。
    排序：release 在前（按版本倒序），snapshot 在后（按版本倒序），老 alpha/beta 沉底。
    """
    if not MCL_OK:
        return []
    raw = None
    try:
        raw = mcl_get_available_versions(str(MINECRAFT_DIR))
    except Exception as _e:
        print(f"[mcl] 官方清单获取失败，回退 BMCLAPI: {_e}", flush=True)
    if raw is None:
        try:
            raw = _bmclapi_version_list()
        except Exception as _e2:
            print(f"[mcl] BMCLAPI 清单也失败: {_e2}", flush=True)
            return []
    releases, snapshots, others = [], [], []
    for v in raw:
        vid = v.get("id", "")
        vtype = v.get("type", "release")
        url = v.get("url", "")
        if not vid:
            continue
        rec = (vid, vtype, url)
        if vtype == "release":
            releases.append(rec)
        elif vtype == "snapshot":
            snapshots.append(rec)
        else:
            others.append(rec)
    releases.sort(key=lambda t: _mc_version_sort_key(t[0]), reverse=True)
    snapshots.sort(key=lambda t: _mc_version_sort_key(t[0]), reverse=True)
    others.sort(key=lambda t: _mc_version_sort_key(t[0]), reverse=True)
    return releases + snapshots + others


def mcl_installed_versions():
    """已安装版本 id 列表。单版本 json 损坏不影响整体（跳过）。"""
    if not MCL_OK:
        return []
    try:
        raw = mcl_get_installed_versions(str(MINECRAFT_DIR))
        return [v.get("id", "") for v in raw if v.get("id")]
    except Exception:
        # lib 的 get_installed_versions 遇到任何损坏 json 会整表抛异常，
        # 兜底：手动扫描 versions/ 目录，逐文件容错
        import json as _json
        out = []
        vdir = MINECRAFT_DIR / "versions"
        if not vdir.is_dir():
            return []
        for d in sorted(vdir.iterdir()):
            if not d.is_dir():
                continue
            jf = d / (d.name + ".json")
            if not jf.is_file() or jf.stat().st_size == 0:
                continue
            try:
                meta = _json.loads(jf.read_text(encoding="utf-8"))
                out.append(d.name)
            except Exception:
                continue
        return out


def mcl_loader_versions(kind, mc_version, stable_only=False):
    """加载器版本列表 [(loader_ver,), ...]，新版本在前。kind: forge/neoforge/fabric/quilt"""
    if not MCL_OK:
        return []
    loader = mcl_get_mod_loader(kind)
    vers = loader.get_loader_versions(mc_version, stable_only)
    return [(v,) for v in vers]


def mcl_install_version(version_id, java_path=None):
    """安装游戏本体（无进度回调，仅日志）。返回 None。"""
    if not MCL_OK:
        raise RuntimeError("minecraft_launcher_lib 未安装")
    callback = _make_mcl_callback()
    mcl_install_minecraft(version_id, MINECRAFT_DIR, callback)


def mcl_install_loader(kind, mc_version, mc_dir, java_path=None, loader_version=None,
                       progress_cb=None):
    """安装 mod 加载器到指定 MC 版本。返回 lib 报告的版本目录名（可能是复合 id，如 '1.20.1-Fabric 0.18.1'）。

    progress_cb(cur, total, note) 非 None 时把下载进度转发给它（上 Tk）。
    """
    if not MCL_OK:
        raise RuntimeError("minecraft_launcher_lib 未安装")
    loader = mcl_get_mod_loader(kind)
    callback = _make_mcl_callback(progress_cb=progress_cb)
    installed = loader.install(
        mc_version, mc_dir,
        loader_version=loader_version,
        java=str(java_path) if java_path else None,
        callback=callback,
    )
    return installed


def mcl_loaders_installed_in(mc_dir, pure_mc_version):
    """扫描版本目录，列出该纯 MC 版本下已装的加载器版本目录。

    返回 [(dir_name, loader_kind, loader_ver), ...]，按目录名排序。
    复合目录 = dir 里有 inheritsFrom 指向纯版本 json（lib 安装时生成），
    并从目录名里识别加载器：'1.20.1-Fabric 0.18.1' / 'neoforge-21.1.34' / '1.20.1-Forge_47.4.18'。
    """
    import re as _re
    vdir = Path(mc_dir) / "versions"
    out = []
    if not vdir.is_dir():
        return out
    for d in sorted(vdir.iterdir()):
        name = d.name
        jf = d / (name + ".json")
        if not d.is_dir() or not jf.is_file() or jf.stat().st_size == 0:
            continue
        # 解析目录 json 的 inheritsFrom，必须是这个纯 MC 版本
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("inheritsFrom") != pure_mc_version:
            continue
        # 从目录名提取加载器种类与版本
        n = name.lower()
        m = None
        if "fabric" in n:
            m = _re.search(r"fabric[\s_-]*([0-9][0-9a-z.\-]*)", n)
            lk = "fabric"
        elif "neoforge" in n or "neo-forge" in n:
            m = _re.search(r"neoforge[\s_-]*([0-9][0-9a-z.\-]*)", n)
            lk = "neoforge"
        elif "forge" in n:
            m = _re.search(r"forge[\s_-]*([0-9][0-9a-z.\-]*)", n)
            lk = "forge"
        else:
            continue
        out.append((name, lk, (m.group(1) if m else "")))
    return out


def mcl_minecraft_version(mc_version: str) -> str:
    """从 '1.20.1-Fabric 0.18.1' 这种复合 id 里提取纯 MC 版本（'1.20.1'）。

    lib 的 install_minecraft_version / 启动命令都需要纯 MC 版本。
    老版本名（如 'rd-20090515'、'b1.7.3'）无法解析，原样返回。
    """
    # '1.20.1' -> '1.20.1'；'1.20.1-Fabric 0.18.1' -> '1.20.1'；'1.8' -> '1.8'
    m = re.match(r"(\d+\.\d+)(\.\d+)?", mc_version or "")
    return m.group(0) if m else (mc_version or "")


def mcl_loader_installed_version(mc_version: str, kind: str):
    """从复合 id（'1.20.1-Fabric 0.18.1'）里提取加载器版本号。

    kind 不匹配或无法解析时返回 None。
    """
    import re as _re
    low = (mc_version or "").lower()
    if kind == "fabric":
        m = _re.search(r"fabric[\s_\-]*([0-9][0-9a-z.\-]*)", low)
    elif kind == "forge":
        m = _re.search(r"forge[\s_\-]*([0-9][0-9a-z.\-]*)", low)
    elif kind in ("neoforge", "neo"):
        m = _re.search(r"neoforge[\s_\-]*([0-9][0-9a-z.\-]*)", low)
    else:
        return None
    return m.group(1) if m else None


def mcl_pure_mc_version_or_self(version_id: str) -> str:
    """安装本体时传给 lib 的版本 id。

    - 纯 MC 版本（'1.21.1'）：原样返回
    - 复合 id（'1.20.1-Fabric 0.18.1'）：返回纯 MC 版本（lib 只接受纯版本）
    这样下载时版本目录名 = 传给 lib 的 id（'1.21.1' 或 '1.20.1'），
    每个版本都隔离在 versions 目录下对应子文件夹里，互不影响（mod 放对应子目录）。
    """
    return mcl_minecraft_version(version_id)


def mcl_extract_natives(version_id):
    """把指定版本的 natives 解压到版本目录（含 inheritsFrom 继承链）。

    lib 的 get_minecraft_command 会生成 -Djava.library.path=<版本目录>/natives，
    启动前必须确保该目录已解压。
    - 纯版本（'1.21.1'）：解压到 versions/1.21.1/natives/
    - 复合版本（'1.20.1-Fabric 0.18.1'）：lib 启动时按 json 继承链把父版本
      的 natives 一并解压；这里先按复合 id 解压自身，失败不致命。
    老版本（1.8.9 等）若 natives 目录缺失会在 1.8.9 启动时 UnsatisfiedLinkError，
    因此本函数对纯版本也做兜底解压。失败时静默。
    """
    if not MCL_OK:
        return
    from minecraft_launcher_lib.natives import extract_natives
    try:
        target = MINECRAFT_DIR / "versions" / version_id / "natives"
        extract_natives(version_id, MINECRAFT_DIR, target)
    except Exception:
        # 复合版本可能 json 解析失败或老版本无 natives；
        # 若版本 id 是复合（纯版本提取不同），再兜底解压纯版本
        pure = mcl_minecraft_version(version_id)
        if pure and pure != version_id:
            try:
                target2 = MINECRAFT_DIR / "versions" / pure / "natives"
                extract_natives(pure, MINECRAFT_DIR, target2)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 外置登录：authlib-injector 的 -javaagent 参数前缀
# ---------------------------------------------------------------------------
# LittleSkin 等皮肤站「拖拽生成」的字符串形如：
#   authlib-injector:yggdrasil-server:https%3A%2F%2Flittleskin.cn%2Fapi%2Fyggdrasil
# 其中冒号后的部分要 urllib.parse.unquote() 解码才是真正的 API 根地址。
AUTHLIB_URI_PREFIX = "authlib-injector:yggdrasil-server:"


def offline_uuid(username: str) -> str:
    """按 Minecraft 离线模式算法由玩家名生成 UUID（带横杠的标准格式）。

    离线 UUID = MD5("OfflinePlayer:<name>") 前 16 字节，第 7 字节高 4 位置 3、
    第 9 字节高 2 位置 2。这样即使纯拖拽模式没有真实 UUID，也绝不会出现
    全 0 的假值，服务端/注入器看到的都是一个合法 UUID。
    """
    import hashlib
    digest = bytearray(hashlib.md5(
        f"OfflinePlayer:{username or 'Steve'}".encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30     # version 3
    digest[8] = (digest[8] & 0x3F) | 0x80     # variant
    h = digest.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def mcl_build_launch_cmd(version_id, java_path, username, mem_mb, jvm_args_extra=None,
                         auth=None, extra_jvm_args=None, game_dir=None):
    """用 lib 生成启动命令。返回 cmd 列表（含 java 路径，可直接 Popen）。

    version_id: versions/ 目录里的完整版本名（'1.21.1' / '1.20.1-Fabric 0.18.1' 等）。
    jvm_args_extra: 用户自定义 JVM 参数（字符串或列表）。
    auth: 外置登录凭证 dict（None = 纯离线模式）。字段：
            injector      authlib-injector.jar 的【绝对路径】
            server        皮肤站 Yggdrasil API 根地址（-javaagent 里 = 后面那段）
            access_token  真实 accessToken（本协议返回；纯拖拽模式可为占位符）
            uuid          真实 UUID（selectedProfile.id，横杠已去掉）
            username      真实玩家名（selectedProfile.name）
          非 None 时：
            1. JVM 参数【最前面】强制插入 -javaagent:<jar>={<server}>
            2. options["uuid"] / options["token"] / options["username"]
               全部换成真实值，绝不使用全 0 / "0"
    extra_jvm_args: 其它额外 JVM 参数（追加在 -javaagent 之后）。
    game_dir: 【版本隔离】游戏工作目录（--gameDir）。
              None = 用全局 MINECRAFT_DIR（所有版本共享存档/设置）；
              传 versions/<版本名> = 该版本独立的 saves / config / options.txt，
              版本之间互不串台。注意 mcl_get_command 的第二个位置参数仍必须是
              MINECRAFT_DIR（版本 json / libraries / assets 都在那里找），
              只有 options["gameDirectory"] 会被换成 game_dir。
    """
    if not MCL_OK:
        raise RuntimeError("minecraft_launcher_lib 未安装")

    # ---- 1) JVM 参数：-javaagent 必须排在最前面 ----
    jvm_arguments = []
    if auth and auth.get("injector"):
        agent = f"-javaagent:{auth['injector']}"
        if auth.get("server"):
            agent += f"={auth['server']}"
        jvm_arguments.append(agent)
        print(f"[外置登录] 注入 JVM 参数: {agent}", flush=True)
    jvm_arguments += [f"-Xmx{mem_mb}M", f"-Xms{max(512, mem_mb // 2)}M"]
    if jvm_args_extra:
        if jvm_args_extra is not None and isinstance(jvm_args_extra, str):
            jvm_arguments += jvm_args_extra.split()
        elif jvm_args_extra:
            jvm_arguments += list(jvm_args_extra)
    if extra_jvm_args:
        jvm_arguments += list(extra_jvm_args)

    # ---- 2) 账号参数：外置登录时全部用真实值覆盖 ----
    if auth:
        real_name = (auth.get("username") or username or "Steve")
        real_uuid = auth.get("uuid") or offline_uuid(real_name)
        real_token = auth.get("access_token") or "0"
        # 兼容去掉横杠的 UUID：lib 原样填 ${auth_uuid}，注入器两种都认
        print(f"[外置登录] username={real_name} uuid={real_uuid} "
              f"token={str(real_token)[:12]}...", flush=True)
    else:
        real_name = username
        real_uuid = offline_uuid(username)
        real_token = "0"

    # ---- 3) 游戏工作目录：版本隔离的核心（--gameDir）----
    work_dir = Path(game_dir) if game_dir else MINECRAFT_DIR
    if game_dir:
        print(f"[版本隔离] --gameDir = {work_dir}", flush=True)

    options = {
        "username": real_name,
        "uuid": real_uuid,
        "token": real_token,
        "executablePath": str(java_path),
        "defaultExecutablePath": str(java_path),
        "jvmArguments": jvm_arguments,
        "gameDirectory": str(work_dir),
        "launcherName": "PyMC-Launcher",
        "launcherVersion": "1.0",
    }
    # 第二个位置参数必须是「装版本/libraries/assets 的根目录」，不能换成版本目录
    cmd = mcl_get_command(version_id, str(MINECRAFT_DIR), options)
    return cmd


def _make_mcl_callback(status_note="", progress_cb=None):
    """构造 mcl 回调 dict（线程安全）。

    progress_cb(cur, total, note) 非 None 时把进度转发给它（用于上 Tk）。
    """
    state = {"max": 0, "cur": 0, "note": ""}

    def set_status(s):
        state["note"] = s
        print(f"[安装{status_note}] {s}", flush=True)
        if progress_cb:
            progress_cb(state["cur"], state["max"], s)

    def set_progress(p):
        state["cur"] = p
        if progress_cb:
            progress_cb(p, state["max"], state["note"])

    def set_max(m):
        state["max"] = m
        if progress_cb:
            progress_cb(state["cur"], m, state["note"])

    return {"setStatus": set_status, "setProgress": set_progress, "setMax": set_max}


def _version_json_path(version_id):
    """返回版本目录里的 json 路径，若存在。"""
    p = MINECRAFT_DIR / "versions" / version_id / f"{version_id}.json"
    return p if p.is_file() else None


def _java_required_by_version_json(version_id):
    """从版本 json（含 inheritsFrom 继承链）读 javaVersion.majorVersion。读不到返回 None。

    新 MC(26.x) 的 client json 里 javaVersion 才准确（如 26.1.2 需 Java 25），
    mc_to_java_major 的字符串推断对新型号可能偏低。
    """
    import json as _json

    def _read_vid(vid):
        p = _version_json_path(vid)
        if not p:
            return None
        try:
            return _json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    # 沿 inheritsFrom 链向上找，直到找到带 javaVersion 的层
    seen = set()
    cur = version_id
    while cur and cur not in seen:
        seen.add(cur)
        data = _read_vid(cur)
        if not data:
            break
        jv = data.get("javaVersion")
        if isinstance(jv, dict) and jv.get("majorVersion"):
            return int(jv["majorVersion"])
        cur = data.get("inheritsFrom")
    return None


def _required_java_major(version_id):
    """该版本需要的最低 Java 大版本：优先读版本 json（含继承链），回退字符串推断。"""
    j = _java_required_by_version_json(version_id)
    if j:
        return j
    return mc_to_java_major(version_id)


# 旧接口（保留，但底层已废弃）
def list_minecraft_versions():
    return mcl_version_list()

def installed_versions():
    return mcl_installed_versions()


def _version_json_data(version_id):
    """读版本 json 内容（dict）。找不到 / 解析失败返回 None。"""
    import json as _json
    p = _version_json_path(version_id)
    if not p:
        return None
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _version_inherits_from(version_id):
    """读版本 json 的 inheritsFrom 字段（父级版本）。读不到返回 ''。

    克隆实例（如 测试3）的 json 常带 inheritsFrom: "1.20.1"，
    表示它的核心 jar 实际属于父级版本 1.20.1。
    """
    try:
        data = _version_json_data(version_id)
        if isinstance(data, dict):
            return str(data.get("inheritsFrom") or "")
    except Exception:
        pass
    return ""


def _instance_base_version(version_id):
    """解析某个实例真正「拥有核心 jar / 需要下载」的版本号。

    优先级：
      1. 版本 json 的 inheritsFrom（克隆实例：测试3 -> 1.20.1）
      2. 复合 id 正则提取（1.20.1-Forge_47.4.18 -> 1.20.1）
      3. 原样返回（本身已是纯版本号 / 无法解析）
    补全时要用它来下载本体，而不是被重命名的实例名。
    """
    parent = _version_inherits_from(version_id)
    if parent:
        return parent
    pure = mcl_minecraft_version(version_id)
    return pure or version_id


def check_version_complete(version_id) -> tuple:
    """检查某版本是否已完整下载。返回 (是否完整, 缺失列表)。

    完整 = 有 json + 核心 jar；libraries/assets 若缺失会再次补充下载。
    核心 jar 不再死板地拼 ${version_id}.jar：
      1. 若该版本目录里存在任意 .jar（克隆实例的 jar 保留父级名，如 1.20.1.jar），
         则算有核心；
      2. 否则读 json 的 inheritsFrom，若父级版本目录里的父级 .jar 存在，也算完整。
    """
    missing = []
    vdir = VERSIONS_DIR / version_id

    def _dir_has_json():
        return vdir.is_dir() and any(
            f.is_file() and f.suffix.lower() == ".json" for f in vdir.iterdir())

    if not (vdir / f"{version_id}.json").is_file() and not _dir_has_json():
        missing.append(f"版本信息 {version_id}.json")

    # 核心 jar：同名字 > 目录里任意 jar > inheritsFrom 父级 jar
    jar_ok = False
    if vdir.is_dir():
        jar_ok = (vdir / f"{version_id}.jar").is_file() or any(
            f.is_file() and f.suffix.lower() == ".jar" for f in vdir.iterdir())
    if not jar_ok:
        parent = _version_inherits_from(version_id)
        if parent and parent != version_id:
            jar_ok = (VERSIONS_DIR / parent / f"{parent}.jar").is_file()
    if not jar_ok:
        missing.append(f"核心程序 {version_id}.jar")

    return (not missing, missing)


# ---------------------------------------------------------------------------
# Minecraft 下载 (核心 jar / libraries / assets)
# ---------------------------------------------------------------------------
def download_minecraft_version(version_id, version_json_url, progress_cb, log_cb):
    """下载指定版本的 jar、libraries、assets。返回 version 数据。"""
    log_cb(f"获取版本信息: {version_id}")
    meta = Net.get_json(version_json_url, timeout=60)

    vdir = VERSIONS_DIR / version_id
    vdir.mkdir(parents=True, exist_ok=True)
    # 保存 json
    (vdir / f"{version_id}.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8")

    tasks = []  # (desc, url, dest)

    # 1) 客户端 jar
    client = meta.get("downloads", {}).get("client", {})
    if client and client.get("url"):
        tasks.append(("client.jar", client["url"],
                      vdir / f"{version_id}.jar"))

    # 2) 各类 libraries
    libs = meta.get("libraries", [])
    for lib in libs:
        if not _lib_applies(lib, meta):
            continue
        artifact = lib.get("downloads", {}).get("artifact")
        if artifact and artifact.get("url") and artifact.get("path"):
            tasks.append(("library", artifact["url"],
                          LIBRARIES_DIR / artifact["path"]))
        # natives (lwjgl 等)
        for key, dl in lib.get("downloads", {}).get("classifiers", {}).items():
            if "natives" in key and dl.get("url") and dl.get("path"):
                tasks.append(("natives", dl["url"],
                              LIBRARIES_DIR / dl["path"]))

    # 3) assets (索引 + 资源)
    asset_index = meta.get("assetIndex", {})
    if asset_index and asset_index.get("url"):
        idx_url = asset_index["url"]
        idx_id = asset_index.get("id", "legacy")
        idx_dest = ASSETS_DIR / "indexes" / f"{idx_id}.json"
        if not idx_dest.exists():
            tasks.append(("asset-index", idx_url, idx_dest))

    # 执行下载：核心文件(客户端 jar + 索引)先下，libraries 与 assets 并发下载
    log_cb(f"共 {len(tasks)} 个文件待下载 (libraries/assets 将并发加速)")
    core = [t for t in tasks if t[0] in ("client.jar", "asset-index")]
    rest = [t for t in tasks if t not in core]
    done_count = 0
    total_count = len(tasks)  # 基础任务数

    def _finish_one(desc, dest, ok=True):
        nonlocal done_count
        done_count += 1
        note = f"[{done_count}/{total_count}] {'OK' if ok else 'X'} {desc}: {dest.name}"
        progress_cb(done_count, total_count, note)

    # 1) 核心文件 (串行，快)
    for desc, url, dest in core:
        if dest.exists() and dest.stat().st_size > 0:
            _finish_one(desc, dest, True)
            continue
        try:
            Net.download(url, dest, timeout=180)
            _finish_one(desc, dest, True)
        except Exception as e:
            log_cb(f"下载失败 {desc} {dest.name}: {e}")
            _finish_one(desc, dest, False)

    # 2) libraries 并发下载
    lib_tasks = [t for t in rest if t[0] in ("library", "natives")]
    if lib_tasks:
        workers = min(8, len(lib_tasks))
        def dl_one(task):
            desc, url, dest = task
            if dest.exists() and dest.stat().st_size > 0:
                return (desc, dest, True)
            try:
                Net.download(url, dest, timeout=180)
                return (desc, dest, True)
            except Exception as e:
                log_cb(f"下载失败 {desc} {dest.name}: {e}")
                return (desc, dest, False)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for desc, dest, ok in ex.map(dl_one, lib_tasks):
                _finish_one(desc, dest, ok)

    # 3) assets 对象 (并发下载，独立进度)
    idx_dest = ASSETS_DIR / "indexes" / f"{asset_index.get('id', 'legacy')}.json"
    if idx_dest.exists():
        # 统计 assets 数量并单独计算进度
        try:
            idx = json.loads(idx_dest.read_text(encoding="utf-8"))
            asset_objects = idx.get("objects", {})
            asset_count = len([o for o in asset_objects.values() if o.get("hash")])
        except Exception:
            asset_count = 0
        overall_total = total_count + asset_count
        log_cb(f"还有 {asset_count} 个 assets 资源待下载 (并发中...)")
        progress_cb(total_count, overall_total, f"下载 assets ({asset_count} 个)...")
        _download_assets(idx_dest, progress_cb, log_cb, base_done=total_count, total=overall_total)
        done_count = overall_total

    progress_cb(done_count, done_count, "完成")
    log_cb(f"版本 {version_id} 下载完成")
    return meta


def _lib_applies(lib, meta):
    """判断 library 是否适用于当前平台 (rules 处理)。"""
    rules = lib.get("rules")
    if not rules:
        return True
    arch = {"32": "x86", "64": "x86_64"}.get(platform.architecture()[0], "x86_64")
    os_name = "windows"
    allowed = False
    for rule in rules:
        action = rule.get("action")
        os_r = rule.get("os", {})
        match_os = (os_r.get("name") == os_name) if "name" in os_r else True
        if "arch" in os_r:
            match_arch = os_r["arch"] == arch
        else:
            match_arch = True
        match = match_os and match_arch
        if action == "allow" and match:
            allowed = True
        elif action == "disallow" and match:
            allowed = False
    return allowed


def _download_assets(idx_dest, progress_cb, log_cb, base_done=0, total=0):
    """根据 assets 索引并发下载 object 资源 (到 objects/xx/xxx)。"""
    try:
        idx = json.loads(idx_dest.read_text(encoding="utf-8"))
    except Exception:
        return
    objects = idx.get("objects", {})
    items = [(name, obj.get("hash")) for name, obj in objects.items() if obj.get("hash")]
    total_assets = len(items)
    if total_assets == 0:
        return
    done = 0
    lock = threading.Lock()

    def dl_one(item):
        nonlocal done
        _name, hash_v = item
        obj_dest = ASSETS_DIR / "objects" / hash_v[:2] / hash_v
        if obj_dest.exists():
            return True
        url = f"https://resources.download.minecraft.net/{hash_v[:2]}/{hash_v}"
        try:
            Net.download(url, obj_dest, timeout=60)
            return True
        except Exception:
            return False

    workers = min(16, total_assets)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for ok in ex.map(dl_one, items):
            with lock:
                done += 1
            if done % 200 == 0 or done == total_assets:
                progress_cb(base_done + done, total, f"assets {done}/{total_assets}")


# ---------------------------------------------------------------------------
# 启动 Minecraft
# ---------------------------------------------------------------------------
def _has_module_info(jar_path: Path) -> bool:
    """检查 jar 是否包含 module-info.class（是命名模块）。"""
    try:
        import zipfile
        with zipfile.ZipFile(jar_path) as z:
            return "module-info.class" in z.namelist()
    except Exception:
        return False


def _get_module_name(jar_path: Path) -> str | None:
    """从 jar 的 module-info.class 中提取模块名。返回 None 如果没有 module-info。"""
    try:
        import zipfile
        with zipfile.ZipFile(jar_path) as z:
            data = z.read("module-info.class")
    except (KeyError, FileNotFoundError):
        return None
    import re
    # 在 class 文件字节流中搜索 UTF-8 字符串，找出模块名
    texts = re.findall(rb"[\x20-\x7e]{4,}", data)
    for t in texts:
        s = t.decode("ascii", errors="replace")
        # 模块名格式：小写字母、数字、点、下划线、连字符组成的标识符序列
        if s.count(".") >= 2 and not s.startswith("/") and " " not in s and "$" not in s:
            parts = s.split(".")
            if all(p.replace("-","").replace("_","").isalnum() for p in parts):
                return s
    return None


def build_launch_args(version_id, java_path, username, uuid,
                      access_token, jvm_args, mem_mb, language=None, progress_cb=None, log_cb=None):
    """构造并返回 (命令列表, natives目录)。"""
    vjson = VERSIONS_DIR / version_id / f"{version_id}.json"
    meta = json.loads(vjson.read_text(encoding="utf-8"))

    mc_dir = MINECRAFT_DIR
    natives = NATIVES_DIR / version_id
    natives.mkdir(parents=True, exist_ok=True)

    # 解压 natives
    for lib in meta.get("libraries", []):
        for key, dl in lib.get("downloads", {}).get("classifiers", {}).items():
            if "natives" in key and dl.get("path"):
                src = LIBRARIES_DIR / dl["path"]
                if src.exists():
                    try:
                        with zipfile.ZipFile(src) as z:
                            z.extractall(natives)
                    except Exception:
                        pass

    # 判断是否为 Mod Loader 版本
    is_mod_loader = any(
        kw in meta.get("mainClass", "")
        for kw in ("bootstraplauncher", "ForgeLauncher", "net.minecraftforge", "net.fabricmc")
    )

    # 收集所有应用到的库 jar 路径
    all_lib_paths = []  # 已解析的 (Path, 名称小写) 列表，用于去重
    for lib in meta.get("libraries", []):
        if not _lib_applies(lib, meta):
            continue
        artifact = lib.get("downloads", {}).get("artifact")
        if artifact and artifact.get("path"):
            p = LIBRARIES_DIR / artifact["path"]
            if p.exists():
                all_lib_paths.append(p)
        else:
            # Fabric Loader 等库没有 downloads.artifact，需要手动构建路径
            name = lib.get("name", "")
            if ":" in name:
                parts = name.split(":")
                if len(parts) >= 3:
                    group, artifact_id, version = parts[0], parts[1], parts[2]
                    candidate = LIBRARIES_DIR / group.replace(".", "/") / artifact_id / version / f"{artifact_id}-{version}.jar"
                    if candidate.exists():
                        all_lib_paths.append(candidate)
                    else:
                        for cls in ["sources", "javadoc"]:
                            candidate2 = LIBRARIES_DIR / group.replace(".", "/") / artifact_id / version / f"{artifact_id}-{version}-{cls}.jar"
                            if candidate2.exists():
                                all_lib_paths.append(candidate2)
                                break

    # 判断是否为 Forge/NeoForge（bootstraplauncher 主类）→ 用模块路径
    main_class = meta.get("mainClass", "")
    is_forge_bootstrap = "bootstraplauncher" in main_class.lower()

    # 模块路径 jar：只放关键模块（bootstraplauncher/securejarhandler/asm*/JarJar*）
    # 不放 modlauncher/eventbus/coremods/mixin/nashorn，避免触发它们对
    # jopt/log4j/slf4j 等非模块化库的命名模块 requires（这些库没有 module-info）
    module_path = ""
    if is_forge_bootstrap:
        MP_ALLOWED = ("bootstraplauncher", "securejarhandler", "JarJar", "asm")
        mp_jars = []
        for p in all_lib_paths:
            if not _has_module_info(p):
                continue
            name = p.name.lower()
            if any(k.lower() in name for k in MP_ALLOWED):
                if name not in [q.name.lower() for q in mp_jars]:
                    mp_jars.append(p)
        module_path = os.pathsep.join(str(p) for p in mp_jars)
        # 模块路径 jar 从 classpath 剔除，避免"more than one module named"冲突
        mp_set = {str(p) for p in mp_jars}
    else:
        mp_set = set()

    # classpath（库 jar - 模块路径 jar + 客户端主 jar）
    cp = [str(p) for p in all_lib_paths if str(p) not in mp_set]
    cp.append(str(VERSIONS_DIR / version_id / f"{version_id}.jar"))
    classpath = os.pathsep.join(cp)

    asset_index_id = meta.get("assetIndex", {}).get("id", "legacy")
    asset_index = ASSETS_DIR / "indexes" / f"{asset_index_id}.json"
    assets_root = ASSETS_DIR / "virtual" / "legacy" if asset_index_id == "legacy" else ASSETS_DIR

    # 默认参数模板
    args = meta.get("arguments", {}).get("game", []) or meta.get("minecraftArguments", "")
    game_args = []
    if isinstance(args, list):
        for a in args:
            if isinstance(a, str):
                game_args.append(_expand(a, version_id, mc_dir, assets_root, asset_index_id,
                                          username, uuid, access_token, natives, language))
    else:
        for tok in args.split(" "):
            if tok.strip():
                game_args.append(_expand(tok, version_id, mc_dir, assets_root, asset_index_id,
                                         username, uuid, access_token, natives))

    cmd = [
        str(java_path),
        *(["-Xms{}M".format(mem_mb // 2)] if mem_mb else []),
        *(["-Xmx{}M".format(mem_mb)] if mem_mb else []),
        *jvm_args.split(),
    ]
    if module_path:
        # PCL 风格：模块路径只放少量关键 jar（避免触发 requires 解析失败）
        # 策略：只放 bootstraplauncher + securejarhandler + asm + JarJar*（有 module-info 的）
        # 其余（modlauncher/eventbus/coremods/mixin 等）放 classpath，module-info 被忽略
        main_class = meta.get("mainClass", "")
        if "knot" in main_class.lower() or "fabric" in main_class.lower():
            # Fabric Knot 不用模块路径
            cmd.extend([
                "--add-opens=java.base/java.lang=ALL-UNNAMED",
                "--add-opens=java.base/java.lang.invoke=ALL-UNNAMED",
                "-Djava.library.path={}".format(natives),
                "-cp", classpath,
                main_class,
            ])
            cmd.extend(a for a in game_args if a)
            if language:
                cmd.append(f"--language {language}")
            return cmd, natives
        # NeoForge/Forge：PCL 的 8-jar 模块路径
        cmd.extend([
            "-Djava.library.path={}".format(natives),
            "-Djna.tmpdir={}".format(natives),
            "-Dorg.lwjgl.system.SharedLibraryExtractPath={}".format(natives),
            "-Dio.netty.native.workdir={}".format(natives),
            "-Dminecraft.launcher.brand=PyMC",
            "-Dfml.ignoreInvalidMinecraftCertificates=true",
            "-Dfml.ignorePatchDiscrepancies=true",
            "-Dlog4j2.formatMsgNoLookups=true",
            "-Dstdout.encoding=utf-8",
            "-Dstderr.encoding=utf-8",
            "-DlibraryDirectory={}".format(LIBRARIES_DIR),
            "-DignoreList=client-extra,{}.jar".format(version_id),
            "-cp", classpath,
            "-p", module_path,
            "--add-modules", "ALL-MODULE-PATH",
            "--add-opens", "java.base/java.util.jar=cpw.mods.securejarhandler",
            "--add-opens", "java.base/java.lang.invoke=cpw.mods.securejarhandler",
            "--add-exports", "java.base/sun.security.util=cpw.mods.securejarhandler",
        ])
        cmd.append(main_class)
        cmd.extend(a for a in game_args if a)
        if language:
            cmd.append(f"--language {language}")
        return cmd, natives
    else:
        # 普通版本：直接使用 classpath
        cmd.extend([
            "--add-opens=java.base/java.lang=ALL-UNNAMED",
            "--add-opens=java.base/java.lang.invoke=ALL-UNNAMED",
            "--add-opens=java.base/java.io=ALL-UNNAMED",
            "--add-opens=java.base/java.util=ALL-UNNAMED",
            "-Djava.library.path={}".format(natives),
            "-cp", classpath,
            meta.get("mainClass", "net.minecraft.client.main.Main"),
        ])
    cmd.extend(a for a in game_args if a)
    # 添加语言参数（只加一次，追加到末尾）
    if language:
        cmd.append(f"--language {language}")
    return cmd, natives


def _expand(arg, version, mc_dir, assets_root, asset_index_id,
            username, uuid, access_token, natives, language=None):
    subs = {
        "${version_name}": version,
        "${game_directory}": str(mc_dir),
        "${assets_root}": str(assets_root),
        "${assets_index_name}": asset_index_id,
        "${auth_player_name}": username,
        "${auth_uuid}": uuid,
        "${auth_access_token}": access_token,
        "${clientid}": "PyMC-Launcher",
        "${auth_xuid}": "",
        "${user_type}": "msa" if access_token != "0" else "legacy",
        "${version_type}": "release",
        "${natives_directory}": str(natives),
        "${launcher_name}": "PyMC",
        "${launcher_version}": "1.0",
        "${classpath}": "",
        "${user_properties}": "{}",
    }
    for k, v in subs.items():
        arg = arg.replace(k, v)
    return arg


# ---------------------------------------------------------------------------
# 图形界面
# ---------------------------------------------------------------------------
class LauncherApp:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.geometry("760x560")
        root.minsize(680, 500)
        try:
            root.iconphoto(True, PhotoImage(file=str(BASE_DIR / "icon.png")))
        except Exception:
            pass

        # 读取配置并应用路径
        self.config = load_config()
        apply_paths(self.config)

        self.versions = []          # (显示名, 类型, id, url)
        self.installed = []     # 已安装版本列表 (有 jar 的文件夹名)
        self.detected_java = []     # [(版本, 路径)]
        self.busy = False
        self._java_download_in_progress = False

        self.username_var = StringVar(value=self.config.get("username", "Steve"))
        self.mem_var = IntVar(value=self.config.get("mem", 2048))
        self.auto_mem_var = BooleanVar(value=True)
        self.jvm_var = StringVar(value=self.config.get("jvm", ""))
        self.game_dir_var = StringVar(value=str(MINECRAFT_DIR))
        self.java_dir_var = StringVar(value=str(JAVA_DIR))
        self.java_var = StringVar()
        self.offline_var = BooleanVar(value=True)
        # 语言选择
        lang = self.config.get("language", "zh_CN")
        self.language_var = StringVar(value=lang)
        # 多目录管理
        self.game_dirs_list = self.config.get("game_dirs", [str(DEFAULT_MINECRAFT_DIR)])
        if not isinstance(self.game_dirs_list, list) or len(self.game_dirs_list) == 0:
            self.game_dirs_list = [str(DEFAULT_MINECRAFT_DIR)]
        self.current_dir_idx = 0  # 当前激活的目录索引

        self._build_ui()
        # 初始化目录下拉框
        self._refresh_dir_combo()
        # 启动时后台加载
        self.after_safe(200, self.refresh_all)

    def after_safe(self, ms, fn):
        self.root.after(ms, fn)

    def _save_settings(self):
        """把当前界面设置保存到配置文件。"""
        self.config.update({
            "game_dirs": self.game_dirs_list,
            "java_dir": self.java_dir_var.get(),
            "username": self.username_var.get(),
            "mem": self.mem_var.get(),
            "jvm": self.jvm_var.get(),
            "language": self.language_var.get(),
        })
        save_config(self.config)

    def _apply_auto_mem(self):
        """勾选/取消「自动分配」时同步内存框数值。"""
        if self.auto_mem_var.get():
            self.mem_var.set(auto_memory_mb())
        # 若取消勾选，保留当前框内数值不动，用户可手动调整

    def _pick_game_dir(self):
        d = filedialog.askdirectory(title="选择 Minecraft 游戏目录 (.minecraft)", initialdir=self.game_dir_var.get())
        if d:
            self.game_dir_var.set(d)
            self._save_settings()
            # 应用路径并刷新已装版本
            apply_paths(self.config)
            self.installed = installed_versions()
            self.after_safe(300, self._refresh_versions_bg)

    def _add_game_dir(self):
        """添加一个新的游戏目录。"""
        try:
            initial = str(self.game_dirs_list[self.current_dir_idx]) if self.game_dirs_list else str(DEFAULT_MINECRAFT_DIR)
            d = filedialog.askdirectory(
                title="选择 Minecraft 游戏目录",
                parent=self.root,
                initialdir=initial
            )
            if d:
                d = str(d)
                if d not in self.game_dirs_list:
                    self.game_dirs_list.append(d)
                    self._save_settings()
                    self._refresh_dir_combo()
                    self._log(f"已添加: {d}")
                else:
                    messagebox.showinfo("提示", f"目录已存在:\n{d}")
            else:
                self._log("已取消选择")
        except Exception as e:
            messagebox.showerror("错误", f"添加目录失败:\n{e}")
            self._log(f"添加目录异常: {e}")

    def _remove_game_dir(self):
        """移除当前选中的游戏目录（至少保留一个）。"""
        if len(self.game_dirs_list) <= 1:
            messagebox.showwarning("提示", "至少需要保留一个游戏目录")
            return
        try:
            idx = self.dir_combo.current()
            if idx < 0:
                idx = self.current_dir_idx
            del self.game_dirs_list[idx]
            if idx >= len(self.game_dirs_list):
                idx = len(self.game_dirs_list) - 1
            self.current_dir_idx = idx
            self._save_settings()
            apply_paths(self.config)
            self._refresh_dir_combo()
            self.installed = installed_versions()
            self.after_safe(300, self._refresh_versions_bg)
        except Exception as e:
            messagebox.showerror("错误", f"移除目录失败:\n{e}")

    def _switch_dir(self, event=None):
        """切换当前使用的游戏目录。"""
        try:
            idx = self.dir_combo.current()
            if idx < 0:
                return
            selected = self.game_dirs_list[idx]
            self._log(f"切换目录: [{idx}] {selected}")
            # 把选中的目录移到第一位
            self.game_dirs_list.pop(idx)
            self.game_dirs_list.insert(0, selected)
            self.current_dir_idx = 0
            # 保存并应用
            self._save_settings()
            apply_paths(self.config)
            self._refresh_dir_combo()
            # 刷新版本列表
            self.installed = installed_versions()
            self._log(f"扫描到 {len(self.installed)} 个版本")
            self._refresh_versions_bg()
        except Exception as e:
            self._log(f"切换目录失败: {e}")
            import traceback; traceback.print_exc()

    def _refresh_dir_combo(self):
        """刷新目录选择下拉框。"""
        try:
            vals = [f"[{i+1}] {d}" for i, d in enumerate(self.game_dirs_list)]
            self.dir_combo["values"] = vals
            if vals:
                idx = min(self.current_dir_idx, len(vals) - 1)
                self.dir_combo.set(vals[idx])
        except Exception as e:
            self._log(f"刷新目录列表失败: {e}")

    def _pick_java_dir(self):
        d = filedialog.askdirectory(title="选择 Java 下载/存放目录", initialdir=self.java_dir_var.get())
        if d:
            self.java_dir_var.set(d)
            self._save_settings()
            apply_paths(self.config)
            self.after_safe(200, self.refresh_java)

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        self.style = ttk.Style()
        try:
            self.style.theme_use("vista")
        except Exception:
            pass

        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        # ===== 顶部：账号与设置 =====
        acc = ttk.LabelFrame(main, text="账号与设置")
        acc.pack(fill="x", **pad)
        acc.columnconfigure(1, weight=1)

        ttk.Label(acc, text="玩家名").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(acc, textvariable=self.username_var).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Checkbutton(acc, text="离线模式", variable=self.offline_var).grid(
            row=0, column=2, sticky="w", **pad)

        ttk.Label(acc, text="游戏语言").grid(row=1, column=0, sticky="w", **pad)
        lang_opts = [("简体中文", "zh_CN"), ("English", "en_US"), ("Deutsch", "de_DE"),
                     ("Français", "fr_FR"), ("Español", "es_ES"), ("日本語", "ja_JP")]
        self.lang_combo = ttk.Combobox(acc, textvariable=self.language_var,
                                       values=[l[0] for l in lang_opts], state="readonly")
        self.lang_combo.grid(row=1, column=1, sticky="ew", **pad)
        self.lang_combo.bind("<<ComboboxSelected>>", lambda e: self._save_settings())

        ttk.Label(acc, text="JVM 参数").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(acc, textvariable=self.jvm_var).grid(row=2, column=1, sticky="ew", **pad)

        # ===== 中间：Notebook 分页 =====
        self.notebook = ttk.Notebook(main)
        self.notebook.pack(fill="both", expand=True, **pad)

        # --- 选项卡1: 版本列表 (已安装) ---
        self.ver_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.ver_tab, text="[1] 版本列表")
        self._build_version_tab(self.ver_tab, pad)

        # --- 选项卡2: 下载管理 ---
        self.dl_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.dl_tab, text="[2] 下载管理")
        self._build_download_tab(self.dl_tab, pad)

        # --- 选项卡3: Mod资源下载 (mod/光影/mod加载器) ---
        self.res_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.res_tab, text="[3] Mod资源")
        self._build_resource_tab(self.res_tab, pad)

        # --- 选项卡4: 整合包下载 ---
        self.pk_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.pk_tab, text="[4] 整合包")
        self._build_modpack_tab(self.pk_tab, pad)

        # ===== 底部：Java + 目录 + 内存 =====
        bottom = ttk.Frame(main)
        bottom.pack(fill="x", **pad)

        # Java 设置
        java_f = ttk.LabelFrame(bottom, text="Java (运行时)")
        java_f.pack(side="left", fill="x", expand=True, **pad)
        java_f.columnconfigure(1, weight=1)
        ttk.Label(java_f, text="Java 版本").grid(row=0, column=0, sticky="w", **pad)
        self.java_combo = ttk.Combobox(java_f, textvariable=self.java_var, state="readonly")
        self.java_combo.grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(java_f, text="重新检测", command=self.refresh_java).grid(row=0, column=2, **pad)
        self.java_status = ttk.Label(java_f, text="检测中...", foreground="#888")
        self.java_status.grid(row=1, column=0, columnspan=3, sticky="w", **pad)
        ttk.Button(java_f, text="下载 Java", command=self.download_java).grid(row=2, column=0, columnspan=3, sticky="w", **pad)

        # 目录管理
        dir_f = ttk.LabelFrame(bottom, text="游戏目录")
        dir_f.pack(side="left", fill="x", expand=True, **pad)
        dir_f.columnconfigure(1, weight=1)
        ttk.Label(dir_f, text="当前目录").grid(row=0, column=0, sticky="w", **pad)
        self.dir_combo = ttk.Combobox(dir_f, state="readonly")
        self.dir_combo.grid(row=0, column=1, sticky="ew", **pad)
        self.dir_combo.bind("<<ComboboxSelected>>", self._switch_dir)
        ttk.Button(dir_f, text="添加", command=self._add_game_dir).grid(row=0, column=2, **pad)
        ttk.Button(dir_f, text="移除", command=self._remove_game_dir).grid(row=0, column=3, **pad)
        ttk.Label(dir_f, text="Java下载目录").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(dir_f, textvariable=self.java_dir_var).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(dir_f, text="浏览…", command=self._pick_java_dir).grid(row=1, column=2, **pad)

        # 内存设置
        mem_f = ttk.LabelFrame(bottom, text="运行设置")
        mem_f.pack(side="left", fill="x", expand=True, **pad)
        mem_f.columnconfigure(1, weight=1)
        ttk.Label(mem_f, text="内存 (MB)").grid(row=0, column=0, sticky="w", **pad)
        ttk.Spinbox(mem_f, from_=512, to=16384, increment=256,
                    textvariable=self.mem_var).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Checkbutton(mem_f, text="自动", variable=self.auto_mem_var,
                        command=self._apply_auto_mem).grid(row=0, column=2, sticky="w", **pad)
        ttk.Label(mem_f, text=f"(系统:{auto_memory_mb()}MB)",
                  foreground="#888").grid(row=0, column=3, sticky="w", **pad)

        # ===== 底部进度/状态 (日志已改为控制台输出) =====
        self.prog_frame = ttk.LabelFrame(main, text="操作状态")
        self.prog_frame.pack(fill="x", side="bottom", **pad)
        self.progress = ttk.Progressbar(self.prog_frame, maximum=100)
        self.progress.pack(fill="x", **pad)
        self.status = ttk.Label(self.prog_frame, text="就绪 (详细日志请查看控制台)",
                                foreground="#333", wraplength=640)
        self.status.pack(fill="x", **pad)

    def _build_version_tab(self, parent, pad):
        """构建版本列表选项卡 — 显示已安装版本，可直接启动"""
        ttk.Label(parent, text="已安装版本 (双击或点启动)").pack(anchor="w", **pad)
        # 版本列表 Treeview
        cols = ("version", "type", "status")
        self.ver_tree = ttk.Treeview(parent, columns=cols, show="headings", height=12)
        self.ver_tree.heading("version", text="版本号")
        self.ver_tree.heading("type", text="类型")
        self.ver_tree.heading("status", text="状态")
        self.ver_tree.column("version", width=150)
        self.ver_tree.column("type", width=80)
        self.ver_tree.column("status", width=100)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.ver_tree.yview)
        self.ver_tree.configure(yscrollcommand=scrollbar.set)
        self.ver_tree.pack(side="left", fill="both", expand=True, **pad)
        scrollbar.pack(side="right", fill="y", **pad)
        self.ver_tree.bind("<Double-1>", lambda e: self.launch())
        self.ver_tree.bind("<<TreeviewSelect>>", self._on_ver_tree_select)
        # 按钮行：启动 + 校验补全
        btn_f = ttk.Frame(parent)
        btn_f.pack(fill="x", **pad)
        ttk.Button(btn_f, text="启动所选版本", command=self.launch).pack(side="left", **pad)
        ttk.Button(btn_f, text="校验/补全所选版本", command=self._repair_selected).pack(side="left", **pad)

    def _build_download_tab(self, parent, pad):
        """构建下载管理选项卡 — 浏览可下载版本"""
        top = ttk.Frame(parent)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="可下载版本").pack(side="left")
        ttk.Button(top, text="刷新列表", command=self._refresh_dl_list).pack(side="left", **pad)
        ttk.Label(top, text="筛选:", anchor="w").pack(side="left", **pad)
        self.dl_filter_var = StringVar(value="release")
        ttk.Radiobutton(top, text="Release", variable=self.dl_filter_var,
                        value="release", command=self._refresh_dl_list).pack(side="left")
        ttk.Radiobutton(top, text="Snapshot", variable=self.dl_filter_var,
                        value="snapshot", command=self._refresh_dl_list).pack(side="left")
        # 下载列表 Treeview
        dcols = ("version", "type", "status")
        self.dl_tree = ttk.Treeview(parent, columns=dcols, show="headings", height=18)
        self.dl_tree.heading("version", text="版本号")
        self.dl_tree.heading("type", text="类型")
        self.dl_tree.heading("status", text="状态")
        self.dl_tree.column("version", width=150)
        self.dl_tree.column("type", width=80)
        self.dl_tree.column("status", width=100)
        dl_scroll = ttk.Scrollbar(parent, orient="vertical", command=self.dl_tree.yview)
        self.dl_tree.configure(yscrollcommand=dl_scroll.set)
        self.dl_tree.pack(side="left", fill="both", expand=True, **pad)
        dl_scroll.pack(side="right", fill="y", **pad)
        self.dl_tree.bind("<Double-1>", lambda e: self._open_version_detail())
        self.dl_tree.bind("<<TreeviewSelect>>", self._on_dl_select)
        # 下载按钮
        btn_f = ttk.Frame(parent)
        btn_f.pack(fill="x", **pad)
        ttk.Button(btn_f, text="[DL] 下载所选版本", command=self.download_version).pack(side="left", **pad)
        ttk.Button(btn_f, text="校验/补全所选版本", command=self._repair_selected).pack(side="left", **pad)
        ttk.Button(btn_f, text="下载全部Release", command=self._download_all_release).pack(side="left", **pad)

    # ------------------------------------------------------------------
    # [3] Mod资源下载 — 加载器 / Mod / 光影
    # ------------------------------------------------------------------
    def _build_resource_tab(self, parent, pad):
        """构建 Mod 资源下载选项卡（加载器 / Mod / 光影）。"""
        # 顶部：目标游戏版本选择（决定下载到哪个版本的 mods/shaderpacks 目录）
        top = ttk.Frame(parent)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="目标版本:").pack(side="left")
        self.res_ver_var = StringVar()
        self.res_ver_combo = ttk.Combobox(top, textvariable=self.res_ver_var,
                                          state="readonly", width=28)
        self.res_ver_combo.pack(side="left", **pad)
        ttk.Button(top, text="刷新", command=self._refresh_res_version_list).pack(side="left", **pad)
        ttk.Button(top, text="打开mods目录", command=lambda: self._open_res_dir("mods")).pack(side="left", **pad)
        ttk.Button(top, text="打开光影目录", command=lambda: self._open_res_dir("shaderpacks")).pack(side="left", **pad)

        # 子页签
        self.res_nb = ttk.Notebook(parent)
        self.res_nb.pack(fill="both", expand=True, **pad)
        self.res_loader_frame = ttk.Frame(self.res_nb)
        self.res_mod_frame = ttk.Frame(self.res_nb)
        self.res_shader_frame = ttk.Frame(self.res_nb)
        self.res_nb.add(self.res_loader_frame, text="Mod加载器")
        self.res_nb.add(self.res_mod_frame, text="Mod")
        self.res_nb.add(self.res_shader_frame, text="光影")

        self._build_loader_subtab(self.res_loader_frame, pad)
        self._build_mod_subtab(self.res_mod_frame, pad)
        self._build_shader_subtab(self.res_shader_frame, pad)
        # 打开 tab 时先填一版已安装版本，避免下拉框空白
        self.after_safe(200, self._refresh_res_version_list)

    # --- 目标版本下拉 ---
    def _refresh_res_version_list(self):
        """刷新目标版本下拉框 = 已安装版本列表。"""
        try:
            inst = installed_versions()
        except Exception:
            inst = self.installed or []
        self.res_versions = inst
        self.res_ver_combo["values"] = list(inst)
        cur = self.res_ver_var.get()
        if cur not in inst:
            if inst:
                self.res_ver_var.set(inst[0])
            else:
                self.res_ver_var.set("")
        return inst

    def _selected_res_version(self):
        """返回当前选中的目标版本 id（未选则取已安装的第一个）。"""
        v = self.res_ver_var.get()
        if v:
            return v
        inst = getattr(self, "res_versions", None) or []
        return inst[0] if inst else ""

    def _open_res_dir(self, sub):
        """打开当前目标版本的 mods/shaderpacks 目录。"""
        try:
            d = _ensure_resource_dir(self._selected_res_version(), sub)
            os.startfile(str(d))
            self._log(f"已打开: {d}")
        except Exception as e:
            self._log(f"打开失败: {e}")

    # --- Mod 加载器子页 ---
    def _build_loader_subtab(self, parent, pad):
        top = ttk.Frame(parent)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="MC版本:").pack(side="left")
        self.loader_mc_var = StringVar()
        self.loader_mc_combo = ttk.Combobox(top, textvariable=self.loader_mc_var,
                                            state="readonly", width=10)
        self.loader_mc_combo.pack(side="left", **pad)
        self.loader_mc_var.set("1.20.1")
        self.loader_mc_combo["values"] = ["1.12.2", "1.16.5", "1.17.1", "1.18.2", "1.19.2",
                                          "1.19.4", "1.20.1", "1.20.2", "1.20.4", "1.20.6", "1.21.1", "1.21.4"]
        ttk.Label(top, text="加载器:").pack(side="left", **pad)
        self.loader_kind_var = StringVar(value="forge")
        ttk.Radiobutton(top, text="Forge", variable=self.loader_kind_var,
                        value="forge", command=self._refresh_loader_list).pack(side="left")
        ttk.Radiobutton(top, text="Fabric", variable=self.loader_kind_var,
                        value="fabric", command=self._refresh_loader_list).pack(side="left")
        ttk.Radiobutton(top, text="NeoForge", variable=self.loader_kind_var,
                        value="neoforge", command=self._refresh_loader_list).pack(side="left")
        ttk.Button(top, text="刷新列表", command=self._refresh_loader_list).pack(side="left", **pad)

        # 加载器列表
        cols = ("ver", "loader", "status")
        self.loader_tree = ttk.Treeview(parent, columns=cols, show="headings", height=12)
        for c, t, w in (("ver", "加载器版本", 120), ("loader", "类型", 80), ("status", "状态", 80)):
            self.loader_tree.heading(c, text=t)
            self.loader_tree.column(c, width=w)
        ls = ttk.Scrollbar(parent, orient="vertical", command=self.loader_tree.yview)
        self.loader_tree.configure(yscrollcommand=ls.set)
        self.loader_tree.pack(side="left", fill="both", expand=True, **pad)
        ls.pack(side="right", fill="y", **pad)

        btn = ttk.Frame(parent)
        btn.pack(fill="x", **pad)
        ttk.Button(btn, text="下载并安装所选加载器", command=self._install_selected_loader).pack(side="left", **pad)
        self._loader_items = {}

    def _refresh_loader_list(self):
        """根据 MC 版本和加载器类型拉取可用加载器列表（后台线程，不卡 UI）。"""
        for item in self.loader_tree.get_children():
            self.loader_tree.delete(item)
        self._loader_items = {}
        kind = self.loader_kind_var.get()
        mc = self.loader_mc_var.get().strip()
        self._log(f"获取 {kind} 加载器列表 (MC {mc})...")
        self._set_status(f"获取 {kind} 列表 (MC {mc})...")

        def fetch_and_apply():
            try:
                if kind == "fabric":
                    versions = get_fabric_versions(mc)
                elif kind == "forge":
                    versions = get_forge_versions(mc)
                else:
                    versions = _get_neoforge_versions(mc, timeout=8)

                def apply():
                    for item in self.loader_tree.get_children():
                        self.loader_tree.delete(item)
                    self._loader_items = {}
                    for entry in versions:
                        if kind == "fabric":
                            loader_ver, inst_url = entry
                            iid = self.loader_tree.insert("", "end",
                                values=(loader_ver, "Fabric", "可下载"))
                            self._loader_items[iid] = ("fabric", loader_ver, inst_url)
                        elif kind == "forge":
                            fv, url = entry
                            iid = self.loader_tree.insert("", "end",
                                values=(fv, "Forge", "可下载"))
                            self._loader_items[iid] = ("forge", fv)
                        else:
                            nv, url = entry
                            iid = self.loader_tree.insert("", "end",
                                values=(nv, "NeoForge", "可下载"))
                            self._loader_items[iid] = ("neoforge", nv, url)
                    if not versions:
                        self._log(f"{kind} (MC {mc}) 未找到加载器版本")
                        self._set_status(f"{kind} (MC {mc}) 无可用版本")
                    else:
                        self._set_status(f"{kind} 列表已更新 ({len(versions)} 项)")

                self.after_safe(0, apply)
            except Exception as _exc:
                self.after_safe(0, lambda msg=repr(_exc): self._log(f"获取加载器列表失败: {msg}"))
                self.after_safe(0, lambda: self._set_status("获取加载器列表失败"))

        threading.Thread(target=fetch_and_apply, daemon=True).start()

    def _install_selected_loader(self):
        """下载并安装所选 mod 加载器到当前游戏目录。"""
        if self.busy:
            return
        sel = self.loader_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选择一个加载器版本")
            return
        item = self._loader_items.get(sel[0])
        if not item:
            return
        kind = item[0]
        ver_id = self._selected_res_version() or self.loader_mc_var.get()
        self._save_settings()
        apply_paths(self.config)
        self.busy = True
        threading.Thread(target=self._install_loader_bg,
                         args=(kind, item, ver_id), daemon=True).start()

    def _install_loader_bg(self, kind, item, ver_id):
        def log(m):
            self.after_safe(0, lambda: self._log(m))
            self.after_safe(0, lambda: self._set_status(f"安装 {kind} 加载器... ({m})"))
        def cb(cur, tot, note=None):
            self.after_safe(0, lambda: self._set_progress(cur, tot,
                                                          f"安装 {kind} 加载器... ({note})"
                                                          if note else f"安装 {kind} 加载器..."))
        try:
            if kind == "fabric":
                _, loader_ver, url = item
                download_fabric(ver_id, loader_ver, url, progress_cb=cb, log_cb=log)
                _download_fabric_api_mc(ver_id, log, cb)
            elif kind == "forge":
                _, fv = item
                download_forge(ver_id, fv, None, None, progress_cb=cb, log_cb=log)
            elif kind == "neoforge":
                _, nv, url = item
                _download_neoforge(ver_id, nv, url, progress_cb=cb, log_cb=log)
            log("加载器安装完成")
            self.after_safe(0, lambda: self._reset_progress)
            self.after_safe(0, lambda: self._set_status(f"{ver_id} 加载器安装完成"))
        except Exception as e:
            log(f"加载器安装失败: {e}")
        finally:
            self.busy = False
            self.after_safe(2000, self._refresh_res_version_list)

    # --- Mod 子页 ---
    def _build_mod_subtab(self, parent, pad):
        top = ttk.Frame(parent)
        top.pack(fill="x", **pad)
        self.mod_search_var = StringVar()
        ttk.Entry(top, textvariable=self.mod_search_var).pack(side="left", fill="x", expand=True, **pad)
        ttk.Label(top, text="加载器:").pack(side="left")
        self.mod_loader_var = StringVar(value="all")
        ttk.Combobox(top, textvariable=self.mod_loader_var, state="readonly", width=10,
                     values=["all", "forge", "fabric", "neoforge", "quilt"]).pack(side="left", **pad)
        ttk.Button(top, text="搜索Mod", command=self._search_mods).pack(side="left", **pad)

        # 结果列表：图标 + 名称 + 简介（简介在下方说明区显示完整）
        cols = ("icon", "name", "loader", "ver")
        self.mod_tree = ttk.Treeview(parent, columns=cols, show="headings", height=10)
        for c, t, w in (("icon", "图标", 46), ("name", "Mod 名称", 220),
                        ("loader", "加载器", 70), ("ver", "游戏版本", 90)):
            self.mod_tree.heading(c, text=t)
            self.mod_tree.column(c, width=w, anchor="center" if c == "icon" else "w")
        ms = ttk.Scrollbar(parent, orient="vertical", command=self.mod_tree.yview)
        self.mod_tree.configure(yscrollcommand=ms.set)
        self.mod_tree.pack(side="left", fill="both", expand=True, **pad)
        ms.pack(side="right", fill="y", **pad)
        self.mod_tree.bind("<<TreeviewSelect>>", self._on_mod_select)
        self._mod_items = {}
        self._mod_rec = {}  # iid -> 已解析好的 version dict

        # 选中的 Mod 简介 + 前置依赖显示
        info_f = ttk.LabelFrame(parent, text="选中的 Mod (简介 / 前置依赖)")
        info_f.pack(fill="x", **pad)
        self.mod_dep_text = scrolledtext.ScrolledText(info_f, height=7, state="disabled",
                                                      font=("Consolas", 9))
        self.mod_dep_text.pack(fill="x", **pad)

        btn = ttk.Frame(parent)
        btn.pack(fill="x", **pad)
        ttk.Button(btn, text="下载此Mod(含前置)到当前版本",
                   command=self._download_selected_mod).pack(side="left", **pad)
        ttk.Label(btn, text="(自动匹配当前版本+加载器，并把必要前置一并下好)",
                  foreground="#888").pack(side="left", **pad)

    # --- 光影子页 ---
    def _build_shader_subtab(self, parent, pad):
        top = ttk.Frame(parent)
        top.pack(fill="x", **pad)
        self.shader_search_var = StringVar()
        ttk.Entry(top, textvariable=self.shader_search_var).pack(side="left", fill="x", expand=True, **pad)
        ttk.Label(top, text="加载器:").pack(side="left")
        self.shader_loader_var = StringVar(value="all")
        ttk.Combobox(top, textvariable=self.shader_loader_var, state="readonly", width=10,
                     values=["all", "iris", "optifine", "forge", "fabric", "neoforge", "quilt"]).pack(side="left", **pad)
        ttk.Button(top, text="搜索光影", command=self._search_shaders).pack(side="left", **pad)

        # 结果列表：图标 + 名称 + 加载器 + 版本
        cols = ("icon", "name", "loader", "ver")
        self.shader_tree = ttk.Treeview(parent, columns=cols, show="headings", height=10)
        for c, t, w in (("icon", "图标", 46), ("name", "光影名称", 220),
                        ("loader", "加载器", 80), ("ver", "游戏版本", 90)):
            self.shader_tree.heading(c, text=t)
            self.shader_tree.column(c, width=w, anchor="center" if c == "icon" else "w")
        ss = ttk.Scrollbar(parent, orient="vertical", command=self.shader_tree.yview)
        self.shader_tree.configure(yscrollcommand=ss.set)
        self.shader_tree.pack(side="left", fill="both", expand=True, **pad)
        ss.pack(side="right", fill="y", **pad)
        self.shader_tree.bind("<<TreeviewSelect>>", self._on_shader_select)
        self._shader_items = {}
        self._shader_rec = {}  # iid -> 已解析好的 version dict

        # 选中的光影简介 + 前置依赖
        info_f = ttk.LabelFrame(parent, text="选中的光影 (简介 / 前置依赖)")
        info_f.pack(fill="x", **pad)
        self.shader_dep_text = scrolledtext.ScrolledText(info_f, height=7, state="disabled",
                                                         font=("Consolas", 9))
        self.shader_dep_text.pack(fill="x", **pad)

        btn = ttk.Frame(parent)
        btn.pack(fill="x", **pad)
        ttk.Button(btn, text="下载此光影(含前置)到当前版本",
                   command=self._download_selected_shader).pack(side="left", **pad)
        ttk.Label(btn, text="(自动匹配当前版本+加载器，并把必要前置一并下好)",
                  foreground="#888").pack(side="left", **pad)

    def _search_shaders(self):
        """搜索光影：用内置 Modrinth 接口（project_type=shader），
        中文关键词先经 MC百科转英文（SDK 可用时），后台拉取不卡 UI。
        """
        query = self.shader_search_var.get().strip()
        if not query:
            messagebox.showwarning("提示", "请输入搜索关键字")
            return
        for item in self.shader_tree.get_children():
            self.shader_tree.delete(item)
        self._shader_items = {}
        self._shader_rec = {}
        loader = self.shader_loader_var.get()
        if loader == "all":
            loader = None
        ver = _pick_game_version(self._selected_res_version()) or None

        self._log(f"搜索光影: {query} (loader={loader}, mc={ver})...")
        self._set_status(f"搜索光影: {query} ...")

        def run():
            try:
                queries = [query]
                if re.search(r"[\u4e00-\u9fff]", query) and modrinth_sdk is not None:
                    try:
                        en_list = modrinth_sdk.mcmod_chinese_lookup(query, limit=6)
                        english = [x["english"] for x in en_list]
                        if english:
                            self._log(f"MC百科 -> 英文名: {english}")
                            queries = english
                    except Exception:
                        pass
                seen = set()
                all_hits = []
                for q in queries:
                    hits = modrinth_search(q, project_type="shader", loader=loader,
                                           game_version=ver, limit=15)
                    for h in hits:
                        if h.get("project_id") not in seen:
                            seen.add(h["project_id"])
                            all_hits.append(h)
                    if len(all_hits) >= 30:
                        break
                self._log(f"找到 {len(all_hits)} 个光影")
                self.after_safe(0, lambda hs=all_hits: self._apply_shader_hits(hs))
            except Exception as e:
                self._log(f"搜索失败: {e}")
                self.after_safe(0, lambda: self._set_status("搜索失败"))

        threading.Thread(target=run, daemon=True).start()

    def _apply_shader_hits(self, hits):
        """把搜索结果插入列表（含图标），并后台解析版本/前置。"""
        for item in self.shader_tree.get_children():
            self.shader_tree.delete(item)
        self._shader_items = {}
        self._shader_rec = {}
        loader = self.shader_loader_var.get()
        if loader == "all":
            loader = None
        ver = _pick_game_version(self._selected_res_version()) or None
        for h in hits:
            img = self._load_mod_icon(h.get("icon_url"))
            iid = self.shader_tree.insert("", "end",
                                          values=(h.get("title", ""),
                                                  ",".join(h.get("categories", []) or [])[:12] or "-",
                                                  ",".join((h.get("versions", []) or [])[-2:])[:14] or "-"),
                                          image=img or "")
            self._shader_items[iid] = h
            threading.Thread(target=self._bg_resolve_shader,
                             args=(h, iid, ver, loader), daemon=True).start()

    def _pick_shader_version(self, pid, ver, loader):
        """拉取某光影的版本列表，挑一个匹配 游戏版本+加载器 的最新版本。"""
        if not pid:
            return None
        versions = modrinth_project_versions(pid)
        if not versions:
            return None

        def ok(v):
            if ver and ver not in (v.get("game_versions") or []):
                return False
            if loader and loader != "all" and loader not in (v.get("loaders") or []):
                return False
            return True

        cands = [v for v in versions if ok(v)]
        if not cands:
            cands = versions
        cands.sort(key=lambda v: v.get("date_published") or "", reverse=True)
        return cands[0]

    def _bg_resolve_shader(self, hit, iid, ver, loader):
        try:
            vinfo = self._pick_shader_version(hit.get("project_id"), ver, loader)

            def apply():
                if not self.shader_tree.exists(iid):
                    return
                self._shader_rec[iid] = vinfo
                if vinfo:
                    deps = modrinth_resolve_dependencies(vinfo, "shader")
                    self.shader_tree.item(iid, values=(
                        hit.get("title", ""),
                        ",".join(vinfo.get("loaders") or hit.get("categories") or [])[:12] or "-",
                        ",".join((vinfo.get("game_versions") or [])[-2:])[:14] or "-",
                    ))
            self.after_safe(0, apply)
        except Exception:
            pass

    def _on_shader_select(self, evt):
        sel = self.shader_tree.selection()
        if not sel:
            return
        item = self._shader_items.get(sel[0])
        if not item:
            return
        self.shader_dep_text.configure(state="normal")
        self.shader_dep_text.delete("1.0", "end")
        desc = item.get("description") or ""
        self.shader_dep_text.insert("end", f"【简介】{desc}\n" if desc else "")
        vinfo = self._shader_rec.get(sel[0])
        if vinfo:
            deps = modrinth_resolve_dependencies(vinfo, "shader")
            self.shader_dep_text.insert("end", f"【版本】{vinfo.get('version_number', '?')} "
                                               f"({','.join(vinfo.get('game_versions') or [])} / "
                                               f"{','.join(vinfo.get('loaders') or [])})\n")
            if deps:
                self.shader_dep_text.insert("end", "【前置依赖】\n")
                for d in deps:
                    self.shader_dep_text.insert("end", f"  • {d.get('project_id')}\n")
            else:
                self.shader_dep_text.insert("end", "【前置依赖】无\n")
        else:
            self.shader_dep_text.insert("end", "(选中后自动解析版本与前置依赖)\n")
        self.shader_dep_text.configure(state="disabled")

    def _selected_shader_hit(self):
        sel = self.shader_tree.selection()
        if sel:
            return self._shader_items.get(sel[0])
        return None

    def _download_selected_shader(self):
        """一键下载选中的光影：匹配 当前游戏版本+加载器 的最新版本，并把必要前置一并下好。"""
        hit = self._selected_shader_hit()
        if not hit:
            messagebox.showwarning("提示", "请先在列表中选中一个光影")
            return
        ver_id = self._selected_res_version()
        if not ver_id:
            messagebox.showwarning("提示", "请先选择目标版本（该版本已安装）")
            return
        self._save_settings()
        apply_paths(self.config)
        self.busy = True
        threading.Thread(target=self._download_shader_bg, args=(hit, ver_id),
                         daemon=True).start()

    def _download_shader_bg(self, hit, ver_id):
        """后台下载光影 + 全部必要前置到该版本的 shaderpacks 目录。"""
        title = hit.get("title") or "shader"
        pid = hit.get("project_id")
        def log(m):
            self.after_safe(0, lambda: self._log(m))
            self.after_safe(0, lambda: self._set_status(f"下载光影 {title}... ({m})"))
        def prog(cur, tot):
            self.after_safe(0, lambda: self._set_progress(cur, tot, f"下载光影 {title}..."))
        try:
            self.after_safe(0, lambda: self._set_status(f"解析 {title} 的版本与前置依赖..."))
            loader = self.shader_loader_var.get()
            if loader == "all":
                loader = None
            ver = _pick_game_version(ver_id) or None
            # 先用后台算好的匹配版本，没有再实时解析
            vinfo = None
            for iid, vv in self._shader_rec.items():
                if self._shader_items.get(iid, {}).get("project_id") == pid:
                    vinfo = vv
                    break
            if not vinfo:
                vinfo = self._pick_shader_version(pid, ver, loader)
            if not vinfo:
                log(f"未能为 {title} 找到可用版本")
                self.busy = False
                return
            target = _ensure_resource_dir(ver_id, "shaderpacks")
            deps = modrinth_resolve_dependencies(vinfo, "shader")
            log(f"使用版本: {vinfo.get('version_number','?')} ({len(deps)} 个前置)")
            for dep in deps:
                self._download_one_shader_dep(dep, target, log, prog, ver, loader)
            # 本体
            log(f"下载 {title} ...")
            modrinth_download_file(vinfo, target, progress_cb=prog, log_cb=log)
            log(f"完成: {title} 已装到 {target}")
            self.after_safe(0, lambda: self._reset_progress)
            self.after_safe(0, lambda: self._set_status(f"{title} 及前置下载完成"))
        except Exception as e:
            log(f"下载失败: {e}")
        finally:
            self.busy = False
            self.after_safe(1000, self._refresh_res_version_list)

    def _download_one_shader_dep(self, dep, target, log, prog, ver, loader):
        """下载一个必要前置到目标目录（用内置接口解析该前置的匹配版本）。"""
        dep_pid = dep.get("project_id")
        if not dep_pid:
            return
        try:
            dep_ver = None
            if dep.get("version_id"):
                try:
                    dep_ver = modrinth_version(dep["version_id"])
                except Exception:
                    dep_ver = None
            if not dep_ver:
                dep_ver = self._pick_shader_version(dep_pid, ver, loader)
            if not dep_ver:
                log(f"前置 {dep_pid} 无可用版本，跳过")
                return
            modrinth_download_file(dep_ver, target, progress_cb=prog, log_cb=log)
            log(f"  前置已下: {dep_pid}")
        except Exception as e:
            log(f"前置 {dep_pid} 下载失败: {e}")

    # --- 整合包子页 ---
    def _build_modpack_tab(self, parent, pad):
        top = ttk.Frame(parent)
        top.pack(fill="x", **pad)
        self.modpack_search_var = StringVar()
        ttk.Entry(top, textvariable=self.modpack_search_var).pack(side="left", fill="x", expand=True, **pad)
        ttk.Label(top, text="加载器:").pack(side="left")
        self.modpack_loader_var = StringVar(value="all")
        ttk.Combobox(top, textvariable=self.modpack_loader_var, state="readonly", width=10,
                     values=["all", "forge", "fabric", "neoforge", "quilt"]).pack(side="left", **pad)
        ttk.Button(top, text="搜索整合包", command=self._search_modpacks).pack(side="left", **pad)

        # 结果列表：图标 + 名称 + 加载器 + 版本
        cols = ("icon", "name", "loader", "ver")
        self.modpack_tree = ttk.Treeview(parent, columns=cols, show="headings", height=10)
        for c, t, w in (("icon", "图标", 46), ("name", "整合包名称", 220),
                        ("loader", "加载器", 80), ("ver", "游戏版本", 90)):
            self.modpack_tree.heading(c, text=t)
            self.modpack_tree.column(c, width=w, anchor="center" if c == "icon" else "w")
        ps = ttk.Scrollbar(parent, orient="vertical", command=self.modpack_tree.yview)
        self.modpack_tree.configure(yscrollcommand=ps.set)
        self.modpack_tree.pack(side="left", fill="both", expand=True, **pad)
        ps.pack(side="right", fill="y", **pad)
        self.modpack_tree.bind("<<TreeviewSelect>>", self._on_modpack_select)
        self._modpack_items = {}
        self._modpack_rec = {}  # iid -> 已解析好的 version dict

        # 选中的整合包简介 + 版本信息
        info_f = ttk.LabelFrame(parent, text="选中的整合包 (简介 / 版本)")
        info_f.pack(fill="x", **pad)
        self.modpack_dep_text = scrolledtext.ScrolledText(info_f, height=7, state="disabled",
                                                          font=("Consolas", 9))
        self.modpack_dep_text.pack(fill="x", **pad)

        btn = ttk.Frame(parent)
        btn.pack(fill="x", **pad)
        ttk.Button(btn, text="下载此整合包(.mrpack)到当前版本",
                   command=self._download_selected_modpack).pack(side="left", **pad)
        ttk.Label(btn, text="(.mrpack 下载到该版本的 modpacks 目录)",
                  foreground="#888").pack(side="left", **pad)

    def _search_modpacks(self):
        """搜索整合包：用内置 Modrinth 接口（project_type=modpack），
        中文关键词先经 MC百科转英文（SDK 可用时），后台拉取不卡 UI。
        """
        query = self.modpack_search_var.get().strip()
        if not query:
            messagebox.showwarning("提示", "请输入搜索关键字")
            return
        for item in self.modpack_tree.get_children():
            self.modpack_tree.delete(item)
        self._modpack_items = {}
        self._modpack_rec = {}
        loader = self.modpack_loader_var.get()
        if loader == "all":
            loader = None
        ver = _pick_game_version(self._selected_res_version()) or None

        self._log(f"搜索整合包: {query} (loader={loader}, mc={ver})...")
        self._set_status(f"搜索整合包: {query} ...")

        def run():
            try:
                queries = [query]
                if re.search(r"[\u4e00-\u9fff]", query) and modrinth_sdk is not None:
                    try:
                        en_list = modrinth_sdk.mcmod_chinese_lookup(query, limit=6)
                        english = [x["english"] for x in en_list]
                        if english:
                            self._log(f"MC百科 -> 英文名: {english}")
                            queries = english
                    except Exception:
                        pass
                seen = set()
                all_hits = []
                for q in queries:
                    hits = modrinth_search(q, project_type="modpack", loader=loader,
                                           game_version=ver, limit=15)
                    for h in hits:
                        if h.get("project_id") not in seen:
                            seen.add(h["project_id"])
                            all_hits.append(h)
                    if len(all_hits) >= 30:
                        break
                self._log(f"找到 {len(all_hits)} 个整合包")
                self.after_safe(0, lambda hs=all_hits: self._apply_modpack_hits(hs))
            except Exception as e:
                self._log(f"搜索失败: {e}")
                self.after_safe(0, lambda: self._set_status("搜索失败"))

        threading.Thread(target=run, daemon=True).start()

    def _apply_modpack_hits(self, hits):
        """把搜索结果插入列表（含图标），并后台解析版本。"""
        for item in self.modpack_tree.get_children():
            self.modpack_tree.delete(item)
        self._modpack_items = {}
        self._modpack_rec = {}
        loader = self.modpack_loader_var.get()
        if loader == "all":
            loader = None
        ver = _pick_game_version(self._selected_res_version()) or None
        for h in hits:
            img = self._load_mod_icon(h.get("icon_url"))
            iid = self.modpack_tree.insert("", "end",
                                           values=(h.get("title", ""),
                                                   ",".join(h.get("categories", []) or [])[:12] or "-",
                                                   ",".join((h.get("versions", []) or [])[-2:])[:14] or "-"),
                                           image=img or "")
            self._modpack_items[iid] = h
            threading.Thread(target=self._bg_resolve_modpack,
                             args=(h, iid, ver, loader), daemon=True).start()

    def _bg_resolve_modpack(self, hit, iid, ver, loader):
        try:
            vinfo = self._pick_shader_version(hit.get("project_id"), ver, loader)

            def apply():
                if not self.modpack_tree.exists(iid):
                    return
                self._modpack_rec[iid] = vinfo
                if vinfo:
                    self.modpack_tree.item(iid, values=(
                        hit.get("title", ""),
                        ",".join(vinfo.get("loaders") or hit.get("categories") or [])[:12] or "-",
                        ",".join((vinfo.get("game_versions") or [])[-2:])[:14] or "-",
                    ))
            self.after_safe(0, apply)
        except Exception:
            pass

    def _on_modpack_select(self, evt):
        sel = self.modpack_tree.selection()
        if not sel:
            return
        item = self._modpack_items.get(sel[0])
        if not item:
            return
        self.modpack_dep_text.configure(state="normal")
        self.modpack_dep_text.delete("1.0", "end")
        desc = item.get("description") or ""
        self.modpack_dep_text.insert("end", f"【简介】{desc}\n" if desc else "")
        vinfo = self._modpack_rec.get(sel[0])
        if vinfo:
            self.modpack_dep_text.insert("end", f"【版本】{vinfo.get('version_number', '?')} "
                                                f"({','.join(vinfo.get('game_versions') or [])} / "
                                                f"{','.join(vinfo.get('loaders') or [])})\n")
            self.modpack_dep_text.insert("end", "【说明】.mrpack 内含全部 mod 文件清单，"
                                                "下载后可在其他整合包工具中导入安装。\n")
        else:
            self.modpack_dep_text.insert("end", "(选中后自动解析版本)\n")
        self.modpack_dep_text.configure(state="disabled")

    def _download_selected_modpack(self):
        """一键下载选中的整合包 .mrpack 到当前目标版本目录。"""
        sel = self.modpack_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先在列表中选中一个整合包")
            return
        hit = self._modpack_items.get(sel[0])
        if not hit:
            return
        ver_id = self._selected_res_version()
        if not ver_id:
            messagebox.showwarning("提示", "请先选择目标版本（该版本已安装）")
            return
        self._save_settings()
        apply_paths(self.config)
        self.busy = True
        threading.Thread(target=self._download_modpack_bg, args=(hit, ver_id),
                         daemon=True).start()

    def _download_modpack_bg(self, hit, ver_id):
        """后台下载整合包 .mrpack 到该版本的 modpacks 目录。"""
        title = hit.get("title") or "modpack"
        pid = hit.get("project_id")
        def log(m):
            self.after_safe(0, lambda: self._log(m))
            self.after_safe(0, lambda: self._set_status(f"下载整合包 {title}... ({m})"))
        def prog(cur, tot):
            self.after_safe(0, lambda: self._set_progress(cur, tot, f"下载整合包 {title}..."))
        try:
            self.after_safe(0, lambda: self._set_status(f"解析 {title} 的版本..."))
            loader = self.modpack_loader_var.get()
            if loader == "all":
                loader = None
            ver = _pick_game_version(ver_id) or None
            # 先用后台算好的匹配版本，没有再实时解析
            vinfo = None
            for iid, vv in self._modpack_rec.items():
                if self._modpack_items.get(iid, {}).get("project_id") == pid:
                    vinfo = vv
                    break
            if not vinfo:
                vinfo = self._pick_shader_version(pid, ver, loader)
            if not vinfo:
                log(f"未能为 {title} 找到可用版本")
                self.busy = False
                return
            target = _ensure_resource_dir(ver_id, "modpacks")
            log(f"使用版本: {vinfo.get('version_number','?')}")
            log(f"下载 {title} (.mrpack) ...")
            modrinth_download_file(vinfo, target, progress_cb=prog, log_cb=log)
            log(f"完成: {title} 已下载到 {target}")
            self.after_safe(0, lambda: self._reset_progress)
            self.after_safe(0, lambda: self._set_status(f"{title} 下载完成"))
        except Exception as e:
            log(f"下载失败: {e}")
        finally:
            self.busy = False
            self.after_safe(1000, self._refresh_res_version_list)

    def _icon_cache_dir(self):
        d = Path(BASE_DIR) / "mod_icons"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _load_mod_icon(self, icon_url):
        """下载并缓存 mod 图标，返回 tk.PhotoImage（失败返回 None）。"""
        if not icon_url:
            return None
        import urllib.parse
        try:
            key = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(icon_url.split("?")[0]))
            if not key:
                key = "icon.png"
            path = self._icon_cache_dir() / key
            if not path.exists():
                import urllib.request
                req = urllib.request.Request(icon_url, headers={"User-Agent": "PyMC-Launcher/1.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    path.write_bytes(r.read())
            return tk.PhotoImage(file=str(path)).subsample(4, 4)
        except Exception:
            return None

    def _on_mod_select(self, evt):
        sel = self.mod_tree.selection()
        if not sel:
            return
        item = self._mod_items.get(sel[0])
        if not item:
            return
        self.mod_dep_text.configure(state="normal")
        self.mod_dep_text.delete("1.0", "end")
        # 简介
        desc = item.get("desc") or ""
        self.mod_dep_text.insert("end", f"【简介】{desc}\n" if desc else "")
        # 前置依赖
        vinfo = self._mod_rec.get(sel[0])
        if vinfo:
            deps = modrinth_sdk.resolve_required_deps(vinfo) if modrinth_sdk else []
            self.mod_dep_text.insert("end", f"【版本】{vinfo.get('version_number', '?')} "
                                           f"({','.join(vinfo.get('game_versions') or [])} / "
                                           f"{','.join(vinfo.get('loaders') or [])})\n")
            if deps:
                self.mod_dep_text.insert("end", "【前置依赖】\n")
                for d in deps:
                    self.mod_dep_text.insert("end", f"  • {d.get('project_id')}\n")
            else:
                self.mod_dep_text.insert("end", "【前置依赖】无\n")
        else:
            self.mod_dep_text.insert("end", "(选中后自动解析版本与前置依赖)\n")
        self.mod_dep_text.configure(state="disabled")

    def _search_mods(self):
        """搜索 mod：中文关键词先经 MC百科转英文，再用 Modrinth 官方 SDK 搜索。
        渲染：名称 + 简介 + 图标；后台解析每个的匹配版本。
        """
        query = self.mod_search_var.get().strip()
        if not query:
            messagebox.showwarning("提示", "请输入搜索关键字")
            return
        for item in self.mod_tree.get_children():
            self.mod_tree.delete(item)
        self._mod_items = {}
        self._mod_rec = {}
        loader = self.mod_loader_var.get()
        if loader == "all":
            loader = None
        ver = _pick_game_version(self._selected_res_version()) or None

        if modrinth_sdk is None:
            messagebox.showwarning("库未安装", "请先运行: pip install modrinth")
            return

        self._log(f"搜索 mod: {query} (loader={loader}, mc={ver})...")
        self._set_status(f"搜索 mod: {query} ...")

        def run():
            try:
                # 中文 -> 英文（MC百科）
                queries = [query]
                if re.search(r"[\u4e00-\u9fff]", query):
                    en_list = modrinth_sdk.mcmod_chinese_lookup(query, limit=6)
                    english = [x["english"] for x in en_list]
                    if english:
                        self._log(f"MC百科 -> 英文名: {english}")
                        queries = english
                # 用每个英文名去 SDK 搜索，合并去重
                seen = set()
                all_hits = []
                for q in queries:
                    hits = modrinth_sdk.modrinth_search_sdk(q, ver, loader, limit=12)
                    for h in hits:
                        if h["project_id"] not in seen:
                            seen.add(h["project_id"])
                            all_hits.append(h)
                    if len(all_hits) >= 30:
                        break
                self._log(f"找到 {len(all_hits)} 个结果")
                self.after_safe(0, lambda hs=all_hits: self._apply_mod_hits(hs))
            except Exception as e:
                self._log(f"搜索失败: {e}")
                self.after_safe(0, lambda: self._set_status("搜索失败"))

        threading.Thread(target=run, daemon=True).start()

    def _apply_mod_hits(self, hits):
        """把搜索结果插入列表（含图标），并后台解析版本/前置。"""
        for item in self.mod_tree.get_children():
            self.mod_tree.delete(item)
        self._mod_items = {}
        self._mod_rec = {}
        loader = self.mod_loader_var.get()
        if loader == "all":
            loader = None
        ver = _pick_game_version(self._selected_res_version()) or None
        for h in hits:
            img = self._load_mod_icon(h.get("icon_url"))
            iid = self.mod_tree.insert("", "end",
                                       values=(h.get("name", ""),
                                               ",".join(h.get("loaders", []) or [])[:12] or "-",
                                               ",".join((h.get("game_versions", []) or [])[-2:])[:14] or "-"),
                                       image=img or "")
            self._mod_items[iid] = h
            # 后台解析该 mod 的匹配版本 + 前置
            threading.Thread(target=self._bg_resolve_mod,
                             args=(h, iid, ver, loader), daemon=True).start()

    def _bg_resolve_mod(self, hit, iid, ver, loader):
        try:
            if not modrinth_sdk:
                return
            vinfo = modrinth_sdk.resolve_version_sdk(hit["project_id"], ver, loader)
            def apply():
                if not self.mod_tree.exists(iid):
                    return
                self._mod_rec[iid] = vinfo
                if vinfo:
                    deps = modrinth_sdk.resolve_required_deps(vinfo)
                    label = f"{vinfo.get('version_number','?')} ({len(deps)}前置)"
                    # 更新列表显示的版本列
                    self.mod_tree.item(iid, values=(
                        hit.get("name", ""),
                        ",".join(vinfo.get("loaders", []) or [])[:12] or "-",
                        ",".join((vinfo.get("game_versions", []) or [])[-2:])[:14] or "-",
                    ))
            self.after_safe(0, apply)
        except Exception:
            pass

    def _selected_mod_hit(self):
        sel = self.mod_tree.selection()
        if sel:
            return self._mod_items.get(sel[0])
        return None

    def _download_selected_mod(self):
        """一键下载选中的 Mod：匹配 当前游戏版本+加载器 的最新版本，并把必要前置一并下好。"""
        hit = self._selected_mod_hit()
        if not hit:
            messagebox.showwarning("提示", "请先在列表中选中一个Mod")
            return
        ver_id = self._selected_res_version()
        if not ver_id:
            messagebox.showwarning("提示", "请先选择目标版本（该版本已安装）")
            return
        if modrinth_sdk is None:
            messagebox.showwarning("库未安装", "请先运行: pip install modrinth")
            return
        self._save_settings()
        apply_paths(self.config)
        self.busy = True
        threading.Thread(target=self._download_mod_bg, args=(hit, ver_id),
                         daemon=True).start()

    def _download_mod_bg(self, hit, ver_id):
        """后台下载 Mod + 全部必要前置到该版本的 mods 目录。"""
        title = hit.get("name") or "mod"
        pid = hit.get("project_id")
        def log(m):
            self.after_safe(0, lambda: self._log(m))
            self.after_safe(0, lambda: self._set_status(f"下载 mod {title}... ({m})"))
        def prog(cur, tot):
            self.after_safe(0, lambda: self._set_progress(cur, tot, f"下载 mod {title}..."))
        try:
            self.after_safe(0, lambda: self._set_status(f"解析 {title} 的版本与前置依赖..."))
            loader = self.mod_loader_var.get()
            if loader == "all":
                loader = None
            ver = _pick_game_version(self._selected_res_version()) or None
            # 先用后台算好的匹配版本，没有再实时解析
            vinfo = None
            for iid, vv in self._mod_rec.items():
                if self._mod_items.get(iid, {}).get("project_id") == pid:
                    vinfo = vv
                    break
            if not vinfo:
                vinfo = modrinth_sdk.resolve_version_sdk(pid, ver, loader)
            if not vinfo:
                log(f"未能为 {title} 找到可用版本")
                self.busy = False
                return
            url, fname = modrinth_sdk.version_download(vinfo)
            if not url:
                log(f"{title} 没有可下载文件")
                self.busy = False
                return
            target = _ensure_resource_dir(ver_id, "mods")
            # 必要前置
            deps = modrinth_sdk.resolve_required_deps(vinfo)
            log(f"使用版本: {vinfo.get('version_number','?')} ({len(deps)} 个前置)")
            for dep in deps:
                self._download_one_dep_sdk(dep, target, log, prog, ver, loader)
            # 本体
            log(f"下载 {title} ...")
            dest = modrinth_sdk.download_file(url, Path(target) / (fname or "mod.jar"), prog)
            log(f"完成: {title} 已装到 {target}")
            self.after_safe(0, lambda: self._reset_progress)
            self.after_safe(0, lambda: self._set_status(f"{title} 及前置下载完成"))
        except Exception as e:
            log(f"下载失败: {e}")
        finally:
            self.busy = False
            self.after_safe(1000, self._refresh_res_version_list)

    def _download_one_dep_sdk(self, dep, target, log, prog, ver, loader):
        """下载一个必要前置到目标目录（用 SDK 解析该前置的匹配版本）。"""
        dep_pid = dep.get("project_id")
        if not dep_pid:
            return
        try:
            dep_ver = modrinth_sdk.resolve_version_sdk(dep_pid, ver, loader)
            if not dep_ver:
                log(f"前置 {dep_pid} 无可用版本，跳过")
                return
            url, fname = modrinth_sdk.version_download(dep_ver)
            if url:
                modrinth_sdk.download_file(url, Path(target) / (fname or "dep.jar"), prog)
                log(f"  前置已下: {dep_pid}")
        except Exception as e:
            log(f"前置 {dep_pid} 下载失败: {e}")

    def _auto_match_java(self, version_id, required=None):
        """根据版本自动匹配合适的 Java。返回匹配到的 java 路径，或 None。"""
        if not self.detected_java:
            return None
        if required is None:
            required = _required_java_major(version_id)
        self._log(f"版本 {version_id} 需要 Java {required}+")

        def to_major(v):
            try:
                # 兼容 int 或 "8"/"17"/"21.0.6" 形式的版本号
                if isinstance(v, int):
                    return v
                s = str(v)
                head = s.split(".")[0]
                if head == "1":  # 1.8.0_392 形式 -> 8
                    return int(s.split(".")[1]) if "." in s else 8
                return int(head)
            except Exception:
                return 0

        required = to_major(required)
        # 找>=required 的最小 Java 大版本
        best, best_ver = None, 999
        for ver, path in self.detected_java:
            vmaj = to_major(ver)
            if vmaj >= required and vmaj < best_ver:
                best = path
                best_ver = vmaj
        if best:
            name = f"Java {best_ver}  @  {best}"
            self.java_var.set(name)
            self._log(f"自动匹配: {name}")
            return best
        # 没有满足的，找最新的
        self.java_var.set(self.java_combo["values"][0] if self.java_combo["values"] else "")
        self._log(f"未找到 Java {required}+，使用最新: {self.java_var.get()}")
        return None

    def _on_ver_tree_select(self, evt):
        sel = self.ver_tree.selection()
        if sel:
            v = self._ver_item_map.get(sel[0])
            if v:
                self._log(f"选中的版本: {v[0]}")
                self._auto_match_java(v[0])

    def _refresh_dl_list(self):
        """刷新下载列表（在线版本，走 minecraft-launcher-lib）"""
        for item in self.dl_tree.get_children():
            self.dl_tree.delete(item)
        filter_type = self.dl_filter_var.get()
        try:
            online = mcl_version_list()
        except Exception as _exc:
            self.after_safe(0, lambda msg=repr(_exc): self._log(f"获取版本列表失败: {msg}"))
            online = []
        self._dl_item_map = {}
        for v in online:
            ver_id, ver_type, url = v
            if filter_type and ver_type != filter_type:
                continue
            status = "已安装" if ver_id in self.installed else "未下载"
            icon = "[OK]" if status == "已安装" else "[DL]"
            iid = self.dl_tree.insert("", "end", values=(ver_id, ver_type, f"{icon} {status}"))
            self._dl_item_map[iid] = (ver_id, ver_type, ver_id, url)

    def _make_log(self, parent, height=10):
        from tkinter import scrolledtext
        txt = scrolledtext.ScrolledText(parent, height=height, state="disabled",
                                        font=("Consolas", 9))
        return txt

    def open_game_log_window(self, version_id):
        """打开独立的「游戏启动日志」tk 窗口，返回 (窗口, 写入函数)。"""
        import tkinter as tk
        from tkinter import scrolledtext
        win = tk.Toplevel(self.root)
        win.title(f"游戏日志 - {version_id}")
        win.geometry("720x440")
        # 顶部工具条：结束游戏进程按钮
        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=6, pady=(4, 0))
        ttk.Button(bar, text="结束进程",
                   command=lambda: self._kill_game_process(version_id)).pack(side="left")
        ttk.Label(bar, text=f"运行中：{version_id}").pack(side="left", padx=8)
        txt = scrolledtext.ScrolledText(win, state="disabled", font=("Consolas", 9))
        txt.pack(fill="both", expand=True, padx=6, pady=6)

        # 批量刷新：后台线程只往 pending 里塞行（不碰 Tk 控件），
        # 主线程每 200ms 把 pending 合并成一个长字符串一次性写入，防止高频日志卡死 UI。
        MAX_LINES = 1000
        pending = []

        def flush_logs():
            if not win.winfo_exists():
                return
            if pending:
                batch = "".join(pending)
                del pending[:]
                txt.configure(state="normal")
                txt.insert("end", batch)
                # 限制最大行数：超过 1000 行删掉最老的
                line_count = int(txt.index("end-1c").split(".")[0])
                if line_count > MAX_LINES:
                    txt.delete("1.0", f"{line_count - MAX_LINES + 1}.0")
                txt.see("end")
                txt.configure(state="disabled")
            win.after(200, flush_logs)

        def wlog(line):
            # 后台线程调用：只入列，不做任何 Tk 操作
            pending.append(line.rstrip("\n") + "\n")

        win.after(200, flush_logs)
        wlog("=== 游戏启动 ===")
        return win, wlog

    def _kill_game_process(self, version_id):
        """日志窗口「结束进程」按钮：强制结束该版本正在运行的游戏进程。"""
        proc = getattr(self, "_running_proc", {}).get(version_id)
        if proc is None or proc.poll() is not None:
            messagebox.showinfo("结束进程", "该版本没有正在运行的进程（可能已退出）")
            return
        import ctypes
        try:
            # Windows 下先尝试优雅结束，再强制结束整个进程树
            if os.name == "nt":
                # 结束游戏窗口：发送 WM_CLOSE 到主窗口，再兜底 kill
                try:
                    proc.terminate()
                except Exception:
                    pass
                # 确保 JVM 真正退出
                if proc.poll() is None:
                    proc.kill()
            else:
                proc.terminate()
            self._log(f"[结束进程] 已结束 {version_id} 的游戏进程")
        except Exception as e:
            messagebox.showerror("结束进程", f"结束进程失败: {e}")

    def _close_game_log(self, version_id):
        """游戏退出后 10 秒自动关闭日志窗口。"""
        try:
            win = getattr(self, "_game_log_win", {}).get(version_id)
            if win is None:
                return
            self.after_safe(0, lambda: win.after(10000, win.destroy))
        except Exception:
            pass

    def _log(self, msg):
        """日志直接打印到控制台（不写入日志文件/窗口）。"""
        try:
            print(msg, flush=True)
        except Exception:
            pass

    def _set_status(self, msg):
        self.status.config(text=msg)
        self.root.update_idletasks()

    def _set_progress(self, cur, total, note=None):
        """进度条 + 状态条同步刷新。total 为 0/None 时显示为不定进度（动画条）。"""
        try:
            if total:
                if self.progress.cget("mode") == "indeterminate":
                    self.progress.stop()
                    self.progress.config(mode="determinate")
                self.progress.config(maximum=total)
                self.progress["value"] = cur
                pct = int(cur * 100 / total)
                text = f"[{pct}%] {note}" if note else f"[{pct}%]"
            else:
                if self.progress.cget("mode") != "indeterminate":
                    self.progress.config(mode="indeterminate")
                    try:
                        self.progress.start(12)
                    except Exception:
                        pass
                text = note or "处理中..."
            self.status.config(text=text)
            self.root.update_idletasks()
        except Exception:
            pass

    def _reset_progress(self):
        try:
            if self.progress.cget("mode") == "indeterminate":
                self.progress.stop()
                self.progress.config(mode="determinate")
            self.progress.config(maximum=100, value=0)
        except Exception:
            pass

    def refresh_all(self):
        threading.Thread(target=self._refresh_java_bg, daemon=True).start()
        threading.Thread(target=self._refresh_versions_bg, daemon=True).start()

    def _refresh_java_bg(self):
        self.after_safe(0, lambda: self.java_status.config(text="检测 Java 中..."))
        det = detect_java()
        self.after_safe(0, lambda: self._apply_java(det))

    def _apply_java(self, det):
        self.detected_java = det
        names = [f"Java {v}  @  {p}" for v, p in det]
        self.java_combo["values"] = names
        if det:
            self.java_status.config(text=f"检测到 {len(det)} 个 Java", foreground="#2a7a2a")
            if not self.java_var.get():
                self.java_var.set(names[0])
        else:
            self.java_status.config(text="未检测到 Java，请下载或安装", foreground="#c0392b")

    def _refresh_versions_bg(self):
        self.after_safe(0, lambda: self._set_status("扫描本地版本..."))
        try:
            self.installed = installed_versions()
            self._log(f"扫描到 {len(self.installed)} 个版本")
        except Exception as _exc:
            self.after_safe(0, lambda msg=repr(_exc): self._log(f"扫描失败: {msg}"))
            import traceback; traceback.print_exc()
            self.installed = []
        self.versions = [(v, "local", v, None) for v in self.installed]
        self._ver_item_map = {}
        def apply():
            for item in self.ver_tree.get_children():
                self.ver_tree.delete(item)
            for v in self.versions:
                iid = self.ver_tree.insert("", "end", values=(v[0], "local", "[OK] 已下载"))
                self._ver_item_map[iid] = v
            self._log(f"版本列表已刷新: {len(self.versions)} 项")
            if self.ver_tree.get_children():
                first = self.ver_tree.get_children()[0]
                self.ver_tree.selection_set(first)
                self.ver_tree.see(first)
            self._set_status(f"就绪 (共 {len(self.versions)} 个版本)")
            self.after_safe(0, self._refresh_dl_list)
        self.after_safe(0, apply)

    def _ver_label(self, v):
        return v[0]

    def _on_version_select(self, evt):
        pass

    def _update_version_mark(self):
        pass

    def _download_all_release(self):
        """批量补全所有本地版本的缺失文件 (json+jar+lib+assets)"""
        if not self.versions:
            messagebox.showinfo("提示", "没有本地版本需要补全")
            return
        if not messagebox.askyesno("确认", f"将补全 {len(self.versions)} 个版本的文件，是否继续?"):
            return
        self.busy = True
        self._save_settings()
        apply_paths(self.config)
        threading.Thread(target=self._batch_download, daemon=True).start()

    def _batch_download(self):
        for v in self.versions:
            if not self.busy:
                break
            version_id = v[0]
            try:
                self.after_safe(0, lambda vid=version_id: self._set_status(f"补全 {vid}..."))
                # 统一走 lib 补全（缺什么下什么，幂等），进度上 Tk
                if MCL_OK:
                    mcl_install_minecraft(mcl_minecraft_version(version_id), MINECRAFT_DIR,
                                          _make_mcl_callback(progress_cb=(
                                              lambda c, t, n=None, _vid=version_id:
                                              self.after_safe(0, lambda: self._set_progress(c, t,
                                                     f"补全 {_vid} ({n})" if n else f"补全 {_vid}")))))
                else:
                    # 回退：老逻辑（manifest url）
                    vdir = VERSIONS_DIR / version_id
                    meta = json.loads((vdir / f"{version_id}.json").read_text(encoding="utf-8"))
                    url = meta.get("url")
                    if url:
                        download_minecraft_version(version_id, url,
                            lambda c, t, n=None: self.after_safe(0, lambda: self._set_progress(c, t, n)),
                            lambda m: self.after_safe(0, lambda: self._log(m)))
            except Exception:
                pass
            except Exception as _exc:
                self.after_safe(0, lambda msg=repr(_exc): self._log(f"补全失败: {msg}"))
        self.busy = False
        self.after_safe(0, self._refresh_versions_bg)

    def refresh_java(self):
        self._refresh_java_bg()

    def _selected_version(self):
        """从版本列表或下载列表中获取选中的版本。返回 (id, type, url) 或 None。"""
        # 优先版本列表
        sel = self.ver_tree.selection()
        if sel:
            v = self._ver_item_map.get(sel[0])
            if v:
                return v
        # 其次下载列表
        sel = self.dl_tree.selection()
        if sel:
            return self._dl_item_map.get(sel[0])
        return None

    def _selected_java(self):
        sel = self.java_var.get()
        for ver, path in self.detected_java:
            if sel.startswith("Java"):
                if path in sel:
                    return path
            if sel == ver:
                return path
        # 最后兜底
        if self.detected_java:
            return self.detected_java[0][1]
        return None

    def download_java(self):
        """下载 Java — 弹窗可选择不同 Java 大版本（默认选当前版本所需的）。"""
        if self.busy:
            return
        v = self._selected_version()
        recommended = mc_to_java_major(v[0]) if v else 17
        choice = self._pick_java_major_dialog(recommended)
        if choice is None:
            return
        self.download_java_with_major(choice)

    def _pick_java_major_dialog(self, recommended):
        """弹出 Java 大版本选择对话框。返回选择的 major (int) 或 None(取消)。"""
        import tkinter as tk
        dlg = tk.Toplevel(self.root)
        dlg.title("选择要下载的 Java 大版本")
        dlg.resizable(False, False)
        majors = [8, 11, 16, 17, 21, 25]
        var = tk.IntVar(value=recommended if recommended in majors else 17)
        box = ttk.Frame(dlg, padding=12)
        box.pack()
        ttk.Label(box, text="选择要下载的 Java 大版本 (基于 Adoptium Temurin):").pack(anchor="w")
        # 用 radio 排列
        for mj in majors:
            note = " (当前版本推荐)" if mj == recommended else ""
            ttk.Radiobutton(box, text=f"Java {mj}{note}",
                           variable=var, value=mj).pack(anchor="w", padx=4, pady=2)
        chosen = {"v": None}
        def on_ok():
            chosen["v"] = var.get()
            dlg.destroy()
        def on_cancel():
            chosen["v"] = None
            dlg.destroy()
        bf = ttk.Frame(box)
        bf.pack(pady=8)
        ttk.Button(bf, text="下载", command=on_ok).pack(side="left", padx=4)
        ttk.Button(bf, text="取消", command=on_cancel).pack(side="left", padx=4)
        self.root.wait_window(dlg)
        return chosen["v"]

    def download_java_with_major(self, java_major):
        """下载并安装指定大版本的 Java（供外部/Java检查调用）。"""
        if self.busy:
            print(f"[Java 下载] 跳过：系统忙碌中，请求 Java {java_major}", flush=True)
            return
        self._save_settings()
        apply_paths(self.config)
        self.busy = True
        self._java_download_in_progress = True
        threading.Thread(target=self._download_java_bg, args=(java_major,), daemon=True).start()

    def _download_java_bg(self, java_major):
        def cb(cur, tot):
            self.after_safe(0, lambda: self._set_progress(cur, tot, f"下载 Java {java_major}..."))
        self.after_safe(0, lambda: self._set_status("解析 Java 下载地址..."))
        try:
            urls, fname = get_java_download_url(java_major)
        except Exception as _exc:
            self.after_safe(0, lambda msg=repr(_exc):
                            messagebox.showerror("错误", f"获取 Java 下载地址失败:\n{msg}"))
            self.after_safe(0, self._java_download_finished)
            return
        if isinstance(urls, str):
            urls = [urls]
        if not urls:
            self.after_safe(0, lambda: messagebox.showerror("错误", "获取 Java 下载地址失败：无可用直链"))
            self.after_safe(0, self._java_download_finished)
            return
        dest = JAVA_DIR / fname
        self.after_safe(0, lambda: self._log(f"下载 {fname}"))
        # 依次尝试候选直链（清华 -> 华为云 -> 官方），任一成功即止
        last_err = None
        for i, url in enumerate(urls, 1):
            try:
                self.after_safe(0, lambda i=i: self._log(f"下载 {fname}（候选 {i}/{len(urls)}）..."))
                Net.download(url, dest, cb, timeout=30)
                break
            except Exception as e:
                last_err = e
                try:
                    part = dest.with_suffix(dest.suffix + ".part")
                    if part.is_file():
                        part.unlink()
                except OSError:
                    pass
                self.after_safe(0, lambda e=e, i=i: self._log(f"候选 {i} 下载失败，尝试下一个: {e}"))
                continue
        else:
            # 所有候选均失败
            if last_err and ("10060" in str(last_err) or "超时" in str(last_err)
                             or "timed out" in str(last_err).lower()):
                self.after_safe(0, lambda: messagebox.showerror(
                    "下载超时",
                    "下载超时，请检查网络或手动下载 Java。\n\n"
                    f"目标文件：{fname}\n已尝试 {len(urls)} 个候选直链均超时。\n\n"
                    "可手动从清华镜像下载：\n"
                    "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/\n"
                    f"下载后放入 Java 目录（{JAVA_DIR}），再点「重新检测」。"))
            elif last_err:
                self.after_safe(0, lambda: messagebox.showerror(
                    "下载失败",
                    f"Java 下载失败：{last_err}\n\n已尝试 {len(urls)} 个候选直链。\n"
                    f"可手动下载 {fname} 后放入 {JAVA_DIR} 再重新检测。"))
            else:
                self.after_safe(0, lambda: messagebox.showerror("下载失败", "Java 下载失败：无可用直链"))
            self.after_safe(0, self._java_download_finished)
            return
        self.after_safe(0, lambda: self._log(f"下载完成: {dest.name}"))
        self.after_safe(0, lambda: self._set_status("准备安装 Java..."))
        # 尝试安装 (zip 解压 / msi 静默)
        self.after_safe(0, lambda: self._install_java(dest, java_major))

    def _java_download_finished(self):
        """Java 下载/安装线程收尾：解除 busy 与「下载中」标志。"""
        self.busy = False
        self._java_download_in_progress = False
        self.after_safe(0, self._reset_progress)

    def _install_java(self, dest, java_major):
        lower = str(dest).lower()
        if lower.endswith(".zip"):
            # 直接解压到 JAVA_DIR 供启动器使用
            try:
                import zipfile
                self._log("解压 Java...")
                extract_dir = JAVA_DIR / (dest.stem + "-extracted")
                extract_dir.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(dest) as z:
                    z.extractall(extract_dir)
                self._log(f"解压完成: {extract_dir}")
                self._set_status("解压完成，正在刷新检测...")
                self.after_safe(0, self._java_download_finished)
                self.after_safe(2000, self.refresh_java)
                return
            except Exception as e:
                messagebox.showerror("解压失败", str(e))
        elif lower.endswith(".msi"):
            if messagebox.askyesno("安装", "检测到 MSI 安装包，是否尝试静默安装?"):
                try:
                    subprocess.Popen(["msiexec", "/i", str(dest), "/qn"],
                                     cwd=str(JAVA_DIR))
                    self._log("已启动静默安装，安装后点击「重新检测」")
                except Exception as e:
                    messagebox.showerror("安装失败", str(e))
        else:
            messagebox.showinfo("下载完成", f"安装包已保存到:\n{dest}\n请手动安装后点击「重新检测」")
        self.after_safe(0, self._java_download_finished)
        self.after_safe(3000, self.refresh_java)

    def _on_dl_select(self, evt):
        """下载列表选择时记录选中项。"""
        pass
    def _open_version_detail(self):
        if self.busy:
            return
        
        # 1. 拿到当前可能选中的所有项
        sel = self.ver_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选择一个版本")
            return
        
        # 2. 取最后一个（通常是你刚才点中的那个）
        item_id = sel[-1]
        v = self._ver_item_map.get(item_id)
        if not v:
            return
        
        version_id = v[0]
        print(f"【真实选中】即将下载的版本: {version_id}")
        
        # 3. 弹窗二次确认，防止 Tkinter 又发癫！
        confirm = messagebox.askyesno("版本确认", f"你准备为哪个版本创建实例/下载？\n\n👉 {version_id} 👈\n\n（如果不对，请点取消，并在列表里重新单击选中！）")
        if not confirm:
            return
            
        # 4. 确认无误后，进入下载
        self._download_new_with_loader_choice(version_id, None)
    def _download_mc_then_install_loader(self, version_id, url,
                                         loader_kind, loader_ver, dl_url):
        """后台先下 MC 本体，完成后自动安装所选加载器。"""
        def cb(cur, tot, note=None):
            self.after_safe(0, lambda: self._set_progress(cur, tot,
                                                           f"下载 {version_id} 本体... ({note})"
                                                           if note else f"下载 {version_id} 本体..."))
        def log(m):
            self.after_safe(0, lambda: self._log(m))
        self.after_safe(0, lambda: self._set_status(f"下载 {version_id} 本体..."))
        try:
            if url:
                download_minecraft_version(version_id, url, cb, log)
            else:
                # 已安装则跳过本体下载
                log(f"{version_id} 已安装，直接安装加载器")
            self.installed.append(version_id)
        except Exception as e:
            def err():
                self.busy = False
                self.after_safe(0, self._reset_progress)
                self.after_safe(0, lambda: self._set_status("下载失败"))
                messagebox.showerror("下载失败", str(e))
            self.after_safe(0, err)
            return
        # 本体完成 → 安装加载器（统一走 lib，进度上 Tk）
        self._install_loader_for_version(version_id, loader_kind, loader_ver, dl_url)

    def download_version(self):
        if self.busy:
            return
        v = self._selected_version()
        if not v:
            messagebox.showwarning("提示", "请选择一个版本")
            return
        version_id = v[0]
        # 管它有没有 url，直接进下载逻辑！
        self._download_new_with_loader_choice(version_id, None)

    def _resolve_version_url(self, version_id, url):
        """url 为空时，从在线清单反查该版本的真实下载地址；查不到返回 None。"""
        if url:
            return url
        try:
            for vid, _vtype, vurl in mcl_version_list():
                if vid == version_id and vurl:
                    return vurl
        except Exception:
            pass
        return None

    def _repair_version_lib(self, version_id):
        """后台用 lib 校验/补全已安装版本。

        - 纯 MC 版本（'1.21.1'）：直接 lib install（幂等，缺什么补什么）
        - 复合加载器版本（'1.20.1-Fabric 0.18.1'）：补全其继承的本体 + 对应加载器
        """
        def _log(m):
            self.after_safe(0, lambda x=m: self._log(x))
        pure = mcl_minecraft_version(version_id)
        def _progress_cb(cur, tot, note=None):
            self.after_safe(0, lambda c=cur, t=tot, n=note:
                            self._set_progress(c, t, f"校验/补全 {version_id}" + (f" ({n})" if n else "")))
        try:
            if pure == version_id:
                # 纯原版：lib 补全
                _log(f"校验 {version_id} 本体 ...")
                mcl_install_minecraft(version_id, MINECRAFT_DIR, _make_mcl_callback(progress_cb=_progress_cb))
            else:
                # 复合版本：先补本体（纯版本），再补对应加载器
                _log(f"校验 {version_id}：先补本体 {pure} ...")
                mcl_install_minecraft(pure, MINECRAFT_DIR, _make_mcl_callback(progress_cb=_progress_cb))
                # 识别该复合版本用的加载器种类
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
                    _log(f"补全 {lk} 加载器 ...")
                    mcl_install_loader(lk, pure, MINECRAFT_DIR, progress_cb=_progress_cb)
            def done():
                self.busy = False
                self.after_safe(0, self._reset_progress)
                self.after_safe(0, lambda: self._set_status(f"{version_id} 校验/补全完成"))
                self.after_safe(0, self._refresh_versions_bg)
            self.after_safe(0, done)
        except Exception as _exc:
            _log(f"校验 {version_id} 失败: {_exc}")
            _errmsg = str(_exc)
            def err():
                self.busy = False
                self.after_safe(0, self._reset_progress)
                self.after_safe(0, lambda: self._set_status(f"{version_id} 校验失败"))
                messagebox.showerror("校验失败", _errmsg)
            self.after_safe(0, err)

    def _repair_selected(self):
        """「校验/补全所选版本」按钮入口。"""
        if self.busy:
            return
        v = self._selected_version()
        if not v:
            messagebox.showwarning("提示", "请先在版本列表里选择一个版本")
            return
        version_id = v[0]
        self._save_settings()
        apply_paths(self.config)
        self.busy = True
        threading.Thread(target=self._repair_version_lib, args=(version_id,),
                         daemon=True).start()

    def _download_new_with_loader_choice(self, version_id, url):
        """下载新版本：先选 原版/加载器 + 可选自定义实例名，再后台下载。

        默认实例名 = 版本 id；填了自定义名则产出 versions/<自定义名>/。
        实例名已存在 → 弹窗提示并终止，不进入下载。
        """
        choice = self._pick_loader_dialog(version_id, is_detail=True)
        if choice is None:
            return  # 用户取消
        loader_kind, loader_ver, custom_name = choice
        final_name = (custom_name or "").strip() or version_id
        if re.search(r'[\\/:*?"<>|]', final_name):
            messagebox.showwarning("实例名无效", "实例名包含非法字符（\\ / : * ? 双引号“” < > |），请换个名字")
            return
        if final_name in self.installed or (VERSIONS_DIR / final_name).is_dir():
            messagebox.showwarning("实例名已存在", f"实例名 {final_name} 已存在，请换个名字")
            return
        self._save_settings()
        apply_paths(self.config)
        self.busy = True
        threading.Thread(target=self._download_new_then_install,
                         args=(version_id, url, loader_kind, loader_ver, final_name),
                         daemon=True).start()

    def _download_new_then_install(self, version_id, url, loader_kind, loader_ver, final_name):
        """后台：下载 MC 本体 →（可选）装加载器 → 按自定义实例名产出 versions/<final_name>。"""
        def log(m):
            self.after_safe(0, lambda x=m: self._log(x))
        def _progress_cb(cur, tot, note=None):
            self.after_safe(0, lambda c=cur, t=tot, n=note:
                            self._set_progress(c, t, f"下载 {version_id}" + (f" ({n})" if n else "")))
        self.after_safe(0, lambda: self._set_status(f"下载 {version_id} 本体..."))
        self.after_safe(0, lambda: self._set_progress(0, 0, f"下载 {version_id} 本体..."))
        try:
            # 1) 游戏本体
            if MCL_OK:
                mcl_install_minecraft(version_id, MINECRAFT_DIR,
                                      _make_mcl_callback(progress_cb=_progress_cb))
            elif url:
                download_minecraft_version(version_id, url, _progress_cb, log)
            else:
                raise RuntimeError("无法获取下载地址，且 minecraft_launcher_lib 不可用")
            log(f"本体 {version_id} 安装完成")
            # 2) 加载器（可选）：lib 产出复合实例目录，如 '1.20.1-Fabric 0.18.1'
            src_id = version_id
            if loader_kind:
                src_id = mcl_install_loader(loader_kind, version_id, MINECRAFT_DIR,
                                            loader_version=loader_ver, progress_cb=_progress_cb)
                log(f"{loader_kind} 加载器已装: {src_id}")
                if loader_kind == "fabric":
                    _download_fabric_api_mc(src_id, log, _progress_cb)
            # 3) 自定义实例名：克隆为 versions/<final_name>（默认名无需克隆）
            if final_name and final_name != version_id:
                if not self._clone_version_instance(src_id, final_name):
                    raise RuntimeError(f"无法创建实例 {final_name}（可能已存在）")
                log(f"已创建自定义实例: {final_name}")
            def done():
                self.busy = False
                self.after_safe(0, self._reset_progress)
                self.after_safe(0, lambda: self._set_status(f"{version_id} 下载完成"))
                self.after_safe(0, self._refresh_versions_bg)
            self.after_safe(0, done)
        except Exception as e:
            def err():
                self.busy = False
                self.after_safe(0, self._reset_progress)
                self.after_safe(0, lambda: self._set_status("下载失败"))
                messagebox.showerror("下载失败", str(e))
            self.after_safe(0, err)

    def _clone_version_instance(self, src_id, new_id):
        """把已安装版本目录克隆为新实例名：复制目录 + 改写 json(id/文件名)。

        不删除源目录（保留原实例），用于「自定义实例名」下载。
        """
        src = VERSIONS_DIR / src_id
        dst = VERSIONS_DIR / new_id
        if not src.is_dir():
            return False
        if dst.exists():
            return False
        shutil.copytree(str(src), str(dst))
        old_jf = dst / f"{src_id}.json"
        new_jf = dst / f"{new_id}.json"
        if old_jf.is_file():
            try:
                data = json.loads(old_jf.read_text(encoding="utf-8"))
                data["id"] = new_id
                new_jf.write_text(json.dumps(data, indent=2), encoding="utf-8")
                old_jf.unlink()
            except Exception:
                pass
        return True

    def _download_new_then_ask_loader(self, version_id):
        """后台用 lib 下载 MC 版本本体，完成后弹出「下一页」加载器选择框。"""
        def log(m):
            self.after_safe(0, lambda x=m: self._log(x))
        def _progress_cb(cur, tot, note=None):
            self.after_safe(0, lambda c=cur, t=tot, n=note:
                            self._set_progress(c, t, f"下载 {version_id}" + (f" ({n})" if n else "")))
        self.after_safe(0, lambda: self._set_status(f"下载 {version_id} 本体..."))
        self.after_safe(0, lambda: self._set_progress(0, 0, f"下载 {version_id}..."))
        try:
            mcl_install_minecraft(version_id, MINECRAFT_DIR, _make_mcl_callback(progress_cb=_progress_cb))
            self.installed.append(version_id)
        except Exception as e:
            def err():
                self.busy = False
                self.after_safe(0, self._reset_progress)
                self.after_safe(0, lambda: self._set_status("下载失败"))
                messagebox.showerror("下载失败", str(e))
            self.after_safe(0, err)
            return
        # 下载完成 → 「下一页」：选择是否安装 mod 加载器
        self.after_safe(0, lambda: self._ask_loader_install(version_id))
    def _ask_loader_install(self, version_id):
        """下载完成后弹出加载器选择对话框（下一页）。"""
        self.busy = False
        self.after_safe(0, self._reset_progress)
        print(f"[下载完成] {version_id}，进入加载器选择", flush=True)
        choice = self._pick_loader_dialog(version_id)
        if choice is None:
            self.after_safe(0, self._refresh_versions_bg)
            self.after_safe(0, lambda: self._set_status(f"{version_id} 已下载为原版（完成）"))
            return
        loader_kind, loader_ver, custom_name = choice
        self.busy = True
        threading.Thread(target=self._install_loader_for_version,
                         args=(version_id, loader_kind, loader_ver, None, custom_name),
                         daemon=True).start()

    def _pick_loader_dialog(self, version_id, is_detail=False):
        """「版本详情页」/「下一页」：选择 原版/加载器 + 可选自定义实例名。

        返回 (kind, loader_ver, custom_name)（kind=None 表示原版），用户取消返回 None。
        is_detail=True 时标题/文案为「选择下载类型」；否则为「本体已下载，是否装加载器」。
        """
        import tkinter as tk
        mc_ver, _ = parse_mc_version(version_id)
        dlg = tk.Toplevel(self.root)
        if is_detail:
            dlg.title(f"{version_id} — 选择下载类型 (原版 / Mod加载器)")
            head = (f"版本 {version_id}。\n"
                    f"请选择下载类型：原版(不装加载器) 或 Mod加载器\n"
                    f"(原版=纯客户端；选加载器会先下本体再装加载器)")
        else:
            dlg.title(f"下载 {version_id} 完成 — 选择 Mod 加载器")
            head = (f"版本 {version_id} 本体已下载。\n"
                    f"请选择下载类型：原版(不装加载器) 或 Mod加载器")
        dlg.resizable(False, False)
        box = ttk.Frame(dlg, padding=12)
        box.pack()
        ttk.Label(box, text=head, wraplength=380,
                  font=("", 10, "bold")).grid(row=0, column=0, columnspan=3,
                                               sticky="w", pady=6)
        kind_var = tk.StringVar(value="none")
        ttk.Radiobutton(box, text="原版 (不装加载器)", variable=kind_var, value="none").grid(
            row=1, column=0, sticky="w", padx=4)
        ttk.Radiobutton(box, text="Forge", variable=kind_var, value="forge").grid(
            row=2, column=0, sticky="w", padx=4)
        ttk.Radiobutton(box, text="Fabric", variable=kind_var, value="fabric").grid(
            row=3, column=0, sticky="w", padx=4)
        ttk.Radiobutton(box, text="NeoForge", variable=kind_var, value="neoforge").grid(
            row=4, column=0, sticky="w", padx=4)
        # 加载器版本下拉
        ver_var = tk.StringVar()
        combo = ttk.Combobox(box, textvariable=ver_var, state="readonly", width=24)
        combo.grid(row=2, column=1, rowspan=3, sticky="w", padx=6, pady=4)
        ttk.Label(box, text="版本").grid(row=3, column=2, sticky="w")

        # 拉取对应加载器版本列表（懒加载 + 后台线程，不卡 UI；对话框已销毁则跳过写回）
        item_store = {"kind": "none", "items": []}

        def refresh_items(kind):
            if kind == "none":
                item_store["items"] = []
                combo["values"] = []
                ver_var.set("")
                return
            # 先清空下拉 + 显示「加载中」
            item_store["items"] = []
            combo["values"] = []
            ver_var.set("加载中...")
            combo["state"] = "disabled"

            def fetch_bg():
                try:
                    items = mcl_loader_versions(kind, mc_ver)  # [(loader_ver,)]
                    def apply():
                        if not dlg.winfo_exists():
                            return
                        item_store["items"] = items
                        combo["values"] = [it[0] for it in items]
                        if items:
                            ver_var.set(items[0][0])
                            combo["state"] = "readonly"
                        else:
                            ver_var.set("(未获取到)")
                            combo["state"] = "disabled"
                            print(f"[加载器列表] {kind} (MC {mc_ver}) 未获取到版本", flush=True)
                    self.after_safe(0, apply)
                except Exception as _exc:
                    def err_apply():
                        if not dlg.winfo_exists():
                            return
                        combo["state"] = "disabled"
                        ver_var.set(f"获取失败: {_exc}")
                    self.after_safe(0, err_apply)

            threading.Thread(target=fetch_bg, daemon=True).start()

        def on_kind_change(*_):
            refresh_items(kind_var.get())

        for r, txt in ((2, "Forge"), (3, "Fabric"), (4, "NeoForge")):
            ttk.Radiobutton(box, text=txt, variable=kind_var, value=txt.lower(),
                           command=on_kind_change).grid(row=r, column=0, sticky="w", padx=4)

        # 自定义实例名（可选）：留空 = 用原版本名
        name_var = tk.StringVar()
        ttk.Label(box, text="自定义实例名(可选):").grid(row=6, column=0, sticky="w", padx=4)
        ttk.Entry(box, textvariable=name_var, width=26).grid(row=6, column=1, columnspan=2,
                                                             sticky="w", padx=6, pady=(10, 0))
        ttk.Label(box, text="留空=使用原版本名；重名会自动提示换名",
                  foreground="#888").grid(row=7, column=0, columnspan=3, sticky="w", padx=4)

        chosen = {"res": None}

        def on_ok():
            kind = kind_var.get()
            custom = name_var.get().strip()
            if kind == "none" or not item_store["items"]:
                chosen["res"] = (None, None, custom)
            else:
                loader_ver = ver_var.get() or item_store["items"][0][0]
                chosen["res"] = (kind, loader_ver, custom)
            dlg.destroy()

        def on_cancel():
            chosen["res"] = None
            dlg.destroy()

        bf = ttk.Frame(box)
        bf.grid(row=8, column=0, columnspan=3, pady=8)
        ttk.Button(bf, text="下一步", command=on_ok).pack(side="left", padx=4)
        ttk.Button(bf, text="关闭", command=on_cancel).pack(side="left", padx=4)
        self.root.wait_window(dlg)
        return chosen["res"]

    def _install_loader_for_version(self, version_id, kind, loader_ver, url, custom_name=None):
        """后台安装所选加载器到指定版本；Fabric 自动追加 Fabric API。

        custom_name 非空时把最终加载器实例克隆为 versions/<custom_name>/。
        """
        kind_names = {"forge": "Forge", "fabric": "Fabric", "neoforge": "NeoForge"}
        status_label = kind_names.get(kind, kind)
        def log(m):
            self.after_safe(0, lambda: self._log(m))
            self.after_safe(0, lambda: self._set_status(
                f"安装 {status_label} 加载器... ({m})"))
        def cb(cur, tot, note=None):
            self.after_safe(0, lambda: self._set_progress(cur, tot,
                                                          f"安装 {status_label} 加载器... ({note})"
                                                          if note else f"安装 {status_label} 加载器..."))
        try:
            self.after_safe(0, lambda: self._set_status(f"安装 {status_label} 加载器..."))
            # 加载器统一走 lib 安装（进度上 Tk，目录规范：versions/纯版本/）
            pure = mcl_minecraft_version(version_id)
            installed = mcl_install_loader(kind, pure, MINECRAFT_DIR,
                                           loader_version=loader_ver,
                                           progress_cb=cb)
            log(f"加载器已装: {installed}")
            if kind == "fabric":
                _download_fabric_api_mc(installed or version_id, log, cb)
            if custom_name:
                if not self._clone_version_instance(installed or version_id, custom_name):
                    log(f"无法创建实例 {custom_name}（可能已存在）")
                else:
                    log(f"已创建自定义实例: {custom_name}")
            self.after_safe(0, lambda: self._reset_progress)
            self.after_safe(0, lambda: self._set_status(f"{version_id} {status_label} 加载器安装完成"))
            self.after_safe(0, self._refresh_versions_bg)
        except Exception as e:
            log(f"加载器安装失败: {e}")
            self.after_safe(0, lambda: self._set_status(f"{version_id} 加载器安装失败"))
        finally:
            self.busy = False

    def _install_version_lib(self, version_id, loader_kind, loader_ver):
        """后台用 lib 安装：先补本体，选加载器则再装加载器。进度/日志上 Tk。

        version_id: 版本目录名（可能是 '1.21.1' 或复合 '1.20.1-Fabric 0.18.1'）
        本体安装只传纯 MC 版本，保证每个版本隔离在 versions\\<纯版本>\\ 里。
        loader_kind: None | 'forge' | 'fabric' | 'neoforge'
        loader_ver: 加载器版本号（如 '21.1.34'），loader_kind 非 None 时使用
        """
        def _progress_cb(cur, tot, note=None):
            self.after_safe(0, lambda c=cur, t=tot, n=note:
                            self._set_progress(c, t, f"安装 {version_id}" + (f" ({n})" if n else "")))
        def _log(m):
            self.after_safe(0, lambda x=m: self._log(x))

        if not MCL_OK:
            def err():
                self.busy = False
                self.after_safe(0, self._reset_progress)
                messagebox.showerror("库未安装",
                    "minecraft_launcher_lib 未安装。\n请先运行:\n  pip install minecraft-launcher-lib")
            self.after_safe(0, err)
            return

        pure = mcl_minecraft_version(version_id)  # 本体安装用纯 MC 版本做目录隔离
        try:
            # 1) 补游戏本体（进度上 Tk）
            self.after_safe(0, lambda: self._set_status(f"安装 {pure} 本体..."))
            self.after_safe(0, lambda: self._set_progress(0, 0, f"安装 {pure} 本体..."))
            mcl_install_minecraft(pure, MINECRAFT_DIR, _make_mcl_callback(progress_cb=_progress_cb))
            _log(f"本体 {pure} 安装完成")

            # 2) 装加载器（可选，进度上 Tk）
            if loader_kind:
                self.after_safe(0, lambda: self._set_status(f"安装 {loader_kind} 加载器..."))
                mcl_install_loader(loader_kind, pure, MINECRAFT_DIR,
                                   loader_version=loader_ver,
                                   progress_cb=_progress_cb)

            def done():
                self.busy = False
                self.after_safe(0, self._reset_progress)
                self.after_safe(0, lambda: self._set_status(f"{version_id} 安装完成"))
                self.after_safe(0, self._refresh_versions_bg)
            self.after_safe(0, done)
        except Exception as e:
            _log(f"安装失败: {e}")
            def err2():
                self.busy = False
                self.after_safe(0, self._reset_progress)
                self.after_safe(0, lambda: self._set_status(f"{version_id} 安装失败"))
                messagebox.showerror("安装失败", str(e))
            self.after_safe(0, err2)

    def _download_new_bg(self, version_id, url):
        def cb(cur, tot, note=None):
            self.after_safe(0, lambda: self._set_progress(cur, tot, note))
        def log(m):
            self.after_safe(0, lambda: self._log(m))
        self.after_safe(0, lambda: self._set_status(f"下载 {version_id}..."))
        try:
            download_minecraft_version(version_id, url, cb, log)
            self.installed.append(version_id)
            def done():
                self.busy = False
                self._set_status(f"{version_id} 下载完成")
                self._refresh_versions_bg()
            self.after_safe(0, done)
        except Exception as e:
            def err():
                self.busy = False
                messagebox.showerror("下载失败", str(e))
            self.after_safe(0, err)

    def _download_single_bg(self, version_id):
        def cb(cur, tot, note=None):
            self.after_safe(0, lambda: self._set_progress(cur, tot, note))
        def log(m):
            self.after_safe(0, lambda: self._log(m))
        self.after_safe(0, lambda: self._set_status(f"补全 {version_id}..."))
        try:
            vdir = VERSIONS_DIR / version_id
            meta = json.loads((vdir / f"{version_id}.json").read_text(encoding="utf-8"))
            url = meta.get("url")
            if url:
                download_minecraft_version(version_id, url, cb, log)
            def done():
                self.busy = False
                self._set_status(f"{version_id} 补全完成")
                self._refresh_versions_bg()
            self.after_safe(0, done)
        except Exception as e:
            def err():
                self.busy = False
                messagebox.showerror("补全失败", str(e))
            self.after_safe(0, err)

    def launch(self):
        if self.busy:
            return
        v = self._selected_version()
        if not v:
            messagebox.showwarning("提示", "请选择一个版本")
            return
        if v[0] not in self.installed:
            messagebox.showwarning("提示", "该版本尚未下载，请先点击「下载所选版本」")
            return
        # 完整性检查
        ok, missing = check_version_complete(v[0])
        if not ok:
            messagebox.showwarning(
                "版本不完整",
                f"版本 {v[0]} 缺少:\n- " + "\n- ".join(missing) +
                "\n\n点击「下载所选版本」将自动补全所有文件(含 libraries/assets)。")
            return
        java = self._selected_java()
        if not java:
            messagebox.showwarning("提示", "未找到 Java，请先下载/安装 Java")
            return
        username = self.username_var.get().strip() or "Steve"
        if self.auto_mem_var.get():
            mem = auto_memory_mb()          # 自动分配：以系统内存为准
        else:
            mem = self.mem_var.get()        # 手动：用框内数值
        offline = self.offline_var.get()

        self._save_settings()

        # 启动前检查/匹配 Java 大版本（不满足时自动切到本机满足要求的 Java）
        java = self._check_java_for_version(v[0], java)

        self.busy = True
        lang = self.language_var.get()
        threading.Thread(target=self._launch_bg, args=(v[0], java, username, mem, offline, lang),
                         daemon=True).start()

    def _check_java_for_version(self, version_id, java_path):
        """启动前检查/匹配 Java 大版本。返回应使用的 java 路径。

        - 满足：返回原 java_path
        - 不满足：自动匹配本机已装且 >= required 的最小 Java；找不到则警告后放行
        """
        import re
        required = _required_java_major(version_id)
        cur_major = self._java_major_of(java_path)
        print(f"[Java 检查] {version_id} 需要 Java>={required}, 当前 {cur_major}", flush=True)
        if cur_major >= required:
            return java_path
        matched = self._auto_match_java(version_id, required)
        if matched:
            print(f"[Java 自动匹配] {version_id} -> {matched}", flush=True)
            return matched
        print(f"[Java 警告] {version_id} 需要 Java>={required}，当前 {cur_major}，"
              f"仍继续启动（若游戏闪退请下载对应 Java）", flush=True)
        return java_path

    def _java_major_of(self, java_path):
        """从路径或 java -version 输出解析大版本号。返回 int，无法解析返回 0。"""
        import re
        # 先从路径猜
        p = str(java_path)
        # jdk-21.0.6.7-hotspot / jdk17 / jdk-17.0.12 等
        m = re.search(r"jdk[-_]?(\d+)", p, re.IGNORECASE)
        if m:
            v = int(m.group(1))
            return 8 if v == 9 else v
        # 路径中带版本号 (25.0.4 / 1.8.0_392) 等
        m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?(?:_[\w]+)?", p)
        if m:
            v = int(m.group(1))
            # 1.x 体系：1.8 -> 8；否则直接取主版本
            minor = int(m.group(2))
            if v == 1:
                return 8 if minor >= 8 else minor
            return 8 if v <= 8 else v
        # 再执行 java -version
        try:
            import subprocess as sp
            out = sp.run([str(java_path), "-version"],
                         capture_output=True, text=True, timeout=5)
            txt = out.stderr + out.stdout
            # 现代: version "21.0.6" ; 老式: version "1.8.0_392"
            m = re.search(r'version "(\d+)(?:\.(\d+))?([^"]*)"', txt)
            if m:
                a = int(m.group(1))
                b = m.group(2)
                if a == 1:
                    # 1.x 体系
                    return 8 if (b and int(b) >= 8) else int(b or 8)
                return 8 if a <= 8 else a
        except Exception:
            pass
        # 最后兜底：跑 PATH 上的 java
        try:
            import subprocess as sp
            out = sp.run(["java", "-version"],
                         capture_output=True, text=True, timeout=5)
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

    def _launch_bg(self, version_id, java, username, mem, offline, language=None):
        def cb(cur, tot, note=None):
            self.after_safe(0, lambda: self._set_progress(cur, tot, note))
        def log(m):
            self.after_safe(0, lambda: self._log(m))
        self.after_safe(0, lambda: self._set_status("启动中..."))
        self.after_safe(0, self._reset_progress)
        uuid = "00000000-0000-0000-0000-000000000000" if offline else "00000000-0000-0000-0000-000000000000"
        access_token = "0"
        jvm_args_extra = self.jvm_var.get().strip() or None
        try:
            # 启动前把 natives 解压到版本目录（lib 的 -Djava.library.path 指向这里）
            mcl_extract_natives(version_id)
            # 启动前同步该版本的 mods/光影 到游戏根目录（PCL 式版本隔离）
            sync_version_mods(version_id)
            # 用 lib 生成启动命令（get_minecraft_command）
            # version_id 来自 get_installed_versions，是 versions/ 目录里的完整版本名
            # （可能是 '1.21.1'、'1.20.1-Fabric 0.18.1'、'neoforge-21.1.234' 等），直接传入
            cmd = mcl_build_launch_cmd(version_id, java, username, mem, jvm_args_extra)
            log(f"启动命令已生成 ({len(cmd)} 个参数):")
            log(" ".join(cmd))
        except Exception as e:
            def err():
                self.busy = False
                self.after_safe(0, lambda: self._set_status("启动失败"))
                messagebox.showerror("启动失败", f"生成启动命令失败: {e}\n\n"
                    f"版本: {version_id}\n"
                    f"如需补全文件，请先点击「下载所选版本」补全。")
            self.after_safe(0, err)
            return
        # 打开独立的「游戏启动日志」窗口
        win, wlog = self.open_game_log_window(version_id)
        self._game_log_win = getattr(self, "_game_log_win", {})
        self._game_log_win[version_id] = win
        wlog(" ".join(cmd))

        # 启动游戏进程，实时把 stdout/stderr 写入日志窗口 + 控制台
        def run():
            try:
                proc = subprocess.Popen(cmd, cwd=str(MINECRAFT_DIR),
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT,
                                        creationflags=subprocess.CREATE_NO_WINDOW
                                        if os.name == "nt" else 0)
                # 记录运行中的进程（供日志窗口「结束进程」按钮使用）
                if not hasattr(self, "_running_proc"):
                    self._running_proc = {}
                self._running_proc[version_id] = proc
                # 线程读取合并输出
                def reader():
                    try:
                        for line in iter(proc.stdout.readline, b""):
                            text = line.decode("utf-8", errors="replace")
                            wlog(text)
                            print(f"[MC {version_id}] {text}", end="", flush=True)
                    except Exception:
                        pass
                rd = threading.Thread(target=reader, daemon=True)
                rd.start()

                def fin():
                    self.busy = False
                    ret = proc.returncode
                    if getattr(self, "_running_proc", {}).get(version_id) is proc:
                        self._running_proc[version_id] = None
                    if ret != 0:
                        self.after_safe(0, lambda: wlog(f"=== 游戏异常退出 (代码 {ret}) ==="))
                        messagebox.showwarning(
                            "游戏退出",
                            f"游戏以代码 {ret} 退出\n\n"
                            f"请查看「游戏日志」窗口，可能是 Java 版本不兼容"
                            f"或版本文件缺失。")
                    else:
                        self.after_safe(0, lambda: wlog("=== 游戏已正常退出 ==="))
                    self._close_game_log(version_id)
                proc.wait()
                self.after_safe(0, fin)
            except Exception as e:
                def err():
                    self.busy = False
                    wlog(f"启动失败: {e}")
                    messagebox.showerror("启动失败", str(e))
                self.after_safe(0, err)
        threading.Thread(target=run, daemon=True).start()
        self.after_safe(0, lambda: self._set_status("游戏启动中... (日志见独立窗口)"))


def main():
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    root = Tk()
    LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
