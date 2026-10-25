from fairserp.canonical import canonical_url, strip_tracking
from fairserp.fusion import rrf
from fairserp.model import WebResult
from fairserp.results import clean_text, displayed_link, highlighted_words, passages, snippet


def test_displayed_link():
    assert displayed_link("https://www.example.com/a/b/c/d") == (
        "https://www.example.com › a › b › c")


def test_clean_text_strips_markdown_and_html():
    raw = "See [Coffee (disambiguation)](https://en.wikipedia.org/wiki/Coffee_(disambiguation))."
    raw += "\n\n![](https://img/x.png)**Bold** <b>tag</b> | cell | snake_case"
    assert clean_text(raw) == "See Coffee (disambiguation). Bold tag cell snake_case"


def test_snippet_prefers_query_relevant_sentences():
    text = ("For other uses, see the disambiguation page for many unrelated things. "
            "This page has a long infobox with nothing useful in it at all. "
            "Arabica coffee beans are grown at high altitude in Ethiopia and Colombia.") * 3
    assert "Arabica coffee beans" in snippet(text, "coffee beans", limit=120)
    assert passages(text, "coffee beans", limit=1)[0].startswith("Arabica")


def test_highlighted_words_keep_casing():
    assert highlighted_words("coffee beans", "Coffee is made from beans.") == ["Coffee", "beans"]


def test_canonical_url_collapses_harmless_variants():
    variants = [
        "https://www.example.com/page/?utm_source=x&b=2&a=1",
        "http://example.com/page?a=1&b=2&srsltid=AYCX",
        "https://m.example.com/page?a=1&b=2#section",
    ]
    assert len({canonical_url(v) for v in variants}) == 1
    assert canonical_url("https://example.com/a") != canonical_url("https://example.com/b")


def test_strip_tracking_keeps_everything_else():
    assert strip_tracking("https://x.org/p?id=7&srsltid=abc#top") == "https://x.org/p?id=7#top"


def test_rrf_dedups_and_orders_by_agreement():
    def results(*urls):
        return [WebResult(url=u, title=u) for u in urls]

    fused = rrf({"a": ("own-index", results("https://x.org/1", "https://x.org/2")),
                 "b": ("own-index", results("https://www.x.org/2/", "https://x.org/3"))},
                limit=10)
    assert [r.canonical_url for r in fused] == ["https://x.org/2", "https://x.org/1",
                                                "https://x.org/3"]
    assert {s.engine for s in fused[0].sources} == {"a", "b"}


def test_clean_text_unescapes_html_entities():
    cleaned = clean_text("Einstein&#039;s <b>fourth</b>-great &amp; co")
    assert cleaned == "Einstein's fourth-great & co"
