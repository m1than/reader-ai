"""SRV-OBS phase 2 [config]: the one-shot `mlflow-migrate` service and its hardening (FR-OBS-29, -33), the mlflow
dependency on it, the worker's read-only mount of deploy/otel/mlflow-setup.yaml (= VEC-OBS-6), the 8443 body limit
(FR-OBS-27), no new healthchecks (FR-OBS-32), no obs service on a non-internal network (FR-OBS-33), the upgrade
checklist's pin agreement (FR-OBS-34), the store-size constant (FR-OBS-31), the burst limits (FR-OBS-35).

@trace FR-OBS-27 FR-OBS-29 FR-OBS-31 FR-OBS-32 FR-OBS-33 FR-OBS-34 FR-OBS-35 FR-DEPLOY-15 VEC-OBS-6
"""

from __future__ import annotations

import re

from deploy.conftest import CADDYFILE, DEPLOY, networks_of, render, requires_docker_cli

from .conftest import FIXTURES, load_yaml, require
from .test_deploy_config import MLFLOW_BASE
from .test_images_migrate import N_IMAGE

OBS = ("rotel", "mlflow", "jaeger", "mlflow-migrate")


def _obs() -> dict:  # type: ignore[type-arg]
    return render(("kernels", "agents", "mcp", "observability"))


@requires_docker_cli
def test_mlflow_migrate_service() -> None:
    model = _obs()
    svc = model["services"]["mlflow-migrate"]
    assert svc["image"] == "reader-mlflow:local"
    assert svc["user"] == "10004:10004" and svc["read_only"] is True
    assert svc["cap_drop"] == ["ALL"] and "no-new-privileges:true" in svc["security_opt"]
    assert any(str(t).startswith("/tmp") for t in svc["tmpfs"])
    assert svc["restart"] == "no"
    assert svc.get("network_mode") == "none" and not svc.get("networks")
    assert [v["source"] for v in svc["volumes"] if v["type"] == "volume"] == ["obs_mlflow"]
    assert "mlflow-migrate.sh" in " ".join(svc.get("entrypoint") or []) + " ".join(svc.get("command") or [])
    assert not svc.get("ports") and not svc.get("secrets")
    assert model["services"]["mlflow"]["depends_on"]["mlflow-migrate"]["condition"] == \
        "service_completed_successfully"
    assert (DEPLOY / "otel" / "mlflow-migrate.sh").is_file()


@requires_docker_cli
def test_worker_mounts_the_setup_file_read_only() -> None:
    worker = _obs()["services"]["worker"]
    (mount,) = [v for v in worker["volumes"] if str(v.get("source", "")).endswith("deploy/otel/mlflow-setup.yaml")]
    assert mount["read_only"] is True and mount["type"] == "bind"


def test_the_setup_file_is_vec_obs_6() -> None:
    assert load_yaml(DEPLOY / "otel" / "mlflow-setup.yaml") == load_yaml(FIXTURES / "mlflow-setup.yaml")


def test_the_8443_site_caps_bodies_at_10_mib() -> None:
    text = CADDYFILE.read_text(encoding="utf-8")
    m = re.search(r"^localhost:8443 \{\n(.*?)^\}", text, re.S | re.M)
    assert m is not None
    assert re.search(r"request_body \{\s*max_size 10MiB\s*\}", m.group(1))


@requires_docker_cli
def test_no_new_healthchecks() -> None:
    services = _obs()["services"]
    assert "healthcheck" not in services["jaeger"]
    assert "healthcheck" not in services["mlflow-migrate"]
    assert services["rotel"]["healthcheck"]["test"] == ["CMD", "bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/4318"]


@requires_docker_cli
def test_obs_services_hardened_and_only_on_internal_networks() -> None:
    model = _obs()
    for name in OBS:
        svc = model["services"][name]
        assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in svc["security_opt"], name
        if name != "mlflow-migrate":
            assert int(svc["mem_limit"]) == int(svc["memswap_limit"]), name
        for net in networks_of(svc):
            assert model["networks"][net].get("internal") is True, (name, net)


def test_compose_and_test_pins_agree() -> None:
    """FR-OBS-34: the Dockerfile's base pin is the one the image tests build on; N is the previous pin by digest."""
    dockerfile = (DEPLOY / "otel" / "mlflow.Dockerfile").read_text(encoding="utf-8")
    assert f"FROM {MLFLOW_BASE}\n" in dockerfile
    assert re.fullmatch(r"ghcr\.io/mlflow/mlflow@sha256:[0-9a-f]{64}", N_IMAGE)
    checklist = (DEPLOY / "otel" / "UPGRADE.md").read_text(encoding="utf-8")  # the PR checklist's source
    for item in ("release notes", "mlflow_catalog.py --check", "CLI canary", "[image]", "writes", "burst"):
        assert item in checklist, item


def test_store_size_and_burst_limits() -> None:
    health = require("reader.obs.health")
    assert health.STORE_BYTES_MAX == 4 * 1024**3  # FR-OBS-31, structural
    assert health.STORE_WARN_RATIO == 0.8
    assert health.STALE_AFTER_S == 75 * 60  # FR-OBS-32
    burst = require("reader.obs.burst")
    assert burst.LIMITS == {"mlflow": 1024**3, "rotel": 256 * 1024**2, "jaeger": 256 * 1024**2}  # FR-OBS-35
    assert burst.MAX_P95_S == 30 and burst.MAX_PEAK_RATIO == 0.8
