"""
Final stage: records every healing event and sends notifications.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import requests

from agent.memory import RunbookMemory

logger = logging.getLogger(__name__)

DEFAULT_AUDIT_PATH = os.environ.get("AUDIT_LOG_PATH", "/tmp/kagent-audit.jsonl")

try:  # pragma: no cover
    import boto3  # type: ignore

    _HAS_BOTO3 = True
except Exception:  # pragma: no cover
    boto3 = None  # type: ignore
    _HAS_BOTO3 = False


class AuditAgent:
    """Persists healing events, fans out to Slack and CloudWatch."""

    def __init__(
        self,
        memory: RunbookMemory | None = None,
        audit_path: str = DEFAULT_AUDIT_PATH,
        slack_webhook_url: str | None = None,
        cloudwatch_namespace: str = "KAgent/HealingEvents",
        aws_region: str | None = None,
    ) -> None:
        self.memory = memory
        self.audit_path = audit_path
        self.slack_webhook_url = slack_webhook_url or os.environ.get(
            "SLACK_WEBHOOK_URL", ""
        )
        self.cloudwatch_namespace = cloudwatch_namespace
        self.aws_region = aws_region or os.environ.get("AWS_REGION", "ap-south-1")
        self._cw = None
        if _HAS_BOTO3 and os.environ.get("KUBERNETES_SERVICE_HOST"):
            try:
                self._cw = boto3.client("cloudwatch", region_name=self.aws_region)
            except Exception as exc:
                logger.warning("CloudWatch client init failed: %s", exc)

    @staticmethod
    def _build_record(
        triage_result: dict[str, Any],
        plan: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "alert_key": triage_result.get("alert_key", ""),
            "severity": triage_result.get("severity", ""),
            "diagnosis": plan.get("diagnosis", ""),
            "action": result.get("action", ""),
            "target": result.get("target", ""),
            "namespace": result.get("namespace", ""),
            "confidence": float(result.get("confidence", 0.0)),
            "executed": bool(result.get("executed", False)),
            "outcome": result.get("reason", ""),
            "dry_run": bool(result.get("dry_run", False)),
        }

    def _write_jsonl(self, record: dict[str, Any]) -> None:
        try:
            with open(self.audit_path, "a", encoding="utf-8") as fp:
                fp.write(json.dumps(record, default=str) + "\n")
        except Exception as exc:
            logger.error("Audit log write failed: %s", exc)

    def _notify_slack(self, record: dict[str, Any]) -> None:
        if not self.slack_webhook_url:
            return
        emoji = ":white_check_mark:" if record["executed"] else ":no_entry:"
        text = (
            f"{emoji} KAgent healing event\n"
            f"*Alert:* `{record['alert_key']}` ({record['severity']})\n"
            f"*Action:* `{record['action']}` on `{record['namespace']}/{record['target']}`\n"
            f"*Confidence:* `{record['confidence']:.2f}` "
            f"*Dry-run:* `{record['dry_run']}`\n"
            f"*Diagnosis:* {record['diagnosis']}\n"
            f"*Outcome:* {record['outcome']}"
        )
        try:
            requests.post(self.slack_webhook_url, json={"text": text}, timeout=5)
        except Exception as exc:
            logger.warning("Slack notification failed: %s", exc)

    def _publish_cw_metric(self, record: dict[str, Any]) -> None:
        if not self._cw:
            return
        try:
            self._cw.put_metric_data(
                Namespace=self.cloudwatch_namespace,
                MetricData=[
                    {
                        "MetricName": "HealingEvent",
                        "Dimensions": [
                            {"Name": "Action", "Value": record["action"]},
                            {"Name": "Executed", "Value": str(record["executed"])},
                        ],
                        "Value": 1,
                        "Unit": "Count",
                    }
                ],
            )
        except Exception as exc:
            logger.warning("CloudWatch metric publish failed: %s", exc)

    def record(
        self,
        triage_result: dict[str, Any],
        plan: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        record = self._build_record(triage_result, plan, result)
        logger.info("AUDIT %s", json.dumps(record, default=str))
        self._write_jsonl(record)
        self._notify_slack(record)
        self._publish_cw_metric(record)
        if self.memory is not None:
            try:
                self.memory.store(
                    {
                        "alert_type": triage_result.get("alert_name", "unknown"),
                        "diagnosis": plan.get("diagnosis", ""),
                        "action": result.get("action", ""),
                        "outcome": "executed" if result.get("executed") else "skipped",
                        "confidence": float(result.get("confidence", 0.0)),
                        "created_at": record["timestamp"],
                    }
                )
            except Exception as exc:
                logger.warning("memory.store from audit failed: %s", exc)
        return record
