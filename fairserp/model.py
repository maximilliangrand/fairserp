"""Native, typed data model. Every wire format (native JSON, SerpApi, MCP) renders from it."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Mode(StrEnum):
    """How the gateway plans a search.

    fast: one organic provider (falling through on failure or emptiness) plus enrichment.
    research: several approved organic providers queried concurrently and fused.
    private: exactly one upstream sees the query; no enrichment fan-out.
    """

    FAST = "fast"
    RESEARCH = "research"
    PRIVATE = "private"


class Freshness(StrEnum):
    HOUR = "h"
    DAY = "d"
    WEEK = "w"
    MONTH = "m"
    YEAR = "y"


class Vertical(StrEnum):
    WEB = "web"
    LOCAL = "local"


# Query parameters an engine may or may not be able to honor.
OPTIONAL_PARAMS = ("country", "language", "safe", "freshness", "location")


class SearchQuery(BaseModel):
    q: str = Field(min_length=1, max_length=500, description="Search query.")
    engine: str = Field("auto", description="An engine name, or 'auto' to let the planner pick.")
    mode: Mode = Mode.FAST
    vertical: Vertical = Vertical.WEB
    num: int = Field(10, ge=1, le=50, description="Results per page.")
    offset: int = Field(0, ge=0, le=500, description="Zero-based result offset.")
    country: str | None = Field(None, pattern=r"^[a-z]{2}$", description="ISO 3166-1 alpha-2.")
    language: str | None = Field(None, pattern=r"^[a-z]{2,3}$", description="ISO 639 code.")
    safe: bool = Field(False, description="Require safe-search filtering.")
    freshness: Freshness | None = None
    location: str | None = Field(None, max_length=200)
    strict_params: bool = Field(
        False, description="Reject the search instead of reporting params an engine ignores."
    )

    def requested_params(self) -> set[str]:
        requested = {p for p in OPTIONAL_PARAMS if getattr(self, p) not in (None, False)}
        return requested

    def cache_material(self, engine: str) -> dict[str, Any]:
        fields = self.model_dump(exclude={"engine", "mode", "strict_params"})
        return {"engine": engine, **fields}


class Link(BaseModel):
    title: str
    url: str


class WebResult(BaseModel):
    url: str
    title: str
    snippet: str = ""
    published_at: str | None = Field(None, description="Publication date if the source states it.")
    updated_at: str | None = Field(None, description="Last-modified date if the source states it.")
    source_name: str | None = None
    favicon: str | None = None
    thumbnail: str | None = None
    sitelinks: list[Link] = Field(default_factory=list)


class Question(BaseModel):
    question: str
    answer: str = ""
    title: str = ""
    url: str = ""


class Place(BaseModel):
    name: str
    category: str = ""
    address: str = ""
    latitude: float
    longitude: float
    distance_m: int | None = None
    phone: str | None = None
    website: str | None = None
    hours: str | None = None
    rating: float | None = Field(None, description="Unknown when the source has no ratings.")
    reviews: int | None = None
    place_id: str = ""
    description: str | None = None


class Entity(BaseModel):
    title: str
    type: str = ""
    description: str = ""
    url: str = ""
    image: str | None = None
    website: str | None = None
    wikidata_id: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)


class Answer(BaseModel):
    text: str
    type: str = "answer"
    generated: bool = Field(description="True when a model wrote this text, not a source.")
    citations: list[str] = Field(default_factory=list)


class EngineOutput(BaseModel):
    """What one engine returned for one query, before provenance is attached."""

    web: list[WebResult] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    places: list[Place] = Field(default_factory=list)
    entity: Entity | None = None
    answer: Answer | None = None
    related: list[str] = Field(default_factory=list)
    total_results: int | None = None
    has_more: bool | None = None

    def is_empty(self, vertical: Vertical) -> bool:
        return not (self.places if vertical == Vertical.LOCAL else self.web)


class SourceRef(BaseModel):
    engine: str
    rank: int = Field(description="The provider's own 1-based rank for this result.")
    provenance: str


class RankedResult(WebResult):
    rank: int
    canonical_url: str
    sources: list[SourceRef]
    passages: list[str] = Field(default_factory=list)


class ProviderStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


class ProviderReport(BaseModel):
    engine: str
    role: str
    status: ProviderStatus
    cached: bool = False
    latency_ms: int | None = None
    results: int = 0
    error: str | None = None
    retry_after: int | None = None
    applied_params: list[str] = Field(default_factory=list)
    ignored_params: list[str] = Field(default_factory=list)


class OutcomeStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    EMPTY = "empty"
    RATE_LIMITED = "rate_limited"
    FAILED = "failed"


class ParamReport(BaseModel):
    applied: list[str] = Field(default_factory=list)
    ignored: list[str] = Field(default_factory=list)


class Retention(BaseModel):
    expires_at: str | None = Field(
        None, description="Absolute deadline after which no copy of this outcome is kept."
    )
    reason: str | None = None


class SearchOutcome(BaseModel):
    id: str
    status: OutcomeStatus
    created_at: str
    elapsed_ms: int
    query: SearchQuery
    requested_engine: str
    resolved_engine: str
    params: ParamReport
    providers: list[ProviderReport]
    results: list[RankedResult] = Field(default_factory=list)
    places: list[Place] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    entity: Entity | None = None
    answer: Answer | None = None
    related: list[str] = Field(default_factory=list)
    total_results: int | None = None
    has_more: bool = False
    attribution: list[str] = Field(default_factory=list)
    rights: dict[str, dict[str, Any]] = Field(default_factory=dict)
    retention: Retention = Field(default_factory=Retention)
    billable: bool = True
    error: str | None = None
