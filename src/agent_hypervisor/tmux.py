from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from pathlib import Path

from .models import AgentManualMode, GroupStatus, HypervisorError, TerminalState, TerminalStatus
from .project_config import AgentSpec, ProjectConfig, load_project_config
from .state import AgentSession, GroupRecord, StateStore, terminal_session_name, utc_now
from .worktrees import GitWorktreeManager
from .task_form import resolve_task_context

SESSION_PREFIX = "agent-hypervisor-"
GROUP_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
DEFAULT_AGENT_COMMAND = "npm exec --yes --package=@qwen-code/qwen-code -- qwen"

Runner = Callable[..., subprocess.CompletedProcess[str]]


class TmuxBackend:
    def __init__(
        self,
        agent_command: Sequence[str] | None = None,
        runner: Runner = subprocess.run,
        agent_runner: Runner = subprocess.run,
        state_store: StateStore | None = None,
        worktrees: GitWorktreeManager | None = None,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        project_config: ProjectConfig | None = None,
    ) -> None:
        configured = os.environ.get("HYPERVISOR_AGENT_COMMAND", DEFAULT_AGENT_COMMAND)
        self.agent_command = tuple(agent_command or shlex.split(configured))
        if not self.agent_command:
            raise HypervisorError("Agent command cannot be empty")
        self._runner = runner
        self._agent_runner = agent_runner
        self.state = state_store or StateStore()
        self.worktrees = worktrees or GitWorktreeManager()
        self._uuid_factory = uuid_factory
        self.project_config = project_config or load_project_config()
        self._terminal_counts: dict[str, int] = {}

    @property
    def workflow(self):
        from .workflow import WorkflowService

        return WorkflowService(self)

    @staticmethod
    def validate_group_id(group_id: str) -> str:
        if not GROUP_ID_PATTERN.fullmatch(group_id):
            raise HypervisorError(
                "Group ID must be 1-48 characters: letters, digits, dot, underscore or dash"
            )
        return group_id

    @classmethod
    def session_name(cls, group_id: str) -> str:
        return f"{SESSION_PREFIX}{cls.validate_group_id(group_id)}"

    def _tmux(self, *args: str, check: bool = True) -> str:
        try:
            result = self._runner(
                ["tmux", *args],
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as error:
            raise HypervisorError("tmux is not installed or is not available in PATH") from error
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown tmux error"
            raise HypervisorError(detail)
        return result.stdout

    def _has_session(self, session_name: str) -> bool:
        try:
            self._tmux("has-session", "-t", session_name)
        except HypervisorError:
            return False
        return True

    def create_group(
        self,
        group_id: str,
        cwd: str | Path | None = None,
        *,
        task_type: str = "feature",
        flow_id: str | None = None,
        request: str | None = None,
        goal: str | None = None,
        sources: str | tuple[str, ...] | None = None,
        constraints: str | None = None,
    ) -> GroupStatus:
        session = self.session_name(group_id)
        if self.state.get(group_id) is not None or self._has_session(session):
            raise HypervisorError(f"Group {group_id!r} already exists")
        source = Path(cwd or Path.cwd()).resolve()
        if not source.is_dir():
            raise HypervisorError(f"Working directory does not exist: {source}")
        flow = self.project_config.select_flow(flow_id)
        context = resolve_task_context(
            self.project_config.form_for_flow(flow_id),
            {
                "request": request,
                "goal": goal,
                "sources": sources,
                "constraints": constraints,
            },
        )
        worktree = self.worktrees.create(group_id, source, task_type)
        sessions = {
            number: [AgentSession(str(self._uuid_factory()), number, agent_id=agent.agent_id)]
            for number, agent in enumerate(self.project_config.agents, 1)
        }
        record = GroupRecord(
            group_id=group_id,
            repository_root=str(worktree.repository_root),
            worktree_path=str(worktree.path),
            branch=worktree.branch,
            sessions=sessions,
            active_sessions={number: items[0].session_id for number, items in sessions.items()},
            agents=self.project_config.agents,
            flow=flow.steps if flow else self.project_config.flow,
            task_code=group_id,
            task_type=task_type,
            flow_id=flow.flow_id if flow else "",
            data_dir=self.project_config.data_dir,
            agent_command=self.agent_command,
        )
        try:
            self.state.save(record)
            self.workflow.initialize(group_id, **context)
        except Exception:
            self.state.delete(group_id)
            try:
                self.worktrees.remove(record.repository_root, record.worktree_path)
            except HypervisorError:
                pass
            raise
        return self.status(group_id)

    def list_groups(self) -> list[GroupStatus]:
        records = {record.group_id: record for record in self.state.list_groups()}
        output = self._tmux("list-sessions", "-F", "#{session_name}", check=False)
        tmux_ids = {
            name.removeprefix(SESSION_PREFIX)
            for name in output.splitlines()
            if name.startswith(SESSION_PREFIX)
        }
        return [self.status(group_id) for group_id in sorted(records.keys() | tmux_ids)]

    def status(self, group_id: str) -> GroupStatus:
        session = self.session_name(group_id)
        record = self.state.get(group_id)
        agents = record.terminal_agents if record else self.project_config.agents
        self._terminal_counts[group_id] = len(agents)
        try:
            rows = self._tmux(
                "list-windows",
                "-t",
                session,
                "-F",
                "#{session_attached}\t#{window_name}\t#{pane_dead}\t#{pane_dead_status}"
                "\t#{pane_current_command}\t#{@hypervisor-state}",
            )
        except HypervisorError as error:
            if record is None:
                raise HypervisorError(f"Group {group_id!r} does not exist") from error
            terminals = tuple(
                TerminalStatus(
                    number,
                    TerminalState.STOPPED,
                    agent_id=agent.agent_id,
                    agent_name=agent.agent_name,
                )
                for number, agent in enumerate(agents, 1)
            )
            return GroupStatus(
                group_id,
                session,
                0,
                terminals,
                record.worktree_path,
                record.branch,
                False,
            )
        by_number: dict[int, TerminalStatus] = {}
        attached_clients = 0
        for row in rows.splitlines():
            attached_raw, index_raw, dead_raw, exit_raw, process, explicit = (
                row.split("\t", 5) + ["", "", "", "", "", ""]
            )[:6]
            if attached_raw.isdigit():
                attached_clients = int(attached_raw)
            match = re.fullmatch(r"terminal-(\d+)", index_raw)
            if match is None:
                continue
            number = int(match.group(1))
            if number not in range(1, len(agents) + 1):
                continue
            dead = dead_raw == "1"
            if explicit == "stopped":
                state = TerminalState.STOPPED
            elif dead:
                state = TerminalState.EXITED
            else:
                state = TerminalState.RUNNING
            exit_code = int(exit_raw) if dead and exit_raw.lstrip("-").isdigit() else None
            agent = agents[number - 1]
            by_number[number] = TerminalStatus(
                number, state, process, exit_code, agent.agent_id, agent.agent_name
            )
        terminals = tuple(
            by_number.get(
                number,
                TerminalStatus(
                    number,
                    TerminalState.MISSING,
                    agent_id=agent.agent_id,
                    agent_name=agent.agent_name,
                ),
            )
            for number, agent in enumerate(agents, 1)
        )
        return GroupStatus(
            group_id,
            session,
            attached_clients,
            terminals,
            record.worktree_path if record else "",
            record.branch if record else "",
            True,
        )

    def activate_group(self, group_id: str) -> GroupStatus:
        record = self.state.get(group_id)
        if record is None:
            raise HypervisorError(f"Group {group_id!r} has no managed worktree")
        if self._has_session(self.session_name(group_id)):
            return self.status(group_id)
        has_started_sessions = any(
            session.started for sessions in record.sessions.values() for session in sessions
        )
        recorded_sessions = (
            set(self._qwen_session_catalog(record.worktree_path, record.agent_command))
            if has_started_sessions
            else set()
        )
        for terminal in range(1, len(record.terminal_agents) + 1):
            record = self.state.get(group_id) or record
            self._start_terminal(
                record,
                terminal,
                record.active_sessions.get(terminal),
                recorded_sessions,
            )
        return self.status(group_id)

    def deactivate_group(self, group_id: str) -> GroupStatus:
        session = self.session_name(group_id)
        record = self.state.get(group_id)
        if record is None:
            raise HypervisorError(f"Group {group_id!r} does not exist")
        if self._has_session(session):
            self._tmux("kill-session", "-t", session)
        return self.status(group_id)

    def rename_group(self, group_id: str, new_group_id: str) -> GroupStatus:
        record = self.state.get(group_id)
        if record is None:
            raise HypervisorError(f"Group {group_id!r} does not exist")
        guard = self.workflow._store(group_id).transaction() if record.flow_id else nullcontext()
        with guard:
            return self._rename_group(record.group_id, new_group_id)

    def _rename_group(self, group_id: str, new_group_id: str) -> GroupStatus:
        old_session = self.session_name(group_id)
        new_session = self.session_name(new_group_id)
        if self.state.get(new_group_id) is not None or self._has_session(new_session):
            raise HypervisorError(f"Group {new_group_id!r} already exists")
        record = self.state.get(group_id)
        if record is None:
            raise HypervisorError(f"Group {group_id!r} does not exist")
        if self._has_session(old_session):
            self._tmux("rename-session", "-t", old_session, new_session)
            self._tmux("set-option", "-t", new_session, "@hypervisor-group-id", new_group_id)
        try:
            self.state.rename(group_id, new_group_id)
        except Exception:
            if self._has_session(new_session):
                self._tmux("rename-session", "-t", new_session, old_session, check=False)
            raise
        return self.status(new_group_id)

    def remove_group(self, group_id: str, remove_worktree: bool = False) -> None:
        record = self.state.get(group_id)
        guard = (
            self.workflow._store(group_id).transaction()
            if record and record.flow_id
            else nullcontext()
        )
        with guard:
            self._remove_group(record.group_id if record else group_id, remove_worktree)

    def _remove_group(self, group_id: str, remove_worktree: bool) -> None:
        session = self.session_name(group_id)
        record = self.state.get(group_id)
        if record is None and not self._has_session(session):
            raise HypervisorError(f"Group {group_id!r} does not exist")
        if self._has_session(session):
            self._tmux("kill-session", "-t", session)
        if remove_worktree and record is not None:
            self.worktrees.remove(record.repository_root, record.worktree_path)
        self.state.delete(group_id)

    def list_terminal_sessions(self, group_id: str, terminal: int) -> list[AgentSession]:
        self._validate_terminal(terminal, group_id)
        record = self.state.get(group_id)
        if record is None:
            return []
        return sorted(
            record.sessions.get(terminal, []),
            key=lambda session: session.last_started_at,
            reverse=True,
        )

    def start_terminal(self, group_id: str, terminal: int, session_id: str | None = None) -> None:
        self._validate_terminal(terminal, group_id)
        record = self.state.get(group_id)
        if record is None:
            raise HypervisorError(f"Group {group_id!r} has no managed worktree")
        self._start_terminal(record, terminal, session_id)

    def _start_terminal(
        self,
        record: GroupRecord,
        terminal: int,
        session_id: str | None,
        recorded_sessions: set[str] | None = None,
    ) -> None:
        if not Path(record.worktree_path).is_dir():
            raise HypervisorError(f"Worktree does not exist: {record.worktree_path}")
        group_id = record.group_id
        agent = record.terminal_agents[terminal - 1]
        session = self._ensure_tmux_session(record)
        known = {item.session_id: item for item in record.sessions.get(terminal, [])}
        resume = False
        if session_id is None:
            session_id = str(self._uuid_factory())
            item = AgentSession(session_id, terminal, agent_id=agent.agent_id)
            item.started = True
            record.sessions.setdefault(terminal, []).append(item)
        elif session_id not in known:
            raise HypervisorError(f"Session {session_id!r} does not belong to terminal {terminal}")
        else:
            managed_session = known[session_id]
            managed_session.last_started_at = utc_now()
            if recorded_sessions is None:
                resume = managed_session.started and self._qwen_session_exists(
                    record.worktree_path, session_id, record.agent_command
                )
            else:
                resume = managed_session.started and session_id in recorded_sessions
            managed_session.started = True
        record.active_sessions[terminal] = session_id
        command = self._qwen_command(
            session_id,
            terminal=terminal,
            resume=resume,
            agent=agent,
            agent_command=record.agent_command,
            record=record,
        )
        target = self._target(session, terminal)
        status = self.status(group_id).terminals[terminal - 1]
        if status.state is TerminalState.MISSING:
            self._tmux(
                "new-window",
                "-d",
                "-t",
                session,
                "-n",
                f"terminal-{terminal}",
                "-c",
                record.worktree_path,
                command,
            )
            self._set_remain_on_exit(session, terminal)
        else:
            self._tmux(
                "respawn-window",
                "-k",
                "-t",
                target,
                "-c",
                record.worktree_path,
                command,
            )
        self._set_terminal_state(session, terminal, "running")
        self._set_terminal_session(session, terminal, session_id)
        self.state.save(record)

    def stop_terminal(self, group_id: str, terminal: int) -> None:
        session = self._require_terminal(group_id, terminal)
        target = self._target(session, terminal)
        status = self.status(group_id).terminals[terminal - 1]
        if status.state is TerminalState.MISSING:
            raise HypervisorError(f"Terminal {terminal} is missing in group {group_id!r}")
        idle = "printf '\\n[agent stopped by hypervisor]\\n'; exec sleep 2147483647"
        self._tmux("respawn-window", "-k", "-t", target, idle)
        self._set_terminal_state(session, terminal, "stopped")

    def restart_terminal(self, group_id: str, terminal: int) -> None:
        record = self.state.get(group_id)
        session_id = record.active_sessions.get(terminal) if record else None
        self.start_terminal(group_id, terminal, session_id)

    def send_text(self, group_id: str, terminal: int, text: str, enter: bool = True) -> None:
        if enter:
            self.queue_prompt(group_id, terminal, text)
        else:
            self._require_running_terminal(group_id, terminal)
            self.send_literal(group_id, terminal, text)

    def workflow_session(self, group_id: str, agent_id: str, session_id: str) -> int:
        """Switch the visible terminal without killing a submitting agent's CLI child.

        Earlier conversations remain in tmux history windows until group removal.
        Every review receives a new UUID, including reviews by the executor's role.
        """
        record = self.state.get(group_id)
        if record is None:
            raise HypervisorError(f"Unknown group: {group_id}")
        number = next(
            (
                index
                for index, agent in enumerate(record.terminal_agents, 1)
                if agent.agent_id == agent_id
            ),
            None,
        )
        if number is None:
            raise HypervisorError(f"Unknown agent in group: {agent_id}")
        session = self._ensure_tmux_session(record)
        current = record.active_sessions.get(number)
        state = self.status(group_id).terminals[number - 1].state
        if current == session_id and state is TerminalState.RUNNING:
            return number
        windows = self._tmux("list-windows", "-t", session, "-F", "#{window_name}").splitlines()
        target = self._target(session, number)
        visible = f"terminal-{number}"
        if visible in windows:
            archive = f"history-{current or uuid.uuid4().hex}"
            if archive in windows:
                archive += f"-{uuid.uuid4().hex[:8]}"
            self._tmux("rename-window", "-t", target, archive)
        known = next(
            (item for item in record.sessions.get(number, []) if item.session_id == session_id),
            None,
        )
        archived = f"history-{session_id}"
        if archived in windows:
            self._tmux("rename-window", "-t", f"{session}:{archived}", visible)
            record.active_sessions[number] = session_id
            self.state.save(record)
            # An archived conversation may have exited while another role ran.
            if self.status(group_id).terminals[number - 1].state is not TerminalState.RUNNING:
                self._start_terminal(record, number, session_id)
        else:
            if known is None:
                record.sessions.setdefault(number, []).append(
                    AgentSession(session_id, number, agent_id=agent_id)
                )
                self.state.save(record)
            self._start_terminal(record, number, session_id)
        return number

    def queue_prompt(
        self,
        group_id: str,
        terminal: int,
        text: str,
        *,
        wait_ready: bool = False,
    ) -> None:
        """Bracketed paste avoids newlines being interpreted as premature submits."""
        session = self._require_running_terminal(group_id, terminal)
        record = self.state.get(group_id)
        workflow = self.workflow if record and record.flow_id else None
        agent_id = record.terminal_agents[terminal - 1].agent_id if record else ""

        def check_mode() -> None:
            if workflow and workflow.agent_mode(group_id, agent_id) == "manual":
                raise AgentManualMode("Agent is in manual mode; automatic prompts are paused")

        check_mode()
        if wait_ready:
            deadline = time.monotonic() + 45
            while True:
                check_mode()
                screen = self.capture(group_id, terminal, lines=0)
                plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", screen)
                if re.search(r"(?m)^\s*[>›❯](?:\s|$)", plain):
                    break
                if time.monotonic() >= deadline:
                    raise HypervisorError(
                        "Qwen prompt is not ready; inspect the terminal and retry delivery"
                    )
                time.sleep(0.15)
        name = f"hypervisor-{uuid.uuid4().hex}"
        guard = workflow._store(group_id).agent_gate(agent_id) if workflow else nullcontext()
        with guard:
            check_mode()
            try:
                loaded = self._runner(
                    ["tmux", "load-buffer", "-b", name, "-"],
                    input=text,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if loaded.returncode:
                    raise HypervisorError(loaded.stderr.strip() or "Cannot load prompt buffer")
                self._tmux(
                    "paste-buffer",
                    "-p",
                    "-r",
                    "-d",
                    "-b",
                    name,
                    "-t",
                    self._target(session, terminal),
                )
                self.send_key(group_id, terminal, "ctrl+q")
            finally:
                self._tmux("delete-buffer", "-b", name, check=False)

    def send_literal(self, group_id: str, terminal: int, text: str) -> None:
        session = self.session_name(group_id)
        self._validate_terminal(terminal, group_id)
        self._tmux("send-keys", "-l", "-t", self._target(session, terminal), text)

    def resize_terminal(self, group_id: str, terminal: int, width: int, height: int) -> None:
        session = self.session_name(group_id)
        self._validate_terminal(terminal, group_id)
        self._tmux(
            "resize-window",
            "-t",
            self._target(session, terminal),
            "-x",
            str(max(20, width)),
            "-y",
            str(max(5, height)),
        )

    def send_key(self, group_id: str, terminal: int, key: str) -> None:
        session = self.session_name(group_id)
        self._validate_terminal(terminal, group_id)
        normalized = normalize_key(key)
        target = self._target(session, terminal)
        if len(normalized) == 1:
            self._tmux("send-keys", "-l", "-t", target, normalized)
        else:
            self._tmux("send-keys", "-t", target, normalized)

    def capture(self, group_id: str, terminal: int, lines: int = 120) -> str:
        session = self.session_name(group_id)
        self._validate_terminal(terminal, group_id)
        target = self._target(session, terminal)
        history = () if lines == 0 else ("-S", f"-{max(1, min(lines, 2000))}")
        return self._tmux("capture-pane", "-p", "-e", "-J", *history, "-t", target).rstrip()

    def _qwen_command(
        self,
        session_id: str,
        *,
        terminal: int,
        resume: bool,
        agent: AgentSpec | None = None,
        agent_command: Sequence[str] = (),
        record: GroupRecord | None = None,
    ) -> str:
        agent = agent or self.project_config.agents[terminal - 1]
        session_flag = "--resume" if resume else "--session-id"
        arguments = [
            *(agent_command or self.agent_command),
            "--chat-recording",
            session_flag,
            session_id,
        ]
        if agent.model is not None:
            arguments.extend(["--model", agent.model])
        if agent.instructions:
            arguments.extend(["--append-system-prompt", agent.instructions])
        if not resume:
            name = terminal_session_name(terminal, session_id, agent.agent_id)
            arguments.extend(["--prompt-interactive", f"/rename {name}"])
        if record and record.flow_id:
            from .mcp_config import session_environment

            arguments = (
                session_environment(record, agent.agent_id, session_id, self.state.path) + arguments
            )
        return shlex.join(arguments)

    def _qwen_session_exists(
        self, cwd: str, session_id: str, agent_command: Sequence[str] = ()
    ) -> bool:
        return session_id in self._qwen_session_catalog(cwd, agent_command)

    def _qwen_session_catalog(
        self, cwd: str, agent_command: Sequence[str] = ()
    ) -> dict[str, dict[str, object]]:
        try:
            result = self._agent_runner(
                [
                    *(agent_command or self.agent_command),
                    "sessions",
                    "list",
                    "--json",
                    "--limit",
                    "1000",
                ],
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}
        if result.returncode != 0:
            return {}
        sessions: dict[str, dict[str, object]] = {}
        for line in result.stdout.splitlines():
            try:
                metadata = json.loads(line)
                session_id = metadata.get("sessionId")
                if isinstance(session_id, str):
                    sessions[session_id] = metadata
            except json.JSONDecodeError, AttributeError:
                continue
        return sessions

    @staticmethod
    def _stopped_command() -> str:
        return "printf '\\n[agent stopped by hypervisor]\\n'; exec sleep 2147483647"

    def _ensure_tmux_session(self, record: GroupRecord) -> str:
        session = self.session_name(record.group_id)
        if self._has_session(session):
            return session
        idle = self._stopped_command()
        self._tmux(
            "new-session",
            "-d",
            "-s",
            session,
            "-n",
            "terminal-1",
            "-c",
            record.worktree_path,
            idle,
        )
        for number in range(2, len(record.terminal_agents) + 1):
            self._tmux(
                "new-window",
                "-d",
                "-t",
                session,
                "-n",
                f"terminal-{number}",
                "-c",
                record.worktree_path,
                idle,
            )
        self._tmux("set-option", "-t", session, "remain-on-exit", "on")
        self._tmux("set-option", "-t", session, "@hypervisor-group-id", record.group_id)
        self._tmux("set-option", "-t", session, "@hypervisor-cwd", record.worktree_path)
        for number in range(1, len(record.terminal_agents) + 1):
            self._set_remain_on_exit(session, number)
            self._set_terminal_state(session, number, "stopped")
            active = record.active_sessions.get(number)
            if active:
                self._set_terminal_session(session, number, active)
        return session

    def _require_group(self, group_id: str) -> str:
        session = self.session_name(group_id)
        if not self._has_session(session):
            raise HypervisorError(f"Group {group_id!r} does not exist")
        return session

    def _require_terminal(self, group_id: str, terminal: int) -> str:
        self._validate_terminal(terminal, group_id)
        return self._require_group(group_id)

    def _validate_terminal(self, terminal: int, group_id: str) -> None:
        count = self._terminal_counts.get(group_id)
        if count is None:
            record = self.state.get(group_id)
            count = len(record.terminal_agents if record else self.project_config.agents)
            self._terminal_counts[group_id] = count
        if terminal not in range(1, count + 1):
            raise HypervisorError(f"Terminal number must be between 1 and {count}")

    def _require_running_terminal(self, group_id: str, terminal: int) -> str:
        session = self._require_terminal(group_id, terminal)
        state = self.status(group_id).terminals[terminal - 1].state
        if state is not TerminalState.RUNNING:
            raise HypervisorError(f"Terminal {terminal} in group {group_id!r} is {state.value}")
        return session

    def _session_option(self, session: str, option: str) -> str:
        return self._tmux("show-options", "-qv", "-t", session, option, check=False).strip()

    def _set_terminal_state(self, session: str, terminal: int, state: str) -> None:
        self._tmux(
            "set-option", "-w", "-t", self._target(session, terminal), "@hypervisor-state", state
        )

    def _set_remain_on_exit(self, session: str, terminal: int) -> None:
        self._tmux(
            "set-option",
            "-w",
            "-t",
            self._target(session, terminal),
            "remain-on-exit",
            "on",
        )

    def _set_terminal_session(self, session: str, terminal: int, session_id: str) -> None:
        self._tmux(
            "set-option",
            "-w",
            "-t",
            self._target(session, terminal),
            "@hypervisor-session-id",
            session_id,
        )

    @staticmethod
    def _target(session: str, terminal: int) -> str:
        return f"{session}:terminal-{terminal}"


def normalize_key(key: str) -> str:
    aliases = {
        "backslash": "\\",
        "left_square_bracket": "[",
        "right_square_bracket": "]",
        "circumflex_accent": "^",
        "underscore": "_",
        "at": "@",
        "plus": "+",
        "exclamation_mark": "!",
        "number_sign": "#",
        "dollar_sign": "$",
        "percent_sign": "%",
        "ampersand": "&",
        "asterisk": "*",
        "left_parenthesis": "(",
        "right_parenthesis": ")",
        "enter": "Enter",
        "return": "Enter",
        "escape": "Escape",
        "esc": "Escape",
        "tab": "Tab",
        "backspace": "BSpace",
        "space": "Space",
        "up": "Up",
        "down": "Down",
        "shift+up": "S-Up",
        "shift+down": "S-Down",
        "left": "Left",
        "right": "Right",
        "page-up": "PPage",
        "pageup": "PPage",
        "page-down": "NPage",
        "pagedown": "NPage",
        "delete": "DC",
        "insert": "IC",
        "home": "Home",
        "end": "End",
    }
    lowered = key.lower()
    if lowered in aliases:
        return aliases[lowered]
    modified = re.fullmatch(r"((?:(?:ctrl|alt|shift)[-+])+)(.+)", lowered)
    if modified:
        modifiers = re.findall(r"ctrl|alt|shift", modified.group(1))
        base = modified.group(2)
        if base in aliases:
            base = aliases[base]
        elif re.fullmatch(r"f(?:[1-9]|[12][0-9]|3[0-5])", base):
            base = base.upper()
        elif len(base) != 1:
            raise HypervisorError(f"Unsupported key: {key!r}")
        prefix = "".join({"ctrl": "C-", "alt": "M-", "shift": "S-"}[m] for m in modifiers)
        return prefix + base
    function = re.fullmatch(r"f(?:[1-9]|1[0-2])", lowered)
    if function:
        return lowered.upper()
    if len(key) == 1:
        return key
    raise HypervisorError(f"Unsupported key: {key!r}")
