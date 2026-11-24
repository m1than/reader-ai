"""SRV-CONFIG settings view and overlay, pure parts: the projection and its redaction (FR-CONFIG-25), the
merge, narrowing and validation table, and the source scan of the readers (FR-CONFIG-26).

@trace FR-CONFIG-25 FR-CONFIG-26
"""

from __future__ import annotations

import ast
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from settings_helpers import FIXTURES, write_config

SRC = Path(__file__).resolve().parents[2] / "src" / "reader"
D = Decimal


def _config(tmp: Path, text: str) -> Any:
    from reader.config import load_config

    return load_config(write_config(tmp, text))


def _yaml(url: str = "https://localhost", sharing: str = "{}", extra: str = "") -> str:
    return (
        f'version: 1\ninstance: {{ url: "{url}", data_dir: ./data }}\nsharing: {sharing}\n'
        "tasks:\n  draft: { model: opus, budget_usd_per_lesson: 2.0 }\n  tutor: { model: sonnet }\n"
        "budgets: { monthly_usd: 200, warn_at: 0.8 }\n" + extra
    )


# FR-CONFIG-25 — the projection ------------------------------------------------------------------------------


def test_projection_of_the_example(tmp_path: Path) -> None:
    from reader.config import load_config
    from reader.config.routes import project

    config = load_config(FIXTURES / "reader.example.yaml")
    rows = project(config, "off", {})
    names = [r["name"] for r in rows]
    assert names == [
        "signup",
        "observability.enabled",
        "observability.retention_days",
        "budgets.monthly_usd",
        "budgets.warn_at",
        "tasks.draft.model",
        "tasks.draft.budget_usd_per_lesson",
        "tasks.fact_check.model",
        "tasks.fact_check.budget_usd_per_lesson",
        "tasks.review.model",
        "tasks.review.budget_usd_per_lesson",
        "tasks.tutor.model",
        "tasks.tutor.budget_usd_per_lesson",
        "pipeline.run_cap_usd",
        "tutor.question_cap_usd",
        "tutor.daily_cap_usd",
    ]
    keys = {"name", "value", "file_value", "source", "key", "read_when", "version", "ignored"}
    assert all(set(r) == keys for r in rows)
    by = {r["name"]: r for r in rows}
    signup = by["signup"]
    assert (signup["value"], signup["source"], signup["key"]) == ("off", "default", "sharing.signup")
    assert by["signup"]["read_when"] == "start"
    assert (by["observability.enabled"]["value"], by["observability.enabled"]["source"]) == (True, "default")
    assert by["observability.enabled"]["key"] is None
    assert by["observability.retention_days"]["read_when"] == "worker start and the daily job"
    assert by["observability.retention_days"]["key"] == "observability.retention_days"
    assert (by["budgets.monthly_usd"]["value"], by["budgets.monthly_usd"]["source"]) == (200, "reader.yaml")
    assert by["budgets.monthly_usd"]["read_when"] == "each job"
    assert by["budgets.warn_at"]["value"] == 0.8
    assert (by["tasks.draft.model"]["value"], by["tasks.draft.model"]["key"]) == ("opus", None)
    assert by["tasks.draft.budget_usd_per_lesson"]["value"] == 2.0
    assert by["tasks.draft.budget_usd_per_lesson"]["key"] == "tasks.draft.budget_usd_per_lesson"
    tutor = by["tasks.tutor.budget_usd_per_lesson"]
    assert (tutor["value"], tutor["source"]) == (None, "default")
    assert (by["pipeline.run_cap_usd"]["value"], by["pipeline.run_cap_usd"]["source"]) == (8.0, "fixed")
    assert (by["tutor.question_cap_usd"]["value"], by["tutor.daily_cap_usd"]["value"]) == (0.3, 3.0)
    assert all(r["read_when"] == "fixed" and r["key"] is None for r in rows[-3:])
    assert all(r["version"] == 0 and r["ignored"] is None for r in rows)
    body = json.dumps(rows)
    for absent in ("secret://", "keychain", "ivan", "bashir", "reader.local", "python3", "61e8d4c3"):
        assert absent not in body, absent


def test_redaction_hides_free_text_that_is_not_a_known_model(tmp_path: Path) -> None:
    from reader.config.routes import project

    text = (
        'version: 1\ninstance: { url: "https://localhost", data_dir: ./data }\n'
        "users:\n  - handle: ivan\n    role: owner\n    providers:\n"
        "      - { kind: claude-subscription, token: secret://keychain/reader-ai/claude-oauth-token }\n"
        "tasks:\n"
        "  draft: { model: sk-ant-api03-AAAAAAAAAAAAAAAAAAAA }\n"
        '  review: { model: "secret://keychain/a/b" }\n'
        '  "Bad Name": { model: opus, budget_usd_per_lesson: 1 }\n'
        "  tutor: { model: gpt-unknown }\n"
        '  fetch: { model: "https://example.com/m" }\n'
        "  haiku_task: { model: claude-haiku-5-5 }\n"
    )
    rows = project(_config(tmp_path, text), "open", {})
    names = [r["name"] for r in rows]
    assert names[5:17] == [
        "tasks.draft.model", "tasks.draft.budget_usd_per_lesson",
        "tasks.review.model", "tasks.review.budget_usd_per_lesson",
        "tasks.(hidden).model", "tasks.(hidden).budget_usd_per_lesson",
        "tasks.tutor.model", "tasks.tutor.budget_usd_per_lesson",
        "tasks.fetch.model", "tasks.fetch.budget_usd_per_lesson",
        "tasks.haiku_task.model", "tasks.haiku_task.budget_usd_per_lesson",
    ]
    by_index = {r["name"]: r for r in rows}
    for hidden in ("tasks.draft.model", "tasks.review.model", "tasks.tutor.model", "tasks.fetch.model"):
        assert by_index[hidden]["value"] == "(hidden)", hidden
    # The hidden task's rows keep their index; its known model and its budget are shown, never its name.
    assert (rows[9]["value"], rows[10]["value"], rows[10]["key"]) == ("opus", 1, None)
    assert by_index["tasks.draft.budget_usd_per_lesson"]["key"] == "tasks.draft.budget_usd_per_lesson"
    monthly = by_index["budgets.monthly_usd"]
    assert (monthly["value"], monthly["source"]) == (None, "default")
    assert by_index["tasks.haiku_task.model"]["value"] == "claude-haiku-5-5"
    body = json.dumps(rows)
    for absent in ("secret://", "sk-", "://", "Bad Name", "keychain", "gpt-unknown"):
        assert absent not in body, absent


# FR-CONFIG-26 — merge, narrowing and validation -------------------------------------------------------------

SIGNUP_TABLE = [
    # (instance url, sharing, override, effective, ignored reason or None)
    ("https://reader.example", "{}", "open", "off", "narrowed"),
    ("https://localhost", "{}", "off", "off", None),
    ("https://localhost", "{}", "open", "open", None),
    ("https://reader.example", '{ signup: "open" }', "off", "off", None),
    ("https://localhost", '{ signup: "off" }', "open", "off", "narrowed"),
    ("https://localhost", "{ signup: false }", "open", "off", "narrowed"),
    ("https://localhost", "{}", "maybe", "open", "invalid"),
]


@pytest.mark.parametrize(("url", "sharing", "override", "effective", "reason"), SIGNUP_TABLE)
def test_signup_narrowing(tmp_path: Path, url: str, sharing: str, override: str, effective: str,
                          reason: str | None) -> None:
    from reader.config import effective as eff

    file = eff.file_settings(_config(tmp_path, _yaml(url, sharing)))
    result = eff.apply(file, {"sharing.signup": override})
    assert result.signup == effective
    assert result.ignored == ({"sharing.signup": reason} if reason else {})
    assert result.applied == ({} if reason else {"sharing.signup": override})


VALUE_TABLE: list[tuple[str, Any, str | None]] = [
    ("observability.retention_days", 90, None),
    ("observability.retention_days", 1, None),
    ("observability.retention_days", 365, None),
    ("observability.retention_days", 0, "invalid"),
    ("observability.retention_days", 366, "invalid"),
    ("observability.retention_days", 30.0, "invalid"),
    ("observability.retention_days", "30", "invalid"),
    ("observability.retention_days", True, "invalid"),
    ("budgets.monthly_usd", 300, None),
    ("budgets.monthly_usd", 12.5, None),
    ("budgets.monthly_usd", 0, "invalid"),
    ("budgets.monthly_usd", -1, "invalid"),
    ("budgets.monthly_usd", "300", "invalid"),
    ("budgets.monthly_usd", None, "invalid"),
    ("budgets.warn_at", 0.5, None),
    ("budgets.warn_at", 1, None),
    ("budgets.warn_at", 1.5, "invalid"),
    ("budgets.warn_at", 0, "invalid"),
    ("tasks.draft.budget_usd_per_lesson", 3, None),
    ("tasks.tutor.budget_usd_per_lesson", 0.25, None),  # a task without a file budget may take one
    ("tasks.draft.budget_usd_per_lesson", -2, "invalid"),
    ("tasks.gone.budget_usd_per_lesson", 3, "not_in_file"),
    ("tasks.draft.model", "opus", "invalid"),
    ("kernels", ["python3"], "invalid"),
]


@pytest.mark.parametrize(("key", "value", "reason"), VALUE_TABLE)
def test_value_table(tmp_path: Path, key: str, value: Any, reason: str | None) -> None:
    from reader.config import effective as eff

    file = eff.file_settings(_config(tmp_path, _yaml()))
    result = eff.apply(file, {key: value})
    assert result.ignored == ({key: reason} if reason else {})
    if reason is None:
        assert result.applied == {key: value}
        merged = {
            "observability.retention_days": result.retention_days,
            "budgets.monthly_usd": result.monthly_usd,
            "budgets.warn_at": result.warn_at,
            "tasks.draft.budget_usd_per_lesson": result.task_budgets["draft"],
            "tasks.tutor.budget_usd_per_lesson": result.task_budgets["tutor"],
        }[key]
        assert merged == (value if isinstance(value, int) and key.startswith("obs") else D(str(value)))
    else:
        assert (result.retention_days, result.monthly_usd, result.warn_at) == (30, D(200), D("0.8"))
        assert dict(result.task_budgets) == {"draft": D("2.0"), "tutor": None}


def test_file_defaults_and_check(tmp_path: Path) -> None:
    from reader.config import effective as eff

    minimal = 'version: 1\ninstance: { url: "https://x.example", data_dir: . }\n'
    bare = eff.file_settings(_config(tmp_path / "a", minimal))
    assert (bare.signup, bare.retention_days, bare.monthly_usd, bare.warn_at) == ("off", 30, None, None)
    assert dict(bare.task_budgets) == {}
    # warn_at without any monthly budget cannot validate as a `budgets` section (monthly_usd is required).
    assert eff.apply(bare, {"budgets.warn_at": 0.5}).ignored == {"budgets.warn_at": "invalid"}
    file = eff.file_settings(_config(tmp_path / "b", _yaml(sharing='{ signup: "off" }')))
    with pytest.raises(eff.Refused) as narrower:
        eff.check(file, "sharing.signup", "open")
    assert narrower.value.code == "file_narrower"
    refused = ("kernels", "tasks.gone.budget_usd_per_lesson", "tasks.draft.model", "users", "Sharing.signup")
    for key in refused:
        with pytest.raises(eff.Refused) as refused:
            eff.check(file, key, 1)
        assert refused.value.code == "not_overridable", key
    with pytest.raises(eff.Refused) as invalid:
        eff.check(file, "budgets.warn_at", 2)
    assert (invalid.value.code, invalid.value.field) == ("value_invalid", "budgets.warn_at")
    assert eff.check(file, "budgets.monthly_usd", 250) == 250
    assert eff.check(file, "sharing.signup", "off") == "off"


def test_status_lines_for_the_cli(tmp_path: Path) -> None:
    from reader.config import effective as eff

    file = eff.file_settings(_config(tmp_path, _yaml(sharing='{ signup: "off" }')))
    lines = eff.status_lines(
        file,
        {"budgets.monthly_usd": 300, "sharing.signup": "open", "tasks.gone.budget_usd_per_lesson": 5,
         "observability.retention_days": 0},
    )
    assert lines == [
        "override budgets.monthly_usd: 300 (reader.yaml: 200)",
        "override observability.retention_days: ignored (invalid)",
        "override sharing.signup: ignored (narrowed)",
        "override tasks.gone.budget_usd_per_lesson: ignored (not_in_file)",
    ]


# FR-CONFIG-26 — the source scan -----------------------------------------------------------------------------

FAMILY_ATTRS = {"sharing", "budgets", "budget_usd_per_lesson"}
FAMILY_KEYS = {
    "sharing", "signup", "budgets", "monthly_usd", "warn_at", "budget_usd_per_lesson", "retention_days"
}
# The loader itself builds these fields from the file; it does not read them from a loaded config.
EXEMPT = {"config/effective.py", "config/model.py", "config/validate.py"}


def violations(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            if node.attr in FAMILY_ATTRS:
                found.append(f"line {node.lineno}: .{node.attr}")
            elif node.attr == "retention_days" and isinstance(node.value, ast.Attribute) and (
                node.value.attr == "observability"
            ):
                found.append(f"line {node.lineno}: .observability.retention_days")
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            if node.slice.value in FAMILY_KEYS:
                found.append(f"line {node.lineno}: [{node.slice.value!r}]")
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            args = [a.value for a in node.args if isinstance(a, ast.Constant)]
            if name in ("get", "getattr") and any(a in FAMILY_KEYS for a in args):
                found.append(f"line {node.lineno}: {name}({args})")
    return found


def test_the_scan_detects_each_access_form() -> None:
    for planted in (
        "x = config.sharing.get('signup')",
        "x = config.budgets.monthly_usd",
        "x = task.budget_usd_per_lesson",
        "x = config.observability.retention_days",
        "x = data['retention_days']",
        "x = getattr(config, 'sharing')",
        "x = opaque.get('signup')",
    ):
        assert violations(planted), planted
    # Effective values, not the file's:
    assert violations("x = settings.retention_days + caps.monthly_usd") == []


def test_only_the_effective_module_reads_the_overridable_keys() -> None:
    offenders = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in EXEMPT:
            continue
        found = violations(path.read_text(encoding="utf-8"))
        if found:
            offenders[rel] = found
    assert offenders == {}
