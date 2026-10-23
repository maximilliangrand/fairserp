"""Record raw upstream payloads into tests/fixtures for parser tests.

Usage: FAIRSERP_BRAVE_API_KEY=... uv run python scripts/record_fixtures.py brave coffee
Writes tests/fixtures/<engine>_<query>.json from the engine's fetch() output.
"""

import asyncio
import json
import sys
from pathlib import Path

from fairserp.config import Settings
from fairserp.engines import build_registry
from fairserp.engines.base import EngineContext, SearchRequest
from fairserp.http import HttpPool

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


async def record(engine_name: str, query: str) -> Path:
    settings = Settings()
    engine = build_registry()[engine_name]
    if not engine.is_configured(settings):
        raise SystemExit(f"{engine_name} is not configured: set its FAIRSERP_* key first")
    http = HttpPool(settings)
    try:
        request = SearchRequest(q=query, engine=engine_name)
        raw = await engine.fetch(request, EngineContext(settings, http))
    finally:
        await http.close()
    path = FIXTURES / f"{engine_name}_{query.replace(' ', '_').lower()}.json"
    path.write_text(json.dumps(raw, indent=1, ensure_ascii=False) + "\n")
    return path


if __name__ == "__main__":
    print(asyncio.run(record(sys.argv[1], " ".join(sys.argv[2:]) or "coffee")))
