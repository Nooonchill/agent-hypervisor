from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import (
    Button,
    Footer,
    Header,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
)

from .clipboard import copy_native
from .config import (
    ACTIONS,
    ACTIVE_GROUP_ICON,
    AGENT_MODE_ICONS,
    BACKGROUND_REFRESH_DIVISOR,
    CAPTURE_HISTORY_LINES,
    GROUP_MARKERS,
    INPUT_BATCH_SECONDS,
    OUTPUT_REFRESH_SECONDS,
    STATE_REFRESH_SECONDS,
    TERMINAL_ACTION_ICONS,
    TERMINAL_LABEL,
    TERMINAL_RENDER_LINES,
    TERMINAL_STATE_LABELS,
    TERMINAL_STATE_MARKERS,
    TEXT,
    WORKFLOW_STATE_LABELS,
)
from .contracts import HypervisorBackend
from .diagnostics import LOGGER_NAME
from .dialogs import (
    BranchTypeScreen,
    ConfirmRemoveScreen,
    CreateGroupScreen,
    FlowPickerScreen,
    GroupContextScreen,
    HumanDecisionScreen,
    ReportScreen,
    RenameGroupScreen,
    SessionPickerScreen,
    WorktreeDispositionScreen,
)
from .models import GroupStatus, HypervisorError, TerminalState
from .project_config import ProjectConfig
from .tmux import TmuxBackend
from .widgets import HypervisorButton, WorkflowRoadmap
from .task_form import DEFAULT_TASK_FORM

LOGGER = logging.getLogger(f"{LOGGER_NAME}.tui")


class GroupItem(ListItem):
    def __init__(self, status: GroupStatus) -> None:
        marker = GROUP_MARKERS[status.state]
        super().__init__(Label(f"{marker}  {status.group_id}"))
        self.group_id = status.group_id


class TerminalPanel(RichLog):
    can_focus = True

    class Selected(Message):
        def __init__(self, number: int) -> None:
            super().__init__()
            self.number = number

    class KeyPressed(Message):
        def __init__(self, number: int, key: str) -> None:
            super().__init__()
            self.number = number
            self.key = key

    class TextPasted(Message):
        def __init__(self, number: int, text: str) -> None:
            super().__init__()
            self.number = number
            self.text = text

    class ScrollRequested(Message):
        def __init__(self, number: int, direction: int) -> None:
            super().__init__()
            self.number = number
            self.direction = direction

    def __init__(self, number: int) -> None:
        super().__init__(
            id=f"terminal-{number}",
            classes="terminal-panel",
            max_lines=TERMINAL_RENDER_LINES,
            min_width=1,
            wrap=False,
            highlight=False,
            markup=False,
            auto_scroll=False,
        )
        self.number = number
        self.border_title = TERMINAL_LABEL.format(number=number)

    def update(self, content: object = "") -> None:
        if self.text_selection is not None:
            return
        self.clear()
        self.write(content, scroll_end=False, animate=False)

    def get_selection(self, selection: Selection) -> tuple[str, str]:
        text = "\n".join(line.text for line in self.lines)
        # Dragging below the last rendered line must not index past the content.
        if selection.start is not None and selection.start.y >= len(self.lines):
            return "", "\n"
        return selection.extract(text).rstrip(), "\n"

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        row = scroll_y + y
        selection = self.text_selection
        if selection is not None and row < len(self.lines):
            text = Text.assemble(*((segment.text, segment.style) for segment in self.lines[row]))
            span = selection.get_span(row)
            if span is not None:
                start, end = span
                text.stylize(
                    self.screen.get_component_rich_style("screen--selection"),
                    start,
                    len(text) if end == -1 else end,
                )
            line = Strip(text.render(self.app.console))
            line = line.crop_extend(
                scroll_x, scroll_x + self.scrollable_content_region.width, self.rich_style
            )
        else:
            line = super().render_line(y)
        return line.apply_offsets(scroll_x, row)

    def on_click(self) -> None:
        if self.screen.get_selected_text():
            return
        self.focus()
        self.post_message(self.Selected(self.number))

    def on_key(self, event: events.Key) -> None:
        key = event.character if event.character is not None else event.key
        event.stop()
        self.post_message(self.KeyPressed(self.number, key))

    def on_paste(self, event: events.Paste) -> None:
        event.stop()
        self.post_message(self.TextPasted(self.number, event.text))

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.prevent_default()
        event.stop()
        self.post_message(self.ScrollRequested(self.number, -1))

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.prevent_default()
        event.stop()
        self.post_message(self.ScrollRequested(self.number, 1))


# Dialog screens are implemented in dialogs.py and re-exported here for compatibility.


class TerminalHeading(Label):
    def __init__(self, number: int) -> None:
        super().__init__(
            TERMINAL_LABEL.format(number=number),
            id=f"terminal-title-{number}",
            classes="terminal-title",
            markup=False,
        )
        self.number = number

    def on_click(self) -> None:
        if not self.screen.get_selected_text():
            self.post_message(TerminalPanel.Selected(self.number))


class HypervisorApp(App[None]):
    TITLE = TEXT["app_title"]
    SUB_TITLE = TEXT["app_subtitle"]

    CSS_PATH = Path(__file__).with_name("styles.tcss")

    BINDINGS: ClassVar[list[Binding]] = [
        Binding(spec.keys, spec.action, spec.title, show=spec.show, priority=spec.priority)
        for spec in ACTIONS.values()
        if spec.keys
    ]

    def __init__(self, backend: HypervisorBackend | None = None) -> None:
        super().__init__()
        self.backend = backend or TmuxBackend()
        project = getattr(self.backend, "project_config", ProjectConfig.default())
        self._panel_count = len(project.agents)
        self.group_ids: list[str] = []
        self._group_sidebar_state: list[tuple[str, str]] = []
        self.selected_group: str | None = None
        self.selected_terminal = 1
        self.selected_terminal_state = TerminalState.MISSING
        self.fullscreen_terminal = False
        self._refreshing = False
        self._current_group: GroupStatus | None = None
        self._resizing = False
        self._terminal_sizes: dict[tuple[str, int], tuple[int, int]] = {}
        self._snapshots: dict[tuple[str, int], str] = {}
        self._output_tick = 0
        self._pending_text: dict[tuple[str, int], str] = {}
        self._flush_timers: dict[tuple[str, int], object] = {}
        self._input_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tmux-input")
        self._clipboard_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clipboard")
        self._workflow_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="workflow-ui"
        )
        self._mode_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-mode")
        self._workflow_status: dict | None = None
        self._workflow_busy_groups: set[str] = set()
        self._main_screen: Screen | None = None
        self._unmounting = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="workspace"):
            with Vertical(id="sidebar"):
                yield Static(TEXT["brand"], id="brand")
                yield Label(TEXT["groups_label"], id="groups-label")
                yield ListView(id="groups")
                with Horizontal(id="group-actions"):
                    for name in (
                        "create_group",
                        "toggle_group_active",
                        "rename_group",
                        "remove_group",
                    ):
                        spec = ACTIONS[name]
                        button = HypervisorButton(
                            spec.icon,
                            id=spec.button_id,
                            classes="icon-button",
                        )
                        button.tooltip = spec.tooltip_for()
                        yield button
            with Vertical(id="main"):
                with Vertical(id="group-header"):
                    with Horizontal(id="group-title-row"):
                        yield Label(TEXT["no_group"], id="group-title")
                        yield HypervisorButton(TEXT["workflow_start"], id="workflow-start")
                        yield Label("", id="group-state")
                    with Horizontal(id="workflow-row"):
                        yield WorkflowRoadmap(id="workflow-roadmap")
                yield Static(TEXT["empty"], id="empty")
                with Container(id="terminal-grid"):
                    for number in range(1, self._panel_count + 1):
                        yield self._terminal_wrapper(number)
        yield Footer()

    async def on_mount(self) -> None:
        self._main_screen = self.screen
        await self.refresh_all()
        self.set_interval(STATE_REFRESH_SECONDS, self.refresh_all)
        self.set_interval(OUTPUT_REFRESH_SECONDS, self.refresh_terminal_output)

    def on_resize(self) -> None:
        if self._main_screen is not None and not self._unmounting:
            self.call_after_refresh(self._resize_layout)

    def _resize_layout(self) -> None:
        if not self._refreshing and not self._unmounting:
            self._apply_fullscreen()

    def on_unmount(self) -> None:
        self._unmounting = True
        self._current_group = None
        self._flush_all_input()
        self._input_executor.shutdown(wait=False, cancel_futures=False)
        self._clipboard_executor.shutdown(wait=False, cancel_futures=True)
        self._workflow_executor.shutdown(wait=False, cancel_futures=True)
        self._mode_executor.shutdown(wait=False, cancel_futures=False)

    @staticmethod
    def _terminal_wrapper(number: int) -> Vertical:
        start_spec = ACTIONS["restart_terminal"]
        stop_spec = ACTIONS["stop_terminal"]
        start = HypervisorButton(
            start_spec.icon, id=f"start-button-{number}", classes="icon-button"
        )
        start.tooltip = start_spec.tooltip_for()
        stop = HypervisorButton(stop_spec.icon, id=f"stop-button-{number}", classes="icon-button")
        stop.tooltip = stop_spec.tooltip_for()
        mode_spec = ACTIONS["toggle_agent_mode"]
        mode = HypervisorButton(mode_spec.icon, id=f"mode-button-{number}", classes="icon-button")
        mode.disabled = True
        mode.tooltip = mode_spec.tooltip_for()
        return Vertical(
            Horizontal(TerminalHeading(number), start, stop, mode, classes="terminal-controls"),
            TerminalPanel(number),
            id=f"terminal-wrap-{number}",
            classes="terminal-wrapper",
        )

    def _main_query_one(self, selector: str, expect_type=None):
        screen = self._main_screen or self.screen
        return screen.query_one(selector, expect_type)

    @on(ListView.Selected, "#groups")
    async def select_group(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, GroupItem):
            self.screen.clear_selection()
            self.selected_group = item.group_id
            await self.refresh_all(rebuild_groups=False)

    @on(Button.Pressed, "#create-group")
    def create_button(self) -> None:
        self.action_create_group()

    @on(Button.Pressed, "#activate-group")
    def activate_button(self) -> None:
        self.action_toggle_group_active()

    @on(Button.Pressed, "#rename-group")
    def rename_button(self) -> None:
        self.action_rename_group()

    @on(Button.Pressed, "#remove-group")
    def remove_button(self) -> None:
        self.action_remove_group()

    @on(TerminalPanel.Selected)
    def select_terminal(self, event: TerminalPanel.Selected) -> None:
        self.selected_terminal = event.number
        self._update_selection()
        self.call_later(self.refresh_all)

    @on(events.TextSelected)
    def copy_selected_text(self) -> None:
        self.action_copy_selection()

    def action_copy_selection(self) -> None:
        text = (self.screen.get_selected_text() or "").rstrip()
        if text:
            self.copy_to_clipboard(text)

    def copy_to_clipboard(self, text: str) -> None:
        super().copy_to_clipboard(text)
        self._clipboard_executor.submit(copy_native, text)

    @on(TerminalPanel.KeyPressed)
    def forward_terminal_key(self, event: TerminalPanel.KeyPressed) -> None:
        self.selected_terminal = event.number
        if self.selected_group is None or self.selected_terminal_state is not TerminalState.RUNNING:
            self.notify(TEXT["terminal_is_stopped"], severity="warning")
            return
        target = (self.selected_group, event.number)
        if len(event.key) == 1 and event.key.isprintable():
            self._pending_text[target] = self._pending_text.get(target, "") + event.key
            if target not in self._flush_timers:
                self._flush_timers[target] = self.set_timer(
                    INPUT_BATCH_SECONDS, lambda target=target: self._flush_input(target)
                )
            return
        self._flush_input(target)
        self._submit_input(self.backend.send_key, *target, event.key)

    @on(TerminalPanel.TextPasted)
    def paste_into_terminal(self, event: TerminalPanel.TextPasted) -> None:
        self.selected_terminal = event.number
        if self.selected_group is None or self.selected_terminal_state is not TerminalState.RUNNING:
            self.notify(TEXT["terminal_is_stopped"], severity="warning")
            return
        target = (self.selected_group, event.number)
        self._flush_input(target)
        self._submit_input(self.backend.send_literal, *target, event.text)

    @on(TerminalPanel.ScrollRequested)
    def scroll_terminal(self, event: TerminalPanel.ScrollRequested) -> None:
        if self.selected_group is None:
            return
        self.selected_terminal = event.number
        self._update_selection()
        if self.selected_terminal_state is TerminalState.RUNNING:
            key = "shift+up" if event.direction < 0 else "shift+down"
            self._submit_input(self.backend.send_key, self.selected_group, event.number, key)
            LOGGER.debug(
                "qwen_scroll group=%s terminal=%s key=%s",
                self.selected_group,
                event.number,
                key,
            )
            return
        LOGGER.debug(
            "scroll_event group=%s terminal=%s direction=%s state=%s",
            self.selected_group,
            event.number,
            event.direction,
            self.selected_terminal_state.value,
        )

    @on(Button.Pressed, ".terminal-controls .icon-button")
    def terminal_control_button(self, event: Button.Pressed) -> None:
        wrapper = event.button.parent.parent
        self.selected_terminal = int(wrapper.id.rsplit("-", 1)[1])
        self._update_selection()
        if event.button.id.startswith("mode-button"):
            self.action_toggle_agent_mode()
            return
        if event.button.id.startswith("stop-button"):
            self._terminal_action("stop")
            return
        action = "restart" if self.selected_terminal_state is TerminalState.RUNNING else "start"
        self._terminal_action(action)

    @on(Button.Pressed, "#workflow-start")
    def workflow_start_button(self) -> None:
        self._run_workflow_action()

    @on(WorkflowRoadmap.StageSelected)
    def roadmap_stage_selected(self, event: WorkflowRoadmap.StageSelected) -> None:
        stage = next(
            (
                stage
                for stage in (self._workflow_status or {}).get("stages", [])
                if str(stage.get("id")) == event.stage_id
            ),
            None,
        )
        if stage is not None:
            self._open_workflow_stage(stage)

    def _open_workflow_stage(self, stage: dict) -> None:
        stage_id = str(stage.get("id", "stage"))
        status = self._workflow_status or {}
        allow_decision = stage.get("state") == "awaiting-human"
        state = str(stage.get("state", "pending"))
        sections = [
            TEXT["workflow_detail_header"].format(
                flow=status.get("flow_id", ""),
                stage=stage_id,
                state=WORKFLOW_STATE_LABELS.get(state, state),
            )
        ]
        if stage.get("goal"):
            sections.append(TEXT["workflow_detail_goal"].format(goal=stage["goal"]))
        if stage.get("summary"):
            sections.append(TEXT["workflow_detail_result"].format(summary=stage["summary"]))
        if stage.get("checks"):
            lines = [TEXT["workflow_detail_checks"]]
            for check in stage["checks"]:
                lines.append(
                    TEXT["workflow_detail_check"].format(
                        marker="✓" if check.get("returncode") == 0 else "×",
                        name=check.get("id", "check"),
                        code=check.get("returncode", "?"),
                    )
                )
            sections.append("\n".join(lines))
        review = stage.get("review") or {}
        if review:
            sections.append(
                TEXT["workflow_detail_review"].format(
                    decision=review.get("decision", ""), summary=review.get("summary", "")
                )
            )
            sections.extend(f"• {finding}" for finding in review.get("findings", []))
        if stage.get("commit"):
            sections.append(TEXT["workflow_detail_commit"].format(commit=stage["commit"]))
        if stage.get("pr"):
            sections.append(TEXT["workflow_detail_pr"].format(status=stage["pr"]))
        if stage.get("error"):
            sections.append(str(stage["error"]))
        details = "\n\n".join(sections)
        reports = self._stage_report_paths(stage)
        self.push_screen(
            HumanDecisionScreen(stage_id, details, allow_decision=allow_decision, reports=reports),
            lambda result: self._submit_human_decision(stage, result) if allow_decision else None,
        )

    @staticmethod
    def _stage_report_paths(stage: dict) -> tuple[str, ...]:
        paths: list[str] = []

        def collect(value: object, key: str = "") -> None:
            if isinstance(value, dict):
                for child_key, child in value.items():
                    collect(child, child_key)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    collect(child, key)
            elif isinstance(value, str) and key in {"report", "reports"}:
                if value and value not in paths:
                    paths.append(value)

        collect(stage)
        return tuple(paths)

    @on(HumanDecisionScreen.ReportRequested)
    def workflow_report_requested(self, event: HumanDecisionScreen.ReportRequested) -> None:
        group_id = self.selected_group
        workflow = getattr(self.backend, "workflow", None)
        method = getattr(workflow, "report", None)
        if group_id is None or not callable(method):
            return
        future = self._workflow_executor.submit(method, group_id, event.path)
        future.add_done_callback(
            lambda result, path=event.path: self._workflow_report_finished(path, result)
        )

    def _workflow_report_finished(self, path: str, future: Future[object]) -> None:
        try:
            content = str(future.result())
            error = None
        except Exception as exc:
            content, error = "", exc
        self._dispatch_to_ui(self._open_workflow_report, path, content, error)

    def _open_workflow_report(self, path: str, content: str, error: Exception | None) -> None:
        if error is not None:
            self.notify(TEXT["workflow_error"].format(error=error), severity="error")
            return
        self.push_screen(ReportScreen(path, content))

    def _refresh_workflow(self, group_id: str) -> None:
        workflow = getattr(self.backend, "workflow", None)
        if workflow is None or not callable(getattr(workflow, "status", None)):
            self._main_query_one("#workflow-row").styles.display = "none"
            return
        try:
            status = workflow.status(group_id)
        except Exception as error:
            LOGGER.warning("workflow status unavailable for %s: %s", group_id, error)
            return
        self._apply_workflow_status(group_id, status, None)

    def _apply_workflow_status(
        self, group_id: str, status: object, error: Exception | None
    ) -> None:
        if self.selected_group != group_id or self._unmounting:
            return
        if error is not None or not isinstance(status, dict):
            LOGGER.warning("workflow status unavailable for %s: %s", group_id, error)
            return
        changed = status != self._workflow_status
        self._workflow_status = status
        self._update_agent_modes()
        state = str(status.get("state", "pending"))
        has_stages = bool(status.get("stages"))
        self._main_query_one("#workflow-row").styles.display = (
            "none" if state == "manual" or not has_stages else "block"
        )
        button = self._main_query_one("#workflow-start", Button)
        button.styles.display = "block" if has_stages else "none"
        busy = group_id in self._workflow_busy_groups
        button.disabled = busy or state in {"completed", "manual"}
        button.label = TEXT["workflow_start"] if state == "pending" else TEXT["workflow_resume"]
        button.tooltip = (
            TEXT["workflow_running"]
            if busy
            else TEXT["workflow_resume_help"]
            if state != "pending"
            else state
        )
        if changed:
            self.call_later(self._render_workflow_roadmap)

    def _render_workflow_roadmap(self) -> None:
        self._main_query_one("#workflow-roadmap", WorkflowRoadmap).update_stages(
            (self._workflow_status or {}).get("stages", [])
        )

    def _run_workflow_action(self) -> None:
        if self.selected_group is None or self.selected_group in self._workflow_busy_groups:
            return
        workflow = getattr(self.backend, "workflow", None)
        if workflow is None:
            return
        state = str((self._workflow_status or {}).get("state", "pending"))
        method = getattr(
            workflow,
            "start" if state == "pending" else "resume",
            None,
        )
        if not callable(method):
            return
        group_id = self.selected_group
        self._workflow_busy_groups.add(group_id)
        self._main_query_one("#workflow-start", Button).disabled = True
        future = self._workflow_executor.submit(method, group_id)
        future.add_done_callback(lambda result: self._workflow_action_finished(group_id, result))

    def _workflow_action_finished(self, group_id: str, future: Future[object]) -> None:
        try:
            future.result()
            error = None
        except Exception as exc:
            error = exc
        self._dispatch_to_ui(self._workflow_action_applied, group_id, error)

    def _workflow_action_applied(self, group_id: str, error: Exception | None) -> None:
        self._workflow_busy_groups.discard(group_id)
        if error is not None:
            self.notify(TEXT["workflow_error"].format(error=error), severity="error")
        if self.selected_group == group_id:
            self._refresh_workflow(group_id)

    def _submit_human_decision(self, stage: dict, result: tuple[bool, str] | None) -> None:
        if result is None or self.selected_group is None:
            return
        workflow = getattr(self.backend, "workflow", None)
        method = getattr(workflow, "human_decision", None)
        if not callable(method):
            return
        group_id = self.selected_group
        step_id = str(stage.get("id", ""))
        attempt = str(stage.get("attempt", ""))
        self._workflow_busy_groups.add(group_id)
        self._main_query_one("#workflow-start", Button).disabled = True
        future = self._workflow_executor.submit(
            method, group_id, step_id, attempt, approved=result[0], comment=result[1]
        )
        future.add_done_callback(lambda result: self._workflow_action_finished(group_id, result))

    async def refresh_all(self, rebuild_groups: bool = True) -> None:
        if (
            self._refreshing
            or self._unmounting
            or not self.is_running
            or (self._main_screen is not None and self.screen is not self._main_screen)
        ):
            return
        self._refreshing = True
        try:
            groups = self.backend.list_groups()
            new_ids = [group.group_id for group in groups]
            sidebar_state = [(group.group_id, group.state) for group in groups]
            if self.selected_group not in new_ids:
                self.selected_group = new_ids[0] if new_ids else None
            if rebuild_groups and sidebar_state != self._group_sidebar_state:
                await self._rebuild_group_list(groups)
            self.group_ids = new_ids
            self._group_sidebar_state = sidebar_state
            selected = next(
                (group for group in groups if group.group_id == self.selected_group), None
            )
            if selected is not None:
                await self._ensure_terminal_panels(len(selected.terminals))
            self._render_selected(selected)
        except HypervisorError as error:
            self.notify(str(error), severity="error", timeout=4)
        finally:
            self._refreshing = False

    def refresh_terminal_output(self) -> None:
        group = self._current_group
        if (
            self._unmounting
            or self._refreshing
            or not self.is_running
            or (self._main_screen is not None and self.screen is not self._main_screen)
            or group is None
            or not self.screen_stack
            or not self.screen.is_running
        ):
            return
        self._output_tick += 1
        for status in group.terminals:
            if status.state is not TerminalState.RUNNING:
                continue
            if (
                status.number != self.selected_terminal
                and self._output_tick % BACKGROUND_REFRESH_DIVISOR != 0
            ):
                continue
            if self._main_query_one(f"#terminal-wrap-{status.number}").styles.display == "none":
                continue
            try:
                snapshot = self.backend.capture(
                    group.group_id,
                    status.number,
                    CAPTURE_HISTORY_LINES,
                )
                self._update_snapshot(group.group_id, status.number, snapshot)
            except HypervisorError:
                # The slower state refresh will render a durable error or exited state.
                pass

    async def _rebuild_group_list(self, groups: list[GroupStatus]) -> None:
        group_list = self._main_query_one("#groups", ListView)
        await group_list.clear()
        if groups:
            await group_list.extend([GroupItem(group) for group in groups])
            target = next(
                (
                    index
                    for index, group in enumerate(groups)
                    if group.group_id == self.selected_group
                ),
                0,
            )
            group_list.index = target

    async def _ensure_terminal_panels(self, count: int) -> None:
        if self._panel_count == count:
            return
        self._main_screen.clear_selection()
        grid = self._main_query_one("#terminal-grid", Container)
        await grid.remove_children()
        await grid.mount(*(self._terminal_wrapper(number) for number in range(1, count + 1)))
        self._panel_count = count
        self.selected_terminal = min(self.selected_terminal, count)

    def _render_selected(self, group: GroupStatus | None) -> None:
        has_group = group is not None
        self._current_group = group
        self._main_query_one("#empty").styles.display = "none" if has_group else "block"
        # Textual's ``display`` property controls visibility only; the grid layout
        # itself is declared in CSS with ``layout: grid``.
        self._main_query_one("#terminal-grid").styles.display = "block" if has_group else "none"
        self._main_query_one("#workflow-row").styles.display = "block" if has_group else "none"
        if group is None:
            self.selected_terminal_state = TerminalState.MISSING
            self._workflow_status = None
            self._main_query_one("#workflow-row").styles.display = "none"
            self._main_query_one("#workflow-start").styles.display = "none"
            self._main_query_one("#group-title", Label).update(TEXT["no_group"])
            self._main_query_one("#group-state", Label).update("")
            self._main_query_one("#activate-group", Button).disabled = True
            self._main_query_one("#rename-group", Button).disabled = True
            self._main_query_one("#remove-group", Button).disabled = True
            return
        self._main_query_one("#rename-group", Button).disabled = False
        self._main_query_one("#remove-group", Button).disabled = False
        activate_button = self._main_query_one("#activate-group", Button)
        activate_button.disabled = False
        activate_button.label = (
            ACTIVE_GROUP_ICON if group.active else ACTIONS["toggle_group_active"].icon
        )
        activate_button.tooltip = ACTIONS["toggle_group_active"].tooltip_for(
            TEXT["deactivate_tooltip"] if group.active else TEXT["activate_tooltip"]
        )
        self._main_query_one("#group-title", Label).update(
            TEXT["group_title"].format(group_id=group.group_id)
        )
        self._main_query_one("#group-state", Label).update(
            TEXT["group_state"].format(state=group.state.upper())
        )
        running = [status for status in group.terminals if status.state is TerminalState.RUNNING]
        snapshots: dict[int, str | Exception] = {}
        for status in running:
            try:
                snapshots[status.number] = self.backend.capture(
                    group.group_id,
                    status.number,
                    CAPTURE_HISTORY_LINES,
                )
            except HypervisorError as error:
                snapshots[status.number] = error
        for status in group.terminals:
            panel = self._main_query_one(f"#terminal-{status.number}", TerminalPanel)
            wrapper = self._main_query_one(f"#terminal-wrap-{status.number}")
            panel.set_class(status.number == self.selected_terminal, "selected")
            panel.set_class(status.state is TerminalState.STOPPED, "stopped")
            wrapper.set_class(status.number == self.selected_terminal, "selected")
            wrapper.set_class(status.state is TerminalState.STOPPED, "stopped")
            if status.state is not TerminalState.RUNNING:
                self._snapshots.pop((group.group_id, status.number), None)
            panel.remove_class("scrollback")
            icon = TERMINAL_STATE_MARKERS[status.state.value]
            label = status.agent_name or TERMINAL_LABEL.format(number=status.number)
            state_label = TERMINAL_STATE_LABELS[status.state.value]
            panel.border_title = f"{icon} {label} · {state_label}"
            if not group.active:
                panel.border_title = (
                    f"{TERMINAL_STATE_MARKERS['stopped']} {label} · {TEXT['inactive_state']}"
                )
                panel.update(
                    Text(
                        TEXT["terminal_inactive"].format(activate_key=TEXT["activate_key"]),
                        style="dim",
                    )
                )
            elif status.state is TerminalState.STOPPED:
                panel.update(
                    Text(
                        TEXT["terminal_stopped"].format(start_key=TEXT["start_key"]), style="yellow"
                    )
                )
            elif status.state is TerminalState.EXITED:
                panel.update(
                    Text(
                        TEXT["terminal_exited"].format(
                            exit_code=status.exit_code,
                            start_key=TEXT["start_key"],
                        ),
                        style="red",
                    )
                )
            elif status.state is TerminalState.MISSING:
                panel.update(Text(TEXT["terminal_missing"], style="red"))
            else:
                snapshot = snapshots[status.number]
                if isinstance(snapshot, Exception):
                    panel.update(Text(str(snapshot), style="red"))
                else:
                    self._update_snapshot(group.group_id, status.number, snapshot, force=True)

            self._main_query_one(f"#terminal-title-{status.number}", Label).update(
                Text(str(panel.border_title))
            )

        selected_status = group.terminals[self.selected_terminal - 1]
        self.selected_terminal_state = selected_status.state
        for status in group.terminals:
            wrapper = self._main_query_one(f"#terminal-wrap-{status.number}")
            controls = list(wrapper.query(".terminal-controls Button"))
            wrapper.query_one(".terminal-controls").styles.display = "block"
            for control in controls:
                control.styles.display = (
                    "block" if status.number == self.selected_terminal else "none"
                )
            controls[0].disabled = not group.active
            controls[1].disabled = not group.active or status.state is not TerminalState.RUNNING
        self._update_terminal_controls(selected_status.state)
        self._refresh_workflow(group.group_id)
        self._apply_fullscreen()
        self.call_after_refresh(self._sync_terminal_sizes)

    def _update_snapshot(
        self,
        group_id: str,
        terminal: int,
        snapshot: str,
        *,
        force: bool = False,
    ) -> None:
        key = (group_id, terminal)
        if not force and self._snapshots.get(key) == snapshot:
            return
        self._snapshots[key] = snapshot
        self._main_query_one(f"#terminal-{terminal}", TerminalPanel).update(
            Text.from_ansi(snapshot or "Waiting for output…")
        )

    def _update_terminal_controls(self, state: TerminalState) -> None:
        start_button = self._main_query_one(f"#start-button-{self.selected_terminal}", Button)
        start_button.label = TERMINAL_ACTION_ICONS[state.value]
        start_button.tooltip = ACTIONS["restart_terminal"].tooltip_for(
            TEXT["restart_tooltip"] if state is TerminalState.RUNNING else TEXT["start_tooltip"]
        )
        self._main_query_one(f"#stop-button-{self.selected_terminal}", Button).disabled = (
            state is not TerminalState.RUNNING
        )

    def _update_agent_modes(self) -> None:
        if self._current_group is None:
            return
        status = self._workflow_status or {}
        modes = status.get("agent_modes", {})
        available = bool(status.get("flow_id")) and callable(
            getattr(getattr(self.backend, "workflow", None), "set_agent_mode", None)
        )
        for terminal in self._current_group.terminals:
            mode = modes.get(terminal.agent_id, "workflow")
            button = self._main_query_one(f"#mode-button-{terminal.number}", Button)
            button.disabled = not available
            button.label = AGENT_MODE_ICONS[mode]
            button.set_class(mode == "manual", "manual-mode")
            button.tooltip = (
                TEXT["agent_no_workflow"]
                if not available
                else TEXT["agent_manual_tooltip" if mode == "manual" else "agent_workflow_tooltip"]
            )
            self._main_query_one(f"#terminal-wrap-{terminal.number}").border_subtitle = (
                TEXT["agent_manual_badge"] if mode == "manual" else ""
            )

    def action_toggle_agent_mode(self) -> None:
        if self.selected_group is None or self._current_group is None:
            return
        workflow = getattr(self.backend, "workflow", None)
        setter = getattr(workflow, "set_agent_mode", None)
        if not callable(setter) or not (self._workflow_status or {}).get("flow_id"):
            return
        terminal = self._current_group.terminals[self.selected_terminal - 1]
        current = (
            (self._workflow_status or {}).get("agent_modes", {}).get(terminal.agent_id, "workflow")
        )
        group_id = self.selected_group
        future = self._mode_executor.submit(
            setter, group_id, terminal.agent_id, "workflow" if current == "manual" else "manual"
        )
        future.add_done_callback(lambda result: self._mode_finished(group_id, result))

    def _mode_finished(self, group_id: str, future: Future[object]) -> None:
        try:
            future.result()
            error = None
        except Exception as exc:
            error = exc
        self._dispatch_to_ui(self._mode_applied, group_id, error)

    def _mode_applied(self, group_id: str, error: Exception | None) -> None:
        if error is not None:
            self.notify(str(error), severity="error", timeout=4)
        if self.selected_group == group_id:
            self._refresh_workflow(group_id)

    def _clear_group_caches(self, group_id: str) -> None:
        self._terminal_sizes = {
            key: value for key, value in self._terminal_sizes.items() if key[0] != group_id
        }
        self._snapshots = {
            key: value for key, value in self._snapshots.items() if key[0] != group_id
        }

    def action_create_group(self) -> None:
        def created(group_id: str | None) -> None:
            if not group_id:
                return

            def choose_type(task_type: str | None) -> None:
                if task_type is None:
                    return
                flows = getattr(getattr(self.backend, "project_config", None), "flows", ())
                if flows:

                    def choose_flow(flow_id: str | None) -> None:
                        if flow_id is None:
                            return

                        def collect_context(context: dict[str, object] | None) -> None:
                            if context is None:
                                return
                            try:
                                self.backend.create_group(
                                    group_id,
                                    task_type=task_type,
                                    flow_id=flow_id,
                                    **context,
                                )
                                self._clear_group_caches(group_id)
                                self.selected_group = group_id
                                self.call_later(self.refresh_all)
                            except HypervisorError as error:
                                self.notify(str(error), severity="error", timeout=4)

                        fields = getattr(
                            self.backend.project_config, "task_form", DEFAULT_TASK_FORM
                        )
                        selected_flow = next(flow for flow in flows if flow.flow_id == flow_id)
                        fields = getattr(selected_flow, "task_form", None) or fields
                        self.push_screen(GroupContextScreen(fields), collect_context)

                    self.push_screen(FlowPickerScreen(tuple(flows)), choose_flow)
                    return
                try:
                    self.backend.create_group(group_id, task_type=task_type)
                    self._clear_group_caches(group_id)
                    self.selected_group = group_id
                    self.call_later(self.refresh_all)
                except HypervisorError as error:
                    self.notify(str(error), severity="error", timeout=4)

            self.push_screen(BranchTypeScreen(group_id), choose_type)

        self.push_screen(CreateGroupScreen(), created)

    def action_rename_group(self) -> None:
        if self.selected_group is None:
            return
        old_group_id = self.selected_group

        def renamed(new_group_id: str | None) -> None:
            if not new_group_id or new_group_id == old_group_id:
                return
            try:
                self.backend.rename_group(old_group_id, new_group_id)
                self._clear_group_caches(old_group_id)
                self.selected_group = new_group_id
                self.notify(f"Renamed {old_group_id} to {new_group_id}")
                self.call_later(self.refresh_all)
            except HypervisorError as error:
                self.notify(str(error), severity="error", timeout=5)

        self.push_screen(RenameGroupScreen(old_group_id), renamed)

    def action_remove_group(self) -> None:
        if self.selected_group is None:
            return
        group_id = self.selected_group

        def disposition(remove_worktree: bool | None) -> None:
            if remove_worktree is None:
                return
            try:
                self.backend.remove_group(group_id, remove_worktree)
                self._clear_group_caches(group_id)
                self.selected_group = None
                suffix = " and worktree" if remove_worktree else "; worktree kept"
                self.notify(f"Removed group {group_id}{suffix}")
                self.call_later(self.refresh_all)
            except HypervisorError as error:
                self.notify(str(error), severity="error", timeout=6)
                self.call_later(self.refresh_all)

        def confirmed(approved: bool) -> None:
            if approved:
                self.push_screen(WorktreeDispositionScreen(), disposition)

        self.push_screen(ConfirmRemoveScreen(group_id), confirmed)

    def action_next_terminal(self) -> None:
        self.selected_terminal = self.selected_terminal % self._panel_count + 1
        self._update_selection()

    def action_select_terminal(self, number: int) -> None:
        if not 1 <= number <= self._panel_count:
            return
        self.selected_terminal = number
        self._update_selection()

    def action_toggle_group_active(self) -> None:
        if self.selected_group is None or self._current_group is None:
            return
        group_id = self.selected_group
        activating = not self._current_group.active
        try:
            if activating:
                self.backend.activate_group(group_id)
            else:
                self.backend.deactivate_group(group_id)
            self._clear_group_caches(group_id)
            self.notify(f"{group_id}: {'activated' if activating else 'deactivated'}")
            self.call_later(self.refresh_all)
        except HypervisorError as error:
            self.notify(str(error), severity="error", timeout=6)

    def action_previous_terminal(self) -> None:
        self.selected_terminal = (self.selected_terminal - 2) % self._panel_count + 1
        self._update_selection()

    def _update_selection(self) -> None:
        for number in range(1, self._panel_count + 1):
            self._main_query_one(f"#terminal-{number}").set_class(
                number == self.selected_terminal, "selected"
            )
            self._main_query_one(f"#terminal-wrap-{number}").set_class(
                number == self.selected_terminal, "selected"
            )
            controls_row = self._main_query_one(f"#terminal-wrap-{number}").query_one(
                ".terminal-controls"
            )
            controls_row.styles.display = "block"
            for control in controls_row.query(Button):
                control.styles.display = "block" if number == self.selected_terminal else "none"
        self._apply_fullscreen()
        if self._current_group is not None:
            state = self._current_group.terminals[self.selected_terminal - 1].state
            self.selected_terminal_state = state
            self._update_terminal_controls(state)
        self._main_query_one(f"#terminal-{self.selected_terminal}", TerminalPanel).focus()

    def action_toggle_fullscreen(self) -> None:
        if self.selected_group is not None:
            self.fullscreen_terminal = not self.fullscreen_terminal
            self._apply_fullscreen()

    def _apply_fullscreen(self) -> None:
        sidebar = self._main_query_one("#sidebar")
        sidebar.styles.display = "none" if self.fullscreen_terminal else "block"
        grid = self._main_query_one("#terminal-grid")
        grid.set_class(self.fullscreen_terminal, "fullscreen")
        count = self._panel_count
        width = grid.content_size.width
        columns = (
            1
            if self.fullscreen_terminal or count == 1 or width < 60
            else (3 if count > 6 and width >= 150 else 2)
        )
        rows = 1 if self.fullscreen_terminal else (count + columns - 1) // columns
        grid.styles.grid_size_columns = columns
        grid.styles.grid_size_rows = rows
        grid.styles.grid_columns = " ".join(["1fr"] * columns)
        row_size = (
            "8" if grid.content_size.height and grid.content_size.height < rows * 9 else "1fr"
        )
        grid.styles.grid_rows = " ".join([row_size] * rows)
        for number in range(1, count + 1):
            wrapper = self._main_query_one(f"#terminal-wrap-{number}")
            panel = self._main_query_one(f"#terminal-{number}")
            wrapper.styles.column_span = (
                columns
                if not self.fullscreen_terminal and number == count and count % columns == 1
                else 1
            )
            wrapper.styles.display = (
                "block"
                if not self.fullscreen_terminal or number == self.selected_terminal
                else "none"
            )
            panel.styles.display = (
                "block"
                if not self.fullscreen_terminal or number == self.selected_terminal
                else "none"
            )
        self.call_after_refresh(self._sync_terminal_sizes)

    def _sync_terminal_sizes(self) -> None:
        if (
            self._resizing
            or self._refreshing
            or not self.is_running
            or self.selected_group is None
            or self._current_group is None
            or not self._current_group.active
        ):
            return
        self._resizing = True
        try:
            requests: list[tuple[int, int, int]] = []
            for number in range(1, self._panel_count + 1):
                panel = self._main_query_one(f"#terminal-{number}", TerminalPanel)
                if self._main_query_one(f"#terminal-wrap-{number}").styles.display == "none":
                    continue
                width = max(20, panel.content_size.width)
                height = max(5, panel.content_size.height)
                cache_key = (self.selected_group, number)
                if self._terminal_sizes.get(cache_key) != (width, height):
                    self._terminal_sizes[cache_key] = (width, height)
                    requests.append((number, width, height))
            for number, width, height in requests:
                self.backend.resize_terminal(
                    self.selected_group,
                    number,
                    width,
                    height,
                )
        except HypervisorError as error:
            self.notify(str(error), severity="error", timeout=4)
        finally:
            self._resizing = False

    def _flush_input(self, target: tuple[str, int]) -> None:
        self._flush_timers.pop(target, None)
        text = self._pending_text.pop(target, "")
        if text:
            self._submit_input(self.backend.send_literal, *target, text)

    def _flush_all_input(self) -> None:
        for target in list(self._pending_text):
            self._flush_input(target)

    def _submit_input(self, operation, *args) -> None:
        future = self._input_executor.submit(operation, *args)
        future.add_done_callback(self._input_finished)

    def _input_finished(self, future: Future[object]) -> None:
        try:
            future.result()
        except HypervisorError as error:
            self._dispatch_to_ui(self.notify, str(error), severity="error", timeout=4)

    def _dispatch_to_ui(self, callback, *args, **kwargs) -> None:
        try:
            if threading.get_ident() == self._thread_id:
                self.call_later(callback, *args, **kwargs)
            else:
                self.call_from_thread(callback, *args, **kwargs)
        except RuntimeError:
            pass

    def action_restart_terminal(self) -> None:
        action = "restart" if self.selected_terminal_state is TerminalState.RUNNING else "start"
        self._terminal_action(action)

    def action_stop_terminal(self) -> None:
        self._terminal_action("stop")

    def _terminal_action(self, action: str) -> None:
        if self.selected_group is None:
            return
        if action == "start":
            self._choose_terminal_session()
            return
        try:
            getattr(self.backend, f"{action}_terminal")(self.selected_group, self.selected_terminal)
            self.notify(
                TEXT["terminal_start_notification"].format(
                    terminal_label=TERMINAL_LABEL.format(number=self.selected_terminal),
                    action=action,
                )
            )
            self.call_later(self.refresh_all)
        except HypervisorError as error:
            self.notify(str(error), severity="error", timeout=4)

    def _choose_terminal_session(self) -> None:
        if self.selected_group is None:
            return
        group_id = self.selected_group
        terminal = self.selected_terminal
        try:
            sessions = self.backend.list_terminal_sessions(group_id, terminal)
        except HypervisorError as error:
            self.notify(str(error), severity="error", timeout=4)
            return

        def chosen(session_id: str | None) -> None:
            if session_id is None:
                return
            try:
                self.backend.start_terminal(group_id, terminal, session_id or None)
                action = "new session" if not session_id else f"resumed {session_id[:8]}…"
                self.notify(f"Terminal {terminal}: {action}")
                self.call_later(self.refresh_all)
            except HypervisorError as error:
                self.notify(str(error), severity="error", timeout=5)

        label = (
            self._current_group.terminals[terminal - 1].agent_name if self._current_group else ""
        )
        self.push_screen(SessionPickerScreen(terminal, sessions, agent_name=label), chosen)

    def action_next_group(self) -> None:
        self._move_group(1)

    def action_previous_group(self) -> None:
        self._move_group(-1)

    def _move_group(self, offset: int) -> None:
        if not self.group_ids:
            return
        current = (
            self.group_ids.index(self.selected_group)
            if self.selected_group in self.group_ids
            else 0
        )
        target = (current + offset) % len(self.group_ids)
        self._main_query_one("#groups", ListView).index = target


def run() -> None:
    HypervisorApp().run()
