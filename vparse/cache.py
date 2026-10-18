"""
解析结果缓存。

同一条链接常在群里被反复粘贴，缓存能省掉一次完整解析：抖音要请求分享页最多三次，
B站要请求 view 加 playurl 两三个接口。播放地址本身带有效期（各家都是几小时），
所以缓存只保留很短一段时间，默认 10 分钟，过期后重新解析。

容量上做了 LRU 上限，避免长期运行把内存吃饱。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any


def _now() -> float:
    """当前单调时间，单独抽出来便于测试注入时钟。"""
    return time.monotonic()


class MediaCache:
    """带有效期与容量上限的解析结果缓存，时间用单调时钟，不受系统改时间影响。"""

    def __init__(self, capacity: int = 64) -> None:
        """初始化缓存，capacity 为最多保留的条目数。"""
        self._items: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._capacity = max(1, int(capacity))

    def get(self, key: str) -> dict[str, Any] | None:
        """取缓存，过期条目顺手清掉；命中时把条目移到队尾（最近使用）。"""
        item = self._items.get(key)
        if item is None:
            return None
        expires_at, value = item
        if _now() >= expires_at:
            self._items.pop(key, None)
            return None
        self._items.move_to_end(key)
        # 返回浅拷贝，避免调用方改动污染缓存里的原始条目。
        return dict(value)

    def put(self, key: str, value: dict[str, Any], ttl: int) -> None:
        """写入缓存，ttl 小于等于 0 时视为不缓存。"""
        if ttl <= 0:
            return
        self._items[key] = (_now() + ttl, value)
        self._items.move_to_end(key)
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)

    def clear(self) -> None:
        """清空缓存，测试与配置变更时用。"""
        self._items.clear()

    def __len__(self) -> int:
        """当前保留的条目数（含未过期的过期判断，仅用于观测）。"""
        return len(self._items)
