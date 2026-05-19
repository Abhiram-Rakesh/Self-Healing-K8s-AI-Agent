"""
Kubernetes MCP server — exposes K8s investigation tools for Gemini function calling.

Investigation tools (read-only):
  get_pod_logs        last 50 lines; tries previous container first for crash-loops
  get_pod_events      recent K8s events (BackOff, OOMKilled, FailedScheduling …)
  describe_pod        phase, node, images, resource limits, restart count
  get_node_conditions Ready, MemoryPressure, DiskPressure, PIDPressure
  resolve_deployment  walks Pod → ReplicaSet → Deployment ownership chain
  recall_past_cases   last 3 matching incidents from SQLite memory

Output tool (Gemini calls this to finalise the investigation):
  submit_plan         structured remediation plan; captured by GeminiClient,
                      not executed here — it flows through RemediationAgent as usual

Standalone MCP server (for Claude Desktop / other MCP clients):
  python -m agent.k8s_mcp_server
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from mcp.server.fastmcp import FastMCP
    _HAS_MCP = True
except Exception:
    FastMCP = None  # type: ignore
    _HAS_MCP = False

# ---------------------------------------------------------------------------
# Injected dependencies — set once at startup via init()
# ---------------------------------------------------------------------------

_cb: Any = None   # agent.context_builder.ContextBuilder
_mem: Any = None  # agent.memory.RunbookMemory

mcp = FastMCP("kagent-k8s") if _HAS_MCP else None  # type: ignore[operator]


def init(context_builder: Any, memory: Any) -> None:
    """Inject K8s client and memory. Must be called before any tool is invoked."""
    global _cb, _mem
    _cb = context_builder
    _mem = memory


# ---------------------------------------------------------------------------
# Tool implementations (plain functions called in-process by GeminiClient)
# ---------------------------------------------------------------------------

def _get_pod_logs(namespace: str, pod: str) -> str:
    if _cb is None:
        return "MCP server not initialised — call k8s_mcp_server.init() first"
    return str(_cb._fetch_pod_logs(namespace, pod))


def _get_pod_events(namespace: str, pod: str) -> str:
    if _cb is None:
        return "MCP server not initialised"
    events = _cb._fetch_events(namespace, pod)
    return json.dumps(events, default=str, indent=2) if events else "No events found."


def _describe_pod(namespace: str, pod: str) -> str:
    if _cb is None:
        return "MCP server not initialised"
    result = _cb._describe_pod(namespace, pod)
    return json.dumps(result, default=str, indent=2) if isinstance(result, dict) else str(result)


def _get_node_conditions(node_name: str) -> str:
    if _cb is None:
        return "MCP server not initialised"
    result = _cb._node_conditions(node_name)
    return json.dumps(result, default=str, indent=2) if isinstance(result, list) else str(result)


def _resolve_deployment(namespace: str, pod: str) -> str:
    if _cb is None:
        return "MCP server not initialised"
    name = _cb._resolve_deployment(namespace, pod)
    return name if name else "Could not resolve deployment"


def _recall_past_cases(alert_type: str) -> str:
    if _mem is None:
        return "No past cases."
    return str(_mem.recall(alert_type))


# ---------------------------------------------------------------------------
# FastMCP registrations — active when running as a standalone MCP server
# ---------------------------------------------------------------------------

if mcp is not None:

    @mcp.tool()
    def get_pod_logs(namespace: str, pod: str) -> str:
        """Get the last 50 log lines. Tries the previous (crashed) container first."""
        return _get_pod_logs(namespace, pod)

    @mcp.tool()
    def get_pod_events(namespace: str, pod: str) -> str:
        """Get recent K8s events for a pod (BackOff, OOMKilled, FailedScheduling …)."""
        return _get_pod_events(namespace, pod)

    @mcp.tool()
    def describe_pod(namespace: str, pod: str) -> str:
        """Get pod state: phase, node, container images, resource limits, restart count."""
        return _describe_pod(namespace, pod)

    @mcp.tool()
    def get_node_conditions(node_name: str) -> str:
        """Get node health conditions: Ready, MemoryPressure, DiskPressure, PIDPressure."""
        return _get_node_conditions(node_name)

    @mcp.tool()
    def resolve_deployment(namespace: str, pod: str) -> str:
        """Walk Pod → ReplicaSet → Deployment ownership chain to find the Deployment name."""
        return _resolve_deployment(namespace, pod)

    @mcp.tool()
    def recall_past_cases(alert_type: str) -> str:
        """Recall the last 3 similar incidents from memory to inform the diagnosis."""
        return _recall_past_cases(alert_type)


# ---------------------------------------------------------------------------
# Gemini function-declaration schemas
# ---------------------------------------------------------------------------

GEMINI_TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "get_pod_logs",
        "description": (
            "Get the last 50 log lines from a pod. "
            "Tries the previous (crashed) container first — most useful for CrashLoopBackOff."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Kubernetes namespace"},
                "pod":       {"type": "string", "description": "Pod name"},
            },
            "required": ["namespace", "pod"],
        },
    },
    {
        "name": "get_pod_events",
        "description": "Get recent Kubernetes events for a pod (BackOff, OOMKilled, FailedScheduling, etc.)",
        "parameters": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "pod":       {"type": "string"},
            },
            "required": ["namespace", "pod"],
        },
    },
    {
        "name": "describe_pod",
        "description": "Get pod state: phase, node, container images, resource limits, restart count.",
        "parameters": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "pod":       {"type": "string"},
            },
            "required": ["namespace", "pod"],
        },
    },
    {
        "name": "get_node_conditions",
        "description": "Get health conditions for a node (Ready, MemoryPressure, DiskPressure, PIDPressure).",
        "parameters": {
            "type": "object",
            "properties": {
                "node_name": {"type": "string", "description": "Node name"},
            },
            "required": ["node_name"],
        },
    },
    {
        "name": "resolve_deployment",
        "description": (
            "Find the Deployment that owns a pod by walking "
            "Pod → ReplicaSet → Deployment ownership chain."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "pod":       {"type": "string"},
            },
            "required": ["namespace", "pod"],
        },
    },
    {
        "name": "recall_past_cases",
        "description": "Recall the last 3 similar incidents from memory to inform the diagnosis.",
        "parameters": {
            "type": "object",
            "properties": {
                "alert_type": {
                    "type": "string",
                    "description": "Alert name, e.g. PodCrashLooping",
                },
            },
            "required": ["alert_type"],
        },
    },
]

SUBMIT_PLAN_DECLARATION: dict[str, Any] = {
    "name": "submit_plan",
    "description": (
        "Submit your final remediation plan once you have gathered enough evidence. "
        "Call this EXACTLY ONCE to conclude your investigation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "diagnosis": {
                "type": "string",
                "description": "Root-cause analysis in plain English",
            },
            "action": {
                "type": "string",
                "enum": [
                    "restart_pod",
                    "scale_up",
                    "cordon_node",
                    "drain_node",
                    "notify_only",
                    "no_action",
                ],
                "description": "Remediation action to take",
            },
            "target": {
                "type": "string",
                "description": "Deployment name to act on — NEVER a pod name",
            },
            "target_namespace": {
                "type": "string",
                "description": "Kubernetes namespace of the target deployment",
            },
            "reason": {
                "type": "string",
                "description": "Brief explanation of why this action was chosen",
            },
            "confidence": {
                "type": "number",
                "description": "Confidence score 0.0–1.0. Use <0.70 alongside notify_only.",
            },
        },
        "required": [
            "diagnosis",
            "action",
            "target",
            "target_namespace",
            "reason",
            "confidence",
        ],
    },
}

ALL_DECLARATIONS: list[dict[str, Any]] = GEMINI_TOOL_DECLARATIONS + [SUBMIT_PLAN_DECLARATION]

# ---------------------------------------------------------------------------
# In-process dispatch (GeminiClient calls this — no MCP transport needed)
# ---------------------------------------------------------------------------

_DISPATCH: dict[str, Any] = {
    "get_pod_logs":        lambda a: _get_pod_logs(**a),
    "get_pod_events":      lambda a: _get_pod_events(**a),
    "describe_pod":        lambda a: _describe_pod(**a),
    "get_node_conditions": lambda a: _get_node_conditions(**a),
    "resolve_deployment":  lambda a: _resolve_deployment(**a),
    "recall_past_cases":   lambda a: _recall_past_cases(**a),
}


def call_tool(name: str, args: dict[str, Any]) -> str:
    """Execute an investigation tool by name and return its string result."""
    fn = _DISPATCH.get(name)
    if fn is None:
        logger.warning("call_tool: unknown tool %r", name)
        return f"Unknown tool: {name!r}"
    try:
        return str(fn(args))
    except Exception as exc:
        logger.error("call_tool %r failed: %s", name, exc)
        return f"Tool error ({name!r}): {exc}"


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not _HAS_MCP:
        raise SystemExit("mcp package not installed — run: pip install 'mcp>=1.0.0'")
    mcp.run()  # type: ignore[union-attr]
