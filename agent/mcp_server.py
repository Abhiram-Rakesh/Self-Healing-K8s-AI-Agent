"""MCP-native Kubernetes toolbox for the KAgent self-healing agent.

All external I/O lives here: Kubernetes reads/writes, SQLite, Slack, CloudWatch.
Gemini calls these tools via ALL_DECLARATIONS. Safety gates are enforced inside
each write tool — not in pipeline or gemini code.
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

    mcp: Any = FastMCP("kagent-k8s-tools")
    _HAS_MCP = True
except Exception:
    mcp = None
    _HAS_MCP = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment constants (re-read in init() to pick up late env changes)
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
# ApprovalStore
# ---------------------------------------------------------------------------


class ApprovalStore:
    """Thread-safe store of pending HITL approvals."""

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
# Read tools
# ---------------------------------------------------------------------------


def get_pod_logs(namespace: str, pod: str) -> str:
    if _core is None:
        return "Unavailable: Kubernetes client not initialised"
    try:
        logs = _core.read_namespaced_pod_log(
            name=pod, namespace=namespace, tail_lines=50, previous=True
        )
        return f"[previous]\n{logs}"
    except Exception:
        pass
    try:
        logs = _core.read_namespaced_pod_log(
            name=pod, namespace=namespace, tail_lines=50, previous=False
        )
        return f"[current]\n{logs}"
    except Exception as exc:
        return f"Unavailable: no logs ({exc})"


def get_pod_events(namespace: str, pod: str) -> str:
    if _core is None:
        return "Unavailable: Kubernetes client not initialised"
    try:
        resp = _core.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={pod}",
        )
        events = sorted(
            resp.items,
            key=lambda e: str(e.last_timestamp or ""),
            reverse=True,
        )
        result = [
            {
                "type": ev.type,
                "reason": ev.reason,
                "message": ev.message,
                "count": ev.count,
                "last_timestamp": str(ev.last_timestamp),
            }
            for ev in events[:10]
        ]
        return json.dumps(result, indent=2) if result else "No events found."
    except Exception as exc:
        return f"Unavailable: {exc}"


def describe_pod(namespace: str, pod: str) -> str:
    if _core is None:
        return "Unavailable: Kubernetes client not initialised"
    try:
        p = _core.read_namespaced_pod(name=pod, namespace=namespace)
        spec = p.spec
        status = p.status
        containers = []
        for c in spec.containers or []:
            res = c.resources or type("R", (), {"requests": None, "limits": None})()
            containers.append(
                {
                    "name": c.name,
                    "image": c.image,
                    "resources": {
                        "requests": dict(res.requests or {}),
                        "limits": dict(res.limits or {}),
                    },
                }
            )
        container_statuses = []
        for cs in status.container_statuses or []:
            state: dict[str, Any] = {}
            if cs.state:
                if cs.state.running:
                    state = {"running": True}
                elif cs.state.waiting:
                    state = {
                        "waiting": {
                            "reason": cs.state.waiting.reason,
                            "message": cs.state.waiting.message,
                        }
                    }
                elif cs.state.terminated:
                    state = {
                        "terminated": {
                            "reason": cs.state.terminated.reason,
                            "exit_code": cs.state.terminated.exit_code,
                        }
                    }
            container_statuses.append(
                {
                    "name": cs.name,
                    "ready": cs.ready,
                    "restartCount": cs.restart_count,
                    "state": state,
                }
            )
        return json.dumps(
            {
                "phase": status.phase,
                "node": spec.node_name,
                "containers": containers,
                "containerStatuses": container_statuses,
            },
            indent=2,
        )
    except Exception as exc:
        return f"Unavailable: {exc}"


def get_node_conditions(node_name: str) -> str:
    if _core is None:
        return "Unavailable: Kubernetes client not initialised"
    try:
        node = _core.read_node(name=node_name)
        conditions = [
            {
                "type": cond.type,
                "status": cond.status,
                "reason": cond.reason,
                "message": cond.message,
            }
            for cond in (node.status.conditions or [])
        ]
        return json.dumps(conditions, indent=2)
    except Exception as exc:
        return f"Unavailable: {exc}"


def resolve_deployment(namespace: str, pod: str) -> str:
    if _core is None or _apps is None:
        return "Could not resolve deployment"
    try:
        p = _core.read_namespaced_pod(name=pod, namespace=namespace)
        rs_name = next(
            (
                ref.name
                for ref in (p.metadata.owner_references or [])
                if ref.kind == "ReplicaSet"
            ),
            None,
        )
        if not rs_name:
            return "Could not resolve deployment"
        rs = _apps.read_namespaced_replica_set(name=rs_name, namespace=namespace)
        deploy_name = next(
            (
                ref.name
                for ref in (rs.metadata.owner_references or [])
                if ref.kind == "Deployment"
            ),
            None,
        )
        return deploy_name if deploy_name else "Could not resolve deployment"
    except Exception:
        return "Could not resolve deployment"


def recall_past_cases(alert_type: str) -> str:
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
# Write tools (each enforces confidence → namespace → dry-run gates)
# ---------------------------------------------------------------------------


def restart_deployment(
    namespace: str, deployment: str, confidence: float, reason: str
) -> str:
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
# record_outcome — always the last tool Gemini calls
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
# scale_down_if_resolved — called directly by pipeline.run(), not a Gemini tool
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
# call_tool — in-process dispatch for GeminiClient
# ---------------------------------------------------------------------------

_DISPATCH: dict[str, Any] = {
    "get_pod_logs": get_pod_logs,
    "get_pod_events": get_pod_events,
    "describe_pod": describe_pod,
    "get_node_conditions": get_node_conditions,
    "resolve_deployment": resolve_deployment,
    "recall_past_cases": recall_past_cases,
    "restart_deployment": restart_deployment,
    "scale_deployment": scale_deployment,
    "cordon_node": cordon_node,
    "drain_node": drain_node,
    "record_outcome": record_outcome,
}


def call_tool(name: str, args: dict) -> str:
    fn = _DISPATCH.get(name)
    if fn is None:
        return f"Unknown tool: {name}"
    try:
        return str(fn(**args))
    except Exception as exc:
        return f"Tool error ({name}): {exc}"


# ---------------------------------------------------------------------------
# ALL_DECLARATIONS — Gemini function-calling format
# ---------------------------------------------------------------------------

ALL_DECLARATIONS: list[dict] = [
    {
        "name": "get_pod_logs",
        "description": (
            "Fetch the last 50 lines of pod logs. Tries previous=True first "
            "(crash-loop case), falls back to current. Returns [previous] or [current] label."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Kubernetes namespace"},
                "pod": {"type": "string", "description": "Pod name"},
            },
            "required": ["namespace", "pod"],
        },
    },
    {
        "name": "get_pod_events",
        "description": "List the 10 most recent events for a pod, sorted newest-first.",
        "parameters": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Kubernetes namespace"},
                "pod": {"type": "string", "description": "Pod name"},
            },
            "required": ["namespace", "pod"],
        },
    },
    {
        "name": "describe_pod",
        "description": "Return pod phase, node, container specs, and container statuses as JSON.",
        "parameters": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Kubernetes namespace"},
                "pod": {"type": "string", "description": "Pod name"},
            },
            "required": ["namespace", "pod"],
        },
    },
    {
        "name": "get_node_conditions",
        "description": "Return all node conditions (Ready, MemoryPressure, DiskPressure, etc.) as JSON.",
        "parameters": {
            "type": "object",
            "properties": {
                "node_name": {"type": "string", "description": "Kubernetes node name"},
            },
            "required": ["node_name"],
        },
    },
    {
        "name": "resolve_deployment",
        "description": "Walk Pod→ReplicaSet→Deployment ownerReferences and return the Deployment name.",
        "parameters": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Kubernetes namespace"},
                "pod": {"type": "string", "description": "Pod name"},
            },
            "required": ["namespace", "pod"],
        },
    },
    {
        "name": "recall_past_cases",
        "description": "Retrieve the 3 most recent incident records for an alert type from SQLite memory.",
        "parameters": {
            "type": "object",
            "properties": {
                "alert_type": {
                    "type": "string",
                    "description": "Alert name, e.g. PodCrashLooping",
                },
            },
            "required": ["alert_type"],
        },
    },
    {
        "name": "restart_deployment",
        "description": (
            "Rolling restart a Deployment by patching the restartedAt annotation. "
            "Enforces confidence, protected-namespace, and dry-run gates."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Kubernetes namespace"},
                "deployment": {
                    "type": "string",
                    "description": "Deployment name (NOT pod name)",
                },
                "confidence": {
                    "type": "number",
                    "description": "Diagnosis confidence 0.0–1.0",
                },
                "reason": {"type": "string", "description": "Human-readable reason"},
            },
            "required": ["namespace", "deployment", "confidence", "reason"],
        },
    },
    {
        "name": "scale_deployment",
        "description": (
            "Scale a Deployment up by 1 replica (ceiling: MAX_REPLICAS). "
            "Records original count for auto-scale-down on alert resolve."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Kubernetes namespace"},
                "deployment": {"type": "string", "description": "Deployment name"},
                "confidence": {
                    "type": "number",
                    "description": "Diagnosis confidence 0.0–1.0",
                },
                "reason": {"type": "string", "description": "Human-readable reason"},
                "alert_key": {
                    "type": "string",
                    "description": "Alert key for scale-down tracking (optional)",
                },
            },
            "required": ["namespace", "deployment", "confidence", "reason"],
        },
    },
    {
        "name": "cordon_node",
        "description": (
            "Mark a node as unschedulable. Requires HITL approval via Slack "
            "(auto-approves after APPROVAL_TIMEOUT_SECONDS)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node_name": {"type": "string", "description": "Kubernetes node name"},
                "confidence": {
                    "type": "number",
                    "description": "Diagnosis confidence 0.0–1.0",
                },
                "reason": {"type": "string", "description": "Human-readable reason"},
            },
            "required": ["node_name", "confidence", "reason"],
        },
    },
    {
        "name": "drain_node",
        "description": (
            "Cordon and evict all non-DaemonSet pods from a node. "
            "Requires HITL approval. Retries PDB-blocked pods."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node_name": {"type": "string", "description": "Kubernetes node name"},
                "confidence": {
                    "type": "number",
                    "description": "Diagnosis confidence 0.0–1.0",
                },
                "reason": {"type": "string", "description": "Human-readable reason"},
            },
            "required": ["node_name", "confidence", "reason"],
        },
    },
    {
        "name": "record_outcome",
        "description": (
            "Record the final outcome of an investigation: writes audit log, "
            "posts Slack, publishes CloudWatch metric, inserts SQLite row. "
            "ALWAYS call this as your last tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "alert_key": {
                    "type": "string",
                    "description": "Unique alert key (alertname:namespace:pod)",
                },
                "alert_name": {"type": "string", "description": "Alert name"},
                "severity": {"type": "string", "description": "Alert severity"},
                "diagnosis": {"type": "string", "description": "Root cause diagnosis"},
                "action": {
                    "type": "string",
                    "description": (
                        "Action taken: restart_deployment, scale_deployment, "
                        "cordon_node, drain_node, notify_only, or no_action"
                    ),
                },
                "target": {
                    "type": "string",
                    "description": "Resource targeted (deployment or node name)",
                },
                "namespace": {"type": "string", "description": "Kubernetes namespace"},
                "confidence": {
                    "type": "number",
                    "description": "Final confidence 0.0–1.0",
                },
                "executed": {
                    "type": "boolean",
                    "description": "Whether the healing action was actually executed",
                },
                "outcome": {
                    "type": "string",
                    "description": "Human-readable outcome description",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Whether this was a dry-run",
                },
            },
            "required": [
                "alert_key",
                "alert_name",
                "severity",
                "diagnosis",
                "action",
                "target",
                "namespace",
                "confidence",
                "executed",
                "outcome",
                "dry_run",
            ],
        },
    },
]


# ---------------------------------------------------------------------------
# FastMCP registrations — active only when mcp package is installed
# ---------------------------------------------------------------------------

if _HAS_MCP and mcp is not None:

    @mcp.tool()
    def _mcp_get_pod_logs(namespace: str, pod: str) -> str:  # noqa: F811
        return get_pod_logs(namespace, pod)

    @mcp.tool()
    def _mcp_get_pod_events(namespace: str, pod: str) -> str:
        return get_pod_events(namespace, pod)

    @mcp.tool()
    def _mcp_describe_pod(namespace: str, pod: str) -> str:
        return describe_pod(namespace, pod)

    @mcp.tool()
    def _mcp_get_node_conditions(node_name: str) -> str:
        return get_node_conditions(node_name)

    @mcp.tool()
    def _mcp_resolve_deployment(namespace: str, pod: str) -> str:
        return resolve_deployment(namespace, pod)

    @mcp.tool()
    def _mcp_recall_past_cases(alert_type: str) -> str:
        return recall_past_cases(alert_type)

    @mcp.tool()
    def _mcp_restart_deployment(
        namespace: str, deployment: str, confidence: float, reason: str
    ) -> str:
        return restart_deployment(namespace, deployment, confidence, reason)

    @mcp.tool()
    def _mcp_scale_deployment(
        namespace: str,
        deployment: str,
        confidence: float,
        reason: str,
        alert_key: str = "",
    ) -> str:
        return scale_deployment(namespace, deployment, confidence, reason, alert_key)

    @mcp.tool()
    def _mcp_cordon_node(node_name: str, confidence: float, reason: str) -> str:
        return cordon_node(node_name, confidence, reason)

    @mcp.tool()
    def _mcp_drain_node(node_name: str, confidence: float, reason: str) -> str:
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
    mcp.run()
