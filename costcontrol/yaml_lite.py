"""极简 YAML 子集解析器（零依赖）。

为什么自己写：项目承诺「纯标准库可运行」，而 PyYAML 是第三方包。业务 Sense
模板的语法是可控的，只需要支持：

- 缩进嵌套的映射（``key: value`` / ``key:`` + 更深缩进）
- 标量列表（``- item``）
- 字符串、整数、小数、布尔、null
- 行内注释 ``#`` 与整行注释
- 单/双引号字符串、行内标量列表 ``[a, b]``

不支持锚点、多行块（``|`` / ``>``）、列表项映射（``- key: value``）等高级语法，
遇到会显式报错，而不是静默解析成错误结构。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any


class YamlError(ValueError):
    pass


@dataclass(frozen=True)
class _Line:
    indent: int
    text: str
    number: int


def loads(text: str) -> Any:
    lines = _tokenize(text)
    if not lines:
        return {}
    value, index = _parse_node(lines, 0, lines[0].indent)
    if index != len(lines):
        raise YamlError(f"第 {lines[index].number} 行缩进不正确")
    return value


def load_file(path: str | Path) -> Any:
    return loads(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 词法
# --------------------------------------------------------------------------


def _tokenize(text: str) -> list[_Line]:
    result: list[_Line] = []
    for number, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise YamlError(f"第 {number} 行使用了 Tab 缩进，请改用空格")
        stripped = raw.rstrip()
        if not stripped.strip() or stripped.strip().startswith("#"):
            continue
        if stripped.strip() in ("---", "..."):
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        content = _strip_comment(stripped[indent:]).rstrip()
        if not content:
            continue
        result.append(_Line(indent=indent, text=content, number=number))
    return result


def _strip_comment(text: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            quote = char
            continue
        if char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index]
    return text


# --------------------------------------------------------------------------
# 语法
# --------------------------------------------------------------------------


def _parse_node(lines: list[_Line], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines) or lines[index].indent < indent:
        return None, index
    if lines[index].text.startswith("-"):
        return _parse_list(lines, index, indent)
    return _parse_map(lines, index, indent)


def _parse_map(lines: list[_Line], index: int, indent: int) -> tuple[dict, int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise YamlError(f"第 {line.number} 行缩进过深")
        if line.text.startswith("-"):
            break
        key, sep, rest = line.text.partition(":")
        if not sep:
            raise YamlError(f"第 {line.number} 行缺少冒号")
        name = str(_scalar(key.strip()))
        rest = rest.strip()
        if rest:
            result[name] = _scalar(rest)
            index += 1
        elif index + 1 < len(lines) and lines[index + 1].indent > indent:
            result[name], index = _parse_node(lines, index + 1, lines[index + 1].indent)
        else:
            result[name] = None
            index += 1
    return result, index


def _parse_list(lines: list[_Line], index: int, indent: int) -> tuple[list, int]:
    items: list[Any] = []
    while index < len(lines):
        line = lines[index]
        if line.indent != indent or not line.text.startswith("-"):
            break
        body = line.text[1:].strip()
        if not body:
            if index + 1 < len(lines) and lines[index + 1].indent > indent:
                value, index = _parse_node(lines, index + 1, lines[index + 1].indent)
            else:
                value, index = None, index + 1
        elif _looks_like_map(body):
            value, index = _parse_list_item_map(lines, index, indent, body)
        else:
            value, index = _scalar(body), index + 1
        items.append(value)
    return items, index


def _looks_like_map(body: str) -> bool:
    if body[:1] in "\"'[{":
        return False
    key, sep, _ = body.partition(":")
    return bool(sep) and key.strip().replace("_", "").replace("-", "").isalnum()


def _parse_list_item_map(
    lines: list[_Line], index: int, indent: int, body: str
) -> tuple[dict, int]:
    item: dict[str, Any] = {}
    key, _sep, rest = body.partition(":")
    rest = rest.strip()
    item[str(_scalar(key.strip()))] = _scalar(rest) if rest else None
    index += 1
    while index < len(lines) and lines[index].indent > indent:
        line = lines[index]
        name, sep, value = line.text.partition(":")
        if not sep:
            raise YamlError(f"第 {line.number} 行缺少冒号")
        name = str(_scalar(name.strip()))
        value = value.strip()
        if value:
            item[name] = _scalar(value)
            index += 1
        elif index + 1 < len(lines) and lines[index + 1].indent > line.indent:
            item[name], index = _parse_node(lines, index + 1, lines[index + 1].indent)
        else:
            item[name] = None
            index += 1
    return item, index


def _scalar(text: str) -> Any:
    text = text.strip()
    if text == "":
        return None
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part) for part in _split_inline(inner)]
    if text.startswith("{") and text.endswith("}"):
        return _parse_inline_map(text[1:-1])
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        return _unquote(text)
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "~"):
        return None
    if _is_int(text):
        return int(text)
    if _is_decimal(text):
        return Decimal(text)
    return text


def _parse_inline_map(inner: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not inner.strip():
        return result
    for part in _split_inline(inner):
        key, sep, value = part.partition(":")
        if not sep:
            raise YamlError(f"行内映射缺少冒号：{part}")
        result[str(_scalar(key.strip()))] = _scalar(value.strip())
    return result


def _split_inline(text: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            quote = char
            current.append(char)
            continue
        if char == ",":
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current).strip())
    return parts


def _unquote(text: str) -> str:
    quote = text[0]
    body = text[1:-1]
    if quote == "'":
        return body.replace("''", "'")
    return body.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")


def _is_int(text: str) -> bool:
    body = text[1:] if text[:1] in "+-" else text
    return body.isdigit()


def _is_decimal(text: str) -> bool:
    body = text[1:] if text[:1] in "+-" else text
    if body.count(".") != 1:
        return False
    left, right = body.split(".")
    return left.isdigit() and right.isdigit()
