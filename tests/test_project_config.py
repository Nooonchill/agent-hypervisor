from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from agent_hypervisor.models import HypervisorError
from agent_hypervisor.project_config import load_project_config
from agent_hypervisor.state import GroupRecord, StateStore
from agent_hypervisor.tmux import TmuxBackend


class ProjectConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "agents").mkdir()
        for agent_id in ("developer", "architect"):
            (self.root / "agents" / f"{agent_id}.md").write_text(
                f"---\nname: {agent_id}\n---\nYou are {agent_id}.\n", encoding="utf-8"
            )
        self.payload = {
            "agents-dir": "agents",
            "agents": [
                {
                    "agent-id": "developer",
                    "agent-name": "Developer",
                    "model": "model-1",
                    "reviewer-id": "architect",
                },
                {"agent-id": "architect", "agent-name": "Architect"},
            ],
            "flow": [{"agent-id": "developer", "goal": "Implement changes"}],
        }

    def load(self):
        path = self.root / "config.yaml"
        path.write_text(yaml.safe_dump(self.payload), encoding="utf-8")
        return load_project_config(path)

    def test_loads_descriptions_model_reviewers_and_flow(self) -> None:
        config = self.load()
        self.assertEqual(len(config.agents), 2)
        self.assertEqual(config.agents[0].reviewer_id, "architect")
        self.assertEqual(config.agents[0].instructions, "You are developer.")
        self.assertFalse(config.agents[0].create_pr)
        self.assertEqual(config.flow[0].step_id, "step-1")
        backend = TmuxBackend(project_config=config)
        command = backend._qwen_command("abcdef00-1234", terminal=1, resume=False)
        self.assertIn("--model model-1", command)
        self.assertIn("--append-system-prompt", command)
        self.assertIn("/rename developer-abcdef00", command)
        self.assertNotIn("--model", backend._qwen_command("old", terminal=2, resume=True))

    def test_invalid_references_and_types_are_rejected(self) -> None:
        for field, value, expected in (
            ("reviewer-id", "missing", "Unknown reviewer-id"),
            ("model", 123, "model must"),
            ("create-pr", "false", "create-pr must"),
            ("agent-id", "../escape", "Invalid agent-id"),
            ("unknown", True, "Unknown fields"),
        ):
            with self.subTest(field=field):
                original = self.payload["agents"][0].copy()
                self.payload["agents"][0][field] = value
                with self.assertRaisesRegex(HypervisorError, expected):
                    self.load()
                self.payload["agents"][0] = original

    def test_missing_description_duplicate_agent_and_unknown_flow_rejected(self) -> None:
        self.payload["agents"][1]["agent-id"] = "developer"
        with self.assertRaisesRegex(HypervisorError, "Duplicate agent-id"):
            self.load()
        self.payload["agents"][1]["agent-id"] = "architect"
        self.payload["flow"][0]["agent-id"] = "missing"
        with self.assertRaisesRegex(HypervisorError, "Unknown agent-id in flow"):
            self.load()
        self.payload["flow"] = []
        (self.root / "agents" / "architect.md").unlink()
        with self.assertRaisesRegex(HypervisorError, "Cannot read agent description"):
            self.load()

    def test_group_agent_snapshot_survives_config_changes(self) -> None:
        config = self.load()
        store = StateStore(self.root / "groups.json")
        record = GroupRecord(
            "TASK-1",
            str(self.root),
            str(self.root),
            "feature/TASK-1",
            agents=config.agents,
            flow=config.flow,
        )
        store.save(record)
        self.payload["agents"][0]["agent-name"] = "Renamed"
        self.load()
        restored = store.get("TASK-1")
        self.assertEqual(restored.terminal_agents[0].agent_name, "Developer")
        self.assertEqual(restored.flow[0].goal, "Implement changes")
