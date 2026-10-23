"""Brave Search API (official). https://api-dashboard.search.brave.com/app/documentation"""

from typing import Any

from fairserp.engines.base import Engine, EngineContext, Fragments, SearchRequest
from fairserp.results import clean_snippet, displayed_link, organic_result

SAFE = {"active": "strict", "off": "off"}


class BraveEngine(Engine):
    name = "brave"
    upstream = "https://api.search.brave.com/res/v1/web/search"
    required_settings = ("brave_api_key",)
    # Brave's API terms limit storing results unless the plan grants storage rights.
    default_cache_ttl = 0
    notes = "Paid key required (card on file, $5/month free credit). Caching off by default."

    async def fetch(self, request: SearchRequest, ctx: EngineContext) -> Any:
        params: dict[str, Any] = {
            "q": request.q,
            "count": min(request.num, 20),
            "offset": min(request.start // max(request.num, 1), 9),
        }
        if request.gl:
            params["country"] = request.gl.lower()
        if request.hl:
            params["search_lang"] = request.hl.split("-")[0].lower()
        if request.safe in SAFE:
            params["safesearch"] = SAFE[request.safe]
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": ctx.settings.brave_api_key,
        }
        return await self.get_json(ctx, "GET", self.upstream, params=params, headers=headers)

    def parse(self, raw: dict[str, Any], request: SearchRequest) -> Fragments:
        fragments: Fragments = {"organic_results": []}
        for index, item in enumerate(raw.get("web", {}).get("results", []), start=1):
            profile = item.get("profile") or {}
            meta = item.get("meta_url") or {}
            result = organic_result(
                index,
                item.get("title", ""),
                item.get("url", ""),
                item.get("description"),
                request.q,
                date=item.get("age"),
                favicon=meta.get("favicon"),
                thumbnail=(item.get("thumbnail") or {}).get("src"),
            )
            if profile.get("name"):
                result["source"] = profile["name"]
            sitelinks = [
                {"title": b.get("title", ""), "link": b.get("url", "")}
                for b in (item.get("deep_results") or {}).get("buttons", [])
            ]
            if sitelinks:
                result["sitelinks"] = {"inline": sitelinks}
            fragments["organic_results"].append(result)

        questions = [
            {
                "question": faq.get("question", ""),
                "snippet": clean_snippet(faq.get("answer")),
                "title": faq.get("title", ""),
                "link": faq.get("url", ""),
                "displayed_link": displayed_link(faq.get("url", "")),
            }
            for faq in raw.get("faq", {}).get("results", [])
        ]
        if questions:
            fragments["related_questions"] = questions

        infobox = (raw.get("infobox") or {}).get("results") or []
        if infobox:
            fragments["knowledge_graph"] = _knowledge_graph(infobox[0])

        places = (raw.get("locations") or {}).get("results") or []
        if places:
            fragments["local_results"] = {"places": [_place(i, p) for i, p in enumerate(places, 1)]}
        return fragments


def _knowledge_graph(box: dict[str, Any]) -> dict[str, Any]:
    graph: dict[str, Any] = {
        "title": box.get("title", ""),
        "type": box.get("category") or box.get("subtype") or "",
        "description": clean_snippet(box.get("long_desc") or box.get("description")),
    }
    if box.get("url"):
        graph["source"] = {"name": displayed_link(box["url"]).split(" ")[0], "link": box["url"]}
    image = (box.get("thumbnail") or {}).get("original") or (box.get("thumbnail") or {}).get("src")
    if image:
        graph["header_images"] = [{"image": image, "source": box.get("url", "")}]
    for label, value in box.get("attributes") or []:
        graph.setdefault(_snake(label), value)
    return {k: v for k, v in graph.items() if v}


def _place(position: int, place: dict[str, Any]) -> dict[str, Any]:
    coords = place.get("coordinates") or [None, None]
    rating = place.get("rating") or {}
    postal = place.get("postal_address") or {}
    result = {
        "position": position,
        "title": place.get("title", ""),
        "place_id": place.get("id", ""),
        "type": ", ".join(place.get("categories") or []),
        "rating": rating.get("ratingValue"),
        "reviews": rating.get("reviewCount"),
        "address": postal.get("displayAddress"),
        "phone": (place.get("contact") or {}).get("telephone"),
        "gps_coordinates": {"latitude": coords[0], "longitude": coords[1]},
        "links": {"website": place.get("url")} if place.get("url") else None,
    }
    return {k: v for k, v in result.items() if v not in (None, "", {})}


def _snake(label: str) -> str:
    return "_".join(label.lower().replace("-", " ").split())
