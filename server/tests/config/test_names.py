"""File names under /run/secrets for references (ADR-0026).

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
    from reader.config import SecretRef, secret_file_name

    assert secret_file_name(SecretRef.parse(case["ref"])) == case["name"]


@pytest.mark.parametrize("ref", NAMES["value_error"])
def test_file_reference_has_no_derived_name(ref: str) -> None:
    from reader.config import SecretRef, secret_file_name

    with pytest.raises(ValueError):
        secret_file_name(SecretRef.parse(ref))


def test_env_var_mapping_is_gone() -> None:
    import reader.config

    assert not hasattr(reader.config, "secret_env_var")


_seg = st.text(alphabet="abcXYZ019-_.", min_size=1, max_size=8).filter(lambda s: s not in (".", ".."))


@given(scheme=st.sampled_from(["keychain", "op", "hostfile"]), parts=st.lists(_seg, min_size=3, max_size=3))
def test_names_are_deterministic_and_well_formed(scheme: str, parts: list[str]) -> None:
    from reader.config import SecretRef, secret_file_name

    text = {"keychain": f"secret://keychain/{parts[0]}/{parts[1]}",
            "op": f"secret://op/{parts[0]}/{parts[1]}/{parts[2]}",
            "hostfile": f"secret://hostfile/{'/'.join(parts)}"}[scheme]
    ref = SecretRef.parse(text)
    assert secret_file_name(ref) == secret_file_name(SecretRef.parse(text))
    assert re.fullmatch(r"(env|hostfile|keychain|op)-[0-9a-f]{32}", secret_file_name(ref))
