from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from pathlib import Path

from agent_orchestra.cli.app import build_cli_application
from agent_orchestra.self_hosting.bootstrap import (
    SelfHostingBootstrapConfig,
    build_self_hosting_template,
    load_runtime_gap_inventory,
)
from agent_orchestra.storage.postgres.models import schema_statements


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-orchestra")
    parser.set_defaults(handler=_handle_root)
    subparsers = parser.add_subparsers(dest="command", required=True)

    group = subparsers.add_parser("group")
    group_subparsers = group.add_subparsers(dest="group_command", required=True)
    group_create = group_subparsers.add_parser("create")
    group_create.add_argument("--group-id", required=True)
    group_create.add_argument("--display-name")
    group_create.set_defaults(handler=_handle_group_create)

    team = subparsers.add_parser("team")
    team_subparsers = team.add_subparsers(dest="team_command", required=True)
    team_create = team_subparsers.add_parser("create")
    team_create.add_argument("--group-id", required=True)
    team_create.add_argument("--team-id", required=True)
    team_create.add_argument("--name", required=True)
    team_create.set_defaults(handler=_handle_team_create)

    session = subparsers.add_parser("session")
    session_subparsers = session.add_subparsers(dest="session_command", required=True)
    session_common = argparse.ArgumentParser(add_help=False)
    session_common.add_argument(
        "--store-backend",
        choices=("in-memory", "postgres"),
        default="postgres",
    )
    session_common.add_argument("--dsn")
    session_common.add_argument("--schema", default="agent_orchestra")
    session_common.add_argument(
        "--output",
        choices=("json", "pretty"),
        default="json",
    )

    session_list = session_subparsers.add_parser("list", parents=[session_common])
    session_list.add_argument("--group-id", required=True)
    session_list.add_argument("--objective-id")
    session_list.set_defaults(handler=_handle_session_list)

    session_inspect = session_subparsers.add_parser("inspect", parents=[session_common])
    session_inspect.add_argument("--work-session-id", required=True)
    session_inspect.set_defaults(handler=_handle_session_inspect)

    session_new = session_subparsers.add_parser("new", parents=[session_common])
    session_new.add_argument("--group-id", required=True)
    session_new.add_argument("--objective-id", required=True)
    session_new.add_argument("--title")
    session_new.set_defaults(handler=_handle_session_new)

    session_attach = session_subparsers.add_parser("attach", parents=[session_common])
    session_attach.add_argument("--work-session-id", required=True)
    session_attach.add_argument("--force-warm-resume", action="store_true")
    session_attach.set_defaults(handler=_handle_session_attach)

    session_wake = session_subparsers.add_parser("wake", parents=[session_common])
    session_wake.add_argument("--work-session-id", required=True)
    session_wake.set_defaults(handler=_handle_session_wake)

    session_fork = session_subparsers.add_parser("fork", parents=[session_common])
    session_fork.add_argument("--work-session-id", required=True)
    session_fork.add_argument("--title")
    session_fork.set_defaults(handler=_handle_session_fork)

    schema = subparsers.add_parser("schema")
    schema.add_argument("--schema", default="agent_orchestra")
    schema.set_defaults(handler=_handle_schema)

    self_host = subparsers.add_parser("self-host")
    self_host_subparsers = self_host.add_subparsers(dest="self_host_command", required=True)

    inventory = self_host_subparsers.add_parser("inventory")
    inventory.add_argument("--knowledge-path")
    inventory.set_defaults(handler=_handle_self_host_inventory)

    seed_template = self_host_subparsers.add_parser("seed-template")
    seed_template.add_argument("--output", required=True)
    seed_template.add_argument("--objective-id", required=True)
    seed_template.add_argument("--group-id", required=True)
    seed_template.add_argument("--max-workstreams", type=int, default=2)
    seed_template.add_argument("--knowledge-path")
    seed_template.set_defaults(handler=_handle_self_host_seed_template)
    return parser


def _resolve_dsn(args: argparse.Namespace) -> str | None:
    if getattr(args, "dsn", None):
        return args.dsn
    return os.environ.get("AGENT_ORCHESTRA_DSN")


def _print_payload(payload: object, *, output: str = "json") -> None:
    indent = 2 if output == "pretty" else None
    print(json.dumps(payload, ensure_ascii=False, indent=indent))


def _session_app(args: argparse.Namespace):
    return build_cli_application(
        store_backend=args.store_backend,
        dsn=_resolve_dsn(args),
        schema=args.schema,
    )


def _handle_root(args: argparse.Namespace) -> int:
    raise SystemExit(f"Unsupported command: {args.command}")


def _handle_group_create(args: argparse.Namespace) -> int:
    _print_payload(
        {
            "command": "group.create",
            "group_id": args.group_id,
            "display_name": args.display_name,
        }
    )
    return 0


def _handle_team_create(args: argparse.Namespace) -> int:
    _print_payload(
        {
            "command": "team.create",
            "group_id": args.group_id,
            "team_id": args.team_id,
            "name": args.name,
        }
    )
    return 0


def _handle_session_list(args: argparse.Namespace) -> int:
    payload = asyncio.run(
        _session_app(args).session_list(
            group_id=args.group_id,
            objective_id=args.objective_id,
        )
    )
    _print_payload(payload, output=args.output)
    return 0


def _handle_session_inspect(args: argparse.Namespace) -> int:
    payload = asyncio.run(
        _session_app(args).session_inspect(work_session_id=args.work_session_id)
    )
    _print_payload(payload, output=args.output)
    return 0


def _handle_session_new(args: argparse.Namespace) -> int:
    payload = asyncio.run(
        _session_app(args).session_new(
            group_id=args.group_id,
            objective_id=args.objective_id,
            title=args.title,
        )
    )
    _print_payload(payload, output=args.output)
    return 0


def _handle_session_attach(args: argparse.Namespace) -> int:
    payload = asyncio.run(
        _session_app(args).session_attach(
            work_session_id=args.work_session_id,
            force_warm_resume=args.force_warm_resume,
        )
    )
    _print_payload(payload, output=args.output)
    return 0


def _handle_session_wake(args: argparse.Namespace) -> int:
    payload = asyncio.run(
        _session_app(args).session_wake(
            work_session_id=args.work_session_id,
        )
    )
    _print_payload(payload, output=args.output)
    return 0


def _handle_session_fork(args: argparse.Namespace) -> int:
    payload = asyncio.run(
        _session_app(args).session_fork(
            work_session_id=args.work_session_id,
            title=args.title,
        )
    )
    _print_payload(payload, output=args.output)
    return 0


def _handle_schema(args: argparse.Namespace) -> int:
    print("\n\n".join(schema_statements(args.schema)))
    return 0


def _handle_self_host_inventory(args: argparse.Namespace) -> int:
    inventory = load_runtime_gap_inventory(args.knowledge_path)
    _print_payload(
        [item.to_dict() for item in inventory],  # type: ignore[arg-type]
        output="pretty",
    )
    return 0


def _handle_self_host_seed_template(args: argparse.Namespace) -> int:
    inventory = load_runtime_gap_inventory(args.knowledge_path)
    template = build_self_hosting_template(
        inventory=inventory,
        config=SelfHostingBootstrapConfig(
            objective_id=args.objective_id,
            group_id=args.group_id,
            max_workstreams=args.max_workstreams,
            knowledge_path=args.knowledge_path,
        ),
    )
    target = Path(args.output)
    target.write_text(json.dumps(template.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    _print_payload(
        {
            "command": "self-host.seed-template",
            "output": str(target),
            "workstreams": len(template.workstreams),
        }
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", _handle_root)
    try:
        return handler(args)
    except Exception as exc:
        output = getattr(args, "output", "json")
        _print_payload({"error": str(exc)}, output=output)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess integration test
    raise SystemExit(main())
