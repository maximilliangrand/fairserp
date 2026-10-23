# fairserp

A self-hostable search API that speaks SerpApi's wire format and answers from **official and openly licensed data sources only**.

Point existing SerpApi client code at your fairserp server by changing the base URL. The request params and response JSON stay the same: `search_metadata`, `organic_results`, `knowledge_graph`, `answer_box`, `related_questions`, `related_searches`, `local_results` and `serpapi_pagination`.

fairserp does **not** scrape Google or Bing, run headless browsers, solve CAPTCHAs or JavaScript challenges, or rotate proxies to dodge blocks. `docs/RESEARCH.md` explains why: in 2026 the live legal risk for SERP tools is DMCA § 1201 anti-circumvention (*Google v. SerpApi*, *Reddit v. SerpApi/Perplexity*), and it reaches tool authors and API customers.

## Quick start

```bash
FAIRSERP_API_KEYS=my-secret-key docker compose up -d
curl "http://localhost:8000/search.json?engine=google&q=coffee+near+Austin,+Texas&api_key=my-secret-key"
```

This works with **no upstream API keys at all**. Organic results come from Firecrawl Keyless, the knowledge graph and answer box from the DuckDuckGo Instant Answer API and Wikipedia, and local results from OpenStreetMap. Add Brave, Mojeek or Tavily keys to get stronger organic results.

Without Docker:

```bash
uv sync
FAIRSERP_API_KEYS=my-secret-key uv run python -m fairserp
```

## Using the official SerpApi clients

| Client | One-line change |
|---|---|
| `serpapi` (Python) | `serpapi.HTTPClient.BASE_DOMAIN = "http://localhost:8000"` |
| `google-search-results` (Python, legacy) | `serpapi.SerpApiClient.BACKEND = "http://localhost:8000"` |
| `serpapi` (npm) | pass a custom `https.Agent` in `config.requestOptions` that connects to your host |
| `google-search-results-nodejs` | patch `SerpApiSearch.prototype.buildUrl` to swap the host |
| LangChain `SerpAPIWrapper` | uses `google-search-results`, so set `SerpApiClient.BACKEND` as above |

```python
import serpapi

serpapi.HTTPClient.BASE_DOMAIN = "http://localhost:8000"
client = serpapi.Client(api_key="my-secret-key")
results = client.search(q="coffee", engine="google")
print(results["organic_results"][0]["link"])
print(results.next_page()["search_information"]["page_number"])  # pagination follows your host
```

`scripts/compat_check.py` runs search, pagination, the archive, account and locations through the official client. CI runs it against the Docker stack.

## Engines and their legal / ToS status

| Engine (`engine=`) | Source | Key | Status | Notes |
|---|---|---|---|---|
| `brave` | Brave Search API | paid (card on file) | **Official API** | Organic, FAQ, infobox, places. Caching off by default because Brave's terms limit storing results |
| `mojeek` | Mojeek Search API | paid (card) | **Official API** | Independent index. Cache capped at 1 hour |
| `tavily` | Tavily Search API | free tier, no card | **Official API** | 1,000 credits/month. LLM-oriented ranking |
| `firecrawl` | Firecrawl `/v2/search` | none needed | **Official API** (keyless tier) | Default organic source. Per-IP daily cap, and a key raises it |
| `marginalia` | Marginalia Search API | `public` or a free key | **Official API** | Small-web index. Results are CC-BY-NC-SA |
| `wikipedia` | MediaWiki Action and REST APIs | none | **Open data API** | Wikipedia articles plus the entity knowledge graph |
| `duckduckgo_ia` | DuckDuckGo Instant Answer API | none | **Open data API** | Knowledge graph, answer box, related searches. No organic links |
| `openstreetmap`, `google_maps`, `google_local` | OSM Nominatim | none | **Open data API** | Local results. Public instance limited to 1 req/s (enforced). Self-host for volume. ODbL attribution |
| `searxng` | your own SearXNG instance | none | **User-operated** | Status follows the upstream engines *your* instance enables. Scraping engines there are your responsibility |
| `auto` (`google`, `bing`, `duckduckgo` aliases) | composite of the above | none | inherits from its parts | Organic from the first working engine in `FAIRSERP_ORGANIC_PRIORITY`, enriched in parallel |
| Exa, You.com, Kagi, Yandex | official APIs | various | not built yet | Good next adapters. See "Adding an engine" |
| Google / Bing HTML scraping | n/a | n/a | **Will not ship** | ToS and robots.txt prohibit it, and § 1201 risk for bypassing bot protection |

Every response says which engines actually answered:

```json
"search_metadata": {
  "id": "36991c22174651ebe8dd3050",
  "status": "Success",
  "fairserp_sources": [
    {"engine": "firecrawl", "status": "ok"},
    {"engine": "duckduckgo_ia", "status": "ok"},
    {"engine": "wikipedia", "status": "ok"},
    {"engine": "openstreetmap", "status": "ok"}
  ]
}
```

`GET /engines` lists the engines with their compliance class and whether they are configured.

### What is never populated

`ads`: no licensed source exposes search ads. `immersive_products`, `ai_overview` and `top_stories`: not available from these sources yet. SerpApi itself omits blocks a page does not have, so clients already handle their absence.

## API surface

| Endpoint | Behavior |
|---|---|
| `GET /search`, `/search.json`, `/search.html`, `/search.md` | Search. Supports `output=json\|html\|md` |
| `GET /searches/{id}`, `/searches/{id}.json\|.md\|.html` | Search archive, visible only to the key that ran the search (31 days; 1 hour for results from engines whose terms forbid caching) |
| `GET /account`, `/account.json` | Usage for the calling key (`this_hour_searches`, `this_month_usage`, limit) |
| `GET /locations.json?q=&limit=` | Location lookup (Nominatim) with SerpApi field names. Unlike SerpApi it needs a key, to protect the shared Nominatim budget |
| `GET /engines` | Engine catalog and status |
| `GET /healthz` | Liveness |

Supported params: `q`, `engine`, `api_key` (also `serp_api_key` or `Authorization: Bearer`), `location`, `gl`, `hl`, `safe`, `start`, `num`, `no_cache`, `async`, `zero_trace`, `output` and `json_restrictor`, plus the Bing dialect (`mkt`, `cc`, `first`, `count`, `safeSearch`) and the DuckDuckGo dialect (`kl`, `safe=1|-1|-2`). `source` is accepted and ignored.

The SerpApi semantics fairserp keeps:

- The key is checked before other validation (401).
- Errors are always `{"error": "..."}`.
- An empty result set is HTTP 200 with `error` set and `organic_results_state: "Fully empty"`.
- Cached responses (exact params, 1 hour default) are free and do not count against the hourly limit, and neither do failed searches.
- `async=true` returns a `Processing` stub. You then poll `json_endpoint`.
- `no_cache` or `zero_trace` together with `async` is rejected.
- A cache hit gets its own search id, archived for the caller.

Security defaults: no keys configured means every request is rejected. `hl` and `gl` are validated because the language code selects an upstream host. Upstream redirects are not followed. API keys are redacted from access logs and stored only as hashes. Upstream timeouts and errors fall through to the next organic engine and never become bare 500s.

## Configuration

All settings are environment variables with the `FAIRSERP_` prefix. `.env.example` has the full list.

| Variable | Default | Purpose |
|---|---|---|
| `FAIRSERP_API_KEYS` | (empty: everything rejected) | `key` or `key:hourly_limit`, comma separated |
| `FAIRSERP_AUTH_DISABLED` | `false` | Open mode, for local development only |
| `FAIRSERP_PUBLIC_URL` | `http://localhost:8000` | Base for `json_endpoint`, `serpapi_pagination` and other self links |
| `FAIRSERP_BACKEND` | `memory` | `redis://...` to share cache, quotas and the archive across workers (compose sets this) |
| `FAIRSERP_CACHE_TTL_SECONDS` | `3600` | Global cache TTL. Engines may lower it (`FAIRSERP_ENGINE_CACHE_TTL=brave:0,mojeek:600`) |
| `FAIRSERP_ORGANIC_PRIORITY` | `brave,mojeek,tavily,firecrawl,marginalia,searxng,wikipedia` | Fallback order for `auto` |
| `FAIRSERP_ENGINE_ALIASES` | `google:auto,bing:auto,duckduckgo:auto` | Map SerpApi engine names to fairserp engines, e.g. `google:brave` |
| `FAIRSERP_PROXIES` | (none) | Egress proxies, rotated round robin |
| `FAIRSERP_ENGINE_PROXIES` | `{}` | Per-engine override as JSON, `"none"` for direct |
| `FAIRSERP_USER_AGENT` | `fairserp/0.1 (...)` | Set a contact address, as Wikimedia and OSM policies ask |
| `FAIRSERP_BRAVE_API_KEY`, `..._MOJEEK_...`, `..._TAVILY_...`, `..._FIRECRAWL_...`, `..._MARGINALIA_...` | | Upstream credentials |
| `FAIRSERP_SEARXNG_URL`, `FAIRSERP_NOMINATIM_URL` | | Self-hosted upstreams |

Proxies are for egress control (corporate networks, fixed egress IPs, data residency). fairserp never rotates to another proxy to retry a refused or rate-limited request.

## Adding an engine

1. Create `fairserp/engines/<name>.py` with a subclass of `Engine`. `fetch()` calls the upstream and returns its raw JSON. `parse()` is a pure function that turns it into SerpApi fragments, using `fairserp.results.organic_result`.
2. Set `compliance`, `required_settings` and `default_cache_ttl` to match the upstream's terms.
3. Register it in `fairserp/engines/__init__.py`.
4. Record a fixture (`scripts/record_fixtures.py <engine> <query>`) and add a parser test in `tests/test_parsers.py`.

## Development

```bash
uv sync
uv run pytest        # parser tests run against saved upstream payloads in tests/fixtures
uv run ruff check .
```

CI (`.github/workflows/ci.yml`) runs the tests, builds the image, starts the stack with `docker compose up --wait`, sends live queries, and runs the official SerpApi Python client against it.

## License

AGPL-3.0-only. Self-hosting and modifying it are free. If you offer a modified fairserp as a network service, you must publish your changes. See `docs/RESEARCH.md` for the reasoning.

Data from upstream sources stays under their terms: OpenStreetMap is ODbL (attribution required), Wikipedia text is CC BY-SA, Marginalia results are CC-BY-NC-SA, and commercial APIs are governed by your agreement with each provider.
