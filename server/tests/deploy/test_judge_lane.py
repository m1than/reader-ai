# @trace FR-DEPLOY-16, INV-DEPLOY-11
"""OPS-DEPLOY [config]: the worker-judge service, the agentnet addresses pinned to agent-proxy's CLIENTS, the
judge-db network, agent-proxy's agentnet bind and the judge slots in the example (ADR-0039 B7b)."""

import ipaddress
from typing import Any

from .conftest import (
    EXAMPLE,
    PROFILE_SETS,
    full_render,
    load_yaml,
    networks_of,
    render,
    requires_docker_cli,
)

pytestmark = requires_docker_cli

AGENTNET = ipaddress.ip_network("10.230.7.0/24")
IP_RANGE = ipaddress.ip_network("10.230.7.128/25")
GATEWAY = "10.230.7.1"
PROXY = "10.230.7.2"
# Keys Compose adds or that legitimately differ between the worker and its lane (FR-DEPLOY-16 a, b).
DIFFERING = {"command", "networks", "volumes", "mem_limit", "memswap_limit", "healthcheck"}


def _agentnet_address(svc: dict[str, Any]) -> str | None:
    entry = (svc.get("networks") or {}).get("agentnet") or {}
    address = entry.get("ipv4_address")
    return str(address) if address else None


def test_lane_service_is_the_worker_with_its_own_command_networks_and_memory() -> None:
    services = full_render()["services"]
    lane, worker = services["worker-judge"], services["worker"]
    assert lane["command"] == ["reader-server", "--role", "worker", "--lane", "judge"]
    assert {k: v for k, v in lane.items() if k not in DIFFERING} == {
        k: v for k, v in worker.items() if k not in DIFFERING
    }
    assert lane["healthcheck"] == worker["healthcheck"]
    assert not render(())["services"]["worker-judge"].get("volumes")  # base: none; the override adds the bind
    assert int(lane["mem_limit"]) == 2 * 1024**3
    assert lane["memswap_limit"] == lane["mem_limit"]
    assert not lane.get("ports")


def test_lane_is_in_the_default_profile_on_its_three_networks() -> None:
    services = render(())["services"]
    assert "worker-judge" in services
    assert networks_of(services["worker-judge"]) == {"agentnet", "judge-db", "otel"}
    nets = render(())["networks"]
    assert nets["judge-db"].get("internal") is True
    assert nets["judge-db"]["ipam"]["config"] == [{"subnet": "10.230.8.0/24"}]


def test_agentnet_ipam_and_fixed_addresses() -> None:
    model = full_render()
    net = model["networks"]["agentnet"]
    assert net.get("internal") is True
    assert net["ipam"]["config"] == [{"subnet": str(AGENTNET), "ip_range": str(IP_RANGE), "gateway": GATEWAY}]
    services = model["services"]
    assert _agentnet_address(services["agent-proxy"]) == PROXY
    assert _agentnet_address(services["worker"]) == "10.230.7.10"
    assert _agentnet_address(services["worker-judge"]) == "10.230.7.11"


def test_agentnet_addresses_equal_the_proxy_client_table() -> None:
    from reader.agent.proxy import CLIENTS

    clients = {address: client.name for address, client in CLIENTS.items()}
    for profiles in PROFILE_SETS:
        for override in (False, True):
            model = render(profiles, override=override)
            services = model["services"]
            members = {n for n, svc in services.items() if "agentnet" in networks_of(svc)}
            expected = {"worker", "worker-judge"} | ({"agent-proxy"} if "agents" in profiles else set())
            assert members == expected, profiles
            addresses = {n: _agentnet_address(services[n]) for n in members}
            assert all(a is not None for a in addresses.values()), addresses
            for name, address in addresses.items():
                ip = ipaddress.ip_address(str(address))
                assert ip in AGENTNET, (name, address)
                assert ip not in IP_RANGE, (name, address)
                assert not services[name].get("network_mode"), name
                assert not services[name].get("extra_hosts"), name
            assert {addresses["worker"]: "worker", addresses["worker-judge"]: "worker-judge"} == clients
            assert GATEWAY not in clients
            if "agent-proxy" in services:
                command = services["agent-proxy"]["command"]
                assert command == ["--bind", f"{addresses['agent-proxy']}:8080"]
            for name, svc in services.items():  # no fixed address on another network equals a client's
                for net, entry in (svc.get("networks") or {}).items():
                    if net != "agentnet" and entry:
                        assert entry.get("ipv4_address") not in clients, (name, net)


def test_example_binds_every_judge_slot() -> None:
    from reader.agent.judges import JUDGE_SLOTS
    from reader.config import load_config

    config = load_config(EXAMPLE)
    tasks = load_yaml(EXAMPLE)["tasks"]
    assert set(JUDGE_SLOTS) <= set(config.tasks)
    assert {n: tasks[n]["model"] for n in JUDGE_SLOTS} == {n: s.v1 for n, s in JUDGE_SLOTS.items()}
