"""
今日头条（头条号）解析。

分享链接跳到移动端作品页后，页面里有一段
`<script id="RENDER_DATA" type="application/json">...</script>`，
内容是 URL 编码后的 JSON，解码后从 `articleInfo` 取作品信息：

- `title` 标题、`mediaUser.screenName` 作者、`posterUrl` 封面；
- `videoDuration` 时长、`diggCount` / `commentCount` / `impressionCount` 互动数；
- `playAuthTokenV2` 是播放凭证。

播放地址要两步才能拿到：

1. `playAuthTokenV2` 是 base64，解开后是 `GetPlayInfoToken`；
2. 拿它请求字节跳动的 VOD 调度接口 `vod.bytedanceapi.com/?{token}`，
   返回的 `Result.Data.PlayInfoList[]` 里 `MainPlayUrl` 就是播放地址，
   `BackupPlayUrl` 是备用地址（主地址被拒时换它）。

整条链路都是自己发请求、自己解析，不经过任何第三方中转服务。
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any
from urllib.parse import unquote

from vparse import common
from vparse.common import VideoParseError


# 移动端作品页：桌面端页面已经是 JS 壳，取不到内嵌数据。
MOBILE_PAGE = "https://m.toutiao.com/video/{item_id}/"
# 字节跳动 VOD 调度接口，凭证直接拼在查询串里。
VOD_API = "https://vod.bytedanceapi.com/?{token}"

# 页面里内嵌数据的起止标签。
RENDER_DATA = re.compile(
    r'<script id="RENDER_DATA" type="application/json">(.*?)</script>', re.DOTALL
)
# 链接里取作品编号。
ITEM_ID = re.compile(r"(?:video/|/video/|/i|/article/)(\d{10,25})", re.IGNORECASE)


def _headers() -> dict[str, str]:
    """作品页要的请求头，移动端 UA 才会返回内嵌数据。"""
    return {
        "User-Agent": common.MOBILE_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.toutiao.com/",
    }


def _item_id(url: str) -> str:
    """从链接里取作品编号。"""
    match = ITEM_ID.search(url)
    return match.group(1) if match else ""


def _count(value: Any) -> str:
    """互动数，统一成「1.2万」这种形式。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""
    if number >= 10000:
        return f"{number / 10000:.1f}万"
    return str(number)


def _play_token(article: dict[str, Any]) -> str:
    """从 playAuthTokenV2 里解出 VOD 调度接口要的 token。"""
    raw = common.text_of(article.get("playAuthTokenV2"))
    if not raw:
        return ""
    try:
        # base64 解开后是 {"GetPlayInfoToken": "..."}
        decoded = base64.b64decode(raw + "==").decode("utf-8", "replace")
        payload = json.loads(decoded)
    except (ValueError, binascii.Error):
        return ""
    return common.text_of(payload.get("GetPlayInfoToken"))


def _pick_play_url(play_list: Any) -> tuple[str, str]:
    """从 VOD 返回的档位里挑清晰度最高的一个，返回 (地址, 档位名)。"""
    if not isinstance(play_list, list):
        return "", ""
    best_url = ""
    best_label = ""
    best_rank = -1
    for item in play_list:
        if not isinstance(item, dict):
            continue
        url = common.text_of(item.get("MainPlayUrl"))
        if not url:
            url = common.text_of(item.get("BackupPlayUrl"))
        if not url:
            continue
        # 高度优先，同高度时码率高的优先。
        try:
            height = int(item.get("Height") or 0)
        except (TypeError, ValueError):
            height = 0
        try:
            bitrate = int(item.get("Bitrate") or 0)
        except (TypeError, ValueError):
            bitrate = 0
        rank = height * 1000 + bitrate
        if rank > best_rank:
            best_url = url
            best_label = common.text_of(item.get("Definition"))
            best_rank = rank
    return best_url, best_label


def _build(article: dict[str, Any], source_url: str) -> dict[str, Any]:
    """把 articleInfo 节点转成统一结构。"""
    title = common.block(article.get("title"))
    user = article.get("mediaUser")
    author = ""
    if isinstance(user, dict):
        author = common.text_of(user.get("screenName") or user.get("name"))
    elif user:
        author = common.text_of(user)

    stats: list[str] = []
    for key, name in (
        ("impressionCount", "浏览"),
        ("diggCount", "点赞"),
        ("commentCount", "评论"),
    ):
        text = _count(article.get(key))
        if text:
            stats.append(f"{name} {text}")

    duration = 0
    try:
        duration = int(article.get("videoDuration") or 0)
    except (TypeError, ValueError):
        duration = 0

    return {
        "platform": "toutiao",
        "platform_name": "今日头条",
        "kind": "video",
        "title": title.split("\n")[0] if title else "头条作品",
        "desc": title,
        "stats": stats,
        "author": author,
        "duration": duration or None,
        "cover": common.https_url(common.text_of(article.get("posterUrl"))),
        "play_url": "",
        "images": [],
        "source_url": source_url,
    }


async def parse(url: str, cookie: str, host: Any, quality: str = "") -> dict[str, Any]:
    """
    解析一条今日头条链接。

    Args:
        url: 头条分享链接。
        cookie: 保留参数，作品页不需要登录。
        host: 插件宿主视图。
        quality: 保留参数，接口按档位返回，插件自动取最高的一档。
    Returns:
        统一结构的媒体信息。
    Raises:
        VideoParseError: 链接里没有作品编号，或页面里没有作品数据。
    """
    item_id = _item_id(url)
    if not item_id:
        # 分享链接可能带别的路径，先跟一次跳转再找编号。
        final_url = await common.follow_short_link(
            host, url, headers=_headers(), timeout=10.0
        )
        item_id = _item_id(final_url)
    if not item_id:
        raise VideoParseError(
            "没有从链接里识别出头条作品编号",
            "请复制 App 分享出来的完整链接",
        )

    result = await host.http_request(
        "GET",
        MOBILE_PAGE.format(item_id=item_id),
        headers=_headers(),
        timeout=12.0,
        max_chars=400000,
    )
    html = str(result.get("text") or "")
    if not html:
        raise VideoParseError(
            "头条作品页没有返回内容", "作品可能已删除，或接口临时不可用"
        )

    match = RENDER_DATA.search(html)
    if not match:
        raise VideoParseError(
            "头条页面里没有找到作品数据",
            "作品可能已失效，或页面结构有变化",
        )

    # RENDER_DATA 的内容是 URL 编码过的，解码后再当 JSON 解析。
    try:
        payload = json.loads(unquote(match.group(1)))
    except ValueError:
        raise VideoParseError(
            "头条作品数据解析失败", "作品可能已失效，或页面结构有变化"
        ) from None
    article = payload.get("articleInfo") if isinstance(payload, dict) else None
    if not isinstance(article, dict) or not article:
        raise VideoParseError(
            "头条没有返回作品数据", "分享链接可能已失效，请重新复制"
        )

    media = _build(article, url)

    # 播放地址要走 VOD 调度：先解出 token，再换播放地址。
    token = _play_token(article)
    if not token:
        raise VideoParseError(
            "头条作品里没有播放凭证", "作品可能已失效，或接口结构有变化"
        )
    vod = await host.http_request(
        "GET",
        VOD_API.format(token=token),
        headers={"User-Agent": common.DESKTOP_UA},
        timeout=12.0,
    )
    vod_payload = common.json_of(vod)
    data = ((vod_payload.get("Result") or {}) if isinstance(vod_payload, dict) else {}).get(
        "Data"
    )
    if not isinstance(data, dict):
        raise VideoParseError(
            "头条播放接口没有返回数据", "作品可能已失效，或接口临时不可用"
        )
    play_url, label = _pick_play_url(data.get("PlayInfoList"))
    if not play_url:
        raise VideoParseError(
            "头条作品里没有找到视频地址", "作品可能已失效，或接口结构有变化"
        )
    media["play_url"] = common.https_url(play_url)
    if label:
        media["quality"] = label
    return media
