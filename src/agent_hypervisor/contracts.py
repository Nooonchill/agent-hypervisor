from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .models import GroupStatus
from .state import AgentSession, StateStore

if TYPE_CHECKING:
    from .project_config import ProjectConfig
    from .workflow import WorkflowService


class HypervisorBackend(Protocol):
    project_config: ProjectConfig

    @property
    def workflow(self) -> WorkflowService: ...

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
    ) -> GroupStatus: ...

    def list_groups(self) -> list[GroupStatus]: ...

    def rename_group(self, group_id: str, new_group_id: str) -> GroupStatus: ...

    def activate_group(self, group_id: str) -> GroupStatus: ...

    def deactivate_group(self, group_id: str) -> GroupStatus: ...

    def remove_group(self, group_id: str, remove_worktree: bool = False) -> None: ...

    def list_terminal_sessions(self, group_id: str, terminal: int) -> list[AgentSession]: ...

    def start_terminal(
        self, group_id: str, terminal: int, session_id: str | None = None
    ) -> None: ...

    def stop_terminal(self, group_id: str, terminal: int) -> None: ...

    def restart_terminal(self, group_id: str, terminal: int) -> None: ...

    def send_literal(self, group_id: str, terminal: int, text: str) -> None: ...

    def send_key(self, group_id: str, terminal: int, key: str) -> None: ...

    def capture(self, group_id: str, terminal: int, lines: int = 120) -> str: ...

    def resize_terminal(
        self,
        group_id: str,
        terminal: int,
        width: int,
        height: int,
    ) -> None: ...


class WorkflowBackend(Protocol):
    state: StateStore
    project_config: ProjectConfig

    def workflow_session(self, group_id: str, agent_id: str, session_id: str) -> int: ...

    def queue_prompt(
        self, group_id: str, terminal: int, text: str, *, wait_ready: bool = False
    ) -> None: ...


class WorkflowOperations(Protocol):
    def validate_agent_session(self, group: str, agent: str, session: str) -> None: ...
    def events(self, group: str) -> list[dict]: ...
    def report(self, group: str, path: str) -> str: ...
    def complete(
        self, group: str, step_id: str, attempt: str, summary: str, session_id: str = ""
    ) -> dict: ...
    def submit_review(
        self,
        group: str,
        step_id: str,
        attempt: str,
        decision: str,
        summary: str,
        findings: list[str] | None = None,
        session_id: str = "",
    ) -> dict: ...
