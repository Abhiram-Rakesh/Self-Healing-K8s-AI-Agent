# Contributing

Thank you for taking the time to contribute. This guide covers everything you
need to get from a fresh clone to an open PR.

## Getting started

```bash
git clone https://github.com/YOUR_USERNAME/self-healing-k8s-kagent.git
cd self-healing-k8s-kagent

# Install Python runtime + dev tools
make install

# Verify everything works
make lint
make test
```

## Development workflow

1. **Fork** the repository and create a branch from `main`.
2. Make your changes — keep commits focused (one logical change per commit).
3. Run `make lint` and `make test` locally — CI will reject PRs where either fails.
4. Open a pull request against `main` using the PR template.

## Code style

| Tool | What it checks |
|------|----------------|
| `ruff` | Python linting (E/F/W/I rules) |
| `black` | Python formatting (88-char line length) |
| `mypy` | Static type checking (`--ignore-missing-imports`) — **failures block CI** |
| `hadolint` | Dockerfile best practices |
| `shellcheck` | Shell script correctness |
| `terraform fmt` | HCL formatting |
| `helm lint` | Helm chart validity |

Run all of them at once: `make lint`.

## Running tests

```bash
make test
# or directly:
pytest agent/tests/ -v --cov=agent --cov-report=term-missing
```

Tests must not require a live Kubernetes cluster or a real Gemini API key —
mock everything at the boundary.

## Adding a new healing action

All healing actions are MCP tools in `agent/mcp_server.py`. Gemini decides
which tool to call — your job is to implement the tool safely and declare it.

1. **Add the tool function** to `agent/mcp_server.py`. Write tools must enforce
   the three safety gates in this exact order before touching the cluster:
   ```python
   def my_action(namespace: str, target: str, confidence: float, reason: str) -> str:
       if (msg := _confidence_gate(confidence)):   return msg
       if (msg := _namespace_gate(namespace)):      return msg   # skip for node actions
       if (msg := _dry_run_gate("my_action", target)): return msg
       # ... actual K8s call ...
       return f"Did my_action on {target}"
   ```
2. **For high-impact actions** (destructive or cluster-wide), add the HITL
   approval flow inside the tool — the `ApprovalStore` pattern in `cordon_node`
   and `drain_node` is the reference implementation. The webhook server's
   `POST /approve/<id>` endpoint calls `mcp_server.approval_store.approve()`
   automatically.
3. **For scale-modifying actions**, store the original replica count in
   `_scale_state[alert_key]` so `scale_down_if_resolved()` can restore it when
   the alert resolves. See `scale_deployment` for the pattern.
4. **Register it in `_DISPATCH`** so `call_tool()` can find it:
   ```python
   _DISPATCH["my_action"] = my_action
   ```
5. **Add a declaration to `ALL_DECLARATIONS`** in Gemini function-calling
   format (JSON Schema, lowercase types). Gemini will not call the tool
   unless it appears here.
6. **Register with FastMCP** inside the `if _HAS_MCP and mcp is not None:` block
   so the standalone MCP server also exposes the new tool.
7. **Add a Prometheus alert rule** in `k8s/monitoring/alert-rules.yaml` that
   can trigger the new action.
8. **Add unit tests** in `agent/tests/test_mcp_server.py` — cover at least the
   confidence gate, namespace gate, dry-run gate, and the happy path.
9. **Update the healing actions table** in `README.md`.

## Adding a new alert rule

1. Edit `k8s/monitoring/alert-rules.yaml`.
2. Set `kagent: "true"` on the alert label so Alertmanager routes it to the
   webhook.
3. Document it in the "Custom alert rules" section of `README.md`.
4. Test it by applying a test workload from `k8s/test-workloads/`.

## Terraform changes

- Run `terraform fmt -recursive terraform/` before committing.
- Add any new variable to `terraform/variables.tf` with a `description` and
  safe `default`.
- Update `terraform/terraform.tfvars.example` with a commented example value.
- The CI workflow runs `terraform plan` on every PR — check the plan comment
  before merging.

## Safety checklist for MCP tool / alert-rule changes

- [ ] `PROTECTED_NAMESPACES` still includes `kube-system`, `kagent`, `monitoring`
- [ ] All three safety gates (confidence → namespace → dry-run) are the first three lines of every write tool in `agent/mcp_server.py`
- [ ] `DRY_RUN=true` remains the default in `helm/kagent-healer/values.yaml`
- [ ] High-impact actions use the `ApprovalStore` HITL flow (see `cordon_node` / `drain_node`)
- [ ] Scale-modifying actions store their pre-change state in `_scale_state` and are reversed by `scale_down_if_resolved()` on alert resolution
- [ ] New tool is declared in `ALL_DECLARATIONS` and registered in `_DISPATCH`
- [ ] `WEBHOOK_TOKEN` is never hardcoded — leave `agent.webhookToken` empty in `values.yaml` and inject via `extraEnv` referencing a K8s Secret in production

## Helm chart changes

When modifying the Helm chart:

- **New env vars** belong in `helm/kagent-healer/templates/configmap.yaml` (non-sensitive) or injected via `extraEnv` referencing a K8s Secret (sensitive values like tokens or API keys). Never put secret values directly in `values.yaml`.
- **Persistence** is controlled by `persistence.enabled`. When `true`, the chart provisions a PVC and mounts it at `/data`; the configmap automatically overrides `MEMORY_DB_PATH` and `AUDIT_LOG_PATH` to `/data/...`. Dev default is `false` (emptyDir at `/tmp`); production default (`values-prod.yaml`) is `true`.
- **New volumes** must be mounted explicitly in `deployment.yaml` — the root filesystem is read-only (`readOnlyRootFilesystem: true`), so any path the agent writes to needs either an emptyDir or PVC mount.
- Run `helm lint helm/kagent-healer/ --set image.repository=placeholder` before pushing.

## Commit messages

Use the [Conventional Commits](https://www.conventionalcommits.org/) style:

```
feat: add drain_node healing action
fix: handle 429 retry in gemini tool-calling loop
chore: bump python:3.11-slim base image
docs: update README with Loki install step
```

## Releasing

Maintainers cut releases by pushing a semver tag:

```bash
git tag v1.2.3
git push origin v1.2.3
```

CI builds and pushes the versioned image to ECR and creates a GitHub Release
automatically.
