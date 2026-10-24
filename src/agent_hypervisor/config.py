from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ActionSpec:
    action: str
    keys: str
    title: str
    show: bool = False
    priority: bool = True
    button_id: str | None = None
    icon: str = ""
    tooltip: str = ""

    def tooltip_for(self, label: str | None = None) -> str:
        text = label if label is not None else self.tooltip
        if not text or not self.keys:
            return text
        keys = self.keys.upper().replace(",", " / ")
        for source, replacement in (("CTRL", "Ctrl"), ("SHIFT", "Shift"), ("ALT", "Alt")):
            keys = keys.replace(source, replacement)
        return f"{text} · {keys}"


TERMINAL_COUNT = 3
TERMINAL_LABEL = "TERMINAL {number}"
SESSION_LABEL = "terminal-{terminal}-{session_id}"
AGENT_SESSION_LABEL = "{agent_id}-{session_id}"
TERMINAL_RENDER_LINES = 2_000

STATE_REFRESH_SECONDS = 1.0
OUTPUT_REFRESH_SECONDS = 0.10
BACKGROUND_REFRESH_DIVISOR = 4
INPUT_BATCH_SECONDS = 0.012
CAPTURE_HISTORY_LINES = 0

GROUP_MARKERS = {"running": "●", "partial": "◐", "stopped": "○", "inactive": "◌"}
TERMINAL_STATE_MARKERS = {"running": "●", "stopped": "○", "exited": "×", "missing": "?"}
TERMINAL_ACTION_ICONS = {"running": "↻", "stopped": "▶", "exited": "▶", "missing": "▶"}
TERMINAL_STATE_LABELS = {
    "running": "RUNNING",
    "stopped": "STOPPED",
    "exited": "EXITED",
    "missing": "MISSING",
}
ACTIVE_GROUP_ICON = "■"

ACTIONS = {
    "quit": ActionSpec("quit", "f12", "Quit", show=True),
    "copy_selection": ActionSpec("copy_selection", "f11,ctrl+shift+c", "Copy", show=True),
    "create_group": ActionSpec(
        "create_group",
        "f9",
        "New group",
        show=True,
        button_id="create-group",
        icon="+",
        tooltip="Create group",
    ),
    "rename_group": ActionSpec(
        "rename_group", "f10", "Rename", button_id="rename-group", icon="✎", tooltip="Rename group"
    ),
    "remove_group": ActionSpec(
        "remove_group",
        "",
        "Delete group",
        button_id="remove-group",
        icon="×",
        tooltip="Delete group",
    ),
    "toggle_fullscreen": ActionSpec("toggle_fullscreen", "f5", "Focus/grid", show=True),
    "toggle_group_active": ActionSpec(
        "toggle_group_active",
        "f6",
        "Activate/deactivate",
        show=True,
        button_id="activate-group",
        icon="▶",
        tooltip="Activate/deactivate group",
    ),
    "restart_terminal": ActionSpec(
        "restart_terminal",
        "f7",
        "Start/restart",
        show=True,
        button_id="start-button",
        icon="↻",
        tooltip="Start/restart selected terminal",
    ),
    "stop_terminal": ActionSpec(
        "stop_terminal",
        "f8",
        "Stop",
        button_id="stop-button",
        icon="■",
        tooltip="Stop selected terminal",
    ),
    "previous_group": ActionSpec("previous_group", "alt+up", "Previous group"),
    "next_group": ActionSpec("next_group", "alt+down", "Next group"),
    "previous_terminal": ActionSpec("previous_terminal", "f3", "Previous agent"),
    "next_terminal": ActionSpec("next_terminal", "f4", "Next agent"),
}

ACTIONS.update(
    {
        f"select_terminal_{number}": ActionSpec(
            f"select_terminal({number})",
            f"alt+{number}",
            TERMINAL_LABEL.format(number=number).title(),
        )
        for number in range(1, 10)
    }
)

TEXT = {
    "app_title": "Agent Hypervisor",
    "app_subtitle": "tmux control plane",
    "brand": "AGENT\nHYPERVISOR",
    "groups_label": "TASK GROUPS",
    "no_group": "No task group selected",
    "empty": "Create a task group, then activate it when needed.",
    "create_title": "Task code",
    "create_help": "Enter the task code, then choose a branch type. Agents stay off until activation.",
    "group_id_placeholder": "Task code, for example PROJ-123",
    "branch_title": "Branch for {task_code}",
    "branch_help": "Choose a type, or enter your own: <type>/<task code>",
    "branch_custom": "Custom branch type",
    "cancel": "Cancel",
    "create": "Create",
    "rename": "Rename",
    "rename_help": "The existing worktree path and branch are kept.",
    "rename_title": "Rename {group_id}",
    "remove_title": "Remove task group {group_id}?",
    "remove_help": "All agent terminals in this group will be stopped.",
    "continue": "Continue",
    "worktree_title": "What should happen to the worktree?",
    "worktree_help": "Removing is allowed only when it has no uncommitted changes. The Git branch is always kept.",
    "keep_worktree": "Keep worktree",
    "remove_worktree": "Remove worktree",
    "session_title": "Start {terminal_label}",
    "session_help": "Resume a Qwen session previously launched here, or start a new one.",
    "new_session": "＋  New session",
    "session_timestamp": "{name}  ·  {created}",
    "group_title": "TASK GROUP  ·  {group_id}",
    "group_state": "{state}",
    "activate_group": "Activate group",
    "terminal_inactive": "Group is inactive.\n\nPress Activate group ({activate_key}) to launch its Qwen agents.",
    "terminal_stopped": "Stopped by Hypervisor.\n\nClick this terminal, then press Start below (or press {start_key}).",
    "terminal_exited": "Qwen exited (code {exit_code}).\n\nClick this terminal, then press Start below (or press {start_key}).",
    "terminal_missing": "Terminal is missing. Select it and press Start.",
    "terminal_is_stopped": "This terminal is stopped. Press Start first.",
    "start_tooltip": "Start selected terminal",
    "restart_tooltip": "Restart selected terminal",
    "deactivate_tooltip": "Deactivate group",
    "activate_tooltip": "Activate group",
    "start_key": ACTIONS["restart_terminal"].keys.upper(),
    "activate_key": ACTIONS["toggle_group_active"].keys.upper(),
    "terminal_start_notification": "{terminal_label}: {action}",
    "inactive_state": "INACTIVE",
}
