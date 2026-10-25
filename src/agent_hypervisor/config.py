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
    scope: str = "manager"

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
EVENT_LOG_MAX_LINES = 2_000
EVENT_LOG_INITIAL_EVENTS = 200
EVENT_LOG_DETAIL_LIMIT = 1_200
EVENT_LOG_HUMAN_READABLE = True
EVENT_LOG_FORMAT_ICONS = {True: "{}", False: "≡"}
EVENT_LOG_TITLES = {
    "task.created": "Группа создана",
    "stage.started": "Начата работа над этапом",
    "stage.submitted": "Исполнитель передал результат",
    "stage.completed": "Этап завершён",
    "delivery.started": "Отправляется задание агенту",
    "delivery.completed": "Задание доставлено",
    "delivery.deferred": "Отправка отложена: агент в ручном режиме",
    "checks.passed": "Проверки пройдены",
    "checks.failed": "Проверки не пройдены",
    "commit.started": "Создаётся коммит",
    "commit.created": "Коммит готов для ревью",
    "review.started": "Начато независимое ревью",
    "review.submitted": "Получен результат ревью",
    "review.approved": "Ревьюер подтвердил результат",
    "review.changes_requested": "Ревьюер вернул работу на доработку",
    "review.blocked": "Работа заблокирована: требуется вмешательство",
    "human.requested": "Требуется подтверждение человека",
    "human.approved": "Пользователь подтвердил результат",
    "human.changes_requested": "Пользователь вернул работу на доработку",
    "agent.mode_changed": "Изменён режим агента",
    "workflow.completed": "Workflow завершён",
    "pr.requested": "Для этапа запрошено создание PR",
    "error": "Ошибка workflow",
}
EVENT_LOG_FIELDS = {
    "request": "Запрос",
    "goal": "Общая цель",
    "sources": "Источники",
    "constraints": "Ограничения",
    "summary": "Результат",
    "message": "Сообщение",
    "comment": "Комментарий",
    "findings": "Замечание",
    "report": "Отчёт",
    "sha": "Коммит",
    "branch": "Ветка",
    "log": "Лог ошибки",
    "agent_id": "Агент",
    "executor": "Исполнитель",
    "reviewer": "Ревьюер",
}
EVENT_LOG_MODE_LABELS = {"manual": "ручное управление", "workflow": "workflow"}
EVENT_LOG_STYLES = {
    "error": "red",
    "checks.failed": "red",
    "review.blocked": "red",
    "review.changes_requested": "yellow",
    "human.requested": "yellow",
    "human.changes_requested": "yellow",
    "stage.completed": "green",
    "workflow.completed": "green",
    "review.approved": "green",
}

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
AGENT_MODE_ICONS = {"workflow": "◇", "manual": "◆"}
ROADMAP_GLYPHS = {"waiting": "━", "completed": "━", "point": "╋", "approval": "◈"}
ROADMAP_ACTIVE_PATTERN = "━━  "
ROADMAP_ACTIVE_STATES = frozenset(
    {"running", "checking", "reviewing", "awaiting-human", "waiting-manual", "changes-requested"}
)
WORKFLOW_STATE_LABELS = {
    "pending": "Waiting",
    "running": "Working",
    "checking": "Checking",
    "reviewing": "Reviewing",
    "awaiting-human": "Approval",
    "completed": "Done",
    "changes-requested": "Changes requested",
    "blocked": "Blocked",
    "failed": "Error",
    "waiting-manual": "Manual control",
}

ACTIONS = {
    "toggle_event_log_format": ActionSpec(
        "toggle_event_log_format",
        "",
        "Event format",
        button_id="event-log-format",
        icon=EVENT_LOG_FORMAT_ICONS[EVENT_LOG_HUMAN_READABLE],
        tooltip="Показать технические данные событий",
    ),
    "quit": ActionSpec("quit", "ctrl+x", "Quit", show=True),
    "copy_selection": ActionSpec("copy_selection", "ctrl+shift+c", "Copy", show=True),
    "create_group": ActionSpec(
        "create_group",
        "ctrl+n",
        "New group",
        show=True,
        button_id="create-group",
        icon="+",
        tooltip="Create group",
    ),
    "rename_group": ActionSpec(
        "rename_group",
        "ctrl+r",
        "Rename",
        button_id="rename-group",
        icon="✎",
        tooltip="Rename group",
    ),
    "remove_group": ActionSpec(
        "remove_group",
        "ctrl+d",
        "Delete group",
        button_id="remove-group",
        icon="×",
        tooltip="Delete group",
    ),
    "toggle_fullscreen": ActionSpec(
        "toggle_fullscreen", "alt+z", "Zoom/grid", show=True, scope="global"
    ),
    "toggle_group_active": ActionSpec(
        "toggle_group_active",
        "",
        "Activate/deactivate",
        show=True,
        button_id="activate-group",
        icon="▶",
        tooltip="Activate/deactivate group",
    ),
    "restart_terminal": ActionSpec(
        "restart_terminal",
        "",
        "Start/restart",
        show=True,
        button_id="start-button",
        icon="↻",
        tooltip="Start/restart selected terminal",
    ),
    "stop_terminal": ActionSpec(
        "stop_terminal",
        "",
        "Stop",
        button_id="stop-button",
        icon="■",
        tooltip="Stop selected terminal",
    ),
    "toggle_agent_mode": ActionSpec(
        "toggle_agent_mode",
        "",
        "Manual / workflow",
        button_id="mode-button",
        icon=AGENT_MODE_ICONS["workflow"],
        tooltip="Take manual control of this agent",
    ),
    "workflow_start": ActionSpec(
        "workflow_start",
        "",
        "Start or resume workflow",
        button_id="workflow-start",
        icon="▶",
        tooltip="Start or resume workflow",
    ),
    "previous_group": ActionSpec("previous_group", "ctrl+up", "Previous group"),
    "next_group": ActionSpec("next_group", "ctrl+down", "Next group"),
    "previous_terminal": ActionSpec("previous_terminal", "shift+tab", "Previous panel"),
    "next_terminal": ActionSpec("next_terminal", "tab", "Next panel"),
}

ACTIONS["toggle_terminal_input"] = ActionSpec(
    "toggle_terminal_input", "alt+h", "Input/manage", show=True, scope="global"
)
ACTIONS.update(
    {
        f"move_terminal_{direction}": ActionSpec(
            f"move_terminal('{direction}')", f"ctrl+shift+{direction}", f"Panel {direction}"
        )
        for direction in ("up", "down", "left", "right")
    }
)
for number in range(1, 11):
    digit = str(number % 10)
    shifted = (
        "right_parenthesis",
        "exclamation_mark",
        "at",
        "number_sign",
        "dollar_sign",
        "percent_sign",
        "circumflex_accent",
        "ampersand",
        "asterisk",
        "left_parenthesis",
    )[number % 10]
    ACTIONS[f"select_terminal_{number}"] = ActionSpec(
        f"select_terminal({number})",
        f"ctrl+shift+{digit},ctrl+shift+{shifted},ctrl+{shifted}",
        TERMINAL_LABEL.format(number=number).title(),
    )
    ACTIONS[f"select_group_{number}"] = ActionSpec(
        f"select_group({number})", f"ctrl+{digit}", f"Group {number}"
    )


TEXT = {
    "keyboard_input": "AGENT INPUT · {key} → управление",
    "keyboard_manager": "УПРАВЛЕНИЕ · {key} → ввод агента",
    "keyboard_events": "EVENTS · только чтение",
    "keyboard_dialog": "Диалог · Esc → назад",
    "event_log_human_tooltip": "Показать понятные описания событий",
    "event_log_check_passed": "пройдена",
    "event_log_check_failed": "ошибка (код {code})",
    "event_log_actor_human": "Пользователь",
    "event_log_title": "EVENTS · read only",
    "event_log_empty": "No workflow events yet.",
    "event_log_unavailable": "Cannot read workflow events: {error}",
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
    "terminal_inactive": "Group is inactive.\n\nUse Activate group in the sidebar to launch its Qwen agents.",
    "terminal_stopped": "Stopped by Hypervisor.\n\nUse Start above this terminal.",
    "terminal_exited": "Qwen exited (code {exit_code}).\n\nUse Start above this terminal.",
    "terminal_missing": "Terminal is missing. Select it and press Start.",
    "terminal_is_stopped": "This terminal is stopped. Press Start first.",
    "start_tooltip": "Start selected terminal",
    "restart_tooltip": "Restart selected terminal",
    "deactivate_tooltip": "Deactivate group",
    "activate_tooltip": "Activate group",
    "terminal_start_notification": "{terminal_label}: {action}",
    "inactive_state": "INACTIVE",
    "agent_manual_badge": "MANUAL",
    "agent_manual_tooltip": "Return to workflow · send or clear your draft first",
    "agent_workflow_tooltip": "Take manual control · pause automatic tasks",
    "agent_no_workflow": "No workflow configured for this group",
    "workflow_start": "Start workflow",
    "workflow_resume": "Resume / recover",
    "workflow_resume_help": "If delivery is unclear, check the workflow status with the CLI before continuing.",
    "workflow_running": "Workflow running",
    "workflow_error": "Workflow action failed: {error}",
    "workflow_detail_title": "Stage details · {stage}",
    "workflow_human_title": "Human approval · {stage}",
    "workflow_approve": "Approve",
    "workflow_changes": "Request Changes",
    "workflow_comment": "Comment (required for changes)",
    "workflow_comment_required": "Add a comment before requesting changes.",
    "close": "Close",
    "workflow_no_human": "This stage does not require human approval.",
    "roadmap_tooltip": "{stage} · {state}\nClick for details",
    "roadmap_approval_tooltip": "Human approval · {stage}\n{state} · Click for details",
    "workflow_detail_header": "Workflow: {flow}\nStage: {stage}\nStatus: {state}",
    "workflow_detail_goal": "Goal: {goal}",
    "workflow_detail_result": "Result\n{summary}",
    "workflow_detail_checks": "Hard gates",
    "workflow_detail_check": "{marker} {name} · exit {code}",
    "workflow_detail_review": "Review: {decision}\n{summary}",
    "workflow_detail_commit": "Commit: {commit}",
    "workflow_detail_pr": "PR: {status}",
    "flow_title": "Choose a workflow",
    "flow_help": "Select the flow for this task.",
    "context_title": "Task context",
    "context_help": "Add the initial request and shared context for the workflow.",
    "context_request": "Initial request",
    "context_goal": "Overall goal",
    "context_sources": "Sources",
    "context_constraints": "Additional constraints",
    "context_required": "Fill in the required field: {label}",
    "context_required_label": "{label} *",
    "no_flows": "No workflows are configured.",
}
