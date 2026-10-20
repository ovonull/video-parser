"""
微博视频解析。

分享链接里带作品编号（fid），从编号换出作品页地址，再 POST 到微博的组件接口：

`POST weibo.com/tv/api/component?page=/tv/show/{fid}`
body `data={"Component_Play_Playinfo":{"oid": fid}}`

返回里 `data.Component_Play_Playinfo` 是作品信息：
`urls` 是各档清晰度的地址（键名形如 `高清 1080P`），还有标题、作者、封面与互动数据。

**微博现在要求带 Cookie**：不带会被跳到新浪访客系统（Sina Visitor System），
访客 Cookie 需要浏览器执行 JS 生成，纯出网请求拿不到。
所以这里沿用插件的 Cookie 配置项，用户填一次微博 Cookie 即可。

视频地址用微博返回的原始地址，不走任何第三方代理；
原始地址需要 Referer 头，NapCat 拉取时若被拒，可在配置里改用备用地址。
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

from vparse import common
from vparse.common import VideoParseError


API = "https://weibo.com/tv/api/component?page={page}"

# 清晰度优先级：数字越大越清晰。
QUALITY_PRIORITY: tuple[tuple[str, int], ...] = (
    ("2K", 5),
    ("4K", 5),
    ("超清", 4),
    ("1080P", 3),
    ("720P", 2),
    ("480P", 1),
    ("360P", 0),
)

# 作品编号的来源：链接里的 fid 参数，或路径里的数字。
FID_PARAM = re.compile(r"[?&]fid=(\d+)", re.IGNORECASE)
TV_PATH = re.compile(r"weibo\.com/tv/(?:show|v)/(\d+)", re.IGNORECASE)


def _headers(cookie: str) -> dict[str, str]:
    """接口要的请求头。"""
    headers = {
        "User-Agent": common.DESKTOP_UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://weibo.com/",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _fid(url: str) -> str:
    """从链接里取作品编号。"""
    match = FID_PARAM.search(url)
    if match:
        return match.group(1)
    match = TV_PATH.search(url)
    return match.group(1) if match else ""


def _quality_rank(label: str) -> int:
    """按档位名算出清晰度优先级。"""
    text = str(label or "").upper()
    for needle, rank in QUALITY_PRIORITY:
        if needle.upper() in text:
            return rank
    return -1


def _best_url(urls: Any) -> tuple[str, str]:
    """从各档地址里挑最清晰的一个，返回 (地址, 档位名)。"""
    if not isinstance(urls, dict):
        return "", ""
    best_url = ""
    best_label = ""
    best_rank = -1
    for label, url in urls.items():
        text = common.text_of(url)
        if not text:
            continue
        rank = _quality_rank(label)
        if rank > best_rank:
            best_url, best_label, best_rank = text, common.text_of(label), rank
    return best_url, best_label


def _count(value: Any) -> str:
    """互动数，统一成「1.2万」这种形式。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""
    if number >= 10000:
        return f"{number / 10000:.1f}万"
    return str(number)


def _build(info: dict[str, Any], source_url: str) -> dict[str, Any]:
    """把作品信息转成统一结构。"""
    title = common.block(info.get("title"))
    play_url, label = _best_url(info.get("urls"))
    if not play_url:
        raise VideoParseError(
            "这个微博作品里没有找到视频地址",
            "作品可能已删除，或接口返回的数据里没有播放地址",
        )

    stats: list[str] = []
    for key, name in (
        ("play_count", "播放"),
        ("reposts_count", "转发"),
        ("comments_count", "评论"),
        ("attitudes_count", "点赞"),
    ):
        text = _count(info.get(key))
        if text:
            stats.append(f"{name} {text}")

    duration = 0
    try:
        duration = int(info.get("duration_time") or 0)
    except (TypeError, ValueError):
        duration = 0

    media: dict[str, Any] = {
        "platform": "weibo",
        "platform_name": "微博",
        "kind": "video",
        "title": title.split("\n")[0] if title else "微博作品",
        "desc": title,
        "stats": stats,
        "author": common.text_of(info.get("author")),
        "duration": duration or None,
        "cover": common.https_url(common.text_of(info.get("cover_image"))),
        "play_url": common.https_url(play_url),
        "images": [],
        "source_url": source_url,
    }
    if label:
        media["quality"] = label
    return media


async def parse(url: str, cookie: str, host: Any, quality: str = "") -> dict[str, Any]:
    """
    解析一条微博链接。

    Args:
        url: 微博分享链接。
        cookie: 微博 Cookie，必填（微博要求登录态）。
        host: 插件宿主视图。
        quality: 保留参数，接口按档位返回，插件自动取最清晰的一档。
    Returns:
        统一结构的媒体信息。
    Raises:
        VideoParseError: 链接里没有作品编号，或接口没返回作品数据。
    """
    fid = _fid(url)
    if not fid and "t.cn" in common.host_of(url):
        # t.cn 短链要先跳转一次才知道作品编号。
        final_url = await common.follow_short_link(
            host, url, headers=_headers(cookie), timeout=10.0
        )
        fid = _fid(final_url)
    if not fid:
        raise VideoParseError(
            "没有从链接里识别出微博作品编号",
            "请复制微博 App 分享出来的完整链接（需要带 fid 参数）",
        )

    body = json.dumps(
        {"Component_Play_Playinfo": {"oid": fid}}, ensure_ascii=False
    )
    # 宿主出网只支持 JSON body；微博的组件接口也接受 JSON 形态的 data。
    result = await host.http_request(
        "POST",
        API.format(page=quote(f"/tv/show/{fid}", safe="")),
        headers=_headers(cookie),
        json_body={"Component_Play_Playinfo": {"oid": fid}},
        timeout=12.0,
    )
    payload = common.json_of(result)
    if not payload:
        # 没拿到 JSON 通常是被跳到了访客系统，说明 Cookie 无效或没填。
        if common.status_of(result) in (301, 302, 303, 307, 308):
            raise VideoParseError(
                "微博要求登录后才能解析",
                "请在插件配置里填好「微博 Cookie」，没有 Cookie 会被跳到新浪访客系统",
            )
        raise VideoParseError(
            "微博接口没有返回数据", "作品可能已删除，或接口临时不可用"
        )

    info = (payload.get("data") or {}).get("Component_Play_Playinfo")
    if not isinstance(info, dict) or not info:
        message = common.text_of(payload.get("msg")) or "接口没有返回作品数据"
        raise VideoParseError(f"微博解析失败：{message}")
    return _build(info, url)
