"""Gemini 2.5 Flash client with MCP tool-calling loop.

Gemini drives investigation and remediation by calling tools in mcp_server.
The loop terminates when Gemini calls record_outcome (which is captured,
not dispatched — the recording happens inside that same call via call_tool
for all other tools, or directly for record_outcome).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

try:
    from google import genai  # type: ignore[import-untyped]
    from google.genai import types as genai_types  # type: ignore[import-untyped]

    _HAS_GENAI = True
except Exception:
    genai = None  # type: ignore[assignment]
    genai_types = None  # type: ignore[assignment]
    _HAS_GENAI = False

from agent import mcp_server

logger = logging.getLogger(__name__)

DEFAULT_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
MAX_TURNS: int = int(os.environ.get("GEMINI_MAX_TURNS", "8"))

VALID_ACTIONS: frozenset[str] = frozenset(
    {
        "restart_deployment",
        "scale_deployment",
        "cordon_node",
        "drain_node",
        "notify_only",
        "no_action",
    }
)

SYSTEM_PROMPT = """\
You are an expert Kubernetes Site Reliability Engineer (SRE).

You have investigation tools that let you inspect a failing workload.
Use them to gather evidence, then act, then always call record_outcome last.

INVESTIGATION ORDER (adjust based on the alert type):
1. get_pod_events + get_pod_logs — fastest path to root cause
2. describe_pod — restart count, resource limits, current state
3. get_node_conditions — if pod is on a node that may be unhealthy
4. recall_past_cases — how similar alerts were resolved before
5. resolve_deployment — when you need the Deployment name

WHEN YOU HAVE ENOUGH EVIDENCE:
- If confidence >= 0.75: call the appropriate write tool (restart_deployment,
  scale_deployment, cordon_node, drain_node)
- If confidence < 0.70: do not call any write tool
- Always call record_outcome as your final tool call

STRICT RULES:
- deployment parameter in write tools MUST be a Deployment name, never a Pod name
- confidence < 0.70 → set action="notify_only" in record_outcome, skip write tools
- NEVER attempt to delete namespaces or cluster-scoped resources
- Prefer the least-disruptive action that addresses the root cause
- Call record_outcome EXACTLY ONCE as your last action
"""

NOTIFY_ONLY_FALLBACK: dict[str, Any] = {
    "action": "notify_only",
    "target": "unknown",
    "namespace": "unknown",
    "diagnosis": "Unable to diagnose.",
    "confidence": 0.0,
    "executed": False,
    "outcome": "Gemini unavailable or loop limit reached.",
    "dry_run": False,
}


class GeminiClient:
    """Gemini client with MCP tool-calling loop for agentic K8s diagnosis."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        max_retries: int = 4,
        max_turns: int = MAX_TURNS,
    ) -> None:
        self.model = model or DEFAULT_MODEL
        self.max_retries = max_retries
        self.max_turns = max_turns
        self._client: Any = None

        if not _HAS_GENAI:
            logger.warning("google-genai not installed — Gemini client disabled")
            return

        key = api_key or os.environ.get("GEMINI_API_KEY", "")
        if not key:
            logger.warning("GEMINI_API_KEY not set — Gemini client disabled")
            return

        try:
            self._client = genai.Client(api_key=key)
        except Exception as exc:
            logger.error("Gemini client init failed: %s", exc)

    def _make_config(self) -> Any:
        declarations = [
            genai_types.FunctionDeclaration(
                name=d["name"],
                description=d["description"],
                parameters=d["parameters"],
            )
            for d in mcp_server.ALL_DECLARATIONS
        ]
        return genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[genai_types.Tool(function_declarations=declarations)],
            temperature=0.2,
        )

    def diagnose(self, triage_result: dict) -> dict:
        """Run the MCP tool-calling loop. Always returns a result dict — never raises."""
        if self._client is None or genai_types is None:
            return dict(NOTIFY_ONLY_FALLBACK)

        alert = triage_result.get("alert", {}) or {}
        labels = alert.get("labels") or {}
        alert_name = triage_result.get(
            "alert_name", labels.get("alertname", "UnknownAlert")
        )
        namespace = triage_result.get("namespace", labels.get("namespace", ""))
        pod = triage_result.get("pod", labels.get("pod", ""))

        initial_message = (
            f"Diagnose this Kubernetes alert and take appropriate action.\n\n"
            f"Alert:     {alert_name}\n"
            f"Namespace: {namespace}\n"
            f"Pod:       {pod}\n"
            f"Labels:    {json.dumps(labels)}\n\n"
            f"Investigate with tools, act if confident, then call record_outcome."
        )

        config = self._make_config()
        contents: list[Any] = [
            genai_types.Content(
                role="user",
                parts=[genai_types.Part(text=initial_message)],
            )
        ]
        outcome_args: dict | None = None

        for turn in range(self.max_turns):
            response = self._call_gemini(contents, config, turn)
            if response is None:
                return dict(NOTIFY_ONLY_FALLBACK)

            candidates = getattr(response, "candidates", []) or []
            if not candidates:
                break

            candidate = candidates[0]
            content = getattr(candidate, "content", None)
            if content is None:
                break

            parts = getattr(content, "parts", []) or []
            contents.append(content)

            fc_parts = [p for p in parts if getattr(p, "function_call", None)]
            if not fc_parts:
                logger.warning(
                    "Gemini returned text without tool call at turn %d", turn + 1
                )
                break

            tool_response_parts: list[Any] = []
            stop_loop = False

            for part in fc_parts:
                fc = part.function_call
                name = fc.name
                args = dict(fc.args) if fc.args else {}

                if name == "record_outcome":
                    outcome_args = args
                    tool_response_parts.append(
                        genai_types.Part(
                            function_response=genai_types.FunctionResponse(
                                name=name,
                                response={"status": "recorded"},
                            )
                        )
                    )
                    stop_loop = True
                    break
                else:
                    result = mcp_server.call_tool(name, args)
                    logger.info("MCP tool %-25s → %d chars", name, len(result))
                    tool_response_parts.append(
                        genai_types.Part(
                            function_response=genai_types.FunctionResponse(
                                name=name,
                                response={"result": result},
                            )
                        )
                    )

            contents.append(genai_types.Content(role="user", parts=tool_response_parts))

            if stop_loop:
                break

        if outcome_args is not None:
            return self._build_result(outcome_args)
        logger.error(
            "Gemini exhausted %d turns without calling record_outcome — notify_only fallback",
            self.max_turns,
        )
        return dict(NOTIFY_ONLY_FALLBACK)

    def _call_gemini(self, contents: list[Any], config: Any, turn: int) -> Any:
        backoffs = [1, 2, 4, 8]
        for attempt in range(self.max_retries):
            try:
                return self._client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=config,
                )
            except Exception as exc:
                msg = str(exc)
                is_rate_limit = (
                    "429" in msg
                    or "RESOURCE_EXHAUSTED" in msg.upper()
                    or "rate" in msg.lower()
                )
                if is_rate_limit and attempt < self.max_retries - 1:
                    delay = backoffs[min(attempt, len(backoffs) - 1)]
                    logger.warning(
                        "Gemini rate-limited (turn %d, attempt %d) — sleeping %ds",
                        turn + 1,
                        attempt + 1,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                logger.error(
                    "Gemini API error (turn %d, attempt %d): %s",
                    turn + 1,
                    attempt + 1,
                    exc,
                )
                if attempt + 1 >= self.max_retries:
                    return None
        return None

    @staticmethod
    def _build_result(outcome_args: dict) -> dict:
        return {
            "action": str(outcome_args.get("action", "notify_only")),
            "target": str(outcome_args.get("target", "unknown")),
            "namespace": str(outcome_args.get("namespace", "unknown")),
            "diagnosis": str(outcome_args.get("diagnosis", "")),
            "confidence": float(outcome_args.get("confidence", 0.0)),
            "executed": bool(outcome_args.get("executed", False)),
            "outcome": str(outcome_args.get("outcome", "")),
            "dry_run": bool(outcome_args.get("dry_run", False)),
        }
