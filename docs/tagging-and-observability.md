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

---

## 6. Per-Module Reference

> **Operating model**: The platform's day-to-day operator is an autonomous agent (#773 pattern). Humans inspect from ONE surface. Every signal below is both agent-actionable and human-readable from the operations centre.

### 6.0 Platform-Wide (Cross-Cutting)

| Concern | Today | Target | Owner | Tracking issue |
|---|---|---|---|---|
| Log shipping (pod → CW) | CloudWatch Observability addon (Fluent Bit) ships stdout | No change needed — working | platform-team | — |
| API GW access log destination | Log group exists but empty (misconfigured stage) | Fix stage config, verify logs flow | platform-team | #903 |
| Account-level CloudWatch role | Not configured | IAM role for CW cross-account (needed for prod) | platform-team | `NO ISSUE YET` |
| Log retention defaults | Mixed (some default/never-expire, some 30d) | 30d pods, 14d Lambda, 90d S3 — enforced in TF | platform-team | `NO ISSUE YET` |
| Custom metrics emission | EMF via stdout (gateway); absent elsewhere | EMF for all modules; PutMetricData prohibited | platform-team | `NO ISSUE YET` |
| Trace propagation | request_id correlation only; X-Ray SDK exists but disabled | request_id correlation (traces deferred to next quarter) | platform-team | — |
| Alert routing P1 (page) | Not wired | Agent: auto-file P1 issue + trigger ops-agent. Human: ops-centre shows red banner | platform-team | #901 |
| Alert routing P2 (urgent) | Not wired | Agent: auto-file issue + start remediation sub-agent. Human: ops-centre amber row | platform-team | #901 |
| Alert routing P3 (awareness) | Not wired | Agent: log to ops-centre, no action. Human: visible in ops-centre feed | platform-team | #881 |
| Alert routing P4 (info) | Not wired | Agent: file issue only. Human: issue list in ops-centre | platform-team | #901 |
| Tag enforcement | No enforcement | CI lint (report-only Phase 1 → blocking Phase 2) | platform-team | #894 |
| Cost-attribution dashboard | None | Per-module + per-tenant cost via tag grouping | platform-team | #885 |
| Operations centre | Does not exist | Single CloudWatch dashboard (see §6.0.A) | platform-team | `NO ISSUE YET` |

**Alert routing verdict**: Keep §3.4 severity tiers (P1-P4). Extend each tier with an agent action column. Agent receives alarm via SNS → Lambda (#901) → dispatches the appropriate sub-agent or files an issue. Humans do NOT need to be in the loop for P2-P4; they inspect results on the operations centre. P1 still pages a human as backstop.

### 6.0.A The Operations Centre (Single Inspection Surface)

**Choice**: CloudWatch dashboard at a fixed URL.

| Property | Value |
|---|---|
| Name | `adp-<env>-operations-centre` |
| URL | `https://<region>.console.aws.amazon.com/cloudwatch/home#dashboards/dashboard/adp-<env>-operations-centre` |
| Update mechanism | Terraform-managed widget definitions + ops-agent writes annotation widgets via `PutDashboard` API |
| Content schema | See below |

**Dashboard content schema** (one row per widget group):

| Widget group | Source | Agent writes? |
|---|---|---|
| Platform health (EKS nodes, pods, API server) | CW metrics — `ContainerInsights` | No (live metrics) |
| Gateway health (5xx rate, p95, pod count) | CW metrics — `AWS/ApplicationELB`, `BedrockGateway` | No (live metrics) |
| Agent pipeline (SQS depth, DLQ, job completion) | CW metrics — `AWS/SQS`, `ADP/AgentFactory` | No (live metrics) |
| Active incidents | Annotation/text widget | Yes — ops-agent updates on alarm state change |
| Recent agent actions | Annotation/text widget (last 10 actions) | Yes — ops-agent appends after each remediation |
| Open issues (P1/P2) | Text widget with GitHub issue links | Yes — ops-agent refreshes on schedule |
| Module status summary | Text widget (one line per module: OK/DEGRADED/DOWN) | Yes — ops-agent computes from alarm states |

**What is NOT the operations centre** (do not proliferate these as inspection surfaces):
- Slack channels (`#platform-alerts`) — routing/notification only, not inspection
- PagerDuty — escalation mechanism only
- Individual per-module CW dashboards — detail drilldown, not the entry point
- GitHub issue threads — execution record, not status summary
- CloudWatch Alarm console — raw signal list, not curated state

**Tracking**: `NO ISSUE YET` — implementing this dashboard is a separate sub-issue.

### 6.1 platform

**Owner**: platform-team | **Module tag**: `Module=platform` | **What it does**: Shared VPC, EKS, ECR, base IAM.

| Surface | Logs | Metrics | Alarms | Auto-remediation hook | Dashboard panel | SLO |
|---|---|---|---|---|---|---|
| EKS cluster | `/aws/eks/adp-dev-eks-cluster` (30d) | `ContainerInsights` — node count, pod pending, API server latency | `adp-dev-eks-nodes-low` P1 (`NO ISSUE YET`), `adp-dev-eks-pods-pending` P2 (`NO ISSUE YET`) | P1: ops-agent runs node-health diagnostic. P2: ops-agent checks KEDA/HPA state | Platform health | EKS API 99.9% |
| EKS pods (all) | `/aws/containerinsights/.../application` (30d) | pod_cpu, pod_memory, restarts | `adp-dev-pod-restarts-high` P2 (`NO ISSUE YET`) | ops-agent: describe pod, check OOM, file issue | Platform health | — |
| VPC / networking | VPC flow logs (S3, 90d) | — | — | (human-only) | — | — |

### 6.2 gateway

**Owner**: gateway-team | **Module tag**: `Module=gateway` | **What it does**: Multi-tenant Bedrock proxy (FastAPI, RDS, Redis, Cognito, CloudFront, Lambdas).

| Surface | Logs | Metrics | Alarms | Auto-remediation hook | Dashboard panel | SLO |
|---|---|---|---|---|---|---|
| Gateway pods | `/aws/containerinsights/.../application` (30d) | `BedrockGateway` — RequestLatencyMs, ErrorCount, TokensIn/Out | `adp-dev-gateway-5xx` P1 (#895) | ops-agent: check pod logs, restart if OOM, scale if load | Gateway health | Availability <0.1% 5xx |
| RDS | `/aws/rds/...` (30d) | `AWS/RDS` — CPU, connections, FreeStorage | `adp-dev-rds-cpu` P2, `adp-dev-rds-storage` P1, `adp-dev-rds-connections` P2 (#880) | P1 storage: ops-agent files urgent issue + alerts human. P2: ops-agent checks slow queries | Gateway health | — |
| Redis | — | `AWS/ElastiCache` — CPU, memory, evictions | `bedrockgw-dev-redis-cpu` P3 (exists), `bedrockgw-dev-redis-memory` P2 (exists) | P2 memory: ops-agent checks key distribution | Gateway health | — |
| CloudFront | S3 access logs (90d) | `AWS/CloudFront` — 5xxErrorRate, OriginLatency | `adp-dev-cloudfront-5xx` P1 (#895) | ops-agent: check origin health, toggle maintenance page | Gateway health | Latency p95 <30s |
| Lambdas (authorizer, auth-broker, budget) | `/aws/lambda/bedrockgw-dev-*` (30d) | `AWS/Lambda` — Errors, Duration, Throttles | `adp-dev-lambda-*-errors` P3 (#883) | ops-agent: check recent errors in logs, file issue | Gateway health | — |
| API Gateway access logs | `/aws/apigateway/bedrockgw-dev-*` (30d) | `AWS/ApiGateway` — 5xx, 4xx, latency | — | — | Gateway health | — |
| Dashboard rename | `bedrockgw-dev-latency` → `adp-dev-gateway-latency` | — | — | — | — | — | Status: #900 |

### 6.3 agent-factory

**Owner**: agent-team | **Module tag**: `Module=agent-factory` | **What it does**: Autonomous code agents — ARC runners, KEDA ScaledJobs, SQS task queues.

| Surface | Logs | Metrics | Alarms | Auto-remediation hook | Dashboard panel | SLO |
|---|---|---|---|---|---|---|
| SQS task queues | — | `AWS/SQS` — ApproximateAgeOfOldestMessage, MessagesVisible | `adp-dev-sqs-age-oldest` P2 (#896) | ops-agent: check KEDA ScaledJob status, verify runner capacity | Agent pipeline | Completion >95% in 10min |
| DLQ | — | `AWS/SQS` — ApproximateNumberOfMessagesVisible (DLQ) | `adp-dev-agent-gateway-dlq-alarm` P2 (exists, action not wired — #881) | ops-agent: sample DLQ messages, file issue with payload | Agent pipeline | — |
| Runner pods | `/aws/containerinsights/.../application` (30d) | `ADP/AgentFactory` — job duration, failure count | `adp-dev-agent-failure-rate` P2 (#898) | ops-agent: check recent failures, correlate with runner logs | Agent pipeline | — |
| ARC controller | `/aws/containerinsights/.../application` (30d) | pod status | — | (human-only) — controller issues are rare | — | — |

### 6.4 webhook-ingress

**Owner**: agent-team | **Module tag**: `Module=webhook-ingress` | **What it does**: Webhook receiver — Lambda, API Gateway, DynamoDB, WAF, SQS dispatch.

| Surface | Logs | Metrics | Alarms | Auto-remediation hook | Dashboard panel | SLO |
|---|---|---|---|---|---|---|
| Webhook Lambda | `/aws/lambda/adp-dev-webhook-*` (14d) | `AWS/Lambda` — Errors, Duration; `ADP/WebhookIngress` — RateLimited | `adp-dev-webhook-lambda-errors` P3 (#897), `adp-dev-webhook-rate-limit-high` P4 (exists, unwired — #881) | P3: ops-agent checks error pattern, files issue | Agent pipeline | Delivery <60s |
| API GW access logs | `/aws/apigateway/adp-dev-webhook-*` (14d) | `AWS/ApiGateway` — 5xx, latency | — | — | Agent pipeline | — |
| Delivery latency | — | `ADP/WebhookIngress` — DeliveryLatencyMs (custom) | `adp-dev-webhook-delivery-latency` P3 (`NO ISSUE YET`) | ops-agent: check SQS consumer lag | Agent pipeline | — |
| Log subscription | — | — | — | — | — | — | Status: planned (#886) |

### 6.5 agent-context

**Owner**: agent-team | **Module tag**: `Module=agent-context` | **What it does**: Code Intelligence — semantic search, code graph, wikis, memory (MCP endpoint).

| Surface | Logs | Metrics | Alarms | Auto-remediation hook | Dashboard panel | SLO |
|---|---|---|---|---|---|---|
| Neptune | `/aws/neptune/adp-dev-*` (30d) | `AWS/Neptune` — CPUUtilization, GremlinQueries/sec | `adp-dev-neptune-cpu` P3 (`NO ISSUE YET`) | ops-agent: check slow queries, file issue | Agent pipeline | — |
| OpenSearch | `/aws/opensearch/adp-dev-*` (30d) | `AWS/ES` — SearchLatency, IndexingRate, FreeStorage | `adp-dev-opensearch-latency` P2 (`NO ISSUE YET`) | ops-agent: check index health, trigger reindex if corrupt | Agent pipeline | Search p99 <5s |
| Ingestion SQS | — | `AWS/SQS` — age, depth | `adp-dev-context-queue-age` P3 (`NO ISSUE YET`) | ops-agent: check consumer pods | Agent pipeline | — |

### 6.6 domain-apps/cyber

**Owner**: agent-team | **Module tag**: `Module=domain-apps/cyber` | **What it does**: Malware analysis — CAPE detonation, URL analysis, evidence storage.

| Surface | Logs | Metrics | Alarms | Auto-remediation hook | Dashboard panel | SLO |
|---|---|---|---|---|---|---|
| Analysis SQS | — | `AWS/SQS` — age, depth, DLQ visible | `adp-dev-cyber-dlq` P3, `adp-dev-cyber-queue-age` P3 (`NO ISSUE YET`) | ops-agent: check worker pod count, scale if needed | Agent pipeline | Completion >90% in 15min |
| Worker pods | `/aws/containerinsights/.../application` (30d) | pod count, CPU, restarts | — | (human-only) | — | — |
| Evidence bucket | — | `AWS/S3` — BucketSizeBytes | — | (human-only) | — | — |

## 7. Logs, Metrics, and Traces — How

### 7.1 Logs

**One-line definition**: Structured JSON records emitted by every compute unit, shipped to CloudWatch Logs for search and alerting.

**Producers** — where they originate

| Source | Mechanism | Destination | Format |
|--------|-----------|-------------|--------|
| Gateway pods | stdout (structured JSON via `src/shared/logging.py`) | `/aws/containerinsights/adp-dev-eks-cluster/application` | JSON line (`timestamp`, `level`, `module`, `request_id`, `org_id`) |
| Agent worker pods | stdout (entrypoint logs) | `/aws/containerinsights/adp-dev-eks-cluster/application` | JSON line (same cluster, filtered by pod label) |
| Webhook-ingress Lambdas | Lambda runtime → CW Logs | `/aws/lambda/adp-dev-webhook-*` | JSON line (Python `logging` + JSON formatter) |
| Gateway Lambdas (authorizer, auth-broker, budget) | Lambda runtime → CW Logs | `/aws/lambda/bedrockgw-dev-*` | JSON line |
| API Gateway access logs (gateway) | API GW stage config | `/aws/apigateway/bedrockgw-dev-*` | JSON (request context fields) |
| API Gateway access logs (webhook-ingress) | API GW stage config | `/aws/apigateway/adp-dev-webhook-*` | JSON |
| EKS control plane | EKS native | `/aws/eks/adp-dev-eks-cluster` | AWS-managed |
| CloudFront access logs | S3 delivery | S3 bucket (not CW Logs) | W3C extended log |

**Transport / collection**

| Component | Role | Configured in |
|-----------|------|---------------|
| CloudWatch Observability addon (Fluent Bit) | Tails container stdout, ships to CW Logs | `platform/infra/modules/eks/main.tf` (addon gated by `enable_container_insights`) |
| Lambda runtime | Auto-ships to log group matching function name | Implicit (AWS-managed) |
| API Gateway stage | Writes access logs to configured log group ARN | `modules/gateway/infra/modules/api-gateway/main.tf`, `modules/agent-factory/webhook-ingress/infra/api-gateway.tf` |

**Retention**

| Log group pattern | Retention | Rationale |
|-------------------|-----------|-----------|
| `/aws/containerinsights/…/application` | 30d | High volume; older logs rarely queried |
| `/aws/lambda/bedrockgw-dev-*` | 30d | Matches RDS log group retention |
| `/aws/lambda/adp-dev-webhook-*` | 14d | Lower-value debugging logs |
| `/aws/apigateway/*` | 14d (webhook-ingress), 30d (gateway) | Access logs; useful for incident replay |
| `/aws/eks/adp-dev-eks-cluster` | 30d | Control-plane audit trail |
| CloudFront S3 logs | S3 lifecycle 90d | Compliance / forensic use only |

Retention is set per-group in Terraform (`retention_in_days` on the `aws_cloudwatch_log_group` resource). No group should use "never expire" — always set an explicit value.

**Querying**

| Pattern | Tool | Example |
|---------|------|---------|
| Single request across pod + Lambda | Logs Insights (multi-group) | `filter request_id = "abc-123"` across container-insights + Lambda groups |
| Error spike diagnosis | Logs Insights | `filter level = "ERROR" \| stats count(*) by module \| sort count desc` |
| Per-tenant request history | Logs Insights | `filter org_id = "tenant-slug" \| fields @timestamp, module, message` |
| CloudFront origin errors | S3 Select or Athena | Query access-log S3 bucket for `sc-status >= 500` |

**What every implementer must do**

1. Emit structured JSON to stdout (pods) or use Python `logging` with `pythonjsonlogger` (Lambdas). Never write to files inside the container.
2. Include `request_id` in every log line — use `src/shared/logging.set_request_context()` for gateway code or pass as a field for new services.
3. Create an explicit `aws_cloudwatch_log_group` in Terraform with `retention_in_days` set (30d default, 14d for high-volume low-value). Never rely on auto-created groups (they default to never-expire).
4. For new Lambdas, add a `depends_on` from the Lambda resource to its log group so Terraform creates the group before first invocation.
5. If adding a new EKS workload, no extra config needed — the CloudWatch Observability addon auto-collects stdout from all pods in the cluster.

### 7.2 Metrics

**One-line definition**: Numeric time-series data points emitted to CloudWatch Metrics for dashboards, alarms, and SLO tracking.

**Producers** — where they originate

| Source | Mechanism | Namespace | Key metrics |
|--------|-----------|-----------|-------------|
| Gateway pods | CloudWatch EMF via stdout (`src/shared/metrics.py`) | `BedrockGateway` | RequestLatencyMs, TokensIn/Out, CostUSD, ErrorCount, PoolHealthy |
| AWS native (ALB, RDS, SQS, Lambda, CloudFront) | Auto-published by AWS | `AWS/<Service>` | Standard per-service metrics |
| Container Insights | CloudWatch Observability addon | `ContainerInsights` | pod_cpu_utilization, pod_memory_utilization, pod_network_rx/tx |
| Webhook-ingress Lambda | CloudWatch EMF via stdout | `ADP/WebhookIngress` | Custom: `RateLimited`, delivery latency |
| Metric filters (log-derived) | CW Logs metric filter | `ADP/Custom` | Derived counts from log patterns (e.g., 5xx count from access logs) |

**Transport / collection**

| Component | Role | Configured in |
|-----------|------|---------------|
| CloudWatch agent (in-cluster) | Scrapes EMF from stdout, publishes to CW Metrics | EKS addon (`platform/infra/modules/eks/`) |
| Lambda runtime | Auto-detects EMF JSON blocks in stdout, publishes to CW Metrics | Implicit (AWS-managed) |
| Metric filters | CW Logs evaluates filter pattern, increments metric on match | Terraform `aws_cloudwatch_metric_filter` |

**Dimensions (required on every custom metric)**

| Dimension | Source | Why |
|-----------|--------|-----|
| `Environment` | `BG_ENVIRONMENT` env var / Terraform `var.environment` | Slice dev vs prod |
| `Module` | Hardcoded per service (e.g., `gateway`, `agent-factory`) | Cost + ownership attribution |
| `Tenant` | `org_id` from request context (where applicable) | Per-tenant SLO measurement (§3.5) |

**Canonical emission method**: **CloudWatch EMF via stdout**. Do NOT call `PutMetricData` from application code (rate-limited, adds latency). Use metric filters only for signals that can't be emitted at the source (e.g., counting patterns in API Gateway access logs).

**Querying**

| Pattern | Tool | Example |
|---------|------|---------|
| Real-time dashboard | CloudWatch Metrics console / Grafana | Widget: `BedrockGateway` → `RequestLatencyMs` by `Tenant` |
| SLO breach detection | CloudWatch Alarm on metric | Alarm on p95 > threshold |
| Ad-hoc investigation | Metrics Insights | `SELECT AVG(RequestLatencyMs) FROM BedrockGateway GROUP BY Tenant WHERE Environment = 'dev'` |

**What every implementer must do**

1. Use `src/shared/metrics.py` (or replicate its EMF pattern) — emit metrics as JSON to stdout with the `_aws` EMF envelope. Never call `PutMetricData` directly.
2. Always include `Environment` and `Module` dimensions. Add `Tenant` if the metric is request-scoped.
3. Use the `BedrockGateway` namespace for gateway services; use `ADP/<ModuleName>` for other modules (e.g., `ADP/AgentFactory`).
4. For log-derived metrics (metric filters), define the `aws_cloudwatch_metric_filter` in the same Terraform module that owns the log group.
5. Register every new custom metric in the relevant CloudWatch dashboard Terraform (`modules/gateway/infra/modules/cloudwatch-dashboard/` or equivalent).

### 7.3 Traces

**One-line definition**: Request-scoped timing spans that show the full call path across service boundaries, rendered in AWS X-Ray.

**Current state**

| Aspect | Status |
|--------|--------|
| X-Ray IAM permissions | Provisioned but gated (`enable_xray_tracing = false` in `platform/infra/main.tf:122`) |
| SDK integration | EXISTS — `modules/gateway/src/shared/tracing.py` uses OpenTelemetry SDK with X-Ray ID generator + OTLP exporter. Gated by `OTEL_ENABLED` env var (currently `false`). |
| OTel Collector sidecar | Not deployed (no K8s manifest for collector pod) |
| End-to-end trace propagation | Not wired (no trace header forwarding through CloudFront → ALB → pod → Bedrock) |

**Target state (this quarter: request-id correlation only; full tracing deferred)**

Distributed tracing is **not in scope this quarter**. The SDK exists but enabling it requires deploying an OTel Collector sidecar and wiring trace-context headers end-to-end. Current priority is log-based correlation via `request_id`.

**Today-state: request-id correlation**

| Hop | How request_id propagates |
|-----|---------------------------|
| Client → CloudFront | Client sends `X-Request-Id` header (or CloudFront generates one) |
| CloudFront → ALB → Pod | Header forwarded unchanged |
| Pod (FastAPI) | Middleware extracts `X-Request-Id`, stores in contextvar, emits on every log line |
| Pod → Bedrock | `request_id` logged on outbound call (no trace header to Bedrock) |
| Webhook → Lambda → SQS → Runner | Webhook event ID serves as correlation key across the async boundary |

**Querying (today)**

| Pattern | Tool | Example |
|---------|------|---------|
| Trace a single request | Logs Insights | `filter request_id = "..." \| sort @timestamp` across pod + Lambda groups |
| Trace webhook → agent | Logs Insights | `filter event_id = "..." ` in webhook Lambda + agent worker logs |

**Future-state (post-quarter, tracked separately)**

When enabled, tracing will use:
- **SDK**: OpenTelemetry Python SDK with `AwsXRayIdGenerator` + `AwsXRayPropagator` (already coded in `src/shared/tracing.py`)
- **Collector**: OTel Collector sidecar (OTLP gRPC → X-Ray)
- **Propagation**: `X-Amzn-Trace-Id` header through CloudFront → ALB → pod
- **Visualization**: AWS X-Ray console + X-Ray groups for per-service filtering

**What every implementer must do**

1. Always propagate `request_id` — extract from `X-Request-Id` header in any new HTTP service, store in log context, and emit on every log line.
2. For async boundaries (SQS, EventBridge), include the correlation ID (`request_id` or `event_id`) as a message attribute so downstream consumers can log it.
3. Do NOT add OpenTelemetry instrumentation or enable `OTEL_ENABLED=true` without coordinating with platform team — the collector sidecar must be deployed first.
4. If writing a new Lambda, log the incoming `request_id` (from API Gateway context or SQS message attribute) in the first log line of every invocation.
5. When full tracing is enabled (future), use `src/shared/tracing.get_tracer(__name__)` to create spans — the no-op fallback ensures zero impact while tracing is disabled.
