"""ASGI guards that run before routing: request body size, Host checks and response headers."""

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Search and fetch bodies are a few hundred bytes; nothing legitimate comes close.
MAX_BODY_BYTES = 64 * 1024
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


class _TooLarge(Exception):
    pass


class BodyLimit:
    """Refuse oversized bodies by Content-Length, and by bytes read for chunked uploads,
    before auth or parsing, so an unauthenticated client cannot make the server buffer them."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = Headers(scope=scope).get("content-length", "")
        if length.isdigit() and int(length) > self.max_bytes:
            await self._refuse(scope, receive, send)
            return
        seen = 0
        started = False

        async def counted_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise _TooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counted_receive, tracked_send)
        except _TooLarge:
            if not started:
                await self._refuse(scope, receive, send)

    async def _refuse(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse({"error": f"Request body over {self.max_bytes} bytes."},
                                status_code=413, headers={"Connection": "close"})
        await response(scope, receive, send)


class LoopbackHostOnly:
    """With auth off, answer only requests addressed to loopback names (plus the configured
    public host). Otherwise a web page could rebind its own domain to 127.0.0.1 and use the
    operator's provider keys from the browser."""

    def __init__(self, app: ASGIApp, extra_hosts: frozenset[str] = frozenset()) -> None:
        self.app = app
        self.allowed = LOOPBACK_HOSTS | extra_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            host = Headers(scope=scope).get("host", "")
            name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
            if name.lower() not in self.allowed:
                response = JSONResponse(
                    {"error": "Auth is off, so this server only answers requests addressed to "
                              "localhost. Set FAIRSERP_API_KEYS, or FAIRSERP_PUBLIC_URL to the "
                              "address you reach it on."}, status_code=421)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class SecurityHeaders:
    """nosniff on every response, so a JSON body is never sniffed into something runnable."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [h for h in message.get("headers", [])
                           if h[0].lower() != b"x-content-type-options"]
                headers.append((b"x-content-type-options", b"nosniff"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, with_headers)
