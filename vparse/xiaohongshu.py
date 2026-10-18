"""
小红书解析。

小红书没有对外开放的接口，网页端未登录一律 302 到登录页（实测首页与笔记页都是），
所以**必须配置 Cookie**，这一点没有绕过的办法：

1. 先从链接里取笔记号（24 位十六进制），xhslink.com 短链先手工跟随跳转；
2. 带 Cookie 请求笔记详情页，取 window.__INITIAL_STATE__ 里的笔记节点；
3. 视频作品取 media.stream 下的 masterUrl，图文作品取 imageList 的原始图片。

页面把 JS 字面量 undefined 直接写进了这段状态，直接 json 解析会失败，需要先替换成 null。
"""

from __future__ import annotations

import json
import re
from typing import Any

from vparse import common
from vparse.common import VideoParseError


EXPLORE_PAGE = "https://www.xiaohongshu.com/explore/{note_id}"
STATE_MARK = "window.__INITIAL_STATE__"
# 笔记页状态动辄几百 KB，按宿主允许的最大值申请，避免被响应长度截断。
PAGE_CHARS = 200000

NOTE_ID_PATTERNS = (
    re.compile(r"/(?:explore|discovery/item)/([0-9a-f]{24})", re.IGNORECASE),
    re.compile(r"[?&]note[_-]?id=([0-9a-f]{24})", re.IGNORECASE),
)

# 状态里的 JS 字面量 undefined 不是合法 JSON，只替换出现在冒号、逗号、中括号之后的那部分。
JS_UNDEFINED = re.compile(r"(?<=[:,\[])\s*undefined\b")

# 视频流的优先级：优先 H.264，兼容性最好。
STREAM_KEYS = ("h264", "h265", "av1", "h266")

# 互动数据在不同版本里分别是驼峰与下划线写法，按顺序取第一个取得到的。
STAT_FIELDS: tuple[tuple[str, ...], str] = (
    (("likedCount", "liked_count"), "点赞"),
    (("collectedCount", "collected_count"), "收藏"),
    (("commentCount", "comment_count"), "评论"),
)


def _page_headers(cookie: str) -> dict[str, str]:
    """笔记页要求的请求头，Cookie 必填。"""
    return {
        "User-Agent": common.DESKTOP_UA,
        "Referer": "https://www.xiaohongshu.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie,
    }


def extract_id(text: str) -> str:
    """从链接或跳转后的地址里取笔记号。"""
    for pattern in NOTE_ID_PATTERNS:
        match = pattern.search(str(text or ""))
        if match:
            return match.group(1).lower()
    return ""


async def _resolve(url: str, cookie: str, host: Any) -> str:
    """短链先跟随跳转，返回最终的笔记地址。"""
    hostname = common.host_of(url)
    if "xhslink.com" not in hostname:
        return url
    return await common.follow_short_link(
        host, url, headers=_page_headers(cookie), timeout=8.0
    )


def parse_state(html: str) -> dict[str, Any] | None:
    """
    取出页面里的 window.__INITIAL_STATE__。

    Args:
        html: 笔记页 HTML。
    Returns:
        解析成功的状态字典；没有这段数据或 JSON 不完整时返回 None。
    """
    start = html.find(STATE_MARK)
    if start < 0:
        return None
    brace = html.find("{", start + len(STATE_MARK))
    if brace < 0:
        return None
    text = JS_UNDEFINED.sub("null", html[brace:])
    try:
        payload, _ = json.JSONDecoder().raw_decode(text)
    except ValueError:
        # 响应被截断时 JSON 不完整，这里直接判为失败，交给上层给提示。
        return None
    return payload if isinstance(payload, dict) else None


def find_note(node: Any) -> dict[str, Any] | None:
    """在状态树里递归找到带笔记号与媒体信息的节点。"""
    if isinstance(node, dict):
        if node.get("noteId") and (node.get("video") or node.get("imageList")):
            return node
        for value in node.values():
            found = find_note(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = find_note(value)
            if found:
                return found
    return None


def _stream_url(node: Any) -> str:
    """从一路码流里取首选地址。"""
    if not isinstance(node, dict):
        return ""
    url = common.text_of(node.get("masterUrl"))
    if url:
        return url
    for candidate in node.get("backupUrls") or []:
        text = common.text_of(candidate)
        if text:
            return text
    return ""


def video_url(video: dict[str, Any]) -> str:
    """从视频节点里取播放地址，按 h264 → h265 → av1 顺序找。"""
    if not isinstance(video, dict):
        return ""
    stream = ((video.get("media") or {}).get("stream") or {})
    for key in STREAM_KEYS:
        entries = stream.get(key) or []
        if isinstance(entries, list):
            for entry in entries:
                url = _stream_url(entry)
                if url:
                    return url
    # 老结构里码流直接挂在 video 下面。
    for key in STREAM_KEYS:
        entries = video.get(key) or []
        if isinstance(entries, list):
            for entry in entries:
                url = _stream_url(entry)
                if url:
                    return url
    key = common.text_of((video.get("consumer") or {}).get("originVideoKey"))
    if key:
        return f"https://sns-video-bd.xhscdn.com/{key}"
    return ""


def _image_url(image: Any) -> str:
    """取图片原图地址，优先 urlDefault，其次 infoList 的最后一项。"""
    if not isinstance(image, dict):
        return ""
    url = common.text_of(image.get("urlDefault")) or common.text_of(image.get("url"))
    if url:
        return common.https_url(url)
    info = image.get("infoList") or []
    if isinstance(info, list):
        for item in reversed(info):
            text = common.text_of((item or {}).get("url"))
            if text:
                return common.https_url(text)
    return ""


def _duration(video: dict[str, Any]) -> int | None:
    """取时长（秒），小红书老字段给秒、个别渠道给毫秒，这里做一次兜底换算。"""
    raw = (video.get("capa") or {}).get("duration")
    try:
        seconds = int(float(raw))
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    if seconds > 7200:
        seconds //= 1000
    return seconds or None


def _cover(note: dict[str, Any], video: dict[str, Any]) -> str:
    """封面：优先视频封面，其次首图。"""
    for source in (
        video.get("image"),
        (note.get("imageList") or [None])[0],
        ((video.get("media") or {}).get("video") or {}).get("thumbnail"),
    ):
        if isinstance(source, dict):
            url = _image_url(source)
            if url:
                return url
    return ""


def _title(note: dict[str, Any]) -> str:
    """标题：优先 title 字段，没有标题时取正文第一行。"""
    title = common.text_of(note.get("title"))
    if title:
        return title
    desc = common.text_of(note.get("desc"))
    if desc:
        return desc.splitlines()[0]
    return "小红书笔记"


def _stats(note: dict[str, Any]) -> list[str]:
    """取点赞、收藏、评论数，字段名有驼峰与下划线两种写法。"""
    source = note.get("interactInfo") or note.get("interact_info") or {}
    if not isinstance(source, dict):
        return []
    parts: list[str] = []
    for keys, label in STAT_FIELDS:
        for key in keys:
            count = common.format_count(source.get(key))
            if count:
                parts.append(f"{label} {count}")
                break
    return parts


def build(note: dict[str, Any], source_url: str) -> dict[str, Any]:
    """把笔记节点转成统一结构，视频与图文分开处理。"""
    video = note.get("video") or {}
    user = note.get("user") or {}
    media: dict[str, Any] = {
        "platform": "xiaohongshu",
        "platform_name": "小红书",
        "kind": "video",
        "title": _title(note),
        "desc": common.block(note.get("desc")),
        "stats": _stats(note),
        "author": common.text_of(user.get("nickname") or user.get("nickName")),
        "duration": _duration(video),
        "cover": _cover(note, video),
        "play_url": "",
        "images": [],
        "source_url": source_url,
    }

    play_url = video_url(video)
    if play_url:
        media["play_url"] = play_url
        return media

    for image in note.get("imageList") or []:
        url = _image_url(image)
        if url:
            media["images"].append(url)
    if media["images"]:
        media["kind"] = "images"
        media["duration"] = None
        return media

    raise VideoParseError(
        "这条笔记里没有找到视频或图片",
        "可能是纯文字笔记、已删除，或仅作者本人可见",
    )


def _missing_cookie() -> VideoParseError:
    """统一的小红书 Cookie 缺失提示。"""
    return VideoParseError(
        "小红书需要登录 Cookie 才能解析",
        "小红书未登录一律跳登录页或 404 页，请在插件配置里填「小红书 Cookie」（网页版登录后从浏览器复制）",
    )


def _redirect_error(location: str) -> VideoParseError:
    """笔记页发生跳转时，按跳转目标给出可执行的提示。"""
    if "/login" in location:
        return VideoParseError(
            "小红书把请求跳到了登录页，Cookie 无效或已过期",
            "重新在网页版登录后复制完整 Cookie（含 a1 与 web_session），更新插件配置里的「小红书 Cookie」",
        )
    if "404" in location:
        # 实测未登录访问笔记页一定被跳到 404 页，这里按最常见的成因给提示。
        return VideoParseError(
            "小红书把请求跳到了 404 页（未登录或触发风控）",
            "请在插件配置里填有效的小红书 Cookie；Cookie 正确时笔记页才会直接返回内容",
        )
    return VideoParseError(
        "小红书返回了跳转", f"跳转地址：{common.clip(location, 80)}"
    )


async def parse(url: str, cookie: str, host: Any) -> dict[str, Any]:
    """
    解析一条小红书链接。

    Args:
        url: 笔记链接，支持 /explore/、/discovery/item/ 与 xhslink.com 短链。
        cookie: 安装配置里的小红书 Cookie，必填。
        host: 插件宿主视图。
    Returns:
        统一结构的媒体信息。
    Raises:
        VideoParseError: 缺 Cookie、笔记号缺失、跳登录页或页面里没有笔记数据。
    """
    cookie = common.text_of(cookie)
    if not cookie:
        raise _missing_cookie()

    resolved = await _resolve(url, cookie, host)
    note_id = extract_id(resolved) or extract_id(url)
    if not note_id:
        raise VideoParseError(
            "没有从链接里识别到笔记号",
            "请复制小红书 App 的分享链接，或 www.xiaohongshu.com/explore/ 开头的地址",
        )

    result = await host.http_request(
        "GET",
        EXPLORE_PAGE.format(note_id=note_id),
        headers=_page_headers(cookie),
        timeout=10.0,
        max_chars=PAGE_CHARS,
    )
    status = common.status_of(result)
    location = common.headers_of(result).get("location") or ""
    if status in (301, 302, 303, 307, 308):
        raise _redirect_error(location)
    if status >= 500:
        raise VideoParseError(
            f"小红书返回 {status}",
            "Cookie 可能无效（缺少 a1 或 web_session），或这条笔记不存在",
        )
    if status >= 400:
        raise VideoParseError(
            f"小红书返回 {status}",
            "笔记可能已删除、设为私密，或需要登录才能查看",
        )

    state = parse_state(str(result.get("text") or ""))
    if state is None:
        raise VideoParseError(
            "没有从笔记页取到数据",
            "Cookie 可能已过期，或这条笔记需要登录与额外权限；也可能是页面过大被截断",
        )
    note = find_note(state)
    if note is None:
        raise VideoParseError(
            "笔记页里没有笔记内容",
            "笔记可能已删除、设为私密，或当前账号无权查看",
        )
    return build(note, url)
