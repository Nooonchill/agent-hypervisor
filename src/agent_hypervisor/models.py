from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TerminalState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    EXITED = "exited"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class TerminalStatus:
    number: int
    state: TerminalState
    process: str = ""
    exit_code: int | None = None
    agent_id: str = ""
    agent_name: str = ""


@dataclass(frozen=True, slots=True)
class GroupStatus:
    group_id: str
    session_name: str
    attached_clients: int
    terminals: tuple[TerminalStatus, ...]
    worktree_path: str = ""
    branch: str = ""
    active: bool = True

    @property
    def state(self) -> str:
        if not self.active:
            return "inactive"
        states = {terminal.state for terminal in self.terminals}
        if states == {TerminalState.RUNNING}:
            return "running"
        if TerminalState.RUNNING in states:
            return "partial"
        if states == {TerminalState.STOPPED}:
            return "stopped"
        return "inactive"


class HypervisorError(RuntimeError):
    """A user-facing error from tmux or invalid hypervisor state."""


class AgentManualMode(HypervisorError):
    """Automatic prompt delivery was deliberately paused by the user."""


class WorkflowBusy(HypervisorError):
    """Another process currently owns the task's workflow transition."""
