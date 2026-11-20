# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx==0.28.1", "websockets==17.4"]
# ///
"""Spike client: start kernels through Kernel Gateway's REST API, run cells over the kernel WebSocket
(Jupyter messaging protocol 5.3) and time the round trip. Prints one JSON object.

    python client.py <gateway-url>
"""

import asyncio
import json
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import websockets

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8888"
SHA256_ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"

RUST_DEPS = """:dep sha2 = "0.11"
:dep hmac = "0.13"
:dep pbkdf2 = "0.13"
:dep aes-gcm = "0.11"
:dep k256 = { version = "0.14", features = ["ecdsa"] }
:dep hex = "0.4"
"""
RUST_PILOT = RUST_DEPS + 'use sha2::{Digest, Sha256};\nprintln!("{}", hex::encode(Sha256::digest(b"abc")));'

Cell = tuple[str, str, str]  # label, code, expected substring in the output

CELLS: dict[str, list[Cell]] = {
    "python3": [
        ("sha256", "import hashlib; print(hashlib.sha256(b'abc').hexdigest())", SHA256_ABC),
        ("stream", "import time\nfor i in range(5):\n    print(i, flush=True); time.sleep(0.3)", "4"),
        # ADR-0014: no route to the internet. Expected to fail fast.
        ("egress", "import urllib.request\ntry:\n"
                   "    urllib.request.urlopen('https://index.crates.io/config.json', timeout=5); print('egress-open')\n"
                   "except Exception as e:\n    print('egress-blocked', type(e).__name__)", "egress-blocked"),
    ],
    "rust": [
        ("trivial", 'println!("{}", 40 + 2);', "42"),
        ("pilot-crates", RUST_PILOT, SHA256_ABC),
        ("after-deps", 'println!("{}", hex::encode(Sha256::digest(b"abc")));', SHA256_ABC),
        ("stream", "for i in 0..5 { println!(\"{}\", i); std::io::Write::flush(&mut std::io::stdout()).unwrap();"
                   " std::thread::sleep(std::time::Duration::from_millis(300)); }", "4"),
    ],
    "deno": [
        ("sha256", "const d = await crypto.subtle.digest('SHA-256', new TextEncoder().encode('abc'));\n"
                   "console.log([...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, '0')).join(''));",
         SHA256_ABC),
        ("stream", "for (let i = 0; i < 5; i++) { console.log(i); await new Promise((r) => setTimeout(r, 300)); }", "4"),
    ],
}


def message(msg_type: str, content: dict[str, Any]) -> dict[str, Any]:
    return {
        "header": {"msg_id": uuid.uuid4().hex, "username": "spike", "session": uuid.uuid4().hex,
                   "msg_type": msg_type, "version": "5.3", "date": datetime.now(UTC).isoformat()},
        "parent_header": {}, "metadata": {}, "content": content, "channel": "shell",
    }


async def request(ws: Any, msg: dict[str, Any], timeout: float) -> tuple[list[dict[str, Any]], float | None]:
    """Send a request; collect iopub outputs until the kernel is idle. Returns outputs and first-output time."""
    t0 = time.monotonic()
    msg_id = msg["header"]["msg_id"]
    await ws.send(json.dumps(msg))
    outputs: list[dict[str, Any]] = []
    first: float | None = None
    async with asyncio.timeout(timeout):
        while True:
            reply = json.loads(await ws.recv())
            if reply.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            kind = reply["msg_type"]
            if kind in ("stream", "execute_result", "display_data", "error"):
                if first is None:
                    first = time.monotonic() - t0
                outputs.append({"t": round(time.monotonic() - t0, 3), "type": kind, "content": reply["content"]})
            if kind == "status" and reply["content"]["execution_state"] == "idle":
                return outputs, first


def text_of(outputs: list[dict[str, Any]]) -> str:
    parts = []
    for o in outputs:
        c = o["content"]
        parts.append(c.get("text") or json.dumps(c.get("data", {})) or "")
        if o["type"] == "error":
            parts.append(f"{c.get('ename')}: {c.get('evalue')}")
    return "".join(parts)


async def run_kernel(client: httpx.AsyncClient, name: str, cells: list[Cell]) -> dict[str, Any]:
    t0 = time.monotonic()
    kernel = (await client.post(f"{BASE}/api/kernels", json={"name": name})).raise_for_status().json()
    ws_url = BASE.replace("http", "ws", 1) + f"/api/kernels/{kernel['id']}/channels"
    result: dict[str, Any] = {"kernel": name, "cells": []}
    try:
        async with websockets.connect(ws_url, max_size=None, open_timeout=120) as ws:
            await request(ws, message("kernel_info_request", {}), timeout=120)
            result["start_s"] = round(time.monotonic() - t0, 2)
            for label, code, expected in cells:
                t1 = time.monotonic()
                outputs, first = await request(ws, message("execute_request", {
                    "code": code, "silent": False, "store_history": True, "user_expressions": {},
                    "allow_stdin": False, "stop_on_error": True}), timeout=900)
                text = text_of(outputs)
                result["cells"].append({
                    "cell": label, "total_s": round(time.monotonic() - t1, 2),
                    "first_output_s": round(first, 2) if first is not None else None,
                    "output_times_s": [o["t"] for o in outputs],
                    "ok": expected in text,
                    "output": text.strip()[-300:] if expected in text else text.strip(),  # failures: keep it all
                })
    finally:
        await client.delete(f"{BASE}/api/kernels/{kernel['id']}")
    return result


async def guarded(coro: Any, name: str) -> dict[str, Any]:
    try:
        return dict(await coro)
    except Exception as exc:  # a spike records the failure and measures the rest
        return {"kernel": name, "error": f"{type(exc).__name__}: {exc}", "cells": []}


def concurrent_cells(tag: str) -> list[Cell]:
    """Two Rust kernels at once (pair sessions, two open notebooks): each keeps its own state."""
    return [
        ("set-tag", f'let tag = "{tag}";', ""),
        ("pilot-crates", RUST_PILOT, SHA256_ABC),
        ("own-tag", 'println!("tag={}", tag);', f"tag={tag}"),
    ]


async def main() -> None:
    async with httpx.AsyncClient(timeout=120) as client:
        specs = (await client.get(f"{BASE}/api/kernelspecs")).raise_for_status().json()
        sequential = [await guarded(run_kernel(client, name, cells), name) for name, cells in CELLS.items()]
        concurrent = await asyncio.gather(
            *(guarded(run_kernel(client, "rust", concurrent_cells(tag)), f"rust-{tag}") for tag in ("A", "B")))
    print(json.dumps({"kernelspecs": sorted(specs["kernelspecs"]), "results": sequential,
                      "concurrent_rust": list(concurrent)}, indent=2))


asyncio.run(main())
