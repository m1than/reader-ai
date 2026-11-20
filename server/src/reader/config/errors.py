"""Config errors: fixed codes and messages, never input scalars (FR-CONFIG-3, FR-CONFIG-11, FR-CONFIG-15)."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

MESSAGES: Final[Mapping[str, str]] = MappingProxyType({
    # validation (FR-CONFIG-3)
    "unknown_key": "key is not allowed here",
    "missing_key": "required key is missing",
    "wrong_type": "value has the wrong type",
    "bad_version": "unsupported version; the supported version is 1",
    "not_secret_ref": "must be a secret:// reference (env, file, hostfile, keychain, op)",
    "bad_sha256": "must be 64 lowercase hex characters",
    "bad_url": "must be an absolute https URL",
    "empty_string": "must not be empty",
    "empty_key": "key must not be empty",
    "out_of_range": "value is out of range",
    "not_finite": "number must be finite",
    "not_unique": "value repeats an earlier item",
    # load (FR-CONFIG-15)
    "file_unreadable": "file cannot be read",
    "not_utf8": "file is not valid UTF-8",
    "yaml_syntax": "YAML syntax error",
    "empty_document": "document is empty",
    "multiple_documents": "only one YAML document is allowed",
    "not_mapping": "top level must be a mapping",
    "duplicate_key": "key appears twice in the same mapping",
    "non_string_key": "mapping keys must be strings",
    "alias_not_allowed": "anchors, aliases and merge keys are not allowed",
    "too_deep": "collections are nested too deeply",
    "tag_not_allowed": "explicit tags are not allowed",
})


@dataclass(frozen=True, slots=True)
class Problem:
    file: str
    line: int | None
    path: str
    code: str

    def render(self) -> str:
        location = self.file if self.line is None else f"{self.file}:{self.line}"
        return ": ".join([location, *([self.path] if self.path else []), self.code, MESSAGES[self.code]])


class ConfigError(Exception):
    """All problems of one load, ordered by line, then key path. Carries no input scalar."""

    def __init__(self, problems: Iterable[Problem]) -> None:
        self.problems: tuple[Problem, ...] = tuple(sorted(problems, key=lambda p: (p.line or 0, p.path)))
        super().__init__("\n".join(p.render() for p in self.problems))
