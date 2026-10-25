from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from agent_hypervisor.event_log import WorkflowEventLog, format_event
from agent_hypervisor.tui import HypervisorApp, TerminalPanel
from tests.test_tui import PopulatedBackend


def event(seq, kind, **data):
    return {
        "seq": seq,
        "time": "2026-10-25T12:00:00+00:00",
        "type": kind,
        "stage": "implement",
        "actor": "developer",
        "attempt": "attempt-1",
        "data": data,
    }


def journal():
    return [
        event(
            1,
            "task.created",
            agents=[{"agent_id": "developer"}],
            steps=[
                {
                    "step_id": "implement",
                    "agent_id": "developer",
                    "goal": "Implement",
                    "human_approval": True,
                }
            ],
            flow_id="flow",
            request="Task context",
            goal="Goal",
            sources=[],
            constraints="",
        )
    ]


def text(log):
    return "\n".join(line.text for line in log.lines)


class EventLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_incremental_events_switching_scrolling_and_read_only_fullscreen(self):
        backend = PopulatedBackend()
        entries = journal() + [
            event(i, "stage.submitted", summary=f"Result {i}") for i in range(2, 45)
        ]
        backend.workflow = SimpleNamespace(status=lambda group: {}, events=lambda group: entries)
        app = HypervisorApp(backend=backend)
        app._event_log_human = False
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            log = app.query_one(WorkflowEventLog)
            wrappers = list(app.query("#terminal-grid > .terminal-wrapper"))
            self.assertEqual(len(wrappers), 4)
            self.assertEqual(wrappers[-1].id, "event-log-wrap")
            self.assertEqual(wrappers[-1].region.width, wrappers[0].region.width)
            self.assertLessEqual(abs(wrappers[-1].region.height - wrappers[0].region.height), 1)
            before = text(log)
            await app.refresh_all()
            await pilot.pause()
            self.assertEqual(text(log), before)
            log.scroll_home(animate=False)
            await pilot.pause()
            self.assertEqual(log.scroll_y, 0)
            entries.append(event(45, "human.requested", message="Approval required"))
            await app.refresh_all()
            await pilot.pause()
            self.assertIn("human.requested", text(log))
            self.assertEqual(log.scroll_y, 0)
            await pilot.click(log, offset=(1, 0))
            await pilot.press("alt+z")
            await pilot.pause()
            self.assertEqual(sum(panel.display for panel in app.query(TerminalPanel)), 0)
            self.assertGreater(log.region.height, 25)
            self.assertFalse(app.check_action("stop_terminal", ()))
            await pilot.press("alt+z")
            await pilot.pause()
            self.assertEqual(sum(panel.display for panel in app.query(TerminalPanel)), 3)
            log.scroll_end(animate=False)
            await pilot.pause()
            self.assertTrue(log.is_vertical_scroll_end)
            entries.append(event(46, "human.approved", comment="Confirmed"))
            await app.refresh_all()
            await pilot.pause()
            self.assertTrue(log.is_vertical_scroll_end)
            entries[:] = journal() + [event(2, "stage.started", executor_session="fresh")]
            backend.group = replace(backend.group, group_id="Another")
            app.selected_group = "Another"
            await app.refresh_all()
            await pilot.pause()
            self.assertIn("fresh", text(log))
            self.assertNotIn("Approval required", text(log))

    async def test_log_selection_uses_same_copy_behavior_as_terminal(self):
        backend = PopulatedBackend()
        entries = journal() + [event(2, "human.requested", message="Human approval required")]
        backend.workflow = SimpleNamespace(status=lambda group: {}, events=lambda group: entries)
        app = HypervisorApp(backend=backend)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            log = app.query_one(WorkflowEventLog)
            log.scroll_home(animate=False)
            with patch("agent_hypervisor.tui.copy_native", return_value=True):
                await pilot.mouse_down(log, offset=(1, 0))
                await pilot.mouse_up(log, offset=(12, 0))
                await pilot.pause()
                self.assertTrue(app.screen.get_selected_text())
                self.assertTrue(app.clipboard)
                self.assertTrue(app._event_log_selected)
                self.assertFalse(app.check_action("stop_terminal", ()))
                original = text(log)
                entries.append(event(3, "workflow.completed"))
                await app.refresh_all()
                self.assertEqual(text(log), original)

    async def test_format_button_rebuilds_view_and_keeps_choice_across_groups(self):
        backend = PopulatedBackend()
        entries = journal() + [event(2, "human.requested")]
        backend.workflow = SimpleNamespace(status=lambda group: {}, events=lambda group: entries)
        app = HypervisorApp(backend=backend)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            log = app.query_one(WorkflowEventLog)
            self.assertIn("Требуется подтверждение человека", text(log))
            self.assertNotIn("human.requested", text(log))
            self.assertEqual(app.query_one("#event-log-format").render_line(0).text.strip(), "{}")
            before = list(entries)
            await pilot.click("#event-log-format")
            await pilot.pause()
            self.assertFalse(log.human_readable)
            self.assertIn("human.requested", text(log))
            self.assertIn('"attempt": "attempt-1"', text(log))
            entries.append(event(3, "stage.completed"))
            await app.refresh_all()
            await pilot.pause()
            self.assertIn("stage.completed", text(log))
            backend.group = replace(
                backend.group, group_id="Other", terminals=backend.group.terminals[:2]
            )
            app.selected_group = "Other"
            await app.refresh_all()
            await pilot.pause()
            log = app.query_one(WorkflowEventLog)
            self.assertFalse(log.human_readable)
            await pilot.click("#event-log-format")
            await pilot.pause()
            self.assertIn("Этап завершён", text(log))
            self.assertNotIn("stage.completed", text(log))
            self.assertEqual(entries[:2], before)
            stable = text(log)
            await app.refresh_all()
            await pilot.pause()
            self.assertEqual(text(log), stable)

    def test_readable_details_show_feedback_and_agent_names_without_session_ids(self):
        item = event(
            4,
            "review.changes_requested",
            summary="Fix acceptance criteria",
            findings=["Missing test"],
            reviewer_session="secret-session",
        )
        readable = format_event(item, agent_names={"developer": "Developer"}).plain
        self.assertIn("Ревьюер вернул работу на доработку", readable)
        self.assertIn("Fix acceptance criteria", readable)
        self.assertIn("Missing test", readable)
        self.assertIn("Developer", readable)
        self.assertNotIn("secret-session", readable)
        raw = format_event(item, human_readable=False).plain
        self.assertIn("secret-session", raw)
        self.assertIn("review.changes_requested", raw)
        unknown = format_event(event(5, "future.event", extra="preserved")).plain
        self.assertIn("future.event", unknown)
        self.assertIn("preserved", unknown)

    def test_format_preserves_event_data_and_does_not_interpret_markup(self):
        formatted = format_event(
            event(3, "error", message="[red]literal[/red]", report="reports/check.log")
        )
        self.assertIn("#3", formatted.plain)
        self.assertIn("[red]literal[/red]", formatted.plain)
        self.assertIn("reports/check.log", formatted.plain)
