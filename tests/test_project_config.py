from __future__ import annotations

import tempfile
import shlex
import unittest
from pathlib import Path

import yaml

from agent_hypervisor.models import HypervisorError
from agent_hypervisor.project_config import load_project_config
from agent_hypervisor.state import GroupRecord, StateStore
from agent_hypervisor.tmux import TmuxBackend


class ProjectConfigTests(unittest.TestCase):
    def test_field_types_descriptions_and_flow_overrides(self) -> None:
        self.payload["task-form"] = {
            "sources": {
                "default": ["a,b.md"],
                "description": "Source context",
                "placeholder": "Paste sources",
            },
            "request": {"type": "text"},
        }
        self.payload["flows"][0]["task-form"] = {"sources": {"type": "multiline"}}
        config = self.load()
        fields = {field.field_id: field for field in config.form_for_flow()}
        self.assertEqual(fields["request"].input_type, "text")
        self.assertEqual(fields["sources"].input_type, "multiline")
        self.assertEqual(fields["sources"].default, "a,b.md")
        self.assertEqual(fields["sources"].description, "Source context")
        self.assertEqual(fields["sources"].placeholder, "Paste sources")
        self.assertEqual(config.task_form[2].input_type, "string-list")

    def test_invalid_field_types_and_hints_are_rejected(self) -> None:
        for settings in (
            {"sources": {"type": "number"}},
            {"request": {"type": "string-list"}},
            {"sources": {"type": "multiline", "default": []}},
            {"goal": {"placeholder": 1}},
            {"goal": {"description": None}},
        ):
            with self.subTest(settings=settings):
                self.payload["task-form"] = settings
                with self.assertRaises(HypervisorError):
                    self.load()

    def test_all_agent_instructions_can_live_in_yaml_without_a_directory(self) -> None:
        self.payload.pop("agents-dir")
        for agent in self.payload["agents"]:
            agent["instructions"] = f"Instructions for {agent['agent-id']}\nUse the task sources."
        config = self.load()
        for agent in config.agents:
            self.assertEqual(agent.description_path, "")
            self.assertIn(agent.agent_id, agent.instructions)
        command = TmuxBackend(project_config=config)._qwen_command(
            "session", terminal=1, resume=False
        )
        args = shlex.split(command)
        self.assertEqual(
            args[args.index("--append-system-prompt") + 1], config.agents[0].instructions
        )

    def test_inline_instructions_take_precedence_and_can_mix_with_files(self) -> None:
        self.payload["agents"][0]["instructions"] = "Inline developer instructions."
        directory = self.root / self.payload["agents-dir"]
        (directory / "developer.md").unlink()
        config = self.load()
        self.assertEqual(config.agents[0].instructions, "Inline developer instructions.")
        self.assertEqual(config.agents[0].description_path, "")
        self.assertEqual(config.agents[1].instructions, "You are architect.")
        self.assertTrue(config.agents[1].description_path.endswith("architect.md"))

    def test_inline_instructions_must_be_nonempty_strings(self) -> None:
        for value in (None, True, "", " ", ["instruction"], "bad\0value"):
            with self.subTest(value=value):
                self.payload["agents"][0]["instructions"] = value
                with self.assertRaisesRegex(
                    HypervisorError, "instructions must be a non-empty string"
                ):
                    self.load()
        self.payload["agents"][0].pop("instructions")
        self.payload.pop("agents-dir")
        with self.assertRaisesRegex(HypervisorError, "requires instructions or agents-dir"):
            self.load()

    def test_workflow_forms_inherit_globals_without_leaking_overrides(self) -> None:
        self.payload["task-form"] = {
            "request": {"label": "Global request", "required": True},
            "sources": {"default": ["shared.md"]},
        }
        self.payload["flows"][0]["task-form"] = {
            "request": {"label": "Feature request"},
            "goal": {"visible": False},
        }
        self.payload["flows"].append(
            {
                "id": "docs",
                "name": "Documentation",
                "steps": [self.payload["flows"][0]["steps"][0].copy()],
                "task-form": {
                    "request": {"label": "Documentation request"},
                    "sources": {"visible": False},
                },
            }
        )
        config = self.load()
        first = {field.field_id: field for field in config.form_for_flow("delivery")}
        docs = {field.field_id: field for field in config.form_for_flow("docs")}
        self.assertEqual(first["request"].label, "Feature request")
        self.assertTrue(first["request"].required)
        self.assertFalse(first["goal"].visible)
        self.assertTrue(docs["goal"].visible)
        self.assertEqual(docs["request"].label, "Documentation request")
        self.assertFalse(docs["sources"].visible)
        self.assertEqual(docs["sources"].default, ("shared.md",))
        self.assertEqual(config.task_form[0].label, "Global request")

    def test_hidden_required_fields_need_a_usable_default(self) -> None:
        self.payload["flows"][0]["task-form"] = {"request": {"required": True, "visible": False}}
        with self.assertRaisesRegex(HypervisorError, "hidden required field"):
            self.load()
        self.payload["flows"][0]["task-form"]["request"]["default"] = "   "
        with self.assertRaisesRegex(HypervisorError, "hidden required field"):
            self.load()
        self.payload["flows"][0]["task-form"]["request"]["default"] = "Configured task"
        self.assertFalse(self.load().form_for_flow()[0].visible)
        self.payload["flows"][0]["task-form"]["request"]["visible"] = "false"
        with self.assertRaisesRegex(HypervisorError, "visible must"):
            self.load()

    def test_task_form_labels_requirements_and_defaults_are_loaded(self) -> None:
        self.payload["task-form"] = {
            "request": {"label": "Задача", "required": True, "default": "Default request"},
            "sources": {"label": "Документы", "default": ["spec.md", "https://example.test"]},
        }
        fields = {field.field_id: field for field in self.load().task_form}
        self.assertEqual(fields["request"].label, "Задача")
        self.assertTrue(fields["request"].required)
        self.assertEqual(fields["request"].default, "Default request")
        self.assertEqual(fields["sources"].default, ("spec.md", "https://example.test"))
        self.assertEqual(set(fields), {"request", "goal", "sources", "constraints"})

    def test_task_form_rejects_unknown_fields_and_invalid_types(self) -> None:
        for settings in (
            {"custom": {}},
            {"request": {"required": "yes"}},
            {"request": {"label": ""}},
            {"request": {"default": []}},
            {"sources": {"default": "not a list"}},
            {"sources": {"default": [None]}},
        ):
            with self.subTest(settings=settings):
                self.payload["task-form"] = settings
                with self.assertRaises(HypervisorError):
                    self.load()

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
            "version": 2,
            "agents-dir": "agents",
            "agents": [
                {"agent-id": "developer", "agent-name": "Developer", "model": "model-1"},
                {"agent-id": "architect", "agent-name": "Architect"},
            ],
            "flows": [
                {
                    "id": "delivery",
                    "name": "Delivery",
                    "steps": [
                        {
                            "id": "implementation",
                            "agent-id": "developer",
                            "goal": "Implement changes",
                            "reviewer-id": "architect",
                            "review-goal": "Check the implementation against its acceptance criteria.",
                            "review-scope": ["src/**", "tests/**"],
                            "checks": [
                                {
                                    "id": "unit-tests",
                                    "command": ["python", "-m", "unittest"],
                                    "timeout": 60,
                                }
                            ],
                            "auto-commit": False,
                            "create-pr": True,
                            "human-approval": True,
                        }
                    ],
                }
            ],
        }

    def load(self):
        path = self.root / "config.yaml"
        path.write_text(yaml.safe_dump(self.payload, sort_keys=False), encoding="utf-8")
        return load_project_config(path)

    def test_loads_v2_flows_checks_and_instructions(self) -> None:
        config = self.load()
        self.assertEqual(len(config.agents), 2)
        self.assertFalse(hasattr(config.agents[0], "reviewer_id"))
        self.assertEqual(config.agents[0].instructions, "You are developer.")
        flow = config.select_flow()
        self.assertEqual(flow.flow_id, "delivery")
        step = flow.steps[0]
        self.assertEqual(step.step_id, "implementation")
        self.assertEqual(step.reviewer_id, "architect")
        self.assertEqual(step.review_scope, ("src/**", "tests/**"))
        self.assertEqual(step.checks[0].command, ("python", "-m", "unittest"))
        self.assertEqual(step.checks[0].timeout, 60)
        self.assertTrue(step.create_pr)
        self.assertTrue(step.human_approval)
        backend = TmuxBackend(project_config=config)
        command = backend._qwen_command("abcdef00-1234", terminal=1, resume=False)
        self.assertIn("--model model-1", command)
        self.assertIn("--append-system-prompt", command)
        self.assertIn("/rename developer-abcdef00", command)
        self.assertNotIn("--model", backend._qwen_command("old", terminal=2, resume=True))

    def test_rejects_invalid_v2_fields_and_paths(self) -> None:
        cases = (
            ("reviewer-id", "missing", "Unknown reviewer-id"),
            ("goal", "  ", "goal must"),
            ("review-goal", "", "review-goal must"),
            ("review-scope", ["../secret"], "relative pattern"),
            ("checks", [{"id": "x", "command": "python"}], "argv list"),
            ("checks", [{"id": "x", "command": ["python"], "timeout": True}], "positive integer"),
            ("auto-commit", "false", "auto-commit must"),
            ("unknown", True, "Unknown fields"),
        )
        for field, value, expected in cases:
            with self.subTest(field=field):
                step = self.payload["flows"][0]["steps"][0]
                original = step.copy()
                step[field] = value
                with self.assertRaisesRegex(HypervisorError, expected):
                    self.load()
                self.payload["flows"][0]["steps"][0] = original

        for data_dir in ("../outside", ".git/logs", "/tmp/hv"):
            with self.subTest(data_dir=data_dir):
                self.payload["data-dir"] = data_dir
                with self.assertRaisesRegex(HypervisorError, "data-dir"):
                    self.load()
        self.payload.pop("data-dir")

    def test_rejects_duplicate_or_unknown_flow_and_step_references(self) -> None:
        second = {**self.payload["flows"][0], "id": "delivery"}
        self.payload["flows"].append(second)
        with self.assertRaisesRegex(HypervisorError, "Duplicate flow id"):
            self.load()
        self.payload["flows"].pop()
        self.payload["flows"][0]["steps"].append(self.payload["flows"][0]["steps"][0].copy())
        with self.assertRaisesRegex(HypervisorError, "Duplicate step id"):
            self.load()
        self.payload["flows"][0]["steps"].pop()
        self.payload["flows"][0]["steps"][0]["agent-id"] = "missing"
        with self.assertRaisesRegex(HypervisorError, "Unknown agent-id in"):
            self.load()

    def test_multiple_flows_require_selection_and_defaults_remain_manual(self) -> None:
        self.payload["flows"].append(
            {"id": "other", "name": "Other", "steps": [self.payload["flows"][0]["steps"][0].copy()]}
        )
        config = self.load()
        with self.assertRaisesRegex(HypervisorError, "flow-id is required"):
            config.select_flow()
        self.assertEqual(config.select_flow("other").flow_id, "other")
        with self.assertRaisesRegex(HypervisorError, "Unknown flow-id"):
            config.select_flow("absent")
        self.assertIsNone(type(config).default().select_flow())

    def test_v1_legacy_flow_migrates_agent_review_settings(self) -> None:
        self.payload = {
            "version": 1,
            "agents-dir": "agents",
            "agents": [
                {
                    "agent-id": "developer",
                    "agent-name": "Developer",
                    "reviewer-id": "architect",
                    "create-pr": True,
                },
                {"agent-id": "architect", "agent-name": "Architect", "reviewer-id": None},
            ],
            "flow": [{"id": "old-step", "agent-id": "developer", "goal": "Old goal"}],
        }
        config = self.load()
        self.assertEqual(len(config.flows), 1)
        self.assertEqual(config.select_flow().flow_id, "legacy")
        self.assertEqual(config.flow[0].reviewer_id, "architect")
        self.assertTrue(config.flow[0].create_pr)
        self.assertEqual(config.flow[0].review_scope, ("**/*",))

    def test_v1_null_reviewer_falls_back_to_step_agent_and_no_flow_is_manual(self) -> None:
        self.payload = {
            "version": 1,
            "agents-dir": "agents",
            "agents": [
                {"agent-id": "developer", "agent-name": "Developer", "reviewer-id": None},
                {"agent-id": "architect", "agent-name": "Architect"},
            ],
            "flow": [{"agent-id": "developer", "goal": "Legacy"}],
        }
        config = self.load()
        self.assertEqual(config.flows[0].steps[0].reviewer_id, "developer")
        self.payload["flow"] = []
        self.assertIsNone(self.load().select_flow())

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


if __name__ == "__main__":
    unittest.main()
