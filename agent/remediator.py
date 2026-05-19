"""
Executes Kubernetes healing actions with three safety gates.

Gate 1: Confidence threshold (default 0.75)
Gate 2: Protected namespace list
Gate 3: Dry-run mode (DRY_RUN env var)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

try:  # pragma: no cover
    from kubernetes import client as k8s_client  # type: ignore
    from kubernetes import config as k8s_config  # type: ignore
    from kubernetes.client.rest import ApiException  # type: ignore

    _HAS_K8S = True
except Exception:  # pragma: no cover
    k8s_client = None  # type: ignore
    k8s_config = None  # type: ignore
    ApiException = Exception  # type: ignore
    _HAS_K8S = False


CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.75"))
MAX_REPLICAS = int(os.environ.get("MAX_REPLICAS", "10"))

PROTECTED_NAMESPACES = {
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


def _is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "true").strip().lower() in ("1", "true", "yes")


def _result(
    plan: dict[str, Any],
    executed: bool,
    reason: str,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    return {
        "action": plan.get("action", "no_action"),
        "target": plan.get("target", "unknown"),
        "namespace": plan.get("target_namespace", "unknown"),
        "confidence": float(plan.get("confidence", 0.0)),
        "executed": bool(executed),
        "reason": reason,
        "dry_run": _is_dry_run() if dry_run is None else bool(dry_run),
    }


class Remediator:
    """Applies Gemini's plan to the cluster, with safety gates."""

    def __init__(
        self,
        confidence_threshold: float | None = None,
        max_replicas: int | None = None,
        protected_namespaces: set[str] | None = None,
    ) -> None:
        self.confidence_threshold = (
            CONFIDENCE_THRESHOLD if confidence_threshold is None else float(confidence_threshold)
        )
        self.max_replicas = MAX_REPLICAS if max_replicas is None else int(max_replicas)
        self.protected = (
            set(PROTECTED_NAMESPACES) if protected_namespaces is None else set(protected_namespaces)
        )
        self._apps: Any = None
        self._core: Any = None
        self._configured = False
        # alert_key → (namespace, deployment, original_replicas) for scale-down on resolution
        self._scale_state: dict[str, tuple[str, str, int]] = {}
        self._scale_lock = threading.Lock()
        self._configure()

    def _configure(self) -> None:
        if not _HAS_K8S:
            logger.warning("kubernetes client not importable — actions will not execute")
            return
        try:
            k8s_config.load_incluster_config()
        except Exception:
            try:
                k8s_config.load_kube_config()
            except Exception as exc:
                logger.error("Failed to load kubeconfig in remediator: %s", exc)
                return
        try:
            self._apps = k8s_client.AppsV1Api()
            self._core = k8s_client.CoreV1Api()
            self._configured = True
        except Exception as exc:
            logger.error("Failed to construct K8s API clients: %s", exc)

    # -------- actions ----------------------------------------------------

    def _restart_pod(self, namespace: str, deployment: str) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": ts,
                        }
                    }
                }
            }
        }
        self._apps.patch_namespaced_deployment(
            name=deployment, namespace=namespace, body=patch
        )

    def _scale_up(self, namespace: str, deployment: str) -> tuple[int, int]:
        """Return (original_replicas, new_replicas)."""
        dep = self._apps.read_namespaced_deployment(name=deployment, namespace=namespace)
        current = int(getattr(dep.spec, "replicas", 0) or 0)
        target = min(current + 1, self.max_replicas)
        if target == current:
            return current, current
        self._apps.patch_namespaced_deployment_scale(
            name=deployment,
            namespace=namespace,
            body={"spec": {"replicas": target}},
        )
        return current, target

    def _cordon_node(self, node_name: str) -> None:
        self._core.patch_node(
            name=node_name, body={"spec": {"unschedulable": True}}
        )

    def _evict_pod(self, pod: Any) -> bool:
        """Attempt eviction. Returns False only if PDB-blocked (HTTP 429), True otherwise."""
        try:
            self._core.create_namespaced_pod_eviction(
                name=pod.metadata.name,
                namespace=pod.metadata.namespace,
                body=k8s_client.V1Eviction(
                    metadata=k8s_client.V1ObjectMeta(
                        name=pod.metadata.name,
                        namespace=pod.metadata.namespace,
                    )
                ),
            )
            return True
        except ApiException as exc:
            if exc.status == 429:
                return False  # PDB-blocked — caller should retry
            logger.warning(
                "Eviction of %s/%s failed: %s",
                pod.metadata.namespace,
                pod.metadata.name,
                exc,
            )
            return True  # non-retryable failure — don't hold up the drain
        except Exception as exc:
            logger.warning(
                "Eviction of %s/%s failed: %s",
                pod.metadata.namespace,
                pod.metadata.name,
                exc,
            )
            return True

    def _drain_node(self, node_name: str) -> str:
        self._cordon_node(node_name)
        pods = self._core.list_pod_for_all_namespaces(
            field_selector=f"spec.nodeName={node_name}"
        )
        evicted: list[str] = []
        skipped: list[str] = []
        pdb_blocked: list[Any] = []

        for pod in pods.items:
            owners = pod.metadata.owner_references or []
            if any(o.kind in ("DaemonSet", "Node") for o in owners):
                skipped.append(pod.metadata.name)
                continue
            if pod.metadata.namespace in self.protected:
                skipped.append(pod.metadata.name)
                continue
            if self._evict_pod(pod):
                evicted.append(pod.metadata.name)
            else:
                pdb_blocked.append(pod)

        # Retry PDB-blocked pods with increasing backoff (5s → 15s → 30s).
        for delay in (5, 15, 30):
            if not pdb_blocked:
                break
            logger.info(
                "Drain: %d pod(s) PDB-blocked on %s — retrying in %ds",
                len(pdb_blocked),
                node_name,
                delay,
            )
            time.sleep(delay)
            still_blocked: list[Any] = []
            for pod in pdb_blocked:
                if self._evict_pod(pod):
                    evicted.append(pod.metadata.name)
                else:
                    still_blocked.append(pod)
            pdb_blocked = still_blocked

        skipped.extend(f"{p.metadata.name}(PDB-blocked)" for p in pdb_blocked)
        return (
            f"Drained node {node_name}: cordoned + evicted {len(evicted)} pod(s), "
            f"skipped {len(skipped)} (DaemonSet/protected/PDB-blocked)"
        )

    # -------- entry point ------------------------------------------------

    def execute(self, plan: dict[str, Any]) -> dict[str, Any]:
        action = str(plan.get("action", "no_action"))
        target = str(plan.get("target", "unknown"))
        namespace = str(plan.get("target_namespace", "unknown"))
        confidence = float(plan.get("confidence", 0.0))
        dry_run = _is_dry_run()

        # Passive actions short-circuit (still recorded as executed=True).
        if action in ("notify_only", "no_action"):
            return _result(plan, executed=True, reason=f"Passive action: {action}", dry_run=dry_run)

        # Gate 1: confidence
        if confidence < self.confidence_threshold:
            return _result(
                plan,
                executed=False,
                reason=(
                    f"Confidence {confidence:.2f} below threshold "
                    f"{self.confidence_threshold:.2f}"
                ),
                dry_run=dry_run,
            )

        # Gate 2: protected namespace (cordon_node may target any node — skip check there)
        if action != "cordon_node" and namespace in self.protected:
            return _result(
                plan,
                executed=False,
                reason=f"Namespace {namespace!r} is protected",
                dry_run=dry_run,
            )

        # Gate 3: dry-run
        if dry_run:
            logger.info(
                "DRY_RUN: would execute action=%s target=%s/%s confidence=%.2f plan=%s",
                action,
                namespace,
                target,
                confidence,
                json.dumps(plan, default=str),
            )
            return _result(
                plan,
                executed=True,
                reason=f"DRY_RUN: skipped real action {action}",
                dry_run=True,
            )

        # Real execution
        if not self._configured:
            return _result(
                plan,
                executed=False,
                reason="Kubernetes client not configured",
                dry_run=False,
            )

        try:
            if action == "restart_pod":
                self._restart_pod(namespace, target)
                reason = f"Patched deployment/{target} restartedAt annotation"
            elif action == "scale_up":
                original, new_replicas = self._scale_up(namespace, target)
                reason = f"Scaled deployment/{target} to {new_replicas} replicas"
                alert_key = str(plan.get("alert_key", ""))
                if alert_key and new_replicas != original:
                    with self._scale_lock:
                        # Only record the first scale-up so we can restore to true original.
                        if alert_key not in self._scale_state:
                            self._scale_state[alert_key] = (namespace, target, original)
            elif action == "cordon_node":
                self._cordon_node(target)
                reason = f"Cordoned node {target}"
            elif action == "drain_node":
                reason = self._drain_node(target)
            else:
                return _result(
                    plan,
                    executed=False,
                    reason=f"Unknown action {action!r}",
                    dry_run=False,
                )
        except ApiException as exc:
            logger.error("Action %s failed: %s", action, exc)
            return _result(plan, executed=False, reason=f"K8s API error: {exc}", dry_run=False)
        except Exception as exc:
            logger.error("Action %s failed: %s", action, exc)
            return _result(plan, executed=False, reason=f"Error: {exc}", dry_run=False)

        logger.info("Successfully executed %s on %s/%s", action, namespace, target)
        return _result(plan, executed=True, reason=reason, dry_run=False)

    def scale_down_if_tracked(self, alert_key: str) -> None:
        """Called when an alert resolves; reverses a tracked scale_up if one exists."""
        with self._scale_lock:
            state = self._scale_state.pop(alert_key, None)
        if state is None:
            return
        namespace, deployment, original_replicas = state
        if _is_dry_run():
            logger.info(
                "DRY_RUN: would scale down %s/%s to %d replica(s) (alert resolved: %s)",
                namespace,
                deployment,
                original_replicas,
                alert_key,
            )
            return
        if not self._configured:
            logger.warning("Cannot scale down %s/%s — K8s client not configured", namespace, deployment)
            return
        try:
            self._apps.patch_namespaced_deployment_scale(
                name=deployment,
                namespace=namespace,
                body={"spec": {"replicas": original_replicas}},
            )
            logger.info(
                "Scaled down %s/%s to %d replica(s) after alert resolved: %s",
                namespace,
                deployment,
                original_replicas,
                alert_key,
            )
        except Exception as exc:
            logger.error("Scale-down for %s/%s failed: %s", namespace, deployment, exc)
