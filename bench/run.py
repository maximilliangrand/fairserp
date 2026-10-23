"""Benchmark: does a configuration find relevant, fresh, non-duplicated evidence, and at
what latency and cost?

Usage: uv run python bench/run.py [--configs a,b] [--limit N]

Queries (bench/queries.jsonl) are stratified across technical, navigational, ambiguous
entity, news, multilingual and local intents. Relevance is judged automatically against
hand-written URL patterns per query (an acceptable-source list, not a graded human
judgment), so treat the numbers as a regression and comparison signal, not as absolute
quality. Raw per-query outputs go to bench/runs/ (git-ignored: several providers' terms
forbid redistributing results); only aggregates are written to bench/results/.

Latency: upstream fragments are cached across configurations so each provider is called
once per query (keyless quotas are small). A configuration's latency per query is therefore
computed from the providers' own measured latencies: the sum along the fast-mode fallback
chain, the maximum across research-mode fan-out.
"""

import argparse
import asyncio
import json
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from fairserp.canonical import canonical_url
from fairserp.config import Settings
from fairserp.core import PlanError, Searcher
from fairserp.engines import build_registry
from fairserp.http import HttpPool
from fairserp.model import Mode, OutcomeStatus, ProviderStatus, SearchQuery, Vertical
from fairserp.store import MemoryStore

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Config:
    name: str
    engine: str = "auto"
    mode: Mode = Mode.FAST
    priority: str = ""
    enrichers: str = ""
    local_only: bool = False


CONFIGS = [
    Config("wikipedia", engine="wikipedia"),
    Config("marginalia", engine="marginalia"),
    Config("firecrawl", engine="firecrawl"),
    Config("auto-fast", priority="firecrawl", enrichers="wikidata,duckduckgo_ia"),
    Config("research-3", mode=Mode.RESEARCH, priority="firecrawl,marginalia,wikipedia",
           enrichers="wikidata,duckduckgo_ia"),
    Config("research-2", mode=Mode.RESEARCH, priority="firecrawl,wikipedia",
           enrichers="wikidata,duckduckgo_ia"),
    Config("local", engine="openstreetmap", local_only=True),
]


def settings_for(config: Config) -> Settings:
    return Settings(
        _env_file=None, opt_in="firecrawl,marginalia,searxng,tavily",
        noncommercial_deployment=True,  # benchmarking is noncommercial evaluation
        organic_priority=config.priority or "firecrawl", enrichers=config.enrichers,
        upstream_budgets="", research_fanout=3, request_deadline_seconds=20,
        enrichment_budget_seconds=4,
    )


def query_for(item: dict, config: Config) -> SearchQuery:
    if item["category"] == "local":
        return SearchQuery(q=item["q"], location=item["location"], vertical=Vertical.LOCAL,
                           engine="openstreetmap", num=10)
    return SearchQuery(q=item["q"], engine=config.engine, mode=config.mode, num=10,
                       language=item.get("language"), freshness=item.get("freshness"))


def judge(item: dict, outcome) -> dict:
    patterns = [re.compile(p) for p in item.get("relevant", [])]
    canon = [canonical_url(r.url) for r in outcome.results[:10]]
    hits = [i for i, url in enumerate(canon) if any(p.search(url) for p in patterns)]
    domains = [url.split("/")[2] if "//" in url else url for url in canon]
    repeats = sum(1 for i, d in enumerate(domains) if d in domains[:i])
    raw = sum(p.results for p in outcome.providers
              if p.role == "organic" and p.status == ProviderStatus.OK)
    top5 = outcome.results[:5]
    dated = [r for r in top5 if r.published_at]
    ages = []
    for result in dated:
        try:
            published = datetime.fromisoformat(result.published_at[:10]).date()
            ages.append((date.today() - published).days)
        except ValueError:
            continue
    return {
        "status": outcome.status.value,
        "n_results": len(outcome.results),
        "hit_at_1": bool(hits and hits[0] == 0),
        "hit_at_3": bool(hits and hits[0] < 3),
        "hit_at_10": bool(hits),
        "rr": 1 / (hits[0] + 1) if hits else 0.0,
        "domain_repeat_rate": repeats / len(canon) if canon else 0.0,
        "overlap_or_dupes": max(0, raw - len(outcome.results)) / raw if raw else 0.0,
        "dated_share_top5": len(dated) / len(top5) if top5 else 0.0,
        "median_age_days": statistics.median(ages) if ages else None,
        "freshness_applied": "freshness" not in outcome.params.ignored,
        "entity_ok": (outcome.entity is not None and outcome.entity.wikidata_id ==
                      item["wikidata"]) if item.get("wikidata") else None,
        "places": len(outcome.places),
        "local_ok": len(outcome.places) >= item.get("min_places", 3)
        if item["category"] == "local" else None,
    }


async def run(configs: list[Config], items: list[dict]) -> dict:
    cache = MemoryStore(100_000)
    latencies: dict[tuple[str, str], int] = {}
    costs: dict[str, float] = {}
    registry = build_registry(load_plugins=False)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    runs_dir = ROOT / "runs"
    runs_dir.mkdir(exist_ok=True)
    raw_log = (runs_dir / f"{stamp}.jsonl").open("w")
    summary: dict[str, dict] = {}
    for config in configs:
        settings = settings_for(config)
        http = HttpPool(settings)
        searcher = Searcher(settings, registry, http, cache, MemoryStore())
        rows: list[dict] = []
        for item in items:
            if (item["category"] == "local") != config.local_only:
                continue
            query = query_for(item, config)
            try:
                plan = searcher.plan(query)
            except PlanError as exc:
                rows.append({"id": item["id"], "category": item["category"],
                             "status": "refused", "error": exc.message})
                continue
            outcome = await searcher.run(query, plan, item["id"], "bench", query.engine)
            organic = [p for p in outcome.providers if p.role in ("organic", "local")]
            for provider in organic:
                if provider.latency_ms is not None and not provider.cached:
                    latencies[(provider.engine, item["id"])] = provider.latency_ms
                    costs[provider.engine] = registry[provider.engine].cost_per_call_usd
            measured = [latencies.get((p.engine, item["id"])) for p in organic
                        if p.status != ProviderStatus.SKIPPED]
            measured = [m for m in measured if m is not None]
            if measured:
                latency = max(measured) if config.mode == Mode.RESEARCH else sum(measured)
            else:
                latency = None
            row = {"id": item["id"], "category": item["category"], "latency_ms": latency,
                   "cost_usd": sum(costs.get(p.engine, 0.0) for p in organic),
                   **judge(item, outcome)}
            rows.append(row)
            raw_log.write(json.dumps({"config": config.name, **row,
                                      "providers": [p.model_dump() for p in outcome.providers],
                                      "urls": [r.url for r in outcome.results]}) + "\n")
            print(f"{config.name:11} {item['id']:8} {row['status']:12} "
                  f"hit@3={row['hit_at_3']!s:5} n={row['n_results']} lat={latency}",
                  file=sys.stderr)
        await http.close()
        summary[config.name] = aggregate(rows)
    raw_log.close()
    return {"generated_at": stamp, "queries": len(items), "configs": summary}


def aggregate(rows: list[dict]) -> dict:
    answered = [r for r in rows if r.get("status") in (OutcomeStatus.SUCCESS,
                                                       OutcomeStatus.PARTIAL)]
    # Latency only for answered queries: an instant circuit-open refusal is not a fast search.
    latencies = sorted(r["latency_ms"] for r in answered if r.get("latency_ms") is not None)
    by_category: dict[str, dict] = {}
    for category in sorted({r["category"] for r in rows}):
        group = [r for r in rows if r["category"] == category]
        judged = [r for r in group if "hit_at_10" in r]
        by_category[category] = {
            "n": len(group),
            "hit_at_3": _mean(r["hit_at_3"] for r in judged) if judged else None,
            "mrr_at_10": _mean(r["rr"] for r in judged) if judged else None,
        }
        entity = [r["entity_ok"] for r in group if r.get("entity_ok") is not None]
        if entity:
            by_category[category]["entity_accuracy"] = _mean(entity)
        local = [r["local_ok"] for r in group if r.get("local_ok") is not None]
        if local:
            by_category[category]["local_success"] = _mean(local)
        if category == "news":
            by_category[category]["dated_share_top5"] = _mean(
                r["dated_share_top5"] for r in judged) if judged else None
            ages = [r["median_age_days"] for r in judged if r["median_age_days"] is not None]
            by_category[category]["median_age_days"] = statistics.median(ages) if ages else None
            by_category[category]["freshness_applied"] = _mean(
                r["freshness_applied"] for r in judged) if judged else None
    judged = [r for r in rows if "hit_at_10" in r and r["category"] != "local"]
    return {
        "n": len(rows),
        "answered_rate": round(len(answered) / len(rows), 3) if rows else None,
        "status_counts": {s: sum(1 for r in rows if r.get("status") == s)
                          for s in sorted({r.get("status") for r in rows})},
        "hit_at_1": _mean(r["hit_at_1"] for r in judged) if judged else None,
        "hit_at_3": _mean(r["hit_at_3"] for r in judged) if judged else None,
        "hit_at_10": _mean(r["hit_at_10"] for r in judged) if judged else None,
        "mrr_at_10": _mean(r["rr"] for r in judged) if judged else None,
        "domain_repeat_rate": _mean(r["domain_repeat_rate"] for r in judged) if judged else None,
        "overlap_or_dupes_removed": _mean(r["overlap_or_dupes"] for r in judged)
        if judged else None,
        "latency_p50_ms": _percentile(latencies, 50),
        "latency_p95_ms": _percentile(latencies, 95),
        "cost_per_1k_usd": round(1000 * _mean(r.get("cost_usd", 0.0) for r in rows), 2)
        if rows else None,
        "by_category": by_category,
    }


def _mean(values) -> float | None:
    values = [float(v) for v in values]
    return round(sum(values) / len(values), 3) if values else None


def _percentile(values: list[int], pct: int) -> int | None:
    if not values:
        return None
    index = min(len(values) - 1, max(0, round(pct / 100 * len(values)) - 1))
    return values[index]


def markdown(result: dict) -> str:
    configs = result["configs"]
    lines = [f"# Benchmark results ({result['generated_at']})", "",
             f"{result['queries']} queries. Aggregates only; see bench/run.py for method and "
             "caveats (automatic URL-pattern relevance, small n, keyless providers only).", "",
             "| Config | n | answered | hit@1 | hit@3 | hit@10 | MRR@10 | domain repeats | "
             "p50 ms | p95 ms | $/1k |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, data in configs.items():
        lines.append(
            f"| {name} | {data['n']} | {data['answered_rate']} | {data['hit_at_1']} | "
            f"{data['hit_at_3']} | {data['hit_at_10']} | {data['mrr_at_10']} | "
            f"{data['domain_repeat_rate']} | {data['latency_p50_ms']} | "
            f"{data['latency_p95_ms']} | {data['cost_per_1k_usd']} |")
    lines += ["", "## hit@3 by category", ""]
    categories = sorted({c for d in configs.values() for c in d["by_category"]})
    lines.append("| Config | " + " | ".join(categories) + " |")
    lines.append("|---|" + "---|" * len(categories))
    for name, data in configs.items():
        cells = []
        for category in categories:
            entry = data["by_category"].get(category)
            if not entry:
                cells.append("-")
            elif category == "local":
                cells.append(str(entry.get("local_success")))
            else:
                cells.append(str(entry.get("hit_at_3")))
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += ["", "## Entities and news", ""]
    for name, data in configs.items():
        entity = data["by_category"].get("entity", {})
        news = data["by_category"].get("news", {})
        if entity.get("entity_accuracy") is not None or news:
            lines.append(f"- **{name}**: knowledge-graph accuracy "
                         f"{entity.get('entity_accuracy')}, news dated share "
                         f"{news.get('dated_share_top5')}, median age days "
                         f"{news.get('median_age_days')}, freshness applied "
                         f"{news.get('freshness_applied')}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--configs", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--reaggregate", default="",
                        help="recompute aggregates from a bench/runs/*.jsonl file")
    args = parser.parse_args()
    if args.reaggregate:
        rows = [json.loads(line) for line in Path(args.reaggregate).read_text().splitlines()]
        names = list(dict.fromkeys(r["config"] for r in rows))
        stamp = Path(args.reaggregate).stem
        result = {"generated_at": stamp, "queries": len({r["id"] for r in rows}),
                  "configs": {n: aggregate([r for r in rows if r["config"] == n])
                              for n in names}}
        _write(result)
        return
    items = [json.loads(line) for line in (ROOT / "queries.jsonl").read_text().splitlines()]
    if args.limit:
        items = items[: args.limit]
    wanted = set(filter(None, args.configs.split(",")))
    configs = [c for c in CONFIGS if not wanted or c.name in wanted]
    _write(asyncio.run(run(configs, items)))


def _write(result: dict) -> None:
    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    day = result["generated_at"][:8]
    (out / f"{day}.json").write_text(json.dumps(result, indent=1) + "\n")
    (out / f"{day}.md").write_text(markdown(result))
    print(markdown(result))


if __name__ == "__main__":
    main()
