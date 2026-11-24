"""SRV-OBS FR-OBS-21 and FR-OBS-26 (ADR-0038): the agent version key and the declarative MLflow setup.

`ensure_setup` is the ONE idempotent setup function: it reads MLflow's state, compares it with VEC-OBS-6
(deploy/otel/mlflow-setup.yaml) and writes only the difference: experiment 0's name and tags, the `bench`
experiment, and one LoggedModel `reader-agent` per version key, finalized READY, whose `reader.last_started`
tag holds the UTC start day (FR-OBS-31's "used"). A repeat on the same day makes 0 writes; the first run of a
new day makes exactly one (that tag). It runs in the job `obs.setup` and inline wherever an experiment is
missing."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field

from reader.jobs.model import JobKind
from reader.obs import phase2
from reader.obs.phase2 import request

log = logging.getLogger("reader.obs")
KIND: Final = "obs.setup"
SHORT: Final = 12


class ObsSetupRefused(Exception):
    """MLflow answered 4xx to a setup write: the key ends dead (it blocks nothing: setup also runs inline)."""


class SetupArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def version_key(
    *, prompt_set_sha256: str, stage_models: Mapping[str, str], pipeline_config_sha256: str
) -> str:
    """FR-OBS-21: sha256 of the canonical JSON of the three inputs."""
    inputs = {
        "prompt_set_sha256": prompt_set_sha256,
        "stage_models": dict(stage_models),
        "pipeline_config_sha256": pipeline_config_sha256,
    }
    return hashlib.sha256(_canonical(inputs).encode()).hexdigest()


def short_key(key: str) -> str:
    return key[:SHORT]


def current_inputs() -> dict[str, Any]:
    """The worker's three inputs: FR-OBS-15's templates by name, the configured task → model map, the
    pipeline's declared stage table (its file's sha256)."""
    from reader.obs import prompts
    from reader.obs.analytics import PROMPTS

    templates = {name: prompts.template(name)[1] for name in sorted(PROMPTS)}
    prompt_set = hashlib.sha256(_canonical(templates).encode()).hexdigest()
    stage_models: dict[str, str] = {}
    try:
        import os

        from reader.config.load import load_config

        path = os.environ.get("READER_CONFIG")
        if path:
            for name, task in load_config(path).tasks.items():
                stage_models[str(name)] = str(getattr(task, "model", ""))
    except Exception:
        stage_models = {}
    stages = Path(__file__).resolve().parents[1] / "pipeline" / "stages.yaml"
    pipeline = hashlib.sha256(stages.read_bytes()).hexdigest() if stages.is_file() else "0" * 64
    return {"prompt_set_sha256": prompt_set, "stage_models": stage_models, "pipeline_config_sha256": pipeline}


def _tags(items: Any) -> dict[str, str]:
    return {str(i["key"]): str(i.get("value", "")) for i in items or [] if isinstance(i, dict) and "key" in i}


async def ensure_setup(
    client: httpx.AsyncClient,
    setup: Mapping[str, Any],
    *,
    version_key: str,
    inputs: Mapping[str, Any],
    day: str,
) -> list[str]:
    """Write only the difference between MLflow and `setup`; returns the writes made (for the log line)."""
    made: list[str] = []
    for exp in setup["experiments"]:
        want_tags: Mapping[str, str] = exp.get("tags") or {}
        if "id" in exp:
            _, got = await request(
                client, "GET", "/api/2.0/mlflow/experiments/get", params={"experiment_id": str(exp["id"])}
            )
            current = got.get("experiment") or {}
            if current.get("name") != exp["name"]:
                await request(
                    client,
                    "POST",
                    "/api/2.0/mlflow/experiments/update",
                    {"experiment_id": str(exp["id"]), "new_name": exp["name"]},
                )
                made.append(f"rename {exp['id']}")
            have = _tags(current.get("tags"))
            for k, v in want_tags.items():
                if have.get(k) != v:
                    await request(
                        client,
                        "POST",
                        "/api/2.0/mlflow/experiments/set-experiment-tag",
                        {"experiment_id": str(exp["id"]), "key": k, "value": v},
                    )
                    made.append(f"tag {exp['id']}")
            continue
        status, _ = await request(
            client,
            "GET",
            "/api/2.0/mlflow/experiments/get-by-name",
            params={"experiment_name": exp["name"]},
            ok404=True,
        )
        if status == 404:
            await request(
                client,
                "POST",
                "/api/2.0/mlflow/experiments/create",
                {"name": exp["name"], "tags": [{"key": k, "value": v} for k, v in want_tags.items()]},
            )
            made.append(f"create {exp['name']}")
    models = setup["logged_models"]
    short = short_key(version_key)
    _, found = await request(
        client,
        "POST",
        "/api/2.0/mlflow/logged-models/search",
        {"experiment_ids": ["0"], "filter": f"tags.`{models['tag']}` = '{short}'"},
    )
    listed = [m for m in found.get("models") or [] if isinstance(m, dict)]
    if listed:
        info = listed[0].get("info") or {}
        model_id = str(info["model_id"])
        last = _tags(info.get("tags")).get(models["last_started_tag"])
    else:
        _, created = await request(
            client,
            "POST",
            "/api/2.0/mlflow/logged-models",
            {
                "experiment_id": "0",
                "name": models["name"],
                "params": [
                    {"key": k, "value": v if isinstance(v, str) else _canonical(v)} for k, v in inputs.items()
                ],
                "tags": [{"key": models["tag"], "value": short}],
            },
        )
        model_id = str(((created.get("model") or {}).get("info") or {})["model_id"])
        await request(
            client,
            "PATCH",
            f"/api/2.0/mlflow/logged-models/{model_id}",
            {"model_id": model_id, "status": "LOGGED_MODEL_READY"},
        )
        made.append("create model")
        last = None
    if last != day:
        await request(
            client,
            "PATCH",
            f"/api/2.0/mlflow/logged-models/{model_id}/tags",
            {"tags": [{"key": models["last_started_tag"], "value": day}]},
        )
        made.append("last_started")
    return made


def setup_digest(setup: Mapping[str, Any], key: str, day: str) -> str:
    return hashlib.sha256(_canonical({"setup": setup, "key": key, "day": day}).encode()).hexdigest()


async def run_setup(client: httpx.AsyncClient, now: datetime | None = None) -> list[str]:
    """`ensure_setup` with the worker's own inputs and today's day; sets the process's version key."""
    from reader.obs import _STATE

    inputs = current_inputs()
    key = version_key(**inputs)
    _STATE["version_key"] = short_key(key)
    day = (now or datetime.now(UTC)).date().isoformat()
    made = await ensure_setup(client, phase2.setup_config(), version_key=key, inputs=inputs, day=day)
    if made:
        log.info("obs setup writes=%d", len(made))
    return made


async def handle(args: SetupArgs, conn: Any, ctx: Any) -> None:
    from reader.obs import setup as obs_setup

    if not obs_setup.current_settings().enabled:
        return
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            await run_setup(client)
    except phase2.MlflowRefused as refused:
        raise ObsSetupRefused(str(refused.status)) from None


async def on_worker_start(jobs: Any, now: datetime | None = None) -> None:
    """Enqueue-only (FR-EVENTS-1 hooks); sets the version key every job root carries (FR-OBS-21 a)."""
    from reader.obs import _STATE

    try:
        inputs = current_inputs()
        key = version_key(**inputs)
        _STATE["version_key"] = short_key(key)
        day = (now or datetime.now(UTC)).date().isoformat()
        await jobs.enqueue(KIND, {"digest": setup_digest(phase2.setup_config(), key, day)})
    except Exception:
        log.warning("obs setup not enqueued")


async def ensure_inline(client: httpx.AsyncClient) -> None:
    """FR-OBS-26 (2): called by obs.runs, obs.bench and obs.label when an experiment they need is missing."""
    await run_setup(client)


KIND_DECL: Final = JobKind(
    name=KIND,
    queue="obs",
    args=SetupArgs,
    key=("digest",),
    handler=handle,
    max_attempts=3,
    transient=(httpx.TransportError, httpx.HTTPStatusError),
    timeout_s=60,
)
