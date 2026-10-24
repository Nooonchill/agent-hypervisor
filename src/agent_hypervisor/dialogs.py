from __future__ import annotations

from typing import ClassVar

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

from .config import TERMINAL_LABEL, TEXT
from .project_config import BRANCH_TYPES
from .state import AgentSession
from .widgets import HypervisorButton


class HypervisorDialog[DialogResult](ModalScreen[DialogResult]):
    """Common keyboard navigation for all application dialogs."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "dismiss_dialog", show=False)]

    def on_mount(self) -> None:
        self.query("Input, Button").first().focus()

    def action_dismiss_dialog(self) -> None:
        self.dismiss(None)

    def on_key(self, event: events.Key) -> None:
        if event.key not in {"up", "down", "left", "right"}:
            return
        if isinstance(self.focused, Input) and event.key in {"left", "right"}:
            return
        event.stop()
        event.prevent_default()
        if event.key in {"up", "left"}:
            self.focus_previous()
        else:
            self.focus_next()


class CreateGroupScreen(HypervisorDialog[str | None]):
    def compose(self) -> ComposeResult:
        with Vertical(id="create-dialog"):
            yield Label(TEXT["create_title"], id="create-title")
            yield Label(TEXT["create_help"], id="create-help")
            yield Input(placeholder=TEXT["group_id_placeholder"], id="group-id")
            with Horizontal(id="create-actions"):
                yield HypervisorButton(TEXT["cancel"], id="cancel")
                yield HypervisorButton(TEXT["continue"], id="confirm")

    @on(Input.Submitted)
    def submit_input(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if value:
            self.dismiss(value)

    @on(Button.Pressed, "#confirm")
    def confirm(self) -> None:
        value = self.query_one(Input).value.strip()
        if value:
            self.dismiss(value)

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)


class RenameGroupScreen(CreateGroupScreen):
    def __init__(self, group_id: str) -> None:
        super().__init__()
        self.group_id = group_id

    def compose(self) -> ComposeResult:
        with Vertical(id="create-dialog"):
            yield Label(TEXT["rename_title"].format(group_id=self.group_id), id="create-title")
            yield Label(TEXT["rename_help"], id="create-help")
            yield Input(value=self.group_id, id="group-id")
            with Horizontal(id="create-actions"):
                yield HypervisorButton(TEXT["cancel"], id="cancel")
                yield HypervisorButton(TEXT["rename"], id="confirm")


class BranchTypeScreen(HypervisorDialog[str | None]):
    def __init__(self, task_code: str) -> None:
        super().__init__()
        self.task_code = task_code

    def compose(self) -> ComposeResult:
        with Vertical(id="create-dialog"):
            yield Label(TEXT["branch_title"].format(task_code=self.task_code), id="create-title")
            yield Label(TEXT["branch_help"], id="create-help")
            for offset in range(0, len(BRANCH_TYPES), 3):
                with Horizontal(classes="branch-presets"):
                    for branch_type in BRANCH_TYPES[offset : offset + 3]:
                        yield HypervisorButton(branch_type, id=f"branch-{branch_type}")
            yield Input(placeholder=TEXT["branch_custom"], id="branch-type")
            with Horizontal(id="create-actions"):
                yield HypervisorButton(TEXT["cancel"], id="cancel")
                yield HypervisorButton(TEXT["create"], id="confirm")

    @on(Button.Pressed)
    def choose_type(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("branch-"):
            self.dismiss(button_id.removeprefix("branch-"))
        elif button_id == "cancel":
            self.dismiss(None)
        elif button_id == "confirm":
            self.submit_type()

    @on(Input.Submitted)
    def submit_type(self) -> None:
        value = self.query_one(Input).value.strip()
        if value:
            self.dismiss(value)


class ConfirmRemoveScreen(HypervisorDialog[bool | None]):
    def __init__(self, group_id: str) -> None:
        super().__init__()
        self.group_id = group_id

    def compose(self) -> ComposeResult:
        with Vertical(id="create-dialog"):
            yield Label(TEXT["remove_title"].format(group_id=self.group_id), id="create-title")
            yield Label(TEXT["remove_help"], id="create-help")
            with Horizontal(id="create-actions"):
                yield HypervisorButton(TEXT["cancel"], id="cancel")
                yield HypervisorButton(TEXT["continue"], id="confirm")

    @on(Button.Pressed, "#confirm")
    def confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(False)


class WorktreeDispositionScreen(HypervisorDialog[bool | None]):
    def compose(self) -> ComposeResult:
        with Vertical(id="create-dialog"):
            yield Label(TEXT["worktree_title"], id="create-title")
            yield Label(TEXT["worktree_help"], id="create-help")
            with Horizontal(id="create-actions"):
                yield HypervisorButton(TEXT["cancel"], id="cancel")
                yield HypervisorButton(TEXT["keep_worktree"], id="keep")
                yield HypervisorButton(TEXT["remove_worktree"], id="remove")

    @on(Button.Pressed, "#keep")
    def choose_keep(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#remove")
    def choose_remove(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def choose_cancel(self) -> None:
        self.dismiss(None)


class SessionPickerScreen(HypervisorDialog[str | None]):
    def __init__(
        self, terminal: int, sessions: list[AgentSession], *, agent_name: str = ""
    ) -> None:
        super().__init__()
        self.terminal = terminal
        self.sessions = sessions
        self.agent_name = agent_name

    def compose(self) -> ComposeResult:
        with Vertical(id="session-dialog"):
            yield Label(
                TEXT["session_title"].format(
                    terminal_label=self.agent_name or TERMINAL_LABEL.format(number=self.terminal)
                ),
                id="session-title",
            )
            yield Label(TEXT["session_help"], id="session-help")
            yield HypervisorButton(TEXT["new_session"], id="new-session")
            for index, session in enumerate(self.sessions):
                created = session.created_at.replace("T", " ").replace("+00:00", " UTC")
                yield HypervisorButton(
                    TEXT["session_timestamp"].format(name=session.name, created=created),
                    id=f"resume-{index}",
                )
            yield HypervisorButton(TEXT["cancel"], id="cancel")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        if event.button.id == "new-session":
            self.dismiss("")
        elif event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id and event.button.id.startswith("resume-"):
            index = int(event.button.id.removeprefix("resume-"))
            self.dismiss(self.sessions[index].session_id)
