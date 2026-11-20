"""SRV-AUTH verify seam (FR-AUTH-15): pure registration and authentication checks over py_webauthn 3.0.1.

@trace FR-AUTH-15 INV-AUTH-4 INV-AUTH-6 VEC-AUTH-1 VEC-AUTH-2
"""

from __future__ import annotations

import json
from typing import Any

import cbor2
import pytest
import softauthn
from auth_helpers import FIXTURES
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from softauthn import BE, BS, UP, UV, SoftAuthenticator, b64url, unb64url

RP, ORIGIN = "localhost", "https://localhost"
CHALLENGE = b"\x02" * 32
HANDLE = b"\x03" * 64


def _reg(response: dict[str, Any], *, challenge: bytes = CHALLENGE) -> Any:
    from reader.auth.webauthn import verify_registration

    return verify_registration(RP, ORIGIN, challenge, response)


def _auth(
    response: dict[str, Any], auth: SoftAuthenticator, *, stored: int = 0, handle: bytes = HANDLE
) -> Any:
    from reader.auth.webauthn import verify_authentication

    return verify_authentication(RP, ORIGIN, CHALLENGE, response, auth.cose_public_key(), stored, handle)


def _reason(name: str) -> Any:
    from reader.auth.policy import FailureReason

    return FailureReason(name)


def test_failure_reasons_match_the_spec() -> None:
    from auth_helpers import SPEC
    from ruamel.yaml import YAML

    from reader.auth.policy import FailureReason

    spec = YAML(typ="safe").load(SPEC.read_text(encoding="utf-8"))
    assert [r.value for r in FailureReason] == spec["failure_reasons"]


# VEC-AUTH-1 -----------------------------------------------------------------------------------------------


def test_vec1_fixture_is_regenerated_byte_for_byte() -> None:
    assert (FIXTURES / "softauthn-es256.json").read_text(encoding="utf-8") == softauthn.render()


def test_vec1_registration_and_authentication_pass() -> None:
    from reader.auth.webauthn import Authenticated, RegisteredCredential, verify_authentication

    vec = json.loads((FIXTURES / "softauthn-es256.json").read_text(encoding="utf-8"))
    registered = _reg(vec["registration"], challenge=unb64url(vec["challenge"]))
    assert isinstance(registered, RegisteredCredential)
    assert registered.credential_id == unb64url(vec["credential_id"])
    assert registered.public_key == unb64url(vec["public_key"])
    assert registered.sign_count == 0
    assert registered.backed_up is True
    authenticated = verify_authentication(
        vec["rp_id"],
        vec["origin"],
        unb64url(vec["challenge"]),
        vec["authentication"],
        registered.public_key,
        0,
        unb64url(vec["user_handle"]),
    )
    assert isinstance(authenticated, Authenticated)
    assert authenticated.new_sign_count == 0


# VEC-AUTH-2 -----------------------------------------------------------------------------------------------
_UPSTREAM = json.loads((FIXTURES / "py-webauthn-vectors.json").read_text(encoding="utf-8"))["vectors"]


@pytest.mark.parametrize("vec", _UPSTREAM, ids=[v["upstream"].rsplit("::", 1)[1] for v in _UPSTREAM])
def test_vec2_upstream_vectors_through_the_seam(vec: dict[str, Any]) -> None:
    from reader.auth.policy import FailureReason
    from reader.auth.webauthn import verify_authentication, verify_registration

    challenge = unb64url(vec["challenge"])
    if vec["kind"] == "registration":
        result = verify_registration(vec["rp_id"], vec["origin"], challenge, vec["credential"])
    else:
        handle = unb64url(vec["expected_user_handle"]) if vec["expected_user_handle"] else b"\x00"
        result = verify_authentication(
            vec["rp_id"],
            vec["origin"],
            challenge,
            vec["credential"],
            unb64url(vec["public_key"]),
            vec["stored_sign_count"],
            handle,
        )
    if vec["expected"] == "pass":
        assert not isinstance(result, FailureReason), result
    else:
        assert result == FailureReason(vec["expected"])


# INV-AUTH-4: single mutations, re-signed, each with its own reason -----------------------------------------
REGISTRATION_MUTATIONS = {
    "origin_mismatch": {"origin": "https://evil.example"},
    "rp_id_mismatch": {"rp_id": "evil.example"},
    "challenge_mismatch": {"challenge": b"\x09" * 32},
    "user_present_missing": {"flags": UV | BE | BS},
    "user_verified_missing": {"flags": UP | BE | BS},
    "wrong_type": {"kind": "webauthn.get"},
    "attestation_format_refused": {"fmt": "packed"},
    "algorithm_refused": {"alg": -35},
}

AUTHENTICATION_MUTATIONS = {
    "origin_mismatch": {"origin": "https://evil.example"},
    "rp_id_mismatch": {"rp_id": "evil.example"},
    "challenge_mismatch": {"challenge": b"\x09" * 32},
    "user_present_missing": {"flags": UV | BE | BS},
    "user_verified_missing": {"flags": UP | BE | BS},
    "wrong_type": {"kind": "webauthn.create"},
    "bad_signature": {"corrupt_signature": True},
    "user_handle_mismatch": {"user_handle": b"\x04" * 64},
    "user_handle_missing": {"user_handle": None},
}


def test_unmutated_responses_pass() -> None:
    from reader.auth.policy import FailureReason

    auth = SoftAuthenticator()
    assert not isinstance(_reg(auth.register()), FailureReason)
    assert not isinstance(_auth(auth.assertion(), auth), FailureReason)


@pytest.mark.parametrize(("reason", "mutation"), REGISTRATION_MUTATIONS.items())
def test_registration_mutation_fails_with_its_reason(reason: str, mutation: dict[str, Any]) -> None:
    assert _reg(SoftAuthenticator().register(**mutation)) == _reason(reason)


@pytest.mark.parametrize(("reason", "mutation"), AUTHENTICATION_MUTATIONS.items())
def test_authentication_mutation_fails_with_its_reason(reason: str, mutation: dict[str, Any]) -> None:
    auth = SoftAuthenticator()
    assert _auth(auth.assertion(**mutation), auth) == _reason(reason)


def test_malformed_input_is_classified() -> None:
    auth = SoftAuthenticator()
    assert _reg({"id": "x"}) == _reason("malformed")
    assert _auth({"id": "x", "response": {}}, auth) == _reason("malformed")


def test_uv_is_checked_before_user_handle() -> None:
    auth = SoftAuthenticator()
    assert _auth(auth.assertion(flags=UP, user_handle=None), auth) == _reason("user_verified_missing")


def test_wrong_public_key_is_bad_signature() -> None:
    from reader.auth.webauthn import verify_authentication

    signer, other = SoftAuthenticator(), SoftAuthenticator(scalar=b"\x05" * 32)
    result = verify_authentication(
        RP, ORIGIN, CHALLENGE, signer.assertion(), other.cose_public_key(), 0, HANDLE
    )
    assert result == _reason("bad_signature")


# Keys that can never verify are refused at registration; broken COSE is malformed at both ceremonies.

_ONE = b"\x01" * 32
UNUSABLE_KEYS = {
    "off-curve EC2 point": cbor2.dumps({1: 2, 3: -7, -1: 1, -2: _ONE, -3: _ONE}),
    "unknown curve 99": cbor2.dumps({1: 2, 3: -7, -1: 99, -2: _ONE, -3: _ONE}),
    "short OKP": cbor2.dumps({1: 1, 3: -8, -1: 6, -2: b"\x01" * 5}),
    "short RSA": cbor2.dumps({1: 3, 3: -257, -1: b"\xc3" * 64, -2: b"\x01\x00\x01"}),
    "COSE alg is text": cbor2.dumps({1: 2, 3: "x", -1: 1, -2: _ONE, -3: _ONE}),
    "COSE key is a list": cbor2.dumps([1]),
    "not CBOR": b"\xff\xff",
}


@pytest.mark.parametrize("key", UNUSABLE_KEYS.values(), ids=UNUSABLE_KEYS.keys())
def test_unusable_keys_are_malformed(key: bytes) -> None:
    from reader.auth.webauthn import verify_authentication

    auth = SoftAuthenticator()
    assert _reg(auth.register(cose_key=key)) == _reason("malformed")
    assert verify_authentication(RP, ORIGIN, CHALLENGE, auth.assertion(), key, 0, HANDLE) == _reason(
        "malformed"
    )


# Totality: the seam returns for any input and never raises.

_JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=12), inner, max_size=4),
    max_leaves=12,
)
_CBOR = st.recursive(
    st.none() | st.booleans() | st.integers() | st.binary(max_size=40) | st.text(max_size=12),
    lambda inner: (
        st.lists(inner, max_size=4) | st.dictionaries(st.integers() | st.text(max_size=8), inner, max_size=5)
    ),
    max_leaves=12,
)
_B64 = st.binary(max_size=300).map(b64url)


def _shaped(response_fields: dict[str, Any]) -> dict[str, Any]:
    return {"id": "AAAA", "rawId": "AAAA", "type": "public-key", "response": response_fields}


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    raw=_JSON,
    client=_B64,
    att=st.one_of(_B64, _CBOR.map(lambda v: b64url(cbor2.dumps(v)))),
    auth_data=st.binary(max_size=300),
    key=st.one_of(st.binary(max_size=200), _CBOR.map(cbor2.dumps)),
)
def test_registration_never_raises(raw: Any, client: str, att: str, auth_data: bytes, key: bytes) -> None:
    from reader.auth.webauthn import verify_registration

    auth = SoftAuthenticator()
    good = auth.register()
    hostile_att = b64url(cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data}))
    for response in [
        raw,
        _shaped({"clientDataJSON": client, "attestationObject": att}),
        {**good, "response": {**good["response"], "attestationObject": att}},
        {**good, "response": {**good["response"], "attestationObject": hostile_att}},
        auth.register(cose_key=key),
    ]:
        verify_registration(RP, ORIGIN, CHALLENGE, response)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    raw=_JSON,
    fields=st.fixed_dictionaries(
        {},
        optional={
            "clientDataJSON": _B64,
            "authenticatorData": _B64,
            "signature": _B64,
            "userHandle": _B64,
        },
    ),
    key=st.one_of(st.binary(max_size=200), _CBOR.map(cbor2.dumps)),
    stored=st.integers(min_value=0, max_value=2**32),
)
def test_authentication_never_raises(raw: Any, fields: dict[str, str], key: bytes, stored: int) -> None:
    from reader.auth.webauthn import verify_authentication

    auth = SoftAuthenticator()
    good = auth.assertion()
    for response in [raw, _shaped(fields), {**good, "response": {**good["response"], **fields}}]:
        verify_authentication(RP, ORIGIN, CHALLENGE, response, key, stored, HANDLE)
        verify_authentication(RP, ORIGIN, CHALLENGE, response, auth.cose_public_key(), stored, HANDLE)


# INV-AUTH-6: counter_cases, one test per row -------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "received", "passes"),
    [(0, 0, True), (0, 5, True), (7, 8, True), (7, 7, False), (7, 3, False), (7, 0, False)],
    ids=["0/0", "0/n>0", "m/n>m", "m/n=m", "m/n<m", "m/0"],
)
def test_counter_cases(stored: int, received: int, passes: bool) -> None:
    from reader.auth.policy import FailureReason

    auth = SoftAuthenticator()
    result = _auth(auth.assertion(sign_count=received), auth, stored=stored)
    if passes:
        assert not isinstance(result, FailureReason), result
        assert result.new_sign_count == received
    else:
        assert result == FailureReason.counter_regressed
