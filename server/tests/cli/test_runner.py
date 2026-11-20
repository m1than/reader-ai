"""The real process runner and the watcher spawner: allow-listed environment, argv-free errors, fds 3 and 4.

@trace FR-CLI-8, FR-CLI-11, FR-CLI-21, INV-CLI-2, INV-CLI-9
"""

import sys
import traceback

import pytest

from .conftest import ENV_ALLOW

CANARY = "CANARY-argv-5d1e"


def test_safe_env_keeps_only_the_allow_list() -> None:
    from reader.cli.runner import safe_env

    environ = {
        "PATH": "/bin",
        "HOME": "/h",
        "LANG": "C",
        "DOCKER_HOST": "unix:///x",
        "DOCKER_CONTEXT": "desktop",
        "OP_SESSION_x": "s",
        "READER_TEST_TOKEN": "t",
        "CLAUDE_CODE_OAUTH_TOKEN": "c",
        "SSH_AUTH_SOCK": "a",
    }
    assert safe_env(environ) == {k: environ[k] for k in ENV_ALLOW}


def test_safe_env_omits_unset_docker_variables() -> None:
    from reader.cli.runner import safe_env

    assert safe_env({"PATH": "/bin", "HOME": "/h"}) == {"PATH": "/bin", "HOME": "/h"}


def test_subprocess_runner_passes_only_the_given_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from reader.cli.runner import SubprocessRunner

    monkeypatch.setenv("READER_TEST_TOKEN", CANARY)
    result = SubprocessRunner().run(
        ["/bin/sh", "-c", 'printf %s "${READER_TEST_TOKEN-unset}"'], env={"PATH": "/usr/bin:/bin"}, timeout=10
    )
    assert result.code == 0
    assert result.out == b"unset"


def test_subprocess_runner_refuses_a_missing_environment() -> None:
    from reader.cli.runner import SubprocessRunner

    with pytest.raises(TypeError):
        SubprocessRunner().run(["/usr/bin/true"], env=None, timeout=10)  # type: ignore[arg-type]


def _surfaces(error: BaseException) -> str:
    return str(error) + repr(error) + "".join(traceback.format_exception(error)) + repr(error.args)


def test_timeout_raises_an_argv_free_error() -> None:
    from reader.cli.runner import CommandTimeout, SubprocessRunner

    with pytest.raises(CommandTimeout) as caught:
        SubprocessRunner().run(["/bin/sleep", "5", CANARY], env={"PATH": "/bin"}, timeout=0.2)
    assert CANARY not in _surfaces(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_missing_binary_raises_an_argv_free_error() -> None:
    from reader.cli.runner import CommandFailed, SubprocessRunner

    with pytest.raises(CommandFailed) as caught:
        SubprocessRunner().run([f"/nonexistent/{CANARY}", CANARY], env={"PATH": "/bin"}, timeout=10)
    assert CANARY not in _surfaces(caught.value)
    assert caught.value.__context__ is None


def test_non_zero_exit_is_a_result_not_an_exception() -> None:
    from reader.cli.runner import SubprocessRunner

    result = SubprocessRunner().run(["/bin/sh", "-c", "exit 3"], env={"PATH": "/bin"}, timeout=10)
    assert result.code == 3


CHILD = (
    "import os,sys\n"
    "data=b''\n"
    "while True:\n"
    "    chunk=os.read(3,65536)\n"
    "    if not chunk: break\n"
    "    data+=chunk\n"
    "ok=data==b'BUNDLE' and os.isatty(0) is False\n"
    "try:\n"
    "    os.fstat(5); extra=True\n"
    "except OSError:\n"
    "    extra=False\n"
    "os.write(4,b'1' if ok and not extra else b'0')\n"
)


def test_spawner_hands_the_bundle_over_fd_3_and_reads_readiness_on_fd_4() -> None:
    from reader.cli.runner import PosixSpawner

    proc = PosixSpawner().spawn(
        [sys.executable, "-c", CHILD], env={"PATH": "/usr/bin:/bin"}, bundle=b"BUNDLE"
    )
    assert proc.wait_ready(10) is True


def test_spawner_readiness_times_out_without_a_byte() -> None:
    from reader.cli.runner import PosixSpawner

    proc = PosixSpawner().spawn(
        [sys.executable, "-c", "import time; time.sleep(5)"], env={"PATH": "/usr/bin:/bin"}, bundle=b""
    )
    try:
        assert proc.wait_ready(0.3) is False
    finally:
        proc.terminate()


def test_spawner_readiness_fails_when_the_child_exits_without_a_byte() -> None:
    from reader.cli.runner import PosixSpawner

    proc = PosixSpawner().spawn([sys.executable, "-c", "pass"], env={"PATH": "/usr/bin:/bin"}, bundle=b"")
    assert proc.wait_ready(10) is False
