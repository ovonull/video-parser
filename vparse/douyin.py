"""
抖音解析。

走「分享页 + 页面内嵌数据」这条路，不需要签名算法：
1. 链接里直接有作品号时不再请求短链；
2. v.douyin.com 短链先手工跟随跳转，再从最终地址里取作品号；
3. 请求 iesdouyin 分享页，取 window._ROUTER_DATA 里的作品节点；
4. play_addr 本身就是无水印地址，遇到 playwm 再兜底替换成 play。

实测抖音前两次访问只回不带数据的壳页面（顺带下发 Cookie），第三次才带出作品，
因此最多重试三次；空壳页面响应很快，重试不会明显拖慢。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from vparse import common
from vparse.common import VideoParseError


SHARE_PAGE = "https://www.iesdouyin.com/share/video/{aweme_id}/"
ROUTER_MARK = "window._ROUTER_DATA"

# 分享页体积约 32KB，关键数据在第 3 万字符之后，必须申请更大的响应长度。
SHARE_PAGE_CHARS = 120000

ATTEMPTS = 3
RETRY_GAP = 0.3

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


def _build(item: dict[str, Any], source_url: str) -> dict[str, Any]:
    """把作品节点转成统一结构，视频与图集分开处理。"""
    video = item.get("video") or {}
    images = item.get("images") or []
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
        return media

    play_url = common.first_url(video.get("play_addr")) or common.first_url(
        video.get("download_addr")
    )
    if not play_url:
        raise VideoParseError(
            "作品里没有找到可播放的地址", "可能是直播回放、合集或受限作品"
        )
    # 分享页给 playwm 是带水印版本，换成 play 拿无水印。
    if "playwm" in play_url:
        play_url = play_url.replace("playwm", "play")
    media["play_url"] = play_url
    return media


async def parse(url: str, cookie: str, host: Any) -> dict[str, Any]:
    """
    解析一条抖音链接。

    Args:
        url: 抖音视频或图文链接（含短链）。
        cookie: 安装配置里的抖音 Cookie，可为空。
        host: 插件宿主视图。
    Returns:
        统一结构的媒体信息。
    Raises:
        VideoParseError: 作品号缺失、页面无数据或作品不可用。
    """
    aweme_id = await resolve_id(url, cookie, host)
    if not aweme_id:
        raise VideoParseError(
            "没有从链接里识别到作品号",
            "请复制抖音 App 的分享链接，或 www.douyin.com/video/ 开头的地址",
        )
    item = await _fetch_item(aweme_id, cookie, host)
    if item is None:
        raise VideoParseError(
            "抖音没有返回视频数据",
            "作品可能已删除、设为私密，或触发了风控；在插件配置里填抖音 Cookie 后成功率更高",
        )
    return _build(item, url)
