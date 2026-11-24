"""SRV-OBS phase 2 (ADR-0038): the shared catalogues and the one MLflow client every phase 2 job uses.

`VEC` is VEC-OBS-5 (phase2.yaml beside this module), `SETUP` is VEC-OBS-6 (mlflow_setup.yaml; the
deployed copy at SETUP_PATH overrides it at runtime, FR-OBS-26). `WRITE_PATHS` is every (method,
path template) a phase 2 job may send; `request` refuses any other, so no job can reach an undeclared MLflow
route (INV-OBS-2's phase 2 clause covers the bodies)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import httpx
from ruamel.yaml import YAML

HERE: Final = Path(__file__).resolve().parent
MLFLOW: Final = "http://mlflow:5000"
HTTP_TIMEOUT_S: Final = 10.0


def _load(path: Path) -> Any:
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


VEC: Final[dict[str, Any]] = _load(HERE / "phase2.yaml")
SETUP: Final[dict[str, Any]] = _load(HERE / "mlflow_setup.yaml")
PATTERNS: Final[Mapping[str, Any]] = VEC["patterns"]
WRITE_PATHS: Final = tuple((w["method"], w["path"]) for w in VEC["writes"])
_TEMPLATES: Final = tuple(
    (method, re.compile("^" + re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", re.escape(path)) + "$"))
    for method, path in WRITE_PATHS
)


SETUP_PATH: Final = Path("/etc/reader/mlflow-setup.yaml")  # compose's read-only bind (FR-OBS-26)


def setup_config(path: Path = SETUP_PATH) -> dict[str, Any]:
    """VEC-OBS-6: the deployed file at SETUP_PATH when it exists, else the package's copy."""
    if path.is_file():
        loaded: dict[str, Any] = _load(path)
        return loaded
    return SETUP


class MlflowRefused(Exception):
    """MLflow answered 4xx (not a caller-handled 404): the job ends dead with the caller's class name."""

    def __init__(self, status: int) -> None:
        super().__init__(str(status))
        self.status = status


def declared(method: str, path: str) -> bool:
    return any(m == method and rx.match(path) for m, rx in _TEMPLATES)


async def request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    body: Mapping[str, Any] | None = None,
    params: Mapping[str, str] | None = None,
    ok404: bool = False,
) -> tuple[int, dict[str, Any]]:
    """One declared MLflow request. 5xx and transport errors raise (transient); 4xx raises MlflowRefused,
    except a 404 when `ok404` (the caller decides). Returns (status, JSON body)."""
    if not declared(method, path):
        raise ValueError(f"undeclared MLflow request {method} {path}")
    response = await client.request(
        method,
        MLFLOW + path,
        json=dict(body) if body is not None else None,
        params=dict(params) if params is not None else None,
        timeout=HTTP_TIMEOUT_S,
    )
    if response.status_code >= 500:
        raise httpx.HTTPStatusError("mlflow", request=response.request, response=response)
    if response.status_code == 404 and ok404:
        return 404, {}
    if response.status_code >= 400:
        raise MlflowRefused(response.status_code)
    try:
        data = response.json()
    except ValueError:
        data = {}
    return response.status_code, data if isinstance(data, dict) else {}


def _enum_from(source: str) -> frozenset[str]:
    """The two VEC-OBS-5 `enum_from` sources: the priced models, and the pipeline's declared stages."""
    if source.startswith("pricing.yaml"):
        from reader.agent.pricing import PRICING_FILE

        return frozenset(_load(PRICING_FILE)["models"])
    if source.startswith("SRV-PIPELINE"):
        stages = _load(HERE.parent / "pipeline" / "stages.yaml")
        items = stages.get("stages", stages) if isinstance(stages, dict) else stages
        names = items if isinstance(items, dict) else {s.get("name") or s.get("id") for s in items or []}
        return frozenset(str(n) for n in names if n)
    if source == "reasons":
        return frozenset(VEC["labels"]["reasons"])
    return frozenset()


def check(value: object, rule: object) -> bool:
    """A VEC-OBS-5 rule: a pattern name, a literal regex, or {enum|int|float|bool} forms."""
    if isinstance(rule, str):
        pattern = PATTERNS.get(rule, rule)
        if isinstance(pattern, Mapping):
            return check(value, pattern)  # a named enum_from pattern (model, stage)
        return (
            isinstance(value, str) and isinstance(pattern, str) and re.fullmatch(pattern, value) is not None
        )
    if isinstance(rule, Mapping):
        if "enum" in rule:
            return value in rule["enum"]
        if "int" in rule:
            low, high = rule["int"]
            return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
        if "float" in rule:
            low, high = rule["float"]
            return isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high
        if "enum_from" in rule:
            return isinstance(value, str) and value in _enum_from(str(rule["enum_from"]))
    return False


STAGE_SQL: Final = (
    "INSERT INTO job_keys (kind, key, args, created_at, updated_at)"
    " VALUES (:kind, :key, CAST(:args AS jsonb), now(), now()) ON CONFLICT DO NOTHING"
)


async def stage(conn: Any, kind: Any, payload: Mapping[str, Any]) -> None:
    """The outbox (FR-OBS-23, -24): the job's key row, in the caller's transaction. SRV-EVENTS' reconcile
    sweep defers a key row that has no live job, so a commit always yields the job; `Jobs.enqueue`
    afterwards (when the api holds a queue) only makes it prompt, and is idempotent on the same key."""
    import json

    from sqlalchemy import text

    from reader.jobs.model import idempotency_key

    args = kind.args.model_validate(dict(payload)).model_dump(mode="json")
    await conn.execute(
        text(STAGE_SQL),
        {"kind": kind.name, "key": idempotency_key(kind, args), "args": json.dumps(args, sort_keys=True)},
    )


def tracing_on() -> bool:
    from reader.obs import _STATE

    return _STATE["provider"] is not None
