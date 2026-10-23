"""Key-value storage shared by the result cache, the search archive and rate limiting.

The in-memory backend suits a single process. Use Redis when running several workers
or replicas so limits and cached results are shared.
"""

import json
import time
from typing import Any, Protocol

import redis.asyncio as redis


class Store(Protocol):
    async def get_json(self, key: str) -> Any | None: ...

    async def set_json(self, key: str, value: Any, ttl: int) -> None: ...

    async def incr(self, key: str, ttl: int) -> int:
        """Increment a counter, starting its expiry window on first use."""
        ...

    async def count(self, key: str) -> int: ...

    async def decr(self, key: str) -> None: ...

    async def ttl(self, key: str) -> int: ...

    async def close(self) -> None: ...


class MemoryStore:
    def __init__(self, max_entries: int = 50_000) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._max_entries = max_entries

    def _live(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time.monotonic():
            del self._data[key]
            return None
        return value

    async def get_json(self, key: str) -> Any | None:
        value = self._live(key)
        return None if value is None else json.loads(value)

    async def set_json(self, key: str, value: Any, ttl: int) -> None:
        self._evict_expired()
        self._data[key] = (time.monotonic() + ttl, json.dumps(value))

    async def incr(self, key: str, ttl: int) -> int:
        current = self._live(key)
        if current is None:
            self._data[key] = (time.monotonic() + ttl, 1)
            return 1
        expires_at, _ = self._data[key]
        self._data[key] = (expires_at, current + 1)
        return current + 1

    async def count(self, key: str) -> int:
        return self._live(key) or 0

    async def decr(self, key: str) -> None:
        current = self._live(key)
        if current:
            expires_at, _ = self._data[key]
            self._data[key] = (expires_at, current - 1)

    async def ttl(self, key: str) -> int:
        entry = self._data.get(key)
        return max(0, int(entry[0] - time.monotonic())) if entry else 0

    def _evict_expired(self) -> None:
        if len(self._data) < self._max_entries:
            return
        now = time.monotonic()
        for key in [k for k, (exp, _) in self._data.items() if exp <= now]:
            del self._data[key]
        # Still full: drop the oldest entries (dicts keep insertion order).
        overflow = len(self._data) - self._max_entries + 1
        for key in list(self._data)[: max(0, overflow)]:
            del self._data[key]

    async def close(self) -> None:
        self._data.clear()


class RedisStore:
    def __init__(self, url: str) -> None:
        self._redis = redis.from_url(url, decode_responses=True)

    async def get_json(self, key: str) -> Any | None:
        value = await self._redis.get(key)
        return None if value is None else json.loads(value)

    async def set_json(self, key: str, value: Any, ttl: int) -> None:
        await self._redis.set(key, json.dumps(value), ex=ttl)

    async def incr(self, key: str, ttl: int) -> int:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, ttl, nx=True)
            count, _ = await pipe.execute()
        return int(count)

    async def count(self, key: str) -> int:
        return int(await self._redis.get(key) or 0)

    async def decr(self, key: str) -> None:
        await self._redis.decr(key)

    async def ttl(self, key: str) -> int:
        return max(0, int(await self._redis.ttl(key)))

    async def close(self) -> None:
        await self._redis.aclose()


def create_store(backend: str) -> MemoryStore | RedisStore:
    if backend.startswith(("redis://", "rediss://")):
        return RedisStore(backend)
    if backend != "memory":
        raise ValueError(f"Unsupported FAIRSERP_BACKEND: {backend!r}")
    return MemoryStore()
