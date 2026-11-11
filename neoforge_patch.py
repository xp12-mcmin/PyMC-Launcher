"""
neoforge_patch.py — 把 minecraft_launcher_lib 的 NeoForge 源切到 BMCLAPI2。

原因：maven.neoforged.net 在国内直连超时。BMCLAPI2 提供同样的 Maven 目录结构：
  版本列表: https://bmclapi2.bangbang93.com/maven/net/neoforged/neoforge/maven-metadata.xml
  installer: https://bmclapi2.bangbang93.com/maven/net/neoforged/neoforge/{ver}/neoforge-{ver}-installer.jar

本文件只做 monkeypatch，不修改库源码。库升级后若 API 变了，直接删掉本文件并
把 mc_launcher.py 里对 neoforge_patch 的 import 去掉即可回退官方源。

用法（在 mc_launcher.py 顶部）：
    try:
        import neoforge_patch
    except Exception:
        pass
"""

import re
import time
import xml.etree.ElementTree as ET

try:
    import requests
except ImportError:
    requests = None  # requests 是 minecraft_launcher_lib 的依赖，缺失时 patch 直接跳过

try:
    from minecraft_launcher_lib.mod_loader import _neoforge as _NF
    _NF_AVAILABLE = True
except Exception:
    _NF_AVAILABLE = False


_BMCLAPI_MAVEN = "https://bmclapi2.bangbang93.com/maven"
_BMCLAPI_METADATA = f"{_BMCLAPI_MAVEN}/net/neoforged/neoforge/maven-metadata.xml"

_session = None
_metadata_cache = None   # 解析后的版本列表（按出现顺序）
_metadata_ts = 0.0


def _get_session():
    global _session
    if _session is None:
        import urllib3
        urllib3.disable_warnings()
        _session = requests.Session()
        _session.verify = False  # BMCLAPI 自签证书
    return _session


def _fetch_metadata():
    """拉 BMCLAPI 的 maven-metadata.xml，返回 NeoForge 全量版本列表。带 60s 缓存。"""
    global _metadata_cache, _metadata_ts
    now = time.time()
    if _metadata_cache is not None and now - _metadata_ts < 60:
        return _metadata_cache
    resp = _get_session().get(_BMCLAPI_METADATA, timeout=15,
                              headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    versions = [v.text for v in root.iter("version") if v.text]
    _metadata_cache = versions
    _metadata_ts = now
    return versions


def _neo_version_key(v: str):
    """NeoForge 版本排序键：'21.1.34' -> (21, 1, 34)。"""
    return tuple(int(p) for p in v.split(".") if p.isdigit())


def _match_mc_prefix(mc_version: str) -> str:
    """MC '1.21.1' -> NeoForge 前缀 '21.1.'；MC '1.20.1' -> '20.1.'。

    库内部 _normalize_minecraft_version：NeoForge 世纪号 '21.1' -> MC '1.21.1'，
    即 NeoForge 前两位 == MC 版本号去掉 '1.' 前缀。
    """
    m = re.match(r"1\.([\d.]+)", mc_version or "")
    return (m.group(1) + ".") if m else (mc_version or "")


# ------------------------------------------------------------------
# patch 各方法
# ------------------------------------------------------------------

def _patched_get_minecraft_versions(self, stable_only: bool):
    """替换 get_minecraft_versions：从 BMCLAPI 的 maven-metadata 解析。"""
    versions = _fetch_metadata()
    seen = set()
    for v in versions:
        m = _NF._VERSION_REG_EX.match(v)
        if not m:
            continue
        normalized = self._normalize_minecraft_version(m.group())
        if normalized.startswith("0."):
            continue
        seen.add(normalized)
    return list(seen)


def _patched_get_loader_versions(self, minecraft_version: str, stable_only: bool):
    """替换 get_loader_versions：从 BMCLAPI 的 maven-metadata 解析，按 MC 版本过滤。"""
    versions = _fetch_metadata()
    prefix = _match_mc_prefix(minecraft_version)
    result = []
    for v in versions:
        if "beta" in v and stable_only:
            continue
        if not v.startswith(prefix):
            continue
        result.append(v)
    result.sort(key=_neo_version_key, reverse=True)
    return result


def _patched_get_installer_url(self, minecraft_version: str, loader_version: str) -> str:
    """替换 get_installer_url：走 BMCLAPI 的 maven 目录。"""
    return (f"{_BMCLAPI_MAVEN}/net/neoforged/neoforge/"
            f"{loader_version}/neoforge-{loader_version}-installer.jar")


def apply():
    """实际打 patch。库未装或类结构变了就静默跳过。"""
    if not _NF_AVAILABLE or _NF is None:
        print("[neoforge_patch] minecraft_launcher_lib.mod_loader._neoforge 不可用，跳过",
              flush=True)
        return
    if requests is None:
        print("[neoforge_patch] requests 未安装，跳过 NeoForge 源切换", flush=True)
        return
    _NF.Neoforge.get_minecraft_versions = _patched_get_minecraft_versions
    _NF.Neoforge.get_loader_versions = _patched_get_loader_versions
    _NF.Neoforge.get_installer_url = _patched_get_installer_url
    print("[neoforge_patch] NeoForge 源已切到 BMCLAPI2: " + _BMCLAPI_METADATA, flush=True)


apply()
