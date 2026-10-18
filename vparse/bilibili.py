"""
哔哩哔哩解析。

优先走官方 web 接口：view 拿标题、封面与时长，playurl 用 html5 + high_quality 拿单文件 mp4。
不用 DASH 分轨是因为分轨需要 ffmpeg 合流，而 html5 单文件自带音轨、拿到就能播。
游客态一般能到 1080P；配置 Cookie 后可以请求更高清晰度。
"""

from __future__ import annotations

import re
from typing import Any

from vparse import common
from vparse.common import VideoParseError


VIEW_API = "https://api.bilibili.com/x/web-interface/view"
PLAY_API = "https://api.bilibili.com/x/player/playurl"

# 清晰度编号与高度、名称的对应关系，取自官方 playurl 文档。
QUALITY_TABLE = {
    127: (4320, "8K 超清"),
    126: (2160, "杜比视界"),
    125: (2160, "HDR 真彩"),
    120: (2160, "4K 超清"),
    116: (1080, "1080P60"),
    112: (1080, "1080P+"),
    80: (1080, "1080P"),
    74: (720, "720P60"),
    64: (720, "720P"),
    32: (480, "480P"),
    16: (360, "360P"),
    6: (240, "240P"),
}

BVID_PATTERN = re.compile(r"(BV[0-9A-Za-z]{10})")
AID_PATTERN = re.compile(r"/video/av(\d+)", re.IGNORECASE)
PAGE_PATTERN = re.compile(r"[?&]p=(\d+)")

# view 接口里的互动数据，字段 → 展示标签。
STAT_PAIRS: tuple[tuple[Any, str], ...] = (
    ("view", "播放"),
    ("danmaku", "弹幕"),
    ("like", "点赞"),
    ("favorite", "收藏"),
)


def _api_headers(cookie: str) -> dict[str, str]:
    """B站接口要求的请求头，带 Cookie 时能拿到更高清晰度。"""
    headers = {
        "User-Agent": common.DESKTOP_UA,
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def extract_ids(text: str) -> tuple[str, str, int]:
    """从各种形态的链接里取出 bvid、av 号与分P序号。"""
    content = str(text or "")
    bvid = BVID_PATTERN.search(content)
    aid = AID_PATTERN.search(content)
    page = PAGE_PATTERN.search(content)
    return (
        bvid.group(1) if bvid else "",
        aid.group(1) if aid else "",
        max(1, int(page.group(1))) if page else 1,
    )


async def _resolve_short_link(url: str, cookie: str, host: Any) -> str:
    """b23.tv 短链手工跟随跳转，拿到视频详情页地址。"""
    if "b23.tv" not in common.host_of(url):
        return url
    return await common.follow_short_link(
        host, url, headers=_api_headers(cookie), timeout=8.0
    )


def _pick_cid(data: dict[str, Any], page: int) -> int:
    """多 P 视频按 p 参数选对应分集，单 P 直接返回 cid。"""
    pages = data.get("pages") or []
    if pages:
        index = min(max(page, 1), len(pages)) - 1
        try:
            return int(pages[index].get("cid") or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(data.get("cid") or 0)
    except (TypeError, ValueError):
        return 0


def _page_title(data: dict[str, Any], page: int) -> str:
    """多 P 视频返回当前分集标题。"""
    pages = data.get("pages") or []
    if len(pages) <= 1:
        return ""
    index = min(max(page, 1), len(pages)) - 1
    return common.text_of(pages[index].get("part"))


async def _pick_play_url(
    bvid: str, cid: int, cookie: str, host: Any
) -> tuple[str, str]:
    """
    取单文件播放地址，按 1080P → 720P → 480P → 360P 逐级降级。

    Returns:
        (播放地址, 清晰度名称)。
    """
    headers = _api_headers(cookie)
    last_message = ""
    for qn in (80, 64, 32, 16):
        result = await host.http_request(
            "GET",
            PLAY_API,
            headers=headers,
            query={
                "bvid": bvid,
                "cid": cid,
                "qn": qn,
                "fnval": 1,
                "fourk": 1,
                "platform": "html5",
                "high_quality": 1,
            },
            timeout=8.0,
        )
        payload = common.json_of(result)
        if payload.get("code") != 0:
            last_message = common.text_of(payload.get("message"))
            continue
        data = payload.get("data") or {}
        durl = data.get("durl") or []
        if not durl:
            continue
        url = common.text_of(durl[0].get("url"))
        if not url:
            continue
        quality = data.get("quality") or qn
        try:
            quality = int(quality)
        except (TypeError, ValueError):
            quality = qn
        _, label = QUALITY_TABLE.get(quality, (None, f"{quality}P"))
        return url, label
    raise VideoParseError(
        "没有取到可播放地址" + (f"（{last_message}）" if last_message else ""),
        "该视频可能需要大会员，或已限制外部分享",
    )


async def parse(url: str, cookie: str, host: Any) -> dict[str, Any]:
    """
    解析一条 B 站链接。

    Args:
        url: 视频详情页链接、b23.tv 短链或 BV/av 号链接。
        cookie: 安装配置里的 B 站 Cookie，可为空。
        host: 插件宿主视图。
    Returns:
        统一结构的媒体信息。
    Raises:
        VideoParseError: 视频号缺失、接口报错或没有可播放地址。
    """
    resolved = await _resolve_short_link(url, cookie, host)
    bvid, aid, page = extract_ids(resolved)
    if not bvid and not aid:
        raise VideoParseError(
            "没有从链接里识别到视频号", "支持 /video/BV 开头、av 号以及 b23.tv 短链"
        )

    params = {"bvid": bvid} if bvid else {"aid": aid}
    result = await host.http_request(
        "GET", VIEW_API, headers=_api_headers(cookie), query=params, timeout=8.0
    )
    payload = common.json_of(result)
    if payload.get("code") != 0:
        raise VideoParseError(
            f"B 站返回错误：{common.text_of(payload.get('message')) or '未知原因'}",
            "视频可能已删除、仅限登录可见或地区受限",
        )
    data = payload.get("data") or {}
    cid = _pick_cid(data, page)
    if not cid:
        raise VideoParseError("这个视频没有可播放的分集信息")

    play_url, quality = await _pick_play_url(
        common.text_of(data.get("bvid")) or bvid, cid, cookie, host
    )
    owner = data.get("owner") or {}
    return {
        "platform": "bilibili",
        "platform_name": "哔哩哔哩",
        "kind": "video",
        "title": common.text_of(data.get("title")) or "B站视频",
        "desc": common.block(data.get("desc")),
        "stats": common.count_line(STAT_PAIRS, data.get("stat") or {}),
        "author": common.text_of(owner.get("name")),
        "duration": int(data.get("duration") or 0) or None,
        "cover": common.https_url(common.text_of(data.get("pic"))),
        "play_url": play_url,
        "images": [],
        "quality": quality,
        "part": _page_title(data, page),
        "source_url": url,
    }
