"""`load_config`: read, parse, validate, build (FR-CONFIG-1, 3, 11, 15; INV-CONFIG-3, 4).

Errors are raised outside any `except` block and `from None`, so no parser exception is chained or kept as
context.
"""

import os
from pathlib import Path

from pydantic import ValidationError

from reader.config.errors import ConfigError, Problem
from reader.config.model import ReaderConfig
from reader.config.validate import ROOT, Context
from reader.config.yaml_tree import LoadFailure, Map, load_tree


def _read(path: Path) -> str | LoadFailure:
    try:
        data = path.read_bytes()
    except OSError:
        return LoadFailure("file_unreadable", None)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return LoadFailure("not_utf8", None)


def _tree(text: str) -> Map | LoadFailure:
    try:
        return load_tree(text)
    except LoadFailure as failure:
        return LoadFailure(failure.code, failure.line)


def load_config(path: str | os.PathLike[str]) -> ReaderConfig:
    file = str(path)
    text = _read(Path(path))
    tree = _tree(text) if isinstance(text, str) else text
    if isinstance(tree, LoadFailure):
        raise ConfigError([Problem(file, tree.line, "", tree.code)]) from None
    ctx = Context(base_dir=os.path.dirname(os.path.abspath(file)))
    values = ROOT(tree, "", ctx)
    if ctx.problems:
        problems = [Problem(file, line, key_path, code) for line, key_path, code in ctx.problems]
        raise ConfigError(problems) from None
    # The rules in validate.py and the model constraints come from the same checks (checks.py). Should they
    # ever disagree, the Pydantic error (which quotes input) is dropped for a fixed code (FR-CONFIG-1).
    config = _model(values)
    if config is None:
        raise ConfigError([Problem(file, None, "", "model_rule_mismatch")]) from None
    return config


def _model(values: object) -> ReaderConfig | None:
    try:
        return ReaderConfig.model_validate(values)
    except ValidationError:
        return None
