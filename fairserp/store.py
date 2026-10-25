"""Key-value storage.

fairserp keeps two stores apart on purpose:
- the cache store (fragments, archive, async jobs) is disposable and may evict;
- the state store (quota counters, upstream budgets, circuit breakers, host limiters) must
  never evict, otherwise limits silently reset under memory pressure.

The in-memory backend suits a single process. Use Redis for several workers or replicas:
cache on an allkeys-lru instance, state on a noeviction instance.
"""

import heapq
import json
import logging
import time
from collections import OrderedDict
from typing import Any, NoReturn, Protocol

import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

logger = logging.getLogger("fairserp")

# Errors that mean Redis is unreachable, as opposed to a bug in the command sent.
_UNREACHABLE = (RedisConnectionError, RedisTimeoutError, OSError)
REDIS_SOCKET_TIMEOUT_SECONDS = 2.0
WARNING_INTERVAL_SECONDS = 30.0
# The in-memory store drops expired entries every this many writes.
SWEEP_EVERY_WRITES = 256


class StoreUnavailable(Exception):
    """The state store cannot be reached. Quotas and budgets cannot be enforced without it,
    so callers should answer 503 rather than serve unmetered traffic."""


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
    """Single-process store. ``max_entries`` bounds a cache store; state stores pass None.

    Expiry times also go on a min-heap, so expired entries are swept every
    SWEEP_EVERY_WRITES writes at a cost proportional to what expired, instead of lingering
    until read again. A full cache evicts its least recently written entry in O(1).
    """

    def __init__(self, max_entries: int | None = None, clock=time.monotonic) -> None:
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._expiry_heap: list[tuple[float, str]] = []
        self._writes = 0
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

    def _put(self, key: str, expires_at: float, value: Any) -> None:
        """Insert or replace an entry with a new expiry, as the most recently written."""
        self._data.pop(key, None)
        self._data[key] = (expires_at, value)
        heapq.heappush(self._expiry_heap, (expires_at, key))
        # Overwrites leave stale heap records behind; rebuild once they dominate, which
        # keeps the heap within a constant factor of the live data.
        if len(self._expiry_heap) > 2 * len(self._data) + 1024:
            self._expiry_heap = [(exp, k) for k, (exp, _) in self._data.items()]
            heapq.heapify(self._expiry_heap)
        self._writes += 1
        if self._writes % SWEEP_EVERY_WRITES == 0:
            self._sweep()

    def _sweep(self) -> None:
        now = self._clock()
        heap = self._expiry_heap
        while heap and heap[0][0] <= now:
            expires_at, key = heapq.heappop(heap)
            entry = self._data.get(key)
            # Skip records for keys that were since rewritten with a later expiry.
            if entry is not None and entry[0] == expires_at:
                del self._data[key]

    async def get_json(self, key: str) -> Any | None:
        value = self._live(key)
        return None if value is None else json.loads(value)

    async def set_json(self, key: str, value: Any, ttl: float) -> None:
        if ttl <= 0:
            self._data.pop(key, None)
            return
        self._make_room(key)
        self._put(key, self._clock() + ttl, json.dumps(value))

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def incr(self, key: str, ttl: int) -> int:
        current = self._live(key)
        if current is None:
            self._put(key, self._clock() + ttl, 1)
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
        self._put(key, self._clock() + ttl_ms / 1000, 1)
        return True

    async def pttl(self, key: str) -> int:
        if self._live(key) is None:
            return 0
        return max(0, int((self._data[key][0] - self._clock()) * 1000))

    async def ping(self) -> bool:
        return True

    def _make_room(self, key: str) -> None:
        if self._max_entries is None or key in self._data:
            return
        if len(self._data) >= self._max_entries:
            self._sweep()
        while len(self._data) >= self._max_entries:
            self._data.popitem(last=False)

    async def close(self) -> None:
        self._data.clear()
        self._expiry_heap.clear()


class RedisStore:
    """Redis-backed store.

    With ``fail_open`` (the cache store) an unreachable Redis degrades to no caching: reads
    miss and writes are skipped, with a warning at most every WARNING_INTERVAL_SECONDS.
    Without it (the state store) every operation raises StoreUnavailable instead.
    """

    def __init__(self, url: str | None = None, client: redis.Redis | None = None,
                 max_connections: int = 50, fail_open: bool = False) -> None:
        if client is None:
            # A blocking pool waits for a free connection instead of raising
            # MaxConnectionsError when a burst of requests exceeds the pool size. Explicit
            # socket timeouts, because older redis-py versions default to waiting forever.
            pool = redis.BlockingConnectionPool.from_url(
                url, max_connections=max_connections, timeout=5, decode_responses=True,
                socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
                socket_connect_timeout=REDIS_SOCKET_TIMEOUT_SECONDS)
            client = redis.Redis(connection_pool=pool)
        self._redis = client
        self._fail_open = fail_open
        self._last_warning = float("-inf")

    def _unavailable(self, error: Exception) -> NoReturn:
        raise StoreUnavailable(f"Redis store unreachable: {type(error).__name__}") from error

    def _skip(self, error: Exception) -> None:
        """Fail open for a cache operation, or raise when this store must not."""
        if not self._fail_open:
            self._unavailable(error)
        now = time.monotonic()
        if now - self._last_warning >= WARNING_INTERVAL_SECONDS:
            self._last_warning = now
            logger.warning("cache store unreachable (%s); serving without cache",
                           type(error).__name__)

    async def get_json(self, key: str) -> Any | None:
        try:
            value = await self._redis.get(key)
        except _UNREACHABLE as error:
            self._skip(error)
            return None
        return None if value is None else json.loads(value)

    async def set_json(self, key: str, value: Any, ttl: float) -> None:
        try:
            if ttl <= 0:
                await self._redis.delete(key)
                return
            await self._redis.set(key, json.dumps(value), px=max(1, int(ttl * 1000)))
        except _UNREACHABLE as error:
            self._skip(error)

    async def delete(self, key: str) -> None:
        try:
            await self._redis.delete(key)
        except _UNREACHABLE as error:
            self._skip(error)

    async def incr(self, key: str, ttl: int) -> int:
        # SET NX EX creates the counter with its expiry and INCR keeps that expiry, all in
        # one MULTI, so this works on any Redis without EXPIRE NX (7.0+) or Lua.
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.set(key, 0, ex=max(1, ttl), nx=True)
                pipe.incr(key)
                pipe.ttl(key)
                _, count, remaining = await pipe.execute()
            if remaining == -1:
                # A counter without expiry, e.g. left by a DECR after it expired, would
                # never reset; give it a window.
                await self._redis.expire(key, max(1, ttl))
        except _UNREACHABLE as error:
            self._unavailable(error)
        return int(count)

    async def decr(self, key: str) -> None:
        try:
            await self._redis.decr(key)
        except _UNREACHABLE as error:
            self._unavailable(error)

    async def count(self, key: str) -> int:
        try:
            return int(await self._redis.get(key) or 0)
        except _UNREACHABLE as error:
            self._unavailable(error)

    async def set_nx(self, key: str, ttl_ms: int) -> bool:
        try:
            return bool(await self._redis.set(key, "1", nx=True, px=ttl_ms))
        except _UNREACHABLE as error:
            self._unavailable(error)

    async def pttl(self, key: str) -> int:
        try:
            return max(0, int(await self._redis.pttl(key)))
        except _UNREACHABLE as error:
            self._unavailable(error)

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except _UNREACHABLE:
            return False

    async def close(self) -> None:
        await self._redis.aclose()


def create_store(backend: str, max_entries: int | None = None, max_connections: int = 50,
                 fail_open: bool | None = None) -> MemoryStore | RedisStore:
    """``fail_open`` marks a disposable cache store; None infers it from ``max_entries``,
    which only cache stores pass."""
    if fail_open is None:
        fail_open = max_entries is not None
    if backend.startswith(("redis://", "rediss://")):
        return RedisStore(backend, max_connections=max_connections, fail_open=fail_open)
    if backend != "memory":
        raise ValueError(f"Unsupported store backend: {backend!r}")
    return MemoryStore(max_entries)
