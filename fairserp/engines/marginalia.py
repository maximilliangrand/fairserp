"""Marginalia Search API, an independent small-web index. https://about.marginalia-search.com/article/api"""

from dataclasses import replace
from typing import Any
from urllib.parse import quote

from fairserp.canonical import strip_tracking
from fairserp.config import Settings
from fairserp.engines.base import Engine, EngineContext, EngineError, ErrorKind, require
from fairserp.model import EngineOutput, SearchQuery, WebResult
from fairserp.results import clean_text, snippet
from fairserp.rights import CommercialUse, Provenance, Rights

FREE_KEY_RIGHTS = Rights(
    provenance=Provenance.OWN_INDEX,
    commercial_use=CommercialUse.NONCOMMERCIAL,
    max_storage_seconds=None,
    attribution="Marginalia Search, CC-BY-NC-SA 4.0",
    terms_url="https://about.marginalia-search.com/article/api/",
    terms_reviewed="2026-10-23",
    notes="Free and shared 'public' keys license results CC-BY-NC-SA 4.0 (noncommercial).",
)


class MarginaliaEngine(Engine):
    name = "marginalia"
    upstream = "https://api.marginalia.nu"
    required_settings = ("marginalia_api_key",)
    max_depth = 100
    fixture_verified = True
    rights = FREE_KEY_RIGHTS
    notes = "Shared 'public' key is often exhausted; free personal key by email (noncommercial)."

    def rights_for(self, settings: Settings) -> Rights:
        if settings.marginalia_commercial_key:
            return replace(FREE_KEY_RIGHTS, commercial_use=CommercialUse.PER_AGREEMENT,
                           attribution="Marginalia Search",
                           notes="Commercial key declared by the operator.")
        return FREE_KEY_RIGHTS

    async def fetch(self, query: SearchQuery, ctx: EngineContext) -> Any:
        if not query.q.strip(". "):
            # The query becomes a path segment; "." and ".." would rewrite the upstream path.
            raise EngineError(self.name, "query cannot be only dots", ErrorKind.REJECTED)
        base = ctx.settings.marginalia_url.rstrip("/")
        key = quote(ctx.settings.marginalia_api_key, safe="")
        url = f"{base}/{key}/search/{quote(query.q, safe='')}"
        params = {"count": min(query.offset + query.num, self.max_depth)}
        return await self.get_json(ctx, "GET", url, params=params)

    def parse(self, raw: dict[str, Any], query: SearchQuery) -> EngineOutput:
        require(isinstance(raw.get("results"), list), "missing results list")
        everything = raw["results"]
        page = everything[query.offset: query.offset + query.num]
        web = [WebResult(url=strip_tracking(it["url"]), title=clean_text(it.get("title")),
                         snippet=snippet(it.get("description"), query.q))
               for it in page]
        return EngineOutput(web=web,
                            has_more=len(everything) >= query.offset + query.num
                            and query.offset + query.num < self.max_depth)
