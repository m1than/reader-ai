"""SRV-AUTH startup validation (FR-AUTH-16), RP ID and origin (FR-AUTH-3), sign-up flag (FR-AUTH-13).

@trace FR-AUTH-3 FR-AUTH-13 FR-AUTH-16
"""

from __future__ import annotations

from pathlib import Path

import pytest
from auth_helpers import SPEC, settings_for
from ruamel.yaml import YAML

OWNER_ONLY = (("ivan", "owner"),)


def _code(tmp_path: Path, **kwargs: object) -> str | None:
    from reader.auth.policy import StartupError

    try:
        settings_for(tmp_path, **kwargs)
    except StartupError as error:
        return error.code
    return None


def test_config_error_codes_match_the_spec() -> None:
    from reader.auth.policy import CONFIG_ERROR_CODES

    spec = YAML(typ="safe").load(SPEC.read_text(encoding="utf-8"))
    assert [e["code"] for e in spec["config_errors"]] == list(CONFIG_ERROR_CODES)


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://localhost/app", "instance_url_path"),
        ("https://localhost/?x=1", "instance_url_query"),
        ("https://localhost/#frag", "instance_url_query"),
        ("https://user@localhost", "instance_url_userinfo"),
        ("https://127.0.0.1", "instance_url_ip_host"),
        ("https://[::1]", "instance_url_ip_host"),
        ("https://l" + chr(0x043E) + "calhost", "instance_url_host_form"),  # Cyrillic o: an IDN host
        ("https://localhost.", "instance_url_host_form"),
    ],
)
def test_instance_url_errors(tmp_path: Path, url: str, code: str) -> None:
    assert _code(tmp_path, url=url, users=OWNER_ONLY) == code


@pytest.mark.parametrize(
    "users",
    [(), (("a", "learner"),), (("a", "owner"), ("b", "owner"))],
    ids=["none", "no-owner", "two-owners"],
)
def test_owner_count_must_be_one(tmp_path: Path, users: tuple[tuple[str, str], ...]) -> None:
    assert _code(tmp_path, users=users) == "owner_count"


def test_unknown_role(tmp_path: Path) -> None:
    assert _code(tmp_path, users=(("ivan", "owner"), ("x", "admin"))) == "unknown_role"


@pytest.mark.parametrize(
    "sharing", ["{ signup: true }", "{ signup: 'false' }", "{ signup: 0 }", "{ signup: null }"]
)
def test_signup_other_than_absent_or_false_fails(tmp_path: Path, sharing: str) -> None:
    assert _code(tmp_path, users=OWNER_ONLY, sharing=sharing) == "signup_not_supported"


@pytest.mark.parametrize("sharing", ["{}", "{ signup: false }", "{ invitations: false }"])
def test_signup_absent_or_false_is_accepted(tmp_path: Path, sharing: str) -> None:
    assert _code(tmp_path, users=OWNER_ONLY, sharing=sharing) is None


def test_first_error_in_table_order_wins(tmp_path: Path) -> None:
    assert _code(tmp_path, url="https://user@localhost/app", users=()) == "instance_url_path"


@pytest.mark.parametrize(
    ("url", "rp_id", "origin"),
    [
        ("https://localhost", "localhost", "https://localhost"),
        ("https://localhost/", "localhost", "https://localhost"),
        ("https://LocalHost:443", "localhost", "https://localhost"),
        ("https://localhost:8443", "localhost", "https://localhost:8443"),
        ("https://Reader.Example.org", "reader.example.org", "https://reader.example.org"),
    ],
)
def test_rp_id_and_origin(tmp_path: Path, url: str, rp_id: str, origin: str) -> None:
    settings = settings_for(tmp_path, url=url, users=OWNER_ONLY)
    assert (settings.rp_id, settings.origin) == (rp_id, origin)


def test_settings_carry_users_and_owner(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    assert settings.owner_handle == "ivan"
    assert [(u.handle, u.role) for u in settings.users] == [("ivan", "owner"), ("live-check", "learner")]
