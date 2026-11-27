"""`reader doctor`'s judge_slots check (FR-CLI-25, ADR-0039 B7; SRV-AGENT FR-AGENT-18).

@trace FR-CLI-25
"""

import json
from pathlib import Path
from typing import Any

import pytest

from .conftest import FIXTURES, make_world, rendered, stack, write_config

SLOTS = {
    "judge.find": "haiku", "judge.link_type": "haiku", "ingest.context_prefix": "haiku",
    "judge.eval.relevance": "opus", "judge.eval.sufficiency": "opus", "judge.eval.groundedness": "opus",
    "judge.eval.self_contained": "opus", "judge.eval.citation_supports_claim": "opus", "judge.align": "opus",
}


def _config(tmp_path: Path, slots: dict[str, str]) -> Any:
    from reader.config import load_config

    base = (FIXTURES / "config.yaml").read_text(encoding="utf-8")
    tasks = "tasks:\n" + "".join(f"  {name}: {{ model: {model} }}\n" for name, model in slots.items())
    return load_config(write_config(tmp_path / "cfg", base + (tasks if slots else "")))


@pytest.mark.parametrize(
    ("missing", "agents", "ok", "detail"),
    [
        ((), True, True, "all bound"),
        (("judge.find",), True, False, "missing: judge.find"),
        (("judge.eval.relevance",), True, True, "missing: judge.eval.relevance"),
        (("judge.find",), False, True, "off"),
    ],
)
def test_judge_slots_check(
    tmp_path: Path, missing: tuple[str, ...], agents: bool, ok: bool, detail: str
) -> None:
    from reader.cli.maintenance import judge_slots_check

    config = _config(tmp_path, {k: v for k, v in SLOTS.items() if k not in missing})
    got_ok, got_detail = judge_slots_check(config, agents)
    assert got_ok is ok
    assert got_detail.startswith(detail)
    if missing and agents:
        assert "add tasks.<slot>: {model: <alias>} to reader.yaml" in got_detail
        assert "deploy/reader.example.yaml" in got_detail


def test_the_check_names_every_slot_of_the_agent_table() -> None:
    from reader.agent.judges import JUDGE_SLOTS

    assert set(JUDGE_SLOTS) == set(SLOTS)


def test_doctor_json_carries_the_check(tmp_path: Path) -> None:
    from reader.cli.maintenance import cmd_doctor

    world = make_world(tmp_path)
    stack(world, rendered(agents=True), volume_exists=True)
    world.runner.on("docker", "compose", "version", result=b"5.6.1\n")
    cmd_doctor(world.ctx, write_config(tmp_path / "cfg"), True)
    check = next(c for c in json.loads(world.ctx.out.getvalue()) if c["name"] == "judge_slots")
    assert check["ok"] is False  # VEC-CLI-1 binds no slot, and agents is chosen
    assert check["detail"].startswith("missing: ")
    assert "judge.find" in check["detail"]
