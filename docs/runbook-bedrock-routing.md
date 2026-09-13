# Runbook: Bedrock Routing via Gateway (Phase 3)

## Saved account mappings are always active

Verified routing rules apply automatically to gateway Bedrock requests. There is
no separate platform or organization activation step. The old
`BG_BEDROCK_ROUTING_ENFORCE` environment variable, SSM
`/adp/<env>/gateway/bedrock-routing-enforce` parameter, and organization setting
`bedrock_routing_enforce` are retired and ignored, including old `false` values.
Existing mappings become active when the updated gateway is deployed; no database
backfill is required.

Configure shared destinations as a platform admin under **Budgets → Bedrock
account routing**, register and verify the destination, then add an organization,
team, or person rule. Personal selections remain under **Settings → Credentials →
Bedrock model calls**. Rules take effect within the existing routing-cache window
(about one minute). Person rules take priority, followed by the authenticated
primary team, organization, and platform account.

An unmapped principal uses the platform account. Once a usable destination is
selected, an invocation failure is returned to the caller rather than retried
against the platform account. To change the payer, change or remove the routing
rule in the UI; an old rollout flag cannot override it. Budget attribution and
rate limits continue to apply through ADP.

## Use an AWS connection owned by someone else

The platform admin does not need administrator access to the team's AWS account.
An AWS account owner supplies the connection; the platform admin decides which
organization and team can use it for Bedrock.

1. **AWS account owner:** Connect and verify the account in **Settings →
   Credentials → Connect AWS Account**. If the connection already exists, reuse it.
   The role must trust ADP and permit shared Bedrock invocation. Older connections
   can be read-only or pinned to one user; the AWS account administrator must update
   those roles before they can serve a team. The
   [routing role template](../modules/gateway/src/auth/cfn_templates/aws_role_v2.yaml)
   documents the required trust and invocation permissions.
2. **Platform admin:** Open **Budgets → Bedrock account routing → Use existing AWS
   connection**. Select the connection (shown with its account, owner, and source
   organization), select the target organization, and choose **Verify & link**.
   The target may differ from the connection owner's organization. ADP uses its
   existing credentials to test the role; it does not require the platform admin's
   AWS credentials. A rejected test leaves the attempted link unsaved and explains
   what the AWS account administrator needs to fix.
3. **Platform admin:** Choose **Add routing rule**, select **Team**, choose the
   organization and team, and select the linked destination. Saving checks the role
   again. An organization rule can provide the default for all of its teams.
4. Check **Effective mapping** for a member. Person rules take priority over the
   authenticated primary team's rule; the organization rule comes next.

Linking grants **Bedrock use only**. It does not transfer the original connection,
copy its secret, or expose it to the organization's general agent tools. Each
connection/organization pair has one link; repeated verification reuses that link.
Verification tests shared role assumption and Bedrock invocation authorization
without generating model tokens. Model availability still depends on the target
AWS account and selected region.

To remove a link, remove its routing rules first, then select **Unlink**. The
original connection and AWS role remain. If the owner deletes the source connection
or revokes AWS permissions, routed requests fail; ADP does not retry a selected
account's failed invocation against the platform account. The admin can remove the
rules and link or reconfigure the destination.

Deployment requires migration `049_bedrock_connection_grants` before the updated
backend. Downgrade refuses while links remain so an older backend cannot silently
lose these explicit grants.

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

### Valid `ADP_BEDROCK_VIA` values

| Value | Behavior |
|-------|----------|
| `gateway` (default) | Bedrock via the local sigv4-proxy → API GW → gateway. Metered, budgeted, attributed. |
| `direct` | Bedrock directly on pod IRSA. **Kill switch** — bypasses gateway budget/audit/metering. |
| `platform` | Legacy alias for `direct`. |
| `user` | **RETIRED (#4747).** Setting it is now a startup error. |

**Why `user` was retired.** It served Bedrock with the customer's own assumed
credentials, so the spend hit their account but no `usage_logs` row was written —
platform metering was blind to it. Per-principal Bedrock account routing (#4692)
replaces it: create a mapping (Settings → Credentials, or the admin Bedrock
routing surface) and leave `ADP_BEDROCK_VIA=gateway`. The calls reach the same
customer account, but metered.

Setting `=user` raises at startup rather than falling back. That is deliberate:
a silent fallback would run the pod on platform-billed IRSA, switching the payer
without telling anyone.

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
