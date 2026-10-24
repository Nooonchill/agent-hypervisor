from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .models import GroupStatus
from .state import AgentSession


class HypervisorBackend(Protocol):
    """Control-plane contract shared by TUI, CLI adapters and future integrations."""

    def create_group(
        self, group_id: str, cwd: str | Path | None = None, *, task_type: str = "feature"
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
