"""ASGI guards that run before routing: request body size, Host checks and response headers."""

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Search and fetch bodies are a few hundred bytes; nothing legitimate comes close.
MAX_BODY_BYTES = 64 * 1024
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# Liveness and readiness carry no data, and probes address the pod IP or a service name.
UNGUARDED_PATHS = frozenset({"/healthz", "/readyz"})


def host_name(host: str) -> str:
    """The name part of a Host header, lowercased, with IPv6 brackets removed."""
    host = host.strip().lower()
    if host.startswith("["):
        name, _, rest = host[1:].partition("]")
        return name if rest == "" or rest[1:].isdigit() and rest[0] == ":" else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


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
        started = refused = False

        async def counted_receive() -> Message:
            # A chunked body has no Content-Length: once it passes the limit, answer 413
            # ourselves and tell the app the client went away, so it stops reading.
            nonlocal seen, refused
            message = await receive()
            if message["type"] == "http.request" and not refused:
                seen += len(message.get("body", b""))
                if seen > self.max_bytes and not started:
                    refused = True
                    await self._refuse(scope, receive, send)
            return {"type": "http.disconnect"} if refused else message

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if refused:
                return
            started = started or message["type"] == "http.response.start"
            await send(message)

        await self.app(scope, counted_receive, tracked_send)

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
        self.allowed = LOOPBACK_HOSTS | {h.lower().strip("[]") for h in extra_hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] not in UNGUARDED_PATHS:
            name = host_name(Headers(scope=scope).get("host", ""))
            if name not in self.allowed:
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
