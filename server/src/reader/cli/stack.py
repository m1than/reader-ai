"""The rendered Compose model, the in-memory override and the delivery plan (FR-CLI-3, FR-CLI-4, FR-CLI-5)."""

import copy
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from ruamel.yaml import YAML

from reader.cli.errors import CliError
from reader.config import ReaderConfig, SecretRef, secret_file_name

DELIVERY_SCRIPT: Final = (
    'umask 077; cat > "/run/secrets/.$1.tmp" && mv "/run/secrets/.$1.tmp" "/run/secrets/$1"'
)
NAME_PATTERN: Final = re.compile(r"db-password|(?:env|hostfile|keychain|op)-[0-9a-f]{32}")
HANDLE_PATTERN: Final = re.compile(r"[a-z0-9][a-z0-9-]*")
HANDLE_MAX: Final = 50  # `kernel-user-` (12) + 50 stays within a 63-character DNS label
DB_PASSWORD: Final = "db-password"  # noqa: S105 - a file name under /run/secrets, not a password
DB_CONSUMERS: Final = ("postgres", "api", "worker", "mcp")  # FR-DEPLOY-10 (b)
TOKEN_CONSUMER: Final = "agent-proxy"  # noqa: S105 - a service name
CONFIG_ROLES: Final = ("api", "worker", "mcp", "agent-proxy")  # FR-CLI-4: the reader.yaml bind
CONFIG_TARGET: Final = "/etc/reader/reader.yaml"
_USER: Final = re.compile(r"(\d+)(?::\d+)?")


def check_handle(handle: str) -> None:
    if len(handle) > HANDLE_MAX or not HANDLE_PATTERN.fullmatch(handle):
        raise CliError(
            "bad_handle", f"user handle {handle!r} must match [a-z0-9][a-z0-9-]* and be at most 50 long"
        )


def check_config_path(path: Path) -> Path:
    """The --config path as the bind source: resolved (so absolute) and free of `$` (Compose interpolates)."""
    resolved = Path(path).resolve(strict=True)
    if "$" in str(resolved):
        raise CliError(
            "config_path_interpolates", "the reader.yaml path contains '$', which Compose would interpolate"
        )
    return resolved


def load_compose_source(path: Path) -> dict[str, Any]:
    """deploy/compose.yaml as written (not rendered): the `x-kernel` template lives only here (FR-CLI-4)."""
    data = YAML(typ="safe", pure=True).load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise CliError("bad_compose", f"{path} is not a mapping")
    return cast(dict[str, Any], data)


def build_override(source: Mapping[str, Any], handles: Iterable[str], config_path: Path) -> dict[str, Any]:
    template = source.get("x-kernel")
    if not isinstance(template, Mapping):
        raise CliError("bad_compose", "compose.yaml has no x-kernel template")
    handles = list(handles)
    services: dict[str, Any] = {}
    networks: dict[str, Any] = {}
    for handle in handles:
        check_handle(handle)
        network = f"sb-user-{handle}"
        services[f"kernel-user-{handle}"] = {**copy.deepcopy(dict(template)), "networks": [network]}
        networks[network] = {"internal": True}
    for role in CONFIG_ROLES:
        bind = {
            "type": "bind",
            "source": str(config_path),
            "target": CONFIG_TARGET,
            "read_only": True,
            "bind": {"create_host_path": False},
        }
        services[role] = {"volumes": [bind]}
    services["api"]["networks"] = {f"sb-user-{h}": {} for h in handles}
    return {"services": services, "networks": networks}


def numeric_uid(user: object) -> int | None:
    match = _USER.fullmatch(user) if isinstance(user, str) else None
    return int(match.group(1)) if match else None


@dataclass(frozen=True)
class Rendered:
    """The parts of `docker compose config --format json` that `reader` reads (FR-CLI-3)."""

    name: str
    pgdata_volume: str
    services: Mapping[str, Mapping[str, Any]]
    volumes: Mapping[str, Mapping[str, Any]]

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> Rendered:
        name = data.get("name")
        volumes = data.get("volumes") or {}
        pgdata = volumes.get("pgdata") if isinstance(volumes, Mapping) else None
        pgdata_name = pgdata.get("name") if isinstance(pgdata, Mapping) else None
        if not isinstance(name, str) or not name:
            raise CliError("bad_model", "the rendered model has no project name")
        if not isinstance(pgdata_name, str) or not pgdata_name:
            raise CliError("bad_model", "the rendered model has no pgdata volume name")
        services = data.get("services") or {}
        return cls(name, pgdata_name, dict(services), dict(volumes))

    def image(self, service: str) -> str:
        image = self.services.get(service, {}).get("image")
        if not isinstance(image, str):
            raise CliError("bad_model", f"service {service} has no image")
        return image

    def uid(self, service: str) -> int:
        uid = numeric_uid(self.services[service].get("user"))
        if uid is None:
            raise CliError("no_uid", f"consumer {service} has no numeric user in the rendered model")
        return uid


@dataclass(frozen=True)
class Delivery:
    service: str
    uid: int
    name: str


@dataclass(frozen=True)
class Plan:
    deliveries: tuple[Delivery, ...]
    sources: Mapping[str, SecretRef | None]  # file name -> reference; None is the generated DB password


def make_plan(model: Rendered, config: ReaderConfig) -> Plan:
    """Who gets which file (FR-CLI-5): only consumers present in the rendered active model."""
    tokens: list[SecretRef] = [p.token for u in config.users for p in u.providers]
    for ref in tokens:
        if ref.scheme == "file":
            raise CliError("not_deliverable", f"{ref} is resolved inside a container, not on the host")
    deliveries: set[Delivery] = set()
    sources: dict[str, SecretRef | None] = {}
    for service in DB_CONSUMERS:
        if service in model.services:
            deliveries.add(Delivery(service, model.uid(service), DB_PASSWORD))
            sources[DB_PASSWORD] = None
    if TOKEN_CONSUMER in model.services:
        uid = model.uid(TOKEN_CONSUMER)
        for ref in tokens:
            name = secret_file_name(ref)
            deliveries.add(Delivery(TOKEN_CONSUMER, uid, name))
            sources[name] = ref
    ordered = tuple(sorted(deliveries, key=lambda d: (d.service, d.name)))
    return Plan(ordered, dict(sorted(sources.items())))


def volume_user(model: Rendered, key: str) -> str:
    """`--user` for a one-off on volume `key` (FR-CLI-17): the mounting services' common user, or 0:0 when
    none declares one. Disagreement refuses."""
    users: list[object] = []
    for body in model.services.values():
        for mount in body.get("volumes") or []:
            if isinstance(mount, Mapping) and mount.get("type") == "volume" and mount.get("source") == key:
                users.append(body.get("user"))
    distinct = set(users)
    if not users:
        raise CliError("volume_unmounted", f"no service mounts volume {key}")
    if distinct == {None}:
        return "0:0"
    if len(distinct) == 1 and isinstance(users[0], str):
        return users[0]
    raise CliError("volume_users_disagree", f"the services mounting volume {key} disagree on their user")
