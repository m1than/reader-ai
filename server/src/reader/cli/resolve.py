"""Host resolvers for keychain, op and hostfile, and the host-side value checks (FR-CLI-2)."""

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Final, NoReturn, Protocol

import keyring.errors

from reader.cli.runner import CommandFailed, CommandTimeout, Runner
from reader.cli.stack import Plan
from reader.config import (
    SECRET_MAX_BYTES,
    Resolver,
    ResolverRegistry,
    Secret,
    SecretRef,
    SecretResolutionError,
    host_registry,
    resolve,
)
from reader.config.refs import SCHEMES

SYSTEM_SERVICE: Final = "reader-ai-system"
USER_SERVICE: Final = "reader-ai"
OP_TIMEOUT: Final = (
    120  # s: room for the 1Password unlock prompt; a hung `op` cannot stall `reader up` (FR-CLI-2)
)


class Keyring(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...


def macos_keyring() -> Keyring:
    """The macOS Keychain backend, always: the configured keyring backend is never consulted, so a file or
    null backend cannot be selected (FR-CLI-2)."""
    from keyring.backends.macOS import Keyring as MacKeyring

    return MacKeyring()  # type: ignore[no-untyped-call]  # keyring's backend constructor is unannotated


def _fail(ref: SecretRef, code: str) -> NoReturn:
    raise SecretResolutionError(ref, code) from None


def strip_one_newline(data: bytes) -> bytes:
    return data[:-1] if data.endswith(b"\n") else data


def check_value(ref: SecretRef, value: bytes | str) -> Secret:
    """At most SECRET_MAX_BYTES, valid UTF-8, non-empty. Failures carry only the code."""
    data: bytes | None = None
    if isinstance(value, str):
        try:
            data = value.encode("utf-8")
        except UnicodeEncodeError:
            data = None
    else:
        data = value
    if data is None:
        _fail(ref, "not_utf8")
    if len(data) > SECRET_MAX_BYTES:
        _fail(ref, "too_large")
    text: str | None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is None:
        _fail(ref, "not_utf8")
    if not text:
        _fail(ref, "empty")
    return Secret(text)


def keychain_resolver(store: Keyring) -> Resolver:
    def read(ref: SecretRef) -> Secret:
        service, account = ref.parts
        code: str | None = None
        value: str | None = None
        try:
            value = store.get_password(service, account)
        except keyring.errors.KeyringError:
            # Locked, denied and a cancelled prompt are not told apart by the macOS backend: one code.
            code = "keychain_unavailable"
        if code is not None:
            _fail(ref, code)
        if value is None:
            _fail(ref, "not_found")
        return check_value(ref, value)

    return read


def op_resolver(runner: Runner, env: Mapping[str, str]) -> Resolver:
    def read(ref: SecretRef) -> Secret:
        code: str | None = None
        result = None
        try:
            result = runner.run(["op", "read", "op://" + "/".join(ref.parts)], env=env, timeout=OP_TIMEOUT)
        except CommandFailed:
            code = "tool_missing"
        except CommandTimeout:
            code = "tool_failed"
        if code is not None or result is None:
            _fail(ref, code or "tool_failed")
        if result.code != 0:
            _fail(ref, "tool_failed")
        return check_value(ref, strip_one_newline(result.out))

    return read


def in_git_work_tree(path: Path) -> bool:
    """True when the path or any ancestor directory contains `.git`."""
    start = Path(os.path.realpath(path))
    return any((directory / ".git").exists() for directory in (start, *start.parents))


def hostfile_resolver(ref: SecretRef) -> Secret:
    path = Path(ref.path)
    if in_git_work_tree(path.parent):
        _fail(ref, "outside_root")
    code: str | None = None
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        code = "not_found"
    except PermissionError:
        code = "unreadable"
    except OSError:
        code = "not_regular_file"  # ELOOP: the last component is a symlink
    if code is not None:
        _fail(ref, code)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            code = "not_regular_file"
        elif stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
            code = "outside_root"
        data = b"" if code else os.read(fd, SECRET_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if code is not None:
        _fail(ref, code)
    return check_value(ref, data)


def host_resolvers(store: Keyring, runner: Runner, env: Mapping[str, str]) -> ResolverRegistry:
    """SRV-CONFIG's host registry plus keychain, op and hostfile; every value goes through check_value."""
    base = host_registry(
        [
            ("keychain", keychain_resolver(store)),
            ("op", op_resolver(runner, env)),
            ("hostfile", hostfile_resolver),
        ]
    )

    def checked(inner: Resolver) -> Resolver:
        return lambda ref: check_value(ref, inner(ref).reveal())

    return ResolverRegistry((s, checked(r)) for s in SCHEMES if (r := base.get(s)) is not None)


def resolve_values(plan: Plan, registry: ResolverRegistry, db_password: Secret) -> dict[str, Secret]:
    return {
        name: db_password if ref is None else resolve(ref, registry) for name, ref in plan.sources.items()
    }
