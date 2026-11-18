"""
抖音解析。

取数走两条路，优先用前者：
1. **免 Cookie 详情接口** `api.amemv.com/aweme/v1/aweme/detail/`：直接给结构化的
   aweme_detail，不用等分享页的空壳页面；
2. **分享页内嵌数据** `window._ROUTER_DATA`：详情接口不可用时兜底。

视频清晰度：
- 多档码率在 `video.bitRateList`（详情接口）或 `video.bit_rate`（分享页）里，
  **按 bitRate 降序**才是真实画质顺序，`quality_type` 只是编号；
- 每档可能有多个 CDN 地址，优先用 v3-web，v26-web 换成对应域名；
- 发视频按码率与时长估算每档体积，在预算内选最高档，避免大档超宿主转存上限；
  原片是发布者原始上传文件，本身无水印、画质最高，但体积动辄 1GB，发视频会超
  转存上限，所以「发送视频」样式走码率档；
- 详情接口的码率地址带 Referer 防盗链，裸链点开是 403。「无水印直链」样式用
  302 播放端点（share_url）：不校验 Referer、点开实时跳到带时效 CDN 地址（裸链
  200），ratio=default 取发布者原片（约 1GB、无水印、画质最高）；直链点开边下
  边播，不受宿主转存上限限制，所以能发原画。

图文作品：发全部原图，与视频走同一套描述卡片。
"""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlencode
from typing import Any

from vparse import common
from vparse.common import VideoParseError


SHARE_PAGE = "https://www.iesdouyin.com/share/video/{aweme_id}/"
ROUTER_MARK = "window._ROUTER_DATA"

# 免 Cookie 详情接口：比分享页稳，不用等空壳页面重试。
DETAIL_API = "https://api.amemv.com/aweme/v1/aweme/detail/"

# 分享页体积约 32KB，关键数据在第 3 万字符之后，必须申请更大的响应长度。
SHARE_PAGE_CHARS = 120000

ATTEMPTS = 3
RETRY_GAP = 0.3

# 抖音码率档位编号对应的视频高度，取自 bit_rate[].quality_type。
QUALITY_TYPE_HEIGHT = {32: 540, 54: 720, 80: 1080}

# 群里可点开的无水印原画直链：302 播放端点不校验 Referer，点开时实时跳到带
# 时效的 CDN 地址（裸链 200）。ratio=default 取发布者原片——原片本身无水印、
# 画质最高，直链点开边下边播不受宿主转存上限限制，所以直链样式固定发原画。
PLAY_API = "https://aweme.snssdk.com/aweme/v1/play/"

# 作品节点里的互动数据，字段 → 展示标签。
STAT_PAIRS: tuple[tuple[Any, str], ...] = (
    ("digg_count", "点赞"),
    ("comment_count", "评论"),
    ("share_count", "分享"),
)

ID_PATTERNS = (
    re.compile(r"/share/video/(\d{6,25})"),
    re.compile(r"/video/(\d{6,25})"),
    re.compile(r"/share/note/(\d{6,25})"),
    re.compile(r"/note/(\d{6,25})"),
    re.compile(r"[?&]modal_id=(\d{6,25})"),
    re.compile(r"[?&]aweme_id=(\d{6,25})"),
)


def _page_headers(cookie: str) -> dict[str, str]:
    """分享页要求的请求头，带 Cookie 时成功率更高。"""
    headers = {
        "User-Agent": common.MOBILE_UA,
        "Referer": "https://www.douyin.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _detail_headers(cookie: str) -> dict[str, str]:
    """详情接口要的请求头。"""
    headers = {
        "User-Agent": common.MOBILE_UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.douyin.com/",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _clean_play_url(url: str) -> str:
    """分享页给 playwm 是带水印版本，换成 play 拿无水印。"""
    text = str(url or "")
    return text.replace("playwm", "play") if "playwm" in text else text


def _int_of(value: Any, default: int = 0) -> int:
    """把接口里的数字字段转成 int，拿不准时返回默认值。"""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _rate_bitrate(rate: dict[str, Any]) -> int:
    """取一档的码率。详情接口给 bitRate、分享页给 bit_rate。"""
    value = rate.get("bitRate")
    if value is None:
        value = rate.get("bit_rate")
    return _int_of(value)


def _bit_rate_list(video: dict[str, Any]) -> list[dict[str, Any]]:
    """取码率档位列表，详情接口给 bitRateList、分享页给 bit_rate。"""
    for key in ("bitRateList", "bit_rate"):
        items = video.get(key)
        if isinstance(items, list) and items:
            return [item for item in items if isinstance(item, dict)]
    return []


def _rate_addresses(rate: dict[str, Any]) -> list[str]:
    """取某一档的候选播放地址。"""
    urls: list[str] = []
    for key in ("playAddr", "play_addr"):
        block = rate.get(key)
        if isinstance(block, list):
            for item in block:
                if isinstance(item, dict):
                    src = common.text_of(item.get("src") or item.get("url"))
                    if src:
                        urls.append(src)
        elif isinstance(block, dict):
            src = common.first_url(block)
            if src:
                urls.append(src)
    return urls


def _prefer_host(urls: list[str]) -> str:
    """
    在候选地址里挑一个最稳的。

    同一份文件会有不同 CDN 域名（v3-web、v26-web 等），它们的可用性随时间和
    地区变化，实测出现过 v3-web 全部 403、而 v26-web 正常返回的情况。因此这里
    **保留平台给的原始域名，不再把 v26-web 改写成 v3-web**——改写会把本来能用
    的地址改坏。其它域名通过镜像地址交给宿主做失败重试。
    """
    for url in urls:
        if url:
            return url
    return ""


def _video_vid(video: dict[str, Any]) -> str:
    """取作品的 vid（play_addr.uri），302 播放端点要用它定位文件。"""
    nodes: list[Any] = [video.get("play_addr"), video]
    play_addr = video.get("playAddr")
    if isinstance(play_addr, list) and play_addr:
        nodes.insert(0, play_addr[0])
    for node in nodes:
        if isinstance(node, dict):
            vid = common.text_of(node.get("uri"))
            if vid:
                return vid
    return ""


def _share_url(vid: str) -> str:
    """
    生成群里点开即可播放的无水印原画直链。

    详情接口的码率地址带 Referer 防盗链，裸链点开是 403；改用 302 播放端点：
    不带 Referer 也能跳，点开时实时 302 到带时效的 CDN 地址（裸链 200），所以
    直链长期有效。ratio=default 取发布者原片：原片本身无水印、画质最高（实测
    约 1GB），直链点开边下边播、不受宿主转存上限限制，能放心发原画。
    """
    if not vid:
        return ""
    return f"{PLAY_API}?{urlencode({'video_id': vid, 'ratio': 'default', 'line': '0'})}"


def _host_key(url: str) -> str:
    """按 hostname 去重：同一域名的多条地址只保留一条。"""
    try:
        from urllib.parse import urlparse

        return (urlparse(url).hostname or url).lower()
    except ValueError:
        return url


def _rate_mirrors(rate: dict[str, Any], primary: str) -> list[str]:
    """
    取同一档里与主地址不同域名的其它地址，作为失败重试的备用。

    抖音码率档的播放路径在各 CDN 域名间通用（同一 path 换前缀即可取到同一
    文件），而域名可用性随时间变化：实测 v3-web 全部 403、v26-web 却正常。
    因此除了接口给的原始域名，再补一个 v26-web 变体兜底，避免接口只返回单
    一失效域名时无地址可换。
    """
    seen = {_host_key(primary)}
    mirrors: list[str] = []
    for url in _rate_addresses(rate):
        url = _clean_play_url(common.https_url(url))
        key = _host_key(url)
        if url and key not in seen:
            seen.add(key)
            mirrors.append(url)

    # 同 path 换 v26-web 前缀：v3-web 失效时的可靠备用。
    if "douyinvod.com" in seen or "v3-web" in primary:
        variant = re.sub(
            r"^(https?://)[^/]*", r"\1v26-web.douyinvod.com", primary, count=1
        )
        key = _host_key(variant)
        if variant != primary and key not in seen:
            seen.add(key)
            mirrors.append(variant)
    return mirrors


def _video_candidates(
    video: dict[str, Any]
) -> list[tuple[int, int, str, str, list[str]]]:
    """
    收集视频的可用清晰度，返回 (高度, 码率, 展示名称, 地址, 镜像地址列表)。

    镜像地址是同一档位的其它 CDN 域名：主地址被防盗链拒绝时，宿主按镜像逐个
    重试。play_addr / download_addr 作为兜底地址附在末尾，高度与码率都记 0。
    """
    candidates: list[tuple[int, int, str, str, list[str]]] = []
    for rate in sorted(_bit_rate_list(video), key=_rate_bitrate, reverse=True):
        raw_urls = _rate_addresses(rate)
        url = _prefer_host(raw_urls)
        if not url:
            continue
        url = _clean_play_url(common.https_url(url))
        height = QUALITY_TYPE_HEIGHT.get(_int_of(rate.get("quality_type")), 0)
        label = common.text_of(rate.get("gearName") or rate.get("gear_name"))
        if not label:
            label = f"{height}P" if height else ""
        mirrors = [m for m in _rate_mirrors(rate, url) if m != url]
        candidates.append((height, _rate_bitrate(rate), label, url, mirrors))

    fallback = _clean_play_url(common.first_url(video.get("play_addr"))) or _clean_play_url(
        common.first_url(video.get("download_addr"))
    )
    if fallback:
        candidates.append((0, 0, "", fallback, []))
    return candidates


def _pick_video(
    candidates: list[tuple[int, int, str, str, list[str]]],
    wanted: int | None,
    duration_ms: int = 0,
    budget_bytes: int = 0,
) -> tuple[int, int, str, str, list[str]] | None:
    """
    在候选里选一档无水印码率地址。

    规则：
    - 先按目标高度筛出候选池（「自动最高」取码率最大的一档）；
    - 再用体积预算收敛：按 ``码率 × 时长`` 估算每档大小，预算内取码率最大的；
      全部超预算时退到估算体积最小的一档，宁可降清晰度也要保证能发出去；
    - 码率或时长缺失、预算为 0 时跳过体积过滤，沿用清晰度规则。
    """
    if not candidates:
        return None
    if wanted is None:
        pool = list(candidates)
    else:
        pool = [item for item in candidates if 0 < item[0] <= wanted]
        if not pool:
            known = [item for item in candidates if item[0] > 0]
            pool = list(known) if known else list(candidates)

    if budget_bytes > 0 and duration_ms > 0:
        # 码率单位是 bit/s，时长是 ms：字节 ≈ bitrate × duration_ms / 1000 / 8。
        def estimated(item: tuple[int, int, str, str, list[str]]) -> int:
            bitrate = item[1]
            return bitrate * duration_ms // 8000 if bitrate > 0 else 0

        affordable = [item for item in pool if 0 < estimated(item) <= budget_bytes]
        if affordable:
            pool = affordable
        else:
            # 没有一档落在预算内：退到估算体积最小的档，至少发得出去。
            sized = [item for item in pool if estimated(item) > 0]
            if sized:
                pool = [min(sized, key=estimated)]
    if wanted is None or any(item[0] > 0 for item in pool):
        return max(pool, key=lambda item: item[1])
    return pool[0]


def extract_id(text: str) -> str:
    """从链接或跳转地址里取作品号。"""
    for pattern in ID_PATTERNS:
        match = pattern.search(str(text or ""))
        if match:
            return match.group(1)
    return ""


async def resolve_id(url: str, cookie: str, host: Any) -> str:
    """取作品号：链接里没有就去跟随短链。"""
    found = extract_id(url)
    if found:
        return found
    if "v.douyin.com" not in common.host_of(url):
        return ""
    final = await common.follow_short_link(
        host, url, headers=_page_headers(cookie), timeout=8.0
    )
    return extract_id(final)


def _find_item(node: Any) -> dict[str, Any] | None:
    """在嵌套结构里递归找到带作品号与媒体的节点。"""
    if isinstance(node, dict):
        if node.get("aweme_id") and (node.get("video") or node.get("images")):
            return node
        for value in node.values():
            found = _find_item(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_item(value)
            if found:
                return found
    return None


async def _fetch_detail(
    aweme_id: str, cookie: str, host: Any
) -> dict[str, Any] | None:
    """
    走免 Cookie 详情接口拿作品数据，失败返回 None 交给分享页兜底。

    详情接口与分享页的字段名一致（desc / author / video / images），
    因此后面的组装逻辑两边通用。
    """
    result = await host.http_request(
        "GET",
        DETAIL_API,
        headers=_detail_headers(cookie),
        query={
            "aweme_id": aweme_id,
            "aid": "6383",
            "device_platform": "web",
            "version_code": "190500",
            "version_name": "19.5.0",
        },
        timeout=8.0,
    )
    payload = common.json_of(result)
    if payload.get("status_code") not in (0, None):
        return None
    detail = payload.get("aweme_detail")
    if not isinstance(detail, dict) or not detail:
        return None
    if str(detail.get("aweme_id") or "") != str(aweme_id):
        return None
    return detail


async def _fetch_item(aweme_id: str, cookie: str, host: Any) -> dict[str, Any] | None:
    """请求分享页并解析内嵌数据，最多三次。"""
    headers = _page_headers(cookie)
    url = SHARE_PAGE.format(aweme_id=aweme_id)
    for index in range(ATTEMPTS):
        if index:
            await asyncio.sleep(RETRY_GAP)
        result = await host.http_request(
            "GET",
            url,
            headers=headers,
            timeout=8.0,
            max_chars=SHARE_PAGE_CHARS,
        )
        html = str(result.get("text") or "")
        start = html.find(ROUTER_MARK)
        if start < 0:
            continue
        brace = html.find("{", start)
        if brace < 0:
            continue
        try:
            payload, _ = json.JSONDecoder().raw_decode(html[brace:])
        except ValueError:
            continue
        item = _find_item(payload)
        if item is not None:
            return item
    return None


def _live_photo_url(images: list[Any]) -> str:
    """
    取实况作品（LivePhoto）里那段短视频的地址。

    实况作品的静态图元素上还挂着 video 节点，里面是这段短视频的信息：
    `playAddr[]`（驼峰）或 `play_addr.url_list`（蛇形）里是播放地址。
    多个地址时沿用视频那边的挑法：优先 v3-web，v26-web 换成对应域名。
    最后把 playwm 换成 play，就是无水印地址。
    """
    for image in images:
        if not isinstance(image, dict):
            continue
        video = image.get("video")
        if not isinstance(video, dict):
            continue
        urls: list[str] = []
        # 驼峰写法：playAddr[].src
        play_addr = video.get("playAddr")
        if isinstance(play_addr, list):
            for item in play_addr:
                if isinstance(item, dict):
                    src = common.text_of(item.get("src"))
                    if src:
                        urls.append(src)
        # 蛇形写法：play_addr.url_list
        snake = video.get("play_addr")
        if isinstance(snake, dict):
            url_list = snake.get("url_list")
            if isinstance(url_list, list):
                for item in url_list:
                    text = common.text_of(item)
                    if text:
                        urls.append(text)
        if not urls:
            continue
        url = _prefer_host(urls)
        if url:
            return common.https_url(url.replace("playwm", "play"))
    return ""


def _build(
    item: dict[str, Any], source_url: str, wanted: int | None, budget_bytes: int = 0
) -> dict[str, Any]:
    """把作品节点转成统一结构，视频与图集分开处理。"""
    video = item.get("video") or {}
    images = item.get("images") or item.get("image_list") or []
    author = item.get("author") or {}
    duration_ms = int(video.get("duration") or 0)

    desc = common.block(item.get("desc"))
    # 抖音没有独立标题，文案首行就是标题，整段文案作为描述展示。
    title = desc.split("\n")[0] if desc else "抖音作品"

    media: dict[str, Any] = {
        "platform": "douyin",
        "platform_name": "抖音",
        "kind": "video",
        "title": title,
        "desc": desc,
        "stats": common.count_line(STAT_PAIRS, item.get("statistics") or {}),
        "author": common.text_of(author.get("nickname")),
        "duration": (duration_ms // 1000) if duration_ms else None,
        "cover": common.https_url(
            common.first_url(video.get("origin_cover"))
            or common.first_url(video.get("cover"))
        ),
        "play_url": "",
        "images": [],
        "source_url": source_url,
    }

    if images:
        media["kind"] = "images"
        for image in images:
            url = common.first_url(image)
            if url:
                media["images"].append(common.https_url(url))
        if not media["images"]:
            raise VideoParseError("这条图文作品里没有取到图片地址")
        # 实况作品（LivePhoto）除了静态图，还带一段短视频：
        # 地址在 images[].video 里，playwm 换成 play 就是无水印地址。
        live = _live_photo_url(images)
        if live:
            media["live_video_url"] = live
        return media

    picked = _pick_video(_video_candidates(video), wanted, duration_ms, budget_bytes)
    if picked is None:
        raise VideoParseError(
            "作品里没有找到可播放的地址", "可能是直播回放、合集或受限作品"
        )
    height, bitrate, label, url, mirrors = picked
    media["play_url"] = url
    if mirrors:
        media["play_url_mirrors"] = mirrors
    # 群里直链样式用：302 端点给的无水印原画地址，点开时实时跳转，裸链可播。
    # 拿不到 vid（少数作品）时留空，插件会退回用 play_url 发直链。
    share = _share_url(_video_vid(video))
    if share:
        media["share_url"] = share
    label = label or (f"{height}P" if height else "")
    if label:
        media["quality"] = label
    # 上面已经按预算挑过一轮档，但全都超预算时 _pick_video 会退到最小的那档——
    # 那一档照样发不出去。把估算体积带上去，让上层直接改发直链，
    # 别再花时间转存一个注定失败的视频。
    if budget_bytes > 0 and bitrate > 0 and duration_ms > 0:
        estimated = bitrate * duration_ms // 8000
        if estimated > budget_bytes:
            media["size"] = estimated
    return media


async def parse(
    url: str,
    cookie: str,
    host: Any,
    quality: str = common.DEFAULT_QUALITY,
    budget_bytes: int = 0,
) -> dict[str, Any]:
    """
    解析一条抖音链接。

    Args:
        url: 抖音视频或图文链接（含短链）。
        cookie: 安装配置里的抖音 Cookie，可为空。
        host: 插件宿主视图。
        quality: 安装配置里的清晰度名称，决定在多档码率里选哪一档。
        budget_bytes: 单视频体积预算（字节），超过则降档；0 表示不限制。
    Returns:
        统一结构的媒体信息。
    Raises:
        VideoParseError: 作品号缺失、页面无数据或作品不可用。
    """
    aweme_id = await resolve_id(url, cookie, host)
    if not aweme_id:
        raise VideoParseError(
            "没有从链接里识别出作品号",
            "请复制抖音 App 的分享链接，或 www.douyin.com/video/ 开头的地址",
        )
    # 免 Cookie 详情接口优先：结构化数据直给，不用等分享页的空壳重试；
    # 接口不可用时再退回分享页。
    item = await _fetch_detail(aweme_id, cookie, host)
    if item is None:
        item = await _fetch_item(aweme_id, cookie, host)
    if item is None:
        raise VideoParseError(
            "抖音没有返回视频数据",
            "作品可能已删除、设为私密，或触发了风控；在插件配置里填抖音 Cookie 后成功率更高",
        )

    wanted = common.quality_target(quality)
    media = _build(item, url, wanted, budget_bytes)
    return media
