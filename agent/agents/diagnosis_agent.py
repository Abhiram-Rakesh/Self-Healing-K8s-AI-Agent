"""
Second stage: cost-guards the Gemini MCP tool-calling loop.

Context collection and memory recall are now handled inside GeminiClient via
the K8s MCP server — DiagnosisAgent's only job is to check the daily budget
before allowing a Gemini investigation to start.
"""

from __future__ import annotations

import logging
from typing import Any

from agent.cost_guard import CostGuard
from agent.gemini_client import NOTIFY_ONLY_FALLBACK, GeminiClient, _normalize_plan

logger = logging.getLogger(__name__)


class DiagnosisAgent:
    """Budget gate around the Gemini MCP tool-calling loop."""

    def __init__(self, gemini: GeminiClient, cost_guard: CostGuard) -> None:
        self.gemini = gemini
        self.cost_guard = cost_guard

    def diagnose(self, triage_result: dict[str, Any]) -> dict[str, Any]:
        alert_name = triage_result.get("alert_name", "UnknownAlert")

        if not self.cost_guard.check_and_increment():
            plan = dict(NOTIFY_ONLY_FALLBACK)
            plan["reason"] = "Daily Gemini request budget exhausted — notify_only."
            logger.warning("Cost guard denied Gemini call for %s", alert_name)
            return _normalize_plan(plan)

        plan = self.gemini.diagnose(triage_result)
        plan.setdefault("alert_name", alert_name)
        return plan
