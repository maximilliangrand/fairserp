"""Record an engine's raw upstream payload into tests/fixtures.

Usage: uv run python scripts/record_fixtures.py <engine> <query> [key=value ...]
e.g.   FAIRSERP_BRAVE_API_KEY=... uv run python scripts/record_fixtures.py brave coffee
       uv run python scripts/record_fixtures.py openstreetmap pizza location="Brooklyn, NY"

Writes tests/fixtures/<engine>_<query>.json and marks it recorded in manifest.json.
"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from fairserp.config import Settings
from fairserp.engines import build_registry
from fairserp.engines.base import EngineContext
from fairserp.http import HttpPool
from fairserp.model import SearchQuery

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


async def record(engine_name: str, query: SearchQuery) -> Path:
    settings = Settings()
    engine = build_registry(load_plugins=False)[engine_name]
    if not engine.is_configured(settings):
        raise SystemExit(f"{engine_name} is not configured: set its FAIRSERP_* key first")
    http = HttpPool(settings)
    try:
        raw = await engine.fetch(query, EngineContext(settings, http))
    finally:
        await http.close()
    name = f"{engine_name}_{query.q.replace(' ', '_').lower()}.json"
    (FIXTURES / name).write_text(json.dumps(raw, indent=1, ensure_ascii=False) + "\n")
    manifest_path = FIXTURES / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    manifest[name] = {"engine": engine_name, "source": "recorded",
                      "recorded_at": datetime.now(UTC).date().isoformat(),
                      "query": query.model_dump(exclude_defaults=True)}
    manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return FIXTURES / name


def main() -> None:
    engine, q, *extra = sys.argv[1:]
    fields = dict(item.split("=", 1) for item in extra)
    print(asyncio.run(record(engine, SearchQuery(q=q, **fields))))


if __name__ == "__main__":
    main()
