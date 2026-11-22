# @trace FR-DEPLOY-5
"""OPS-DEPLOY: the server image owns the blobs mount point as uid 10001.

[config] Dockerfile order; [image] a fresh named volume is writable as 10001 under --read-only --cap-drop ALL.
"""

import secrets
import subprocess

from .conftest import DEPLOY, REPO, requires_docker_daemon, run_ok

DOCKERFILE = DEPLOY / "images" / "server.Dockerfile"
BLOBS = "/var/lib/reader/blobs"
CHOWN = f"mkdir -p {BLOBS} && chown 10001:10001 {BLOBS}"


def test_dockerfile_creates_blobs_owned_before_user() -> None:
    lines = [ln.strip() for ln in DOCKERFILE.read_text().splitlines()]
    run_idx = next((i for i, ln in enumerate(lines) if ln.startswith("RUN ") and CHOWN in ln), None)
    user_idx = next((i for i, ln in enumerate(lines) if ln.startswith("USER ")), None)
    assert run_idx is not None, f"server.Dockerfile has no `RUN {CHOWN}`"
    assert user_idx is not None, "server.Dockerfile has no USER"
    assert run_idx < user_idx, "the blobs RUN must precede USER"


@requires_docker_daemon
def test_fresh_blobs_volume_writable_as_10001() -> None:
    tag = f"reader-server:blobs-test-{secrets.token_hex(4)}"
    volume = f"reader-test-blobs-{secrets.token_hex(4)}"
    try:
        run_ok("docker", "build", "-q", "-f", str(DOCKERFILE), "-t", tag, str(REPO))
        run_ok("docker", "volume", "create", volume)
        out = run_ok(
            "docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--user", "10001:10001", "--entrypoint", "", "-v", f"{volume}:{BLOBS}", tag,
            "sh", "-c", f"echo x > {BLOBS}/probe && stat -c '%u:%g' {BLOBS}",
        )
        assert out.strip() == "10001:10001"
    finally:
        for cleanup in (["docker", "volume", "rm", "-f", volume], ["docker", "image", "rm", "-f", tag]):
            subprocess.run(cleanup, capture_output=True, check=False)  # noqa: S603
