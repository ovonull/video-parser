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
- 想要真正的「原画」，用 `play_addr.uri`（vid）拼地址再跟 302 取最终文件。

图文作品：发全部原图，与视频走同一套描述卡片。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from app.services.plugin_sdk import PluginCallError

from vparse import common
from vparse.common import VideoParseError


SHARE_PAGE = "https://www.iesdouyin.com/share/video/{aweme_id}/"
ROUTER_MARK = "window._ROUTER_DATA"

# 免 Cookie 详情接口：比分享页稳，不用等空壳页面重试。
DETAIL_API = "https://api.amemv.com/aweme/v1/aweme/detail/"
# 原画直链入口：带上 vid 后 302 跳到真正的无水印文件。
ORIGINAL_PLAY_API = "https://aweme.snssdk.com/aweme/v1/play/"

# 分享页体积约 32KB，关键数据在第 3 万字符之后，必须申请更大的响应长度。
SHARE_PAGE_CHARS = 120000

ATTEMPTS = 3
RETRY_GAP = 0.3

# 抖音码率档位编号对应的视频高度，取自 bit_rate[].quality_type。
QUALITY_TYPE_HEIGHT = {32: 540, 54: 720, 80: 1080}

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

    抖音会给出不同 CDN 域名的同一份文件：v3-web 最稳，v26-web 需要换成对应域名。
    """
    v3 = ""
    v26 = ""
    for url in urls:
        if "v3-web" in url:
            v3 = url
            break
        if not v26 and "v26-web" in url:
            v26 = url
    if v3:
        return v3
    if v26:
        return v26.replace("v26-web", "v3-web")
    return urls[0] if urls else ""


def _video_candidates(video: dict[str, Any]) -> list[tuple[int, int, str, str]]:
    """
    收集视频的可用清晰度，返回 (高度, 码率, 展示名称, 地址)。

    play_addr / download_addr 作为兜底地址附在末尾，高度与码率都记 0。
    """
    candidates: list[tuple[int, int, str, str]] = []
    for rate in sorted(_bit_rate_list(video), key=_rate_bitrate, reverse=True):
        url = _prefer_host(_rate_addresses(rate))
        if not url:
            continue
        height = QUALITY_TYPE_HEIGHT.get(_int_of(rate.get("quality_type")), 0)
        label = common.text_of(rate.get("gearName") or rate.get("gear_name"))
        if not label:
            label = f"{height}P" if height else ""
        candidates.append((height, _rate_bitrate(rate), label, _clean_play_url(url)))

    fallback = _clean_play_url(common.first_url(video.get("play_addr"))) or _clean_play_url(
        common.first_url(video.get("download_addr"))
    )
    if fallback:
        candidates.append((0, 0, "", fallback))
    return candidates


def _pick_video(
    candidates: list[tuple[int, int, str, str]], wanted: int | None
) -> tuple[int, int, str, str] | None:
    """
    在候选里选一档。

    规则：选不超过目标高度的最高档（同一高度里取码率最大的）；
    「自动最高」直接取码率最大的一档，即抖音画质最好的一档。
    """
    if not candidates:
        return None
    if wanted is None:
        return max(candidates, key=lambda item: item[1])
    fitted = [item for item in candidates if 0 < item[0] <= wanted]
    if fitted:
        return max(fitted, key=lambda item: item[1])
    known = [item for item in candidates if item[0] > 0]
    if known:
        return min(known, key=lambda item: item[0])
    return candidates[0]


def _video_vid(video: dict[str, Any]) -> str:
    """取视频的 vid，原画直链要用它拼地址。"""
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


async def _resolve_original_url(vid: str, host: Any) -> str:
    """
    vid 拼出的原画地址会 302 跳到真正的文件，这里把 Location 取出来。

    宿主出网不自动跟随重定向，因此这里能直接拿到 302 的 location 头。
    """
    if not vid:
        return ""
    result = await host.http_request(
        "GET",
        ORIGINAL_PLAY_API,
        headers={"User-Agent": common.DESKTOP_UA, "Referer": "https://www.douyin.com/"},
        query={"video_id": vid, "ratio": "default", "line": "0"},
        timeout=8.0,
    )
    return common.headers_of(result).get("location") or ""


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


def _build(item: dict[str, Any], source_url: str, wanted: int | None) -> dict[str, Any]:
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

    picked = _pick_video(_video_candidates(video), wanted)
    if picked is None:
        raise VideoParseError(
            "作品里没有找到可播放的地址", "可能是直播回放、合集或受限作品"
        )
    height, _bitrate, label, url = picked
    media["play_url"] = url
    label = label or (f"{height}P" if height else "")
    if label:
        media["quality"] = label
    return media


async def parse(
    url: str, cookie: str, host: Any, quality: str = common.DEFAULT_QUALITY
) -> dict[str, Any]:
    """
    解析一条抖音链接。

    Args:
        url: 抖音视频或图文链接（含短链）。
        cookie: 安装配置里的抖音 Cookie，可为空。
        host: 插件宿主视图。
        quality: 安装配置里的清晰度名称，决定在多档码率里选哪一档。
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
    media = _build(item, url, wanted)

    # 「自动最高」时再走一次 vid 原画：这是抖音真正的高清直链。
    if media.get("kind") == "video" and wanted is None:
        vid = _video_vid(item.get("video") or {})
        if vid:
            try:
                original = await _resolve_original_url(vid, host)
            except PluginCallError:
                original = ""
            if original:
                media["play_url"] = original
                media["quality"] = "原画"
    return media
