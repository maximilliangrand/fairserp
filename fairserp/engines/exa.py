"""Exa search API. https://docs.exa.ai/reference/search

Parser written from the published schema; no key was available to record a fixture.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from fairserp.canonical import strip_tracking
from fairserp.engines.base import Engine, EngineContext, require
from fairserp.model import EngineOutput, Freshness, SearchQuery, WebResult
from fairserp.results import clean_text, snippet
from fairserp.rights import CommercialUse, Provenance, Rights

WINDOW = {Freshness.HOUR: timedelta(hours=1), Freshness.DAY: timedelta(days=1),
          Freshness.WEEK: timedelta(weeks=1), Freshness.MONTH: timedelta(days=31),
          Freshness.YEAR: timedelta(days=366)}


class ExaEngine(Engine):
    name = "exa"
    upstream = "https://api.exa.ai/search"
    required_settings = ("exa_api_key",)
    supports = frozenset({"country", "freshness"})
    max_depth = 100
    cost_per_call_usd = 0.007
    rights = Rights(
        provenance=Provenance.UNDISCLOSED,
        commercial_use=CommercialUse.PER_AGREEMENT,
        max_storage_seconds=None,
        attribution=None,
        terms_url="https://exa.ai/terms",
        terms_reviewed="2026-10-23",
        notes="Neural search over its own crawl, but sources are not documented per result.",
    )
    notes = "Free tier without a card ($10/month credit). Opt-in. Fixture unverified."

    async def fetch(self, query: SearchQuery, ctx: EngineContext) -> Any:
        body: dict[str, Any] = {"query": query.q, "type": "auto",
                                "numResults": min(query.offset + query.num, self.max_depth),
                                "contents": {"highlights": {"numSentences": 2}}}
        if query.country:
            body["userLocation"] = query.country.upper()
        if query.freshness:
            since = datetime.now(UTC) - WINDOW[query.freshness]
            body["startPublishedDate"] = since.isoformat(timespec="seconds")
        headers = {"x-api-key": ctx.settings.exa_api_key}
        return await self.get_json(ctx, "POST", self.upstream, json=body, headers=headers)

    def parse(self, raw: dict[str, Any], query: SearchQuery) -> EngineOutput:
        require(isinstance(raw.get("results"), list), "missing results list")
        everything = raw["results"]
        page = everything[query.offset: query.offset + query.num]
        web = [WebResult(url=strip_tracking(item["url"]), title=clean_text(item.get("title")),
                         snippet=snippet(" ".join(item.get("highlights") or [])
                                         or item.get("text") or item.get("summary"), query.q),
                         published_at=(item.get("publishedDate") or "")[:10] or None)
               for item in page]
        return EngineOutput(web=web, has_more=len(everything) >= query.offset + query.num
                            and query.offset + query.num < self.max_depth)
