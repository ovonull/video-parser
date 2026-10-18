"""
视频解析插件。

功能：
1. 群里或私聊里贴抖音、B站、小红书链接，机器人先回一条描述卡片（标题、作者、时长、
   互动数据、作品简介），紧接着把视频发出来；只有 QQ 拒收时才补一条无水印直链；
2. 抖音图文作品发原图，B站取 html5 单文件 mp4（自带音轨，不需要 ffmpeg 合流）；
3. 冷却、生效范围、是否直发视频、时长上限（0 为不限）、解析缓存都由安装配置控制。

约定：
- 出网一律走 host.http_request，域名受清单白名单约束；
- 解析失败回一条中文提示，不向宿主抛裸异常，避免整条事件判定为失败；
- 不直接调用 OneBot，发消息一律通过 host 完成。
"""

from __future__ import annotations

import time
from typing import Any

from app.services.plugin_sdk import PluginCallError, on_message

from vparse import bilibili, common, douyin, xiaohongshu
from vparse.cache import MediaCache
from vparse.common import VideoParseError


DEFAULT_TRIGGERS = "解析,视频解析"
# 0 表示不限时长：宿主给数字型配置项的默认值是 0，若把 0 当成「上限 0 秒」，
# 任何视频都会被判超限、只能发链接，所以这里必须把 0 解释成不设限。
DEFAULT_MAX_DURATION = 0
DEFAULT_MAX_IMAGES = 3
DEFAULT_COOLDOWN = 15
DEFAULT_CACHE_SECONDS = 600

MAX_TITLE_LENGTH = 80
# 描述正文的上限：QQ 单条文本能满足，但整段文案动辄上千字，截到可读范围即可。
MAX_DESC_LENGTH = 260
# 短于这个长度的「描述」基本都是平台里的占位符（如 -、www），没有展示价值。
MIN_DESC_LENGTH = 4
MAX_TRIGGER_COUNT = 10
MAX_DURATION_LIMIT = 3600
MAX_IMAGES_LIMIT = 9
MAX_COOLDOWN = 300
MAX_CACHE_SECONDS = 3600

# 会话级冷却与最近一次解析的链接，避免同一条链接被反复刷。
_last_reply: dict[str, tuple[float, str]] = {}

# 跨会话的解析结果缓存：同一条链接被不同的人反复粘贴时不再重复请求平台。
_media_cache = MediaCache(64)


def _text_config(host: Any, key: str, default: str) -> str:
    """读取文本型配置，缺失时返回默认值。"""
    value = host.config.get(key)
    text = str(value).strip() if value is not None else ""
    return text or default


def _choice(host: Any, key: str, default: str) -> str:
    """读取下拉型配置，空值按默认项处理。"""
    return _text_config(host, key, default)


def _int_config(
    host: Any,
    key: str,
    default: int,
    low: int,
    high: int,
    *,
    zero_as_default: bool = False,
) -> int:
    """
    读取数字型配置，非法或越界时回落到默认值。

    Args:
        host: 插件宿主视图。
        key: 配置键。
        default: 缺省值。
        low: 允许的最小值。
        high: 允许的最大值。
        zero_as_default: 为真时，0 与空值一样按「未填写」处理并取默认值。
            用于「0 没有实际意义」的配置项（例如图文张数），
            避免安装时默认写入的 0 把功能压到最小值。
    """
    raw = host.config.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    if zero_as_default and value <= 0:
        return default
    return max(low, min(value, high))


def _seconds_config(host: Any, key: str, default: int, high: int) -> int:
    """
    读取「秒」类配置。

    宿主给数字型配置项的默认值是 0，因此这里把 0 与空值都视为「未填写」取默认值，
    想显式关闭就填 -1。这样新装插件拿到的就是设计上的默认行为，而不是全部归零。
    """
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


def _message_text(event: dict[str, Any]) -> str:
    """从事件里取出纯文本，数组消息只拼接 text 段。"""
    message = event.get("message")
    if isinstance(message, str):
        text = message
    elif isinstance(message, list):
        parts: list[str] = []
        for segment in message:
            if isinstance(segment, dict) and segment.get("type") == "text":
                data = segment.get("data") or {}
                parts.append(str(data.get("text") or ""))
        text = "".join(parts)
    else:
        text = str(event.get("raw_message") or "")
    # NapCat 在 @ 之后可能插入特殊空格，统一成普通空格再处理。
    return text.replace("\u2005", " ").replace("\xa0", " ").strip()


def _parse_triggers(raw: str) -> list[str]:
    """把配置里的触发词拆开，兼容中英文逗号与分号。"""
    normalized = raw.replace("，", ",").replace("；", ",").replace(";", ",")
    words = [item.strip() for item in normalized.split(",") if item.strip()]
    return words[:MAX_TRIGGER_COUNT] or ["解析"]


def _chat_key(host: Any, event: dict[str, Any]) -> str:
    """会话标识：群聊按群号，私聊按用户号。"""
    chat_id = (
        event.get("group_id")
        if event.get("message_type") == "group"
        else event.get("user_id")
    )
    return f"{host.bot_qq}:{chat_id}"


def _duplicated(host: Any, event: dict[str, Any], url: str, seconds: int) -> bool:
    """同一会话在冷却时间内重复贴同一条链接时直接忽略。"""
    if seconds <= 0:
        return False
    key = _chat_key(host, event)
    now = time.time()
    last = _last_reply.get(key)
    if last and now - last[0] < seconds and last[1] == url:
        return True
    _last_reply[key] = (now, url)
    return False


def _describe(media: dict[str, Any], note: str = "") -> str:
    """
    拼描述卡片：标题、作者与时长等元信息、互动数据、正文简介。

    抖音这类平台没有独立标题，标题就是文案首行，正文里会把这一行去掉再展示，
    避免同一句话在卡片上出现两次。
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

    # 先按「标题 + 正文」的完整长度取，够长才能判断正文是否以标题开头。
    body = common.block(media.get("desc"), MAX_DESC_LENGTH + MAX_TITLE_LENGTH)
    if title and body.startswith(title):
        body = body[len(title) :].strip()
    body = common.block(body, MAX_DESC_LENGTH)
    # 不少老视频的简介就是「-」「www」「。。。」这类占位符，别当成正文发出去。
    if len(body) < MIN_DESC_LENGTH or len(set(body)) == 1:
        body = ""
    if body:
        lines.append("")
        lines.append(body)

    if note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


def _link_card(media: dict[str, Any], note: str = "") -> str:
    """拼纯文本卡片：描述信息 + 无水印直链。"""
    lines = [_describe(media, note)]
    play_url = media.get("play_url") or ""
    if play_url:
        lines.append(f"无水印直链：{play_url}")
    elif media.get("images"):
        lines.append(f"共 {len(media['images'])} 张原图")
    return "\n".join(lines)


def _as_text(value: str) -> dict[str, Any]:
    """构造文本消息段。"""
    return {"type": "text", "data": {"text": value}}


async def _reply(host: Any, event: dict[str, Any], message: Any) -> None:
    """按会话类型把消息发回去。"""
    if event.get("message_type") == "group":
        await host.send_group_msg(event.get("group_id"), message)
    else:
        await host.send_private_msg(event.get("user_id"), message)


async def _safe_reply(host: Any, event: dict[str, Any], message: Any, label: str) -> bool:
    """发一条消息，被拒绝时只记日志不抛异常，返回是否成功。"""
    try:
        await _reply(host, event, message)
        return True
    except PluginCallError as error:
        host.log(f"{label}失败：{error}", "warning")
        return False


def _over_duration(media: dict[str, Any], config: dict[str, int]) -> bool:
    """判断视频是否超过直发上限，上限为 0 表示不限时长。"""
    limit = config["max_duration"]
    duration = media.get("duration") or 0
    return bool(limit and duration and duration > limit)


async def _send_video(host: Any, event: dict[str, Any], media: dict[str, Any]) -> bool:
    """
    单独发一条视频消息，失败时返回 False 由调用方降级成直链。

    QQ 侧对视频有转码和体积限制，超限或格式不支持会让动作直接失败，
    所以这里单独捕获异常，不影响已经发出去的描述。
    """
    segment = {"type": "video", "data": {"file": media["play_url"]}}
    if not await _safe_reply(host, event, [segment], "发送视频"):
        return False
    host.log(f"已发送视频：{common.clip(media.get('title'), 40)}")
    return True


async def _send_images(
    host: Any, event: dict[str, Any], media: dict[str, Any], config: dict[str, int]
) -> None:
    """图文作品发前几张原图，再补一条带描述的说明。"""
    images = [item for item in media.get("images") or [] if item][: config["max_images"]]
    if not images:
        await _reply(host, event, _link_card(media))
        return
    segments: list[dict[str, Any]] = [
        {"type": "image", "data": {"file": url}} for url in images
    ]
    total = len(media.get("images") or [])
    note = f"已发 {len(images)}/{total} 张原图" if total > len(images) else ""
    segments.append(_as_text(_describe(media, note)))
    await _reply(host, event, segments)


async def _dispatch_parse(
    host: Any, platform: str, url: str
) -> dict[str, Any]:
    """按平台调用对应解析器。"""
    if platform == "douyin":
        return await douyin.parse(url, _text_config(host, "douyin_cookie", ""), host)
    if platform == "bilibili":
        return await bilibili.parse(
            url, _text_config(host, "bilibili_cookie", ""), host
        )
    if platform == "xiaohongshu":
        return await xiaohongshu.parse(
            url, _text_config(host, "xhs_cookie", ""), host
        )
    raise VideoParseError("暂不支持这个平台", "目前支持抖音、B 站与小红书")


@on_message
async def handle_video(event: dict[str, Any], host: Any) -> None:
    """收到消息时判断是否包含可解析的视频链接，命中则回视频或链接。"""
    if event.get("post_type") != "message":
        return
    if str(event.get("user_id")) == str(event.get("self_id")):
        # 机器人自己发的消息不处理，避免自我循环。
        return

    scope = _choice(host, "reply_scope", "群聊与私聊")
    is_group = event.get("message_type") == "group"
    if (scope == "仅群聊" and not is_group) or (scope == "仅私聊" and is_group):
        return

    text = _message_text(event)
    if not text:
        return

    platform, url = common.normalize_url(text)
    auto = _choice(host, "auto_parse", "粘贴链接即解析") == "粘贴链接即解析"
    triggers = _parse_triggers(_text_config(host, "trigger_words", DEFAULT_TRIGGERS))
    if not auto:
        # 指令模式下必须**以触发词开头**才算命令，避免普通聊天里出现「解析」就被误触发。
        head = text.lstrip("。.,!?！？~ 　")
        if not any(head.startswith(word) for word in triggers):
            return
        if not platform:
            await _reply(
                host,
                event,
                f"用法：{triggers[0]} + 视频链接，支持抖音、B 站与小红书。",
            )
            return
    if not platform:
        return

    if not host.has("msg.send"):
        host.log("缺少 msg.send 能力，无法回复", "error")
        return
    if not host.has("http.outbound"):
        host.log("缺少 http.outbound 能力，无法解析视频", "error")
        await _reply(host, event, "本机器人未获批「访问外网」能力，视频解析不可用。")
        return

    cooldown = _seconds_config(host, "cooldown_seconds", DEFAULT_COOLDOWN, MAX_COOLDOWN)
    if _duplicated(host, event, url, cooldown):
        host.log(f"同一链接在冷却中，忽略：{url}")
        return

    config = {
        "send_video": _choice(host, "send_video", "发送视频"),
        "max_duration": _int_config(
            host, "max_duration", DEFAULT_MAX_DURATION, 0, MAX_DURATION_LIMIT
        ),
        "max_images": _int_config(
            host,
            "max_images",
            DEFAULT_MAX_IMAGES,
            1,
            MAX_IMAGES_LIMIT,
            zero_as_default=True,
        ),
    }

    cache_seconds = _seconds_config(
        host, "cache_seconds", DEFAULT_CACHE_SECONDS, MAX_CACHE_SECONDS
    )
    cache_key = f"{platform}:{url}"
    media = _media_cache.get(cache_key) if cache_seconds else None
    if media is not None:
        host.log(f"命中解析缓存：{common.clip(url, 80)}")
    else:
        host.log(f"开始解析 {platform}：{common.clip(url, 120)}")
        try:
            media = await _dispatch_parse(host, platform, url)
        except VideoParseError as error:
            host.log(f"解析失败：{error.message}", "warning")
            await _reply(host, event, f"解析失败：{error.message}" + (f"\n{error.hint}" if error.hint else ""))
            return
        except PluginCallError as error:
            host.log(f"出网请求被拒绝或失败：{error}", "error")
            await _reply(host, event, "解析服务暂时不可用，请稍后再试。")
            return
        except Exception as error:  # 插件内部兜底，不让异常冒到宿主
            host.log(f"解析出现未预期错误：{type(error).__name__}: {error}", "error")
            await _reply(host, event, "解析出错了，请稍后再试。")
            return
        # 播放地址自带有效期，缓存时间由安装配置控制，过期后重新解析。
        _media_cache.put(cache_key, media, cache_seconds)

    try:
        if media.get("kind") == "images":
            await _send_images(host, event, media, config)
            return

        note = ""
        if _over_duration(media, config):
            note = (
                f"视频时长 {common.format_duration(media.get('duration'))} 超过上限 "
                f"{common.format_duration(config['max_duration'])}，请点链接观看"
            )
        # 只发链接模式，或视频超时长上限：一条消息说完，卡片里带描述与直链。
        if config["send_video"] != "发送视频" or note:
            await _reply(host, event, _link_card(media, note))
            return

        # 描述和视频分两条发：混在同一个消息段数组里时，
        # 部分 NapCat 版本会丢掉文本段，表现就是「只有视频、没有说明」。
        await _safe_reply(host, event, _describe(media), "发送视频说明")
        if await _send_video(host, event, media):
            return
        # 视频被 QQ 拒收，补一条直链；上面已经发过标题与描述，这里不再重复。
        play_url = media.get("play_url") or ""
        fallback = f"无水印直链：{play_url}" if play_url else "视频发送失败，请稍后再试。"
        await _safe_reply(host, event, fallback, "补发直链")
    except PluginCallError as error:
        host.log(f"发送消息失败：{error}", "error")
