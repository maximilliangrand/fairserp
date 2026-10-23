"""DuckDuckGo Instant Answer API (official, keyless). https://api.duckduckgo.com/api

Returns entity abstracts, infoboxes and direct answers. It never returns organic links,
so it is used to enrich another engine's results rather than on its own.
"""

from typing import Any
from urllib.parse import unquote, urlsplit

from fairserp.engines.base import Compliance, Engine, EngineContext, Fragments, Role, SearchRequest
from fairserp.results import clean_snippet

DDG = "https://duckduckgo.com"


class DuckDuckGoInstantAnswerEngine(Engine):
    name = "duckduckgo_ia"
    role = Role.ENRICHMENT
    compliance = Compliance.OPEN_DATA_API
    upstream = "https://api.duckduckgo.com/"
    notes = "No key. Knowledge graph, answer box and related searches only."

    async def fetch(self, request: SearchRequest, ctx: EngineContext) -> Any:
        params = {"q": request.q, "format": "json", "no_html": 1, "skip_disambig": 1}
        return await self.get_json(ctx, "GET", self.upstream, params=params)

    def parse(self, raw: dict[str, Any], request: SearchRequest) -> Fragments:
        fragments: Fragments = {}
        if raw.get("Heading") and raw.get("AbstractText"):
            fragments["knowledge_graph"] = _knowledge_graph(raw)
        if raw.get("Answer"):
            fragments["answer_box"] = {
                "type": raw.get("AnswerType") or "answer",
                "answer": clean_snippet(str(raw["Answer"])),
            }
        elif raw.get("Definition"):
            fragments["answer_box"] = {
                "type": "dictionary_results",
                "syllables": raw.get("Heading", ""),
                "definitions": [raw["Definition"]],
                "source": raw.get("DefinitionURL", ""),
            }
        related = _related_searches(raw.get("RelatedTopics") or [])
        if related:
            fragments["related_searches"] = related
        return fragments


def _knowledge_graph(raw: dict[str, Any]) -> dict[str, Any]:
    graph: dict[str, Any] = {
        "title": raw["Heading"],
        "type": (raw.get("Entity") or "").capitalize(),
        "description": raw["AbstractText"],
        "source": {"name": raw.get("AbstractSource", ""), "link": raw.get("AbstractURL", "")},
    }
    if raw.get("OfficialWebsite"):
        graph["website"] = raw["OfficialWebsite"]
    if raw.get("Image"):
        image = raw["Image"]
        graph["header_images"] = [
            {"image": image if image.startswith("http") else DDG + image,
             "source": raw.get("AbstractURL", "")}
        ]
    infobox = raw.get("Infobox") or {}
    for item in infobox.get("content", []) if isinstance(infobox, dict) else []:
        if item.get("data_type") != "string" or not item.get("label"):
            continue
        key = "_".join(item["label"].lower().replace("-", " ").split())
        graph.setdefault(key, item.get("value"))
    return {k: v for k, v in graph.items() if v}


def _related_searches(topics: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    for topic in topics:
        url = topic.get("FirstURL") or ""
        path = urlsplit(url).path.strip("/")
        if not url.startswith(DDG) or not path or path.startswith("c/"):
            continue
        query = unquote(path).replace("_", " ")
        queries.append({"block_position": 1, "query": query})
        if len(queries) == limit:
            break
    return queries
