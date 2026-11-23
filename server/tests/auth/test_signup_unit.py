"""SRV-AUTH sign-up, pure rules: the switch, handle and display-name rules, the skeleton, the static pages.

@trace FR-AUTH-23 FR-AUTH-25 FR-AUTH-31 INV-AUTH-15
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pytest
from auth_helpers import config_text, settings_for, write_config
from hypothesis import given, settings
from hypothesis import strategies as st

OWNER_ONLY = (("ivan", "owner"),)
STATIC = Path(__file__).resolve().parents[2] / "src" / "reader" / "auth" / "static"
CYRILLIC_A = chr(0x430)


# FR-AUTH-23 — the switch ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "sharing", "mode", "note"),
    [
        ("https://localhost", "{}", "open", False),
        ("https://localhost:8443", "{}", "open", False),
        ("https://reader.example", "{}", "off", True),
        ("https://localhost", "{ signup: false }", "off", False),
        ("https://localhost", '{ signup: "off" }', "off", False),
        ("https://localhost", "{ signup: off }", "off", False),
        ("https://localhost", '{ signup: "open" }', "open", False),
        ("https://reader.example", '{ signup: "open" }', "open", False),
        ("https://reader.example", '{ signup: "off" }', "off", False),
    ],
)
def test_switch_parse_table(tmp_path: Path, url: str, sharing: str, mode: str, note: bool) -> None:
    s = settings_for(tmp_path, url=url, users=OWNER_ONLY, sharing=sharing)
    assert (s.signup, s.signup_default_note) == (mode, note)


@pytest.mark.parametrize(
    "sharing",
    [
        "{ signup: true }",
        "{ signup: 'yes' }",
        "{ signup: 1 }",
        '{ signup: "Open" }',
        "{ signup: null }",
        "{ signup: 'false' }",
    ],
)
def test_switch_refuses_other_values(tmp_path: Path, sharing: str) -> None:
    from reader.auth.policy import StartupError, settings_from_config
    from reader.config import load_config

    config = load_config(write_config(tmp_path, config_text(users=OWNER_ONLY, sharing=sharing)))
    with pytest.raises(StartupError) as raised:
        settings_from_config(config)
    assert raised.value.code == "signup_not_supported"


# FR-AUTH-25 — skeleton, handles, display names -------------------------------------------------------------


@pytest.mark.parametrize(
    ("handle", "expected"),
    [
        ("admin", "admln"),
        ("adm1n", "admln"),
        ("mike", "mlke"),
        ("rnike", "mlke"),
        ("r-nike", "mlke"),
        ("ivan", "lvan"),
        ("i-van", "lvan"),
        ("lvan", "lvan"),
        ("vvatt", "watt"),
        ("watt", "watt"),
        ("lc-1a2b", "lcla2b"),
        ("Ivan", "lvan"),
        ("rrnn", "rmn"),
    ],
)
def test_skeleton_steps_in_order(handle: str, expected: str) -> None:
    from reader.auth.handles import skeleton

    assert skeleton(handle) == expected


@pytest.mark.parametrize(
    ("handle", "ok"),
    [
        ("ab", False),
        ("a" * 33, False),
        ("a" * 32, True),
        ("abc", True),
        ("Ivan", False),
        ("ivan_", False),
        ("-ab", False),
        ("ab-", False),
        ("a--b", False),
        ("1abc", False),
        (CYRILLIC_A + "bc", False),
        ("admin", False),  # reserved
        ("adm1n", False),  # the skeleton of reserved `admin`
        ("live-check", False),  # reserved
        ("signups", False),
        ("null", False),
        ("lc-1a2b", True),
        ("ab\n", False),
        (None, False),
        (12345, False),
    ],
)
def test_handle_candidate_rules(handle: object, ok: bool) -> None:
    from reader.auth.handles import handle_candidate_ok

    assert handle_candidate_ok(handle) is ok


E_ACUTE, COMBINING_ACUTE = chr(0xE9), chr(0x301)
THUMB, SKIN = chr(0x1F44D), chr(0x1F3FD)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", None),
        ("   ", None),
        ("a" * 64, "a" * 64),
        ("a" * 65, None),
        ("  Анна Петрова  ", "Анна Петрова"),
        ("a\tb", None),
        ("evil" + chr(0x202E) + "abc", None),  # Cf, a bidi override
        ("zero" + chr(0x200B) + "width", None),  # Cf
        ("x" + chr(0xD800) + "y", None),  # Cs, a lone surrogate (JSON "\ud800")
        ("pua" + chr(0xE000), None),  # Co
        ("un" + chr(0xE0080), None),  # Cn, unassigned
        ("line" + chr(0x2028) + "sep", None),  # Zl
        ("para" + chr(0x2029) + "sep", None),  # Zp
        ("zw" + chr(0x200C) + "nj", None),  # ZWNJ is Cf: the stated known limit
        ("e" + COMBINING_ACUTE, E_ACUTE),  # decomposed é becomes one code point
        (E_ACUTE * 64, E_ACUTE * 64),
        (("e" + COMBINING_ACUTE) * 64, E_ACUTE * 64),  # 128 code points before NFC, 64 after
        (THUMB + SKIN, THUMB + SKIN),  # an emoji with a skin-tone modifier: two code points
        (None, None),
        (5, None),
    ],
)
def test_display_name_rules(value: object, expected: str | None) -> None:
    from reader.auth.handles import display_name

    assert display_name(value) == expected


REFUSED = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}


@settings(max_examples=300)
@given(st.text(max_size=80))
def test_display_name_property(value: str) -> None:
    from reader.auth.handles import display_name

    out = display_name(value)
    if out is not None:
        assert 1 <= len(out) <= 64
        assert out == unicodedata.normalize("NFC", out)
        assert not any(unicodedata.category(ch) in REFUSED for ch in out)


HANDLES = st.from_regex(re.compile(r"[a-z][a-z0-9-]{2,31}"), fullmatch=True)


@settings(max_examples=300)
@given(HANDLES)
def test_accepted_handles_fit_the_kernel_service_rule(handle: str) -> None:
    """INV-AUTH-15: OPS-CLI FR-CLI-4 builds `kernel-user-<handle>` from `[a-z0-9][a-z0-9-]*`, at most 50."""
    from reader.auth.handles import handle_candidate_ok

    if handle_candidate_ok(handle):
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", handle) and len(handle) <= 50


# FR-AUTH-31 — the pages' static rules ---------------------------------------------------------------------


def test_signup_page_static_rules() -> None:
    html = (STATIC / "signup.html").read_text(encoding="utf-8")
    assert re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S | re.I) == [""]
    assert html.count('<link rel="stylesheet" href="/api/auth/static/auth.css">') == 1
    assert " style=" not in html and "<style" not in html.lower()
    assert not re.search(r"\son[a-z]+=", html, re.I)
    assert 'data-ceremony="signup"' in html and 'data-state="loading"' in html
    form = re.search(r"<form\b[^>]*>", html)
    assert form and "novalidate" in form.group(0) and "action=" not in form.group(0)
    for needle in [
        'id="handle"',
        'autocomplete="username"',
        'autocapitalize="none"',
        'spellcheck="false"',
        'maxlength="32"',
        'id="display-name"',
        'autocomplete="name"',
        'maxlength="64"',
        "Create an account",
        "Choose a handle and create a passkey. The owner of this reader-ai approves new accounts.",
        "Create account with a passkey",
        'href="/api/auth/signin"',
        "Already have an account?",
    ]:
        assert needle in html, needle
    button = re.search(r"<button\b[^>]*id=\"go\"[^>]*>", html)
    assert button and "hidden" in button.group(0) and "disabled" in button.group(0)


def test_signin_page_carries_one_marker_and_the_fragment_is_fixed() -> None:
    from reader.auth.routes import SIGNIN_MARKER, signin_page

    raw = (STATIC / "signin.html").read_text(encoding="utf-8")
    assert raw.count(SIGNIN_MARKER) == 1
    opened = signin_page("open").decode("utf-8")
    closed = signin_page("off").decode("utf-8")
    assert SIGNIN_MARKER not in opened and SIGNIN_MARKER not in closed
    assert opened.count('href="/api/auth/signup"') == 1 and "New here? Create an account" in opened
    assert 'href="/api/auth/signup"' not in closed
    assert closed == raw.replace(SIGNIN_MARKER, "")


def test_passkey_script_next_is_a_raw_comparison_to_one_constant() -> None:
    js = (STATIC / "passkey.js").read_text(encoding="utf-8")
    assert 'location.search === "?next=/signups"' in js
    assert "URLSearchParams" not in js and "decodeURIComponent" not in js
    # The only argument of location.assign is a constant or the allow-list constant.
    for arg in re.findall(r"location\.assign\(([^)]*)\)", js):
        assert arg.strip() in {'"/"', "target"}, arg
