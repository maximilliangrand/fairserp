# Benchmark

`bench/run.py` runs a stratified query set (`bench/queries.jsonl`, 50 queries) through each
configuration and reports relevance, duplication, freshness, latency and cost. Only
aggregates are committed (`bench/results/`); raw per-query outputs stay in the git-ignored
`bench/runs/` because several providers' terms forbid redistributing results.

```bash
uv run python bench/run.py                       # all configurations
uv run python bench/run.py --configs firecrawl,research-2
```

## Method and its limits

- **Strata:** technical (10), navigational (10), ambiguous entities (10), news (8),
  multilingual (6), local (6).
- **Relevance** is judged automatically against hand-written acceptable-URL patterns per
  query (official docs, the official site, the entity's Wikipedia article, language-local
  domains). That makes it a regression and comparison signal, not a graded human judgment.
  hit@k means a relevant URL appears in the top k. MRR@10 is the mean reciprocal rank of
  the first relevant URL.
- **Entity accuracy:** the knowledge graph's Wikidata ID matches the expected entity.
- **Local success:** at least three places of the right category.
- **Latency:** each provider is called once per query (keyless quotas are small) and
  fragments are reused across configurations. A configuration's latency is computed from
  the providers' measured latencies: summed along a fast-mode fallback chain, the maximum
  across research-mode fan-out.
- **Cost:** list price per call for paid engines. Every engine in this run is keyless or
  free, so cost is $0.
- **Only keyless providers were measured.** Brave, Mojeek, Tavily, Exa, Parallel and Jina
  need keys this project does not hold. Google/SerpApi baselines were not collected: doing
  that by scraping is exactly what this project refuses to do.

## Results, 2026-10-23

Full table: [`results/20261023.md`](results/20261023.md).

**Provider availability dominated the run.**
- Firecrawl's keyless tier hit its per-IP daily cap after 17 queries. It answered 429 with
  `Retry-After: 81756` (about 22.7 hours), so the circuit breaker kept it closed for the rest
  of the run, as designed.
- Marginalia's shared `public` key was exhausted all day (0 of 44 answered).
- Unpaired totals therefore mostly measure who was up, not who ranks better.

**Paired comparison on the 17 queries where Firecrawl answered (10 technical, 7 navigational):**

| Config | hit@1 | hit@3 | MRR@10 | domain repeats in top 10 |
|---|---|---|---|---|
| firecrawl alone | 0.941 | 1.000 | 0.971 | 0.047 |
| research-2 (Firecrawl + Wikipedia, RRF) | 0.588 | 1.000 | 0.784 | 0.294 |
| research-3 (+ Marginalia, which was down) | 0.588 | 1.000 | 0.784 | 0.282 |
| wikipedia alone | 0.000 | 0.000 | 0.000 | 0.622 |

**Fusion did not help.** On navigational queries, RRF rewards agreement: the Wikipedia
article *about* a site (for example "European Central Bank") appears in both lists and
outranks the official site. On technical queries it adds Wikipedia noise. **Decision:**
`fast` stays the default. `research` mode stays available as an explicit, experimental
opt-in (it does raise coverage when the primary provider is down), and is documented as
not improving ranking. Fusing several *general-web* indexes (Brave + Mojeek + Marginalia),
the case RRF is built for, has not been measured without keys.

**What did work:**
- Wikidata knowledge graph: 7 of 7 entity queries resolved to the right Wikidata ID
  (accuracy 1.0), including the bare query "einstein".
- OpenStreetMap local: 6 of 6 local queries returned at least three places of the right
  category (p50 1.97 s, p95 3.28 s, bounded by the public Nominatim's 1 request/second).
- Wikipedia: 0.83 hit@3 on multilingual queries and 1.0 on entity queries; useless for
  navigational and technical queries, as expected.
- Firecrawl (when up): 1.0 hit@3 on technical, 0.7 on navigational; p50 693 ms, p95
  1766 ms. Latency percentiles count answered queries only (an instant circuit-open refusal
  is not a fast search). Wikipedia: p50 323 ms, p95 523 ms.
- Freshness: news queries could not be measured (Firecrawl was capped by then).

**Next run:** with a Brave or Mojeek key, compare single-index vs three-index fusion
(Brave + Mojeek + Marginalia) on all strata, and repeat Firecrawl on a fresh day.
