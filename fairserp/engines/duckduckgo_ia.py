"""DuckDuckGo Instant Answer API (keyless). https://api.duckduckgo.com/api

Returns entity abstracts that name their source, and direct answers. It never returns
organic links, so it only enriches another engine's results.
"""

from typing import Any
from urllib.parse import unquote, urlsplit

from fairserp.engines.base import Engine, EngineContext, Role, require
from fairserp.model import Answer, EngineOutput, Entity, SearchQuery
from fairserp.results import clean_text
from fairserp.rights import CommercialUse, Provenance, Rights

DDG = "https://duckduckgo.com"


class DuckDuckGoInstantAnswerEngine(Engine):
    name = "duckduckgo_ia"
    role = Role.ENRICHMENT
    upstream = "https://api.duckduckgo.com/"
    fixture_verified = True
    rights = Rights(
        provenance=Provenance.AGGREGATOR,
        commercial_use=CommercialUse.UNSPECIFIED,
        max_storage_seconds=None,
        attribution="Instant answers from DuckDuckGo; abstracts name their own source",
        terms_url="https://duckduckgo.com/api",
        terms_reviewed="2026-10-23",
        notes="Each abstract states its source (usually Wikipedia).",
    )
    notes = "No key. Answers, entity abstracts and related topics only."

    async def fetch(self, query: SearchQuery, ctx: EngineContext) -> Any:
        params = {"q": query.q, "format": "json", "no_html": 1, "skip_disambig": 1}
        return await self.get_json(ctx, "GET", self.upstream, params=params)

    def parse(self, raw: dict[str, Any], query: SearchQuery) -> EngineOutput:
        require(isinstance(raw, dict) and "Heading" in raw, "not an Instant Answer body")
        entity = None
        if raw.get("Heading") and raw.get("AbstractText"):
            entity = _entity(raw)
        answer = None
        if raw.get("Answer"):
            answer = Answer(text=clean_text(str(raw["Answer"])),
                            type=raw.get("AnswerType") or "answer", generated=False)
        elif raw.get("Definition"):
            answer = Answer(text=raw["Definition"], type="definition", generated=False,
                            citations=[raw.get("DefinitionURL", "")])
        return EngineOutput(entity=entity, answer=answer,
                            related=_related(raw.get("RelatedTopics") or []))


def _entity(raw: dict[str, Any]) -> Entity:
    image = raw.get("Image") or None
    if image and not image.startswith("http"):
        image = DDG + image
    attributes = {}
    infobox = raw.get("Infobox") or {}
    for item in infobox.get("content", []) if isinstance(infobox, dict) else []:
        if item.get("data_type") == "string" and item.get("label"):
            key = "_".join(item["label"].lower().replace("-", " ").split())
            attributes.setdefault(key, str(item.get("value")))
    return Entity(title=raw["Heading"], type=(raw.get("Entity") or "").capitalize(),
                  description=raw["AbstractText"], url=raw.get("AbstractURL", ""),
                  image=image, website=raw.get("OfficialWebsite") or None,
                  attributes=attributes)


def _related(topics: list[dict[str, Any]], limit: int = 8) -> list[str]:
    queries: list[str] = []
    for topic in topics:
        url = topic.get("FirstURL") or ""
        path = urlsplit(url).path.strip("/")
        if url.startswith(DDG) and path and not path.startswith("c/"):
            queries.append(unquote(path).replace("_", " "))
        if len(queries) == limit:
            break
    return queries
