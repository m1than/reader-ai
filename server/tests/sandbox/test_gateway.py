# @trace FR-SANDBOX-3, FR-SANDBOX-10
"""OPS-SANDBOX Kernel Gateway settings (listing off, max_kernels 4, the KERNEL_* env rule, memory of 4 idle
kernels) and the image under the x-kernel template (a cell runs; read-only root; noexec /tmp)."""

import json
import re
import time

from .conftest import Sandbox, docker, requires_docker_daemon

GIB = 1024**3
UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": GIB, "kB": 1000, "MB": 1000**2, "GB": 1000**3}


def _mem_bytes(usage: str) -> float:
    m = re.match(r"^\s*([\d.]+)\s*([A-Za-z]+)", usage)
    assert m, usage
    return float(m.group(1)) * UNITS[m.group(2)]


@requires_docker_daemon
def test_listing_off_max_four_kernels_and_memory(sandbox: Sandbox) -> None:
    sandbox.start()
    assert sandbox.http("GET", 8888, "/api/kernels")["status"] == 403
    created = [sandbox.http("POST", 8888, "/api/kernels", '{"name": "python3"}')["status"] for _ in range(4)]
    assert created == [201, 201, 201, 201]
    fifth = sandbox.http("POST", 8888, "/api/kernels", '{"name": "python3"}')
    assert fifth["status"] >= 400, fifth
    time.sleep(5)
    stats = docker("stats", "--no-stream", "--format", "{{.MemUsage}}", sandbox.name)
    assert stats.returncode == 0, stats.stderr
    used = _mem_bytes(stats.stdout.split("/")[0])
    print(f"memory with 4 idle python3 kernels: {used / 1024**2:.1f} MiB")
    assert used <= GIB


@requires_docker_daemon
def test_request_env_only_kernel_prefixed_keys(sandbox: Sandbox) -> None:
    sandbox.start()
    code = "import os, json; print(json.dumps(['KERNEL_X' in os.environ, 'OTHER' in os.environ]))"
    res = sandbox.cell(code, env={"KERNEL_X": "1", "OTHER": "1"})
    assert res["status"] == "ok", res
    assert json.loads(res["stdout"]) == [True, False]


@requires_docker_daemon
def test_cell_runs_under_template_readonly_and_noexec(sandbox: Sandbox) -> None:
    sandbox.start()
    res = sandbox.cell("print(1)")
    assert res["status"] == "ok", res
    assert res["stdout"] == "1\n"
    res = sandbox.cell(r'''
import json, os, shutil, subprocess
out = {}
for path in ("/usr/local/x", "/x"):
    try:
        open(path, "w").write("x")
        out[path] = "written"
    except OSError as e:
        out[path] = type(e).__name__
shutil.copy("/bin/true", "/tmp/t")
os.chmod("/tmp/t", 0o755)
try:
    subprocess.run(["/tmp/t"], check=False)
    out["exec"] = "ran"
except OSError as e:
    out["exec"] = type(e).__name__
print(json.dumps(out))
''')
    assert res["status"] == "ok", res
    out = json.loads(res["stdout"])
    assert out["/usr/local/x"] in {"PermissionError", "OSError"}
    assert out["/x"] in {"PermissionError", "OSError"}
    assert out["exec"] == "PermissionError"
