"""`reader up` profiles for the vertical slice: kernels always, agents when a provider is configured and the
agent-proxy image exists; tokens go to agent-proxy only.

@trace FR-CLI-4, FR-CLI-5, FR-CLI-8, VEC-CLI-2
"""

import json
from pathlib import Path
from typing import Any

import pytest

from .conftest import FIXTURES, World, make_world, rendered, stack, write_config

NO_PROVIDERS = """\
version: 1
instance: {url: https://localhost, data_dir: ./data}
users:
  - {handle: ivan, role: owner}
  - {handle: anna, role: learner}
observability: {enabled: false}
"""
# FR-CLI-22: `observability` is off in these profile tests (and in VEC-CLI-1), so the profile sets stay as
# written; test_observability.py turns it on.


def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, agents_in_model: bool) -> World:
    monkeypatch.setenv("READER_TEST_TOKEN", "CANARY-prof-env")
    world = make_world(tmp_path)
    world.keyring.items[("reader-ai-system", "postgres")] = "CANARY-prof-db"
    world.keyring.items[("reader-ai", "claude-oauth-token")] = "CANARY-prof-kc"
    world.runner.on("op", "read", result=b"CANARY-prof-op\n")
    stack(world, rendered(agents=agents_in_model), volume_exists=True)
    return world


def _config(tmp_path: Path, text: str | None = None) -> Path:
    secret = tmp_path / "secrets" / "anna-api-key"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("CANARY-prof-hf")
    secret.chmod(0o600)
    if text is None:
        text = (
            (FIXTURES / "config.yaml")
            .read_text()
            .replace(
                "secret://hostfile/tmp/reader-ai-test/anna-api-key",
                "secret://hostfile" + str(secret.resolve()),
            )
        )
    return write_config(tmp_path / "cfg", text)


def _up(world: World, config: Path) -> int:
    from reader.cli.commands import cmd_up

    return int(cmd_up(world.ctx, config))


def _profiles(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--profile"]


def _compose_calls(world: World) -> dict[str, list[str]]:
    calls = {}
    for c in world.runner.calls:
        if c.argv[:2] == ["docker", "compose"] and "-f" in c.argv:
            key = "up" if c.argv[-2:] == ["up", "-d"] else c.argv[-1] if c.argv[-1] == "build" else "config"
            calls[key] = c.argv
    return calls


def test_agents_and_kernels_profiles_with_a_provider_and_the_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, agents_in_model=True)
    assert _up(world, _config(tmp_path)) == 0
    (inspect,) = world.runner.argvs("docker", "image", "inspect")
    assert inspect == ["docker", "image", "inspect", "reader-agent-proxy:local"]
    calls = _compose_calls(world)
    assert set(calls) == {"config", "build", "up"}
    for argv in calls.values():
        assert _profiles(argv) == ["agents", "kernels"]
        assert argv.index("--profile") < argv.index("-f")
    assert (world.ctx.run_dir / "profiles").read_text().split() == ["agents"]


def test_missing_agent_proxy_image_leaves_agents_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch, agents_in_model=False)
    world.runner.on("docker", "image", "inspect", result=(1, b"", b"No such image"))
    assert _up(world, _config(tmp_path)) == 0
    for argv in _compose_calls(world).values():
        assert _profiles(argv) == ["kernels"]
    assert "agents" in world.err


def test_no_provider_means_no_agents_and_no_token_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reader.cli.watcher import decode_bundle

    world = _world(tmp_path, monkeypatch, agents_in_model=False)
    assert _up(world, _config(tmp_path, NO_PROVIDERS)) == 0
    assert world.runner.argvs("docker", "image", "inspect") == []
    for argv in _compose_calls(world).values():
        assert _profiles(argv) == ["kernels"]
    assert world.runner.argvs("op", "read") == []
    assert ("keyring.get", "reader-ai", "claude-oauth-token") not in world.journal
    assert set(decode_bundle(world.spawner.spawned[0]["bundle"])) == {"db-password"}


def test_without_agent_proxy_in_the_model_no_provider_token_is_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, agents_in_model=False)
    world.runner.on("docker", "image", "inspect", result=(1, b"", b"No such image"))
    assert _up(world, _config(tmp_path)) == 0
    assert world.runner.argvs("op", "read") == []
    assert ("keyring.get", "reader-ai", "claude-oauth-token") not in world.journal


def test_provider_tokens_go_to_agent_proxy_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch, agents_in_model=True)
    assert _up(world, _config(tmp_path)) == 0
    (spawned,) = world.spawner.spawned
    argv = spawned["argv"]
    plan = json.loads(argv[argv.index("--plan") + 1])
    tokens = [d for d in plan if d["name"] != "db-password"]
    assert len(tokens) == 4
    assert {d["service"] for d in tokens} == {"agent-proxy"}
    assert {d["service"] for d in plan if d["name"] == "db-password"} == {"postgres", "api", "worker"}


def test_status_renders_with_the_recorded_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from reader.cli.commands import cmd_status

    world = _world(tmp_path, monkeypatch, agents_in_model=True)
    config = _config(tmp_path)
    assert _up(world, config) == 0
    world.runner.calls.clear()
    cmd_status(world.ctx, config)
    (render,) = [c.argv for c in world.runner.calls if c.argv[-3:] == ["config", "--format", "json"]]
    assert _profiles(render) == ["agents", "kernels"]


def _record(world: World, text: str) -> None:
    world.ctx.run_dir.mkdir(parents=True, exist_ok=True)
    (world.ctx.run_dir / "profiles").write_text(text)


def test_failed_up_leaves_the_previous_profile_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, agents_in_model=False)
    world.runner.on("docker", "image", "inspect", result=(1, b"", b"No such image"))
    world.spawner.ready = False
    _record(world, "agents\n")
    assert _up(world, _config(tmp_path)) != 0
    assert (world.ctx.run_dir / "profiles").read_text() == "agents\n"
    assert world.runner.argvs("rm", "-sf") == []


def test_profiles_are_recorded_after_readiness_just_before_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, agents_in_model=True)
    written: list[int] = []
    original = Path.write_text

    def spy(self: Path, data: str, *args: Any, **kwargs: Any) -> int:
        if self.name == "profiles":
            written.append(len(world.journal))
        return original(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", spy)
    assert _up(world, _config(tmp_path)) == 0
    ready = next(i for i, e in enumerate(world.journal) if e[0] == "ready")
    up = next(i for i, e in enumerate(world.journal) if e[0] == "run" and e[-2:] == ("up", "-d"))
    assert len(written) == 1
    assert ready < written[0] <= up


def test_withdrawn_agents_profile_removes_agent_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, agents_in_model=False)
    world.runner.on("docker", "image", "inspect", result=(1, b"", b"No such image"))
    _record(world, "agents\n")
    assert _up(world, _config(tmp_path)) == 0
    (rm,) = world.runner.argvs("rm", "-sf")
    assert rm == ["docker", "compose", "-p", "reader-vec", "rm", "-sf", "agent-proxy"]
    calls = [c.argv for c in world.runner.calls]
    up = next(i for i, a in enumerate(calls) if a[-2:] == ["up", "-d"])
    assert calls.index(rm) < up
    assert (world.ctx.run_dir / "profiles").read_text().split() == []


def test_provider_removed_withdraws_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch, agents_in_model=False)
    _record(world, "agents\n")
    assert _up(world, _config(tmp_path, NO_PROVIDERS)) == 0
    assert world.runner.argvs("rm", "-sf") == [
        ["docker", "compose", "-p", "reader-vec", "rm", "-sf", "agent-proxy"]
    ]


def test_no_withdrawal_when_the_profile_stays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch, agents_in_model=True)
    _record(world, "agents\n")
    assert _up(world, _config(tmp_path)) == 0
    assert world.runner.argvs("rm", "-sf") == []


def test_failed_withdrawal_stops_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch, agents_in_model=False)
    world.runner.on("docker", "image", "inspect", result=(1, b"", b"No such image"))
    world.runner.on("rm", "-sf", result=(1, b"", b"daemon error"))
    _record(world, "agents\n")
    assert _up(world, _config(tmp_path)) != 0
    assert world.runner.argvs("up", "-d") == []
    assert world.spawner.procs[0].terminated


def test_doctor_checks_with_the_profiles_up_would_choose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reader.cli.maintenance import cmd_doctor

    world = _world(tmp_path, monkeypatch, agents_in_model=True)
    cmd_doctor(world.ctx, _config(tmp_path), True)
    (render,) = [c.argv for c in world.runner.calls if c.argv[-3:] == ["config", "--format", "json"]]
    assert _profiles(render) == ["agents", "kernels"]
    assert world.runner.argvs("op", "read")  # provider references checked before any `up` with agents


def test_override_renders_kernel_user_services_for_every_user(tmp_path: Path) -> None:
    from reader.cli.stack import build_override, load_compose_source

    from .conftest import COMPOSE

    config = write_config(tmp_path / "cfg")
    override: dict[str, Any] = build_override(load_compose_source(COMPOSE), ["ivan", "anna"], config)
    kernels = {s for s in override["services"] if s.startswith("kernel-user-")}
    assert kernels == {"kernel-user-ivan", "kernel-user-anna"}
    assert set(override["services"]["api"]["networks"]) == {"sb-user-ivan", "sb-user-anna"}
    assert override["services"]["agent-proxy"]["volumes"][0]["target"] == "/etc/reader/reader.yaml"
    assert "networks" not in override["services"]["agent-proxy"]
