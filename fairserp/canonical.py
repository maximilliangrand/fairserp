"""Conservative URL canonicalization for deduplication and fusion.

Only changes that cannot alter which document a URL names: lowercase scheme and host,
drop default ports, "www." and "m." host prefixes, fragments, trailing slashes and known
tracking parameters (including Google's ``srsltid``). Query order is normalized.
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {
    "srsltid", "gclid", "dclid", "fbclid", "msclkid", "yclid", "mc_cid", "mc_eid", "igshid",
    "ref", "ref_src", "_hsenc", "_hsmi", "spm",
}


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    host = (parts.hostname or "").lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix) and host.count(".") >= 2:
            host = host[len(prefix):]
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    path = parts.path.rstrip("/") or ""
    return urlunsplit(("https" if scheme in ("http", "https") else scheme, netloc, path,
                       urlencode(sorted(query)), ""))


def strip_tracking(url: str) -> str:
    """Remove tracking parameters but otherwise keep the URL exactly as given."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
