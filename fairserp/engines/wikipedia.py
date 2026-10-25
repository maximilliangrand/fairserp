"""Wikipedia full-text search (MediaWiki Action API, keyless).

Wikimedia asks API clients to send a descriptive User-Agent with contact details; set
FAIRSERP_USER_AGENT for production use.
"""

import re
from typing import Any
from urllib.parse import quote

from fairserp.engines.base import Engine, EngineContext, require
from fairserp.model import EngineOutput, SearchQuery, WebResult
from fairserp.results import clean_text, snippet
from fairserp.rights import CommercialUse, Provenance, Rights

# The language code becomes part of the upstream hostname, so it must strictly be a code.
LANG = re.compile(r"[a-z]{2,3}")

WIKIPEDIA_RIGHTS = Rights(
    provenance=Provenance.OPEN_DATA,
    commercial_use=CommercialUse.ALLOWED,
    max_storage_seconds=None,
    attribution="Text from Wikipedia, CC BY-SA 4.0",
    terms_url="https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use",
    terms_reviewed="2026-10-23",
    notes="Share-alike applies to reused text. API etiquette requires an identifying UA.",
)


def wiki_lang(query: SearchQuery, default: str) -> str:
    lang = (query.language or default).lower()
    return lang if LANG.fullmatch(lang) else default


class WikipediaEngine(Engine):
    name = "wikipedia"
    upstream = "https://{lang}.wikipedia.org/w/api.php"
    supports = frozenset({"language"})
    max_depth = 500
    fixture_verified = True
    rights = WIKIPEDIA_RIGHTS
    notes = "No key. Wikipedia articles only; useful directly, not as a general web engine."

    async def fetch(self, query: SearchQuery, ctx: EngineContext) -> Any:
        base = f"https://{wiki_lang(query, ctx.settings.wikipedia_lang)}.wikipedia.org"
        params = {
            "action": "query", "list": "search", "srsearch": query.q, "format": "json",
            "srlimit": query.num, "sroffset": query.offset, "srprop": "snippet|timestamp",
        }
        return {"base": base,
                "search": await self.get_json(ctx, "GET", f"{base}/w/api.php", params=params)}

    def parse(self, raw: dict[str, Any], query: SearchQuery) -> EngineOutput:
        result = raw["search"].get("query")
        require(isinstance(result, dict), "missing query object")
        hits = result.get("search")
        require(isinstance(hits, list), "missing search list")
        web = [
            WebResult(
                url=f"{raw['base']}/wiki/{quote(hit['title'].replace(' ', '_'), safe='')}",
                title=hit["title"],
                snippet=snippet(clean_text(hit.get("snippet")), query.q),
                # The search API reports the last edit, which is an update, not publication.
                updated_at=(hit.get("timestamp") or "")[:10] or None,
                source_name="Wikipedia",
            )
            for hit in hits
        ]
        total = (result.get("searchinfo") or {}).get("totalhits")
        return EngineOutput(web=web, total_results=total,
                            has_more=bool(total) and query.offset + query.num < min(total,
                                                                                     self.max_depth))
