"""The CLI's own failure: a fixed code plus a detail that never holds a secret value."""


class CliError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)
