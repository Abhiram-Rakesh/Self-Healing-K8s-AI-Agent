#!/bin/bash
# demo.sh — repeatable end-to-end demo for the self-healing agent.
# Drops a crash-loop workload, an OOM workload, and a Litmus chaos experiment,
# then watches the agent diagnose and heal each one.
set -euo pipefail

# ---- helpers --------------------------------------------------------------
RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'
CYAN=$'\033[0;36m'
NC=$'\033[0m'

log()  { printf "%s[demo]%s %s\n" "$CYAN" "$NC" "$*"; }
ok()   { printf "%s[ ok ]%s %s\n" "$GREEN" "$NC" "$*"; }
warn() { printf "%s[warn]%s %s\n" "$YELLOW" "$NC" "$*"; }
die()  { printf "%s[err ]%s %s\n" "$RED" "$NC" "$*" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KAGENT_NS="${KAGENT_NS:-kagent}"
DEMO_TIMEOUT="${DEMO_TIMEOUT:-180}"

# Spinner that waits for a predicate to become true (or timeout).
wait_for() {
  local description="$1" timeout="$2" check_cmd="$3"
  local elapsed=0 chars="|/-\\" i=0
  while (( elapsed < timeout )); do
    if eval "$check_cmd" >/dev/null 2>&1; then
      printf "\r%s[ ok ]%s %s (after %ds)\n" "$GREEN" "$NC" "$description" "$elapsed"
      return 0
    fi
    printf "\r%s[wait]%s %s %s (%ds)" "$YELLOW" "$NC" "$description" "${chars:i++%${#chars}:1}" "$elapsed"
    sleep 2
    elapsed=$((elapsed + 2))
  done
  printf "\r%s[err ]%s %s — timed out after %ds\n" "$RED" "$NC" "$description" "$timeout"
  return 1
}

# ---- step 1: cluster health ----------------------------------------------
log "1) Cluster health"
kubectl get nodes -o wide || die "kubectl get nodes failed — check your kubeconfig"
kubectl get pods -n "$KAGENT_NS" -l app.kubernetes.io/name=kagent-healer

# ---- step 2: deploy crash-loop -------------------------------------------
log "2) Injecting crash-loop workload"
kubectl apply -f "$REPO_ROOT/k8s/test-workloads/crash-loop.yaml"

log "Waiting for CrashLoopBackOff state to be observed"
wait_for "crash-test in CrashLoopBackOff" 120 \
  "kubectl get pods -n default -l app=crash-test -o jsonpath='{.items[*].status.containerStatuses[*].state.waiting.reason}' | grep -q CrashLoopBackOff" \
  || warn "Did not observe CrashLoopBackOff in time — continuing"

# ---- step 3: wait for alert to reach the agent ---------------------------
log "3) Waiting for PodCrashLooping alert -> agent log"
wait_for "agent log mentions PodCrashLooping" "$DEMO_TIMEOUT" \
  "kubectl logs -n $KAGENT_NS -l app.kubernetes.io/name=kagent-healer --tail=200 | grep -q PodCrashLooping"

log "Recent agent logs:"
kubectl logs -n "$KAGENT_NS" -l app.kubernetes.io/name=kagent-healer --tail=20 || true

# ---- step 4: OOM workload -------------------------------------------------
log "4) Injecting OOMKilled workload"
kubectl apply -f "$REPO_ROOT/k8s/test-workloads/oom-test.yaml"

wait_for "agent log mentions PodOOMKilled" "$DEMO_TIMEOUT" \
  "kubectl logs -n $KAGENT_NS -l app.kubernetes.io/name=kagent-healer --tail=200 | grep -q PodOOMKilled" \
  || warn "Did not see OOMKilled diagnosis in time"

# ---- step 5: litmus chaos -------------------------------------------------
log "5) Running Litmus pod-delete chaos on demo-app"
kubectl apply -f "$REPO_ROOT/k8s/test-workloads/demo-app.yaml"
kubectl rollout status deploy/demo-app -n default --timeout=60s || true

if kubectl get ns litmus >/dev/null 2>&1; then
  kubectl apply -f "$REPO_ROOT/k8s/chaos/pod-delete-chaos.yaml"
  ok "Chaos engine submitted — observe ChaosResult with: kubectl get chaosresult -n litmus"
else
  warn "Litmus namespace not found — skipping chaos step. Install Litmus per the README."
fi

# ---- step 6: audit summary -----------------------------------------------
log "6) Audit log summary (last 20 events)"
POD=$(kubectl get pod -n "$KAGENT_NS" -l app.kubernetes.io/name=kagent-healer -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [[ -n "${POD:-}" ]]; then
  kubectl exec -n "$KAGENT_NS" "$POD" -- sh -c 'tail -n 20 /tmp/kagent-audit.jsonl 2>/dev/null || true'
else
  warn "Healer pod not found — skipping audit dump"
fi

# ---- step 7: cleanup ------------------------------------------------------
log "7) Cleaning up demo workloads"
kubectl delete -f "$REPO_ROOT/k8s/test-workloads/crash-loop.yaml" --ignore-not-found
kubectl delete -f "$REPO_ROOT/k8s/test-workloads/oom-test.yaml" --ignore-not-found
kubectl delete -f "$REPO_ROOT/k8s/test-workloads/demo-app.yaml" --ignore-not-found

# ---- step 8: grafana link ------------------------------------------------
log "8) Useful endpoints"
if kubectl -n monitoring get svc monitoring-grafana >/dev/null 2>&1; then
  printf "  Grafana:    kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80\n"
fi
if kubectl -n monitoring get svc monitoring-kube-prometheus-prometheus >/dev/null 2>&1; then
  printf "  Prometheus: kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 9090:9090\n"
fi
if kubectl -n monitoring get svc monitoring-kube-prometheus-alertmanager >/dev/null 2>&1; then
  printf "  Alertmanager: kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-alertmanager 9093:9093\n"
fi

ok "Demo finished."
