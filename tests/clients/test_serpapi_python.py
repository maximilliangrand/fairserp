"""SerpApi's current Python client (`pip install serpapi`) with only BASE_DOMAIN changed.
Run in CI's client-truth job: uv run --with serpapi pytest tests/clients/test_serpapi_python.py
"""

import pytest

from tests.clients.recording_server import RecordingServer

serpapi = pytest.importorskip("serpapi")
if not hasattr(serpapi, "HTTPClient"):
    pytest.skip("legacy google-search-results package installed", allow_module_level=True)


def test_every_request_goes_to_the_configured_host(monkeypatch):
    with RecordingServer() as server:
        monkeypatch.setattr(serpapi.HTTPClient, "BASE_DOMAIN", server.url)
        client = serpapi.Client(api_key="k")
        results = client.search(q="rust", engine="google")
        results.next_page()
        client.search_archive({"search_id": "abc"})
        client.account()
        client.locations(q="Austin", limit=1)
    paths = [r["path"] for r in server.requests]
    assert paths == ["/search", "/search.json", "/searches/abc", "/account.json", "/locations.json"]
    # The client sends the key as a query parameter; fairserp redacts it from its logs.
    assert server.requests[0]["query"]["api_key"] == ["k"]
