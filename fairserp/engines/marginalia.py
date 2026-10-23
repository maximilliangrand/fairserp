"""Marginalia Search API (official, independent index). https://about.marginalia-search.com/article/api"""

from typing import Any
from urllib.parse import quote

from fairserp.engines.base import Engine, EngineContext, Fragments, SearchRequest
from fairserp.results import organic_result


class MarginaliaEngine(Engine):
    name = "marginalia"
    upstream = "https://api.marginalia.nu"
    required_settings = ("marginalia_api_key",)
    notes = "Shared 'public' key is often exhausted; free personal key by email. CC-BY-NC-SA."

    async def fetch(self, request: SearchRequest, ctx: EngineContext) -> Any:
        base = ctx.settings.marginalia_url.rstrip("/")
        key = quote(ctx.settings.marginalia_api_key, safe="")
        url = f"{base}/{key}/search/{quote(request.q, safe='')}"
        params = {"count": min(request.start + request.num, 100)}
        return await self.get_json(ctx, "GET", url, params=params)

    def parse(self, raw: dict[str, Any], request: SearchRequest) -> Fragments:
        page = (raw.get("results") or [])[request.start : request.start + request.num]
        return {
            "organic_results": [
                organic_result(i, item.get("title", ""), item.get("url", ""),
                               item.get("description"), request.q)
                for i, item in enumerate(page, start=1)
            ]
        }
