"""Tavily Search API (official). https://docs.tavily.com/documentation/api-reference/endpoint/search"""

from typing import Any

from fairserp.engines.base import Engine, EngineContext, Fragments, SearchRequest
from fairserp.results import organic_result


class TavilyEngine(Engine):
    name = "tavily"
    upstream = "https://api.tavily.com/search"
    required_settings = ("tavily_api_key",)
    notes = "Free key without a card (1,000 credits/month). LLM-oriented ranking."

    async def fetch(self, request: SearchRequest, ctx: EngineContext) -> Any:
        body: dict[str, Any] = {
            "query": request.q,
            "max_results": min(request.start + request.num, 20),
            "include_answer": True,
        }
        if request.gl and len(request.gl) > 2:
            body["country"] = request.gl
        headers = {"Authorization": f"Bearer {ctx.settings.tavily_api_key}"}
        return await self.get_json(ctx, "POST", self.upstream, json=body, headers=headers)

    def parse(self, raw: dict[str, Any], request: SearchRequest) -> Fragments:
        # Tavily has no offset, so pages are sliced out of a larger result set.
        items = (raw.get("results") or [])[request.start : request.start + request.num]
        fragments: Fragments = {
            "organic_results": [
                organic_result(i, it.get("title", ""), it.get("url", ""), it.get("content"),
                               request.q, date=it.get("published_date"))
                for i, it in enumerate(items, start=1)
            ]
        }
        if raw.get("answer") and request.start == 0:
            fragments["answer_box"] = {"type": "organic_result", "snippet": raw["answer"]}
        return fragments
