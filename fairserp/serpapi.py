"""SerpApi compatibility: the migration endpoint.

Translates SerpApi request params into a native ``SearchQuery`` (reporting every param it
cannot honor) and renders a ``SearchOutcome`` in SerpApi's response shape. fairserp does
not query Google; SerpApi engine names only work when the operator maps them explicitly
with FAIRSERP_SERPAPI_ENGINES (for example ``google:auto,google_maps:openstreetmap``).
"""

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from pydantic import ValidationError

from fairserp.model import Freshness, Mode, SearchOutcome, SearchQuery, Vertical
from fairserp.results import displayed_link, highlighted_words, source_name

TRANSPORT_PARAMS = {
    "api_key", "serp_api_key", "no_cache", "async", "output", "source", "json_restrictor",
    "zero_trace",
}
# SerpApi params that change Google's behavior and have no fairserp equivalent.
NO_EQUIVALENT = {
    "google_domain", "device", "lr", "cr", "uule", "filter", "nfpr", "ludocid", "lsig",
    "kgmid", "si", "ibp", "uds", "color_scheme", "lat", "lon", "radius", "tbm", "m",
}
NATIVE_TO_SERPAPI = {"country": "gl", "language": "hl", "freshness": "tbs", "safe": "safe",
                     "location": "location"}
SERPAPI_ENGINE_NAMES = re.compile(r"^(google|bing|duckduckgo|yahoo|yandex|baidu|naver)\w*$")
LOCAL_ENGINES = {"google_maps", "google_local"}
_LANG = re.compile(r"^([A-Za-z]{2,3})(?:[-_][A-Za-z0-9]{2,4})?$")
_COUNTRY = re.compile(r"^[A-Za-z]{2}$")


class SerpApiParamError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Translation:
    query: SearchQuery
    requested_engine: str
    ignored: list[str] = field(default_factory=list)  # SerpApi param names with no effect


def translate(params: dict[str, str], aliases: dict[str, str], known: set[str],
              strict_default: bool) -> Translation:
    q = (params.get("q") or "").strip()
    if not q:
        raise SerpApiParamError("Missing query `q` parameter.")
    requested = params.get("engine") or "google"
    engine = aliases.get(requested, requested)
    if engine not in known and engine != "auto":
        if SERPAPI_ENGINE_NAMES.match(requested):
            raise SerpApiParamError(
                f"engine={requested} is a SerpApi engine name. fairserp does not query "
                f"{requested.split('_')[0].title()}. To answer it from fairserp's providers, "
                f"map it explicitly, e.g. FAIRSERP_SERPAPI_ENGINES={requested}:auto "
                "(see docs/MIGRATION.md).")
        raise SerpApiParamError(f"Unsupported `{requested}` search engine.")

    ignored: list[str] = []
    values: dict[str, Any] = {"q": q, "engine": engine}
    if requested in LOCAL_ENGINES or engine == "openstreetmap":
        values["vertical"] = Vertical.LOCAL

    country, language = params.get("gl") or params.get("cc"), params.get("hl")
    for source, first, second in (("mkt", "language", "country"), ("kl", "country", "language")):
        if params.get(source) and "-" in params[source]:
            left, right = params[source].split("-", 1)
            picked = {first: left, second: right}
            country = country or picked["country"]
            language = language or picked["language"]
    if country:
        if not _COUNTRY.match(country):
            raise SerpApiParamError("Invalid `gl` parameter.")
        values["country"] = country.lower()
    if language:
        match = _LANG.match(language)
        if not match:
            raise SerpApiParamError("Invalid `hl` parameter.")
        values["language"] = match.group(1).lower()

    values["num"] = _int(params, "count", _int(params, "num", 10, 1, 50), 1, 50)
    values["offset"] = _int(params, "start", 0, 0, 500)
    if params.get("first"):
        values["offset"] = _int(params, "first", 1, 1, 501) - 1

    safe = (params.get("safe") or params.get("safeSearch") or "").lower()
    values["safe"] = safe in ("active", "1", "strict")

    tbs = params.get("tbs")
    if tbs:
        match = re.fullmatch(r"qdr:([hdwmy])", tbs)
        if match:
            values["freshness"] = Freshness(match.group(1))
        else:
            ignored.append("tbs")
    if params.get("df") in ("d", "w", "m", "y"):
        values["freshness"] = Freshness(params["df"])
    if params.get("location"):
        values["location"] = params["location"]
    if params.get("fairserp_mode"):
        if params["fairserp_mode"] not in {m.value for m in Mode}:
            raise SerpApiParamError("Invalid `fairserp_mode`; use fast, research or private.")
        values["mode"] = Mode(params["fairserp_mode"])
    values["strict_params"] = params.get("fairserp_strict", str(strict_default)).lower() in (
        "1", "true")

    handled = {"q", "engine", "gl", "cc", "hl", "mkt", "kl", "num", "count", "start", "first",
               "safe", "safeSearch", "tbs", "df", "location", "fairserp_mode",
               "fairserp_strict"}
    for name in params:
        if name in NO_EQUIVALENT or (name not in handled and name not in TRANSPORT_PARAMS):
            ignored.append(name)
    if values["strict_params"] and ignored:
        raise SerpApiParamError(f"fairserp cannot apply: {', '.join(sorted(ignored))}")
    try:
        query = SearchQuery(**values)
    except ValidationError as exc:
        raise SerpApiParamError(f"Invalid parameter: {exc.errors()[0]['loc'][-1]}") from exc
    return Translation(query=query, requested_engine=requested, ignored=sorted(set(ignored)))


def _int(params: dict[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = params.get(name)
    if raw in (None, ""):
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError as exc:
        raise SerpApiParamError(f"Invalid `{name}` parameter.") from exc


def search_url(base: str, params: dict[str, Any]) -> str:
    query = {k: v for k, v in params.items() if k not in TRANSPORT_PARAMS and v is not None}
    return f"{base}/search.json?{urlencode(sorted(query.items()))}"


def render(outcome: SearchOutcome, base: str, params: dict[str, str],
           serp_ignored: list[str], archived: bool) -> dict[str, Any]:
    """A SearchOutcome in SerpApi's response shape, with fairserp's provenance attached."""
    ignored = sorted(set(serp_ignored) | {NATIVE_TO_SERPAPI[p] for p in outcome.params.ignored})
    applied = sorted(NATIVE_TO_SERPAPI[p] for p in outcome.params.applied)
    meta: dict[str, Any] = {
        "id": outcome.id,
        "status": "Error" if outcome.status in ("failed", "rate_limited") else "Success",
        "json_endpoint": f"{base}/searches/{outcome.id}.json",
        "markdown_endpoint": f"{base}/searches/{outcome.id}.md",
        "raw_html_file": f"{base}/searches/{outcome.id}.html",
        "created_at": outcome.created_at,
        "processed_at": outcome.created_at,
        "total_time_taken": round(outcome.elapsed_ms / 1000, 2),
        "fairserp": {
            "status": outcome.status.value,
            "requested_engine": outcome.requested_engine,
            "resolved_engine": outcome.resolved_engine,
            "providers": [p.model_dump(exclude_none=True, exclude_defaults=True)
                          | {"engine": p.engine, "status": p.status.value}
                          for p in outcome.providers],
            "params": {"applied": applied, "ignored": ignored},
            "attribution": outcome.attribution,
            "notes": outcome.notes,
            "retention": outcome.retention.model_dump(),
            "archived": archived,
        },
    }
    response: dict[str, Any] = {
        "search_metadata": meta,
        "search_parameters": {"engine": outcome.requested_engine, "q": outcome.query.q,
                              **{k: _echo(k, v) for k, v in params.items()
                                 if k not in TRANSPORT_PARAMS | {"engine", "q"}}},
        "search_information": _information(outcome),
    }
    local_list = outcome.query.vertical == Vertical.LOCAL
    if outcome.places:
        places = [_place(i, p) for i, p in enumerate(outcome.places, start=1)]
        response["local_results"] = places if local_list else {"places": places}
    if outcome.entity:
        response["knowledge_graph"] = _knowledge_graph(outcome, base, params)
    if outcome.answer and not outcome.answer.generated:
        response["answer_box"] = {"type": outcome.answer.type, "answer": outcome.answer.text,
                                  **({"link": outcome.answer.citations[0]}
                                     if outcome.answer.citations else {})}
    if outcome.answer and outcome.answer.generated:
        response["ai_overview"] = {
            "text_blocks": [{"type": "paragraph", "snippet": outcome.answer.text}],
            "references": [{"index": i, "link": link}
                           for i, link in enumerate(outcome.answer.citations)],
            "fairserp_generated": True,
        }
    if outcome.questions:
        response["related_questions"] = [
            {"question": q.question, "snippet": q.answer, "title": q.title, "link": q.url,
             "displayed_link": displayed_link(q.url) if q.url else ""}
            for q in outcome.questions
        ]
    if outcome.results:
        response["organic_results"] = [_organic(r, outcome.query.q) for r in outcome.results]
    if outcome.related:
        response["related_searches"] = [
            {"block_position": 1, "query": text,
             "serpapi_link": search_url(base, {**params, "q": text, "start": None,
                                               "first": None})}
            for text in outcome.related
        ]
    if outcome.results:
        response.update(_pagination(outcome, base, params))
    if outcome.status == "empty":
        response["error"] = outcome.error
    return response


def _echo(key: str, value: str) -> Any:
    return int(value) if key in ("start", "num") and value.lstrip("-").isdigit() else value


def _information(outcome: SearchOutcome) -> dict[str, Any]:
    info: dict[str, Any] = {
        "organic_results_state": "Results for exact spelling" if outcome.results or
        outcome.places else "Fully empty",
        "query_displayed": outcome.query.q,
    }
    if outcome.total_results is not None:
        info["total_results"] = outcome.total_results
    info["time_taken_displayed"] = round(outcome.elapsed_ms / 1000, 2)
    page = outcome.query.offset // outcome.query.num + 1
    if page > 1:
        info["page_number"] = page
    return info


def _organic(result, q: str) -> dict[str, Any]:
    data: dict[str, Any] = {
        "position": result.rank,
        "title": result.title,
        "link": result.url,
        "displayed_link": displayed_link(result.url),
    }
    if result.published_at:
        data["date"] = result.published_at
    data["snippet"] = result.snippet
    words = highlighted_words(q, result.snippet)
    if words:
        data["snippet_highlighted_words"] = words
    if result.sitelinks:
        data["sitelinks"] = {"inline": [{"title": s.title, "link": s.url}
                                        for s in result.sitelinks]}
    for key, value in (("favicon", result.favicon), ("thumbnail", result.thumbnail),
                       ("fairserp_updated_at", result.updated_at)):
        if value:
            data[key] = value
    data["source"] = result.source_name or source_name(result.url)
    data["fairserp_sources"] = [s.model_dump() for s in result.sources]
    return data


def _knowledge_graph(outcome: SearchOutcome, base: str, params: dict[str, str]) -> dict:
    entity = outcome.entity
    graph: dict[str, Any] = {"title": entity.title, "type": entity.type,
                             "description": entity.description}
    if entity.url:
        graph["source"] = {"name": source_name(entity.url), "link": entity.url}
    if entity.website:
        graph["website"] = entity.website
    if entity.image:
        graph["header_images"] = [{"image": entity.image, "source": entity.url}]
    if entity.wikidata_id:
        graph["wikidata_id"] = entity.wikidata_id
    for key, value in entity.attributes.items():
        graph.setdefault(key, value)
    graph["serpapi_knowledge_graph_search_link"] = search_url(
        base, {**params, "q": entity.title, "start": None, "first": None})
    return {k: v for k, v in graph.items() if v not in ("", None)}


def _place(position: int, place) -> dict[str, Any]:
    data: dict[str, Any] = {
        "position": position, "title": place.name, "place_id": place.place_id,
        "type": place.category, "address": place.address,
        "gps_coordinates": {"latitude": place.latitude, "longitude": place.longitude},
    }
    optional = {"phone": place.phone, "hours": place.hours, "rating": place.rating,
                "reviews": place.reviews, "description": place.description,
                "fairserp_distance_m": place.distance_m, "fairserp_in_area": place.in_area}
    data.update({k: v for k, v in optional.items() if v is not None})
    if place.website:
        data["links"] = {"website": place.website}
    return data


def _pagination(outcome: SearchOutcome, base: str, params: dict[str, str]) -> dict[str, Any]:
    num, offset = outcome.query.num, outcome.query.offset
    page = offset // num + 1
    plain = {k: v for k, v in params.items() if k not in ("first", "start")}

    def link(number: int) -> str:
        return search_url(base, {**plain, "start": (number - 1) * num})

    others = {str(n): link(n) for n in range(max(1, page - 4), page)}
    if outcome.has_more:
        others[str(page + 1)] = link(page + 1)
    pagination: dict[str, Any] = {"current": page, "other_pages": others}
    serpapi: dict[str, Any] = {"current": page, "other_pages": others}
    if outcome.has_more:
        pagination["next"] = serpapi["next"] = serpapi["next_link"] = link(page + 1)
    if page > 1:
        pagination["previous"] = serpapi["previous"] = serpapi["previous_link"] = link(page - 1)
    return {"pagination": pagination, "serpapi_pagination": serpapi}
