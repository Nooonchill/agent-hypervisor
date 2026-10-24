from __future__ import annotations

import unittest
from unittest.mock import patch

from textual import events
from textual.widgets import Button

from agent_hypervisor.dialogs import BranchTypeScreen, HypervisorButton
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

    def create_group(self, group_id, cwd=None, *, task_type="feature"):
        self.last_create = (group_id, task_type)
        return self.group

    def activate_group(self, group_id):
        self.last_group_action = ("activate", group_id)
        return self.group

    def deactivate_group(self, group_id):
        self.last_group_action = ("deactivate", group_id)
        return self.group


class TuiSmokeTests(unittest.IsolatedAsyncioTestCase):
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
                    self.assertEqual(app.selected_terminal, 1)
                    if count <= 9:
                        await pilot.press(f"alt+{count}")
                    else:
                        app.action_previous_terminal()
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
                await pilot.mouse_down(panel, offset=(2, 1))
                await pilot.mouse_up(panel, offset=(10, 1))
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

    def test_hypervisor_hotkeys_do_not_intercept_qwen_shortcuts(self) -> None:
        qwen_shortcuts = {
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
        hypervisor_shortcuts = {binding.key for binding in HypervisorApp.BINDINGS}
        self.assertTrue(hypervisor_shortcuts.isdisjoint(qwen_shortcuts))

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
            self.assertGreaterEqual(panel_height, grid_height - 2)

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

            self.assertEqual(str(app.query_one("#start-button", Button).label), "▶")
            self.assertIn(
                "press Start",
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
            await pilot.click("#start-button")
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
