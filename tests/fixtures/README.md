# Parser fixtures

`manifest.json` records where every fixture came from:

- `"source": "recorded"`: the raw payload an adapter's `fetch()` received from the live
  upstream, written by `scripts/record_fixtures.py` (or, for the 2026-10-23 originals, by
  curl). Descriptions in `firecrawl_coffee.json` are trimmed to 600 characters.
- `"source": "handwritten", "verified": false`: written from the provider's published
  response schema because recording needs a paid or signup key fairserp does not hold
  (Brave, Mojeek, Tavily, and SearXNG, which needs an instance). Adapters whose parser has
  only handwritten fixtures report `fixture_verified: false` in `/v1/engines`, and the test
  suite enforces that the two stay consistent. Re-record these before relying on them.
