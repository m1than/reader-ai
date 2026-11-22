"""The log scrubber for procrastinate's records (SRV-EVENTS FR-EVENTS-16, INV-EVENTS-5).

procrastinate passes the job as `extra={"job": {...}}`, which `Logger.makeRecord` applies after the
record factory has built the record, so the scrubber is a filter on the root logger's handlers,
installed after logging is configured.
"""

from __future__ import annotations

import logging
from typing import Any


class JobRecordScrubber(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith("procrastinate"):
            return True
        job: Any = getattr(record, "job", None)
        if isinstance(job, dict):
            call_string = job.get("call_string")
            job.pop("task_kwargs", None)
            job.pop("call_string", None)
            if isinstance(call_string, str) and call_string:
                label = f"{job.get('task_name', '?')}[{job.get('id', '?')}]"
                message = record.getMessage()
                if call_string in message:
                    record.msg = message.replace(call_string, label)
                    record.args = None
        return True


_SCRUBBER = JobRecordScrubber()


def install_log_filter() -> None:
    """Add the scrubber to every handler of the root logger; idempotent."""
    for handler in logging.getLogger().handlers:
        if _SCRUBBER not in handler.filters:
            handler.addFilter(_SCRUBBER)
