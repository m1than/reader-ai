"""Secret values, resolvers and registries (FR-CONFIG-6, 7, 8, 12, 13, 16).

A resolved value lives only inside `Secret`. Readers return a `Secret` or a failure code; the exception is
raised in a frame that never held the value, so tracebacks with locals cannot show it (INV-CONFIG-2).
"""

import errno
import logging
import os
import stat
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final, NoReturn

from reader.config.refs import SCHEMES, SecretRef, secret_file_name

SECRET_MAX_BYTES: Final = 65536  # structural bound for a bounded read (FR-CONFIG-13), not a measured limit
DEFAULT_ROOT: Final = Path("/run/secrets")
_MASK: Final = "Secret(********)"
_log = logging.getLogger(__name__)


class Secret:
    """A resolved secret. Only `reveal()` returns the plaintext."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return _MASK

    def __str__(self) -> str:
        return _MASK

    def __format__(self, spec: str) -> str:
        return _MASK

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        raise TypeError("a Secret cannot be serialised")

    def __getstate__(self) -> NoReturn:
        raise TypeError("a Secret cannot be serialised")

    def __copy__(self) -> NoReturn:
        raise TypeError("a Secret cannot be copied")

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise TypeError("a Secret cannot be copied")


class SecretResolutionError(Exception):
    """Carries the reference and a fixed code; never the value or the underlying error text (FR-CONFIG-8)."""

    def __init__(self, ref: SecretRef, code: str) -> None:
        self.ref = ref
        self.code = code
        super().__init__(f"cannot resolve {ref}: {code}")


Resolver = Callable[[SecretRef], Secret]


class ResolverRegistry:
    """Immutable map from scheme to resolver, built once from (scheme, resolver) pairs (FR-CONFIG-7)."""

    __slots__ = ("_resolvers",)
    _resolvers: Mapping[str, Resolver]

    def __init__(self, pairs: Iterable[tuple[str, Resolver]]) -> None:
        resolvers: dict[str, Resolver] = {}
        for scheme, resolver in pairs:
            if scheme not in SCHEMES:
                raise ValueError(f"unknown secret scheme: {scheme}")
            if scheme in resolvers:
                raise ValueError(f"scheme registered twice: {scheme}")
            resolvers[scheme] = resolver
        object.__setattr__(self, "_resolvers", MappingProxyType(resolvers))

    def __setattr__(self, name: str, value: object) -> NoReturn:
        raise AttributeError("ResolverRegistry is immutable")

    def __delattr__(self, name: str) -> NoReturn:
        raise AttributeError("ResolverRegistry is immutable")

    def get(self, scheme: str) -> Resolver | None:
        return self._resolvers.get(scheme)

    def __repr__(self) -> str:
        return f"ResolverRegistry({sorted(self._resolvers)})"


def _fail(ref: SecretRef, code: str) -> NoReturn:
    raise SecretResolutionError(ref, code) from None


def _read_under(root: Path, name: str) -> Secret | str:
    """The secret at root/name, or a failure code. Bytes and text never leave this frame unwrapped."""
    root_real = os.path.realpath(root)
    target = os.path.join(root, name)
    if not os.path.lexists(target):
        return "not_found"
    real = os.path.realpath(target)
    if os.path.commonpath([root_real, real]) != root_real:
        return "outside_root"
    try:  # no symlink at open time, no blocking on a FIFO; every later check is on this descriptor
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return "not_found"
    except OSError as error:
        return "outside_root" if error.errno == errno.ELOOP else "unreadable"
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return "not_regular_file"
    with os.fdopen(fd, "rb") as handle:
        try:
            data = handle.read(SECRET_MAX_BYTES + 1)
        except OSError:
            return "unreadable"
    if len(data) > SECRET_MAX_BYTES:
        return "too_large"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "not_utf8"
    text = text.removesuffix("\n")
    return Secret(text) if text else "empty"


def _from_file(root: Path, ref: SecretRef, name: str) -> Secret:
    outcome = _read_under(root, name)
    if isinstance(outcome, Secret):
        return outcome
    _fail(ref, outcome)


def env_resolver(ref: SecretRef) -> Secret:
    """The calling process's environment; for the host CLI and tests, not for containers (FR-CONFIG-6)."""
    present = ref.name in os.environ
    if not present:
        _fail(ref, "env_unset")
    if not os.environ[ref.name]:
        _fail(ref, "empty")
    return Secret(os.environ[ref.name])


def file_resolver(root: Path = DEFAULT_ROOT) -> Resolver:
    return lambda ref: _from_file(root, ref, ref.name)


def host_resolved_resolver(root: Path = DEFAULT_ROOT) -> Resolver:
    """Inside a container: env/hostfile/keychain/op values that the host CLI passed as Compose secrets."""
    return lambda ref: _from_file(root, ref, secret_file_name(ref))


def container_registry(root: Path | str = DEFAULT_ROOT) -> ResolverRegistry:
    base = Path(root)
    host_resolved = host_resolved_resolver(base)
    return ResolverRegistry([
        ("file", file_resolver(base)),
        *((scheme, host_resolved) for scheme in ("env", "hostfile", "keychain", "op")),
    ])


def host_registry(extra: Iterable[tuple[str, Resolver]] = ()) -> ResolverRegistry:
    return ResolverRegistry([("env", env_resolver), *extra])


def resolve(ref: SecretRef | str, registry: ResolverRegistry) -> Secret:
    parsed = ref if isinstance(ref, SecretRef) else SecretRef.parse(ref)
    resolver = registry.get(parsed.scheme)
    if resolver is None:
        _log.warning("cannot resolve %s: %s", parsed, "no_resolver")
        _fail(parsed, "no_resolver")
    try:
        secret = resolver(parsed)
    except SecretResolutionError as error:
        _log.warning("cannot resolve %s: %s", parsed, error.code)
        raise
    _log.debug("resolved %s", parsed)
    return secret
