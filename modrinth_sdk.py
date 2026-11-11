# -*- coding: utf-8 -*-
"""modrinth_sdk.py

基于 Modrinth 官方 Python SDK (pip install modrinth) 的封装 + MC百科中文搜索。

用法：
    from modrinth_sdk import patch_sdk, mcmod_chinese_lookup, modrinth_search_sdk,
        resolve_version_sdk, download_to_dir

特性：
1. 中文搜索：先调 MC百科 API (https://mcmod-api.zkitefly.eu.org/s/key=xxx) 拿英文名
   (data.sub_name)，再用英文名去 Modrinth 官方库搜索。
2. 搜索渲染：返回 {id, slug, name, desc, icon_url, project_id} 列表。
3. 一键下载：按 游戏版本 + 加载器 选最新版本，返回 (url, filename)。
4. 自动装前置：解析 dependencies，返回 required 依赖列表。

注意：官方 SDK 内部用裸 requests.get，本机 GFW 会 SSL EOF。这里把 modrinth.requests
替换成 带 verify=False + 重试 的 session，使其可用。
"""

import os
import re

# ---------------------------------------------------------------------------
# 让官方 SDK 的 requests 走可用通道（verify=False + 重试 + UA）
# ---------------------------------------------------------------------------
def _make_session():
    import requests
    import urllib3
    urllib3.disable_warnings()
    s = requests.Session()
    s.verify = False
    s.headers["User-Agent"] = "PyMC-Launcher/1.0"
    adapter = requests.adapters.HTTPAdapter(max_retries=3)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


_session = None


def get_session():
    global _session
    if _session is None:
        _session = _make_session()
    return _session


def patch_sdk():
    """把 modrinth.requests.get 换成走工作 session 的版本。幂等。"""
    try:
        import modrinth
        if getattr(modrinth, "_sdk_patched", False):
            return True
        orig_get = modrinth.requests.get

        def patched_get(url, *a, **kw):
            kw.setdefault("timeout", 20)
            kw.pop("params", None)  # 防止与 url 里的 query 冲突（SDK 都是拼进 url）
            return get_session().get(url, *a, **kw)

        modrinth.requests.get = patched_get
        modrinth._sdk_patched = True
        return True
    except Exception as e:
        print(f"[modrinth_sdk] SDK 打补丁失败: {e}", flush=True)
        return False


# ---------------------------------------------------------------------------
# MC百科 中文 -> 英文
# ---------------------------------------------------------------------------
def mcmod_chinese_lookup(keyword: str, limit=12):
    """调 MC百科 API 把中文关键词转成英文名列表。找不到返回空列表。

    返回 [{chinese, english, mcmod_id}]，english 来自 data.sub_name（英文名）。
    """
    import json
    result = []
    if not keyword or not re.search(r"[\u4e00-\u9fff]", keyword):
        return result
    url = f"https://mcmod-api.zkitefly.eu.org/s/key={keyword}"
    try:
        r = get_session().get(url, timeout=25)
        if r.status_code != 200:
            return result
        data = r.json()
        for it in (data if isinstance(data, list) else []):
            d = it.get("data") or {}
            en = d.get("sub_name")
            chn = d.get("chinese_name") or ""
            mid = d.get("mcmod_id")
            # 只用有英文名的（sub_name 有值），过滤纯物品/方块条目
            if en and isinstance(en, str) and en.strip():
                result.append({"chinese": chn, "english": en.strip(), "mcmod_id": mid})
            if len(result) >= limit:
                break
    except Exception as e:
        print(f"[MC百科] 查询失败: {e}", flush=True)
    return result


# ---------------------------------------------------------------------------
# Modrinth 搜索（用官方 SDK）
# ---------------------------------------------------------------------------
def modrinth_search_sdk(query, game_version=None, loader=None, limit=15, offset=0,
                        index="relevance"):
    """用官方 SDK 搜索 mod。返回 [{id, slug, name, desc, icon_url, project_id}]。

    query 传英文名；game_version/loader 用于 facets 过滤。
    offset 用于分页（第一页 0，第二页 limit，…）。
    index: 排序方式，relevance(默认/热门) | downloads | updated。
    query 为空字符串时，Modrinth API 返回当前最热门的项目列表。
    """
    from modrinth import Projects
    if not patch_sdk():
        return []
    try:
        versions = [game_version] if game_version else []
        categories = [loader] if loader and loader != "all" else []
        S = Projects.Search(query or "", versions=versions, categories=categories,
                            project_types=["mod"], limit=limit, offset=offset)
        out = []
        for p in S.hits:
            out.append({
                "project_id": p.id,
                "id": p.id,
                "slug": p.slug,
                "name": p.name,
                "desc": (p.desc or "")[:200],
                "icon_url": p.iconURL,
                "game_versions": list(getattr(p, "versions", []) or []),
            })
        return out
    except Exception as e:
        print(f"[modrinth_sdk] 搜索失败: {e}", flush=True)
        return []


def project_versions_sdk(project_id):
    """取某项目全部版本。返回原始 version dict 列表。

    直接拉官方 API（一次请求拿全部版本），每个字段都用 .get() 容错，
    避免官方 SDK 的 ModrinthVersion.__init__ 里硬取 rJSON['project_id']
    导致单个异常版本（响应缺字段）就把整批版本解析拖垮（KeyError: 'project_id'）。
    """
    if not patch_sdk():
        return []
    try:
        url = f"https://api.modrinth.com/v2/project/{project_id}/version"
        r = get_session().get(url, timeout=25)
        if r.status_code != 200:
            print(f"[modrinth_sdk] 取版本失败: HTTP {r.status_code}", flush=True)
            return []
        data = r.json()
        if not isinstance(data, list):
            return []
        out = []
        for v in data:
            if not isinstance(v, dict):
                continue
            files = []
            for f in (v.get("files") or []):
                if isinstance(f, dict):
                    files.append({
                        "url": f.get("url") or "",
                        "filename": f.get("filename") or "",
                        "primary": bool(f.get("primary")),
                    })
            deps = []
            for d in (v.get("dependencies") or []):
                if isinstance(d, dict):
                    deps.append({
                        "project_id": d.get("project_id") or "",
                        "dependency_type": d.get("dependency_type") or "",
                        "project_type": d.get("project_type") or "",
                    })
            out.append({
                "id": v.get("version") or "",
                "version_number": v.get("version_number") or "",
                "name": v.get("name") or "",
                "game_versions": list(v.get("game_versions") or []),
                "loaders": list(v.get("loaders") or []),
                "files": files,
                "dependencies": deps,
                "version_type": v.get("version_type") or "",
            })
        return out
    except Exception as e:
        print(f"[modrinth_sdk] 取版本失败: {e}", flush=True)
        return []


def resolve_version_sdk(project_id, game_version=None, loader=None):
    """为该 mod 挑选匹配 游戏版本+加载器 的最新版本。

    返回 version dict 或 None。
    """
    versions = project_versions_sdk(project_id)
    if not versions:
        return None
    # 1) 精确匹配 游戏版本 + 加载器
    if game_version and loader:
        for v in versions:
            if game_version in (v["game_versions"] or []) and loader in (v["loaders"] or []):
                return v
    # 2) 只匹配游戏版本
    if game_version:
        for v in versions:
            if game_version in (v["game_versions"] or []):
                return v
    # 3) 只匹配加载器
    if loader:
        for v in versions:
            if loader in (v["loaders"] or []):
                return v
    # 4) 兜底：取第一个
    return versions[0]


def version_download(version_info):
    """从 version dict 取主下载 (url, filename)。返回元组或 (None, None)。"""
    files = (version_info or {}).get("files", []) or []
    if not files:
        return None, None
    primary = next((f for f in files if f.get("primary")), files[0])
    return primary.get("url"), primary.get("filename")


def resolve_required_deps(version_info):
    """从 version dict 解析 required 依赖。返回 [{project_id, project_type}]。"""
    deps = (version_info or {}).get("dependencies", []) or []
    return [
        d for d in deps
        if d.get("dependency_type") == "required" and d.get("project_id")
    ]


def _dl_candidate_urls(url):
    """产出下载候选 URL：优先社区镜像 mod.mcimirror.top，失败再回退官方 CDN。"""
    u = str(url)
    if "cdn.modrinth.com" in u:
        yield u.replace("cdn.modrinth.com", "mod.mcimirror.top")
    yield u


# 文件写全局锁：多个下载任务并发时，.part 写盘/原子改名必须串行，
# 防止两个任务撞同一个 .part 触发 [WinError 32] 文件被占用。
import threading

_DL_WRITE_LOCK = threading.Lock()


def _is_share_violation(e: BaseException) -> bool:
    """Windows 上判断是否为文件共享冲突（另一个进程/线程正占用该文件）。"""
    if isinstance(e, FileNotFoundError):
        return False
    oe = getattr(e, "winerror", None) or getattr(e, "errno", None)
    return oe in (32, 13)  # 32=SHARING VIOLATION, 13=PERM


def _download_one(url, dest, progress_cb=None, timeout=120):
    """按单个 URL 下载到 dest（写 .part 临时文件，完成才原子改名）。成功返回 dest。

    并发安全：
      1. .part 临时文件名带线程标识 + 随机后缀，两个任务绝不写同一个 .part；
      2. 写盘/改名段拿全局 _DL_WRITE_LOCK 串行；
      3. Windows 上若撞上 [WinError 32] 文件被占用，短暂退避后重试本候选源。
    """
    from pathlib import Path
    import random
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return dest
    # 每个任务独立的 .part 临时文件（线程号 + 随机后缀），杜绝同文件写撞车
    tmp = dest.with_name(dest.name + f".part.{threading.get_ident()}.{random.randint(100000, 999999)}")

    last_err = None
    for attempt in range(4):  # 最多重试 4 次（首次 + 3 次退避）
        if dest.exists():
            return dest
        try:
            r = get_session().get(url, stream=True, timeout=timeout)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            total = int(r.headers.get("Content-Length") or 0)
            cur = 0
            last_cb = 0.0
            import time as _time
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        f.write(chunk)
                        cur += len(chunk)
                        if progress_cb:
                            # 进度回调每 0.3s 最多喊一次，防止多任务并发刷爆日志
                            now = _time.monotonic()
                            if now - last_cb >= 0.3:
                                last_cb = now
                                progress_cb(cur, total)
            if progress_cb:
                progress_cb(cur, total)  # 收尾补一次
            # 写盘 + 原子改名串行化
            with _DL_WRITE_LOCK:
                if dest.exists():
                    tmp.unlink(missing_ok=True)
                    return dest
                os.replace(str(tmp), str(dest))
            return dest
        except BaseException as e:
            last_err = e
            # 清掉本次 .part（被别的候选源接管前别留脏文件）
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            if _is_share_violation(e):
                # [WinError 32] 文件被占用：退避 0.5s * 次数 后重试本候选
                import time as _time
                _time.sleep(0.5 * (attempt + 1))
                continue
            raise
    # 共享冲突重试耗尽 -> 交给 download_file 的候选源链去切源
    raise last_err


def download_file(url, dest, progress_cb=None, timeout=120):
    """下载文件到 dest（带进度）。返回 dest 或抛异常。

    先试社区镜像 mod.mcimirror.top（modrinth CDN 在国内太慢），
    任何一步失败（HTTP 错 / 超时 / 网络中断）都清掉 .part 后回退
    原始 cdn.modrinth.com 地址；全失败才抛出。
    """
    from pathlib import Path
    last_err = None
    for cand in _dl_candidate_urls(url):
        try:
            return _download_one(cand, dest, progress_cb, timeout)
        except Exception as e:
            last_err = e
            host = ""
            try:
                host = cand.split("//", 1)[1].split("/", 1)[0]
            except Exception:
                pass
            print(f"[modrinth_sdk] 源 {host} 下载失败: {e}，尝试下一候选源", flush=True)
    raise RuntimeError(f"所有下载源均失败: {last_err}")


if __name__ == "__main__":
    patch_sdk()
    print("patch:", patch_sdk())
    print("mcmod:", mcmod_chinese_lookup("机械动力")[:5])
    hits = modrinth_search_sdk("create", "1.21.1", "neoforge", limit=5)
    print("hits:", len(hits))
    for h in hits[:3]:
        print(" ", h["name"], h["icon_url"])