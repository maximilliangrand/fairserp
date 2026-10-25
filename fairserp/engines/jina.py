"""Jina search (s.jina.ai). https://jina.ai/reader/

Parser written from the published schema; no key was available to record a fixture.
"""

from typing import Any

from fairserp.canonical import strip_tracking
from fairserp.engines.base import Engine, EngineContext, require
from fairserp.model import EngineOutput, SearchQuery, WebResult
from fairserp.results import clean_text, snippet
from fairserp.rights import CommercialUse, Provenance, Rights


class JinaEngine(Engine):
    name = "jina"
    upstream = "https://s.jina.ai/"
    required_settings = ("jina_api_key",)
    supports = frozenset({"country", "language"})
    max_depth = 10
    rights = Rights(
        provenance=Provenance.UNDISCLOSED,
        commercial_use=CommercialUse.PER_AGREEMENT,
        max_storage_seconds=None,
        attribution=None,
        terms_url="https://jina.ai/legal",
        terms_reviewed="2026-10-23",
        notes="Search plus reader; the backing index is not disclosed.",
    )
    notes = "Key required. Opt-in. Fixture unverified. One page of results."

    async def fetch(self, query: SearchQuery, ctx: EngineContext) -> Any:
        params: dict[str, Any] = {"q": query.q}
        if query.country:
            params["gl"] = query.country
        if query.language:
            params["hl"] = query.language
        headers = {"Authorization": f"Bearer {ctx.settings.jina_api_key}",
                   "Accept": "application/json", "X-Respond-With": "no-content"}
        return await self.get_json(ctx, "GET", self.upstream, params=params, headers=headers)

    def parse(self, raw: dict[str, Any], query: SearchQuery) -> EngineOutput:
        require(raw.get("code") == 200 and isinstance(raw.get("data"), list),
                f"code {raw.get('code')!r}")
        page = raw["data"][query.offset: query.offset + query.num]
        web = [WebResult(url=strip_tracking(item["url"]), title=clean_text(item.get("title")),
                         snippet=snippet(item.get("description") or item.get("content"), query.q),
                         published_at=item.get("date") or None)
               for item in page]
        return EngineOutput(web=web, has_more=False)
