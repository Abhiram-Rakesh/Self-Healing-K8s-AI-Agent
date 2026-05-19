"""KAgent startup: secrets, metrics, wiring, serve."""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


def _load_secrets_from_aws() -> None:
    if not os.environ.get("KUBERNETES_SERVICE_HOST"):
        return
    try:
        import boto3  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("boto3 not available — skipping AWS Secrets Manager")
        return

    region = os.environ.get("SECRETS_MANAGER_REGION", "ap-south-1")
    try:
        sm = boto3.client("secretsmanager", region_name=region)
    except Exception as exc:
        logger.error("Failed to create Secrets Manager client: %s", exc)
        return

    _fetch_secret(
        sm,
        os.environ.get("GEMINI_API_KEY_SECRET", "kagent/gemini-api-key"),
        "GEMINI_API_KEY",
    )
    _fetch_secret(
        sm,
        os.environ.get("SLACK_WEBHOOK_SECRET", "kagent/slack-webhook"),
        "SLACK_WEBHOOK_URL",
    )


def _fetch_secret(sm: object, secret_id: str, env_var: str) -> None:
    if os.environ.get(env_var):
        return
    try:
        value = sm.get_secret_value(SecretId=secret_id)["SecretString"]  # type: ignore[attr-defined]
        os.environ[env_var] = value
        logger.info("Loaded %s from Secrets Manager", env_var)
    except Exception as exc:
        logger.error("Failed to load %s (secret %s): %s", env_var, secret_id, exc)


def _init_mcp_server() -> None:
    from agent import mcp_server

    try:
        from kubernetes import client as k8s_client  # type: ignore[import-untyped]
        from kubernetes import config as k8s_config  # type: ignore[import-untyped]

        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()
        core_api = k8s_client.CoreV1Api()
        apps_api = k8s_client.AppsV1Api()
    except Exception as exc:
        logger.warning(
            "Kubernetes config not available (%s) — tools return Unavailable", exc
        )
        core_api = None
        apps_api = None

    mcp_server.init(
        core_api=core_api,
        apps_api=apps_api,
        db_path=os.environ.get("MEMORY_DB_PATH", "/tmp/kagent-memory.db"),
        audit_path=os.environ.get("AUDIT_LOG_PATH", "/tmp/kagent-audit.jsonl"),
        slack_url=os.environ.get("SLACK_WEBHOOK_URL", ""),
        aws_region=os.environ.get("AWS_REGION", "ap-south-1"),
    )


def _build_metrics(cost_guard: object) -> dict:
    try:
        from prometheus_client import (  # type: ignore[import-untyped]
            Counter,
            Gauge,
            Histogram,
        )

        alerts_total = Counter(
            "kagent_alerts_total", "Total alerts received", ["severity"]
        )
        gemini_calls_total = Counter(
            "kagent_gemini_calls_total", "Total Gemini API calls"
        )
        actions_total = Counter(
            "kagent_actions_total", "Total healing actions", ["action", "executed"]
        )
        healing_seconds = Histogram(
            "kagent_healing_seconds", "Time spent per healing pipeline run"
        )
        last_confidence = Gauge(
            "kagent_last_confidence", "Last Gemini confidence score"
        )
        requests_remaining = Gauge(
            "kagent_requests_remaining", "Remaining Gemini requests today"
        )
        requests_remaining.set_function(lambda: float(cost_guard.remaining()))  # type: ignore[attr-defined]

        return {
            "alerts_total": alerts_total,
            "gemini_calls_total": gemini_calls_total,
            "actions_total": actions_total,
            "healing_seconds": healing_seconds,
            "last_confidence": last_confidence,
            "requests_remaining": requests_remaining,
        }
    except Exception as exc:
        logger.warning("Prometheus metrics unavailable: %s", exc)
        return {}


def main() -> int:
    _configure_logging()
    _load_secrets_from_aws()
    _init_mcp_server()

    from agent.cost_guard import CostGuard
    from agent.gemini import GeminiClient
    from agent.server import WebhookServer

    cost_guard = CostGuard()
    _build_metrics(cost_guard)

    try:
        from prometheus_client import start_http_server  # type: ignore[import-untyped]

        start_http_server(int(os.environ.get("METRICS_PORT", "8001")))
        logger.info("Prometheus metrics on :%s", os.environ.get("METRICS_PORT", "8001"))
    except Exception as exc:
        logger.warning("Could not start metrics server: %s", exc)

    gemini = GeminiClient()
    server = WebhookServer(
        gemini,
        cost_guard,
        port=int(os.environ.get("WEBHOOK_PORT", "8000")),
    )
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
