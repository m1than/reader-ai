"""SRV-FRESH errors as data: every code is fixed text, never a prompt, an output or a URL."""

from __future__ import annotations

from reader.agent.errors import TransientAgentError


class FreshError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OutputInvalid(TransientAgentError):
    """Invalid JSON or schema from a model call (FR-FRESH-3, -8): transient, like SRV-AGENT's own code."""

    def __init__(self) -> None:
        super().__init__("output_invalid")
