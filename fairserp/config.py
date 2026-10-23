"""Runtime configuration, read from FAIRSERP_* environment variables."""

import re
from dataclasses import dataclass
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fairserp.rights import Profile

_KEY_ENTRY = re.compile(r"^[^:\s]+(:\d+){0,2}$")
_BUDGET_ENTRY = re.compile(r"^([a-z0-9_]+):(\d+)/(hour|day|month)(/key)?$")
WINDOW_SECONDS = {"hour": 3600, "day": 86400, "month": 31 * 86400}


@dataclass(frozen=True)
class KeyLimits:
    hourly: int
    monthly: int  # 0 means no monthly cap


@dataclass(frozen=True)
class Budget:
    engine: str
    limit: int
    window: str
    per_key: bool


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FAIRSERP_", env_file=".env", extra="ignore")

    # Base URL for self links (json_endpoint, pagination). Unset: derived from each request.
    public_url: str | None = None

    # Comma separated "key", "key:hourly" or "key:hourly:monthly". Empty plus
    # auth_disabled=False rejects every request, so a fresh deploy is never an open relay.
    api_keys: str = ""
    auth_disabled: bool = False
    default_hourly_limit: int = 1000
    default_monthly_limit: int = 0

    # Two stores. The cache store holds disposable data (fragments, archive, async jobs) and
    # may evict. The state store holds accounting, budgets and limiters and must not evict.
    cache_backend: str = "memory"
    state_backend: str = "memory"
    cache_ttl_seconds: int = 3600
    archive_ttl_seconds: int = 31 * 24 * 3600
    cache_max_entries: int = 50_000

    # Outbound HTTP. trust_env stays off so HTTP(S)_PROXY variables never reroute traffic
    # behind the operator's back; "none" in engine_proxies then really means direct.
    user_agent: str = "fairserp/0.2 (+https://github.com/maximilliangrand/fairserp)"
    http_timeout_seconds: float = 10.0
    http_trust_env: bool = False
    # One egress proxy for all engines, plus optional sticky per-engine overrides as JSON,
    # e.g. FAIRSERP_ENGINE_PROXIES='{"brave": "http://proxy:3128", "wikipedia": "none"}'.
    # There is deliberately no rotation across a pool.
    proxy: str = ""
    engine_proxies: dict[str, str] = Field(default_factory=dict)

    # Provider policy. "strict" runs only providers with disclosed provenance; anything else
    # must be named in opt_in. Noncommercial-licensed sources need an explicit declaration.
    profile: Profile = Profile.STRICT
    opt_in: str = ""
    noncommercial_deployment: bool = False

    # SerpApi migration: which fairserp engine answers a SerpApi engine name such as
    # "google". Empty by default: fairserp does not query Google and will not pretend to.
    serpapi_engines: str = ""
    organic_priority: str = "brave,mojeek,marginalia,tavily,exa,parallel,jina,firecrawl,searxng"
    research_fanout: int = 3
    # Keyless enrichment sources queried alongside organic results (page 1 only).
    enrichers: str = "wikidata,duckduckgo_ia"
    strict_params: bool = False

    # Time budgets.
    request_deadline_seconds: float = 15.0
    enrichment_budget_seconds: float = 2.5

    # Upstream budgets shared by the whole server, "engine:N/window" or per key
    # "engine:N/window/key". Windows: hour, day, month.
    upstream_budgets: str = "firecrawl:900/month,firecrawl:100/day/key"
    circuit_failures: int = 5
    circuit_cooldown_seconds: int = 30

    # Engine credentials and endpoints.
    brave_api_key: str = ""
    mojeek_api_key: str = ""
    tavily_api_key: str = ""
    exa_api_key: str = ""
    parallel_api_key: str = ""
    jina_api_key: str = ""
    firecrawl_api_key: str = ""
    firecrawl_url: str = "https://api.firecrawl.dev"
    marginalia_api_key: str = "public"
    marginalia_url: str = "https://api.marginalia.nu"
    # True only if you hold a commercial Marginalia key; free keys are CC-BY-NC-SA.
    marginalia_commercial_key: bool = False
    searxng_url: str = ""
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    wikipedia_lang: str = "en"

    # Lower (never raise) an engine's storage limit, e.g. "mojeek:600".
    engine_cache_ttl: str = ""
    log_queries: bool = True

    @field_validator("api_keys")
    @classmethod
    def _check_api_keys(cls, value: str) -> str:
        for entry in _split(value):
            if not _KEY_ENTRY.match(entry):
                raise ValueError(f"API key entries look like key[:hourly[:monthly]], got {entry!r}")
        return value

    @field_validator("upstream_budgets")
    @classmethod
    def _check_budgets(cls, value: str) -> str:
        for entry in _split(value):
            if not _BUDGET_ENTRY.match(entry):
                raise ValueError(f"budget entries look like engine:N/day[/key], got {entry!r}")
        return value

    def parsed_api_keys(self) -> dict[str, KeyLimits]:
        keys: dict[str, KeyLimits] = {}
        for entry in _split(self.api_keys):
            key, *limits = entry.split(":")
            hourly = int(limits[0]) if limits else self.default_hourly_limit
            monthly = int(limits[1]) if len(limits) > 1 else self.default_monthly_limit
            keys[key] = KeyLimits(hourly, monthly)
        return keys

    def parsed_budgets(self) -> list[Budget]:
        budgets = []
        for entry in _split(self.upstream_budgets):
            engine, limit, window, per_key = _BUDGET_ENTRY.match(entry).groups()
            budgets.append(Budget(engine, int(limit), window, bool(per_key)))
        return budgets

    def parsed_serpapi_engines(self) -> dict[str, str]:
        return dict(entry.split(":", 1) for entry in _split(self.serpapi_engines))

    def parsed_engine_cache_ttl(self) -> dict[str, int]:
        pairs = (entry.split(":", 1) for entry in _split(self.engine_cache_ttl))
        return {name: int(ttl) for name, ttl in pairs}

    def opt_in_set(self) -> set[str]:
        return set(_split(self.opt_in))

    def enricher_names(self) -> list[str]:
        return _split(self.enrichers)

    def priority(self) -> list[str]:
        return _split(self.organic_priority)


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
