# Resource Tagging & Observability Strategy

> Design doc for EPIC #872. Covers current-state audit, target-state design, and decomposition plan.

---

## 1. Audit: Current State

### 1.1 Tagging Today

#### Tag keys in use (from Terraform source)

| Module | `default_tags` set? | `Project` value | `Environment` | `ManagedBy` | `Owner` | `CostCenter` | Other |
|--------|---------------------|-----------------|---------------|-------------|---------|--------------|-------|
| `platform/infra/` | Yes (provider) | `adp` | `var.environment` | `terraform` | - | - | - |
| `modules/gateway/infra/` | Yes (provider) | `BedrockGateway` | `var.environment` | `terraform` | `platform-team` | `var.cost_center` | - |
| `modules/agent-factory/infra/` | Yes (provider) | `adp-agent-factory` | `var.environment` | `terraform` | - | - | - |
| `modules/agent-factory/webhook-ingress/infra/` | Yes (provider) | `adp-webhook-ingress` | `var.environment` | `terraform` | - | - | - |
| `modules/agent-context/terraform/` | Yes (provider) | `agent-context-platform` | `var.environment` | `terraform` | - | - | Passes `var.tags` (merge) |
| `modules/domain-apps/cyber/infra/` | Yes (provider) | `adp` | `var.environment` | `terraform` | - | - | - |
| `modules/gateway/cloudwatch-agent/` | No (provider) | `var.project_name` (per-resource) | - | - | - | - | - |

#### Inconsistencies found

1. **`Project` tag is fragmented** — 5 distinct values (`adp`, `BedrockGateway`, `adp-agent-factory`, `adp-webhook-ingress`, `agent-context-platform`). Makes Cost Explorer grouping unreliable.
2. **`Owner` tag exists only in gateway** — every other module is unowned from a tagging perspective.
3. **`CostCenter` exists only in gateway** — cost attribution by module is impossible for agent-factory, agent-context, or platform.
4. **No `Module` tag** — the `Project` tag is being overloaded to serve as both project identifier and module identifier. There should be a `Project=adp` on everything, with a separate `Module` tag distinguishing resources.
5. **No `Tenant` tag** — critical gap for multi-tenant cost attribution (#869, #745).
6. **No `EPIC` tag** — prevents correlating cost with feature work.
7. **`common_tags` propagation is partial** — platform passes `common_tags` to all submodules, but agent-factory and webhook-ingress only apply `default_tags` at provider level with no `common_tags` local for resource-level merge.
8. **CloudWatch Agent Lambda resources have minimal tags** — only `Name` and `Project`, no `Environment` or `ManagedBy`.
9. **Resources created outside Terraform** (EKS Ingress ALB, KEDA-scaled pods) have zero tags.

#### Resources missing tags entirely (from Terraform source)

- `aws_sqs_queue` in agent-factory SQS module: has `tags = var.tags` but the variable defaults to `{}` — tags propagate only if the caller passes them.
- `aws_s3_bucket.public_cfn` in agent-factory: has a resource-level `tags` but no `Environment` or `ManagedBy`.
- `aws_security_group_rule` resources (gateway, platform): cannot be tagged (AWS limitation).
- `aws_eks_access_entry` / `aws_eks_access_policy_association`: minimal `Name` tag only.
- `aws_ssm_parameter` resources in gateway: have `tags = local.common_tags` (good).
- `aws_dynamodb_table.identity_index`: has extra merge tags (good pattern to follow).

### 1.2 Observability Today

#### CloudWatch Dashboards

| Dashboard | Module | What it shows |
|-----------|--------|---------------|
| `bedrockgw-dev-latency` | Gateway | CloudFront origin latency, ALB response time, pod-level Bedrock timings, X-Ray trace count, pod CPU/memory/network |

**Gaps**: No dashboard for agent-factory (SQS depth, job completion, runner utilization), no dashboard for webhook-ingress (Lambda invocations, error rates), no dashboard for agent-context (Neptune, OpenSearch, ingestion queue depth).

#### CloudWatch Alarms

| Alarm | Module | Metric | Threshold | Action |
|-------|--------|--------|-----------|--------|
| `bedrockgw-dev-redis-cpu-utilization` | Gateway/Redis | CPU | >80% 2 periods | SNS (if configured) |
| `bedrockgw-dev-redis-memory-utilization` | Gateway/Redis | Memory | >90% 2 periods | SNS (if configured) |
| `adp-dev-agent-gateway-dlq-alarm` | Agent Factory/SQS | DLQ messages visible | >0 | None (no action configured) |
| `adp-dev-webhook-rate-limit-high` | Webhook Ingress | Custom `RateLimited` metric | >10/min | None (no action configured) |

**Gaps**:
- **No alarm actions configured** on 2 of 4 alarms (DLQ, rate-limit) — they fire but nobody is notified.
- **No gateway 5xx alarm** — the most critical signal for end-user impact.
- **No RDS alarms** — CPU, connections, storage, replication lag all unmonitored.
- **No Lambda error alarms** — budget Lambda, auth broker Lambda, authorizer Lambda could all fail silently.
- **No SQS age-of-oldest-message alarm** — tasks could sit unprocessed without detection.
- **No EKS node/pod health alarms** — pod restarts, OOMKill events, node not-ready.
- **No CloudFront error rate alarm**.

#### Log Groups (known)

| Log Group | Source | Retention | Subscribed? |
|-----------|--------|-----------|-------------|
| `/aws/containerinsights/adp-dev-eks-cluster/application` | EKS pods (Fluent Bit) | Default | Dashboard queries it |
| `/aws/lambda/bedrockgw-dev-budget-*` | Budget Lambdas | Varies | No |
| `/aws/lambda/bedrockgw-dev-github-auth-broker` | Auth broker | Varies | No |
| `/aws/lambda/bedrockgw-dev-lambda-authorizer` | API GW authorizer | Varies | No |
| `/aws/apigateway/bedrockgw-dev-*` | API Gateway access logs | Varies | No |
| `/aws/eks/adp-dev-eks-cluster` | EKS control plane | Default | No |
| Webhook ingress Lambda logs | Webhook Lambdas | Varies | No |

**Gaps**: No log group for agent-context services. CloudWatch Agent Lambda (log→issue) exists but is not subscribed to most log groups.

#### Tracing

- **X-Ray**: IAM permissions provisioned for gateway pods (`enable_xray_tracing` flag). Trace count appears on dashboard. But no evidence of X-Ray SDK integration in application code — likely a placeholder.
- **No distributed tracing** through the full request path (CloudFront → ALB → pod → Bedrock, or webhook → Lambda → SQS → runner).

#### Security Scan Artifacts

- S3 bucket `adp-dev-security-scans-*` stores SARIF results from CodeBuild.
- No CloudWatch integration — scan failures don't trigger alarms or issues.
- The CloudWatch Agent Lambda (`modules/gateway/cloudwatch-agent/`) can create GitHub issues from log errors, but it's not connected to the security scan pipeline.

### 1.3 Summary of Gaps

| Gap | Impact | Priority |
|-----|--------|----------|
| Inconsistent `Project` tag values | Can't aggregate cost by project in Cost Explorer | High |
| Missing `Owner` tag on 4/5 modules | No accountability for resources | High |
| Missing `Module` tag everywhere | Can't slice cost by module | High |
| Missing `Tenant` tag | Blocks multi-tenant cost attribution | High |
| No gateway 5xx alarm | User-facing outages go undetected | Critical |
| Alarm actions not wired (2/4 alarms) | Alarms fire into void | High |
| No agent-factory dashboard | Zero visibility into agent pipeline health | Medium |
| No RDS monitoring alarms | DB issues detected only by user reports | High |
| No Lambda error monitoring | Silent Lambda failures | Medium |
| No SLOs defined | No objective measure of platform health | Medium |
| No unified alert routing | Each alarm is an island | Medium |

---

## 2. Tagging Strategy

### 2.1 Required Tags (must be on every taggable resource)

| Tag Key | Values | Purpose |
|---------|--------|---------|
| `Project` | `adp` (always) | Top-level cost aggregation; single value across all modules |
| `Environment` | `dev`, `staging`, `prod` | Environment isolation |
| `Module` | `platform`, `gateway`, `agent-factory`, `webhook-ingress`, `agent-context`, `domain-apps/cyber` | Cost attribution by module |
| `ManagedBy` | `terraform`, `helm`, `kubectl`, `manual` | Identifies drift-prone resources |
| `Owner` | `platform-team`, `agent-team`, `gateway-team` | Accountability and escalation routing |

### 2.2 Recommended Tags (set when applicable)

| Tag Key | Values | Purpose |
|---------|--------|---------|
| `CostCenter` | Org-defined string (e.g., `engineering`, `platform-ops`) | Finance allocation |
| `Tenant` | Tenant slug or `shared` | Multi-tenant cost slicing |
| `Component` | Free-form (e.g., `rds`, `redis`, `sqs`, `lambda`, `eks-addon`) | Sub-module granularity |
| `EPIC` | Issue number (e.g., `872`) | Feature cost attribution (short-lived, applied during feature development) |

### 2.3 Naming Conventions

- **Tag keys**: PascalCase (e.g., `CostCenter`, not `cost_center` or `cost-center`).
- **Tag values**: lowercase-kebab-case for identifiers (e.g., `platform-team`, `agent-factory`). Exception: `Environment` uses the exact environment name variable (e.g., `dev`).
- **Maximum lengths**: keys ≤ 128 chars, values ≤ 256 chars (AWS limits).
- **No PII in tags** — never put emails, user IDs, or secrets in tag values.

### 2.4 Implementation Pattern

All modules should follow this pattern:

```hcl
# In root main.tf of each module
locals {
  common_tags = {
    Project     = "adp"
    Environment = var.environment
    Module      = "gateway"           # <-- module-specific
    ManagedBy   = "terraform"
    Owner       = "platform-team"     # <-- module-specific
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = local.common_tags
  }
}
```

- `default_tags` on the provider handles 80% of resources automatically.
- Resources needing extra tags (e.g., `Component`, `Tenant`) use `tags = merge(local.common_tags, { Component = "rds" })`.
- Submodules accept a `common_tags` variable and merge it: `tags = merge(var.common_tags, { Name = "..." })`.

### 2.5 Enforcement Strategy

**Phase 1 (report-only)**: CI lint check in PR workflows.
- A script (`platform/scripts/lint-tags.sh`) runs `terraform plan -out=plan.tfplan && terraform show -json plan.tfplan | jq` to extract planned resources and verify required tags are present.
- Fails the PR check with a clear message listing resources missing required tags.
- Does NOT block deployment initially — just reports.

**Phase 2 (enforcement)**: After backfill is complete:
- CI lint becomes a blocking check.
- Optional: AWS Config rule `required-tags` reports non-compliant resources (dashboard visibility).
- We do NOT recommend SCPs for tag enforcement — too risky for operational agility and third-party integrations.

### 2.6 Backfill Plan

1. Update each module's `default_tags` to the unified schema (incremental PRs).
2. For already-deployed resources whose tags differ from Terraform, the next `terraform apply` will update tags in-place (non-destructive for most resources).
3. Resources created outside Terraform (EKS Ingress ALB) need manual tag application via AWS CLI or a one-shot script.
4. Priority order: platform → gateway → agent-factory → webhook-ingress → agent-context → domain-apps.

---

## 3. Observability Strategy

### 3.1 Signal Hierarchy

```
Log  →  Metric  →  Alarm  →  Action
 │         │          │         │
 │         │          │         ├─ Page on-call (PagerDuty/Slack alert)
 │         │          │         ├─ Create GitHub issue (CloudWatch Agent Lambda)
 │         │          │         └─ Post to #platform-alerts Slack channel
 │         │          │
 │         │          └─ Fires when metric crosses threshold
 │         │
 │         └─ Derived from logs (Metric Filter) or native AWS metrics
 │
 └─ Raw application/infrastructure logs in CloudWatch Logs
```

**Principle**: Not everything needs an alarm. The hierarchy is:
- **Logs**: Everything gets logged. Searchable via Logs Insights.
- **Metrics**: Key health indicators get surfaced as metrics (native or custom).
- **Alarms**: Only actionable conditions get alarms — conditions where someone must act.
- **Pages**: Only customer-impacting or data-loss-risk conditions page on-call.

### 3.2 Signals Per Surface

| Surface | Metrics to Monitor | Dashboard | Alarm (threshold) | Action |
|---------|-------------------|-----------|-------------------|--------|
| **Gateway pods** | 5xx rate, p95 latency, pod restarts, OOMKills | `adp-gateway` | 5xx > 5/min (5m), p95 > 30s (5m), restarts > 3 (10m) | Page |
| **RDS** | CPU, connections, free storage, read/write latency | `adp-gateway` | CPU > 80% (10m), free storage < 5GB, connections > 80% max | Page (storage), Slack (CPU) |
| **Redis** | CPU, memory, evictions, connections | `adp-gateway` | CPU > 80% (existing), memory > 90% (existing), evictions > 0 | Slack |
| **SQS (agent tasks)** | Age of oldest message, DLQ depth, messages in flight | `adp-agent-factory` | DLQ > 0 (existing), age > 15min | Slack + issue |
| **SQS (webhook)** | Same pattern | `adp-agent-factory` | Same | Slack + issue |
| **Lambda (authorizer)** | Errors, duration p99, throttles | `adp-gateway` | Errors > 5/min (5m) | Slack |
| **Lambda (auth broker)** | Errors, duration | `adp-gateway` | Errors > 3/min (5m) | Slack |
| **Lambda (budget)** | Errors, invocations | `adp-gateway` | Errors > 0 (consecutive 2) | Issue |
| **Lambda (webhook)** | Errors, duration, concurrent executions | `adp-agent-factory` | Errors > 10/min (5m) | Slack |
| **EKS cluster** | Node count, pod pending, API server latency | `adp-platform` | Nodes < expected (10m), pods pending > 5 (10m) | Page |
| **CloudFront** | 5xx rate, origin latency p99 | `adp-gateway` (existing) | 5xx > 1% (5m) | Page |
| **API Gateway** | 5xx, 4xx, latency, throttles | `adp-gateway` | 5xx > 10/min (5m), throttles > 50/min | Slack |
| **Agent runner pods** | Job completion rate, duration p95, failures | `adp-agent-factory` | Failure rate > 20% (1h) | Issue |
| **Security scans** | SARIF findings HIGH+CRITICAL count | `adp-security` | New HIGH/CRITICAL > 0 | Issue |
| **Neptune** (agent-context) | CPU, queries/sec, storage | `adp-agent-context` | CPU > 80% (10m) | Slack |
| **OpenSearch** (agent-context) | Indexing rate, search latency, storage | `adp-agent-context` | Search p99 > 5s (5m) | Slack |

### 3.3 Dashboards (target state)

| Dashboard Name | Scope | Key Widgets |
|----------------|-------|-------------|
| `adp-dev-gateway` | Gateway end-to-end | Existing latency dashboard + RDS/Redis health + Lambda errors |
| `adp-dev-agent-factory` | Agent pipeline | SQS depths, job completion rate, runner utilization, KEDA scaling |
| `adp-dev-agent-context` | Intelligence platform | Neptune queries, OpenSearch indexing, SQS ingestion depth |
| `adp-dev-platform` | Shared infra | EKS node health, API server latency, VPC flow rejects |
| `adp-dev-security` | Security posture | Scan findings trend, alarm history, drift detection |
| `adp-dev-cost` | Cost attribution | Per-module cost (requires tagging), per-tenant Bedrock spend |

### 3.4 Alert Routing

| Severity | Condition | Routing |
|----------|-----------|---------|
| **P1 (page)** | Customer-facing service down, data loss risk | PagerDuty / on-call phone |
| **P2 (urgent)** | Degraded performance, component failure not yet customer-visible | Slack `#platform-alerts` + auto-created GitHub issue |
| **P3 (awareness)** | Capacity warning, cost anomaly, non-critical Lambda errors | Slack `#platform-alerts` only |
| **P4 (info)** | Security scan findings, drift detected | GitHub issue only |

**Implementation**: SNS topic per severity level → subscriptions (Slack webhook Lambda, PagerDuty integration, CloudWatch Agent Lambda for issue creation).

### 3.5 SLOs (initial set)

| SLO | Target | Measurement | Alert on breach |
|-----|--------|-------------|-----------------|
| Gateway availability (5xx rate) | < 0.1% of requests | CloudFront 5xx / total requests | P1 if > 1% for 5min |
| Gateway latency (p95) | < 30s (Bedrock-bound) | ALB TargetResponseTime p95 | P2 if > 60s for 5min |
| Agent task completion rate | > 95% of tasks succeed within 10min | SQS messages processed vs DLQ | P2 if < 90% in 1h |
| Per-tenant Bedrock latency (p50) | < 5s | Custom metric from gateway logs | P3 (dashboard only initially) |
| Webhook-to-agent delivery latency | < 60s from webhook receipt to runner start | Custom metric (timestamp diff) | P3 |

### 3.6 Cost-Attribution Queries (enabled by tagging)

Once tags are deployed, these Cost Explorer queries become possible:

- **Cost per module**: Group by tag `Module` → shows gateway vs. agent-factory vs. platform split.
- **Cost per environment**: Group by tag `Environment` → shows dev/staging/prod burn.
- **Cost per tenant**: Group by tag `Tenant` → shows per-customer Bedrock spend (requires #869 pool tagging).
- **Cost per EPIC**: Group by tag `EPIC` → shows R&D cost of a feature (useful for sprint cost tracking).
- **Bedrock cost per model per tenant**: Combine Bedrock usage reports with gateway chat-log cost records (existing budget Lambda).

---

## 4. Incremental Rollout Plan

Sub-issues below are sized for ≤ 1 day of implementation work each. Ordered by dependency and impact.

| # | Title | Section | Dependencies |
|---|-------|---------|--------------|
| 1 | Unify `default_tags` in `platform/infra/` — add `Module` and `Owner` | 2.4, 2.6 | None |
| 2 | Unify `default_tags` in `modules/gateway/infra/` — normalize `Project` to `adp`, add `Module` | 2.4, 2.6 | None |
| 3 | Unify `default_tags` in `modules/agent-factory/infra/` + `webhook-ingress/infra/` | 2.4, 2.6 | None |
| 4 | Unify `default_tags` in `modules/agent-context/terraform/` | 2.4, 2.6 | None |
| 5 | Add CI tag linter (`platform/scripts/lint-tags.sh`) — report-only | 2.5 | 1-4 |
| 6 | Add CloudWatch alarm: gateway pod 5xx rate > 5/min | 3.2 | None |
| 7 | Add CloudWatch alarm: RDS CPU > 80%, free storage < 5GB, connections > 80% | 3.2 | None |
| 8 | Wire existing alarms to SNS + Slack (create SNS topics, configure alarm actions) | 3.4 | None |
| 9 | Add CloudWatch dashboard: agent-factory (SQS depth, job completion, runner utilization) | 3.3 | None |
| 10 | Add CloudWatch alarm: Lambda errors for authorizer + auth-broker + budget | 3.2 | None |
| 11 | Add CloudWatch alarm: SQS age-of-oldest-message > 15min for task queues | 3.2 | None |
| 12 | Add CloudWatch dashboard: per-tenant Bedrock latency + cost (Logs Insights) | 3.3, 3.5 | Tagging (1-4) |
| 13 | Subscribe webhook-ingress Lambda logs to CloudWatch Agent (log→issue) | 1.2 | None |
| 14 | Backfill tags on already-deployed EKS Ingress ALB + out-of-Terraform resources | 2.6 | 1-4 |

---

## 5. Decision Log

| Decision | Rationale | Alternatives considered |
|----------|-----------|------------------------|
| Stay on CloudWatch (no Datadog/New Relic) | Already in use, cost-effective for current scale, no vendor procurement needed | Datadog (better UX but ~$15/host/month + log volume), Grafana Cloud |
| No SCP enforcement for tags | Too risky — could break Terraform applies during transition. CI lint is sufficient. | SCP deny on untagged CreateResource |
| `Project` always = `adp` | Single top-level group. Module-level slicing via `Module` tag. | Keep fragmented Project values (rejected: breaks Cost Explorer aggregation) |
| PascalCase for tag keys | AWS convention, matches existing tags, compatible with Cost Explorer filters | snake_case (rejected: inconsistent with AWS service-generated tags) |
| SNS per severity for routing | Simple, native, supports multiple subscribers (Slack, PagerDuty, Lambda) | EventBridge (more flexible but over-engineered for alarm routing) |
