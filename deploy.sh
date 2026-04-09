#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# FSI AI Gateway — full deploy script
# Cluster : https://api.cluster-9n5fl.9n5fl.sandbox3963.opentlc.com:6443
# Namespace: fsi-ai-gateway
# Image    : quay.io/navila/ai-gateway:latest
#
# Usage:
#   chmod +x deploy.sh
#   ANTHROPIC_API_KEY=sk-ant-xxx ./deploy.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

NAMESPACE="fsi-ai-gateway"
IMAGE="quay.io/navila/ai-gateway:latest"
CLUSTER="https://api.cluster-9n5fl.9n5fl.sandbox3963.opentlc.com:6443"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()     { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
section() { echo -e "\n${GREEN}══════════════════════════════════════${NC}"; echo -e "${GREEN} $*${NC}"; echo -e "${GREEN}══════════════════════════════════════${NC}"; }

# ── Preflight ─────────────────────────────────────────────────────────────────
section "Preflight checks"

command -v oc     &>/dev/null || die "oc not found — install OpenShift CLI first"
command -v podman &>/dev/null || die "podman not found"

[[ -z "${ANTHROPIC_API_KEY:-}" ]] && \
  die "ANTHROPIC_API_KEY not set. Run: ANTHROPIC_API_KEY=sk-ant-xxx ./deploy.sh"

CURRENT_USER=$(oc whoami 2>/dev/null) || die "Not logged in to OpenShift. Run: oc login $CLUSTER"
info "Logged in as: $CURRENT_USER"
info "Cluster: $(oc whoami --show-server)"

# ── Namespace ─────────────────────────────────────────────────────────────────
section "Namespace"
if oc get project "$NAMESPACE" &>/dev/null; then
  info "Project $NAMESPACE already exists"
else
  info "Creating project $NAMESPACE"
  oc new-project "$NAMESPACE" \
    --description="FSI AI Gateway MVP" \
    --display-name="FSI AI Gateway"
fi
oc project "$NAMESPACE"

# ── Build & push image ────────────────────────────────────────────────────────
section "Build & push image"
info "Building $IMAGE"
podman build -t "$IMAGE" .

info "Pushing $IMAGE"
podman push "$IMAGE"

# ── Secrets ───────────────────────────────────────────────────────────────────
section "Secrets"
if oc get secret ai-gateway-secrets -n "$NAMESPACE" &>/dev/null; then
  warn "Secret ai-gateway-secrets already exists — deleting and recreating"
  oc delete secret ai-gateway-secrets -n "$NAMESPACE"
fi
oc create secret generic ai-gateway-secrets \
  --from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  -n "$NAMESPACE"
info "Secret created"

# ── Quay pull secret (if repo is private) ─────────────────────────────────────
# Uncomment if your quay.io/navila/ai-gateway repo is private:
# oc create secret docker-registry quay-pull-secret \
#   --docker-server=quay.io \
#   --docker-username=navila \
#   --docker-password="YOUR_QUAY_TOKEN" \
#   -n "$NAMESPACE"
# oc secrets link default quay-pull-secret --for=pull -n "$NAMESPACE"

# ── Apply manifests (ordered) ─────────────────────────────────────────────────
section "Applying manifests"

MANIFESTS=(
  openshift/configmap.yaml
  openshift/rbac.yaml
  openshift/quota.yaml
  openshift/pvc.yaml
  openshift/redis.yaml
  openshift/observability.yaml
  openshift/deployment.yaml
  openshift/service-route.yaml
  openshift/hpa.yaml
  openshift/pdb.yaml
  openshift/networkpolicy.yaml
)

for manifest in "${MANIFESTS[@]}"; do
  info "Applying $manifest"
  oc apply -f "$manifest" -n "$NAMESPACE"
done

# ── Wait for rollout ──────────────────────────────────────────────────────────
section "Waiting for rollout"
info "Redis..."
oc rollout status deployment/redis -n "$NAMESPACE" --timeout=3m

info "AI Gateway..."
oc rollout status deployment/ai-gateway -n "$NAMESPACE" --timeout=5m

info "Prometheus..."
oc rollout status deployment/prometheus -n "$NAMESPACE" --timeout=3m

info "Grafana..."
oc rollout status deployment/grafana -n "$NAMESPACE" --timeout=3m

# ── Smoke test ────────────────────────────────────────────────────────────────
section "Smoke test"
GATEWAY=$(oc get route ai-gateway -n "$NAMESPACE" -o jsonpath='{.spec.host}')
GRAFANA=$(oc get route grafana   -n "$NAMESPACE" -o jsonpath='{.spec.host}')

info "Gateway URL : https://$GATEWAY"
info "Grafana URL : https://$GRAFANA"

sleep 3  # give the pod a moment after rollout

HTTP=$(curl -sk -o /dev/null -w "%{http_code}" "https://$GATEWAY/health")
if [[ "$HTTP" == "200" ]]; then
  info "Health check passed (HTTP $HTTP)"
  curl -sk "https://$GATEWAY/health" | python3 -m json.tool
else
  warn "Health check returned HTTP $HTTP — checking pod logs:"
  oc logs -l app=ai-gateway -n "$NAMESPACE" --tail=20
fi

# ── Summary ───────────────────────────────────────────────────────────────────
section "Deploy complete"
echo ""
echo "  Gateway  : https://$GATEWAY"
echo "  Grafana  : https://$GRAFANA  (admin / CHANGE_THIS_PASSWORD)"
echo "  Budget   : https://$GATEWAY/v1/budget"
echo "  Metrics  : https://$GATEWAY/metrics"
echo "  API docs : https://$GATEWAY/docs"
echo ""
echo "  Quick test:"
echo "  curl -X POST https://$GATEWAY/v1/chat \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"message\": \"I was charged twice\", \"department\": \"CX\"}'"
echo ""
