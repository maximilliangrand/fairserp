"""Brave Search API. https://api-dashboard.search.brave.com/app/documentation"""

from typing import Any

from fairserp.canonical import strip_tracking
from fairserp.engines.base import Engine, EngineContext, require
from fairserp.model import (
    EngineOutput,
    Entity,
    Freshness,
    Link,
    Place,
    Question,
    SearchQuery,
    WebResult,
)
from fairserp.results import clean_text, snippet
from fairserp.rights import CommercialUse, Provenance, Rights

FRESHNESS = {Freshness.DAY: "pd", Freshness.WEEK: "pw", Freshness.MONTH: "pm",
             Freshness.YEAR: "py"}


class BraveEngine(Engine):
    name = "brave"
    upstream = "https://api.search.brave.com/res/v1/web/search"
    required_settings = ("brave_api_key",)
    supports = frozenset({"country", "language", "safe", "freshness"})
    max_depth = 200  # 10 pages of up to 20
    cost_per_call_usd = 0.005
    rights = Rights(
        provenance=Provenance.OWN_INDEX,
        commercial_use=CommercialUse.PER_AGREEMENT,
        max_storage_seconds=0,
        attribution="Search results from Brave Search",
        terms_url="https://api-dashboard.search.brave.com/terms-of-service",
        terms_reviewed="2026-10-23",
        notes="Standard terms limit storage, redistribution and some AI uses; an order form "
              "can change this. Results are never cached or archived by default.",
    )
    notes = "Paid key (card on file, $5/month credit)."

    def applied(self, query: SearchQuery) -> set[str]:
        applied = query.requested_params() & self.supports
        if query.freshness == Freshness.HOUR:
            applied.discard("freshness")
        return applied

    async def fetch(self, query: SearchQuery, ctx: EngineContext) -> Any:
        count = min(query.num, 20)
        params: dict[str, Any] = {"q": query.q, "count": count,
                                  "offset": min(query.offset // count, 9)}
        if query.country:
            params["country"] = query.country
        if query.language:
            params["search_lang"] = query.language
        if query.safe:
            params["safesearch"] = "strict"
        if query.freshness in FRESHNESS:
            params["freshness"] = FRESHNESS[query.freshness]
        headers = {"Accept": "application/json",
                   "X-Subscription-Token": ctx.settings.brave_api_key}
        return await self.get_json(ctx, "GET", self.upstream, params=params, headers=headers)

    def parse(self, raw: dict[str, Any], query: SearchQuery) -> EngineOutput:
        require(isinstance(raw, dict) and raw.get("type") == "search", "not a Brave search body")
        web = []
        for item in (raw.get("web") or {}).get("results", []):
            require(item.get("url"), "web result without url")
            web.append(WebResult(
                url=strip_tracking(item["url"]),
                title=clean_text(item.get("title")),
                snippet=snippet(item.get("description"), query.q),
                published_at=item.get("page_age"),
                source_name=(item.get("profile") or {}).get("name"),
                favicon=(item.get("meta_url") or {}).get("favicon"),
                thumbnail=(item.get("thumbnail") or {}).get("src"),
                sitelinks=[Link(title=b.get("title", ""), url=b.get("url", ""))
                           for b in (item.get("deep_results") or {}).get("buttons", [])],
            ))
        questions = [
            Question(question=f.get("question", ""), answer=clean_text(f.get("answer")),
                     title=f.get("title", ""), url=f.get("url", ""))
            for f in (raw.get("faq") or {}).get("results", [])
        ]
        boxes = (raw.get("infobox") or {}).get("results") or []
        places = [_place(p) for p in (raw.get("locations") or {}).get("results", [])]
        more = (raw.get("query") or {}).get("more_results_available")
        return EngineOutput(
            web=web, questions=questions, places=places,
            entity=_entity(boxes[0]) if boxes else None,
            has_more=bool(more) and query.offset + query.num < self.max_depth,
        )


def _entity(box: dict[str, Any]) -> Entity:
    thumb = box.get("thumbnail") or {}
    return Entity(
        title=box.get("title", ""),
        type=box.get("category") or box.get("subtype") or "",
        description=clean_text(box.get("long_desc") or box.get("description")),
        url=box.get("url", ""),
        image=thumb.get("original") or thumb.get("src"),
        attributes={"_".join(str(k).lower().split()): str(v)
                    for k, v in box.get("attributes") or []},
    )


def _place(place: dict[str, Any]) -> Place:
    coords = place.get("coordinates") or [None, None]
    rating = place.get("rating") or {}
    return Place(
        name=place.get("title", ""),
        category=", ".join(place.get("categories") or []),
        address=(place.get("postal_address") or {}).get("displayAddress", ""),
        latitude=coords[0], longitude=coords[1],
        phone=(place.get("contact") or {}).get("telephone"),
        website=place.get("url"),
        rating=rating.get("ratingValue"), reviews=rating.get("reviewCount"),
        place_id=f"brave:{place.get('id', '')}",
    )
