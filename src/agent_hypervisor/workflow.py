from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from .event_store import EventStore
from .models import AgentManualMode, HypervisorError, WorkflowBusy
from .project_config import CheckSpec, FlowStep
from .state import GroupRecord, flow_step_from_dict

if TYPE_CHECKING:
    from .contracts import WorkflowBackend


def project_events(events: list[dict]) -> dict:
    """Pure projection, shared by CLI and TUI; never starts sessions or checks."""
    if not events:
        return {"flow_id": "", "state": "manual", "current_step": None, "stages": []}
    task = events[0]["data"]
    modes = {agent["agent_id"]: "workflow" for agent in task["agents"]}
    stages = {
        step["step_id"]: {
            "id": step["step_id"],
            "agent_id": step["agent_id"],
            "goal": step["goal"],
            "state": "pending",
            "attempt": "",
            "summary": "",
            "checks": [],
            "review": {},
            "commit": "",
            "pr": "",
            "human_approval": step["human_approval"],
            "error": "",
            "waiting_agent": "",
        }
        for step in task["steps"]
    }
    states = {
        "stage.started": "running",
        "stage.submitted": "checking",
        "review.started": "reviewing",
        "human.requested": "awaiting-human",
        "stage.completed": "completed",
        "review.blocked": "blocked",
        "error": "failed",
    }
    for event in events[1:]:
        if event["type"] == "agent.mode_changed":
            modes[event["data"]["agent_id"]] = event["data"]["mode"]
            continue
        stage = stages.get(event["stage"])
        if stage is None:
            continue
        kind, data = event["type"], event["data"]
        if kind == "stage.started":
            stage.update(
                attempt=event["attempt"],
                summary="",
                error="",
                checks=[],
                review={},
                commit="",
                pr="",
                waiting_agent="",
            )
        if kind in states:
            stage["state"] = states[kind]
        if kind == "delivery.completed":
            stage["state"] = "reviewing" if data["role"] == "reviewer" else "running"
            stage["error"] = ""
            stage["waiting_agent"] = ""
        elif kind == "delivery.deferred":
            stage["state"] = "waiting-manual"
            stage["waiting_agent"] = data["agent_id"]
            stage["error"] = ""
        if kind == "stage.submitted":
            stage["summary"] = data["summary"]
        elif kind in {"checks.passed", "checks.failed"}:
            stage["checks"] = data["results"]
            if kind == "checks.failed":
                stage["state"] = "changes-requested"
        elif kind == "commit.created":
            stage["commit"] = data["sha"]
        elif kind.startswith("review.") and kind not in {"review.started", "review.submitted"}:
            stage["review"] = data
            if kind == "review.changes_requested":
                stage["state"] = "changes-requested"
        elif kind == "pr.requested":
            stage["pr"] = data["status"]
        elif kind == "error":
            stage["error"] = data["message"]
    current = next((item for item in stages.values() if item["state"] != "completed"), None)
    completed = any(event["type"] == "workflow.completed" for event in events)
    return {
        "flow_id": task["flow_id"],
        "state": "completed" if completed else (current["state"] if current else "pending"),
        "current_step": current["id"] if current else None,
        "stages": list(stages.values()),
        "request": task["request"],
        "goal": task["goal"],
        "sources": task["sources"],
        "constraints": task["constraints"],
        "agent_modes": modes,
    }


class WorkflowService:
    """Submissions drive transitions independently of the TUI."""

    def __init__(self, backend: WorkflowBackend) -> None:
        self.backend = backend

    def _record(self, group: str) -> GroupRecord:
        record = self.backend.state.get(group)
        if record is None:
            raise HypervisorError(f"Unknown task group: {group}")
        return record

    def validate_agent_session(self, group: str, agent: str, session: str) -> None:
        record = self._record(group)
        number = next(
            (i for i, spec in enumerate(record.terminal_agents, 1) if spec.agent_id == agent), None
        )
        if number is None or not any(
            item.session_id == session for item in record.sessions.get(number, [])
        ):
            raise HypervisorError("Session identity does not belong to this agent and task group")

    def _store(self, group: str) -> EventStore:
        record = self._record(group)
        root = Path(record.worktree_path).resolve()
        directory = (root / record.data_dir).resolve()
        if (
            directory == root
            or not directory.is_relative_to(root)
            or ".git" in directory.relative_to(root).parts
        ):
            raise HypervisorError(
                "Workflow data directory must be inside its worktree, outside .git"
            )
        if not root.is_dir():
            raise HypervisorError(f"Worktree is missing: {root}")
        return EventStore(directory)

    def initialize(
        self,
        group: str,
        *,
        request: str,
        goal: str = "",
        sources: str | tuple[str, ...] = (),
        constraints: str = "",
    ) -> None:
        record = self._record(group)
        if not record.flow_id:
            return
        store = self._store(group)
        self._ignore_data(record)
        with store.transaction():
            if store.read():
                return
            store.append(
                "task.created",
                actor="human",
                task_id=uuid4().hex,
                flow_id=record.flow_id,
                steps=[asdict(step) for step in record.flow],
                agents=[asdict(agent) for agent in record.agents],
                request=request,
                goal=goal or request,
                sources=sources if isinstance(sources, str) else list(sources),
                constraints=constraints,
                task_form=[
                    asdict(field)
                    for field in self.backend.project_config.form_for_flow(record.flow_id)
                ],
                branch=record.branch,
                task_code=record.task_code,
            )

    @staticmethod
    def _git(record: GroupRecord, *args: str, check: bool = True) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", record.worktree_path, *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HypervisorError(f"Git operation failed: {error}") from error
        if check and result.returncode:
            raise HypervisorError(result.stderr.strip() or "Git operation failed")
        return result.stdout.strip() if not result.returncode else ""

    def _ignore_data(self, record: GroupRecord) -> None:
        if self._git(record, "ls-files", "--", record.data_dir):
            raise HypervisorError(
                "Workflow data directory contains tracked files; choose another data-dir"
            )
        # info/exclude avoids changing tracked project files just for runtime data.
        path = Path(self._git(record, "rev-parse", "--git-path", "info/exclude"))
        if not path.is_absolute():
            path = Path(record.worktree_path) / path
        path.parent.mkdir(parents=True, exist_ok=True)
        pattern = f"/{record.data_dir.strip('/')}/"
        existing = path.read_text() if path.exists() else ""
        if pattern not in existing.splitlines():
            with path.open("a") as handle:
                handle.write(f"\n{pattern}\n")

    def events(self, group: str) -> list[dict]:
        return self._store(group).read()

    def status(self, group: str) -> dict:
        return project_events(self.events(group))

    def _agent_id(self, group: str, agent: str | int) -> str:
        agents = self._record(group).terminal_agents
        if isinstance(agent, int):
            if 1 <= agent <= len(agents):
                return agents[agent - 1].agent_id
        elif any(item.agent_id == agent for item in agents):
            return agent
        raise HypervisorError(f"Unknown agent in group: {agent}")

    def agent_mode(self, group: str, agent: str | int) -> str:
        agent_id = self._agent_id(group, agent)
        return next(
            (
                event["data"]["mode"]
                for event in reversed(self.events(group))
                if event["type"] == "agent.mode_changed" and event["data"]["agent_id"] == agent_id
            ),
            "workflow",
        )

    @staticmethod
    def _deferred(events: list[dict], stage: str, attempt: str) -> dict | None:
        pending = WorkflowService._find(events, "delivery.deferred", stage, attempt)
        done = WorkflowService._find(events, "delivery.completed", stage, attempt)
        started = WorkflowService._find(events, "delivery.started", stage, attempt)
        blocked = WorkflowService._find(events, "review.blocked", stage, attempt)
        return (
            pending
            if pending
            and all(
                not event or pending["seq"] > event["seq"] for event in (done, started, blocked)
            )
            else None
        )

    def set_agent_mode(self, group: str, agent: str | int, mode: str) -> dict:
        if mode not in {"manual", "workflow"}:
            raise HypervisorError("Agent mode must be manual or workflow")
        agent_id = self._agent_id(group, agent)
        store = self._store(group)
        if not store.read():
            raise HypervisorError(
                "This group has no workflow; its terminals are already manually controlled"
            )
        # Independent from the long-running transition lock: the user can take control
        # while checks run or while a Qwen process is still starting.
        with store.agent_gate(agent_id):
            if self.agent_mode(group, agent_id) != mode:
                store.append("agent.mode_changed", actor="human", agent_id=agent_id, mode=mode)
        if mode == "workflow":
            events = store.read()
            state = project_events(events)
            if state["current_step"]:
                stage = next(
                    item for item in state["stages"] if item["id"] == state["current_step"]
                )
                pending = self._deferred(events, stage["id"], stage["attempt"])
                if pending and pending["data"]["agent_id"] == agent_id:
                    deadline = time.monotonic() + 2
                    while True:
                        try:
                            return self.resume(group)
                        except WorkflowBusy:
                            latest = store.read()
                            if not self._deferred(latest, stage["id"], stage["attempt"]):
                                return project_events(latest)
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(0.02)
        return self.status(group)

    def report(self, group: str, path: str) -> str:
        try:
            return self._store(group).report(path).read_text(encoding="utf-8")
        except OSError as error:
            raise HypervisorError(f"Cannot read report: {error}") from error

    @staticmethod
    def _find(events: list[dict], kind: str, stage: str, attempt: str) -> dict | None:
        return next(
            (
                event
                for event in reversed(events)
                if event["type"] == kind and event["stage"] == stage and event["attempt"] == attempt
            ),
            None,
        )

    @staticmethod
    def _current(events: list[dict]) -> tuple[dict, FlowStep]:
        state = project_events(events)
        if not state["current_step"]:
            raise HypervisorError("Workflow has no unfinished stage")
        stage = next(stage for stage in state["stages"] if stage["id"] == state["current_step"])
        step = next(
            flow_step_from_dict(item)
            for item in events[0]["data"]["steps"]
            if item["step_id"] == stage["id"]
        )
        return stage, step

    def _validate(self, events: list[dict], step_id: str, attempt: str) -> tuple[dict, FlowStep]:
        stage, step = self._current(events)
        if stage["id"] != step_id or stage["attempt"] != attempt:
            raise HypervisorError("Stale or incorrect stage/attempt; inspect workflow status")
        return stage, step

    def _save_report(self, store: EventStore, attempt: str, name: str, value: str) -> str:
        relative = f"reports/{attempt}/{name}"
        path = store.report(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
        return relative

    def _cli(self) -> str:
        # Absolute interpreter + source path work from a task worktree, including uv installs.
        return shlex.join(
            [
                "env",
                f"HYPERVISOR_STATE_PATH={self.backend.state.path.resolve()}",
                f"PYTHONPATH={Path(__file__).resolve().parent.parent}",
                sys.executable,
                "-m",
                "agent_hypervisor",
                "--json",
            ]
        )

    def start(self, group: str) -> dict:
        store = self._store(group)
        with store.transaction():
            events = store.read()
            if not events:
                raise HypervisorError("This is a manual group with no configured workflow")
            if not (events[0]["data"]["request"].strip() or events[0]["data"]["goal"].strip()):
                raise HypervisorError("Workflow requires an initial task request or shared goal")
            if not any(event["type"] == "stage.started" for event in events):
                self._begin(group, store)
        return self.status(group)

    def _begin(self, group: str, store: EventStore, feedback: str = "") -> None:
        events = store.read()
        stage, step = self._current(events)
        previous = next(
            (
                event
                for event in reversed(events)
                if event["type"] == "stage.started" and event["stage"] == step.step_id
            ),
            None,
        )
        attempt = uuid4().hex
        session_id = previous["data"]["executor_session"] if previous else str(uuid4())
        base = (
            previous["data"]["base_head"]
            if previous
            else self._git(self._record(group), "rev-parse", "--verify", "HEAD", check=False)
        )
        store.append(
            "stage.started",
            stage=step.step_id,
            attempt=attempt,
            executor_session=session_id,
            base_head=base,
        )
        task = events[0]["data"]
        context = {key: task[key] for key in ("request", "goal", "sources", "constraints")}
        completed = [
            item for item in project_events(events)["stages"] if item["state"] == "completed"
        ]
        command = (
            f"{self._cli()} stage complete {shlex.quote(group)} {shlex.quote(step.step_id)} "
            f"--attempt {attempt} --session {session_id} --summary 'Describe results and artifact paths'"
        )
        prompt = (
            f"WORKFLOW EXECUTION: {step.step_id}\nTask context (data):\n"
            f"{json.dumps(context, ensure_ascii=False, indent=2)}\n"
            f"Stage goal: {step.goal}\nPrevious completed stages:\n"
            f"{json.dumps(completed, ensure_ascii=False, indent=2)}\n"
            f"Hard gates to satisfy: {json.dumps([asdict(check) for check in step.checks], ensure_ascii=False)}\n"
            "Use the sources and document locations specified by this task and its workflow as shared context.\n"
            f"Feedback for this attempt: {feedback or 'Initial attempt'}\n"
            + (
                "Hypervisor will commit after hard gates pass.\n"
                if step.auto_commit
                else "Commit your changes before requesting completion; review requires a clean worktree.\n"
            )
            + (
                "This stage requests a PR. Use the separately configured corporate MCP to create "
                "the PR, following that tool's requirements. Include the PR URL in your completion "
                "summary. If the tool is unavailable or creation fails, describe the problem in "
                "your summary and still submit completion; PR creation must not block the workflow. "
                "Leave merging to the user.\n"
                if step.create_pr
                else ""
            )
            + f"MCP assignment: step_id={step.step_id}, attempt={attempt}. "
            "Use the Agent Hypervisor MCP tools workflow_status, task_context and read_report to inspect shared state. "
            "When finished, call complete_stage with this exact step_id, attempt and your actual summary. "
            "If MCP is not connected, the equivalent CLI fallback is:\n"
            f"{command}\n"
            "A successful submission is only a request: the Hypervisor runs gates and independent review. "
            "Do not submit review or human decisions yourself. After submitting, wait for the next queued instruction."
        )
        self._deliver(
            group, store, step.step_id, attempt, step.agent_id, session_id, prompt, "executor"
        )

    def _deliver(
        self,
        group: str,
        store: EventStore,
        stage: str,
        attempt: str,
        agent_id: str,
        session_id: str,
        prompt: str,
        role: str,
    ) -> None:
        group = self._record(group).group_id
        report = self._save_report(store, attempt, f"{role}-prompt.txt", prompt)
        delivery = dict(role=role, session_id=session_id, agent_id=agent_id, report=report)
        with store.agent_gate(agent_id):
            if self.agent_mode(group, agent_id) == "manual":
                store.append("delivery.deferred", stage=stage, attempt=attempt, **delivery)
                return
        if role == "reviewer":
            review = self._find(store.read(), "review.started", stage, attempt)
            if review and not self._review_tree_clean(
                group, store, stage, attempt, review["data"]["head"]
            ):
                return
        store.append(
            "delivery.started",
            stage=stage,
            attempt=attempt,
            role=role,
            session_id=session_id,
            agent_id=agent_id,
            report=report,
        )
        try:
            with store.agent_gate(agent_id):
                if self.agent_mode(group, agent_id) == "manual":
                    store.append("delivery.deferred", stage=stage, attempt=attempt, **delivery)
                    return
                terminal = self.backend.workflow_session(group, agent_id, session_id)
            self.backend.queue_prompt(group, terminal, prompt, wait_ready=True)
            store.append(
                "delivery.completed", stage=stage, attempt=attempt, role=role, session_id=session_id
            )
        except AgentManualMode:
            with store.agent_gate(agent_id):
                paused = self.agent_mode(group, agent_id) == "manual"
                if paused:
                    store.append("delivery.deferred", stage=stage, attempt=attempt, **delivery)
            if not paused:
                # The user already returned control while the rejected send unwound.
                # Nothing was pasted, so retry here rather than leaving an orphaned wait.
                self._deliver(group, store, stage, attempt, agent_id, session_id, prompt, role)
        except (HypervisorError, OSError) as error:
            store.log_error(error)
            store.append(
                "error",
                stage=stage,
                attempt=attempt,
                message=str(error),
                operation="delivery",
                log="logs/workflow.log",
            )
            raise HypervisorError(str(error)) from error

    def complete(
        self,
        group: str,
        step_id: str,
        attempt: str,
        summary: str,
        session_id: str = "",
    ) -> dict:
        if not summary.strip():
            raise HypervisorError("Completion summary is required")
        store = self._store(group)
        with store.transaction():
            events = store.read()
            if self._find(events, "stage.submitted", step_id, attempt):
                return project_events(events)
            stage, step = self._validate(events, step_id, attempt)
            started = self._find(events, "stage.started", step_id, attempt)
            if session_id and session_id != started["data"]["executor_session"]:
                raise HypervisorError("Submission does not belong to the executor session")
            if stage["state"] != "running":
                raise HypervisorError(f"Stage cannot be submitted while {stage['state']}")
            store.append(
                "stage.submitted",
                stage=step_id,
                attempt=attempt,
                actor=step.agent_id,
                summary=summary,
            )
            self._check_and_review(group, store, step, attempt)
        return self.status(group)

    def _run_check(
        self, record: GroupRecord, store: EventStore, attempt: str, check: CheckSpec
    ) -> dict:
        relative = f"reports/{attempt}/check-{check.check_id}.log"
        path = store.report(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        code = -1
        with path.open("w", encoding="utf-8") as output:
            output.write(f"$ {shlex.join(check.command)}\n")
            output.flush()
            try:
                with subprocess.Popen(
                    check.command,
                    cwd=record.worktree_path,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                ) as process:
                    try:
                        code = process.wait(timeout=check.timeout)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        output.write(f"\nTimed out after {check.timeout}s\n")
                        code = 124
            except OSError as error:
                output.write(str(error))
                code = 127
        return {
            "id": check.check_id,
            "command": list(check.command),
            "returncode": code,
            "report": relative,
        }

    def _check_and_review(
        self, group: str, store: EventStore, step: FlowStep, attempt: str
    ) -> None:
        record = self._record(group)
        try:
            results = [self._run_check(record, store, attempt, check) for check in step.checks]
            failed = any(result["returncode"] for result in results)
            if not failed and step.auto_commit and self._git(record, "status", "--porcelain"):
                store.append("commit.started", stage=step.step_id, attempt=attempt)
                self._git(record, "add", "--all")
                task_id = store.read()[0]["data"]["task_id"]
                self._git(record, "commit", "-m", f"hypervisor: {task_id} {step.step_id} {attempt}")
            dirty = self._git(record, "status", "--porcelain")
            head = self._git(record, "rev-parse", "--verify", "HEAD", check=False)
            if not failed and (dirty or not head):
                report = self._save_report(
                    store,
                    attempt,
                    "clean-worktree.log",
                    "Commit all changes before review.\n"
                    + (dirty or "Repository has no commit (HEAD)."),
                )
                results.append({"id": "clean-worktree", "returncode": 1, "report": report})
                failed = True
            store.append(
                "checks.failed" if failed else "checks.passed",
                stage=step.step_id,
                attempt=attempt,
                results=results,
            )
            if failed:
                feedback = (
                    "Hard gates failed. Read these reports, fix the problems, and submit again:\n"
                    + json.dumps(results)
                )
                self._begin(group, store, feedback)
                return
            store.append(
                "commit.created",
                stage=step.step_id,
                attempt=attempt,
                sha=head,
                automatic=step.auto_commit,
            )
            self._review(group, store, step, attempt, head)
        except HypervisorError as error:
            store.log_error(error)
            store.append(
                "error",
                stage=step.step_id,
                attempt=attempt,
                message=str(error),
                operation="checks",
                log="logs/workflow.log",
            )
            raise

    def _review_tree_clean(
        self, group: str, store: EventStore, stage: str, attempt: str, head: str
    ) -> bool:
        record = self._record(group)
        if (
            self._git(record, "status", "--porcelain")
            or self._git(record, "rev-parse", "HEAD") != head
        ):
            data = {
                "decision": "blocked",
                "summary": "Worktree or HEAD changed before review. Human intervention required.",
                "findings": [],
            }
            data["report"] = self._save_report(
                store, attempt, "review-gate.json", json.dumps(data, ensure_ascii=False, indent=2)
            )
            store.append("review.blocked", stage=stage, attempt=attempt, **data)
            return False
        return True

    def _review(
        self, group: str, store: EventStore, step: FlowStep, attempt: str, head: str
    ) -> None:
        if not self._review_tree_clean(group, store, step.step_id, attempt, head):
            return
        events = store.read()
        started = self._find(events, "stage.started", step.step_id, attempt)
        session_id = str(uuid4())
        store.append(
            "review.started",
            stage=step.step_id,
            attempt=attempt,
            reviewer_session=session_id,
            head=head,
        )
        summary = self._find(events, "stage.submitted", step.step_id, attempt)["data"]["summary"]
        command = (
            f"{self._cli()} review submit {shlex.quote(group)} {shlex.quote(step.step_id)} "
            f"--attempt {attempt} --session {session_id} --decision approved "
            "--summary 'Explain the review outcome' --finding 'Optional concrete finding'"
        )
        prompt = (
            f"INDEPENDENT REVIEW: {step.step_id}\nReview goal: {step.review_goal}\n"
            f"Shared task context: {json.dumps({key: events[0]['data'][key] for key in ('request', 'goal', 'sources', 'constraints')}, ensure_ascii=False)}\n"
            f"Allowed review scope: {json.dumps(step.review_scope)}\nExecutor summary: {summary}\n"
            f"Review base commit: {started['data']['base_head'] or '(empty repository)'}\n"
            f"Review head commit: {head}\n"
            "Inspect only the given project artifacts and relevant diff. Do not modify, stage, or commit files. "
            "Do not inspect unrelated host directories. Use your fresh context for independent judgment.\n"
            f"MCP assignment: step_id={step.step_id}, attempt={attempt}. "
            "Read context and history via task_context, workflow_events and read_report. "
            "When done, call Agent Hypervisor MCP submit_review with this exact step_id, attempt, "
            "decision, nonempty summary and concrete findings. "
            "If MCP is not connected, the equivalent CLI fallback is (edit values):\n"
            f"{command}\nValid decisions: approved, changes_requested, blocked. "
            "Repeat --finding for concrete remarks; omit it if there are none. "
            "Hypervisor checks that both HEAD and the worktree remain unchanged after review. "
            "A dirty worktree or changed HEAD stops the workflow for human intervention."
        )
        self._deliver(
            group, store, step.step_id, attempt, step.reviewer_id, session_id, prompt, "reviewer"
        )

    def submit_review(
        self,
        group: str,
        step_id: str,
        attempt: str,
        decision: str,
        summary: str,
        findings: list[str] | None = None,
        session_id: str = "",
    ) -> dict:
        if decision not in {"approved", "changes_requested", "blocked"} or not summary.strip():
            raise HypervisorError("Review requires a valid decision and non-empty summary")
        if findings is not None and (
            not isinstance(findings, list) or any(not isinstance(item, str) for item in findings)
        ):
            raise HypervisorError("Review findings must be a list of strings")
        store = self._store(group)
        with store.transaction():
            events = store.read()
            if self._find(events, "review.submitted", step_id, attempt):
                return project_events(events)
            stage, step = self._validate(events, step_id, attempt)
            review = self._find(events, "review.started", step_id, attempt)
            if stage["state"] != "reviewing" or not review:
                raise HypervisorError("Stage is not waiting for review")
            if session_id and session_id != review["data"]["reviewer_session"]:
                raise HypervisorError("Review does not belong to the independent reviewer session")
            data = {"decision": decision, "summary": summary, "findings": findings or []}
            record = self._record(group)
            dirty = self._git(record, "status", "--porcelain")
            head = self._git(record, "rev-parse", "HEAD")
            if dirty or head != review["data"]["head"]:
                data.update(
                    decision="blocked",
                    summary="Review integrity gate failed: worktree or HEAD changed. Human intervention required.",
                    submitted_decision=decision,
                    submitted_summary=summary,
                    dirty=dirty,
                    expected_head=review["data"]["head"],
                    actual_head=head,
                )
                decision = "blocked"
            report = self._save_report(
                store, attempt, "review.json", json.dumps(data, ensure_ascii=False, indent=2)
            )
            data["report"] = report
            store.append(
                "review.submitted", stage=step_id, attempt=attempt, actor=step.reviewer_id, **data
            )
            store.append(
                f"review.{decision}", stage=step_id, attempt=attempt, actor=step.reviewer_id, **data
            )
            if decision == "changes_requested":
                self._begin(group, store, json.dumps(data, ensure_ascii=False))
            elif decision == "approved":
                self._after_review(group, store, step, attempt)
        return self.status(group)

    def _after_review(self, group: str, store: EventStore, step: FlowStep, attempt: str) -> None:
        events = store.read()
        if step.create_pr and not self._find(events, "pr.requested", step.step_id, attempt):
            store.append("pr.requested", stage=step.step_id, attempt=attempt, status="external_mcp")
        if step.human_approval:
            if not self._find(store.read(), "human.requested", step.step_id, attempt):
                store.append("human.requested", stage=step.step_id, attempt=attempt)
        else:
            self._finish(group, store, step, attempt)

    def human_decision(
        self,
        group: str,
        step_id: str,
        attempt: str,
        approved: bool,
        comment: str = "",
    ) -> dict:
        if not approved and not comment.strip():
            raise HypervisorError("Request Changes requires a comment")
        store = self._store(group)
        with store.transaction():
            events = store.read()
            for kind in ("human.approved", "human.changes_requested"):
                if self._find(events, kind, step_id, attempt):
                    return project_events(events)
            stage, step = self._validate(events, step_id, attempt)
            if stage["state"] != "awaiting-human":
                raise HypervisorError("Stage is not waiting for human approval")
            record = self._record(group)
            reviewed = self._find(events, "review.started", step_id, attempt)
            if approved and (
                self._git(record, "status", "--porcelain")
                or self._git(record, "rev-parse", "HEAD") != reviewed["data"]["head"]
            ):
                raise HypervisorError(
                    "Worktree changed since review; request changes and repeat checks/review"
                )
            store.append(
                "human.approved" if approved else "human.changes_requested",
                stage=step_id,
                attempt=attempt,
                actor="human",
                comment=comment,
            )
            if approved:
                self._finish(group, store, step, attempt)
            else:
                self._begin(group, store, comment)
        return self.status(group)

    def _finish(self, group: str, store: EventStore, step: FlowStep, attempt: str) -> None:
        if not self._find(store.read(), "stage.completed", step.step_id, attempt):
            store.append("stage.completed", stage=step.step_id, attempt=attempt)
        state = project_events(store.read())
        if state["current_step"]:
            self._begin(group, store)
        elif state["state"] != "completed":
            store.append("workflow.completed")

    def resume(self, group: str, *, retry_delivery: bool = False) -> dict:
        """Explicit recovery, never a background replay of an uncertain terminal send."""
        store = self._store(group)
        with store.transaction():
            events = store.read()
            if not events:
                raise HypervisorError("Manual group has no workflow to resume")
            state = project_events(events)
            if state["state"] == "completed":
                return state
            if not state["current_step"]:
                store.append("workflow.completed")
                return project_events(store.read())
            stage, step = self._current(events)
            attempt = stage["attempt"]
            delivery = self._find(events, "delivery.started", step.step_id, attempt)
            done = self._find(events, "delivery.completed", step.step_id, attempt)
            review = self._find(events, "review.started", step.step_id, attempt)
            submitted_review = self._find(events, "review.submitted", step.step_id, attempt)
            pending = self._deferred(events, step.step_id, attempt)
            if pending:
                data = pending["data"]
                if self.agent_mode(group, data["agent_id"]) == "manual":
                    return state
                self._deliver(
                    group,
                    store,
                    step.step_id,
                    attempt,
                    data["agent_id"],
                    data["session_id"],
                    self.report(group, data["report"]),
                    data["role"],
                )
            elif delivery and (not done or delivery["seq"] > done["seq"]):
                if not retry_delivery:
                    raise HypervisorError(
                        "Prompt delivery outcome is uncertain. Inspect the terminal, then use workflow resume --retry-delivery to explicitly resend it."
                    )
                data = delivery["data"]
                self._deliver(
                    group,
                    store,
                    step.step_id,
                    attempt,
                    data["agent_id"],
                    data["session_id"],
                    self.report(group, data["report"]),
                    data["role"],
                )
            elif submitted_review and not self._find(
                events, f"review.{submitted_review['data']['decision']}", step.step_id, attempt
            ):
                data = submitted_review["data"]
                record = self._record(group)
                if (
                    self._git(record, "status", "--porcelain")
                    or self._git(record, "rev-parse", "HEAD") != review["data"]["head"]
                ):
                    data = {
                        **data,
                        "decision": "blocked",
                        "summary": "Review recovery integrity gate failed; human intervention required",
                    }
                store.append(
                    f"review.{data['decision']}",
                    stage=step.step_id,
                    attempt=attempt,
                    actor=step.reviewer_id,
                    **data,
                )
                if data["decision"] == "approved":
                    self._after_review(group, store, step, attempt)
                elif data["decision"] == "changes_requested":
                    self._begin(group, store, json.dumps(data, ensure_ascii=False))
            elif stage["state"] in {"pending", "blocked", "changes-requested"}:
                self._begin(
                    group,
                    store,
                    "Human resumed the stage. Resolve any recorded conflicts and repeat completion.",
                )
            elif self._find(events, "human.approved", step.step_id, attempt):
                self._finish(group, store, step, attempt)
            elif self._find(events, "human.changes_requested", step.step_id, attempt):
                self._begin(group, store, "Resume after human changes request")
            elif self._find(events, "review.approved", step.step_id, attempt):
                self._after_review(group, store, step, attempt)
            elif self._find(events, "review.changes_requested", step.step_id, attempt):
                self._begin(group, store, json.dumps(stage["review"], ensure_ascii=False))
            elif review and (not delivery or review["seq"] > delivery["seq"]):
                self._review(group, store, step, attempt, review["data"]["head"])
            elif stage["state"] == "checking" or (
                stage["state"] == "failed"
                and self._find(events, "stage.submitted", step.step_id, attempt)
            ):
                self._check_and_review(group, store, step, attempt)
            elif stage["state"] in {"running", "reviewing", "failed"} and not delivery:
                # A crash after recording stage/review start but before delivery is safe to retry.
                if stage["state"] == "reviewing":
                    self._review(
                        group,
                        store,
                        step,
                        attempt,
                        self._git(self._record(group), "rev-parse", "HEAD"),
                    )
                else:
                    self._begin(group, store, "Resuming an interrupted stage start")
        return self.status(group)
