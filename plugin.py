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

import asyncio
import time
from typing import Any

from app.services.plugin_sdk import PluginCallError, on_message

from vparse import bilibili, common, douyin, jimengai, pipigx, xiaohongshu
from vparse import doubao
from vparse.cache import MediaCache
from vparse.common import VideoParseError


DEFAULT_TRIGGERS = "解析,视频解析"
# 0 表示不限时长：宿主给数字型配置项的默认值是 0，若把 0 当成「上限 0 秒」，
# 任何视频都会被判超限、只能发链接，所以这里必须把 0 解释成不设限。
DEFAULT_MAX_DURATION = 0
DEFAULT_COOLDOWN = 15
DEFAULT_CACHE_SECONDS = 600

MAX_TITLE_LENGTH = 80
# 描述正文的上限：QQ 单条文本能满足，但整段文案动辄上千字，截到可读范围即可。
MAX_DESC_LENGTH = 260
# 短于这个长度的「描述」基本都是平台里的占位符（如 -、www），没有展示价值。
MIN_DESC_LENGTH = 4
MAX_TRIGGER_COUNT = 10
MAX_DURATION_LIMIT = 3600
# NapCat 对单条消息里的 image 段数量和总大小都比较敏感，图文作品分批发送。
# 3 张一批兼顾消息数量与发送稳定性；单批失败时还会逐张降级重试。
IMAGE_BATCH_SIZE = 3
MAX_COOLDOWN = 300
MAX_CACHE_SECONDS = 3600

# 会话级冷却与最近一次解析的链接，避免同一条链接被反复刷。
_last_reply: dict[str, tuple[float, str]] = {}

# 跨会话的解析结果缓存：同一条链接被不同的人反复粘贴时不再重复请求平台。
_media_cache = MediaCache(64)

# 后台任务集合：解析、发送这些慢动作放进来跑。
#
# 宿主对单个事件的处理有时间上限（默认 10 秒），超时就重启插件子进程。
# 而这些动作都很慢：解析要等平台接口（分享页还会重试三次，每次 8 秒超时），
# 发送要等 NapCat 把文件下载、转发、上传完。放在事件里等就会把事件拖到超时，
# 子进程被重启后，已经发出的内容会残缺（多图作品表现为「10 张只到 9 张」），
# 连最后那条描述卡片也一起丢掉。所以这里让事件先结束，慢动作在后台继续。
_background_tasks: set[asyncio.Task] = set()

# 后台流水线之间的串行锁：同一时刻只跑一条「解析 + 发送」，
# 免得两条链接的图文交叉着发，读起来乱。
# 延迟创建，兼容不同 Python 版本对锁的绑定时机。
_pipeline_lock: asyncio.Lock | None = None


def _pipeline_guard() -> asyncio.Lock:
    """取后台流水线的串行锁。"""
    global _pipeline_lock
    if _pipeline_lock is None:
        _pipeline_lock = asyncio.Lock()
    return _pipeline_lock


def _spawn(coro: Any) -> None:
    """把协程丢到后台执行，并持有引用防止被垃圾回收。"""
    try:
        task = asyncio.ensure_future(coro)
    except RuntimeError:  # 没有事件循环时退化为不执行
        coro.close()
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


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
    if await _safe_reply(host, event, [segment], "发送视频"):
        host.log(f"已发送视频：{common.clip(media.get('title'), 40)}")
        return True
    # 原地址被拒多半是 CDN 区域节点的防盗链，换中机房域名再试一次。
    mirror = media.get("play_url_mirror") or ""
    if mirror and mirror != media.get("play_url"):
        host.log("视频被拒，换中机房 CDN 地址重试", "warning")
        retry = {"type": "video", "data": {"file": mirror}}
        if await _safe_reply(host, event, [retry], "发送视频（中机房重试）"):
            host.log(f"已发送视频：{common.clip(media.get('title'), 40)}")
            return True
    return False


async def _send_images(
    host: Any, event: dict[str, Any], media: dict[str, Any]
) -> None:
    """
    图文作品按批发送全部原图，再补一条描述卡片。

    QQ/NapCat 对单条消息里的图片段数量有限制，3 张以上混在一起容易丢段或触发限速。
    这里把图片切成 3 张一条，每条只发图片段；描述另起一条文字消息发。
    不限制图片张数：作品里有多少张就发多少张。
    单批发送失败时会自动按单张降级重试，尽量不让前面已发的丢失。
    """
    all_images = [item for item in media.get("images") or [] if item]
    total = len(all_images)

    # 没有任何图片时直接回文字卡片，避免「已发 0/0 张原图」这种空话。
    if not total:
        await _reply(host, event, _link_card(media))
        return

    # 按 IMAGE_BATCH_SIZE 分批；最后一批可能少于 3 张，按真实长度走。
    sent = 0
    for index in range(0, total, IMAGE_BATCH_SIZE):
        batch = all_images[index : index + IMAGE_BATCH_SIZE]
        # 单批失败时按单张重试：丢一张比丢整批更能保住已发送的内容。
        if not await _send_image_batch(host, event, batch):
            recovered = 0
            for url in batch:
                if await _send_image_batch(host, event, [url]):
                    recovered += 1
            sent += recovered
            host.log(
                f"图文第 {index // IMAGE_BATCH_SIZE + 1} 批降级重试："
                f"原 {len(batch)} 张，发成 {recovered} 张",
                "warning",
            )
        else:
            sent += len(batch)

    # 张数不设上限，但个别图仍可能被 QQ 拒收；失败时提示点链接看全。
    note = ""
    if sent < total:
        note = (
            f"已发 {sent}/{total} 张原图，"
            f"其余 {total - sent} 张请点链接查看：{media.get('source_url') or ''}"
        ).strip()
    await _reply(host, event, _describe(media, note))


async def _send_image_batch(
    host: Any, event: dict[str, Any], urls: list[str]
) -> bool:
    """
    单独发一批图片，被拒绝时只记日志，返回是否整批成功。

    图片段单独成消息，不与文本段混排：NapCat 在多 image 段 + 文本段一起发时，
    部分版本会丢掉文本段，这与「描述 + 视频」拆两条发是同一个原因。
    """
    if not urls:
        return True
    segments: list[dict[str, Any]] = [
        {"type": "image", "data": {"file": url}} for url in urls
    ]
    return await _safe_reply(host, event, segments, "发送图文")


async def _deliver_video(host: Any, event: dict[str, Any], media: dict[str, Any]) -> None:
    """
    后台发送视频；被 QQ 拒收时补一条直链。

    单独拆出来是为了让事件能尽快结束：发视频要等 NapCat 完成下载与上传，
    回执经常超过宿主给事件的时间上限。
    """
    try:
        if await _send_video(host, event, media):
            return
        # 视频被 QQ 拒收，补一条直链；描述已经发过了，这里不再重复。
        play_url = media.get("play_url") or ""
        fallback = (
            f"无水印直链：{play_url}" if play_url else "视频发送失败，请稍后再试。"
        )
        await _safe_reply(host, event, fallback, "补发直链")
    except PluginCallError as error:
        host.log(f"后台发送视频失败：{error}", "warning")


async def _resolve_media(
    host: Any, event: dict[str, Any], platform: str, url: str, config: dict[str, Any]
) -> dict[str, Any] | None:
    """
    取作品数据：命中缓存直接用，否则解析。

    失败时把提示发给用户并返回 None，不往上抛异常。
    """
    cache_seconds = _seconds_config(
        host, "cache_seconds", DEFAULT_CACHE_SECONDS, MAX_CACHE_SECONDS
    )
    # 清晰度会直接改变 play_url（抖音多档码率、B 站 qn、小红书多条码流），
    # 缓存键必须带上它，否则用户换了清晰度后，缓存里旧的播放地址还会被发出去。
    cache_key = f"{platform}:{url}:{config['video_quality']}"
    media = _media_cache.get(cache_key) if cache_seconds else None
    if media is not None:
        host.log(f"命中解析缓存：{common.clip(url, 80)}")
        return media

    host.log(f"开始解析 {platform}：{common.clip(url, 120)}")
    try:
        media = await _dispatch_parse(host, platform, url, config["video_quality"])
    except VideoParseError as error:
        host.log(f"解析失败：{error.message}", "warning")
        await _reply(
            host,
            event,
            f"解析失败：{error.message}" + (f"\n{error.hint}" if error.hint else ""),
        )
        return None
    except PluginCallError as error:
        host.log(f"出网请求被拒绝或失败：{error}", "error")
        await _reply(host, event, "解析服务暂时不可用，请稍后再试。")
        return None
    except Exception as error:  # 插件内部兜底，不让异常冒到宿主
        host.log(f"解析出现未预期错误：{type(error).__name__}: {error}", "error")
        await _reply(host, event, "解析出错了，请稍后再试。")
        return None
    # 播放地址自带有效期，缓存时间由安装配置控制，过期后重新解析。
    _media_cache.put(cache_key, media, cache_seconds)
    return media


async def _deliver_media(
    host: Any, event: dict[str, Any], media: dict[str, Any], config: dict[str, Any]
) -> None:
    """按作品类型把内容发回会话。"""
    if media.get("kind") == "images":
        await _send_images(host, event, media)
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
    await _deliver_video(host, event, media)


async def _run_pipeline(
    host: Any, event: dict[str, Any], platform: str, url: str, config: dict[str, Any]
) -> None:
    """
    后台跑完一整条流水线：解析 → 发送。

    整条链路都放后台，是因为两段都可能很慢：解析要等平台接口
    （分享页还会重试三次，每次 8 秒超时），发送要等 NapCat 转发文件。
    只要有一段留在事件里，事件就可能被拖到宿主的时间上限（默认 10 秒），
    宿主随即重启插件子进程——正在发的图会连同最后那条描述卡片一起丢掉，
    表现就是「10 张图只到了 9 张」。

    用一把锁把多条流水线串起来：同一个机器人同时收到两条链接时，
    后一条要等前一条发完，消息顺序不会乱。
    """
    try:
        async with _pipeline_guard():
            media = await _resolve_media(host, event, platform, url, config)
            if media is None:
                return
            await _deliver_media(host, event, media, config)
    except PluginCallError as error:
        host.log(f"发送消息失败：{error}", "error")
    except Exception as error:  # 后台任务异常只记日志，不能影响宿主
        host.log(f"流水线出现未预期错误：{type(error).__name__}: {error}", "error")


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
    raise VideoParseError(
        "暂不支持这个平台",
        "目前支持抖音、B 站、小红书、皮皮搞笑、即梦 AI 与豆包 AI",
    )


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
        "video_quality": _choice(host, "video_quality", common.DEFAULT_QUALITY),
    }

    # 整条流水线（解析 + 发送）都放到后台，事件立刻结束。
    # 事件里等解析或等发送，都会撞上宿主的事件时间上限（默认 10 秒），
    # 宿主一超时就重启插件子进程，正在发的东西会丢在半路。
    _spawn(_run_pipeline(host, event, platform, url, config))
