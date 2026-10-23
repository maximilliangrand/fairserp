"""Firecrawl search API (official, keyless tier). https://docs.firecrawl.dev/api-reference/endpoint/search"""

from typing import Any

from fairserp.engines.base import Engine, EngineContext, Fragments, SearchRequest
from fairserp.results import organic_result


class FirecrawlEngine(Engine):
    name = "firecrawl"
    upstream = "https://api.firecrawl.dev/v2/search"
    notes = "Works with no key (Firecrawl Keyless, per-IP daily cap). Key raises limits."

    async def fetch(self, request: SearchRequest, ctx: EngineContext) -> Any:
        # Firecrawl has no offset parameter: request enough results to cover the page.
        body: dict[str, Any] = {"query": request.q, "limit": min(request.start + request.num, 100)}
        if request.gl:
            body["country"] = request.gl.upper()
        if request.location:
            body["location"] = request.location
        headers = {}
        if ctx.settings.firecrawl_api_key:
            headers["Authorization"] = f"Bearer {ctx.settings.firecrawl_api_key}"
        url = f"{ctx.settings.firecrawl_url.rstrip('/')}/v2/search"
        return await self.get_json(ctx, "POST", url, json=body, headers=headers)

    def parse(self, raw: dict[str, Any], request: SearchRequest) -> Fragments:
        data = raw.get("data") or {}
        web = data.get("web", []) if isinstance(data, dict) else data
        page = web[request.start : request.start + request.num]
        return {
            "organic_results": [
                organic_result(i, item.get("title", ""), item.get("url", ""),
                               item.get("description"), request.q)
                for i, item in enumerate(page, start=1)
            ]
        }
