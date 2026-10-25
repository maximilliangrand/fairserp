"""Planner policy, outcome classification, retention, budgets, circuits and deadlines."""

import time

import pytest

from fairserp.core import PlanError, Searcher
from fairserp.engines.base import ErrorKind, Role
from fairserp.http import HttpPool
from fairserp.model import EngineOutput, Mode, OutcomeStatus, ProviderStatus, SearchQuery
from fairserp.rights import CommercialUse, Provenance
from fairserp.store import MemoryStore
from tests import stubs
from tests.conftest import settings


def searcher(*engines, **overrides) -> Searcher:
    values = {"organic_priority": ",".join(e.name for e in engines if e.role == Role.ORGANIC),
              "upstream_budgets": "", **overrides}
    s = settings(**values)
    registry = {e.name: e for e in engines}
    registry.setdefault("wikidata", stubs.Stub("wikidata", Role.ENRICHMENT))
    registry.setdefault("duckduckgo_ia", stubs.Stub("duckduckgo_ia", Role.ENRICHMENT))
    registry.setdefault("openstreetmap", stubs.Stub("openstreetmap", Role.LOCAL))
    return Searcher(s, registry, HttpPool(s), MemoryStore(1000), MemoryStore())


async def run(searcher_: Searcher, query: SearchQuery, api_key: str = "good"):
    plan = searcher_.plan(query)
    return await searcher_.run(query, plan, "abc", api_key, query.engine)


# ------------------------------------------------------------------ policy


def test_strict_profile_excludes_undisclosed_provenance_without_opt_in():
    hidden = stubs.organic("firecrawl", rights_=stubs.rights(Provenance.UNDISCLOSED))
    with pytest.raises(PlanError) as exc:
        searcher(hidden).plan(SearchQuery(q="coffee"))
    assert exc.value.status == 503 and "FAIRSERP_OPT_IN=firecrawl" in exc.value.message
    assert searcher(hidden, opt_in="firecrawl").plan(SearchQuery(q="coffee")).organic


def test_named_engine_blocked_by_policy_is_403():
    hidden = stubs.organic("firecrawl", rights_=stubs.rights(Provenance.UNDISCLOSED))
    with pytest.raises(PlanError) as exc:
        searcher(hidden).plan(SearchQuery(q="coffee", engine="firecrawl"))
    assert exc.value.status == 403


def test_noncommercial_sources_need_a_declaration():
    marginalia = stubs.organic("marginalia",
                               rights_=stubs.rights(commercial=CommercialUse.NONCOMMERCIAL))
    for overrides in ({}, {"opt_in": "marginalia"}):
        with pytest.raises(PlanError) as exc:
            searcher(marginalia, **overrides).plan(SearchQuery(q="x"))
        assert exc.value.code == "no_engine_enabled"
        assert "noncommercial" in exc.value.details[0]["reason"]
    assert searcher(marginalia, noncommercial_deployment=True).plan(SearchQuery(q="x")).organic


def test_safe_search_is_never_relaxed():
    unsafe = stubs.organic("plain")
    safe = stubs.organic("careful", supports=frozenset({"safe"}))
    plan = searcher(unsafe, safe).plan(SearchQuery(q="x", safe=True))
    assert [e.name for e in plan.organic] == ["careful"]
    assert plan.skipped[0].error == "cannot enforce safe search"
    with pytest.raises(PlanError) as exc:
        searcher(unsafe).plan(SearchQuery(q="x", safe=True))
    assert exc.value.status == 400


def test_strict_params_reject_instead_of_ignoring():
    plain = stubs.organic("plain")
    with pytest.raises(PlanError) as exc:
        searcher(plain).plan(SearchQuery(q="x", freshness="d", strict_params=True))
    assert exc.value.status == 400 and exc.value.code == "params_unsupported"
    assert "can apply freshness" in exc.value.message
    assert searcher(plain).plan(SearchQuery(q="x", freshness="d")).organic


async def test_ignored_params_are_reported():
    outcome = await run(searcher(stubs.organic("plain")),
                        SearchQuery(q="x", freshness="d", country="de"))
    assert outcome.params.ignored == ["country", "freshness"]
    assert outcome.providers[0].ignored_params == ["country", "freshness"]


def test_private_mode_uses_one_upstream_and_no_enrichment():
    plan = searcher(stubs.organic("a"), stubs.organic("b"), stubs.entity_engine()).plan(
        SearchQuery(q="x", mode=Mode.PRIVATE))
    assert [e.name for e in plan.organic] == ["a"] and plan.enrichers == []


# ------------------------------------------------------------------ outcomes


async def test_failover_on_failure_is_partial_and_reported():
    first, second = stubs.failing("a"), stubs.organic("b")
    outcome = await run(searcher(first, second), SearchQuery(q="x"))
    assert outcome.status == OutcomeStatus.PARTIAL
    assert [p.status for p in outcome.providers[:2]] == [ProviderStatus.FAILED,
                                                         ProviderStatus.OK]
    assert outcome.results[0].sources[0].engine == "b"


async def test_empty_answer_falls_through_to_next_engine():
    empty = stubs.Stub("a", output=EngineOutput())
    outcome = await run(searcher(empty, stubs.organic("b")), SearchQuery(q="x"))
    assert outcome.status == OutcomeStatus.SUCCESS
    assert [p.status for p in outcome.providers[:2]] == [ProviderStatus.EMPTY, ProviderStatus.OK]


async def test_all_empty_is_empty_not_failure():
    outcome = await run(searcher(stubs.Stub("a"), stubs.Stub("b")), SearchQuery(q="zzqx"))
    assert outcome.status == OutcomeStatus.EMPTY and outcome.billable


async def test_upstream_failure_is_never_empty_success():
    outcome = await run(searcher(stubs.failing("a"), stubs.Stub("b")), SearchQuery(q="x"))
    assert outcome.status == OutcomeStatus.FAILED and not outcome.billable


async def test_rate_limits_are_classified():
    outcome = await run(searcher(stubs.failing("a", ErrorKind.RATE_LIMITED, 30)),
                        SearchQuery(q="x"))
    assert outcome.status == OutcomeStatus.RATE_LIMITED
    assert outcome.providers[0].retry_after == 30


async def test_enrichment_blocks_and_attribution():
    org = stubs.organic("a", rights_=stubs.rights(attribution="Results from A"))
    outcome = await run(searcher(org, stubs.entity_engine(), stubs.answer_engine()),
                        SearchQuery(q="coffee"))
    assert outcome.entity.wikidata_id == "Q8486"
    assert outcome.answer.text == "42" and outcome.related == ["coffee beans"]
    assert "Results from A" in outcome.attribution
    assert set(outcome.rights) == {"a", "wikidata", "duckduckgo_ia"}


async def test_slow_enrichment_cannot_stall_organic_results():
    slow = stubs.entity_engine(delay=5)
    s = searcher(stubs.organic("a"), slow, enrichment_budget_seconds=0.2)
    started = time.monotonic()
    outcome = await run(s, SearchQuery(q="coffee"))
    assert time.monotonic() - started < 1.0
    assert outcome.status == OutcomeStatus.PARTIAL
    timed_out = [p for p in outcome.providers if p.engine == "wikidata"][0]
    assert timed_out.status == ProviderStatus.TIMEOUT
    assert outcome.results


async def test_overall_deadline_bounds_a_hanging_provider():
    s = searcher(stubs.organic("a", delay=5), request_deadline_seconds=0.3)
    started = time.monotonic()
    outcome = await run(s, SearchQuery(q="x"))
    assert time.monotonic() - started < 1.0
    assert outcome.providers[0].status == ProviderStatus.TIMEOUT
    assert outcome.status == OutcomeStatus.FAILED


async def test_local_results_when_query_has_local_intent():
    s = searcher(stubs.organic("a"), stubs.local_engine())
    outcome = await run(s, SearchQuery(q="coffee near Austin"))
    assert outcome.places[0].name == "Cafe One"
    assert "location" not in outcome.params.ignored


# ------------------------------------------------------------------ retention and cache


async def test_storage_forbidden_results_get_an_immediate_deadline_and_no_cache():
    brave = stubs.organic("brave", rights_=stubs.rights(storage=0))
    s = searcher(brave)
    first = await run(s, SearchQuery(q="x"))
    await run(s, SearchQuery(q="x"))
    assert len(brave.calls) == 2
    assert first.retention.expires_at is not None


async def test_retention_is_the_shortest_contributing_limit():
    org = stubs.organic("mojeek", rights_=stubs.rights(storage=3600))
    outcome = await run(searcher(org, stubs.entity_engine()), SearchQuery(q="coffee"))
    deadline = time.time() + 3600
    from datetime import datetime
    assert abs(datetime.fromisoformat(outcome.retention.expires_at).timestamp() - deadline) < 5


async def test_fragments_are_cached_independently():
    brave = stubs.organic("brave", rights_=stubs.rights(storage=0))
    wikidata = stubs.entity_engine()
    s = searcher(brave, wikidata)
    await run(s, SearchQuery(q="coffee"))
    second = await run(s, SearchQuery(q="coffee"))
    assert len(brave.calls) == 2 and len(wikidata.calls) == 1
    assert [p.cached for p in second.providers if p.engine == "wikidata"] == [True]


async def test_cache_hit_keeps_original_deadline_and_is_not_billable():
    org = stubs.organic("mojeek", rights_=stubs.rights(storage=3600))
    s = searcher(org)
    first = await run(s, SearchQuery(q="x"))
    second = await run(s, SearchQuery(q="x"))
    assert second.retention.expires_at == first.retention.expires_at
    assert first.billable and not second.billable


# ------------------------------------------------------------------ budgets and circuits


async def test_per_key_upstream_budget():
    org = stubs.organic("firecrawl")
    s = searcher(org, upstream_budgets="firecrawl:100/day,firecrawl:2/day/key")
    statuses = []
    for i in range(3):
        outcome = await run(s, SearchQuery(q=f"q{i}"))
        statuses.append(outcome.status)
    other = await run(s, SearchQuery(q="q9"), api_key="other")
    assert statuses == [OutcomeStatus.SUCCESS, OutcomeStatus.SUCCESS, OutcomeStatus.RATE_LIMITED]
    assert other.status == OutcomeStatus.SUCCESS
    assert len(org.calls) == 3


async def test_circuit_opens_after_repeated_failures_and_honors_retry_after():
    broken = stubs.failing("a")
    s = searcher(broken, circuit_failures=2)
    for i in range(3):
        await run(s, SearchQuery(q=f"q{i}"))
    assert len(broken.calls) == 2
    limited = stubs.failing("b", ErrorKind.RATE_LIMITED, retry_after=120)
    s2 = searcher(limited)
    await run(s2, SearchQuery(q="x"))
    outcome = await run(s2, SearchQuery(q="y"))
    assert len(limited.calls) == 1
    assert outcome.providers[0].retry_after >= 119


# ------------------------------------------------------------------ fusion


async def test_research_mode_fuses_and_keeps_provider_ranks():
    a = stubs.Stub("a", output=EngineOutput(web=stubs.web("shared", 3) + stubs.web("a", 2)))
    b = stubs.Stub("b", output=EngineOutput(web=stubs.web("b", 2) + stubs.web("shared", 3)))
    outcome = await run(searcher(a, b), SearchQuery(q="x", mode=Mode.RESEARCH))
    urls = [r.url for r in outcome.results]
    assert len(urls) == len(set(urls)) == 7
    top = outcome.results[0]
    assert top.url == "https://shared.example/0"
    assert {s.engine: s.rank for s in top.sources} == {"a": 1, "b": 3}


async def test_a_provider_bug_fails_that_provider_not_the_search():
    shape = stubs.Stub("wikidata", Role.ENRICHMENT, error=KeyError("entities"))
    crash = stubs.Stub("duckduckgo_ia", Role.ENRICHMENT, error=RuntimeError("boom"))
    outcome = await run(searcher(stubs.organic("a"), shape, crash), SearchQuery(q="coffee"))
    reports = {r.engine: r for r in outcome.providers}
    assert outcome.status == "partial" and len(outcome.results) == 10
    assert reports["wikidata"].status == "failed" and "payload" in reports["wikidata"].error
    assert reports["duckduckgo_ia"].error == "internal error: RuntimeError"
