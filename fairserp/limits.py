"""Quotas, upstream budgets, circuit breakers and host spacing, all on the state store.

The state store never evicts, so none of these can silently reset under memory pressure.
Every counter uses an atomic increment first and gives the slot back when over the limit,
so concurrent requests cannot overshoot.
"""

import asyncio
import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fairserp.config import WINDOW_SECONDS, Budget, KeyLimits
from fairserp.engines.base import EngineError, ErrorKind
from fairserp.store import Store

# Hosts whose usage policies set a minimum spacing between requests, in milliseconds.
HOST_INTERVAL_MS = {"nominatim.openstreetmap.org": 1000}
MAX_SLOT_WAIT_MS = 1500


def owner_id(api_key: str) -> str:
    """Stable, non-reversible id for a key, used in storage instead of the key itself."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def window_bucket(window: str, moment: datetime) -> str:
    return {"hour": f"{moment:%Y%m%d%H}", "day": f"{moment:%Y%m%d}",
            "month": f"{moment:%Y%m}"}[window]


class QuotaExceeded(Exception):
    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class Reservation:
    """A reserved search. Carries the exact counter keys it incremented, so a refund always
    lands in the same hour and month bucket even if it happens after the hour rolls over."""

    keys: tuple[str, ...]


class Quotas:
    def __init__(self, state: Store, clock=lambda: datetime.now(UTC)) -> None:
        self._state = state
        self._clock = clock

    def usage_keys(self, api_key: str, moment: datetime | None = None) -> tuple[str, str]:
        moment = moment or self._clock()
        owner = owner_id(api_key)
        return (f"usage:hour:{owner}:{window_bucket('hour', moment)}",
                f"usage:month:{owner}:{window_bucket('month', moment)}")

    async def reserve(self, api_key: str, limits: KeyLimits) -> Reservation:
        moment = self._clock()
        hour_key, month_key = self.usage_keys(api_key, moment)
        if await self._state.incr(hour_key, WINDOW_SECONDS["hour"]) > limits.hourly:
            await self._state.decr(hour_key)
            raise QuotaExceeded("Your searches for the past hour have exceeded your hourly "
                                "limit.", _seconds_to_next_hour(moment))
        if await self._state.incr(month_key, WINDOW_SECONDS["month"]) > (
                limits.monthly or float("inf")):
            await self._state.decr(month_key)
            await self._state.decr(hour_key)
            raise QuotaExceeded("Your account has run out of searches.", 3600)
        return Reservation((hour_key, month_key))

    async def release(self, reservation: Reservation) -> None:
        for key in reservation.keys:
            await self._state.decr(key)

    async def usage(self, api_key: str) -> dict[str, int]:
        hour_key, month_key = self.usage_keys(api_key)
        last_hour = datetime.fromtimestamp(self._clock().timestamp() - 3600, UTC)
        last_hour_key, _ = self.usage_keys(api_key, last_hour)
        return {"hour": await self._state.count(hour_key),
                "month": await self._state.count(month_key),
                "last_hour": await self._state.count(last_hour_key)}


def _seconds_to_next_hour(moment: datetime) -> int:
    return 3600 - (moment.minute * 60 + moment.second)


class UpstreamBudgets:
    """Caps on calls to one upstream, server-wide and per key, so one tenant cannot drain a
    shared allowance such as Firecrawl's per-IP keyless cap."""

    def __init__(self, state: Store, budgets: list[Budget],
                 clock=lambda: datetime.now(UTC)) -> None:
        self._state = state
        self._budgets = budgets
        self._clock = clock

    async def spend(self, engine: str, api_key: str) -> Reservation:
        moment = self._clock()
        taken: list[str] = []
        for budget in (b for b in self._budgets if b.engine == engine):
            scope = owner_id(api_key) if budget.per_key else "all"
            key = f"budget:{engine}:{budget.window}:{scope}:{window_bucket(budget.window, moment)}"
            if await self._state.incr(key, WINDOW_SECONDS[budget.window]) > budget.limit:
                await self._state.decr(key)
                for done in taken:
                    await self._state.decr(done)
                who = "this key's" if budget.per_key else "the server's"
                raise EngineError(engine, f"{who} {budget.window}ly upstream budget "
                                          f"({budget.limit}) is spent", ErrorKind.THROTTLED)
            taken.append(key)
        return Reservation(tuple(taken))

    async def refund(self, reservation: Reservation) -> None:
        for key in reservation.keys:
            await self._state.decr(key)


class CircuitBreaker:
    """Stops calling an engine after repeated failures, or for as long as it asked
    (Retry-After). Shared across workers through the state store."""

    def __init__(self, state: Store, failures: int, cooldown: int) -> None:
        self._state = state
        self._failures = failures
        self._cooldown = cooldown

    async def check(self, engine: str) -> None:
        remaining = await self._state.pttl(f"circuit:{engine}")
        if remaining > 0:
            seconds = max(1, remaining // 1000)
            raise EngineError(engine, f"circuit open after upstream failures, retry in "
                                      f"{seconds}s", ErrorKind.THROTTLED, seconds)

    async def record(self, engine: str, error: EngineError | None) -> None:
        streak = f"circuit_streak:{engine}"
        if error is None:
            await self._state.delete(streak)
            return
        # Only signs of an upstream outage count. A 4xx or malformed payload can be caused by
        # one caller's query, so it must not let that caller switch an engine off for all.
        if error.kind not in (ErrorKind.FAILED, ErrorKind.TIMEOUT, ErrorKind.RATE_LIMITED):
            return
        if error.kind == ErrorKind.RATE_LIMITED and error.retry_after:
            await self._open(engine, error.retry_after)
            return
        if await self._state.incr(streak, self._cooldown * 4) >= self._failures:
            await self._open(engine, self._cooldown)
            await self._state.delete(streak)

    async def _open(self, engine: str, seconds: int) -> None:
        await self._state.set_json(f"circuit:{engine}", 1, seconds)


class HostLimiter:
    """Minimum spacing between requests to policy-limited hosts, coordinated across workers
    with SET NX PX on the state store. Waits at most MAX_SLOT_WAIT_MS, then gives up with a
    rate-limit error instead of queueing, so one slow host cannot stall a whole response."""

    def __init__(self, state: Store, intervals: dict[str, int] | None = None,
                 max_wait_ms: int = MAX_SLOT_WAIT_MS) -> None:
        self._state = state
        self._intervals = HOST_INTERVAL_MS if intervals is None else intervals
        self._max_wait_ms = max_wait_ms

    async def acquire(self, engine: str, url: str) -> None:
        host = urlsplit(url).hostname or ""
        interval = self._intervals.get(host)
        if not interval:
            return
        deadline = time.monotonic() + self._max_wait_ms / 1000
        while not await self._state.set_nx(f"slot:{host}", interval):
            wait = await self._state.pttl(f"slot:{host}") / 1000
            if time.monotonic() + wait > deadline:
                raise EngineError(engine, f"{host} allows one request per {interval} ms and "
                                          "the next slot is too far away",
                                  ErrorKind.THROTTLED, 1)
            await asyncio.sleep(wait)
