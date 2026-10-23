"""SerpApi request normalization and response assembly."""

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from fairserp.engines.base import SearchRequest

# Params that never change what a search returns, so they stay out of cache keys and
# the search_parameters echo.
TRANSPORT_PARAMS = {
    "api_key", "serp_api_key", "no_cache", "async", "output", "source", "json_restrictor",
    "zero_trace",
}
NO_RESULTS = "fairserp hasn't returned any results for this query."

# Order of top-level keys in a SerpApi google response.
BLOCK_ORDER = (
    "ads", "local_map", "local_results", "knowledge_graph", "answer_box",
    "related_questions", "organic_results", "related_searches",
)


_GL = re.compile(r"[A-Za-z]{2}")
_HL = re.compile(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,4})?")


class ParamError(ValueError):
    pass


def truthy(value: str | None) -> bool:
    return str(value).lower() in {"true", "1", "yes"}


def _int(params: dict[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = params.get(name)
    if raw in (None, ""):
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError as exc:
        raise ParamError(f"Invalid `{name}` parameter.") from exc


def build_request(params: dict[str, str]) -> SearchRequest:
    """Map SerpApi params (including bing and duckduckgo dialects) onto one request."""
    q = (params.get("q") or "").strip()
    if not q:
        raise ParamError("Missing query `q` parameter.")
    engine = params.get("engine") or "google"
    gl, hl = params.get("gl"), params.get("hl")
    if params.get("mkt") and "-" in params["mkt"]:
        hl, gl = (hl or params["mkt"].split("-")[0]), (gl or params["mkt"].split("-")[1])
    if params.get("kl") and "-" in params["kl"]:
        gl, hl = (gl or params["kl"].split("-")[0]), (hl or params["kl"].split("-")[1])
    gl = gl or params.get("cc")
    if gl and not _GL.fullmatch(gl):
        raise ParamError("Invalid `gl` parameter.")
    if hl and not _HL.fullmatch(hl):
        raise ParamError("Invalid `hl` parameter.")

    num = _int(params, "num", 10, 1, 100)
    num = _int(params, "count", num, 1, 50)
    start = _int(params, "start", 0, 0, 1000)
    if params.get("first"):
        start = _int(params, "first", 1, 1, 1000) - 1

    safe = params.get("safe")
    if params.get("safeSearch"):
        safe = "active" if params["safeSearch"].lower() == "strict" else "off"
    if safe == "1":
        safe = "active"
    elif safe in ("-1", "-2"):
        safe = "off"

    return SearchRequest(
        q=q, engine=engine, location=params.get("location"), gl=gl, hl=hl, safe=safe,
        num=num, start=start, params=dict(params),
    )


def cache_key(params: dict[str, str]) -> str:
    material = sorted((k, v) for k, v in params.items() if k not in TRANSPORT_PARAMS)
    digest = hashlib.sha256(json.dumps(material).encode()).hexdigest()
    return f"cache:{digest}"


def new_search_id() -> str:
    return secrets.token_hex(12)


def timestamp(moment: datetime | None = None) -> str:
    return (moment or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M:%S UTC")


def search_url(public_url: str, params: dict[str, Any]) -> str:
    query = {k: v for k, v in params.items() if k not in TRANSPORT_PARAMS and v is not None}
    return f"{public_url.rstrip('/')}/search.json?{urlencode(sorted(query.items()))}"


def metadata(search_id: str, public_url: str, status: str) -> dict[str, Any]:
    base = public_url.rstrip("/")
    now = timestamp()
    return {
        "id": search_id,
        "status": status,
        "json_endpoint": f"{base}/searches/{search_id}.json",
        "markdown_endpoint": f"{base}/searches/{search_id}.md",
        "raw_html_file": f"{base}/searches/{search_id}.html",
        "created_at": now,
        "processed_at": now,
    }


def search_parameters(request: SearchRequest) -> dict[str, Any]:
    echoed: dict[str, Any] = {"engine": request.engine, "q": request.q}
    for key, value in request.params.items():
        if key in TRANSPORT_PARAMS or key in echoed or value in (None, ""):
            continue
        if key == "location":
            echoed["location_requested"] = value
            echoed["location_used"] = value
        elif key in ("start", "num"):
            echoed[key] = int(value)
        else:
            echoed[key] = value
    return echoed


def assemble(
    request: SearchRequest,
    fragments: dict[str, Any],
    meta: dict[str, Any],
    public_url: str,
    elapsed: float,
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    organic = fragments.get("organic_results") or []
    info = {
        "organic_results_state": "Results for exact spelling" if organic else "Fully empty",
        "query_displayed": request.q,
        **(fragments.get("search_information") or {}),
        "time_taken_displayed": round(elapsed, 2),
    }
    if request.page > 1:
        info["page_number"] = request.page

    meta = {**meta, "processed_at": timestamp(), "total_time_taken": round(elapsed, 2)}
    meta["fairserp_sources"] = sources
    response: dict[str, Any] = {
        "search_metadata": meta,
        "search_parameters": search_parameters(request),
        "search_information": info,
    }
    for block in BLOCK_ORDER:
        if fragments.get(block):
            response[block] = _link_block(block, fragments[block], request, public_url)
    if request.engine in ("google_maps", "google_local") and "local_results" in response:
        response["local_results"] = response["local_results"].get("places", [])
    if organic:
        response.update(_pagination(request, public_url, len(organic)))
    elif not any(block in response for block in BLOCK_ORDER):
        response["error"] = NO_RESULTS
    return response


def _link_block(block: str, value: Any, request: SearchRequest, public_url: str) -> Any:
    """Add serpapi_link style self links so clients can follow them to this server."""
    if block == "related_searches":
        return [
            {**item, "serpapi_link": search_url(public_url, {**request.params, "q": item["query"],
                                                             "start": None})}
            for item in value
        ]
    if block == "knowledge_graph" and value.get("title"):
        link = search_url(public_url, {**request.params, "q": value["title"], "start": None})
        return {**value, "serpapi_knowledge_graph_search_link": link}
    return value


def _pagination(request: SearchRequest, public_url: str, count: int) -> dict[str, Any]:
    page = request.page
    # Bing's 1-based "first" would override "start", so links always use "start".
    base = {k: v for k, v in request.params.items() if k != "first"}

    def page_link(number: int) -> str:
        return search_url(public_url, {**base, "start": (number - 1) * request.num})

    has_next = count >= request.num
    others = {str(n): page_link(n) for n in range(max(1, page - 4), page + 6) if n != page}
    if not has_next:
        others = {k: v for k, v in others.items() if int(k) < page}
    nxt = page_link(page + 1) if has_next else None
    pagination: dict[str, Any] = {"current": page, "other_pages": others}
    serpapi_pagination: dict[str, Any] = {"current": page, "other_pages": others}
    if nxt:
        pagination["next"] = nxt
        serpapi_pagination["next_link"] = nxt
        serpapi_pagination["next"] = nxt
    if page > 1:
        prev = page_link(page - 1)
        pagination["previous"] = prev
        serpapi_pagination["previous_link"] = prev
        serpapi_pagination["previous"] = prev
    return {"pagination": pagination, "serpapi_pagination": serpapi_pagination}
