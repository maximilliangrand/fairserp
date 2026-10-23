# Migrating from SerpApi

fairserp is not Google and does not query Google. Its SerpApi-compatible endpoint exists so
agent code written against SerpApi's JSON can move to providers you control, with an honest
account of what changed. If you need real Google rankings (rank tracking, SEO audits),
fairserp cannot do that job.

## 1. Map the engine names you use

SerpApi engine names are refused until you map them:

```bash
FAIRSERP_SERPAPI_ENGINES=google:auto,bing:auto,google_maps:openstreetmap
```

`auto` is fairserp's planner: organic results from the first permitted provider in
`FAIRSERP_ORGANIC_PRIORITY`, plus a Wikidata knowledge graph, DuckDuckGo instant answers and
OpenStreetMap local results. You can also map a name straight to one engine, e.g.
`google:brave`.

## 2. Point the client at your server

| Client | Change |
|---|---|
| `serpapi` (Python) | `serpapi.HTTPClient.BASE_DOMAIN = "https://search.internal"` |
| `google-search-results` (Python, legacy) | `serpapi.SerpApiClient.BACKEND = "https://search.internal"` |
| `serpapi` (npm) | `config.requestOptions = { agent }` with an `https.Agent` whose `createConnection` connects to your host. The `Host` header stays `serpapi.com`, so do not route on Host |
| LangChain `SerpAPIWrapper` | **Do not patch it.** Its `aresults()` hardcodes `https://serpapi.com/search` and sends your key and every query to SerpApi (pinned by `tests/clients/test_langchain_community.py`). Use `fairserp.integrations.langchain.FairserpAPIWrapper` (same `results/aresults/run/arun` methods) or `FairserpSearchTool` |
| Anything new | Use the native API (`/v1/search`) or `fairserp.client.Fairserp` / `fairserp-client` (npm), which require an explicit base URL |

## 3. Read what you got

Every SerpApi-shaped response carries `search_metadata.fairserp`:

```json
"fairserp": {
  "status": "partial",
  "requested_engine": "google",
  "resolved_engine": "auto",
  "providers": [
    {"engine": "brave", "status": "failed", "error": "upstream timed out"},
    {"engine": "mojeek", "status": "ok", "results": 10, "applied_params": ["country"]},
    {"engine": "wikidata", "status": "ok", "cached": true}
  ],
  "params": {"applied": ["gl"], "ignored": ["device", "tbs"]},
  "attribution": ["Facts from Wikidata (CC0); description from Wikipedia (CC BY-SA 4.0)"],
  "retention": {"expires_at": "2026-10-23T11:02:11+00:00"},
  "archived": true
}
```

Add `fairserp_strict=true` (or set `FAIRSERP_STRICT_PARAMS=true`) to get a 400 instead of a
report when a param cannot be applied. `safe=active` is always enforced: engines that cannot
filter are skipped, and if none can, the request is refused.

## 4. Behavior differences that matter

- Upstream failures are HTTP 502 (or 429 with `Retry-After`), never an empty 200. A genuinely
  empty result is 200 with `error` set, as on SerpApi.
- Cached results and failed searches are not counted against the hourly limit.
- `async=true` is refused when a planned provider's terms forbid storing results (Brave), and
  async jobs do not survive a server restart (they read as failed).
- Results from providers that forbid storage are not archived: `json_endpoint` returns 410
  with the reason.
- `ads`, `top_stories`, shopping and other Google verticals are never returned.

The full parameter table is in [CAPABILITIES.md](CAPABILITIES.md).
