"""A small in-memory TTL + LRU cache with request coalescing."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Hashable


class TTLCache:
    def __init__(self, max_entries: int = 4096) -> None:
        self.max_entries = max_entries
        self._data: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._inflight: dict[Hashable, asyncio.Future] = {}

    def get(self, key: Hashable) -> tuple[bool, Any]:
        item = self._data.get(key)
        if item is None:
            return False, None
        expires, value = item
        if expires < time.monotonic():
            self._data.pop(key, None)
            return False, None
        self._data.move_to_end(key)
        return True, value

    def set(self, key: Hashable, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        self._data[key] = (time.monotonic() + ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

    def invalidate(self, predicate: Callable[[Hashable], bool]) -> int:
        keys = [k for k in self._data if predicate(k)]
        for k in keys:
            self._data.pop(k, None)
        return len(keys)

    def __len__(self) -> int:
        return len(self._data)

    async def get_or_fetch(
        self, key: Hashable, ttl: float, fetch: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Return a cached value or run ``fetch`` once, even if many callers
        ask for the same key concurrently."""
        hit, value = self.get(key)
        if hit:
            return value
        pending = self._inflight.get(key)
        if pending is not None:
            return await asyncio.shield(pending)
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._inflight[key] = future
        try:
            value = await fetch()
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
                future.exception()  # mark retrieved so it is never "unhandled"
            raise
        else:
            self.set(key, value, ttl)
            if not future.done():
                future.set_result(value)
            return value
        finally:
            self._inflight.pop(key, None)
