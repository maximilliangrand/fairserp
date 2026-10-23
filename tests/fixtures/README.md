# Parser fixtures

Recorded from the live upstream APIs on 2026-10-23 (keyless endpoints):
`firecrawl_coffee.json` (descriptions trimmed to 600 chars), `ddg_ia_einstein.json`,
`ddg_ia_coffee.json`, `wikipedia_search_einstein.json`, `wikipedia_summary_einstein.json`,
`nominatim_cafe_austin.json`, `nominatim_austin.json`, `marginalia_coffee.json`.

Written by hand from each provider's published response schema, because recording them
needs a key fairserp does not hold: `brave_coffee.json`, `mojeek_coffee.json`,
`tavily_coffee.json`, `searxng_coffee.json`. Re-record these with a real key before
relying on the adapters in production (see `scripts/record_fixtures.py`).
