"""A bounded view of the existing journal; never writes workflow state."""

from __future__ import annotations

import json
from datetime import datetime

from rich.text import Text
from textual.message import Message

from .config import (
    EVENT_LOG_DETAIL_LIMIT,
    EVENT_LOG_FIELDS,
    EVENT_LOG_HUMAN_READABLE,
    EVENT_LOG_MODE_LABELS,
    EVENT_LOG_TITLES,
    EVENT_LOG_INITIAL_EVENTS,
    EVENT_LOG_MAX_LINES,
    EVENT_LOG_STYLES,
    TEXT,
)
from .widgets import SelectableLog


def _readable_details(event: dict, agent_names: dict[str, str], steps: dict[str, dict]) -> str:
    data, kind = event["data"], event["type"]
    details = []
    if kind in {"stage.started", "review.started"}:
        role = "executor" if kind == "stage.started" else "reviewer"
        key = "agent_id" if role == "executor" else "reviewer_id"
        agent = steps.get(event.get("stage", ""), {}).get(key)
        if agent:
            details.append(f"{EVENT_LOG_FIELDS[role]}: {agent_names.get(agent, agent)}")
    for key in (
        "agent_id",
        "request",
        "goal",
        "sources",
        "constraints",
        "branch",
        "summary",
        "message",
        "comment",
        "findings",
        "sha",
        "report",
        "log",
    ):
        value = data.get(key)
        if not value:
            continue
        values = value if isinstance(value, list) else [value]
        for value in values:
            if key == "agent_id":
                value = agent_names.get(value, value)
            elif key == "sha":
                value = str(value)[:12]
            details.append(f"{EVENT_LOG_FIELDS[key]}: {value}")
    if "mode" in data:
        details.append(EVENT_LOG_MODE_LABELS.get(data["mode"], data["mode"]))
    for result in data.get("results", []):
        outcome = (
            TEXT["event_log_check_passed"]
            if result["returncode"] == 0
            else TEXT["event_log_check_failed"].format(code=result["returncode"])
        )
        details.append(f"{result['id']}: {outcome}")
        if result.get("report"):
            details.append(f"{EVENT_LOG_FIELDS['report']}: {result['report']}")
    return "\n".join(details)


def format_event(
    event: dict,
    *,
    human_readable: bool = EVENT_LOG_HUMAN_READABLE,
    agent_names: dict[str, str] | None = None,
    steps: dict[str, dict] | None = None,
) -> Text:
    stamp = event.get("time", "")
    try:
        stamp = datetime.fromisoformat(stamp).astimezone().strftime("%H:%M:%S")
    except (ValueError, TypeError):
        pass
    kind = event["type"]
    line = Text()
    line.append(f"{stamp}  #{event['seq']}  ", style="dim")
    line.append(
        EVENT_LOG_TITLES.get(kind, kind) if human_readable else kind,
        style=EVENT_LOG_STYLES.get(kind, "bold"),
    )
    for key in ("stage", "actor"):
        value = event.get(key)
        if value:
            value = (
                (agent_names or {}).get(value, value)
                if human_readable and key == "actor"
                else value
            )
            line.append(f" · {value}")
    if human_readable and kind in EVENT_LOG_TITLES:
        detail = _readable_details(event, agent_names or {}, steps or {})
    else:
        detail = json.dumps(event, ensure_ascii=False)
    if detail:
        if len(detail) > EVENT_LOG_DETAIL_LIMIT:
            detail = detail[:EVENT_LOG_DETAIL_LIMIT] + "…"
        line.append("\n" + detail)
    return line


class WorkflowEventLog(SelectableLog):
    can_focus = True

    class Selected(Message):
        pass

    def __init__(self, *, human_readable: bool = EVENT_LOG_HUMAN_READABLE):
        super().__init__(
            id="event-log",
            classes="terminal-panel",
            max_lines=EVENT_LOG_MAX_LINES,
            wrap=True,
            highlight=False,
            markup=False,
            auto_scroll=False,
            min_width=1,
        )
        self.human_readable = human_readable
        self._group: str | None = None
        self._last_sequence = 0
        self._placeholder = False
        self._read_error: str | None = None

    def set_format(self, human_readable: bool) -> None:
        if human_readable != self.human_readable:
            self.human_readable = human_readable
            self.clear()
            self._last_sequence = 0
            self._placeholder = False

    def update_events(self, group: str | None, events: list[dict]) -> None:
        switched = group != self._group
        if not switched and self.text_selection is not None:
            return
        self._read_error = None
        reset = switched or (events and events[-1]["seq"] < self._last_sequence)
        if reset:
            self.clear()
            self._last_sequence = 0
            self._group = group
            self._placeholder = False
        fresh = [event for event in events if event["seq"] > self._last_sequence]
        if not fresh:
            if not self.lines:
                self.write(Text(TEXT["event_log_empty"], style="dim"))
                self._placeholder = True
            return
        follow = switched or not self.lines or self._placeholder or self.is_vertical_scroll_end
        follow = follow and self.text_selection is None
        if self._placeholder:
            self.clear()
            self._placeholder = False
        if self._last_sequence == 0:
            fresh = fresh[-EVENT_LOG_INITIAL_EVENTS:]
        task = events[0]["data"] if events and events[0]["type"] == "task.created" else {}
        agent_names = {
            agent["agent_id"]: agent.get("agent_name", agent["agent_id"])
            for agent in task.get("agents", [])
        }
        agent_names.update(hypervisor="Hypervisor", human=TEXT["event_log_actor_human"])
        steps = {step["step_id"]: step for step in task.get("steps", [])}
        for event in fresh:
            self.write(
                format_event(
                    event, human_readable=self.human_readable, agent_names=agent_names, steps=steps
                ),
                scroll_end=follow,
                animate=False,
            )
        self._last_sequence = events[-1]["seq"]

    def show_error(self, group: str, error: Exception) -> None:
        if group != self._group:
            self.update_events(group, [])
        message = str(error)
        if message != self._read_error:
            self.write(Text(TEXT["event_log_unavailable"].format(error=message), style="red"))
            self._read_error = message

    def on_click(self) -> None:
        if not self.screen.get_selected_text():
            self.focus()
            self.post_message(self.Selected())

    def on_focus(self) -> None:
        if self.app.focused is self:
            self.post_message(self.Selected())
