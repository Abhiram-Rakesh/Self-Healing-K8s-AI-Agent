"""Tests for agent/gemini.py — Gemini tool-calling loop with mocked SDK."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.gemini import GeminiClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client() -> GeminiClient:
    c = GeminiClient(api_key="fake-key", max_retries=2, max_turns=5)
    c._client = MagicMock()
    return c


def _fc_part(name: str, args: dict) -> SimpleNamespace:
    fc = SimpleNamespace(name=name, args=args)
    return SimpleNamespace(function_call=fc)


def _text_part(text: str) -> SimpleNamespace:
    return SimpleNamespace(function_call=None, text=text)


def _candidate(parts: list) -> SimpleNamespace:
    content = SimpleNamespace(parts=parts)
    return SimpleNamespace(content=content)


def _response(*parts) -> SimpleNamespace:
    return SimpleNamespace(candidates=[_candidate(list(parts))])


def _triage(
    alertname: str = "PodCrashLooping", ns: str = "default", pod: str = "x"
) -> dict:
    return {
        "alert_key": f"{alertname}:{ns}:{pod}",
        "alert_name": alertname,
        "severity": "warning",
        "namespace": ns,
        "pod": pod,
        "alert": {"labels": {"alertname": alertname, "namespace": ns, "pod": pod}},
    }


def _record_outcome_args(**overrides) -> dict:
    base = {
        "alert_key": "PodCrashLooping:default:x",
        "alert_name": "PodCrashLooping",
        "severity": "warning",
        "diagnosis": "OOM killed",
        "action": "restart_deployment",
        "target": "myapp",
        "namespace": "default",
        "confidence": 0.9,
        "executed": True,
        "outcome": "Restarted successfully",
        "dry_run": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Core loop tests
# ---------------------------------------------------------------------------


def test_record_outcome_call_concludes_loop():
    """When Gemini calls record_outcome on the first turn, loop ends immediately."""
    client = _make_client()
    client._client.models.generate_content.return_value = _response(
        _fc_part("record_outcome", _record_outcome_args())
    )

    with patch("agent.mcp_server.call_tool") as mock_tool:
        result = client.diagnose(_triage())

    mock_tool.assert_not_called()
    assert result["action"] == "restart_deployment"
    assert result["confidence"] == pytest.approx(0.9)
    assert client._client.models.generate_content.call_count == 1


def test_investigation_tool_then_record_outcome():
    """Turn 1: investigation tool. Turn 2: record_outcome."""
    client = _make_client()
    responses = [
        _response(_fc_part("get_pod_logs", {"namespace": "default", "pod": "x"})),
        _response(_fc_part("record_outcome", _record_outcome_args())),
    ]
    client._client.models.generate_content.side_effect = responses

    with patch("agent.mcp_server.call_tool", return_value="some logs") as mock_tool:
        result = client.diagnose(_triage())

    mock_tool.assert_called_once_with(
        "get_pod_logs", {"namespace": "default", "pod": "x"}
    )
    assert result["action"] == "restart_deployment"
    assert client._client.models.generate_content.call_count == 2


def test_multiple_investigation_tools():
    """Two investigation tools, then record_outcome — Gemini called 3 times."""
    client = _make_client()
    responses = [
        _response(_fc_part("get_pod_events", {"namespace": "default", "pod": "x"})),
        _response(_fc_part("describe_pod", {"namespace": "default", "pod": "x"})),
        _response(
            _fc_part(
                "record_outcome",
                _record_outcome_args(action="notify_only", executed=False),
            )
        ),
    ]
    client._client.models.generate_content.side_effect = responses

    with patch("agent.mcp_server.call_tool", return_value="data"):
        result = client.diagnose(_triage())

    assert result["action"] == "notify_only"
    assert client._client.models.generate_content.call_count == 3


def test_max_turns_returns_fallback():
    """If Gemini never calls record_outcome within max_turns, return fallback."""
    client = _make_client()
    client.max_turns = 3
    client._client.models.generate_content.return_value = _response(
        _fc_part("get_pod_logs", {"namespace": "default", "pod": "x"})
    )

    with patch("agent.mcp_server.call_tool", return_value="logs"):
        result = client.diagnose(_triage())

    assert result["action"] == "notify_only"
    assert result["confidence"] == pytest.approx(0.0)
    assert client._client.models.generate_content.call_count == 3


def test_api_error_returns_fallback():
    client = _make_client()
    client._client.models.generate_content.side_effect = RuntimeError("network error")
    result = client.diagnose(_triage())
    assert result["action"] == "notify_only"
    assert result["confidence"] == pytest.approx(0.0)


def test_empty_candidates_returns_fallback():
    client = _make_client()
    client._client.models.generate_content.return_value = SimpleNamespace(candidates=[])
    result = client.diagnose(_triage())
    assert result["action"] == "notify_only"


def test_text_response_without_tool_returns_fallback():
    """Gemini returns plain text instead of a tool call — fall back."""
    client = _make_client()
    client._client.models.generate_content.return_value = _response(
        _text_part("I think the pod should be restarted.")
    )
    result = client.diagnose(_triage())
    assert result["action"] == "notify_only"


def test_rate_limit_retries_then_succeeds():
    client = _make_client()
    calls: dict[str, int] = {"n": 0}

    def side_effect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("429 RESOURCE_EXHAUSTED rate limited")
        return _response(_fc_part("record_outcome", _record_outcome_args()))

    client._client.models.generate_content.side_effect = side_effect

    with patch("agent.gemini.time.sleep"):
        result = client.diagnose(_triage())

    assert result["action"] == "restart_deployment"
    assert calls["n"] == 2


def test_no_client_returns_fallback():
    """If _client is None (no API key), return fallback immediately."""
    client = GeminiClient(api_key="", max_retries=2, max_turns=5)
    # _client is None since no valid key
    result = client.diagnose(_triage())
    assert result["action"] == "notify_only"
    assert result["confidence"] == pytest.approx(0.0)
