# Contributing to fairserp

Thanks for helping. fairserp is search infrastructure for AI agents that only uses sources
whose terms allow it, and says where every result came from. Contributions keep that
promise or they do not land.

## The line

Pull requests that add any of the following will be closed without review:

- scraping search engine result pages (Google, Bing, DuckDuckGo, Brave, Mojeek or any other)
  or any site whose terms or robots.txt disallow it;
- headless browsers, JavaScript-challenge or CAPTCHA solving, fingerprint spoofing, token
  reuse, or anything else that gets past bot protection;
- rotating IP addresses or proxies to spread load across egress points, or retrying a
  refused request through another route;
- logging into accounts to obtain results.

This is not a style preference. In 2026 the live legal exposure for SERP tools is DMCA
§ 1201 anti-circumvention, and courts have let it reach tool authors and API customers
(see `docs/RESEARCH.md`). Shipping that code would make every user of this project a
defendant-in-waiting.

## Adding an engine

1. Subclass `fairserp.engines.base.Engine` in `fairserp/engines/<name>.py`:
   - `fetch()` calls the upstream through `self.get_json(...)` and returns the raw payload;
   - `parse()` is a pure function returning an `EngineOutput`; call `require(...)` to raise
     `PayloadError` on anything that is not a valid envelope;
   - declare `rights` (provenance, commercial use, max storage, attribution, terms URL and
     review date), `supports` (only params the upstream truly applies), `max_depth`,
     `required_settings`, and `cost_per_call_usd`.
2. Register it in `fairserp/engines/__init__.py` (or ship it as a separate package that
   registers through the `fairserp.engines` entry-point group).
3. Record a fixture: `uv run python scripts/record_fixtures.py <engine> <query>`. Set
   `fixture_verified = True` only for recorded fixtures; handwritten ones stay unverified
   and are marked as such in `tests/fixtures/manifest.json`.
4. Add a request contract test (`tests/test_contracts.py`), parser tests including
   malformed payloads (`tests/test_parsers.py`), and make sure
   `fairserp.testing.conformance.check_engine` passes (`tests/test_conformance.py`).
5. Regenerate the capability matrix: `uv run python scripts/capabilities.py`.

Read the provider's terms yourself and cite them in `rights.notes`. If a provider does not
disclose where its results come from, its provenance is `undisclosed` and it stays opt-in.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
```

Required CI runs the unit tests, the client-truth tests and a Docker Compose stack against
a mock upstream. Nothing in required CI may depend on a third-party provider being up; live
checks belong in `.github/workflows/canary.yml`.

## Style

- Follow the existing code: small functions, explicit names, comments that say why.
- No em or en dashes in docs, UI copy or messages.
- Conventional commits: `feat(engines): ...`, `fix(api): ...`, `docs: ...`.

By contributing you agree that your contribution is licensed under the project's license
(`LICENSE`) and that you follow the [Code of Conduct](CODE_OF_CONDUCT.md).
