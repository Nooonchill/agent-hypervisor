from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .diagnostics import configure_diagnostics, read_log_tail
from .models import GroupStatus, HypervisorError
from .project_config import load_project_config
from .tmux import TmuxBackend


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hypervisor", description="Manage Qwen Code terminals in tmux worktrees"
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--config", type=Path, help="YAML configuration (default: ./config.yaml)")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("tui", help="open the interactive Textual interface")
    logs = subparsers.add_parser("logs", help="show recent diagnostic log entries")
    logs.add_argument("--lines", type=int, default=100)
    subparsers.add_parser(
        "check-config", help="validate agent definitions and flow without starting Qwen"
    )
    create = subparsers.add_parser("create", help="create a task group with configured agents")
    create.add_argument("group_id")
    create.add_argument("--cwd", type=Path, default=Path.cwd())
    create.add_argument("--agent-command", help="command launched in every terminal")
    create.add_argument(
        "--type", dest="task_type", default="feature", help="branch type, e.g. feature, fix, docs"
    )

    subparsers.add_parser("list", help="list groups")
    status = subparsers.add_parser("status", help="show one group")
    status.add_argument("group_id")

    rename = subparsers.add_parser("rename", help="rename a group (keeps its worktree path)")
    rename.add_argument("group_id")
    rename.add_argument("new_group_id")

    for name in ("activate", "deactivate"):
        command = subparsers.add_parser(name, help=f"{name} all terminals in a group")
        command.add_argument("group_id")

    for name in ("start", "stop", "restart"):
        command = subparsers.add_parser(name, help=f"{name} a terminal")
        command.add_argument("group_id")
        command.add_argument("terminal", help="agent-id or terminal number")
        if name == "start":
            command.add_argument("--session", help="resume this managed Qwen session UUID")

    send = subparsers.add_parser("send", help="send a prompt followed by Enter")
    send.add_argument("group_id")
    send.add_argument("terminal", help="agent-id or terminal number")
    send.add_argument("text")
    send.add_argument("--no-enter", action="store_true")

    key = subparsers.add_parser("key", help="send one control or navigation key")
    key.add_argument("group_id")
    key.add_argument("terminal", help="agent-id or terminal number")
    key.add_argument("key")

    capture = subparsers.add_parser("capture", help="print terminal screen/history")
    capture.add_argument("group_id")
    capture.add_argument("terminal", help="agent-id or terminal number")
    capture.add_argument("--lines", type=int, default=120)

    remove = subparsers.add_parser("remove", help="remove a group and its tmux session")
    remove.add_argument("group_id")
    remove.add_argument(
        "--remove-worktree",
        action="store_true",
        help="also remove a clean worktree (the branch is kept)",
    )
    sessions = subparsers.add_parser("sessions", help="list Qwen sessions for one terminal")
    sessions.add_argument("group_id")
    sessions.add_argument("terminal", help="agent-id or terminal number")
    return parser


def _group_dict(group: GroupStatus) -> dict[str, object]:
    payload = asdict(group)
    payload["state"] = group.state
    return payload


def _print_group(group: GroupStatus, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_group_dict(group), ensure_ascii=False))
        return
    print(f"{group.group_id}  {group.state}  tmux={group.session_name}")
    if group.worktree_path:
        print(f"  worktree: {group.worktree_path}")
    if group.branch:
        print(f"  branch: {group.branch}")
    for terminal in group.terminals:
        details = f" ({terminal.process})" if terminal.process else ""
        if terminal.exit_code is not None:
            details += f" exit={terminal.exit_code}"
        label = (
            f"{terminal.number} {terminal.agent_id}" if terminal.agent_id else str(terminal.number)
        )
        print(f"  {label}: {terminal.state.value}{details}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "logs":
        print(read_log_tail(args.lines))
        return 0
    try:
        project_config = load_project_config(args.config)
        if args.command == "check-config":
            if args.json:
                print(json.dumps(asdict(project_config), ensure_ascii=False))
            else:
                print(
                    f"Configuration OK: {len(project_config.agents)} agents, {len(project_config.flow)} flow steps"
                )
                for agent in project_config.agents:
                    print(
                        f"  {agent.agent_id}: {agent.agent_name}  model={agent.model or 'default'}"
                    )
                for step in project_config.flow:
                    print(f"  {step.step_id} -> {step.agent_id}: {step.goal}")
            return 0
        if args.command == "create" and args.agent_command:
            import shlex

            backend = TmuxBackend(shlex.split(args.agent_command), project_config=project_config)
        else:
            backend = TmuxBackend(project_config=project_config)
        if hasattr(args, "terminal"):
            terminals = backend.status(args.group_id).terminals
            matched = next(
                (item.number for item in terminals if item.agent_id == args.terminal), None
            )
            if matched is not None:
                args.terminal = matched
            elif args.terminal.isdecimal():
                args.terminal = int(args.terminal)
            else:
                raise HypervisorError(
                    f"Unknown agent-id in group {args.group_id!r}: {args.terminal}"
                )
        if args.command in (None, "tui"):
            from .tui import HypervisorApp

            configure_diagnostics()
            HypervisorApp(backend=backend).run()
            return 0
        if args.command == "create":
            _print_group(
                backend.create_group(args.group_id, args.cwd, task_type=args.task_type), args.json
            )
        elif args.command == "list":
            groups = backend.list_groups()
            if args.json:
                print(json.dumps([_group_dict(group) for group in groups], ensure_ascii=False))
            elif not groups:
                print("No groups")
            else:
                for group in groups:
                    print(f"{group.group_id}\t{group.state}")
        elif args.command == "status":
            _print_group(backend.status(args.group_id), args.json)
        elif args.command == "rename":
            _print_group(backend.rename_group(args.group_id, args.new_group_id), args.json)
        elif args.command in {"activate", "deactivate"}:
            _print_group(getattr(backend, f"{args.command}_group")(args.group_id), args.json)
        elif args.command in {"start", "stop", "restart"}:
            if args.command == "start":
                backend.start_terminal(args.group_id, args.terminal, args.session)
            else:
                getattr(backend, f"{args.command}_terminal")(args.group_id, args.terminal)
            _print_group(backend.status(args.group_id), args.json)
        elif args.command == "send":
            backend.send_text(args.group_id, args.terminal, args.text, not args.no_enter)
        elif args.command == "key":
            backend.send_key(args.group_id, args.terminal, args.key)
        elif args.command == "capture":
            print(backend.capture(args.group_id, args.terminal, args.lines))
        elif args.command == "remove":
            backend.remove_group(args.group_id, args.remove_worktree)
            if args.json:
                print(
                    json.dumps(
                        {
                            "removed": args.group_id,
                            "worktree_removed": args.remove_worktree,
                        },
                        ensure_ascii=False,
                    )
                )
        elif args.command == "sessions":
            sessions = backend.list_terminal_sessions(args.group_id, args.terminal)
            if args.json:
                print(json.dumps([asdict(session) for session in sessions], ensure_ascii=False))
            elif not sessions:
                print("No managed sessions")
            else:
                for session in sessions:
                    print(
                        f"{session.session_id}\tcreated={session.created_at}"
                        f"\tlast_started={session.last_started_at}"
                    )
        return 0
    except HypervisorError as error:
        if args.json:
            print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        else:
            print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
