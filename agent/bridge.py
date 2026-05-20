"""Thin Alertmanager → kagent bridge.

Receives Alertmanager webhook POSTs, deduplicates within DEDUP_TTL_SECONDS,
handles resolved alerts via scale_down_if_resolved, and forwards firing alerts
to the kagent agent via the A2A protocol.

Also exposes /approve/<id> for HITL approval of cordon/drain actions.

Note: kagent's A2A endpoint follows the Google A2A spec (JSON-RPC 2.0,
method "tasks/send"). Verify the exact format against your kagent version
by inspecting: kubectl port-forward -n kagent svc/kagent-controller 8083:8083
"""

from __future__ import annotations

import json
import logging
import os
import socketserver
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests  # type: ignore[import-untyped]

from agent import mcp_server
from agent.cost_guard import CostGuard

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEDUP_TTL_SECONDS: int = int(os.environ.get("DEDUP_TTL_SECONDS", "300"))
_WEBHOOK_TOKEN: str = os.environ.get("WEBHOOK_TOKEN", "")

# kagent controller A2A endpoint: http://<controller>:8083/api/a2a/<ns>/<agent>
_KAGENT_CONTROLLER_URL: str = os.environ.get(
    "KAGENT_CONTROLLER_URL",
    "http://kagent-controller.kagent.svc.cluster.local:8083",
)
_KAGENT_NAMESPACE: str = os.environ.get("KAGENT_NAMESPACE", "kagent")
_KAGENT_AGENT_NAME: str = os.environ.get("KAGENT_AGENT_NAME", "healer-agent")

# ---------------------------------------------------------------------------
# Budget guard — caps forwarded alerts per UTC day (proxy for kagent/LLM calls)
# ---------------------------------------------------------------------------

_cost_guard = CostGuard()

# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

_seen: dict[str, float] = {}
_seen_lock: threading.Lock = threading.Lock()


def _alert_key(alert: dict) -> str:
    labels = alert.get("labels") or {}
    return (
        f"{labels.get('alertname', 'unknown')}"
        f":{labels.get('namespace', '-')}"
        f":{labels.get('pod', '-')}"
    )


def _is_duplicate(key: str) -> bool:
    now = time.monotonic()
    with _seen_lock:
        expired = [k for k, ts in _seen.items() if now - ts > DEDUP_TTL_SECONDS]
        for k in expired:
            del _seen[k]
        if key in _seen:
            return True
        _seen[key] = now
    return False


# ---------------------------------------------------------------------------
# Alert forwarding
# ---------------------------------------------------------------------------


def _build_prompt(alert: dict) -> str:
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    return (
        f"ALERT FIRING: {labels.get('alertname', 'unknown')}\n"
        f"Severity: {labels.get('severity', 'unknown')}\n"
        f"Namespace: {labels.get('namespace', '-')}\n"
        f"Pod: {labels.get('pod', '-')}\n"
        f"Node: {labels.get('node', '-')}\n"
        f"Description: {annotations.get('description', annotations.get('summary', '-'))}\n"
        f"Value: {annotations.get('value', '-')}\n"
        f"StartsAt: {alert.get('startsAt', '-')}\n\n"
        "Investigate this alert using your available tools. Start by calling "
        "recall_past_cases to check prior incidents, then gather evidence with "
        "k8s_get_pod_logs, k8s_get_events, and k8s_describe_resource. "
        "When you have a diagnosis, take the appropriate healing action. "
        "ALWAYS call record_outcome as your final tool."
    )


def _forward_to_kagent(alert: dict) -> None:
    """Forward a firing alert to the kagent agent via the A2A protocol."""
    url = (
        f"{_KAGENT_CONTROLLER_URL}/api/a2a" f"/{_KAGENT_NAMESPACE}/{_KAGENT_AGENT_NAME}"
    )
    task_id = uuid.uuid4().hex
    # Google A2A spec: JSON-RPC 2.0, method "tasks/send"
    payload = {
        "jsonrpc": "2.0",
        "id": task_id,
        "method": "tasks/send",
        "params": {
            "id": task_id,
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": _build_prompt(alert)}],
            },
        },
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        logger.info(
            "Forwarded %s to kagent → HTTP %d", _alert_key(alert), resp.status_code
        )
    except Exception as exc:
        logger.error("Failed to forward alert to kagent: %s", exc)


def _process_alert(alert: dict) -> None:
    key = _alert_key(alert)
    if alert.get("status") == "resolved":
        mcp_server.scale_down_if_resolved(key)
        return
    if _is_duplicate(key):
        logger.debug("Duplicate alert %s — dropping", key)
        return
    if not _cost_guard.check_and_increment():
        logger.warning("CostGuard: daily limit reached — dropping %s", key)
        return
    _forward_to_kagent(alert)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/health", "/healthz", "/readyz"):
            self._respond(200, {"status": "ok", "version": "2.0.0"})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/webhook":
            self._handle_webhook()
        elif self.path.startswith("/approve/"):
            action_id = self.path[len("/approve/") :]
            ok = mcp_server.approval_store.approve(action_id)
            if ok:
                self._respond(200, {"approved": action_id})
            else:
                self._respond(404, {"error": "unknown action_id"})
        else:
            self._respond(404, {"error": "not found"})

    def _handle_webhook(self) -> None:
        if _WEBHOOK_TOKEN:
            if self.headers.get("Authorization", "") != f"Bearer {_WEBHOOK_TOKEN}":
                self._respond(401, {"error": "unauthorized"})
                return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self._respond(400, {"error": "malformed json"})
            return

        if isinstance(payload, dict) and "alerts" in payload:
            alerts = payload["alerts"]
        elif isinstance(payload, list):
            alerts = payload
        else:
            alerts = [payload]

        for alert in alerts:
            threading.Thread(target=_process_alert, args=(alert,), daemon=True).start()

        self._respond(200, {"received": len(alerts)})

    def _respond(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        logger.debug("HTTP " + fmt, *args)


class BridgeServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 8000) -> None:
        self._httpd = _ThreadingHTTPServer((host, port), _Handler)
        self.port: int = self._httpd.server_address[1]

    def start(self) -> None:
        logger.info("Bridge listening on :%d", self.port)
        self._httpd.serve_forever()

    def stop(self) -> None:
        self._httpd.shutdown()
