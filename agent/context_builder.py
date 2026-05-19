"""
Builds Kubernetes context for Gemini diagnosis prompts.

Collects pod logs, events, pod state, and node conditions.
Never raises — always returns partial context on error.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

try:  # pragma: no cover - thin import shim
    from kubernetes import client as k8s_client  # type: ignore
    from kubernetes import config as k8s_config  # type: ignore
    from kubernetes.client.rest import ApiException  # type: ignore

    _HAS_K8S = True
except Exception:  # pragma: no cover
    k8s_client = None  # type: ignore
    k8s_config = None  # type: ignore
    ApiException = Exception  # type: ignore
    _HAS_K8S = False


def _unavailable(msg: str) -> str:
    return f"Unavailable: {msg}"


class ContextBuilder:
    """Collects context about a failing pod for the Gemini prompt."""

    def __init__(self) -> None:
        self._core: Any = None
        self._apps: Any = None
        self._configured = False
        self._configure()

    def _configure(self) -> None:
        if not _HAS_K8S:
            logger.warning("kubernetes client not importable — context will be empty")
            return
        try:
            k8s_config.load_incluster_config()
            logger.info("Loaded in-cluster kubeconfig")
        except Exception:
            try:
                k8s_config.load_kube_config()
                logger.info("Loaded local kubeconfig")
            except Exception as exc:
                logger.error("Failed to load any kubeconfig: %s", exc)
                return
        try:
            self._core = k8s_client.CoreV1Api()
            self._apps = k8s_client.AppsV1Api()
            self._configured = True
        except Exception as exc:
            logger.error("Failed to construct K8s API clients: %s", exc)

    @staticmethod
    def _alert_labels(alert: dict[str, Any]) -> dict[str, str]:
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
        out: dict[str, str] = {}
        for src in (annotations, labels):
            for k, v in src.items():
                out.setdefault(str(k), str(v))
        return out

    def _fetch_pod_logs(self, namespace: str, pod: str) -> str:
        if not self._configured or not pod or not namespace:
            return _unavailable("no pod/namespace or k8s not configured")
        # Try previous first (crash-loop case), then current.
        for previous in (True, False):
            try:
                logs = self._core.read_namespaced_pod_log(
                    name=pod,
                    namespace=namespace,
                    tail_lines=50,
                    previous=previous,
                )
                if logs:
                    label = "previous" if previous else "current"
                    return f"[{label}]\n{logs}"
            except ApiException as exc:
                logger.debug(
                    "pod logs (previous=%s) failed: %s", previous, getattr(exc, "reason", exc)
                )
                continue
            except Exception as exc:
                logger.debug("pod logs (previous=%s) failed: %s", previous, exc)
                continue
        return _unavailable("no logs available")

    def _fetch_events(self, namespace: str, pod: str) -> list[dict[str, Any]]:
        if not self._configured or not pod or not namespace:
            return []
        try:
            events = self._core.list_namespaced_event(
                namespace=namespace,
                field_selector=f"involvedObject.name={pod}",
            )
            items = list(events.items or [])

            def _ts(e: Any) -> Any:
                return getattr(e, "last_timestamp", None) or getattr(
                    e, "event_time", None
                )

            items.sort(key=lambda e: _ts(e) or "", reverse=True)
            out = []
            for ev in items[:10]:
                out.append(
                    {
                        "type": getattr(ev, "type", ""),
                        "reason": getattr(ev, "reason", ""),
                        "message": getattr(ev, "message", ""),
                        "count": getattr(ev, "count", 0),
                        "timestamp": str(_ts(ev) or ""),
                    }
                )
            return out
        except Exception as exc:
            logger.debug("events lookup failed: %s", exc)
            return []

    def _describe_pod(self, namespace: str, pod: str) -> dict[str, Any] | str:
        if not self._configured or not pod or not namespace:
            return _unavailable("no pod/namespace or k8s not configured")
        try:
            p = self._core.read_namespaced_pod(name=pod, namespace=namespace)
        except Exception as exc:
            return _unavailable(str(exc))
        try:
            containers = []
            for c in (p.spec.containers or []) if p.spec else []:
                resources = c.resources.to_dict() if c.resources else {}
                containers.append(
                    {
                        "name": c.name,
                        "image": c.image,
                        "resources": resources,
                    }
                )
            statuses = []
            for cs in (p.status.container_statuses or []) if p.status else []:
                state = {}
                if cs.state:
                    for key in ("running", "waiting", "terminated"):
                        v = getattr(cs.state, key, None)
                        if v is not None:
                            state[key] = v.to_dict() if hasattr(v, "to_dict") else str(v)
                statuses.append(
                    {
                        "name": cs.name,
                        "ready": cs.ready,
                        "restartCount": cs.restart_count,
                        "state": state,
                    }
                )
            return {
                "phase": getattr(p.status, "phase", "") if p.status else "",
                "node": getattr(p.spec, "node_name", "") if p.spec else "",
                "containers": containers,
                "containerStatuses": statuses,
            }
        except Exception as exc:
            return _unavailable(str(exc))

    def _node_conditions(self, node_name: str) -> list[dict[str, Any]] | str:
        if not self._configured or not node_name:
            return _unavailable("no node name or k8s not configured")
        try:
            node = self._core.read_node(name=node_name)
            conds = []
            for c in (node.status.conditions or []) if node.status else []:
                conds.append(
                    {
                        "type": getattr(c, "type", ""),
                        "status": getattr(c, "status", ""),
                        "reason": getattr(c, "reason", ""),
                        "message": getattr(c, "message", ""),
                    }
                )
            return conds
        except Exception as exc:
            return _unavailable(str(exc))

    def _resolve_deployment(self, namespace: str, pod: str) -> str:
        """Walk ownerReferences: Pod → ReplicaSet → Deployment."""
        if not self._configured or not pod or not namespace:
            return ""
        try:
            p = self._core.read_namespaced_pod(name=pod, namespace=namespace)
            rs_name = ""
            for ref in (p.metadata.owner_references or []):
                if getattr(ref, "kind", "") == "ReplicaSet":
                    rs_name = str(ref.name)
                    break
            if not rs_name:
                return ""
            rs = self._apps.read_namespaced_replica_set(name=rs_name, namespace=namespace)
            for ref in (rs.metadata.owner_references or []):
                if getattr(ref, "kind", "") == "Deployment":
                    return str(ref.name)
            return ""
        except Exception as exc:
            logger.debug("pod→deployment resolution failed for %s/%s: %s", namespace, pod, exc)
            return ""

    def build(self, alert: dict[str, Any]) -> dict[str, Any]:
        """Return a context dict; every field handles its own errors."""
        labels = self._alert_labels(alert)
        alert_name = (
            labels.get("alertname") or labels.get("alert_name") or "UnknownAlert"
        )
        severity = labels.get("severity", "unknown")
        pod = labels.get("pod", "")
        namespace = labels.get("namespace", "")

        pod_describe = self._describe_pod(namespace, pod)
        node_name = ""
        if isinstance(pod_describe, dict):
            node_name = str(pod_describe.get("node", ""))

        deployment_name = self._resolve_deployment(namespace, pod)

        try:
            pod_logs = self._fetch_pod_logs(namespace, pod)
        except Exception as exc:
            pod_logs = _unavailable(str(exc))

        try:
            events = self._fetch_events(namespace, pod)
        except Exception as exc:
            events = []
            logger.debug("events wrapper failed: %s", exc)

        try:
            node_conditions = self._node_conditions(node_name)
        except Exception as exc:
            node_conditions = _unavailable(str(exc))

        return {
            "alert_name": alert_name,
            "severity": severity,
            "pod_name": pod,
            "namespace": namespace,
            "deployment_name": deployment_name,
            "node_name": node_name,
            "pod_logs": pod_logs,
            "k8s_events": events,
            "pod_describe": pod_describe,
            "node_conditions": node_conditions,
        }
