from __future__ import annotations

from dataclasses import dataclass

from .config import TEXT
from .models import HypervisorError

FIELD_TYPES = frozenset({"text", "multiline", "string-list"})


@dataclass(frozen=True, slots=True)
class TaskFieldSpec:
    field_id: str
    label: str
    required: bool = False
    default: str | tuple[str, ...] = ""
    visible: bool = True
    input_type: str = "multiline"
    description: str = ""
    placeholder: str = ""

    def display_value(self) -> str:
        return "\n".join(self.default) if isinstance(self.default, tuple) else self.default


DEFAULT_TASK_FORM = (
    TaskFieldSpec("request", TEXT["context_request"]),
    TaskFieldSpec("goal", TEXT["context_goal"]),
    TaskFieldSpec("sources", TEXT["context_sources"], default=(), input_type="string-list"),
    TaskFieldSpec("constraints", TEXT["context_constraints"]),
)


def resolve_task_context(fields: tuple[TaskFieldSpec, ...], values: dict) -> dict:
    # None selects a default; an explicit empty value clears an optional default.
    result = {}
    for field in fields:
        value = values.get(field.field_id)
        if value is None:
            value = field.default
        if field.input_type == "string-list":
            if isinstance(value, str):
                value = value.splitlines()
            if not isinstance(value, (tuple, list)) or any(
                not isinstance(item, str) or "\0" in item for item in value
            ):
                raise HypervisorError(f"Task {field.field_id} must be a list of strings")
            value = tuple(item.strip() for item in value if item.strip())
        else:
            if field.field_id == "sources" and isinstance(value, (tuple, list)):
                if any(not isinstance(item, str) for item in value):
                    raise HypervisorError("Task sources must contain strings")
                value = "\n".join(value)
            if not isinstance(value, str) or "\0" in value:
                raise HypervisorError(f"Task {field.field_id} must be a string")
            value = value.strip()
        if field.required and not value:
            raise HypervisorError(TEXT["context_required"].format(label=field.label))
        result[field.field_id] = value
    return result
