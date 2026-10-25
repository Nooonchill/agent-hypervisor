from __future__ import annotations

import unittest
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Tooltip

from agent_hypervisor.widgets import WorkflowRoadmap


class RoadmapApp(App):
    CSS_PATH = Path(__file__).parents[1] / "src/agent_hypervisor/styles.tcss"

    def __init__(self):
        super().__init__()
        self.selected = []

    def compose(self) -> ComposeResult:
        yield WorkflowRoadmap(id="workflow-roadmap")

    def on_workflow_roadmap_stage_selected(self, event: WorkflowRoadmap.StageSelected) -> None:
        self.selected.append(event.stage_id)


class RoadmapTests(unittest.IsolatedAsyncioTestCase):
    async def test_path_stretches_and_colours_intervals_without_losing_interaction(self) -> None:
        app = RoadmapApp()
        app.no_color = False
        async with app.run_test(size=(60, 10), tooltips=True) as pilot:
            roadmap = app.query_one(WorkflowRoadmap)
            roadmap.update_stages(
                [
                    {"id": "intent", "state": "completed"},
                    {"id": "implementation", "state": "running"},
                    {"id": "release", "state": "pending", "human_approval": True},
                ]
            )
            await pilot.pause()
            line = roadmap.render()
            self.assertEqual(line.cell_len, 60)
            self.assertIn("━", line.plain)
            self.assertIn("━━  ", line.plain)
            self.assertEqual(line.plain[0], "╋")
            self.assertEqual(line.plain[-1], "◈")
            points = roadmap.boundaries()
            for index, colour in enumerate(((57, 217, 138), (57, 217, 138), (255, 255, 255))):
                middle = (points[index] + points[index + 1]) // 2
                self.assertEqual(
                    line.get_style_at_offset(app.console, middle).color.triplet, colour
                )
            middle = (points[1] + points[2]) // 2
            await pilot.hover(roadmap, offset=(middle, 0))
            await pilot.pause(0.8)
            self.assertIn("implementation", roadmap.tooltip)
            self.assertIn("implementation", str(app.screen.query_one(Tooltip).render()))
            before_click = roadmap.render()
            await pilot.click(roadmap, offset=(middle, 0))
            await pilot.pause()
            self.assertEqual(app.selected[-1], "implementation")
            selected_line = roadmap.render()
            self.assertEqual(selected_line.plain, before_click.plain)
            self.assertFalse(selected_line.get_style_at_offset(app.console, middle).underline)
            await pilot.press("right", "enter")
            await pilot.pause()
            self.assertEqual(app.selected[-1], "release")
            await pilot.hover(roadmap, offset=(59, 0))
            await pilot.pause(0.8)
            self.assertIn("Human approval", roadmap.tooltip)
            self.assertIn("Human approval", str(app.screen.query_one(Tooltip).render()))
            roadmap.tooltip = None
            await pilot.pause()
            for width in (20, 100, 3, 2, 1):
                await pilot.resize_terminal(width, 10)
                await pilot.pause()
                self.assertEqual(roadmap.content_size.width, width)
                self.assertEqual(roadmap.render().cell_len, width)
