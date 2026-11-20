"""Host resolvers (keychain, op, hostfile) and the host-side value checks.

@trace FR-CLI-2, FR-CLI-11
"""

import os
import traceback
from pathlib import Path
from typing import Any

import keyring.errors
import pytest

from .conftest import ENV_ALLOW, FakeKeyring, FakeRunner

CANARY = "CANARY-resolve-91c2"


def _rendered(error: BaseException) -> str:
    return str(error) + repr(error) + "".join(traceback.format_exception(error))


def _code(call: Any) -> str:
    from reader.config import SecretResolutionError

    with pytest.raises(SecretResolutionError) as caught:
        call()
    assert CANARY not in _rendered(caught.value)
    return str(caught.value.code)


def _ref(text: str) -> Any:
    from reader.config import SecretRef

    return SecretRef(text)


# keychain -------------------------------------------------------------------------------------------------


def test_keychain_returns_the_item_as_a_secret() -> None:
    from reader.cli.resolve import keychain_resolver

    fake = FakeKeyring({("reader-ai", "claude-oauth-token"): CANARY})
    secret = keychain_resolver(fake)(_ref("secret://keychain/reader-ai/claude-oauth-token"))
    assert secret.reveal() == CANARY
    assert CANARY not in repr(secret)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (None, "not_found"),
        (keyring.errors.KeyringLocked("locked"), "keychain_unavailable"),
        (keyring.errors.KeyringError("denied"), "keychain_unavailable"),
    ],
)
def test_keychain_failures_have_codes(error: BaseException | None, code: str) -> None:
    from reader.cli.resolve import keychain_resolver

    fake = FakeKeyring(error=error)
    assert _code(lambda: keychain_resolver(fake)(_ref("secret://keychain/reader-ai/x"))) == code


def test_keyring_backend_is_pinned_to_the_macos_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring as keyring_module

    from reader.cli.resolve import macos_keyring

    monkeypatch.setattr(keyring_module, "get_keyring", lambda: pytest.fail("configured backend consulted"))
    backend = macos_keyring()
    assert type(backend).__module__ == "keyring.backends.macOS"
    assert type(backend).__name__ == "Keyring"


# op -------------------------------------------------------------------------------------------------------


def _op(runner: FakeRunner, env: dict[str, str] | None = None) -> Any:
    from reader.cli.resolve import op_resolver

    return op_resolver(runner, env or {"PATH": "/usr/bin", "HOME": "/h"})


@pytest.mark.parametrize(
    ("out", "value"),
    [
        (CANARY.encode() + b"\n", CANARY),
        (CANARY.encode() + b"\n\n", CANARY + "\n"),
        (CANARY.encode(), CANARY),
    ],
)
def test_op_read_strips_exactly_one_trailing_newline(out: bytes, value: str) -> None:
    runner = FakeRunner().on("op", "read", result=out)
    secret = _op(runner)(_ref("secret://op/Private/anna-claude/token"))
    assert secret.reveal() == value
    (call,) = runner.calls
    assert call.argv == ["op", "read", "op://Private/anna-claude/token"]
    assert call.timeout == 120


def test_op_timeout_is_tool_failed() -> None:
    from reader.cli.runner import CommandTimeout

    def hang(_call: Any) -> Any:
        raise CommandTimeout()

    runner = FakeRunner().on("op", "read", handler=hang)
    assert _code(lambda: _op(runner)(_ref("secret://op/Private/anna-claude/token"))) == "tool_failed"


def test_op_runs_with_the_allow_listed_environment() -> None:
    runner = FakeRunner().on("op", "read", result=b"v\n")
    _op(runner, {"PATH": "/usr/bin", "HOME": "/h"})(_ref("secret://op/Private/anna-claude/token"))
    (call,) = runner.calls
    assert call.env is not None
    assert set(call.env) <= ENV_ALLOW
    assert not any(k.startswith("OP_") for k in call.env)


def test_op_missing_binary_is_tool_missing() -> None:
    from reader.cli.runner import CommandFailed

    def missing(_call: Any) -> Any:
        raise CommandFailed()

    runner = FakeRunner().on("op", "read", handler=missing)
    assert _code(lambda: _op(runner)(_ref("secret://op/Private/anna-claude/token"))) == "tool_missing"


def test_op_non_zero_exit_is_tool_failed() -> None:
    runner = FakeRunner().on(
        "op", "read", result=(1, CANARY.encode(), b"[ERROR] not signed in " + CANARY.encode())
    )
    assert _code(lambda: _op(runner)(_ref("secret://op/Private/anna-claude/token"))) == "tool_failed"


# hostfile -------------------------------------------------------------------------------------------------


def _hostfile(path: Path) -> Any:
    from reader.cli.resolve import hostfile_resolver

    return hostfile_resolver(_ref("secret://hostfile" + str(path)))


def _write(path: Path, text: str = CANARY, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def test_hostfile_reads_a_0600_file_of_the_user(tmp_path: Path) -> None:
    assert _hostfile(_write(tmp_path / "s" / "key")).reveal() == CANARY


def test_hostfile_with_wrong_mode_is_outside_root(tmp_path: Path) -> None:
    assert _code(lambda: _hostfile(_write(tmp_path / "s" / "key", mode=0o644))) == "outside_root"


def test_hostfile_inside_a_git_work_tree_is_outside_root(tmp_path: Path) -> None:
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    assert _code(lambda: _hostfile(_write(tmp_path / "repo" / "deep" / "key"))) == "outside_root"


def test_hostfile_symlink_is_not_followed(tmp_path: Path) -> None:
    target = _write(tmp_path / "s" / "real")
    link = tmp_path / "s" / "link"
    link.symlink_to(target)
    assert _code(lambda: _hostfile(link)) == "not_regular_file"


def test_hostfile_directory_is_not_a_regular_file(tmp_path: Path) -> None:
    directory = tmp_path / "s" / "dir"
    directory.mkdir(parents=True)
    directory.chmod(0o700)
    assert _code(lambda: _hostfile(directory)) == "not_regular_file"


def test_hostfile_missing_is_not_found(tmp_path: Path) -> None:
    assert _code(lambda: _hostfile(tmp_path / "nope")) == "not_found"


@pytest.mark.skipif(os.geteuid() == 0, reason="permissions do not apply to uid 0")
def test_hostfile_unreadable(tmp_path: Path) -> None:
    assert _code(lambda: _hostfile(_write(tmp_path / "s" / "key", mode=0o000))) in {
        "unreadable",
        "outside_root",
    }


# value checks ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (b"x" * 65537, "too_large"),
        (b"\xff\xfe" + CANARY.encode(), "not_utf8"),
        (b"", "empty"),
        ("", "empty"),
    ],
)
def test_values_are_checked_on_the_host(value: bytes | str, code: str) -> None:
    from reader.cli.resolve import check_value

    assert _code(lambda: check_value(_ref("secret://keychain/reader-ai/x"), value)) == code


def test_value_at_the_size_bound_passes() -> None:
    from reader.cli.resolve import check_value

    assert len(check_value(_ref("secret://keychain/reader-ai/x"), b"x" * 65536).reveal()) == 65536


def test_every_scheme_goes_through_the_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from reader.cli.resolve import host_resolvers
    from reader.config import resolve

    monkeypatch.setenv("READER_TEST_TOKEN", "x" * 70000)
    registry = host_resolvers(
        FakeKeyring({("reader-ai", "big"): "y" * 70000}), FakeRunner(), {"PATH": "/usr/bin"}
    )
    for ref in ("secret://env/READER_TEST_TOKEN", "secret://keychain/reader-ai/big"):
        assert _code(lambda r=ref: resolve(r, registry)) == "too_large"
