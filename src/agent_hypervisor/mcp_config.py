from __future__ import annotations

import sys
from pathlib import Path

from .state import GroupRecord

MCP_SERVER_NAME = "agent-hypervisor"
SESSION_ENV = (
    "HYPERVISOR_GROUP",
    "HYPERVISOR_AGENT",
    "HYPERVISOR_SESSION",
    "HYPERVISOR_STATE_PATH",
)


def qwen_settings() -> dict:
    return {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "command": sys.executable,
                "args": ["-m", "agent_hypervisor", "mcp", "serve"],
                "env": {
                    "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
                    **{key: "${" + key + "}" for key in SESSION_ENV},
                },
                "timeout": 600000,
                "trust": False,
            }
        }
    }


def session_environment(
    record: GroupRecord, agent_id: str, session_id: str, state_path: Path
) -> list[str]:
    """Each Qwen process gets its own immutable MCP identity; settings remain shared."""
    return [
        "env",
        f"HYPERVISOR_GROUP={record.group_id}",
        f"HYPERVISOR_AGENT={agent_id}",
        f"HYPERVISOR_SESSION={session_id}",
        f"HYPERVISOR_STATE_PATH={state_path.resolve()}",
    ]
