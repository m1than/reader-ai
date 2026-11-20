"""reader.yaml settings, JSON Schema, secret references and resolution (spec/modules/SRV-CONFIG.yaml)."""

from reader.config.errors import ConfigError, Problem
from reader.config.load import load_config
from reader.config.model import ReaderConfig, export_json_schema
from reader.config.refs import SecretRef, secret_env_var, secret_file_name
from reader.config.secrets import (
    SECRET_MAX_BYTES,
    Resolver,
    ResolverRegistry,
    Secret,
    SecretResolutionError,
    container_registry,
    host_registry,
    resolve,
)

__all__ = [
    "SECRET_MAX_BYTES",
    "ConfigError",
    "Problem",
    "ReaderConfig",
    "Resolver",
    "ResolverRegistry",
    "Secret",
    "SecretRef",
    "SecretResolutionError",
    "container_registry",
    "export_json_schema",
    "host_registry",
    "load_config",
    "resolve",
    "secret_env_var",
    "secret_file_name",
]
