"""Engine adapter contract.

An adapter has two halves: ``fetch`` talks to the upstream and returns its raw payload,
``parse`` turns that payload into a typed ``EngineOutput``. ``parse`` is pure, so every
parser is tested against saved payloads without the network, including malformed ones.

Each adapter also declares, as data:
- ``rights``: provenance, commercial use, storage limit, attribution and terms;
- ``supports``: which optional query params it can honor (the rest are reported ignored);
- ``max_depth``: how deep into a result list the upstream can page;
- ``fixture_verified``: whether its parser fixtures were recorded from the real API.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from fairserp.config import Settings
from fairserp.http import HttpPool
from fairserp.model import EngineOutput, SearchQuery
from fairserp.rights import Rights


class Role(StrEnum):
    ORGANIC = "organic"
    ENRICHMENT = "enrichment"
    LOCAL = "local"


class ErrorKind(StrEnum):
    FAILED = "failed"  # upstream outage: connection error or 5xx. Counts toward the circuit.
    REJECTED = "rejected"  # upstream refused this request (4xx) or sent a bad payload
    RATE_LIMITED = "rate_limited"  # upstream said slow down (429)
    THROTTLED = "throttled"  # fairserp's own limits: budget, open circuit, host spacing
    TIMEOUT = "timeout"
    NOT_CONFIGURED = "not_configured"
    POLICY = "policy"
    BAD_REQUEST = "bad_request"


class EngineError(Exception):
    """An upstream or policy failure the caller should see. Never retried around."""

    def __init__(
        self,
        engine: str,
        message: str,
        kind: ErrorKind = ErrorKind.FAILED,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(f"{engine}: {message}")
        self.engine = engine
        self.message = message
        self.kind = kind
        self.retry_after = retry_after


class PayloadError(ValueError):
    """The upstream answered with something that is not a valid result envelope."""


def require(condition: Any, message: str) -> None:
    if not condition:
        raise PayloadError(message)


@dataclass
class EngineContext:
    settings: Settings
    http: HttpPool
    cache: Any = None  # cache Store, for engine-internal lookups such as geocoding
    limiter: Any = None  # HostLimiter, spacing requests to hosts with usage policies


class Engine(ABC):
    name: str
    role: Role = Role.ORGANIC
    upstream: str = ""
    rights: Rights
    required_settings: tuple[str, ...] = ()
    supports: frozenset[str] = frozenset()
    max_depth: int = 100
    fixture_verified: bool = False
    cost_per_call_usd: float = 0.0
    notes: str = ""

    def rights_for(self, settings: Settings) -> Rights:
        """Rights can depend on deployment settings (e.g. which kind of key is held)."""
        return self.rights

    def applied(self, query: SearchQuery) -> set[str]:
        """The requested optional params this engine will actually honor for the query."""
        return query.requested_params() & self.supports

    def is_configured(self, settings: Settings) -> bool:
        return all(getattr(settings, attr) for attr in self.required_settings)

    async def search(self, query: SearchQuery, ctx: EngineContext) -> EngineOutput:
        if not self.is_configured(ctx.settings):
            missing = ", ".join(f"FAIRSERP_{a.upper()}" for a in self.required_settings)
            raise EngineError(self.name, f"not configured, set {missing}",
                              ErrorKind.NOT_CONFIGURED)
        try:
            raw = await self.fetch(query, ctx)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            # fetch() steps through intermediate payloads (e.g. Wikidata's lookups), so a
            # shape surprise there is a bad upstream payload too, not a server error.
            raise EngineError(self.name, f"unexpected upstream payload: {exc!r}",
                              ErrorKind.REJECTED) from exc
        try:
            return self.parse(raw, query)
        except (PayloadError, KeyError, TypeError, ValueError, AttributeError) as exc:
            raise EngineError(self.name, f"unexpected upstream payload: {exc}",
                              ErrorKind.REJECTED) from exc

    @abstractmethod
    async def fetch(self, query: SearchQuery, ctx: EngineContext) -> Any: ...

    @abstractmethod
    def parse(self, raw: Any, query: SearchQuery) -> EngineOutput: ...

    async def get_json(self, ctx: EngineContext, method: str, url: str, **kwargs) -> Any:
        if ctx.limiter is not None:
            await ctx.limiter.acquire(self.name, url)
        try:
            response = await ctx.http.request(self.name, method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise EngineError(self.name, "upstream timed out", ErrorKind.TIMEOUT) from exc
        except httpx.HTTPError as exc:
            raise EngineError(self.name, "upstream connection failed") from exc
        status = response.status_code
        if status == 429 or (status == 503 and "retry-after" in response.headers):
            raise EngineError(self.name, "upstream rate limit reached", ErrorKind.RATE_LIMITED,
                              _retry_after(response))
        if status in (401, 403):
            raise EngineError(self.name, f"upstream refused the request ({status})",
                              ErrorKind.REJECTED)
        if 300 <= status < 400:
            raise EngineError(self.name, f"upstream redirected ({status}); not followed",
                              ErrorKind.REJECTED)
        if 400 <= status < 500:
            raise EngineError(self.name, f"upstream returned HTTP {status}", ErrorKind.REJECTED)
        if status >= 500:
            raise EngineError(self.name, f"upstream returned HTTP {status}")
        try:
            return response.json()
        except ValueError as exc:
            raise EngineError(self.name, "upstream returned non-JSON content",
                              ErrorKind.REJECTED) from exc


def _retry_after(response: httpx.Response) -> int | None:
    value = response.headers.get("retry-after", "")
    return int(value) if value.isdigit() else None
