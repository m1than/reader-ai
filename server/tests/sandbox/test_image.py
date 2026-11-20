# @trace FR-SANDBOX-1, FR-SANDBOX-2, FR-SANDBOX-8
"""OPS-SANDBOX image: base pin, hash-pinned lock, no fetch-and-run, no VOLUME, user and ownership, the
kernelspec, image environment and the environment a kernel sees."""

import json
import re
from pathlib import Path

from .conftest import (
    DOCKERFILE,
    IMAGE,
    LOCK,
    PROBES,
    SANDBOX,
    SERVER_DOCKERFILE,
    Build,
    Sandbox,
    docker,
    load_yaml,
    requires_docker_daemon,
)


def _from_line(path: str) -> str:
    text = Path(path).read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().upper().startswith("FROM ")]
    assert len(lines) == 1, lines
    return lines[0]


def test_dockerfile_pins_base_and_forbids_fetch_and_volume() -> None:
    assert DOCKERFILE.exists(), "sandbox/Dockerfile does not exist"
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert _from_line(str(DOCKERFILE)) == _from_line(str(SERVER_DOCKERFILE))
    assert "@sha256:" in _from_line(str(DOCKERFILE))
    upper = text.upper()
    assert "VOLUME" not in upper
    assert not re.search(r"\b(curl|wget)\b", text)
    assert not re.search(r"build-essential|\bgcc\b|\bclang\b|apt-get install", text)
    pip_lines = [ln for ln in text.splitlines() if "pip install" in ln]
    assert pip_lines
    for ln in pip_lines:
        assert "--only-binary=:all:" in ln
        assert "--require-hashes" in ln
        assert "--no-deps" in ln
        assert "requirements.lock" in ln


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_lock_is_hash_pinned_and_generated() -> None:
    assert LOCK.exists(), "sandbox/requirements.lock does not exist"
    pins: dict[str, str] = {}
    current: str | None = None
    hashed: set[str] = set()
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s\\;]+)", line)
        if m:
            current = _norm(m.group(1))
            pins[current] = m.group(2)
        if "--hash=sha256:" in line and current is not None:
            hashed.add(current)
    assert pins.get("jupyter-kernel-gateway") == "3.0.1"
    assert pins.get("ipykernel") == "7.4.0"
    assert set(pins) == hashed, set(pins) - hashed
    generator = SANDBOX / "lock.sh"
    assert generator.exists(), "the committed lock generator sandbox/lock.sh does not exist"
    assert "requirements.in" in generator.read_text(encoding="utf-8")
    assert "--generate-hashes" in generator.read_text(encoding="utf-8")


@requires_docker_daemon
def test_image_builds_and_pip_check(image: Build) -> None:
    assert image.ok, image.log
    proc = docker("run", "--rm", "--network", "none", "--entrypoint", "python3", IMAGE, "-m", "pip", "check")
    assert proc.returncode == 0, proc.stdout + proc.stderr


@requires_docker_daemon
def test_no_volume_and_no_anonymous_mount(sandbox: Sandbox) -> None:
    sandbox.start()
    info = sandbox.inspect()
    assert info["Mounts"] == []
    img = json.loads(docker("image", "inspect", IMAGE).stdout)[0]
    assert not img["Config"].get("Volumes")


@requires_docker_daemon
def test_user_home_and_root_owned_files(sandbox: Sandbox) -> None:
    sandbox.start()
    img = json.loads(docker("image", "inspect", IMAGE).stdout)[0]
    assert img["Config"]["User"] == "1000:1000"
    out = json.loads(sandbox.exec_ok(r'''
import json, os, pwd
p = pwd.getpwnam("lab")
bad_owner, writable = [], []
for root, dirs, files in os.walk("/usr/local"):
    for n in dirs + files:
        f = os.path.join(root, n)
        st = os.lstat(f)
        if st.st_uid != 0:
            bad_owner.append(f)
        if not os.path.islink(f) and os.access(f, os.W_OK):
            writable.append(f)
print(json.dumps({"uid": os.getuid(), "gid": os.getgid(), "pw": [p.pw_uid, p.pw_gid, p.pw_dir],
                  "bad_owner": bad_owner[:5], "writable": writable[:5]}))
'''))
    assert out["uid"] == 1000
    assert out["gid"] == 1000
    assert out["pw"] == [1000, 1000, "/home/lab"]
    assert out["bad_owner"] == []
    assert out["writable"] == []


@requires_docker_daemon
def test_only_python3_kernelspec(sandbox: Sandbox) -> None:
    sandbox.start()
    resp = sandbox.http("GET", 8888, "/api/kernelspecs")
    assert resp["status"] == 200
    assert set(json.loads(resp["body"])["kernelspecs"]) == {"python3"}


@requires_docker_daemon
def test_image_env_and_kernel_env(sandbox: Sandbox) -> None:
    sandbox.start()
    img = json.loads(docker("image", "inspect", IMAGE).stdout)[0]
    base_ref = _from_line(str(DOCKERFILE)).split()[1]
    base = json.loads(docker("image", "inspect", base_ref).stdout)[0]
    names = {e.split("=", 1)[0] for e in img["Config"]["Env"]}
    base_names = {e.split("=", 1)[0] for e in base["Config"]["Env"]}
    assert names <= base_names | {"PATH", "LANG", "HOME"}, names - base_names
    expected = next(p for p in load_yaml(PROBES)["probes"] if p["id"] == "env-names")["value"]
    code = (
        "import os, json\n"
        "print(json.dumps([sorted(os.environ), os.path.exists('/run/secrets'), os.environ['PATH']]))"
    )
    res = sandbox.cell(code)
    assert res["status"] == "ok", res
    env_names, secrets, path = json.loads(res["stdout"])
    assert env_names == sorted(expected)
    assert secrets is False
    entries = path.split(":")
    assert len(entries) == len(set(entries)), path
