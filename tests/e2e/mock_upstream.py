"""Deterministic stand-in for a SearXNG instance, used by CI's docker-compose e2e job so the
required pipeline never depends on third-party uptime."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAYLOAD = (Path(__file__).parent.parent / "fixtures" / "searxng_coffee.json").read_bytes()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = PAYLOAD if self.path.startswith("/search") else json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
