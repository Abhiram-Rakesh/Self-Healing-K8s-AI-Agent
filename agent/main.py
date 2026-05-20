"""KAgent startup: init MCP server, run bridge in thread, serve FastMCP SSE server.

Process layout:
  Thread 1 (main): FastMCP SSE server on MCP_PORT (default 8080) — kagent calls write tools here
  Thread 2 (daemon): BridgeServer on WEBHOOK_PORT (default 8000) — Alertmanager posts here
"""

from __future__ import annotations

import logging
import os
import sys
import threading

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
            "Kubernetes config not available (%s) — write tools degraded", exc
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


def main() -> int:
    _configure_logging()
    _load_secrets_from_aws()
    _init_mcp_server()

    from agent.bridge import BridgeServer
    from agent.mcp_server import _HAS_MCP, mcp

    # Start the Alertmanager bridge in a background thread
    bridge = BridgeServer(port=int(os.environ.get("WEBHOOK_PORT", "8000")))
    bridge_thread = threading.Thread(target=bridge.start, daemon=True, name="bridge")
    bridge_thread.start()
    logger.info("Bridge thread started on :%s", os.environ.get("WEBHOOK_PORT", "8000"))

    if not _HAS_MCP or mcp is None:
        logger.error("FastMCP not available — cannot serve write tools to kagent")
        bridge_thread.join()
        return 1

    # FastMCP SSE server runs in the main thread (blocking, uses asyncio internally)
    logger.info("Starting FastMCP SSE server on port %d", mcp.settings.port)
    try:
        mcp.run(transport="sse")
    except KeyboardInterrupt:
        bridge.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
