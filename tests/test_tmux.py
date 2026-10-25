from __future__ import annotations

import subprocess
import unittest
from unittest.mock import Mock, patch

from agent_hypervisor.models import HypervisorError
from agent_hypervisor.tmux import TmuxBackend, normalize_key


def completed(args: list[str], code: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args, code, stdout, stderr)


class TmuxBackendTests(unittest.TestCase):
    def test_default_command_launches_qwen_through_npm_exec(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            backend = TmuxBackend(runner=Mock())
        self.assertEqual(
            backend.agent_command,
            ("npm", "exec", "--yes", "--package=@qwen-code/qwen-code", "--", "qwen"),
        )

    def test_group_id_is_restricted_to_tmux_safe_names(self) -> None:
        self.assertEqual(TmuxBackend.session_name("task-123"), "agent-hypervisor-task-123")
        with self.assertRaises(HypervisorError):
            TmuxBackend.session_name("bad:id")

    def test_normalize_control_key(self) -> None:
        self.assertEqual(normalize_key("ctrl-q"), "C-q")
        self.assertEqual(normalize_key("enter"), "Enter")
        self.assertEqual(normalize_key("F12"), "F12")
        self.assertEqual(normalize_key("shift+up"), "S-Up")
        self.assertEqual(normalize_key("shift+down"), "S-Down")
        self.assertEqual(normalize_key("shift+tab"), "S-Tab")
        self.assertEqual(normalize_key("ctrl+home"), "C-Home")
        self.assertEqual(normalize_key("ctrl+end"), "C-End")
        self.assertEqual(normalize_key("ctrl+shift+up"), "C-S-Up")
        with self.assertRaises(HypervisorError):
            normalize_key("launch-missiles")

    def test_send_text_pastes_literal_multiline_text_then_queues_with_ctrl_q(self) -> None:
        def run(args, **kwargs):
            rows = "1\tterminal-1\t0\t\tbash\trunning\n" if args[1] == "list-windows" else ""
            return completed(args, stdout=rows)

        runner = Mock(side_effect=run)
        backend = TmuxBackend(runner=runner)
        prompt = "hello; $(safe)\nsecond line"
        backend.send_text("123", 1, prompt)
        calls = [call.args[0] for call in runner.call_args_list]
        load = next(call for call in runner.call_args_list if call.args[0][1] == "load-buffer")
        self.assertEqual(load.kwargs["input"], prompt)
        paste = next(call for call in calls if call[1] == "paste-buffer")
        self.assertIn("-p", paste)
        self.assertIn("-r", paste)
        self.assertTrue(any(call[1] == "send-keys" and call[-1] == "C-q" for call in calls))

    def test_status_is_collected_with_one_tmux_process(self) -> None:
        runner = Mock(
            return_value=completed(
                [],
                stdout=(
                    "2\tterminal-1\t0\t\tcodex\trunning\n"
                    "2\tterminal-2\t1\t7\tcodex\trunning\n"
                    "2\tterminal-3\t0\t\tsleep\tstopped\n"
                ),
            )
        )
        status = TmuxBackend(runner=runner).status("123")

        self.assertEqual(runner.call_count, 1)
        self.assertEqual(status.attached_clients, 2)
        self.assertEqual(status.terminals[1].exit_code, 7)
        self.assertEqual(status.terminals[2].state.value, "stopped")

    def test_capture_uses_one_tmux_process(self) -> None:
        runner = Mock(return_value=completed([], stdout="screen\n"))
        output = TmuxBackend(runner=runner).capture("123", 1, lines=0)

        self.assertEqual(output, "screen")
        self.assertEqual(runner.call_count, 1)
        self.assertNotIn("-S", runner.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
