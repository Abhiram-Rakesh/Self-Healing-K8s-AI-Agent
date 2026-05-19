"""
Entry point. Starts metric server, loads secrets, starts webhook server.
"""

from __future__ import annotations

import logging
import os
import sys

from prometheus_client import Counter, Gauge, Histogram, start_http_server

from agent import k8s_mcp_server
from agent.agents import (
    ApprovalStore,
    AuditAgent,
    DiagnosisAgent,
    RemediationAgent,
    TriageAgent,
)
from agent.context_builder import ContextBuilder
from agent.cost_guard import CostGuard
from agent.gemini_client import GeminiClient
from agent.memory import RunbookMemory
from agent.remediator import Remediator
from agent.webhook_server import HealingPipeline, WebhookServer

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        stream=sys.stdout,
    )


def _running_in_cluster() -> bool:
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST"))


def _load_secret(client: object, secret_id: str) -> str:
    """Read a secret string from AWS Secrets Manager. Returns '' on failure."""
    try:
        resp = client.get_secret_value(SecretId=secret_id)  # type: ignore[attr-defined]
        return str(resp.get("SecretString") or "")
    except Exception as exc:
        logger.warning("Failed to load secret %s: %s", secret_id, exc)
        return ""


def _load_secrets_from_aws() -> None:
    """Populate env vars from AWS Secrets Manager when in-cluster."""
    if not _running_in_cluster():
        logger.info("Not running in-cluster — skipping AWS Secrets Manager fetch")
        return

    try:
        import boto3  # type: ignore
    except Exception as exc:
        logger.error("boto3 not available — cannot load secrets: %s", exc)
        return

    region = os.environ.get("SECRETS_MANAGER_REGION") or os.environ.get(
        "AWS_REGION", "ap-south-1"
    )
    try:
        sm = boto3.client("secretsmanager", region_name=region)
    except Exception as exc:
        logger.error("Could not create Secrets Manager client: %s", exc)
        return

    gemini_secret_id = os.environ.get("GEMINI_API_KEY_SECRET", "kagent/gemini-api-key")
    slack_secret_id = os.environ.get("SLACK_WEBHOOK_SECRET", "kagent/slack-webhook")

    if not os.environ.get("GEMINI_API_KEY"):
        key = _load_secret(sm, gemini_secret_id)
        if key:
            os.environ["GEMINI_API_KEY"] = key
            logger.info("Loaded GEMINI_API_KEY from %s", gemini_secret_id)

    if not os.environ.get("SLACK_WEBHOOK_URL"):
        webhook = _load_secret(sm, slack_secret_id)
        if webhook:
            os.environ["SLACK_WEBHOOK_URL"] = webhook
            logger.info("Loaded SLACK_WEBHOOK_URL from %s", slack_secret_id)


def _build_metrics(cost_guard: CostGuard) -> dict[str, object]:
    alerts_total = Counter(
        "kagent_alerts_total", "Alerts received", ["severity"]
    )
    gemini_calls_total = Counter(
        "kagent_gemini_calls_total", "Gemini API calls"
    )
    actions_total = Counter(
        "kagent_actions_total", "Actions executed", ["action", "executed"]
    )
    healing_seconds = Histogram(
        "kagent_healing_seconds", "Alert to resolution time"
    )
    confidence_gauge = Gauge(
        "kagent_last_confidence", "Last Gemini confidence score"
    )
    budget_remaining = Gauge(
        "kagent_requests_remaining_today", "Remaining daily Gemini requests"
    )
    stage_duration = Histogram(
        "kagent_stage_duration_seconds",
        "Per-stage pipeline duration",
        ["stage"],
        buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
    )
    stage_errors = Counter(
        "kagent_stage_errors_total",
        "Per-stage pipeline errors",
        ["stage"],
    )

    # Periodically refresh budget gauge via callback.
    budget_remaining.set_function(lambda: float(cost_guard.remaining()))

    return {
        "alerts_total": alerts_total,
        "gemini_calls_total": gemini_calls_total,
        "actions_total": actions_total,
        "healing_seconds": healing_seconds,
        "confidence_gauge": confidence_gauge,
        "budget_remaining": budget_remaining,
        "stage_duration": stage_duration,
        "stage_errors": stage_errors,
    }


def main() -> int:
    _configure_logging()
    _load_secrets_from_aws()

    metrics_port = int(os.environ.get("METRICS_PORT", "8001"))
    webhook_port = int(os.environ.get("WEBHOOK_PORT", os.environ.get("PORT", "8000")))

    # Components
    memory = RunbookMemory()
    cost_guard = CostGuard()
    gemini = GeminiClient()
    context_builder = ContextBuilder()
    remediator = Remediator()
    approval_store = ApprovalStore()

    # Wire the K8s MCP server so Gemini can call investigation tools in-process
    k8s_mcp_server.init(context_builder, memory)
    logger.info("K8s MCP server initialised with %d tools", len(k8s_mcp_server.ALL_DECLARATIONS))

    metrics = _build_metrics(cost_guard)

    triage = TriageAgent()
    diagnosis = DiagnosisAgent(gemini, cost_guard)
    remediation = RemediationAgent(remediator, approval_store=approval_store)
    audit = AuditAgent(memory=memory)

    pipeline = HealingPipeline(triage, diagnosis, remediation, audit, metrics=metrics)

    start_http_server(metrics_port)
    logger.info("Prometheus metrics server listening on :%d", metrics_port)

    server = WebhookServer(pipeline, port=webhook_port, approval_store=approval_store)
    try:
        server.start()
    except KeyboardInterrupt:
        logger.info("Shutting down")
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
