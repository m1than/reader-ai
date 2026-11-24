# @trace FR-DEPLOY-3, FR-DEPLOY-4, INV-DEPLOY-4, INV-DEPLOY-5, VEC-DEPLOY-2, VEC-DEPLOY-3, FR-DEPLOY-13
"""OPS-DEPLOY [config]: networks, the api bind address, kernel isolation and the reachability matrix."""

from .conftest import (
    PROFILES,
    REACHABILITY,
    full_render,
    kernel_services,
    load_yaml,
    networks_of,
    render,
    requires_docker_cli,
    sharing_pairs,
)

pytestmark = requires_docker_cli

API_FRONT = "10.230.1.10"


def test_network_membership() -> None:
    model = full_render()
    members: dict[str, set[str]] = {}
    for name, svc in model["services"].items():
        for net in networks_of(svc):
            members.setdefault(net, set()).add(name)
    assert members["edge"] == {"caddy"}
    assert members["front"] == {"caddy", "api", "mcp"}
    assert members["core"] == {"api", "worker", "mcp", "postgres"}
    assert members["agentnet"] == {"worker", "agent-proxy"}
    assert members["egress"] == {"agent-proxy"}
    assert members["sb-verify"] == {"worker", "kernel-verify"}
    assert members["sb-agent"] == {"worker", "kernel-agent"}
    assert members["sb-pair-1"] == {"api", "kernel-pair-1"}
    assert members["sb-user-ivan"] == {"api", "kernel-user-ivan"}
    # Paired SRV-SEARCH change (FR-DEPLOY-3, ADR-0028): embed's own internal network, and embed on no other.
    assert members["embed"] == {"api", "worker", "embed"}
    assert networks_of(model["services"]["embed"]) == {"embed"}
    assert model["networks"]["embed"].get("internal") is True
    # Paired SRV-OBS change (FR-DEPLOY-13, ADR-0032): otel, obs and obs-ui.
    assert members["otel"] == {"api", "worker", "rotel"}
    assert members["obs"] == {"rotel", "mlflow", "worker"}
    assert members["obs-ui"] == {"caddy", "mlflow"}
    front = model["networks"]["front"]
    assert front["ipam"]["config"] == [{"subnet": "10.230.1.0/24", "ip_range": "10.230.1.128/25"}]
    assert model["services"]["api"]["networks"]["front"]["ipv4_address"] == API_FRONT


def test_only_edge_and_egress_are_not_internal() -> None:
    model = full_render()
    external = {name for name, net in model["networks"].items() if not net.get("internal")}
    assert external == {"edge", "egress"}


def test_api_binds_front_address_and_worker_listens_nowhere() -> None:
    services = full_render()["services"]
    api_cmd = services["api"]["command"]
    assert "--bind" in api_cmd
    bind = api_cmd[api_cmd.index("--bind") + 1]
    assert bind == f"{services['api']['networks']['front']['ipv4_address']}:8000"
    assert "--bind" not in services["worker"]["command"]


def test_kernel_resolves_only_its_declared_caller() -> None:
    model = full_render()
    services = model["services"]
    callers = {
        "kernel-verify": "worker", "kernel-agent": "worker",
        "kernel-pair-1": "api", "kernel-user-ivan": "api",
    }
    for kernel, caller in callers.items():
        peers = {
            other for other, svc in services.items()
            if other != kernel and networks_of(svc) & networks_of(services[kernel])
        }
        assert peers == {caller}, (kernel, peers)
    assert set(kernel_services(model)) == set(callers)


def _as_pairs(rows: list[list[str]]) -> set[frozenset[str]]:
    return {frozenset(row) for row in rows}


def test_sharing_pairs_equal_the_matrix() -> None:
    matrix = load_yaml(REACHABILITY)
    assert sharing_pairs(full_render()) == _as_pairs(matrix["allowed"])
    assert not sharing_pairs(full_render()) & _as_pairs(matrix["never"])


def test_override_adds_only_kernel_and_its_network() -> None:
    base = render(PROFILES)  # the same profiles as full_render (observability included, FR-DEPLOY-13)
    merged = full_render()
    assert set(merged["services"]) - set(base["services"]) == {"kernel-user-ivan"}
    assert set(merged["networks"]) - set(base["networks"]) == {"sb-user-ivan"}
