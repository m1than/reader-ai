"""Secret resolution: registries, resolvers, the Secret type, errors and leak surfaces.

@trace FR-CONFIG-6, FR-CONFIG-7, FR-CONFIG-8, FR-CONFIG-12, FR-CONFIG-13, FR-CONFIG-16
@trace VEC-CONFIG-6, INV-CONFIG-2
"""

import base64
import copy
import json
import logging
import os
import pickle
from pathlib import Path
from typing import Any

import pytest
from conftest import IS_ROOT, load_fixture, rendered, rendered_with_locals

RESOLVE = load_fixture("resolve-cases.yaml")
CANARY = "CANARY-secret-7f3a"


def _build(base: Path) -> Path:
    for top, entries in RESOLVE["tree"].items():
        directory = base / top
        directory.mkdir()
        for name, spec in entries.items():
            path = directory / name
            if spec.get("dir"):
                path.mkdir()
            elif "link" in spec:
                path.symlink_to(spec["link"])
            elif "size" in spec:
                path.write_bytes(b"x" * spec["size"])
            elif "content_b64" in spec:
                path.write_bytes(base64.b64decode(spec["content_b64"]))
            else:
                path.write_text(spec["content"], encoding="utf-8")
            if spec.get("mode") is not None:
                os.chmod(path, spec["mode"])
    return base / "root"


def _params() -> list[Any]:
    out = []
    for case in RESOLVE["cases"]:
        marks = [pytest.mark.skipif(IS_ROOT, reason="uid 0")] if case["ref"].endswith("/locked") else []
        out.append(pytest.param(case, marks=marks, id=f"{case['registry']}:{case['ref']}"))
    return out


@pytest.mark.parametrize("case", _params())
def test_resolver_cases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: dict[str, Any]) -> None:
    from reader.config import SecretRef, SecretResolutionError, container_registry, host_registry, resolve

    root = _build(tmp_path)
    for name, value in case.get("env", {}).items():
        monkeypatch.setenv(name, value)
    registry = container_registry(root) if case["registry"] == "container" else host_registry()
    ref = SecretRef.parse(case["ref"])
    if "code" in case:
        with pytest.raises(SecretResolutionError) as caught:
            resolve(ref, registry)
        # Render before any assert: pytest's assertion rewriting puts the assert's literals into this frame's
        # locals, which a traceback with locals would otherwise show.
        surfaces = rendered(caught.value) + rendered_with_locals(caught.value)
        assert caught.value.code == case["code"]
        assert caught.value.ref == ref
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None or caught.value.__suppress_context__
        assert "CANARY-" not in surfaces
    elif "value_length" in case:
        assert len(resolve(ref, registry).reveal()) == case["value_length"]
    else:
        assert resolve(ref, registry).reveal() == case["value"]


def test_registry_takes_pairs_and_rejects_unknown_or_duplicate_schemes() -> None:
    from reader.config import ResolverRegistry, Secret

    def resolver(ref: Any) -> Secret:
        return Secret("x")

    ResolverRegistry([("env", resolver), ("keychain", resolver)])
    with pytest.raises(ValueError):
        ResolverRegistry([("env", resolver), ("env", resolver)])
    with pytest.raises(ValueError):
        ResolverRegistry([("vault", resolver)])


def test_registry_is_immutable() -> None:
    from reader.config import ResolverRegistry, Secret

    registry = ResolverRegistry([("env", lambda ref: Secret("x"))])
    with pytest.raises((AttributeError, TypeError)):
        registry.resolvers = {}  # type: ignore[attr-defined, misc]


def test_host_registry_accepts_extra_resolvers() -> None:
    from reader.config import Secret, SecretRef, host_registry, resolve

    registry = host_registry([("keychain", lambda ref: Secret("from-keychain"))])
    assert resolve(SecretRef.parse("secret://keychain/a/b"), registry).reveal() == "from-keychain"


def test_secret_never_shows_its_value() -> None:
    from reader.config import Secret

    secret = Secret(CANARY)
    for text in (str(secret), repr(secret), f"{secret}", format(secret, ">40"), f"{secret!r}"):
        assert CANARY not in text
    for dump in (json.dumps, pickle.dumps, copy.copy, copy.deepcopy):
        try:
            out = dump(secret)
        except TypeError:
            continue
        assert CANARY not in repr(out)
        assert CANARY.encode() not in (out if isinstance(out, bytes) else b"")
    assert secret.reveal() == CANARY


def test_resolved_value_absent_from_traceback_with_locals_after_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reader.config import SecretRef, container_registry, host_registry, resolve

    monkeypatch.setenv("READER_CANARY_TOKEN", CANARY)
    root = tmp_path / "root"
    root.mkdir()
    (root / "tok").write_text(CANARY + "\n", encoding="utf-8")
    cases = [
        (SecretRef.parse("secret://env/READER_CANARY_TOKEN"), host_registry()),
        (SecretRef.parse("secret://file/run/secrets/tok"), container_registry(root)),
    ]
    for ref, registry in cases:
        def after_read(ref: Any = ref, registry: Any = registry) -> None:
            secret = resolve(ref, registry)  # noqa: F841 - the local is what the traceback captures
            raise RuntimeError("failure after the value was read")

        with pytest.raises(RuntimeError) as caught:
            after_read()
        assert CANARY not in rendered_with_locals(caught.value)
        assert CANARY not in repr(registry) + str(registry)


def test_module_logs_references_and_codes_never_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from reader.config import SecretRef, SecretResolutionError, container_registry, host_registry, resolve

    monkeypatch.setenv("READER_CANARY_TOKEN", CANARY)
    root = tmp_path / "root"
    root.mkdir()
    (root / "bad").write_bytes(CANARY.encode() + b"\xff")
    with caplog.at_level(logging.DEBUG, logger="reader.config"):
        resolve(SecretRef.parse("secret://env/READER_CANARY_TOKEN"), host_registry())
        with pytest.raises(SecretResolutionError):
            resolve(SecretRef.parse("secret://file/run/secrets/bad"), container_registry(root))
    assert caplog.records, "resolve logs the reference and the outcome"
    for record in caplog.records:
        assert CANARY not in record.getMessage()
        assert CANARY not in repr(record.args)
    assert any("secret://env/READER_CANARY_TOKEN" in r.getMessage() for r in caplog.records)
