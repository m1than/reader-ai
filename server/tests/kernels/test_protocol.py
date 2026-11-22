# @trace FR-KERNEL-1, FR-KERNEL-8, FR-KERNEL-11, INV-KERNEL-2, INV-KERNEL-3, VEC-KERNEL-1, VEC-KERNEL-3
"""SRV-KERNEL message rules as pure functions: the filter table (VEC-KERNEL-1), rebuilding from five keys,
rewrites, msg_id rules, notices, and the handle → service mapping (VEC-KERNEL-3)."""

from __future__ import annotations

import json
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from .conftest import execute, fixture, kernel_msg, load

KEYS = {"channel", "header", "parent_header", "metadata", "content"}
VALID_CONTENT: dict[str, dict[str, Any]] = {
    "complete_request": {"code": "x", "cursor_pos": 1},
    "inspect_request": {"code": "x", "cursor_pos": 1},
    "is_complete_request": {"code": "x"},
}


def _inbound(frame: Any, pending: frozenset[str] = frozenset()) -> Any:
    protocol = load("reader.kernels.protocol")
    text = frame if isinstance(frame, str) else json.dumps(frame)
    return protocol.inbound(text, session_id="s" * 32, handle="ivan", pending=pending)


def _rejected(frame: Any, pending: frozenset[str] = frozenset()) -> Any:
    protocol = load("reader.kernels.protocol")
    try:
        _inbound(frame, pending)
    except protocol.Rejected as r:
        return r
    raise AssertionError(f"not rejected: {frame!r}")


def test_service_names_never_reach_pool_services() -> None:
    protocol = load("reader.kernels.protocol")
    for case in fixture("naming.yaml")["cases"]:
        assert protocol.service_name(case["handle"]) == case["service"]
        if "never" in case:
            assert protocol.service_name(case["handle"]) != case["never"]
    upstream = load("reader.kernels.upstream")
    ep = upstream.default_resolve("ivan")
    assert (ep.host, ep.gateway_port, ep.control_port) == ("kernel-user-ivan", 8888, 8889)


def _browser_frame(row: dict[str, Any]) -> dict[str, Any]:
    msg = execute("m1")
    msg["channel"] = row["channel"]
    msg["header"]["msg_type"] = row["msg_type"]
    if row["msg_type"] != "execute_request":
        msg["content"] = VALID_CONTENT.get(row["msg_type"], {})
    for key in ("parent_header", "buffers"):
        if key in row:
            msg[key] = row[key]
    if "content" in row:
        msg["content"] = row["content"]
    if "header" in row:
        msg["header"].update(row["header"])
        if msg["header"]["msg_id"] == "129 characters":   # the fixture describes the value
            msg["header"]["msg_id"] = "x" * 129
    if "extra_keys" in row:
        msg.update(row["extra_keys"])
    return msg


def test_browser_to_kernel_table() -> None:
    table = fixture("filter.yaml")["browser_to_kernel"]
    for row in table:
        if "frame" in row or "<id of a pending request>" in str(row.get("header", "")):
            continue
        frame = _browser_frame(row)
        if row["action"] == "reject":
            _rejected(frame)
        else:
            out = _inbound(frame)
            assert set(out) == KEYS, row
            assert out["header"]["session"] == "s" * 32
            assert out["header"]["username"] == "ivan"
            if row["msg_type"] == "execute_request":
                assert out["content"]["allow_stdin"] is False
                assert out["content"]["user_expressions"] == {}


def test_browser_frames_that_are_not_messages() -> None:
    assert _rejected("not json").msg_id is None
    assert _rejected("[1, 2]").msg_id is None
    over = execute("x" * 129)
    assert _rejected(over).msg_id is None
    assert _rejected(execute(7)).msg_id is None  # type: ignore[arg-type]
    dup = _rejected(execute("m1"), pending=frozenset({"m1"}))
    assert dup.msg_id == "m1"


def test_example_exchange_rewrites() -> None:
    ex = fixture("filter.yaml")["example_exchange"]
    sent = ex["browser_sends"][0]
    out = _inbound(sent)
    expect = ex["kernel_receives"][0]
    assert out["header"]["msg_id"] == expect["header"]["msg_id"]
    assert out["content"] == expect["content"]
    protocol = load("reader.kernels.protocol")
    received = []
    for k in ex["kernel_sends"]:
        msg = kernel_msg(k["channel"], k["msg_type"], k["parent_msg_id"], k["content"])
        rebuilt = protocol.outbound(json.dumps(msg))
        if rebuilt is not None:
            received.append(rebuilt["header"]["msg_type"])
    assert received == ex["browser_receives_types"]


def test_kernel_to_browser_table() -> None:
    protocol = load("reader.kernels.protocol")
    for row in fixture("filter.yaml")["kernel_to_browser"]:
        if "frame" in row or "message" in row:
            continue
        msg = kernel_msg(row["channel"], row["msg_type"], "p1", {"name": "stdout", "text": "x"})
        if "extra_keys" in row:
            msg.update(row["extra_keys"])
        out = protocol.outbound(json.dumps(msg))
        if row["action"] == "drop":
            assert out is None, row
        else:
            assert out is not None, row
            assert set(out) == KEYS


def test_kernel_frames_that_are_not_messages() -> None:
    protocol = load("reader.kernels.protocol")
    assert protocol.outbound(b"\x00binary") is None
    assert protocol.outbound("not json") is None
    assert protocol.outbound(json.dumps({"content": {}})) is None
    assert protocol.message_type(b"x") == "binary"
    assert protocol.message_type("nope") == "unparseable"


def test_notice_shape() -> None:
    protocol = load("reader.kernels.protocol")
    codes = {"rejected", "busy", "timeout", "output_truncated", "kernel_restarted", "kernel_dead",
             "kernel_reset", "kernel_unavailable"}
    assert set(protocol.NOTICE_CODES) == codes
    n = protocol.notice("timeout", "m1")
    assert n["channel"] == "reader"
    assert n["header"]["msg_type"] == "reader_notice"
    assert len(n["header"]["msg_id"]) == 32
    assert n["parent_header"] == {}
    assert n["content"] == {"code": "timeout", "parent_msg_id": "m1"}


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=8), inner, max_size=3),
    max_leaves=10,
)
CONTENT_KEYS = ["code", "cursor_pos", "silent", "allow_stdin", "user_expressions", "store_history", "x"]
REPLY_TYPES = ["execute_reply", "stream", "comm_open", "comm_msg", "comm_close", "input_request", "status",
               "display_data", "debug_event", "shutdown_reply", "whatever"]
REQUEST_TYPES = ["execute_request", "kernel_info_request", "complete_request", "inspect_request",
                 "is_complete_request", "comm_open", "history_request", "shutdown_request", "input_reply"]


@settings(max_examples=300, deadline=None)
@given(
    channel=st.sampled_from(["shell", "control", "stdin", "iopub", "x"]),
    msg_type=st.sampled_from(REQUEST_TYPES),
    msg_id=st.text(min_size=0, max_size=140),
    parent=st.one_of(st.just({}), json_values),
    content=st.dictionaries(st.sampled_from(CONTENT_KEYS), json_values, max_size=5),
    extra=st.dictionaries(st.sampled_from(["buffers", "signature", "idents", "msg_id"]), json_values,
                          max_size=2),
)
def test_inbound_property(
    channel: str, msg_type: str, msg_id: str, parent: Any, content: Any, extra: Any
) -> None:
    protocol = load("reader.kernels.protocol")
    frame = {"channel": channel, "header": {"msg_id": msg_id, "msg_type": msg_type, "session": "spoof",
                                            "username": "mallory"},
             "parent_header": parent, "metadata": {}, "content": content, **extra}
    try:
        out = _inbound(frame, pending=frozenset({"taken"}))
    except protocol.Rejected:
        return
    assert set(out) == KEYS
    assert out["channel"] == "shell"
    assert out["header"]["msg_type"] in protocol.SHELL_REQUESTS
    assert isinstance(out["header"]["msg_id"], str)
    assert 1 <= len(out["header"]["msg_id"]) <= 128
    assert out["header"]["msg_id"] != "taken"
    assert out["header"]["session"] == "s" * 32
    assert out["header"]["username"] == "ivan"
    assert out["parent_header"] == {}
    if out["header"]["msg_type"] == "execute_request":
        assert out["content"]["allow_stdin"] is False
        assert out["content"]["user_expressions"] == {}


@settings(max_examples=300, deadline=None)
@given(
    channel=st.sampled_from(["shell", "control", "stdin", "iopub", "reader", "x"]),
    msg_type=st.sampled_from(REPLY_TYPES),
    extra=st.dictionaries(st.sampled_from(["buffers", "signature", "idents"]), json_values, max_size=2),
    as_bytes=st.booleans(),
)
def test_outbound_property(channel: str, msg_type: str, extra: Any, as_bytes: bool) -> None:
    protocol = load("reader.kernels.protocol")
    msg = kernel_msg(channel, msg_type, "p", {"x": 1}, **extra)
    frame: str | bytes = json.dumps(msg)
    if as_bytes:
        frame = frame.encode()
    out = protocol.outbound(frame)
    if out is None:
        return
    assert not as_bytes
    assert set(out) == KEYS
    assert msg_type not in {"comm_open", "comm_msg", "comm_close", "input_request"}
    assert (out["channel"], out["header"]["msg_type"]) in {("shell", t) for t in protocol.SHELL_REPLIES} | {
        ("iopub", t) for t in protocol.IOPUB_TYPES
    }
