"""Startup validation of settings, and store behaviour under failure and growth."""

import logging

import fakeredis
import pytest
from pydantic import ValidationError

from fairserp.store import (
    SWEEP_EVERY_WRITES,
    WARNING_INTERVAL_SECONDS,
    MemoryStore,
    RedisStore,
    StoreUnavailable,
    create_store,
)
from tests.conftest import settings

# Nothing listens on port 1, so connections are refused at once.
DOWN_REDIS = "redis://127.0.0.1:1/0"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize("overrides", [
    {"serpapi_engines": "google"},
    {"serpapi_engines": "google:brave,bing"},
    {"engine_cache_ttl": "mojeek"},
    {"engine_cache_ttl": "mojeek:ten"},
    {"default_hourly_limit": -1},
    {"default_monthly_limit": -5},
    {"request_deadline_seconds": 0},
    {"enrichment_budget_seconds": -1},
    {"http_timeout_seconds": 0},
    {"circuit_failures": 0},
    {"circuit_cooldown_seconds": 0},
    {"research_fanout": 0},
    {"cache_max_entries": 0},
    {"redis_max_connections": 0},
    {"public_url": "serp.example.org"},
    {"public_url": "ftp://serp.example.org"},
    {"public_url": "https://serp.example.org/?a=1"},
    {"public_url": "https://serp.example.org/#top"},
])
def test_invalid_settings_fail_at_startup(overrides):
    with pytest.raises(ValidationError):
        settings(**overrides)


def test_valid_settings_parse():
    config = settings(serpapi_engines="google:brave, bing:mojeek",
                      engine_cache_ttl="mojeek:600", default_hourly_limit=0,
                      default_monthly_limit=0, public_url="http://localhost:8000/serp")
    assert config.parsed_serpapi_engines() == {"google": "brave", "bing": "mojeek"}
    assert config.parsed_engine_cache_ttl() == {"mojeek": 600}
    assert settings(public_url="").public_url == ""
    assert settings(public_url=None).public_url is None


def test_unknown_engine_names():
    config = settings(organic_priority="brave,bravee,mojeek", enrichers="wikidata,wikidta",
                      opt_in="bravee,tavly")
    known = {"brave", "mojeek", "wikidata", "tavily"}
    assert config.unknown_engine_names(known) == ["bravee", "wikidta", "tavly"]
    assert settings(organic_priority="brave", enrichers="", opt_in="").unknown_engine_names(
        {"brave"}) == []


async def test_down_cache_store_fails_open_with_rate_limited_warning(caplog):
    cache = create_store(DOWN_REDIS, max_entries=100)
    with caplog.at_level(logging.WARNING, logger="fairserp"):
        assert await cache.get_json("k") is None
        await cache.set_json("k", {"a": 1}, 60)
        await cache.delete("k")
        assert len(caplog.records) == 1
        cache._last_warning -= WARNING_INTERVAL_SECONDS
        cache._down_until = float("-inf")  # past the cooldown, so Redis is tried again
        assert await cache.get_json("k") is None
        assert len(caplog.records) == 2
        with pytest.raises(StoreUnavailable):  # archive and job records never fail open
            await cache.set_json("k", {"a": 1}, 60, required=True)
    assert not await cache.ping()
    await cache.close()


async def test_down_state_store_raises_store_unavailable():
    state = create_store(DOWN_REDIS)
    for call in (state.incr("k", 60), state.decr("k"), state.count("k"),
                 state.set_nx("k", 1000), state.pttl("k"), state.get_json("k"),
                 state.set_json("k", 1, 60), state.delete("k")):
        with pytest.raises(StoreUnavailable):
            await call
    assert not await state.ping()
    await state.close()


async def test_create_store_fail_open_can_be_explicit():
    assert create_store(DOWN_REDIS, fail_open=True)._fail_open
    assert not create_store(DOWN_REDIS, max_entries=10, fail_open=False)._fail_open


def test_redis_client_has_socket_timeouts():
    kwargs = create_store(DOWN_REDIS)._redis.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == kwargs["socket_connect_timeout"] == 2.0


async def test_redis_incr_works_without_expire_nx():
    store = RedisStore(client=fakeredis.FakeAsyncRedis(version=(6, 2), decode_responses=True))
    assert [await store.incr("c", 60) for _ in range(3)] == [1, 2, 3]
    assert 0 < await store._redis.ttl("c") <= 60
    # A counter left without expiry, e.g. by DECR after it expired, gets a window again.
    await store._redis.set("orphan", 4)
    assert await store.incr("orphan", 60) == 5
    assert 0 < await store._redis.ttl("orphan") <= 60


async def test_memory_state_store_sweeps_expired_counters():
    clock = FakeClock()
    state = MemoryStore(clock=clock)
    for i in range(1000):
        await state.incr(f"old:{i}", 10)
    clock.now += 11
    for i in range(SWEEP_EVERY_WRITES):
        await state.incr(f"new:{i}", 10)
    assert len(state._data) <= SWEEP_EVERY_WRITES
    assert await state.count("new:0") == 1


async def test_memory_store_heap_stays_bounded_under_rewrites():
    state = MemoryStore()
    for _ in range(10_000):
        await state.set_json("circuit:x", 1, 60)
    assert len(state._data) == 1
    assert len(state._expiry_heap) <= 2 * len(state._data) + 1025


async def test_memory_cache_evicts_least_recently_written():
    cache = MemoryStore(max_entries=3)
    for key in "abc":
        await cache.set_json(key, key, 60)
    await cache.set_json("a", "a2", 60)  # rewrite makes "a" the newest
    await cache.set_json("d", "d", 60)
    assert await cache.get_json("b") is None
    assert [await cache.get_json(k) for k in "acd"] == ["a2", "c", "d"]


async def test_memory_cache_drops_expired_before_live_entries():
    clock = FakeClock()
    cache = MemoryStore(max_entries=3, clock=clock)
    await cache.set_json("live", 1, 100)
    await cache.set_json("short1", 1, 1)
    await cache.set_json("short2", 1, 1)
    clock.now += 2
    await cache.set_json("new", 1, 100)
    assert await cache.get_json("live") == 1 and await cache.get_json("new") == 1
    assert len(cache._data) == 2


async def test_down_cache_is_skipped_during_the_cooldown():
    cache = create_store(DOWN_REDIS, max_entries=100)
    calls = 0
    real_get = cache._redis.get

    async def counting_get(key):
        nonlocal calls
        calls += 1
        return await real_get(key)

    cache._redis.get = counting_get
    for _ in range(5):
        assert await cache.get_json("k") is None
    assert calls == 1
    await cache.close()
