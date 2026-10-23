"""SerpApi Locations API equivalent, backed by OpenStreetMap Nominatim."""

from typing import Any

import httpx

from fairserp.engines.base import EngineContext, EngineError
from fairserp.store import Store

CACHE_TTL = 7 * 24 * 3600


async def search(q: str, limit: int, ctx: EngineContext, store: Store) -> list[dict[str, Any]]:
    cache_key = f"locations:{q.casefold()}:{limit}"
    cached = await store.get_json(cache_key)
    if cached is not None:
        return cached
    url = f"{ctx.settings.nominatim_url.rstrip('/')}/search"
    params = {"q": q, "format": "jsonv2", "limit": limit, "addressdetails": 1}
    try:
        response = await ctx.http.request("openstreetmap", "GET", url, params=params)
    except httpx.HTTPError as exc:
        raise EngineError("openstreetmap", "upstream connection failed") from exc
    if response.status_code != 200:
        raise EngineError("openstreetmap", f"upstream returned HTTP {response.status_code}")
    result = [_location(item) for item in response.json()]
    await store.set_json(cache_key, result, CACHE_TTL)
    return result


def _location(item: dict[str, Any]) -> dict[str, Any]:
    canonical = ",".join(part.strip() for part in item.get("display_name", "").split(","))
    return {
        "id": f"osm:{item.get('osm_type')}/{item.get('osm_id')}",
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
