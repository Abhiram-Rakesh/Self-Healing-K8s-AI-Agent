"""Tests for agent/server.py — webhook, approve, and health endpoints."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from unittest.mock import MagicMock, patch

import agent.mcp_server as mcp
import agent.server as server_module
from agent.server import WebhookServer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_server(**kwargs) -> WebhookServer:
    gemini = MagicMock()
    cost_guard = MagicMock()
    cost_guard.check_and_increment.return_value = True
    srv = WebhookServer(gemini, cost_guard, host="127.0.0.1", port=0, **kwargs)
    return srv


def _start(srv: WebhookServer) -> threading.Thread:
    t = threading.Thread(target=srv.start, daemon=True)
    t.start()
    time.sleep(0.05)
    return t


def _url(srv: WebhookServer, path: str) -> str:
    return f"http://127.0.0.1:{srv.port}{path}"


def _get(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url) as resp:
        return resp.status, json.loads(resp.read())


def _post(
    url: str, body: dict | None = None, headers: dict | None = None
) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


def test_health_returns_ok():
    srv = _make_server()
    _start(srv)
    try:
        code, data = _get(_url(srv, "/health"))
        assert code == 200
        assert data["status"] == "ok"
    finally:
        srv.stop()


def test_healthz_returns_ok():
    srv = _make_server()
    _start(srv)
    try:
        code, data = _get(_url(srv, "/healthz"))
        assert code == 200
    finally:
        srv.stop()


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------


def test_webhook_returns_immediately():
    srv = _make_server()
    _start(srv)
    try:
        code, data = _post(
            _url(srv, "/webhook"),
            body={"alerts": [{"labels": {"alertname": "Test"}, "status": "firing"}]},
        )
        assert code == 200
        assert data["received"] == 1
    finally:
        srv.stop()


def test_webhook_fires_pipeline():
    srv = _make_server()
    _start(srv)
    try:
        with patch("agent.pipeline.run") as mock_run:
            alert = {
                "labels": {"alertname": "Test", "namespace": "default", "pod": "p"},
                "status": "firing",
            }
            _post(_url(srv, "/webhook"), body={"alerts": [alert]})
            time.sleep(0.2)
            mock_run.assert_called_once()
    finally:
        srv.stop()


def test_malformed_json_returns_400():
    srv = _make_server()
    _start(srv)
    try:
        req = urllib.request.Request(
            _url(srv, "/webhook"),
            data=b"not json at all",
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
    finally:
        srv.stop()


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


def test_auth_wrong_token_returns_401():
    srv = _make_server()
    _start(srv)
    try:
        old_token = server_module._WEBHOOK_TOKEN
        server_module._WEBHOOK_TOKEN = "correct-token"
        try:
            code, _ = _post(
                _url(srv, "/webhook"),
                body={},
                headers={"Authorization": "Bearer wrong-token"},
            )
            assert code == 401
        finally:
            server_module._WEBHOOK_TOKEN = old_token
    finally:
        srv.stop()


def test_auth_correct_token_returns_200():
    srv = _make_server()
    _start(srv)
    try:
        old_token = server_module._WEBHOOK_TOKEN
        server_module._WEBHOOK_TOKEN = "mytoken"
        try:
            code, data = _post(
                _url(srv, "/webhook"),
                body={"alerts": []},
                headers={"Authorization": "Bearer mytoken"},
            )
            assert code == 200
        finally:
            server_module._WEBHOOK_TOKEN = old_token
    finally:
        srv.stop()


def test_no_auth_when_token_unset():
    srv = _make_server()
    _start(srv)
    try:
        old_token = server_module._WEBHOOK_TOKEN
        server_module._WEBHOOK_TOKEN = ""
        try:
            code, _ = _post(_url(srv, "/webhook"), body={})
            assert code == 200
        finally:
            server_module._WEBHOOK_TOKEN = old_token
    finally:
        srv.stop()


# ---------------------------------------------------------------------------
# Approve endpoint
# ---------------------------------------------------------------------------


def test_approve_endpoint_sets_event():
    srv = _make_server()
    _start(srv)
    try:
        event = mcp.approval_store.register("test-action-001")
        assert not event.is_set()
        code, data = _post(_url(srv, "/approve/test-action-001"))
        assert code == 200
        assert data["approved"] == "test-action-001"
        assert event.is_set()
    finally:
        srv.stop()


def test_approve_unknown_id_returns_404():
    srv = _make_server()
    _start(srv)
    try:
        code, _ = _post(_url(srv, "/approve/no-such-id"))
        assert code == 404
    finally:
        srv.stop()
