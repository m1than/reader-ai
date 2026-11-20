# @trace FR-DEPLOY-8, INV-DEPLOY-8, VEC-DEPLOY-3
"""OPS-DEPLOY [config]: the x-kernel template and the equality of every kernel service."""

from typing import Any

from .conftest import full_render, kernel_services, requires_docker_cli, tmpfs_map

pytestmark = requires_docker_cli

GIB2 = "2147483648"
LITERAL: dict[str, Any] = {
    "image": "reader-kernel:local",
    "user": "1000:1000",
    "read_only": True,
    "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges:true"],
    "mem_limit": GIB2,
    "memswap_limit": GIB2,
    "cpus": 2,
    "pids_limit": 256,
    "restart": "always",
    "profiles": ["kernels"],
    "healthcheck": {"test": ["CMD", "/usr/local/bin/reader-kernel-health"]},
}
LITERAL_TMPFS = {
    "/home/lab": {"noexec", "size=256m", "uid=1000"},
    "/tmp": {"noexec", "size=256m", "uid=1000"},  # noqa: S108 - a mount point inside the container
    "/run/evcxr": {"exec", "nosuid", "nodev", "size=1g", "uid=1000"},
}
FORBIDDEN = ("secrets", "volumes", "ports", "environment")


def _without_networks(svc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in svc.items() if k != "networks"}


def test_x_kernel_template_present() -> None:
    model = full_render()
    template = model["x-kernel"]
    assert template["image"] == "reader-kernel:local"
    assert template["mem_limit"] == template["memswap_limit"]


def test_all_kernel_services_equal_except_networks() -> None:
    kernels = kernel_services(full_render())
    assert set(kernels) == {"kernel-verify", "kernel-agent", "kernel-pair-1", "kernel-user-ivan"}
    shapes = [_without_networks(svc) for svc in kernels.values()]
    assert all(shape == shapes[0] for shape in shapes)


def test_one_kernel_equals_the_spec_literals() -> None:
    svc = kernel_services(full_render())["kernel-verify"]
    assert {k: svc.get(k) for k in LITERAL} == LITERAL
    assert tmpfs_map(svc) == LITERAL_TMPFS


def test_kernels_carry_no_secrets_volumes_ports_or_env() -> None:
    for name, svc in kernel_services(full_render()).items():
        assert not any(svc.get(k) for k in FORBIDDEN), name
