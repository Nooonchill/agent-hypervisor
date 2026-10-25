from __future__ import annotations

import json
import logging
import os
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from pathlib import Path
from logging.handlers import RotatingFileHandler
from uuid import uuid4

from .models import HypervisorError, WorkflowBusy
from .state import utc_now


class EventStore:
    """Append-only workflow history. Readers never wait for a running hard gate."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "events.jsonl"

    def read(self) -> list[dict]:
        if self.path.is_symlink():
            raise HypervisorError("Workflow journal must not be a symbolic link")
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as error:
            raise HypervisorError(f"Cannot read workflow events: {error}") from error
        records = []
        for line in raw.splitlines(keepends=True):
            # An interrupted append cannot invalidate earlier durable events.
            if not line.endswith(b"\n"):
                break
            try:
                event = json.loads(line)
                if event["seq"] != len(records) + 1 or not isinstance(event["data"], dict):
                    raise ValueError("invalid event sequence or payload")
                records.append(event)
            except (ValueError, TypeError, KeyError) as error:
                raise HypervisorError(
                    f"Invalid workflow event {len(records) + 1}: {error}"
                ) from error
        return records

    @contextmanager
    def transaction(self) -> Iterator[EventStore]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "workflow.lock").open("a") as handle:
            try:
                flock(handle.fileno(), LOCK_EX | LOCK_NB)
            except BlockingIOError as error:
                raise WorkflowBusy(
                    "Another workflow operation is running for this group"
                ) from error
            try:
                with self._journal_lock():
                    self.read()
                yield self
            finally:
                flock(handle.fileno(), LOCK_UN)

    @contextmanager
    def _journal_lock(self) -> Iterator[None]:
        """Short writer lock: mode switches need not wait for lengthy hard gates."""
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "events.lock").open("a") as handle:
            flock(handle.fileno(), LOCK_EX)
            try:
                if self.path.is_symlink():
                    raise HypervisorError("Workflow journal must not be a symbolic link")
                if self.path.exists():
                    raw = self.path.read_bytes()
                    if raw and not raw.endswith(b"\n"):
                        with self.path.open("r+b") as stream:
                            stream.truncate(raw.rfind(b"\n") + 1)
                            stream.flush()
                            os.fsync(stream.fileno())
                yield
            finally:
                flock(handle.fileno(), LOCK_UN)

    @contextmanager
    def agent_gate(self, agent_id: str) -> Iterator[None]:
        """Serialize a mode switch with the final paste + submit, across processes."""
        if not agent_id or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for char in agent_id
        ):
            raise HypervisorError("Invalid agent identifier for delivery gate")
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / f"agent-{agent_id}.lock").open("a") as handle:
            flock(handle.fileno(), LOCK_EX)
            try:
                yield
            finally:
                flock(handle.fileno(), LOCK_UN)

    def append(
        self,
        event_type: str,
        *,
        stage: str = "",
        attempt: str = "",
        actor: str = "hypervisor",
        **data: object,
    ) -> dict:
        """Workflow transitions also hold transaction(); mode events use this short lock."""
        with self._journal_lock():
            return self._append(event_type, stage=stage, attempt=attempt, actor=actor, **data)

    def _append(
        self, event_type: str, *, stage: str, attempt: str, actor: str, **data: object
    ) -> dict:
        event = {
            "seq": len(self.read()) + 1,
            "id": uuid4().hex,
            "time": utc_now(),
            "type": event_type,
            "stage": stage,
            "attempt": attempt,
            "actor": actor,
            "data": data,
        }
        payload = (json.dumps(event, ensure_ascii=False) + "\n").encode()
        with self.path.open("ab", buffering=0) as stream:
            remaining = memoryview(payload)
            while remaining:
                remaining = remaining[stream.write(remaining) :]
            os.fsync(stream.fileno())
        return event

    def report(self, relative_path: str) -> Path:
        path = (self.directory / relative_path).resolve()
        root = self.directory.resolve()
        reports = (root / "reports").resolve()
        if not reports.is_relative_to(root) or not path.is_relative_to(reports):
            raise HypervisorError("Report path must be inside this task's reports directory")
        return path

    def log_error(self, error: Exception) -> None:
        directory = (self.directory / "logs").resolve()
        if not directory.is_relative_to(self.directory.resolve()):
            raise HypervisorError("Workflow logs must stay inside the task data directory")
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            directory / "workflow.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        try:
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            handler.emit(
                logging.makeLogRecord(
                    {
                        "levelno": logging.ERROR,
                        "levelname": "ERROR",
                        "msg": "".join(traceback.format_exception(error)),
                        "args": (),
                    }
                )
            )
        finally:
            handler.close()
