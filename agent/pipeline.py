"""Alert triage (deduplication + severity scoring) and pipeline runner.

Gemini drives investigation and recording. This module's only job is to
deduplicate alerts and hand non-duplicate firing alerts to Gemini.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from agent import mcp_server

if TYPE_CHECKING:
    from agent.cost_guard import CostGuard
    from agent.gemini import GeminiClient

logger = logging.getLogger(__name__)

DEDUP_TTL_SECONDS: int = 300
SEVERITY_WEIGHTS: dict[str, int] = {"critical": 3, "warning": 2, "info": 1}

_seen: dict[str, float] = {}
_seen_lock: threading.Lock = threading.Lock()


def _alert_key(alert: dict) -> str:
    labels = alert.get("labels") or {}
    return (
        f"{labels.get('alertname', 'unknown')}"
        f":{labels.get('namespace', '-')}"
        f":{labels.get('pod', '-')}"
    )


def triage(alert: dict) -> dict | None:
    """Return a triage result dict or None if this alert is a duplicate."""
    labels = alert.get("labels") or {}
    key = _alert_key(alert)
    severity = labels.get("severity", "info").lower()
    now = time.monotonic()

    with _seen_lock:
        # Prune expired entries
        expired = [k for k, ts in _seen.items() if now - ts > DEDUP_TTL_SECONDS]
        for k in expired:
            del _seen[k]

        if key in _seen:
            logger.debug("Duplicate alert %s — dropping", key)
            return None

        _seen[key] = now

    return {
        "alert_key": key,
        "alert_name": labels.get("alertname", "unknown"),
        "severity": severity,
        "severity_weight": SEVERITY_WEIGHTS.get(severity, 1),
        "namespace": labels.get("namespace", ""),
        "pod": labels.get("pod", ""),
        "alert": alert,
    }


def run(alert: dict, gemini_client: GeminiClient, cost_guard: CostGuard) -> None:
    """Process one alert: scale-down resolved alerts, triage+diagnose firing ones."""
    if alert.get("status") == "resolved":
        key = _alert_key(alert)
        mcp_server.scale_down_if_resolved(key)
        return

    result = triage(alert)
    if result is None:
        return

    if not cost_guard.check_and_increment():
        logger.warning("Daily budget exhausted — skipping %s", result["alert_key"])
        return

    # record_outcome is called as a Gemini tool call inside diagnose()
    gemini_client.diagnose(result)
