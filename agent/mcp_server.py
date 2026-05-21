"""Custom MCP server for safety-gated Kubernetes write tools.

Exposes healing write tools (restart, scale, cordon, drain), incident memory
(recall_past_cases, record_outcome), and HITL approval to the kagent framework.

Read tools (pod logs, events, describe) are provided by kagent's built-in
kagent-tool-server and are NOT duplicated here.

Run standalone:  python -m agent.mcp_server
kagent reaches this server via the RemoteMCPServer CRD (SSE transport, port 8080).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

try:
    import requests  # type: ignore[import-untyped]

    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

try:
    import boto3  # type: ignore[import-untyped]

    _HAS_BOTO3 = True
except ImportError:
    _HAS_BOTO3 = False

try:
    import kubernetes as _kubernetes_pkg  # type: ignore[import-untyped]  # noqa: F401

    _HAS_K8S = True
except ImportError:
    _HAS_K8S = False

try:
    from mcp.server.fastmcp import FastMCP  # type: ignore[import-untyped]
    from mcp.server.transport_security import (  # type: ignore[import-untyped]
        TransportSecuritySettings,
    )

    mcp: Any = FastMCP(
        "kagent-healer-tools",
        host="0.0.0.0",
        port=int(os.environ.get("MCP_PORT", "8080")),
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    _HAS_MCP = True
except Exception:
    mcp = None
    _HAS_MCP = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safety constants (re-read in init() to pick up late env changes)
# ---------------------------------------------------------------------------

CONFIDENCE_THRESHOLD: float = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.75"))
MAX_REPLICAS: int = int(os.environ.get("MAX_REPLICAS", "10"))
DRY_RUN: bool = os.environ.get("DRY_RUN", "true").lower() == "true"
APPROVAL_TIMEOUT_SECONDS: int = int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", "300"))
WEBHOOK_BASE_URL: str = os.environ.get("WEBHOOK_BASE_URL", "")

PROTECTED_NAMESPACES: frozenset[str] = frozenset(
    {
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "monitoring",
        "litmus",
        "kagent",
        "external-secrets",
        "cert-manager",
        "aws-load-balancer-controller",
        "local-path-storage",
    }
)

# ---------------------------------------------------------------------------
# Module-level state (set by init())
# ---------------------------------------------------------------------------

_core: Any = None
_apps: Any = None
_db_path: str = "/tmp/kagent-memory.db"
_audit_path: str = "/tmp/kagent-audit.jsonl"
_slack_url: str = ""
_cw_client: Any = None
_db_lock: threading.Lock = threading.Lock()

_scale_state: dict[str, tuple[str, str, int]] = {}
_scale_lock: threading.Lock = threading.Lock()


# ---------------------------------------------------------------------------
# ApprovalStore — thread-safe HITL approval registry
# ---------------------------------------------------------------------------


class ApprovalStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, threading.Event] = {}

    def register(self, action_id: str) -> threading.Event:
        event = threading.Event()
        with self._lock:
            self._pending[action_id] = event
        return event

    def approve(self, action_id: str) -> bool:
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


approval_store: ApprovalStore = ApprovalStore()


# ---------------------------------------------------------------------------
# init()
# ---------------------------------------------------------------------------


def init(
    core_api: Any,
    apps_api: Any,
    db_path: str,
    audit_path: str,
    slack_url: str,
    aws_region: str,
) -> None:
    global _core, _apps, _db_path, _audit_path, _slack_url, _cw_client
    global CONFIDENCE_THRESHOLD, MAX_REPLICAS, DRY_RUN, APPROVAL_TIMEOUT_SECONDS, WEBHOOK_BASE_URL

    _core = core_api
    _apps = apps_api
    _db_path = db_path
    _audit_path = audit_path
    _slack_url = slack_url

    CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.75"))
    MAX_REPLICAS = int(os.environ.get("MAX_REPLICAS", "10"))
    DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
    APPROVAL_TIMEOUT_SECONDS = int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", "300"))
    WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL", "")

    try:
        _init_db(db_path)
    except Exception as exc:
        logger.error("SQLite init failed: %s", exc)

    if os.environ.get("KUBERNETES_SERVICE_HOST") and _HAS_BOTO3:
        try:
            _cw_client = boto3.client("cloudwatch", region_name=aws_region)
        except Exception as exc:
            logger.error("CloudWatch client init failed: %s", exc)


def _init_db(path: str) -> None:
    with _db_lock:
        con = sqlite3.connect(path)
        try:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS incidents (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_type TEXT NOT NULL,
                    diagnosis  TEXT NOT NULL,
                    action     TEXT NOT NULL,
                    outcome    TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_alert_type ON incidents(alert_type);
            """)
            con.commit()
        finally:
            con.close()


# ---------------------------------------------------------------------------
# Safety gate helpers
# ---------------------------------------------------------------------------


def _confidence_gate(confidence: float) -> str | None:
    if confidence < CONFIDENCE_THRESHOLD:
        return f"Skipped: confidence {confidence:.2f} below threshold {CONFIDENCE_THRESHOLD:.2f}"
    return None


def _namespace_gate(namespace: str) -> str | None:
    if namespace in PROTECTED_NAMESPACES:
        return f"Skipped: namespace '{namespace}' is protected"
    return None


def _dry_run_gate(action: str, target: str) -> str | None:
    if DRY_RUN:
        logger.info("DRY_RUN: would execute %s on %s", action, target)
        return f"DRY_RUN: would execute {action} on {target}"
    return None


# ---------------------------------------------------------------------------
# Memory tool
# ---------------------------------------------------------------------------


def recall_past_cases(alert_type: str) -> str:
    """Retrieve the 3 most recent incident records for an alert type from SQLite memory."""
    try:
        with _db_lock:
            con = sqlite3.connect(_db_path)
            try:
                rows = con.execute(
                    "SELECT diagnosis, action, outcome, confidence, created_at "
                    "FROM incidents WHERE alert_type = ? ORDER BY id DESC LIMIT 3",
                    (alert_type,),
                ).fetchall()
            finally:
                con.close()
        if not rows:
            return "No past cases."
        lines = [
            f"- [{created_at}] diagnosis={diagnosis!r}, action={action}, "
            f"outcome={outcome!r}, confidence={confidence:.2f}"
            for diagnosis, action, outcome, confidence, created_at in rows
        ]
        return "\n".join(lines)
    except Exception as exc:
        return f"Unavailable: {exc}"


# ---------------------------------------------------------------------------
# Write tools — confidence → namespace → dry-run gates enforced first
# ---------------------------------------------------------------------------


def restart_deployment(
    namespace: str, deployment: str, confidence: float, reason: str
) -> str:
    """Rolling restart a Deployment by patching the restartedAt annotation."""
    if msg := _confidence_gate(confidence):
        return msg
    if msg := _namespace_gate(namespace):
        return msg
    if msg := _dry_run_gate("restart_deployment", f"{namespace}/{deployment}"):
        return msg
    if _apps is None:
        return "Unavailable: Kubernetes client not initialised"
    try:
        now = datetime.now(UTC).isoformat()
        _apps.patch_namespaced_deployment(
            name=deployment,
            namespace=namespace,
            body={
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {"kubectl.kubernetes.io/restartedAt": now}
                        }
                    }
                }
            },
        )
        return f"Restarted deployment/{deployment} in {namespace}"
    except Exception as exc:
        return f"Failed: {exc}"


def scale_deployment(
    namespace: str,
    deployment: str,
    confidence: float,
    reason: str,
    alert_key: str = "",
) -> str:
    """Scale a Deployment up by 1 replica. Records original count for auto-restore on resolve."""
    if msg := _confidence_gate(confidence):
        return msg
    if msg := _namespace_gate(namespace):
        return msg
    if msg := _dry_run_gate("scale_deployment", f"{namespace}/{deployment}"):
        return msg
    if _apps is None:
        return "Unavailable: Kubernetes client not initialised"
    try:
        scale = _apps.read_namespaced_deployment_scale(
            name=deployment, namespace=namespace
        )
        current = scale.spec.replicas or 0
        if current >= MAX_REPLICAS:
            return f"Skipped: already at max replicas ({current})"
        new_count = current + 1
        if alert_key:
            with _scale_lock:
                if alert_key not in _scale_state:
                    _scale_state[alert_key] = (namespace, deployment, current)
        _apps.patch_namespaced_deployment_scale(
            name=deployment,
            namespace=namespace,
            body={"spec": {"replicas": new_count}},
        )
        return f"Scaled deployment/{deployment} from {current} to {new_count} replicas"
    except Exception as exc:
        return f"Failed: {exc}"


def cordon_node(node_name: str, confidence: float, reason: str) -> str:
    """Mark a node unschedulable. Sends Slack HITL request and waits for approval."""
    if msg := _confidence_gate(confidence):
        return msg
    if msg := _dry_run_gate("cordon_node", node_name):
        return msg
    if _core is None:
        return "Unavailable: Kubernetes client not initialised"

    action_id = f"cordon-{node_name}-{uuid.uuid4().hex[:8]}"
    event = approval_store.register(action_id)
    approval_url = (
        f"{WEBHOOK_BASE_URL}/approve/{action_id}"
        if WEBHOOK_BASE_URL
        else f"/approve/{action_id}"
    )
    _post_slack(
        f":warning: HITL approval requested: `cordon_node` on *{node_name}*\n"
        f"Reason: {reason}\n"
        f"Approve: {approval_url}\n"
        f"Auto-approves in {APPROVAL_TIMEOUT_SECONDS}s"
    )
    event.wait(timeout=APPROVAL_TIMEOUT_SECONDS)
    approval_store.cancel(action_id)

    try:
        _core.patch_node(name=node_name, body={"spec": {"unschedulable": True}})
        return f"Cordoned node {node_name}"
    except Exception as exc:
        return f"Failed: {exc}"


def drain_node(node_name: str, confidence: float, reason: str) -> str:
    """Cordon and evict all non-DaemonSet pods from a node. Requires HITL approval."""
    if msg := _confidence_gate(confidence):
        return msg
    if msg := _dry_run_gate("drain_node", node_name):
        return msg
    if _core is None:
        return "Unavailable: Kubernetes client not initialised"

    action_id = f"drain-{node_name}-{uuid.uuid4().hex[:8]}"
    event = approval_store.register(action_id)
    approval_url = (
        f"{WEBHOOK_BASE_URL}/approve/{action_id}"
        if WEBHOOK_BASE_URL
        else f"/approve/{action_id}"
    )
    _post_slack(
        f":warning: HITL approval requested: `drain_node` on *{node_name}*\n"
        f"Reason: {reason}\n"
        f"Approve: {approval_url}\n"
        f"Auto-approves in {APPROVAL_TIMEOUT_SECONDS}s"
    )
    event.wait(timeout=APPROVAL_TIMEOUT_SECONDS)
    approval_store.cancel(action_id)

    try:
        _core.patch_node(name=node_name, body={"spec": {"unschedulable": True}})
    except Exception as exc:
        return f"Failed to cordon before drain: {exc}"

    try:
        pod_list = _core.list_pod_for_all_namespaces(
            field_selector=f"spec.nodeName={node_name}"
        )
    except Exception as exc:
        return f"Failed to list pods: {exc}"

    evicted = 0
    skipped = 0
    pdb_blocked: list[tuple[str, str]] = []

    for pod in pod_list.items:
        ns = pod.metadata.namespace
        pname = pod.metadata.name
        owner_kinds = {ref.kind for ref in (pod.metadata.owner_references or [])}
        if "DaemonSet" in owner_kinds or "Node" in owner_kinds:
            skipped += 1
            continue
        if ns in PROTECTED_NAMESPACES:
            skipped += 1
            continue
        try:
            _evict_pod(ns, pname)
            evicted += 1
        except Exception as exc:
            if "429" in str(exc) or "PodDisruptionBudget" in str(exc):
                pdb_blocked.append((ns, pname))
            else:
                skipped += 1

    for delay in [5, 15, 30]:
        if not pdb_blocked:
            break
        time.sleep(delay)
        still_blocked = []
        for ns, pname in pdb_blocked:
            try:
                _evict_pod(ns, pname)
                evicted += 1
            except Exception:
                still_blocked.append((ns, pname))
        pdb_blocked = still_blocked
    skipped += len(pdb_blocked)

    return (
        f"Drained {node_name}: evicted {evicted} pods, "
        f"skipped {skipped} (DaemonSet/protected/PDB-blocked)"
    )


def _evict_pod(namespace: str, pod: str) -> None:
    if not _HAS_K8S:
        raise RuntimeError("kubernetes package not installed")
    from kubernetes.client.models import (  # type: ignore[import-untyped]
        V1Eviction,
        V1ObjectMeta,
    )

    eviction = V1Eviction(metadata=V1ObjectMeta(name=pod, namespace=namespace))
    _core.create_namespaced_pod_eviction(name=pod, namespace=namespace, body=eviction)


# ---------------------------------------------------------------------------
# record_outcome — final tool kagent calls after every investigation
# ---------------------------------------------------------------------------


def record_outcome(
    alert_key: str,
    alert_name: str,
    severity: str,
    diagnosis: str,
    action: str,
    target: str,
    namespace: str,
    confidence: float,
    executed: bool,
    outcome: str,
    dry_run: bool,
) -> str:
    """Record investigation outcome: audit JSONL, Slack, CloudWatch, SQLite."""
    timestamp = datetime.now(UTC).isoformat()

    # 1. Audit JSONL
    try:
        entry = {
            "timestamp": timestamp,
            "alert_key": alert_key,
            "alert_name": alert_name,
            "severity": severity,
            "diagnosis": diagnosis,
            "action": action,
            "target": target,
            "namespace": namespace,
            "confidence": confidence,
            "executed": executed,
            "outcome": outcome,
            "dry_run": dry_run,
        }
        with open(_audit_path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception as exc:
        logger.error("Audit log write failed: %s", exc)

    # 2. Slack
    if _slack_url:
        try:
            emoji = "✅" if executed else "⛔"
            _post_slack(
                f"{emoji} *KAgent* | {alert_key}\n"
                f"Action: `{action}` | Confidence: {confidence:.2f}\n"
                f"Diagnosis: {diagnosis}\n"
                f"Outcome: {outcome}"
            )
        except Exception as exc:
            logger.error("Slack post failed: %s", exc)

    # 3. CloudWatch
    if _cw_client:
        try:
            _cw_client.put_metric_data(
                Namespace="KAgent/HealingEvents",
                MetricData=[
                    {
                        "MetricName": "HealingAction",
                        "Dimensions": [
                            {"Name": "Action", "Value": action},
                            {"Name": "Executed", "Value": str(executed)},
                        ],
                        "Value": 1.0,
                        "Unit": "Count",
                    }
                ],
            )
        except Exception as exc:
            logger.error("CloudWatch put_metric_data failed: %s", exc)

    # 4. SQLite
    try:
        with _db_lock:
            con = sqlite3.connect(_db_path)
            try:
                con.execute(
                    "INSERT INTO incidents "
                    "(alert_type, diagnosis, action, outcome, confidence, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (alert_name, diagnosis, action, outcome, confidence, timestamp),
                )
                con.commit()
            finally:
                con.close()
    except Exception as exc:
        logger.error("SQLite insert failed: %s", exc)

    return f"Outcome recorded for {alert_key}"


def _post_slack(text: str) -> None:
    if not _slack_url or not _HAS_REQUESTS:
        return
    requests.post(_slack_url, json={"text": text}, timeout=10)


# ---------------------------------------------------------------------------
# scale_down_if_resolved — called by bridge.py on resolved alerts, not a kagent tool
# ---------------------------------------------------------------------------


def scale_down_if_resolved(alert_key: str) -> None:
    with _scale_lock:
        entry = _scale_state.pop(alert_key, None)
    if entry is None:
        return
    namespace, deployment, original_replicas = entry
    if DRY_RUN:
        logger.info(
            "DRY_RUN: would scale %s/%s back to %d",
            namespace,
            deployment,
            original_replicas,
        )
        return
    if _apps is None:
        logger.error("scale_down_if_resolved: Kubernetes client not initialised")
        return
    try:
        _apps.patch_namespaced_deployment_scale(
            name=deployment,
            namespace=namespace,
            body={"spec": {"replicas": original_replicas}},
        )
        logger.info(
            "Scaled %s/%s back to %d replicas (alert resolved)",
            namespace,
            deployment,
            original_replicas,
        )
    except Exception as exc:
        logger.error(
            "scale_down_if_resolved failed for %s/%s: %s",
            namespace,
            deployment,
            exc,
        )


# ---------------------------------------------------------------------------
# FastMCP tool registrations — kagent calls these via SSE on port 8080
# ---------------------------------------------------------------------------

if _HAS_MCP and mcp is not None:

    @mcp.tool()
    def _mcp_recall_past_cases(alert_type: str) -> str:
        """Retrieve the 3 most recent incident records for this alert type."""
        return recall_past_cases(alert_type)

    @mcp.tool()
    def _mcp_restart_deployment(
        namespace: str, deployment: str, confidence: float, reason: str
    ) -> str:
        """Rolling restart a Deployment. Enforces confidence, namespace, and dry-run gates."""
        return restart_deployment(namespace, deployment, confidence, reason)

    @mcp.tool()
    def _mcp_scale_deployment(
        namespace: str,
        deployment: str,
        confidence: float,
        reason: str,
        alert_key: str = "",
    ) -> str:
        """Scale a Deployment up by 1 replica (max MAX_REPLICAS). Records original for auto-restore."""
        return scale_deployment(namespace, deployment, confidence, reason, alert_key)

    @mcp.tool()
    def _mcp_cordon_node(node_name: str, confidence: float, reason: str) -> str:
        """Mark node unschedulable. Sends Slack HITL request; auto-approves after timeout."""
        return cordon_node(node_name, confidence, reason)

    @mcp.tool()
    def _mcp_drain_node(node_name: str, confidence: float, reason: str) -> str:
        """Cordon and evict all non-DaemonSet pods. Requires HITL approval."""
        return drain_node(node_name, confidence, reason)

    @mcp.tool()
    def _mcp_record_outcome(
        alert_key: str,
        alert_name: str,
        severity: str,
        diagnosis: str,
        action: str,
        target: str,
        namespace: str,
        confidence: float,
        executed: bool,
        outcome: str,
        dry_run: bool,
    ) -> str:
        """Record the final outcome. ALWAYS call this as your last tool."""
        return record_outcome(
            alert_key,
            alert_name,
            severity,
            diagnosis,
            action,
            target,
            namespace,
            confidence,
            executed,
            outcome,
            dry_run,
        )


if __name__ == "__main__":
    if not _HAS_MCP:
        raise SystemExit("mcp package not installed")
    mcp.run(transport="sse")
