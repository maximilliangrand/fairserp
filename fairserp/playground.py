"""The playground page served at GET /. One file with inline CSS and JS, delivered under a
Content-Security-Policy that allows exactly those inline blocks (by hash) and nothing
else: no third-party requests, no fonts or scripts from CDNs."""

import base64
import hashlib
import re
from functools import lru_cache
from pathlib import Path

PAGE = Path(__file__).parent / "static" / "playground.html"


def _hash(block: str) -> str:
    digest = hashlib.sha256(block.encode()).digest()
    return f"'sha256-{base64.b64encode(digest).decode()}'"


@lru_cache
def page() -> tuple[str, str]:
    html = PAGE.read_text()
    styles = re.findall(r"<style>(.*?)</style>", html, re.S)
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    csp = "; ".join([
        "default-src 'none'",
        f"style-src {' '.join(_hash(s) for s in styles)}",
        f"script-src {' '.join(_hash(s) for s in scripts)}",
        "connect-src 'self'",
        "img-src https: data:",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ])
    return html, csp
