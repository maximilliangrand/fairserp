"""Engine adapter contract.

An adapter is two halves: ``fetch`` talks to the upstream and returns its raw payload,
``parse`` turns that payload into SerpApi-shaped fragments. Keeping ``parse`` pure is what
lets every parser be tested against saved fixtures without the network.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

from fairserp.config import Settings
from fairserp.http import HttpPool


class Compliance(StrEnum):
    """How an adapter obtains data. Shown in /engines and in the README table."""

    OFFICIAL_API = "official-api"
    OPEN_DATA_API = "open-data-api"
    USER_OPERATED = "user-operated"


class Role(StrEnum):
    ORGANIC = "organic"
    ENRICHMENT = "enrichment"
    COMPOSITE = "composite"


@dataclass
class SearchRequest:
    q: str
    engine: str
    location: str | None = None
    gl: str | None = None
    hl: str | None = None
    safe: str | None = None
    num: int = 10
    start: int = 0
    params: dict[str, str] = field(default_factory=dict)

    @property
    def page(self) -> int:
        return self.start // self.num + 1


@dataclass
class EngineContext:
    settings: Settings
    http: HttpPool


class EngineError(Exception):
    """An upstream failure the caller should see, never retried around."""

    def __init__(self, engine: str, message: str, status: int = 502) -> None:
        super().__init__(f"{engine}: {message}")
        self.engine = engine
        self.message = message
        self.status = status


Fragments = dict[str, Any]


class Engine(ABC):
    name: str
    role: Role = Role.ORGANIC
    compliance: Compliance = Compliance.OFFICIAL_API
    upstream: str = ""
    # Setting attribute names that must be non-empty for the engine to be usable.
    required_settings: tuple[str, ...] = ()
    # None means "use the global cache TTL". 0 disables caching for this engine.
    default_cache_ttl: int | None = None
    notes: str = ""

    def is_configured(self, settings: Settings) -> bool:
        return all(getattr(settings, attr) for attr in self.required_settings)

    async def search(self, request: SearchRequest, ctx: EngineContext) -> Fragments:
        if not self.is_configured(ctx.settings):
            missing = ", ".join(f"FAIRSERP_{a.upper()}" for a in self.required_settings)
            raise EngineError(self.name, f"engine is not configured, set {missing}", 503)
        raw = await self.fetch(request, ctx)
        try:
            return self.parse(raw, request)
        except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
            raise EngineError(self.name, "upstream returned an unexpected payload") from exc

    @abstractmethod
    async def fetch(self, request: SearchRequest, ctx: EngineContext) -> Any: ...

    @abstractmethod
    def parse(self, raw: Any, request: SearchRequest) -> Fragments: ...

    async def get_json(self, ctx: EngineContext, method: str, url: str, **kwargs) -> Any:
        try:
            response = await ctx.http.request(self.name, method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise EngineError(self.name, "upstream timed out", 504) from exc
        except httpx.HTTPError as exc:
            raise EngineError(self.name, "upstream connection failed") from exc
        if response.status_code in (401, 403):
            raise EngineError(self.name, f"upstream refused the request ({response.status_code})")
        if response.status_code == 429:
            raise EngineError(self.name, "upstream rate limit reached", 429)
        if response.status_code >= 400:
            raise EngineError(self.name, f"upstream returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise EngineError(self.name, "upstream returned non-JSON content") from exc
