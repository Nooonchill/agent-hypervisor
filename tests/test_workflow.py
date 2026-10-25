from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from agent_hypervisor.event_store import EventStore
from agent_hypervisor.models import AgentManualMode, HypervisorError
from agent_hypervisor.project_config import AgentSpec, CheckSpec, FlowSpec, FlowStep, ProjectConfig
from agent_hypervisor.state import StateStore
from agent_hypervisor.tmux import TmuxBackend
from agent_hypervisor.task_form import DEFAULT_TASK_FORM


class RecordingBackend(TmuxBackend):
    """Use real Git and workflow persistence, but do not launch tmux or an agent."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.prompts: list[dict] = []
        self.fail_prompt_once = False

    def workflow_session(self, group_id: str, agent_id: str, session_id: str) -> int:
        record = self.state.get(group_id)
        assert record is not None
        return next(
            i for i, agent in enumerate(record.terminal_agents, 1) if agent.agent_id == agent_id
        )

    def queue_prompt(
        self, group_id: str, terminal: int, text: str, *, wait_ready: bool = False
    ) -> None:
        self.prompts.append(
            {"group": group_id, "terminal": terminal, "prompt": text, "wait_ready": wait_ready}
        )
        if self.fail_prompt_once:
            self.fail_prompt_once = False
            raise HypervisorError("simulated uncertain prompt delivery")


class WorkflowAcceptanceTests(unittest.TestCase):
    def test_selected_flow_context_is_frozen_and_reaches_executor_and_reviewer(self) -> None:
        values = {
            "request": "Write the docs",
            "goal": "Explain the workflow",
            "sources": ("docs/index.md",),
            "constraints": "Keep executable code unchanged",
        }
        form = tuple(
            replace(
                field,
                label=f"Docs {field.field_id}",
                default=values[field.field_id],
                visible=field.field_id == "request",
            )
            for field in DEFAULT_TASK_FORM
        )
        config = self.config(self.steps())
        config = replace(
            config, flows=(*config.flows, FlowSpec("docs", "Documentation", self.steps(), form))
        )
        backend = self.make_backend(config)
        backend.create_group("TASK-1", self.repository, flow_id="docs")
        created = backend.workflow.events("TASK-1")[0]["data"]
        self.assertEqual(created["request"], values["request"])
        self.assertEqual(created["task_form"][0]["label"], "Docs request")
        self.assertFalse(created["task_form"][1]["visible"])
        restarted = self.make_backend(ProjectConfig.default())
        state = restarted.workflow.start("TASK-1")
        attempt = state["stages"][0]["attempt"]
        restarted.workflow.complete("TASK-1", "implementation", attempt, "Docs completed")
        self.assertEqual(restarted.workflow.status("TASK-1")["state"], "reviewing")
        for prompt in restarted.prompts:
            for value in (
                "Write the docs",
                "Explain the workflow",
                "docs/index.md",
                "Keep executable code unchanged",
            ):
                self.assertIn(value, prompt["prompt"])

    def test_form_defaults_are_frozen_and_delivered_using_stable_field_ids(self) -> None:
        defaults = {
            "request": "Configured request",
            "goal": "Configured goal",
            "sources": ("spec.md",),
            "constraints": "Preserve API",
        }
        fields = tuple(
            replace(
                field,
                label=f"Custom {field.field_id}",
                required=True,
                default=defaults[field.field_id],
            )
            for field in DEFAULT_TASK_FORM
        )
        backend = self.make_backend(replace(self.config(self.steps()), task_form=fields))
        backend.create_group("TASK-1", self.repository)
        created = backend.workflow.events("TASK-1")[0]["data"]
        self.assertEqual(created["request"], "Configured request")
        self.assertEqual(created["sources"], ["spec.md"])
        self.assertEqual(created["task_form"][0]["label"], "Custom request")
        restarted = self.make_backend(ProjectConfig.default())
        restarted.workflow.start("TASK-1")
        prompt = restarted.prompts[-1]["prompt"]
        for value in ("Configured request", "Configured goal", "spec.md", "Preserve API"):
            self.assertIn(value, prompt)

    def test_required_form_values_fail_before_creating_worktree(self) -> None:
        fields = tuple(
            replace(field, label="Required task", required=True)
            if field.field_id == "request"
            else field
            for field in DEFAULT_TASK_FORM
        )
        backend = self.make_backend(replace(self.config(self.steps()), task_form=fields))
        with self.assertRaisesRegex(HypervisorError, "Required task"):
            backend.create_group("TASK-1", self.repository)
        self.assertIsNone(backend.state.get("TASK-1"))
        self.assertFalse((self.root / ".project-worktrees" / "TASK-1").exists())
        backend.create_group("TASK-1", self.repository, request="User supplied request")
        self.assertEqual(
            backend.workflow.events("TASK-1")[0]["data"]["request"], "User supplied request"
        )

    def test_returning_control_cannot_overtake_recording_a_deferred_task(self) -> None:
        self.backend.create_group("TASK-1", self.repository, request="Test task")
        self.backend.workflow.set_agent_mode("TASK-1", "developer", "manual")
        at_defer, release = Event(), Event()
        append = EventStore.append

        def delayed_append(store, event_type, **kwargs):
            if event_type == "delivery.deferred":
                at_defer.set()
                release.wait(1)
            return append(store, event_type, **kwargs)

        with (
            patch.object(EventStore, "append", delayed_append),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            starting = pool.submit(self.start)
            self.assertTrue(at_defer.wait(1))
            returning = pool.submit(
                self.backend.workflow.set_agent_mode, "TASK-1", "developer", "workflow"
            )
            self.assertFalse(returning.done())
            release.set()
            starting.result()
            self.assertEqual(returning.result()["state"], "running")
        self.assertEqual(len(self.backend.prompts), 1)

    def test_rapid_manual_then_workflow_switch_during_rejected_send_does_not_lose_task(
        self,
    ) -> None:
        self.backend.create_group("TASK-1", self.repository, request="Test task")
        queue = self.backend.queue_prompt
        first = True

        def rapid_switch(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                self.backend.workflow.set_agent_mode("TASK-1", "developer", "manual")
                self.backend.workflow.set_agent_mode("TASK-1", "developer", "workflow")
                raise AgentManualMode("manual check rejected this send before pasting")
            return queue(*args, **kwargs)

        with patch.object(self.backend, "queue_prompt", side_effect=rapid_switch):
            self.assertEqual(self.start()["state"], "running")
        self.assertEqual(len(self.backend.prompts), 1)

    def test_releasing_manual_mode_waits_for_an_inflight_transition_to_finish(self) -> None:
        self.backend.create_group("TASK-1", self.repository, request="Test task")
        self.backend.workflow.set_agent_mode("TASK-1", "developer", "manual")
        self.start()
        ready = Event()
        release = Event()

        def in_flight():
            with self.backend.workflow._store("TASK-1").transaction():
                ready.set()
                release.wait(0.15)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(in_flight)
            self.assertTrue(ready.wait(1))
            result = self.backend.workflow.set_agent_mode("TASK-1", "developer", "workflow")
            future.result()
        self.assertEqual(result["state"], "running")
        self.assertEqual(len(self.backend.prompts), 1)

    def test_concurrent_mode_updates_keep_a_single_ordered_journal(self) -> None:
        self.backend.create_group("TASK-1", self.repository, request="Test task")

        def toggle(agent):
            for mode in ("manual", "workflow") * 5:
                self.backend.workflow.set_agent_mode("TASK-1", agent, mode)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(toggle, ("developer", "reviewer")))
        events = self.backend.workflow.events("TASK-1")
        self.assertEqual([event["seq"] for event in events], list(range(1, 22)))
        self.assertEqual(
            self.backend.workflow.status("TASK-1")["agent_modes"],
            {
                "developer": "workflow",
                "reviewer": "workflow",
            },
        )

    def test_manual_mode_defers_delivery_and_survives_restart_without_new_attempt(self) -> None:
        self.backend.create_group("TASK-1", self.repository, request="Test task")
        self.backend.workflow.set_agent_mode("TASK-1", "developer", "manual")
        paused = self.start()
        self.assertEqual(paused["state"], "waiting-manual")
        self.assertEqual(paused["stages"][0]["waiting_agent"], "developer")
        self.assertEqual(self.backend.prompts, [])
        started = next(
            e for e in self.backend.workflow.events("TASK-1") if e["type"] == "stage.started"
        )
        self.backend.workflow.resume("TASK-1")
        self.assertEqual(self.backend.prompts, [])
        restarted = self.make_backend(ProjectConfig.default(1))
        resumed = restarted.workflow.set_agent_mode("TASK-1", 1, "workflow")
        self.assertEqual(resumed["state"], "running")
        self.assertEqual(resumed["stages"][0]["attempt"], started["attempt"])
        self.assertEqual(len(restarted.prompts), 1)
        self.assertEqual(
            len([e for e in restarted.workflow.events("TASK-1") if e["type"] == "stage.started"]), 1
        )
        restarted.workflow.set_agent_mode("TASK-1", 1, "workflow")
        self.assertEqual(len(restarted.prompts), 1)

    def test_manual_mode_changes_no_running_stage_and_can_switch_under_transition_lock(
        self,
    ) -> None:
        self.backend.create_group("TASK-1", self.repository, request="Test task")
        self.start()
        original = self.backend.workflow.status("TASK-1")
        store = self.backend.workflow._store("TASK-1")
        with store.transaction():
            self.backend.workflow.set_agent_mode("TASK-1", "developer", "manual")
            self.backend.workflow.set_agent_mode("TASK-1", "developer", "manual")
        current = self.backend.workflow.status("TASK-1")
        self.assertEqual(current["stages"], original["stages"])
        self.assertEqual(current["state"], "running")
        self.assertEqual(current["agent_modes"], {"developer": "manual", "reviewer": "workflow"})
        self.assertEqual(len(self.backend.prompts), 1)
        self.assertEqual(
            len(
                [
                    e
                    for e in self.backend.workflow.events("TASK-1")
                    if e["type"] == "agent.mode_changed"
                ]
            ),
            1,
        )

    def test_reviewer_manual_mode_pauses_only_reviewer_delivery(self) -> None:
        self.backend.create_group("TASK-1", self.repository, request="Test task")
        self.backend.workflow.set_agent_mode("TASK-1", "reviewer", "manual")
        self.start()
        attempt = self.current_attempt()
        self.submit(attempt)
        state = self.backend.workflow.status("TASK-1")
        self.assertEqual(state["state"], "waiting-manual")
        self.assertTrue(state["stages"][0]["commit"])
        self.assertEqual(len(self.backend.prompts), 1)
        state = self.backend.workflow.set_agent_mode("TASK-1", "reviewer", "workflow")
        self.assertEqual(state["state"], "reviewing")
        self.assertEqual(state["stages"][0]["attempt"], attempt)
        self.assertEqual(len(self.backend.prompts), 2)
        self.assertIn("INDEPENDENT REVIEW", self.backend.prompts[-1]["prompt"])

    def test_manual_changes_before_releasing_review_still_require_integrity_gate(self) -> None:
        self.backend.create_group("TASK-1", self.repository, request="Test task")
        self.backend.workflow.set_agent_mode("TASK-1", "reviewer", "manual")
        self.start()
        self.submit(self.current_attempt())
        worktree = Path(self.state.get("TASK-1").worktree_path)
        (worktree / "manual-change.txt").write_text("human change")
        self.assertEqual(
            self.backend.workflow.set_agent_mode("TASK-1", "reviewer", "workflow")["state"],
            "blocked",
        )
        self.assertEqual(len(self.backend.prompts), 1)

    def test_manual_switch_during_delivery_is_deferred_not_failed(self) -> None:
        self.backend.create_group("TASK-1", self.repository, request="Test task")

        def changed_mode(*args, **kwargs):
            self.backend.workflow.set_agent_mode("TASK-1", "developer", "manual")
            raise AgentManualMode("manual")

        with patch.object(self.backend, "queue_prompt", side_effect=changed_mode):
            self.assertEqual(self.start()["state"], "waiting-manual")
        self.assertNotIn("error", self.event_types())
        self.assertEqual(
            self.backend.workflow.set_agent_mode("TASK-1", "developer", "workflow")["state"],
            "running",
        )

    def test_failed_release_of_deferred_prompt_still_needs_explicit_uncertain_retry(self) -> None:
        self.backend.create_group("TASK-1", self.repository, request="Test task")
        self.backend.workflow.set_agent_mode("TASK-1", "developer", "manual")
        self.start()
        self.backend.fail_prompt_once = True
        with self.assertRaises(HypervisorError):
            self.backend.workflow.set_agent_mode("TASK-1", "developer", "workflow")
        with self.assertRaisesRegex(HypervisorError, "uncertain"):
            self.backend.workflow.resume("TASK-1")
        self.assertEqual(
            self.backend.workflow.resume("TASK-1", retry_delivery=True)["state"], "running"
        )

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="workflow-v2-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / "project"
        self.repository.mkdir()
        self.git(self.repository, "init", "-q")
        self.git(self.repository, "config", "user.name", "Workflow Tests")
        self.git(self.repository, "config", "user.email", "workflow@example.test")
        (self.repository / "README.md").write_text("seed\n", encoding="utf-8")
        self.git(self.repository, "add", "README.md")
        self.git(self.repository, "commit", "-qm", "initial")
        self.state = StateStore(self.root / "state.json")
        self.backend = self.make_backend(self.config(self.steps()))

    @staticmethod
    def git(cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    @staticmethod
    def steps(
        *,
        auto_commit: bool = True,
        human: bool = False,
        pr: bool = False,
        checks: tuple[CheckSpec, ...] = (),
    ) -> tuple[FlowStep, ...]:
        return (
            FlowStep(
                "developer",
                "Implement the first stage",
                "implementation",
                "reviewer",
                "Review correctness",
                ("**/*",),
                checks,
                auto_commit,
                pr,
                human,
            ),
            FlowStep(
                "developer",
                "Deliver the second stage",
                "delivery",
                "reviewer",
                "Review delivery",
                ("**/*",),
                (),
                True,
                False,
                False,
            ),
        )

    @staticmethod
    def config(steps: tuple[FlowStep, ...], flow_id: str = "delivery") -> ProjectConfig:
        agents = (AgentSpec("developer", "Developer"), AgentSpec("reviewer", "Reviewer"))
        return ProjectConfig(agents=agents, flows=(FlowSpec(flow_id, "Delivery", steps),))

    def make_backend(self, config: ProjectConfig) -> RecordingBackend:
        missing_tmux = Mock(return_value=subprocess.CompletedProcess([], 1, "", "no tmux"))
        return RecordingBackend(runner=missing_tmux, state_store=self.state, project_config=config)

    def start(self, backend: RecordingBackend | None = None) -> dict:
        return (backend or self.backend).workflow.start("TASK-1")

    def submit(
        self,
        attempt: str,
        *,
        backend: RecordingBackend | None = None,
        step: str = "implementation",
        summary: str = "Implemented the requested change",
    ) -> dict:
        return (backend or self.backend).workflow.complete("TASK-1", step, attempt, summary)

    def current_attempt(
        self, backend: RecordingBackend | None = None, step: str = "implementation"
    ) -> str:
        service = (backend or self.backend).workflow
        state = service.status("TASK-1")
        return next(item["attempt"] for item in state["stages"] if item["id"] == step)

    def approve_review(
        self,
        attempt: str,
        *,
        backend: RecordingBackend | None = None,
        session: str = "",
        step: str = "implementation",
    ) -> dict:
        return (backend or self.backend).workflow.submit_review(
            "TASK-1", step, attempt, "approved", "Looks correct", session_id=session
        )

    def event_types(self, backend: RecordingBackend | None = None) -> list[str]:
        return [event["type"] for event in (backend or self.backend).workflow.events("TASK-1")]

    def crash_after(
        self,
        backend: RecordingBackend,
        event_type: str,
        *,
        stage: str = "implementation",
        attempt: str | None = None,
    ) -> None:
        """Keep the durable event prefix ending at a crash boundary."""
        events = backend.workflow.events("TASK-1")
        candidates = [
            event
            for event in events
            if event["type"] == event_type
            and event["stage"] == stage
            and (attempt is None or event["attempt"] == attempt)
        ]
        self.assertTrue(candidates, f"missing crash boundary {event_type}/{stage}/{attempt}")
        target = candidates[-1]
        path = backend.workflow._store("TASK-1").path
        path.write_bytes(
            b"".join(
                (json.dumps(event, ensure_ascii=False) + "\n").encode()
                for event in events
                if event["seq"] <= target["seq"]
            )
        )

    def test_commit_checks_fresh_reviewer_human_gate_next_stage_and_done(self) -> None:
        # The successful gate is an actual subprocess in the task worktree.
        step = FlowStep(
            "developer",
            "Implement",
            "implementation",
            "reviewer",
            "Review",
            ("**/*",),
            (CheckSpec("ok", ("python", "-c", "print('ok')"), 5),),
            True,
            True,
            True,
        )
        config = self.config((step, self.steps()[1]))
        backend = self.make_backend(config)
        backend.create_group("TASK-1", self.repository, request="Build it")
        created = backend.workflow.events("TASK-1")[0]["data"]
        self.assertEqual(created["flow_id"], "delivery")
        self.assertEqual(created["request"], "Build it")
        record = self.state.get("TASK-1")
        assert record is not None
        worktree = Path(record.worktree_path)

        self.start(backend)
        first = self.current_attempt(backend)
        (worktree / "implementation.txt").write_text("done\n", encoding="utf-8")
        self.submit(first, backend=backend)
        events = backend.workflow.events("TASK-1")
        review_started = next(e for e in events if e["type"] == "review.started")
        executor_started = next(e for e in events if e["type"] == "stage.started")
        self.assertEqual(review_started["data"]["head"], self.git(worktree, "rev-parse", "HEAD"))
        self.assertNotEqual(
            review_started["data"]["reviewer_session"], executor_started["data"]["executor_session"]
        )
        self.assertIn("checks.passed", [e["type"] for e in events])
        self.assertIn("commit.created", [e["type"] for e in events])
        self.assertEqual(
            len([p for p in backend.prompts if "INDEPENDENT REVIEW" in p["prompt"]]), 1
        )

        self.approve_review(
            first, backend=backend, session=review_started["data"]["reviewer_session"]
        )
        self.assertEqual(backend.workflow.status("TASK-1")["state"], "awaiting-human")
        self.assertIn("pr.requested", self.event_types(backend))
        self.assertEqual(backend.workflow.events("TASK-1")[-1]["type"], "human.requested")
        backend.workflow.human_decision("TASK-1", "implementation", first, True, "Accepted")
        next_state = backend.workflow.status("TASK-1")
        self.assertEqual(next_state["current_step"], "delivery")
        second = self.current_attempt(backend, "delivery")
        self.assertNotEqual(first, second)
        second_started = next(
            e
            for e in backend.workflow.events("TASK-1")
            if e["type"] == "stage.started" and e["stage"] == "delivery"
        )
        first_started = next(
            e
            for e in backend.workflow.events("TASK-1")
            if e["type"] == "stage.started" and e["stage"] == "implementation"
        )
        self.assertNotEqual(
            second_started["data"]["executor_session"], first_started["data"]["executor_session"]
        )
        (worktree / "delivery.txt").write_text("shipped\n", encoding="utf-8")
        self.submit(second, backend=backend, step="delivery", summary="Delivered")
        self.approve_review(second, backend=backend, step="delivery")
        self.assertEqual(backend.workflow.status("TASK-1")["state"], "completed")
        self.assertIsNone(backend.workflow.status("TASK-1")["current_step"])

    def test_manual_commit_dirty_worktree_retries_executor_without_review(self) -> None:
        # For manual commit, the cleanliness gate refuses review and assigns a fresh attempt.
        backend = self.make_backend(self.config(self.steps(auto_commit=False)))
        backend.create_group("TASK-1", self.repository, request="Test task")
        worktree = Path(self.state.get("TASK-1").worktree_path)
        self.start(backend)
        original = self.current_attempt(backend)
        executor = next(
            e for e in backend.workflow.events("TASK-1") if e["type"] == "stage.started"
        )
        (worktree / "uncommitted.txt").write_text("changes\n", encoding="utf-8")
        self.submit(original, backend=backend)
        state = backend.workflow.status("TASK-1")
        retried = self.current_attempt(backend)
        self.assertEqual(state["state"], "running")
        self.assertNotEqual(original, retried)
        started = [e for e in backend.workflow.events("TASK-1") if e["type"] == "stage.started"]
        self.assertEqual(
            started[-1]["data"]["executor_session"], executor["data"]["executor_session"]
        )
        self.assertFalse(
            any(e["type"] == "review.started" for e in backend.workflow.events("TASK-1"))
        )
        failed = next(e for e in backend.workflow.events("TASK-1") if e["type"] == "checks.failed")
        self.assertIn(
            "Commit all changes before review",
            backend.workflow.report("TASK-1", failed["data"]["results"][0]["report"]),
        )

    def test_failed_hard_gate_records_report_and_feedback_for_new_attempt(self) -> None:
        failed = CheckSpec("fails", ("python", "-c", "raise SystemExit(3)"), 5)
        backend = self.make_backend(self.config(self.steps(checks=(failed,))))
        backend.create_group("TASK-1", self.repository, request="Test task")
        self.start(backend)
        attempt = self.current_attempt(backend)
        (Path(self.state.get("TASK-1").worktree_path) / "change.txt").write_text(
            "x", encoding="utf-8"
        )
        self.submit(attempt, backend=backend)
        events = backend.workflow.events("TASK-1")
        failed_event = next(e for e in events if e["type"] == "checks.failed")
        self.assertEqual(failed_event["data"]["results"][0]["returncode"], 3)
        report = backend.workflow.report("TASK-1", failed_event["data"]["results"][0]["report"])
        self.assertIn("raise SystemExit(3)", report)
        self.assertNotEqual(self.current_attempt(backend), attempt)
        self.assertIn("Hard gates failed", backend.prompts[-1]["prompt"])
        self.assertFalse(any(e["type"] == "review.started" for e in events))

    def test_changes_requested_reuses_executor_but_creates_fresh_reviewer(self) -> None:
        backend = self.make_backend(self.config(self.steps()))
        backend.create_group("TASK-1", self.repository, request="Test task")
        worktree = Path(self.state.get("TASK-1").worktree_path)
        self.start(backend)
        first = self.current_attempt(backend)
        (worktree / "first.txt").write_text("first\n", encoding="utf-8")
        self.submit(first, backend=backend)
        first_review = next(
            e for e in backend.workflow.events("TASK-1") if e["type"] == "review.started"
        )
        backend.workflow.submit_review(
            "TASK-1",
            "implementation",
            first,
            "changes_requested",
            "Please revise",
            ["Add a regression check"],
        )
        second = self.current_attempt(backend)
        self.assertNotEqual(first, second)
        self.assertIn("Please revise", backend.prompts[-1]["prompt"])
        # The worktree must be clean before the next completion can reach review.
        (worktree / "second.txt").write_text("second\n", encoding="utf-8")
        self.git(worktree, "add", "second.txt")
        self.git(worktree, "commit", "-qm", "revise")
        attempts = [
            e
            for e in backend.workflow.events("TASK-1")
            if e["type"] == "stage.started" and e["stage"] == "implementation"
        ]
        self.assertEqual(
            attempts[0]["data"]["executor_session"], attempts[1]["data"]["executor_session"]
        )
        (worktree / "third.txt").write_text("third\n", encoding="utf-8")
        self.submit(second, backend=backend, summary="Revised implementation")
        reviews = [
            e
            for e in backend.workflow.events("TASK-1")
            if e["type"] == "review.started" and e["stage"] == "implementation"
        ]
        self.assertEqual(len(reviews), 2)
        self.assertNotEqual(
            first_review["data"]["reviewer_session"], reviews[1]["data"]["reviewer_session"]
        )

    def test_dirty_or_moved_head_during_review_blocks_and_never_advances(self) -> None:
        for mutation in ("dirty", "head"):
            with self.subTest(mutation=mutation):
                backend = self.make_backend(self.config(self.steps()))
                self.state = StateStore(self.root / f"state-{mutation}.json")
                backend.state = self.state
                backend.create_group("TASK-1", self.repository, request="Test task")
                worktree = Path(self.state.get("TASK-1").worktree_path)
                self.start(backend)
                attempt = self.current_attempt(backend)
                (worktree / "implementation.txt").write_text("done\n", encoding="utf-8")
                self.submit(attempt, backend=backend)
                review = next(
                    e for e in backend.workflow.events("TASK-1") if e["type"] == "review.started"
                )
                if mutation == "dirty":
                    (worktree / "review-leak.txt").write_text(
                        "reviewer wrote this", encoding="utf-8"
                    )
                else:
                    (worktree / "review-head.txt").write_text("moved", encoding="utf-8")
                    self.git(worktree, "add", "review-head.txt")
                    self.git(worktree, "commit", "-qm", "review moved HEAD")
                state = backend.workflow.submit_review(
                    "TASK-1",
                    "implementation",
                    attempt,
                    "approved",
                    "I approve",
                    session_id=review["data"]["reviewer_session"],
                )
                self.assertEqual(state["current_step"], "implementation")
                self.assertEqual(state["state"], "blocked")
                submitted = next(
                    e for e in backend.workflow.events("TASK-1") if e["type"] == "review.submitted"
                )
                self.assertEqual(submitted["data"]["decision"], "blocked")
                self.assertFalse(
                    any(e["stage"] == "delivery" for e in backend.workflow.events("TASK-1"))
                )

    def test_unintegrated_pr_is_informational_and_does_not_block_completion(self) -> None:
        one_step = (self.steps(pr=True)[0],)
        backend = self.make_backend(self.config(one_step))
        backend.create_group("TASK-1", self.repository, request="Test task")
        worktree = Path(self.state.get("TASK-1").worktree_path)
        self.start(backend)
        attempt = self.current_attempt(backend)
        (worktree / "feature.txt").write_text("feature\n", encoding="utf-8")
        self.submit(attempt, backend=backend)
        self.approve_review(attempt, backend=backend)
        self.assertEqual(backend.workflow.status("TASK-1")["state"], "completed")
        pr = next(e for e in backend.workflow.events("TASK-1") if e["type"] == "pr.requested")
        self.assertEqual(pr["data"]["status"], "not_integrated")

    def test_repeated_stage_review_human_callbacks_are_idempotent(self) -> None:
        backend = self.make_backend(self.config(self.steps(human=True)))
        backend.create_group("TASK-1", self.repository, request="Test task")
        worktree = Path(self.state.get("TASK-1").worktree_path)
        self.start(backend)
        attempt = self.current_attempt(backend)
        (worktree / "feature.txt").write_text("feature\n", encoding="utf-8")
        backend.workflow.complete("TASK-1", "implementation", attempt, "Done")
        backend.workflow.complete("TASK-1", "implementation", attempt, "Duplicate callback")
        self.approve_review(attempt, backend=backend)
        backend.workflow.submit_review(
            "TASK-1", "implementation", attempt, "approved", "Duplicate callback"
        )
        backend.workflow.human_decision("TASK-1", "implementation", attempt, True, "Accepted")
        self.assertEqual(backend.workflow.status("TASK-1")["current_step"], "delivery")
        # Delayed retries from the completed attempt must not replay its handoff.
        backend.workflow.complete("TASK-1", "implementation", attempt, "Duplicate callback")
        backend.workflow.submit_review(
            "TASK-1", "implementation", attempt, "approved", "Duplicate callback"
        )
        backend.workflow.human_decision(
            "TASK-1", "implementation", attempt, True, "Duplicate callback"
        )
        events = backend.workflow.events("TASK-1")
        for kind in (
            "stage.submitted",
            "review.started",
            "review.submitted",
            "human.requested",
            "human.approved",
            "stage.completed",
        ):
            self.assertEqual(sum(e["type"] == kind for e in events), 1, kind)
        self.assertEqual(sum(e["type"] == "stage.started" for e in events), 2)
        self.assertEqual(len(backend.prompts), 3)  # Executor, reviewer, then next-stage executor.

    def test_stale_attempt_and_noncurrent_stage_are_rejected(self) -> None:
        self.backend.create_group("TASK-1", self.repository, request="Test task")
        self.start()
        attempt = self.current_attempt()
        with self.assertRaisesRegex(HypervisorError, "Stale or incorrect"):
            self.backend.workflow.complete("TASK-1", "implementation", "old-attempt", "Done")
        with self.assertRaisesRegex(HypervisorError, "Stale or incorrect"):
            self.backend.workflow.complete("TASK-1", "delivery", attempt, "Wrong stage")

    def test_reconstructed_backend_uses_frozen_group_flow_after_config_edit(self) -> None:
        # Group creation snapshots the flow; a backend reloaded with a different config resumes it.
        expected = self.config(self.steps())
        backend = self.make_backend(expected)
        backend.create_group("TASK-1", self.repository, request="Original request")
        changed = self.config(
            (
                FlowStep(
                    "reviewer",
                    "Changed config goal",
                    "other",
                    "developer",
                    "Other review",
                    ("**/*",),
                ),
            ),
            flow_id="changed",
        )
        restarted = self.make_backend(changed)
        restarted.workflow.resume("TASK-1")
        self.assertEqual(restarted.workflow.status("TASK-1")["flow_id"], "delivery")
        self.assertEqual(restarted.workflow.status("TASK-1")["current_step"], "implementation")
        self.assertIn("Implement the first stage", restarted.prompts[-1]["prompt"])
        self.assertIn("Original request", restarted.prompts[-1]["prompt"])

    def test_partial_tail_is_repaired_under_writer_lock_and_readers_do_not_wait(self) -> None:
        self.backend.create_group("TASK-1", self.repository, request="Test task")
        store = self.backend.workflow._store("TASK-1")
        self.assertEqual(len(self.backend.workflow.events("TASK-1")), 1)
        with store.path.open("ab") as stream:
            stream.write(b'{"seq":2,"partial":')
        # An unlocked reader gets the durable prefix even while the tail is incomplete.
        self.assertEqual(len(self.backend.workflow.events("TASK-1")), 1)
        with store.transaction():
            with self.assertRaisesRegex(HypervisorError, "Another workflow operation"):
                self.backend.workflow.start("TASK-1")
            self.assertEqual(len(self.backend.workflow.events("TASK-1")), 1)
            self.assertEqual(self.backend.workflow.status("TASK-1")["state"], "pending")
        self.assertEqual(len(store.path.read_bytes().splitlines()), 1)
        # The first writer truncates the interrupted record, then proceeds normally.
        self.backend.workflow.start("TASK-1")
        self.assertEqual(self.backend.workflow.status("TASK-1")["state"], "running")

    def test_report_path_traversal_is_rejected(self) -> None:
        self.backend.create_group("TASK-1", self.repository, request="Test task")
        with self.assertRaisesRegex(HypervisorError, "inside this task's reports directory"):
            self.backend.workflow.report("TASK-1", "../../outside.txt")

    def test_workflow_start_requires_a_request_or_shared_goal(self) -> None:
        self.backend.create_group("TASK-1", self.repository)
        with self.assertRaisesRegex(HypervisorError, "initial task request or shared goal"):
            self.backend.workflow.start("TASK-1")
        self.assertEqual(self.backend.workflow.status("TASK-1")["state"], "pending")
        self.assertEqual(self.backend.prompts, [])

    def test_resume_after_review_started_but_before_reviewer_delivery(self) -> None:
        backend = self.make_backend(self.config(self.steps()))
        backend.create_group("TASK-1", self.repository, request="Test task")
        worktree = Path(self.state.get("TASK-1").worktree_path)
        self.start(backend)
        attempt = self.current_attempt(backend)
        (worktree / "implementation.txt").write_text("done\n", encoding="utf-8")
        self.submit(attempt, backend=backend)
        self.assertEqual(len(backend.prompts), 2)  # Original executor and reviewer sends.
        self.crash_after(backend, "review.started", attempt=attempt)

        restarted = self.make_backend(self.config(self.steps()))
        state = restarted.workflow.resume("TASK-1")
        self.assertEqual(state["state"], "reviewing")
        self.assertEqual(len(restarted.prompts), 1)
        self.assertIn("INDEPENDENT REVIEW", restarted.prompts[0]["prompt"])
        deliveries = [
            event
            for event in restarted.workflow.events("TASK-1")
            if event["type"] == "delivery.completed" and event["data"]["role"] == "reviewer"
        ]
        self.assertEqual(len(deliveries), 1)

    def test_recovery_never_delivers_review_of_a_dirty_worktree(self) -> None:
        backend = self.backend
        backend.create_group("TASK-1", self.repository, request="Test task")
        self.start()
        attempt = self.current_attempt()
        self.submit(attempt)
        self.crash_after(backend, "review.started", attempt=attempt)
        worktree = Path(self.state.get("TASK-1").worktree_path)
        (worktree / "unexpected.txt").write_text("concurrent manual change")
        restarted = self.make_backend(self.config(self.steps()))
        self.assertEqual(restarted.workflow.resume("TASK-1")["state"], "blocked")
        self.assertEqual(restarted.prompts, [])

    def test_inflight_agent_callback_survives_group_rename(self) -> None:
        backend = self.backend
        backend.create_group("TASK-1", self.repository, request="Test task")
        self.start()
        attempt = self.current_attempt()
        backend.rename_group("TASK-1", "TASK-RENAMED")
        result = backend.workflow.complete("TASK-1", "implementation", attempt, "No changes needed")
        self.assertEqual(result["state"], "reviewing")
        self.assertEqual(backend.prompts[-1]["group"], "TASK-RENAMED")

    def test_group_lifecycle_cannot_interrupt_a_workflow_transition(self) -> None:
        backend = self.backend
        backend.create_group("TASK-1", self.repository, request="Test task")
        with backend.workflow._store("TASK-1").transaction():
            for operation in (
                lambda: backend.remove_group("TASK-1"),
                lambda: backend.rename_group("TASK-1", "renamed"),
            ):
                with self.assertRaisesRegex(HypervisorError, "Another workflow operation"):
                    operation()
        self.assertIsNotNone(backend.state.get("TASK-1"))

    def test_tracked_runtime_directory_is_rejected_before_overwriting_files(self) -> None:
        directory = self.repository / ".hypervisor"
        directory.mkdir()
        (directory / "events.jsonl").write_text("project-owned file\n")
        self.git(self.repository, "add", ".hypervisor")
        self.git(self.repository, "commit", "-qm", "tracked runtime directory")
        with self.assertRaisesRegex(HypervisorError, "contains tracked files"):
            self.backend.create_group("TASK-1", self.repository, request="Test task")
        self.assertIsNone(self.backend.state.get("TASK-1"))
        self.assertEqual((directory / "events.jsonl").read_text(), "project-owned file\n")

    def test_resume_after_review_submitted_applies_recorded_decision(self) -> None:
        flow = (self.steps(human=True)[0],)
        backend = self.make_backend(self.config(flow))
        backend.create_group("TASK-1", self.repository, request="Test task")
        worktree = Path(self.state.get("TASK-1").worktree_path)
        self.start(backend)
        attempt = self.current_attempt(backend)
        (worktree / "implementation.txt").write_text("done\n", encoding="utf-8")
        self.submit(attempt, backend=backend)
        backend.workflow.submit_review("TASK-1", "implementation", attempt, "approved", "Approved")
        self.crash_after(backend, "review.submitted", attempt=attempt)

        restarted = self.make_backend(self.config(flow))
        state = restarted.workflow.resume("TASK-1")
        self.assertEqual(state["state"], "awaiting-human")
        self.assertTrue(
            any(event["type"] == "review.approved" for event in restarted.workflow.events("TASK-1"))
        )
        self.assertTrue(
            any(event["type"] == "human.requested" for event in restarted.workflow.events("TASK-1"))
        )
        self.assertEqual(len(restarted.prompts), 0)  # A submitted review is never sent again.

    def test_resume_after_human_approval_finishes_once_and_hands_off(self) -> None:
        backend = self.make_backend(self.config(self.steps(human=True)))
        backend.create_group("TASK-1", self.repository, request="Test task")
        worktree = Path(self.state.get("TASK-1").worktree_path)
        self.start(backend)
        attempt = self.current_attempt(backend)
        (worktree / "implementation.txt").write_text("done\n", encoding="utf-8")
        self.submit(attempt, backend=backend)
        self.approve_review(attempt, backend=backend)
        backend.workflow.human_decision("TASK-1", "implementation", attempt, True, "Accepted")
        self.crash_after(backend, "human.approved", attempt=attempt)

        restarted = self.make_backend(self.config(self.steps(human=True)))
        state = restarted.workflow.resume("TASK-1")
        events = restarted.workflow.events("TASK-1")
        self.assertEqual(state["current_step"], "delivery")
        self.assertEqual(
            sum(
                event["type"] == "stage.completed" and event["stage"] == "implementation"
                for event in events
            ),
            1,
        )
        self.assertEqual(
            sum(
                event["type"] == "stage.started" and event["stage"] == "delivery"
                for event in events
            ),
            1,
        )
        self.assertEqual(len(restarted.prompts), 1)
        self.assertIn("WORKFLOW EXECUTION: delivery", restarted.prompts[0]["prompt"])

    def test_resume_after_stage_completed_starts_next_stage_once(self) -> None:
        backend = self.make_backend(self.config(self.steps()))
        backend.create_group("TASK-1", self.repository, request="Test task")
        worktree = Path(self.state.get("TASK-1").worktree_path)
        self.start(backend)
        first = self.current_attempt(backend)
        (worktree / "implementation.txt").write_text("done\n", encoding="utf-8")
        self.submit(first, backend=backend)
        self.approve_review(first, backend=backend)
        self.crash_after(backend, "stage.completed", attempt=first)

        restarted = self.make_backend(self.config(self.steps()))
        state = restarted.workflow.resume("TASK-1")
        events = restarted.workflow.events("TASK-1")
        self.assertEqual(state["current_step"], "delivery")
        self.assertEqual(
            sum(
                event["type"] == "stage.completed" and event["stage"] == "implementation"
                for event in events
            ),
            1,
        )
        self.assertEqual(
            sum(
                event["type"] == "stage.started" and event["stage"] == "delivery"
                for event in events
            ),
            1,
        )
        self.assertEqual(len(restarted.prompts), 1)
        self.assertIn("WORKFLOW EXECUTION: delivery", restarted.prompts[0]["prompt"])

    def test_uncertain_prompt_delivery_requires_explicit_retry_and_clears_failure(self) -> None:
        backend = self.make_backend(self.config(self.steps()))
        backend.create_group("TASK-1", self.repository, request="Test task")
        backend.fail_prompt_once = True
        with self.assertRaisesRegex(HypervisorError, "simulated uncertain prompt delivery"):
            self.start(backend)
        before = len(backend.prompts)
        self.assertEqual(backend.workflow.status("TASK-1")["state"], "failed")
        with self.assertRaisesRegex(HypervisorError, "outcome is uncertain"):
            backend.workflow.resume("TASK-1")
        self.assertEqual(len(backend.prompts), before)

        restarted = self.make_backend(self.config(self.steps()))
        state = restarted.workflow.resume("TASK-1", retry_delivery=True)
        self.assertEqual(state["state"], "running")
        self.assertEqual(len(restarted.prompts), 1)
        events = restarted.workflow.events("TASK-1")
        self.assertTrue(
            any(
                event["type"] == "delivery.completed" and event["data"]["role"] == "executor"
                for event in events
            )
        )

    def test_resume_does_not_repeat_auto_commit_if_crash_precedes_commit_event(self) -> None:
        backend = self.make_backend(self.config(self.steps()))
        backend.create_group("TASK-1", self.repository, request="Test task")
        worktree = Path(self.state.get("TASK-1").worktree_path)
        self.start(backend)
        attempt = self.current_attempt(backend)
        (worktree / "implementation.txt").write_text("done\n", encoding="utf-8")
        append = EventStore.append

        class SimulatedCrash(BaseException):
            pass

        def crash_before_commit_event(event_store, event_type, **kwargs):
            if event_type == "commit.created":
                raise SimulatedCrash()
            return append(event_store, event_type, **kwargs)

        before_head = self.git(worktree, "rev-parse", "HEAD")
        with patch.object(EventStore, "append", crash_before_commit_event):
            with self.assertRaises(SimulatedCrash):
                self.submit(attempt, backend=backend)
        committed_head = self.git(worktree, "rev-parse", "HEAD")
        self.assertNotEqual(before_head, committed_head)
        before_count = int(self.git(worktree, "rev-list", "--count", "HEAD"))

        restarted = self.make_backend(self.config(self.steps()))
        state = restarted.workflow.resume("TASK-1")
        self.assertEqual(state["state"], "reviewing")
        self.assertEqual(self.git(worktree, "rev-parse", "HEAD"), committed_head)
        self.assertEqual(int(self.git(worktree, "rev-list", "--count", "HEAD")), before_count)
        self.assertTrue(
            any(
                event["type"] == "commit.created" and event["attempt"] == attempt
                for event in restarted.workflow.events("TASK-1")
            )
        )


if __name__ == "__main__":
    unittest.main()
