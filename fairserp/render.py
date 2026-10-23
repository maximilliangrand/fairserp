"""HTML and Markdown views of a SerpApi-shaped response, for output=html and output=md.

Result URLs come from third parties, so the HTML view only links http(s) URLs and the app
serves it with a restrictive Content-Security-Policy.
"""

import re
from html import escape
from typing import Any
from urllib.parse import quote, urlsplit

HTML_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src https:; base-uri 'none'; " \
           "form-action 'none'; frame-ancestors 'none'"


def safe_href(url: str) -> str | None:
    return url if urlsplit(url).scheme in ("http", "https") else None


_MD_SPECIAL = re.compile(r"([\\`*_\[\]()<>#!|])")


def md_text(text: str) -> str:
    """Escape Markdown control characters in third-party text."""
    return _MD_SPECIAL.sub(r"\\\1", " ".join(str(text).split()))


def md_href(url: str) -> str | None:
    """An http(s) URL that cannot break out of a Markdown link target."""
    href = safe_href(url)
    if href is None:
        return None
    return quote(href, safe=":/?#[]@!$&'*+,;=%-._~").replace("(", "%28").replace(")", "%29")


def to_markdown(response: dict[str, Any]) -> str:
    params = response.get("search_parameters", {})
    lines = [f"# {md_text(params.get('q', ''))}", ""]
    graph = response.get("knowledge_graph")
    if graph:
        lines += [f"## {md_text(graph.get('title', ''))}", "",
                  md_text(graph.get("description", "")), ""]
    for result in response.get("organic_results", []):
        href = md_href(result["link"])
        title = md_text(result["title"])
        lines += [f"{result['position']}. [{title}](<{href}>)" if href else
                  f"{result['position']}. {title}",
                  f"   {md_text(result.get('snippet', ''))}"]
    places = response.get("local_results")
    if isinstance(places, dict):
        places = places.get("places", [])
    for place in places or []:
        lines.append(f"- {md_text(place['title'])}, {md_text(place.get('address', ''))}")
    attribution = response.get("search_metadata", {}).get("fairserp", {}).get("attribution")
    if attribution:
        lines += ["", "Sources: " + md_text("; ".join(attribution))]
    return "\n".join(lines) + "\n"


def to_html(response: dict[str, Any]) -> str:
    query = escape(str(response.get("search_parameters", {}).get("q", "")))
    items = []
    for result in response.get("organic_results", []):
        href = safe_href(result["link"])
        title = escape(result["title"])
        anchor = (f'<a href="{escape(href)}" rel="noopener noreferrer nofollow">{title}</a>'
                  if href else title)
        items.append(f"<li>{anchor}<p>{escape(result.get('snippet', ''))}</p></li>")
    attribution = response.get("search_metadata", {}).get("fairserp", {}).get("attribution")
    footer = f"<footer>{escape('; '.join(attribution))}</footer>" if attribution else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{query} - fairserp</title></head>"
        f"<body><h1>{query}</h1><ol>{''.join(items)}</ol>{footer}</body></html>"
    )
