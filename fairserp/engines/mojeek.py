"""Mojeek Web Search API. https://www.mojeek.com/support/api/search/request_parameters.html"""

from datetime import UTC, datetime
from typing import Any

from fairserp.canonical import strip_tracking
from fairserp.engines.base import Engine, EngineContext, require
from fairserp.model import EngineOutput, SearchQuery, WebResult
from fairserp.results import clean_text, snippet
from fairserp.rights import CommercialUse, Provenance, Rights


class MojeekEngine(Engine):
    name = "mojeek"
    upstream = "https://api.mojeek.com/search"
    required_settings = ("mojeek_api_key",)
    supports = frozenset({"country", "language", "safe"})
    max_depth = 1000
    cost_per_call_usd = 0.003
    rights = Rights(
        provenance=Provenance.OWN_INDEX,
        commercial_use=CommercialUse.ALLOWED,
        max_storage_seconds=3600,
        attribution=None,
        terms_url="https://www.mojeek.com/services/search/web-search-api/",
        terms_reviewed="2026-10-23",
        notes="Entry plans allow caching for up to one hour; longer storage needs a plan "
              "that grants storage rights.",
    )
    notes = "Paid key (bank card to open an account). Independent index."

    async def fetch(self, query: SearchQuery, ctx: EngineContext) -> Any:
        params: dict[str, Any] = {"q": query.q, "api_key": ctx.settings.mojeek_api_key,
                                  "fmt": "json", "s": query.offset + 1, "t": query.num}
        if query.country:
            params["rb"] = query.country.upper()
        if query.language:
            params["lb"] = query.language.upper()
        if query.safe:
            params["safe"] = 1
        return await self.get_json(ctx, "GET", self.upstream, params=params)

    def parse(self, raw: dict[str, Any], query: SearchQuery) -> EngineOutput:
        response = raw.get("response")
        require(isinstance(response, dict), "missing response object")
        require(response.get("status") == "OK", f"status {response.get('status')!r}")
        web = [
            WebResult(url=strip_tracking(item["url"]), title=clean_text(item.get("title")),
                      snippet=snippet(item.get("desc"), query.q),
                      published_at=_date(item.get("date")))
            for item in response.get("results") or []
        ]
        total = (response.get("head") or {}).get("results")
        return EngineOutput(
            web=web, total_results=int(total) if total else None,
            has_more=bool(total) and query.offset + query.num < min(int(total or 0),
                                                                     self.max_depth),
        )


def _date(value: Any) -> str | None:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, UTC).date().isoformat()
    return value or None
