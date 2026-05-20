"""Tests for agent/bridge.py — dedup, alert routing, webhook auth, approve endpoint."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

import agent.bridge as bridge
import agent.mcp_server as mcp

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_bridge_state(tmp_path):
    bridge._seen.clear()
    os.environ.pop("WEBHOOK_TOKEN", None)
    # Init mcp_server with mocks so scale_down_if_resolved and approval_store work
    mcp.init(
        core_api=MagicMock(),
        apps_api=MagicMock(),
        db_path=str(tmp_path / "test.db"),
        audit_path=str(tmp_path / "audit.jsonl"),
        slack_url="",
        aws_region="us-east-1",
    )
    mcp.DRY_RUN = False
    mcp._scale_state.clear()
    yield
    bridge._seen.clear()
    mcp._scale_state.clear()


def _start_server() -> bridge.BridgeServer:
    srv = bridge.BridgeServer(host="127.0.0.1", port=0)
    t = threading.Thread(target=srv.start, daemon=True)
    t.start()
    time.sleep(0.05)
    return srv


def _post(port: int, path: str, body: dict, token: str = "") -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(port: int, path: str) -> tuple[int, dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as resp:
        return resp.status, json.loads(resp.read())


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


def test_health_returns_200():
    srv = _start_server()
    code, body = _get(srv.port, "/health")
    assert code == 200
    assert body["status"] == "ok"
    srv.stop()


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def test_duplicate_within_ttl_is_dropped():
    alert = {
        "labels": {
            "alertname": "PodCrashLooping",
            "namespace": "default",
            "pod": "mypod",
        }
    }
    key = bridge._alert_key(alert)
    assert bridge._is_duplicate(key) is False
    assert bridge._is_duplicate(key) is True


def test_different_pods_not_duplicates():
    a1 = {"labels": {"alertname": "X", "namespace": "default", "pod": "pod-a"}}
    a2 = {"labels": {"alertname": "X", "namespace": "default", "pod": "pod-b"}}
    assert bridge._is_duplicate(bridge._alert_key(a1)) is False
    assert bridge._is_duplicate(bridge._alert_key(a2)) is False


def test_dedup_expires_after_ttl(monkeypatch):
    alert = {"labels": {"alertname": "Exp", "namespace": "ns", "pod": "p"}}
    key = bridge._alert_key(alert)
    # Inject a stale entry
    bridge._seen[key] = time.monotonic() - bridge.DEDUP_TTL_SECONDS - 1
    # Should NOT be a duplicate — stale entry is purged
    assert bridge._is_duplicate(key) is False


# ---------------------------------------------------------------------------
# Resolved alert → scale_down_if_resolved
# ---------------------------------------------------------------------------


def test_resolved_alert_calls_scale_down():
    mcp._scale_state["PodCrashLooping:default:mypod"] = ("default", "myapp", 2)
    alert = {
        "status": "resolved",
        "labels": {
            "alertname": "PodCrashLooping",
            "namespace": "default",
            "pod": "mypod",
        },
    }
    bridge._process_alert(alert)
    assert "PodCrashLooping:default:mypod" not in mcp._scale_state


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------


def test_webhook_fires_for_firing_alert():
    with patch.object(bridge, "_forward_to_kagent") as mock_fwd:
        srv = _start_server()
        alert = {
            "status": "firing",
            "labels": {"alertname": "Test", "namespace": "default", "pod": "p"},
        }
        code, body = _post(srv.port, "/webhook", {"alerts": [alert]})
        time.sleep(0.1)
        assert code == 200
        assert body["received"] == 1
        mock_fwd.assert_called_once_with(alert)
        srv.stop()


def test_webhook_requires_auth_when_token_set():
    bridge._WEBHOOK_TOKEN = "secret"
    bridge._seen.clear()
    srv = _start_server()
    code, _ = _post(srv.port, "/webhook", {"alerts": []})
    assert code == 401
    code2, _ = _post(srv.port, "/webhook", {"alerts": []}, token="secret")
    assert code2 == 200
    srv.stop()
    bridge._WEBHOOK_TOKEN = ""


def test_webhook_malformed_json_returns_400():
    srv = _start_server()
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}/webhook",
        data=b"not-json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        assert e.code == 400
    srv.stop()


# ---------------------------------------------------------------------------
# Approve endpoint
# ---------------------------------------------------------------------------


def test_approve_known_action_id():
    event = mcp.approval_store.register("test-action-123")
    assert not event.is_set()
    srv = _start_server()
    code, body = _post(srv.port, "/approve/test-action-123", {})
    assert code == 200
    assert body["approved"] == "test-action-123"
    assert event.is_set()
    srv.stop()


def test_approve_unknown_action_id():
    srv = _start_server()
    code, body = _post(srv.port, "/approve/does-not-exist", {})
    assert code == 404
    srv.stop()
