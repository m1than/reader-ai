"""Pipeline errors as data (SRV-PIPELINE `errors`); codes are fixed text."""

from __future__ import annotations


class PipelineError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TransientPipelineError(PipelineError):
    """Retried by SRV-EVENTS within the step's max_attempts."""


class VerifyUnavailable(TransientPipelineError):
    def __init__(self) -> None:
        super().__init__("verify_unavailable")


class InputNotReady(TransientPipelineError):
    def __init__(self) -> None:
        super().__init__("input_not_ready")
