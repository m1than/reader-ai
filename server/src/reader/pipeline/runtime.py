"""What a pipeline step needs from outside the database: provider, lab verifier, span reader, blobs, config.

Tests configure their own; the worker and the api build the default from the environment on first use.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from reader.config import ReaderConfig, load_config
from reader.pipeline import prompts as prompts_mod
from reader.pipeline import stages as stages_mod
from reader.pipeline.prompts import Prompts
from reader.pipeline.source import DbSpanReader, SpanReader
from reader.pipeline.stages import Pipeline


@dataclass
class Runtime:
    provider: Any  # reader.agent.provider.Provider
    verify: Any  # reader.pipeline.verify.VerifyClient (run(notebook) -> LabReport)
    spans: SpanReader
    blobs_root: Path
    pipeline: Pipeline
    prompts: Prompts
    config: ReaderConfig
    jobs: Callable[[], Any]  # the Jobs a step enqueues through


_RUNTIME: list[Runtime | None] = [None]


def configure(runtime: Runtime | None) -> None:
    _RUNTIME[0] = runtime


def get(engine: AsyncEngine) -> Runtime:
    current = _RUNTIME[0]
    if current is None:
        current = default(engine)
        _RUNTIME[0] = current
    return current


def default(engine: AsyncEngine) -> Runtime:
    from reader import blobs
    from reader.agent.provider import AgentProvider, HttpProxyClient
    from reader.jobs import running
    from reader.pipeline.verify import VerifyClient

    config = load_config(Path(os.environ.get("READER_CONFIG", "/etc/reader/reader.yaml")))
    return Runtime(
        provider=AgentProvider(engine, config, HttpProxyClient()),
        verify=VerifyClient(),
        spans=DbSpanReader(),
        blobs_root=blobs.DEFAULT_ROOT,
        pipeline=stages_mod.load(),
        prompts=prompts_mod.load(),
        config=config,
        jobs=running,
    )
