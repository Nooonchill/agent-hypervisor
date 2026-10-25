from __future__ import annotations

from dataclasses import dataclass

from .config import TEXT
from .models import HypervisorError


@dataclass(frozen=True, slots=True)
class TaskFieldSpec:
    field_id: str
    label: str
    required: bool = False
    default: str | tuple[str, ...] = ""
    visible: bool = True


DEFAULT_TASK_FORM = (
    TaskFieldSpec("request", TEXT["context_request"]),
    TaskFieldSpec("goal", TEXT["context_goal"]),
    TaskFieldSpec("sources", TEXT["context_sources"], default=()),
    TaskFieldSpec("constraints", TEXT["context_constraints"]),
)


def resolve_task_context(fields: tuple[TaskFieldSpec, ...], values: dict) -> dict:
    """Shared defaults and validation for the form and programmatic creation.

    None means omitted; an explicit empty value may clear an optional default.
    The internal field identifiers do not change when display labels change.
    """
    result = {}
    for field in fields:
        value = values.get(field.field_id)
        if value is None:
            value = field.default
        if field.field_id == "sources":
            if not isinstance(value, (tuple, list)) or any(
                not isinstance(item, str) or "\0" in item for item in value
            ):
                raise HypervisorError("Task sources must be a list of strings")
            value = tuple(item.strip() for item in value if item.strip())
        else:
            if not isinstance(value, str) or "\0" in value:
                raise HypervisorError(f"Task {field.field_id} must be a string")
            value = value.strip()
        if field.required and not value:
            raise HypervisorError(TEXT["context_required"].format(label=field.label))
        result[field.field_id] = value
    return result
