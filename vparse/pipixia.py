"""
皮皮下（皮皮虾）解析。

分享链接跳两跳后能从地址里拿到作品编号（cell_id），再调官方分享接口取数据：

`GET h5.pipix.com/bds/cell/cell_h5_comment/?count=5&aid=1319&app_name=super&cell_id={id}`

返回里 `data.cell_comments[1].comment_info.item` 是作品本体：
视频在 `video.video_high.url_list`，图集在 `note.multi_image`，
另有作者、封面与文案。

整条链路都是自己发请求、自己解析接口返回，不经过任何第三方中转服务。
"""

from __future__ import annotations

import re
from typing import Any

from vparse import common
from vparse.common import VideoParseError


API = (
    "https://h5.pipix.com/bds/cell/cell_h5_comment/"
    "?count=5&aid=1319&app_name=super&cell_id={cell_id}"
)

# 作品编号的来源：跳转后地址里的 /item/{id}，或链接里直接带的 cell_id 参数。
CELL_ID = re.compile(r"/item/([^?/#]+)")
CELL_ID_PARAM = re.compile(r"[?&]cell_id=([^&#]+)", re.IGNORECASE)


def _headers() -> dict[str, str]:
    """接口要的请求头，移动端 UA。"""
    return {
        "User-Agent": common.MOBILE_UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://h5.pipix.com/",
    }


def _cell_id(url: str) -> str:
    """从地址里取作品编号。"""
    match = CELL_ID.search(url)
    if match:
        return match.group(1)
    # 有些分享链接跳转后仍是 cell_id 参数形式。
    match = CELL_ID_PARAM.search(url)
    return match.group(1) if match else ""


def _url_list(node: Any) -> str:
    """从 url_list 结构里取第一个地址。"""
    if isinstance(node, dict):
        items = node.get("url_list")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    url = common.text_of(item.get("url"))
                    if url:
                        return url
                else:
                    url = common.text_of(item)
                    if url:
                        return url
        url = common.text_of(node.get("url"))
        if url:
            return url
    return ""


def _atlas_images(item: dict[str, Any]) -> list[str]:
    """图集作品：note.multi_image 里是全部原图。"""
    note = item.get("note")
    if not isinstance(note, dict):
        return []
    multi = note.get("multi_image")
    if not isinstance(multi, list):
        return []
    result: list[str] = []
    for entry in multi:
        url = _url_list(entry)
        if url:
            result.append(common.https_url(url))
    return result


def _video_url(item: dict[str, Any]) -> str:
    """视频作品：优先 video_high 那档。"""
    video = item.get("video")
    if not isinstance(video, dict):
        return ""
    for key in ("video_high", "video_medium", "video_low"):
        url = _url_list(video.get(key))
        if url:
            return common.https_url(url)
    return ""


def _build(item: dict[str, Any], source_url: str) -> dict[str, Any]:
    """把作品节点转成统一结构。"""
    author = item.get("author")
    if not isinstance(author, dict):
        author = {}
    title = common.block(item.get("content"))
    cover = _url_list(item.get("cover"))

    media: dict[str, Any] = {
        "platform": "pipixia",
        "platform_name": "皮皮下",
        "kind": "video",
        "title": title.split("\n")[0] if title else "皮皮下作品",
        "desc": title,
        "stats": [],
        "author": common.text_of(author.get("name")),
        "duration": None,
        "cover": common.https_url(cover) if cover else "",
        "play_url": "",
        "images": [],
        "source_url": source_url,
    }

    images = _atlas_images(item)
    if images:
        media["kind"] = "images"
        media["images"] = images
        return media

    play_url = _video_url(item)
    if not play_url:
        raise VideoParseError(
            "这个皮皮下作品里没有找到视频地址",
            "作品可能已删除，或接口结构有变化",
        )
    media["play_url"] = play_url
    return media


async def parse(url: str, cookie: str, host: Any, quality: str = "") -> dict[str, Any]:
    """
    解析一条皮皮下链接。

    Args:
        url: 皮皮下分享链接。
        cookie: 保留参数，接口不需要登录。
        host: 插件宿主视图。
        quality: 保留参数，接口只给一种画质。
    Returns:
        统一结构的媒体信息。
    Raises:
        VideoParseError: 链接里没有作品编号，或接口没返回作品数据。
    """
    # 分享链接有两类：一种地址里直接带 cell_id，一种要跳转后才露出编号。
    # 先看原链接，没有再跟跳转。
    cell_id = _cell_id(url)
    if not cell_id:
        final_url = url
        for _ in range(2):
            final_url = await common.follow_short_link(
                host, final_url, headers=_headers(), timeout=10.0
            )
            cell_id = _cell_id(final_url)
            if cell_id:
                break
    if not cell_id:
        raise VideoParseError(
            "没有从链接里识别出皮皮下作品编号",
            "请复制 App 分享出来的完整链接",
        )

    result = await host.http_request(
        "GET",
        API.format(cell_id=cell_id),
        headers=_headers(),
        timeout=12.0,
    )
    payload = common.json_of(result)
    if not payload:
        raise VideoParseError(
            "皮皮下接口没有返回数据", "作品可能已删除，或接口临时不可用"
        )
    # 接口用 status_code 表示业务错误，11001 是参数不对。
    code = payload.get("status_code")
    if code not in (None, 0, 200, "0", "200"):
        message = common.text_of(payload.get("message")) or "接口拒绝了这次请求"
        raise VideoParseError(f"皮皮下解析失败：{message}")

    comments = (payload.get("data") or {}).get("cell_comments")
    if not isinstance(comments, list) or len(comments) < 2:
        raise VideoParseError(
            "皮皮下没有返回作品数据", "作品可能已删除，或接口结构有变化"
        )
    info = (comments[1] or {}).get("comment_info")
    if not isinstance(info, dict):
        raise VideoParseError("皮皮下没有返回作品数据", "作品可能已删除")
    item = info.get("item")
    if not isinstance(item, dict) or not item:
        raise VideoParseError("皮皮下没有返回作品数据", "作品可能已删除")
    return _build(item, url)
