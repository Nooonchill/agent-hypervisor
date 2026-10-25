from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from textual import events
from textual.widgets import Input
from textual._xterm_parser import XTermParser

from agent_hypervisor.dialogs import ConfirmRemoveScreen, CreateGroupScreen, RenameGroupScreen
from agent_hypervisor.tui import HypervisorApp, TerminalPanel
from agent_hypervisor.event_log import WorkflowEventLog
from agent_hypervisor.keyboard import preserve_alt_with_associated_text
from tests.test_tui import PopulatedBackend


class KeyboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_mode_toggle_tabs_and_spatial_navigation(self):
        backend = PopulatedBackend()
        app = HypervisorApp(backend=backend)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            self.assertNotIsInstance(app.focused, TerminalPanel)
            await pilot.press("tab")
            self.assertEqual(app.selected_terminal, 2)
            self.assertNotIsInstance(app.focused, TerminalPanel)
            await pilot.press("shift+tab")
            self.assertEqual(app.selected_terminal, 1)
            await pilot.press("ctrl+shift+right")
            self.assertEqual(app.selected_terminal, 2)
            await pilot.press("ctrl+shift+left", "ctrl+shift+down")
            self.assertEqual(app.selected_terminal, 3)
            await pilot.press("ctrl+shift+right")
            self.assertEqual(app.selected_panel, 4)
            self.assertIsInstance(app.focused, WorkflowEventLog)
            await pilot.press("ctrl+shift+up")
            self.assertEqual(app.selected_terminal, 2)
            await pilot.press("ctrl+shift+2")
            self.assertEqual(app.selected_terminal, 2)
            await pilot.press("alt+h")
            await pilot.pause()
            self.assertEqual(app.focused.id, "terminal-2")
            self.assertIn("AGENT INPUT", app.sub_title)
            await pilot.press(
                "tab", "shift+tab", "ctrl+shift+left", "ctrl+1", "ctrl+up", "ctrl+down", "ctrl+x"
            )
            app.focused.post_message(events.Key("ctrl+shift+1", "!"))
            await pilot.pause()
            self.assertEqual(app.selected_terminal, 2)
            self.assertEqual(app.focused.id, "terminal-2")
            forwarded = {op[3] for op in backend.input_ops if op[0] == "key"}
            self.assertTrue(
                {
                    "tab",
                    "shift+tab",
                    "ctrl+shift+left",
                    "ctrl+1",
                    "ctrl+up",
                    "ctrl+down",
                    "ctrl+shift+1",
                    "ctrl+x",
                }.issubset(forwarded)
            )
            await pilot.press("alt+z")
            self.assertTrue(app.fullscreen_terminal)
            self.assertEqual(app.focused.id, "terminal-2")
            await pilot.press("alt+h")
            await pilot.pause()
            self.assertNotIsInstance(app.focused, TerminalPanel)
            self.assertIn("УПРАВЛЕНИЕ", app.sub_title)
            await pilot.press("ctrl+shift+down", "alt+z")
            self.assertEqual(app.selected_panel, 4)
            self.assertFalse(app.fullscreen_terminal)

    async def test_raw_keyboard_protocol_keeps_alt_and_shifted_digit_navigation(self):
        backend = PopulatedBackend()
        app = HypervisorApp(backend=backend)
        parser = XTermParser()
        associated_text = hasattr(XTermParser, "_parse_extended_key")

        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()

            async def send(sequence):
                if not associated_text and sequence.count(";") == 2:
                    sequence = sequence.rsplit(";", 1)[0] + "u"
                for event in parser.feed(sequence):
                    app.post_message(event)
                await pilot.pause()

            await send("\x1b[50;6;64u")  # Ctrl+Shift+2, with associated @.
            self.assertEqual(app.selected_panel, 2)
            await send("\x1b[104;3;104u")
            self.assertEqual(app.focused.id, "terminal-2")
            await send("\x1b[122;3;122u")
            self.assertTrue(app.fullscreen_terminal)
            await send("\x1b[104;3;104u")
            self.assertNotIsInstance(app.focused, TerminalPanel)
            await send("\x1b[52;6;36u")
            self.assertEqual(app.selected_panel, 4)
            self.assertEqual(app.focused.id, "event-log")
            self.assertIn("только чтение", app.sub_title)
            self.assertTrue(app.query_one("#event-log-wrap").display)
            self.assertFalse(any(panel.display for panel in app.query(TerminalPanel)))
            await pilot.press("alt+h")
            self.assertEqual(app.focused.id, "event-log")
            await pilot.press("tab")
            self.assertEqual(app.selected_panel, 1)
            await pilot.press("shift+tab")
            self.assertEqual(app.selected_panel, 4)
            await pilot.press("alt+z", "ctrl+shift+up")
            self.assertEqual(app.selected_panel, 2)
            await pilot.press("alt+h")
            await send("\x1b[49;6;33u")
            self.assertEqual(app.selected_panel, 2)
            self.assertIn(
                (
                    "key",
                    app.selected_group,
                    2,
                    "ctrl+exclamation_mark" if associated_text else "ctrl+shift+1",
                ),
                backend.input_ops,
            )

    async def test_group_number_and_arrows_change_group_not_just_highlight(self):
        backend = PopulatedBackend()
        groups = [replace(backend.group, group_id=f"Group-{n}") for n in range(1, 11)]
        backend.list_groups = lambda: groups
        app = HypervisorApp(backend=backend)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+3")
            await pilot.pause()
            self.assertEqual(app.selected_group, "Group-3")
            self.assertEqual(app._current_group.group_id, "Group-3")
            await pilot.press("ctrl+down")
            self.assertEqual(app.selected_group, "Group-4")
            await pilot.press("ctrl+up")
            self.assertEqual(app.selected_group, "Group-3")
            await pilot.press("ctrl+0")
            self.assertEqual(app.selected_group, "Group-10")
            await pilot.press("ctrl+down")
            self.assertEqual(app.selected_group, "Group-1")
            await pilot.press("alt+h", "ctrl+3", "ctrl+down")
            self.assertEqual(app.selected_group, "Group-1")

    async def test_creation_rename_delete_and_modal_editing_are_scoped(self):
        backend = PopulatedBackend()
        app = HypervisorApp(backend=backend)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+n")
            await pilot.pause()
            self.assertIsInstance(app.screen, CreateGroupScreen)
            self.assertIn("Диалог", app.sub_title)
            await pilot.press("tab")
            self.assertEqual(app.focused.id, "cancel")
            await pilot.press("shift+tab")
            self.assertEqual(app.focused.id, "group-id")
            await pilot.press("ctrl+d", "ctrl+n", "ctrl+x", "alt+h", "alt+z")
            self.assertIsInstance(app.screen, CreateGroupScreen)
            self.assertFalse(app.fullscreen_terminal)
            await pilot.press("escape", "ctrl+r")
            await pilot.pause()
            self.assertIsInstance(app.screen, RenameGroupScreen)
            self.assertIsInstance(app.focused, Input)
            await pilot.press("escape", "ctrl+d")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmRemoveScreen)
            self.assertFalse(hasattr(backend, "last_remove"))
            await pilot.press("escape")
            self.assertIs(app.screen, app._main_screen)

    async def test_shifted_digit_encoding_selects_agent_and_quit_only_closes_tui(self):
        backend = PopulatedBackend()
        app = HypervisorApp(backend=backend)
        with patch.object(backend, "deactivate_group", create=True) as deactivate:
            async with app.run_test(size=(140, 48)) as pilot:
                await pilot.pause()
                await pilot.press("ctrl+shift+at")
                self.assertEqual(app.selected_terminal, 2)
                await pilot.press("ctrl+x")
            self.assertFalse(deactivate.called)
            self.assertFalse(app.is_running)


class KeyboardProtocolTests(unittest.TestCase):
    def test_alt_repair_is_idempotent_and_preserves_altgr_and_cached_events(self):
        preserve_alt_with_associated_text()
        parse = getattr(XTermParser, "_parse_extended_key", None)
        preserve_alt_with_associated_text()
        self.assertIs(getattr(XTermParser, "_parse_extended_key", None), parse)
        for sequence, expected in (
            ("\x1b[104;3;104u", "alt+h"),
            ("\x1b[122;3;122u", "alt+z"),
            ("\x1b[27;3;104~", "alt+h"),
            ("\x1b[104;3u", "alt+h"),
            ("\x1bh", "alt+h"),
            ("\x1b[101;3;8364u", "euro_sign"),
            ("\x1b[101;7;8364u", "ctrl+euro_sign"),
            ("\x1b[104;1;104u", "h"),
        ):
            with self.subTest(sequence=sequence):
                if parse is None and sequence.count(";") == 2:
                    continue  # Textual 8.0 does not request associated text.
                for _ in range(2):
                    parser = XTermParser()
                    with patch("textual._parser.get_time", return_value=0):
                        keys = list(parser.feed(sequence))
                    with patch("textual._parser.get_time", return_value=10):
                        keys.extend(parser.tick())
                    self.assertEqual([key.key for key in keys], [expected])
