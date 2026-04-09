# FSI AI Gateway — Production MVP

Full-stack AI Gateway for FSI customer support on OpenShift.
All 5 layers implemented: routing, cost/cache, security, observability, providers.

---

## Architecture

```
CX Platform / IT Portal / Internal Tools
           │
           ▼
    ┌──────────────────────────────────────────┐
    │           AI Gateway (FastAPI)            │
    │                                           │
    │  Layer 3: Security                        │
    │    PII masking (FSI patterns)             │
    │    Prompt injection detection             │
    │    DLP output scan                        │
    │                                           │
    │  Layer 2: Semantic Cache                  │
    │    Redis + cosine similarity              │
    │    sentence-transformers / hash fallback  │
    │                                           │
    │  Layer 1: Semantic Router                 │
    │    CX_SIMPLE / CX_COMPLEX / CX_ESCALATE  │
    │    Budget-triggered model downgrade       │
    │                                           │
    │  Layer 2: Budget Manager                  │
    │    Redis INCRBY (multi-replica safe)      │
    │    Burn-rate alerting                     │
    │    Hard-limit reject                      │
    │                                           │
    │  Layer 4: Observability                   │
    │    Prometheus metrics + JSON logs         │
    │    VADER sentiment scoring                │
    └──────────────────────────────────────────┘
           │
           ▼
    Anthropic Claude (Haiku / Sonnet)
```

---

## File structure

```
ai-gateway/
├── app/
│   ├── main.py          # FastAPI app — full request pipeline
│   ├── router.py        # Semantic routing (CX_SIMPLE / COMPLEX / ESCALATE / IT)
│   ├── cache.py         # Redis semantic cache + embedding backend
│   ├── security.py      # PII masking, injection detection, VADER sentiment, DLP
│   ├── budget.py        # Redis-backed token budgets + burn-rate alerting
│   ├── providers.py     # Anthropic client with retry / backoff
│   ├── observability.py # Prometheus counters + JSON structured logging
│   └── config.py        # Pydantic settings (all env-driven)
├── openshift/
│   ├── configmap.yaml
│   ├── secret.yaml
│   ├── deployment.yaml
│   ├── service-route.yaml
│   ├── redis.yaml
│   ├── observability.yaml   # Prometheus + Grafana
│   ├── hpa.yaml             # HorizontalPodAutoscaler
│   ├── pdb.yaml             # PodDisruptionBudget
│   ├── networkpolicy.yaml   # FSI-grade network isolation
│   └── prometheus-local.yml # For docker-compose only
├── tests/
│   ├── conftest.py          # Shared fixtures
│   └── test_gateway.py      # Unit + integration tests
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── pytest.ini
└── requirements.txt
```

---

## Local development

```bash
cp .env.example .env
# Edit .env — set ANTHROPIC_API_KEY

podman compose up --build
```

Services:
- Gateway → http://localhost:8080
- Prometheus → http://localhost:9090
- Grafana → http://localhost:3000  (admin / admin)

Test a request:
```bash
curl -X POST http://localhost:8080/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "I was charged twice last month", "department": "CX"}'
```

Run tests:
```bash
pip install -r requirements.txt
pytest
```

---

## OpenShift deployment

### 1. Build and push

```bash
podman build -t quay.io/your-org/ai-gateway:latest .
podman push quay.io/your-org/ai-gateway:latest
```

Update `openshift/deployment.yaml` with your image URL.

### 2. Create namespace and deploy

```bash
oc new-project fsi-ai-gateway

# Secrets first (never commit real values)
# Edit openshift/secret.yaml with your Anthropic API key, then:
oc apply -f openshift/secret.yaml

# Everything else
oc apply -f openshift/configmap.yaml
oc apply -f openshift/redis.yaml
oc apply -f openshift/observability.yaml
oc apply -f openshift/deployment.yaml
oc apply -f openshift/service-route.yaml
oc apply -f openshift/hpa.yaml
oc apply -f openshift/pdb.yaml
oc apply -f openshift/networkpolicy.yaml
```

### 3. Verify

```bash
oc get pods -n fsi-ai-gateway
oc rollout status deployment/ai-gateway -n fsi-ai-gateway

GATEWAY=$(oc get route ai-gateway -n fsi-ai-gateway -o jsonpath='{.spec.host}')
curl https://$GATEWAY/health
curl https://$GATEWAY/v1/budget
```

### 4. Connect Grafana

1. `oc get route grafana -n fsi-ai-gateway -o jsonpath='{.spec.host}'`
2. Login: admin / (password from `grafana-secret`)
3. Add data source: Prometheus → `http://prometheus:9090`
4. Key metrics to dashboard:

| Metric | What it tells you |
|--------|-------------------|
| `gateway_requests_total` | Volume by department |
| `gateway_tokens_used_total` | Spend by model |
| `gateway_cache_hits_total` | Cost avoided |
| `gateway_request_latency_seconds` | p50 / p95 / p99 |
| `gateway_sentiment_score` | Escalation signal |
| `gateway_injection_attempts_total` | Security events |
| `gateway_budget_fraction_used` | Budget headroom |

---

## Common operations

```bash
# New image rollout
oc set image deployment/ai-gateway \
  ai-gateway=quay.io/your-org/ai-gateway:v2 -n fsi-ai-gateway
oc rollout status deployment/ai-gateway -n fsi-ai-gateway

# Roll back
oc rollout undo deployment/ai-gateway -n fsi-ai-gateway

# Scale manually (HPA will take over automatically)
oc scale deployment/ai-gateway --replicas=4 -n fsi-ai-gateway

# Update a budget limit without redeploying
oc patch configmap ai-gateway-config -n fsi-ai-gateway \
  --type merge -p '{"data":{"BUDGET_CX_DAILY_TOKENS":"10000000"}}'
oc rollout restart deployment/ai-gateway -n fsi-ai-gateway

# Tail structured logs (pipe to jq for readability)
oc logs -l app=ai-gateway -n fsi-ai-gateway --follow | jq .

# Watch HPA
oc get hpa ai-gateway-hpa -n fsi-ai-gateway -w
```

---

## FSI security notes

**PII masking** covers: card numbers (13-16 digit), NI numbers, SSNs, IBANs,
sort codes, 8-digit account numbers, emails, UK/US phone numbers, dates,
UK postcodes, passport numbers, and full names. Applied to both inbound
requests and outbound model responses (DLP scan).

**Prompt injection**: 12 pattern families screened per request. Blocked
requests return HTTP 400 and increment `gateway_injection_attempts_total`.

**Network isolation**: NetworkPolicies enforce default-deny ingress with
explicit allow rules for router → gateway, Prometheus → gateway, gateway →
Redis, and Grafana → Prometheus.

**Audit trail**: Every request emits a structured JSON log with `trace_id`,
`department`, `route`, `model`, `tokens`, `latency_ms`, and masked PII field
types. Ship to your SIEM via a log forwarder (Fluentd / Vector).

**Budget failover**: CX spend >110% of daily budget triggers automatic model
downgrade for non-escalation routes. At 150% requests are rejected with
HTTP 429. Escalation routes (`CX_ESCALATE`) are never downgraded.

---

## Production hardening checklist

- [ ] Replace `stringData` in `secret.yaml` with HashiCorp Vault / OpenShift Secrets Manager
- [ ] Add `sentence-transformers` to requirements for fuzzy semantic caching
- [ ] Wire PersistentVolumeClaim for Prometheus and Grafana data
- [ ] Add mTLS between gateway pods (OpenShift Service Mesh / Istio)
- [ ] Enable OpenShift audit logging for the namespace
- [ ] Set resource quotas at the namespace level
- [ ] Add RBAC roles scoped to least-privilege for each service account
- [ ] Configure log forwarding to enterprise SIEM
- [ ] Add readiness gate to hold traffic during slow LLM cold-start
