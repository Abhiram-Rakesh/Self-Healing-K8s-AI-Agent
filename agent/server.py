"""Webhook HTTP server with /webhook, /approve/<id>, and /health endpoints."""

from __future__ import annotations

import json
import logging
import os
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

from agent import mcp_server, pipeline

if TYPE_CHECKING:
    from agent.cost_guard import CostGuard
    from agent.gemini import GeminiClient

logger = logging.getLogger(__name__)

_WEBHOOK_TOKEN: str = os.environ.get("WEBHOOK_TOKEN", "")


def _safe_run(alert: dict, gemini_client: GeminiClient, cost_guard: CostGuard) -> None:
    try:
        pipeline.run(alert, gemini_client, cost_guard)
    except Exception:
        logger.exception(
            "Unhandled error in pipeline.run for alert %s", alert.get("labels")
        )


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _make_handler(gemini_client: GeminiClient, cost_guard: CostGuard) -> type:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path in ("/health", "/healthz", "/readyz"):
                self._respond(200, {"status": "ok", "version": "1.0.0"})
            else:
                self._respond(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path == "/webhook":
                self._handle_webhook()
            elif self.path.startswith("/approve/"):
                action_id = self.path[len("/approve/") :]
                self._handle_approve(action_id)
            else:
                self._respond(404, {"error": "not found"})

        def _handle_webhook(self) -> None:
            token = _WEBHOOK_TOKEN
            if token:
                auth = self.headers.get("Authorization", "")
                if auth != f"Bearer {token}":
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
                t = threading.Thread(
                    target=_safe_run,
                    args=(alert, gemini_client, cost_guard),
                    daemon=True,
                )
                t.start()

            self._respond(200, {"received": len(alerts)})

        def _handle_approve(self, action_id: str) -> None:
            ok = mcp_server.approval_store.approve(action_id)
            if ok:
                self._respond(200, {"approved": action_id})
            else:
                self._respond(404, {"error": "unknown action_id"})

        def _respond(self, code: int, data: dict) -> None:
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            logger.debug("HTTP " + fmt, *args)

    return _Handler


class WebhookServer:
    def __init__(
        self,
        gemini_client: GeminiClient,
        cost_guard: CostGuard,
        host: str = "0.0.0.0",
        port: int = 8000,
    ) -> None:
        self._httpd = _ThreadingHTTPServer(
            (host, port), _make_handler(gemini_client, cost_guard)
        )
        self.port: int = self._httpd.server_address[1]

    def start(self) -> None:
        logger.info("Webhook server listening on port %d", self.port)
        self._httpd.serve_forever()

    def stop(self) -> None:
        self._httpd.shutdown()
