"""SRV-AUTH policy values, failure reasons and startup validation of reader.yaml (FR-AUTH-3, FR-AUTH-16)."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal
from urllib.parse import urlsplit

from reader.config import ReaderConfig
from reader.config.effective import file_signup

CHALLENGE_BYTES: Final = 32
CHALLENGE_TTL_S: Final = 300
BOOTSTRAP_TOKEN_BYTES: Final = 32
BOOTSTRAP_TOKEN_TTL_S: Final = 900
SESSION_ID_BYTES: Final = 32
SESSION_TTL_S: Final = 1_209_600
RECENT_SESSION_S: Final = 600
FAILURE_MAX: Final = 100
FAILURE_WINDOW_S: Final = 3600
CLEANUP_INTERVAL_S: Final = 3600
ALGORITHMS: Final = (-7, -8, -257)
ATTESTATION_FORMATS: Final = ("none",)
SESSION_COOKIE: Final = "__Host-reader_session"
CEREMONY_COOKIE: Final = "__Host-reader_ceremony"
ROLES: Final = ("owner", "learner")

# Sign-up (ADR-0031, FR-AUTH-23..30; spec `policy.signup`).
SIGNUP_MAX: Final = 60
SIGNUP_WINDOW_S: Final = 3600
SIGNUP_FAILURE_MAX: Final = 100
SIGNUP_FAILURE_WINDOW_S: Final = 3600
PENDING_MAX: Final = 20
PENDING_TTL_S: Final = 604_800
DISPLAY_NAME_MAX: Final = 64
RESERVED_HANDLES: Final = frozenset(
    {
        "owner", "admin", "administrator", "root", "system", "support", "help", "security", "staff",
        "moderator", "reader", "reader-ai", "readerai", "api", "auth", "signin", "signup", "signout",
        "bootstrap", "register", "login", "logout", "mcp", "live-check", "null", "undefined", "anonymous",
        "me", "settings", "static", "assets", "signups",
    }
)
# pg_advisory_xact_lock(namespace, key) for sign-up ("RDAU"); not the migration namespace of reader.db.
AUTH_LOCK_NAMESPACE: Final = 0x52444155
SIGNUP_ATTEMPT_LOCK: Final = 2
SIGNUP_COMMIT_LOCK: Final = 3
SignupMode = Literal["open", "off"]


class FailureReason(StrEnum):
    malformed = "malformed"
    wrong_type = "wrong_type"
    unknown_credential = "unknown_credential"
    challenge_mismatch = "challenge_mismatch"
    origin_mismatch = "origin_mismatch"
    rp_id_mismatch = "rp_id_mismatch"
    user_present_missing = "user_present_missing"
    user_verified_missing = "user_verified_missing"
    bad_signature = "bad_signature"
    user_handle_missing = "user_handle_missing"
    user_handle_mismatch = "user_handle_mismatch"
    counter_regressed = "counter_regressed"
    attestation_format_refused = "attestation_format_refused"
    algorithm_refused = "algorithm_refused"
    credential_id_taken = "credential_id_taken"
    ceremony_invalid = "ceremony_invalid"
    token_invalid = "token_invalid"  # noqa: S105 (a failure reason name, not a credential)
    owner_has_credential = "owner_has_credential"
    user_disabled = "user_disabled"
    library_rejected = "library_rejected"


CONFIG_ERROR_CODES: Final = (
    "instance_url_path",
    "instance_url_query",
    "instance_url_userinfo",
    "instance_url_ip_host",
    "instance_url_host_form",
    "owner_count",
    "unknown_role",
    "signup_not_supported",
)


class StartupError(Exception):
    """A configuration that SRV-AUTH refuses at startup; carries one fixed code and no input values."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class UserEntry:
    handle: str
    role: str


@dataclass(frozen=True)
class Settings:
    rp_id: str
    origin: str
    users: tuple[UserEntry, ...]
    owner_handle: str
    signup: SignupMode = "off"
    # True when the key is absent on a host other than localhost (FR-AUTH-23): the api logs a startup note.
    signup_default_note: bool = False


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _url_problem(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.path not in ("", "/"):
        return "instance_url_path"
    if parts.query or parts.fragment or "?" in url or "#" in url:
        return "instance_url_query"
    if "@" in parts.netloc:
        return "instance_url_userinfo"
    host = parts.hostname or ""
    if _is_ip(host) or parts.netloc.startswith("["):
        return "instance_url_ip_host"
    if not host.isascii() or host.endswith(".") or _ends_in_a_number(host):
        return "instance_url_host_form"
    return None


def _ends_in_a_number(host: str) -> bool:
    """WHATWG URL host parsing treats a host whose last label is a number as IPv4 (`127.1`, `0x7f.1`)."""
    last = host.rsplit(".", 1)[-1].lower()
    if last.isdigit():
        return True
    return last.startswith("0x") and all(c in "0123456789abcdef" for c in last[2:])


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    scheme = parts.scheme.lower()
    port = parts.port
    default = {"https": 443, "http": 80}.get(scheme)
    origin = f"{scheme}://{host}" if port in (None, default) else f"{scheme}://{host}:{port}"
    return host, origin


def settings_from_config(config: ReaderConfig) -> Settings:
    """Validate the inputs SRV-AUTH reads; raise StartupError with the first code in table order."""
    problems: list[str] = []
    url_problem = _url_problem(config.instance.url)
    if url_problem:
        problems.append(url_problem)
    owners = [u for u in config.users if u.role == "owner"]
    if len(owners) != 1:
        problems.append("owner_count")
    if any(u.role not in ROLES for u in config.users):
        problems.append("unknown_role")
    # FR-AUTH-23 through SRV-CONFIG's overlay module, the only reader of `sharing.signup` (FR-CONFIG-26): an
    # absent key opens sign-up only on localhost ("this Mac only", ADR-0031); None is an unsupported value.
    mode, absent = file_signup(config)
    if mode is None:
        problems.append("signup_not_supported")
    if problems:
        raise StartupError(min(problems, key=CONFIG_ERROR_CODES.index))
    rp_id, origin = _origin(config.instance.url)
    users = tuple(UserEntry(u.handle, u.role) for u in config.users)
    return Settings(
        rp_id=rp_id,
        origin=origin,
        users=users,
        owner_handle=owners[0].handle,
        signup=mode or "off",
        signup_default_note=absent and rp_id != "localhost",
    )
