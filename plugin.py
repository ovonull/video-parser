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
# 作品原文要完整给到模型：多数平台文案在几百字内，留足余量，别把后半段切掉。
# 宿主 result_limit 会兜底裁剪整份工具结果，card 排在附件之前不会被先挤掉。
MAX_DESC_LENGTH = 1200
MAX_DURATION_LIMIT = 3600
# 图文作品一次最多排队几张原图：太多会让补发拖得很久，也会挤占群消息。
MAX_IMAGE_ATTACHMENTS = 9
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


def _switch_config(host: Any, key: str, default: bool) -> bool:
    """读取开关型配置；面板存的是布尔值，也兼容手写的 true/on/1。"""
    raw = host.config.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


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


# 抖音单视频体积预算默认值（MB）。宿主转存上限默认 100MB，这里留出余量，
# 让「无水印 + 发得出去」同时成立：短视频照发高码率档，长视频自动降到有余量
# 的档；估算有偏差时也不会顶到上限。用户可调大此值配合宿主上限发更高清。
DEFAULT_VIDEO_BUDGET_MB = 90
MAX_VIDEO_BUDGET_MB = 4096


def _video_budget_bytes(host: Any) -> int:
    """读取单视频体积预算并换算成字节；0 或空表示用默认值，-1 表示不限。"""
    raw = host.config.get("video_max_mb")
    if raw is None or str(raw).strip() == "":
        mb = DEFAULT_VIDEO_BUDGET_MB
    else:
        try:
            mb = int(float(str(raw).strip()))
        except (TypeError, ValueError):
            mb = DEFAULT_VIDEO_BUDGET_MB
        if mb == -1:
            return 0
        if mb < 0:
            mb = DEFAULT_VIDEO_BUDGET_MB
        mb = min(mb, MAX_VIDEO_BUDGET_MB)
    return mb * 1024 * 1024


def _describe(media: dict[str, Any]) -> str:
    """拼描述卡片：标题、元信息、互动数据，以及发布者写的完整原文。

    抖音这类平台没有独立标题，标题就是文案首行，所以原文原样再给一遍，
    不再从正文里剥掉首行——剥掉就等于把那句原话藏起来了。短文案同样保留，
    只挡掉纯重复字符的脏数据。
    """
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

    body = common.block(media.get("desc"), MAX_DESC_LENGTH)
    if body and len(set(body)) > 1:
        lines.append("")
        lines.append("作品原文：")
        lines.append(body)
    return "\n".join(lines)


async def _dispatch_parse(
    host: Any, platform: str, url: str, quality: str
) -> dict[str, Any]:
    """按平台调用对应解析器。"""
    if platform == "douyin":
        return await douyin.parse(
            url,
            _text_config(host, "douyin_cookie", ""),
            host,
            quality,
            _video_budget_bytes(host),
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


def _caption_for(host: Any, media: dict[str, Any]) -> str:
    """拼随视频发送的文案。

    开关「是否发送抖音文案」打开时发一张卡片：标题、作者、时长、画质、互动
    数据排成两行，跟群里常见的那种「【抖音】标题 @作者 · 时长 · 画质 · 点赞」
    一致；关掉就只发媒体，不带任何文字。

    图文作品（kind=images）不适用：图片本身带说明，发 caption 反而重复。
    """
    if str(media.get("kind") or "") == "images":
        return ""
    if not _switch_config(host, "send_caption", True):
        return ""

    title = common.flatten(media.get("title"))
    title_text = common.clip(title, MAX_TITLE_LENGTH) or "未命名作品"
    platform = str(media.get("platform_name") or "")
    head = f"【{platform}】{title_text}" if platform else title_text
    parts: list[str] = []
    if media.get("author"):
        parts.append(f"@{media['author']}")
    duration = common.format_duration(media.get("duration"))
    if duration:
        parts.append(f"时长 {duration}")
    if media.get("quality"):
        parts.append(str(media["quality"]))
    for item in media.get("stats") or []:
        if item:
            parts.append(str(item))
    return "\n".join(item for item in [head, " · ".join(parts)] if item)


def _attachments(
    host: Any, media: dict[str, Any]
) -> tuple[list[dict[str, Any]], str]:
    """把作品里的视频/图片转成宿主待发队列条目。

    返回 ``(条目, 跳过原因)``：有媒体时原因为空串；跳过时原因是一句能直接看懂
    的中文，会随工具结果一起回给模型，也会出现在运行记录的调用步骤里——
    排查「怎么只回了一句话、视频没到」时不用再翻服务端日志。

    宿主会在 AI 正文发出之后逐条补发这些媒体；发不出来就只记日志，不再补
    直链，也不会改变已经发出的正文。

    「回复样式」两档：
    - 发送视频：视频地址交给宿主上传/回源，群里是可播放的视频；
    - 发送无水印直链：不传文件，只把平台的去水印地址当文本发出去。

    文案挂在视频条目上（``caption``），由宿主决定怎么发：
    - 合并发送打开：文案和视频装进一张聊天记录卡片，群里是「查看 2 条转发消息」；
      接入端不支持时宿主会自动退回两条；
    - 合并发送关闭：文案一条、视频一条，挨着发。
    直链模式本身就是文本，文案和链接走同一条。
    """
    items: list[dict[str, Any]] = []
    caption = _caption_for(host, media)
    if str(media.get("kind") or "") == "images":
        for url in (media.get("images") or [])[:MAX_IMAGE_ATTACHMENTS]:
            if url:
                items.append({"type": "image", "file": str(url)})
        live = common.text_of(media.get("live_video_url"))
        if live:
            video_item: dict[str, Any] = {"type": "video", "file": live}
            if caption:
                video_item["caption"] = caption
            items.append(video_item)
        return items, "" if items else "图文作品没有取到图片地址"

    limit = _int_config(host, "max_duration", 0, 0, MAX_DURATION_LIMIT)
    duration = media.get("duration") or 0
    if limit and duration and duration > limit:
        reason = f"时长 {duration} 秒超过上限 {limit} 秒"
        host.log(f"不发送视频：{reason}")
        return items, reason
    play_url = str(media.get("play_url") or "")
    if not play_url:
        host.log("不发送视频：没有解析到播放地址")
        return items, "没有解析到播放地址"

    style = _choice(host, "reply_style", "发送视频")
    host.log(
        f"随回复发送媒体：样式={style} 文案={'有' if caption else '无'} "
        f"合并={'是' if _switch_config(host, 'merge_caption', False) else '否'} "
        f"时长={duration or '未知'} 秒 上限={limit or '不限'}"
    )
    if "直链" in style:
        # 详情接口的码率地址带 Referer 防盗链，群里裸链点开是 403。抖音会给
        # share_url（302 播放端点的原片地址：无水印、画质最高、点开即播），优先用它；
        # 其它平台或拿不到 vid 时退回 play_url。
        link = str(media.get("share_url") or "") or play_url
        # 直链本身就是文本，文案和链接放同一条，不会因为段结构被拒。
        text = f"{caption}\n{link}" if caption else link
        items.append({"type": "text", "text": text})
        return items, ""

    # 抖音有 302 原画端点（share_url）：发布者原片，无水印、画质最高。发送视频
    # 也优先发原画，码率档降为备用——原画点开即播不受大小限制，但「发送视频」
    # 是宿主先转存再发，1GB 原画下载慢，超时/失败时宿主会接着试下面的码率档，
    # 保证视频还能发出去，而不是只剩一句文字。其它平台没有这个端点，仍用 play_url。
    share = str(media.get("share_url") or "")
    primary = share or play_url
    item: dict[str, Any] = {"type": "video", "file": primary}
    if caption:
        item["caption"] = caption
        if _switch_config(host, "merge_caption", False):
            # 交给宿主装进合并转发卡片；接入端不认视频节点时宿主会自己拆开。
            item["forward"] = True
    # 备用地址：原画失败时退码率档；码率档自身还有同档 CDN 域名镜像可逐个重试。
    # 主地址被防盗链拒绝（403/fetch failed）或下载超时，宿主会按顺序转存重试。
    fallbacks: list[str] = []
    if share:
        # 原画为主时，码率档整档（含镜像）都是备用。
        fallbacks.append(play_url)
    for candidate in (
        *(media.get("play_url_mirrors") or []),
        media.get("play_url_mirror"),
    ):
        text = str(candidate or "")
        if text and text != primary and text not in fallbacks:
            fallbacks.append(text)
    if fallbacks:
        item["fallbacks"] = fallbacks
    items.append(item)
    return items, ""


def _tool_error(message: str) -> dict[str, Any]:
    """工具失败时回一段模型读得懂的中文说明，不把异常抛给对话循环。"""
    return {"ok": False, "error": message}


def _hint(has_media: bool) -> str:
    """给模型的提示：这一轮有没有排队媒体，说法不一样。

    没排上媒体还按「已发送」的口吻回复，群里就会出现「视频发你了」但什么都
    没到的情况，所以要把结论直接写进提示里。
    """
    head = "作品信息以 card 为准，card 里的「作品原文」是发布者写的完整文案；"
    tail = (
        "可以引用原文里的原话，但不要贴链接、不要整段照抄卡片、"
        "也不要编造工具没给到的信息。"
    )
    if has_media:
        return (
            head
            + "视频或图片已排队，会由系统紧跟着你的回复发出。请按你的人设自然接话，"
            + tail
        )
    return (
        head
        + "这次没有随回复发送视频或图片，不要说「已发送」「看视频」这类话，只谈作品内容。"
        + tail
    )


def _tool_payload(media: dict[str, Any], url: str, host: Any) -> dict[str, Any]:
    """把作品数据收敛成模型好用的结构，并挂上待发媒体。

    作品文案只通过 ``card`` 给一次：``card`` 里既有标题、作者、时长、互动数据，
    也有「作品原文」整段（含被当作标题的那一行），再单独塞一份 ``desc`` 只是
    重复占上下文。模型能读到原话，但 ``hint`` 仍要求它不要整段照抄。
    """
    # 放在最后：返回给模型时可能被长度裁剪，前面的信息不能先被挤掉。
    # 宿主在裁剪前就取走这一项登记待发媒体，模型不需要看到这些地址。
    attachments, skipped = _attachments(host, media)
    payload: dict[str, Any] = {
        "ok": True,
        "platform": str(media.get("platform_name") or ""),
        "url": url,
        "title": common.clip(media.get("title"), MAX_TITLE_LENGTH),
        "author": str(media.get("author") or ""),
        "duration": common.format_duration(media.get("duration")),
        "stats": [str(item) for item in media.get("stats") or [] if item],
        "kind": str(media.get("kind") or ""),
        "image_count": len(media.get("images") or []),
        "card": _describe(media),
        "hint": _hint(bool(attachments)),
    }
    if attachments:
        payload["attachments"] = attachments
    elif skipped:
        # 顺手回给模型和运行记录：没发视频是因为什么，一眼能看懂。
        payload["media_skipped"] = skipped
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
        # 预算参与缓存键：改了体积上限后，同一链接要重新按新预算选档。
        budget = _video_budget_bytes(host)
        cache_key = f"{platform}:{url}:{quality}:{budget}"
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
