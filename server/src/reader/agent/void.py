"""`python -I -m reader.agent.void <reservation_id>`: void a reservation a dead worker left open (FR-AGENT-9).

Run by the owner inside the worker container. Appends a logged `void` row; refused when the reservation has a
settlement or a void already. Exit 0 voided, 1 refused or unreachable, 2 usage.
"""

from __future__ import annotations

import asyncio
import os
import sys

from reader.agent.errors import AgentError
from reader.agent.ledger import void
from reader.db import make_engine
from reader.serve.main import Env, _target

USAGE = "usage: python -I -m reader.agent.void <reservation_id>\n"


async def _void(reservation_id: int) -> None:
    engine = make_engine(_target(Env.read(os.environ)))
    try:
        await void(engine, reservation_id)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0].isdigit():
        sys.stderr.write(USAGE)
        return 2
    try:
        asyncio.run(_void(int(args[0])))
    except AgentError as error:
        sys.stderr.write(f"void: {error.code}\n")
        return 1
    except Exception:
        sys.stderr.write("void: database_unreachable\n")
        return 1
    sys.stdout.write(f"voided {args[0]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
