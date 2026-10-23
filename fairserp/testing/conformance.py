"""Conformance checks every engine adapter must pass, built-in or plugin.

Use it from a plugin's own test suite:

    from fairserp.testing.conformance import check_engine
    from my_package import MyEngine

    def test_my_engine_conforms():
        check_engine(MyEngine(), sample_payload=load("my_fixture.json"))

It checks the declared metadata (name, role, rights, supported params, depth), that the
parser turns a recorded payload into well-formed results, and that malformed payloads raise
a typed EngineError instead of crashing or returning a silent empty success.
"""

import asyncio
from typing import Any
from urllib.parse import urlsplit

from fairserp.config import Settings
from fairserp.engines.base import Engine, EngineContext, EngineError, ErrorKind, Role
from fairserp.http import HttpPool
from fairserp.model import OPTIONAL_PARAMS, EngineOutput, SearchQuery
from fairserp.rights import CommercialUse, Provenance, Rights

DEFAULT_NEGATIVE: list[Any] = [None, [], {}, "not json", {"results": "nope"}]


class ConformanceError(AssertionError):
    pass


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise ConformanceError(message)


def check_metadata(engine: Engine, settings: Settings | None = None) -> None:
    settings = settings or Settings(_env_file=None)
    _expect(isinstance(engine.name, str) and engine.name.isidentifier(),
            "name must be a lowercase identifier")
    _expect(engine.name == engine.name.lower(), "name must be lowercase")
    _expect(isinstance(engine.role, Role), "role must be a Role")
    rights = engine.rights_for(settings)
    _expect(isinstance(rights, Rights), "rights must be a fairserp.rights.Rights")
    _expect(isinstance(rights.provenance, Provenance), "rights.provenance must be set")
    _expect(isinstance(rights.commercial_use, CommercialUse), "rights.commercial_use must be set")
    _expect(rights.max_storage_seconds is None or rights.max_storage_seconds >= 0,
            "max_storage_seconds must be None or >= 0")
    _expect(urlsplit(rights.terms_url).scheme == "https", "terms_url must be an https URL")
    _expect(bool(rights.terms_reviewed), "terms_reviewed must name a date")
    _expect(set(engine.supports) <= set(OPTIONAL_PARAMS),
            f"supports may only contain {OPTIONAL_PARAMS}")
    _expect(engine.max_depth > 0, "max_depth must be positive")
    for attr in engine.required_settings:
        _expect(hasattr(settings, attr), f"required setting {attr!r} does not exist")
    full = SearchQuery(q="x", country="de", language="de", safe=True, freshness="d",
                       location="Berlin")
    _expect(engine.applied(full) <= set(engine.supports),
            "applied() may only return params listed in supports")


def check_parse(engine: Engine, payload: Any, query: SearchQuery | None = None) -> EngineOutput:
    query = query or SearchQuery(q="coffee")
    output = engine.parse(payload, query)
    _expect(isinstance(output, EngineOutput), "parse() must return an EngineOutput")
    _expect(len(output.web) <= query.num, "parse() must respect query.num")
    for result in output.web:
        _expect(urlsplit(result.url).scheme in ("http", "https"),
                f"result URL must be http(s): {result.url!r}")
        _expect(bool(result.title), "results need a title")
        _expect("<" not in result.title, "titles must be plain text")
    return output


def check_negative(engine: Engine, payloads: list[Any] | None = None,
                   settings: Settings | None = None, query: SearchQuery | None = None) -> None:
    settings = settings or Settings(_env_file=None)
    query = query or SearchQuery(q="coffee", location="Berlin", vertical="local")

    class Replay(type(engine)):
        async def fetch(self, q, ctx):
            return self._payload

    adapter = Replay.__new__(Replay)
    adapter.__dict__.update(engine.__dict__)
    ctx = EngineContext(settings, HttpPool(settings))
    for payload in DEFAULT_NEGATIVE if payloads is None else payloads:
        adapter._payload = payload
        try:
            asyncio.run(_unconfigured_ok(adapter, query, ctx))
        except EngineError as exc:
            _expect(exc.kind in (ErrorKind.REJECTED, ErrorKind.FAILED),
                    f"malformed payload {payload!r} raised {exc.kind}, expected rejected")
            continue
        raise ConformanceError(f"malformed payload {payload!r} was accepted as a result")


async def _unconfigured_ok(adapter: Engine, query: SearchQuery, ctx: EngineContext):
    raw = await adapter.fetch(query, ctx)
    try:
        return adapter.parse(raw, query)
    except EngineError:
        raise
    except Exception as exc:  # the same wrapping Engine.search applies
        raise EngineError(adapter.name, f"unexpected upstream payload: {exc}",
                          ErrorKind.REJECTED) from exc


def check_engine(engine: Engine, sample_payload: Any = None,
                 negative_payloads: list[Any] | None = None,
                 query: SearchQuery | None = None) -> None:
    check_metadata(engine)
    if sample_payload is not None:
        check_parse(engine, sample_payload, query)
    check_negative(engine, negative_payloads)
