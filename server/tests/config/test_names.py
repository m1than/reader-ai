"""Compose secret names and environment variables for references.

@trace FR-CONFIG-14, VEC-CONFIG-5, INV-CONFIG-6
"""

import re

import pytest
from conftest import load_fixture
from hypothesis import given
from hypothesis import strategies as st

NAMES = load_fixture("secret-names.yaml")


@pytest.mark.parametrize("case", NAMES["names"], ids=[c["ref"] for c in NAMES["names"]])
def test_fixed_names(case: dict[str, str]) -> None:
    from reader.config import SecretRef, secret_env_var, secret_file_name

    ref = SecretRef.parse(case["ref"])
    assert secret_file_name(ref) == case["name"]
    assert secret_env_var(ref) == case["env"]


@pytest.mark.parametrize("ref", NAMES["value_error"])
def test_file_reference_has_no_derived_name(ref: str) -> None:
    from reader.config import SecretRef, secret_env_var, secret_file_name

    parsed = SecretRef.parse(ref)
    with pytest.raises(ValueError):
        secret_file_name(parsed)
    with pytest.raises(ValueError):
        secret_env_var(parsed)


_seg = st.text(alphabet="abcXYZ019-_.", min_size=1, max_size=8).filter(lambda s: s not in (".", ".."))


@given(scheme=st.sampled_from(["keychain", "op", "hostfile"]), parts=st.lists(_seg, min_size=3, max_size=3))
def test_names_are_deterministic_and_well_formed(scheme: str, parts: list[str]) -> None:
    from reader.config import SecretRef, secret_env_var, secret_file_name

    text = {"keychain": f"secret://keychain/{parts[0]}/{parts[1]}",
            "op": f"secret://op/{parts[0]}/{parts[1]}/{parts[2]}",
            "hostfile": f"secret://hostfile/{'/'.join(parts)}"}[scheme]
    ref = SecretRef.parse(text)
    assert secret_file_name(ref) == secret_file_name(SecretRef.parse(text))
    assert re.fullmatch(r"(env|hostfile|keychain|op)-[0-9a-f]{32}", secret_file_name(ref))
    assert re.fullmatch(r"READER_SECRET_(ENV|HOSTFILE|KEYCHAIN|OP)_[0-9A-F]{32}", secret_env_var(ref))
    assert secret_file_name(ref).split("-", 1)[1].upper() == secret_env_var(ref).rsplit("_", 1)[1]
