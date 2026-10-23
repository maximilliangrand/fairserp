"""MCP server: web search, local search and a guarded fetch tool, as compact cited evidence.

    fairserp mcp                      # stdio, for Claude Desktop / Claude Code / Cursor
    claude mcp add fairserp -- uvx --from git+https://github.com/maximilliangrand/fairserp \
        fairserp mcp

It runs the same planner and policy as the HTTP API, in-process: the strict profile, opt-ins,
budgets and retention rules all apply. Configure it with the usual FAIRSERP_* variables.
"""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import MCPServer

from fairserp import __version__
from fairserp.config import Settings, get_settings
from fairserp.core import PlanError, Searcher
from fairserp.engines import build_registry
from fairserp.fetch import Fetcher, FetchError
from fairserp.http import HttpPool
from fairserp.model import Mode, SearchOutcome, SearchQuery, Vertical
from fairserp.store import create_store

MCP_KEY = "mcp-local"


@dataclass
class Runtime:
    searcher: Searcher
    fetcher: Fetcher
    http: HttpPool


def compact(outcome: SearchOutcome) -> dict[str, Any]:
    """What an agent needs to cite: short results with provider provenance, nothing else."""
    data: dict[str, Any] = {
        "status": outcome.status.value,
        "results": [
            {"n": r.rank, "title": r.title, "url": r.url, "snippet": r.snippet,
             **({"published_at": r.published_at} if r.published_at else {}),
             "via": [f"{s.engine}#{s.rank} ({s.provenance})" for s in r.sources]}
            for r in outcome.results
        ],
    }
    if outcome.places:
        data["places"] = [{"name": p.name, "category": p.category, "address": p.address,
                           "distance_m": p.distance_m, "in_area": p.in_area,
                           "website": p.website, "hours": p.hours} for p in outcome.places]
    if outcome.entity:
        data["entity"] = {"title": outcome.entity.title, "type": outcome.entity.type,
                          "description": outcome.entity.description[:400],
                          "wikidata_id": outcome.entity.wikidata_id,
                          "facts": outcome.entity.attributes, "url": outcome.entity.url}
    if outcome.answer:
        data["answer"] = {"text": outcome.answer.text, "generated": outcome.answer.generated,
                          "citations": outcome.answer.citations}
    problems = [f"{p.engine}: {p.status.value}" + (f" ({p.error})" if p.error else "")
                for p in outcome.providers if p.status.value not in ("ok", "empty", "skipped")]
    if problems:
        data["degraded"] = problems
    if outcome.params.ignored:
        data["ignored_params"] = outcome.params.ignored
    if outcome.notes:
        data["notes"] = outcome.notes
    if outcome.attribution:
        data["attribution"] = outcome.attribution
    if outcome.error and outcome.status.value != "success":
        data["error"] = outcome.error
    return data


def build(settings: Settings | None = None, runtime: Runtime | None = None) -> MCPServer:
    settings = settings or get_settings()
    state: dict[str, Runtime] = {}

    @asynccontextmanager
    async def lifespan(_server):
        if runtime is not None:
            state["rt"] = runtime
            yield
            return
        cache = create_store(settings.cache_backend, settings.cache_max_entries)
        accounting = create_store(settings.state_backend)
        http = HttpPool(settings)
        state["rt"] = Runtime(Searcher(settings, build_registry(), http, cache, accounting),
                              Fetcher(settings, cache), http)
        yield
        await state["rt"].fetcher.close()
        await http.close()

    server = MCPServer(
        name="fairserp", version=__version__, lifespan=lifespan,
        instructions="Search the web through providers with disclosed provenance. Every "
                     "result names the provider that returned it; cite URLs, and use fetch "
                     "to read a page before relying on it.",
    )

    async def _search(query: SearchQuery) -> dict[str, Any]:
        rt = state["rt"] if runtime is None else runtime
        try:
            plan = rt.searcher.plan(query)
        except PlanError as exc:
            return {"status": "refused", "error": exc.message}
        outcome = await rt.searcher.run(query, plan, "mcp", MCP_KEY, query.engine)
        return compact(outcome)

    @server.tool(description="Search the web. Returns numbered results with URL, snippet and "
                             "the provider (with its rank and provenance) for each.")
    async def web_search(query: str, num: int = 5, freshness: str | None = None,
                         country: str | None = None, language: str | None = None,
                         mode: str = "fast") -> dict[str, Any]:
        """freshness: h, d, w, m or y. mode: fast, research (several providers fused) or
        private (one provider sees the query)."""
        return await _search(SearchQuery(q=query, num=min(max(num, 1), 20), freshness=freshness,
                                         country=country, language=language, mode=Mode(mode)))

    @server.tool(description="Find places of a kind near a location, from OpenStreetMap. "
                             "Example: query='pizza', near='Brooklyn, New York'.")
    async def local_search(query: str, near: str, num: int = 5) -> dict[str, Any]:
        return await _search(SearchQuery(q=query, location=near, vertical=Vertical.LOCAL,
                                         num=min(max(num, 1), 20)))

    @server.tool(description="Read a public web page and return the passages most relevant to "
                             "an optional question, with title, dates and the final URL. "
                             "Respects robots.txt; refuses private network addresses.")
    async def fetch(url: str, question: str = "") -> dict[str, Any]:
        rt = state["rt"] if runtime is None else runtime
        try:
            evidence = await rt.fetcher.fetch(url, question)
        except FetchError as exc:
            return {"status": "refused" if exc.status in (400, 403, 415) else "failed",
                    "error": exc.message, "url": url}
        return {"status": "ok", **evidence.as_dict()}

    return server


def main() -> None:
    build().run("stdio")


if __name__ == "__main__":
    main()
