"""Tests for the Gemini MCP tool-calling client."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.gemini_client import GeminiClient, _normalize_plan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client() -> GeminiClient:
    c = GeminiClient(api_key="fake-key", max_retries=2, max_turns=5)
    c._client = MagicMock()
    return c


def _fc_part(name: str, args: dict) -> SimpleNamespace:
    """Fake a Gemini Part that contains a function_call."""
    fc = SimpleNamespace(name=name, args=args)
    return SimpleNamespace(function_call=fc)


def _text_part(text: str) -> SimpleNamespace:
    """Fake a Gemini Part that contains plain text (no function_call)."""
    return SimpleNamespace(function_call=None, text=text)


def _candidate(parts: list) -> SimpleNamespace:
    content = SimpleNamespace(parts=parts)
    return SimpleNamespace(content=content)


def _response(*parts) -> SimpleNamespace:
    return SimpleNamespace(candidates=[_candidate(list(parts))])


def _triage(alertname: str = "PodCrashLooping", ns: str = "default", pod: str = "x") -> dict:
    return {
        "alert_name": alertname,
        "alert": {
            "labels": {"alertname": alertname, "namespace": ns, "pod": pod},
        },
    }


# ---------------------------------------------------------------------------
# submit_plan tool → plan returned correctly
# ---------------------------------------------------------------------------

def test_submit_plan_returns_normalized_plan() -> None:
    client = _make_client()
    plan_args = {
        "diagnosis": "OOM killed container",
        "action": "restart_pod",
        "target": "myapp",
        "target_namespace": "default",
        "reason": "memory limit exceeded",
        "confidence": 0.91,
    }
    client._client.models.generate_content.return_value = _response(
        _fc_part("submit_plan", plan_args)
    )
    plan = client.diagnose(_triage())
    assert plan["action"] == "restart_pod"
    assert plan["target"] == "myapp"
    assert plan["confidence"] == pytest.approx(0.91)


def test_investigation_tool_called_then_submit_plan() -> None:
    """Gemini calls an investigation tool on turn 1, submit_plan on turn 2."""
    client = _make_client()

    plan_args = {
        "diagnosis": "crash loop",
        "action": "restart_pod",
        "target": "svc",
        "target_namespace": "default",
        "reason": "OOMKill",
        "confidence": 0.88,
    }

    responses = [
        _response(_fc_part("get_pod_logs", {"namespace": "default", "pod": "x"})),
        _response(_fc_part("submit_plan", plan_args)),
    ]
    client._client.models.generate_content.side_effect = responses

    with patch("agent.k8s_mcp_server.call_tool", return_value="fake logs") as mock_tool:
        plan = client.diagnose(_triage())

    mock_tool.assert_called_once_with("get_pod_logs", {"namespace": "default", "pod": "x"})
    assert plan["action"] == "restart_pod"
    assert client._client.models.generate_content.call_count == 2


def test_multiple_investigation_tools_before_submit_plan() -> None:
    """Gemini calls two tools, then submits the plan."""
    client = _make_client()

    plan_args = {
        "diagnosis": "node memory pressure",
        "action": "cordon_node",
        "target": "ip-10-0-1-5",
        "target_namespace": "default",
        "reason": "MemoryPressure condition True",
        "confidence": 0.85,
    }

    responses = [
        _response(_fc_part("describe_pod", {"namespace": "default", "pod": "x"})),
        _response(_fc_part("get_node_conditions", {"node_name": "ip-10-0-1-5"})),
        _response(_fc_part("submit_plan", plan_args)),
    ]
    client._client.models.generate_content.side_effect = responses

    with patch("agent.k8s_mcp_server.call_tool", return_value="ok"):
        plan = client.diagnose(_triage())

    assert plan["action"] == "cordon_node"
    assert client._client.models.generate_content.call_count == 3


# ---------------------------------------------------------------------------
# Safety normalisation
# ---------------------------------------------------------------------------

def test_low_confidence_coerced_to_notify_only() -> None:
    client = _make_client()
    plan_args = {
        "diagnosis": "uncertain",
        "action": "restart_pod",
        "target": "x",
        "target_namespace": "default",
        "reason": "guessing",
        "confidence": 0.5,
    }
    client._client.models.generate_content.return_value = _response(
        _fc_part("submit_plan", plan_args)
    )
    plan = client.diagnose(_triage())
    assert plan["action"] == "notify_only"
    assert plan["confidence"] == pytest.approx(0.5)


def test_unknown_action_coerced_to_notify_only() -> None:
    client = _make_client()
    plan_args = {
        "diagnosis": "bad action",
        "action": "delete_everything",
        "target": "x",
        "target_namespace": "default",
        "reason": "oops",
        "confidence": 0.95,
    }
    client._client.models.generate_content.return_value = _response(
        _fc_part("submit_plan", plan_args)
    )
    plan = client.diagnose(_triage())
    assert plan["action"] == "notify_only"


# ---------------------------------------------------------------------------
# Fallback paths
# ---------------------------------------------------------------------------

def test_api_error_returns_notify_only() -> None:
    client = _make_client()
    client._client.models.generate_content.side_effect = RuntimeError("network error")
    plan = client.diagnose(_triage())
    assert plan["action"] == "notify_only"
    assert plan["confidence"] == pytest.approx(0.0)


def test_max_turns_exceeded_returns_notify_only() -> None:
    """If Gemini keeps calling tools and never calls submit_plan, fall back."""
    client = _make_client()
    client.max_turns = 3

    # Always return an investigation tool call — never submit_plan
    client._client.models.generate_content.return_value = _response(
        _fc_part("get_pod_logs", {"namespace": "default", "pod": "x"})
    )

    with patch("agent.k8s_mcp_server.call_tool", return_value="logs"):
        plan = client.diagnose(_triage())

    assert plan["action"] == "notify_only"
    assert client._client.models.generate_content.call_count == 3


def test_empty_candidates_returns_notify_only() -> None:
    client = _make_client()
    client._client.models.generate_content.return_value = SimpleNamespace(candidates=[])
    plan = client.diagnose(_triage())
    assert plan["action"] == "notify_only"


def test_text_response_without_submit_plan_returns_notify_only() -> None:
    """If Gemini outputs plain text instead of calling submit_plan, fall back."""
    client = _make_client()
    client._client.models.generate_content.return_value = _response(
        _text_part("I think the pod should be restarted.")
    )
    plan = client.diagnose(_triage())
    assert plan["action"] == "notify_only"


def test_rate_limit_retries_then_succeeds() -> None:
    client = _make_client()
    plan_args = {
        "diagnosis": "ok",
        "action": "restart_pod",
        "target": "x",
        "target_namespace": "default",
        "reason": "ok",
        "confidence": 0.9,
    }
    calls = {"n": 0}

    def side_effect(*a, **kw):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("429 RESOURCE_EXHAUSTED rate limited")
        return _response(_fc_part("submit_plan", plan_args))

    client._client.models.generate_content.side_effect = side_effect

    with patch("agent.gemini_client.time.sleep"):
        plan = client.diagnose(_triage())

    assert plan["action"] == "restart_pod"
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# _normalize_plan unit tests
# ---------------------------------------------------------------------------

def test_normalize_clamps_confidence_above_one() -> None:
    plan = _normalize_plan({"action": "notify_only", "confidence": 1.5})
    assert plan["confidence"] == pytest.approx(1.0)


def test_normalize_fills_missing_keys() -> None:
    plan = _normalize_plan({})
    assert plan["action"] == "notify_only"
    assert plan["target"] == "unknown"
    assert plan["target_namespace"] == "unknown"
    assert plan["diagnosis"] == "No diagnosis provided."
    assert plan["reason"] == "No reason provided."
