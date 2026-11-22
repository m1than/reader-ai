# @trace FR-AGENT-2, FR-AGENT-3, FR-AGENT-4, FR-AGENT-5, INV-AGENT-2, INV-AGENT-3, INV-AGENT-5
"""agent-proxy in process (ASGI) against a recording upstream stub (httpx.MockTransport): token loading, run_tokens,
the forwarding allow-lists, the body checks, the per-token upstream record and logging."""

from __future__ import annotations

import json
import logging
import random
from pathlib import Path

import httpx
import pytest

from .conftest import DROP, TOKENS, Upstream, forward, issue, make_proxy, message_body, need, spec_bounds

pytestmark = pytest.mark.anyio


async def test_forward_swaps_the_run_token_for_the_users_token(tmp_path: Path) -> None:
    p = make_proxy(tmp_path)
    async with p.client() as c:
        token = await issue(c, "ivan")
        r = await forward(
            c, token, headers={"x-api-key": "client-key", "x-stainless-os": "Linux", "cookie": "a=b"}
        )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "connection" not in r.headers  # hop-by-hop dropped
    sent = p.upstream.requests[0]
    assert str(sent.url) == "https://api.anthropic.com/v1/messages?beta=true"
    assert sent.headers["authorization"] == f"Bearer {TOKENS['ivan']}"
    assert "x-api-key" not in sent.headers and "cookie" not in sent.headers
    assert sent.headers["x-stainless-os"] == "Linux"


async def test_each_run_token_maps_to_its_own_user(tmp_path: Path) -> None:
    p = make_proxy(tmp_path)
    async with p.client() as c:
        a, b = await issue(c, "ivan"), await issue(c, "anna")
        await forward(c, b)
        await forward(c, a)
    assert [r.headers["authorization"] for r in p.upstream.requests] == [
        f"Bearer {TOKENS['anna']}",
        f"Bearer {TOKENS['ivan']}",
    ]


async def test_token_loading_missing_file_and_unknown_user(tmp_path: Path) -> None:
    p = make_proxy(tmp_path, handles=("ivan",))
    async with p.client() as c:
        r = await c.post("/run-tokens", json={"user": "anna", "max_output_tokens": 100})
        assert (r.status_code, r.json()) == (409, {"error": "provider_missing"})
        r = await c.post(
            "/run-tokens", json={"user": "lea", "max_output_tokens": 100}
        )  # no provider configured
        assert (r.status_code, r.json()) == (409, {"error": "provider_missing"})
        r = await c.post("/run-tokens", json={"user": "nobody", "max_output_tokens": 100})
        assert r.status_code == 409
        from .conftest import deliver_tokens

        deliver_tokens(p.secrets_root, ("anna",))  # appears later: re-read on each request (FR-AGENT-2)
        assert (
            await c.post("/run-tokens", json={"user": "anna", "max_output_tokens": 100})
        ).status_code == 200
        bad = await c.post("/run-tokens", json={"user": "ivan"})
        assert bad.status_code == 400


def test_default_config_path_is_the_bind_target() -> None:
    proxy = need("reader.agent.proxy")
    settings = proxy.ProxySettings()
    assert settings.config_path == Path("/etc/reader/reader.yaml")
    assert settings.secrets_root == Path("/run/secrets")
    assert settings.upstream == "https://api.anthropic.com"


async def test_run_token_shape_revoke_record_and_unknown(tmp_path: Path) -> None:
    p = make_proxy(tmp_path, Upstream(status=529))
    async with p.client() as c:
        token = await issue(c)
        assert len(token) == 43
        await forward(c, token)
        await forward(c, token, body=message_body(max_tokens=99999))
        r = await c.post("/run-tokens/revoke", json={"run_token": token})
        assert r.status_code == 200
        assert r.json() == {"requests": 1, "statuses": [529], "any_2xx": False, "refused": ["output_cap"]}
        assert (await forward(c, token)).status_code == 401  # revoked
        again = await c.post("/run-tokens/revoke", json={"run_token": token})
        assert (again.status_code, again.json()) == (404, {"error": "run_token_unknown"})


async def test_expiry_and_eviction(tmp_path: Path) -> None:
    p = make_proxy(tmp_path)
    b = spec_bounds()
    async with p.client() as c:
        old = await issue(c)
        p.clock[0] += b["run_token_ttl_s"] + 1
        assert (await forward(c, old)).status_code == 401
        tokens = [await issue(c) for _ in range(b["run_tokens_max"] + 1)]
        assert (await forward(c, tokens[0])).status_code == 401  # the oldest was evicted
        assert (await c.post("/run-tokens/revoke", json={"run_token": tokens[0]})).status_code == 404
        assert (await forward(c, tokens[-1])).status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/v1/messages", 404),
        ("POST", "/v1/messages?beta=false", 404),
        ("POST", "/v1/messages?beta=true&x=1", 404),
        ("POST", "/v1/complete", 404),
        ("POST", "/v1/messages/count_tokens", 404),
        ("POST", "/v1/messages", 200),
        ("POST", "/v1/messages?beta=true", 200),
    ],
)
async def test_method_path_query_allow_list(tmp_path: Path, method: str, path: str, status: int) -> None:
    p = make_proxy(tmp_path)
    async with p.client() as c:
        token = await issue(c)
        r = await forward(c, token, method=method, path=path)
    assert r.status_code == status
    assert len(p.upstream.requests) == (1 if status == 200 else 0)


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        (b"not json", 415, "body_not_json"),
        (b"[1, 2]", 415, "body_not_json"),
        (message_body(tools=[{"name": "Bash"}]), 403, "tools_mismatch"),
        (message_body(max_tokens=16001), 403, "output_cap"),
        (message_body(max_tokens=DROP), 403, "output_cap"),
        (message_body(tools=DROP), 200, None),
    ],
)
async def test_body_checks(tmp_path: Path, body: bytes, status: int, code: str | None) -> None:
    p = make_proxy(tmp_path)
    async with p.client() as c:
        token = await issue(c, max_output_tokens=16000)
        r = await forward(c, token, body=body)
    assert r.status_code == status
    if code:
        assert r.json() == {"error": code}
        assert p.upstream.requests == []


async def test_unknown_or_missing_bearer_is_401_and_not_forwarded(tmp_path: Path) -> None:
    p = make_proxy(tmp_path)
    async with p.client() as c:
        await issue(c)
        assert (await forward(c, None)).status_code == 401
        assert (await forward(c, "x" * 43)).status_code == 401
        r = await forward(c, None, headers={"x-api-key": TOKENS["ivan"]})
        assert r.status_code == 401
    assert p.upstream.requests == []


async def test_oversized_body_is_413(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = need("reader.agent.proxy")
    monkeypatch.setattr(proxy, "BODY_MAX_BYTES", 1024)
    p = make_proxy(tmp_path)
    async with p.client() as c:
        token = await issue(c)
        r = await forward(c, token, body=message_body(pad="x" * 2000))
    assert r.status_code == 413
    assert p.upstream.requests == []


def test_body_max_is_the_spec_bound() -> None:
    assert spec_bounds()["proxy_body_max_bytes"] == need("reader.agent.proxy").BODY_MAX_BYTES


async def test_upstream_redirect_is_not_followed(tmp_path: Path) -> None:
    proxy = need("reader.agent.proxy")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://evil.example/v1/messages"})

    from .conftest import deliver_tokens, write_config

    deliver_tokens(tmp_path / "s")
    app = proxy.create_app(
        proxy.ProxySettings(config_path=write_config(tmp_path), secrets_root=tmp_path / "s"),
        upstream_transport=httpx.MockTransport(handler),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent-proxy:8080"
    ) as c:
        token = await issue(c)
        r = await forward(c, token)
    assert r.status_code == 307
    assert seen == ["https://api.anthropic.com/v1/messages?beta=true"]


async def test_only_the_pinned_upstream_is_contacted_property(tmp_path: Path) -> None:
    """INV-AGENT-3: random methods, paths, queries and header sets; only allowed requests reach the stub, always at
    the pinned host, with only allow-listed headers. INV-AGENT-2: tokens of two users across issue/revoke/expiry."""
    proxy = need("reader.agent.proxy")
    p = make_proxy(tmp_path)
    rnd = random.Random(7)  # noqa: S311
    methods = ["GET", "POST", "PUT", "DELETE", "PATCH"]
    paths = [
        "/v1/messages",
        "/v1/messages/batches",
        "/v1/models",
        "/",
        "/run-tokens/x",
        "/v1/messages/../models",
    ]
    queries = ["", "?beta=true", "?beta=true&beta=true", "?x=1", "?beta=True"]
    header_names = [
        "x-api-key",
        "cookie",
        "host",
        "x-forwarded-for",
        "anthropic-beta",
        "x-stainless-arch",
        "x-other",
        "proxy-authorization",
        "user-agent",
        "x-app",
    ]
    live: dict[str, str] = {}
    async with p.client() as c:
        for _ in range(150):
            if rnd.random() < 0.2 or not live:
                handle = rnd.choice(["ivan", "anna"])
                live[await issue(c, handle)] = handle
            token = rnd.choice([*live, "x" * 43])
            if rnd.random() < 0.1 and token in live:
                await c.post("/run-tokens/revoke", json={"run_token": token})
                live.pop(token)
            method, path, query = rnd.choice(methods), rnd.choice(paths), rnd.choice(queries)
            headers = {name: "v" for name in rnd.sample(header_names, 4)}
            before = len(p.upstream.requests)
            r = await forward(c, token, method=method, path=path + query, headers=headers)
            allowed = (
                method == "POST" and path == "/v1/messages" and query in ("", "?beta=true") and token in live
            )
            assert (len(p.upstream.requests) == before + 1) is allowed, (method, path, query, r.status_code)
            if allowed:
                sent = p.upstream.requests[-1]
                assert sent.url.host == "api.anthropic.com" and sent.url.scheme == "https"
                assert sent.headers["authorization"] == f"Bearer {TOKENS[live[token]]}"
                for name in sent.headers:
                    assert proxy.forwarded_header(name), name
                assert not {"x-api-key", "cookie", "proxy-authorization", "x-forwarded-for", "x-other"} & set(
                    sent.headers
                )


async def test_logging_has_no_secrets(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    p = make_proxy(tmp_path)
    canary = "CANARY-PROMPT-7f3a"
    async with p.client() as c:
        token = await issue(c)
        await forward(c, token, body=message_body(messages=[{"role": "user", "content": canary}]))
        await c.post("/run-tokens/revoke", json={"run_token": token})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "POST /v1/messages 200" in text
    for secret in (token, *TOKENS.values(), canary, "beta=true"):
        assert secret not in text
    assert token[:8] not in text  # the digest prefix is logged, never the token's own prefix
    lines = [r.getMessage() for r in caplog.records if r.name == "reader.agent.proxy"]
    assert any("ivan" in line for line in lines)
    assert json.dumps(TOKENS) not in text
