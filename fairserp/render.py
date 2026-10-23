"""HTML and Markdown views of a SerpApi-shaped response, for output=html and output=md.

Result URLs come from third parties, so the HTML view only links http(s) URLs and the app
serves it with a restrictive Content-Security-Policy.
"""

from html import escape
from typing import Any
from urllib.parse import urlsplit

HTML_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src https:; base-uri 'none'; " \
           "form-action 'none'; frame-ancestors 'none'"


def safe_href(url: str) -> str | None:
    return url if urlsplit(url).scheme in ("http", "https") else None


def to_markdown(response: dict[str, Any]) -> str:
    params = response.get("search_parameters", {})
    lines = [f"# {params.get('q', '')}", ""]
    graph = response.get("knowledge_graph")
    if graph:
        lines += [f"## {graph.get('title', '')}", "", graph.get("description", ""), ""]
    for result in response.get("organic_results", []):
        href = safe_href(result["link"])
        title = result["title"].replace("[", "(").replace("]", ")")
        lines += [f"{result['position']}. [{title}]({href})" if href else
                  f"{result['position']}. {title}",
                  f"   {result.get('snippet', '')}"]
    places = response.get("local_results")
    if isinstance(places, dict):
        places = places.get("places", [])
    for place in places or []:
        lines.append(f"- {place['title']}, {place.get('address', '')}")
    attribution = response.get("search_metadata", {}).get("fairserp", {}).get("attribution")
    if attribution:
        lines += ["", "Sources: " + "; ".join(attribution)]
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
