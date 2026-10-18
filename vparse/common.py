"""
公共工具：链接识别、出网封装、文本格式化。

约定：
- 所有出网都走 host.http_request，域名受清单白名单约束；
- 宿主出网客户端**不自动跟随跳转**，短链由本模块手工跟随并校验跳转目标；
- 解析失败统一抛 VideoParseError，带上给用户看的原因与建议，不向宿主抛裸异常。
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse


# 移动端 UA：抖音分享页只有移动端 UA 才会返回内嵌数据。
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
# 桌面端 UA：B站接口按桌面端返回 JSON。
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 短链跟随的最大跳转次数，避免被重定向环拖住。
MAX_REDIRECTS = 2

# 平台识别规则：先匹配到哪个平台就交给对应解析器。
PLATFORM_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "douyin",
        re.compile(
            r"https?://(?:v\.douyin\.com/[A-Za-z0-9_-]+"
            r"|(?:www\.)?douyin\.com/(?:video|note|share/video)/\d{6,25}"
            r"|(?:www\.)?douyin\.com/\S*?[?&](?:modal_id|aweme_id)=\d{6,25}"
            r"|(?:www\.)?iesdouyin\.com/share/(?:video|note)/\d{6,25})",
            re.IGNORECASE,
        ),
    ),
    (
        "bilibili",
        re.compile(
            r"https?://(?:b23\.tv/[A-Za-z0-9]+"
            r"|(?:www\.|m\.)?bilibili\.com/video/(?:BV[0-9A-Za-z]{10}|av\d+)"
            r"|(?:www\.)?bilibili\.com/bangumi/play/\S+)",
            re.IGNORECASE,
        ),
    ),
    (
        "xiaohongshu",
        re.compile(
            r"https?://(?:xhslink\.com/[A-Za-z0-9/_-]+"
            r"|(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/[0-9a-f]{24})",
            re.IGNORECASE,
        ),
    ),
)

# 消息里没有 http 前缀时补全，方便用户直接粘 v.douyin.com/xxx 这种半截链接。
BARE_LINK = re.compile(
    r"(?<![A-Za-z0-9.])((?:v\.douyin\.com|b23\.tv|xhslink\.com|"
    r"(?:www\.|m\.)?douyin\.com|(?:www\.)?iesdouyin\.com|"
    r"(?:www\.|m\.)?bilibili\.com|(?:www\.)?xiaohongshu\.com)/[^\s\u4e00-\u9fff]+)",
    re.IGNORECASE,
)


class VideoParseError(Exception):
    """解析失败，message 给用户看，hint 是可选的处理建议。"""

    def __init__(self, message: str, hint: str = "") -> None:
        """记录原因与建议。"""
        super().__init__(message)
        self.message = message
        self.hint = hint


def normalize_url(text: str) -> tuple[str, str]:
    """
    从一段文本里找出第一个受支持的视频链接。

    Args:
        text: 用户消息的纯文本。
    Returns:
        (平台, 链接)；没找到时返回 ("", "")。
    """
    content = str(text or "")
    if not content:
        return "", ""
    for platform, pattern in PLATFORM_RULES:
        match = pattern.search(content)
        if match:
            return platform, match.group(0)
    bare = BARE_LINK.search(content)
    if bare:
        candidate = "https://" + bare.group(1).rstrip("，。,.；;)）")
        for platform, pattern in PLATFORM_RULES:
            if pattern.search(candidate):
                return platform, candidate
    return "", ""


def host_of(url: str) -> str:
    """取链接的域名（小写），解析失败时返回空字符串。"""
    try:
        return (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def status_of(result: dict[str, Any]) -> int:
    """从出网结果里读 HTTP 状态码。"""
    try:
        return int(result.get("status") or 0)
    except (TypeError, ValueError):
        return 0


def json_of(result: dict[str, Any]) -> dict[str, Any]:
    """从出网结果里取 JSON，取不到时回退到解析 text。"""
    payload = result.get("json")
    if isinstance(payload, dict):
        return payload
    text = str(result.get("text") or "")
    if not text:
        return {}
    try:
        loaded = json.loads(text)
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def headers_of(result: dict[str, Any]) -> dict[str, str]:
    """取响应头，统一转成小写键，兼容不同写法。"""
    raw = result.get("headers")
    if not isinstance(raw, dict):
        return {}
    return {str(key).lower(): str(value) for key, value in raw.items()}


async def follow_short_link(
    host: Any, url: str, *, headers: dict[str, str], timeout: float = 8.0
) -> str:
    """
    手工跟随短链跳转，返回最终地址。

    宿主出网客户端不自动跟随跳转，这里逐跳读取 Location，
    同时校验目标域名，避免被跳到白名单之外的站点。

    Args:
        host: 插件宿主视图。
        url: 短链地址。
        headers: 请求头。
        timeout: 单跳超时秒数。
    Returns:
        跳转后的地址；没有跳转时返回原地址。
    """
    current = str(url or "")
    for _ in range(MAX_REDIRECTS):
        result = await host.http_request(
            "GET", current, headers=headers, timeout=timeout
        )
        status = status_of(result)
        if status not in (301, 302, 303, 307, 308):
            return current
        target = headers_of(result).get("location") or ""
        if not target:
            return current
        if target.startswith("/"):
            parsed = urlparse(current)
            target = f"{parsed.scheme}://{parsed.netloc}{target}"
        if not target.startswith(("http://", "https://")):
            return current
        current = target
    return current


def text_of(value: Any) -> str:
    """把任意值转成去掉首尾空白的字符串。"""
    return "" if value is None else str(value).strip()


def first_url(node: Any) -> str:
    """从 {url_list: [...]} 这类结构里取第一个地址。"""
    if not isinstance(node, dict):
        return ""
    urls = node.get("url_list") or []
    if isinstance(urls, list):
        for item in urls:
            text = text_of(item)
            if text:
                return text
    return ""


def https_url(url: str) -> str:
    """把 http 图片地址升级成 https，避免部分客户端拒绝加载。"""
    text = text_of(url)
    if text.startswith("http://"):
        return "https://" + text[len("http://") :]
    return text


def format_duration(seconds: Any) -> str:
    """把秒数格式化成 03:25 或 1:02:03，取值异常时返回空字符串。"""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, remain = divmod(total, 3600)
    minutes, secs = divmod(remain, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def flatten(text: Any) -> str:
    """把任意文本压成单行，连续空白折成一个空格。"""
    return " ".join(str(text or "").split())


def block(text: Any, limit: int = 0) -> str:
    """
    整理多行正文：统一换行符、去掉行内多余空白与空行。

    Args:
        text: 原始正文，可能来自平台接口，含 \\r\\n 或大量空行。
        limit: 大于 0 时按长度截断并补省略号。
    Returns:
        整理后的正文；没有内容时返回空字符串。
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in raw.split("\n")]
    value = "\n".join(line for line in lines if line)
    if limit <= 0 or len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip() + "…"


def clip(text: Any, limit: int) -> str:
    """把文本压成单行后截断，保留可读性。"""
    value = flatten(text)
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)] + "…"


def format_count(value: Any) -> str:
    """把播放、点赞等计数格式化成 1.2万 / 3.4亿，取值异常时返回空字符串。"""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    if number >= 100_000_000:
        scaled = f"{number / 100_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{scaled}亿"
    if number >= 10_000:
        scaled = f"{number / 10_000:.1f}".rstrip("0").rstrip(".")
        return f"{scaled}万"
    return str(number)


def count_line(pairs: tuple[tuple[Any, str], ...], source: dict[str, Any]) -> list[str]:
    """
    按「字段名 → 中文标签」的顺序把计数拼成短句。

    Args:
        pairs: ((接口字段, 中文标签), ...)。
        source: 平台返回的统计节点。
    Returns:
        形如 ["点赞 1.2万", "评论 345"] 的列表，取不到的项直接跳过。
    """
    if not isinstance(source, dict):
        return []
    parts: list[str] = []
    for key, label in pairs:
        count = format_count(source.get(key))
        if count:
            parts.append(f"{label} {count}")
    return parts
