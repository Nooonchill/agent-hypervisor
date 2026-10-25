from __future__ import annotations

from rich.text import Text
from rich.style import Style
from textual import events
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button

from .config import (
    ROADMAP_ACTIVE_PATTERN,
    ROADMAP_ACTIVE_STATES,
    ROADMAP_GLYPHS,
    TEXT,
    WORKFLOW_STATE_LABELS,
)


class HypervisorButton(Button):
    """Render button content with the widget's full Rich style, including its background."""

    def render(self) -> Text:
        return Text(str(self.label), style=self.rich_style)


class WorkflowRoadmap(Widget):
    """A responsive path: stages are intervals and approval gates are endpoints."""

    can_focus = True
    COMPONENT_CLASSES = {
        "roadmap--waiting",
        "roadmap--completed",
        "roadmap--active",
        "roadmap--selected",
    }

    class StageSelected(Message):
        def __init__(self, stage_id: str) -> None:
            super().__init__()
            self.stage_id = stage_id

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.stages: list[dict] = []
        self.selected_stage = 0

    def update_stages(self, stages: list[dict]) -> None:
        if stages == self.stages:
            return
        self.stages = [dict(stage) for stage in stages]
        self.selected_stage = min(self.selected_stage, max(0, len(stages) - 1))
        self.refresh()

    def boundaries(self) -> list[int]:
        count = len(self.stages)
        width = max(1, self.content_size.width)
        return [round(index * (width - 1) / count) for index in range(count + 1)] if count else []

    def stage_at(self, x: int) -> tuple[int, bool]:
        points = self.boundaries()
        # At a human checkpoint prefer that explicit gate over its adjacent interval.
        gates = [
            index
            for index, stage in enumerate(self.stages)
            if points[index + 1] == x and stage.get("human_approval")
        ]
        if gates:
            index = next(
                (index for index in gates if self.stages[index].get("state") == "awaiting-human"),
                gates[0],
            )
            return index, True
        index = next(
            (index for index in range(len(self.stages)) if x <= points[index + 1]),
            len(self.stages) - 1,
        )
        return max(0, index), False

    def stage_tooltip(self, index: int, approval: bool = False) -> str:
        stage = self.stages[index]
        state = str(stage.get("state", "pending"))
        return TEXT["roadmap_approval_tooltip" if approval else "roadmap_tooltip"].format(
            stage=stage["id"], state=WORKFLOW_STATE_LABELS.get(state, state)
        )

    def render(self) -> Text:
        width = self.content_size.width
        if not self.stages or width < 1:
            return Text(" " * max(0, width))
        points = self.boundaries()
        cells = [ROADMAP_GLYPHS["waiting"]] * width
        styles = [self.get_component_rich_style("roadmap--waiting")] * width
        for index, stage in enumerate(self.stages):
            state = stage.get("state", "pending")
            kind = (
                "completed"
                if state == "completed"
                else "active"
                if state in ROADMAP_ACTIVE_STATES
                else "waiting"
            )
            style = self.get_component_rich_style(f"roadmap--{kind}")
            if self.has_focus and index == self.selected_stage:
                style += Style(bgcolor=self.get_component_rich_style("roadmap--selected").bgcolor)
            for x in range(points[index] + 1, points[index + 1]):
                cells[x] = (
                    ROADMAP_ACTIVE_PATTERN[(x - points[index] - 1) % len(ROADMAP_ACTIVE_PATTERN)]
                    if kind == "active"
                    else ROADMAP_GLYPHS[kind]
                )
                styles[x] = style
        for index, x in enumerate(points):
            stage = self.stages[max(0, index - 1)]
            green = stage.get("state") == "completed" or (
                index == 0 and stage.get("state") in ROADMAP_ACTIVE_STATES
            )
            cells[x] = (
                ROADMAP_GLYPHS["approval"]
                if index and stage.get("human_approval")
                else ROADMAP_GLYPHS["point"]
            )
            styles[x] = self.get_component_rich_style(
                "roadmap--completed" if green else "roadmap--waiting"
            )
        return Text.assemble(*zip(cells, styles))

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self.stages:
            index, approval = self.stage_at(event.x)
            self.tooltip = self.stage_tooltip(index, approval)

    def on_click(self, event: events.Click) -> None:
        if not self.stages:
            return
        event.stop()
        self.focus()
        self.selected_stage, _ = self.stage_at(event.x)
        self.refresh()
        self.post_message(self.StageSelected(str(self.stages[self.selected_stage]["id"])))

    def on_key(self, event: events.Key) -> None:
        if not self.stages or event.key not in {"left", "right", "enter", "space"}:
            return
        event.stop()
        event.prevent_default()
        if event.key in {"left", "right"}:
            self.selected_stage = (self.selected_stage + (1 if event.key == "right" else -1)) % len(
                self.stages
            )
            self.tooltip = self.stage_tooltip(self.selected_stage)
            self.refresh()
        else:
            self.post_message(self.StageSelected(str(self.stages[self.selected_stage]["id"])))
