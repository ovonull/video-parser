"""
视频解析插件：只给 AI 提供工具，不监听消息、不主动发送任何内容。

AI 在处理视频分享链接时调用 ``parse_video_link``；工具返回作品信息与待发的
视频/图片，由宿主把媒体排在 AI 回复之后统一发出。插件自己不抢占消息，也不
调用消息发送能力，这条消息的回复主体始终是 AI。

约定：
- 出网一律走 host.http_request，域名受清单白名单约束；
- 解析失败返回一段模型读得懂的中文说明，不向宿主抛裸异常；
- 媒体通过返回体里的 ``attachments`` 交给宿主，不直接调用 OneBot。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.services.plugin_sdk import PluginCallError, on_tool

from vparse import bilibili, common, douyin, jimengai, kuaishou, pipigx
from vparse import doubao, pipixia, toutiao, weibo, xiaohongshu
from vparse.cache import MediaCache
from vparse.common import VideoParseError


DEFAULT_CACHE_SECONDS = 600
MAX_CACHE_SECONDS = 3600
MAX_TITLE_LENGTH = 80
MAX_DESC_LENGTH = 260
MIN_DESC_LENGTH = 4
MAX_DURATION_LIMIT = 3600
# 图文作品一次最多排队几张原图：太多会让补发拖得很久，也会挤占群消息。
MAX_IMAGE_ATTACHMENTS = 9
TOOL_DESC_LIMIT = 260
# 解析要等平台接口，分享页还会重试三次；给足时间，别让模型只拿到一句「超时」。
TOOL_PARSE_TIMEOUT_SECONDS = 35.0

# 跨会话的解析结果缓存：同一条链接被反复问到时不再重复请求平台。
_media_cache = MediaCache(64)


def _text_config(host: Any, key: str, default: str) -> str:
    """读取文本型配置，缺失时返回默认值。"""
    value = host.config.get(key)
    text = str(value).strip() if value is not None else ""
    return text or default


def _choice(host: Any, key: str, default: str) -> str:
    """读取下拉型配置，空值按默认项处理。"""
    return _text_config(host, key, default)


def _int_config(host: Any, key: str, default: int, low: int, high: int) -> int:
    """读取数字型配置，非法或越界时回落到默认值。"""
    raw = host.config.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    return max(low, min(value, high))


def _seconds_config(host: Any, key: str, default: int, high: int) -> int:
    """读取「秒」类配置；0 与空值按默认处理，想显式关闭填 -1。"""
    raw = host.config.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    if value < 0:
        return 0
    if value == 0:
        return default
    return min(value, high)


def _describe(media: dict[str, Any]) -> str:
    """拼描述卡片：标题、作者与时长等元信息、互动数据、正文简介。"""
    title = common.flatten(media.get("title"))
    lines = [
        f"【{media.get('platform_name')}】"
        f"{common.clip(title, MAX_TITLE_LENGTH) or '未命名作品'}"
    ]

    meta: list[str] = []
    if media.get("author"):
        meta.append(f"@{media['author']}")
    duration = common.format_duration(media.get("duration"))
    if duration:
        meta.append(f"时长 {duration}")
    if media.get("quality"):
        meta.append(str(media["quality"]))
    if media.get("part"):
        meta.append(common.clip(media["part"], 30))
    if meta:
        lines.append(" · ".join(meta))

    stats = [str(item) for item in media.get("stats") or [] if item]
    if stats:
        lines.append(" · ".join(stats))

    body = common.block(media.get("desc"), MAX_DESC_LENGTH + MAX_TITLE_LENGTH)
    if title and body.startswith(title):
        body = body[len(title) :].strip()
    body = common.block(body, MAX_DESC_LENGTH)
    if len(body) < MIN_DESC_LENGTH or len(set(body)) == 1:
        body = ""
    if body:
        lines.append("")
        lines.append(body)
    return "\n".join(lines)


async def _dispatch_parse(
    host: Any, platform: str, url: str, quality: str
) -> dict[str, Any]:
    """按平台调用对应解析器。"""
    if platform == "douyin":
        return await douyin.parse(
            url, _text_config(host, "douyin_cookie", ""), host, quality
        )
    if platform == "bilibili":
        return await bilibili.parse(
            url, _text_config(host, "bilibili_cookie", ""), host, quality
        )
    if platform == "xiaohongshu":
        return await xiaohongshu.parse(
            url, _text_config(host, "xhs_cookie", ""), host, quality
        )
    if platform == "pipigx":
        return await pipigx.parse(url, "", host)
    if platform == "jimengai":
        return await jimengai.parse(url, "", host)
    if platform == "doubao":
        return await doubao.parse(
            url, _text_config(host, "doubao_cookie", ""), host
        )
    if platform == "kuaishou":
        return await kuaishou.parse(
            url, _text_config(host, "kuaishou_cookie", ""), host, quality
        )
    if platform == "toutiao":
        return await toutiao.parse(url, "", host, quality)
    if platform == "weibo":
        return await weibo.parse(
            url, _text_config(host, "weibo_cookie", ""), host, quality
        )
    if platform == "pipixia":
        return await pipixia.parse(url, "", host, quality)
    raise VideoParseError(
        "暂不支持这个平台",
        "目前支持抖音、B 站、小红书、快手、今日头条、微博、皮皮搞笑、"
        "皮皮虾、即梦 AI 与豆包 AI",
    )


def _attachments(host: Any, media: dict[str, Any]) -> list[dict[str, Any]]:
    """把作品里的视频/图片转成宿主待发队列条目。

    宿主会在 AI 正文发出之后逐条补发这些媒体；发不出来就只记日志，不再补
    直链，也不会改变已经发出的正文。
    """
    items: list[dict[str, Any]] = []
    if str(media.get("kind") or "") == "images":
        for url in (media.get("images") or [])[:MAX_IMAGE_ATTACHMENTS]:
            if url:
                items.append({"type": "image", "file": str(url)})
        live = common.text_of(media.get("live_video_url"))
        if live:
            items.append({"type": "video", "file": live})
        return items

    # 旧版本装过来的配置可能还留着「只发链接」，按「不发送媒体」处理。
    choice = _choice(host, "send_video", "发送视频")
    if choice in {"不发送视频", "只发链接"}:
        return items
    limit = _int_config(host, "max_duration", 0, 0, MAX_DURATION_LIMIT)
    duration = media.get("duration") or 0
    if limit and duration and duration > limit:
        return items
    play_url = str(media.get("play_url") or "")
    if not play_url:
        return items
    item: dict[str, Any] = {"type": "video", "file": play_url}
    mirror = str(media.get("play_url_mirror") or "")
    if mirror and mirror != play_url:
        item["fallbacks"] = [mirror]
    items.append(item)
    return items


def _tool_error(message: str) -> dict[str, Any]:
    """工具失败时回一段模型读得懂的中文说明，不把异常抛给对话循环。"""
    return {"ok": False, "error": message}


def _tool_payload(media: dict[str, Any], url: str, host: Any) -> dict[str, Any]:
    """把作品数据收敛成模型好用的结构，并挂上待发媒体。"""
    payload: dict[str, Any] = {
        "ok": True,
        "platform": str(media.get("platform_name") or ""),
        "url": url,
        "title": common.clip(media.get("title"), MAX_TITLE_LENGTH),
        "author": str(media.get("author") or ""),
        "duration": common.format_duration(media.get("duration")),
        "stats": [str(item) for item in media.get("stats") or [] if item],
        "desc": common.block(media.get("desc"), TOOL_DESC_LIMIT),
        "kind": str(media.get("kind") or ""),
        "image_count": len(media.get("images") or []),
        "card": _describe(media),
        "hint": (
            "视频或图片已排队，会由系统紧跟着你的回复发出。请按你的人设自然接话，"
            "可以点评作品，但不要贴链接、不要整段复述数据、不要说「已发送」，"
            "也不要编造工具没给到的信息。"
        ),
    }
    # 放在最后：返回给模型时可能被长度裁剪，前面的信息不能先被挤掉。
    # 宿主在裁剪前就取走这一项登记待发媒体，模型不需要看到这些地址。
    attachments = _attachments(host, media)
    if attachments:
        payload["attachments"] = attachments
    return payload


@on_tool("parse_video_link")
async def tool_parse_video_link(
    arguments: dict[str, Any], host: Any
) -> dict[str, Any]:
    """返回一条视频链接的解析结果；只取数据与待发媒体，不发消息、不抢占消息。"""
    try:
        if not host.has("http.outbound"):
            return _tool_error("本机器人未获批「访问外网」能力，视频解析不可用。")
        raw = str(arguments.get("url") or "").strip()
        if not raw:
            return _tool_error("缺少要解析的视频链接。")
        platform, url = common.normalize_url(raw)
        if not platform:
            return _tool_error("这条链接不是受支持的视频平台，无法解析。")

        quality = _choice(host, "video_quality", common.DEFAULT_QUALITY)
        cache_seconds = _seconds_config(
            host, "cache_seconds", DEFAULT_CACHE_SECONDS, MAX_CACHE_SECONDS
        )
        cache_key = f"{platform}:{url}:{quality}"
        media = _media_cache.get(cache_key) if cache_seconds else None
        if media is None:
            host.log(f"解析视频链接：{common.clip(url, 80)}")
            try:
                media = await asyncio.wait_for(
                    _dispatch_parse(host, platform, url, quality),
                    timeout=TOOL_PARSE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                return _tool_error("解析超时，请告诉用户暂时看不了，不要编造视频内容。")
            except VideoParseError as error:
                return _tool_error(f"解析失败：{error.message}")
            except PluginCallError as error:
                return _tool_error(f"出网请求被拒绝或失败：{error}")
            if not isinstance(media, dict):
                return _tool_error("解析器没有返回有效结果。")
            if cache_seconds:
                # 播放地址自带有效期，缓存时间由安装配置控制，过期后重新解析。
                _media_cache.put(cache_key, media, cache_seconds)
        return _tool_payload(media, url, host)
    except Exception as error:  # 工具异常只回一段可读文本，不中断对话循环
        return _tool_error(f"视频信息工具出错：{type(error).__name__}: {error}")
