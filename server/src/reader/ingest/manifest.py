"""The owner's manifest: an append-only chain of active sets with a CAS on the parent, and the activation
contract (SRV-INGEST FR-INGEST-38, -39; ADR-0039 B2, P4, P5)."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

import reader.ingest as ingest
from reader.ingest import packs
from reader.ingest.artifact import canonical

log = logging.getLogger("reader.ingest")
VIEWS: Final = ("active_view", "profile_for_pack")
CAS_RETRIES: Final = 3  # bounds.manifest_cas_retries
ACTIVE_PACKS_WARN: Final = 200  # bounds.active_packs_warn
REANCHOR_KIND: Final = "ingest.reanchor"


class ManifestError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class View:
    seq: int
    brain_version: str
    entry: dict[str, Any]


def make_entry(elements: Mapping[str, Mapping[str, str]]) -> dict[str, Any]:
    return {"packs": [{"pack_id": p, "overlays": dict(elements[p])} for p in sorted(elements)],
            "registry_view": None}


EMPTY: Final = make_entry({})


def brain_version(entry: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(dict(entry))).hexdigest()


def _elements(entry: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    return {e["pack_id"]: dict(e["overlays"]) for e in entry["packs"]}


async def _entry_at(conn: AsyncConnection, owner_id: uuid.UUID, seq: int) -> dict[str, Any]:
    if seq == 0:
        return make_entry({})
    value = (await conn.execute(text("SELECT entry FROM manifest_entries WHERE owner_id = :o AND seq = :s"),
                                {"o": owner_id, "s": seq})).scalar()
    if value is None:
        raise ManifestError("manifest_conflict")
    return dict(value)


async def head(conn: AsyncConnection, owner_id: uuid.UUID) -> tuple[int, dict[str, Any]]:
    row = (await conn.execute(text(
        "SELECT seq, entry FROM manifest_entries WHERE owner_id = :o ORDER BY seq DESC LIMIT 1"),
        {"o": owner_id})).first()
    return (0, make_entry({})) if row is None else (int(row[0]), dict(row[1]))


async def active_view(conn: AsyncConnection, owner_id: uuid.UUID) -> View:
    seq, entry = await head(conn, owner_id)
    return View(seq, brain_version(entry), entry)


async def _check(conn: AsyncConnection, owner_id: uuid.UUID, pid: str, named: Mapping[str, str]) -> None:
    row = (await conn.execute(text(
        "SELECT state, collected_at IS NOT NULL, purged_at IS NOT NULL FROM base_packs"
        " WHERE owner_id = :o AND pack_id = :p"), {"o": owner_id, "p": pid})).first()
    if row is None or row[0] != "sealed":
        raise ManifestError("not_sealed")
    if row[2]:
        raise ManifestError("pack_purged")
    if row[1]:
        raise ManifestError("pack_collected")
    for kind, oid in named.items():
        o = (await conn.execute(text(
            "SELECT state, pack_id, kind, collected_at IS NOT NULL FROM overlays"
            " WHERE owner_id = :o AND overlay_id = :i"), {"o": owner_id, "i": oid})).first()
        if o is None:
            raise ManifestError("not_sealed")
        if o[1] != pid or o[2] != kind:
            raise ManifestError("overlay_mismatch")
        if o[3]:
            raise ManifestError("overlay_collected")
        if o[0] != "sealed":
            raise ManifestError("not_sealed")


async def _insert(conn: AsyncConnection, owner_id: uuid.UUID, parent_seq: int, entry: dict[str, Any],
                  reason: str) -> None:
    try:
        async with conn.begin_nested():
            await conn.execute(text(
                "INSERT INTO manifest_entries (owner_id, seq, parent_seq, entry, brain_version, reason)"
                " VALUES (:o, :s, :p, CAST(:e AS jsonb), :b, :r)"),
                {"o": owner_id, "s": parent_seq + 1, "p": parent_seq, "e": json.dumps(entry),
                 "b": brain_version(entry), "r": reason})
    except IntegrityError:
        raise ManifestError("manifest_conflict") from None


async def append(conn: AsyncConnection, owner_id: uuid.UUID, parent_seq: int, entry: Mapping[str, Any],
                 reason: str) -> int:
    """FR-INGEST-38: lock the changed packs, re-check them, insert seq = parent + 1 (CAS on the parent)."""
    content = make_entry(_elements(entry))
    parent = _elements(await _entry_at(conn, owner_id, parent_seq))
    elements = _elements(content)
    if elements == parent:
        raise ManifestError("no_change")
    changed = [p for p, o in elements.items() if parent.get(p) != o]
    await packs.lock_packs(conn, changed)
    for pid in changed:
        await _check(conn, owner_id, pid, elements[pid])
    materials = [await packs.material_of(conn, owner_id, p) for p in elements]
    if len(set(materials)) != len(materials):
        raise ManifestError("material_twice")
    await _insert(conn, owner_id, parent_seq, content, reason)
    if len(elements) > ACTIVE_PACKS_WARN:
        log.warning("manifest active_packs=%d", len(elements))
    return parent_seq + 1


async def request_activation(conn: AsyncConnection, owner_id: uuid.UUID, pid: str,
                             overlays: Mapping[str, str]) -> None:
    """Merge the named overlays (kind → id) into the pack's pending row."""
    sha = await packs.material_of(conn, owner_id, pid)
    if sha is None:
        raise ManifestError("not_sealed")
    await conn.execute(text(
        "INSERT INTO pending_activations (owner_id, pack_id, material_sha256, overlays)"
        " VALUES (:o, :p, :s, CAST(:v AS jsonb)) ON CONFLICT (owner_id, pack_id) DO UPDATE"
        " SET overlays = pending_activations.overlays || excluded.overlays, updated_at = now()"),
        {"o": owner_id, "p": pid, "s": sha, "v": json.dumps(dict(overlays))})


async def _element_of(conn: AsyncConnection, owner_id: uuid.UUID, entry: Mapping[str, Any],
                      sha: str) -> tuple[str, dict[str, str]] | None:
    for pid, named in _elements(entry).items():
        if await packs.material_of(conn, owner_id, pid) == sha:
            return pid, named
    return None


async def _version(conn: AsyncConnection, owner_id: uuid.UUID, pid: str) -> str:
    return str((await conn.execute(text(
        "SELECT parse_config ->> 'converter_version' FROM base_packs WHERE owner_id = :o AND pack_id = :p"),
        {"o": owner_id, "p": pid})).scalar_one())


async def _precondition(conn: AsyncConnection, owner_id: uuid.UUID, pid: str, sha: str,
                        current: tuple[str, dict[str, str]] | None, new: Mapping[str, str]) -> str | None:
    deleted = (await conn.execute(text(
        "SELECT deleted_at IS NOT NULL FROM materials WHERE owner_id = :o AND sha256 = :s"),
        {"o": owner_id, "s": sha})).scalar()
    if deleted:
        return "deleted"
    try:
        await _check(conn, owner_id, pid, new)
    except ManifestError as error:
        return error.code
    if current is not None and "vectors" in current[1] and "vectors" not in new:
        return "vectors_missing"
    version = await _version(conn, owner_id, pid)
    legacy = (await conn.execute(text(
        "SELECT ingest_id FROM base_packs WHERE owner_id = :o AND material_sha256 = :s"
        " AND parse_config ->> 'converter_version' = ANY(:v) ORDER BY created_at LIMIT 1"),
        {"o": owner_id, "s": sha, "v": list(ingest.LEGACY_VERSIONS)})).scalar()
    if legacy is not None and version not in ingest.LEGACY_VERSIONS:
        mapped = (await conn.execute(text(
            "SELECT 1 FROM seq_maps WHERE owner_id = :o AND from_ingest_id = :i AND state = 'accepted'"),
            {"o": owner_id, "i": legacy})).first()
        if mapped is None:  # a follow-up lost while the kind was unwired is enqueued again (by its key)
            from reader.ingest.jobs import SEQMAP_KIND, enqueue_known

            await enqueue_known(SEQMAP_KIND, {"owner_id": str(owner_id), "material_sha256": sha,
                                              "from_ingest_id": str(legacy), "to_pack_id": pid})
            return "seqmap_missing"
    if current is not None and current[0] != pid:
        anchored = (await conn.execute(text(
            "SELECT 1 FROM anchor_runs WHERE owner_id = :o AND from_pack_id = :f AND to_pack_id = :t"),
            {"o": owner_id, "f": current[0], "t": pid})).first()
        if anchored is None:
            await _enqueue_reanchor(owner_id, current[0], pid)
            return "anchors_missing"
    return None


async def _enqueue_reanchor(owner_id: uuid.UUID, from_pack: str, to_pack: str) -> None:
    from reader.ingest.jobs import enqueue_known

    await enqueue_known(REANCHOR_KIND, {"owner_id": str(owner_id), "from_pack_id": from_pack,
                                        "to_pack_id": to_pack})


async def _wait(conn: AsyncConnection, owner_id: uuid.UUID, pid: str, reason: str) -> str:
    await conn.execute(text(
        "UPDATE pending_activations SET reason = :r, updated_at = now()"
        " WHERE owner_id = :o AND pack_id = :p"),
        {"r": reason, "o": owner_id, "p": pid})
    log.info("ingest activate_waiting %s", reason)
    return reason


async def _done(conn: AsyncConnection, owner_id: uuid.UUID, pid: str, outcome: str) -> str:
    await conn.execute(text("DELETE FROM pending_activations WHERE owner_id = :o AND pack_id = :p"),
                       {"o": owner_id, "p": pid})
    return outcome


async def try_activate(conn: AsyncConnection, owner_id: uuid.UUID, pid: str) -> str | None:
    """FR-INGEST-39: append the pack's pending desire when its preconditions hold; else store the reason.

    Returns None without a pending row, else `appended`, `no_change`, `superseded` or the waiting reason.
    """
    sha = await packs.material_of(conn, owner_id, pid)
    if sha is None:
        return None
    _, top = await head(conn, owner_id)
    current = await _element_of(conn, owner_id, top, sha)
    await packs.lock_packs(conn, [pid] + ([current[0]] if current else []))
    row = (await conn.execute(text(
        "SELECT overlays FROM pending_activations WHERE owner_id = :o AND pack_id = :p FOR UPDATE"),
        {"o": owner_id, "p": pid})).first()
    if row is None:
        return None
    desired: dict[str, str] = dict(row[0])
    for _ in range(CAS_RETRIES):
        seq, top = await head(conn, owner_id)
        current = await _element_of(conn, owner_id, top, sha)
        if current is not None and current[0] != pid and (
            packs.version_rank(await _version(conn, owner_id, current[0]))
            > packs.version_rank(await _version(conn, owner_id, pid))
        ):
            return await _done(conn, owner_id, pid, "superseded")
        kept = current[1] if current is not None and current[0] == pid else {}
        new = {**kept, **desired}
        reason = await _precondition(conn, owner_id, pid, sha, current, new)
        if reason is not None:
            return await _wait(conn, owner_id, pid, reason)
        elements = _elements(top)
        if current is not None:
            del elements[current[0]]
        elements[pid] = new
        try:
            await append(conn, owner_id, seq, make_entry(elements), "activate")
        except ManifestError as error:
            if error.code == "no_change":
                return await _done(conn, owner_id, pid, "no_change")
            if error.code != "manifest_conflict":
                return await _wait(conn, owner_id, pid, error.code)
            continue
        return await _done(conn, owner_id, pid, "appended")
    return await _wait(conn, owner_id, pid, "manifest_conflict")


async def sweep(engine: AsyncEngine) -> None:
    """The worker-start sweep: try_activate for every pending row, each in its own transaction."""
    async with engine.connect() as conn:
        rows: Sequence[Any] = (await conn.execute(text(
            "SELECT owner_id, pack_id FROM pending_activations ORDER BY owner_id, pack_id"))).all()
    for owner_id, pid in rows:
        async with engine.begin() as conn:
            await try_activate(conn, owner_id, str(pid))


async def _last_element(
    conn: AsyncConnection, owner_id: uuid.UUID, sha: str
) -> tuple[str, dict[str, str]] | None:
    """The material's element in the last manifest entry that named it."""
    row = (await conn.execute(text(
        "SELECT e.pack_id, e.overlays FROM manifest_entries m"
        " CROSS JOIN LATERAL jsonb_to_recordset(m.entry -> 'packs') AS e(pack_id text, overlays jsonb)"
        " JOIN base_packs b ON b.owner_id = m.owner_id AND b.pack_id = e.pack_id"
        " WHERE m.owner_id = :o AND b.material_sha256 = :s ORDER BY m.seq DESC LIMIT 1"),
        {"o": owner_id, "s": sha})).first()
    return None if row is None else (str(row[0]), dict(row[1]))


async def _pack_gone(conn: AsyncConnection, owner_id: uuid.UUID, pid: str) -> bool:
    return bool((await conn.execute(text(
        "SELECT collected_at IS NOT NULL OR purged_at IS NOT NULL FROM base_packs"
        " WHERE owner_id = :o AND pack_id = :p"), {"o": owner_id, "p": pid})).scalar())


async def _pending(
    conn: AsyncConnection, owner_id: uuid.UUID, sha: str
) -> dict[str, tuple[dict[str, str], Any]]:
    rows = await conn.execute(text(
        "SELECT pack_id, overlays, reason FROM pending_activations"
        " WHERE owner_id = :o AND material_sha256 = :s"),
        {"o": owner_id, "s": sha})
    return {str(r[0]): (dict(r[1]), r[2]) for r in rows}


async def _anchor_inline(conn: AsyncConnection, owner_id: uuid.UUID, old: str, new: str) -> None:
    """FR-INGEST-43 run inline by restore and rollback (reused when the pair's run exists)."""
    from reader.ingest import reanchor

    await reanchor.run(conn, owner_id, old, new)


async def restore_material(conn: AsyncConnection, owner_id: uuid.UUID, sha: str,
                           ensure: packs.SearchEnsure | None = None) -> list[str]:
    """FR-INGEST-39's restore, after FR-INGEST-31's locks; returns try_activate's outcomes in order."""
    last = await _last_element(conn, owner_id, sha)
    _, top = await head(conn, owner_id)
    current = await _element_of(conn, owner_id, top, sha)
    await packs.lock_packs(conn, {*(await _pending(conn, owner_id, sha)), *([last[0]] if last else []),
                                  *([current[0]] if current else [])})
    if last is not None and not await _pack_gone(conn, owner_id, last[0]):
        live = {}
        for kind, oid in last[1].items():
            gone = (await conn.execute(text(
                "SELECT collected_at IS NOT NULL FROM overlays WHERE owner_id = :o AND overlay_id = :i"),
                {"o": owner_id, "i": oid})).scalar()
            if not gone:
                live[kind] = oid
        desired = (await _pending(conn, owner_id, sha)).get(last[0], ({}, None))[0]
        await request_activation(conn, owner_id, last[0], {**live, **desired})  # merged under desires
    floor = None if last is None else packs.version_rank(await _version(conn, owner_id, last[0]))
    order = sorted([(packs.version_rank(await _version(conn, owner_id, p)), p)
                    for p in await _pending(conn, owner_id, sha)])
    outcomes: list[str] = []
    for rank, pid in order:
        if floor is not None and rank < floor:
            await _done(conn, owner_id, pid, "skipped")
            continue
        _, top = await head(conn, owner_id)
        current = await _element_of(conn, owner_id, top, sha)
        if current is not None and current[0] != pid:
            await _anchor_inline(conn, owner_id, current[0], pid)
        outcomes.append(str(await try_activate(conn, owner_id, pid)))
    left = (await _pending(conn, owner_id, sha)).values()
    if any(reason == "vectors_missing" or "vectors" not in named for named, reason in left):
        await (ensure or packs.SEARCH_ENSURE[0])(conn, owner_id, sha)
    return outcomes


async def rollback(conn: AsyncConnection, owner_id: uuid.UUID, to_seq: int) -> int:
    """FR-INGEST-39's rollback (P4 and the property test only; no route, job or command calls it)."""
    seq, top = await head(conn, owner_id)
    target, current = _elements(await _entry_at(conn, owner_id, to_seq)), _elements(top)
    await packs.lock_packs(conn, [p for p in {*target, *current} if target.get(p) != current.get(p)])
    by_material = {await packs.material_of(conn, owner_id, p): p for p in current}
    elements: dict[str, dict[str, str]] = {}
    for pid, named in target.items():
        sha = await packs.material_of(conn, owner_id, pid)
        deleted = (await conn.execute(text(
            "SELECT deleted_at IS NOT NULL FROM materials WHERE owner_id = :o AND sha256 = :s"),
            {"o": owner_id, "s": sha})).scalar()
        if deleted:
            continue  # a rollback never re-activates a deleted material
        elements[pid] = named
        old = by_material.get(sha)
        if old is not None and old != pid and not await _pack_gone(conn, owner_id, pid):
            await _anchor_inline(conn, owner_id, old, pid)
    return await append(conn, owner_id, seq, make_entry(elements), "rollback")


async def remove_material(conn: AsyncConnection, owner_id: uuid.UUID, material_sha256: str) -> bool:
    """FR-INGEST-31's delete: one entry without the material's element when the head names it; no lock."""
    return await _remove(conn, owner_id, material_sha256, None, "delete")


async def remove_pack(conn: AsyncConnection, owner_id: uuid.UUID, pid: str) -> bool:
    """FR-INGEST-45's purge: one entry without the pack when the head names it (the caller holds its lock)."""
    return await _remove(conn, owner_id, None, pid, "purge")


async def _remove(conn: AsyncConnection, owner_id: uuid.UUID, material_sha256: str | None, pid: str | None,
                  reason: str) -> bool:
    for _ in range(CAS_RETRIES):
        seq, top = await head(conn, owner_id)
        if material_sha256 is not None:
            current = await _element_of(conn, owner_id, top, material_sha256)
            gone = None if current is None else current[0]
        else:
            gone = pid if pid in _elements(top) else None
        if gone is None:
            return False
        elements = _elements(top)
        del elements[gone]
        try:
            await append(conn, owner_id, seq, make_entry(elements), reason)
        except ManifestError as error:
            if error.code != "manifest_conflict":
                raise
            continue
        return True
    raise ManifestError("manifest_conflict")
