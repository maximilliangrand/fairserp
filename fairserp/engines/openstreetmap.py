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
from fairserp.model import Area, EngineOutput, Place, SearchQuery
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
        area = await self._geocode(ctx, base, place, query.language)
        if area is None:
            return {"category": category.search, "center": None, "pois": []}
        params: dict[str, Any] = {
            "q": category.search, "format": "jsonv2", "limit": self.max_depth, "bounded": 1,
            "viewbox": _viewbox(area), "addressdetails": 1, "extratags": 1,
        }
        if query.language:
            params["accept-language"] = query.language
        pois = await self.get_json(ctx, "GET", base, params=params)
        return {"category": category.search, "center": area, "pois": pois}

    async def _geocode(self, ctx: EngineContext, base: str, place: str,
                       language: str | None) -> dict[str, Any] | None:
        key = f"geo2:{place.casefold()}:{language or ''}"
        if ctx.cache is not None and (cached := await ctx.cache.get_json(key)) is not None:
            return cached or None
        # A simplified boundary polygon (about 1 KB for a borough) lets results be checked
        # against the named area itself, not just its bounding rectangle.
        params = {"q": place, "format": "jsonv2", "limit": 1, "polygon_geojson": 1,
                  "polygon_threshold": 0.002}
        if language:
            params["accept-language"] = language
        hits = await self.get_json(ctx, "GET", base, params=params)
        area = None
        if hits:
            hit = hits[0]
            shape = hit.get("geojson") or {}
            area = {
                "name": hit.get("name") or place.split(",")[0],
                "lat": float(hit["lat"]), "lon": float(hit["lon"]),
                "box": [float(v) for v in hit["boundingbox"]],
                "polygon": shape if shape.get("type") in ("Polygon", "MultiPolygon") else None,
            }
        if ctx.cache is not None:
            await ctx.cache.set_json(key, area or {}, GEOCODE_TTL)
        return area

    def parse(self, raw: dict[str, Any], query: SearchQuery) -> EngineOutput:
        require(isinstance(raw, dict) and "category" in raw, "not a local lookup result")
        request = local_request(query)
        if request is None or not raw.get("center"):
            return EngineOutput()
        category, _ = request
        area = raw["center"]
        require(isinstance(raw.get("pois"), list), "missing POI list")
        places = [_place(item, category, area) for item in raw["pois"]
                  if item.get("name") and _matches(item, category)]
        inside = sorted((p for p in places if p.in_area), key=lambda p: p.distance_m or 0)
        outside = sorted((p for p in places if not p.in_area), key=lambda p: p.distance_m or 0)
        name = area.get("name", "the area")
        notes: list[str] = []
        wanted = min(3, query.num)
        if len(inside) >= wanted or not outside:
            ranked = inside
        else:
            ranked = inside + outside
            notes.append(f"Only {len(inside)} {category.label.lower()} result"
                         f"{'' if len(inside) == 1 else 's'} inside {name}; widened to nearby "
                         f"places outside its boundary (marked in_area: false).")
        if not area.get("polygon"):
            notes.append(f"OpenStreetMap has no boundary for {name}; searched its bounding "
                         "box instead.")
        page = ranked[query.offset: query.offset + query.num]
        south, north, west, east = area["box"]
        return EngineOutput(
            places=page, has_more=len(ranked) > query.offset + query.num, notes=notes,
            area=Area(name=name, latitude=area["lat"], longitude=area["lon"],
                      bbox=[south, north, west, east], has_boundary=bool(area.get("polygon"))),
        )


def _viewbox(area: dict[str, Any]) -> str:
    """The area's bounding box plus a margin (so widening has candidates), clipped to city
    scale around the centre."""
    south, north, west, east = area["box"]
    lat, lon = area["lat"], area["lon"]
    margin_lat, margin_lon = (north - south) * 0.15, (east - west) * 0.15
    south, north = south - margin_lat, north + margin_lat
    west, east = west - margin_lon, east + margin_lon
    south, north = max(south, lat - MAX_BOX_DEGREES), min(north, lat + MAX_BOX_DEGREES)
    west, east = max(west, lon - MAX_BOX_DEGREES), min(east, lon + MAX_BOX_DEGREES)
    if north - south < 0.02:
        south, north = lat - 0.02, lat + 0.02
    if east - west < 0.02:
        west, east = lon - 0.02, lon + 0.02
    return f"{west},{north},{east},{south}"


def in_area(lat: float, lon: float, area: dict[str, Any]) -> bool:
    polygon = area.get("polygon")
    if not polygon:
        south, north, west, east = area["box"]
        return south <= lat <= north and west <= lon <= east
    shapes = [polygon["coordinates"]] if polygon["type"] == "Polygon" else polygon["coordinates"]
    return any(_in_rings(lon, lat, rings) for rings in shapes)


def _in_rings(x: float, y: float, rings: list) -> bool:
    """Even-odd ray casting over the outer ring and its holes."""
    inside = False
    for ring in rings:
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=True):
            if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                inside = not inside
    return inside


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


def _place(item: dict[str, Any], category: Category, area: dict[str, Any]) -> Place:
    tags = item.get("extratags") or {}
    lat, lon = float(item["lat"]), float(item["lon"])
    cuisine = tags.get("cuisine")
    return Place(
        name=item["name"],
        category=category.label,
        address=_address(item.get("address") or {}) or item.get("display_name", ""),
        latitude=lat, longitude=lon,
        distance_m=int(_haversine(area["lat"], area["lon"], lat, lon)),
        in_area=in_area(lat, lon, area),
        phone=tags.get("phone") or tags.get("contact:phone"),
        website=tags.get("website") or tags.get("contact:website"),
        hours=tags.get("opening_hours"),
        place_id=f"osm:{item.get('osm_type')}/{item.get('osm_id')}",
        description=cuisine.replace("_", " ").replace(";", ", ") if cuisine else None,
    )


def _address(address: dict[str, str]) -> str:
    """Street, neighbourhood, borough or district, city, region and postcode, without
    repeating a name (NYC addresses say "New York" for both city and state)."""
    street = " ".join(p for p in (address.get("house_number"), address.get("road")) if p)
    local = address.get("neighbourhood") or address.get("quarter")
    borough = address.get("suburb") or address.get("city_district")
    city = address.get("city") or address.get("town") or address.get("village")
    if city and city == address.get("state"):
        city = None  # "New York, New York 11238" says the same name twice
    region = " ".join(p for p in (address.get("state"), address.get("postcode")) if p)
    parts: list[str] = []
    for part in (street, local, borough, city, region):
        if part and part not in parts:
            parts.append(part)
    return ", ".join(parts)


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))
