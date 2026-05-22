# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

**Self-Healing Kubernetes Cluster with KAgent** is an AI-powered self-healing platform for Amazon EKS. It integrates:
- **kagent framework** (CNCF sandbox) — manages Agent CRD and LLM tool-calling loop (default: Claude Haiku; switchable via `ModelConfig` CRD)
- **Prometheus + Alertmanager** — fires alerts on cluster health anomalies
- **Thin bridge** (Python, :8000) — receives Alertmanager webhooks, deduplicates, forwards to kagent
- **Custom MCP server** (Python 3.11 + FastMCP, :8080) — exposes safety-gated write tools and incident memory
- **PostgreSQL** (Bitnami Helm dep, production) / **SQLite** (local dev fallback) — persists incident history for context recall; PostgreSQL enables HPA/KEDA
- **Helm + Terraform** — reproducible infra provisioning on AWS EKS

## Architecture

```
Prometheus → Alertmanager → Bridge (:8000) → kagent Agent (A2A v0.3.0)
                                                    ↓
                                            Claude / Gemini LLM (tool-calling loop)
                                                    ↓
                                    ┌───────────────┴───────────────┐
                                    ↓                               ↓
                         kagent-tool-server              healer-mcp-server (:8080)
                         (read K8s tools)                (write + memory tools)
                              ↓                                ↓
                         Kubernetes API          ┌─────────────┴──────────────┐
                                                  ↓                            ↓
                                    PostgreSQL / SQLite memory       Safety gates + Slack HITL
```

### Key Components

**Bridge** (`agent/bridge.py`):
- Listens on `:8000` for Alertmanager webhook POSTs
- Deduplicates alerts within `DEDUP_TTL_SECONDS` (default 300s)
- Enforces daily budget via `CostGuard` (limits LLM calls forwarded per UTC day)
- Forwards firing alerts to kagent Agent via A2A v0.3.0 (JSON-RPC 2.0, message/send)
- Exposes `POST /approve/<action_id>` for HITL approval of cordon/drain actions
- Health endpoints: `/health`, `/healthz`, `/readyz`

**MCP Server** (`agent/mcp_server.py`):
- FastMCP SSE server on `:8080` — kagent calls write tools here
- Write tools enforce three safety gates (in order): confidence threshold → protected namespace → dry-run
- Write tools: `restart_deployment`, `scale_deployment`, `cordon_node`, `drain_node`
- Memory tools: `recall_past_cases` (PostgreSQL or SQLite), `record_outcome` (PostgreSQL or SQLite + Slack + CloudWatch)
- DB backend selected at runtime: `DATABASE_URL` env set → PostgreSQL (psycopg2); unset → SQLite fallback
- `ApprovalStore` — thread-safe registry for HITL approval of high-impact actions
- `_scale_state` — tracks original replica counts for auto-restore when alerts resolve

**Cost Guard** (`agent/cost_guard.py`):
- Thread-safe daily budget enforcer (resets at midnight UTC)
- Used by bridge to cap alerts forwarded per day (proxy for LLM API calls)
- Logs warning at 80% consumption

**Main** (`agent/main.py`):
- Loads secrets from AWS Secrets Manager (Slack webhook URL via IRSA)
- Initializes Kubernetes client (in-cluster or via kubeconfig)
- Initializes MCP server state (DB, audit log, Slack URL)
- Starts bridge in daemon thread on `WEBHOOK_PORT` (default 8000)
- Runs FastMCP SSE server in main thread on `MCP_PORT` (default 8080)

### Safety Gates

All write tools enforce (in order):
1. **Confidence gate**: `confidence < CONFIDENCE_THRESHOLD` (default 0.75) → rejected
2. **Namespace gate**: target namespace in `PROTECTED_NAMESPACES` → rejected
3. **Dry-run gate**: `DRY_RUN=true` → logged but not executed

HITL approval is required for `cordon_node` and `drain_node` (Slack message with `/approve/<id>` URL).

## Development Commands

### Setup & Install
```bash
make install              # Install Python runtime + dev tools (ruff, black, mypy, pytest, pytest-cov)
pip install -r agent/requirements.txt  # Runtime deps only
```

### Linting & Type Checking
```bash
make lint                 # Run all linters (ruff, black, mypy, terraform fmt, hadolint, shellcheck)
ruff check agent/         # Linting (E/F/I/N/W/B/UP rules, E501 ignored)
black --check agent/      # Format check (88-char lines)
mypy agent/ --ignore-missing-imports  # Type checking (strict mode)
```

### Testing
```bash
make test                 # Run tests with coverage (pytest + coverage.py)
pytest agent/tests/ -v --cov=agent --cov-report=term-missing
pytest agent/tests/test_bridge.py -v  # Single test file
pytest agent/tests/test_bridge.py::test_health_returns_200 -v  # Single test
```

### Building & Local Running
```bash
make build                # Docker build (multi-stage, amd64+arm64, non-root user)
docker build -t kagent-healer:local .
make run                  # docker-compose up (requires .env)
docker-compose up         # Same as make run
```

### Docker-Compose Local Development
Create `.env` from `.env.example`:
```bash
cp .env.example .env
# Edit .env: set KUBECONFIG, SLACK_WEBHOOK_URL (optional), etc.
docker-compose up
```
- Bridge listens on `:8000`
- MCP server listens on `:8080`
- Mounts `$HOME/.kube` for kubeconfig access (local cluster testing only)
- Health check probes `/health` every 30s

### Infrastructure & Deployment
```bash
make demo                 # E2E demo: crash injection → alert → diagnosis → healing (dry-run default)
./scripts/demo.sh         # Same; runs 9 phases with annotations
./scripts/port-forward.sh # Opens 6 port-forward tunnels (Grafana, Prometheus, Alertmanager, etc.)
make port-forward         # Same as above
./scripts/teardown.sh     # Destroys cluster + Helm releases + Terraform (asks for confirmation)
```

For full deployment instructions, see `README.md` Steps 1–10 (prerequisites, Terraform init, cluster add-ons, agent deployment, etc.).

## Configuration

### Environment Variables
| Var | Default | Set by | Purpose |
|-----|---------|--------|---------|
| `DRY_RUN` | `true` | Helm/env | Log actions but don't execute (safety default) |
| `CONFIDENCE_THRESHOLD` | `0.75` | Helm/env | Min confidence for write tools |
| `MAX_REPLICAS` | `10` | Helm/env | Hard cap for `scale_deployment` |
| `DAILY_REQUEST_LIMIT` | `200` | Helm/env | Daily LLM call budget (alerts forwarded per UTC day) |
| `DATABASE_URL` | `""` | Helm/env (Secret) | PostgreSQL DSN (`postgresql://user:pass@host:5432/db`); if set, SQLite is bypassed |
| `MEMORY_DB_PATH` | `/tmp/kagent-memory.db` | Helm/env | SQLite incident store path (ignored when `DATABASE_URL` is set) |
| `AUDIT_LOG_PATH` | `/tmp/kagent-audit.jsonl` | Helm/env | JSONL audit log |
| `LOG_LEVEL` | `INFO` | Helm/env | Python log level |
| `WEBHOOK_PORT` | `8000` | Helm/env | Bridge HTTP port |
| `MCP_PORT` | `8080` | Helm/env | MCP SSE port |
| `WEBHOOK_TOKEN` | `""` | Helm/env | If set, `/webhook` requires `Authorization: Bearer <token>` |
| `WEBHOOK_BASE_URL` | `""` | Helm/env | External URL for Slack `/approve/<id>` links |
| `KUBERNETES_SERVICE_HOST` | (auto) | K8s | Signals in-cluster mode; skips Secrets Manager if unset |
| `AWS_REGION` | `ap-south-1` | Helm/env | AWS region (Secrets Manager, CloudWatch) |
| `SECRETS_MANAGER_REGION` | `ap-south-1` | Helm/env | Region for Secrets Manager client |
| `SLACK_WEBHOOK_SECRET` | `kagent/slack-webhook` | Helm/env | Secrets Manager secret ID for Slack URL |

### Helm Values
Main chart: `helm/kagent-healer/values.yaml` (dev default — SQLite on `/tmp`, `postgresql.enabled=false`)
Production overlay: `helm/kagent-healer/values-prod.yaml` (PostgreSQL enabled, HPA enabled, PDB, NetworkPolicy, higher confidence threshold)

Before deploying with `postgresql.enabled=true`, run:
```bash
helm dependency update helm/kagent-healer/
kubectl create secret generic kagent-pg-secret --from-literal=password=<strong-password>
```

## Healing Actions Reference

| Alert | PromQL | Action(s) | Confidence | Gates |
|-------|--------|-----------|------------|-------|
| `PodCrashLooping` | `kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"} == 1` for 2m | `restart_deployment` | ≥0.75 | confidence, namespace, dry-run |
| `PodOOMKilled` | `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"} == 1` | `restart_deployment` or `scale_deployment` | ≥0.75 | confidence, namespace, dry-run |
| `PodPendingTooLong` | `kube_pod_status_phase{phase="Pending"} == 1` for 5m | `scale_deployment` or `notify_only` | ≥0.75 | confidence, namespace, dry-run |
| `NodeNotReady` | `kube_node_status_condition{condition="Ready",status="true"} == 0` for 2m | `cordon_node` or `drain_node` | ≥0.80 + HITL | confidence, HITL approval, dry-run |
| `PVCUsageHigh` | `kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes > 0.85` for 5m | `notify_only` | n/a | human decision only |

All confidence values < 0.70 force `notify_only` regardless of the LLM's suggestion (enforced server-side in write tools).

## Tests

Unit tests live in `agent/tests/`:
- `test_bridge.py` — Alertmanager webhook, dedup, auth, approval endpoint
- `test_mcp_server.py` — write tools, safety gates, SQLite memory, scale tracking
- `conftest.py` — shared fixtures (mock K8s clients, temp DB paths)

Tests use `unittest.mock` to stub K8s client, boto3, requests, and SQLite. No live cluster or LLM API key required.

## Code Style

| Tool | Config | Line length |
|------|--------|-------------|
| ruff | `pyproject.toml` | 88 (E501 ignored) |
| black | `pyproject.toml` | 88 |
| mypy | `pyproject.toml` | — |
| pytest | `pyproject.toml` | — |

All four must pass CI. Run `make lint` and `make test` before pushing.

## Adding a New Healing Action

1. Add tool function to `agent/mcp_server.py` — enforce safety gates in order:
   ```python
   @mcp.tool()
   def my_action(namespace: str, target: str, confidence: float, reason: str) -> str:
       if (msg := _confidence_gate(confidence)):      return msg
       if (msg := _namespace_gate(namespace)):         return msg
       if (msg := _dry_run_gate("my_action", target)): return msg
       # ... K8s call ...
       return f"Did my_action on {target}"
   ```

2. For high-impact actions, add HITL approval using `ApprovalStore` pattern (see `cordon_node`, `drain_node`).

3. For scale-modifying actions, store original replica count in `_scale_state[alert_key]` so `scale_down_if_resolved()` auto-restores.

4. Expose tool in `k8s/kagent/agent.yaml` under `healer-mcp-server` toolNames list.

5. Update system prompt in `k8s/kagent/agent.yaml` spec.declarative.systemMessage.

6. Add Prometheus alert rule in `k8s/monitoring/alert-rules.yaml` (must include labels: `severity`, `kagent: "true"`, `pod`, `namespace`).

7. Add unit tests in `agent/tests/test_mcp_server.py` — cover gates and happy path.

8. Update healing actions table in `README.md`.

## Adding a New Alert Rule

1. Edit `k8s/monitoring/alert-rules.yaml`.
2. Add entry under `spec.groups[0].rules` with required annotations:
   ```yaml
   - alert: MyNewAlert
     expr: my_metric{namespace=~".+"} > 0
     for: 2m
     labels:
       severity: warning
       kagent: "true"   # Required: routes to agent
     annotations:
       summary: "Something happened in {{ $labels.namespace }}/{{ $labels.pod }}"
       pod: "{{ $labels.pod }}"                # Required: MCP investigation
       namespace: "{{ $labels.namespace }}"    # Required: MCP investigation
   ```
3. Apply: `kubectl apply -f k8s/monitoring/alert-rules.yaml`.
4. Verify in Prometheus UI → Alerts; tail agent logs for first firing.

## Troubleshooting

**Agent pod not starting (CrashLoopBackOff)**:
- Check Secrets Manager access: `kubectl logs -n kagent <pod> --previous | grep -i secret`
- Confirm IRSA role annotation: `kubectl get sa -n default kagent-healer -o yaml | grep eks.amazonaws.com/role-arn`
- If Slack webhook not needed: set `agent.slackWebhookSecret=""` in Helm values

**Alerts fire but agent doesn't act**:
- Check AlertmanagerConfig routing with `kagent: "true"` label
- Verify bridge is receiving webhooks: `kubectl logs -n kagent ... | grep "Forwarded"`
- Check confidence threshold: `kubectl logs -n kagent ... | grep "below threshold"`

**Agent returns low confidence repeatedly**:
- Agent tools may be running too late (pod already deleted)
- Increase alert `for:` duration in alert-rules.yaml so pod survives investigation
- Verify tool output: `kubectl logs -n kagent -l app.kubernetes.io/name=kagent -f | grep -i "tool"`

**LLM API key invalid**:
- Error appears in kagent **controller** logs, not healer pod
- Anthropic: `kubectl create secret generic kagent-anthropic -n kagent --from-literal ANTHROPIC_API_KEY="new-key" --dry-run=client -o yaml | kubectl apply -f -`
- Gemini: `kubectl create secret generic kagent-gemini -n kagent --from-literal GOOGLE_API_KEY="new-key" --dry-run=client -o yaml | kubectl apply -f -`
- Restart controller: `kubectl rollout restart deployment/kagent -n kagent`

## Observability

- **Agent metrics** (request count, tool latency, token usage): kubectl metrics on kagent controller; UI at `:8083`
- **Logs** (bridge + MCP server): `kubectl logs -n default -l app.kubernetes.io/name=kagent-healer -f`
- **Structured logs** (Loki): Grafana → Explore → Loki → `{app="kagent-healer"}`
- **Dashboard** (pre-built): Grafana → auto-provisioned from ConfigMap at `k8s/monitoring/grafana-dashboard-configmap.yaml`
- **Audit log** (incident history): `kubectl exec -n default <kagent-healer-pod> -- cat /data/kagent-audit.jsonl | jq`

## CI/CD

GitHub Actions (`.github/workflows/ci.yml`):
1. **Lint** (Python + Infra): ruff, black, mypy, terraform fmt, hadolint, helm lint, shellcheck
2. **Test** (pytest + coverage): Python 3.11, coverage report to Codecov
3. **Build & push** (main branch only): Docker buildx (amd64+arm64), ECR push, Trivy scan (fail on CRITICAL)
4. **Release** (git tags v*.*.*): Docker buildx tag push, changelog generation, GitHub Release creation

Terraform CI: `.github/workflows/terraform.yml` (plan/apply for infrastructure).

Uses AWS OIDC (no long-lived credentials) and secrets for ECR registry.
