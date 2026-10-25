"""Python client with an explicit base URL. It never defaults to a hosted service, so a
misconfigured client fails loudly instead of sending your key and query somewhere else.

    from fairserp.client import Fairserp
    client = Fairserp(base_url="http://localhost:8000", api_key="...")
    outcome = client.search("rust async runtime", freshness="m")
    for result in outcome.results:
        print(result.rank, result.url, [s.engine for s in result.sources])
"""

from typing import Any

import httpx

from fairserp.model import SearchOutcome


class FairserpError(Exception):
    def __init__(self, status: int, body: Any) -> None:
        message = body.get("error") if isinstance(body, dict) else body
        if isinstance(message, dict):
            message = message.get("message")
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.body = body


def _auth(api_key: str | None) -> dict[str, str]:
    """No key is fine against a server with auth off (the default on 127.0.0.1)."""
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _check_base_url(base_url: str) -> str:
    if not base_url or not base_url.startswith(("http://", "https://")):
        raise ValueError("base_url is required, e.g. 'http://localhost:8000'")
    return base_url.rstrip("/")


def _raise_for(response: httpx.Response) -> None:
    if response.status_code >= 400:
        try:
            body = response.json()
        except ValueError:
            body = response.text
        raise FairserpError(response.status_code, body)


class Fairserp:
    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._http = httpx.Client(base_url=_check_base_url(base_url), timeout=timeout,
                                  headers=_auth(api_key),
                                  transport=transport, trust_env=False)

    def search(self, q: str, **params: Any) -> SearchOutcome:
        response = self._http.post("/v1/search", json={"q": q, **params})
        _raise_for(response)
        return SearchOutcome.model_validate(response.json())

    def serpapi(self, params: dict[str, Any]) -> dict[str, Any]:
        """A SerpApi-shaped search on the migration endpoint."""
        response = self._http.get("/search.json", params=params)
        _raise_for(response)
        return response.json()

    def engines(self) -> dict[str, Any]:
        response = self._http.get("/v1/engines")
        _raise_for(response)
        return response.json()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Fairserp":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class AsyncFairserp:
    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(base_url=_check_base_url(base_url), timeout=timeout,
                                       headers=_auth(api_key),
                                       transport=transport, trust_env=False)

    async def search(self, q: str, **params: Any) -> SearchOutcome:
        response = await self._http.post("/v1/search", json={"q": q, **params})
        _raise_for(response)
        return SearchOutcome.model_validate(response.json())

    async def serpapi(self, params: dict[str, Any]) -> dict[str, Any]:
        response = await self._http.get("/search.json", params=params)
        _raise_for(response)
        return response.json()

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncFairserp":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
