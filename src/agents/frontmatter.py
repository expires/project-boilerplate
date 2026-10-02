from __future__ import annotations

import re
from typing import Any

_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)
_INT = re.compile(r"-?\d+")
_FLOAT = re.compile(r"-?\d+\.\d+")


def _clean(text: Any) -> str:
    return str(text).replace("\r", " ").replace("\n", " ")


def format_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return "'" + _clean(value).replace("'", "''") + "'"


def format_value(value: Any) -> str:
    if isinstance(value, dict):
        inner = ", ".join(f"{key}: {format_value(item)}" for key, item in value.items())
        return "{" + inner + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(format_value(item) for item in value) + "]"
    return format_scalar(value)


def dumps(data: dict[str, Any]) -> str:
    return "\n".join(f"{key}: {format_value(value)}" for key, value in data.items())


def _split_top(text: str, separator: str) -> list[str]:
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            buffer.append(char)
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    buffer.append(text[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
            buffer.append(char)
        elif char in "[{(":
            depth += 1
            buffer.append(char)
        elif char in "]})":
            depth -= 1
            buffer.append(char)
        elif char == separator and depth == 0:
            parts.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
        index += 1
    parts.append("".join(buffer))
    return parts


def _unquote(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == "'" and token[-1] == "'":
        return token[1:-1].replace("''", "'")
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        return token[1:-1].replace('\\"', '"')
    return token


def parse_scalar(token: str) -> Any:
    token = token.strip()
    if token == "":
        return ""
    if token[0] in "'\"":
        return _unquote(token)
    if token in ("null", "~", "None"):
        return None
    if token.lower() == "true":
        return True
    if token.lower() == "false":
        return False
    if _INT.fullmatch(token):
        return int(token)
    if _FLOAT.fullmatch(token):
        return float(token)
    return token


def parse_value(token: str) -> Any:
    token = token.strip()
    if token.startswith("[") and token.endswith("]"):
        inner = token[1:-1].strip()
        if not inner:
            return []
        return [parse_value(part) for part in _split_top(inner, ",")]
    if token.startswith("{") and token.endswith("}"):
        inner = token[1:-1].strip()
        result: dict[str, Any] = {}
        if inner:
            for pair in _split_top(inner, ","):
                key, _, value = pair.partition(":")
                result[key.strip()] = parse_value(value)
        return result
    return parse_scalar(token)


def parse(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text.strip()
    data: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, rest = line.partition(":")
        if not separator:
            continue
        data[key.strip()] = parse_value(rest)
    return data, match.group(2).strip()


def loads(text: str) -> dict[str, Any]:
    return parse(text)[0]
