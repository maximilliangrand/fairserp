"""LangChain integration with an explicit base URL for both sync and async calls.

Why not patch ``SerpAPIWrapper``: in langchain-community its ``aresults()`` builds
``https://serpapi.com/search`` itself and ignores any base-URL override, so async agents
would send your fairserp key and every query to SerpApi. These classes route both paths
through fairserp's client.

    from fairserp.integrations.langchain import FairserpSearchTool
    tool = FairserpSearchTool(base_url="http://localhost:8000", api_key="...")
    tool.invoke("latest postgres release notes")

Requires ``pip install fairserp[langchain]`` (langchain-core).
"""

from typing import Any

from langchain_core.tools import BaseTool
from pydantic import ConfigDict, Field, PrivateAttr

from fairserp.client import AsyncFairserp, Fairserp
from fairserp.model import SearchOutcome


def format_outcome(outcome: SearchOutcome, max_results: int = 5) -> str:
    """Compact cited evidence for a model: numbered results with URL and provider."""
    lines = []
    if outcome.answer and not outcome.answer.generated:
        lines.append(f"Answer: {outcome.answer.text}")
    if outcome.entity:
        lines.append(f"Entity: {outcome.entity.title}: {outcome.entity.description[:300]}")
    for result in outcome.results[:max_results]:
        engines = ", ".join(s.engine for s in result.sources)
        lines.append(f"[{result.rank}] {result.title}\n{result.url}\n{result.snippet} "
                     f"(via {engines})")
    if not lines:
        lines.append(f"No results ({outcome.status.value}).")
    return "\n\n".join(lines)


class FairserpSearchTool(BaseTool):
    """Web search through a fairserp server, returning cited results."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = "web_search"
    description: str = ("Search the web. Input is a search query. Returns numbered results "
                        "with URLs and the provider that returned each one.")
    base_url: str
    api_key: str
    max_results: int = 5
    search_params: dict[str, Any] = Field(default_factory=dict)
    _sync: Fairserp | None = PrivateAttr(default=None)
    _async: AsyncFairserp | None = PrivateAttr(default=None)

    def _client(self) -> Fairserp:
        if self._sync is None:
            self._sync = Fairserp(self.base_url, self.api_key)
        return self._sync

    def _aclient(self) -> AsyncFairserp:
        if self._async is None:
            self._async = AsyncFairserp(self.base_url, self.api_key)
        return self._async

    def _run(self, query: str, **_: Any) -> str:
        outcome = self._client().search(query, num=self.max_results, **self.search_params)
        return format_outcome(outcome, self.max_results)

    async def _arun(self, query: str, **_: Any) -> str:
        outcome = await self._aclient().search(query, num=self.max_results, **self.search_params)
        return format_outcome(outcome, self.max_results)


class FairserpAPIWrapper:
    """Drop-in for code that calls ``SerpAPIWrapper.results/aresults/run/arun``, with the
    base URL honored on every path. Returns SerpApi-shaped dicts."""

    def __init__(self, base_url: str, api_key: str, params: dict[str, Any] | None = None,
                 transport: Any = None, async_transport: Any = None):
        self._base_url, self._api_key = base_url, api_key
        self._transport, self._async_transport = transport, async_transport
        self.params = {"engine": "google", **(params or {})}

    def results(self, query: str) -> dict[str, Any]:
        with Fairserp(self._base_url, self._api_key, transport=self._transport) as client:
            return client.serpapi({**self.params, "q": query})

    async def aresults(self, query: str) -> dict[str, Any]:
        async with AsyncFairserp(self._base_url, self._api_key,
                                 transport=self._async_transport) as client:
            return await client.serpapi({**self.params, "q": query})

    def run(self, query: str) -> str:
        return _first_snippets(self.results(query))

    async def arun(self, query: str) -> str:
        return _first_snippets(await self.aresults(query))


def _first_snippets(response: dict[str, Any]) -> str:
    if "answer_box" in response:
        return str(response["answer_box"].get("answer", ""))
    snippets = [r.get("snippet", "") for r in response.get("organic_results", [])[:5]]
    return "\n".join(s for s in snippets if s) or "No good search result found"
