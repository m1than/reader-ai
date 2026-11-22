"""The log scrubber for procrastinate's records (SRV-EVENTS FR-EVENTS-16, INV-EVENTS-5).

procrastinate passes the job as `extra={"job": {...}}`, which `Logger.makeRecord` applies after the
record factory has built the record, so the scrubber is a filter on the root logger's handlers,
installed after logging is configured and before the procrastinate app opens. Some records carry the
call string only in their message (the shutdown line "Waiting for job to finish: worker: <call>"), so
every call string `name[id](...)` is also masked by pattern, with its parentheses matched.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final

_CALL_START: Final = re.compile(r"([a-z][a-z0-9_.]*)\[(\d+)\]\(")


def mask_call_strings(message: str) -> str:
    """Replace every `name[id](...)` with `name[id]`, matching parentheses and skipping quoted text."""
    out: list[str] = []
    pos = 0
    while (match := _CALL_START.search(message, pos)) is not None:
        out.append(message[pos : match.start()])
        out.append(f"{match.group(1)}[{match.group(2)}]")
        depth, i, quote = 1, match.end(), ""
        while i < len(message) and depth:
            ch = message[i]
            if quote:
                if ch == "\\":
                    i += 1
                elif ch == quote:
                    quote = ""
            elif ch in "'\"":
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        pos = i
    out.append(message[pos:])
    return "".join(out)


class JobRecordScrubber(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith("procrastinate"):
            return True
        job: Any = getattr(record, "job", None)
        if isinstance(job, dict):
            job.pop("task_kwargs", None)
            job.pop("call_string", None)
        message = record.getMessage()
        masked = mask_call_strings(message)
        if masked != message:
            record.msg = masked
            record.args = None
        return True


_SCRUBBER = JobRecordScrubber()


def install_log_filter() -> None:
    """Add the scrubber to every handler of the root logger; idempotent."""
    for handler in logging.getLogger().handlers:
        if _SCRUBBER not in handler.filters:
            handler.addFilter(_SCRUBBER)
