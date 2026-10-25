from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from .config import TERMINAL_COUNT, TERMINAL_LABEL
from .models import HypervisorError
from .task_form import DEFAULT_TASK_FORM, FIELD_TYPES, TaskFieldSpec

AGENT_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
BRANCH_TYPES = ("feature", "fix", "refactor", "docs", "test", "chore")


@dataclass(frozen=True, slots=True)
class AgentSpec:
    agent_id: str
    agent_name: str
    model: str | None = None
    description_path: str = ""
    instructions: str = ""


@dataclass(frozen=True, slots=True)
class CheckSpec:
    check_id: str
    command: tuple[str, ...]
    timeout: int = 300


@dataclass(frozen=True, slots=True)
class FlowStep:
    agent_id: str
    goal: str
    step_id: str = ""
    reviewer_id: str = ""
    review_goal: str = ""
    review_scope: tuple[str, ...] = ()
    checks: tuple[CheckSpec, ...] = ()
    auto_commit: bool = False
    create_pr: bool = False
    human_approval: bool = False


@dataclass(frozen=True, slots=True)
class FlowSpec:
    flow_id: str
    name: str
    steps: tuple[FlowStep, ...]
    task_form: tuple[TaskFieldSpec, ...] | None = None


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    agents: tuple[AgentSpec, ...]
    flow: tuple[FlowStep, ...] = ()
    source: str = ""
    flows: tuple[FlowSpec, ...] = ()
    data_dir: str = ".hypervisor"
    task_form: tuple[TaskFieldSpec, ...] = DEFAULT_TASK_FORM

    def __post_init__(self) -> None:
        if not self.agents:
            raise HypervisorError("At least one agent is required")

    def select_flow(self, flow_id: str | None = None) -> FlowSpec | None:
        if not self.flows:
            if flow_id is not None:
                raise HypervisorError(f"Unknown flow-id: {flow_id}")
            return None
        if flow_id is not None:
            for workflow in self.flows:
                if workflow.flow_id == flow_id:
                    return workflow
            raise HypervisorError(f"Unknown flow-id: {flow_id}")
        if len(self.flows) > 1:
            raise HypervisorError("flow-id is required when config contains multiple flows")
        return self.flows[0]

    def form_for_flow(self, flow_id: str | None = None) -> tuple[TaskFieldSpec, ...]:
        flow = self.select_flow(flow_id)
        return flow.task_form if flow and flow.task_form is not None else self.task_form

    @classmethod
    def default(cls, count: int = TERMINAL_COUNT) -> ProjectConfig:
        return cls(
            tuple(
                AgentSpec(f"terminal-{number}", TERMINAL_LABEL.format(number=number))
                for number in range(1, count + 1)
            )
        )


def _mapping(value: object, label: str, allowed: set[str]) -> dict:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise HypervisorError(f"{label} must be a mapping")
    unknown = value.keys() - allowed
    if unknown:
        raise HypervisorError(f"Unknown fields in {label}: {', '.join(sorted(unknown))}")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise HypervisorError(f"{label} must be a non-empty string")
    return value.strip()


def _optional_string(value: object, label: str) -> str | None:
    return None if value is None else _string(value, label)


def _identifier(value: object, label: str) -> str:
    identifier = _string(value, label)
    if not AGENT_ID.fullmatch(identifier):
        raise HypervisorError(
            f"Invalid {label}: {identifier!r}; use letters, digits, dash or underscore"
        )
    return identifier


def _instructions(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise HypervisorError(f"Cannot read agent description {path}: {error}") from error
    if text.startswith("---\n"):
        match = re.match(r"\A---\n(.*?)\n---[ \t]*(?:\n|\Z)", text, re.DOTALL)
        if match is None:
            raise HypervisorError(f"Unclosed YAML frontmatter in {path}")
        try:
            metadata = yaml.safe_load(match.group(1))
        except yaml.YAMLError as error:
            raise HypervisorError(f"Invalid frontmatter in {path}: {error}") from error
        if metadata is not None and not isinstance(metadata, dict):
            raise HypervisorError(f"Frontmatter in {path} must be a mapping")
        text = text[match.end() :]
    return _string(text, f"Agent description {path}")


def _data_directory(value: object) -> str:
    directory = _string(value, "data-dir")
    path = PurePosixPath(directory)
    if (
        path.is_absolute()
        or re.match(r"^[A-Za-z]:[\\/]", directory)
        or directory.startswith("\\")
        or ".." in path.parts
        or ".git" in path.parts
        or not path.parts
    ):
        raise HypervisorError("data-dir must be a relative path without '..' or '.git'")
    return directory


def _review_scope(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise HypervisorError(f"{label} must be a non-empty list of relative patterns")
    patterns: list[str] = []
    for index, raw in enumerate(value, 1):
        pattern = _string(raw, f"{label}[{index}]")
        path = PurePosixPath(pattern)
        if (
            path.is_absolute()
            or re.match(r"^[A-Za-z]:[\\/]", pattern)
            or ".." in path.parts
            or pattern.startswith(("/", "\\"))
        ):
            raise HypervisorError(f"{label}[{index}] must be a relative pattern without '..'")
        patterns.append(pattern)
    return tuple(patterns)


def _checks(value: object, label: str) -> tuple[CheckSpec, ...]:
    if not isinstance(value, list):
        raise HypervisorError(f"{label} must be a list")
    checks: list[CheckSpec] = []
    ids: set[str] = set()
    for index, raw in enumerate(value, 1):
        item_label = f"{label}[{index}]"
        item = _mapping(raw, item_label, {"id", "command", "timeout"})
        check_id = _identifier(item.get("id"), f"{item_label}.id")
        if check_id in ids:
            raise HypervisorError(f"Duplicate check id in {label}: {check_id}")
        ids.add(check_id)
        command = item.get("command")
        if not isinstance(command, list) or not command:
            raise HypervisorError(f"{item_label}.command must be a non-empty argv list")
        if (
            any(not isinstance(arg, str) or "\0" in arg for arg in command)
            or not command[0].strip()
        ):
            raise HypervisorError(f"{item_label}.command must contain string arguments without NUL")
        argv = tuple(command)
        timeout = item.get("timeout", 300)
        if type(timeout) is not int or timeout <= 0:
            raise HypervisorError(f"{item_label}.timeout must be a positive integer")
        checks.append(CheckSpec(check_id, argv, timeout))
    return tuple(checks)


def _bool(item: dict, key: str, label: str, default: bool = False) -> bool:
    value = item.get(key, default)
    if type(value) is not bool:
        raise HypervisorError(f"{label}.{key} must be true or false")
    return value


def _form_hint(value: object, label: str) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise HypervisorError(f"{label} must be a string")
    return value


def _task_form(
    value: object, base: tuple[TaskFieldSpec, ...] = DEFAULT_TASK_FORM, location: str = "task-form"
) -> tuple[TaskFieldSpec, ...]:
    entries = _mapping(value, location, {field.field_id for field in base})
    fields = []
    for field in base:
        label = f"{location}.{field.field_id}"
        item = _mapping(
            entries.get(field.field_id, {}),
            label,
            {"label", "required", "default", "visible", "type", "description", "placeholder"},
        )
        input_type = item.get("type", field.input_type)
        if not isinstance(input_type, str) or input_type not in FIELD_TYPES:
            raise HypervisorError(f"{label}.type must be text, multiline or string-list")
        if field.field_id != "sources" and input_type == "string-list":
            raise HypervisorError(f"{label}.type string-list is supported only for sources")
        default = item.get("default", field.default)
        if input_type != field.input_type and "default" not in item:
            if input_type == "string-list":
                default = tuple(default.splitlines())
            elif isinstance(default, tuple):
                default = "\n".join(default)
        if input_type == "string-list":
            if not isinstance(default, (list, tuple)):
                raise HypervisorError(f"{label}.default must be a list of source strings")
            default = tuple(_string(source, f"{label}.default") for source in default)
        elif not isinstance(default, str) or "\0" in default:
            raise HypervisorError(f"{label}.default must be a string")
        required = _bool(item, "required", label, field.required)
        visible = _bool(item, "visible", label, field.visible)
        if (
            not visible
            and required
            and not (default.strip() if isinstance(default, str) else default)
        ):
            raise HypervisorError(f"{label}: a hidden required field needs a non-empty default")
        fields.append(
            TaskFieldSpec(
                field.field_id,
                _string(item.get("label", field.label), f"{label}.label"),
                required,
                default,
                visible,
                input_type,
                _form_hint(item.get("description", field.description), f"{label}.description"),
                _form_hint(item.get("placeholder", field.placeholder), f"{label}.placeholder"),
            )
        )
    return tuple(fields)


def _parse_step(
    raw: object,
    label: str,
    index: int,
    agent_ids: set[str],
    *,
    legacy_reviewer: str | None = None,
    legacy_create_pr: bool = False,
    legacy: bool = False,
) -> FlowStep:
    allowed = {"id", "agent-id", "goal"}
    if not legacy:
        allowed |= {
            "reviewer-id",
            "review-goal",
            "review-scope",
            "checks",
            "auto-commit",
            "create-pr",
            "human-approval",
        }
    item = _mapping(raw, label, allowed)
    agent_id = _identifier(item.get("agent-id"), f"{label}.agent-id")
    if agent_id not in agent_ids:
        raise HypervisorError(f"Unknown agent-id in {label}: {agent_id}")
    step_id = _identifier(item.get("id", f"step-{index}"), f"{label}.id")
    if legacy:
        reviewer_id = legacy_reviewer or agent_id
        return FlowStep(
            agent_id,
            _string(item.get("goal"), f"{label}.goal"),
            step_id,
            reviewer_id,
            "Review the stage result against its goal.",
            ("**/*",),
            (),
            False,
            legacy_create_pr,
            False,
        )
    reviewer_id = _identifier(item.get("reviewer-id"), f"{label}.reviewer-id")
    if reviewer_id not in agent_ids:
        raise HypervisorError(f"Unknown reviewer-id in {label}: {reviewer_id}")
    return FlowStep(
        agent_id=agent_id,
        goal=_string(item.get("goal"), f"{label}.goal"),
        step_id=step_id,
        reviewer_id=reviewer_id,
        review_goal=_string(item.get("review-goal"), f"{label}.review-goal"),
        review_scope=_review_scope(item.get("review-scope"), f"{label}.review-scope"),
        checks=_checks(item.get("checks", []), f"{label}.checks"),
        auto_commit=_bool(item, "auto-commit", label),
        create_pr=_bool(item, "create-pr", label),
        human_approval=_bool(item, "human-approval", label),
    )


def load_project_config(
    path: str | Path | None = None, *, cwd: Path | None = None
) -> ProjectConfig:
    base = cwd or Path.cwd()
    requested = path or os.environ.get("HYPERVISOR_CONFIG")
    source = Path(requested).expanduser() if requested else base / "config.yaml"
    if not source.is_absolute():
        source = base / source
    source = source.resolve()
    if not source.exists() and not requested:
        return ProjectConfig.default()
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise HypervisorError(f"Cannot load config {source}: {error}") from error
    root = _mapping(
        payload,
        "config",
        {"version", "agents-dir", "agents", "flow", "flows", "data-dir", "task-form"},
    )
    version = root.get("version", 1)
    if type(version) is not int or version not in (1, 2):
        raise HypervisorError("config.version must be 1 or 2")
    if version == 2 and "flow" in root:
        raise HypervisorError("config.version 2 uses 'flows', not 'flow'")
    directory = None
    if "agents-dir" in root:
        directory = Path(_string(root["agents-dir"], "agents-dir")).expanduser()
        directory = (source.parent / directory).resolve()
        if not directory.is_dir():
            raise HypervisorError(f"Agent directory does not exist: {directory}")
    entries = root.get("agents")
    if not isinstance(entries, list) or not entries:
        raise HypervisorError("agents must be a non-empty list")
    agents: list[AgentSpec] = []
    ids: set[str] = set()
    legacy_agent_options: dict[str, tuple[str | None, bool]] = {}
    for index, entry in enumerate(entries, 1):
        label = f"agents[{index}]"
        allowed = {"agent-id", "agent-name", "model", "instructions"}
        if version == 1:
            allowed |= {"reviewer-id", "create-pr"}
        item = _mapping(entry, label, allowed)
        agent_id = _identifier(item.get("agent-id"), f"{label}.agent-id")
        if agent_id in ids:
            raise HypervisorError(f"Duplicate agent-id: {agent_id}")
        ids.add(agent_id)
        description_path = ""
        if "instructions" in item:
            instructions = _string(item["instructions"], f"{label}.instructions")
        else:
            if directory is None:
                raise HypervisorError(f"{label} requires instructions or agents-dir")
            description = (directory / f"{agent_id}.md").resolve()
            if not description.is_relative_to(directory):
                raise HypervisorError(f"Agent description must be inside {directory}")
            description_path = str(description)
            instructions = _instructions(description)
        legacy_reviewer = _optional_string(item.get("reviewer-id"), f"{label}.reviewer-id")
        create_pr = _bool(item, "create-pr", label) if version == 1 else False
        legacy_agent_options[agent_id] = (legacy_reviewer, create_pr)
        agents.append(
            AgentSpec(
                agent_id,
                _string(item.get("agent-name"), f"{label}.agent-name"),
                _optional_string(item.get("model"), f"{label}.model"),
                description_path,
                instructions,
            )
        )

    if version == 1:
        for agent_id, (reviewer_id, _create_pr) in legacy_agent_options.items():
            if reviewer_id is not None and reviewer_id not in ids:
                raise HypervisorError(f"Unknown reviewer-id {reviewer_id!r} for {agent_id}")

    task_form = _task_form(root.get("task-form", {}))
    flows: list[FlowSpec] = []
    if version == 1:
        legacy_entries = root.get("flow", [])
        if not isinstance(legacy_entries, list):
            raise HypervisorError("flow must be a list")
        migrated: list[FlowStep] = []
        seen_step_ids: set[str] = set()
        for index, entry in enumerate(legacy_entries, 1):
            label = f"flow[{index}]"
            # Resolve old agent-level reviewer settings after all agent IDs are known.
            raw = _mapping(entry, label, {"id", "agent-id", "goal"})
            agent_id = _identifier(raw.get("agent-id"), f"{label}.agent-id")
            reviewer, create_pr = legacy_agent_options.get(agent_id, (None, False))
            reviewer = reviewer or agent_id
            if reviewer not in ids:
                raise HypervisorError(f"Unknown reviewer-id {reviewer!r} for {agent_id}")
            step = _parse_step(
                raw,
                label,
                index,
                ids,
                legacy_reviewer=reviewer,
                legacy_create_pr=create_pr,
                legacy=True,
            )
            if step.step_id in seen_step_ids:
                raise HypervisorError(f"Duplicate flow step id: {step.step_id}")
            seen_step_ids.add(step.step_id)
            migrated.append(step)
        if migrated:
            flows.append(FlowSpec("legacy", "Legacy workflow", tuple(migrated)))
    else:
        raw_flows = root.get("flows")
        if not isinstance(raw_flows, list) or not raw_flows:
            raise HypervisorError("version 2 requires at least one flow")
        flow_ids: set[str] = set()
        for flow_index, raw_flow in enumerate(raw_flows, 1):
            label = f"flows[{flow_index}]"
            item = _mapping(raw_flow, label, {"id", "name", "steps", "task-form"})
            flow_id = _identifier(item.get("id"), f"{label}.id")
            if flow_id in flow_ids:
                raise HypervisorError(f"Duplicate flow id: {flow_id}")
            flow_ids.add(flow_id)
            raw_steps = item.get("steps")
            if not isinstance(raw_steps, list) or not raw_steps:
                raise HypervisorError(f"{label}.steps must be a non-empty list")
            steps: list[FlowStep] = []
            step_ids: set[str] = set()
            for step_index, raw_step in enumerate(raw_steps, 1):
                step_label = f"{label}.steps[{step_index}]"
                step = _parse_step(raw_step, step_label, step_index, ids)
                if step.step_id in step_ids:
                    raise HypervisorError(f"Duplicate step id in {flow_id}: {step.step_id}")
                step_ids.add(step.step_id)
                steps.append(step)
            flows.append(
                FlowSpec(
                    flow_id,
                    _string(item.get("name"), f"{label}.name"),
                    tuple(steps),
                    _task_form(item["task-form"], task_form, f"{label}.task-form")
                    if "task-form" in item
                    else None,
                )
            )

    compatible_flow = tuple(flows[0].steps) if flows else ()
    return ProjectConfig(
        tuple(agents),
        compatible_flow,
        str(source),
        tuple(flows),
        _data_directory(root.get("data-dir", ".hypervisor")),
        task_form,
    )
