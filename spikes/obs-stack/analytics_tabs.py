"""Spike round 4, every tab (never shipped; the owner's "use all tabs, best practices"). VARIANT=before replays
today's span shapes only; VARIANT=after replays the proposed V.obs-analytics data, all synthetic, ids/enums/numbers:

- traces: span types on every span we own (CHAIN, AGENT, RETRIEVER, TOOL, PARSER, EMBEDDING), root request/response
  previews holding ids and enums only (`mlflow.spanInputs`/`mlflow.spanOutputs` as JSON strings), root tags
  (`mlflow.traceTag.reader.*`: kind, stage, lesson, run, attempt, outcome, error code/class, model, task, cache share),
  the trace name, the session (compile run id; tutor lesson id), the linked prompt versions;
- deeper child spans: tutor retrieval and citation check, lab kernel run, ingest parse/chunk, embed batches, verify
  sub-steps, with counts, sizes and durations as numbers;
- assessments: code-sourced feedback on traces (verify verdict, fact-check pass with counts, lab verification, freshness
  verdict, tutor citation check) through MLflow's client;
- prompts: our own repository templates (synthetic stand-ins here, no book text), versions tagged with a git sha.
Writes /out/tabs-data-<VARIANT>.json and /out/browser-ids.json. Zero credit; no network beyond the spike networks.
"""

import json
import os
import sys
import time

sys.path.insert(0, "/spike")
import analytics as a  # noqa: E402
from opentelemetry import context as otel_context  # noqa: E402
from opentelemetry import trace  # noqa: E402
from opentelemetry.trace import SpanKind, Status, StatusCode  # noqa: E402

VARIANT = os.environ.get("VARIANT", "after")
GIT_SHA = "2ffc1ee077d4736b7b1d4d4eaa016c3700c65397"
LESSON = "20000000-0000-4000-8000-0000000000bb"
RUN = a.RUN
MS = 1_000_000
OUT: dict = {"variant": VARIANT, "traces": {}, "assessments": [], "prompts": []}


def span(name: str, start: int, dur_ms: int, attrs: dict, tracer=None, error: bool = False) -> None:
    s = (tracer or a.ours).start_span(name, start_time=start, attributes=attrs)
    if error:
        s.set_status(Status(StatusCode.ERROR))
    s.end(end_time=start + dur_ms * MS)


def rich_root(name: str, kind: str, start: int, attrs: dict, tags: dict, inputs: dict, outputs: dict,
              session: str | None, prompts: list[dict] | None = None) -> trace.Span:
    full = {"mlflow.spanType": "CHAIN", "mlflow.traceTag.mlflow.traceName": name,
            "mlflow.spanInputs": json.dumps(inputs), "mlflow.spanOutputs": json.dumps(outputs), **attrs}
    for k, v in tags.items():
        full[f"mlflow.traceTag.{k}"] = str(v)
    if session:
        full["session.id"] = session
    if prompts:
        full["mlflow.traceTag.mlflow.linkedPrompts"] = json.dumps(prompts)
    return a.ours.start_span(name, context=otel_context.Context(), kind=SpanKind.CONSUMER, start_time=start,
                             attributes=full)


COURSE = "50000000-0000-4000-8000-0000000000cc"
LESSON_2 = "20000000-0000-4000-8000-0000000000dd"
# pricing.yaml's per-token prices (USD per million) for the input/output split of the ledger's settled amount.
# server/src/reader/agent/pricing.yaml, claude-sonnet-5-5 (the run of 20261124T105821 used 3/15/0.3/3.75 here; the
# totals are the ledger amounts either way, only the input/output split moves).
PRICE = {"input": 2.0, "output": 10.0, "cache_read": 0.1, "cache_write": 2.5}


def ledger_cost(usage: tuple[int, int, int, int], settled: float) -> str:
    """`mlflow.llm.cost` from the ledger: total = the settled amount; the input/output split follows pricing.yaml's
    prices scaled to that total (the ledger records the total only)."""
    inp, out, cread, cwrite = usage
    i = (inp * PRICE["input"] + cread * PRICE["cache_read"] + cwrite * PRICE["cache_write"]) / 1e6
    o = out * PRICE["output"] / 1e6
    scale = settled / (i + o) if i + o else 0.0
    return json.dumps({"input_cost": round(i * scale, 6), "output_cost": round(o * scale, 6), "total_cost": settled})


def agent_call(start: int, dur_ms: int, usage: tuple[int, int, int, int], task: str, stage: str | None,
               settled: float = 0.0, lesson: str = LESSON, step: int | None = None) -> None:
    inp, out, cread, cwrite = usage
    attrs = {"mlflow.spanType": "AGENT", "gen_ai.operation.name": "invoke_agent", "gen_ai.provider.name": "anthropic",
             "gen_ai.request.model": a.MODEL, "reader.task": task, "gen_ai.usage.input_tokens": inp + cread + cwrite,
             "gen_ai.usage.output_tokens": out, "gen_ai.usage.cache_read.input_tokens": cread,
             "gen_ai.usage.cache_write.input_tokens": cwrite, "reader.cost_usd": settled,
             "mlflow.llm.cost": ledger_cost(usage, settled), "reader.lesson.id": lesson, "reader.course.id": COURSE}
    if stage:
        attrs["reader.stage"] = stage
    if step is not None:
        attrs["reader.step"] = step
    call = a.ours.start_span("agent.call", kind=SpanKind.CLIENT, start_time=start, attributes=attrs)
    with trace.use_span(call, end_on_exit=False):
        a._cli_children(start + MS, dur_ms - 2, usage)
    call.end(end_time=start + dur_ms * MS)


def tutor(name: str, offset_s: float, dur_ms: int, qid: str, cited: int, ok: bool, settled: float = 0.04,
          lesson: str = LESSON) -> str:
    start = a.NOW - int(offset_s * 1e9)
    root = rich_root("job tutor.answer", "tutor.answer", start,
                     {"reader.job.kind": "tutor.answer", "reader.question_id": qid, "reader.job.outcome": "succeeded"},
                     {"reader.job.kind": "tutor.answer", "reader.lesson.id": lesson, "reader.course.id": COURSE,
                      "reader.task": "tutor", "reader.model": a.MODEL, "reader.job.attempt": 0,
                      "reader.job.outcome": "succeeded", "reader.cache_read_share": "0.91",
                      "reader.cost_usd": f"{settled:.4f}"},
                     {"question_id": qid, "lesson_id": lesson}, {"outcome": "answered", "citations": cited},
                     lesson, [{"name": "reader.tutor", "version": "1"}])
    with trace.use_span(root, end_on_exit=False):
        span("tutor.retrieve", start + MS, 180, {"mlflow.spanType": "RETRIEVER", "reader.retrieval.hits": 8,
                                                  "reader.retrieval.candidates": 40, "reader.retrieval.top_score": 0.83})
        agent_call(start + 200 * MS, dur_ms - 400, (20, 300 + dur_ms // 30, 9000, 0), "tutor", None, settled, lesson)
        span("tutor.citations", start + (dur_ms - 150) * MS, 40,
             {"mlflow.spanType": "PARSER", "reader.citations.total": cited, "reader.citations.valid": cited if ok else cited - 1})
    root.end(end_time=start + dur_ms * MS)
    tid = format(root.get_span_context().trace_id, "032x")
    OUT["traces"][name] = tid
    return tid


def stage(name: str, offset_s: float, st: str, dur_ms: int, outcome: str = "succeeded", code: str | None = None,
          settled: float = 0.21, run_spent: float = 0.0, lesson: str = LESSON, run: str = RUN, step: int = 2,
          attempt: int = 0) -> str:
    start = a.NOW - int(offset_s * 1e9)
    tags = {"reader.job.kind": "pipeline.stage", "reader.stage": st, "reader.lesson.id": lesson,
            "reader.course.id": COURSE, "reader.run_id": run, "reader.step": step, "reader.job.attempt": attempt,
            "reader.job.outcome": outcome, "reader.task": st, "reader.model": a.MODEL,
            "reader.cache_read_share": "0.78", "reader.cost_usd": f"{settled:.4f}",
            "reader.run.spent_usd": f"{run_spent:.4f}"}
    if code:
        tags["reader.error_code"] = code
    root = rich_root("job pipeline.stage", "pipeline.stage", start,
                     {"reader.job.kind": "pipeline.stage", "reader.stage": st, "reader.run_id": run,
                      "reader.step": step, "reader.run.spent_usd": run_spent},
                     tags, {"run_id": run, "lesson_id": lesson, "stage": st, "step": step},
                     {"outcome": outcome, "error_code": code} if code else {"outcome": outcome},
                     run, [{"name": f"reader.{st}", "version": "1"}])
    with trace.use_span(root, end_on_exit=False):
        agent_call(start + MS, dur_ms - 6_000, (50, 4000, 30000, 8000), st, st, settled, lesson, step)
        v0 = start + (dur_ms - 5_000) * MS
        span("verify.claims", v0, 1_200, {"mlflow.spanType": "PARSER", "reader.claims.total": 14,
                                           "reader.claims.supported": 13 if outcome == "succeeded" else 9})
        span("verify.lab", v0 + 1_300 * MS, 3_000, {"mlflow.spanType": "TOOL", "reader.kernel.cells": 12,
                                                     "reader.kernel.cells_ok": 12 if outcome == "succeeded" else 10,
                                                     "reader.kernel.ms": 2_950}, error=outcome != "succeeded")
    if outcome != "succeeded":
        root.set_status(Status(StatusCode.ERROR))
    root.end(end_time=start + dur_ms * MS)
    tid = format(root.get_span_context().trace_id, "032x")
    OUT["traces"][name] = tid
    return tid


def ingest(name: str, offset_s: float) -> str:
    start = a.NOW - int(offset_s * 1e9)
    sha = "aa11" * 16
    root = rich_root("job ingest.import", "ingest.import", start,
                     {"reader.job.kind": "ingest.import", "reader.material_sha256": sha},
                     {"reader.job.kind": "ingest.import", "reader.job.outcome": "succeeded", "reader.job.attempt": 0},
                     {"material_sha256": sha}, {"outcome": "succeeded", "pages": 594, "chunks": 2310}, None)
    with trace.use_span(root, end_on_exit=False):
        span("ingest.parse", start + MS, 60_000, {"mlflow.spanType": "PARSER", "reader.pages": 594, "reader.bytes": 18_400_000})
        span("ingest.chunk", start + 61_000 * MS, 9_000, {"mlflow.spanType": "PARSER", "reader.chunks": 2310})
        for i in range(4):
            span("embed.batch", start + (70_000 + i * 12_000) * MS, 11_500,
                 {"mlflow.spanType": "EMBEDDING", "reader.embed.vectors": 64, "reader.embed.batch": i})
    root.end(end_time=start + 120_000 * MS)
    tid = format(root.get_span_context().trace_id, "032x")
    OUT["traces"][name] = tid
    return tid


def assessments_and_prompts() -> None:
    import mlflow
    from mlflow.entities import AssessmentSource

    mlflow.set_tracking_uri("http://mlflow:5000")
    code = lambda sid: AssessmentSource(source_type="CODE", source_id=sid)  # noqa: E731
    t = OUT["traces"]
    plan = [
        (t["stage_fact_check"], "fact_check.passed", True, "reader.pipeline.fact_check", {"claims": "14", "supported": "13"}),
        (t["stage_review_dead"], "fact_check.passed", False, "reader.pipeline.fact_check", {"claims": "14", "supported": "9"}),
        (t["stage_fact_check"], "verify.lab_passed", True, "reader.pipeline.verify", {"cells": "12", "cells_ok": "12"}),
        (t["stage_review_dead"], "verify.lab_passed", False, "reader.pipeline.verify", {"cells": "12", "cells_ok": "10"}),
        (t["stage_fact_check"], "freshness.verdict", "current", "reader.freshness", {}),
        (t["tutor_fast"], "tutor.citations_valid", True, "reader.tutor.citations", {"total": "3", "valid": "3"}),
        (t["tutor_slow"], "tutor.citations_valid", False, "reader.tutor.citations", {"total": "4", "valid": "3"}),
        (t["tutor_slow"], "tutor.citation_share", 0.75, "reader.tutor.citations", {}),
    ]
    for tid, name, value, sid, meta in plan:
        try:
            fb = mlflow.log_feedback(trace_id=f"tr-{tid}", name=name, value=value, source=code(sid), metadata=meta)
            OUT["assessments"].append({"name": name, "ok": True, "id": getattr(fb, "assessment_id", None)})
        except Exception as e:  # noqa: BLE001
            OUT["assessments"].append({"name": name, "ok": False, "error": str(e)[:200]})
    templates = {
        "reader.tutor": "Answer the learner's question about lesson {{lesson_id}} using only the cited spans.",
        "reader.fact_check": "Check each claim of step {{step}} against its source spans; report supported or not.",
        "reader.review": "Review the draft of step {{step}} for clarity and correctness against the plan.",
    }
    for pname, template in templates.items():
        try:
            pv = mlflow.genai.register_prompt(name=pname, template=template, commit_message=f"git {GIT_SHA[:12]}",
                                              tags={"reader.git_sha": GIT_SHA})
            OUT["prompts"].append({"name": pname, "version": getattr(pv, "version", None)})
        except Exception as e:  # noqa: BLE001
            OUT["prompts"].append({"name": pname, "error": str(e)[:200]})


def main() -> None:
    for _ in range(60):
        try:
            import urllib.request
            if urllib.request.urlopen("http://mlflow:5000/health", timeout=2).status == 200:
                break
        except Exception:  # noqa: BLE001
            time.sleep(1)
    if VARIANT == "before":
        for n, kind, off, ms_, usage, usd, st, oc, code in [
            ("tutor_fast", "tutor.answer", 500, 2000, (20, 200, 9000, 0), 0.03, None, "succeeded", None),
            ("tutor_slow", "tutor.answer", 400, 9000, (30, 900, 9000, 2000), 0.11, None, "succeeded", None),
            ("stage_fact_check", "pipeline.stage", 300, 40000, (50, 4000, 30000, 8000), 0.21, "fact_check", "succeeded", None),
            ("stage_review_dead", "pipeline.stage", 200, 60000, (50, 6000, 30000, 8000), 0.30, "review", "dead", "exhausted"),
        ]:
            OUT["traces"][n] = a.job(n, kind, "baseline", offset_s=off, ms=ms_, usage=usage, ledger_usd=usd, stage=st,
                                     outcome=oc, code=code)
        a.http_span("http_ok", "/api/obs/summary", 200, 50, 30)
    else:
        tutor("tutor_fast", 500, 2000, "30000000-0000-4000-8000-000000000001", 3, True, 0.03)
        tutor("tutor_slow", 400, 9000, "30000000-0000-4000-8000-000000000002", 4, False, 0.11)
        tutor("tutor_lesson2", 380, 3000, "30000000-0000-4000-8000-000000000003", 2, True, 0.05, LESSON_2)
        # Lesson 1, run 1: fact_check's first attempt failed after billing 0.23 (a failed paid attempt), the retry
        # settled 0.21; review died. Lesson 2, run 2: one draft step.
        stage("stage_fact_check_failed_paid", 320, "fact_check", 30000, "failed", "Transient", 0.23, 0.85, attempt=0)
        stage("stage_fact_check", 300, "fact_check", 40000, settled=0.21, run_spent=1.06, attempt=1)
        stage("stage_review_dead", 200, "review", 60000, "dead", "exhausted", settled=0.30, run_spent=1.36, step=3)
        stage("stage_lesson2_draft", 180, "draft", 50000, settled=0.62, run_spent=0.62,
              lesson=LESSON_2, run="10000000-0000-4000-8000-0000000000ee", step=1)
        ingest("ingest_import", 260)
        OUT["traces"]["compile_like"] = a.compile_like("compile_like", 150)
        a.http_span("http_ok", "/api/obs/summary", 200, 50, 30)
    a.provider.force_flush()
    a.cli_provider.force_flush()
    time.sleep(8)
    if VARIANT == "after":
        assessments_and_prompts()
        # Per-lesson cost in MLflow: can the metrics API group or filter cost by a lesson tag?
        sum_ = [{"aggregation_type": "SUM"}]
        OUT["lesson_cost"] = {
            "span cost by model": a.metrics("SPANS", "total_cost", sum_, ["span_model_name"]),
            "span cost by span name (expected refused)": a.metrics("SPANS", "total_cost", sum_, ["span_name"]),
            "trace cost total": a.metrics("TRACES", "total_cost", sum_),
            **{f"trace cost, lesson {les[-2:]} (tag filter)": a.metrics(
                "TRACES", "total_cost", sum_, None, [f"trace.tag.`reader.lesson.id` = '{les}'"]) for les in (LESSON, LESSON_2)},
            "span cost grouped by tag (expected refused)": a.metrics("SPANS", "total_cost", sum_, ["tag.reader.lesson.id"]),
            **{f"span cost, lesson {les[-2:]} (trace-tag filter)": a.metrics(
                "SPANS", "total_cost", sum_, None, [f"trace.tag.`reader.lesson.id` = '{les}'"]) for les in (LESSON, LESSON_2)},
            **{f"traces of lesson {les[-2:]} (search)": a.search(f"tag.`reader.lesson.id` = '{les}'")
               for les in (LESSON, LESSON_2)},
            "pipeline traces of lesson 1 (search)": a.search(
                f"tag.`reader.lesson.id` = '{LESSON}' AND tag.`reader.job.kind` = 'pipeline.stage'"),
        }
        names = {v: k for k, v in OUT["traces"].items()}
        for res in OUT["lesson_cost"].values():
            if "matches" in res:
                res["matches"] = [names.get(m, m) for m in res["matches"]]
    OUT["views"] = {k: a.view(v) for k, v in OUT["traces"].items()}
    json.dump(OUT, open(f"/out/tabs-data-{VARIANT}.json", "w"), indent=1, default=str)
    json.dump(OUT["traces"], open("/out/browser-ids.json", "w"))
    print("done", VARIANT, len(OUT["traces"]))


if __name__ == "__main__":
    main()
