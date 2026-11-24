"""The Prompts tab (SRV-OBS FR-OBS-15, FR-OBS-18; ADR-0035): job kind `obs.prompts` registers the repository's
static instruction files (VEC-OBS-4 `prompts`, never book text) in MLflow's prompt registry, one version per
sha256;
`version_of` finds the registered version of a template for the linked-prompts tag, cached per process. Every
request is built from the file's bytes and fixed tags only."""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field

from reader.jobs.model import JobKind
from reader.obs.analytics import PROMPTS

log = logging.getLogger("reader.obs")
KIND: Final = "obs.prompts"
MLFLOW: Final = "http://mlflow:5000"
HTTP_TIMEOUT_S: Final = 10.0
LOOKUP_TIMEOUT_S: Final = 2.0
MISS_TTL_S: Final = 600.0  # a miss or a failed lookup is retried at most once per name per 10 minutes
SHA_TAG: Final = "reader.prompt_sha256"
VERSION_TAG: Final = "reader.version"
SOURCE: Final = "dummy-source"  # MLflow 3.18.0 accepts only its own placeholder as a prompt version's source
_PACKAGE: Final = Path(__file__).resolve().parents[1]  # the installed `reader` package
_PREFIX: Final = "server/src/reader/"
_CACHE: dict[str, int] = {}
_MISSES: dict[str, float] = {}  # name -> monotonic time after which a lookup may run again


class PromptsRefused(Exception):
    """MLflow answered 4xx: the key ends dead with this class name."""


class PromptsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(pattern=r"^[0-9A-Za-z.+-]{1,64}$")
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def digest(day: str) -> str:
    """FR-OBS-15: the key, sha256 of the templates' sha256s in VEC-OBS-4 order and the start day (UTC)."""
    parts = [template(name)[1] for name in PROMPTS] + [day]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def template_path(name: str) -> Path:
    return _PACKAGE / PROMPTS[name].removeprefix(_PREFIX)


def template(name: str) -> tuple[str, str]:
    """The file's text and its sha256 (hex)."""
    raw = template_path(name).read_bytes()
    return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()


def _versions(data: Any) -> list[dict[str, Any]]:
    found = (data or {}).get("model_versions") or []
    return [v for v in found if isinstance(v, dict)]


def _tag(version: dict[str, Any], key: str) -> str | None:
    for tag in version.get("tags") or []:
        if isinstance(tag, dict) and tag.get("key") == key:
            value = tag.get("value")
            return value if isinstance(value, str) else None
    return None


def search_filter(name: str) -> str:
    """MLflow 3.18 leaves prompt versions out of a model-version search unless the filter asks for prompts
    (its store anti-joins every `mlflow.prompt.is_prompt` row otherwise; found by the [image] test)."""
    return f"name='{name}' AND tags.`mlflow.prompt.is_prompt` = 'true'"


async def _search(client: httpx.AsyncClient, name: str, limit_s: float) -> list[dict[str, Any]]:
    got = await client.get(f"{MLFLOW}/api/2.0/mlflow/model-versions/search",
                           params={"filter": search_filter(name), "max_results": 1000}, timeout=limit_s)
    _raise_for(got)
    return _versions(got.json())


def _matching(versions: list[dict[str, Any]], sha: str) -> int | None:
    for version in versions:
        number = version.get("version")
        if _tag(version, SHA_TAG) == sha and isinstance(number, str) and number.isdigit():
            return int(number)
    return None


def registration_requests(name: str, package_version: str) -> list[tuple[str, dict[str, Any]]]:
    """The two POSTs that create a prompt and its version (INV-OBS-2's canary covers their bodies)."""
    text, sha = template(name)
    return [
        ("/api/2.0/mlflow/registered-models/create",
         {"name": name, "tags": [{"key": "mlflow.prompt.is_prompt", "value": "true"}]}),
        ("/api/2.0/mlflow/model-versions/create",
         {"name": name, "source": SOURCE, "tags": [
             {"key": "mlflow.prompt.is_prompt", "value": "true"},
             {"key": "mlflow.prompt.text", "value": text},
             {"key": "_mlflow_prompt_type", "value": "text"},
             {"key": SHA_TAG, "value": sha},
             {"key": VERSION_TAG, "value": package_version},
         ]}),
    ]


async def register(client: httpx.AsyncClient, name: str, package_version: str) -> int | None:
    """The version whose sha tag equals the file's; created only when none does."""
    _, sha = template(name)
    found = _matching(await _search(client, name, HTTP_TIMEOUT_S), sha)
    if found is not None:
        return found
    (model_path, model_body), (version_path, version_body) = registration_requests(name, package_version)
    created = await client.post(MLFLOW + model_path, json=model_body, timeout=HTTP_TIMEOUT_S)
    if created.status_code >= 400 and "RESOURCE_ALREADY_EXISTS" not in created.text:
        _raise_for(created)
    made = await client.post(MLFLOW + version_path, json=version_body, timeout=HTTP_TIMEOUT_S)
    _raise_for(made)
    number = ((made.json() or {}).get("model_version") or {}).get("version")
    return int(number) if isinstance(number, str) and number.isdigit() else None


def _raise_for(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    if response.status_code < 500:
        raise PromptsRefused(str(response.status_code))
    raise httpx.HTTPStatusError("mlflow", request=response.request, response=response)


async def handle(args: PromptsArgs, conn: Any, ctx: Any) -> None:
    from reader.obs import setup as obs_setup

    if not obs_setup.current_settings().enabled:
        return
    async with httpx.AsyncClient(trust_env=False) as client:
        for name in PROMPTS:
            number = await register(client, name, args.version)
            if number is not None:
                _CACHE[name] = number
                _MISSES.pop(name, None)


async def version_of(name: str) -> int | None:
    """FR-OBS-18: the registered version of `name`'s current file, cached per process; None when tracing is
    off, MLflow is unreachable or the file is not registered yet. Never raises."""
    from reader.obs import _STATE

    if _STATE["provider"] is None or name not in PROMPTS:
        return None
    if name in _CACHE:
        return _CACHE[name]
    if _MISSES.get(name, 0.0) > time.monotonic():
        return None
    found: int | None = None
    try:
        _, sha = template(name)
        async with httpx.AsyncClient(trust_env=False) as client:
            found = _matching(await _search(client, name, LOOKUP_TIMEOUT_S), sha)
    except Exception:
        found = None
    if found is None:
        _MISSES[name] = time.monotonic() + MISS_TTL_S
        return None
    _CACHE[name] = found
    return found


async def on_worker_start(jobs: Any, now: datetime | None = None) -> None:
    """Enqueue-only (FR-EVENTS-1 hooks): a failure is logged and the next start retries."""
    from reader.obs.export import reader_version

    try:
        day = (now or datetime.now(UTC)).date().isoformat()
        await jobs.enqueue(KIND, {"version": reader_version(), "digest": digest(day)})
    except Exception:
        log.warning("obs prompts not enqueued")


KIND_DECL: Final = JobKind(
    name=KIND,
    queue="obs",
    args=PromptsArgs,
    key=("digest",),
    handler=handle,
    max_attempts=3,
    transient=(httpx.TransportError, httpx.HTTPStatusError),
    timeout_s=60,
)
