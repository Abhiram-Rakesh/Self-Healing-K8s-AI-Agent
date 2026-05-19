# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| `main` (latest) | Yes |
| Older tags | No — please upgrade |

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Please report them via [GitHub Private Security Advisories](../../security/advisories/new).
You can also email **abhiramrakesh@gmail.com** with the subject line
`[SECURITY] self-healing-k8s-kagent`.

Include:
- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept
- The affected component (agent code, Terraform, Helm chart, CI/CD)

You will receive an acknowledgement within **48 hours** and a resolution timeline
within **7 days** of triage.

## Disclosure policy

We follow [coordinated disclosure](https://en.wikipedia.org/wiki/Coordinated_vulnerability_disclosure).
Once a fix is merged and released, we will publish a GitHub Security Advisory
crediting the reporter (unless anonymity is requested).

## Security design notes

| Control | Detail |
|---------|--------|
| Dry-run by default | `DRY_RUN=true` in `values.yaml` — no live actions without explicit opt-in |
| Confidence gate | Each MCP write tool rejects calls below `CONFIDENCE_THRESHOLD` (default 0.75) |
| Protected namespaces | `kube-system`, `monitoring`, `kagent`, and others are never touched |
| HITL for high-impact actions | `cordon_node` / `drain_node` send a Slack notification and wait for approval |
| Non-root container | Agent runs as UID 1000, all capabilities dropped, read-only root filesystem |
| IRSA (no static keys) | AWS access uses IAM Roles for Service Accounts — no long-lived credentials |
| Secrets Manager | Gemini API key and Slack webhook are stored in AWS Secrets Manager, not in ConfigMaps |
| Trivy scan in CI | Every image push is scanned for CRITICAL CVEs before promotion |
| Least-privilege RBAC | ClusterRole grants only the specific verbs needed (get/list/watch/patch on targeted resources) |
