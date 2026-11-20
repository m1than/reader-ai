"""Rendered model, override and delivery plan.

@trace FR-CLI-3, FR-CLI-4, FR-CLI-5, VEC-CLI-1, VEC-CLI-2, VEC-CLI-3
"""

from pathlib import Path
from typing import Any

import pytest

from .conftest import COMPOSE, DEPLOY_FIXTURES, FIXTURES, load_yaml, write_config

PLAN = load_yaml(FIXTURES / "delivery-plan.yaml")
TEST_DIR = "/tmp/reader-ai-test"  # noqa: S108 - a fixture literal that the test replaces, never opened


def _swap(node: Any, real_dir: str) -> Any:
    """The fixture with the per-test directory's resolved path substituted for /tmp/reader-ai-test."""
    if isinstance(node, dict):
        return {k: _swap(v, real_dir) for k, v in node.items()}
    if isinstance(node, list):
        return [_swap(v, real_dir) for v in node]
    if isinstance(node, str) and node.startswith(TEST_DIR + "/"):
        return real_dir + node[len(TEST_DIR) :]
    return node


def _comparable(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: doc[k] for k in ("services", "networks")}


def _override(tmp_path: Path, handles: list[str]) -> tuple[dict[str, Any], str]:
    from reader.cli.stack import build_override, load_compose_source

    config = write_config(tmp_path / "cfg")
    return build_override(load_compose_source(COMPOSE), handles, config), str(config.parent)


def test_override_for_two_users_equals_vec_cli_2(tmp_path: Path) -> None:
    override, real_dir = _override(tmp_path, ["ivan", "anna"])
    expected = _swap(load_yaml(FIXTURES / "override-two-users.yaml"), real_dir)
    assert _comparable(override) == _comparable(expected)


def test_override_for_ivan_alone_equals_vec_deploy_3(tmp_path: Path) -> None:
    override, real_dir = _override(tmp_path, ["ivan"])
    expected = _swap(load_yaml(DEPLOY_FIXTURES / "cli-override.example.yaml"), real_dir)
    assert _comparable(override) == _comparable(expected)


def test_override_kernel_body_comes_from_the_x_kernel_source(tmp_path: Path) -> None:
    from reader.cli.stack import load_compose_source

    override, _ = _override(tmp_path, ["ivan"])
    kernel = dict(override["services"]["kernel-user-ivan"])
    assert kernel.pop("networks") == ["sb-user-ivan"]
    assert kernel == load_compose_source(COMPOSE)["x-kernel"]


def test_override_binds_reader_yaml_only_into_the_server_roles(tmp_path: Path) -> None:
    override, real_dir = _override(tmp_path, ["ivan"])
    bound = {s for s, body in override["services"].items() if "volumes" in body}
    assert bound == {"api", "worker", "mcp"}
    for service in bound:
        (volume,) = override["services"][service]["volumes"]
        assert volume == {
            "type": "bind",
            "source": real_dir + "/reader.yaml",
            "target": "/etc/reader/reader.yaml",
            "read_only": True,
            "bind": {"create_host_path": False},
        }
        assert "environment" not in override["services"][service]


@pytest.mark.parametrize("handle", ["a" * 51, "Ivan", "-ivan", "iv an", "ivan_1", ""])
def test_bad_handles_stop_with_an_error_naming_the_handle(handle: str) -> None:
    from reader.cli.errors import CliError
    from reader.cli.stack import check_handle

    with pytest.raises(CliError) as caught:
        check_handle(handle)
    assert caught.value.code == "bad_handle"


@pytest.mark.parametrize("handle", ["a" * 50, "ivan", "live-check", "0x"])
def test_good_handles_pass(handle: str) -> None:
    from reader.cli.stack import check_handle

    assert check_handle(handle) is None


def test_config_path_with_dollar_is_refused(tmp_path: Path) -> None:
    from reader.cli.errors import CliError
    from reader.cli.stack import check_config_path

    path = write_config(tmp_path / "a$b")
    with pytest.raises(CliError) as caught:
        check_config_path(path)
    assert caught.value.code == "config_path_interpolates"


def test_config_path_is_resolved_to_an_absolute_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from reader.cli.stack import check_config_path

    write_config(tmp_path / "cfg")
    monkeypatch.chdir(tmp_path)
    resolved = check_config_path(Path("cfg/../cfg/reader.yaml"))
    assert resolved.is_absolute()
    assert resolved == (tmp_path / "cfg" / "reader.yaml").resolve()


@pytest.mark.parametrize("case", PLAN["cases"], ids=lambda c: c["id"])
def test_rendered_names_come_from_the_model(case: dict[str, Any]) -> None:
    from reader.cli.stack import Rendered

    model = Rendered.parse(case["rendered"])
    assert model.name == case["expected"]["project"]
    assert model.pgdata_volume == case["expected"]["pgdata_volume"]


@pytest.mark.parametrize("case", PLAN["cases"], ids=lambda c: c["id"])
def test_delivery_plan_equals_vec_cli_3(case: dict[str, Any]) -> None:
    from reader.cli.stack import Rendered, make_plan

    from reader.config import load_config

    plan = make_plan(Rendered.parse(case["rendered"]), load_config(FIXTURES / "config.yaml"))
    got = [{"service": d.service, "uid": d.uid, "name": d.name} for d in plan.deliveries]
    assert got == case["expected"]["plan"]
    if "resolved" in case["expected"]:
        assert sorted(plan.sources) == case["expected"]["resolved"]


def test_delivery_script_is_the_fixture_constant() -> None:
    from reader.cli.stack import DELIVERY_SCRIPT

    assert PLAN["script"].strip() == DELIVERY_SCRIPT


def test_consumer_without_numeric_uid_stops_naming_the_service() -> None:
    from reader.cli.errors import CliError
    from reader.cli.stack import Rendered, make_plan

    from reader.config import load_config

    data = PLAN["cases"][0]["rendered"]
    broken = {**data, "services": {**data["services"], "api": {}}}
    with pytest.raises(CliError) as caught:
        make_plan(Rendered.parse(broken), load_config(FIXTURES / "config.yaml"))
    assert caught.value.code == "no_uid"
    assert "api" in str(caught.value)


def test_file_scheme_provider_token_is_not_deliverable(tmp_path: Path) -> None:
    from reader.cli.errors import CliError
    from reader.cli.stack import Rendered, make_plan

    from reader.config import load_config

    text = (
        (FIXTURES / "config.yaml").read_text().replace("secret://env/READER_TEST_TOKEN", "secret://file/tok")
    )
    config = load_config(write_config(tmp_path, text))
    with pytest.raises(CliError) as caught:
        make_plan(Rendered.parse(PLAN["cases"][1]["rendered"]), config)
    assert caught.value.code == "not_deliverable"
