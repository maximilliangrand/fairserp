"""Log redaction. Query strings are parsed (so encoded names like ``api%5fkey`` are caught)
and secret values are replaced before any handler formats the record."""

import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SECRET_PARAMS = {"api_key", "serp_api_key", "key", "token", "access_token"}
_QUERY_IN_TEXT = re.compile(r"(https?://\S+|/\S*\?\S+)")


def redact_url(url: str, hide_query: bool = False) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    zero_trace = any(k.lower() == "zero_trace" and v.lower() in ("1", "true") for k, v in pairs)
    cleaned = []
    for name, value in pairs:
        lowered = name.lower()
        if lowered in SECRET_PARAMS:
            value = "REDACTED"
        elif lowered == "q" and (hide_query or zero_trace):
            value = "REDACTED"
        cleaned.append((name, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(cleaned),
                       parts.fragment))


class RedactSecrets(logging.Filter):
    def __init__(self, hide_queries: bool = False) -> None:
        super().__init__()
        self.hide_queries = hide_queries

    def _clean(self, value: object) -> object:
        if not isinstance(value, str) or "?" not in value:
            return value
        return _QUERY_IN_TEXT.sub(lambda m: redact_url(m.group(0), self.hide_queries), value)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self._clean(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: self._clean(v) for k, v in record.args.items()}
        record.msg = self._clean(record.msg)
        return True


def install(hide_queries: bool) -> None:
    for name in ("uvicorn.access", "uvicorn.error", "httpx", "httpcore", "fairserp"):
        logger = logging.getLogger(name)
        logger.filters = [f for f in logger.filters if not isinstance(f, RedactSecrets)]
        logger.addFilter(RedactSecrets(hide_queries))
