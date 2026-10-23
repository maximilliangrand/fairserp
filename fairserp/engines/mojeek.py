"""Mojeek Web Search API (official). https://www.mojeek.com/support/api/search/request_parameters.html"""

from typing import Any

from fairserp.engines.base import Engine, EngineContext, EngineError, Fragments, SearchRequest
from fairserp.results import organic_result


class MojeekEngine(Engine):
    name = "mojeek"
    upstream = "https://api.mojeek.com/search"
    required_settings = ("mojeek_api_key",)
    # Mojeek's entry plans allow caching for up to one hour.
    default_cache_ttl = 3600
    notes = "Paid key required (bank card to open an account). Independent index."

    async def fetch(self, request: SearchRequest, ctx: EngineContext) -> Any:
        params: dict[str, Any] = {
            "q": request.q,
            "api_key": ctx.settings.mojeek_api_key,
            "fmt": "json",
            "s": request.start + 1,
            "t": min(request.num, 100),
        }
        if request.gl:
            params["rb"] = request.gl.upper()
        if request.hl:
            params["lb"] = request.hl.split("-")[0].upper()
        if request.safe == "active":
            params["safe"] = 1
        return await self.get_json(ctx, "GET", self.upstream, params=params)

    def parse(self, raw: dict[str, Any], request: SearchRequest) -> Fragments:
        response = raw.get("response") or {}
        if response.get("status", "OK") != "OK":
            raise EngineError(self.name, str(response.get("status")))
        results = [
            organic_result(
                index,
                item.get("title", ""),
                item.get("url", ""),
                item.get("desc"),
                request.q,
                date=item.get("date"),
            )
            for index, item in enumerate(response.get("results") or [], start=1)
        ]
        fragments: Fragments = {"organic_results": results}
        total = (response.get("head") or {}).get("results")
        if total:
            fragments["search_information"] = {"total_results": int(total)}
        return fragments
