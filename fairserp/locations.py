"""SerpApi Locations API equivalent, backed by OpenStreetMap Nominatim."""

from typing import Any

from fairserp.engines.base import EngineContext, EngineError
from fairserp.engines.openstreetmap import OpenStreetMapEngine
from fairserp.store import Store

CACHE_TTL = 7 * 24 * 3600
_ENGINE = OpenStreetMapEngine()


async def search(q: str, limit: int, ctx: EngineContext, store: Store) -> list[dict[str, Any]]:
    cache_key = f"locations:{q.casefold()}:{limit}"
    cached = await store.get_json(cache_key)
    if cached is not None:
        return cached
    url = f"{ctx.settings.nominatim_url.rstrip('/')}/search"
    params = {"q": q, "format": "jsonv2", "limit": limit, "addressdetails": 1}
    items = await _ENGINE.get_json(ctx, "GET", url, params=params)
    try:
        result = [_location(item) for item in items]
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise EngineError("openstreetmap", "unexpected upstream payload") from exc
    await store.set_json(cache_key, result, CACHE_TTL)
    return result


def _location(item: dict[str, Any]) -> dict[str, Any]:
    canonical = ",".join(part.strip() for part in item["display_name"].split(","))
    return {
        "id": f"osm:{item['osm_type']}/{item['osm_id']}",
        "google_id": None,
        "google_parent_id": None,
        "name": item.get("name") or canonical.split(",")[0],
        "canonical_name": canonical,
        "country_code": (item.get("address") or {}).get("country_code", "").upper(),
        "target_type": str(item.get("addresstype", "")).replace("_", " ").title(),
        "reach": None,
        "gps": [float(item["lon"]), float(item["lat"])],
        "keys": sorted({w.lower() for w in canonical.replace(",", " ").split()}),
    }
