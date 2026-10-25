from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from textual import events
from textual.widgets import Button, Input, TextArea

from agent_hypervisor.dialogs import (
    BranchTypeScreen,
    FlowPickerScreen,
    GroupContextScreen,
    HumanDecisionScreen,
    HypervisorButton,
    ReportScreen,
)
from agent_hypervisor.models import GroupStatus, TerminalState, TerminalStatus
from agent_hypervisor.state import AgentSession
from agent_hypervisor.tui import (
    ConfirmRemoveScreen,
    CreateGroupScreen,
    HypervisorApp,
    RenameGroupScreen,
    SessionPickerScreen,
    TerminalPanel,
    WorktreeDispositionScreen,
)
from agent_hypervisor.widgets import WorkflowRoadmap
from agent_hypervisor.task_form import DEFAULT_TASK_FORM


def terminal_text(panel: TerminalPanel) -> str:
    return "\n".join(line.text for line in panel.lines)


class EmptyBackend:
    def list_groups(self):
        return []


class PopulatedBackend:
    group = GroupStatus(
        group_id="Test-123",
        session_name="agent-hypervisor-Test-123",
        attached_clients=0,
        terminals=tuple(
            TerminalStatus(number, TerminalState.RUNNING, "codex") for number in range(1, 4)
        ),
    )

    def list_groups(self):
        return [self.group] if self.group is not None else []

    def capture(self, group_id, terminal, lines=120):
        if lines > 0:
            return "\n".join(f"history line {number}" for number in range(200))
        return f"Terminal {terminal} is ready"

    def send_text(self, group_id, terminal, text, enter=True):
        self.last_send = (group_id, terminal, text, enter)

    def send_literal(self, group_id, terminal, text):
        if not hasattr(self, "input_ops"):
            self.input_ops = []
        self.input_ops.append(("text", group_id, terminal, text))

    def send_key(self, group_id, terminal, key):
        if not hasattr(self, "input_ops"):
            self.input_ops = []
        self.input_ops.append(("key", group_id, terminal, key))

    def resize_terminal(self, group_id, terminal, width, height):
        pass

    def list_terminal_sessions(self, group_id, terminal):
        return [AgentSession("aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb", terminal)]

    def start_terminal(self, group_id, terminal, session_id=None):
        self.last_start = (group_id, terminal, session_id)

    def remove_group(self, group_id, remove_worktree=False):
        self.last_remove = (group_id, remove_worktree)
        self.group = None

    def rename_group(self, group_id, new_group_id):
        self.last_rename = (group_id, new_group_id)
        return self.group

    def create_group(
        self,
        group_id,
        cwd=None,
        *,
        task_type="feature",
        flow_id=None,
        request="",
        goal="",
        sources=(),
        constraints="",
    ):
        self.last_create = (group_id, task_type)
        self.last_create_args = (
            group_id,
            task_type,
            flow_id,
            {"request": request, "goal": goal, "sources": sources, "constraints": constraints},
        )
        return self.group

    def activate_group(self, group_id):
        self.last_group_action = ("activate", group_id)
        return self.group

    def deactivate_group(self, group_id):
        self.last_group_action = ("deactivate", group_id)
        return self.group


class TuiSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_flow_controls_visible_form_and_hidden_context(self) -> None:
        backend = PopulatedBackend()
        fields = tuple(
            replace(
                field,
                label=f"Docs {field.field_id}",
                visible=field.field_id in {"request", "sources"},
                default=("docs.md",) if field.field_id == "sources" else "Docs context",
            )
            for field in DEFAULT_TASK_FORM
        )
        first = SimpleNamespace(flow_id="first", name="First", task_form=None)
        docs = SimpleNamespace(flow_id="docs", name="Docs", task_form=fields)
        backend.project_config = SimpleNamespace(
            agents=(1, 2, 3), flows=(first, docs), task_form=DEFAULT_TASK_FORM
        )
        app = HypervisorApp(backend=backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_create_group()
            await pilot.pause()
            await pilot.press("D", "O", "C", "S", "enter")
            await pilot.click("#branch-docs")
            await pilot.pause()
            await pilot.click("#flow-docs")
            await pilot.pause()
            self.assertIsInstance(app.screen, GroupContextScreen)
            self.assertFalse(app.screen.query("#context-goal"))
            self.assertFalse(app.screen.query("#context-constraints"))
            self.assertEqual(app.screen.query_one("#context-request").text, "Docs context")
            await pilot.click("#confirm")
            await pilot.pause()
            self.assertEqual(backend.last_create_args[2], "docs")
            self.assertEqual(backend.last_create_args[3]["constraints"], "Docs context")
            self.assertEqual(backend.last_create_args[3]["sources"], ("docs.md",))

    async def test_form_uses_configured_types_and_keeps_free_text_sources(self) -> None:
        fields = tuple(
            replace(field, input_type="text", placeholder="Task name")
            if field.field_id == "request"
            else replace(
                field,
                input_type="multiline",
                default="",
                description="Links or background context",
                placeholder="Paste any text",
            )
            if field.field_id == "sources"
            else field
            for field in DEFAULT_TASK_FORM
        )
        app = HypervisorApp(backend=EmptyBackend())
        results = []
        async with app.run_test(size=(110, 50)) as pilot:
            dialog = GroupContextScreen(fields)
            app.push_screen(dialog, results.append)
            await pilot.pause()
            request = dialog.query_one("#context-request", Input)
            source = dialog.query_one("#context-sources", TextArea)
            self.assertEqual(request.placeholder, "Task name")
            self.assertEqual(source.placeholder, "Paste any text")
            self.assertTrue(list(dialog.query(".context-description")))
            request.value = "Task"
            value = "Read the design, especially section 2.\nAsk about examples."
            source.load_text(value)
            await pilot.click("#confirm")
            await pilot.pause()
            self.assertEqual(results[0]["sources"], value)

    async def test_configured_task_form_prefills_values_and_requires_missing_field(self) -> None:
        fields = tuple(
            replace(
                field,
                label=f"Custom {field.field_id}",
                required=field.field_id == "goal",
                default=("spec.md",)
                if field.field_id == "sources"
                else "Preset request"
                if field.field_id == "request"
                else "",
            )
            for field in DEFAULT_TASK_FORM
        )
        app = HypervisorApp(backend=EmptyBackend())
        results = []
        async with app.run_test(size=(100, 45)) as pilot:
            dialog = GroupContextScreen(fields)
            app.push_screen(dialog, results.append)
            await pilot.pause()
            self.assertEqual(dialog.query_one("#context-request").text, "Preset request")
            self.assertEqual(dialog.query_one("#context-sources").text, "spec.md")
            await pilot.click("#confirm")
            await pilot.pause(0.3)
            self.assertIs(app.screen, dialog)
            dialog.query_one("#context-goal").load_text("Goal supplied by the user")
            await pilot.click("#confirm")
            await pilot.pause()
            self.assertEqual(results[0]["goal"], "Goal supplied by the user")
            self.assertEqual(results[0]["sources"], ("spec.md",))

    async def test_manual_mode_button_preserves_session_and_allows_typing(self) -> None:
        backend = PopulatedBackend()
        backend.group = replace(
            backend.group,
            terminals=tuple(
                replace(terminal, agent_id=f"agent-{terminal.number}")
                for terminal in backend.group.terminals
            ),
        )

        class Workflow:
            modes = {"agent-1": "workflow"}
            changes = []

            def status(self, group):
                return {
                    "flow_id": "delivery",
                    "state": "running",
                    "current_step": "work",
                    "stages": [{"id": "work", "state": "running"}],
                    "agent_modes": dict(self.modes),
                }

            def set_agent_mode(self, group, agent_id, mode):
                self.changes.append((group, agent_id, mode))
                self.modes[agent_id] = mode
                return self.status(group)

        backend.workflow = Workflow()
        app = HypervisorApp(backend=backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            button = app.query_one("#mode-button-1", Button)
            self.assertFalse(button.disabled)
            await pilot.click(button)
            await pilot.pause(0.1)
            self.assertTrue(button.has_class("manual-mode"))
            self.assertEqual(app.query_one("#terminal-wrap-1").border_subtitle, "MANUAL")
            await pilot.click("#terminal-1")
            await pilot.press("h", "i", "enter")
            await pilot.pause(0.1)
            self.assertIn(("key", "Test-123", 1, "enter"), backend.input_ops)
            await pilot.click(button)
            await pilot.pause(0.1)
            self.assertFalse(button.has_class("manual-mode"))
            self.assertEqual(
                backend.workflow.changes,
                [("Test-123", "agent-1", "manual"), ("Test-123", "agent-1", "workflow")],
            )
            self.assertFalse(hasattr(backend, "last_start"))

    async def test_variable_panel_counts_navigation_and_group_switch(self) -> None:
        backend = PopulatedBackend()
        app = HypervisorApp(backend=backend)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            for count in (1, 2, 4, 6, 10):
                with self.subTest(count=count):
                    backend.group = GroupStatus(
                        "Test-123",
                        "agent-hypervisor-Test-123",
                        0,
                        tuple(
                            TerminalStatus(
                                i,
                                TerminalState.RUNNING,
                                "qwen",
                                agent_id=f"agent-{i}",
                                agent_name=f"Agent {i}",
                            )
                            for i in range(1, count + 1)
                        ),
                    )
                    await app.refresh_all()
                    await pilot.pause()
                    panels = list(app.query(TerminalPanel))
                    self.assertEqual(len(panels), count)
                    self.assertTrue(
                        all(panel.region.width > 0 and panel.region.height > 0 for panel in panels)
                    )
                    app.action_select_terminal(count)
                    self.assertEqual(app.selected_terminal, count)
                    self.assertIn(f"Agent {count}", panels[-1].border_title)
                    app.action_next_terminal()
                    self.assertEqual(app.selected_panel, count + 1)
                    app.action_next_terminal()
                    self.assertEqual(app.selected_terminal, 1)
                    await pilot.press(f"ctrl+shift+{count % 10}")
                    self.assertEqual(app.selected_terminal, count)
                    app.action_toggle_fullscreen()
                    await pilot.pause()
                    self.assertEqual(sum(panel.display for panel in panels), 1)
                    app.action_toggle_fullscreen()
                    await pilot.pause()

    async def test_creation_asks_task_code_then_branch_type(self) -> None:
        backend = PopulatedBackend()
        app = HypervisorApp(backend=backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_create_group()
            await pilot.pause()
            await pilot.press("T", "A", "S", "K", "-", "4", "2", "enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, BranchTypeScreen)
            await pilot.click("#branch-fix")
            await pilot.pause()
            self.assertEqual(backend.last_create, ("TASK-42", "fix"))

    async def test_creation_with_flow_collects_initial_context(self) -> None:
        backend = PopulatedBackend()
        flow = SimpleNamespace(flow_id="default", name="Default", steps=())
        backend.project_config = SimpleNamespace(agents=(1, 2, 3), flows=(flow,))
        app = HypervisorApp(backend=backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_create_group()
            await pilot.pause()
            await pilot.press("T", "A", "S", "K", "-", "8", "8", "enter")
            await pilot.click("#branch-fix")
            await pilot.pause()
            await pilot.click("#flow-default")
            await pilot.pause()
            self.assertIsInstance(app.screen, GroupContextScreen)
            app.screen.query_one("#context-request").load_text("Implement the feature")
            app.screen.query_one("#context-goal").load_text("Ship it")
            app.screen.query_one("#context-sources").load_text("spec.md\nhttps://example.test")
            app.screen.query_one("#context-constraints").load_text("Keep compatibility")
            await pilot.click("#confirm")
            await pilot.pause()
            self.assertEqual(backend.last_create_args[0:3], ("TASK-88", "fix", "default"))
            self.assertEqual(backend.last_create_args[3]["request"], "Implement the feature")
            self.assertEqual(
                backend.last_create_args[3]["sources"], ("spec.md", "https://example.test")
            )

    async def test_flow_picker_and_human_decision_dialog(self) -> None:
        app = HypervisorApp(backend=EmptyBackend())
        async with app.run_test(size=(100, 35)) as pilot:
            flow = SimpleNamespace(flow_id="review", name="Review flow")
            picker = FlowPickerScreen((flow,))
            app.push_screen(picker)
            await pilot.pause()
            self.assertEqual(picker.query_one("#flow-review").label, "Review flow · review")
            await pilot.click("#flow-review")
            await pilot.pause()

            dialog = HumanDecisionScreen("implementation", '{"checks": []}', allow_decision=True)
            app.push_screen(dialog)
            await pilot.pause()
            await pilot.click("#changes")
            self.assertIs(app.screen, dialog)
            dialog.query_one("#human-comment").load_text("Please fix the failing check")
            # Textual deliberately ignores clicks during the button's active animation.
            await pilot.pause(dialog.query_one("#changes", Button).active_effect_duration + 0.05)
            await pilot.click("#changes")
            await pilot.pause()
            self.assertIsNot(app.screen, dialog)

    async def test_workflow_roadmap_shows_stage_and_human_approval_details(self) -> None:
        backend = PopulatedBackend()
        backend.project_config = SimpleNamespace(
            agents=(1, 2, 3),
            flows=(),  # A group keeps its event-sourced flow snapshot if global config changes.
        )

        class Workflow:
            decisions = []
            resumed = []

            def status(self, group_id):
                return {
                    "flow_id": "default",
                    "state": "awaiting-human",
                    "current_step": "implement",
                    "stages": [
                        {
                            "id": "implement",
                            "state": "awaiting-human",
                            "attempt": "a1b2c3d4-55aa-4cc0-88dd-1234567890ab",
                            "human_approval": True,
                            "summary": "Implemented change",
                            "checks": [{"report": "checks.txt", "returncode": 0}],
                            "review": {"decision": "approved", "summary": "Looks good"},
                            "commit": "abc123",
                            "pr": "not_integrated",
                        }
                    ],
                }

            def human_decision(self, *args, **kwargs):
                self.decisions.append((args, kwargs))
                return {}

            def start(self, group_id):
                return {}

            def resume(self, group_id):
                self.resumed.append(group_id)
                return {}

            def report(self, group_id, path):
                return f"Report contents for {path} in {group_id}"

        backend.workflow = Workflow()
        app = HypervisorApp(backend=backend)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause(0.2)
            roadmap = app.query_one("#workflow-roadmap", WorkflowRoadmap)
            self.assertEqual(roadmap.stages[0]["id"], "implement")
            self.assertEqual(app.query_one("#workflow-row").styles.display, "block")
            self.assertTrue(
                app.query_one("#group-header").content_region.contains_region(roadmap.region)
            )
            await pilot.click("#workflow-start")
            await pilot.pause(0.1)
            self.assertEqual(backend.workflow.resumed, ["Test-123"])
            await pilot.click(roadmap, offset=(roadmap.content_size.width - 1, 0))
            await pilot.pause()
            self.assertIsInstance(app.screen, HumanDecisionScreen)
            details = str(app.screen.query_one("#human-details").render())
            self.assertIn("approved", details)
            self.assertTrue(app.screen.query_one("#workflow-report-0").display)
            self.assertIn("abc123", details)
            await pilot.click("#workflow-report-0")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, ReportScreen)
            self.assertIn("Report contents", str(app.screen.query_one("#report-body").lines))
            await pilot.click("#close-report")
            await pilot.pause()
            await pilot.click("#approve")
            await pilot.pause(0.1)
            self.assertEqual(
                backend.workflow.decisions[0][0][0:3],
                ("Test-123", "implement", "a1b2c3d4-55aa-4cc0-88dd-1234567890ab"),
            )

    async def test_hovered_dialog_button_renders_its_background_with_the_label(self) -> None:
        app = HypervisorApp(backend=EmptyBackend())
        async with app.run_test(size=(100, 30)) as pilot:
            dialog = CreateGroupScreen()
            app.push_screen(dialog)
            await pilot.pause()
            button = dialog.query_one("#confirm", HypervisorButton)
            await pilot.hover(button)
            await pilot.pause()

            self.assertTrue(button.has_pseudo_class("hover"))
            self.assertEqual(button.render().style.bgcolor, button.rich_style.bgcolor)

    async def test_form_and_session_dialogs_support_arrows_and_escape(self) -> None:
        app = HypervisorApp(backend=PopulatedBackend())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for dialog in (CreateGroupScreen(), RenameGroupScreen("Test-123")):
                app.push_screen(dialog)
                await pilot.pause()
                self.assertEqual(dialog.focused.id, "group-id")
                await pilot.press("down")
                self.assertEqual(dialog.focused.id, "cancel")
                await pilot.press("right")
                self.assertEqual(dialog.focused.id, "confirm")
                await pilot.press("escape")
                await pilot.pause()
                self.assertIsNot(app.screen, dialog)

            picker = SessionPickerScreen(1, [AgentSession("abcd1234", 1)])
            app.push_screen(picker)
            await pilot.pause()
            self.assertEqual(picker.focused.id, "new-session")
            await pilot.press("down")
            self.assertEqual(picker.focused.id, "resume-0")
            await pilot.press("up")
            self.assertEqual(picker.focused.id, "new-session")
            await pilot.press("escape")

    async def test_drag_selects_and_copies_terminal_text_without_interrupting_qwen(self) -> None:
        backend = PopulatedBackend()
        app = HypervisorApp(backend=backend)
        with patch("agent_hypervisor.tui.copy_native", return_value=True):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.click("#terminal-1")
                panel = app.query_one("#terminal-1", TerminalPanel)
                await pilot.mouse_down(panel, offset=(1, 0))
                await pilot.mouse_up(panel, offset=(9, 0))
                await pilot.pause()

                self.assertEqual(app.screen.get_selected_text(), "Terminal")
                self.assertEqual(app.clipboard, "Terminal")
                panel.update("New output while reading")
                self.assertIn("Terminal 1 is ready", terminal_text(panel))
                await pilot.press("ctrl+c")
                await pilot.pause()
                self.assertEqual(backend.input_ops[-1], ("key", "Test-123", 1, "ctrl+c"))

    async def test_copy_selection_from_hypervisor_label(self) -> None:
        app = HypervisorApp(backend=PopulatedBackend())
        with patch("agent_hypervisor.tui.copy_native", return_value=True):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.mouse_down("#group-title", offset=(0, 0))
                await pilot.mouse_up("#group-title", offset=(4, 0))
                await pilot.pause()
                self.assertEqual(app.clipboard, "TASK")

    async def test_hypervisor_hotkeys_do_not_intercept_qwen_shortcuts(self) -> None:
        qwen_shortcuts = {
            "f1",
            "f5",
            "f12",
            "ctrl+g",
            "ctrl+i",
            "ctrl+m",
            "escape",
            "enter",
            "tab",
            "shift+tab",
            "shift+enter",
            "shift+up",
            "shift+down",
            "pageup",
            "pagedown",
            "home",
            "end",
            "delete",
            "up",
            "down",
            "left",
            "right",
            "alt+t",
            "alt+m",
            "ctrl+a",
            "ctrl+b",
            "ctrl+c",
            "ctrl+d",
            "ctrl+e",
            "ctrl+f",
            "ctrl+h",
            "ctrl+j",
            "ctrl+k",
            "ctrl+l",
            "ctrl+n",
            "ctrl+o",
            "ctrl+p",
            "ctrl+q",
            "ctrl+r",
            "ctrl+s",
            "ctrl+t",
            "ctrl+u",
            "ctrl+v",
            "ctrl+w",
            "ctrl+x",
            "ctrl+y",
            "ctrl+z",
            "ctrl+delete",
            "ctrl+home",
            "ctrl+end",
            "ctrl+enter",
            "ctrl+left",
            "ctrl+right",
            "ctrl+shift+up",
            "ctrl+shift+down",
            "ctrl+shift+z",
        }
        backend = PopulatedBackend()
        app = HypervisorApp(backend=backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#terminal-1")
            await pilot.press(*sorted(qwen_shortcuts))
            await pilot.pause()
            forwarded = {op[3] for op in backend.input_ops if op[0] == "key"}
            self.assertTrue(qwen_shortcuts.issubset(forwarded), qwen_shortcuts - forwarded)
            self.assertIs(app.screen, app._main_screen)
            self.assertIsInstance(app.focused, TerminalPanel)
            self.assertNotIn("ctrl+q", app._bindings.key_to_bindings)
            self.assertNotIn("ctrl+p", app._bindings.key_to_bindings)

    async def test_app_mounts_without_groups(self) -> None:
        app = HypervisorApp(backend=EmptyBackend())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(app.group_ids, [])

    async def test_app_renders_a_populated_group_and_fullscreen(self) -> None:
        app = HypervisorApp(backend=PopulatedBackend())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(app.selected_group, "Test-123")
            self.assertEqual(app.query_one("#terminal-grid").styles.display, "block")
            self.assertIn(
                "Terminal 1 is ready",
                terminal_text(app.query_one("#terminal-1", TerminalPanel)),
            )

            app.action_toggle_fullscreen()
            await pilot.pause()
            self.assertEqual(app.query_one("#terminal-2").styles.display, "none")
            grid_height = app.query_one("#terminal-grid").size.height
            panel_height = app.query_one("#terminal-1").size.height
            self.assertGreaterEqual(panel_height, grid_height - 5)

            app.action_toggle_fullscreen()
            await pilot.pause()
            self.assertEqual(app.query_one("#terminal-2").styles.display, "block")

    async def test_click_selects_terminal_and_keys_are_forwarded_directly(self) -> None:
        backend = PopulatedBackend()
        app = HypervisorApp(backend=backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#terminal-2")
            await pilot.pause()
            self.assertEqual(app.selected_terminal, 2)

            await pilot.press("h", "i", "enter")
            await pilot.pause()
            text = "".join(
                operation[3] for operation in backend.input_ops if operation[0] == "text"
            )
            self.assertEqual(text, "hi")
            self.assertEqual(backend.input_ops[-1], ("key", "Test-123", 2, "enter"))

    async def test_stopped_terminal_explains_how_to_start_it(self) -> None:
        backend = PopulatedBackend()
        backend.group = GroupStatus(
            group_id="Test-123",
            session_name="agent-hypervisor-Test-123",
            attached_clients=0,
            terminals=(
                TerminalStatus(1, TerminalState.RUNNING, "codex"),
                TerminalStatus(2, TerminalState.STOPPED, "sleep"),
                TerminalStatus(3, TerminalState.RUNNING, "codex"),
            ),
        )
        app = HypervisorApp(backend=backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#terminal-2")
            await pilot.pause()

            self.assertEqual(str(app.query_one("#start-button-2", Button).label), "▶")
            self.assertIn(
                "Use Start",
                terminal_text(app.query_one("#terminal-2", TerminalPanel)),
            )
            self.assertEqual(
                app.query_one("#terminal-1").size.height,
                app.query_one("#terminal-2").size.height,
            )

    async def test_mouse_wheel_scrolls_qwen_native_history(self) -> None:
        backend = PopulatedBackend()
        app = HypervisorApp(backend=backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            panel = app.query_one("#terminal-1", TerminalPanel)
            panel.post_message(events.MouseScrollUp(panel, 1, 1, 0, -1, 0, False, False, False))
            await pilot.pause()

            self.assertFalse(panel.has_class("scrollback"))
            self.assertEqual(backend.input_ops[-1], ("key", "Test-123", 1, "shift+up"))

    async def test_stopped_terminal_opens_session_picker(self) -> None:
        backend = PopulatedBackend()
        backend.group = GroupStatus(
            group_id="Test-123",
            session_name="agent-hypervisor-Test-123",
            attached_clients=0,
            terminals=tuple(
                TerminalStatus(number, TerminalState.STOPPED) for number in range(1, 4)
            ),
        )
        app = HypervisorApp(backend=backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#start-button-1")
            await pilot.pause()
            self.assertIsInstance(app.screen, SessionPickerScreen)

            await pilot.click("#resume-0")
            await pilot.pause()
            self.assertEqual(
                backend.last_start,
                ("Test-123", 1, "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"),
            )

    async def test_inactive_group_has_visible_actions_and_activation(self) -> None:
        backend = PopulatedBackend()
        backend.group = GroupStatus(
            group_id="Test-123",
            session_name="agent-hypervisor-Test-123",
            attached_clients=0,
            terminals=tuple(
                TerminalStatus(number, TerminalState.STOPPED) for number in range(1, 4)
            ),
            worktree_path="/tmp/worktree",
            branch="hypervisor/Test-123",
            active=False,
        )
        app = HypervisorApp(backend=backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(str(app.query_one("#activate-group", Button).label), "▶")
            self.assertIn(
                "Group is inactive",
                terminal_text(app.query_one("#terminal-1", TerminalPanel)),
            )
            self.assertGreater(app.query_one("#remove-group", Button).region.width, 0)

            group_buttons = [
                app.query_one(f"#{button_id}", Button)
                for button_id in (
                    "create-group",
                    "activate-group",
                    "rename-group",
                    "remove-group",
                )
            ]
            self.assertEqual({button.region.width for button in group_buttons}, {5})
            self.assertEqual(len({button.region.y for button in group_buttons}), 1)

            app.query_one("#activate-group", Button).press()
            await pilot.pause()
            self.assertEqual(backend.last_group_action, ("activate", "Test-123"))

    async def test_remove_group_requires_confirmation_and_worktree_choice(self) -> None:
        backend = PopulatedBackend()
        app = HypervisorApp(backend=backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.query_one("#remove-group", Button).press()
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmRemoveScreen)

            dialog = app.screen.query_one("#create-dialog")
            self.assertLessEqual(abs(dialog.region.center[0] - app.screen.region.center[0]), 1)
            self.assertLessEqual(abs(dialog.region.center[1] - app.screen.region.center[1]), 1)
            self.assertEqual(app.screen.styles.background.a, 0)
            await pilot.press("right")
            self.assertEqual(app.screen.focused.id, "confirm")
            await pilot.press("left")
            self.assertEqual(app.screen.focused.id, "cancel")

            await pilot.click("#confirm")
            await pilot.pause()
            self.assertIsInstance(app.screen, WorktreeDispositionScreen)

            buttons = list(app.screen.query(Button))
            self.assertEqual(len({button.region.y for button in buttons}), 1)
            for button in buttons:
                self.assertTrue(
                    app.screen.query_one("#create-actions").region.contains_region(button.region)
                )
            await pilot.press("right", "right")
            self.assertEqual(app.screen.focused.id, "remove")

            await pilot.click("#remove")
            await pilot.pause()
            self.assertEqual(backend.last_remove, ("Test-123", True))


if __name__ == "__main__":
    unittest.main()
