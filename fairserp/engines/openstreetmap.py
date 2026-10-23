"""OpenStreetMap Nominatim for local results (open data, keyless).

The public instance allows at most 1 request per second, requires caching, and asks heavy
users to self-host. fairserp spaces requests (see fairserp.http) and caches results for
the global TTL. Point FAIRSERP_NOMINATIM_URL at your own instance for production.
"""

import re
from typing import Any

from fairserp.engines.base import Compliance, Engine, EngineContext, Fragments, Role, SearchRequest

# Common search words mapped to the OSM phrases Nominatim understands.
POI_PHRASES = {
    "coffee": "cafe", "cafe": "cafe", "café": "cafe", "espresso": "cafe",
    "restaurant": "restaurant", "restaurants": "restaurant", "food": "restaurant",
    "pizza": "restaurant", "sushi": "restaurant", "burger": "fast food",
    "bar": "bar", "bars": "bar",
    "pub": "pub", "hotel": "hotel", "hotels": "hotel", "pharmacy": "pharmacy",
    "hospital": "hospital", "gym": "gym", "bakery": "bakery", "bank": "bank", "atm": "atm",
    "supermarket": "supermarket", "grocery": "supermarket", "dentist": "dentist",
    "doctor": "doctors", "gas": "fuel", "parking": "parking", "museum": "museum",
    "library": "library", "cinema": "cinema", "hairdresser": "hairdresser",
    "barber": "hairdresser", "bookstore": "books", "school": "school", "park": "park",
}
NEAR = re.compile(r"\s+(?:near|in|around)\s+(.+)$", re.IGNORECASE)


def local_query(request: SearchRequest) -> str | None:
    """Return a Nominatim query when the search has local intent, else None."""
    match = NEAR.search(request.q)
    place = match.group(1) if match else request.location
    subject = request.q[: match.start()] if match else request.q
    if not place:
        return None
    words = [w.strip(".,!?").lower() for w in subject.split()]
    phrase = next((POI_PHRASES[w] for w in words if w in POI_PHRASES), None)
    if phrase is None:
        return None
    return f"{phrase} in {place}"


class OpenStreetMapEngine(Engine):
    name = "openstreetmap"
    role = Role.ENRICHMENT
    compliance = Compliance.OPEN_DATA_API
    upstream = "https://nominatim.openstreetmap.org/search"
    default_cache_ttl = 24 * 3600
    notes = "No key. 1 req/s on the public instance; self-host Nominatim for volume. ODbL."

    async def fetch(self, request: SearchRequest, ctx: EngineContext) -> Any:
        query = local_query(request)
        if query is None:
            return []
        params = {
            "q": query, "format": "jsonv2", "limit": min(request.num, 20),
            "addressdetails": 1, "extratags": 1,
        }
        if request.hl:
            params["accept-language"] = request.hl
        url = f"{ctx.settings.nominatim_url.rstrip('/')}/search"
        return await self.get_json(ctx, "GET", url, params=params)

    def parse(self, raw: list[dict[str, Any]], request: SearchRequest) -> Fragments:
        places = [_place(i, item) for i, item in enumerate(raw, start=1) if item.get("name")]
        if not places:
            return {}
        return {"local_results": {"places": places}}


def _place(position: int, item: dict[str, Any]) -> dict[str, Any]:
    tags = item.get("extratags") or {}
    place: dict[str, Any] = {
        "position": position,
        "title": item["name"],
        "place_id": f"osm:{item.get('osm_type')}/{item.get('osm_id')}",
        "type": str(item.get("type", "")).replace("_", " ").capitalize(),
        "address": _address(item.get("address") or {}) or item.get("display_name", ""),
        "gps_coordinates": {"latitude": float(item["lat"]), "longitude": float(item["lon"])},
    }
    if tags.get("phone") or tags.get("contact:phone"):
        place["phone"] = tags.get("phone") or tags.get("contact:phone")
    if tags.get("opening_hours"):
        place["hours"] = tags["opening_hours"]
    website = tags.get("website") or tags.get("contact:website")
    if website:
        place["links"] = {"website": website}
    if tags.get("cuisine"):
        place["description"] = tags["cuisine"].replace("_", " ").replace(";", ", ")
    return place


def _address(address: dict[str, str]) -> str:
    street = " ".join(p for p in (address.get("house_number"), address.get("road")) if p)
    city = address.get("city") or address.get("town") or address.get("village")
    region = " ".join(p for p in (address.get("state"), address.get("postcode")) if p)
    return ", ".join(p for p in (street, city, region) if p)
