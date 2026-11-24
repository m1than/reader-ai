"""Pipeline errors as data (SRV-PIPELINE `errors`); codes are fixed text."""

from __future__ import annotations

import uuid


class PipelineError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CourseDeleted(PipelineError):
    """FR-PIPELINE-22: an import whose plan course maps to a deleted course; the answer names the course."""

    def __init__(self, course_id: uuid.UUID) -> None:
        super().__init__("course_deleted")
        self.course_id = course_id


class TransientPipelineError(PipelineError):
    """Retried by SRV-EVENTS within the step's max_attempts."""


class VerifyUnavailable(TransientPipelineError):
    def __init__(self) -> None:
        super().__init__("verify_unavailable")


class InputNotReady(TransientPipelineError):
    def __init__(self) -> None:
        super().__init__("input_not_ready")
