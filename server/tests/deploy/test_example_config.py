# @trace FR-DEPLOY-12, VEC-DEPLOY-1
"""OPS-DEPLOY [config]: deploy/reader.example.yaml loads through SRV-CONFIG and covers every section."""

from .conftest import EXAMPLE, load_yaml

SECTIONS = {"version", "instance", "users", "tasks", "budgets", "kernels", "materials", "sectors", "sharing"}


def test_example_loads_through_load_config() -> None:
    assert EXAMPLE.exists(), "deploy/reader.example.yaml does not exist"
    from reader.config import load_config

    config = load_config(EXAMPLE)
    assert config.instance.url == "https://localhost"


def test_example_covers_all_sections_with_references_only() -> None:
    data = load_yaml(EXAMPLE)
    assert set(data) == SECTIONS
    tokens = [p["token"] for u in data["users"] for p in u.get("providers", [])]
    assert tokens
    assert all(t.startswith("secret://") for t in tokens)


def test_compile_task_budgets_fit_two_worst_case_calls_per_run() -> None:
    data = load_yaml(EXAMPLE)
    budgets = {name: task.get("budget_usd_per_lesson") for name, task in data["tasks"].items()}
    compile_tasks = {k: budgets[k] for k in ("draft", "fact_check", "review")}
    assert compile_tasks == {"draft": 4.0, "fact_check": 2.0, "review": 4.0}
