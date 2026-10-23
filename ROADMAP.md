# Roadmap

Ordered by value. Anything that would require scraping or getting past bot protection is out
of scope permanently (see CONTRIBUTING.md).

## Next

- **Record the unverified fixtures** (Brave, Mojeek, Tavily, Exa, Parallel, Jina, SearXNG)
  with real keys and flip `fixture_verified`; add a keyed nightly schema-drift canary.
- **Benchmark with independent indexes**: Brave + Mojeek + Marginalia, single vs fused, on
  all strata, with blinded human judgments for a holdout set. Decide whether research mode
  graduates from experimental.
- **Durable async jobs** (Redis-backed queue with leases and recovery), or keep the current
  honest lease-only behavior if nobody needs durability.
- **Ask Firecrawl what backs hosted search** and record the answer in its rights metadata.

## Later

- Tavily-compatible and Brave-compatible wire dialects next to the SerpApi one.
- MCP over streamable HTTP with bearer auth, published to the MCP registry.
- Self-hosted Photon/Nominatim recipe for local search at volume; opening-hours parsing
  (`open_now`).
- Wikidata answer templates for factoid questions (capital of, population of, founded).
- Federation with a user's own corpus (docs, licensed datasets) with permission checks
  before ranking and caching.
- Yandex Cloud and Kagi adapters (paid, official APIs).

## Decisions pending with the owner

- License: AGPL-3.0 today; both external reviews recommend Apache-2.0.
- Project name and whether to make the repository and image public.
- Which paid provider keys, if any, to fund for production use.
