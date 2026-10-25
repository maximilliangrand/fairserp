"""Quotas, budgets and limiters against both store backends, under pressure."""

import asyncio
import time
from datetime import UTC, datetime

import fakeredis
import pytest

from fairserp.config import Budget, KeyLimits
from fairserp.engines.base import EngineError
from fairserp.limits import HostLimiter, QuotaExceeded, Quotas, UpstreamBudgets
from fairserp.store import MemoryStore, RedisStore


@pytest.fixture(params=["memory", "redis"])
def state(request):
    if request.param == "memory":
        return MemoryStore()
    return RedisStore(client=fakeredis.FakeAsyncRedis(decode_responses=True))


async def test_concurrent_reservations_never_overshoot(state):
    quotas = Quotas(state)

    async def attempt():
        try:
            await quotas.reserve("k", KeyLimits(hourly=5, monthly=0))
            return True
        except QuotaExceeded:
            return False

    results = await asyncio.gather(*(attempt() for _ in range(40)))
    assert sum(results) == 5
    assert (await quotas.usage("k"))["hour"] == 5


async def test_monthly_cap(state):
    quotas = Quotas(state)
    for _ in range(3):
        await quotas.reserve("k", KeyLimits(hourly=100, monthly=3))
    with pytest.raises(QuotaExceeded, match="run out"):
        await quotas.reserve("k", KeyLimits(hourly=100, monthly=3))
    assert (await quotas.usage("k"))["hour"] == 3


async def test_refund_lands_in_the_original_hour_bucket(state):
    now = {"t": datetime(2026, 10, 23, 9, 59, 59, tzinfo=UTC)}
    quotas = Quotas(state, clock=lambda: now["t"])
    first = await quotas.reserve("k", KeyLimits(hourly=10, monthly=0))
    now["t"] = datetime(2026, 10, 23, 10, 0, 1, tzinfo=UTC)
    await quotas.reserve("k", KeyLimits(hourly=10, monthly=0))
    await quotas.release(first)
    assert (await quotas.usage("k"))["hour"] == 1  # the 10:00 search is still counted


async def test_upstream_budget_is_shared_across_keys_and_per_key(state):
    budgets = UpstreamBudgets(state, [Budget("firecrawl", 3, "day", False),
                                      Budget("firecrawl", 2, "day", True)])
    await budgets.spend("firecrawl", "a")
    await budgets.spend("firecrawl", "a")
    with pytest.raises(EngineError, match="this key's"):
        await budgets.spend("firecrawl", "a")
    await budgets.spend("firecrawl", "b")
    with pytest.raises(EngineError, match="the server's"):
        await budgets.spend("firecrawl", "c")
    await budgets.spend("other-engine", "a")  # unbudgeted engines are free


async def test_cache_pressure_cannot_evict_accounting():
    cache, state = MemoryStore(max_entries=10), MemoryStore()
    quotas = Quotas(state)
    await quotas.reserve("k", KeyLimits(hourly=2, monthly=0))
    for i in range(500):
        await cache.set_json(f"frag:{i}", {"x": i}, 3600)
    await quotas.reserve("k", KeyLimits(hourly=2, monthly=0))
    with pytest.raises(QuotaExceeded):
        await quotas.reserve("k", KeyLimits(hourly=2, monthly=0))
    assert len(cache._data) <= 10


async def test_memory_cache_evicts_oldest_first():
    cache = MemoryStore(max_entries=3)
    for key in "abcd":
        await cache.set_json(key, key, 60)
    assert await cache.get_json("a") is None
    assert await cache.get_json("d") == "d"


async def test_host_limiter_spaces_requests_across_workers(state):
    url = "https://nominatim.openstreetmap.org/search"
    worker_one = HostLimiter(state, {"nominatim.openstreetmap.org": 200})
    worker_two = HostLimiter(state, {"nominatim.openstreetmap.org": 200})
    started = time.monotonic()
    await worker_one.acquire("openstreetmap", url)
    await worker_two.acquire("openstreetmap", url)
    await worker_one.acquire("openstreetmap", url)
    assert time.monotonic() - started >= 0.38


async def test_host_limiter_gives_up_instead_of_queueing(state):
    url = "https://nominatim.openstreetmap.org/search"
    limiter = HostLimiter(state, {"nominatim.openstreetmap.org": 5000}, max_wait_ms=100)
    await limiter.acquire("openstreetmap", url)
    started = time.monotonic()
    with pytest.raises(EngineError, match="next slot"):
        await limiter.acquire("openstreetmap", url)
    assert time.monotonic() - started < 0.2


async def test_redis_store_ttl_and_markers():
    store = RedisStore(client=fakeredis.FakeAsyncRedis(decode_responses=True))
    await store.set_json("a", {"x": 1}, 10)
    assert await store.get_json("a") == {"x": 1}
    await store.set_json("a", {"x": 1}, 0)
    assert await store.get_json("a") is None
    assert await store.set_nx("m", 1000) and not await store.set_nx("m", 1000)
    assert 0 < await store.pttl("m") <= 1000
    assert await store.ping()
