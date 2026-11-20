"""Entry point, config-free commands, down, status, secrets set and doctor.

@trace FR-CLI-1, FR-CLI-7, FR-CLI-12, FR-CLI-13, FR-CLI-15
"""

import json
import signal
import tomllib
from pathlib import Path
from typing import Any

import pytest

from .conftest import PYPROJECT, World, held_lock, make_world, ps_lines, rendered, stack, write_config

CANARY = "CANARY-cmd-6f0a"


def _main(world: World, *argv: str) -> int:
    from reader.cli.app import main

    return int(main(list(argv), ctx=world.ctx))


# FR-CLI-1 -------------------------------------------------------------------------------------------------


def test_console_script_is_registered() -> None:
    data = tomllib.loads(PYPROJECT.read_text())
    assert data["project"].get("scripts", {}).get("reader") == "reader.cli.app:main"


def test_parser_has_every_subcommand() -> None:
    from reader.cli.app import build_parser

    parser = build_parser()
    for argv in (
        ["up"],
        ["down"],
        ["down", "--untrust"],
        ["status"],
        ["status", "--json"],
        ["watcher", "status"],
        ["watcher", "stop"],
        ["bootstrap"],
        ["bootstrap", "--handle", "live-check"],
        ["doctor"],
        ["doctor", "--json"],
        ["secrets", "set", "name"],
        ["trust"],
        ["backup", "/x"],
        ["restore", "/x"],
    ):
        assert parser.parse_args(argv) is not None
    with pytest.raises(SystemExit):
        parser.parse_args(["backup"])


@pytest.mark.parametrize(
    "command", [["up"], ["bootstrap"], ["doctor"], ["trust"], ["backup", "/x"], ["restore", "/x"]]
)
def test_config_commands_exit_2_on_a_config_error(tmp_path: Path, command: list[str]) -> None:
    world = make_world(tmp_path)
    bad = write_config(tmp_path / "bad", "version: 2\n")
    assert _main(world, *command, "--config", str(bad)) == 2
    assert world.err.strip()
    assert world.runner.calls == []


# FR-CLI-7: down -------------------------------------------------------------------------------------------


def test_down_without_config_uses_the_recorded_project(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.ctx.run_dir.mkdir()
    (world.ctx.run_dir / "project").write_text("reader-vec\n")
    assert _main(world, "down", "--config", str(tmp_path / "missing.yaml")) == 0
    (argv,) = world.runner.argvs("down")
    assert argv == ["docker", "compose", "-p", "reader-vec", "down", "--remove-orphans"]


def test_down_with_config_uses_the_rendered_name_and_stops_the_watcher(tmp_path: Path) -> None:
    holder: list[Any] = []

    def release(_pid: int, sig: int) -> None:
        if sig == signal.SIGTERM:
            holder[0].release()

    world = make_world(tmp_path, kill=release)
    stack(world, rendered("reader-other"))
    config = write_config(tmp_path / "cfg")
    with held_lock(world.ctx.run_dir, pid=4242) as lock:
        holder.append(lock)
        assert _main(world, "down", "--config", str(config)) == 0
    assert world.kills == [(4242, signal.SIGTERM)]
    (argv,) = world.runner.argvs("down")
    assert argv == ["docker", "compose", "-p", "reader-other", "down", "--remove-orphans"]
    assert world.journal.index(("kill", signal.SIGTERM)) < [e[:1] + e[-2:] for e in world.journal].index(
        ("run", "down", "--remove-orphans")
    )


def test_down_never_removes_volumes(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.ctx.run_dir.mkdir()
    (world.ctx.run_dir / "project").write_text("reader-vec\n")
    _main(world, "down", "--config", str(tmp_path / "missing.yaml"))
    for call in world.runner.calls:
        assert "-v" not in call.argv
        assert "--volumes" not in call.argv


# FR-CLI-13: status ----------------------------------------------------------------------------------------

EXPECTED = {"api", "worker", "postgres", "caddy"}


@pytest.mark.parametrize(
    ("states", "watcher", "code"),
    [
        ({s: ("running", "healthy") for s in EXPECTED}, True, 0),
        ({**{s: ("running", "healthy") for s in EXPECTED}, "api": ("running", "unhealthy")}, True, 1),
        ({s: ("running", "healthy") for s in EXPECTED - {"worker"}}, True, 1),
        ({s: ("running", "healthy") for s in EXPECTED}, False, 2),
        ({**{s: ("running", "healthy") for s in EXPECTED}, "api": ("exited", "")}, False, 3),
        ({}, False, 3),
    ],
)
def test_status_code_bits(states: dict[str, tuple[str, str]], watcher: bool, code: int) -> None:
    from reader.cli.commands import status_code

    services = [{"Service": s, "State": st, "Health": h} for s, (st, h) in states.items()]
    assert status_code(services, EXPECTED, watcher) == code


def test_status_json_schema(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    model = rendered()
    stack(world, model, ps=ps_lines({"api": ("running", "healthy"), "postgres": ("running", "starting")}))
    config = write_config(tmp_path / "cfg")
    with held_lock(world.ctx.run_dir):
        code = _main(world, "status", "--json", "--config", str(config))
    assert code == 1
    doc = json.loads(world.out)
    assert doc["project"] == "reader-vec"
    assert doc["watcher"] == "running"
    assert {"name": "api", "state": "running", "health": "healthy"} in doc["services"]
    assert all(set(s) == {"name", "state", "health"} for s in doc["services"])


def test_status_without_config_uses_the_recorded_project(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.ctx.run_dir.mkdir()
    (world.ctx.run_dir / "project").write_text("reader-vec\n")
    world.runner.on("docker", "compose", "ps", result=ps_lines({"api": ("running", "healthy")}))
    assert _main(world, "status", "--config", str(tmp_path / "missing.yaml")) == 2
    (argv,) = world.runner.argvs("ps")
    assert argv[:4] == ["docker", "compose", "-p", "reader-vec"]


def test_status_exit_4_without_a_project_name(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert _main(world, "status", "--config", str(tmp_path / "missing.yaml")) == 4


def test_status_exit_4_when_docker_is_unreachable(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.ctx.run_dir.mkdir()
    (world.ctx.run_dir / "project").write_text("reader-vec\n")
    world.runner.on("docker", "compose", "ps", result=(1, b"", b"Cannot connect to the Docker daemon"))
    assert _main(world, "status", "--config", str(tmp_path / "missing.yaml")) == 4


# FR-CLI-12: secrets set -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["postgres", "system", "system-x", "systemd", "Bad", "a b", "", "a" * 65, "-x"]
)
def test_secrets_set_refuses_reserved_and_invalid_names(tmp_path: Path, name: str) -> None:
    world = make_world(tmp_path, stdin_text=CANARY + "\n")
    assert _main(world, "secrets", "set", name) == 2
    assert world.keyring.sets == []


@pytest.mark.parametrize(
    ("piped", "stored"),
    [
        (CANARY + "\n", CANARY),
        (CANARY + "\n\n", CANARY + "\n"),
        (CANARY, CANARY),
    ],
)
def test_secrets_set_from_a_pipe(tmp_path: Path, piped: str, stored: str) -> None:
    world = make_world(tmp_path, stdin_text=piped)
    assert _main(world, "secrets", "set", "anna-key") == 0
    assert world.keyring.sets == [("reader-ai", "anna-key", stored)]
    assert world.out == "secret://keychain/reader-ai/anna-key\n"
    assert CANARY not in world.err


def test_secrets_set_from_a_tty_uses_a_no_echo_prompt(tmp_path: Path) -> None:
    world = make_world(tmp_path, tty=True, getpass_value=CANARY)
    assert _main(world, "secrets", "set", "anna-key") == 0
    assert world.keyring.sets == [("reader-ai", "anna-key", CANARY)]
    assert CANARY not in world.out + world.err


@pytest.mark.parametrize("piped", ["\n", "", "x" * 65537])
def test_secrets_set_checks_the_value(tmp_path: Path, piped: str) -> None:
    world = make_world(tmp_path, stdin_text=piped)
    assert _main(world, "secrets", "set", "anna-key") == 1
    assert world.keyring.sets == []


def test_secrets_set_needs_no_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    world = make_world(tmp_path, stdin_text="v\n")
    assert _main(world, "secrets", "set", "k") == 0


# FR-CLI-15: doctor ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "major"),
    [("5.6.1", 5), ("v2.29.1", 2), ("2.0.0-desktop.1", 2), ("1.29.2", 1), ("garbage", None)],
)
def test_compose_major(text: str, major: int | None) -> None:
    from reader.cli.maintenance import compose_major

    assert compose_major(text) == major


def _doctor(tmp_path: Path, compose: bytes) -> tuple[int, list[dict[str, Any]], World]:
    world = make_world(tmp_path)
    stack(world, rendered(agents=True), volume_exists=True)
    world.runner.on("docker", "compose", "version", result=compose)
    world.keyring.items[("reader-ai-system", "postgres")] = CANARY
    world.keyring.items[("reader-ai", "claude-oauth-token")] = CANARY
    config = write_config(tmp_path / "cfg")
    code = _main(world, "doctor", "--json", "--config", str(config))
    return code, json.loads(world.out), world


def test_doctor_reports_each_check_without_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("READER_TEST_TOKEN", raising=False)
    code, checks, world = _doctor(tmp_path, b"5.6.1\n")
    assert all(set(c) == {"name", "ok", "detail"} for c in checks)
    names = {c["name"] for c in checks}
    assert {"docker", "compose", "keyring", "config", "secrets", "db-password", "watcher"} <= names
    failed = {c["name"]: c["detail"] for c in checks if not c["ok"]}
    assert "env_unset" in failed["secrets"]
    assert code == 1
    assert CANARY not in world.out + world.err


def test_doctor_fails_on_compose_v1(tmp_path: Path) -> None:
    _, checks, _ = _doctor(tmp_path, b"1.29.2\n")
    assert next(c for c in checks if c["name"] == "compose")["ok"] is False
