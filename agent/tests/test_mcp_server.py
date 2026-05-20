"""Tests for agent/mcp_server.py — safety gates, ApprovalStore, record_outcome, scale."""

from __future__ import annotations

import json
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import agent.mcp_server as mcp

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
# recall_past_cases
# ---------------------------------------------------------------------------


def test_recall_past_cases_empty():
    assert mcp.recall_past_cases("PodCrashLooping") == "No past cases."


def test_recall_past_cases_returns_rows():
    ts = "2024-01-01T00:00:00+00:00"
    with sqlite3.connect(mcp._db_path) as con:
        con.execute(
            "INSERT INTO incidents (alert_type, diagnosis, action, outcome, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("PodCrashLooping", "OOM killed", "restart_deployment", "success", 0.9, ts),
        )
        con.commit()
    result = mcp.recall_past_cases("PodCrashLooping")
    assert "OOM killed" in result


# ---------------------------------------------------------------------------
# restart_deployment — confidence / namespace / dry-run gates
# ---------------------------------------------------------------------------


def test_restart_deployment_low_confidence_skipped(mock_apps):
    result = mcp.restart_deployment("default", "myapp", confidence=0.5, reason="test")
    assert "below threshold" in result
    mock_apps.patch_namespaced_deployment.assert_not_called()


def test_restart_deployment_protected_namespace_skipped(mock_apps):
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
    result = mcp.restart_deployment("default", "myapp", confidence=0.9, reason="test")
    assert "Restarted" in result
    mock_apps.patch_namespaced_deployment.assert_called_once()
    call_args = mock_apps.patch_namespaced_deployment.call_args
    body = call_args.kwargs.get("body") or call_args.args[2]
    annotation = body["spec"]["template"]["metadata"]["annotations"]
    assert "kubectl.kubernetes.io/restartedAt" in annotation


# ---------------------------------------------------------------------------
# scale_deployment
# ---------------------------------------------------------------------------


def test_scale_deployment_increments_by_one(mock_apps):
    scale_obj = SimpleNamespace(spec=SimpleNamespace(replicas=3))
    mock_apps.read_namespaced_deployment_scale.return_value = scale_obj
    result = mcp.scale_deployment("default", "myapp", confidence=0.9, reason="test")
    assert "3 to 4" in result
    mock_apps.patch_namespaced_deployment_scale.assert_called_once()


def test_scale_deployment_respects_max(mock_apps):
    mcp.MAX_REPLICAS = 5
    scale_obj = SimpleNamespace(spec=SimpleNamespace(replicas=5))
    mock_apps.read_namespaced_deployment_scale.return_value = scale_obj
    result = mcp.scale_deployment("default", "myapp", confidence=0.9, reason="test")
    assert "max replicas" in result
    mock_apps.patch_namespaced_deployment_scale.assert_not_called()


def test_scale_deployment_records_original_count(mock_apps):
    scale_obj = SimpleNamespace(spec=SimpleNamespace(replicas=2))
    mock_apps.read_namespaced_deployment_scale.return_value = scale_obj
    mcp.scale_deployment(
        "default", "myapp", confidence=0.9, reason="test", alert_key="Alert:default:pod"
    )
    assert mcp._scale_state["Alert:default:pod"] == ("default", "myapp", 2)


# ---------------------------------------------------------------------------
# record_outcome
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
    assert rows[0] == ("TestAlert", "notify_only")


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
# ApprovalStore
# ---------------------------------------------------------------------------


def test_approve_sets_event():
    store = mcp.ApprovalStore()
    event = store.register("abc123")
    assert not event.is_set()
    assert store.approve("abc123") is True
    assert event.is_set()


def test_unknown_id_returns_false():
    assert mcp.ApprovalStore().approve("does-not-exist") is False


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
# scale_down_if_resolved
# ---------------------------------------------------------------------------


def test_scale_down_restores_original_replicas(mock_apps):
    mcp._scale_state["Alert:default:pod"] = ("default", "myapp", 2)
    mcp.scale_down_if_resolved("Alert:default:pod")
    mock_apps.patch_namespaced_deployment_scale.assert_called_once()
    kwargs = mock_apps.patch_namespaced_deployment_scale.call_args.kwargs
    assert kwargs["body"]["spec"]["replicas"] == 2
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
