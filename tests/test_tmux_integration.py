from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from agent_hypervisor.models import TerminalState
from agent_hypervisor.project_config import ProjectConfig
from agent_hypervisor.state import StateStore
from agent_hypervisor.tmux import TmuxBackend


@unittest.skipUnless(
    os.environ.get("HYPERVISOR_TEST_TMUX"), "set HYPERVISOR_TEST_TMUX to a tmux binary"
)
class TmuxIntegrationTests(unittest.TestCase):
    def test_variable_agent_counts_and_persisted_groups(self) -> None:
        binary = os.environ["HYPERVISOR_TEST_TMUX"]
        socket = f"hypervisor-counts-{uuid4().hex[:12]}"

        def runner(args, *, check=False, **kwargs):
            return subprocess.run(
                [binary, "-L", socket, "-f", "/dev/null", *args[1:]], check=check, **kwargs
            )

        with tempfile.TemporaryDirectory(prefix="hypervisor-counts-") as directory:
            root = Path(directory) / "repo"
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            store = StateStore(Path(directory) / "state.json")
            try:
                for count in (1, 2, 4, 6):
                    with self.subTest(count=count):
                        backend = TmuxBackend(
                            agent_command=["sh", "-c", "exec sleep 600"],
                            runner=runner,
                            state_store=store,
                            project_config=ProjectConfig.default(count),
                        )
                        group_id = f"TASK-{count}"
                        group = backend.create_group(group_id, root, task_type="fix")
                        self.assertEqual(group.branch, f"fix/{group_id}")
                        self.assertEqual(len(group.terminals), count)
                        self.assertEqual(len(backend.activate_group(group_id).terminals), count)
                        backend.send_key(group_id, count, "ctrl+q")
                        backend.deactivate_group(group_id)
                        reloaded = TmuxBackend(
                            runner=runner,
                            state_store=store,
                            project_config=ProjectConfig.default(1),
                        )
                        self.assertEqual(len(reloaded.status(group_id).terminals), count)
                        backend.remove_group(group_id, remove_worktree=True)
            finally:
                subprocess.run(
                    [binary, "-L", socket, "kill-server"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

    def test_group_lifecycle_on_private_server(self) -> None:
        binary = os.environ["HYPERVISOR_TEST_TMUX"]
        socket = f"hypervisor-test-{uuid4().hex[:12]}"

        def run_tmux(args: list[str], *, check: bool = False, **kwargs):
            return subprocess.run(
                [binary, "-L", socket, "-f", "/dev/null", *args[1:]], check=check, **kwargs
            )

        with tempfile.TemporaryDirectory(prefix="hypervisor-integration-") as directory:
            root = Path(directory) / "repo"
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            backend = TmuxBackend(
                agent_command=["sh", "-c", "printf 'READY\\n'; exec sleep 600"],
                runner=run_tmux,
                agent_runner=Mock(return_value=subprocess.CompletedProcess([], 0, "", "")),
                state_store=StateStore(Path(directory) / "groups.json"),
            )
            try:
                self.assertFalse(backend.create_group("check", root).active)
                group = backend.activate_group("check")
                self.assertEqual(group.state, "running")
                backend.resize_terminal("check", 1, 90, 30)
                backend.send_text("check", 1, "hello")
                for key in ("ctrl+home", "ctrl+end", "shift+tab", "ctrl+shift+up", "ctrl+q"):
                    backend.send_key("check", 1, key)
                backend.capture("check", 1)
                backend.stop_terminal("check", 2)
                self.assertEqual(backend.status("check").terminals[1].state, TerminalState.STOPPED)
                backend.restart_terminal("check", 2)
                self.assertEqual(backend.status("check").terminals[1].state, TerminalState.RUNNING)
                self.assertFalse(backend.deactivate_group("check").active)
                backend.remove_group("check", remove_worktree=True)
                self.assertEqual(backend.list_groups(), [])
                self.assertFalse(Path(group.worktree_path).exists())
            finally:
                subprocess.run(
                    [binary, "-L", socket, "kill-server"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
