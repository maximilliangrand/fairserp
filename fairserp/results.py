"""Text helpers shared by parsers and renderers."""

import re
from urllib.parse import urlsplit

# Link targets may contain one level of parentheses, as in Wikipedia's "Coffee_(drink)".
_LINK_TARGET = r"\((?:[^()]|\([^()]*\))*\)"
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]" + _LINK_TARGET)
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]" + _LINK_TARGET)
_MARKDOWN_NOISE = re.compile(r"[#*`|>\\]+|(?<!\w)_+|_+(?!\w)")
_WHITESPACE = re.compile(r"\s+")
_HTML_TAG = re.compile(r"<[^>]+>")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
_TERM = re.compile(r"\w+", re.UNICODE)
_STOPWORDS = {"the", "a", "an", "of", "in", "on", "for", "to", "and", "or", "is", "what", "how"}


def displayed_link(url: str) -> str:
    """Render a URL the way SERPs show it: ``https://www.example.com › docs › page``."""
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    base = f"{parts.scheme}://{parts.netloc}" if parts.scheme else parts.netloc
    return " › ".join([base, *segments[:3]])


def source_name(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.")


def clean_text(text: str | None) -> str:
    """Flatten markdown or HTML into one line of plain text."""
    if not text:
        return ""
    text = _HTML_TAG.sub("", text)
    text = _MARKDOWN_IMAGE.sub("", text)
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = _MARKDOWN_NOISE.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " ..."


def query_terms(query: str) -> list[str]:
    terms = [t.lower() for t in _TERM.findall(query) if len(t) > 1]
    return [t for t in dict.fromkeys(terms) if t not in _STOPWORDS] or terms


def passages(text: str, query: str, limit: int = 3, max_chars: int = 320) -> list[str]:
    """The sentences (or sentence pairs) of ``text`` that best match the query terms."""
    sentences = [s.strip() for s in _SENTENCE.split(text) if len(s.strip()) > 20]
    if not sentences:
        return [truncate(text, max_chars)] if text else []
    terms = query_terms(query)

    def score(sentence: str) -> float:
        words = {w.lower() for w in _TERM.findall(sentence)}
        return sum(1 for t in terms if t in words) + min(len(sentence), 300) / 3000

    ranked = sorted(range(len(sentences)), key=lambda i: -score(sentences[i]))
    chosen: list[str] = []
    for index in ranked[:limit]:
        if score(sentences[index]) < 1 and chosen:
            break
        chosen.append(truncate(sentences[index], max_chars))
    return chosen


def snippet(text: str | None, query: str, limit: int = 300) -> str:
    """A query-relevant snippet: the best matching sentences, not just the page lead."""
    flat = clean_text(text)
    if len(flat) <= limit:
        return flat
    best = passages(flat, query, limit=2, max_chars=limit)
    joined = " ... ".join(best)
    return truncate(joined, limit) if best else truncate(flat, limit)


def highlighted_words(query: str, text: str) -> list[str]:
    """Query terms that appear in the text, in the text's own casing."""
    found: list[str] = []
    for term in dict.fromkeys(t for t in re.split(r"\W+", query) if len(t) > 1):
        match = re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE)
        if match and match.group(0) not in found:
            found.append(match.group(0))
    return found
