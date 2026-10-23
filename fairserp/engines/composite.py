"""The "auto" engine: one organic provider plus open-data enrichment.

This is what serves SerpApi's engine=google by default. Organic results come from the
first configured provider in FAIRSERP_ORGANIC_PRIORITY, falling through to the next one
when a provider fails. Knowledge graph, answer box, related searches and local results
come from keyless open-data APIs in parallel.
"""

import asyncio
from typing import Any

from fairserp.engines.base import (
    Compliance,
    Engine,
    EngineContext,
    EngineError,
    Fragments,
    Role,
    SearchRequest,
)
from fairserp.engines.openstreetmap import local_query

SOURCES_KEY = "_sources"


class CompositeEngine(Engine):
    name = "auto"
    role = Role.COMPOSITE
    compliance = Compliance.OFFICIAL_API
    upstream = "first configured organic engine + duckduckgo_ia, wikipedia, openstreetmap"
    notes = "Default target of engine=google/bing/duckduckgo. Inherits its parts' status."

    def __init__(self, engines: dict[str, Engine]) -> None:
        self._engines = engines

    async def fetch(self, request: SearchRequest, ctx: EngineContext) -> Any:
        raise NotImplementedError("CompositeEngine overrides search()")

    def parse(self, raw: Any, request: SearchRequest) -> Fragments:
        raise NotImplementedError("CompositeEngine overrides search()")

    async def search(self, request: SearchRequest, ctx: EngineContext) -> Fragments:
        enrichers = self._enrichers(request)
        organic_task = asyncio.create_task(self._organic(request, ctx))
        enrichment = await asyncio.gather(
            *(self._engines[name].search(request, ctx) for name in enrichers),
            return_exceptions=True,
        )
        organic, sources = await organic_task

        merged: Fragments = dict(organic)
        by_name = dict(zip(enrichers, enrichment, strict=True))
        for name, result in by_name.items():
            if isinstance(result, Exception):
                sources.append(_source(name, "error", str(result)))
                continue
            sources.append(_source(name, "ok"))
            _merge_enrichment(merged, name, result)
        merged[SOURCES_KEY] = sources
        return merged

    def _enrichers(self, request: SearchRequest) -> list[str]:
        if request.start > 0:
            return []
        names = ["duckduckgo_ia", "wikipedia"]
        if local_query(request) is not None:
            names.append("openstreetmap")
        return names

    async def _organic(
        self, request: SearchRequest, ctx: EngineContext
    ) -> tuple[Fragments, list[dict[str, Any]]]:
        sources: list[dict[str, Any]] = []
        priority = [n.strip() for n in ctx.settings.organic_priority.split(",") if n.strip()]
        for name in priority:
            engine = self._engines.get(name)
            if engine is None or not engine.is_configured(ctx.settings):
                continue
            try:
                fragments = await engine.search(request, ctx)
            except EngineError as exc:
                sources.append(_source(name, "error", exc.message))
                continue
            sources.append(_source(name, "ok"))
            return fragments, sources
        failures = "; ".join(f"{s['engine']}: {s.get('error')}" for s in sources)
        detail = failures or "none configured"
        raise EngineError(self.name, f"no organic engine succeeded ({detail})")


def _merge_enrichment(merged: Fragments, name: str, result: Fragments) -> None:
    """Fill gaps only: the organic provider's own blocks always win."""
    for key in ("knowledge_graph", "answer_box", "related_searches", "local_results"):
        if key in result and key not in merged:
            merged[key] = result[key]


def _source(engine: str, status: str, error: str | None = None) -> dict[str, Any]:
    source = {"engine": engine, "status": status}
    if error:
        source["error"] = error
    return source
