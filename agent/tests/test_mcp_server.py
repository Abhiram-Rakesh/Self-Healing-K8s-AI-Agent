"""Tests for agent/mcp_server.py — Kubernetes tools, safety gates, ApprovalStore."""

from __future__ import annotations

import json
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import agent.mcp_server as mcp

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_container(name="app", image="nginx:latest"):
    resources = SimpleNamespace(requests={"cpu": "100m"}, limits={"memory": "256Mi"})
    return SimpleNamespace(name=name, image=image, resources=resources)


def _make_cs(name="app", ready=True, restart_count=0, state=None):
    s = state or SimpleNamespace(
        running=SimpleNamespace(), waiting=None, terminated=None
    )
    return SimpleNamespace(name=name, ready=ready, restart_count=restart_count, state=s)


def _make_pod(
    phase="Running", node_name="node-1", containers=None, container_statuses=None
):
    spec = SimpleNamespace(
        node_name=node_name,
        containers=containers or [_make_container()],
        owner_references=[],
    )
    status = SimpleNamespace(
        phase=phase,
        container_statuses=container_statuses or [_make_cs()],
    )
    meta = SimpleNamespace(
        owner_references=[],
        namespace="default",
        name="mypod",
    )
    return SimpleNamespace(spec=spec, status=status, metadata=meta)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_core():
    return MagicMock()


@pytest.fixture
def mock_apps():
    return MagicMock()


@pytest.fixture(autouse=True)
def setup_mcp(tmp_path, mock_core, mock_apps):
    """Initialise mcp_server with mocked K8s clients and a temp DB for each test."""
    os.environ.pop("DRY_RUN", None)
    mcp.init(
        core_api=mock_core,
        apps_api=mock_apps,
        db_path=str(tmp_path / "test.db"),
        audit_path=str(tmp_path / "audit.jsonl"),
        slack_url="",
        aws_region="us-east-1",
    )
    mcp.DRY_RUN = False
    mcp._scale_state.clear()
    yield
    mcp._scale_state.clear()


# ---------------------------------------------------------------------------
# Read tool tests
# ---------------------------------------------------------------------------


def test_get_pod_logs_returns_previous_first(mock_core):
    mock_core.read_namespaced_pod_log.return_value = "PREV LOG"
    result = mcp.get_pod_logs("default", "mypod")
    assert "[previous]" in result
    assert "PREV LOG" in result


def test_get_pod_logs_falls_back_to_current(mock_core):
    def side_effect(**kwargs):
        if kwargs.get("previous"):
            raise Exception("no previous")
        return "CURR LOG"

    mock_core.read_namespaced_pod_log.side_effect = side_effect
    result = mcp.get_pod_logs("default", "mypod")
    assert "[current]" in result
    assert "CURR LOG" in result


def test_get_pod_events_sorted_descending(mock_core):
    ev1 = SimpleNamespace(
        type="Normal",
        reason="Started",
        message="started",
        count=1,
        last_timestamp="2024-01-01T00:00:00",
    )
    ev2 = SimpleNamespace(
        type="Warning",
        reason="BackOff",
        message="crash",
        count=5,
        last_timestamp="2024-01-02T00:00:00",
    )
    mock_core.list_namespaced_event.return_value = SimpleNamespace(items=[ev1, ev2])
    result = mcp.get_pod_events("default", "mypod")
    parsed = json.loads(result)
    assert parsed[0]["reason"] == "BackOff"
    assert parsed[1]["reason"] == "Started"


def test_describe_pod_returns_phase_and_node(mock_core):
    mock_core.read_namespaced_pod.return_value = _make_pod(
        phase="Running", node_name="ip-1-2-3-4"
    )
    result = mcp.describe_pod("default", "mypod")
    data = json.loads(result)
    assert data["phase"] == "Running"
    assert data["node"] == "ip-1-2-3-4"


def test_get_node_conditions_returns_json(mock_core):
    cond = SimpleNamespace(
        type="Ready", status="True", reason="KubeletReady", message="ok"
    )
    node = SimpleNamespace(status=SimpleNamespace(conditions=[cond]))
    mock_core.read_node.return_value = node
    result = mcp.get_node_conditions("ip-1-2-3-4")
    data = json.loads(result)
    assert data[0]["type"] == "Ready"
    assert data[0]["status"] == "True"


def test_resolve_deployment_walks_owner_refs(mock_core, mock_apps):
    rs_ref = SimpleNamespace(kind="ReplicaSet", name="myapp-rs")
    deploy_ref = SimpleNamespace(kind="Deployment", name="myapp")
    pod = SimpleNamespace(metadata=SimpleNamespace(owner_references=[rs_ref]))
    rs = SimpleNamespace(metadata=SimpleNamespace(owner_references=[deploy_ref]))
    mock_core.read_namespaced_pod.return_value = pod
    mock_apps.read_namespaced_replica_set.return_value = rs

    result = mcp.resolve_deployment("default", "mypod")
    assert result == "myapp"


def test_recall_past_cases_returns_no_past_cases_when_empty():
    result = mcp.recall_past_cases("PodCrashLooping")
    assert result == "No past cases."


def test_recall_past_cases_returns_matching_rows():
    ts = "2024-01-01T00:00:00+00:00"
    with sqlite3.connect(mcp._db_path) as con:
        con.execute(
            "INSERT INTO incidents (alert_type, diagnosis, action, outcome, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("PodCrashLooping", "OOM killed", "restart_deployment", "success", 0.9, ts),
        )
        con.execute(
            "INSERT INTO incidents (alert_type, diagnosis, action, outcome, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "PodCrashLooping",
                "config error",
                "restart_deployment",
                "success",
                0.85,
                ts,
            ),
        )
        con.commit()

    result = mcp.recall_past_cases("PodCrashLooping")
    assert "OOM killed" in result
    assert "config error" in result


# ---------------------------------------------------------------------------
# Write tool safety gate tests
# ---------------------------------------------------------------------------


def test_restart_deployment_low_confidence_skipped(mock_apps):
    result = mcp.restart_deployment("default", "myapp", confidence=0.5, reason="test")
    assert "below threshold" in result
    mock_apps.patch_namespaced_deployment.assert_not_called()


def test_restart_deployment_protected_namespace_skipped(mock_apps):
    mcp.DRY_RUN = False
    result = mcp.restart_deployment(
        "kube-system", "coredns", confidence=0.95, reason="test"
    )
    assert "protected" in result
    mock_apps.patch_namespaced_deployment.assert_not_called()


def test_restart_deployment_dry_run_skips_patch(mock_apps):
    mcp.DRY_RUN = True
    result = mcp.restart_deployment("default", "myapp", confidence=0.9, reason="test")
    assert "DRY_RUN" in result
    mock_apps.patch_namespaced_deployment.assert_not_called()


def test_restart_deployment_patches_annotation(mock_apps):
    mcp.DRY_RUN = False
    result = mcp.restart_deployment("default", "myapp", confidence=0.9, reason="test")
    assert "Restarted" in result
    mock_apps.patch_namespaced_deployment.assert_called_once()
    call_args = mock_apps.patch_namespaced_deployment.call_args
    body = call_args.kwargs.get("body") or call_args.args[2]
    annotation = body["spec"]["template"]["metadata"]["annotations"]
    assert "kubectl.kubernetes.io/restartedAt" in annotation


def test_scale_deployment_increments_by_one(mock_apps):
    mcp.DRY_RUN = False
    scale_obj = SimpleNamespace(spec=SimpleNamespace(replicas=3))
    mock_apps.read_namespaced_deployment_scale.return_value = scale_obj

    result = mcp.scale_deployment("default", "myapp", confidence=0.9, reason="test")
    assert "3 to 4" in result
    mock_apps.patch_namespaced_deployment_scale.assert_called_once()


def test_scale_deployment_respects_max(mock_apps):
    mcp.DRY_RUN = False
    mcp.MAX_REPLICAS = 5
    scale_obj = SimpleNamespace(spec=SimpleNamespace(replicas=5))
    mock_apps.read_namespaced_deployment_scale.return_value = scale_obj

    result = mcp.scale_deployment("default", "myapp", confidence=0.9, reason="test")
    assert "max replicas" in result
    mock_apps.patch_namespaced_deployment_scale.assert_not_called()


def test_scale_deployment_records_original_count(mock_apps):
    mcp.DRY_RUN = False
    scale_obj = SimpleNamespace(spec=SimpleNamespace(replicas=2))
    mock_apps.read_namespaced_deployment_scale.return_value = scale_obj

    mcp.scale_deployment(
        "default", "myapp", confidence=0.9, reason="test", alert_key="Alert:default:pod"
    )
    assert "Alert:default:pod" in mcp._scale_state
    assert mcp._scale_state["Alert:default:pod"] == ("default", "myapp", 2)


# ---------------------------------------------------------------------------
# record_outcome tests
# ---------------------------------------------------------------------------


def test_record_outcome_writes_jsonl(tmp_path):
    mcp._audit_path = str(tmp_path / "audit.jsonl")
    mcp.record_outcome(
        alert_key="PodCrashLooping:default:mypod",
        alert_name="PodCrashLooping",
        severity="warning",
        diagnosis="OOM killed",
        action="restart_deployment",
        target="myapp",
        namespace="default",
        confidence=0.9,
        executed=True,
        outcome="Restarted successfully",
        dry_run=False,
    )
    with open(mcp._audit_path) as fh:
        data = json.loads(fh.readline())
    assert data["action"] == "restart_deployment"
    assert data["alert_key"] == "PodCrashLooping:default:mypod"
    assert data["executed"] is True


def test_record_outcome_stores_in_sqlite():
    mcp.record_outcome(
        alert_key="TestAlert:default:pod",
        alert_name="TestAlert",
        severity="critical",
        diagnosis="disk full",
        action="notify_only",
        target="unknown",
        namespace="default",
        confidence=0.6,
        executed=False,
        outcome="notified",
        dry_run=False,
    )
    with sqlite3.connect(mcp._db_path) as con:
        rows = con.execute("SELECT alert_type, action FROM incidents").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "TestAlert"
    assert rows[0][1] == "notify_only"


def test_record_outcome_posts_to_slack(tmp_path):
    mcp._slack_url = "http://fake-slack"
    mcp._audit_path = str(tmp_path / "audit.jsonl")
    with patch("agent.mcp_server.requests") as mock_requests:
        mock_requests.post = MagicMock()
        mcp.record_outcome(
            alert_key="k:n:p",
            alert_name="A",
            severity="warning",
            diagnosis="d",
            action="notify_only",
            target="t",
            namespace="default",
            confidence=0.7,
            executed=False,
            outcome="ok",
            dry_run=False,
        )
    mock_requests.post.assert_called_once()
    mcp._slack_url = ""


# ---------------------------------------------------------------------------
# ApprovalStore tests
# ---------------------------------------------------------------------------


def test_approve_sets_event():
    store = mcp.ApprovalStore()
    event = store.register("abc123")
    assert not event.is_set()
    ok = store.approve("abc123")
    assert ok is True
    assert event.is_set()


def test_unknown_id_returns_false():
    store = mcp.ApprovalStore()
    assert store.approve("does-not-exist") is False


def test_cancel_removes_entry():
    store = mcp.ApprovalStore()
    store.register("xyz")
    store.cancel("xyz")
    assert "xyz" not in store.pending_ids()


def test_approve_twice_returns_false():
    store = mcp.ApprovalStore()
    store.register("dup")
    store.approve("dup")
    assert store.approve("dup") is False


# ---------------------------------------------------------------------------
# scale_down_if_resolved tests
# ---------------------------------------------------------------------------


def test_scale_down_restores_original_replicas(mock_apps):
    mcp.DRY_RUN = False
    mcp._scale_state["Alert:default:pod"] = ("default", "myapp", 2)
    mcp.scale_down_if_resolved("Alert:default:pod")
    mock_apps.patch_namespaced_deployment_scale.assert_called_once()
    call_kwargs = mock_apps.patch_namespaced_deployment_scale.call_args.kwargs
    assert call_kwargs["body"]["spec"]["replicas"] == 2
    assert "Alert:default:pod" not in mcp._scale_state


def test_scale_down_noop_when_not_tracked(mock_apps):
    mcp.scale_down_if_resolved("unknown:key")
    mock_apps.patch_namespaced_deployment_scale.assert_not_called()


def test_scale_down_dry_run_skips_patch(mock_apps):
    mcp.DRY_RUN = True
    mcp._scale_state["Alert:default:pod"] = ("default", "myapp", 1)
    mcp.scale_down_if_resolved("Alert:default:pod")
    mock_apps.patch_namespaced_deployment_scale.assert_not_called()
    assert "Alert:default:pod" not in mcp._scale_state
