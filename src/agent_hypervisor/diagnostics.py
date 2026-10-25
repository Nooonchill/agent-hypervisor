from __future__ import annotations

import logging
import os
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "agent_hypervisor"


def log_path() -> Path:
    configured = os.environ.get("HYPERVISOR_LOG_PATH")
    if configured:
        return Path(configured).expanduser()
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home / "agent-hypervisor" / "hypervisor.log"


def configure_diagnostics() -> Path:
    path = log_path()
    logger = logging.getLogger(LOGGER_NAME)
    if any(isinstance(handler, RotatingFileHandler) for handler in logger.handlers):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path,
        maxBytes=1_000_000,
        backupCount=2,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.propagate = False
    logger.debug("diagnostics_started pid=%s", os.getpid())
    return path


def read_log_tail(lines: int = 100) -> str:
    path = log_path()
    if not path.exists():
        return f"No diagnostics yet: {path}"
    with path.open(encoding="utf-8", errors="replace") as stream:
        tail = deque(stream, maxlen=max(1, min(lines, 5_000)))
    return "".join(tail).rstrip()
