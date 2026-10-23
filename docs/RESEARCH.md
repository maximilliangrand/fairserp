# Research: building an open SerpApi alternative (October 2026)

Researched 2026-10-23. Live probes were run from a residential connection on that date and are marked **[live]**. This is a risk assessment, not legal advice. Court dockets move, so check the current status before relying on any of it.

## TL;DR

1. **The legal line in 2026 is DMCA Section 1201, not scraping as such.** Google's suit against SerpApi pleads only anti-circumvention claims: defeating SearchGuard's JavaScript challenge, spoofing fingerprints, syndicating solved tokens, solving CAPTCHAs. A parallel Reddit case let the same theory reach SerpApi's *customer* (Perplexity). Shipping code whose purpose is to get past Google's bot protection is the riskiest thing an open-source project could do.
2. **Public official APIs are scarce.** Bing's Web Search API was retired on 2025-08-11, and Google's Custom Search JSON API is closed to new customers and ends on 2027-01-01. What is left: Brave, Mojeek and Yandex (paid, card required), plus Tavily, Exa and You.com (free tier, no card), plus keyless sources: Firecrawl Keyless, Wikipedia/Wikidata, the DuckDuckGo Instant Answer API, and OpenStreetMap.
3. **The open-source field is broken or not SerpApi-shaped.** Whoogle was archived in August 2026 after Google blocked non-JS clients, SearXNG's Google engine is broken, and the active SerpApi-like projects (karust/openserp and smaller ones) scrape Google through proxies. None of them speaks the SerpApi wire format, and none has API keys or quotas.
4. **Design consequence:** fairserp is a SerpApi-compatible *gateway over licensed and open data sources*. It ships no HTML scrapers, never solves challenges, and never rotates proxies to retry a block. `engine=google` is served by the best configured legal provider, and the response says which one.

---

## 1. SerpApi, the product

### Engines
About 140 `engine=` values. The core ones: `google` (web) and the Google verticals (`google_images`, `google_news`, `google_maps`, `google_local`, `google_shopping`, `google_jobs`, `google_scholar`, `google_trends`, `google_autocomplete`, `google_lens`, `google_flights`, `google_hotels`, `google_ai_mode` and more), plus `bing`, `duckduckgo`, `yahoo`, `yandex`, `baidu`, `naver`, `ebay`, `walmart`, `amazon`, `home_depot`, `apple_app_store`, `google_play` and `youtube`. Source: https://serpapi.com/search-api and the per-engine docs pages.

### Pricing (https://serpapi.com/pricing, read 2026-10-23)

| Plan | $/month | Searches/month | Searches/hour | $/search |
|---|---|---|---|---|
| Free | 0 | 250 | 50 | 0 |
| Starter | 25 | 1,000 | 200 | 0.025 |
| Developer | 75 | 5,000 | 1,000 | 0.015 |
| Production | 150 | 15,000 | 3,000 | 0.010 |
| Big Data | 275 | 30,000 | 6,000 | 0.0092 |
| Searcher | 725 | 100,000 | 20,000 | 0.0073 |
| Volume | 1,475 | 250,000 | 50,000 | 0.0059 |
| Infrastructure | 2,750 | 500,000 | 100,000 | 0.0055 |
| Cloud 1M to 54M | 3,750 to 106,050 | 1M to 54M | 110k to 640k | 0.00375 to 0.0020 |

"Ludicrous Speed" costs 2x and "Ludicrous Speed Max" costs 4x. Paid plans from Production upward include a "U.S. Legal Shield" (up to $2M coverage). Only successful searches count. Cached searches (exact parameter match, 1 hour), errors, the Account API and the Locations API are free. Google stopped honoring `num` on 2025-09-14, so each 10-result page is a billed search.

### Request API (what clients actually call)
- `GET /search` with `output=json|html|md`, `GET /search.json`, `/search.html`, `/search.md`
- `GET /searches/{id}`, `/searches/{id}.json`, `/searches/{hash}/{id}.json` (Search Archive, 31 days, 410 when expired)
- `GET /account` and `/account.json`; `GET /locations.json` (no key, returns a bare array)
- Common params: `q`, `engine`, `api_key`, `location`, `uule`, `google_domain`, `gl`, `hl`, `lr`, `cr`, `safe`, `tbm`, `tbs`, `start`, `num` (ignored by Google now), `device`, `no_cache`, `async`, `zero_trace`, `output`, `json_restrictor`. Bing uses `mkt`, `cc`, `first`, `count`. DuckDuckGo uses `kl`, `safe` (1/-1/-2) and `df`.
- Clients add `source=python|nodejs...`. The legacy Python client sends `serp_api_key` when its class-level key is set.
- Errors are always `{"error": "..."}`. A bad key returns 401 and is checked before any other validation. Other codes: 400, 403, 404, 410, 429 (hourly throughput or out of searches), 500, 503. "No results" is **HTTP 200** with an `error` field. The npm client rejects anything that isn't 200.

### Response schema (from a live `engine=google&q=coffee` response)
Top-level keys: `search_metadata`, `search_parameters`, `search_information`, `local_map`, `local_results`, `knowledge_graph`, `immersive_products`, `related_questions`, `ai_overview`, `organic_results`, `perspectives`, `related_searches`, `pagination`, `serpapi_pagination`, plus `ads`, `answer_box` and `top_stories` when present.

- `search_metadata`: `id` (24 hex), `status` (Processing, Success or Error), `json_endpoint`, `markdown_endpoint`, `created_at`/`processed_at` (`YYYY-MM-DD HH:MM:SS UTC`), `<engine>_url`, `raw_html_file`, `total_time_taken`.
- `organic_results[]`: `position`, `title`, `link`, `redirect_link`, `displayed_link` (`https://site › a › b`), `favicon`, `date`, `snippet`, `snippet_highlighted_words`, `sitelinks{inline,expanded}`, `rich_snippet`, `source`.
- `local_results`: `{places: [...], more_locations_link}` for `google`, and a bare list for `google_maps`. Each place has `position`, `title`, `place_id`, `rating`, `reviews`, `price`, `type`, `address`, `phone`, `hours`, `gps_coordinates{latitude,longitude}` and `links`.
- `knowledge_graph`: `title`, `type`, `kgmid`, `header_images[{image,source}]`, `description`, `source{name,link}`, plus snake_case attributes taken from the visible labels.
- `related_questions[]`: `question`, `snippet`, `title`, `link`, `displayed_link`. `related_searches[]`: `block_position`, `query`, `link`, `serpapi_link`.
- `serpapi_pagination`: `current`, `next`, `next_link` and `other_pages{"2": url}`, all pointing at the API host. The new Python client follows `next` exactly, so these links must use the self-hosted base URL.

### Official clients and "change only the base URL"
None of them reads an environment variable or takes a constructor argument for the host, but each can be redirected with one line:

| Library | Override |
|---|---|
| `serpapi` (Python, current) | `serpapi.HTTPClient.BASE_DOMAIN = "https://your.host"` |
| `google-search-results` (Python, legacy) | `serpapi.SerpApiClient.BACKEND = "https://your.host"` |
| `serpapi` (npm) | custom `https.Agent` in `config.requestOptions` that connects to your host (the Host header stays `serpapi.com`) |
| `google-search-results-nodejs` (legacy) | patch `SerpApiSearch.prototype.buildUrl` |

Sources: https://github.com/serpapi/serpapi-python, https://github.com/serpapi/google-search-results-python, https://github.com/serpapi/serpapi-javascript, https://github.com/serpapi/google-search-results-nodejs (each override was tested against a local echo server).

---

## 2. Existing open-source options and their gaps

| Project | State (Oct 2026) | What it does badly |
|---|---|---|
| SearXNG (AGPL, ~39k stars) | Active. Google engine broken since Google required JS (2025-01-15); the stopgap `google_cse` engine dies with CSE on 2027-01-01 | JSON is not SerpApi-shaped (no `position`, `knowledge_graph`, `local_results`, `search_metadata`). No API keys, quotas or tenants. Scrapers fail silently into `unresponsive_engines`. JSON is disabled on most public instances |
| Whoogle (MIT) | **Archived 2026-08-14**: "Whoogle no longer returns search results" | Google-only scraper |
| LibreX / LibreY, 4get, Websurfx | LibreX dead; the others alive | HTML scrapers with their own JSON. 4get's public API forbids SEO scanning |
| Stract | Repo archived March 2025 | Own index, dead |
| Marginalia | Active, own small-web index | Small index. The shared `public` API key is often exhausted [live] |
| YaCy | Active P2P | Low coverage unless you crawl yourself |
| Perplexica (now "Vane") | Active | AI answer engine on top of SearXNG, not a SERP API |
| Firecrawl | Active, AGPL | The good search is the hosted API; self-hosted search depends on SearXNG |
| karust/openserp (MIT, ~1.5k stars) | Active | Scrapes Google/Bing/Yandex/Baidu, with headless browser and proxies. Own schema, no keys/quotas. Exactly the § 1201 risk profile |
| cse-bridge, jiro, misc. "SerpApi-compatible" repos | 0 to 10 stars | Mostly proxy-dependent Google scrapers |
| SerpBear | Active | A rank tracker that *consumes* SERP APIs, so it is a potential user of fairserp |

**Gaps fairserp fills:** (1) faithful SerpApi wire compatibility, so existing SDKs, LangChain's `SerpAPIWrapper` and SerpBear work after a base-URL change; (2) API keys, hourly quotas, usage accounting, and an archive scoped to each key's owner; (3) a legal-first engine layer that composes licensed and open data into structured `knowledge_graph`, `answer_box` and `local_results`, which scrapers rarely get right; (4) bring-your-own-key adapters normalized into one schema; (5) explicit per-request provenance (`search_metadata.fairserp_sources`) instead of silent failures.

Sources: https://github.com/searxng/searxng, https://docs.searxng.org/dev/search_api.html, https://github.com/benbusby/whoogle-search, https://github.com/Ahwxorg/librey, https://github.com/StractOrg/stract, https://about.marginalia-search.com/article/api, https://github.com/karust/openserp, https://github.com/Booyaka101/cse-bridge, https://github.com/towfiqi/serpbear, https://www.firecrawl.dev/blog/firecrawl-keyless-launch

---

## 3. The legal picture

### Google LLC v. SerpApi, LLC (N.D. Cal., No. 4:25-cv-10826-YGR)
- **Filed 2025-12-19** before Chief Judge Yvonne Gonzalez Rogers. Complaint: https://storage.googleapis.com/gweb-uniblog-publish-prod/documents/Google_v._SerpApi__Complaint.pdf ; Google's post: https://blog.google/technology/safety-security/serpapi-lawsuit/
- **Claims: DMCA only.** Count I is § 1201(a)(1)(A), circumventing an access control. Count II is § 1201(a)(2), trafficking in circumvention technology or services. There is no ToS, CFAA or trespass count. Statutory damages are $200 to $2,500 *per act of circumvention*.
- **Allegations.** SearchGuard (launched January 2025) sends a JavaScript challenge to unrecognized clients and also uses CAPTCHAs. Google says SerpApi defeated it by misrepresenting the device, software and location; by solving the challenge once and syndicating the authorization to machines worldwide; by running "fake browsers" across many IPs; and by solving CAPTCHAs automatically. The copyrighted works at issue are licensed content in results, chiefly Knowledge Panel images.
- **2026-07-20 order** (https://caselaw.findlaw.com/court/us-dis-crt-n-d-cal/350669.html):
  - The motion to dismiss was granted. Claims over results containing no copyrighted material were dismissed **without** leave to amend. Claims over copyrighted components were dismissed **with** leave, because Google had not alleged SearchGuard was applied with the copyright owner's authority.
  - The court still held that Google has standing under § 1203.
  - It also held that the spoofing, IP rotation and token-syndication methods were adequately alleged as "circumvention."
- **2026-08-10:** Google amended, narrowing to licensed content (a licensing partner, the Reddit agreement and others). **2026-08-24:** SerpApi moved to dismiss again, with prejudice. **2026-10-05:** the Oct 13 hearing was vacated, so the motion may be decided on the papers. **2026-10-12:** Google cited *Ted Entertainment v. ByteDance*.
- **Status as of 2026-10-23: second motion to dismiss pending.** No injunction and no settlement. SerpApi is still operating (https://ppc.land/serpapi-now-scrapes-any-public-web-page-crawling-up-to-100-per-request/). Docket: https://www.courtlistener.com/docket/72059948/google-llc-v-serpapi-llc/
- SerpApi's position: "Google is the largest scraper in the world." It says it does not bypass authentication, access private accounts or retrieve non-public data (https://serpapi.com/blog/google-tried-again-were-moving-to-dismiss-their-claims-a-second-time/).

### Related cases
- **Reddit v. SerpApi, Oxylabs, AWMProxy, Perplexity (S.D.N.Y. 1:25-cv-08736, filed 2025-10-22).** On 2026-07-31 Judge Engelmayer largely denied the motions to dismiss. The § 1201(a)(2) trafficking claim survives against SerpApi, and the § 1201(a)(1)(A) claim survives against **Perplexity, the paying customer**. The court treated SearchGuard as an effective access control because it "differentiates between automated and human users." This is the most dangerous precedent: a content owner other than Google can sue, and API customers are exposed. https://www.loeb.com/en/insights/publications/2026/08/reddit-v-serpapi-llc
- **Ted Entertainment v. ByteDance (N.D. Cal. 2026):** a § 1201 claim over getting past YouTube's anti-scraping measures survived dismissal. https://www.mediapost.com/publications/article/418510/bytedance-must-face-suit-over-scraping.html
- **hiQ v. LinkedIn:** logged-out scraping of public data is likely not a CFAA violation, but hiQ still lost on breach of contract ($500k consent judgment).
- **Meta v. Bright Data (2024):** logged-out scraping did not breach Meta's terms. **X v. Bright Data:** scraping-related contract claims were held preempted by copyright, while trespass-to-chattels (server load) and deception theories survived.

### Terms of service and robots.txt
- **Google** ToS (effective 2026-07-30) bars "using automated means to access content from any of our services in violation of the machine-readable instructions on our web pages (for example, robots.txt files...)" (https://policies.google.com/terms). google.com/robots.txt has `Disallow: /search`. Read together, automated `/search` queries breach the terms.
- **Bing:** the Microsoft Services Agreement bars "impermissible scraping," and robots.txt has `Disallow: /search`.
- **DuckDuckGo:** no explicit scraping clause, but robots.txt disallows `/html` and `/lite`. [live] Both returned the bot "anomaly" challenge.
- **Mojeek:** "scraping the Services without Our prior consent is expressly prohibited." [live] It returns 403 to automated queries and points to its API.
- **Brave:** robots.txt disallows `/search`. The paid API's terms limit caching or storing results and AI training.

### Which engines have official, legal APIs (October 2026)

| API | Status | Free use | Card needed | Price |
|---|---|---|---|---|
| Brave Search API | Active | $5/month credit (~1k queries); old 5k free plan gone for new users | Yes | $5 / 1k |
| Mojeek Search API | Active | Trial credits | Yes | £1 to £3 / 1k |
| Yandex Search API | Active | 1k/month, night hours | Cloud billing | ~$3 to $4 / 1k |
| Bing Web Search API | **Retired 2025-08-11** | n/a | n/a | n/a |
| Grounding with Bing | Active | none | Azure | ~$35 / 1k; terms ban storage, reuse outside Microsoft agents and competing services, so unusable here |
| Google Custom Search JSON | **Closed to new customers, ends 2027-01-01** | n/a | n/a | n/a |
| Google "Web Search Service API" | Partner-only, no public signup | n/a | n/a | n/a |
| Tavily | Active | 1,000 credits/month | No | PAYG |
| Exa | Active | $10/month credit | No | ~$7 / 1k |
| You.com | Active | 100 calls/day | No | $5 / 1k |
| Kagi | Active | none | Yes | ~$2.50 / 1k |
| Firecrawl Keyless | Active (launched 2026-06-16) | 1,000 credits/month, per-IP cap, **no key** | No | Paid after signup |
| Marginalia | Active | Shared `public` key; free personal key by email | No | Commercial keys sold; results CC-BY-NC-SA |
| Wikipedia / Wikidata | Active | Free, needs a descriptive User-Agent; 200 req/min identified | No | Free |
| DuckDuckGo Instant Answer | Active | Free, no key; abstracts and answers, no organic links | No | Free |
| OSM Nominatim (public) | Active | 1 req/s, caching required, heavy users self-host | No | Free (ODbL attribution) |
| Overpass (public) | Active | For human-driven tools; [live] asked automated clients to self-host | No | Free |

Sources: https://brave.com/search/api/ , https://www.mojeek.com/services/search/web-search-api/ , https://yandex.cloud/en/docs/search-api/pricing , https://learn.microsoft.com/en-us/lifecycle/announcements/bing-search-api-retirement , https://www.microsoft.com/bing/apis/grounding-legal , https://brave.com/learn/google-api-shutdown/ , https://www.searchenginejournal.com/google-documents-partner-only-api-for-full-web-search-results/ , https://docs.tavily.com/documentation/api-credits.md , https://www.mediawiki.org/wiki/Wikimedia_APIs/Rate_limits , https://operations.osmfoundation.org/policies/nominatim/ , https://api.duckduckgo.com/api

### Risk ranking (highest to lowest)
1. **A hosted commercial service that scrapes Google: high.** This is the SerpApi fact pattern, and *Reddit* shows customers can be named too.
2. **Scraping Google yourself: medium-high.** It breaches the ToS and robots.txt. The § 1201 exposure turns on whether you defeat SearchGuard (JS challenge solving, fingerprint spoofing, token reuse, CAPTCHA solving). Distributing code built for that is § 1201(a)(2) "trafficking" risk for the *tool author*.
3. **Scraping Bing or DuckDuckGo: medium-low.** It breaks their terms and robots.txt, but there has been no DMCA suit of this kind, and logged-out contract claims are weak. It is also unreliable: DDG served bot challenges in our probes.
4. **A self-hosted personal metasearch (SearXNG-style): low.** In practice this means IP blocks, not lawsuits. Adding stealth or challenge-solving moves it back up to (2).
5. **Official APIs: lowest.** Only contract compliance remains (for example Brave's caching limits).

---

## 4. How the findings drive the design

| Finding | Design decision |
|---|---|
| § 1201 liability attaches to defeating bot protection and to *trafficking* in tools that do | fairserp ships **no** Google or Bing HTML scrapers, no headless browser, no challenge solving, no fingerprint spoofing. An upstream 403/429/challenge is surfaced as an error and **never retried through another proxy** |
| Official raw-results APIs are few and mostly paid | A pluggable adapter layer with bring-your-own-key engines (Brave, Mojeek, Tavily), a keyless default (Firecrawl Keyless) and an independent index (Marginalia) |
| No single legal source matches Google's SERP features | A composite `auto` engine: organic from the best configured provider, `knowledge_graph` and `answer_box` from the DuckDuckGo IA API and Wikipedia, `local_results` from OpenStreetMap |
| Existing SDKs hardcode serpapi.com, and pagination links are followed verbatim | Exact path, parameter and error compatibility. Every self link is built from `FAIRSERP_PUBLIC_URL` |
| `engine=google` is what client code sends | It is an alias (configurable) to `auto`. `search_metadata.fairserp_sources` states which engines actually answered, so nobody mistakes these for Google results |
| Upstream terms differ on caching (Brave restricts storage; Nominatim requires caching) | Per-engine cache TTL defaults (Brave 0, Nominatim 24h), overridable per deployment |
| Wikimedia and OSM require identification and politeness | A configurable descriptive User-Agent, plus a built-in 1 req/s spacing for the public Nominatim instance |
| SearXNG is the escape hatch people already run | A `searxng` adapter for a user-operated instance, labeled "user-operated": the legal status follows whatever engines that instance enables |
| Hosting a resale endpoint is the high-risk pattern | Self-hosted by default with per-key quotas. The license decision (below) covers hosted forks |

### Stack choice
Python 3.12 with FastAPI, httpx and pydantic-settings, and Redis optional. Python because SerpApi's most used clients and the LLM tooling that consumes SERP data (LangChain and friends) are Python-first, and because the parser and test story with saved JSON fixtures is simplest there. There is **no Playwright**. A headless browser is only needed to execute bot challenges, which is exactly what the design rules out, and leaving it out keeps the image small.

### License
**AGPL-3.0** (the same as SearXNG and Firecrawl). The realistic commercial threat is someone hosting fairserp as a paid SERP API without giving changes back, and AGPL's network clause covers that while costing self-hosters nothing. The tradeoff is that some companies' policies block AGPL dependencies. If adoption matters more than preventing closed hosted forks, MIT or Apache-2.0 is the alternative. This is a decision for the project owner.
