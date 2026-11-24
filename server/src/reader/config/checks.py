"""Checks and frozen types shared by the loader (validate.py) and the Pydantic models (model.py).

One definition per rule, so `ReaderConfig.model_validate` rejects what `load_config` rejects (FR-CONFIG-1).
"""

import math
import re
from collections.abc import Hashable, Mapping, Sequence
from decimal import Decimal
from typing import Any, Final, NoReturn
from urllib.parse import urlsplit

SHA256: Final = re.compile(r"[0-9a-f]{64}")
DECIMAL_EXPONENT: Final = 999999  # default decimal context Emax/-Emin (FR-CONFIG-10)


def forbidden(c: str) -> bool:
    """Whitespace or a control character (FR-CONFIG-5); the one definition, used by refs.py too."""
    return c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F


UPLOAD_ID_PREFIX: Final = "up-"
MATERIAL_ID_MAX: Final = 64


def material_id_problem(value: str) -> str | None:
    """FR-CONFIG-18. `reserved_id` for an id starting with `up-` (case-sensitive, as written): SRV-INGEST
    names uploaded materials `up-<12 hex>`. `bad_material_id` for an id longer than 64 code points or with a
    character that is white space, not printable (`str.isprintable`) or `/`: the id is a path segment of
    SRV-INGEST's routes and SRV-EVENTS' audit `object_id`, which applies this same rule. The empty id is the
    caller's `empty_string`."""
    if value.startswith(UPLOAD_ID_PREFIX):
        return "reserved_id"
    if len(value) > MATERIAL_ID_MAX or any(c.isspace() or not c.isprintable() or c == "/" for c in value):
        return "bad_material_id"
    return None


def url_problem(value: str) -> str | None:
    """`bad_url` unless an absolute https URL with a host and a valid port, free of whitespace and `\\`.

    FR-CONFIG-3.
    """
    if "\\" in value or any(forbidden(c) for c in value):
        return "bad_url"
    try:
        parts = urlsplit(value)
        host = parts.hostname
        parts.port  # noqa: B018 - reading it validates it (non-numeric or out of range raises ValueError)
    except ValueError:
        return "bad_url"
    return None if parts.scheme == "https" and host else "bad_url"


def decimal_in_context(number: Decimal) -> bool:
    return number.is_finite() and -DECIMAL_EXPONENT <= number.adjusted() <= DECIMAL_EXPONENT


class FrozenDict[K, V](dict[K, V]):
    """A dict that cannot change. Pickles, copies and dumps as a plain mapping (FR-CONFIG-1)."""

    __slots__ = ()

    def _refuse(self, *args: object, **kwargs: object) -> NoReturn:
        raise TypeError("this mapping is immutable")

    __setitem__ = _refuse
    __delitem__ = _refuse
    __ior__ = _refuse
    clear = _refuse
    pop = _refuse
    popitem = _refuse
    setdefault = _refuse
    update = _refuse

    def __reduce__(self) -> tuple[type[FrozenDict[K, V]], tuple[dict[K, V]]]:
        return (type(self), (dict(self),))


def freeze_opaque(value: object) -> Any:
    """An immutable copy of a tree of core-schema values; refuses non-finite floats and non-string keys."""
    match value:
        case Mapping():
            if not all(isinstance(k, str) for k in value):
                raise ValueError("opaque mapping keys must be strings")
            return FrozenDict({k: freeze_opaque(v) for k, v in value.items()})
        case list() | tuple():
            return tuple(freeze_opaque(v) for v in value)
        case float() if not math.isfinite(value):
            raise ValueError("number must be finite")
        case None | bool() | int() | float() | str():
            return value
    raise ValueError("opaque values must be core-schema values")


def unique(values: Sequence[Hashable]) -> bool:
    return len(set(values)) == len(values)
