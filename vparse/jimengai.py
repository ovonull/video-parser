"""
即梦 AI 解析。

分享链接里带 item_id，POST 到官方接口取作品数据：
`POST jimeng.jianying.com/mweb/v1/get_item_info`  body `{"published_item_id": item_id}`。

返回结构 `data`：
- `common_attr.description` 是文案，`common_attr.cover_url` 是封面；
- `author.name` 是作者；
- `video.transcoded.origin.video_url` 是**原画**地址，`origin_video.video_url` 同理。
"""

from __future__ import annotations

import re
from typing import Any

from vparse import common
from vparse.common import VideoParseError


API = "https://jimeng.jianying.com/mweb/v1/get_item_info"

ITEM_ID_PATTERN = re.compile(r"[?&]item_id=([\w-]+)", re.IGNORECASE)


def _headers() -> dict[str, str]:
    """接口要的请求头。"""
    return {
        "User-Agent": common.MOBILE_UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://jimeng.jianying.com/",
        "Content-Type": "application/json",
    }


def _extract_id(url: str) -> str:
    """从链接里取作品 ID。"""
    match = ITEM_ID_PATTERN.search(url)
    return match.group(1) if match else ""


def _video_url(video: dict[str, Any]) -> str:
    """取视频地址，优先原画。"""
    transcoded = video.get("transcoded")
    if isinstance(transcoded, dict):
        origin = transcoded.get("origin")
        if isinstance(origin, dict):
            url = common.text_of(origin.get("video_url"))
            if url:
                return url
    origin_video = video.get("origin_video")
    if isinstance(origin_video, dict):
        url = common.text_of(origin_video.get("video_url"))
        if url:
            return url
    return common.find_text(video, "video_url", "play_url", "url")


def _cover_url(common_attr: dict[str, Any]) -> str:
    """取封面：cover_url_map 里挑一个，或用 cover_url。"""
    cover_map = common_attr.get("cover_url_map")
    if isinstance(cover_map, dict) and cover_map:
        for value in cover_map.values():
            url = common.text_of(value)
            if url:
                return url
    return common.text_of(common_attr.get("cover_url"))


async def parse(url: str, cookie: str, host: Any, quality: str = "") -> dict[str, Any]:
    """
    解析一条即梦 AI 作品链接。

    Args:
        url: 带 item_id 的分享链接。
        cookie: 保留参数，接口不需要。
        host: 插件宿主视图。
        quality: 保留参数，接口只给原画一种。
    Returns:
        统一结构的媒体信息。
    Raises:
        VideoParseError: 链接里没有 item_id 或接口没返回视频。
    """
    item_id = _extract_id(url)
    if not item_id:
        raise VideoParseError(
            "没有从链接里识别出即梦的作品编号",
            "请复制即梦分享出来的完整链接（需要带 item_id 参数）",
        )

    result = await host.http_request(
        "POST",
        API,
        headers=_headers(),
        json_body={"published_item_id": item_id},
        timeout=10.0,
    )
    payload = common.json_of(result)
    data = payload.get("data")
    if not isinstance(data, dict) or not data:
        raise VideoParseError(
            "即梦没有返回作品数据", "作品可能已删除，或接口临时不可用"
        )

    common_attr = data.get("common_attr") or {}
    author = data.get("author") or {}
    video = data.get("video") or {}
    statistic = data.get("statistic") or {}

    play_url = _video_url(video)
    if not play_url:
        raise VideoParseError("这个作品里没有找到可播放的地址")

    desc = common.block(common_attr.get("description"))
    stats = []
    for key, label in (("like_count", "点赞"), ("comment_count", "评论")):
        count = common.format_count(statistic.get(key))
        if count:
            stats.append(f"{label} {count}")

    return {
        "platform": "jimengai",
        "platform_name": "即梦AI",
        "kind": "video",
        "title": desc.split("\n")[0] if desc else "即梦作品",
        "desc": desc,
        "stats": stats,
        "author": common.text_of(author.get("name")),
        "duration": None,
        "cover": _cover_url(common_attr),
        "play_url": play_url,
        "images": [],
        "source_url": url,
    }
