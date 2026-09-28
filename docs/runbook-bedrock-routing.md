# Runbook: Bedrock Routing via Gateway (Phase 3)

## Overview

As of Phase 3 (#748), the developer/ops/pm agent worker routes Bedrock API calls
through the platform gateway by default. A local `sigv4-proxy` subprocess in each
pod re-signs requests for the gateway's API Gateway endpoint. The gateway then
forwards to Bedrock, applying per-tenant budget, rate limits, audit, and cost
attribution.

## Architecture

```
Agent Worker Pod
+-------------------+      +----------------+      +-----------+      +---------+
| entrypoint.py     | ---> | sigv4-proxy    | ---> | API GW    | ---> | Gateway | ---> Bedrock
| (ANTHROPIC_       |      | (127.0.0.1:    |      | /agent/*  |      | pod     |
|  BEDROCK_BASE_URL |      |  9090)         |      | (IAM auth)|      |         |
|  = localhost:9090)|      +----------------+      +-----------+      +---------+
+-------------------+
```

## Configuration

| Env Var | Value (gateway mode) | Source |
|---------|---------------------|--------|
| `ADP_BEDROCK_VIA` | `gateway` | ConfigMap (Terraform-managed) |
| `SIGV4_PROXY_TARGET` | `https://<api-gw-id>.execute-api.<region>.amazonaws.com/<stage>/agent` | ConfigMap (from SSM param) |
| `SIGV4_PROXY_PORT` | `9090` | ConfigMap |
| `ANTHROPIC_BEDROCK_BASE_URL` | `http://127.0.0.1:9090` | Set by entrypoint.py |
| `CLAUDE_CODE_USE_BEDROCK` | `1` | ConfigMap + entrypoint.py |

## Rollback: Switch to Direct Bedrock

**Time to revert: ~30 seconds.**

### Option A: Quick revert via kubectl (no Terraform)

```bash
# Edit the configmap directly
kubectl edit configmap agent-gateway-config -n adp-gateway-agents
# Change: ADP_BEDROCK_VIA: "direct"

# Force new pods to pick up the change (KEDA spawns fresh pods from template)
kubectl delete jobs -n adp-gateway-agents -l app.kubernetes.io/name=agent-gateway-worker
```

### Option B: Durable revert via Terraform

In `modules/agent-factory/infra/gateway-main.tf`, change:
```hcl
ADP_BEDROCK_VIA = "direct"
```

Then apply:
```bash
cd modules/agent-factory/infra
terraform apply -var-file=terraform.tfvars -auto-approve
```

### Effect of rollback

- In-flight pods continue using whatever path they started with (gateway or direct)
- New pods spawned by KEDA use the direct path (pod IRSA → Bedrock)
- No restart of running pods needed — they finish their current task naturally
- Gateway audit/budget/rate-limit no longer applies to new agent calls

## Monitoring

### Alarms to watch during cutover

1. **Agent error rate** (`adp-dev-agent-gateway-worker` job failure rate)
   - Baseline: matches pre-cutover error rate (within +/-10%)
   - Alert: sustained spike for >5 minutes → rollback

2. **Gateway pod health** (`kubectl get pods -n adp-gateway`)
   - Both replicas must be Running
   - If gateway is down, all agent calls queue until it recovers (or timeout)

3. **API Gateway 5xx rate** (CloudWatch: `ApiGateway/5xxError`)
   - Baseline: 0
   - Any sustained 5xx → investigate gateway logs

4. **sigv4-proxy health check failures** (agent pod logs)
   - Look for: `sigv4-proxy failed to start; falling back to ADP_BEDROCK_VIA=direct`
   - Single occurrences are normal (race condition on pod startup)
   - Sustained failures → SIGV4_PROXY_TARGET may be wrong or proxy script missing

### Checking agent pod logs

```bash
# Find running agent jobs
kubectl get jobs -n adp-gateway-agents --sort-by=.metadata.creationTimestamp

# Check a specific pod's logs for proxy startup
kubectl logs -n adp-gateway-agents <pod-name> | grep -E '\[sigv4-proxy\]|ADP_BEDROCK_VIA'
```

### Expected latency

Gateway-routed calls add ~1.4-3.2s per Bedrock turn (measured in spike #765
retest #5). This is acceptable for async issue-driven agent workflows.

## Troubleshooting

### sigv4-proxy exits immediately

- Check `SIGV4_PROXY_TARGET` is set and valid
- Check the proxy script exists at `/app/dist/sigv4-proxy.js`
- Check Node.js is available in the image

### 403 from API Gateway

- The pod's IRSA role needs `execute-api:Invoke` on the `/agent/*` resource
- Check: `kubectl describe sa adp-agent -n adp-gateway-agents | grep role-arn`
- Verify the role has the `execute-api-invoke` policy attached

### Gateway returns 502

- Gateway pod may be unhealthy or restarting
- Check: `kubectl get pods -n adp-gateway` (both replicas Running?)
- Check gateway logs: `kubectl logs -n adp-gateway -l app=bedrockgateway --tail=50`

### Tenant not authorized

- `x-agent-orgid` header must match a registered tenant
- The sigv4-proxy injects this from the `TENANT_ID` env var
- TENANT_ID is set by entrypoint.py from the SQS envelope's `tenant_id` field
