from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

from agent_hypervisor.models import HypervisorError
from agent_hypervisor.project_config import ProjectConfig
from agent_hypervisor.state import AgentSession, GroupRecord, StateStore
from agent_hypervisor.tmux import TmuxBackend
from agent_hypervisor.worktrees import GitWorktreeManager


class StateStoreTests(unittest.TestCase):
    def test_new_group_is_persisted_without_starting_tmux(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            tmux_runner = Mock(
                return_value=subprocess.CompletedProcess([], 1, "", "no tmux session")
            )
            backend = TmuxBackend(
                runner=tmux_runner,
                state_store=StateStore(Path(directory) / "groups.json"),
            )

            status = backend.create_group("sleeping", root)

            self.assertFalse(status.active)
            self.assertEqual(status.state, "inactive")
            calls = [call.args[0] for call in tmux_runner.call_args_list]
            self.assertFalse(any("new-session" in call for call in calls))

    def test_round_trips_group_sessions_and_rename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "groups.json")
            record = GroupRecord(
                "task-1",
                "/repo",
                "/repo-worktrees/task-1",
                "hypervisor/task-1",
                {1: [AgentSession("session-1", 1)]},
                {1: "session-1"},
            )

            store.save(record)
            loaded = store.get("task-1")
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.sessions[1][0].session_id, "session-1")

            renamed = store.rename("task-1", "task-2")
            self.assertEqual(renamed.group_id, "task-2")
            self.assertIsNone(store.get("task-1"))
            self.assertEqual(store.get("task-2").worktree_path, record.worktree_path)

    def test_qwen_commands_distinguish_new_and_resumed_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = TmuxBackend(
                project_config=ProjectConfig.default(),
                state_store=StateStore(Path(directory) / "groups.json"),
                uuid_factory=lambda: UUID("00000000-0000-0000-0000-000000000001"),
            )

            new_command = backend._qwen_command("new-id", terminal=2, resume=False)
            resumed_command = backend._qwen_command("old-id", terminal=2, resume=True)

            self.assertIn("--session-id new-id", new_command)
            self.assertIn("--resume old-id", resumed_command)
            self.assertIn("--chat-recording", new_command)
            self.assertIn("/rename terminal-2-newid", new_command)
            self.assertNotIn("/rename", resumed_command)

    def test_qwen_session_catalog_detects_recorded_transcript(self) -> None:
        runner = Mock(
            return_value=subprocess.CompletedProcess(
                [],
                0,
                '{"sessionId":"saved-id"}\n',
                "",
            )
        )
        backend = TmuxBackend(agent_runner=runner)

        self.assertTrue(backend._qwen_session_exists("/tmp", "saved-id"))
        self.assertFalse(backend._qwen_session_exists("/tmp", "new-id"))


class GitWorktreeManagerTests(unittest.TestCase):
    def test_creates_orphan_worktree_for_repository_without_commits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "empty-project"
            subprocess.run(["git", "init", "-q", str(root)], check=True)

            worktree = GitWorktreeManager().create("first-task", root)

            self.assertTrue(worktree.path.is_dir())
            self.assertEqual(worktree.branch, "hypervisor/first-task")
            branch = subprocess.run(
                ["git", "-C", str(worktree.path), "branch", "--show-current"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(branch, worktree.branch)

    def test_creates_unique_sibling_worktrees_and_keeps_branch_on_remove(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "README.md").write_text("test\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-qm",
                    "initial",
                ],
                check=True,
            )
            manager = GitWorktreeManager()

            first = manager.create("task", root)
            second = manager.create("task", root)

            self.assertEqual(first.path, root.parent / ".project-worktrees" / "task")
            self.assertEqual(second.path.name, "task-2")
            manager.remove(first.repository_root, first.path)
            self.assertFalse(first.path.exists())
            branch = subprocess.run(
                ["git", "-C", str(root), "branch", "--list", first.branch],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn(first.branch, branch)

            (second.path / "uncommitted.txt").write_text("keep me\n", encoding="utf-8")
            with self.assertRaisesRegex(HypervisorError, "uncommitted changes"):
                manager.remove(second.repository_root, second.path)
            self.assertTrue(second.path.exists())


if __name__ == "__main__":
    unittest.main()
