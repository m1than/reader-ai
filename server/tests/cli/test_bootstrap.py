"""`reader bootstrap [--handle H]`: the token goes only to `open`'s argv.

@trace FR-CLI-21, INV-CLI-9
"""

import logging
from pathlib import Path
from typing import Any

import pytest

from .conftest import World, held_lock, make_world, ps_lines, rendered, stack, write_config

TOKEN = "CANARYtok3n_x-91"  # noqa: S105 - a canary, not a credential
URL = "https://localhost/api/auth/bootstrap#" + TOKEN


def _world(
    tmp_path: Path, link: Any = None, *, healthy: bool = True, open_result: Any = 0
) -> tuple[World, Path]:
    world = make_world(tmp_path)
    model = rendered()
    states = {s: ("running", "healthy") for s in model["services"]}
    if not healthy:
        states["api"] = ("running", "unhealthy")
    stack(world, model, ps=ps_lines(states))
    world.runner.on("bootstrap-link", result=link if link is not None else (URL + "\n").encode())
    if callable(open_result):
        world.runner.on("open", handler=open_result)
    else:
        world.runner.on("open", result=open_result)
    return world, write_config(tmp_path / "cfg")


def _bootstrap(world: World, config: Path, *extra: str, watcher: bool = True) -> int:
    from reader.cli.app import main

    if not watcher:
        return int(main(["bootstrap", "--config", str(config), *extra], ctx=world.ctx))
    with held_lock(world.ctx.run_dir):
        return int(main(["bootstrap", "--config", str(config), *extra], ctx=world.ctx))


def _no_leak(world: World, caplog: pytest.LogCaptureFixture) -> None:
    assert TOKEN not in world.out
    assert TOKEN not in world.err
    assert TOKEN not in caplog.text
    for path in world.ctx.run_dir.rglob("*") if world.ctx.run_dir.exists() else []:
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes()


def test_bootstrap_opens_the_link_without_printing_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    world, config = _world(tmp_path)
    with caplog.at_level(logging.DEBUG):
        assert _bootstrap(world, config) == 0
    (link,) = [c for c in world.runner.calls if "bootstrap-link" in c.argv]
    assert link.argv == [
        "docker",
        "compose",
        "-p",
        "reader-vec",
        "exec",
        "-T",
        "api",
        "reader-server",
        "bootstrap-link",
    ]
    assert link.timeout == 15
    (opened,) = [c for c in world.runner.calls if c.argv[0] == "open"]
    assert opened.argv == ["open", URL]
    assert opened.timeout == 10
    _no_leak(world, caplog)


def test_bootstrap_passes_the_handle(tmp_path: Path) -> None:
    world, config = _world(tmp_path)
    assert _bootstrap(world, config, "--handle", "live-check") == 0
    (link,) = world.runner.argvs("bootstrap-link")
    assert link[-3:] == ["bootstrap-link", "--handle", "live-check"]


@pytest.mark.parametrize("handle", ["Live", "a" * 51, "-x", "x y"])
def test_bootstrap_refuses_a_bad_handle_and_runs_nothing(tmp_path: Path, handle: str) -> None:
    world, config = _world(tmp_path)
    assert _bootstrap(world, config, "--handle", handle) == 2
    assert world.runner.argvs("bootstrap-link") == []
    assert world.runner.argvs("open") == []


def test_bootstrap_needs_a_healthy_stack(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    world, config = _world(tmp_path, healthy=False)
    assert _bootstrap(world, config) != 0
    assert world.runner.argvs("bootstrap-link") == []
    assert world.runner.argvs("open") == []


def test_bootstrap_needs_the_watcher(tmp_path: Path) -> None:
    world, config = _world(tmp_path)
    assert _bootstrap(world, config, watcher=False) != 0
    assert world.runner.argvs("bootstrap-link") == []


def test_exit_1_means_a_passkey_exists_or_the_handle_is_unknown(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    world, config = _world(tmp_path, (1, b"", ("user already has a passkey " + TOKEN).encode()))
    with caplog.at_level(logging.DEBUG):
        assert _bootstrap(world, config) == 1
    assert "already has a passkey" in world.err
    assert world.runner.argvs("open") == []
    _no_leak(world, caplog)


@pytest.mark.parametrize(
    "output",
    [
        (URL + "\n" + URL + "\n").encode(),
        ("https://evil.example/api/auth/bootstrap#" + TOKEN + "\n").encode(),
        ("note " + URL + "\n").encode(),
        (URL + " extra\n").encode(),
        b"https://localhost/api/auth/bootstrap#\n",
        (URL + "\n\n").encode(),
        b"\xff" + TOKEN.encode(),
    ],
)
def test_malformed_output_gives_a_fixed_message_and_no_open(
    tmp_path: Path, output: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    world, config = _world(tmp_path, output)
    with caplog.at_level(logging.DEBUG):
        assert _bootstrap(world, config) != 0
    assert world.runner.argvs("open") == []
    _no_leak(world, caplog)


def _raise(kind: str) -> Any:
    def handler(_call: Any) -> Any:
        from reader.cli.runner import CommandFailed, CommandTimeout

        raise CommandTimeout() if kind == "timeout" else CommandFailed()

    return handler


@pytest.mark.parametrize("failure", ["exit", "timeout", "missing"])
def test_open_failure_gives_a_fixed_message(
    tmp_path: Path, failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    result: Any = (
        (1, b"", ("LSOpenURLsWithRole() failed for " + URL).encode())
        if failure == "exit"
        else _raise(failure)
    )
    world, config = _world(tmp_path, open_result=result)
    with caplog.at_level(logging.DEBUG):
        assert _bootstrap(world, config) != 0
    _no_leak(world, caplog)
    messages = world.err
    world2, config2 = _world(tmp_path / "again", open_result=result)
    _bootstrap(world2, config2)
    assert world2.err == messages


@pytest.mark.parametrize("failure", ["timeout", "missing", "exit"])
def test_link_failure_gives_a_fixed_message(
    tmp_path: Path, failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    link: Any = (2, (URL + "\n").encode(), TOKEN.encode())
    world, config = _world(tmp_path, link)
    if failure != "exit":
        world.runner.on("bootstrap-link", handler=_raise(failure))
    with caplog.at_level(logging.DEBUG):
        assert _bootstrap(world, config) != 0
    assert world.runner.argvs("open") == []
    _no_leak(world, caplog)
