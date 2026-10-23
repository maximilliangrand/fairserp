"""Firecrawl search API (keyless tier available). https://docs.firecrawl.dev/api-reference/endpoint/search

Provenance is undisclosed. Live results carry Google's ``srsltid`` click parameter and
Firecrawl's open-source fallback path scrapes DuckDuckGo HTML, so results may include
third-party SERP data. It is opt-in only and never a silent default.
"""

from typing import Any

from fairserp.canonical import strip_tracking
from fairserp.engines.base import Engine, EngineContext, require
from fairserp.model import EngineOutput, SearchQuery, WebResult
from fairserp.results import clean_text, snippet
from fairserp.rights import CommercialUse, Provenance, Rights


class FirecrawlEngine(Engine):
    name = "firecrawl"
    upstream = "https://api.firecrawl.dev/v2/search"
    supports = frozenset({"freshness", "location"})
    max_depth = 100
    fixture_verified = True
    rights = Rights(
        provenance=Provenance.UNDISCLOSED,
        commercial_use=CommercialUse.PER_AGREEMENT,
        max_storage_seconds=None,
        attribution=None,
        terms_url="https://www.firecrawl.dev/terms-of-service",
        terms_reviewed="2026-10-23",
        notes="Index provenance undisclosed; live results carry Google's srsltid parameter. "
              "Keyless tier is capped per IP.",
    )
    notes = "Works without a key (per-IP cap). Opt-in only because provenance is undisclosed."

    async def fetch(self, query: SearchQuery, ctx: EngineContext) -> Any:
        # No offset parameter upstream: request enough results to cover the page.
        body: dict[str, Any] = {"query": query.q,
                                "limit": min(query.offset + query.num, self.max_depth)}
        if query.freshness:
            body["tbs"] = f"qdr:{query.freshness.value}"
        if query.location:
            body["location"] = query.location
        headers = {}
        if ctx.settings.firecrawl_api_key:
            headers["Authorization"] = f"Bearer {ctx.settings.firecrawl_api_key}"
        url = f"{ctx.settings.firecrawl_url.rstrip('/')}/v2/search"
        return await self.get_json(ctx, "POST", url, json=body, headers=headers)

    def parse(self, raw: dict[str, Any], query: SearchQuery) -> EngineOutput:
        require(raw.get("success") is True, f"success={raw.get('success')!r}: {raw.get('error')}")
        data = raw.get("data")
        everything = data.get("web") if isinstance(data, dict) else data
        require(isinstance(everything, list), "missing data.web list")
        page = everything[query.offset: query.offset + query.num]
        web = [WebResult(url=strip_tracking(it["url"]), title=clean_text(it.get("title")),
                         snippet=snippet(it.get("description"), query.q))
               for it in page]
        return EngineOutput(web=web,
                            has_more=len(everything) >= query.offset + query.num
                            and query.offset + query.num < self.max_depth)
