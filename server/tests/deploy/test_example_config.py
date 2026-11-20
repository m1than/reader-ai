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
