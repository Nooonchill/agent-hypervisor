from __future__ import annotations

import unittest

from textual.widgets import Button, Tooltip

from agent_hypervisor.dialogs import SessionPickerScreen
from agent_hypervisor.state import AgentSession
from agent_hypervisor.tui import HypervisorApp
from tests.test_tui import PopulatedBackend


class UiStyleTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_controls_stay_with_the_selected_terminal(self) -> None:
        app = HypervisorApp(backend=PopulatedBackend())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertTrue(app.query_one("#terminal-wrap-1 #start-button-1", Button).display)
            title = app.query_one("#terminal-title-1")
            for control in app.query_one("#terminal-wrap-1").query(Button):
                self.assertEqual(control.region.y, title.region.y)
                self.assertEqual(control.region.height, 1)
                self.assertGreaterEqual(control.region.x, title.region.right)
            self.assertFalse(app.query_one("#terminal-wrap-2 #start-button-2", Button).display)
            app.action_select_terminal(2)
            await pilot.pause()
            self.assertFalse(app.query_one("#terminal-wrap-1 #start-button-1", Button).display)
            self.assertTrue(app.query_one("#terminal-wrap-2 #start-button-2", Button).display)

    async def test_delete_button_and_tooltip_fit_at_sidebar_edge(self) -> None:
        app = HypervisorApp(backend=PopulatedBackend())
        async with app.run_test(size=(120, 40), tooltips=True) as pilot:
            await pilot.pause()
            row = app.query_one("#group-actions")
            for button in row.query(Button):
                self.assertTrue(row.content_region.contains_region(button.region))
            await pilot.hover("#remove-group", offset=(2, 1))
            await pilot.pause(0.8)
            tooltip = app.screen.query_one(Tooltip)
            self.assertTrue(tooltip.display)
            self.assertIn("Delete group", str(tooltip.render()))
            self.assertTrue(app.screen.region.contains_region(tooltip.region))

    async def test_hover_label_background_matches_button_surface(self) -> None:
        app = HypervisorApp(backend=PopulatedBackend())
        app.no_color = False
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(SessionPickerScreen(1, [AgentSession("12345678", 1)]))
            await pilot.pause()
            focused = app.screen.query_one("#new-session", Button)
            self.assertTrue(focused.has_pseudo_class("focus"))
            focused_segments = [
                segment
                for segment in focused.render_line(0)
                if segment.text.strip() and "session" in segment.text.lower()
            ]
            self.assertTrue(focused_segments)
            for segment in focused_segments:
                self.assertEqual(segment.style.bgcolor, focused.rich_style.bgcolor)

            await pilot.hover("#resume-0", offset=(4, 1))
            await pilot.pause()
            for selector in ("#new-session", "#resume-0"):
                button = app.screen.query_one(selector, Button)
                background = button.rich_style.bgcolor
                text_segments = [
                    segment for segment in button.render_line(0) if segment.text.strip()
                ]
                self.assertTrue(text_segments)
                for segment in text_segments:
                    self.assertEqual(segment.style.bgcolor, background)
                    self.assertFalse(segment.style.reverse)
