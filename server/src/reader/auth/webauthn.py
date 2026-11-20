"""Verify seam (FR-AUTH-15): pure registration and authentication checks over py_webauthn 3.0.1.

Each function classifies a response with its own checks, in the spec's fixed order, over the library's
parsers, and then calls the library's `verify_*` as the final gate. Exceptions are classified by type,
never by message.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from webauthn import verify_authentication_response, verify_registration_response
from webauthn.helpers import (
    decode_credential_public_key,
    decoded_public_key_to_cryptography,
    parse_authentication_credential_json,
    parse_authenticator_data,
    parse_cbor,
    parse_client_data_json,
    parse_registration_credential_json,
    verify_signature,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticationCredential,
    AuthenticatorData,
    ClientDataType,
    CollectedClientData,
    RegistrationCredential,
)

from reader.auth.policy import ALGORITHMS, ATTESTATION_FORMATS, FailureReason

Response = str | dict[str, Any]


@dataclass(frozen=True)
class RegisteredCredential:
    credential_id: bytes
    public_key: bytes
    sign_count: int
    aaguid: str
    backed_up: bool
    transports: tuple[str, ...]


@dataclass(frozen=True)
class Authenticated:
    credential_id: bytes
    new_sign_count: int
    backed_up: bool


class _Fail(Exception):
    def __init__(self, reason: FailureReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


def _client_data(raw: bytes, kind: ClientDataType, challenge: bytes, origin: str) -> CollectedClientData:
    try:
        data = parse_client_data_json(raw)
    except (WebAuthnException, ValueError, TypeError, KeyError) as error:
        raise _Fail(FailureReason.malformed) from error
    if data.type != kind:
        raise _Fail(FailureReason.wrong_type)
    if not hmac.compare_digest(data.challenge, challenge):
        raise _Fail(FailureReason.challenge_mismatch)
    if data.origin != origin:
        raise _Fail(FailureReason.origin_mismatch)
    return data


def _flags(auth_data: AuthenticatorData, rp_id: str) -> None:
    if not hmac.compare_digest(auth_data.rp_id_hash, hashlib.sha256(rp_id.encode()).digest()):
        raise _Fail(FailureReason.rp_id_mismatch)
    if not auth_data.flags.up:
        raise _Fail(FailureReason.user_present_missing)
    if not auth_data.flags.uv:
        raise _Fail(FailureReason.user_verified_missing)


def _registration(rp_id: str, origin: str, challenge: bytes, response: Response) -> RegisteredCredential:
    try:
        credential: RegistrationCredential = parse_registration_credential_json(response)
    except (WebAuthnException, ValueError, TypeError, KeyError) as error:
        raise _Fail(FailureReason.malformed) from error
    _client_data(credential.response.client_data_json, ClientDataType.WEBAUTHN_CREATE, challenge, origin)
    try:
        raw = parse_cbor(credential.response.attestation_object)
    except (WebAuthnException, ValueError, TypeError) as error:
        raise _Fail(FailureReason.malformed) from error
    if not isinstance(raw, dict) or not isinstance(raw.get("authData"), bytes):
        raise _Fail(FailureReason.malformed)
    if raw.get("fmt") not in ATTESTATION_FORMATS:
        raise _Fail(FailureReason.attestation_format_refused)
    try:
        auth_data = parse_authenticator_data(raw["authData"])
    except (WebAuthnException, ValueError, TypeError) as error:
        raise _Fail(FailureReason.malformed) from error
    _flags(auth_data, rp_id)
    attested = auth_data.attested_credential_data
    if attested is None:
        raise _Fail(FailureReason.malformed)
    try:
        decoded = decode_credential_public_key(attested.credential_public_key)
    except (WebAuthnException, ValueError, TypeError, KeyError) as error:
        raise _Fail(FailureReason.malformed) from error
    if int(decoded.alg) not in ALGORITHMS:
        raise _Fail(FailureReason.algorithm_refused)
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            require_user_presence=True,
            require_user_verification=True,
            supported_pub_key_algs=[COSEAlgorithmIdentifier(a) for a in ALGORITHMS],
        )
    except (WebAuthnException, ValueError, TypeError, KeyError) as error:
        raise _Fail(FailureReason.library_rejected) from error
    transports = tuple(str(t.value) for t in (credential.response.transports or []))
    return RegisteredCredential(
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        aaguid=verified.aaguid,
        backed_up=verified.credential_backed_up,
        transports=transports,
    )


def _authentication(
    rp_id: str,
    origin: str,
    challenge: bytes,
    response: Response,
    public_key: bytes,
    stored_count: int,
    expected_user_handle: bytes,
) -> Authenticated:
    try:
        credential: AuthenticationCredential = parse_authentication_credential_json(response)
    except (WebAuthnException, ValueError, TypeError, KeyError) as error:
        raise _Fail(FailureReason.malformed) from error
    client_raw = credential.response.client_data_json
    _client_data(client_raw, ClientDataType.WEBAUTHN_GET, challenge, origin)
    auth_raw = credential.response.authenticator_data
    try:
        auth_data = parse_authenticator_data(auth_raw)
    except (WebAuthnException, ValueError, TypeError) as error:
        raise _Fail(FailureReason.malformed) from error
    _flags(auth_data, rp_id)
    handle = credential.response.user_handle
    if not handle:
        raise _Fail(FailureReason.user_handle_missing)
    if not hmac.compare_digest(handle, expected_user_handle):
        raise _Fail(FailureReason.user_handle_mismatch)
    try:
        decoded = decode_credential_public_key(public_key)
        key = decoded_public_key_to_cryptography(decoded)
    except (WebAuthnException, ValueError, TypeError, KeyError) as error:
        raise _Fail(FailureReason.malformed) from error
    try:
        verify_signature(
            public_key=key,
            signature_alg=decoded.alg,
            signature=credential.response.signature,
            data=auth_raw + hashlib.sha256(client_raw).digest(),
        )
    except InvalidSignature as error:
        raise _Fail(FailureReason.bad_signature) from error
    except (WebAuthnException, ValueError, TypeError) as error:
        raise _Fail(FailureReason.malformed) from error
    new = auth_data.sign_count
    if (new > 0 or stored_count > 0) and new <= stored_count:
        raise _Fail(FailureReason.counter_regressed)
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=public_key,
            credential_current_sign_count=stored_count,
            require_user_verification=True,
        )
    except (WebAuthnException, ValueError, TypeError, KeyError) as error:
        raise _Fail(FailureReason.library_rejected) from error
    return Authenticated(
        credential_id=verified.credential_id,
        new_sign_count=verified.new_sign_count,
        backed_up=verified.credential_backed_up,
    )


def verify_registration(
    rp_id: str, origin: str, challenge: bytes, response: Response
) -> RegisteredCredential | FailureReason:
    try:
        return _registration(rp_id, origin, challenge, response)
    except _Fail as failure:
        return failure.reason


def verify_authentication(
    rp_id: str,
    origin: str,
    challenge: bytes,
    response: Response,
    public_key: bytes,
    stored_count: int,
    expected_user_handle: bytes,
) -> Authenticated | FailureReason:
    try:
        return _authentication(
            rp_id, origin, challenge, response, public_key, stored_count, expected_user_handle
        )
    except _Fail as failure:
        return failure.reason
