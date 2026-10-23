from fairserp.results import clean_snippet, displayed_link, highlighted_words, organic_result


def test_displayed_link_matches_google_style():
    assert displayed_link("https://www.example.com/a/b/c/d") == (
        "https://www.example.com › a › b › c"
    )
    assert displayed_link("https://example.com") == "https://example.com"


def test_clean_snippet_strips_markdown_and_html():
    raw = "See [Coffee (disambiguation)](https://en.wikipedia.org/wiki/Coffee_(disambiguation)).\n\n"
    raw += "![](https://img/x.png)**Bold** <b>tag</b> | cell |"
    assert clean_snippet(raw) == "See Coffee (disambiguation). Bold tag cell"


def test_clean_snippet_truncates_on_word_boundary():
    text = clean_snippet("word " * 100, limit=22)
    assert text == "word word word word ..."


def test_highlighted_words_keeps_snippet_casing():
    assert highlighted_words("coffee beans", "Coffee is made from roasted beans.") == [
        "Coffee", "beans",
    ]


def test_organic_result_drops_empty_extras():
    result = organic_result(1, "T", "https://x.org/p", "s", "q", date=None, favicon="")
    assert "date" not in result and "favicon" not in result
    assert result["source"] == "x.org"
