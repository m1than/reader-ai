"""SecretRef grammar.

@trace FR-CONFIG-5, VEC-CONFIG-3, INV-CONFIG-1
"""

import re

import pytest
from conftest import load_fixture
from hypothesis import given, settings
from hypothesis import strategies as st

REFS = load_fixture("secret-refs.yaml")
NBSP = chr(0xA0)


def oracle(text: str) -> bool:
    """An independent reading of FR-CONFIG-5, used as the property oracle."""
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        return False
    match = re.fullmatch(r"secret://(env|file|hostfile|keychain|op)/(.*)", text, re.DOTALL)
    if not match:
        return False
    scheme, rest = match.groups()
    parts = rest.split("/")

    def segment(p: str) -> bool:
        return p not in ("", ".", "..") and "%" not in p

    match scheme:
        case "env":
            return len(parts) == 1 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", parts[0]) is not None
        case "file":
            if len(parts) != 3 or parts[:2] != ["run", "secrets"]:
                return False
            return re.fullmatch(r"[A-Za-z0-9._-]+", parts[2]) is not None and parts[2] not in (".", "..")
        case "hostfile":
            return all(segment(p) for p in parts)
        case "keychain":
            return len(parts) == 2 and all(segment(p) for p in parts)
        case _:
            return len(parts) == 3 and all(segment(p) for p in parts)


@pytest.mark.parametrize("case", REFS["accept"], ids=[c["ref"] for c in REFS["accept"]])
def test_accepts(case: dict[str, str]) -> None:
    from reader.config import SecretRef

    ref = SecretRef.parse(case["ref"])
    assert ref == case["ref"]
    assert ref.scheme == case["scheme"]
    if "path" in case:
        assert ref.path == case["path"]


@pytest.mark.parametrize("case", REFS["reject"], ids=[repr(c["ref"]) for c in REFS["reject"]])
def test_rejects(case: dict[str, str]) -> None:
    from reader.config import SecretRef

    with pytest.raises(ValueError) as caught:
        SecretRef.parse(case["ref"])
    if case["ref"]:
        assert case["ref"] not in str(caught.value)
    with pytest.raises(ValueError):
        SecretRef(case["ref"])  # the constructor validates too


@pytest.mark.parametrize("text", ["garbage", "garbage\nINJECT", "secret://env/X\nINJECT"])
def test_constructor_never_makes_an_invalid_reference(text: str) -> None:
    from reader.config import SecretRef, secret_file_name

    with pytest.raises(ValueError):
        SecretRef(text)
    with pytest.raises(ValueError):
        secret_file_name(text)


_valid = st.sampled_from([c["ref"] for c in REFS["accept"]])
_mutation = st.sampled_from([
    lambda s: s.upper(), lambda s: s.replace("/", "//", 1), lambda s: s + "/", lambda s: s + "/..",
    lambda s: s.replace("secret://", "secret:/"), lambda s: s + "\n", lambda s: s + "\x00", lambda s: s + " ",
    lambda s: s + NBSP, lambda s: s + "%41", lambda s: s.rsplit("/", 1)[0], lambda s: s + "/x",
    lambda s: s.replace("/", "/./", 1), lambda s: " " + s, lambda s: s.replace("/", "\t/", 1),
])


@settings(max_examples=400, deadline=None)
@given(text=st.one_of(st.text(), _valid.flatmap(lambda v: _mutation.map(lambda m: m(v)))))
def test_accepts_iff_grammar_matches(text: str) -> None:
    from reader.config import SecretRef

    try:
        SecretRef.parse(text)
        accepted = True
    except ValueError:
        accepted = False
    assert accepted == oracle(text)
