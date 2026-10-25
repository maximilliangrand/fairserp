"""The playground page served at GET /. One file with inline CSS and JS, delivered under a
Content-Security-Policy that allows exactly those inline blocks (by hash) and nothing
else: no third-party requests, no fonts or scripts from CDNs. Also the /docs page, whose
Swagger UI comes from a pinned CDN release under Subresource Integrity."""

import base64
import hashlib
import re
from functools import lru_cache
from pathlib import Path

PAGE = Path(__file__).parent / "static" / "playground.html"

# Swagger UI for /docs, pinned to one release and checked by Subresource Integrity, so a
# compromised or changed CDN file is refused by the browser instead of run.
SWAGGER = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.34.0"
SWAGGER_JS_SRI = "sha384-9Td7MKiIr2S83E1kZyyVi8KdJw/S8CpAvu0llLyMeg0xq/hDGiZJfVn0fV4llCmK"
SWAGGER_CSS_SRI = "sha384-4c3vsE5f+trhFyHrFrLM1fFGUn531NIaC7dcqEWIJdoRQJ5CkJbSuSCwXLpaS1KF"


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


@lru_cache
def api_docs() -> tuple[str, str]:
    from fastapi.openapi.docs import get_swagger_ui_html

    js, css = f"{SWAGGER}/swagger-ui-bundle.js", f"{SWAGGER}/swagger-ui.css"
    html = get_swagger_ui_html(openapi_url="/openapi.json", title="fairserp API",
                               swagger_js_url=js, swagger_css_url=css,
                               swagger_favicon_url="data:,").body.decode()
    html = html.replace(f'src="{js}"',
                        f'src="{js}" integrity="{SWAGGER_JS_SRI}" crossorigin="anonymous"')
    html = html.replace(f'href="{css}"',
                        f'href="{css}" integrity="{SWAGGER_CSS_SRI}" crossorigin="anonymous"')
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    csp = "; ".join([
        "default-src 'none'",
        f"script-src {js} {' '.join(_hash(s) for s in scripts)}",
        # Swagger UI sets element styles at runtime.
        f"style-src {css} 'unsafe-inline'",
        "connect-src 'self'",
        "img-src 'self' data:",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ])
    return html, csp
