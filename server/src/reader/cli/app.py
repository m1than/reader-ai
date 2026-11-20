"""Entry point of the `reader` console script (FR-CLI-1)."""

import argparse
import getpass
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from reader.cli.commands import (
    Ctx,
    cmd_bootstrap,
    cmd_down,
    cmd_secrets_set,
    cmd_status,
    cmd_up,
    cmd_watcher_status,
    cmd_watcher_stop,
)
from reader.cli.maintenance import cmd_backup, cmd_doctor, cmd_restore, cmd_trust
from reader.cli.resolve import macos_keyring
from reader.cli.runner import PosixSpawner, SubprocessRunner, safe_env

SUPPORT = Path.home() / "Library" / "Application Support" / "reader-ai"


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", type=Path, default=Path("reader.yaml"), help="reader.yaml (default ./reader.yaml)"
    )
    parser = argparse.ArgumentParser(
        prog="reader", description="Start, watch and maintain the reader-ai stack."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("up", parents=[common], help="build, deliver secrets, start and wait for health")
    down = commands.add_parser("down", parents=[common], help="stop the watcher and the stack (volumes stay)")
    down.add_argument("--untrust", action="store_true", help="also remove every trusted local root")
    status = commands.add_parser("status", parents=[common], help="service health and watcher state")
    status.add_argument("--json", action="store_true")
    watcher = commands.add_parser("watcher", help="the secret-delivery watcher")
    watcher_commands = watcher.add_subparsers(dest="watcher_command", required=True)
    watcher_commands.add_parser("status")
    watcher_commands.add_parser("stop")
    bootstrap = commands.add_parser("bootstrap", parents=[common], help="open a passkey sign-up link")
    bootstrap.add_argument("--handle", help="user handle (default: the owner)")
    doctor = commands.add_parser("doctor", parents=[common], help="check the host setup")
    doctor.add_argument("--json", action="store_true")
    secrets = commands.add_parser("secrets", help="Keychain secrets")
    secrets_commands = secrets.add_subparsers(dest="secrets_command", required=True)
    secrets_set = secrets_commands.add_parser("set", help="store a value under service reader-ai")
    secrets_set.add_argument("name")
    commands.add_parser("trust", parents=[common], help="trust the local Caddy root for TLS (owner-run)")
    backup = commands.add_parser("backup", parents=[common], help="back up the database and volumes")
    backup.add_argument("dir", type=Path)
    restore = commands.add_parser("restore", parents=[common], help="restore onto a fresh stack")
    restore.add_argument("dir", type=Path)
    return parser


def default_ctx() -> Ctx:
    repo = Path(__file__).resolve().parents[4]
    return Ctx(
        runner=SubprocessRunner(),
        keyring=macos_keyring(),
        env=safe_env(os.environ),
        run_dir=SUPPORT / "run",
        log_dir=Path.home() / "Library" / "Logs" / "reader-ai",
        compose_file=repo / "deploy" / "compose.yaml",
        spawner=PosixSpawner(),
        out=sys.stdout,
        err=sys.stderr,
        stdin=sys.stdin,
        is_tty=sys.stdin.isatty(),
        getpass=getpass.getpass,
        ask=lambda prompt: input(prompt).strip().lower() in ("y", "yes"),
        sleep=time.sleep,
        monotonic=time.monotonic,
        kill=os.kill,
    )


def _literal_values(argv: Sequence[str]) -> list[str]:
    """A handle or a secret name that starts with `-` reaches its own rule (exit 2) instead of argparse
    reading it as an option: `--handle -x` becomes `--handle=-x`; `secrets set -x`, `secrets set -- -x`."""
    out: list[str] = []
    items = iter(argv)
    for item in items:
        value = next(items, None) if item == "--handle" else None
        out.append(f"--handle={value}" if value is not None else item)
    if out[:2] == ["secrets", "set"] and len(out) > 2 and out[2] != "--":
        out.insert(2, "--")
    return out


def main(argv: Sequence[str] | None = None, ctx: Ctx | None = None) -> int:
    args = build_parser().parse_args(_literal_values(sys.argv[1:] if argv is None else argv))
    ctx = ctx if ctx is not None else default_ctx()
    match args.command:
        case "up":
            return cmd_up(ctx, args.config)
        case "down":
            return cmd_down(ctx, args.config, args.untrust)
        case "status":
            return cmd_status(ctx, args.config, args.json)
        case "watcher":
            return cmd_watcher_status(ctx) if args.watcher_command == "status" else cmd_watcher_stop(ctx)
        case "bootstrap":
            return cmd_bootstrap(ctx, args.config, args.handle)
        case "doctor":
            return cmd_doctor(ctx, args.config, args.json)
        case "secrets":
            return cmd_secrets_set(ctx, args.name)
        case "trust":
            return cmd_trust(ctx, args.config)
        case "backup":
            return cmd_backup(ctx, args.config, args.dir)
        case "restore":
            return cmd_restore(ctx, args.config, args.dir)
    return 2
