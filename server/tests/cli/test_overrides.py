"""`reader status` and `reader up` print the settings overrides (SRV-CONFIG FR-CONFIG-26, paired OPS-CLI
change).

@trace FR-CONFIG-26
"""

from pathlib import Path

from .conftest import FIXTURES, held_lock, make_world, rendered, stack, write_config

SQL = "SELECT key, value FROM settings_overrides ORDER BY key"
ROWS = (
    b"budgets.monthly_usd|300\n"
    b'sharing.signup|"open"\n'
    b"tasks.gone.budget_usd_per_lesson|5\n"
)


def _config(tmp_path: Path) -> Path:
    text = (FIXTURES / "config.yaml").read_text(encoding="utf-8")
    text += 'sharing: { signup: "off" }\nbudgets: { monthly_usd: 200 }\n'
    return write_config(tmp_path / "cfg", text)


def test_status_prints_applied_and_ignored_overrides(tmp_path: Path) -> None:
    from reader.cli.app import main

    world = make_world(tmp_path)
    stack(world, rendered())
    world.runner.on("psql", result=ROWS)
    config = _config(tmp_path)
    with held_lock(world.ctx.run_dir):
        main(["status", "--config", str(config)], ctx=world.ctx)
    lines = [line.strip() for line in world.out.splitlines()]
    assert "override budgets.monthly_usd: 300 (reader.yaml: 200)" in lines
    assert "override sharing.signup: ignored (narrowed)" in lines
    assert "override tasks.gone.budget_usd_per_lesson: ignored (not_in_file)" in lines
    (call,) = [c for c in world.runner.calls if "psql" in c.argv]
    assert call.argv == [
        "docker", "compose", "-p", "reader-vec", "exec", "-T", "postgres",
        "psql", "-U", "postgres", "-d", "postgres", "-At", "-c", SQL,
    ]
    assert call.timeout is not None
    assert call.timeout > 0


def test_a_failed_query_prints_unavailable_and_does_not_fail_status(tmp_path: Path) -> None:
    from reader.cli.app import main

    codes = []
    for failing in (False, True):
        world = make_world(tmp_path / str(failing))
        stack(world, rendered())
        world.runner.on("psql", result=(2, b"", b"relation does not exist") if failing else b"")
        with held_lock(world.ctx.run_dir):
            codes.append(main(["status", "--config", str(_config(tmp_path / str(failing)))], ctx=world.ctx))
        if failing:
            assert "overrides: unavailable" in world.out
            assert "relation" not in world.out + world.err
        else:
            assert "override" not in world.out
    assert codes[0] == codes[1]


def test_up_prints_the_overrides_once_healthy(tmp_path: Path) -> None:
    from reader.cli.commands import cmd_up

    world = make_world(tmp_path)
    world.keyring.items[("reader-ai-system", "postgres")] = "pw"
    world.keyring.items[("reader-ai", "claude-oauth-token")] = "tok"
    stack(world, rendered(), volume_exists=True)
    world.runner.on("psql", result=b"budgets.monthly_usd|300\n")
    text = 'version: 1\ninstance: { url: "https://localhost", data_dir: ./data }\n' \
        "users:\n  - { handle: ivan, role: owner }\nbudgets: { monthly_usd: 200 }\n" \
        "observability: { enabled: false }\n"
    assert cmd_up(world.ctx, write_config(tmp_path / "cfg", text)) == 0
    out = world.out.splitlines()
    assert "override budgets.monthly_usd: 300 (reader.yaml: 200)" in [line.strip() for line in out]
    assert [c for c in world.runner.calls if "psql" in c.argv]
