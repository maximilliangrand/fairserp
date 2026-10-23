"""Bring-your-own SearXNG instance, normalized to the SerpApi shape.

The instance's operator decides which upstream engines it queries, so the legal status
of results depends entirely on that instance's settings.yml.
"""

from typing import Any

from fairserp.engines.base import Compliance, Engine, EngineContext, Fragments, SearchRequest
from fairserp.results import clean_snippet, organic_result


class SearxngEngine(Engine):
    name = "searxng"
    compliance = Compliance.USER_OPERATED
    upstream = "your SearXNG instance (search.formats must include json)"
    required_settings = ("searxng_url",)
    notes = "Status depends on the engines enabled in your instance. See README."

    async def fetch(self, request: SearchRequest, ctx: EngineContext) -> Any:
        params: dict[str, Any] = {"q": request.q, "format": "json", "pageno": request.page}
        if request.hl:
            params["language"] = request.hl
        if request.safe:
            params["safesearch"] = 2 if request.safe == "active" else 0
        url = f"{ctx.settings.searxng_url.rstrip('/')}/search"
        return await self.get_json(ctx, "GET", url, params=params)

    def parse(self, raw: dict[str, Any], request: SearchRequest) -> Fragments:
        results = raw.get("results") or []
        fragments: Fragments = {
            "organic_results": [
                organic_result(i, item.get("title", ""), item.get("url", ""),
                               item.get("content"), request.q,
                               date=item.get("publishedDate"),
                               thumbnail=item.get("thumbnail"))
                for i, item in enumerate(results[: request.num], start=1)
            ]
        }
        boxes = raw.get("infoboxes") or []
        if boxes:
            box = boxes[0]
            graph = {
                "title": box.get("infobox", ""),
                "description": clean_snippet(box.get("content")),
            }
            if box.get("img_src"):
                graph["header_images"] = [{"image": box["img_src"], "source": box.get("id", "")}]
            for attribute in box.get("attributes") or []:
                key = "_".join(str(attribute.get("label", "")).lower().split())
                if key and attribute.get("value"):
                    graph.setdefault(key, attribute["value"])
            fragments["knowledge_graph"] = graph
        if raw.get("answers"):
            answer = raw["answers"][0]
            text = answer.get("answer") if isinstance(answer, dict) else answer
            fragments["answer_box"] = {"type": "organic_result", "snippet": clean_snippet(text)}
        if raw.get("suggestions"):
            fragments["related_searches"] = [{"query": s} for s in raw["suggestions"]]
        if raw.get("number_of_results"):
            fragments["search_information"] = {"total_results": int(raw["number_of_results"])}
        return fragments
