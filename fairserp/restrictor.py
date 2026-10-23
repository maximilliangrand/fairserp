"""SerpApi's ``json_restrictor`` field selection.

Supported syntax: ``a.b`` (field), ``a[0]`` (index), ``a[]`` (every item),
``a[1:3]`` (half-open slice), ``a.{b, c[].d}`` (subquery), and comma separated paths.
"""

import re
from typing import Any

_INDEX = re.compile(r"\[(\d*)(?::(\d*))?\]")


class RestrictorError(ValueError):
    pass


MAX_LENGTH = 1000
MAX_DEPTH = 8


def apply(data: Any, expression: str) -> Any:
    if len(expression) > MAX_LENGTH:
        raise RestrictorError("json_restrictor is too long")
    if _max_depth(expression) > MAX_DEPTH:
        raise RestrictorError("json_restrictor is nested too deeply")
    return _apply(data, expression)


def _max_depth(expression: str) -> int:
    depth = deepest = 0
    for char in expression:
        depth += {"{": 1, "}": -1}.get(char, 0)
        deepest = max(deepest, depth)
    return deepest


def _apply(data: Any, expression: str) -> Any:
    result: dict[str, Any] = {}
    for path in _split_top_level(expression):
        _merge(result, _select(data, path.strip()))
    return result


def _split_top_level(expression: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for char in expression:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                raise RestrictorError("Unbalanced braces in json_restrictor")
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    if depth != 0:
        raise RestrictorError("Unbalanced braces in json_restrictor")
    parts.append(current)
    return [p for p in (part.strip() for part in parts) if p]


def _select(data: Any, path: str) -> Any:
    """Return a structure shaped like ``data`` but containing only ``path``."""
    if not path:
        return data
    if path.startswith("{") and path.endswith("}"):
        return _apply(data, path[1:-1])
    match = re.match(r"([A-Za-z0-9_]+)((?:\[[0-9:]*\])*)\.?(.*)", path)
    if not match:
        raise RestrictorError(f"Invalid json_restrictor path: {path}")
    key, brackets, rest = match.groups()
    if not isinstance(data, dict) or key not in data:
        return {}
    return {key: _apply_brackets(data[key], brackets, rest)}


def _apply_brackets(value: Any, brackets: str, rest: str) -> Any:
    if not brackets:
        return _select(value, rest)
    first = _INDEX.match(brackets)
    if first is None or not isinstance(value, list):
        return value if not isinstance(value, list) else []
    remaining = brackets[first.end():]
    start, stop = first.group(1), first.group(2)
    if ":" not in first.group(0) and start:
        index = int(start)
        if index >= len(value):
            return []
        return [_apply_brackets(value[index], remaining, rest)]
    lo = int(start) if start else 0
    hi = int(stop) if stop else len(value)
    return [_apply_brackets(item, remaining, rest) for item in value[lo:hi]]


def _merge(target: dict[str, Any], addition: Any) -> None:
    if not isinstance(addition, dict):
        return
    for key, value in addition.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            _merge(target[key], value)
        elif key in target and isinstance(target[key], list) and isinstance(value, list):
            for index, item in enumerate(value):
                if index < len(target[key]) and isinstance(item, dict):
                    _merge(target[key][index], item)
                elif index >= len(target[key]):
                    target[key].append(item)
        else:
            target[key] = value
