from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_hypervisor import cli
from agent_hypervisor.models import HypervisorError
from agent_hypervisor.project_config import ProjectConfig


class WorkflowCliTests(unittest.TestCase):
    def test_agent_mode_dispatches_by_id_or_terminal_number(self) -> None:
        self.backend.status.return_value = SimpleNamespace(
            terminals=(SimpleNamespace(number=1, agent_id="developer"),)
        )
        self.service.set_agent_mode.return_value = {"agent_modes": {"developer": "manual"}}
        code, output, _ = self.run_cli(["mode", "TASK-1", "developer", "manual", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["agent_modes"]["developer"], "manual")
        self.service.set_agent_mode.assert_called_with("TASK-1", 1, "manual")
        code, _, _ = self.run_cli(["mode", "TASK-1", "1", "workflow"])
        self.assertEqual(code, 0)
        self.service.set_agent_mode.assert_called_with("TASK-1", 1, "workflow")

    def setUp(self) -> None:
        self.service = Mock()
        self.service.status.return_value = {"state": "running", "current_step": "build"}
        self.service.start.return_value = {"state": "running"}
        self.service.resume.return_value = {"state": "running"}
        self.service.events.return_value = [{"type": "task.created"}]
        self.service.report.return_value = "report content\n"
        self.service.complete.return_value = {"state": "reviewing"}
        self.service.submit_review.return_value = {"state": "awaiting-human"}
        self.service.human_decision.return_value = {"state": "completed"}
        self.backend = Mock()
        self.backend.workflow = self.service
        self.backend.create_group.return_value = SimpleNamespace(group_id="TASK-1")

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        backend_factory = patch.object(cli, "TmuxBackend", return_value=self.backend)
        with (
            backend_factory as factory,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result = cli.main(argv)
        self.backend_factory = factory
        return result, stdout.getvalue(), stderr.getvalue()

    def test_workflow_status_and_events_emit_json(self) -> None:
        code, output, _ = self.run_cli(["workflow", "status", "TASK-1", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), {"state": "running", "current_step": "build"})
        code, output, _ = self.run_cli(["--json", "workflow", "events", "TASK-1"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), [{"type": "task.created"}])

    def test_workflow_commands_dispatch_service_arguments(self) -> None:
        code, _, _ = self.run_cli(["workflow", "resume", "TASK-1", "--retry-delivery"])
        self.assertEqual(code, 0)
        self.service.resume.assert_called_with("TASK-1", retry_delivery=True)

        code, _, _ = self.run_cli(
            [
                "stage",
                "complete",
                "TASK-1",
                "build",
                "--attempt",
                "a1",
                "--session",
                "s1",
                "--summary",
                "Done",
            ]
        )
        self.assertEqual(code, 0)
        self.service.complete.assert_called_with("TASK-1", "build", "a1", "Done", session_id="s1")

        code, _, _ = self.run_cli(
            [
                "review",
                "submit",
                "TASK-1",
                "build",
                "--attempt",
                "a1",
                "--session",
                "s2",
                "--decision",
                "changes_requested",
                "--summary",
                "Fix issue",
                "--finding",
                "first",
                "--finding",
                "second",
            ]
        )
        self.assertEqual(code, 0)
        self.service.submit_review.assert_called_with(
            "TASK-1",
            "build",
            "a1",
            "changes_requested",
            "Fix issue",
            findings=["first", "second"],
            session_id="s2",
        )

        code, _, _ = self.run_cli(
            [
                "human",
                "approve",
                "TASK-1",
                "build",
                "--attempt",
                "a1",
            ]
        )
        self.assertEqual(code, 0)
        self.service.human_decision.assert_called_with(
            "TASK-1", "build", "a1", approved=True, comment=""
        )

    def test_report_text_and_json_output(self) -> None:
        code, output, _ = self.run_cli(["workflow", "report", "TASK-1", "reports/a.txt"])
        self.assertEqual(code, 0)
        self.assertEqual(output, "report content\n")
        code, output, _ = self.run_cli(["workflow", "report", "TASK-1", "reports/a.txt", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(output), {"path": "reports/a.txt", "content": "report content\n"}
        )

    def test_create_passes_context_and_starts_only_when_requested(self) -> None:
        with patch.object(cli, "load_project_config", return_value=ProjectConfig.default()):
            code, output, _ = self.run_cli(
                [
                    "create",
                    "TASK-1",
                    "--flow",
                    "delivery",
                    "--request",
                    "Do work",
                    "--goal",
                    "Shared goal",
                    "--source",
                    "spec.md",
                    "--source",
                    "https://example.test",
                    "--constraints",
                    "Keep API stable",
                    "--start-workflow",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), {"state": "running"})
        self.backend.create_group.assert_called_with(
            "TASK-1",
            Path.cwd(),
            task_type="feature",
            flow_id="delivery",
            request="Do work",
            goal="Shared goal",
            sources=("spec.md", "https://example.test"),
            constraints="Keep API stable",
        )
        self.service.start.assert_called_once_with("TASK-1")

    def test_reject_requires_nonempty_comment(self) -> None:
        code, _, stderr = self.run_cli(
            [
                "human",
                "reject",
                "TASK-1",
                "build",
                "--attempt",
                "a1",
                "--comment",
                "  ",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("Reject requires a non-empty comment", stderr)
        self.service.human_decision.assert_not_called()

    def test_reject_requires_comment_argument(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["human", "reject", "TASK-1", "build", "--attempt", "a1"])

    def test_group_workflow_command_ignores_invalid_cwd_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_text("this is: [not valid", encoding="utf-8")
            old_cwd = Path.cwd()
            os.chdir(root)
            try:
                with patch.object(
                    cli, "load_project_config", side_effect=AssertionError("must not load YAML")
                ):
                    code, output, _ = self.run_cli(["workflow", "status", "TASK-1", "--json"])
            finally:
                os.chdir(old_cwd)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["state"], "running")
        configured = self.backend_factory.call_args.kwargs["project_config"]
        self.assertIsInstance(configured, ProjectConfig)
        self.assertEqual(configured, ProjectConfig.default())

    def test_hypervisor_errors_are_clear_json_errors(self) -> None:
        self.service.start.side_effect = HypervisorError("workflow is already active")
        code, _, stderr = self.run_cli(["--json", "workflow", "start", "TASK-1"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stderr), {"error": "workflow is already active"})


if __name__ == "__main__":
    unittest.main()
