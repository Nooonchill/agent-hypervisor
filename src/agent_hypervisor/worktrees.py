from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .models import HypervisorError

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class Worktree:
    repository_root: Path
    path: Path
    branch: str


class GitWorktreeManager:
    def __init__(self, runner: Runner = subprocess.run) -> None:
        self._runner = runner

    def create(self, group_id: str, cwd: str | Path, branch_type: str = "hypervisor") -> Worktree:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", branch_type):
            raise HypervisorError("Branch type must use lowercase letters, digits or dashes")
        self._git("check-ref-format", "--branch", f"{branch_type}/{group_id}")
        source_root = Path(self._git("-C", str(cwd), "rev-parse", "--show-toplevel").strip())
        common_dir = Path(
            self._git("-C", str(source_root), "rev-parse", "--git-common-dir").strip()
        )
        if not common_dir.is_absolute():
            common_dir = (source_root / common_dir).resolve()
        root = common_dir.parent if common_dir.name == ".git" else source_root
        head = self._git(
            "-C",
            str(source_root),
            "rev-parse",
            "--verify",
            "HEAD",
            check=False,
        )
        assert isinstance(head, subprocess.CompletedProcess)
        start_point = head.stdout.strip() if head.returncode == 0 else None
        base = root.parent / f".{root.name}-worktrees"
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", group_id).strip("-.") or "group"
        suffix = 1
        while True:
            name = slug if suffix == 1 else f"{slug}-{suffix}"
            path = base / name
            branch = f"{branch_type}/{name}"
            branch_exists = (
                self._git(
                    "-C",
                    str(root),
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{branch}",
                    check=False,
                ).returncode
                == 0
            )
            if not path.exists() and not branch_exists:
                break
            suffix += 1
        base.mkdir(parents=True, exist_ok=True)
        if start_point is None:
            # Git before 2.42 cannot add an orphan worktree directly. A temporary
            # unreferenced commit lets us attach it, then make its HEAD unborn.
            # Neither the main checkout's HEAD nor any existing branch changes.
            tree = self._git("-C", str(root), "mktree", input_text="").strip()
            bootstrap = self._git(
                "-C",
                str(root),
                "-c",
                "user.name=Agent Hypervisor",
                "-c",
                "user.email=hypervisor@localhost",
                "-c",
                "commit.gpgSign=false",
                "commit-tree",
                tree,
                input_text="Initialize worktree metadata\n",
            ).strip()
            self._git(
                "-C",
                str(root),
                "worktree",
                "add",
                "--detach",
                str(path),
                bootstrap,
            )
            self._git("-C", str(path), "symbolic-ref", "HEAD", f"refs/heads/{branch}")
        else:
            self._git(
                "-C",
                str(root),
                "worktree",
                "add",
                "-b",
                branch,
                str(path),
                start_point,
            )
        return Worktree(root, path, branch)

    def remove(self, repository_root: str | Path, worktree_path: str | Path) -> None:
        root = Path(repository_root)
        path = Path(worktree_path)
        if not path.exists():
            return
        status = self._git("-C", str(path), "status", "--porcelain")
        assert isinstance(status, str)
        if status.strip():
            raise HypervisorError(f"Worktree has uncommitted changes and was kept: {path}")
        self._git("-C", str(root), "worktree", "remove", str(path))

    def _git(
        self, *args: str, check: bool = True, input_text: str | None = None
    ) -> str | subprocess.CompletedProcess[str]:
        try:
            result = self._runner(
                ["git", *args],
                check=False,
                capture_output=True,
                text=True,
                input=input_text,
            )
        except FileNotFoundError as error:
            raise HypervisorError("git is not installed or is not available in PATH") from error
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
            raise HypervisorError(detail)
        return result.stdout if check else result
