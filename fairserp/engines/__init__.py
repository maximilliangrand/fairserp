"""Engine registry. Built-in adapters plus any installed through the ``fairserp.engines``
entry-point group, so ``pip install fairserp-something`` can add an engine."""

import logging
from importlib.metadata import entry_points

from fairserp.engines.base import Engine, EngineError, ErrorKind, Role
from fairserp.engines.brave import BraveEngine
from fairserp.engines.duckduckgo_ia import DuckDuckGoInstantAnswerEngine
from fairserp.engines.exa import ExaEngine
from fairserp.engines.firecrawl import FirecrawlEngine
from fairserp.engines.jina import JinaEngine
from fairserp.engines.marginalia import MarginaliaEngine
from fairserp.engines.mojeek import MojeekEngine
from fairserp.engines.openstreetmap import OpenStreetMapEngine
from fairserp.engines.parallel import ParallelEngine
from fairserp.engines.searxng import SearxngEngine
from fairserp.engines.tavily import TavilyEngine
from fairserp.engines.wikidata import WikidataEngine
from fairserp.engines.wikipedia import WikipediaEngine

logger = logging.getLogger("fairserp")

BUILTIN = (
    BraveEngine, MojeekEngine, MarginaliaEngine, TavilyEngine, ExaEngine, ParallelEngine,
    JinaEngine, FirecrawlEngine, SearxngEngine, WikipediaEngine, WikidataEngine,
    DuckDuckGoInstantAnswerEngine, OpenStreetMapEngine,
)


def build_registry(load_plugins: bool = True) -> dict[str, Engine]:
    engines: dict[str, Engine] = {cls.name: cls() for cls in BUILTIN}
    if load_plugins:
        for entry in entry_points(group="fairserp.engines"):
            try:
                engine = entry.load()()
            except Exception:
                logger.exception("could not load engine plugin %s", entry.name)
                continue
            if engine.name in engines:
                logger.warning("engine plugin %s shadows a built-in; ignored", engine.name)
                continue
            engines[engine.name] = engine
    return engines


__all__ = ["Engine", "EngineError", "ErrorKind", "Role", "build_registry"]
