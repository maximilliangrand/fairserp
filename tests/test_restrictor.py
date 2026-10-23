import pytest

from fairserp.restrictor import RestrictorError, apply

DATA = {
    "search_metadata": {"id": "abc", "status": "Success"},
    "organic_results": [
        {"title": "A", "link": "a", "sitelinks": {"inline": [{"link": "a1"}]}},
        {"title": "B", "link": "b"},
        {"title": "C", "link": "c"},
    ],
}


def test_top_level_fields():
    assert apply(DATA, "search_metadata") == {"search_metadata": DATA["search_metadata"]}


def test_index_slice_and_projection():
    assert apply(DATA, "organic_results[0]") == {"organic_results": [DATA["organic_results"][0]]}
    assert apply(DATA, "organic_results[1:3].title") == {
        "organic_results": [{"title": "B"}, {"title": "C"}]
    }
    assert apply(DATA, "organic_results[].{title, sitelinks.inline[].link}") == {
        "organic_results": [
            {"title": "A", "sitelinks": {"inline": [{"link": "a1"}]}},
            {"title": "B"},
            {"title": "C"},
        ]
    }


def test_multiple_paths_and_missing_keys():
    assert apply(DATA, "search_metadata.id, nope, organic_results[9]") == {
        "search_metadata": {"id": "abc"},
        "organic_results": [],
    }


def test_unbalanced_braces():
    with pytest.raises(RestrictorError):
        apply(DATA, "organic_results[].{title")
