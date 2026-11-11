# -*- coding: utf-8 -*-
"""mojang_mirror_patch.py

把 minecraft_launcher_lib 里走 Mojang 官方域名的下载，在被墙/失败时自动回退到
BMCLAPI2 镜像（国内可直连）。单独放在本文件，库升级后删掉即可。

原理：lib 所有 vanilla 下载都走 minecraft_launcher_lib._helper.download_file，
这里包装它：原 URL 失败（SSL EOF / 非 200 / checksum 不匹配）时，把域名换成
BMCLAPI2 镜像再试一次。每个下载成功即可（跳过已存在文件逻辑不受影响）。

镜像映射：
  piston-data.mojang.com            -> bmclapi2.bangbang93.com   （路径相同）
  piston-meta.mojang.com            -> bmclapi2.bangbang93.com   （路径相同）
  launcher.mojang.com               -> bmclapi2.bangbang93.com   （路径相同）
  resources.download.minecraft.net  -> bmclapi2.bangbang93.com/assets（路径后段相同）
  libraries.minecraft.net           -> bmclapi2.bangbang93.com/maven（路径后段相同）
  其他 lwjgl 等（libraries.minecraft.net 之外的）保持原样/走 BMCLAPI /maven。
"""
import os
import re
import sys

_BMC = "https://bmclapi2.bangbang93.com"

# 需要关掉 SSL 校验（BMCLAPI 证书自签名）+ 加 UA
def _session():
    import urllib3
    import requests
    urllib3.disable_warnings()
    s = requests.Session()
    s.verify = False
    s.headers["User-Agent"] = "PyMC-Launcher/1.0"
    return s


def _mirror_url(url: str) -> str:
    """把 Mojang 官方域名换成 BMCLAPI2 镜像地址。非 Mojang 域名原样返回。"""
    if "piston-data.mojang.com" in url or "piston-meta.mojang.com" in url or "launcher.mojang.com" in url:
        # BMCLAPI 与官方路径相同，仅换 host
        return re.sub(r"https?://[^/]+/", f"{_BMC}/", url, count=1)
    if "resources.download.minecraft.net" in url:
        # /<hh>/<hash> -> /assets/<hh>/<hash>
        m = re.search(r"resources\.download\.minecraft\.net/(\w{2}/\w+)", url)
        if m:
            return f"{_BMC}/assets/{m.group(1)}"
    if "libraries.minecraft.net" in url:
        # /path/... -> /maven/path/...
        m = re.search(r"libraries\.minecraft\.net/(.+)$", url)
        if m:
            return f"{_BMC}/maven/{m.group(1)}"
    # 其他（lwjgl 等第三方库走 libraries.minecraft.net 之外，或已是指定 url）
    return url


_ORIGINAL = None  # 保存原始 download_file（避免递归）


def _patched_download_file(url, path, callback=None, sha1=None, lzma_compressed=False,
                           session=None, minecraft_directory=None, overwrite=False):
    """包装 lib 的 download_file：原 URL 失败时用 BMCLAPI 镜像重试。"""
    from minecraft_launcher_lib import _helper as H
    original = _ORIGINAL

    # 已存在且校验通过则直接返回（原逻辑）
    import os as _os
    if os.path.isfile(path) and not overwrite:
        try:
            if sha1 is None or H.get_sha1_hash(path) == sha1:
                return False
        except Exception:
            pass

    # 先试官方
    try:
        return original(url, path, callback, sha1, lzma_compressed, session,
                        minecraft_directory, overwrite)
    except Exception:
        pass

    # 官方失败 -> 尝试镜像
    murl = _mirror_url(url)
    if murl == url:
        # 没有可替换的镜像地址，把异常抛出（让上层显示）
        return original(url, path, callback, sha1, lzma_compressed, session,
                        minecraft_directory, overwrite)

    print(f"[镜像回退] {os.path.basename(path)}: 官方失败，改用 BMCLAPI2", flush=True)
    # 用独立 session（BMCLAPI 需 verify=False）
    try:
        r = _session().get(murl, stream=True, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"BMCLAPI2 HTTP {r.status_code}")
        try:
            os.makedirs(os.path.dirname(path))
        except Exception:
            pass
        with open(path, 'wb') as f:
            r.raw.decode_content = True
            if lzma_compressed:
                import lzma
                f.write(lzma.decompress(r.content))
            else:
                import shutil as _shutil
                _shutil.copyfileobj(r.raw, f)
        if sha1 is not None:
            if H.get_sha1_hash(path) != sha1:
                raise RuntimeError("BMCLAPI2 checksum 不匹配")
        return True
    except Exception as e:
        # 镜像也失败：抛出原始可读异常
        print(f"[镜像回退] BMCLAPI2 也失败: {e}", flush=True)
        raise


def apply():
    """打补丁：替换 lib 的 download_file。"""
    global _ORIGINAL
    try:
        from minecraft_launcher_lib import _helper as H
        if getattr(H, "_mojang_mirror_patched", False):
            return
        _ORIGINAL = H.download_file  # 保存原始函数，避免递归
        H.download_file = _patched_download_file
        H._mojang_mirror_patched = True
        print("[mojang_mirror_patch] 已启用 BMCLAPI2 下载镜像回退", flush=True)
    except Exception as e:
        print(f"[mojang_mirror_patch] 未启用（lib 不可用?）：{e}", flush=True)


# 模块导入即打补丁
if __name__ != "__main__":
    try:
        apply()
    except Exception:
        pass