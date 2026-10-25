"""robots.txt evaluation per RFC 9309.

Python's ``urllib.robotparser`` does plain prefix matching and ignores the ``*`` and ``$``
wildcards that real robots.txt files use (GitHub's, for one), so it can allow paths a site
disallows. This implements the RFC: pick the group for the most specific matching
user-agent token (falling back to ``*``), then the longest matching rule wins, with Allow
winning a tie.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (allow, pattern)


class Robots:
    def __init__(self, text: str) -> None:
        self.groups: list[_Group] = []
        current: _Group | None = None
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if current is None or not last_was_agent:
                    current = _Group()
                    self.groups.append(current)
                current.agents.append(value.lower())
                last_was_agent = True
            elif key in ("allow", "disallow") and current is not None:
                last_was_agent = False
                if value:
                    current.rules.append((key == "allow", _normalize(value)))
            else:
                last_was_agent = False

    def _rules_for(self, token: str) -> list[tuple[bool, str]]:
        token = token.lower()
        specific = [g for g in self.groups if any(a != "*" and a in token for a in g.agents)]
        chosen = specific or [g for g in self.groups if "*" in g.agents]
        return [rule for group in chosen for rule in group.rules]

    def allowed(self, token: str, url: str) -> bool:
        parts = urlsplit(url)
        target = _normalize(parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        best: tuple[int, bool] | None = None
        for allow, pattern in self._rules_for(token):
            if _matches(pattern, target):
                candidate = (len(pattern), allow)
                if best is None or candidate[0] > best[0] or (
                        candidate[0] == best[0] and allow):
                    best = candidate
        return True if best is None else best[1]


def _normalize(path: str) -> str:
    return quote(unquote(path), safe="/*$?=&;:@!,'()+~%-._")


def _matches(pattern: str, target: str) -> bool:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.match(regex + ("$" if anchored else ""), target) is not None
