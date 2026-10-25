from __future__ import annotations

from .agent_api import AgentBinding, AgentTools
from .contracts import WorkflowOperations


def create_server(workflow: WorkflowOperations, binding: AgentBinding):
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(
        "Agent Hypervisor",
        instructions="Use workflow_status to inspect your assignment and task_context for shared input. Submit only your assigned role. Hypervisor alone controls transitions; human approval is never an agent tool.",
    )
    tools = AgentTools(workflow, binding)
    for name in (
        "workflow_status",
        "task_context",
        "workflow_events",
        "read_report",
        "complete_stage",
        "submit_review",
    ):
        server.tool()(getattr(tools, name))
    return server
