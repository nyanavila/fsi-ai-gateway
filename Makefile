.PHONY: help dev test test-cov lint build push deploy clean

REGISTRY   ?= quay.io/nyanavila
IMAGE      ?= ai-gateway
TAG        ?= latest
NAMESPACE  ?= fsi-ai-gateway
FULL_IMAGE  = $(REGISTRY)/$(IMAGE):$(TAG)

help:
	@echo ""
	@echo "FSI AI Gateway — available targets"
	@echo "-----------------------------------"
	@echo "  make dev          Start local stack (podman compose)"
	@echo "  make test         Run test suite"
	@echo "  make test-cov     Run tests with HTML coverage report"
	@echo "  make lint         Run ruff linter"
	@echo "  make build        Build container image"
	@echo "  make push         Push image to registry"
	@echo "  make deploy       Apply all OpenShift manifests"
	@echo "  make rollout      Trigger rolling restart on OpenShift"
	@echo "  make status       Show pod and HPA status"
	@echo "  make logs         Tail gateway logs (pipe through jq)"
	@echo "  make clean        Remove local containers and volumes"
	@echo ""

dev:
	cp -n .env.example .env 2>/dev/null || true
	podman compose up --build

test:
	pytest tests/ -v

test-cov:
	pytest tests/ -v --cov=app --cov-report=html --cov-report=term-missing
	@echo "Coverage report: htmlcov/index.html"

lint:
	pip install ruff --quiet
	ruff check app/ tests/

build:
	podman build -t $(FULL_IMAGE) .
	@echo "Built: $(FULL_IMAGE)"

push: build
	podman push $(FULL_IMAGE)
	@echo "Pushed: $(FULL_IMAGE)"

deploy:
	oc apply -f openshift/secret.yaml
	oc apply -f openshift/configmap.yaml
	oc apply -f openshift/redis.yaml
	oc apply -f openshift/observability.yaml
	oc apply -f openshift/deployment.yaml
	oc apply -f openshift/service-route.yaml
	oc apply -f openshift/hpa.yaml
	oc apply -f openshift/pdb.yaml
	oc apply -f openshift/networkpolicy.yaml
	@echo "All manifests applied to namespace: $(NAMESPACE)"

rollout:
	oc rollout restart deployment/ai-gateway -n $(NAMESPACE)
	oc rollout status deployment/ai-gateway -n $(NAMESPACE)

status:
	@echo "=== Pods ==="
	oc get pods -n $(NAMESPACE) -l app=ai-gateway
	@echo ""
	@echo "=== HPA ==="
	oc get hpa ai-gateway-hpa -n $(NAMESPACE)
	@echo ""
	@echo "=== Route ==="
	oc get route ai-gateway -n $(NAMESPACE)

logs:
	oc logs -l app=ai-gateway -n $(NAMESPACE) --follow | jq .

clean:
	podman compose down -v --remove-orphans

smoke:
	GATEWAY_URL=http://localhost:8080 pytest tests/test_smoke.py -v -m smoke

smoke-ocp:
	@GATEWAY=$$(oc get route ai-gateway -n $(NAMESPACE) -o jsonpath='{.spec.host}') && \
	GATEWAY_URL=https://$$GATEWAY pytest tests/test_smoke.py -v -m smoke

deploy-full:
	oc apply -f openshift/secret.yaml
	oc apply -f openshift/configmap.yaml
	oc apply -f openshift/rbac.yaml
	oc apply -f openshift/quota.yaml
	oc apply -f openshift/pvc.yaml
	oc apply -f openshift/redis.yaml
	oc apply -f openshift/observability.yaml
	oc apply -f openshift/deployment.yaml
	oc apply -f openshift/service-route.yaml
	oc apply -f openshift/hpa.yaml
	oc apply -f openshift/pdb.yaml
	oc apply -f openshift/networkpolicy.yaml
	@echo "Full production stack deployed to namespace: $(NAMESPACE)"
