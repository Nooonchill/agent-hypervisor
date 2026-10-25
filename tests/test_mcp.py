from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_hypervisor.mcp_config import qwen_settings
from agent_hypervisor.agent_api import AgentBinding, AgentTools
from agent_hypervisor.mcp_server import create_server
from agent_hypervisor.models import HypervisorError
from agent_hypervisor.project_config import AgentSpec, FlowSpec, FlowStep, ProjectConfig
from agent_hypervisor.state import AgentSession, StateStore
from agent_hypervisor.tmux import TmuxBackend
from tests.test_workflow import RecordingBackend


class McpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hypervisor-mcp-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        for key, value in (("user.name", "MCP Test"), ("user.email", "mcp@example.test")):
            subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
        self.state = StateStore(self.root / "groups.json")
        self.config = ProjectConfig(
            agents=(AgentSpec("developer", "Developer"), AgentSpec("reviewer", "Reviewer")),
            flows=(
                FlowSpec(
                    "flow",
                    "Flow",
                    (
                        FlowStep(
                            "developer",
                            "Implement",
                            "implement",
                            "reviewer",
                            "Review",
                            ("**/*",),
                            auto_commit=True,
                        ),
                    ),
                ),
            ),
        )
        self.backend = RecordingBackend(
            state_store=self.state,
            project_config=self.config,
            runner=Mock(return_value=subprocess.CompletedProcess([], 1, "", "no tmux")),
        )
        self.backend.create_group(
            "TASK",
            repo,
            request="Implement request",
            goal="Shared goal",
            sources=("README.md",),
            constraints="Keep simple",
        )
        self.backend.workflow.start("TASK")
        self.attempt = self.backend.workflow.status("TASK")["stages"][0]["attempt"]
        self.executor = self.bind("stage.started", "developer", "executor_session")

    def bind(self, kind, agent, session_key):
        event = next(
            event
            for event in reversed(self.backend.workflow.events("TASK"))
            if event["type"] == kind
        )
        session = event["data"][session_key]
        record = self.state.get("TASK")
        number = 1 if agent == "developer" else 2
        record.sessions.setdefault(number, []).append(AgentSession(session, number, agent_id=agent))
        self.state.save(record)
        return AgentTools(self.backend.workflow, AgentBinding("TASK", agent, session))

    def test_real_service_handoffs_and_feedback_without_cli(self):
        context = self.executor.task_context()
        self.assertEqual(context["task"]["sources"], ["README.md"])
        self.assertEqual(context["task"]["goal"], "Shared goal")
        path = Path(self.state.get("TASK").worktree_path) / "result.txt"
        path.write_text("result\n")
        state = self.executor.complete_stage("implement", self.attempt, "Wrote result.txt")
        self.assertEqual(state["state"], "reviewing")
        reviewer = self.bind("review.started", "reviewer", "reviewer_session")
        with self.assertRaisesRegex(HypervisorError, "non-empty summary"):
            reviewer.submit_review("implement", self.attempt, "changes_requested", " ")
        result = reviewer.submit_review(
            "implement", self.attempt, "changes_requested", "Fix the result", ["Missing details"]
        )
        self.assertEqual(result["state"], "running")
        self.assertNotEqual(result["stages"][0]["attempt"], self.attempt)
        events = self.executor.workflow_events()["events"]
        feedback = next(e for e in events if e["type"] == "review.changes_requested")
        self.assertEqual(feedback["data"]["summary"], "Fix the result")
        self.assertIn("Missing details", self.executor.read_report(feedback["data"]["report"]))
        attempt = result["stages"][0]["attempt"]
        self.executor.complete_stage("implement", attempt, "Fixed details")
        fresh_reviewer = self.bind("review.started", "reviewer", "reviewer_session")
        self.assertNotEqual(reviewer.binding.session, fresh_reviewer.binding.session)
        final = fresh_reviewer.submit_review("implement", attempt, "approved", "Correct")
        self.assertEqual(final["state"], "completed")
        # Repeated submission uses the same service-level idempotency.
        self.assertEqual(
            fresh_reviewer.submit_review("implement", attempt, "approved", "Correct"), final
        )

    def test_bound_session_cannot_impersonate_reviewer_or_change_attempt(self):
        before = self.backend.workflow.events("TASK")
        with self.assertRaisesRegex(HypervisorError, "not the reviewer"):
            self.executor.submit_review("implement", self.attempt, "approved", "Looks good")
        with self.assertRaisesRegex(HypervisorError, "not the executor"):
            self.executor.complete_stage("implement", "stale", "Done")
        unknown = AgentTools(
            self.backend.workflow, AgentBinding("TASK", "reviewer", self.executor.binding.session)
        )
        with self.assertRaisesRegex(HypervisorError, "identity"):
            unknown.task_context()
        self.assertEqual(before, self.backend.workflow.events("TASK"))

    def test_reads_are_live_paginated_and_reports_confined(self):
        page = self.executor.workflow_events(limit=2)
        next_page = self.executor.workflow_events(page["next_sequence"], 200)
        self.assertTrue(page["has_more"])
        self.assertGreater(next_page["events"][0]["seq"], page["next_sequence"])
        with self.assertRaises(HypervisorError):
            self.executor.workflow_events(limit=201)
        with self.assertRaises(HypervisorError):
            self.executor.read_report("../../groups.json")
        with self.assertRaises(HypervisorError):
            self.executor.read_report("events.jsonl")
        restarted = TmuxBackend(state_store=self.state)
        tools = AgentTools(restarted.workflow, self.executor.binding)
        self.assertEqual(tools.workflow_status()["workflow"]["state"], "running")
        self.backend.workflow.set_agent_mode("TASK", "developer", "manual")
        self.assertEqual(tools.workflow_status()["workflow"]["agent_modes"]["developer"], "manual")

    def test_settings_json_and_launch_environment_are_session_specific(self):
        settings = qwen_settings()["mcpServers"]["agent-hypervisor"]
        self.assertEqual(settings["args"], ["-m", "agent_hypervisor", "mcp", "serve"])
        self.assertFalse(settings["trust"])
        self.assertEqual(settings["env"]["HYPERVISOR_SESSION"], "${HYPERVISOR_SESSION}")
        record = self.state.get("TASK")
        for number, agent in enumerate(record.agents, 1):
            command = self.backend._qwen_command(
                f"session-{number}", terminal=number, resume=False, agent=agent, record=record
            )
            args = shlex.split(command)
            self.assertEqual(args[0], "env")
            self.assertIn(f"HYPERVISOR_AGENT={agent.agent_id}", args)
            self.assertIn(f"HYPERVISOR_SESSION=session-{number}", args)
            self.assertNotIn("--mcp-config", args)
            self.assertFalse((Path(record.worktree_path) / ".qwen/settings.json").exists())

    def test_stdio_sdk_discovery_validation_and_state(self):
        async def exercise():
            binding = self.executor.binding
            (self.root / "config.yaml").write_text("invalid: [unterminated")
            parameters = StdioServerParameters(
                command=sys.executable,
                cwd=str(self.root),
                args=[
                    "-m",
                    "agent_hypervisor",
                    "mcp",
                    "serve",
                    "--group",
                    binding.group,
                    "--agent",
                    binding.agent,
                    "--session",
                    binding.session,
                    "--state-path",
                    str(self.state.path),
                ],
                env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
            )
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    tools = (await client.list_tools()).tools
                    self.assertEqual(
                        {tool.name for tool in tools},
                        {
                            "workflow_status",
                            "task_context",
                            "workflow_events",
                            "read_report",
                            "complete_stage",
                            "submit_review",
                        },
                    )
                    for tool in tools:
                        self.assertNotIn("group", tool.inputSchema.get("properties", {}))
                        self.assertNotIn("session", tool.inputSchema.get("properties", {}))
                    result = await client.call_tool("workflow_status", {})
                    self.assertFalse(result.isError)
                    self.assertEqual(result.structuredContent["workflow"]["state"], "running")
                    wrong = await client.call_tool(
                        "submit_review",
                        {
                            "step_id": "implement",
                            "attempt": self.attempt,
                            "decision": "approved",
                            "summary": "Try impersonation",
                        },
                    )
                    self.assertTrue(wrong.isError)
                    self.assertFalse((await client.call_tool("task_context", {})).isError)

        asyncio.run(exercise())

    def test_agent_api_needs_only_workflow_operations(self):
        workflow = Mock(spec=["validate_agent_session", "events"])
        workflow.events.return_value = []
        tools = AgentTools(workflow, AgentBinding("group", "agent", "session"))
        self.assertEqual(tools.workflow_status()["workflow"]["state"], "manual")
        workflow.validate_agent_session.assert_called_once_with("group", "agent", "session")

    def test_mcp_import_does_not_load_terminal_or_ui_adapters(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import agent_hypervisor.mcp_server; assert 'agent_hypervisor.tmux' not in sys.modules; assert 'textual' not in sys.modules",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_tool_schemas_do_not_expose_human_or_terminal_controls(self):
        async def inspect_tools():
            server = create_server(self.backend.workflow, self.executor.binding)
            tools = await server.list_tools()
            self.assertEqual(len(tools), 6)
            names = {tool.name for tool in tools}
            self.assertNotIn("human_decision", names)
            self.assertNotIn("send_text", names)

        asyncio.run(inspect_tools())
