"""Deterministic stand-in engines for core and API tests."""

import asyncio
from typing import Any

from fairserp.engines.base import Engine, EngineError, ErrorKind, Role
from fairserp.model import Answer, EngineOutput, Entity, Place, SearchQuery, WebResult
from fairserp.rights import CommercialUse, Provenance, Rights


def rights(provenance=Provenance.OWN_INDEX, storage=None, commercial=CommercialUse.ALLOWED,
           attribution=None):
    return Rights(provenance=provenance, commercial_use=commercial, max_storage_seconds=storage,
                  attribution=attribution, terms_url="https://example.org/terms",
                  terms_reviewed="2026-10-23")


def web(prefix: str, count: int, start: int = 0) -> list[WebResult]:
    return [WebResult(url=f"https://{prefix}.example/{i}", title=f"{prefix} {i}",
                      snippet=f"about {prefix} {i}") for i in range(start, start + count)]


class Stub(Engine):
    """Behavior is set per instance: output, error, delay, and call counting."""

    def __init__(self, name: str, role: Role = Role.ORGANIC, *, output: EngineOutput | None = None,
                 error: EngineError | None = None, delay: float = 0.0,
                 supports: frozenset[str] = frozenset(), rights_: Rights | None = None,
                 required: tuple[str, ...] = ()) -> None:
        self.name = name
        self.role = role
        self.output = output if output is not None else EngineOutput()
        self.error = error
        self.delay = delay
        self.supports = supports
        self.rights = rights_ or rights()
        self.required_settings = required
        self.calls: list[SearchQuery] = []

    async def fetch(self, query: SearchQuery, ctx) -> Any:
        self.calls.append(query)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.output

    def parse(self, raw: Any, query: SearchQuery) -> EngineOutput:
        return raw


def organic(name: str, count: int = 10, **kwargs) -> Stub:
    return Stub(name, output=EngineOutput(web=web(name, count), has_more=True), **kwargs)


def failing(name: str, kind: ErrorKind = ErrorKind.FAILED, retry_after=None, **kwargs) -> Stub:
    return Stub(name, error=EngineError(name, f"{name} broke", kind, retry_after), **kwargs)


def entity_engine(name: str = "wikidata", **kwargs) -> Stub:
    return Stub(name, Role.ENRICHMENT, output=EngineOutput(
        entity=Entity(title="Coffee", wikidata_id="Q8486")), **kwargs)


def answer_engine(name: str = "duckduckgo_ia", **kwargs) -> Stub:
    return Stub(name, Role.ENRICHMENT, output=EngineOutput(
        answer=Answer(text="42", generated=False), related=["coffee beans"]), **kwargs)


def local_engine(name: str = "openstreetmap", **kwargs) -> Stub:
    return Stub(name, Role.LOCAL, output=EngineOutput(places=[
        Place(name="Cafe One", category="Cafe", latitude=1.0, longitude=2.0, place_id="osm:1")]),
        supports=frozenset({"location"}), **kwargs)
