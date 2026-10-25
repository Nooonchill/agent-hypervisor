from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .contracts import WorkflowOperations
from .models import HypervisorError
from .workflow import project_events


@dataclass(frozen=True, slots=True)
class AgentBinding:
    group: str
    agent: str
    session: str

    def validate(self, workflow: WorkflowOperations) -> None:
        workflow.validate_agent_session(self.group, self.agent, self.session)


class AgentTools:
    """No caller-supplied group/session IDs and no cached projection of workflow state."""

    def __init__(self, workflow: WorkflowOperations, binding: AgentBinding) -> None:
        self.workflow = workflow
        self.binding = binding

    def _events(self) -> list[dict]:
        self.binding.validate(self.workflow)
        return self.workflow.events(self.binding.group)

    def _authorize(self, step_id: str, attempt: str, role: str) -> None:
        events = self._events()
        if not events:
            raise HypervisorError("This task group has no workflow")
        step = next(
            (item for item in events[0]["data"]["steps"] if item["step_id"] == step_id), None
        )
        event_type = "stage.started" if role == "executor" else "review.started"
        session_key = "executor_session" if role == "executor" else "reviewer_session"
        agent_key = "agent_id" if role == "executor" else "reviewer_id"
        started = next(
            (
                event
                for event in reversed(events)
                if event["type"] == event_type
                and event["stage"] == step_id
                and event["attempt"] == attempt
            ),
            None,
        )
        if (
            not step
            or not started
            or step[agent_key] != self.binding.agent
            or started["data"][session_key] != self.binding.session
        ):
            raise HypervisorError(f"This session is not the {role} for that stage and attempt")

    def workflow_status(self) -> dict[str, object]:
        """Read live group state and this session's assignment. Never starts an agent."""
        events = self._events()
        status = project_events(events)
        assignments = []
        for event in events:
            key = {"stage.started": "executor_session", "review.started": "reviewer_session"}.get(
                event["type"]
            )
            if key and event["data"][key] == self.binding.session:
                assignments.append(
                    {
                        "step_id": event["stage"],
                        "attempt": event["attempt"],
                        "role": "executor" if key == "executor_session" else "reviewer",
                    }
                )
        return {
            "group": self.binding.group,
            "agent_id": self.binding.agent,
            "session_id": self.binding.session,
            "workflow": status,
            "latest_assignment": assignments[-1] if assignments else None,
        }

    def task_context(self) -> dict[str, object]:
        """Read frozen task input and all stage results/reviews from the existing journal."""
        events = self._events()
        task = events[0]["data"] if events else {}
        return {"task": task, "workflow": project_events(events)}

    def workflow_events(self, after_sequence: int = 0, limit: int = 100) -> dict[str, object]:
        """Read ordered history, including prior attempts, feedback and report paths."""
        if after_sequence < 0 or not 1 <= limit <= 200:
            raise HypervisorError("after_sequence must be nonnegative; limit must be 1..200")
        events = [event for event in self._events() if event["seq"] > after_sequence]
        page = events[:limit]
        return {
            "events": page,
            "next_sequence": page[-1]["seq"] if page else after_sequence,
            "has_more": len(events) > limit,
        }

    def read_report(self, path: str) -> str:
        """Read a reports/... path returned by workflow history, confined to this group."""
        self.binding.validate(self.workflow)
        return self.workflow.report(self.binding.group, path)

    def complete_stage(self, step_id: str, attempt: str, summary: str) -> dict[str, object]:
        """Executor only: submit results. Hypervisor runs gates/review and decides transitions.

        Obtain step_id and attempt from workflow_status before working. Reuse those exact
        values when retrying a submission. Include artifact paths in summary.
        """
        self._authorize(step_id, attempt, "executor")
        return self.workflow.complete(
            self.binding.group, step_id, attempt, summary, self.binding.session
        )

    def submit_review(
        self,
        step_id: str,
        attempt: str,
        decision: Literal["approved", "changes_requested", "blocked"],
        summary: str,
        findings: list[str] | None = None,
    ) -> dict[str, object]:
        """Reviewer only: approve or return work with nonempty feedback; never edit files.

        Use the step_id and attempt assigned to this review. Hypervisor enforces review
        integrity and controls retries, human approvals and the next stage.
        """
        self._authorize(step_id, attempt, "reviewer")
        return self.workflow.submit_review(
            self.binding.group, step_id, attempt, decision, summary, findings, self.binding.session
        )
