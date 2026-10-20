"""
快手解析。

分享链接形如 `https://v.kuaishou.com/xxx`，跳转到作品页后从页面内嵌数据里取媒体信息：

1. 页面里的 `window.INIT_STATE = {...}` —— 快手主要的数据来源；
2. 页面里的 `window.__APOLLO_STATE__ = {...}` —— INIT_STATE 缺失时的兜底。

作品数据在 `photo` 节点：视频走 `photo.srcNoMark`（无水印）或 `photo.photoUrl`，
图集走 `photo.ext_params.atlas.list`，同时带标题、作者、点赞数与音乐信息。

整条链路都是自己发请求、自己解析页面，不经过任何第三方中转服务。
"""

from __future__ import annotations

import json
import re
from typing import Any

from vparse import common
from vparse.common import VideoParseError


# 快手作品页的三种形态：短视频、长视频（图集也走这里）、图片作品。
ID_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("short-video", re.compile(r"/short-video/([^?/#]+)")),
    ("long-video", re.compile(r"/long-video/([^?/#]+)")),
    ("photo", re.compile(r"/photo/([^?/#]+)")),
)

# 页面里两处内嵌数据的位置。
INIT_STATE = re.compile(r"window\.INIT_STATE\s*=\s*(\{.*?\})\s*</script>", re.DOTALL)
APOLLO_STATE = re.compile(
    r"window\.__APOLLO_STATE__\s*=\s*(\{.*?\})\s*</script>", re.DOTALL
)
# INIT_STATE 里作品数据所在的顶层键都以这个前缀开头。
PHOTO_KEY_PREFIX = "tusjoh"
# INIT_STATE 里个别字段被二次转义，解析失败时先去掉转义再试。
ESCAPED_QUOTES = (
    '"{"err_msg":"launchApplication:fail"}"',
    '"{"err_msg":"system:access_denied"}"',
)


def _headers() -> dict[str, str]:
    """作品页要的请求头，桌面端 UA 才能拿到内嵌数据。"""
    return {
        "User-Agent": common.DESKTOP_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.kuaishou.com/",
    }


def _content_id(url: str) -> str:
    """从跳转后的地址里取作品编号。"""
    for _kind, pattern in ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return ""


def _load_state(html: str, pattern: re.Pattern[str]) -> dict[str, Any] | None:
    """从页面里取出内嵌的 JSON 数据。"""
    match = pattern.search(html)
    if not match:
        return None
    raw = match.group(1).strip().rstrip(";")
    try:
        data = json.loads(raw)
    except ValueError:
        # 字段被二次转义时，去掉转义再试一次。
        cleaned = raw.replace("\\\\", "\\").replace('\\"', '"')
        for needle in ESCAPED_QUOTES:
            cleaned = cleaned.replace(needle, needle[1:-1])
        try:
            data = json.loads(cleaned)
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


def _walk(node: Any) -> dict[str, Any] | None:
    """在嵌套结构里递归找带 photo 的节点（页面结构变动时的兜底）。"""
    if isinstance(node, dict):
        photo = node.get("photo")
        if isinstance(photo, dict) and (
            photo.get("srcNoMark") or photo.get("photoUrl") or photo.get("ext_params")
        ):
            return photo
        for value in node.values():
            found = _walk(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _walk(item)
            if found is not None:
                return found
    return None


def _find_photo(state: dict[str, Any]) -> dict[str, Any] | None:
    """
    在内嵌数据里找作品节点。

    作品数据放在顶层键以 `tusjoh` 开头的条目下（每个条目一个作品），
    优先取第一个带媒体信息的条目。
    """
    for key, value in state.items():
        if not str(key).startswith(PHOTO_KEY_PREFIX) or not isinstance(value, dict):
            continue
        photo = value.get("photo")
        if isinstance(photo, dict) and (
            photo.get("srcNoMark") or photo.get("photoUrl") or photo.get("ext_params")
        ):
            return photo
    # 顶层没命中时再递归找一遍，兼容页面结构变动。
    return _walk(state)


def _atlas_images(photo: dict[str, Any]) -> list[str]:
    """图集作品：取 ext_params.atlas.list 里的原图地址。"""
    ext = photo.get("ext_params")
    if not isinstance(ext, dict):
        return []
    atlas = ext.get("atlas")
    if not isinstance(atlas, dict):
        return []
    items = atlas.get("list")
    if not isinstance(items, list):
        return []
    result: list[str] = []
    for item in items:
        url = common.first_url(item) or common.text_of(item)
        if url:
            result.append(common.https_url(url))
    return result


def _video_url(photo: dict[str, Any]) -> str:
    """视频作品：优先无水印地址。"""
    for key in ("srcNoMark", "photoUrl", "playUrl", "photo"):
        value = photo.get(key)
        url = common.text_of(value) if not isinstance(value, dict) else ""
        if url:
            return common.https_url(url)
    return ""


def _count(value: Any) -> str:
    """点赞数这类数字，统一成「1.2万」这种可读形式。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""
    if number >= 10000:
        return f"{number / 10000:.1f}万"
    return str(number)


def _build(photo: dict[str, Any], source_url: str) -> dict[str, Any]:
    """把 photo 节点转成统一结构。"""
    title = common.block(photo.get("caption"))
    user = photo.get("userName") or photo.get("user_name") or ""
    if isinstance(user, dict):
        user = user.get("name") or ""

    images = _atlas_images(photo)
    media: dict[str, Any] = {
        "platform": "kuaishou",
        "platform_name": "快手",
        "kind": "video",
        "title": title.split("\n")[0] if title else "快手作品",
        "desc": title,
        "stats": [],
        "author": common.text_of(user),
        "duration": None,
        "cover": "",
        "play_url": "",
        "images": [],
        "source_url": source_url,
    }

    like = _count(photo.get("likeCount"))
    if like:
        media["stats"].append(f"点赞 {like}")

    if images:
        media["kind"] = "images"
        media["images"] = images
        cover = common.first_url(photo.get("coverUrls") or [])
        if cover:
            media["cover"] = common.https_url(cover)
        return media

    play_url = _video_url(photo)
    if not play_url:
        raise VideoParseError(
            "这个快手作品里没有找到视频地址",
            "作品可能已删除，或页面结构有变化",
        )
    media["play_url"] = play_url
    cover = common.first_url(photo.get("coverUrl") or photo.get("coverUrls") or [])
    if cover:
        media["cover"] = common.https_url(cover)
    return media


async def parse(url: str, cookie: str, host: Any, quality: str = "") -> dict[str, Any]:
    """
    解析一条快手链接。

    Args:
        url: 快手分享链接。
        cookie: 保留参数，作品页不需要登录。
        host: 插件宿主视图。
        quality: 保留参数，快手只给一种画质。
    Returns:
        统一结构的媒体信息。
    Raises:
        VideoParseError: 链接里没有作品编号，或页面里没有媒体数据。
    """
    # 分享链接先跳到作品页，跳转后的地址里才有作品编号。
    final_url = await common.follow_short_link(
        host, url, headers=_headers(), timeout=10.0
    )
    content_id = _content_id(final_url)
    if not content_id:
        raise VideoParseError(
            "没有从链接里识别出快手作品编号",
            "请复制 App 分享出来的完整链接",
        )

    result = await host.http_request(
        "GET",
        final_url,
        headers=_headers(),
        timeout=12.0,
        max_chars=400000,
    )
    html = str(result.get("text") or "")
    if not html:
        raise VideoParseError(
            "快手作品页没有返回内容", "作品可能已删除，或接口临时不可用"
        )

    # INIT_STATE 优先，缺失时用 APOLLO_STATE 兜底。
    photo: dict[str, Any] | None = None
    for pattern in (INIT_STATE, APOLLO_STATE):
        state = _load_state(html, pattern)
        if state is None:
            continue
        photo = _find_photo(state)
        if photo is not None:
            break

    if photo is None:
        raise VideoParseError(
            "快手页面里没有找到作品数据",
            "作品可能已删除、设为私密，或页面结构有变化",
        )
    return _build(photo, url)
