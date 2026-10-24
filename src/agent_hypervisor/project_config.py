from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import TERMINAL_COUNT, TERMINAL_LABEL
from .models import HypervisorError

AGENT_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
BRANCH_TYPES = ("feature", "fix", "refactor", "docs", "test", "chore")


@dataclass(frozen=True, slots=True)
class AgentSpec:
    agent_id: str
    agent_name: str
    model: str | None = None
    reviewer_id: str | None = None
    create_pr: bool = False
    description_path: str = ""
    instructions: str = ""


@dataclass(frozen=True, slots=True)
class FlowStep:
    agent_id: str
    goal: str
    step_id: str = ""


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    agents: tuple[AgentSpec, ...]
    flow: tuple[FlowStep, ...] = ()
    source: str = ""

    def __post_init__(self) -> None:
        if not self.agents:
            raise HypervisorError("At least one agent is required")

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
    if not isinstance(value, str) or not value.strip():
        raise HypervisorError(f"{label} must be a non-empty string")
    return value.strip()


def _optional_string(value: object, label: str) -> str | None:
    return None if value is None else _string(value, label)


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
    root = _mapping(payload, "config", {"version", "agents-dir", "agents", "flow"})
    if type(root.get("version", 1)) is not int or root.get("version", 1) != 1:
        raise HypervisorError("config.version must be 1")
    directory = Path(_string(root.get("agents-dir"), "agents-dir")).expanduser()
    directory = (source.parent / directory).resolve()
    if not directory.is_dir():
        raise HypervisorError(f"Agent directory does not exist: {directory}")
    entries = root.get("agents")
    if not isinstance(entries, list) or not entries:
        raise HypervisorError("agents must be a non-empty list")
    agents: list[AgentSpec] = []
    ids: set[str] = set()
    for index, entry in enumerate(entries, 1):
        label = f"agents[{index}]"
        item = _mapping(
            entry, label, {"agent-id", "agent-name", "model", "reviewer-id", "create-pr"}
        )
        agent_id = _string(item.get("agent-id"), f"{label}.agent-id")
        if not AGENT_ID.fullmatch(agent_id):
            raise HypervisorError(
                f"Invalid agent-id: {agent_id!r}; use letters, digits, dash or underscore"
            )
        if agent_id in ids:
            raise HypervisorError(f"Duplicate agent-id: {agent_id}")
        ids.add(agent_id)
        create_pr = item.get("create-pr", False)
        if type(create_pr) is not bool:
            raise HypervisorError(f"{label}.create-pr must be true or false")
        description = (directory / f"{agent_id}.md").resolve()
        if not description.is_relative_to(directory):
            raise HypervisorError(f"Agent description must be inside {directory}")
        agents.append(
            AgentSpec(
                agent_id,
                _string(item.get("agent-name"), f"{label}.agent-name"),
                _optional_string(item.get("model"), f"{label}.model"),
                _optional_string(item.get("reviewer-id"), f"{label}.reviewer-id"),
                create_pr,
                str(description),
                _instructions(description),
            )
        )
    for agent in agents:
        if agent.reviewer_id is not None and agent.reviewer_id not in ids:
            raise HypervisorError(f"Unknown reviewer-id {agent.reviewer_id!r} for {agent.agent_id}")
    entries = root.get("flow", [])
    if not isinstance(entries, list):
        raise HypervisorError("flow must be a list")
    flow: list[FlowStep] = []
    step_ids: set[str] = set()
    for index, entry in enumerate(entries, 1):
        item = _mapping(entry, f"flow[{index}]", {"id", "agent-id", "goal"})
        agent_id = _string(item.get("agent-id"), f"flow[{index}].agent-id")
        if agent_id not in ids:
            raise HypervisorError(f"Unknown agent-id in flow[{index}]: {agent_id}")
        step_id = _string(item.get("id", f"step-{index}"), f"flow[{index}].id")
        if step_id in step_ids:
            raise HypervisorError(f"Duplicate flow step id: {step_id}")
        step_ids.add(step_id)
        flow.append(FlowStep(agent_id, _string(item.get("goal"), f"flow[{index}].goal"), step_id))
    return ProjectConfig(tuple(agents), tuple(flow), str(source))
