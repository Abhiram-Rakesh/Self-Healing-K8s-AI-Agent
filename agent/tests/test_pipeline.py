"""Tests for agent/pipeline.py — deduplication, triage, and pipeline runner."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

import agent.pipeline as pipeline

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _alert(
    alertname: str = "PodCrashLooping",
    ns: str = "default",
    pod: str = "mypod",
    severity: str = "warning",
    status: str = "firing",
) -> dict:
    return {
        "status": status,
        "labels": {
            "alertname": alertname,
            "namespace": ns,
            "pod": pod,
            "severity": severity,
        },
    }


def _make_clients():
    gemini = MagicMock()
    gemini.diagnose.return_value = {
        "action": "notify_only",
        "confidence": 0.5,
        "executed": False,
        "diagnosis": "",
        "target": "",
        "namespace": "",
        "outcome": "",
        "dry_run": False,
    }
    cost_guard = MagicMock()
    cost_guard.check_and_increment.return_value = True
    return gemini, cost_guard


@pytest.fixture(autouse=True)
def reset_seen():
    """Clear the dedup cache before each test."""
    with pipeline._seen_lock:
        pipeline._seen.clear()
    yield
    with pipeline._seen_lock:
        pipeline._seen.clear()


# ---------------------------------------------------------------------------
# triage() tests
# ---------------------------------------------------------------------------


def test_first_alert_passes_through():
    result = pipeline.triage(_alert())
    assert result is not None
    assert result["alert_name"] == "PodCrashLooping"


def test_duplicate_within_ttl_dropped():
    alert = _alert()
    assert pipeline.triage(alert) is not None
    assert pipeline.triage(alert) is None


def test_duplicate_after_ttl_passes():
    alert = _alert()
    key = pipeline._alert_key(alert)
    assert pipeline.triage(alert) is not None

    with pipeline._seen_lock:
        pipeline._seen[key] = time.monotonic() - pipeline.DEDUP_TTL_SECONDS - 1

    assert pipeline.triage(alert) is not None


def test_different_pods_same_alert_not_duplicates():
    a1 = _alert(pod="pod-1")
    a2 = _alert(pod="pod-2")
    assert pipeline.triage(a1) is not None
    assert pipeline.triage(a2) is not None


def test_severity_weight_attached():
    result = pipeline.triage(_alert(severity="critical"))
    assert result is not None
    assert result["severity_weight"] == 3


# ---------------------------------------------------------------------------
# run() tests
# ---------------------------------------------------------------------------


def test_run_calls_diagnose_for_firing_alert():
    gemini, cost_guard = _make_clients()
    pipeline.run(_alert(), gemini, cost_guard)
    gemini.diagnose.assert_called_once()


def test_run_drops_duplicate():
    gemini, cost_guard = _make_clients()
    alert = _alert()
    pipeline.run(alert, gemini, cost_guard)
    pipeline.run(alert, gemini, cost_guard)
    gemini.diagnose.assert_called_once()


def test_resolved_alert_calls_scale_down_not_diagnose():
    gemini, cost_guard = _make_clients()
    with patch("agent.mcp_server.scale_down_if_resolved") as mock_sd:
        pipeline.run(_alert(status="resolved"), gemini, cost_guard)
    gemini.diagnose.assert_not_called()
    mock_sd.assert_called_once()


def test_budget_exhausted_skips_diagnose():
    gemini, cost_guard = _make_clients()
    cost_guard.check_and_increment.return_value = False
    pipeline.run(_alert(), gemini, cost_guard)
    gemini.diagnose.assert_not_called()
