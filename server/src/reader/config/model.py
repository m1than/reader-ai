"""Typed, immutable settings for reader.yaml v1 (the spec's field table) and their JSON Schema.

FR-CONFIG-1, FR-CONFIG-4. The models enforce the same constraints as the loader (checks.py), so
`ReaderConfig.model_validate` cannot construct a config that `load_config` would reject.
"""

from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainValidator,
    StringConstraints,
    model_validator,
)

from reader.config.checks import FrozenDict, decimal_in_context, freeze_opaque, unique, url_problem
from reader.config.refs import SecretRef

_FROZEN = ConfigDict(frozen=True, extra="forbid", strict=True)


def _url(value: str) -> str:
    if url_problem(value):
        raise ValueError("bad_url")
    return value


def _in_context(value: Decimal) -> Decimal:
    if not decimal_in_context(value):
        raise ValueError("out_of_range")
    return value


def _exact_int(value: object) -> object:
    """Literal[1] accepts anything equal to 1 (True, 1.0, Decimal(1), 1+0j); the version must be the int 1."""
    if type(value) is not int:
        raise ValueError("wrong_type")
    return value


def _empty() -> Mapping[str, Any]:
    return FrozenDict()


Text = Annotated[str, StringConstraints(min_length=1)]
Url = Annotated[str, AfterValidator(_url)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False), AfterValidator(_in_context)]
Fraction = Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False), AfterValidator(_in_context)]
Opaque = Annotated[Mapping[str, Any], PlainValidator(freeze_opaque, json_schema_input_type=Mapping[str, Any])]


class Instance(BaseModel):
    model_config = _FROZEN
    url: Url
    data_dir: Text


class Provider(BaseModel):
    model_config = _FROZEN
    kind: Text
    token: SecretRef


class User(BaseModel):
    model_config = _FROZEN
    handle: Text
    role: Text
    providers: tuple[Provider, ...] = ()


class Task(BaseModel):
    model_config = _FROZEN
    model: Text
    budget_usd_per_lesson: Positive | None = None


def _tasks(value: Mapping[str, Task]) -> Mapping[str, Task]:
    if not all(value):
        raise ValueError("empty_key")
    return FrozenDict(value)


class Budgets(BaseModel):
    model_config = _FROZEN
    monthly_usd: Positive
    warn_at: Fraction | None = None


class Material(BaseModel):
    model_config = _FROZEN
    id: Text
    file: Text
    sha256: Sha256
    license: Text
    language: Text


class Observability(BaseModel):
    """FR-CONFIG-17 (paired SRV-OBS change): tracing on or off, and the trace retention in days."""

    model_config = _FROZEN
    enabled: bool = True
    retention_days: Annotated[int, Field(ge=1, le=365)] = 30


class ReaderConfig(BaseModel):
    model_config = _FROZEN
    version: Annotated[Literal[1], BeforeValidator(_exact_int)]  # schema keeps "const": 1
    instance: Instance
    users: tuple[User, ...] = ()
    # Mappings are FrozenDict (immutable, yet copyable, picklable, dumpable); defaults come from factories.
    tasks: Annotated[Mapping[str, Task], AfterValidator(_tasks)] = Field(default_factory=_empty)
    budgets: Budgets | None = None
    kernels: tuple[Text, ...] = ()
    materials: tuple[Material, ...] = ()
    sectors: Opaque = Field(default_factory=_empty)
    sharing: Opaque = Field(default_factory=_empty)
    observability: Observability = Field(default_factory=Observability)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        lists = ([u.handle for u in self.users], [m.id for m in self.materials], list(self.kernels))
        if not all(unique(values) for values in lists):
            raise ValueError("not_unique")
        return self


def export_json_schema() -> dict[str, Any]:
    return ReaderConfig.model_json_schema()
