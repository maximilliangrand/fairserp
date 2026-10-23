"""Helpers that build SerpApi-shaped result objects from plain values."""

import re
from typing import Any
from urllib.parse import urlsplit

# Link targets may contain one level of parentheses, as in Wikipedia's "Coffee_(drink)".
_LINK_TARGET = r"\((?:[^()]|\([^()]*\))*\)"
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]" + _LINK_TARGET)
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]" + _LINK_TARGET)
_MARKDOWN_NOISE = re.compile(r"[#*_`|>\\]+")
_WHITESPACE = re.compile(r"\s+")
_HTML_TAG = re.compile(r"<[^>]+>")


def displayed_link(url: str) -> str:
    """Render a URL the way Google shows it: ``https://www.example.com › docs › page``."""
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    base = f"{parts.scheme}://{parts.netloc}" if parts.scheme else parts.netloc
    return " › ".join([base, *segments[:3]])


def source_name(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.")


def clean_snippet(text: str | None, limit: int = 300) -> str:
    """Flatten markdown or HTML snippets into one line of plain text."""
    if not text:
        return ""
    text = _HTML_TAG.sub("", text)
    text = _MARKDOWN_IMAGE.sub("", text)
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = _MARKDOWN_NOISE.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + " ..."
    return text


def highlighted_words(query: str, snippet: str) -> list[str]:
    """Query terms that appear in the snippet, in the snippet's own casing."""
    found: list[str] = []
    for term in dict.fromkeys(t for t in re.split(r"\W+", query) if len(t) > 1):
        match = re.search(rf"\b{re.escape(term)}\b", snippet, re.IGNORECASE)
        if match and match.group(0) not in found:
            found.append(match.group(0))
    return found


def organic_result(
    position: int,
    title: str,
    link: str,
    snippet: str | None,
    query: str,
    **extra: Any,
) -> dict[str, Any]:
    text = clean_snippet(snippet)
    result: dict[str, Any] = {
        "position": position,
        "title": clean_snippet(title, limit=200),
        "link": link,
        "displayed_link": displayed_link(link),
        "snippet": text,
    }
    words = highlighted_words(query, text)
    if words:
        result["snippet_highlighted_words"] = words
    result["source"] = source_name(link)
    result.update({k: v for k, v in extra.items() if v not in (None, "", [], {})})
    return result
