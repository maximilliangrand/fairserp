"""Parallel Search API. https://docs.parallel.ai/search/search-quickstart

Parser written from the published schema; no key was available to record a fixture.
"""

from typing import Any

from fairserp.canonical import strip_tracking
from fairserp.engines.base import Engine, EngineContext, require
from fairserp.model import EngineOutput, SearchQuery, WebResult
from fairserp.results import clean_text, snippet
from fairserp.rights import CommercialUse, Provenance, Rights


class ParallelEngine(Engine):
    name = "parallel"
    upstream = "https://api.parallel.ai/v1beta/search"
    required_settings = ("parallel_api_key",)
    max_depth = 40
    cost_per_call_usd = 0.005
    rights = Rights(
        provenance=Provenance.UNDISCLOSED,
        commercial_use=CommercialUse.PER_AGREEMENT,
        max_storage_seconds=None,
        attribution=None,
        terms_url="https://parallel.ai/terms-of-service",
        terms_reviewed="2026-10-23",
        notes="Agent-oriented search with query-relevant excerpts; index sources undisclosed.",
    )
    notes = "Key required. Opt-in. Fixture unverified."

    async def fetch(self, query: SearchQuery, ctx: EngineContext) -> Any:
        body = {"objective": query.q, "search_queries": [query.q],
                "max_results": min(query.offset + query.num, self.max_depth),
                "max_chars_per_result": 1500}
        headers = {"x-api-key": ctx.settings.parallel_api_key}
        return await self.get_json(ctx, "POST", self.upstream, json=body, headers=headers)

    def parse(self, raw: dict[str, Any], query: SearchQuery) -> EngineOutput:
        require(isinstance(raw.get("results"), list), "missing results list")
        everything = raw["results"]
        page = everything[query.offset: query.offset + query.num]
        web = [WebResult(url=strip_tracking(item["url"]), title=clean_text(item.get("title")),
                         snippet=snippet(" ".join(item.get("excerpts") or []), query.q),
                         published_at=item.get("publish_date"))
               for item in page]
        return EngineOutput(web=web, has_more=len(everything) >= query.offset + query.num
                            and query.offset + query.num < self.max_depth)
