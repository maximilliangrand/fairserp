"""FastAPI application exposing SerpApi-compatible endpoints."""

import asyncio
import hashlib
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from fairserp import __version__, locations, render, restrictor, serp
from fairserp.config import Settings, get_settings
from fairserp.engines import Engine, EngineError, build_registry, resolve
from fairserp.engines.base import EngineContext, SearchRequest
from fairserp.engines.composite import SOURCES_KEY
from fairserp.http import HttpPool
from fairserp.store import Store, create_store

logger = logging.getLogger("fairserp")


class RedactKeys(logging.Filter):
    """Keep API keys sent as query params out of access logs."""

    PATTERN = re.compile(r"((?:serp_)?api_key=)[^&\s]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self.PATTERN.sub(r"\1REDACTED", a) if isinstance(a, str) else a
                for a in record.args
            )
        return True


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Services:
    settings: Settings
    store: Store
    http: HttpPool
    registry: dict[str, Engine]
    background: set[asyncio.Task] = field(default_factory=set)

    @property
    def ctx(self) -> EngineContext:
        return EngineContext(self.settings, self.http)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.services = Services(
            settings=settings,
            store=create_store(settings.backend),
            http=HttpPool(settings),
            registry=build_registry(),
        )
        yield
        services: Services = app.state.services
        await asyncio.gather(*services.background, return_exceptions=True)
        await services.http.close()
        await services.store.close()

    app = FastAPI(title="fairserp", version=__version__, lifespan=lifespan)
    logging.getLogger("uvicorn.access").addFilter(RedactKeys())

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = ", ".join(str(e["loc"][-1]) for e in exc.errors())
        return JSONResponse({"error": f"Invalid parameter: {fields}"}, status_code=400)

    _register_routes(app)
    return app


def _services(request: Request) -> Services:
    return request.app.state.services


# ---------------------------------------------------------------- auth and quota


def _authenticate(request: Request, services: Services) -> str:
    params = request.query_params
    header = request.headers.get("authorization", "")
    key = params.get("api_key") or params.get("serp_api_key")
    if not key and header.lower().startswith("bearer "):
        key = header[7:].strip()
    if services.settings.auth_disabled:
        return key or "anonymous"
    if not key or key not in services.settings.parsed_api_keys():
        raise ApiError(401, "Invalid API key. Check FAIRSERP_API_KEYS on this fairserp server.")
    return key


def _owner(api_key: str) -> str:
    """Stable, non-reversible id for a key, used in storage instead of the key itself."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def _usage_keys(api_key: str, moment: datetime | None = None) -> tuple[str, str]:
    moment = moment or datetime.now(UTC)
    owner = _owner(api_key)
    return f"usage:hour:{owner}:{moment:%Y%m%d%H}", f"usage:month:{owner}:{moment:%Y%m}"


def _hourly_limit(api_key: str, settings: Settings) -> int:
    return settings.parsed_api_keys().get(api_key, settings.default_hourly_limit)


async def _reserve_quota(api_key: str, services: Services) -> None:
    """Count the search up front so concurrent requests cannot overshoot the limit."""
    hour_key, _ = _usage_keys(api_key)
    if await services.store.incr(hour_key, 3600) > _hourly_limit(api_key, services.settings):
        await services.store.decr(hour_key)
        raise ApiError(429, "Your searches for the past hour have exceeded your hourly limit.")


async def _release_quota(api_key: str, services: Services) -> None:
    """Failed searches are free, as on SerpApi."""
    hour_key, _ = _usage_keys(api_key)
    await services.store.decr(hour_key)


# ---------------------------------------------------------------- search pipeline


def _cache_ttl(sources: list[dict[str, Any]], services: Services) -> int:
    """The shortest TTL any contributing engine allows. 0 means do not cache."""
    overrides = services.settings.parsed_engine_cache_ttl()
    ttls = [services.settings.cache_ttl_seconds]
    for source in sources:
        name = source["engine"]
        engine = services.registry.get(name)
        if name in overrides:
            ttls.append(overrides[name])
        elif engine is not None and engine.default_cache_ttl is not None:
            ttls.append(engine.default_cache_ttl)
    return max(0, min(ttls))


async def _archive(
    services: Services, api_key: str, response: dict[str, Any], ttl: int | None = None
) -> None:
    if serp.truthy(response.get("search_parameters", {}).get("zero_trace")):
        return
    await services.store.set_json(
        f"search:{response['search_metadata']['id']}",
        {"owner": _owner(api_key), "response": response},
        ttl or services.settings.archive_ttl_seconds,
    )


async def _run_search(
    services: Services,
    engine: Engine,
    req: SearchRequest,
    meta: dict[str, Any],
    api_key: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    zero_trace = serp.truthy(req.params.get("zero_trace"))
    try:
        fragments = await engine.search(req, services.ctx)
        sources = fragments.pop(SOURCES_KEY, None) or [{"engine": engine.name, "status": "ok"}]
        response = serp.assemble(
            req, fragments, {**meta, "status": "Success"}, services.settings.public_url,
            time.perf_counter() - started, sources,
        )
    except Exception as exc:
        await _release_quota(api_key, services)
        if not isinstance(exc, EngineError):
            logger.exception("search failed on engine %s", engine.name)
            exc = EngineError(engine.name, "internal error while searching", 500)
        if not zero_trace:
            failed = {"search_metadata": {**meta, "status": "Error"}, "error": exc.message}
            await _archive(services, api_key, failed)
        raise ApiError(exc.status, exc.message) from exc

    if not zero_trace:
        cache_ttl = _cache_ttl(sources, services)
        # Upstreams that forbid caching only get a short-lived archive copy.
        await _archive(services, api_key, response, None if cache_ttl else 3600)
        if cache_ttl > 0:
            await services.store.set_json(serp.cache_key(req.params), response, cache_ttl)
    _, month_key = _usage_keys(api_key)
    await services.store.incr(month_key, 32 * 24 * 3600)
    return response


async def _from_cache(
    services: Services, api_key: str, cached: dict[str, Any], zero_trace: bool
) -> dict[str, Any]:
    """Serve a cached result under a fresh search id the caller owns in the archive."""
    fresh = serp.metadata(serp.new_search_id(), services.settings.public_url, "Success")
    links = ("id", "json_endpoint", "markdown_endpoint", "raw_html_file")
    meta = {**cached["search_metadata"], **{k: fresh[k] for k in links}}
    response = {**cached, "search_metadata": meta}
    if not zero_trace:
        await _archive(services, api_key, response)
    return response


async def _search(request: Request, output: str | None) -> Response:
    services = _services(request)
    api_key = _authenticate(request, services)
    params = dict(request.query_params)
    output = output or params.get("output", "json")
    try:
        req = serp.build_request(params)
        engine = resolve(req.engine, services.registry, services.settings)
    except serp.ParamError as exc:
        raise ApiError(400, str(exc)) from exc
    except EngineError as exc:
        raise ApiError(exc.status, exc.message) from exc

    no_cache, run_async = serp.truthy(params.get("no_cache")), serp.truthy(params.get("async"))
    zero_trace = serp.truthy(params.get("zero_trace"))
    if no_cache and run_async:
        raise ApiError(400, "`no_cache` and `async` parameters can't be used together.")
    if zero_trace and run_async:
        raise ApiError(400, "`zero_trace` and `async` parameters can't be used together.")

    if not no_cache:
        cached = await services.store.get_json(serp.cache_key(params))
        if cached is not None:
            response = await _from_cache(services, api_key, cached, zero_trace)
            return _format(response, output, params.get("json_restrictor"))

    await _reserve_quota(api_key, services)
    meta = serp.metadata(serp.new_search_id(), services.settings.public_url, "Processing")

    if run_async:
        await _archive(services, api_key, {"search_metadata": meta})
        task = asyncio.create_task(_run_search(services, engine, req, meta, api_key))
        services.background.add(task)
        task.add_done_callback(services.background.discard)
        task.add_done_callback(_consume_exception)
        return JSONResponse({
            "search_metadata": meta,
            "search_parameters": serp.search_parameters(req),
        })

    response = await _run_search(services, engine, req, meta, api_key)
    return _format(response, output, params.get("json_restrictor"))


def _consume_exception(task: asyncio.Task) -> None:
    # The outcome, success or error, is already stored in the archive.
    if not task.cancelled():
        task.exception()


def _format(response: dict[str, Any], output: str, json_restrictor: str | None) -> Response:
    if output == "html":
        return HTMLResponse(render.to_html(response))
    if output == "md":
        return PlainTextResponse(render.to_markdown(response), media_type="text/markdown")
    if json_restrictor:
        try:
            response = restrictor.apply(response, json_restrictor)
        except restrictor.RestrictorError as exc:
            raise ApiError(400, str(exc)) from exc
    return JSONResponse(response)


# ---------------------------------------------------------------- routes


def _register_routes(app: FastAPI) -> None:
    @app.get("/search")
    async def search(request: Request) -> Response:
        return await _search(request, None)

    @app.get("/search.{ext}")
    async def search_ext(request: Request, ext: str) -> Response:
        if ext not in ("json", "html", "md"):
            raise ApiError(404, "Unsupported output format.")
        return await _search(request, ext)

    @app.get("/searches/{path:path}")
    async def archive(request: Request, path: str) -> Response:
        services = _services(request)
        api_key = _authenticate(request, services)
        last = path.rstrip("/").split("/")[-1]
        search_id, _, ext = last.partition(".")
        output = ext or request.query_params.get("output", "json")
        stored = await services.store.get_json(f"search:{search_id}")
        if stored is None:
            raise ApiError(404, "Search not found or expired from the archive.")
        if not services.settings.auth_disabled and stored["owner"] != _owner(api_key):
            raise ApiError(404, "Search not found or expired from the archive.")
        return _format(stored["response"], output, request.query_params.get("json_restrictor"))

    @app.get("/account")
    @app.get("/account.json")
    async def account(request: Request) -> dict[str, Any]:
        services = _services(request)
        api_key = _authenticate(request, services)
        hour_key, month_key = _usage_keys(api_key)
        last_hour_key, _ = _usage_keys(api_key, datetime.now(UTC) - timedelta(hours=1))
        limit = _hourly_limit(api_key, services.settings)
        this_hour = await services.store.count(hour_key)
        return {
            "account_id": hashlib.sha256(api_key.encode()).hexdigest()[:24],
            "api_key": api_key,
            "account_email": None,
            "account_status": "Active",
            "plan_id": "self_hosted",
            "plan_name": "fairserp self-hosted",
            "plan_monthly_price": 0.0,
            "plan_renewal_date": None,
            "searches_per_month": None,
            "plan_searches_left": None,
            "extra_credits": 0,
            "total_searches_left": max(0, limit - this_hour),
            "this_month_usage": await services.store.count(month_key),
            "this_hour_searches": this_hour,
            "last_hour_searches": await services.store.count(last_hour_key),
            "account_rate_limit_per_hour": limit,
        }

    @app.get("/locations.json")
    async def locations_api(request: Request, q: str = "", limit: int = 10) -> list[dict]:
        services = _services(request)
        _authenticate(request, services)
        if not q.strip():
            raise ApiError(400, "Missing query `q` parameter.")
        try:
            return await locations.search(q.strip(), max(1, min(limit, 10)), services.ctx,
                                          services.store)
        except EngineError as exc:
            raise ApiError(exc.status, exc.message) from exc

    @app.get("/engines")
    async def engines(request: Request) -> dict[str, Any]:
        services = _services(request)
        settings = services.settings
        return {
            "aliases": settings.parsed_aliases(),
            "organic_priority": settings.organic_priority.split(","),
            "engines": [
                {
                    "name": engine.name,
                    "role": engine.role.value,
                    "compliance": engine.compliance.value,
                    "configured": engine.is_configured(settings),
                    "upstream": engine.upstream,
                    "notes": engine.notes,
                }
                for engine in services.registry.values()
            ],
        }

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}
