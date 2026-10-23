"""HTTP API: the native /v1 endpoints and the SerpApi migration endpoints."""

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from fairserp import __version__, locations, logs, playground, render, restrictor, serpapi
from fairserp.archive import Archive
from fairserp.config import KeyLimits, Settings, get_settings
from fairserp.core import PlanError, Searcher
from fairserp.engines import Engine, EngineError, build_registry
from fairserp.fetch import Fetcher, FetchError
from fairserp.http import HttpPool
from fairserp.limits import QuotaExceeded, Quotas, owner_id
from fairserp.model import OutcomeStatus, SearchOutcome, SearchQuery
from fairserp.store import Store, create_store

logger = logging.getLogger("fairserp")


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str = "error",
                 retry_after: int | None = None, details: list[dict] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.retry_after = retry_after
        self.details = details or []


@dataclass
class Services:
    settings: Settings
    cache: Store
    state: Store
    http: HttpPool
    registry: dict[str, Engine]
    searcher: Searcher
    quotas: Quotas
    archive: Archive
    fetcher: Fetcher
    background: set[asyncio.Task] = field(default_factory=set)


def create_app(settings: Settings | None = None,
               registry: dict[str, Engine] | None = None) -> FastAPI:
    settings = settings or get_settings()
    logs.install(hide_queries=not settings.log_queries, secrets=settings.secret_values())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cache = create_store(settings.cache_backend, settings.cache_max_entries,
                             settings.redis_max_connections)
        state = create_store(settings.state_backend, None, settings.redis_max_connections)
        http = HttpPool(settings)
        engines = registry if registry is not None else build_registry()
        app.state.services = Services(
            settings=settings, cache=cache, state=state, http=http, registry=engines,
            searcher=Searcher(settings, engines, http, cache, state),
            quotas=Quotas(state), archive=Archive(cache, settings.archive_ttl_seconds),
            fetcher=Fetcher(settings, cache),
        )
        if not settings.public_url:
            logger.warning("FAIRSERP_PUBLIC_URL is unset; self links use each request's host")
        yield
        services: Services = app.state.services
        await asyncio.gather(*services.background, return_exceptions=True)
        await services.fetcher.close()
        await http.close()
        await cache.close()
        await state.close()

    app = FastAPI(
        title="fairserp", version=__version__, lifespan=lifespan,
        description="Self-hosted search infrastructure for AI agents, with provenance on "
                    "every result. The /v1 API is native; /search speaks SerpApi's format.",
    )

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        if request.url.path.startswith("/v1/"):
            body: dict[str, Any] = {"error": {"code": exc.code, "message": exc.message}}
            if exc.details:
                body["error"]["details"] = exc.details
        else:
            body = {"error": exc.message}
        return JSONResponse(body, status_code=exc.status, headers=headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return await api_error(request, ApiError(exc.status_code, str(exc.detail), "http"))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = ", ".join(str(e["loc"][-1]) for e in exc.errors())
        return await api_error(request, ApiError(400, f"Invalid parameter: {fields}",
                                                 "invalid_parameter"))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s", request.url.path)
        return await api_error(request, ApiError(500, "Internal server error.", "internal"))

    _register(app)
    return app


def _services(request: Request) -> Services:
    return request.app.state.services


def _base_url(request: Request, settings: Settings) -> str:
    return (settings.public_url or str(request.base_url)).rstrip("/")


# ---------------------------------------------------------------- auth


def authenticate(request: Request) -> str:
    """Bearer auth is preferred; the api_key query param exists for SerpApi clients."""
    services = _services(request)
    header = request.headers.get("authorization", "")
    key = header[7:].strip() if header.lower().startswith("bearer ") else None
    key = key or request.query_params.get("api_key") or request.query_params.get("serp_api_key")
    if services.settings.auth_disabled:
        return "anonymous"
    if not key or key not in services.settings.parsed_api_keys():
        raise ApiError(401, "Invalid API key. Send it as 'Authorization: Bearer <key>'; keys "
                            "are configured in FAIRSERP_API_KEYS.", "invalid_api_key")
    return key


def _limits(api_key: str, settings: Settings) -> KeyLimits:
    return settings.parsed_api_keys().get(
        api_key, KeyLimits(settings.default_hourly_limit, settings.default_monthly_limit))


# ---------------------------------------------------------------- shared pipeline


@dataclass
class Execution:
    outcome: SearchOutcome | None
    archived: bool = False
    job: dict[str, Any] | None = None


async def execute(services: Services, query: SearchQuery, requested_engine: str,
                  api_key: str, context: dict[str, Any], *, run_async: bool = False,
                  no_cache: bool = False, zero_trace: bool = False,
                  search_id: str | None = None) -> Execution:
    """Validate, plan, reserve quota, run, settle and archive one search."""
    if no_cache and run_async:
        raise ApiError(400, "`no_cache` and `async` parameters can't be used together.")
    if zero_trace and run_async:
        raise ApiError(400, "`zero_trace` and `async` parameters can't be used together.")
    try:
        plan = services.searcher.plan(query)
    except PlanError as exc:
        raise ApiError(exc.status, exc.message, exc.code, details=exc.details) from exc
    if run_async:
        forbidden = services.searcher.storage_forbidden(plan)
        if forbidden:
            raise ApiError(400, f"async needs to store the result, which the terms of "
                                f"{', '.join(forbidden)} do not allow.", "storage_forbidden")
    try:
        reservation = await services.quotas.reserve(api_key, _limits(api_key, services.settings))
    except QuotaExceeded as exc:
        raise ApiError(429, exc.message, "quota_exceeded", exc.retry_after) from exc

    search_id = search_id or secrets.token_hex(12)
    owner = owner_id(api_key)
    if run_async:
        lease = services.settings.request_deadline_seconds + 15
        job = await services.archive.start_job(owner, search_id, context, lease)
        task = asyncio.create_task(_background(services, query, plan, search_id, api_key,
                                               requested_engine, reservation, context))
        services.background.add(task)
        task.add_done_callback(services.background.discard)
        return Execution(outcome=None, job={**job, "id": search_id})

    try:
        outcome = await services.searcher.run(query, plan, search_id, api_key,
                                              requested_engine, use_cache=not no_cache)
    except Exception:
        await services.quotas.release(reservation)
        raise
    if not outcome.billable:
        await services.quotas.release(reservation)
    archived = False if zero_trace else await services.archive.save(owner, outcome, context)
    return Execution(outcome=outcome, archived=archived)


async def _background(services: Services, query: SearchQuery, plan, search_id: str,
                      api_key: str, requested_engine: str, reservation, context) -> None:
    owner = owner_id(api_key)
    try:
        outcome = await services.searcher.run(query, plan, search_id, api_key, requested_engine)
    except Exception:
        logger.exception("async search %s failed", search_id)
        await services.quotas.release(reservation)
        await services.archive.fail_job(owner, search_id, context, "internal error")
        return
    if not outcome.billable:
        await services.quotas.release(reservation)
    await services.archive.save(owner, outcome, context)


def _outcome_http_status(outcome: SearchOutcome) -> int:
    return {OutcomeStatus.RATE_LIMITED: 429, OutcomeStatus.FAILED: 502}.get(outcome.status, 200)


def _retry_after(outcome: SearchOutcome) -> dict[str, str] | None:
    waits = [p.retry_after for p in outcome.providers if p.retry_after]
    if outcome.status == OutcomeStatus.RATE_LIMITED:
        return {"Retry-After": str(min(waits) if waits else 60)}
    return None


# ---------------------------------------------------------------- SerpApi endpoints


async def _serpapi_search(request: Request, output: str | None) -> Response:
    services = _services(request)
    api_key = authenticate(request)
    params = dict(request.query_params)
    output = output or params.get("output", "json")
    if output not in ("json", "html", "md"):
        raise ApiError(400, "Unsupported `output` value; use json, html or md.")
    if params.get("json_restrictor"):
        try:
            restrictor.validate(params["json_restrictor"])
        except restrictor.RestrictorError as exc:
            raise ApiError(400, str(exc)) from exc
    try:
        translation = serpapi.translate(
            params, services.settings.parsed_serpapi_engines(), set(services.registry),
            services.settings.strict_params)
    except serpapi.SerpApiParamError as exc:
        raise ApiError(exc.status, str(exc)) from exc

    truthy = {"true", "1"}
    stored_params = {k: v for k, v in params.items() if k not in ("api_key", "serp_api_key")}
    context = {"params": stored_params, "ignored": translation.ignored}
    execution = await execute(
        services, translation.query, translation.requested_engine, api_key, context,
        run_async=params.get("async", "").lower() in truthy,
        no_cache=params.get("no_cache", "").lower() in truthy,
        zero_trace=params.get("zero_trace", "").lower() in truthy,
    )
    base = _base_url(request, services.settings)
    if execution.job is not None:
        return JSONResponse(_job_stub(execution.job, base, translation.query.q,
                                      translation.requested_engine))
    outcome = execution.outcome
    rendered = serpapi.render(outcome, base, params, translation.ignored, execution.archived)
    if outcome.status in (OutcomeStatus.FAILED, OutcomeStatus.RATE_LIMITED):
        return JSONResponse({"error": outcome.error, "search_metadata": rendered[
            "search_metadata"]}, status_code=_outcome_http_status(outcome),
            headers=_retry_after(outcome))
    return _format(rendered, output, params.get("json_restrictor"))


def _job_stub(job: dict[str, Any], base: str, q: str, engine: str) -> dict[str, Any]:
    return {"search_metadata": {
        "id": job["id"], "status": "Processing",
        "json_endpoint": f"{base}/searches/{job['id']}.json",
        "created_at": job["created_at"], "processed_at": job["created_at"]},
        "search_parameters": {"engine": engine, "q": q}}


def _format(response: dict[str, Any], output: str, json_restrictor: str | None) -> Response:
    if output == "html":
        return HTMLResponse(render.to_html(response),
                            headers={"Content-Security-Policy": render.HTML_CSP,
                                     "X-Content-Type-Options": "nosniff"})
    if output == "md":
        return PlainTextResponse(render.to_markdown(response), media_type="text/markdown")
    if json_restrictor:
        response = restrictor.apply(response, json_restrictor)
    return JSONResponse(response)


async def _serpapi_archive(request: Request, path: str) -> Response:
    services = _services(request)
    api_key = authenticate(request)
    last = path.rstrip("/").split("/")[-1]
    search_id, _, ext = last.partition(".")
    output = ext or request.query_params.get("output", "json")
    if request.query_params.get("json_restrictor"):
        try:
            restrictor.validate(request.query_params["json_restrictor"])
        except restrictor.RestrictorError as exc:
            raise ApiError(400, str(exc)) from exc
    record = await services.archive.load(search_id)
    if record is None or (not services.settings.auth_disabled
                          and record["owner"] != owner_id(api_key)):
        raise ApiError(404, "Search not found or expired from the archive.")
    base = _base_url(request, services.settings)
    if record["kind"] == "expired":
        raise ApiError(410, f"This search is not retained: {record['reason']}.")
    context = record.get("context") or {}
    if record["kind"] == "job":
        return JSONResponse(_job_stub({**record, "id": search_id}, base,
                                      context.get("params", {}).get("q", ""),
                                      context.get("params", {}).get("engine", "google")))
    if record["kind"] == "failed":
        return JSONResponse({"search_metadata": {"id": search_id, "status": "Error"},
                             "error": record["error"]})
    outcome = SearchOutcome.model_validate(record["outcome"])
    rendered = serpapi.render(outcome, base, context.get("params", {}),
                              context.get("ignored", []), archived=True)
    if outcome.status in (OutcomeStatus.FAILED, OutcomeStatus.RATE_LIMITED):
        rendered["error"] = outcome.error
    return _format(rendered, output, request.query_params.get("json_restrictor"))


# ---------------------------------------------------------------- routes


def _register(app: FastAPI) -> None:
    @app.get("/v1/search", response_model=SearchOutcome, tags=["native"],
             summary="Search with provenance (native API)")
    async def native_search(request: Request, query: Annotated[SearchQuery, Query()],
                            api_key: Annotated[str, Depends(authenticate)]) -> Response:
        return await _native(request, query, api_key)

    @app.post("/v1/search", response_model=SearchOutcome, tags=["native"],
              summary="Search with provenance (native API, JSON body)")
    async def native_search_post(request: Request, query: SearchQuery,
                                 api_key: Annotated[str, Depends(authenticate)]) -> Response:
        return await _native(request, query, api_key)

    @app.get("/v1/searches/{search_id}", response_model=SearchOutcome, tags=["native"])
    async def native_archive(request: Request, search_id: str,
                             api_key: Annotated[str, Depends(authenticate)]) -> Response:
        services = _services(request)
        record = await services.archive.load(search_id)
        if record is None or (not services.settings.auth_disabled
                              and record["owner"] != owner_id(api_key)):
            raise ApiError(404, "Search not found or expired from the archive.", "not_found")
        if record["kind"] == "expired":
            raise ApiError(410, f"Not retained: {record['reason']}.", "not_retained")
        if record["kind"] == "job":
            return JSONResponse({"id": search_id, "status": "processing"}, status_code=202)
        if record["kind"] == "failed":
            raise ApiError(502, record["error"], "job_failed")
        return JSONResponse(record["outcome"])

    @app.get("/search", tags=["serpapi"], summary="SerpApi-compatible search (migration)")
    async def search(
        request: Request,
        q: Annotated[str, Query(description="Search query")],
        engine: Annotated[str | None, Query(description="fairserp engine, or a SerpApi "
                                            "name mapped by FAIRSERP_SERPAPI_ENGINES")] = None,
        location: str | None = None, gl: str | None = None, hl: str | None = None,
        safe: str | None = None, tbs: str | None = None, start: int | None = None,
        num: int | None = None, no_cache: bool | None = None,
        async_: Annotated[bool | None, Query(alias="async")] = None,
        output: str | None = None, json_restrictor: str | None = None,
        api_key: Annotated[str | None, Query(description="Prefer the Authorization: "
                                             "Bearer header")] = None,
    ) -> Response:
        return await _serpapi_search(request, None)

    @app.get("/search.{ext}", tags=["serpapi"], include_in_schema=False)
    async def search_ext(request: Request, ext: str) -> Response:
        if ext not in ("json", "html", "md"):
            raise ApiError(404, "Unsupported output format.")
        return await _serpapi_search(request, ext)

    @app.get("/searches/{path:path}", tags=["serpapi"], summary="SerpApi search archive")
    async def archive(request: Request, path: str) -> Response:
        return await _serpapi_archive(request, path)

    @app.get("/account", tags=["serpapi"])
    @app.get("/account.json", tags=["serpapi"], include_in_schema=False)
    async def account(request: Request,
                      api_key: Annotated[str, Depends(authenticate)]) -> dict[str, Any]:
        services = _services(request)
        limits = _limits(api_key, services.settings)
        usage = await services.quotas.usage(api_key)
        return {
            "account_id": owner_id(api_key), "api_key": api_key, "account_email": None,
            "account_status": "Active", "plan_id": "self_hosted",
            "plan_name": "fairserp self-hosted", "plan_monthly_price": 0.0,
            "plan_renewal_date": None, "searches_per_month": limits.monthly or None,
            "plan_searches_left": max(0, limits.monthly - usage["month"])
            if limits.monthly else None,
            "extra_credits": 0, "total_searches_left": max(0, limits.hourly - usage["hour"]),
            "this_month_usage": usage["month"], "this_hour_searches": usage["hour"],
            "last_hour_searches": usage["last_hour"],
            "account_rate_limit_per_hour": limits.hourly,
        }

    @app.get("/locations.json", tags=["serpapi"])
    async def locations_api(request: Request, q: str = "", limit: int = 10,
                            api_key: Annotated[str, Depends(authenticate)] = "") -> list[dict]:
        services = _services(request)
        if not q.strip():
            raise ApiError(400, "Missing query `q` parameter.")
        try:
            reservation = await services.quotas.reserve(api_key,
                                                        _limits(api_key, services.settings))
        except QuotaExceeded as exc:
            raise ApiError(429, exc.message, "quota_exceeded", exc.retry_after) from exc
        try:
            return await locations.search(q.strip(), max(1, min(limit, 10)),
                                          services.searcher.ctx, services.cache)
        except EngineError as exc:
            await services.quotas.release(reservation)
            limited = exc.kind in ("rate_limited", "throttled")
            raise ApiError(429 if limited else 502, exc.message,
                           retry_after=exc.retry_after) from exc

    @app.get("/v1/engines", tags=["native"], summary="Engines, rights and policy status")
    @app.get("/engines", include_in_schema=False)
    async def engines(request: Request,
                      api_key: Annotated[str, Depends(authenticate)]) -> dict[str, Any]:
        services = _services(request)
        settings, searcher = services.settings, services.searcher
        return {
            "profile": settings.profile.value,
            "opt_in": sorted(settings.opt_in_set()),
            "serpapi_engines": settings.parsed_serpapi_engines(),
            "organic_priority": settings.priority(),
            "engines": [
                {"name": e.name, "role": e.role.value, "enabled": searcher.block_reason(e) is None,
                 "blocked_reason": searcher.block_reason(e), "supports": sorted(e.supports),
                 "max_depth": e.max_depth, "fixture_verified": e.fixture_verified,
                 "rights": searcher.rights(e).as_dict(), "upstream": e.upstream,
                 "notes": e.notes}
                for e in services.registry.values()
            ],
        }

    @app.get("/v1/fetch", tags=["native"], summary="Read one public page as cited evidence")
    async def fetch_page(request: Request,
                         url: Annotated[str, Query(max_length=2048)],
                         question: Annotated[str, Query(max_length=500)] = "",
                         api_key: Annotated[str, Depends(authenticate)] = "") -> JSONResponse:
        """Respects robots.txt, refuses non-public addresses, caps size and time.
        Counts against the key's quota like a search; refused or failed fetches are free."""
        services = _services(request)
        try:
            reservation = await services.quotas.reserve(api_key,
                                                        _limits(api_key, services.settings))
        except QuotaExceeded as exc:
            raise ApiError(429, exc.message, "quota_exceeded", exc.retry_after) from exc
        try:
            evidence = await services.fetcher.fetch(url, question)
        except FetchError as exc:
            await services.quotas.release(reservation)
            raise ApiError(exc.status, exc.message, "fetch_refused") from exc
        except Exception:
            await services.quotas.release(reservation)
            raise
        return JSONResponse(evidence.as_dict())

    @app.get("/", include_in_schema=False)
    async def playground_page() -> HTMLResponse:
        html, csp = playground.page()
        return HTMLResponse(html, headers={"Content-Security-Policy": csp,
                                           "X-Content-Type-Options": "nosniff",
                                           "Referrer-Policy": "no-referrer"})

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["ops"])
    async def readyz(request: Request) -> JSONResponse:
        services = _services(request)
        checks = {}
        for name, store in (("cache", services.cache), ("state", services.state)):
            try:
                checks[name] = "ok" if await store.ping() else "down"
            except Exception as exc:
                checks[name] = f"down: {type(exc).__name__}"
        ready = all(v == "ok" for v in checks.values())
        return JSONResponse({"status": "ready" if ready else "not_ready", "checks": checks},
                            status_code=200 if ready else 503)


async def _native(request: Request, query: SearchQuery, api_key: str) -> Response:
    services = _services(request)
    execution = await execute(services, query, query.engine, api_key,
                              {"native": True})
    outcome = execution.outcome
    return JSONResponse(outcome.model_dump(mode="json"),
                        status_code=_outcome_http_status(outcome),
                        headers=_retry_after(outcome))
