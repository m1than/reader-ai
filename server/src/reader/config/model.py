"""Typed, immutable settings for reader.yaml v1 (the spec's field table) and their JSON Schema.

FR-CONFIG-1, FR-CONFIG-4.
"""

from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SkipValidation

from reader.config.refs import SecretRef

_FROZEN = ConfigDict(frozen=True, extra="forbid")


def _freeze[V](mapping: Mapping[str, V]) -> Mapping[str, V]:
    return MappingProxyType(dict(mapping))


def _empty() -> Mapping[str, Any]:
    return MappingProxyType({})


Opaque = Annotated[Mapping[str, Any], SkipValidation]  # already an immutable tree of core-schema values


class Instance(BaseModel):
    model_config = _FROZEN
    url: str
    data_dir: str


class Provider(BaseModel):
    model_config = _FROZEN
    kind: str
    token: SecretRef


class User(BaseModel):
    model_config = _FROZEN
    handle: str
    role: str
    providers: tuple[Provider, ...] = ()


class Task(BaseModel):
    model_config = _FROZEN
    model: str
    budget_usd_per_lesson: Decimal | None = None


class Budgets(BaseModel):
    model_config = _FROZEN
    monthly_usd: Decimal
    warn_at: Decimal | None = None


class Material(BaseModel):
    model_config = _FROZEN
    id: str
    file: str
    sha256: str
    license: str
    language: str


class ReaderConfig(BaseModel):
    model_config = _FROZEN
    version: Literal[1]
    instance: Instance
    users: tuple[User, ...] = ()
    # Mapping defaults come from factories: Pydantic deep-copies plain defaults, which a mappingproxy refuses.
    tasks: Annotated[Mapping[str, Task], AfterValidator(_freeze)] = Field(default_factory=_empty)
    budgets: Budgets | None = None
    kernels: tuple[str, ...] = ()
    materials: tuple[Material, ...] = ()
    sectors: Opaque = Field(default_factory=_empty)
    sharing: Opaque = Field(default_factory=_empty)


def export_json_schema() -> dict[str, Any]:
    return ReaderConfig.model_json_schema()
