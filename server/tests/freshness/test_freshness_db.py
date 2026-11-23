# @trace FR-FRESH-1 FR-FRESH-3 FR-FRESH-7 FR-FRESH-8 FR-FRESH-9 FR-FRESH-10 FR-FRESH-11 FR-FRESH-12
# @trace FR-FRESH-13 INV-FRESH-2 INV-FRESH-4
"""[db] The routes, the two jobs and the store against a migrated Postgres, with a fake Provider that writes
real ledger rows and drives the search gate, and fake Jobs. No provider call leaves the process."""

from __future__ import annotations

import json
import logging
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from .conftest import (
    MAT,
    FakeJobs,
    FakeProvider,
    Pg,
    Reply,
    add_material,
    client,
    deps,
    execute,
    need,
    rows,
    run_job,
    scalar,
    world,
)

pytestmark = pytest.mark.anyio

BIB = {
    "title": "Practical Distributed Ledgers",
    "authors": ["Ada Example"],
    "edition": "2nd",
    "year": 2017,
    "publisher": "Example Press",
}
SCAN_BIB = {k: BIB[k] for k in ("title", "authors", "edition", "year")}
CLAIM = {"span": "s3", "subject": "consensus protocol v1", "kind": "protocol", "why": "w"}
CLAIM["confidence"] = "high"
SCAN_OUT = json.dumps({"bibliography": SCAN_BIB, "claims": [CLAIM]})
REPORT_OUT = json.dumps(
    {
        "verdict": "partly_outdated",
        "reasons": [{"text": "A newer edition exists.", "citations": ["r1"]}],
        "claims": [{"id": "c1", "status": "changed", "note": "v3 is current.", "citations": ["r3"]}],
        "recommendations": [
            {"source": "r1", "date": "2024-06", "kind": "book_edition", "why": "Newer.", "covers": [0]},
            {"source": "r3", "kind": "official_docs", "why": "Current spec.", "covers": [2, 99]},
            {"source": "r5", "date": "2030", "kind": "course", "why": "Hands-on.", "covers": []},
        ],
    }
)
SUMMARY = "Unique summary text that must never be stored."
GET_KEYS = {
    "report", "stale", "stale_reason", "bibliography", "scan_bibliography", "estimate", "daily_cap_usd",
}
REPORT_KEYS = {
    "id", "status", "created_at", "finished_at", "error", "progress", "verdict", "reasons", "claims",
    "claim_status", "recommendations", "topics", "dropped",
}


def test_read_shapes_equal_the_web_fixture() -> None:
    """FR-FRESH-10/-11: the keys the server pins are the keys WEB-FRESH's card reads (VEC-FRESHUI-1), and
    the daily cap refusal has the same status on both sides (429, as SRV-TUTOR's daily cap)."""
    repo = Path(__file__).resolve().parents[3]
    web = json.loads((repo / "spec" / "fixtures" / "WEB-FRESH" / "bodies.json").read_text(encoding="utf-8"))
    for state in web["states"].values():
        body = state.get("body", state)
        assert set(body) == GET_KEYS
        if body["report"] is not None:
            assert set(body["report"]) == REPORT_KEYS
    assert web["errors"]["daily_budget_exhausted"]["status"] == 429
    assert set(web["errors"]["daily_budget_exhausted"]["body"]) == {"error", "daily_cap_usd", "spent_usd"}


def links(i: int) -> list[tuple[str, str]]:
    return [(f"Result {i}a", f"https://site{i}.example/a"), (f"Result {i}b", f"https://site{i}.example/b")]


def searches_ok(n: int = 5) -> list[Reply]:
    return [Reply(links=links(i), summary=SUMMARY) for i in range(1, n + 1)]


def path(w: Any, handle: str = "ivan") -> str:
    return f"/api/materials/{w.materials[handle]}"


async def post_check(c: Any, w: Any, extra: dict[str, Any] | None = None) -> Any:
    estimate = (await c.get(path(w) + "/freshness")).json()["estimate"]
    return await c.post(path(w) + "/freshness", json={"high_usd": estimate["high_usd"], **(extra or {})})


async def spend_today(w: Any, task: str, usd: str) -> None:
    from reader.agent import ledger

    scope = ledger.Scope(w.users["ivan"], uuid.uuid4(), None, "x", task, 0)
    caps = ledger.Caps(Decimal("100"), None, None, None)
    rid = await ledger.reserve(w.engine, scope, "claude-sonnet-5-5", Decimal(usd), caps)
    record = ledger.UpstreamRecord((200,), ())
    await ledger.settle(w.engine, rid, Decimal(usd), Decimal(usd), Decimal(usd), None, record)


async def report_row(w: Any, report_id: str) -> dict[str, Any]:
    store = need("reader.freshness.store")
    async with w.engine.connect() as conn:
        report = await store.load(conn, uuid.UUID(report_id))
    assert report is not None
    return dict(report.data)


async def full_check(w: Any, tmp: Path, provider: FakeProvider) -> tuple[str, FakeJobs]:
    jobs = FakeJobs()
    async with client(w, jobs=jobs) as c:
        assert (await c.put(path(w) + "/bibliography", json=BIB)).status_code == 200
        response = await post_check(c, w)
        assert response.status_code == 202
        report_id = response.json()["report_id"]
    d = deps(w, provider, jobs)
    await run_job(w, "freshness.scan", uuid.UUID(report_id), d)
    assert jobs.enqueued[-1] == ("freshness.web", {"report_id": report_id})
    await run_job(w, "freshness.web", uuid.UUID(report_id), d)
    return report_id, jobs


# ---- storage -------------------------------------------------------------------------------------------


async def test_tables_are_owned_and_one_check_runs_per_user(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    for table in ("freshness_reports", "material_bibliography"):
        nullable = await scalar(
            w.engine,
            "SELECT is_nullable FROM information_schema.columns"
            " WHERE table_name = :t AND column_name = 'owner_id'",
            t=table,
        )
        assert nullable == "NO"
    index = await scalar(
        w.engine, "SELECT indexdef FROM pg_indexes WHERE indexname = 'freshness_reports_one_running'"
    )
    assert "UNIQUE" in index
    assert "status = 'running'" in index


# ---- routes: shapes, owner only, errors ----------------------------------------------------------------


async def test_get_shape_without_a_check(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    async with client(w) as c:
        body = (await c.get(path(w) + "/freshness")).json()
        by_config_id = (await c.get("/api/materials/book/freshness")).json()
    assert set(body) == GET_KEYS
    assert body == by_config_id
    assert body["report"] is None
    assert body["stale"] is False
    assert body["stale_reason"] is None
    assert body["bibliography"] is None
    assert body["scan_bibliography"] is None
    assert body["daily_cap_usd"] == "2.00"
    assert set(body["estimate"]) == {"low_usd", "high_usd", "cap_usd"}
    assert body["estimate"]["cap_usd"] == "1.00"


async def test_put_bibliography(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    bib = need("reader.freshness.bibliography")
    async with client(w) as c:
        saved = await c.put(path(w) + "/bibliography", json=BIB)
        invalid = await c.put(path(w) + "/bibliography", json={**BIB, "year": 1000})
        unknown = await c.put(path(w) + "/bibliography", json={**BIB, "notes": "x"})
        shown = (await c.get(path(w) + "/freshness")).json()["bibliography"]
    assert saved.status_code == 200
    expected = {**bib.canonical(BIB), "source": "owner", "sha256": bib.sha256(BIB)}
    assert saved.json() == expected
    assert shown == expected
    assert invalid.status_code == 400
    assert invalid.json() == {"error": "bibliography_invalid", "field": "year"}
    assert unknown.json() == {"error": "bibliography_invalid", "field": "notes"}


async def test_only_the_materials_owner_sees_anything(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    report_id, _ = await full_check(w, tmp_path, FakeProvider(w.engine, _replies()))
    not_found = {"error": "not_found"}
    async with client(w, "anna") as anna:  # a learner, even on her own material
        assert (await anna.get(path(w, "anna") + "/freshness")).json() == not_found
        assert (await anna.get(path(w) + "/freshness")).json() == not_found
        assert (await anna.get(path(w) + f"/freshness/{report_id}")).json() == not_found
        assert (await anna.put(path(w) + "/bibliography", json=BIB)).json() == not_found
        assert (await anna.post(path(w) + "/freshness", json={"high_usd": "1"})).json() == not_found
    async with client(w) as ivan:
        other = await ivan.get(path(w, "anna") + "/freshness")
        assert other.status_code == 404
        assert other.json() == not_found
        assert (await ivan.get(f"/api/materials/{uuid.uuid4()}/freshness")).json() == not_found
        assert (await ivan.get(path(w) + f"/freshness/{uuid.uuid4()}")).json() == not_found
        assert (await ivan.get(path(w) + "/freshness/not-a-uuid")).json() == not_found
        own = await ivan.get(path(w) + f"/freshness/{report_id}")
    assert own.status_code == 200
    assert set(own.json()) == REPORT_KEYS
    async with client(w, None) as anonymous:
        assert (await anonymous.get(path(w) + "/freshness")).status_code == 401


async def test_post_estimate_running_and_not_imported(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    other, _ = await add_material(w, "ivan", "f" * 64, [], [], ingested=False, config_id="other")
    jobs = FakeJobs()
    async with client(w, jobs=jobs) as c:
        changed = await c.post(path(w) + "/freshness", json={"high_usd": "0.01"})
        not_object = await c.post(path(w) + "/freshness", json=[1])
        accepted = await post_check(c, w)
        running = await post_check(c, w)
        missing = await c.post(f"/api/materials/{other}/freshness", json={"high_usd": "0.41"})
        estimate = (await c.get(path(w) + "/freshness")).json()["estimate"]
    assert changed.status_code == 409
    assert changed.json() == {"error": "estimate_changed", "estimate": estimate}
    assert not_object.json()["error"] == "estimate_changed"
    assert accepted.status_code == 202
    report_id = accepted.json()["report_id"]
    assert jobs.enqueued == [("freshness.scan", {"report_id": report_id})]
    assert running.status_code == 409
    assert running.json() == {"error": "freshness_running"}
    assert missing.status_code == 404
    assert missing.json() == {"error": "not_imported"}
    row = await report_row(w, report_id)
    assert row["status"] == "running"
    assert all(set(item) == {"span", "page"} for item in row["sample"])  # span ids and pages only


async def test_confirmation_codes(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    async with client(w, jobs=FakeJobs()) as c:
        nothing = await post_check(c, w, {"bibliography_sha256": "a" * 64})
    assert nothing.status_code == 409
    assert nothing.json() == {"error": "nothing_to_confirm"}
    await _scan_only(w)
    async with client(w, jobs=FakeJobs()) as c:
        wrong = await post_check(c, w, {"bibliography_sha256": "b" * 64})
        garbage = await post_check(c, w, {"bibliography_sha256": 5})
    assert wrong.json() == {"error": "bibliography_unconfirmed"}
    assert garbage.json() == {"error": "bibliography_unconfirmed"}


async def test_daily_cap_pre_check_carries_the_numbers(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    await spend_today(w, "freshness_scan", "1.20")
    await spend_today(w, "freshness_report", "0.70")
    await spend_today(w, "tutor", "5.00")  # another task does not count
    async with client(w, jobs=FakeJobs()) as c:
        refused = await post_check(c, w)
    assert refused.status_code == 429  # as SRV-TUTOR's daily cap
    assert refused.json() == {"error": "daily_budget_exhausted", "daily_cap_usd": "2.00", "spent_usd": "1.90"}


async def test_enqueue_failure_ends_the_report(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    async with client(w, jobs=FakeJobs(fail=True)) as c:
        accepted = await post_check(c, w)
    row = await report_row(w, accepted.json()["report_id"])
    assert (row["status"], row["error"]) == ("failed", "job_failed")


# ---- jobs ----------------------------------------------------------------------------------------------


def _replies(
    searches: list[Reply] | None = None, report: list[Reply] | None = None
) -> dict[str, list[Reply]]:
    return {
        "scan": [Reply(text=SCAN_OUT)],
        "search": searches if searches is not None else searches_ok(),
        "report": report if report is not None else [Reply(text=REPORT_OUT)],
    }


async def test_full_check_stores_only_resolved_fields(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    provider = FakeProvider(w.engine, _replies())
    report_id, _ = await full_check(w, tmp_path, provider)
    row = await report_row(w, report_id)
    assert row["status"] == "done"
    assert row["verdict"] == "partly_outdated"
    assert row["error"] is None
    r_urls = {u for i in range(1, 6) for _, u in links(i)}
    stored = [c["url"] for r in row["reasons"] for c in r["citations"]]
    stored += [c["url"] for s in row["claim_status"] for c in s["citations"]]
    stored += [r["url"] for r in row["recommendations"]]
    assert stored == [
        "https://site1.example/a", "https://site2.example/a", "https://site1.example/a",
        "https://site2.example/a", "https://site3.example/a",
    ]
    assert set(stored) <= r_urls
    assert row["recommendations"][0] == {
        "url": "https://site1.example/a", "host": "site1.example", "title": "Result 1a", "date": "2024-06",
        "date_source": "model", "kind": "book_edition", "why": "Newer.", "covers": [0],
    }
    assert row["recommendations"][2]["date"] is None
    assert row["dropped"] == {"section_unknown": 1, "date_invalid": 1}
    assert [s["status"] for s in row["searches"]] == ["ok"] * 5
    assert all(set(s) == {"query", "template", "status", "result_count"} for s in row["searches"])
    templates = [s["template"] for s in row["searches"]]
    assert templates == ["edition", "claim", "topic_docs", "topic_docs", "books"]
    assert row["web_search_requests"] == 5
    assert row["cost_usd"] == Decimal("0.070000")  # 7 calls at the fake's $0.01
    assert row["inputs_sha256"] is not None
    assert set(row["models"]) == {"freshness_scan", "freshness_web", "freshness_report"}
    assert set(row["prompt_versions"]) == {"scan", "search", "report"}
    blob = json.dumps({k: v for k, v in row.items() if k in need("reader.freshness.store").JSON_COLUMNS})
    assert SUMMARY not in blob  # INV-FRESH-2: summaries live in memory only
    assert "https://site4.example/b" not in blob  # unused R entries are not stored
    for forbidden in ("cited_text", "encrypted_content", "encrypted_index", "page_age"):
        assert forbidden not in blob


async def test_calls_carry_the_spec_options(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    provider = FakeProvider(w.engine, _replies())
    report_id, _ = await full_check(w, tmp_path, provider)
    scan, *search_calls, report = provider.calls
    assert (scan.task, scan.stage, scan.web_search) == ("freshness_scan", "scan", None)
    assert scan.max_output_tokens == 4000
    assert (report.task, report.stage, report.web_search) == ("freshness_report", "report", None)
    assert report.max_output_tokens == 4000
    assert "https://" not in report.prompt  # the report sees ids, hosts and titles, never URLs
    assert len(search_calls) == 5
    for call in [scan, *search_calls, report]:
        assert call.run_id == uuid.UUID(report_id)
        assert call.run_cap_usd == Decimal("1.00")
        assert call.daily_budget_usd == Decimal("2.00")
        assert call.thinking_off is True
    row = await report_row(w, report_id)
    for call, planned in zip(search_calls, row["searches"], strict=True):
        assert (call.task, call.stage) == ("freshness_web", "search")
        assert call.prompt == f"Query: {planned['query']}"
        assert call.max_output_tokens == 1000
        assert call.floor_usd == Decimal("0.02")
        assert call.abort_on_retry is True
        assert call.deadline_s == 240
        allowance = call.web_search.allowance
        shape = (allowance.searches_max, allowance.searches_per_request_max, allowance.requests_max)
        assert shape == (1, 1, 3)
        assert call.web_search.max_turns == 2
    assert provider.allowed_queries == [s["query"] for s in row["searches"]]


async def test_get_reads_progress_and_the_report_shape(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    provider = FakeProvider(w.engine, _replies())
    jobs = FakeJobs()
    async with client(w, jobs=jobs) as c:
        await c.put(path(w) + "/bibliography", json=BIB)
        report_id = (await post_check(c, w)).json()["report_id"]
        first = (await c.get(path(w) + "/freshness")).json()["report"]
        d = deps(w, provider, jobs)
        await run_job(w, "freshness.scan", uuid.UUID(report_id), d)
        second = (await c.get(path(w) + "/freshness")).json()["report"]
        await run_job(w, "freshness.web", uuid.UUID(report_id), d)
        done = (await c.get(path(w) + "/freshness")).json()
    assert first["progress"] == {"stage": "scan", "done": 0, "total": 1}
    assert second["progress"] == {"stage": "search", "done": 0, "total": 0}
    report = done["report"]
    assert set(report) == REPORT_KEYS
    assert report["status"] == "done"
    assert report["progress"] is None
    assert report["claims"] == [
        {"id": "c1", "subject": "consensus protocol v1", "kind": "protocol", "confidence": "high", "page": 8,
         "span": f"{MAT}:2", "cell": None}
    ]
    assert report["topics"] == [{"id": 0, "title": "Ledgers"}, {"id": 1, "title": "Blocks"},
                                {"id": 2, "title": "Clients"}]
    assert done["stale"] is False


async def test_scan_only_check_then_confirmation_reuses_the_scan(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    first_id, scanner = await _scan_only(w)
    assert [c.stage for c in scanner.calls] == ["scan"]
    row = await report_row(w, first_id)
    assert (row["status"], row["verdict"], row["bibliography"]) == ("done", None, None)
    async with client(w, jobs=FakeJobs()) as c:
        body = (await c.get(path(w) + "/freshness")).json()
    bib = need("reader.freshness.bibliography")
    scan_sha = bib.sha256(SCAN_BIB)
    assert body["scan_bibliography"] == {**bib.canonical(SCAN_BIB), "sha256": scan_sha}
    assert body["bibliography"]["source"] == "scan"
    assert body["report"]["verdict"] is None
    assert body["report"]["topics"] is None
    jobs = FakeJobs()
    async with client(w, jobs=jobs) as c:
        confirmed = await post_check(c, w, {"bibliography_sha256": scan_sha})
    assert confirmed.status_code == 202
    second_id = confirmed.json()["report_id"]
    provider2 = FakeProvider(w.engine, _replies())
    await run_job(w, "freshness.scan", uuid.UUID(second_id), deps(w, provider2, jobs))
    assert provider2.calls == []  # FR-FRESH-3: no provider call for a reused scan
    second = await report_row(w, second_id)
    assert second["scan_reused_from"] == uuid.UUID(first_id)
    assert second["claims"] == row["claims"]
    assert second["bibliography"]["source"] == "scan"
    assert jobs.enqueued[-1] == ("freshness.web", {"report_id": second_id})


async def test_an_owner_entry_is_never_overwritten_by_a_scan(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    async with client(w) as c:
        await c.put(path(w) + "/bibliography", json={**BIB, "title": "Owner title"})
    await full_check(w, tmp_path, FakeProvider(w.engine, _replies()))
    title = await scalar(w.engine, "SELECT title FROM material_bibliography WHERE source = 'owner'")
    assert title == "Practical Distributed Ledgers"  # full_check's own PUT, never the scan's copy
    assert await scalar(w.engine, "SELECT count(*) FROM material_bibliography WHERE source = 'scan'") == 0


async def _scan_only(w: Any) -> tuple[str, FakeProvider]:
    jobs = FakeJobs()
    async with client(w, jobs=jobs) as c:
        report_id = (await post_check(c, w)).json()["report_id"]
    provider = FakeProvider(w.engine, _replies())
    await run_job(w, "freshness.scan", uuid.UUID(report_id), deps(w, provider, jobs))
    assert jobs.enqueued == [("freshness.scan", {"report_id": report_id})]  # no web job
    return report_id, provider


async def test_search_retries_only_after_a_zero_settle(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    searches = [
        Reply(code="provider_unavailable", paid=False),
        Reply(links=links(1)),
        Reply(code="rate_limited", paid=True),
        Reply(links=links(3)),
        Reply(links=links(4)),
        Reply(links=links(5)),
    ]
    report = [Reply(text=REPORT_OUT.replace('"r3"', '"r1"').replace('"r5"', '"r1"'))]
    provider = FakeProvider(w.engine, _replies(searches, report))
    report_id, _ = await full_check(w, tmp_path, provider)
    row = await report_row(w, report_id)
    assert [s["status"] for s in row["searches"]] == ["ok", "failed", "ok", "ok", "ok"]
    assert len([c for c in provider.calls if c.stage == "search"]) == 6
    attempts = [c.attempt for c in provider.calls if c.stage == "search"]
    assert len(set(attempts)) == 6  # every call has its own ledger attempt number
    assert row["status"] == "done"


async def test_report_has_one_paid_retry(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    provider = FakeProvider(w.engine, _replies(report=[Reply(text="not json"), Reply(text=REPORT_OUT)]))
    report_id, _ = await full_check(w, tmp_path, provider)
    assert (await report_row(w, report_id))["status"] == "done"
    assert len([c for c in provider.calls if c.stage == "report"]) == 2


async def test_report_is_not_retried_twice(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    report = [Reply(text="x"), Reply(text="y"), Reply(text=REPORT_OUT)]
    provider = FakeProvider(w.engine, _replies(report=report))
    report_id, _ = await full_check(w, tmp_path, provider)
    row = await report_row(w, report_id)
    assert (row["status"], row["error"]) == ("failed", "output_invalid")
    assert len([c for c in provider.calls if c.stage == "report"]) == 2


async def test_scan_output_invalid_after_a_paid_call_is_final(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    jobs = FakeJobs()
    async with client(w, jobs=jobs) as c:
        await c.put(path(w) + "/bibliography", json=BIB)
        report_id = (await post_check(c, w)).json()["report_id"]
    provider = FakeProvider(w.engine, {"scan": [Reply(text="garbage"), Reply(text=SCAN_OUT)]})
    await run_job(w, "freshness.scan", uuid.UUID(report_id), deps(w, provider, jobs))
    row = await report_row(w, report_id)
    assert (row["status"], row["error"]) == ("failed", "output_invalid")
    assert len(provider.calls) == 1
    assert jobs.enqueued == [("freshness.scan", {"report_id": report_id})]


async def test_no_results_ends_failed_without_a_report_call(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    provider = FakeProvider(w.engine, _replies([Reply() for _ in range(5)]))
    report_id, _ = await full_check(w, tmp_path, provider)
    row = await report_row(w, report_id)
    assert (row["status"], row["error"]) == ("failed", "no_results")
    assert [s["status"] for s in row["searches"]] == ["empty"] * 5
    assert not [c for c in provider.calls if c.stage == "report"]


async def test_gate_denial_is_recorded(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    searches = [Reply(links=links(1), tool_input={"query": "something else entirely"}), *searches_ok(4)]
    provider = FakeProvider(w.engine, _replies(searches))
    report_id, _ = await full_check(w, tmp_path, provider)
    row = await report_row(w, report_id)
    assert row["searches"][0]["status"] == "denied"
    assert row["searches"][0]["result_count"] == 0
    assert row["dropped"]["query_denied"] == 1
    out = provider.hook_outputs[0]["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "something else" not in out["permissionDecisionReason"]


async def test_daily_cap_refusal_inside_the_job(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    jobs = FakeJobs()
    async with client(w, jobs=jobs) as c:
        await c.put(path(w) + "/bibliography", json=BIB)
        report_id = (await post_check(c, w)).json()["report_id"]
    provider = FakeProvider(w.engine, _replies())
    d = deps(w, provider, jobs)
    await run_job(w, "freshness.scan", uuid.UUID(report_id), d)
    await spend_today(w, "freshness_web", "1.99")
    await run_job(w, "freshness.web", uuid.UUID(report_id), d)
    row = await report_row(w, report_id)
    assert (row["status"], row["error"]) == ("budget_refused", "daily_budget_exhausted")
    assert row["searches"][0]["status"] == "refused"
    assert [c.stage for c in provider.calls] == ["scan"]


async def test_a_final_report_is_never_updated(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    store = need("reader.freshness.store")
    report_id, _ = await full_check(w, tmp_path, FakeProvider(w.engine, _replies()))
    assert await store.update(w.engine, uuid.UUID(report_id), verdict="current") is False
    assert await store.finish(w.engine, uuid.UUID(report_id), "failed", error="x") is False
    assert (await report_row(w, report_id))["verdict"] == "partly_outdated"


async def test_staleness(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    report_id, _ = await full_check(w, tmp_path, FakeProvider(w.engine, _replies()))
    await execute(
        w.engine, "UPDATE freshness_reports SET created_at = now() - interval '91 days' WHERE id = :r",
        r=uuid.UUID(report_id),
    )
    async with client(w) as c:
        old = (await c.get(path(w) + "/freshness")).json()
        await c.put(path(w) + "/bibliography", json={**BIB, "edition": "3rd"})
        changed = (await c.get(path(w) + "/freshness")).json()
    assert (old["stale"], old["stale_reason"]) == (True, "ttl")
    assert (changed["stale"], changed["stale_reason"]) == (True, "bibliography_changed")
    assert changed["report"]["id"] == report_id  # a stale report is shown, never deleted


async def test_a_running_report_without_a_live_job_fails(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    async with client(w, jobs=FakeJobs()) as c:
        await c.put(path(w) + "/bibliography", json=BIB)
        report_id = (await post_check(c, w)).json()["report_id"]
        young = (await c.get(path(w) + "/freshness")).json()["report"]
        await execute(
            w.engine, "UPDATE freshness_reports SET created_at = now() - interval '61 seconds' WHERE id = :r",
            r=uuid.UUID(report_id),
        )
        old = (await c.get(path(w) + "/freshness")).json()["report"]
    assert young["status"] == "running"
    assert (old["status"], old["error"]) == ("failed", "job_failed")


async def test_logs_carry_no_text(pg: Pg, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    w = await world(pg, tmp_path)
    caplog.set_level(logging.INFO, logger="reader.freshness")
    report_id, _ = await full_check(w, tmp_path, FakeProvider(w.engine, _replies()))
    row = await report_row(w, report_id)
    lines = [r.getMessage() for r in caplog.records if r.name == "reader.freshness"]
    assert any(line.startswith("freshness_stage") for line in lines)
    stage_line = next(line for line in lines if "stage=report" in line)
    for key in ("report=", "index=", "status=done", "code=", "searches=5", "denials=0", "links_dropped=0",
                "cost=", "seconds="):
        assert key in stage_line
    secrets = [s["query"] for s in row["searches"]] + [BIB["title"], "Ada Example", SUMMARY, "site1.example",
                                                       "Result 1a", "consensus protocol"]
    for line in lines:
        for value in secrets:
            assert value not in line


async def test_ledger_rows_are_scoped_to_the_report(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    report_id, _ = await full_check(w, tmp_path, FakeProvider(w.engine, _replies()))
    tasks = await rows(
        w.engine, "SELECT task, count(*) FROM agent_spend WHERE run_id = :r AND kind = 'settle' GROUP BY task"
        " ORDER BY task", r=uuid.UUID(report_id),
    )
    assert [tuple(t) for t in tasks] == [("freshness_report", 1), ("freshness_scan", 1), ("freshness_web", 5)]
