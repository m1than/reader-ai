"""`reader up`: start order, hand-off, readiness, health, the DB password, leak and fail-closed invariants.

@trace FR-CLI-5, FR-CLI-8, FR-CLI-10, FR-CLI-14, FR-CLI-16
@trace INV-CLI-1, INV-CLI-2, INV-CLI-4, INV-CLI-5, INV-CLI-6
"""

import json
import logging
import signal
from pathlib import Path
from typing import Any

import keyring.errors
import pytest

from .conftest import (
    ENV_ALLOW,
    FIXTURES,
    T0,
    Lock,
    World,
    held_lock,
    make_world,
    ps_lines,
    rendered,
    stack,
    write_config,
    writes,
)

DB = ("reader-ai-system", "postgres")
CANARIES = {
    "keychain": "CANARY-kc-11aa",
    "env": "CANARY-env-22bb",
    "op": "CANARY-op-33cc",
    "hostfile": "CANARY-hf-44dd",
    "db": "CANARY-db-55ee",
}


def _config(tmp_path: Path) -> Path:
    """VEC-CLI-1 with its hostfile pointing at a real 0600 file outside any git work tree."""
    secret = tmp_path / "secrets" / "anna-api-key"
    secret.parent.mkdir(parents=True)
    secret.write_text(CANARIES["hostfile"])
    secret.chmod(0o600)
    text = (
        (FIXTURES / "config.yaml")
        .read_text()
        .replace(
            "secret://hostfile/tmp/reader-ai-test/anna-api-key", "secret://hostfile" + str(secret.resolve())
        )
    )
    return write_config(tmp_path / "cfg", text)


def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, agents: bool = True, **kw: Any) -> World:
    monkeypatch.setenv("READER_TEST_TOKEN", CANARIES["env"])
    world = make_world(tmp_path, **kw)
    world.keyring.items[("reader-ai", "claude-oauth-token")] = CANARIES["keychain"]
    world.keyring.items[DB] = CANARIES["db"]
    world.runner.on("op", "read", result=(CANARIES["op"] + "\n").encode())
    stack(world, rendered(agents=agents), volume_exists=True)
    return world


def _up(world: World, config: Path) -> int:
    from reader.cli.commands import cmd_up

    return int(cmd_up(world.ctx, config))


def _index(journal: list[tuple[Any, ...]], *prefix: str) -> int:
    for i, entry in enumerate(journal):
        if entry[: len(prefix)] == prefix:
            return i
    raise AssertionError(f"{prefix} not in journal")


# order and hand-off (FR-CLI-8) ----------------------------------------------------------------------------


def test_up_order_build_t0_resolve_watcher_ready_then_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch)
    assert _up(world, _config(tmp_path)) == 0
    j = world.journal
    build = next(i for i, e in enumerate(j) if e[:3] == ("run", "docker", "compose") and e[-1] == "build")
    info = _index(j, "run", "docker", "info")
    resolve = _index(j, "keyring.get", "reader-ai", "claude-oauth-token")
    spawn = _index(j, "spawn")
    ready = _index(j, "ready")
    up = next(i for i, e in enumerate(j) if e[0] == "run" and "up" in e and "-d" in e)
    assert build < info < resolve < spawn < ready < up


def test_watcher_gets_t0_and_the_plan_but_no_value_in_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch)
    _up(world, _config(tmp_path))
    (spawned,) = world.spawner.spawned
    argv = spawned["argv"]
    assert argv[1:3] == ["-m", "reader.cli.watcher"]
    assert argv[argv.index("--since") + 1] == T0
    assert argv[argv.index("--project") + 1] == "reader-vec"
    plan = json.loads(argv[argv.index("--plan") + 1])
    assert {"service": "postgres", "uid": 999, "name": "db-password"} in plan
    assert world.spawner.procs[0].ready_timeouts == [10]


def test_no_readiness_byte_stops_the_watcher_and_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, ready=False)
    assert _up(world, _config(tmp_path)) != 0
    assert world.runner.argvs("up", "-d") == []
    assert world.spawner.procs[0].terminated


def test_compose_calls_take_the_override_on_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch)
    _up(world, _config(tmp_path))
    for tokens in (("config",), ("build",), ("up", "-d")):
        (argv,) = world.runner.argvs("docker", "compose", *tokens)
        call = next(c for c in world.runner.calls if c.argv == argv)
        assert argv[:6] == ["docker", "compose", "-f", str(world.ctx.compose_file), "-f", "-"]
        override = json.loads(call.stdin or b"")
        assert "kernel-user-ivan" in override["services"]


# INV-CLI-2: values only in the bundle ---------------------------------------------------------------------


def test_values_reach_only_the_watcher_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    world = _world(tmp_path, monkeypatch)
    with caplog.at_level(logging.DEBUG):
        assert _up(world, _config(tmp_path)) == 0
    for call in world.runner.calls:
        assert call.env is not None
        assert set(call.env) <= ENV_ALLOW
        surfaces = " ".join(call.argv) + json.dumps(call.env) + (call.stdin or b"").decode(errors="replace")
        for canary in CANARIES.values():
            assert canary not in surfaces
    (spawned,) = world.spawner.spawned
    assert spawned["env"] is not None
    assert set(spawned["env"]) <= ENV_ALLOW
    assert "READER_TEST_TOKEN" not in spawned["env"]
    for canary in CANARIES.values():
        assert canary not in " ".join(spawned["argv"]) + json.dumps(spawned["env"])
        assert canary.encode() in spawned["bundle"]
        assert canary not in world.out + world.err + caplog.text


def test_default_profile_resolves_only_the_db_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reader.cli.watcher import decode_bundle

    world = _world(tmp_path, monkeypatch, agents=False)
    assert _up(world, _config(tmp_path)) == 0
    assert world.runner.argvs("op", "read") == []
    assert ("keyring.get", "reader-ai", "claude-oauth-token") not in world.journal
    assert set(decode_bundle(world.spawner.spawned[0]["bundle"])) == {"db-password"}


# INV-CLI-1 and INV-CLI-6: nothing written but runtime state -----------------------------------------------


def test_up_writes_no_value_and_no_override_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch)
    config = _config(tmp_path)
    with writes() as written:
        assert _up(world, config) == 0
    allowed = (str(world.ctx.run_dir), str(world.ctx.log_dir))
    for path in written:
        assert path.startswith(allowed), path
    for root in (world.ctx.run_dir, world.ctx.log_dir, config.parent):
        for path in root.rglob("*") if root.exists() else []:
            if path.is_file():
                text = path.read_bytes()
                for canary in CANARIES.values():
                    assert canary.encode() not in text, path


def test_up_records_the_project_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch)
    _up(world, _config(tmp_path))
    assert (world.ctx.run_dir / "project").read_text().strip() == "reader-vec"
    assert (world.ctx.run_dir.stat().st_mode & 0o777) == 0o700


# INV-CLI-4: fail closed -----------------------------------------------------------------------------------


def test_resolution_failure_starts_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch)
    world.runner.on("op", "read", result=(1, b"", b"not signed in"))
    assert _up(world, _config(tmp_path)) != 0
    assert world.spawner.spawned == []
    assert world.runner.argvs("up", "-d") == []
    assert "tool_failed" in world.err


def test_config_error_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch)
    bad = write_config(tmp_path / "bad", "version: 2\n")
    assert _up(world, bad) == 2
    assert world.runner.calls == []


def test_spawn_failure_is_a_coded_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch)

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("posix_spawn failed")

    world.spawner.spawn = broken  # type: ignore[method-assign]
    assert _up(world, _config(tmp_path)) == 1
    assert "spawn_failed" in world.err
    assert world.runner.argvs("up", "-d") == []


def test_unparsable_compose_json_is_a_coded_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch)
    world.runner.on("docker", "compose", "config", result=b"not json")
    assert _up(world, _config(tmp_path)) == 1
    assert "bad_model" in world.err
    assert "Traceback" not in world.err


# FR-CLI-10: health bound, second up -----------------------------------------------------------------------


def test_up_waits_180_seconds_after_up_for_health(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch, agents=False)
    world.runner.on("docker", "compose", "ps", result=ps_lines({"api": ("running", "starting")}))
    assert _up(world, _config(tmp_path)) != 0
    assert 180 <= sum(world.clock.slept) < 190


def test_up_succeeds_when_all_default_services_are_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, agents=False)
    assert _up(world, _config(tmp_path)) == 0
    assert world.spawner.procs[0].terminated is False


def test_second_up_stops_the_old_watcher_after_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder: list[Lock] = []

    def release(_pid: int, sig: int) -> None:
        if sig == signal.SIGTERM:
            holder[0].release()

    world = _world(tmp_path, monkeypatch, kill=release)
    with held_lock(world.ctx.run_dir, pid=4242) as lock:
        holder.append(lock)
        assert _up(world, _config(tmp_path)) == 0
    j = world.journal
    assert (
        _index(j, "keyring.get", "reader-ai", "claude-oauth-token") < _index(j, "kill") < _index(j, "spawn")
    )


def test_failed_resolution_leaves_the_old_watcher_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch)
    world.runner.on("op", "read", result=(1, b"", b""))
    with held_lock(world.ctx.run_dir, pid=4242):
        assert _up(world, _config(tmp_path)) != 0
    assert world.kills == []


# FR-CLI-16: an agent-run (non-interactive) up never trusts ----------------------------------------------


def test_non_interactive_up_skips_trust(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch, tty=False, answer=True)
    _up(world, _config(tmp_path))
    assert world.runner.argvs("security") == []


# FR-CLI-14 and INV-CLI-5: the DB password -----------------------------------------------------------------

ITEM = {"present": "existing-pw", "absent": None, "error": keyring.errors.KeyringError("denied")}
VOLUME = {
    "present": (0, b"[]", b""),
    "absent": (1, b"", b"Error response from daemon: get reader-vec_pgdata: no such volume"),
    "error": (1, b"", b"Cannot connect to the Docker daemon at unix:///var/run/docker.sock"),
}


@pytest.mark.parametrize("item", sorted(ITEM))
@pytest.mark.parametrize("volume", sorted(VOLUME))
def test_db_password_is_generated_only_when_item_and_volume_are_both_absent(
    tmp_path: Path, item: str, volume: str
) -> None:
    from reader.cli.commands import ensure_db_password
    from reader.cli.errors import CliError

    world = make_world(tmp_path)
    if isinstance(ITEM[item], BaseException):
        world.keyring.error = ITEM[item]  # type: ignore[assignment]
    elif ITEM[item] is not None:
        world.keyring.items[DB] = ITEM[item]  # type: ignore[assignment]
    world.runner.on("docker", "volume", "inspect", result=VOLUME[volume])
    if item == "present":
        assert ensure_db_password(world.ctx, "reader-vec_pgdata").reveal() == "existing-pw"
    elif (item, volume) == ("absent", "absent"):
        secret = ensure_db_password(world.ctx, "reader-vec_pgdata")
        assert len(secret.reveal()) >= 43
        assert world.keyring.sets == [(*DB, secret.reveal())]
        (argv,) = world.runner.argvs("docker", "volume", "inspect")
        assert argv[-1] == "reader-vec_pgdata"
    else:
        with pytest.raises(CliError):
            ensure_db_password(world.ctx, "reader-vec_pgdata")
    if (item, volume) != ("absent", "absent"):
        assert world.keyring.sets == []
