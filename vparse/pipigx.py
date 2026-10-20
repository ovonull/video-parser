"""
皮皮搞笑解析。

分享链接里带 pid 与 mid，POST 到官方分享接口取作品数据：
`POST h5.pipigx.com/ppapi/share/fetch_content`  body `{"pid": 整数, "mid": 整数}`。

返回结构 `data.post`，里面 `content` 是文案、`videos[]` 是视频地址、
`thumb` 是封面编号（拼成 file.ippzone.com 的封面地址）。
"""

from __future__ import annotations

import re
from typing import Any

from vparse import common
from vparse.common import VideoParseError


API = "https://h5.pipigx.com/ppapi/share/fetch_content"
COVER_HOST = "https://file.ippzone.com/img/frame/id/"

PID_PATTERN = re.compile(r"[?&]pid=(\d+)", re.IGNORECASE)
MID_PATTERN = re.compile(r"[?&]mid=(\d+)", re.IGNORECASE)


def _headers() -> dict[str, str]:
    """接口要的请求头。"""
    return {
        "User-Agent": common.MOBILE_UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://h5.pipigx.com/",
        "Content-Type": "application/json",
    }


def _params(url: str) -> tuple[int, int]:
    """从链接里取 pid 与 mid。"""
    pid = PID_PATTERN.search(url)
    mid = MID_PATTERN.search(url)
    return (
        int(pid.group(1)) if pid else 0,
        int(mid.group(1)) if mid else 0,
    )


async def parse(url: str, cookie: str, host: Any, quality: str = "") -> dict[str, Any]:
    """
    解析一条皮皮搞笑链接。

    Args:
        url: 带 pid 与 mid 的分享链接。
        cookie: 保留参数，接口不需要。
        host: 插件宿主视图。
        quality: 保留参数，接口只给一种画质。
    Returns:
        统一结构的媒体信息。
    Raises:
        VideoParseError: 链接里没有 pid/mid 或接口没返回视频。
    """
    pid, mid = _params(url)
    if not pid or not mid:
        raise VideoParseError(
            "没有从链接里识别出皮皮搞笑的作品编号",
            "请复制 App 分享出来的完整链接（需要带 pid 与 mid 参数）",
        )

    result = await host.http_request(
        "POST",
        API,
        headers=_headers(),
        json_body={"pid": pid, "mid": mid},
        timeout=10.0,
    )
    payload = common.json_of(result)
    post = (payload.get("data") or {}).get("post")
    if not isinstance(post, dict) or not post:
        raise VideoParseError(
            "皮皮搞笑没有返回作品数据", "作品可能已删除，或接口临时不可用"
        )

    videos = post.get("videos") or []
    if not isinstance(videos, list) or not videos:
        raise VideoParseError("这个作品里没有找到视频地址")
    first = videos[0] if isinstance(videos[0], dict) else {}
    play_url = common.text_of(first.get("url"))
    if not play_url:
        raise VideoParseError("这个作品里没有找到可播放的地址")

    title = common.block(post.get("content"))
    cover = ""
    thumb = common.text_of(first.get("thumb"))
    if thumb:
        cover = f"{COVER_HOST}{thumb}"

    return {
        "platform": "pipigx",
        "platform_name": "皮皮搞笑",
        "kind": "video",
        "title": title.split("\n")[0] if title else "皮皮搞笑作品",
        "desc": title,
        "stats": [],
        "author": common.text_of(post.get("author") or post.get("nickname")),
        "duration": None,
        "cover": cover,
        "play_url": play_url,
        "images": [],
        "source_url": url,
    }
