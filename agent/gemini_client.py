"""
Gemini 2.5 Flash client — MCP tool-calling loop for Kubernetes diagnosis.

Instead of receiving a pre-built context snapshot, Gemini now drives its own
investigation by calling K8s MCP tools (get_pod_logs, get_pod_events, …) and
finalises with submit_plan once it has enough evidence.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from agent import k8s_mcp_server

logger = logging.getLogger(__name__)

try:  # pragma: no cover
    from google import genai  # type: ignore
    from google.genai import errors as genai_errors  # type: ignore
    from google.genai import types as genai_types  # type: ignore

    _HAS_GENAI = True
except Exception:  # pragma: no cover
    genai = None  # type: ignore
    genai_errors = None  # type: ignore
    genai_types = None  # type: ignore
    _HAS_GENAI = False


DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
MAX_TURNS = int(os.environ.get("GEMINI_MAX_TURNS", "8"))

VALID_ACTIONS = {
    "restart_pod",
    "scale_up",
    "cordon_node",
    "drain_node",
    "notify_only",
    "no_action",
}

SYSTEM_PROMPT = """You are an expert Kubernetes Site Reliability Engineer (SRE).

You have investigation tools that let you inspect a failing workload.
Use them to gather evidence, then call submit_plan with your diagnosis.

INVESTIGATION APPROACH:
1. Start with get_pod_events and get_pod_logs — fastest path to the root cause
2. Call describe_pod to see restart count, resource limits, and current state
3. If the pod is running on a node, call get_node_conditions to check node health
4. Call recall_past_cases to see how similar alerts were resolved before
5. Call resolve_deployment if you need the Deployment name for submit_plan
6. Call submit_plan once you have sufficient evidence — do not over-investigate

STRICT RULES FOR submit_plan:
- action MUST be one of: restart_pod | scale_up | cordon_node | drain_node | notify_only | no_action
- target MUST be a Deployment name — NEVER a Pod name
- confidence < 0.70 → set action to notify_only
- NEVER recommend deleting namespaces or cluster-scoped resources
- Always prefer the least-disruptive action that addresses the root cause
- Call submit_plan EXACTLY ONCE to conclude your investigation
"""

NOTIFY_ONLY_FALLBACK: dict[str, Any] = {
    "diagnosis": "Unable to diagnose.",
    "action": "notify_only",
    "target": "unknown",
    "target_namespace": "unknown",
    "reason": "Gemini response was unavailable or max turns reached.",
    "confidence": 0.0,
}


def _normalize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    action = str(plan.get("action", "notify_only")).strip()
    if action not in VALID_ACTIONS:
        logger.warning("Gemini returned unknown action %r — coercing to notify_only", action)
        action = "notify_only"
    try:
        confidence = float(plan.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    if confidence < 0.70 and action != "notify_only":
        logger.info(
            "Gemini confidence %.2f below 0.70 — coercing %s → notify_only",
            confidence,
            action,
        )
        action = "notify_only"
    return {
        "diagnosis":        str(plan.get("diagnosis", "")).strip() or "No diagnosis provided.",
        "action":           action,
        "target":           str(plan.get("target", "unknown")).strip() or "unknown",
        "target_namespace": str(plan.get("target_namespace", "unknown")).strip() or "unknown",
        "reason":           str(plan.get("reason", "")).strip() or "No reason provided.",
        "confidence":       confidence,
    }


class GeminiClient:
    """Wraps Gemini with an MCP tool-calling loop for agentic K8s diagnosis."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        max_retries: int = 4,
        max_turns: int = MAX_TURNS,
    ) -> None:
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.model = model or DEFAULT_MODEL
        self.max_retries = max_retries
        self.max_turns = max_turns
        self._client = None
        if _HAS_GENAI and self.api_key:
            try:
                self._client = genai.Client(api_key=self.api_key)
            except Exception as exc:
                logger.error("Failed to construct Gemini client: %s", exc)

    def _make_config(self) -> Any:
        """Build GenerateContentConfig with all MCP tool declarations."""
        declarations = [
            genai_types.FunctionDeclaration(
                name=d["name"],
                description=d["description"],
                parameters=d["parameters"],
            )
            for d in k8s_mcp_server.ALL_DECLARATIONS
        ]
        return genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[genai_types.Tool(function_declarations=declarations)],
            temperature=0.2,
        )

    def _call_gemini(
        self, contents: list[Any], config: Any, turn: int
    ) -> Any:
        """Single Gemini API call with rate-limit retry. Returns response or None."""
        backoffs = [1, 2, 4, 8]
        for attempt in range(self.max_retries):
            try:
                return self._client.models.generate_content(  # type: ignore[union-attr]
                    model=self.model,
                    contents=contents,
                    config=config,
                )
            except Exception as exc:
                msg = str(exc)
                is_rate_limit = (
                    "429" in msg
                    or "RESOURCE_EXHAUSTED" in msg
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
                logger.error("Gemini API error (turn %d): %s", turn + 1, exc)
                return None
        return None

    def diagnose(self, triage_result: dict[str, Any]) -> dict[str, Any]:
        """
        Run the MCP tool-calling loop.
        Gemini calls investigation tools until it calls submit_plan.
        Always returns a plan dict — never raises.
        """
        if self._client is None or genai_types is None:
            plan = dict(NOTIFY_ONLY_FALLBACK)
            plan["reason"] = "Gemini client not initialised — check GEMINI_API_KEY"
            return _normalize_plan(plan)

        alert = triage_result.get("alert", {}) or {}
        labels = alert.get("labels") or {}
        alert_name = triage_result.get("alert_name", labels.get("alertname", "UnknownAlert"))
        namespace = labels.get("namespace", "")
        pod = labels.get("pod", "")

        initial_prompt = (
            f"Diagnose this Kubernetes alert and recommend a remediation action.\n\n"
            f"Alert:     {alert_name}\n"
            f"Namespace: {namespace}\n"
            f"Pod:       {pod}\n"
            f"Labels:    {json.dumps(labels)}\n\n"
            f"Use the investigation tools to gather evidence, then call submit_plan."
        )

        config = self._make_config()
        contents: list[Any] = [
            genai_types.Content(
                role="user",
                parts=[genai_types.Part(text=initial_prompt)],
            )
        ]

        for turn in range(self.max_turns):
            response = self._call_gemini(contents, config, turn)
            if response is None or not response.candidates:
                break

            candidate = response.candidates[0]
            contents.append(candidate.content)

            function_calls = [
                p.function_call
                for p in (candidate.content.parts or [])
                if getattr(p, "function_call", None)
            ]

            if not function_calls:
                # Gemini returned text without calling submit_plan
                logger.warning(
                    "Gemini stopped at turn %d without calling submit_plan", turn + 1
                )
                break

            # Process each function call
            tool_response_parts: list[Any] = []
            plan_input: dict[str, Any] | None = None

            for fc in function_calls:
                tool_args = dict(fc.args) if fc.args else {}

                if fc.name == "submit_plan":
                    plan_input = tool_args
                    # The API requires a function_response even for submit_plan
                    tool_response_parts.append(
                        genai_types.Part(
                            function_response=genai_types.FunctionResponse(
                                name="submit_plan",
                                response={"status": "plan_received"},
                            )
                        )
                    )
                else:
                    result = k8s_mcp_server.call_tool(fc.name, tool_args)
                    logger.info("MCP tool %-25s → %d chars", fc.name, len(result))
                    tool_response_parts.append(
                        genai_types.Part(
                            function_response=genai_types.FunctionResponse(
                                name=fc.name,
                                response={"result": result},
                            )
                        )
                    )

            if plan_input is not None:
                logger.info(
                    "Gemini MCP diagnosis complete: action=%s target=%s/%s confidence=%.2f",
                    plan_input.get("action"),
                    plan_input.get("target_namespace"),
                    plan_input.get("target"),
                    float(plan_input.get("confidence", 0.0)),
                )
                return _normalize_plan(plan_input)

            # Feed tool results back and continue
            contents.append(
                genai_types.Content(
                    role="user",
                    parts=tool_response_parts,
                )
            )

        logger.error(
            "Gemini exhausted %d turns without calling submit_plan — notify_only fallback",
            self.max_turns,
        )
        return _normalize_plan(dict(NOTIFY_ONLY_FALLBACK))
