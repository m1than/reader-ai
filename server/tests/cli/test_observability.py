"""OPS-CLI FR-CLI-22 (paired SRV-OBS change): profile `observability`, the 8443 port in the override, the
traces line, and the doctor's gate check.

@trace FR-CLI-22
"""

from pathlib import Path
from typing import Any

import pytest

from .conftest import COMPOSE, write_config
from .test_profiles import _compose_calls, _profiles, _up, _world

ON = """\
version: 1
instance: {url: https://localhost, data_dir: ./data}
users:
  - {handle: ivan, role: owner}
"""


def _cfg(tmp: Path, enabled: bool | None) -> Path:
    tail = "" if enabled is None else f"observability: {{enabled: {str(enabled).lower()}}}\n"
    return write_config(tmp / "cfg", ON + tail)


def test_optional_profiles_and_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from reader.cli.commands import OPTIONAL_PROFILES, choose_profiles
    from reader.config import load_config

    assert "observability" in OPTIONAL_PROFILES
    world = _world(tmp_path, monkeypatch, agents_in_model=False)
    assert choose_profiles(world.ctx, load_config(_cfg(tmp_path, None))) == ("observability",)  # default on
    assert choose_profiles(world.ctx, load_config(_cfg(tmp_path, True))) == ("observability",)
    assert choose_profiles(world.ctx, load_config(_cfg(tmp_path, False))) == ()


def test_override_publishes_8443_only_with_the_profile(tmp_path: Path) -> None:
    from reader.cli.stack import build_override, load_compose_source

    config = write_config(tmp_path / "c")
    source = load_compose_source(COMPOSE)
    with_obs: dict[str, Any] = build_override(source, ["ivan"], config, observability=True)
    assert with_obs["services"]["caddy"] == {"ports": ["127.0.0.1:8443:8443"]}
    without: dict[str, Any] = build_override(source, ["ivan"], config, observability=False)
    assert "caddy" not in without["services"]
    assert "caddy" not in build_override(source, ["ivan"], config)["services"]


def test_up_with_the_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch, agents_in_model=False)
    assert _up(world, _cfg(tmp_path, True)) == 0
    for argv in _compose_calls(world).values():
        assert _profiles(argv) == ["kernels", "observability"]
    assert "traces: https://localhost:8443/" in world.out
    assert (world.ctx.run_dir / "profiles").read_text().split() == ["observability"]


def test_up_without_the_profile_prints_no_traces_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, agents_in_model=False)
    assert _up(world, _cfg(tmp_path, False)) == 0
    assert "traces:" not in world.out


@pytest.mark.parametrize(
    ("status", "ok", "detail"),
    [
        (401, True, "gate ok"),
        (200, False, "gate open"),
        (204, False, "gate open"),
        (302, False, "gate open"),
        (399, False, "gate open"),
        (403, False, "status 403"),
        (502, False, "status 502"),
        (None, False, "unreachable"),
    ],
)
def test_doctor_gate_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int | None, ok: bool, detail: str
) -> None:
    import json

    from reader.cli import maintenance

    calls: list[str] = []
    seen_roots: list[bytes] = []

    def fake(url: str, roots: Any) -> int | None:
        calls.append(url)
        seen_roots.extend(Path(r).read_bytes() for r in roots)
        return status

    root_pem = _test_ca(tmp_path)[0].read_bytes()
    monkeypatch.setattr(maintenance, "gate_status", fake)
    world = _world(tmp_path, monkeypatch, agents_in_model=False)
    world.runner.on("exec", "-T", "caddy", "cat", result=root_pem)  # no `reader trust` record in this world
    rc = maintenance.cmd_doctor(world.ctx, _cfg(tmp_path, True), True)
    listed = json.loads(world.out.strip().splitlines()[-1])
    checks = {c["name"]: c for c in listed}
    assert calls == ["https://localhost:8443/"]
    assert seen_roots == [root_pem]  # Caddy's current public root, read the way `reader trust` reads it
    assert (checks["observability"]["ok"], checks["observability"]["detail"]) == (ok, detail)
    assert rc == (0 if all(c["ok"] for c in listed) else 1)  # the check feeds the exit code
    saved = world.ctx.run_dir / "doctor-root.pem"
    assert saved.stat().st_mode & 0o777 == 0o600
    assert not world.runner.argvs("security", "add-trusted-cert")  # the doctor never adds trust


@pytest.mark.parametrize(
    "exported",
    [
        (1, b"", b"no such service"),
        b"",
        b"not a certificate",
        b"MIIB-----BEGIN CERTIFICATE-----\n",
        b"-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n",  # PEM shape, not a certificate
        "truncated",
    ],
)
def test_doctor_without_a_usable_caddy_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exported: Any
) -> None:
    """Live-fixes review: a bad or truncated root is no root, never an ssl.SSLError out of `reader doctor`."""
    import json

    from reader.cli import maintenance

    if exported == "truncated":
        full = _test_ca(tmp_path)[0].read_bytes()
        exported = full[: len(full) // 2] + b"\n-----END CERTIFICATE-----\n"
    monkeypatch.setattr(maintenance, "gate_status", lambda url, roots: pytest.fail("no request: no root"))
    world = _world(tmp_path, monkeypatch, agents_in_model=False)
    world.runner.on("exec", "-T", "caddy", "cat", result=exported)
    rc = maintenance.cmd_doctor(world.ctx, _cfg(tmp_path, True), True)
    checks = {c["name"]: c for c in json.loads(world.out.strip().splitlines()[-1])}
    assert (checks["observability"]["ok"], checks["observability"]["detail"]) == (
        False,
        "unreachable: no Caddy root (stack down?)",
    )
    assert rc == 1
    assert not (world.ctx.run_dir / "doctor-root.pem").exists()


def test_doctor_uses_the_recorded_root_and_caddys_and_runs_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reader.cli import maintenance

    first = _test_ca(tmp_path / "a")[0].read_bytes()
    second = _test_ca(tmp_path / "b")[0].read_bytes()
    seen: list[list[bytes]] = []

    def record(url: str, roots: Any) -> int:
        seen.append([Path(r).read_bytes() for r in roots])
        return 401

    monkeypatch.setattr(maintenance, "gate_status", record)
    world = _world(tmp_path, monkeypatch, agents_in_model=False)
    recorded = world.ctx.run_dir / "trusted-roots"
    recorded.mkdir(parents=True, exist_ok=True)
    (recorded / "ABC.pem").write_bytes(first)
    world.runner.on("security", "trust-settings-export", result=1)
    for caddy in (second, first):  # a second run replaces run/doctor-root.pem (unlink, then O_EXCL)
        world.runner.on("exec", "-T", "caddy", "cat", result=caddy)
        maintenance.cmd_doctor(world.ctx, _cfg(tmp_path, True), True)
    assert seen == [[first, second], [first, first]]
    assert (world.ctx.run_dir / "doctor-root.pem").read_bytes() == first


def test_doctor_gate_check_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from reader.cli import maintenance

    monkeypatch.setattr(maintenance, "gate_status", lambda url, roots: pytest.fail("no request when off"))
    world = _world(tmp_path, monkeypatch, agents_in_model=False)
    maintenance.cmd_doctor(world.ctx, _cfg(tmp_path, False), True)
    checks = {c["name"]: c for c in json.loads(world.out.strip().splitlines()[-1])}
    assert (checks["observability"]["ok"], checks["observability"]["detail"]) == (True, "off")


def _test_ca(tmp: Path) -> tuple[Path, Path, Path]:
    """A throwaway CA and a `localhost` leaf it signed: (ca.pem, leaf.pem, leaf.key)."""
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.now(UTC)

    def cert(subject: str, key: Any, issuer: str, signer: Any, ca: bool) -> Any:
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer)]))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            # Python 3.13+ verifies strictly (VERIFY_X509_STRICT): key identifiers and key usage are required.
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), critical=False
            )
        )
        if ca:
            usage = x509.KeyUsage(False, False, False, False, False, True, True, False, False)
        else:
            usage = x509.KeyUsage(True, False, False, False, False, False, False, False, False)
            builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
            server_auth = x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH])
            builder = builder.add_extension(server_auth, False)
        return builder.add_extension(usage, critical=True).sign(signer, hashes.SHA256())

    ca_key, leaf_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca = cert("reader test CA", ca_key, "reader test CA", ca_key, True)
    leaf = cert("localhost", leaf_key, "reader test CA", ca_key, False)
    tmp.mkdir(parents=True, exist_ok=True)
    paths = (tmp / "ca.pem", tmp / "leaf.pem", tmp / "leaf.key")
    paths[0].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths[1].write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    paths[2].write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    return paths


def test_gate_status_verifies_and_never_follows_a_redirect(tmp_path: Path) -> None:
    """The real function against a local HTTPS server on 127.0.0.1 (no Docker, no published port)."""
    import http.server
    import ssl
    import threading

    from reader.cli.maintenance import gate_status

    ca, leaf, key = _test_ca(tmp_path)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/")
            else:
                self.send_response(200 if self.headers.get("Cookie") else 401)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: Any) -> None: ...

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(leaf, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        assert gate_status(f"https://localhost:{port}/", [ca]) == 401
        assert gate_status(f"https://localhost:{port}/redirect", [ca]) == 302  # not followed to the 401
        assert gate_status(f"https://localhost:{port}/", []) is None  # untrusted root: no answer counts
        assert gate_status(f"https://127.0.0.1:{port}/", [ca]) is None  # the host name is checked
        assert gate_status("https://localhost:1/", [ca]) is None  # nothing listens
        bad = tmp_path / "bad.pem"
        bad.write_bytes(b"-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n")
        missing = tmp_path / "missing.pem"
        assert gate_status(f"https://localhost:{port}/", [bad, missing, ca]) == 401  # bad roots are skipped
        assert gate_status(f"https://localhost:{port}/", [bad]) is None  # and never raise
    finally:
        server.shutdown()
        server.server_close()
