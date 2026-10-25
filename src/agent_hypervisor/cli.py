from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from .diagnostics import configure_diagnostics, read_log_tail
from .models import GroupStatus, HypervisorError
from .project_config import ProjectConfig, load_project_config
from .tmux import TmuxBackend


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hypervisor",
        description="Manage task groups, sequential workflows, and Qwen Code terminals",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--config", type=Path, help="YAML configuration (default: ./config.yaml)")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("tui", help="open the interactive Textual interface")
    mcp = subparsers.add_parser("mcp", help="MCP server and Qwen settings.json configuration")
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_sub.add_parser("config", help="print a fragment to merge into Qwen settings.json")
    serve = mcp_sub.add_parser("serve", help="run the session-bound stdio MCP server")
    for flag, variable in (
        ("group", "HYPERVISOR_GROUP"),
        ("agent", "HYPERVISOR_AGENT"),
        ("session", "HYPERVISOR_SESSION"),
    ):
        serve.add_argument(f"--{flag}", default=os.environ.get(variable))
    serve.add_argument("--state-path", type=Path, default=os.environ.get("HYPERVISOR_STATE_PATH"))
    logs = subparsers.add_parser("logs", help="show recent diagnostic log entries")
    logs.add_argument("--lines", type=int, default=100)
    subparsers.add_parser(
        "check-config", help="validate agent definitions and named workflows without starting Qwen"
    )
    create = subparsers.add_parser("create", help="create a task group with configured agents")
    create.add_argument("group_id")
    create.add_argument("--cwd", type=Path, default=Path.cwd())
    create.add_argument("--agent-command", help="command launched in every terminal")
    create.add_argument(
        "--type", dest="task_type", default="feature", help="branch type, e.g. feature, fix, docs"
    )
    create.add_argument("--flow", help="named workflow to snapshot into the new task group")
    create.add_argument("--request", help="initial task request (default from task-form)")
    create.add_argument("--goal", help="shared task goal (default from task-form)")
    create.add_argument(
        "--source", action="append", help="source path or URL (repeatable; default from task-form)"
    )
    create.add_argument(
        "--constraint",
        "--constraints",
        dest="constraints",
        help="additional shared constraints",
    )
    create.add_argument(
        "--start-workflow",
        action="store_true",
        help="deliver the first workflow prompt after creating the group",
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

    send = subparsers.add_parser("send", help="queue a prompt with Ctrl+Q")
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

    mode = subparsers.add_parser("mode", help="pause or restore automatic tasks for one agent")
    mode.add_argument("group_id")
    mode.add_argument("terminal", help="agent-id or terminal number")
    mode.add_argument("mode", choices=("manual", "workflow"))
    mode.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    workflow = subparsers.add_parser("workflow", help="inspect or advance a task workflow")
    workflow_commands = workflow.add_subparsers(dest="workflow_command", required=True)
    for name, help_text in (
        ("status", "show workflow state"),
        ("start", "start the first workflow stage"),
        ("resume", "explicitly resume a workflow after interruption"),
        ("events", "show workflow events"),
    ):
        command = workflow_commands.add_parser(name, help=help_text)
        command.add_argument("group_id")
        if name == "resume":
            command.add_argument(
                "--retry-delivery",
                action="store_true",
                help="explicitly resend an uncertain prompt",
            )
        command.add_argument(
            "--json",
            dest="json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="print machine-readable JSON",
        )
    report = workflow_commands.add_parser("report", help="read a workflow report")
    report.add_argument("group_id")
    report.add_argument("path", help="report path recorded in workflow events")
    report.add_argument(
        "--json",
        dest="json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="wrap report content in machine-readable JSON",
    )

    stage = subparsers.add_parser("stage", help="submit a workflow stage")
    stage_sub = stage.add_subparsers(dest="stage_command", required=True)
    complete = stage_sub.add_parser("complete", help="submit a stage for checks and review")
    complete.add_argument("group_id")
    complete.add_argument("step_id")
    complete.add_argument("--attempt", required=True)
    complete.add_argument("--session", required=True)
    complete.add_argument("--summary", required=True)
    complete.add_argument("--json", dest="json", action="store_true", default=argparse.SUPPRESS)

    review = subparsers.add_parser("review", help="submit an independent review decision")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    submit = review_sub.add_parser("submit", help="submit review decision and findings")
    submit.add_argument("group_id")
    submit.add_argument("step_id")
    submit.add_argument("--attempt", required=True)
    submit.add_argument("--session", required=True)
    submit.add_argument(
        "--decision", required=True, choices=("approved", "changes_requested", "blocked")
    )
    submit.add_argument("--summary", required=True)
    submit.add_argument(
        "--finding", action="append", default=[], help="concrete finding (repeatable)"
    )
    submit.add_argument("--json", dest="json", action="store_true", default=argparse.SUPPRESS)

    human = subparsers.add_parser("human", help="record a human workflow decision")
    human_sub = human.add_subparsers(dest="human_command", required=True)
    for name in ("approve", "reject"):
        decision = human_sub.add_parser(name, help=f"{name} the current stage")
        decision.add_argument("group_id")
        decision.add_argument("step_id")
        decision.add_argument("--attempt", required=True)
        decision.add_argument("--comment", required=name == "reject", default="")
        decision.add_argument("--json", dest="json", action="store_true", default=argparse.SUPPRESS)
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


def _print_workflow(value: object, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False))
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "logs":
        print(read_log_tail(args.lines))
        return 0
    try:
        if args.command == "mcp":
            if args.mcp_command == "config":
                from .mcp_config import qwen_settings

                print(json.dumps(qwen_settings(), ensure_ascii=False, indent=2))
                return 0
            from .agent_api import AgentBinding
            from .mcp_server import create_server
            from .state import StateStore

            if not all((args.group, args.agent, args.session)):
                raise HypervisorError(
                    "MCP requires group, agent and session identity; launch it from a Hypervisor-managed Qwen session or pass --group, --agent and --session"
                )
            backend = TmuxBackend(
                state_store=StateStore(args.state_path) if args.state_path else StateStore(),
                project_config=ProjectConfig.default(),
            )
            create_server(backend.workflow, AgentBinding(args.group, args.agent, args.session)).run(
                transport="stdio"
            )
            return 0
        reads_yaml = args.command in (None, "tui", "check-config", "create")
        project_config = load_project_config(args.config) if reads_yaml else ProjectConfig.default()
        if args.command == "check-config":
            if args.json:
                print(json.dumps(asdict(project_config), ensure_ascii=False))
            else:
                print(
                    f"Configuration OK: {len(project_config.agents)} agents, {len(project_config.flows)} workflows"
                )
                for agent in project_config.agents:
                    print(
                        f"  {agent.agent_id}: {agent.agent_name}  model={agent.model or 'default'}"
                    )
                for flow in project_config.flows:
                    print(f"  {flow.flow_id}: {flow.name}")
                    for step in flow.steps:
                        print(f"    {step.step_id} -> {step.agent_id}: {step.goal}")
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
            group = backend.create_group(
                args.group_id,
                args.cwd,
                task_type=args.task_type,
                flow_id=args.flow,
                request=args.request,
                goal=args.goal,
                sources=tuple(args.source) if args.source is not None else None,
                constraints=args.constraints,
            )
            if args.start_workflow:
                _print_workflow(backend.workflow.start(args.group_id), args.json)
            else:
                _print_group(group, args.json)
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
        elif args.command == "mode":
            _print_workflow(
                backend.workflow.set_agent_mode(args.group_id, args.terminal, args.mode), args.json
            )
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
        elif args.command == "workflow":
            service = backend.workflow
            subcommand = args.workflow_command
            if subcommand == "status":
                _print_workflow(service.status(args.group_id), args.json)
            elif subcommand == "start":
                _print_workflow(service.start(args.group_id), args.json)
            elif subcommand == "resume":
                _print_workflow(
                    service.resume(args.group_id, retry_delivery=args.retry_delivery), args.json
                )
            elif subcommand == "events":
                _print_workflow(service.events(args.group_id), args.json)
            elif subcommand == "report":
                content = service.report(args.group_id, args.path)
                if args.json:
                    print(json.dumps({"path": args.path, "content": content}, ensure_ascii=False))
                else:
                    print(content, end="" if content.endswith("\n") else "\n")
        elif args.command == "stage" and args.stage_command == "complete":
            _print_workflow(
                backend.workflow.complete(
                    args.group_id,
                    args.step_id,
                    args.attempt,
                    args.summary,
                    session_id=args.session,
                ),
                args.json,
            )
        elif args.command == "review" and args.review_command == "submit":
            _print_workflow(
                backend.workflow.submit_review(
                    args.group_id,
                    args.step_id,
                    args.attempt,
                    args.decision,
                    args.summary,
                    findings=args.finding,
                    session_id=args.session,
                ),
                args.json,
            )
        elif args.command == "human":
            if args.human_command == "reject" and not args.comment.strip():
                raise HypervisorError("Reject requires a non-empty comment")
            _print_workflow(
                backend.workflow.human_decision(
                    args.group_id,
                    args.step_id,
                    args.attempt,
                    approved=args.human_command == "approve",
                    comment=args.comment,
                ),
                args.json,
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
