"""LangChain-community's SerpAPIWrapper honors the legacy client's BACKEND for sync calls
but hardcodes https://serpapi.com/search in aresults(), leaking the key and the query.
This test pins that behavior so the README warning stays true, and shows that
fairserp's own integration does not leak.

Run: uv run --with google-search-results --with langchain-community --with aiohttp \
       pytest tests/clients/test_langchain_community.py
"""

import pytest

from tests.clients.recording_server import RecordingServer

serpapi = pytest.importorskip("serpapi")
pytest.importorskip("langchain_community")
aiohttp = pytest.importorskip("aiohttp")
if not hasattr(serpapi, "SerpApiClient"):
    pytest.skip("needs the legacy google-search-results package", allow_module_level=True)

from langchain_community.utilities import SerpAPIWrapper  # noqa: E402


class LeakDetected(Exception):
    pass


async def test_aresults_ignores_the_base_url_override(monkeypatch):
    attempted: list[str] = []

    def fake_get(self, url, *args, **kwargs):
        attempted.append(str(url))
        raise LeakDetected(url)

    monkeypatch.setattr(aiohttp.ClientSession, "get", fake_get)
    with RecordingServer() as server:
        monkeypatch.setattr(serpapi.SerpApiClient, "BACKEND", server.url)
        wrapper = SerpAPIWrapper(serpapi_api_key="fairserp-key")
        assert wrapper.results("rust")["organic_results"]
        assert server.requests[0]["path"] == "/search"
        with pytest.raises(LeakDetected):
            await wrapper.aresults("rust")
    assert attempted and attempted[0].startswith("https://serpapi.com/search")
