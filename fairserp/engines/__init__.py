"""Engine registry and SerpApi engine-name aliasing."""

from fairserp.config import Settings
from fairserp.engines.base import Engine, EngineError
from fairserp.engines.brave import BraveEngine
from fairserp.engines.composite import CompositeEngine
from fairserp.engines.duckduckgo_ia import DuckDuckGoInstantAnswerEngine
from fairserp.engines.firecrawl import FirecrawlEngine
from fairserp.engines.marginalia import MarginaliaEngine
from fairserp.engines.mojeek import MojeekEngine
from fairserp.engines.openstreetmap import OpenStreetMapEngine
from fairserp.engines.searxng import SearxngEngine
from fairserp.engines.tavily import TavilyEngine
from fairserp.engines.wikipedia import WikipediaEngine

# SerpApi engine names fairserp answers with local results only.
MAPS_ENGINES = {"google_maps", "google_local"}


def build_registry() -> dict[str, Engine]:
    engines: dict[str, Engine] = {
        engine.name: engine
        for engine in (
            BraveEngine(),
            MojeekEngine(),
            TavilyEngine(),
            FirecrawlEngine(),
            MarginaliaEngine(),
            SearxngEngine(),
            DuckDuckGoInstantAnswerEngine(),
            WikipediaEngine(),
            OpenStreetMapEngine(),
        )
    }
    engines["auto"] = CompositeEngine(engines)
    return engines


def resolve(name: str, registry: dict[str, Engine], settings: Settings) -> Engine:
    if name in MAPS_ENGINES:
        return registry["openstreetmap"]
    target = settings.parsed_aliases().get(name, name)
    engine = registry.get(target)
    if engine is None:
        raise EngineError(name, f"Unsupported `{name}` search engine.", 400)
    return engine


__all__ = ["Engine", "EngineError", "MAPS_ENGINES", "build_registry", "resolve"]
