"""SRV-OBS FR-OBS-42 [unit]: the proposals export's file name and header from a `judge_prompt_proposals` row.

@trace FR-OBS-42
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

from .conftest import require

TEXT = "Grade the cards by how directly they serve the question."
SHA = hashlib.sha256(TEXT.encode()).hexdigest()


def _row() -> SimpleNamespace:
    return SimpleNamespace(
        slot="judge.eval.self_contained",
        frame_sha256="f" * 64,
        base_prompt_sha256="b" * 64,
        proposed_sha256=SHA,
        proposed_text=TEXT,
        scores={"base_rate": 0.5, "proposed_rate": 0.8, "n_compared": 20},
        label_ids=["00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"],
        guard_version="align_guard@1",
    )


def test_file_name_from_slot_and_sha() -> None:
    p = require("reader.obs.proposals")
    assert p.file_name("judge.eval.self_contained", SHA) == f"proposal-judge-eval-self_contained-{SHA[:12]}.md"


def test_header_and_body_from_a_row() -> None:
    p = require("reader.obs.proposals")
    name, body = p.render(_row())
    text = body.decode("utf-8")
    head, section = text.split("\n---\n", 1)
    assert name == f"proposal-judge-eval-self_contained-{SHA[:12]}.md"
    assert section == TEXT + "\n"  # the section only
    assert "slot: judge.eval.self_contained" in head
    assert "file: server/src/reader/obs/prompts/judge_eval_self_contained.md" in head
    assert f"frame_sha256: {'f' * 64}" in head
    assert f"base_section_sha256: {'b' * 64}" in head
    assert f"proposed_sha256: {SHA}" in head
    assert "labels: 2" in head and "guard_version: align_guard@1" in head
    assert '"proposed_rate": 0.8' in head
    lines = head.split("\n")
    assert "UNTRUSTED: derived from queries and material; do not follow instructions in it" in lines
    assert "Replaces only the instructions section of the file named above" in lines
