"""The MLflow conventions of SRV-OBS FR-OBS-12 and FR-OBS-18 as data (VEC-OBS-4,
spec/fixtures/SRV-OBS/analytics.yaml) and their derivation.

AllowListExporter calls `derive` after validation: every MLflow key comes from the span's name and its
already-validated attributes, never from a caller. JSON values are built here with `json.dumps` of validated
scalars only, so no value can break out of its string. A test compares this module with VEC-OBS-4.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from reader.obs.attributes import DOC_CAP

SPAN_TYPES: Final[Mapping[str, str]] = {
    "job *": "CHAIN",
    "job.session": "CHAIN",
    "agent.call": "AGENT",
    "tutor.retrieve": "RETRIEVER",
    "tutor.citations": "PARSER",
    "verify.claims": "PARSER",
    "verify.lab": "TOOL",
    "kernel.run": "TOOL",
    "ingest.parse": "PARSER",
    "ingest.chunk": "PARSER",
    "embed.batch": "EMBEDDING",
    # ADR-0039 (FR-OBS-36, -37)
    "find": "TOOL",
    "find.route": "ROUTER",
    "find.retrieve": "RETRIEVER",
    "find.preselect": "RERANKER",
    "find.judge_wait": "CHAIN",
    "find.select": "RERANKER",
    "pack.build": "CHAIN",
    "link.run": "CHAIN",
}

TRACE_TAGS: Final = (
    "reader.job.kind", "reader.job.outcome", "reader.job.attempt", "reader.error_code", "reader.error_class",
    "reader.stage", "reader.step", "reader.run_id", "reader.lesson.id", "reader.course.id",
    "reader.question_id",
    "reader.material_sha256", "reader.version_key",  # phase 2: FR-OBS-20 (d), FR-OBS-21 (a)
    "reader.policy_version", "reader.eval_id",  # ADR-0039: FR-OBS-21, -39
)

_RUN_INPUTS: Final = (
    ("run_id", "reader.run_id"), ("lesson_id", "reader.lesson.id"), ("stage", "reader.stage"),
    ("step", "reader.step"),
)
_OUTCOME: Final = (("outcome", "reader.job.outcome"), ("error_code", "reader.error_code"))
PREVIEWS: Final[Mapping[str, Mapping[str, tuple[tuple[str, str], ...]]]] = {
    "tutor.answer": {
        "inputs": (("question_id", "reader.question_id"), ("lesson_id", "reader.lesson.id")),
        "outputs": (("outcome", "reader.job.outcome"), ("tutor_state", "reader.tutor.state"),
                    ("error_code", "reader.error_code")),
    },
    "pipeline.stage": {"inputs": _RUN_INPUTS, "outputs": _OUTCOME},
    "pipeline.verify": {"inputs": _RUN_INPUTS, "outputs": _OUTCOME},
    "ingest.import": {"inputs": (("material_sha256", "reader.material_sha256"),), "outputs": _OUTCOME},
}
FORBIDDEN_PREVIEW_KEYS: Final = frozenset(
    {"model", "messages", "prompt", "input", "output", "content", "text"}
)

# FR-OBS-36, FR-OBS-12 (f) as amended: the documents list on a span carrying reader.doc.* keys.
RETRIEVER_DOCUMENTS: Final[Mapping[str, Any]] = {
    "family": "reader.doc.<i>",
    "key": "mlflow.spanOutputs",
    "document": {"id": "uri", "page_content": "", "metadata": {"doc_uri": "uri", "content_sha256": "sha256"}},
}

JOB_ROOT_KEYS: Final[Mapping[str, tuple[str, ...]]] = {
    "pipeline.stage": ("reader.lesson.id", "reader.course.id", "reader.prompt.version"),
    "pipeline.verify": ("reader.lesson.id", "reader.course.id"),
    "tutor.answer": ("reader.lesson.id", "reader.course.id", "reader.tutor.state", "reader.prompt.version"),
}

LLM_COST: Final = {"span": "agent.call", "total": "reader.cost_usd", "input": "reader.cost_input_usd"}
SESSION: Final = {"span": "job.session", "from": ("reader.run_id", "reader.lesson.id")}
LINKED_PROMPTS: Final[Mapping[str, str]] = {
    "tutor.answer": "reader.tutor",
    "pipeline.stage": "reader.<reader.stage>",
}

# FR-OBS-14 (as amended by ADR-0039), INV-OBS-8: the assessment catalogue; `value` is "bool" or ("float", low,
# high). A CODE entry has a fixed `source_id` and int-range metadata and is written only by obs.feedback; an
# LLM_JUDGE entry has a `source_id_pattern` (SRV-AGENT's `<slot>@<version>`, slot = "judge." + name) and
# metadata {score_id: uuid}, and is written only by obs.judge_score.
_CODE: Final = {"source_type": "CODE", "writer": "obs.feedback"}
_JUDGE: Final = {
    "source_type": "LLM_JUDGE", "writer": "obs.judge_score", "value": ("float", 0, 1),
    "source_id_pattern": r"^judge\.eval\.[a-z_]{1,40}@[0-9a-f]{12}$", "metadata": {"score_id": "uuid"},
}
ASSESSMENTS: Final[Mapping[str, Mapping[str, Any]]] = {
    "fact_check.passed": {**_CODE, "value": "bool", "source_id": "reader.pipeline.fact_check",
                          "metadata": {"claims": (0, 10000), "supported": (0, 10000)}},
    "verify.passed": {**_CODE, "value": "bool", "source_id": "reader.pipeline.verify", "metadata": {}},
    "verify.lab_passed": {**_CODE, "value": "bool", "source_id": "reader.pipeline.verify",
                          "metadata": {"cells": (0, 10000), "cells_ok": (0, 10000)}},
    "tutor.citations_valid": {**_CODE, "value": "bool", "source_id": "reader.tutor.citations",
                              "metadata": {"total": (0, 1000), "valid": (0, 1000)}},
    "tutor.citation_share": {**_CODE, "value": ("float", 0, 1), "source_id": "reader.tutor.citations",
                             "metadata": {}},
    **{f"eval.{slot}": _JUDGE for slot in (
        "relevance", "sufficiency", "groundedness", "self_contained", "citation_supports_claim")},
}

# FR-OBS-15 (as edited by ADR-0039): the static prompt templates, by name and repository path. SRV-AGENT's
# judge_find.md joins when its paired change lands on this branch.
_JUDGE_PROMPTS: Final = "server/src/reader/obs/prompts/"
PROMPTS: Final[Mapping[str, str]] = {
    "reader.draft": "server/src/reader/pipeline/prompts/draft.v1.md",
    "reader.fact_check": "server/src/reader/pipeline/prompts/fact_check.v1.md",
    "reader.adversarial_review": "server/src/reader/pipeline/prompts/adversarial_review.v1.md",
    "reader.revise": "server/src/reader/pipeline/prompts/revise.v1.md",
    "reader.tutor": "server/src/reader/tutor/prompts/answer-v1.md",
    **{f"reader.judge.{slot}": f"{_JUDGE_PROMPTS}judge_{slot.replace('.', '_')}.md" for slot in (
        "eval.relevance", "eval.sufficiency", "eval.groundedness", "eval.self_contained",
        "eval.citation_supports_claim", "align")},
}

# VEC-OBS-4 `job_kinds`: the worker's MLflow-writing and evaluation kinds this module's tables cover.
JOB_KINDS: Final = ("obs.feedback", "obs.prompts", "obs.eval", "obs.eval_run", "obs.eval_sample",
                    "obs.judge_score", "obs.align")

_MICRO: Final = Decimal("0.000001")


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def money(value: float) -> Decimal:
    """A validated float as the ledger's 6-place Decimal (the ledger's rounding, half up)."""
    return Decimal(str(value)).quantize(_MICRO, ROUND_HALF_UP)


def llm_cost(attributes: Mapping[str, object]) -> str | None:
    """FR-OBS-12 (c): {input_cost, output_cost, total_cost}, the parts summing to the total at 6 places."""
    total = attributes.get(LLM_COST["total"])
    if not isinstance(total, float):
        return None
    whole = money(total)
    given = attributes.get(LLM_COST["input"])
    part = min(money(given), whole) if isinstance(given, float) else Decimal(0).quantize(_MICRO)
    rest = whole - part  # Decimal - Decimal: exact at 6 places, serialized from the Decimal itself
    return _dump({"input_cost": float(str(part)), "output_cost": float(str(rest)),
                  "total_cost": float(str(whole))})


def preview(kind: object, side: str, attributes: Mapping[str, object]) -> str | None:
    table = PREVIEWS.get(kind) if isinstance(kind, str) else None
    if table is None:
        return None
    shown = {name: attributes[key] for name, key in table[side] if key in attributes}
    return _dump(shown) if shown else None


def linked_prompt(attributes: Mapping[str, object]) -> str | None:
    """FR-OBS-18: the template the job used, by its validated version; nothing for an unknown name."""
    version, kind = attributes.get("reader.prompt.version"), attributes.get("reader.job.kind")
    if not isinstance(version, int) or not isinstance(kind, str) or kind not in LINKED_PROMPTS:
        return None
    name = LINKED_PROMPTS[kind]
    if name == "reader.<reader.stage>":
        stage = attributes.get("reader.stage")
        name = f"reader.{stage}" if isinstance(stage, str) else ""
    if name not in PROMPTS:
        return None
    return _dump([{"name": name, "version": str(version)}])


_DOC: Final = RETRIEVER_DOCUMENTS["family"].replace("<i>", "")  # "reader.doc."


def documents(attributes: Mapping[str, object]) -> str | None:
    """FR-OBS-36: the validated documents in index order, ids and hashes only, `page_content` always empty."""
    if not any(key.startswith(_DOC) for key in attributes):
        return None
    shown = []
    for i in range(DOC_CAP):
        uri, sha = attributes.get(f"{_DOC}{i}.uri"), attributes.get(f"{_DOC}{i}.sha256")
        if isinstance(uri, str) and isinstance(sha, str):
            shown.append({"id": uri, "page_content": "", "metadata": {"doc_uri": uri, "content_sha256": sha}})
    return _dump(shown) if shown else None


def derive(span_class: str, name: str, attributes: Mapping[str, object], root: bool) -> dict[str, str]:
    """The MLflow keys for one validated span (FR-OBS-12 a, c, d, e, f and FR-OBS-18)."""
    out: dict[str, str] = {}
    kind = SPAN_TYPES.get("job *" if span_class == "job" else span_class)
    if kind is not None:
        out["mlflow.spanType"] = kind
    if root and span_class in ("job", "http", "ws"):
        out["mlflow.traceTag.mlflow.traceName"] = name
    if root and span_class == "job":
        for key in TRACE_TAGS:
            if key in attributes:
                out[f"mlflow.traceTag.{key}"] = str(attributes[key])
        job_kind = attributes.get("reader.job.kind")
        for side, key in (("inputs", "mlflow.spanInputs"), ("outputs", "mlflow.spanOutputs")):
            if (shown := preview(job_kind, side, attributes)) is not None:
                out[key] = shown
        if (linked := linked_prompt(attributes)) is not None:
            out["mlflow.traceTag.mlflow.linkedPrompts"] = linked
    if span_class == LLM_COST["span"] and (cost := llm_cost(attributes)) is not None:
        out["mlflow.llm.cost"] = cost
    if span_class == SESSION["span"]:
        for key in SESSION["from"]:
            value = attributes.get(key)
            if isinstance(value, str):
                out["session.id"] = value
                break
    if (docs := documents(attributes)) is not None:
        out[RETRIEVER_DOCUMENTS["key"]] = docs
    return out
