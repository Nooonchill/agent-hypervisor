from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from agent_hypervisor.models import AgentManualMode, TerminalState
from agent_hypervisor.project_config import AgentSpec, FlowSpec, FlowStep, ProjectConfig
from agent_hypervisor.state import StateStore
from agent_hypervisor.tmux import TmuxBackend


@unittest.skipUnless(
    os.environ.get("HYPERVISOR_TEST_TMUX"), "set HYPERVISOR_TEST_TMUX to a tmux binary"
)
class TmuxIntegrationTests(unittest.TestCase):
    def test_standalone_agent_cli_callbacks_finish_workflow_without_tui(self) -> None:
        binary = os.environ["HYPERVISOR_TEST_TMUX"]
        socket = f"hypervisor-callback-{uuid4().hex[:12]}"

        def runner(args, *, check=False, **kwargs):
            return subprocess.run(
                [binary, "-L", socket, "-f", "/dev/null", *args[1:]], check=check, **kwargs
            )

        with tempfile.TemporaryDirectory(prefix="hypervisor-callback-") as directory:
            base = Path(directory)
            root = base / "repo"
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            for key, value in (("user.name", "Workflow Test"), ("user.email", "test@example.test")):
                subprocess.run(["git", "-C", str(root), "config", key, value], check=True)
            (root / "README.md").write_text("seed\n")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "seed"], check=True)
            wrapper = base / "bin"
            wrapper.mkdir()
            (wrapper / "tmux").write_text(
                f'#!/bin/sh\nexec {binary} -L {socket} -f /dev/null "$@"\n'
            )
            (wrapper / "tmux").chmod(0o755)
            program = base / "agent.py"
            log = base / "callbacks.log"
            # Deterministic terminal peer: exercises real child CLI processes and tmux,
            # without provider access or depending on model compliance in a test.
            program.write_text(
                "import os,sys,tty,shlex,subprocess,time\n"
                "from pathlib import Path\n"
                "os.environ['PATH']=sys.argv[1]+os.pathsep+os.environ['PATH']\n"
                "tty.setraw(sys.stdin.fileno())\n"
                "print('\\x1b[?2004h> ready', flush=True)\n"
                "pending=b''\n"
                "while True:\n"
                " pending+=os.read(sys.stdin.fileno(),65536)\n"
                " if b'\\x11' not in pending: continue\n"
                " text,pending=pending.split(b'\\x11',1)\n"
                " prompt=text.decode('utf-8').replace('\\x1b[200~','').replace('\\x1b[201~','')\n"
                " if 'WORKFLOW EXECUTION:' in prompt: Path('result.txt').write_text('done\\n')\n"
                " command=next(line for line in prompt.splitlines() if line.startswith('env HYPERVISOR_STATE_PATH='))\n"
                " for retry in range(10):\n"
                "  result=subprocess.run(shlex.split(command),capture_output=True,text=True)\n"
                "  if 'Another workflow operation' not in result.stderr: break\n"
                "  time.sleep(0.1)\n"
                " with open(sys.argv[2],'a') as log: log.write('CALLBACK DONE '+str(result.returncode)+' '+result.stdout+result.stderr+'\\n')\n",
                encoding="utf-8",
            )
            step = FlowStep(
                "worker",
                "Write result.txt",
                "implement",
                "worker",
                "Review result.txt",
                ("result.txt",),
                auto_commit=True,
            )
            backend = TmuxBackend(
                agent_command=[sys.executable, str(program), str(wrapper), str(log)],
                runner=runner,
                state_store=StateStore(base / "groups.json"),
                project_config=ProjectConfig(
                    (AgentSpec("worker", "Worker"),),
                    flows=(FlowSpec("delivery", "Delivery", (step,)),),
                ),
            )
            try:
                backend.create_group("TEST", root, request="Write the result artifact")
                backend.workflow.set_agent_mode("TEST", "worker", "manual")
                self.assertEqual(backend.workflow.start("TEST")["state"], "waiting-manual")
                self.assertFalse(backend.status("TEST").active)
                backend.workflow.set_agent_mode("TEST", "worker", "workflow")
                deadline = time.monotonic() + 20
                while (
                    time.monotonic() < deadline
                    and backend.workflow.status("TEST")["state"] != "completed"
                ):
                    time.sleep(0.05)
                self.assertEqual(
                    backend.workflow.status("TEST")["state"],
                    "completed",
                    log.read_text() if log.exists() else str(backend.workflow.events("TEST")),
                )
                self.assertEqual(
                    (Path(backend.state.get("TEST").worktree_path) / "result.txt").read_text(),
                    "done\n",
                )
                events = backend.workflow.events("TEST")
                self.assertEqual(sum(event["type"] == "stage.completed" for event in events), 1)
                execution = next(event for event in events if event["type"] == "stage.started")
                review = next(event for event in events if event["type"] == "review.started")
                self.assertNotEqual(
                    execution["data"]["executor_session"], review["data"]["reviewer_session"]
                )
                # The final event is visible before its CLI process has finished writing.
                # Wait for both callbacks to exit before deleting their working directory.
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if log.exists() and log.read_text().count("CALLBACK DONE 0 ") == 2:
                        break
                    time.sleep(0.02)
                self.assertEqual(log.read_text().count("CALLBACK DONE 0 "), 2, log.read_text())
            finally:
                subprocess.run(
                    [binary, "-L", socket, "kill-server"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

    def test_workflow_session_switching_preserves_process_and_queues_multiline_prompt(self) -> None:
        binary = os.environ["HYPERVISOR_TEST_TMUX"]
        socket = f"hypervisor-queue-{uuid4().hex[:12]}"

        def runner(args, *, check=False, **kwargs):
            return subprocess.run(
                [binary, "-L", socket, "-f", "/dev/null", *args[1:]], check=check, **kwargs
            )

        with tempfile.TemporaryDirectory(prefix="hypervisor-queue-") as directory:
            base = Path(directory)
            root = base / "repo"
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            program = base / "agent.py"
            received = base / "received.bin"
            program.write_text(
                "import os,sys,tty\n"
                "tty.setraw(sys.stdin.fileno())\n"
                "print('\\x1b[?2004h> ready', flush=True)\n"
                "while True:\n"
                " data=os.read(sys.stdin.fileno(), 65536)\n"
                " with open(sys.argv[1], 'ab', buffering=0) as log: log.write(data)\n",
                encoding="utf-8",
            )
            backend = TmuxBackend(
                agent_command=[sys.executable, str(program), str(received)],
                runner=runner,
                state_store=StateStore(base / "groups.json"),
                project_config=ProjectConfig(
                    ProjectConfig.default(1).agents,
                    flows=(
                        FlowSpec(
                            "flow",
                            "Flow",
                            (
                                FlowStep(
                                    "terminal-1", "Test", "test", "terminal-1", "Review", ("**/*",)
                                ),
                            ),
                        ),
                    ),
                ),
            )
            try:
                backend.create_group("queue", root)
                first, second = str(uuid4()), str(uuid4())
                number = backend.workflow_session("queue", "terminal-1", first)
                backend.queue_prompt("queue", number, "line one\nстрока два", wait_ready=True)
                deadline = time.monotonic() + 3
                while (
                    not received.exists() or b"\x11" not in received.read_bytes()
                ) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertIn("line one\nстрока два".encode(), received.read_bytes())
                self.assertIn(b"\x1b[200~", received.read_bytes())
                self.assertIn(b"\x11", received.read_bytes())  # Ctrl+Q, not Enter.
                backend.workflow.set_agent_mode("queue", "terminal-1", "manual")
                with self.assertRaises(AgentManualMode):
                    backend.queue_prompt("queue", number, "must not arrive")
                backend.send_literal("queue", number, "human input")
                deadline = time.monotonic() + 3
                while b"human input" not in received.read_bytes() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertIn(b"human input", received.read_bytes())
                self.assertNotIn(b"must not arrive", received.read_bytes())
                backend.workflow.set_agent_mode("queue", "terminal-1", "workflow")
                target = backend._target(backend.session_name("queue"), 1)
                first_pid = backend._tmux("display-message", "-p", "-t", target, "#{pane_pid}")
                backend.workflow_session("queue", "terminal-1", second)
                second_pid = backend._tmux("display-message", "-p", "-t", target, "#{pane_pid}")
                self.assertNotEqual(first_pid, second_pid)
                backend.workflow_session("queue", "terminal-1", first)
                self.assertEqual(
                    first_pid, backend._tmux("display-message", "-p", "-t", target, "#{pane_pid}")
                )
                self.assertEqual(backend.state.get("queue").active_sessions[1], first)
            finally:
                subprocess.run(
                    [binary, "-L", socket, "kill-server"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

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
                self.assertFalse(
                    backend.create_group("check", root, request="Lifecycle test").active
                )
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
