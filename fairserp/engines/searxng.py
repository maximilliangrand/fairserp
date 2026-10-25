"""Bring-your-own SearXNG instance.

The instance's operator decides which upstream engines it queries, so the legal status of
results depends on that instance's settings.yml. Opt-in only.
"""

from typing import Any

from fairserp.canonical import strip_tracking
from fairserp.engines.base import Engine, EngineContext, EngineError, require
from fairserp.model import Answer, EngineOutput, Entity, Freshness, SearchQuery, WebResult
from fairserp.results import clean_text, snippet
from fairserp.rights import CommercialUse, Provenance, Rights

TIME_RANGE = {Freshness.DAY: "day", Freshness.WEEK: "week", Freshness.MONTH: "month",
              Freshness.YEAR: "year"}


class SearxngEngine(Engine):
    name = "searxng"
    upstream = "your SearXNG instance (search.formats must include json)"
    required_settings = ("searxng_url",)
    supports = frozenset({"language", "safe", "freshness"})
    max_depth = 200
    rights = Rights(
        provenance=Provenance.USER_OPERATED,
        commercial_use=CommercialUse.UNSPECIFIED,
        max_storage_seconds=None,
        attribution=None,
        terms_url="https://docs.searxng.org/",
        terms_reviewed="2026-10-23",
        notes="Rights follow the upstream engines your instance enables.",
    )
    notes = "Opt-in. Status depends on the engines enabled in your instance."

    def applied(self, query: SearchQuery) -> set[str]:
        applied = query.requested_params() & self.supports
        if query.freshness == Freshness.HOUR:
            applied.discard("freshness")
        return applied

    async def fetch(self, query: SearchQuery, ctx: EngineContext) -> Any:
        params: dict[str, Any] = {"q": query.q, "format": "json",
                                  "pageno": query.offset // query.num + 1}
        if query.language:
            params["language"] = query.language
        params["safesearch"] = 2 if query.safe else 0
        if query.freshness in TIME_RANGE:
            params["time_range"] = TIME_RANGE[query.freshness]
        url = f"{ctx.settings.searxng_url.rstrip('/')}/search"
        return await self.get_json(ctx, "GET", url, params=params)

    def parse(self, raw: dict[str, Any], query: SearchQuery) -> EngineOutput:
        require(isinstance(raw.get("results"), list), "missing results list")
        unresponsive = raw.get("unresponsive_engines") or []
        if not raw["results"] and unresponsive:
            names = ", ".join(str(e[0] if isinstance(e, list) else e) for e in unresponsive)
            raise EngineError(self.name, f"instance engines unresponsive: {names}")
        web = [WebResult(url=strip_tracking(it["url"]), title=clean_text(it.get("title")),
                         snippet=snippet(it.get("content"), query.q),
                         published_at=it.get("publishedDate"), thumbnail=it.get("thumbnail"))
               for it in raw["results"][: query.num]]
        entity = None
        boxes = raw.get("infoboxes") or []
        if boxes:
            box = boxes[0]
            entity = Entity(
                title=box.get("infobox", ""), description=clean_text(box.get("content")),
                url=box.get("id", ""), image=box.get("img_src"),
                attributes={"_".join(str(a.get("label", "")).lower().split()): str(a["value"])
                            for a in box.get("attributes") or [] if a.get("value")},
            )
        answer = None
        if raw.get("answers"):
            first = raw["answers"][0]
            text = first.get("answer") if isinstance(first, dict) else first
            answer = Answer(text=clean_text(text), generated=False)
        return EngineOutput(web=web, entity=entity, answer=answer,
                            related=list(raw.get("suggestions") or []),
                            total_results=raw.get("number_of_results") or None,
                            has_more=len(raw["results"]) >= query.num)
