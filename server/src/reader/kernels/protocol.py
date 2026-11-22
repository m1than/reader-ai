"""Jupyter messaging rules of the relay, as data and pure functions (SRV-KERNEL FR-KERNEL-1, -8, -11, -12).

Browser → kernel messages are validated, rebuilt from five keys and rewritten (`inbound`); kernel → browser
messages are rebuilt and filtered (`outbound`). The relay adds `reader_notice` messages of its own (`notice`).
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Final

MiB: Final = 1024 * 1024

# FR-KERNEL-8: what passes, per direction.
SHELL_REQUESTS: Final = frozenset(
    {"execute_request", "kernel_info_request", "complete_request", "inspect_request", "is_complete_request"}
)
SHELL_REPLIES: Final = frozenset(
    {"execute_reply", "kernel_info_reply", "complete_reply", "inspect_reply", "is_complete_reply"}
)
IOPUB_TYPES: Final = frozenset(
    {
        "status", "execute_input", "stream", "display_data", "update_display_data", "execute_result", "error",
        "clear_output",
    }
)
KEYS: Final = ("channel", "header", "parent_header", "metadata", "content")

# FR-KERNEL-11
NOTICE_CODES: Final = frozenset(
    {
        "rejected", "busy", "timeout", "output_truncated", "kernel_restarted", "kernel_dead", "kernel_reset",
        "kernel_unavailable",
    }
)

# Structural bounds (FR-KERNEL-8, -10, -12; reasons in the spec).
MSG_ID_MAX: Final = 128
BROWSER_FRAME_MAX: Final = 1 * MiB
UPSTREAM_MESSAGE_MAX: Final = 4 * MiB
EXEC_OUTPUT_MAX: Final = 16 * MiB
SEND_BUFFER_MAX: Final = 16 * MiB
QUEUE_MAX: Final = 8
PENDING_OTHER_MAX: Final = 32
EXEC_TIMEOUT_S: Final = 60.0
INTERRUPT_GRACE_S: Final = 5.0

KERNEL_ID: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
LESSON_ID: Final = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
SESSION_ID: Final = re.compile(r"^[0-9a-f]{32}$")

# REF-KERNEL-1 content field types per request type (fields not listed are passed through as received).
_CONTENT_TYPES: Final[dict[str, dict[str, tuple[type, ...]]]] = {
    "execute_request": {
        "code": (str,), "silent": (bool,), "store_history": (bool,), "stop_on_error": (bool,),
    },
    "kernel_info_request": {},
    "complete_request": {"code": (str,), "cursor_pos": (int,)},
    "inspect_request": {"code": (str,), "cursor_pos": (int,), "detail_level": (int,)},
    "is_complete_request": {"code": (str,)},
}
_REQUIRED: Final[dict[str, tuple[str, ...]]] = {
    "execute_request": ("code",),
    "kernel_info_request": (),
    "complete_request": ("code", "cursor_pos"),
    "inspect_request": ("code", "cursor_pos"),
    "is_complete_request": ("code",),
}


def service_name(handle: str) -> str:
    """FR-KERNEL-1: the upstream service of a handle, always with the `kernel-user-` prefix."""
    return f"kernel-user-{handle}"


class Rejected(Exception):
    """A browser frame that is not forwarded; `msg_id` is set when one parsed (FR-KERNEL-8)."""

    def __init__(self, msg_id: str | None, reason: str) -> None:
        super().__init__(reason)
        self.msg_id = msg_id
        self.reason = reason


def _msg_id(header: Any) -> str | None:
    if isinstance(header, dict):
        value = header.get("msg_id")
        if isinstance(value, str) and 1 <= len(value) <= MSG_ID_MAX:
            return value
    return None


def inbound(
    frame: str, *, session_id: str, handle: str, pending: set[str] | frozenset[str]
) -> dict[str, Any]:
    """Validate, rebuild and rewrite one browser text frame (FR-KERNEL-8, INV-KERNEL-2). Raises Rejected."""
    try:
        raw = json.loads(frame)
    except (ValueError, RecursionError):
        raise Rejected(None, "not_json") from None
    if not isinstance(raw, dict):
        raise Rejected(None, "not_object")
    header = raw.get("header")
    msg_id = _msg_id(header)
    if not isinstance(header, dict) or msg_id is None:
        raise Rejected(None, "msg_id")
    if msg_id in pending:
        raise Rejected(msg_id, "duplicate_msg_id")
    msg_type = header.get("msg_type")
    if raw.get("channel") != "shell" or msg_type not in SHELL_REQUESTS:
        raise Rejected(msg_id, "type")
    if raw.get("parent_header") != {}:
        raise Rejected(msg_id, "parent_header")
    if raw.get("buffers") not in (None, []):
        raise Rejected(msg_id, "buffers")
    metadata = raw.get("metadata", {})
    content = raw.get("content")
    if not isinstance(metadata, dict) or not isinstance(content, dict):
        raise Rejected(msg_id, "shape")
    types = _CONTENT_TYPES[str(msg_type)]
    for name in _REQUIRED[str(msg_type)]:
        if name not in content:
            raise Rejected(msg_id, "content")
    for name, allowed in types.items():
        if name not in content:
            continue
        value = content[name]
        if not isinstance(value, allowed) or (bool not in allowed and isinstance(value, bool)):
            raise Rejected(msg_id, "content")
    new_header = {k: v for k, v in header.items() if k in ("msg_id", "msg_type", "version", "date")}
    new_header["session"] = session_id
    new_header["username"] = handle
    new_content = dict(content)
    if msg_type == "execute_request":
        new_content["allow_stdin"] = False
        new_content["user_expressions"] = {}
    return {
        "channel": "shell",
        "header": new_header,
        "parent_header": {},
        "metadata": dict(metadata),
        "content": new_content,
    }


def outbound(frame: str | bytes) -> dict[str, Any] | None:
    """Rebuild and filter one kernel message (FR-KERNEL-8, INV-KERNEL-3); None means drop."""
    if not isinstance(frame, str):
        return None
    try:
        raw = json.loads(frame)
    except (ValueError, RecursionError):
        return None
    if not isinstance(raw, dict):
        return None
    channel, header = raw.get("channel"), raw.get("header")
    if not isinstance(header, dict):
        return None
    msg_type = header.get("msg_type")
    shell = channel == "shell" and msg_type in SHELL_REPLIES
    iopub = channel == "iopub" and msg_type in IOPUB_TYPES
    if not (shell or iopub):
        return None
    parent = raw.get("parent_header", {})
    metadata = raw.get("metadata", {})
    content = raw.get("content", {})
    if not all(isinstance(x, dict) for x in (parent, metadata, content)):
        return None
    return {
        "channel": channel,
        "header": header,
        "parent_header": parent,
        "metadata": metadata,
        "content": content,
    }


def message_type(frame: str | bytes) -> str:
    """A loggable type label for a dropped frame (never its content)."""
    if not isinstance(frame, str):
        return "binary"
    try:
        raw = json.loads(frame)
    except (ValueError, RecursionError):
        return "unparseable"
    if isinstance(raw, dict) and isinstance(raw.get("header"), dict):
        value = raw["header"].get("msg_type")
        if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,40}", value):
            return f"{raw.get('channel') if isinstance(raw.get('channel'), str) else '?'}:{value}"[:60]
    return "malformed"


def parent_id(message: dict[str, Any]) -> str | None:
    value = message.get("parent_header", {}).get("msg_id")
    return value if isinstance(value, str) else None


def notice(code: str, parent_msg_id: str | None = None) -> dict[str, Any]:
    """FR-KERNEL-11: a relay notice."""
    if code not in NOTICE_CODES:
        raise ValueError(code)
    return {
        "channel": "reader",
        "header": {"msg_type": "reader_notice", "msg_id": uuid.uuid4().hex},
        "parent_header": {},
        "content": {"code": code, "parent_msg_id": parent_msg_id},
    }
