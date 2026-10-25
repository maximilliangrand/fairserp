"""Log redaction.

Applied to uvicorn's access and error logs, httpx/httpcore and fairserp's own logger:
- query strings are parsed (so encoded names like ``api%5fkey`` are caught) and secret
  values replaced; everything after the first ``?`` counts, including a raw ``#``;
- ``q`` is redacted when the request carries ``zero_trace=true`` (or always, when query
  logging is off);
- configured secret values (provider keys, which can sit in a URL path, and client API
  keys) are replaced wherever they appear;
- non-string arguments such as ``httpx.URL`` are converted before redaction.
"""

import logging
import re
from urllib.parse import parse_qsl, urlencode

SECRET_PARAMS = {"api_key", "serp_api_key", "key", "token", "access_token"}
_URLISH = re.compile(r"(?:https?://|/)\S*\?\S*")
MIN_SECRET_LENGTH = 6


def redact_url(url: str, hide_query: bool = False) -> str:
    base, sep, query = url.partition("?")
    if not sep:
        return url
    pairs = parse_qsl(query, keep_blank_values=True)
    zero_trace = any(k.lower() == "zero_trace" and v.lower() in ("1", "true") for k, v in pairs)
    cleaned = []
    for name, value in pairs:
        lowered = name.lower().split("#")[-1]
        if lowered in SECRET_PARAMS:
            value = "REDACTED"
        elif lowered == "q" and (hide_query or zero_trace):
            value = "REDACTED"
        cleaned.append((name.split("#")[-1] if "#" in name else name, value))
    return f"{base}?{urlencode(cleaned)}"


class RedactSecrets(logging.Filter):
    def __init__(self, hide_queries: bool = False, secrets: list[str] | None = None) -> None:
        super().__init__()
        self.hide_queries = hide_queries
        self.secrets = sorted({s for s in secrets or [] if len(s) >= MIN_SECRET_LENGTH},
                              key=len, reverse=True)

    def _clean(self, value: object) -> object:
        if value is None or isinstance(value, int | float | bool):
            return value
        text = value if isinstance(value, str) else str(value)
        if "?" in text:
            text = _URLISH.sub(lambda m: redact_url(m.group(0), self.hide_queries), text)
        for secret in self.secrets:
            if secret in text:
                text = text.replace(secret, "REDACTED")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self._clean(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: self._clean(v) for k, v in record.args.items()}
        record.msg = self._clean(record.msg)
        return True


def install(hide_queries: bool, secrets: list[str] | None = None) -> None:
    for name in ("uvicorn.access", "uvicorn.error", "httpx", "httpcore", "fairserp"):
        logger = logging.getLogger(name)
        logger.filters = [f for f in logger.filters if not isinstance(f, RedactSecrets)]
        logger.addFilter(RedactSecrets(hide_queries, secrets))
