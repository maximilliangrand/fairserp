"""Key-value storage.

fairserp keeps two stores apart on purpose:
- the cache store (fragments, archive, async jobs) is disposable and may evict;
- the state store (quota counters, upstream budgets, circuit breakers, host limiters) must
  never evict, otherwise limits silently reset under memory pressure.

The in-memory backend suits a single process. Use Redis for several workers or replicas:
cache on an allkeys-lru instance, state on a noeviction instance.
"""

import json
import time
from typing import Any, Protocol

import redis.asyncio as redis


class Store(Protocol):
    async def get_json(self, key: str) -> Any | None: ...

    async def set_json(self, key: str, value: Any, ttl: float) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def incr(self, key: str, ttl: int) -> int:
        """Increment a counter, starting its expiry window on first use."""
        ...

    async def decr(self, key: str) -> None: ...

    async def count(self, key: str) -> int: ...

    async def set_nx(self, key: str, ttl_ms: int) -> bool:
        """Set a marker only if absent. True when this caller set it."""
        ...

    async def pttl(self, key: str) -> int:
        """Milliseconds until the key expires; 0 when absent."""
        ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...


class MemoryStore:
    """Single-process store. ``max_entries`` bounds a cache store; state stores pass None."""

    def __init__(self, max_entries: int | None = None, clock=time.monotonic) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._max_entries = max_entries
        self._clock = clock

    def _live(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= self._clock():
            del self._data[key]
            return None
        return value

    async def get_json(self, key: str) -> Any | None:
        value = self._live(key)
        return None if value is None else json.loads(value)

    async def set_json(self, key: str, value: Any, ttl: float) -> None:
        if ttl <= 0:
            self._data.pop(key, None)
            return
        self._make_room()
        self._data.pop(key, None)  # re-insert so insertion order tracks recency
        self._data[key] = (self._clock() + ttl, json.dumps(value))

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def incr(self, key: str, ttl: int) -> int:
        current = self._live(key)
        if current is None:
            self._data[key] = (self._clock() + ttl, 1)
            return 1
        expires_at, _ = self._data[key]
        self._data[key] = (expires_at, current + 1)
        return current + 1

    async def decr(self, key: str) -> None:
        current = self._live(key)
        if current:
            expires_at, _ = self._data[key]
            self._data[key] = (expires_at, current - 1)

    async def count(self, key: str) -> int:
        return self._live(key) or 0

    async def set_nx(self, key: str, ttl_ms: int) -> bool:
        if self._live(key) is not None:
            return False
        self._data[key] = (self._clock() + ttl_ms / 1000, 1)
        return True

    async def pttl(self, key: str) -> int:
        if self._live(key) is None:
            return 0
        return max(0, int((self._data[key][0] - self._clock()) * 1000))

    async def ping(self) -> bool:
        return True

    def _make_room(self) -> None:
        if self._max_entries is None or len(self._data) < self._max_entries:
            return
        now = self._clock()
        for key in [k for k, (exp, _) in self._data.items() if exp <= now]:
            del self._data[key]
        overflow = len(self._data) - self._max_entries + 1
        for key in list(self._data)[: max(0, overflow)]:
            del self._data[key]

    async def close(self) -> None:
        self._data.clear()


class RedisStore:
    def __init__(self, url: str | None = None, client: redis.Redis | None = None) -> None:
        self._redis = client or redis.from_url(url, decode_responses=True)

    async def get_json(self, key: str) -> Any | None:
        value = await self._redis.get(key)
        return None if value is None else json.loads(value)

    async def set_json(self, key: str, value: Any, ttl: float) -> None:
        if ttl <= 0:
            await self._redis.delete(key)
            return
        await self._redis.set(key, json.dumps(value), px=max(1, int(ttl * 1000)))

    async def delete(self, key: str) -> None:
        await self._redis.delete(key)

    async def incr(self, key: str, ttl: int) -> int:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, ttl, nx=True)
            count, _ = await pipe.execute()
        return int(count)

    async def decr(self, key: str) -> None:
        await self._redis.decr(key)

    async def count(self, key: str) -> int:
        return int(await self._redis.get(key) or 0)

    async def set_nx(self, key: str, ttl_ms: int) -> bool:
        return bool(await self._redis.set(key, "1", nx=True, px=ttl_ms))

    async def pttl(self, key: str) -> int:
        return max(0, int(await self._redis.pttl(key)))

    async def ping(self) -> bool:
        return bool(await self._redis.ping())

    async def close(self) -> None:
        await self._redis.aclose()


def create_store(backend: str, max_entries: int | None = None) -> MemoryStore | RedisStore:
    if backend.startswith(("redis://", "rediss://")):
        return RedisStore(backend)
    if backend != "memory":
        raise ValueError(f"Unsupported store backend: {backend!r}")
    return MemoryStore(max_entries)
