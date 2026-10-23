"""Wikipedia search and page summaries (official MediaWiki APIs, keyless).

Wikimedia asks API clients to send a descriptive User-Agent, set FAIRSERP_USER_AGENT
with a contact address for production use.
"""

import re
from typing import Any
from urllib.parse import quote

from fairserp.engines.base import Compliance, Engine, EngineContext, Fragments, Role, SearchRequest
from fairserp.results import organic_result

# The language code becomes part of the upstream hostname, so it must be strictly a code.
LANG = re.compile(r"[a-z]{2,3}")


class WikipediaEngine(Engine):
    name = "wikipedia"
    role = Role.ENRICHMENT
    compliance = Compliance.OPEN_DATA_API
    upstream = "https://{lang}.wikipedia.org/w/api.php"
    notes = "No key. Wikipedia articles as organic results plus an entity knowledge graph."

    async def fetch(self, request: SearchRequest, ctx: EngineContext) -> Any:
        lang = (request.hl or ctx.settings.wikipedia_lang).split("-")[0].lower()
        if not LANG.fullmatch(lang):
            lang = ctx.settings.wikipedia_lang
        base = f"https://{lang}.wikipedia.org"
        params = {
            "action": "query", "list": "search", "srsearch": request.q, "format": "json",
            "srlimit": request.num, "sroffset": request.start, "srprop": "snippet|timestamp",
        }
        search = await self.get_json(ctx, "GET", f"{base}/w/api.php", params=params)
        hits = search.get("query", {}).get("search", [])
        summary = None
        if hits and _same_entity(hits[0]["title"], request.q):
            title = quote(hits[0]["title"].replace(" ", "_"), safe="")
            summary = await self.get_json(ctx, "GET", f"{base}/api/rest_v1/page/summary/{title}")
        return {"base": base, "search": search, "summary": summary}

    def parse(self, raw: dict[str, Any], request: SearchRequest) -> Fragments:
        base = raw["base"]
        query = raw["search"].get("query", {})
        results = [
            organic_result(
                index,
                hit["title"],
                f"{base}/wiki/{quote(hit['title'].replace(' ', '_'), safe='')}",
                hit.get("snippet"),
                request.q,
                source="Wikipedia",
                date=(hit.get("timestamp") or "")[:10],
            )
            for index, hit in enumerate(query.get("search", []), start=1)
        ]
        fragments: Fragments = {"organic_results": results}
        total = query.get("searchinfo", {}).get("totalhits")
        if total is not None:
            fragments["search_information"] = {"total_results": total}
        summary = raw.get("summary")
        if summary and summary.get("type") == "standard":
            fragments["knowledge_graph"] = _knowledge_graph(summary)
        return fragments


def _same_entity(title: str, query: str) -> bool:
    return title.casefold().strip() == query.casefold().strip()


def _knowledge_graph(summary: dict[str, Any]) -> dict[str, Any]:
    page = summary.get("content_urls", {}).get("desktop", {}).get("page", "")
    graph: dict[str, Any] = {
        "title": summary.get("title", ""),
        "type": (summary.get("description") or "").split(" (")[0].capitalize(),
        "description": summary.get("extract", ""),
        "source": {"name": "Wikipedia", "link": page},
    }
    if summary.get("wikibase_item"):
        graph["wikidata_id"] = summary["wikibase_item"]
    image = (summary.get("thumbnail") or {}).get("source")
    if image:
        graph["header_images"] = [{"image": image, "source": page}]
    return {k: v for k, v in graph.items() if v}
