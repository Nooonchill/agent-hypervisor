from __future__ import annotations

import logging
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
)
from .contracts import HypervisorBackend
from .diagnostics import LOGGER_NAME
from .dialogs import (
    BranchTypeScreen,
    ConfirmRemoveScreen,
    CreateGroupScreen,
    RenameGroupScreen,
    SessionPickerScreen,
    WorktreeDispositionScreen,
)
from .models import GroupStatus, HypervisorError, TerminalState
from .project_config import ProjectConfig
from .tmux import TmuxBackend
from .widgets import HypervisorButton

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
                with Horizontal(id="group-header"):
                    yield Label(TEXT["no_group"], id="group-title")
                    yield Label("", id="group-state")
                yield Static(TEXT["empty"], id="empty")
                with Container(id="terminal-grid"):
                    for number in range(1, self._panel_count + 1):
                        yield TerminalPanel(number)
                with Horizontal(id="command-row"):
                    start_spec = ACTIONS["restart_terminal"]
                    start = HypervisorButton(
                        start_spec.icon, id=start_spec.button_id, classes="icon-button"
                    )
                    start.tooltip = start_spec.tooltip_for()
                    yield start
                    stop_spec = ACTIONS["stop_terminal"]
                    stop = HypervisorButton(
                        stop_spec.icon, id=stop_spec.button_id, classes="icon-button"
                    )
                    stop.tooltip = stop_spec.tooltip_for()
                    yield stop
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

    @on(Button.Pressed, "#start-button")
    def start_button(self) -> None:
        action = "restart" if self.selected_terminal_state is TerminalState.RUNNING else "start"
        self._terminal_action(action)

    @on(Button.Pressed, "#stop-button")
    def stop_button(self) -> None:
        self._terminal_action("stop")

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
            panel = self._main_query_one(f"#terminal-{status.number}", TerminalPanel)
            if panel.styles.display == "none":
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
        await grid.mount(*(TerminalPanel(number) for number in range(1, count + 1)))
        self._panel_count = count
        self.selected_terminal = min(self.selected_terminal, count)

    def _render_selected(self, group: GroupStatus | None) -> None:
        has_group = group is not None
        self._current_group = group
        self._main_query_one("#empty").styles.display = "none" if has_group else "block"
        # Textual's ``display`` property controls visibility only; the grid layout
        # itself is declared in CSS with ``layout: grid``.
        self._main_query_one("#terminal-grid").styles.display = "block" if has_group else "none"
        self._main_query_one("#command-row").styles.display = "block" if has_group else "none"
        if group is None:
            self.selected_terminal_state = TerminalState.MISSING
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
            panel.set_class(status.number == self.selected_terminal, "selected")
            panel.set_class(status.state is TerminalState.STOPPED, "stopped")
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

        selected_status = group.terminals[self.selected_terminal - 1]
        self.selected_terminal_state = selected_status.state
        self._update_terminal_controls(selected_status.state)
        self._main_query_one("#start-button", Button).disabled = not group.active
        self._main_query_one("#stop-button", Button).disabled = (
            not group.active or selected_status.state is not TerminalState.RUNNING
        )
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
        start_button = self._main_query_one("#start-button", Button)
        start_button.label = TERMINAL_ACTION_ICONS[state.value]
        start_button.tooltip = ACTIONS["restart_terminal"].tooltip_for(
            TEXT["restart_tooltip"] if state is TerminalState.RUNNING else TEXT["start_tooltip"]
        )
        self._main_query_one("#stop-button", Button).disabled = state is not TerminalState.RUNNING

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
            panel = self._main_query_one(f"#terminal-{number}")
            panel.styles.column_span = (
                columns
                if not self.fullscreen_terminal and number == count and count % columns == 1
                else 1
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
                if panel.styles.display == "none":
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
            try:
                self.call_from_thread(self.notify, str(error), severity="error", timeout=4)
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
