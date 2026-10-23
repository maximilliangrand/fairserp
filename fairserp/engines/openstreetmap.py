"""Local results from OpenStreetMap via Nominatim (open data, keyless).

Two lookups: geocode the place (cached for a week), then search the category inside the
place's bounding box. Results are filtered by real OSM tags (``amenity=cafe``,
``cuisine=pizza``) and sorted by distance from the place's centre.

The public Nominatim instance allows at most one request per second across all of a
deployment's workers, requires caching, and forbids reselling geocoding. fairserp spaces
requests through the shared state store; point FAIRSERP_NOMINATIM_URL at your own
instance for real traffic.
"""

import math
import re
from dataclasses import dataclass, field
from typing import Any

from fairserp.engines.base import Engine, EngineContext, Role, require
from fairserp.model import EngineOutput, Place, SearchQuery
from fairserp.rights import CommercialUse, Provenance, Rights

GEOCODE_TTL = 7 * 24 * 3600
MAX_BOX_DEGREES = 0.15
NEAR = re.compile(r"\s+(?:near|in|around)\s+(.+)$", re.IGNORECASE)


@dataclass(frozen=True)
class Category:
    search: str  # what Nominatim is asked for inside the bounding box
    label: str
    tags: dict[str, str] = field(default_factory=dict)  # required OSM key=value
    cuisine: str | None = None


def _c(search, label, key, value, cuisine=None):
    return Category(search, label, {key: value}, cuisine)


CATEGORIES = {
    "coffee": _c("cafe", "Cafe", "amenity", "cafe"),
    "cafe": _c("cafe", "Cafe", "amenity", "cafe"),
    "café": _c("cafe", "Cafe", "amenity", "cafe"),
    "espresso": _c("cafe", "Cafe", "amenity", "cafe"),
    "restaurant": _c("restaurant", "Restaurant", "amenity", "restaurant"),
    "restaurants": _c("restaurant", "Restaurant", "amenity", "restaurant"),
    "pizza": _c("pizza restaurant", "Pizza restaurant", "amenity", "restaurant", "pizza"),
    "sushi": _c("sushi restaurant", "Sushi restaurant", "amenity", "restaurant", "sushi"),
    "burger": _c("burger restaurant", "Burger restaurant", "amenity", "fast_food", "burger"),
    "bar": _c("bar", "Bar", "amenity", "bar"),
    "bars": _c("bar", "Bar", "amenity", "bar"),
    "pub": _c("pub", "Pub", "amenity", "pub"),
    "hotel": _c("hotel", "Hotel", "tourism", "hotel"),
    "hotels": _c("hotel", "Hotel", "tourism", "hotel"),
    "pharmacy": _c("pharmacy", "Pharmacy", "amenity", "pharmacy"),
    "hospital": _c("hospital", "Hospital", "amenity", "hospital"),
    "gym": _c("fitness centre", "Gym", "leisure", "fitness_centre"),
    "bakery": _c("bakery", "Bakery", "shop", "bakery"),
    "bank": _c("bank", "Bank", "amenity", "bank"),
    "atm": _c("atm", "ATM", "amenity", "atm"),
    "supermarket": _c("supermarket", "Supermarket", "shop", "supermarket"),
    "grocery": _c("supermarket", "Supermarket", "shop", "supermarket"),
    "dentist": _c("dentist", "Dentist", "amenity", "dentist"),
    "doctor": _c("doctors", "Doctor", "amenity", "doctors"),
    "gas": _c("fuel", "Gas station", "amenity", "fuel"),
    "parking": _c("parking", "Parking", "amenity", "parking"),
    "museum": _c("museum", "Museum", "tourism", "museum"),
    "library": _c("library", "Library", "amenity", "library"),
    "cinema": _c("cinema", "Cinema", "amenity", "cinema"),
    "hairdresser": _c("hairdresser", "Hairdresser", "shop", "hairdresser"),
    "barber": _c("hairdresser", "Hairdresser", "shop", "hairdresser"),
    "bookstore": _c("bookshop", "Bookstore", "shop", "books"),
    "park": _c("park", "Park", "leisure", "park"),
}


def local_request(query: SearchQuery) -> tuple[Category, str] | None:
    """The category and place of a query with local intent, else None."""
    match = NEAR.search(query.q)
    place = (match.group(1) if match else query.location or "").strip()
    subject = query.q[: match.start()] if match else query.q
    if not place:
        return None
    for word in (w.strip(".,!?").lower() for w in subject.split()):
        if word in CATEGORIES:
            return CATEGORIES[word], place
    return None


class OpenStreetMapEngine(Engine):
    name = "openstreetmap"
    role = Role.LOCAL
    upstream = "https://nominatim.openstreetmap.org/search"
    supports = frozenset({"location", "language"})
    max_depth = 40
    fixture_verified = True
    rights = Rights(
        provenance=Provenance.OPEN_DATA,
        commercial_use=CommercialUse.ALLOWED,
        max_storage_seconds=None,
        attribution="© OpenStreetMap contributors, ODbL 1.0",
        terms_url="https://operations.osmfoundation.org/policies/nominatim/",
        terms_reviewed="2026-10-23",
        notes="Public Nominatim: 1 request/second, caching required, no resale of geocoding. "
              "OSM has no ratings or reviews; those fields stay unknown.",
    )
    notes = "No key. Local results with categories and distance. Self-host for volume."

    async def fetch(self, query: SearchQuery, ctx: EngineContext) -> Any:
        request = local_request(query)
        if request is None:
            return {"category": None}
        category, place = request
        base = f"{ctx.settings.nominatim_url.rstrip('/')}/search"
        center = await self._geocode(ctx, base, place, query.language)
        if center is None:
            return {"category": category.search, "center": None, "pois": []}
        params: dict[str, Any] = {
            "q": category.search, "format": "jsonv2", "limit": self.max_depth, "bounded": 1,
            "viewbox": _viewbox(center), "addressdetails": 1, "extratags": 1,
        }
        if query.language:
            params["accept-language"] = query.language
        pois = await self.get_json(ctx, "GET", base, params=params)
        return {"category": category.search, "center": center, "pois": pois}

    async def _geocode(self, ctx: EngineContext, base: str, place: str,
                       language: str | None) -> dict[str, Any] | None:
        key = f"geo:{place.casefold()}"
        if ctx.cache is not None and (cached := await ctx.cache.get_json(key)) is not None:
            return cached or None
        params = {"q": place, "format": "jsonv2", "limit": 1}
        if language:
            params["accept-language"] = language
        hits = await self.get_json(ctx, "GET", base, params=params)
        center = None
        if hits:
            box = [float(v) for v in hits[0]["boundingbox"]]
            center = {"lat": float(hits[0]["lat"]), "lon": float(hits[0]["lon"]), "box": box}
        if ctx.cache is not None:
            await ctx.cache.set_json(key, center or {}, GEOCODE_TTL)
        return center

    def parse(self, raw: dict[str, Any], query: SearchQuery) -> EngineOutput:
        require(isinstance(raw, dict) and "category" in raw, "not a local lookup result")
        request = local_request(query)
        if request is None or not raw.get("center"):
            return EngineOutput()
        category, _ = request
        center = raw["center"]
        require(isinstance(raw.get("pois"), list), "missing POI list")
        places = [_place(item, category, center) for item in raw["pois"]
                  if item.get("name") and _matches(item, category)]
        places.sort(key=lambda p: p.distance_m or 0)
        page = places[query.offset: query.offset + query.num]
        for position, place in enumerate(page):
            place.place_id = place.place_id or f"osm:{position}"
        return EngineOutput(places=page, has_more=len(places) > query.offset + query.num)


def _viewbox(center: dict[str, Any]) -> str:
    south, north, west, east = center["box"]
    lat, lon = center["lat"], center["lon"]
    south, north = max(south, lat - MAX_BOX_DEGREES), min(north, lat + MAX_BOX_DEGREES)
    west, east = max(west, lon - MAX_BOX_DEGREES), min(east, lon + MAX_BOX_DEGREES)
    if north - south < 0.02:
        south, north = lat - 0.02, lat + 0.02
    if east - west < 0.02:
        west, east = lon - 0.02, lon + 0.02
    return f"{west},{north},{east},{south}"


def _matches(item: dict[str, Any], category: Category) -> bool:
    tags = {item.get("category"): item.get("type")}
    extratags = item.get("extratags") or {}
    if category.cuisine:
        cuisine = (extratags.get("cuisine") or "").lower()
        named = category.cuisine in (item.get("name") or "").lower()
        food = item.get("category") == "amenity" and item.get("type") in (
            "restaurant", "fast_food", "cafe")
        return food and (category.cuisine in cuisine or named)
    return all(tags.get(key) == value for key, value in category.tags.items())


def _place(item: dict[str, Any], category: Category, center: dict[str, Any]) -> Place:
    tags = item.get("extratags") or {}
    lat, lon = float(item["lat"]), float(item["lon"])
    cuisine = tags.get("cuisine")
    return Place(
        name=item["name"],
        category=category.label,
        address=_address(item.get("address") or {}) or item.get("display_name", ""),
        latitude=lat, longitude=lon,
        distance_m=int(_haversine(center["lat"], center["lon"], lat, lon)),
        phone=tags.get("phone") or tags.get("contact:phone"),
        website=tags.get("website") or tags.get("contact:website"),
        hours=tags.get("opening_hours"),
        place_id=f"osm:{item.get('osm_type')}/{item.get('osm_id')}",
        description=cuisine.replace("_", " ").replace(";", ", ") if cuisine else None,
    )


def _address(address: dict[str, str]) -> str:
    street = " ".join(p for p in (address.get("house_number"), address.get("road")) if p)
    city = address.get("city") or address.get("town") or address.get("village") or address.get(
        "suburb")
    region = " ".join(p for p in (address.get("state"), address.get("postcode")) if p)
    return ", ".join(p for p in (street, city, region) if p)


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))
