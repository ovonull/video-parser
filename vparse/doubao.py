"""
豆包 AI 解析。

分享链接里带 share_id 与 video_id，POST 到官方接口取作品数据：
`POST www.doubao.com/creativity/share/get_video_share_info`
body `{"share_id": ..., "vid": ..., "creation_id": ""}`。

接口成功时返回 code=0 与 data；data 里的字段名各版本不完全一致，
这里用深搜兜底取播放地址与封面。
"""

from __future__ import annotations

import re
from typing import Any

from vparse import common
from vparse.common import VideoParseError


API = (
    "https://www.doubao.com/creativity/share/get_video_share_info"
    "?version_code=20800&language=zh-CN&device_platform=web&aid=497858"
    "&real_aid=497858&pkg_type=release_version&device_id=&pc_version=3.8.3"
    "&region=&sys_region=&samantha_web=1&use-olympus-account=1"
    "&web_tab_id=4d5d17a6-6729-4c1e-9f55-09f093100f0a"
)

SHARE_ID_PATTERN = re.compile(r"[?&]share_id=([\w-]+)", re.IGNORECASE)
VIDEO_ID_PATTERN = re.compile(r"[?&]video_id=([\w-]+)", re.IGNORECASE)


def _headers(cookie: str) -> dict[str, str]:
    """接口要的请求头。"""
    headers = {
        "User-Agent": common.DESKTOP_UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.doubao.com/",
        "Content-Type": "application/json",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _params(url: str) -> tuple[str, str]:
    """从链接里取 share_id 与 video_id。"""
    share = SHARE_ID_PATTERN.search(url)
    video = VIDEO_ID_PATTERN.search(url)
    return (
        share.group(1) if share else "",
        video.group(1) if video else "",
    )


async def parse(
    url: str, cookie: str, host: Any, quality: str = ""
) -> dict[str, Any]:
    """
    解析一条豆包 AI 作品链接。

    Args:
        url: 带 share_id 与 video_id 的分享链接。
        cookie: 安装配置里的豆包 Cookie，可为空。
        host: 插件宿主视图。
        quality: 保留参数，接口只给一种画质。
    Returns:
        统一结构的媒体信息。
    Raises:
        VideoParseError: 链接里没有编号或接口没返回视频。
    """
    share_id, video_id = _params(url)
    if not share_id or not video_id:
        raise VideoParseError(
            "没有从链接里识别出豆包的作品编号",
            "请复制豆包分享出来的完整链接（需要带 share_id 与 video_id 参数）",
        )

    result = await host.http_request(
        "POST",
        API,
        headers=_headers(cookie),
        json_body={"share_id": share_id, "vid": video_id, "creation_id": ""},
        timeout=10.0,
    )
    payload = common.json_of(result)
    if payload.get("code") not in (0, None):
        raise VideoParseError(
            f"豆包返回错误：{common.text_of(payload.get('message')) or '未知原因'}",
            "作品可能已删除，或需要登录后才能查看",
        )
    data = payload.get("data")
    if not isinstance(data, dict) or not data:
        raise VideoParseError(
            "豆包没有返回作品数据", "作品可能已删除，或接口临时不可用"
        )

    play_url = common.find_text(
        data, "play_url", "video_url", "url", "download_url", "src"
    )
    if not play_url:
        raise VideoParseError("这个作品里没有找到可播放的地址")

    desc = common.block(
        common.find_text(data, "desc", "description", "content", "title", "prompt")
    )
    cover = common.find_text(data, "cover_url", "cover", "poster", "thumbnail")
    author = common.find_text(data, "author_name", "user_name", "author", "nickname")

    return {
        "platform": "doubao",
        "platform_name": "豆包AI",
        "kind": "video",
        "title": desc.split("\n")[0] if desc else "豆包作品",
        "desc": desc,
        "stats": [],
        "author": author,
        "duration": None,
        "cover": cover,
        "play_url": play_url,
        "images": [],
        "source_url": url,
    }
