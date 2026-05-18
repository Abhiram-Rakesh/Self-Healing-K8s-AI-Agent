#!/bin/bash
# teardown.sh — destroys everything in the correct order.
# Order matters: Helm releases first (cleans up AWS LBs), then namespaces,
# then a wait for ENIs to detach, then any straggler ELBs, then Terraform.
set -euo pipefail

RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'
CYAN=$'\033[0;36m'
NC=$'\033[0m'

log()  { printf "%s[teardown]%s %s\n" "$CYAN" "$NC" "$*"; }
ok()   { printf "%s[   ok   ]%s %s\n" "$GREEN" "$NC" "$*"; }
warn() { printf "%s[  warn  ]%s %s\n" "$YELLOW" "$NC" "$*"; }
die()  { printf "%s[  err   ]%s %s\n" "$RED" "$NC" "$*" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="${TF_DIR:-$REPO_ROOT/terraform}"
AWS_REGION="${AWS_REGION:-ap-south-1}"
CONFIRM_PHRASE="yes I want to destroy everything"

read -r -p "Type exactly: \"$CONFIRM_PHRASE\" to proceed > " RESPONSE
[[ "$RESPONSE" == "$CONFIRM_PHRASE" ]] || die "Confirmation phrase did not match — aborting"

# ---------------------------------------------------------------------------
# 1) Uninstall Helm releases (triggers controller cleanup of AWS LBs/TGs)
# ---------------------------------------------------------------------------
log "1) Uninstalling Helm releases"
for ns_release in \
    "kagent kagent-healer" \
    "kagent kagent" \
    "kagent kagent-crds" \
    "monitoring monitoring" \
    "litmus litmus"; do
  ns="${ns_release% *}"; release="${ns_release#* }"
  if helm -n "$ns" status "$release" >/dev/null 2>&1; then
    log "  helm uninstall $release -n $ns"
    helm -n "$ns" uninstall "$release" --wait --timeout 5m || warn "  uninstall of $release failed"
  fi
done

# ---------------------------------------------------------------------------
# 2) Delete namespaces (forces remaining workload + PVC cleanup)
# ---------------------------------------------------------------------------
log "2) Deleting namespaces"
for ns in kagent monitoring litmus; do
  if kubectl get ns "$ns" >/dev/null 2>&1; then
    log "  kubectl delete ns $ns"
    kubectl delete ns "$ns" --wait=true --timeout=5m || warn "  ns/$ns delete timed out"
  fi
done

# ---------------------------------------------------------------------------
# 3) Wait for AWS to detach and remove ENIs from the VPC's subnets
# ---------------------------------------------------------------------------
log "3) Waiting 60s for AWS to detach ENIs"
sleep 60

# ---------------------------------------------------------------------------
# 4) Force-delete any remaining ELBs in the VPC (Kubernetes controllers sometimes miss these)
# ---------------------------------------------------------------------------
VPC_ID="$(terraform -chdir="$TF_DIR" output -raw vpc_id 2>/dev/null || true)"
if [[ -n "$VPC_ID" ]]; then
  log "4) Cleaning leftover ELBs/NLBs in VPC $VPC_ID"
  # ALBs / NLBs
  LB_ARNS="$(aws elbv2 describe-load-balancers --region "$AWS_REGION" \
              --query "LoadBalancers[?VpcId=='$VPC_ID'].LoadBalancerArn" --output text || true)"
  for arn in $LB_ARNS; do
    log "  deleting $arn"
    aws elbv2 delete-load-balancer --region "$AWS_REGION" --load-balancer-arn "$arn" || warn "  delete $arn failed"
  done
  # Classic ELBs
  CLB_NAMES="$(aws elb describe-load-balancers --region "$AWS_REGION" \
                --query "LoadBalancerDescriptions[?VPCId=='$VPC_ID'].LoadBalancerName" --output text || true)"
  for name in $CLB_NAMES; do
    log "  deleting classic ELB $name"
    aws elb delete-load-balancer --region "$AWS_REGION" --load-balancer-name "$name" || warn "  delete $name failed"
  done
  if [[ -n "$LB_ARNS$CLB_NAMES" ]]; then
    log "  sleeping 60s for ENI propagation"
    sleep 60
  fi
else
  warn "Could not read vpc_id from terraform output — skipping ELB cleanup"
fi

# ---------------------------------------------------------------------------
# 5) terraform destroy
# ---------------------------------------------------------------------------
log "5) terraform destroy"
terraform -chdir="$TF_DIR" destroy -auto-approve || die "terraform destroy failed — see the README troubleshooting section"

ok "Teardown complete."
