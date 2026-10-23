"""A local HTTP server that records every request and answers with SerpApi-shaped JSON."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

BODY = {
    "search_metadata": {"id": "abc", "status": "Success"},
    "organic_results": [{"position": 1, "title": "Rust", "link": "https://rust-lang.org",
                         "snippet": "Rust lang"}],
    "serpapi_pagination": {"current": 1, "next": None},
}


class RecordingServer:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parts = urlsplit(self.path)
                recorder.requests.append({"path": parts.path, "query": parse_qs(parts.query),
                                          "headers": dict(self.headers)})
                if parts.path.startswith("/locations"):
                    body = [{"canonical_name": "Austin,Texas,United States"}]
                elif parts.path.startswith("/account"):
                    body = {"this_hour_searches": 1}
                else:
                    body = dict(BODY)
                    body["serpapi_pagination"] = {
                        "current": 1,
                        "next": f"http://127.0.0.1:{recorder.port}/search.json?q=rust&start=10"}
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "RecordingServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
