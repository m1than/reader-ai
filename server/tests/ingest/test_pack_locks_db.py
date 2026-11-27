"""Per-pack advisory locks (SRV-INGEST FR-INGEST-46): key order, no deadlock, re-entry and lock_order.

@trace FR-INGEST-46
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path

import pytest

from .conftest import Pg, need, world

pytestmark = pytest.mark.anyio

RKPK = 0x524B504B


def _ids(n: int, seed: int = 7) -> list[str]:
    rnd = random.Random(seed)
    return sorted(f"{rnd.getrandbits(256):064x}" for _ in range(n))


def test_key_is_monotone_in_the_hex_id() -> None:
    packs = need("reader.ingest.packs")
    ids = _ids(500) + ["0" * 64, "f" * 64, "12345678" + "0" * 56, "12345678" + "f" * 56]
    ids.sort()
    keys = [packs.lock_key(i) for i in ids]
    assert keys == sorted(keys)
    assert packs.lock_key("0" * 64) == -(2**31)
    assert packs.lock_key("f" * 64) == 2**31 - 1
    assert packs.lock_key("12345678" + "0" * 56) == packs.lock_key("12345678" + "f" * 56)
    assert packs.LOCK_NAMESPACE == RKPK


async def _awaited(w: object, timeout: float = 5.0) -> int:
    """Number of advisory locks in the RKPK namespace that some backend waits for."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        n = int(await w.scalar(  # type: ignore[attr-defined]
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted AND classid = :ns",
            ns=RKPK,
        ))
        if n:
            return n
        await asyncio.sleep(0.02)
    return 0


async def test_opposite_orders_do_not_deadlock(pg: Pg, tmp_path: Path) -> None:
    packs = need("reader.ingest.packs")
    a, b = "1" * 64, "2" * 64
    async with world(pg, tmp_path) as w:
        first = await w.engine.connect()
        await first.begin()
        await packs.lock_packs(first, [a, b])

        async def other() -> None:
            async with w.engine.begin() as conn:
                await packs.lock_packs(conn, [b, a])

        task = asyncio.create_task(other())
        assert await _awaited(w) == 1  # the second waits on the lowest key, never holding b first
        await first.commit()
        await first.close()
        await asyncio.wait_for(task, 10)


async def test_later_calls_reenter_and_refuse_smaller_new_keys(pg: Pg, tmp_path: Path) -> None:
    from sqlalchemy import text

    packs = need("reader.ingest.packs")
    low, mid, high = "1" * 64, "5" * 64, "9" * 64
    async with world(pg, tmp_path) as w:
        async with w.engine.begin() as conn:
            await packs.lock_packs(conn, [mid])
            await packs.lock_packs(conn, [mid])  # held: granted at once
            await packs.lock_packs(conn, [mid, high])  # greater new key
            with pytest.raises(packs.PackError) as refused:
                await packs.lock_packs(conn, [low, high])
            assert refused.value.code == "lock_order"
            held = (await conn.execute(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = :ns"
                " AND pid = pg_backend_pid() AND objid = (:k)::bigint::oid"),
                {"ns": RKPK, "k": packs.lock_key(low) % 2**32})).scalar()
            assert held == 0  # nothing taken by the refused call
