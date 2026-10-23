"""Runtime configuration, read from FAIRSERP_* environment variables."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FAIRSERP_", env_file=".env", extra="ignore")

    # Public base URL used to build search_metadata.json_endpoint links.
    public_url: str = "http://localhost:8000"

    # Comma separated "key" or "key:hourly_limit" entries. Empty plus auth_disabled=False
    # means every request is rejected, so a fresh deploy is never an open relay.
    api_keys: str = ""
    auth_disabled: bool = False
    default_hourly_limit: int = 1000

    # "memory" or a redis:// URL. Redis is shared by cache, rate limits and the archive.
    backend: str = "memory"
    cache_ttl_seconds: int = 3600
    archive_ttl_seconds: int = 31 * 24 * 3600

    # Outbound HTTP.
    user_agent: str = "fairserp/0.1 (+https://github.com/maximilliangrand/fairserp)"
    http_timeout_seconds: float = 15.0
    # Comma separated proxy URLs, rotated round robin. Per engine override as JSON, e.g.
    # FAIRSERP_ENGINE_PROXIES='{"brave": "http://proxy:3128", "wikipedia": "none"}'.
    proxies: str = ""
    engine_proxies: dict[str, str] = Field(default_factory=dict)

    # Which engine serves SerpApi's engine=google (and other aliases). "auto" picks the
    # first configured organic provider and enriches it with the open data sources.
    engine_aliases: str = "google:auto,bing:auto,duckduckgo:auto"
    organic_priority: str = "brave,mojeek,tavily,firecrawl,marginalia,searxng,wikipedia"

    # Engine credentials and endpoints. Nothing here is required for the keyless defaults.
    brave_api_key: str = ""
    mojeek_api_key: str = ""
    tavily_api_key: str = ""
    firecrawl_api_key: str = ""
    firecrawl_url: str = "https://api.firecrawl.dev"
    marginalia_api_key: str = "public"
    marginalia_url: str = "https://api.marginalia.nu"
    searxng_url: str = ""
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    wikipedia_lang: str = "en"

    # Engine-specific cache policy overrides, e.g. "brave:0" when an upstream's terms
    # forbid storing results. Engines also declare their own defaults.
    engine_cache_ttl: str = ""

    def parsed_api_keys(self) -> dict[str, int]:
        keys: dict[str, int] = {}
        for entry in _split(self.api_keys):
            key, _, limit = entry.partition(":")
            keys[key] = int(limit) if limit else self.default_hourly_limit
        return keys

    def parsed_aliases(self) -> dict[str, str]:
        return dict(entry.split(":", 1) for entry in _split(self.engine_aliases))

    def parsed_engine_cache_ttl(self) -> dict[str, int]:
        pairs = (entry.split(":", 1) for entry in _split(self.engine_cache_ttl))
        return {name: int(ttl) for name, ttl in pairs}

    def proxy_list(self) -> list[str]:
        return _split(self.proxies)


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
