"""
Third stage: executes the healing action with safety gates.

High-impact actions (cordon_node) require HITL approval via Slack.
Auto-approves after 300s if no response received.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import requests

from agent.remediator import Remediator

logger = logging.getLogger(__name__)

REQUIRES_APPROVAL = {"cordon_node", "drain_node"}
APPROVAL_TIMEOUT_SECONDS = int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", "300"))


class ApprovalStore:
    """Thread-safe store of pending high-impact action approvals.

    RemediationAgent registers an action_id and waits on its Event.
    The webhook server calls approve(action_id) when an operator hits /approve/<id>.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, threading.Event] = {}

    def register(self, action_id: str) -> threading.Event:
        event = threading.Event()
        with self._lock:
            self._pending[action_id] = event
        return event

    def approve(self, action_id: str) -> bool:
        """Signal approval. Returns False if the action_id is unknown or already expired."""
        with self._lock:
            event = self._pending.pop(action_id, None)
        if event is None:
            return False
        event.set()
        return True

    def cancel(self, action_id: str) -> None:
        with self._lock:
            self._pending.pop(action_id, None)

    def pending_ids(self) -> list[str]:
        with self._lock:
            return list(self._pending.keys())


class RemediationAgent:
    """Executes the plan via Remediator, optionally gated by Slack approval."""

    def __init__(
        self,
        remediator: Remediator,
        slack_webhook_url: str | None = None,
        approval_timeout_seconds: int = APPROVAL_TIMEOUT_SECONDS,
        approval_store: ApprovalStore | None = None,
    ) -> None:
        self.remediator = remediator
        self.slack_webhook_url = slack_webhook_url or os.environ.get(
            "SLACK_WEBHOOK_URL", ""
        )
        self.approval_timeout_seconds = int(approval_timeout_seconds)
        self.approval_store = approval_store

    def _request_approval(self, plan: dict[str, Any]) -> bool:
        """Send a Slack notification and wait for explicit approval via /approve/<id>.
        Auto-approves after timeout so a missing response never permanently blocks healing.
        """
        action_id = (
            f"{plan.get('action')}:{plan.get('target_namespace')}:"
            f"{plan.get('target')}:{int(time.time())}"
        )

        event: threading.Event | None = None
        if self.approval_store is not None:
            event = self.approval_store.register(action_id)

        if self.slack_webhook_url:
            base_url = os.environ.get("WEBHOOK_BASE_URL", "").rstrip("/")
            approve_url = (
                f"{base_url}/approve/{action_id}" if base_url
                else f"POST /approve/{action_id} on the kagent-healer service"
            )
            msg = {
                "text": (
                    f":warning: *KAgent high-impact action pending approval*\n"
                    f"*Action:* `{plan.get('action')}`\n"
                    f"*Target:* `{plan.get('target_namespace')}/{plan.get('target')}`\n"
                    f"*Confidence:* `{plan.get('confidence', 0.0):.2f}`\n"
                    f"*Reason:* {plan.get('reason', '')}\n"
                    f"*Approve:* {approve_url}\n"
                    f"_Auto-approves in {self.approval_timeout_seconds}s if no response._"
                )
            }
            try:
                requests.post(self.slack_webhook_url, json=msg, timeout=5)
            except Exception as exc:
                logger.warning("Slack approval notification failed: %s", exc)
        else:
            logger.info(
                "No Slack webhook configured — auto-approving high-impact action %s",
                plan.get("action"),
            )

        logger.info(
            "Awaiting approval for %s (action_id=%s, timeout=%ds)",
            plan.get("action"),
            action_id,
            self.approval_timeout_seconds,
        )

        if event is not None:
            approved_explicitly = event.wait(timeout=self.approval_timeout_seconds)
            if not approved_explicitly:
                logger.warning(
                    "Approval timeout for action_id=%s — auto-approving", action_id
                )
                self.approval_store.cancel(action_id)  # type: ignore[union-attr]
        else:
            time.sleep(self.approval_timeout_seconds)

        return True

    def execute(self, plan: dict[str, Any]) -> dict[str, Any]:
        action = str(plan.get("action", "no_action"))
        if action in REQUIRES_APPROVAL:
            approved = self._request_approval(plan)
            if not approved:
                logger.warning("HITL approval denied for %s", action)
                return {
                    "action": action,
                    "target": plan.get("target", "unknown"),
                    "namespace": plan.get("target_namespace", "unknown"),
                    "confidence": float(plan.get("confidence", 0.0)),
                    "executed": False,
                    "reason": "HITL approval denied",
                    "dry_run": False,
                }
        return self.remediator.execute(plan)

    def handle_resolved(self, alert_key: str) -> None:
        """Scale back down any deployment that was scaled up for this alert."""
        self.remediator.scale_down_if_tracked(alert_key)
