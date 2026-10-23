"""The search planner and executor shared by every wire format.

``plan`` validates a query against deployment policy before any upstream call or billing.
``run`` executes the plan within one overall deadline and returns a ``SearchOutcome`` that
says, honestly, what happened: which providers answered, which params were applied or
ignored, what failed, and how long any copy may be kept.
"""

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fairserp.config import Settings
from fairserp.engines.base import Engine, EngineContext, EngineError, ErrorKind, Role
from fairserp.engines.openstreetmap import local_request
from fairserp.fusion import rank_single, rrf
from fairserp.http import HttpPool
from fairserp.limits import CircuitBreaker, HostLimiter, Reservation, UpstreamBudgets
from fairserp.model import (
    EngineOutput,
    Mode,
    OutcomeStatus,
    ParamReport,
    ProviderReport,
    ProviderStatus,
    Retention,
    SearchOutcome,
    SearchQuery,
    Vertical,
)
from fairserp.rights import Rights, policy_block
from fairserp.store import Store

LOCAL_ENGINE = "openstreetmap"
ENRICHMENT_GRACE_SECONDS = 0.3


class PlanError(Exception):
    """The query cannot run under this deployment's policy. Nothing was billed or sent."""

    def __init__(self, status: int, message: str, code: str = "plan_rejected",
                 details: list[dict] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.details = details or []


@dataclass
class Plan:
    resolved_engine: str
    organic: list[Engine]
    enrichers: list[Engine] = field(default_factory=list)
    local: Engine | None = None
    skipped: list[ProviderReport] = field(default_factory=list)


@dataclass
class RunState:
    api_key: str
    deadline: float
    use_cache: bool = True


@dataclass
class Fragment:
    engine: Engine
    report: ProviderReport
    output: EngineOutput | None = None
    expires_at: float | None = None  # epoch seconds; None means no storage limit
    budget: Reservation | None = None


class Searcher:
    def __init__(self, settings: Settings, registry: dict[str, Engine], http: HttpPool,
                 cache: Store, state: Store) -> None:
        self.settings = settings
        self.registry = registry
        self.cache = cache
        self.limiter = HostLimiter(state) if "openstreetmap.org" in settings.nominatim_url \
            else HostLimiter(state, intervals={})
        self.ctx = EngineContext(settings, http, cache=cache, limiter=self.limiter)
        self.budgets = UpstreamBudgets(state, settings.parsed_budgets())
        self.breaker = CircuitBreaker(state, settings.circuit_failures,
                                      settings.circuit_cooldown_seconds)

    # ------------------------------------------------------------------ policy

    def rights(self, engine: Engine) -> Rights:
        return engine.rights_for(self.settings)

    def block_reason(self, engine: Engine) -> str | None:
        if not engine.is_configured(self.settings):
            missing = ", ".join(f"FAIRSERP_{a.upper()}" for a in engine.required_settings)
            return f"not configured, set {missing}"
        return policy_block(self.rights(engine), engine.name, self.settings.profile,
                            self.settings.opt_in_set(), self.settings.noncommercial_deployment)

    def storage_limit(self, engine: Engine) -> int | None:
        limit = self.rights(engine).max_storage_seconds
        override = self.settings.parsed_engine_cache_ttl().get(engine.name)
        if override is not None:
            limit = override if limit is None else min(limit, override)
        return limit

    # ------------------------------------------------------------------ planning

    def plan(self, query: SearchQuery) -> Plan:
        if query.engine != "auto":
            return self._plan_named(query)
        if query.vertical == Vertical.LOCAL:
            return self._plan_named(query.model_copy(update={"engine": LOCAL_ENGINE}))

        local_planned = (query.mode != Mode.PRIVATE and query.offset == 0
                         and local_request(query) is not None
                         and self.block_reason(self.registry[LOCAL_ENGINE]) is None)
        needed = query.requested_params() - ({"location"} if local_planned else set())
        skipped: list[ProviderReport] = []
        candidates: list[Engine] = []
        for name in self.settings.priority():
            engine = self.registry.get(name)
            if engine is None or engine.role != Role.ORGANIC:
                continue
            reason = self.block_reason(engine)
            if reason is None and query.safe and "safe" not in engine.applied(query):
                reason = "cannot enforce safe search"
            if reason is None and self._strict(query) and needed - engine.applied(query):
                reason = f"cannot apply {', '.join(sorted(needed - engine.applied(query)))}"
            if reason:
                skipped.append(_skip(engine, reason))
            else:
                candidates.append(engine)
        if not candidates:
            if query.safe:
                raise PlanError(400, _no_engine_message(query, skipped), "safe_search_unavailable")
            raise PlanError(503, _no_engine_message(query, skipped), "no_engine_enabled",
                            [{"engine": r.engine, "reason": r.error} for r in skipped])
        if query.mode == Mode.PRIVATE:
            candidates = candidates[:1]
        elif query.mode == Mode.RESEARCH:
            candidates = candidates[: max(1, self.settings.research_fanout)]

        plan = Plan(resolved_engine="auto", organic=candidates, skipped=skipped)
        if query.mode != Mode.PRIVATE and query.offset == 0:
            plan.enrichers = [self.registry[n] for n in self.settings.enricher_names()
                              if n in self.registry and self.block_reason(self.registry[n]) is None]
            if local_planned:
                plan.local = self.registry[LOCAL_ENGINE]
        return plan

    def _plan_named(self, query: SearchQuery) -> Plan:
        engine = self.registry.get(query.engine)
        if engine is None:
            raise PlanError(400, f"Unsupported `{query.engine}` search engine.")
        reason = self.block_reason(engine)
        if reason:
            status = 503 if reason.startswith("not configured") else 403
            raise PlanError(status, f"{engine.name}: {reason}")
        if query.safe and engine.role != Role.LOCAL and "safe" not in engine.applied(query):
            raise PlanError(400, f"{engine.name} cannot enforce safe search; refusing to run "
                                 "the query unfiltered.")
        ignored = query.requested_params() - engine.applied(query)
        if self._strict(query) and ignored:
            raise PlanError(400, f"{engine.name} cannot apply: {', '.join(sorted(ignored))}")
        if engine.role == Role.LOCAL:
            return Plan(resolved_engine=engine.name, organic=[], local=engine)
        return Plan(resolved_engine=engine.name, organic=[engine])

    def _strict(self, query: SearchQuery) -> bool:
        return query.strict_params or self.settings.strict_params

    def storage_forbidden(self, plan: Plan) -> list[str]:
        """Engines in the plan whose terms forbid keeping any copy (async needs one)."""
        engines = [*plan.organic, *plan.enrichers, *([plan.local] if plan.local else [])]
        return [e.name for e in engines if self.storage_limit(e) == 0]

    # ------------------------------------------------------------------ execution

    async def run(self, query: SearchQuery, plan: Plan, search_id: str, api_key: str,
                  requested_engine: str, use_cache: bool = True) -> SearchOutcome:
        started = time.monotonic()
        state = RunState(api_key, started + self.settings.request_deadline_seconds, use_cache)
        side = [*plan.enrichers, *([plan.local] if plan.local and plan.organic else [])]
        side_tasks = {e.name: asyncio.create_task(self._call(e, query, state)) for e in side}

        if plan.organic:
            primary = await self._organic(query, plan, state)
        else:
            primary = [await self._call(plan.local, query, state)]

        enrichment = await self._collect(side_tasks, started, state.deadline)
        outcome = self._assemble(query, plan, primary, enrichment, search_id,
                                 requested_engine, started)
        await self._settle_budgets([*primary, *enrichment], outcome)
        return outcome

    async def _organic(self, query: SearchQuery, plan: Plan,
                       state: RunState) -> list[Fragment]:
        if query.mode == Mode.RESEARCH and len(plan.organic) > 1:
            deep = query.model_copy(update={"offset": 0, "num": min(query.offset + query.num, 50)})
            return list(await asyncio.gather(
                *(self._call(e, deep, state) for e in plan.organic)))
        fragments: list[Fragment] = []
        for engine in plan.organic:
            fragment = await self._call(engine, query, state)
            fragments.append(fragment)
            if fragment.report.status == ProviderStatus.OK:
                break
            if time.monotonic() >= state.deadline:
                break
        return fragments

    async def _collect(self, tasks: dict[str, asyncio.Task], started: float,
                       deadline: float) -> list[Fragment]:
        if not tasks:
            return []
        now = time.monotonic()
        budget_end = max(started + self.settings.enrichment_budget_seconds,
                         now + ENRICHMENT_GRACE_SECONDS)
        timeout = max(0.0, min(budget_end, deadline) - now)
        done, pending = await asyncio.wait(tasks.values(), timeout=timeout)
        fragments = []
        for name, task in tasks.items():
            if task in done:
                fragments.append(task.result())
                continue
            task.cancel()
            engine = self.registry[name]
            fragments.append(Fragment(engine, ProviderReport(
                engine=name, role=engine.role.value, status=ProviderStatus.TIMEOUT,
                error="enrichment time budget exceeded")))
        await asyncio.gather(*pending, return_exceptions=True)
        return fragments

    async def _call(self, engine: Engine, query: SearchQuery, state: RunState) -> Fragment:
        applied = engine.applied(query)
        report = ProviderReport(
            engine=engine.name, role=engine.role.value, status=ProviderStatus.OK,
            applied_params=sorted(applied),
            ignored_params=sorted(query.requested_params() - applied),
        )
        storage = self.storage_limit(engine)
        key = _fragment_key(engine.name, query)
        cache_ttl = self.settings.cache_ttl_seconds if storage is None else min(
            self.settings.cache_ttl_seconds, storage)
        hit = await self.cache.get_json(key) if cache_ttl > 0 and state.use_cache else None
        if hit is not None:
            if hit["expires_at"] is None or hit["expires_at"] > time.time():
                output = EngineOutput.model_validate(hit["output"])
                report.cached = True
                return self._finish(engine, query, report, output, hit["expires_at"])

        started = time.monotonic()
        fragment = Fragment(engine, report)
        try:
            await self.breaker.check(engine.name)
            fragment.budget = await self.budgets.spend(engine.name, state.api_key)
            remaining = state.deadline - time.monotonic()
            if remaining <= 0:
                raise EngineError(engine.name, "request deadline reached", ErrorKind.TIMEOUT)
            async with asyncio.timeout(remaining):
                output = await engine.search(query, self.ctx)
        except TimeoutError:
            error = EngineError(engine.name, "request deadline reached", ErrorKind.TIMEOUT)
            await self.breaker.record(engine.name, error)
            return self._failed(fragment, error, started)
        except EngineError as error:
            await self.breaker.record(engine.name, error)
            return self._failed(fragment, error, started)
        await self.breaker.record(engine.name, None)
        report.latency_ms = int((time.monotonic() - started) * 1000)
        expires_at = None if storage is None else time.time() + storage
        if cache_ttl > 0:
            await self.cache.set_json(key, {"output": output.model_dump(mode="json"),
                                            "expires_at": expires_at}, cache_ttl)
        result = self._finish(engine, query, report, output, expires_at)
        result.budget = fragment.budget
        return result

    def _finish(self, engine: Engine, query: SearchQuery, report: ProviderReport,
                output: EngineOutput, expires_at: float | None) -> Fragment:
        vertical = Vertical.LOCAL if engine.role == Role.LOCAL else query.vertical
        if engine.role == Role.ENRICHMENT:
            empty = output.entity is None and output.answer is None and not output.related
        else:
            empty = output.is_empty(vertical)
        report.status = ProviderStatus.EMPTY if empty else ProviderStatus.OK
        report.results = len(output.places if vertical == Vertical.LOCAL else output.web)
        return Fragment(engine, report, output, expires_at)

    def _failed(self, fragment: Fragment, error: EngineError, started: float) -> Fragment:
        status = {
            ErrorKind.RATE_LIMITED: ProviderStatus.RATE_LIMITED,
            ErrorKind.THROTTLED: ProviderStatus.RATE_LIMITED,
            ErrorKind.TIMEOUT: ProviderStatus.TIMEOUT,
        }.get(error.kind, ProviderStatus.FAILED)
        fragment.report.status = status
        fragment.report.error = error.message
        fragment.report.retry_after = error.retry_after
        fragment.report.latency_ms = int((time.monotonic() - started) * 1000)
        return fragment

    async def _settle_budgets(self, fragments: list[Fragment], outcome: SearchOutcome) -> None:
        """Budget slots are only refunded when the upstream was never reached."""
        for fragment in fragments:
            if fragment.budget and fragment.report.status == ProviderStatus.RATE_LIMITED:
                await self.budgets.refund(fragment.budget)

    # ------------------------------------------------------------------ assembly

    def _assemble(self, query: SearchQuery, plan: Plan, primary: list[Fragment],
                  enrichment: list[Fragment], search_id: str, requested_engine: str,
                  started: float) -> SearchOutcome:
        local_vertical = not plan.organic
        ok_primary = [f for f in primary if f.report.status == ProviderStatus.OK]
        contributing = [*ok_primary, *(f for f in enrichment
                                       if f.report.status == ProviderStatus.OK)]
        outcome = SearchOutcome(
            id=search_id, status=OutcomeStatus.SUCCESS,
            created_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            query=query, requested_engine=requested_engine,
            resolved_engine=plan.resolved_engine, params=ParamReport(),
            providers=[*plan.skipped, *(f.report for f in primary),
                       *(f.report for f in enrichment)],
        )
        if local_vertical:
            if ok_primary:
                outcome.places = ok_primary[0].output.places
                outcome.area = ok_primary[0].output.area
                outcome.has_more = bool(ok_primary[0].output.has_more)
        else:
            self._merge_organic(outcome, query, ok_primary)
        for fragment in contributing:
            self._merge_blocks(outcome, fragment)

        applied: set[str] = set()
        for fragment in ok_primary[:1] or primary[:1]:
            applied |= set(fragment.report.applied_params)
        if any(f.engine.role == Role.LOCAL and f.output and f.output.places
               for f in contributing):
            applied.add("location")
        requested = query.requested_params()
        outcome.params = ParamReport(applied=sorted(applied & requested),
                                     ignored=sorted(requested - applied))

        for fragment in contributing:
            rights = self.rights(fragment.engine)
            outcome.rights[fragment.engine.name] = rights.as_dict()
            if rights.attribution and rights.attribution not in outcome.attribution:
                outcome.attribution.append(rights.attribution)
        deadlines = [f.expires_at for f in contributing if f.expires_at is not None]
        if deadlines:
            earliest = min(deadlines)
            outcome.retention = Retention(
                expires_at=datetime.fromtimestamp(earliest, UTC).isoformat(timespec="seconds"),
                reason="shortest storage limit among contributing providers")

        outcome.status, outcome.error = _classify(primary, enrichment, local_vertical)
        outcome.billable = outcome.status in (OutcomeStatus.SUCCESS, OutcomeStatus.PARTIAL,
                                              OutcomeStatus.EMPTY) and not all(
            f.report.cached for f in primary if f.output is not None)
        return outcome

    def _merge_organic(self, outcome: SearchOutcome, query: SearchQuery,
                       ok: list[Fragment]) -> None:
        if not ok:
            return
        if len(ok) == 1:
            fragment = ok[0]
            outcome.results = rank_single(fragment.engine.name,
                                          self.rights(fragment.engine).provenance.value,
                                          fragment.output.web, offset=query.offset)
            outcome.has_more = bool(fragment.output.has_more)
            outcome.total_results = fragment.output.total_results
            return
        lists = {f.engine.name: (self.rights(f.engine).provenance.value, f.output.web)
                 for f in ok}
        fused = rrf(lists, limit=query.offset + query.num)
        outcome.results = fused[query.offset:]
        for position, result in enumerate(outcome.results, start=query.offset + 1):
            result.rank = position
        outcome.has_more = any(f.output.has_more for f in ok)

    @staticmethod
    def _merge_blocks(outcome: SearchOutcome, fragment: Fragment) -> None:
        """Fill gaps only; the organic provider's own blocks win over enrichment."""
        output = fragment.output
        if output.entity and outcome.entity is None:
            outcome.entity = output.entity
        if output.answer and outcome.answer is None:
            outcome.answer = output.answer
        if output.places and not outcome.places:
            outcome.places = output.places
            outcome.area = output.area
        for note in output.notes:
            if note not in outcome.notes:
                outcome.notes.append(note)
        if output.questions and not outcome.questions:
            outcome.questions = output.questions
        for related in output.related:
            if related not in outcome.related:
                outcome.related.append(related)


def _classify(primary: list[Fragment], enrichment: list[Fragment],
              local_vertical: bool) -> tuple[OutcomeStatus, str | None]:
    statuses = [f.report.status for f in primary]
    failures = [f for f in [*primary, *enrichment] if f.report.status in (
        ProviderStatus.FAILED, ProviderStatus.RATE_LIMITED, ProviderStatus.TIMEOUT)]
    if ProviderStatus.OK in statuses:
        if failures:
            names = ", ".join(f.engine.name for f in failures)
            return OutcomeStatus.PARTIAL, f"degraded: {names} did not answer"
        return OutcomeStatus.SUCCESS, None
    primary_failures = [f for f in primary if f.report.status != ProviderStatus.EMPTY]
    if not primary_failures:
        return OutcomeStatus.EMPTY, "fairserp hasn't returned any results for this query."
    detail = "; ".join(f"{f.engine.name}: {f.report.error}" for f in primary_failures)
    if all(f.report.status == ProviderStatus.RATE_LIMITED for f in primary_failures):
        return OutcomeStatus.RATE_LIMITED, f"upstream limits reached ({detail})"
    return OutcomeStatus.FAILED, f"no provider answered ({detail})"


def _skip(engine: Engine, reason: str) -> ProviderReport:
    return ProviderReport(engine=engine.name, role=engine.role.value,
                          status=ProviderStatus.SKIPPED, error=reason)


def _no_engine_message(query: SearchQuery, skipped: list[ProviderReport]) -> str:
    if query.safe:
        return ("Safe search was requested but no enabled organic engine can enforce it; "
                "refusing to run the query unfiltered.")
    return ("No web search provider is enabled on this server. Add a provider key (for "
            "example FAIRSERP_BRAVE_API_KEY) or opt in to a keyless provider "
            "(FAIRSERP_OPT_IN=firecrawl). GET /v1/engines lists why each one is off.")


def _fragment_key(engine: str, query: SearchQuery) -> str:
    material = json.dumps(query.cache_material(engine), sort_keys=True, default=str)
    return f"frag:{engine}:{hashlib.sha256(material.encode()).hexdigest()}"

