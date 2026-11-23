"""OPS-CLI FR-CLI-22 (paired SRV-OBS change): profile `observability`, the 8443 port in the override, the
traces line, and the doctor's gate check.

@trace FR-CLI-22
"""

from pathlib import Path
from typing import Any

import pytest

from .conftest import COMPOSE, write_config
from .test_profiles import _compose_calls, _profiles, _up, _world

ON = """\
version: 1
instance: {url: https://localhost, data_dir: ./data}
users:
  - {handle: ivan, role: owner}
"""


def _cfg(tmp: Path, enabled: bool | None) -> Path:
    tail = "" if enabled is None else f"observability: {{enabled: {str(enabled).lower()}}}\n"
    return write_config(tmp / "cfg", ON + tail)


def test_optional_profiles_and_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from reader.cli.commands import OPTIONAL_PROFILES, choose_profiles
    from reader.config import load_config

    assert "observability" in OPTIONAL_PROFILES
    world = _world(tmp_path, monkeypatch, agents_in_model=False)
    assert choose_profiles(world.ctx, load_config(_cfg(tmp_path, None))) == ("observability",)  # default on
    assert choose_profiles(world.ctx, load_config(_cfg(tmp_path, True))) == ("observability",)
    assert choose_profiles(world.ctx, load_config(_cfg(tmp_path, False))) == ()


def test_override_publishes_8443_only_with_the_profile(tmp_path: Path) -> None:
    from reader.cli.stack import build_override, load_compose_source

    config = write_config(tmp_path / "c")
    source = load_compose_source(COMPOSE)
    with_obs: dict[str, Any] = build_override(source, ["ivan"], config, observability=True)
    assert with_obs["services"]["caddy"] == {"ports": ["127.0.0.1:8443:8443"]}
    without: dict[str, Any] = build_override(source, ["ivan"], config, observability=False)
    assert "caddy" not in without["services"]
    assert "caddy" not in build_override(source, ["ivan"], config)["services"]


def test_up_with_the_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch, agents_in_model=False)
    assert _up(world, _cfg(tmp_path, True)) == 0
    for argv in _compose_calls(world).values():
        assert _profiles(argv) == ["kernels", "observability"]
    assert "traces: https://localhost:8443/" in world.out
    assert (world.ctx.run_dir / "profiles").read_text().split() == ["observability"]


def test_up_without_the_profile_prints_no_traces_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, agents_in_model=False)
    assert _up(world, _cfg(tmp_path, False)) == 0
    assert "traces:" not in world.out


@pytest.mark.parametrize(
    ("status", "ok", "detail"),
    [
        (401, True, "gate ok"),
        (200, False, "gate open"),
        (302, False, "gate open"),
        (None, False, "unreachable"),
    ],
)
def test_doctor_gate_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int | None, ok: bool, detail: str
) -> None:
    import json

    from reader.cli import maintenance

    calls: list[str] = []

    def fake(url: str) -> int | None:
        calls.append(url)
        return status

    monkeypatch.setattr(maintenance, "gate_status", fake)
    world = _world(tmp_path, monkeypatch, agents_in_model=False)
    maintenance.cmd_doctor(world.ctx, _cfg(tmp_path, True), True)
    checks = {c["name"]: c for c in json.loads(world.out.strip().splitlines()[-1])}
    assert calls == ["https://localhost:8443/"]
    assert (checks["observability"]["ok"], checks["observability"]["detail"]) == (ok, detail)


def test_doctor_gate_check_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from reader.cli import maintenance

    monkeypatch.setattr(maintenance, "gate_status", lambda url: pytest.fail("no request when off"))
    world = _world(tmp_path, monkeypatch, agents_in_model=False)
    maintenance.cmd_doctor(world.ctx, _cfg(tmp_path, False), True)
    checks = {c["name"]: c for c in json.loads(world.out.strip().splitlines()[-1])}
    assert (checks["observability"]["ok"], checks["observability"]["detail"]) == (True, "off")
