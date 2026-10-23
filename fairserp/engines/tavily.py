"""Tavily Search API. https://docs.tavily.com/documentation/api-reference/endpoint/search"""

from typing import Any

from fairserp.canonical import strip_tracking
from fairserp.engines.base import Engine, EngineContext, require
from fairserp.model import Answer, EngineOutput, Freshness, SearchQuery, WebResult
from fairserp.results import clean_text, snippet
from fairserp.rights import CommercialUse, Provenance, Rights

TIME_RANGE = {Freshness.DAY: "day", Freshness.WEEK: "week", Freshness.MONTH: "month",
              Freshness.YEAR: "year"}
# Tavily takes full lowercase country names. Codes outside this table are reported ignored.
COUNTRY_NAMES = {
    "us": "united states", "gb": "united kingdom", "de": "germany", "fr": "france",
    "es": "spain", "it": "italy", "nl": "netherlands", "at": "austria", "ch": "switzerland",
    "ca": "canada", "au": "australia", "in": "india", "jp": "japan", "br": "brazil",
    "mx": "mexico", "se": "sweden", "no": "norway", "dk": "denmark", "pl": "poland",
    "ie": "ireland", "be": "belgium", "pt": "portugal", "kr": "south korea", "sg": "singapore",
}


class TavilyEngine(Engine):
    name = "tavily"
    upstream = "https://api.tavily.com/search"
    required_settings = ("tavily_api_key",)
    supports = frozenset({"country", "freshness"})
    max_depth = 20
    cost_per_call_usd = 0.008
    rights = Rights(
        provenance=Provenance.UNDISCLOSED,
        commercial_use=CommercialUse.PER_AGREEMENT,
        max_storage_seconds=None,
        attribution=None,
        terms_url="https://tavily.com/terms",
        terms_reviewed="2026-10-23",
        notes="Does not disclose where results come from. Its answer field is model-generated.",
    )
    notes = "Free key without a card (1,000 credits/month). Opt-in only."

    def applied(self, query: SearchQuery) -> set[str]:
        applied = query.requested_params() & self.supports
        if query.country and query.country not in COUNTRY_NAMES:
            applied.discard("country")
        if query.freshness == Freshness.HOUR:
            applied.discard("freshness")
        return applied

    async def fetch(self, query: SearchQuery, ctx: EngineContext) -> Any:
        body: dict[str, Any] = {"query": query.q, "include_answer": True,
                                "max_results": min(query.offset + query.num, self.max_depth)}
        if query.country in COUNTRY_NAMES:
            body["country"] = COUNTRY_NAMES[query.country]
        if query.freshness in TIME_RANGE:
            body["time_range"] = TIME_RANGE[query.freshness]
        headers = {"Authorization": f"Bearer {ctx.settings.tavily_api_key}"}
        return await self.get_json(ctx, "POST", self.upstream, json=body, headers=headers)

    def parse(self, raw: dict[str, Any], query: SearchQuery) -> EngineOutput:
        require(isinstance(raw.get("results"), list), "missing results list")
        everything = raw["results"]
        page = everything[query.offset: query.offset + query.num]
        web = [WebResult(url=strip_tracking(it["url"]), title=clean_text(it.get("title")),
                         snippet=snippet(it.get("content"), query.q),
                         published_at=it.get("published_date"))
               for it in page]
        answer = None
        if raw.get("answer") and query.offset == 0:
            answer = Answer(text=raw["answer"], type="generated_summary", generated=True,
                            citations=[r.url for r in web[:5]])
        return EngineOutput(web=web, answer=answer,
                            has_more=len(everything) >= query.offset + query.num
                            and query.offset + query.num < self.max_depth)
