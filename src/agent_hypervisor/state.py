from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import Any

from .config import AGENT_SESSION_LABEL, SESSION_LABEL
from .models import HypervisorError
from .project_config import AgentSpec, CheckSpec, FlowStep, ProjectConfig


def default_state_path() -> Path:
    configured = os.environ.get("HYPERVISOR_STATE_PATH")
    if configured:
        return Path(configured).expanduser()
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "agent-hypervisor" / "groups.json"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def terminal_session_name(terminal: int, session_id: str, agent_id: str = "") -> str:
    short_id = session_id.replace("-", "")[:8]
    if agent_id:
        return AGENT_SESSION_LABEL.format(agent_id=agent_id, session_id=short_id)
    return SESSION_LABEL.format(terminal=terminal, session_id=short_id)


@dataclass(slots=True)
class AgentSession:
    session_id: str
    terminal: int
    created_at: str = field(default_factory=utc_now)
    last_started_at: str = field(default_factory=utc_now)
    started: bool = False
    agent_id: str = ""

    @property
    def name(self) -> str:
        return terminal_session_name(self.terminal, self.session_id, self.agent_id)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AgentSession:
        return cls(
            session_id=str(value["session_id"]),
            terminal=int(value["terminal"]),
            created_at=str(value.get("created_at") or utc_now()),
            last_started_at=str(value.get("last_started_at") or utc_now()),
            started=bool(value.get("started", True)),
            agent_id=str(value.get("agent_id", "")),
        )


@dataclass(slots=True)
class GroupRecord:
    group_id: str
    repository_root: str
    worktree_path: str
    branch: str
    sessions: dict[int, list[AgentSession]] = field(default_factory=dict)
    active_sessions: dict[int, str] = field(default_factory=dict)
    agents: tuple[AgentSpec, ...] = ()
    flow: tuple[FlowStep, ...] = ()
    task_code: str = ""
    task_type: str = "feature"
    flow_id: str = ""
    data_dir: str = ".hypervisor"
    agent_command: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    @property
    def terminal_agents(self) -> tuple[AgentSpec, ...]:
        if self.agents:
            return self.agents
        return (
            ProjectConfig.default(max(self.sessions)) if self.sessions else ProjectConfig.default()
        ).agents

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> GroupRecord:
        sessions = {
            int(number): [AgentSession.from_dict(item) for item in items]
            for number, items in value.get("sessions", {}).items()
        }
        return cls(
            group_id=str(value["group_id"]),
            repository_root=str(value["repository_root"]),
            worktree_path=str(value["worktree_path"]),
            branch=str(value["branch"]),
            sessions=sessions,
            active_sessions={
                int(number): str(session_id)
                for number, session_id in value.get("active_sessions", {}).items()
            },
            agents=tuple(
                AgentSpec(
                    **{
                        key: val
                        for key, val in item.items()
                        if key not in {"reviewer_id", "create_pr"}
                    }
                )
                for item in value.get("agents", [])
            ),
            flow=tuple(flow_step_from_dict(item) for item in value.get("flow", [])),
            task_code=str(value.get("task_code", value["group_id"])),
            task_type=str(value.get("task_type", "feature")),
            flow_id=str(value.get("flow_id", "")),
            data_dir=str(value.get("data_dir", ".hypervisor")),
            agent_command=tuple(value.get("agent_command", ())),
            aliases=tuple(value.get("aliases", ())),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["sessions"] = {str(number): items for number, items in value["sessions"].items()}
        value["active_sessions"] = {
            str(number): session_id for number, session_id in value["active_sessions"].items()
        }
        return value


def flow_step_from_dict(item: dict[str, Any]) -> FlowStep:
    return FlowStep(
        **{
            **item,
            "review_scope": tuple(item.get("review_scope", ())),
            "checks": tuple(
                CheckSpec(check["check_id"], tuple(check["command"]), check["timeout"])
                for check in item.get("checks", ())
            ),
        }
    )


class StateStore:
    """Small atomic JSON registry shared by the TUI and CLI."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_state_path()

    def list_groups(self) -> list[GroupRecord]:
        return sorted(self._read().values(), key=lambda group: group.group_id.lower())

    def get(self, group_id: str) -> GroupRecord | None:
        groups = self._read()
        return groups.get(group_id) or next(
            (record for record in groups.values() if group_id in record.aliases), None
        )

    def save(self, group: GroupRecord) -> None:
        with self._lock():
            groups = self._read()
            groups[group.group_id] = group
            self._write(groups)

    def delete(self, group_id: str) -> None:
        with self._lock():
            groups = self._read()
            groups.pop(group_id, None)
            self._write(groups)

    def rename(self, old_id: str, new_id: str) -> GroupRecord:
        with self._lock():
            groups = self._read()
            group = groups.pop(old_id, None)
            if group is None:
                raise HypervisorError(f"Group {old_id!r} does not exist")
            if new_id in groups or any(new_id in item.aliases for item in groups.values()):
                raise HypervisorError(f"Group {new_id!r} already exists")
            group.aliases = (*group.aliases, old_id)
            group.group_id = new_id
            groups[new_id] = group
            self._write(groups)
            return group

    @contextmanager
    def _lock(self) -> Iterator[None]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_suffix(self.path.suffix + ".lock")
            lock_file = lock_path.open("a", encoding="utf-8")
        except OSError as error:
            raise HypervisorError(f"Cannot lock Hypervisor state: {error}") from error
        with lock_file:
            flock(lock_file.fileno(), LOCK_EX)
            try:
                yield
            finally:
                flock(lock_file.fileno(), LOCK_UN)

    def _read(self) -> dict[str, GroupRecord]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return {
                group_id: GroupRecord.from_dict(value)
                for group_id, value in payload.get("groups", {}).items()
            }
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise HypervisorError(f"Cannot read Hypervisor state: {error}") from error

    def _write(self, groups: dict[str, GroupRecord]) -> None:
        payload = {
            "version": 1,
            "groups": {group_id: group.to_dict() for group_id, group in groups.items()},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError as error:
            raise HypervisorError(f"Cannot save Hypervisor state: {error}") from error
