"""Validation of the positioned YAML tree against the field table (FR-CONFIG-1, 2, 3, 5, 9, 10, 11).

Rules are data: each rule turns a node into a plain value or records problems (line, key path, code).
Keys that come from the input are named by position, never by text (FR-CONFIG-11).
"""

import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Final
from urllib.parse import urlsplit

from reader.config.refs import SecretRef
from reader.config.yaml_tree import Map, Node, Scalar, Seq


class _Invalid:
    pass


INVALID: Final = _Invalid()
Value = object


@dataclass
class Context:
    base_dir: str
    problems: list[tuple[int, str, str]] = field(default_factory=list)

    def fail(self, line: int, path: str, code: str) -> _Invalid:
        self.problems.append((line, path, code))
        return INVALID


Rule = Callable[[Node, str, Context], Value]


def _join(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name


def _string(node: Node, path: str, ctx: Context) -> str | _Invalid:
    if not (isinstance(node, Scalar) and node.kind == "str"):
        return ctx.fail(node.line, path, "wrong_type")
    return str(node.value)


def text(check: Callable[[str], str | None] | None = None) -> Rule:
    """A non-empty string, optionally checked further (`check` returns a code or None)."""

    def rule(node: Node, path: str, ctx: Context) -> Value:
        value = _string(node, path, ctx)
        if isinstance(value, _Invalid):
            return value
        if not value:
            return ctx.fail(node.line, path, "empty_string")
        code = check(value) if check else None
        return ctx.fail(node.line, path, code) if code else value

    return rule


def _https(value: str) -> str | None:
    parts = urlsplit(value)
    return None if parts.scheme == "https" and parts.netloc else "bad_url"


def path_value(node: Node, path: str, ctx: Context) -> Value:
    """Lexical join to the reader.yaml directory and normalisation; no filesystem access (FR-CONFIG-9)."""
    value = text()(node, path, ctx)
    if isinstance(value, _Invalid):
        return value
    assert isinstance(value, str)  # noqa: S101
    return os.path.normpath(value if os.path.isabs(value) else os.path.join(ctx.base_dir, value))


def secret_ref(node: Node, path: str, ctx: Context) -> Value:
    value = _string(node, path, ctx)
    if isinstance(value, _Invalid):
        return value
    try:
        return SecretRef.parse(value)
    except ValueError:
        return ctx.fail(node.line, path, "not_secret_ref")


_SHA256: Final = re.compile(r"[0-9a-f]{64}")


def sha256(node: Node, path: str, ctx: Context) -> Value:
    value = _string(node, path, ctx)
    if isinstance(value, _Invalid):
        return value
    return value if _SHA256.fullmatch(value) else ctx.fail(node.line, path, "bad_sha256")


def decimal(upper: Decimal | None = None) -> Rule:
    """A YAML int or float only (no quoted strings, no booleans), finite, > 0 and <= upper (FR-CONFIG-10)."""

    def rule(node: Node, path: str, ctx: Context) -> Value:
        if not (isinstance(node, Scalar) and node.kind in ("int", "float")):
            return ctx.fail(node.line, path, "wrong_type")
        if node.kind == "float" and not math.isfinite(float(node.value)):  # type: ignore[arg-type]
            return ctx.fail(node.line, path, "not_finite")
        number = Decimal(int(node.value)) if node.kind == "int" else Decimal(node.text)  # type: ignore[call-overload]
        if number <= 0 or (upper is not None and number > upper):
            return ctx.fail(node.line, path, "out_of_range")
        return number

    return rule


def version(node: Node, path: str, ctx: Context) -> Value:
    if not (isinstance(node, Scalar) and node.kind == "int"):
        return ctx.fail(node.line, path, "wrong_type")
    return node.value if node.value == 1 else ctx.fail(node.line, path, "bad_version")


@dataclass(frozen=True)
class Field:
    rule: Rule
    required: bool = True


def record(fields: Mapping[str, Field]) -> Rule:
    def rule(node: Node, path: str, ctx: Context) -> Value:
        if not isinstance(node, Map):
            return ctx.fail(node.line, path, "wrong_type")
        values: dict[str, Value] = {}
        for position, entry in enumerate(node.entries, start=1):
            spec = fields.get(entry.key)
            if spec is None:
                ctx.fail(entry.key_line, _join(path, f"<unknown key #{position}>"), "unknown_key")
                continue
            values[entry.key] = spec.rule(entry.value, _join(path, entry.key), ctx)
        for name, spec in fields.items():
            if spec.required and name not in values:
                ctx.fail(node.line, _join(path, name), "missing_key")
        return values

    return rule


def _repeats(path: str, keys: Sequence[tuple[int, str, object]], ctx: Context) -> None:
    """Every occurrence after the first is one not_unique at its own item path and line (FR-CONFIG-10)."""
    seen: set[object] = set()
    for line, item_path, key in keys:
        if key in seen:
            ctx.fail(line, item_path, "not_unique")
        seen.add(key)


def items(item: Rule, unique_by: str | None = None, unique: bool = False) -> Rule:
    def rule(node: Node, path: str, ctx: Context) -> Value:
        if not isinstance(node, Seq):
            return ctx.fail(node.line, path, "wrong_type")
        values = tuple(item(child, _join(path, str(i)), ctx) for i, child in enumerate(node.items))
        if unique:
            _repeats(path, [(c.line, _join(path, str(i)), c.value) for i, c in enumerate(node.items)
                            if isinstance(c, Scalar) and c.kind == "str"], ctx)
        if unique_by:
            _repeats(path, [(e.value.line, _join(path, f"{i}.{unique_by}"), e.value.value)
                            for i, c in enumerate(node.items) if isinstance(c, Map)
                            for e in c.entries if e.key == unique_by and isinstance(e.value, Scalar)], ctx)
        return values

    return rule


def open_map(value_rule: Rule) -> Rule:
    """Open keys (task names): non-empty strings, named by position in errors."""

    def rule(node: Node, path: str, ctx: Context) -> Value:
        if not isinstance(node, Map):
            return ctx.fail(node.line, path, "wrong_type")
        values: dict[str, Value] = {}
        for position, entry in enumerate(node.entries, start=1):
            entry_path = _join(path, f"<key #{position}>")
            if not entry.key:
                ctx.fail(entry.key_line, entry_path, "empty_key")
            values[entry.key] = value_rule(entry.value, entry_path, ctx)
        return values

    return rule


def _freeze_tree(node: Node, path: str, ctx: Context) -> Value:
    match node:
        case Map(entries=entries):
            return MappingProxyType({e.key: _freeze_tree(e.value, _join(path, f"<key #{i}>"), ctx)
                                     for i, e in enumerate(entries, start=1)})
        case Seq(items=children):
            return tuple(_freeze_tree(c, _join(path, str(i)), ctx) for i, c in enumerate(children))
        case Scalar(kind="float", value=value) if not math.isfinite(float(value)):  # type: ignore[arg-type]
            return ctx.fail(node.line, path, "not_finite")
        case Scalar(value=value):
            return value


def opaque(node: Node, path: str, ctx: Context) -> Value:
    """`sectors` and `sharing`: any mapping of core-schema values, kept immutable (FR-CONFIG-1)."""
    if not isinstance(node, Map):
        return ctx.fail(node.line, path, "wrong_type")
    return _freeze_tree(node, path, ctx)


ONE: Final = Decimal(1)

ROOT: Final = record({
    "version": Field(version),
    "instance": Field(record({"url": Field(text(_https)), "data_dir": Field(path_value)})),
    "users": Field(items(record({
        "handle": Field(text()),
        "role": Field(text()),
        "providers": Field(
            items(record({"kind": Field(text()), "token": Field(secret_ref)})), required=False
        ),
    }), unique_by="handle"), required=False),
    "tasks": Field(open_map(record({
        "model": Field(text()),
        "budget_usd_per_lesson": Field(decimal(), required=False),
    })), required=False),
    "budgets": Field(record({
        "monthly_usd": Field(decimal()),
        "warn_at": Field(decimal(ONE), required=False),
    }), required=False),
    "kernels": Field(items(text(), unique=True), required=False),
    "materials": Field(items(record({
        "id": Field(text()),
        "file": Field(path_value),
        "sha256": Field(sha256),
        "license": Field(text()),
        "language": Field(text()),
    }), unique_by="id"), required=False),
    "sectors": Field(opaque, required=False),
    "sharing": Field(opaque, required=False),
})
