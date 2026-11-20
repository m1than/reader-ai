"""`secret://` references: grammar (FR-CONFIG-5) and Compose secret names (FR-CONFIG-14)."""

import hashlib
import re
from typing import Any, Final, Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

SCHEMES: Final = ("env", "file", "hostfile", "keychain", "op")
_PREFIX: Final = re.compile(r"secret://(env|file|hostfile|keychain|op)/(.*)", re.DOTALL)
_ENV_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FILE_NAME: Final = re.compile(r"[A-Za-z0-9._-]+")
_SEGMENTS: Final = {"keychain": 2, "op": 3}  # exact segment counts; hostfile takes any count >= 1


def _forbidden(c: str) -> bool:
    return c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F


def _segment(part: str) -> bool:
    return part not in ("", ".", "..") and "%" not in part


def _valid(text: str) -> bool:
    match = _PREFIX.fullmatch(text)
    if match is None or any(_forbidden(c) for c in text):
        return False
    scheme, rest = match.groups()
    parts = rest.split("/")
    match scheme:
        case "env":
            return len(parts) == 1 and _ENV_NAME.fullmatch(parts[0]) is not None
        case "file":
            return (len(parts) == 3 and parts[:2] == ["run", "secrets"]
                    and _FILE_NAME.fullmatch(parts[2]) is not None and parts[2] not in (".", ".."))
        case "hostfile":
            return all(map(_segment, parts))
        case _:
            return len(parts) == _SEGMENTS[scheme] and all(map(_segment, parts))


class SecretRef(str):
    """A validated `secret://` reference. The reference names a secret; it is not the secret."""

    __slots__ = ()

    @classmethod
    def parse(cls, text: object) -> Self:
        if not isinstance(text, str) or not _valid(text):
            raise ValueError("not a secret:// reference (FR-CONFIG-5)")
        return cls(text)

    @property
    def scheme(self) -> str:
        return self.split("/", 3)[2]

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(self.split("/")[3:])

    @property
    def name(self) -> str:
        """The environment variable name (env) or the file name under /run/secrets (file)."""
        return self.parts[-1]

    @property
    def path(self) -> str:
        """The absolute path a `file` or `hostfile` reference means."""
        if self.scheme not in ("file", "hostfile"):
            raise ValueError("only file and hostfile references name a path")
        return "/" + "/".join(self.parts)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(cls.parse, core_schema.str_schema())


def _digest(ref: SecretRef) -> tuple[str, str]:
    if ref.scheme == "file":
        raise ValueError("a file reference already names its file under /run/secrets")
    return ref.scheme, hashlib.sha256(ref.encode("utf-8")).hexdigest()[:32]


def secret_file_name(ref: SecretRef) -> str:
    """Compose secret name, mounted at /run/secrets/<name> (FR-CONFIG-14); shared with OPS-CLI."""
    scheme, digest = _digest(ref)
    return f"{scheme}-{digest}"


def secret_env_var(ref: SecretRef) -> str:
    """Environment variable that carries the value into `docker compose` (FR-CONFIG-14)."""
    scheme, digest = _digest(ref)
    return f"READER_SECRET_{scheme.upper()}_{digest.upper()}"
