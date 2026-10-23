"""HTML and Markdown views of a stored search, for output=html and output=md."""

from html import escape
from typing import Any


def to_markdown(response: dict[str, Any]) -> str:
    params = response.get("search_parameters", {})
    lines = [f"# {params.get('q', '')}", ""]
    graph = response.get("knowledge_graph")
    if graph:
        lines += [f"## {graph.get('title', '')}", "", graph.get("description", ""), ""]
    for result in response.get("organic_results", []):
        lines += [
            f"{result['position']}. [{result['title']}]({result['link']})",
            f"   {result.get('snippet', '')}",
        ]
    places = response.get("local_results")
    if isinstance(places, dict):
        places = places.get("places", [])
    for place in places or []:
        lines.append(f"- {place['title']}, {place.get('address', '')}")
    return "\n".join(lines) + "\n"


def to_html(response: dict[str, Any]) -> str:
    query = escape(str(response.get("search_parameters", {}).get("q", "")))
    items = "".join(
        f'<li><a href="{escape(r["link"])}">{escape(r["title"])}</a>'
        f"<p>{escape(r.get('snippet', ''))}</p></li>"
        for r in response.get("organic_results", [])
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{query} - fairserp</title></head>"
        f"<body><h1>{query}</h1><ol>{items}</ol></body></html>"
    )
