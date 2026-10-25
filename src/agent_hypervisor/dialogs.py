from __future__ import annotations

from typing import ClassVar

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RichLog, Static, TextArea

from .config import TERMINAL_LABEL, TEXT
from .project_config import BRANCH_TYPES
from .state import AgentSession
from .models import HypervisorError
from .task_form import DEFAULT_TASK_FORM, TaskFieldSpec, resolve_task_context
from .widgets import HypervisorButton


class HypervisorDialog[DialogResult](ModalScreen[DialogResult]):
    """Common keyboard navigation for all application dialogs."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "dismiss_dialog", show=False)]

    def on_mount(self) -> None:
        self.query("Input, TextArea, Button").first().focus()

    def action_dismiss_dialog(self) -> None:
        self.dismiss(None)

    def on_key(self, event: events.Key) -> None:
        if event.key not in {"up", "down", "left", "right"}:
            return
        if isinstance(self.focused, TextArea):
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


class FlowPickerScreen(HypervisorDialog[str | None]):
    def __init__(self, flows: tuple[object, ...]) -> None:
        super().__init__()
        self.flows = flows

    def compose(self) -> ComposeResult:
        with Vertical(id="flow-dialog"):
            yield Label(TEXT["flow_title"], id="create-title")
            yield Label(TEXT["flow_help"], id="create-help")
            for flow in self.flows:
                yield HypervisorButton(
                    f"{flow.name} · {flow.flow_id}",
                    id=f"flow-{flow.flow_id}",
                )
            with Horizontal(id="create-actions"):
                yield HypervisorButton(TEXT["cancel"], id="cancel")

    @on(Button.Pressed)
    def choose_flow(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "cancel":
            self.dismiss(None)
        elif button_id.startswith("flow-"):
            self.dismiss(button_id.removeprefix("flow-"))


class GroupContextScreen(HypervisorDialog[dict[str, object] | None]):
    def __init__(self, fields: tuple[TaskFieldSpec, ...] = DEFAULT_TASK_FORM) -> None:
        super().__init__()
        self.fields = fields

    def compose(self) -> ComposeResult:
        with Vertical(id="context-dialog"):
            yield Label(TEXT["context_title"], id="create-title")
            yield Label(TEXT["context_help"], id="create-help")
            for field in self.fields:
                if not field.visible:
                    continue
                yield Label(
                    TEXT["context_required_label"].format(label=field.label)
                    if field.required
                    else field.label
                )
                if field.field_id == "sources":
                    yield Input(value=", ".join(field.default), id=f"context-{field.field_id}")
                else:
                    yield TextArea(str(field.default), id=f"context-{field.field_id}")
            with Horizontal(id="create-actions"):
                yield HypervisorButton(TEXT["cancel"], id="cancel")
                yield HypervisorButton(TEXT["create"], id="confirm")

    def _value(self) -> dict[str, object]:
        values = {}
        for field in self.fields:
            if not field.visible:
                continue
            if field.field_id == "sources":
                values[field.field_id] = tuple(
                    self.query_one(f"#context-{field.field_id}", Input).value.split(",")
                )
            else:
                values[field.field_id] = self.query_one(f"#context-{field.field_id}", TextArea).text
        return resolve_task_context(self.fields, values)

    @on(Button.Pressed, "#confirm")
    def confirm_context(self) -> None:
        try:
            self.dismiss(self._value())
        except HypervisorError as error:
            self.app.notify(str(error), severity="warning")

    @on(Button.Pressed, "#cancel")
    def cancel_context(self) -> None:
        self.dismiss(None)


class HumanDecisionScreen(HypervisorDialog[tuple[bool, str] | None]):
    class ReportRequested(Message):
        def __init__(self, path: str) -> None:
            super().__init__()
            self.path = path

    def __init__(
        self, stage: str, details: str, *, allow_decision: bool, reports: tuple[str, ...] = ()
    ) -> None:
        super().__init__()
        self.stage = stage
        self.details = details
        self.allow_decision = allow_decision
        self.reports = reports

    def compose(self) -> ComposeResult:
        with Vertical(id="human-dialog"):
            title = TEXT["workflow_human_title" if self.allow_decision else "workflow_detail_title"]
            yield Label(title.format(stage=self.stage), id="create-title")
            yield Static(self.details, id="human-details", markup=False)
            for index, path in enumerate(self.reports):
                yield HypervisorButton(
                    path, id=f"workflow-report-{index}", classes="workflow-report"
                )
            if self.allow_decision:
                yield TextArea(placeholder=TEXT["workflow_comment"], id="human-comment")
            with Horizontal(id="create-actions"):
                yield HypervisorButton(TEXT["cancel"], id="cancel")
                if self.allow_decision:
                    yield HypervisorButton(TEXT["workflow_changes"], id="changes")
                    yield HypervisorButton(TEXT["workflow_approve"], id="approve")

    @on(Button.Pressed, ".workflow-report")
    def open_report(self, event: Button.Pressed) -> None:
        index = int((event.button.id or "").removeprefix("workflow-report-"))
        self.post_message(self.ReportRequested(self.reports[index]))

    @on(Button.Pressed, "#approve")
    def approve(self) -> None:
        if not self.allow_decision:
            return
        self.dismiss((True, self.query_one("#human-comment", TextArea).text.strip()))

    @on(Button.Pressed, "#changes")
    def request_changes(self) -> None:
        if not self.allow_decision:
            return
        comment = self.query_one("#human-comment", TextArea).text.strip()
        if not comment:
            self.app.notify(TEXT["workflow_comment_required"], severity="warning")
            return
        self.dismiss((False, comment))

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)


class ReportScreen(HypervisorDialog[None]):
    def __init__(self, title: str, content: str) -> None:
        super().__init__()
        self.title_text = title
        self.content = content

    def compose(self) -> ComposeResult:
        with Vertical(id="report-dialog"):
            yield Label(self.title_text, id="create-title")
            yield RichLog(id="report-body", markup=False, wrap=True, auto_scroll=False)
            with Horizontal(id="create-actions"):
                yield HypervisorButton(TEXT["close"], id="close-report")

    def on_mount(self) -> None:
        body = self.query_one("#report-body", RichLog)
        body.write(self.content or " ")
        body.focus()

    def on_key(self, event: events.Key) -> None:
        if isinstance(self.focused, RichLog):
            return
        super().on_key(event)

    @on(Button.Pressed, "#close-report")
    def close_report(self) -> None:
        self.dismiss(None)


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
