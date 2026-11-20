"""Software WebAuthn authenticator for the SRV-AUTH tests, and the generator of VEC-AUTH-1.

@trace VEC-AUTH-1 INV-AUTH-4

It builds browser-shaped JSON (`PublicKeyCredential.toJSON()`) for a registration with `none` attestation
and for an assertion, from a P-256 key. Signatures are deterministic (RFC 6979), so the fixture regenerates
byte for byte.
Every field the verify seam checks can be overridden, and the authenticator re-signs after the override, so a
mutation makes only that field wrong (spec INV-AUTH-4).

Regenerate the fixture: `cd server && uv run python tests/auth/softauthn.py`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

UP, UV, BE, BS, AT = 0x01, 0x04, 0x08, 0x10, 0x40
DEFAULT_FLAGS = UP | UV | BE | BS
FIXTURE = Path(__file__).resolve().parents[3] / "spec" / "fixtures" / "SRV-AUTH" / "softauthn-es256.json"

# VEC-AUTH-1 inputs (spec/modules/SRV-AUTH.yaml).
VEC_SCALAR = b"\x01" * 32
VEC_RP_ID = "localhost"
VEC_ORIGIN = "https://localhost"
VEC_CHALLENGE = b"\x02" * 32
VEC_USER_HANDLE = b"\x03" * 64
VEC_AAGUID = b"\x00" * 16


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass
class SoftAuthenticator:
    scalar: bytes = VEC_SCALAR
    aaguid: bytes = VEC_AAGUID

    def __post_init__(self) -> None:
        self.key = ec.derive_private_key(int.from_bytes(self.scalar, "big"), ec.SECP256R1())
        numbers = self.key.public_key().public_numbers()
        self.x = numbers.x.to_bytes(32, "big")
        self.y = numbers.y.to_bytes(32, "big")
        self.credential_id = hashlib.sha256(b"\x04" + self.x + self.y).digest()

    @classmethod
    def random(cls) -> SoftAuthenticator:
        import secrets

        while True:
            scalar = secrets.token_bytes(32)
            if (
                0
                < int.from_bytes(scalar, "big")
                < 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
            ):
                return cls(scalar=scalar)

    def cose_public_key(self, alg: int = -7) -> bytes:
        return cbor2.dumps({1: 2, 3: alg, -1: 1, -2: self.x, -3: self.y})

    def _sign(self, data: bytes) -> bytes:
        return self.key.sign(data, ec.ECDSA(hashes.SHA256(), deterministic_signing=True))

    @staticmethod
    def _client_data(kind: str, challenge: bytes, origin: str) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": b64url(challenge), "origin": origin, "crossOrigin": False},
            separators=(",", ":"),
        ).encode()

    def register(
        self,
        *,
        rp_id: str = VEC_RP_ID,
        origin: str = VEC_ORIGIN,
        challenge: bytes = VEC_CHALLENGE,
        flags: int = DEFAULT_FLAGS,
        sign_count: int = 0,
        kind: str = "webauthn.create",
        fmt: str = "none",
        alg: int = -7,
        credential_id: bytes | None = None,
    ) -> dict[str, Any]:
        cred_id = self.credential_id if credential_id is None else credential_id
        attested = self.aaguid + struct.pack(">H", len(cred_id)) + cred_id + self.cose_public_key(alg)
        auth_data = (
            hashlib.sha256(rp_id.encode()).digest() + bytes([flags | AT]) + struct.pack(">I", sign_count)
        )
        auth_data += attested
        att_obj = cbor2.dumps({"fmt": fmt, "attStmt": {}, "authData": auth_data})
        return {
            "id": b64url(cred_id),
            "rawId": b64url(cred_id),
            "response": {
                "clientDataJSON": b64url(self._client_data(kind, challenge, origin)),
                "attestationObject": b64url(att_obj),
                "transports": ["hybrid", "internal"],
            },
            "type": "public-key",
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }

    def assertion(
        self,
        *,
        rp_id: str = VEC_RP_ID,
        origin: str = VEC_ORIGIN,
        challenge: bytes = VEC_CHALLENGE,
        user_handle: bytes | None = VEC_USER_HANDLE,
        flags: int = DEFAULT_FLAGS,
        sign_count: int = 0,
        kind: str = "webauthn.get",
        corrupt_signature: bool = False,
    ) -> dict[str, Any]:
        auth_data = hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", sign_count)
        client_data = self._client_data(kind, challenge, origin)
        signature = self._sign(auth_data + hashlib.sha256(client_data).digest())
        if corrupt_signature:
            signature = signature[:-1] + bytes([signature[-1] ^ 0x01])
        response: dict[str, Any] = {
            "authenticatorData": b64url(auth_data),
            "clientDataJSON": b64url(client_data),
            "signature": b64url(signature),
        }
        if user_handle is not None:
            response["userHandle"] = b64url(user_handle)
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "response": response,
            "type": "public-key",
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }


def vector() -> dict[str, Any]:
    """The VEC-AUTH-1 fixture content."""
    auth = SoftAuthenticator()
    return {
        "source": "server/tests/auth/softauthn.py (spec/modules/SRV-AUTH.yaml VEC-AUTH-1)",
        "rp_id": VEC_RP_ID,
        "origin": VEC_ORIGIN,
        "challenge": b64url(VEC_CHALLENGE),
        "user_handle": b64url(VEC_USER_HANDLE),
        "credential_id": b64url(auth.credential_id),
        "public_key": b64url(auth.cose_public_key()),
        "registration": auth.register(),
        "authentication": auth.assertion(),
    }


def render() -> str:
    return json.dumps(vector(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(render(), encoding="utf-8")
    sys.stdout.write(f"wrote {FIXTURE}\n")
