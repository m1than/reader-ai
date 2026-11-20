"""Throwaway WebAuthn relying party for the passkey-localhost spike (never shipped).

RP ID `localhost`, origin from READER_ORIGIN (default https://localhost). Discoverable credentials,
user verification required, in-memory storage. Every step is appended to /data/log.jsonl.
"""

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

RP_ID = "localhost"
ORIGIN = os.environ.get("READER_ORIGIN", "https://localhost")
LOG = Path("/data/log.jsonl")
HERE = Path(__file__).parent

app = FastAPI()
pending: dict[str, bytes] = {}  # flow id -> challenge
credentials: dict[str, dict[str, Any]] = {}  # credential id (base64url) -> public key, sign count, user


def log(event: str, **fields: Any) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps({"t": round(time.time(), 3), "event": event, **fields}) + "\n")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(HERE / "index.html")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "rp_id": RP_ID, "origin": ORIGIN, "credentials": len(credentials)}


@app.post("/register/options")
async def register_options(request: Request) -> JSONResponse:
    body = await request.json()
    user = str(body.get("user") or "owner")[:64]
    opts = generate_registration_options(
        rp_id=RP_ID, rp_name="reader-ai spike", user_name=user, user_display_name=user,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    flow = secrets.token_urlsafe(16)
    pending[flow] = opts.challenge
    log("register.options", flow=flow, user=user, ua=request.headers.get("user-agent", ""))
    return JSONResponse({"flow": flow, "publicKey": json.loads(options_to_json(opts))})


@app.post("/register/verify")
async def register_verify(request: Request) -> JSONResponse:
    body = await request.json()
    flow, cred = body.get("flow", ""), body.get("credential", {})
    try:
        v = verify_registration_response(
            credential=cred, expected_challenge=pending.pop(flow), expected_rp_id=RP_ID,
            expected_origin=ORIGIN, require_user_verification=True,
        )
    except Exception as exc:  # spike: record every failure reason
        log("register.fail", flow=flow, error=f"{type(exc).__name__}: {exc}", ua=request.headers.get("user-agent", ""))
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)
    cid = bytes_to_base64url(v.credential_id)
    credentials[cid] = {"public_key": v.credential_public_key, "sign_count": v.sign_count, "user": body.get("user")}
    fields = {"flow": flow, "credential_id": cid, "aaguid": v.aaguid, "fmt": str(v.fmt),
              "device_type": str(v.credential_device_type), "backed_up": v.credential_backed_up,
              "uv": v.user_verified, "ua": request.headers.get("user-agent", "")}
    log("register.ok", **fields)
    return JSONResponse({"ok": True, **fields})


@app.post("/login/options")
async def login_options(request: Request) -> JSONResponse:
    opts = generate_authentication_options(rp_id=RP_ID, user_verification=UserVerificationRequirement.REQUIRED)
    flow = secrets.token_urlsafe(16)
    pending[flow] = opts.challenge
    log("login.options", flow=flow, ua=request.headers.get("user-agent", ""))
    return JSONResponse({"flow": flow, "publicKey": json.loads(options_to_json(opts))})


@app.post("/login/verify")
async def login_verify(request: Request) -> JSONResponse:
    body = await request.json()
    flow, cred = body.get("flow", ""), body.get("credential", {})
    stored = credentials.get(cred.get("id", ""))
    try:
        if stored is None:
            raise LookupError("unknown credential id (in-memory store: was the app restarted?)")
        v = verify_authentication_response(
            credential=cred, expected_challenge=pending.pop(flow), expected_rp_id=RP_ID, expected_origin=ORIGIN,
            credential_public_key=stored["public_key"], credential_current_sign_count=stored["sign_count"],
            require_user_verification=True,
        )
    except Exception as exc:
        log("login.fail", flow=flow, error=f"{type(exc).__name__}: {exc}", ua=request.headers.get("user-agent", ""))
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)
    stored["sign_count"] = v.new_sign_count
    fields = {"flow": flow, "credential_id": cred["id"], "user": stored["user"], "uv": v.user_verified,
              "sign_count": v.new_sign_count, "ua": request.headers.get("user-agent", "")}
    log("login.ok", **fields)
    return JSONResponse({"ok": True, **fields})
