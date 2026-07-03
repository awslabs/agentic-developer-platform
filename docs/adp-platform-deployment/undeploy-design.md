# Undeploy Re-Orchestration — Design Document

> **Status**: Design-only. No code, no teardown executed.  
> **Issue**: #2651  
> **Parent EPIC**: #2571  
> **Target inventory**: Account `261421447505` (deploy #2562)  
> **Author**: @agent-architect  
> **Date**: 2026-07-02

---

## 1. Phase Order + Dependency Rationale

The undeploy sequence reverses the deploy order, respecting cross-resource
dependencies that would cause Terraform-destroy hangs if violated.

### Phase Table

| # | Phase | What It Destroys | Blocking Dependency (must complete before next) |
|---|-------|------------------|-------------------------------------------------|
| 0 | **Account guard + confirmation** | — | Gate: typed account ID + `DESTROY` confirmation |
| 1 | **Agent Context** | Neptune, OpenSearch, SQS, DynamoDB state, S3 vectors, `agent_context` DB | Reads gateway RDS state → must destroy before gateway |
| 2 | **Webhook Ingress** | REST API GW, SQS FIFO, DynamoDB tables, KEDA ScaledJob, Lambda, secrets | ScaledJob pods in `adp-agents` namespace use EKS; must destroy before platform |
| 3 | **Agent Factory** | ARC runners, IRSA, DynamoDB beads-manifest, S3 beads-state, K8s namespaces | ARC Helm charts + KEDA ScaledJobs use EKS; must destroy before platform |
| 4 | **Gateway** | Ingress/ALB, K8s, RDS, Redis, Cognito, CloudFront, S3, Lambdas, API GW, SSM params | ALB ENIs live in platform VPC SGs; must clean before platform SG/VPC destroy |
| 5 | **Platform** | EKS cluster, VPC, subnets, NAT, security groups, ECR, IAM, KMS, CodeBuild | Foundation — nothing depends on it being destroyed |
| 6 | **Bootstrap (opt-in)** | Terraform state backend (S3 + DynamoDB lock table) | Separate, explicit, never automatic |

### Why This Order

1. **Agent Context → before Gateway**: Agent-context's terraform reads
   `dev/modules/gateway/terraform.tfstate` (for shared RDS endpoint when
   `rds_enabled=true`). Destroying gateway first would orphan that data source
   reference. Also, agent-context K8s resources use EKS (must be gone before
   platform).

2. **Webhook Ingress → before Agent Factory**: Webhook-ingress creates the KEDA
   ScaledJob that actually runs agent pods. Agent Factory creates the `arc-runners`
   namespace and ARC controller that KEDA depends on. Destroying agent-factory
   first removes KEDA's operator namespace while ScaledJobs still reference it.
   Additionally, both are independent of gateway state but both require EKS.

3. **Agent Factory → before Gateway**: Agent factory's S3 beads-state bucket and
   DynamoDB are independent of gateway, but the ARC runner's Helm release uses
   the `arc-systems` namespace (cleaned in platform). Ordering it here groups all
   agent-execution teardown before the data-plane (gateway).

4. **Gateway → before Platform**: The ALB is created by the AWS Load Balancer
   Controller (running on EKS). Deleting the Ingress resource triggers ALB
   deletion; the controller must be running for that finalizer to fire. If
   platform (EKS) is destroyed first, the ALB + its ENIs + target groups become
   orphans that block VPC/SG deletion. The `delete-ingress-and-wait.sh` helper
   enforces this: delete Ingress → wait for ALB removal → clean orphaned ENIs →
   then terraform destroy.

5. **Platform last**: VPC, security groups, and NAT Gateway cannot be deleted
   while ENIs from ALBs/pods/Lambda VPC attachments still reference them.
   Platform destroy must be the final infrastructure phase.

6. **Bootstrap separate**: Terraform state must survive all module destroys (so
   you can re-run a failed destroy). Only destroyed on explicit operator request
   after verification that all state files are empty.

### Pre-destroy Ordering Within Gateway Phase (Critical)

Gateway is the most complex phase. Internal sub-steps MUST execute in this order:

```
4a. delete-ingress-and-wait.sh     → removes ALB via K8s finalizer, cleans ENIs
4b. kubectl delete ns adp-gateway  → removes pods, services, configmaps
4c. empty-s3-buckets.sh            → empties frontend + chat-artifacts buckets
4d. force-delete-secrets.sh        → removes gateway secrets (not GitHub App ones)
4e. CloudFront disable + wait      → must disable before TF can delete distribution
4f. terraform destroy              → destroys RDS, Redis, Cognito, CloudFront, S3, Lambda
4g. SSM parameter cleanup          → removes discovery params (post-destroy)
```

---

## 2. Reuse Map

### Per-Phase Mapping to Existing Building Blocks

| Phase | Existing Workflow | Existing Helpers Called | Gap (what's missing today) |
|-------|-------------------|------------------------|----------------------------|
| Agent Context | `agent-context-infra-destroy.yml` | (none — direct kubectl + TF) | None — workflow is complete |
| Webhook Ingress | `webhook-ingress-destroy.yml` | `.github/actions/load-deploy-config` | No K8s namespace cleanup for `adp-agents` (KEDA ScaledJob pods). Terraform handles the KEDA resources but pods may linger if finalizers stall. Add `kubectl delete ns adp-agents --timeout=120s \|\| true` pre-step. |
| Agent Factory | `agent-factory-infra-destroy.yml` | `empty-s3-buckets.sh` | None — workflow handles namespaces + S3 + TF |
| Gateway | `gateway-infra-destroy.yml` | `delete-ingress-and-wait.sh`, `empty-s3-buckets.sh`, `force-delete-secrets.sh` | Protect-list gap (#2629): `adp/*/github-app/*` not in force-delete-secrets.sh |
| Platform | `platform-infra-destroy.yml` | (inline ENI cleanup) | None — workflow handles namespaces + ENIs + TF + CodeBuild |
| Bootstrap | `bootstrap-destroy.sh` (script, no workflow) | `empty-s3-buckets.sh` | None — script is complete and guarded |

### Disposition of #2627 Review Findings

| Finding | Issue | Disposition | Rationale |
|---------|-------|-------------|-----------|
| deploy-all.sh omits webhook-ingress | #2628 | **Absorbed structurally** | The new orchestrator includes webhook-ingress as Phase 2. deploy-all.sh is deprecated as the entry point (retained as legacy reference). |
| force-delete-secrets.sh protect-list incomplete | #2629 | **Standalone helper fix (required before orchestrator ships)** | Must add `adp/*/github-app/*` to the `is_protected()` function. This is a 2-line fix in the helper that all paths (workflow, script, manual) benefit from. Ship as a prerequisite PR. |
| Agent Factory cleanup gaps | #2630 | **Absorbed structurally** | The orchestrator's Phase 3 sequences the existing workflow which already handles namespace + S3 cleanup. If #2630 identifies specific missing cleanup (e.g., chat-artifacts bucket pattern), that's a workflow-internal fix (not orchestrator scope). |
| Gateway module cleanup gaps | #2631 | **Absorbed structurally** | The orchestrator's Phase 4 sequences the gateway workflow which has the most complete 7-step cleanup. Any sub-step gaps (e.g., chat-artifacts bucket not in deploy-all.sh but IS in the workflow) are resolved by preferring the workflow's logic over deploy-all.sh. |
| Webhook-ingress coverage in destroy path | #2632 | **Absorbed structurally** | The new orchestrator includes webhook-ingress as Phase 2. The workflow already exists and works (`webhook-ingress-destroy.yml`). |
| Inconsistent account guard across workflows | #2633 | **Absorbed structurally** | The new orchestrator provides ONE account guard at entry (Phase 0). Per-module workflows retain their string-confirmation as defense-in-depth (the orchestrator's `workflow_dispatch` call passes the correct confirmation string programmatically). |

### Summary: What Ships First vs. What the Orchestrator Absorbs

**Ships first (prerequisite PRs):**
- #2629: Add `adp/*/github-app/*` to `force-delete-secrets.sh` protect-list

**Absorbed by orchestrator (no separate fix needed):**
- #2628 (webhook-ingress coverage)
- #2630 (agent-factory cleanup — workflow already handles it)
- #2631 (gateway cleanup — workflow already handles it)
- #2632 (webhook-ingress coverage)
- #2633 (account guard consistency)

**Recommend closing as "won't fix" / obsolete:**
- None — all have a clear disposition

---

## 3. Single Account Guard + Confirmation

### Design

The orchestrator introduces **one gate at entry** that replaces the inconsistent
per-workflow/per-script guards:

```
┌─────────────────────────────────────────────────────────────┐
│  Phase 0: Account Guard                                      │
│                                                             │
│  1. Resolve caller identity: aws sts get-caller-identity    │
│  2. Display: account ID, ARN, region, environment           │
│  3. Require typed account ID (not "yes") to proceed         │
│  4. If config/deployment.yml exists, cross-check account    │
│  5. Store confirmed account ID in runtime state             │
└─────────────────────────────────────────────────────────────┘
```

### Why Typed Account ID (Not "yes")

- `deploy-all.sh --destroy` currently asks for "yes" — this confirms intent but
  not target. An operator with wrong `AWS_PROFILE` active still proceeds.
- `bootstrap-destroy.sh` already requires typed account ID — proven pattern.
- The orchestrator adopts the stronger guard: **type the 12-digit account ID**.

### Defense-in-Depth

Per-module workflows retain their existing string-confirmation inputs (`"gateway"`,
`"agent-context"`, etc.). When the orchestrator dispatches them via
`gh workflow run`, it passes the correct confirmation programmatically. When an
operator runs a single module manually, the per-module guard still protects.

### Script Mode (Self-Managed)

```bash
./platform/scripts/undeploy.sh
# Output:
#   AWS Account: 261421447505
#   ARN: arn:aws:iam::261421447505:role/adp-dev-deploy-role
#   Region: us-east-1
#   Environment: dev
#
#   This will destroy ALL ADP infrastructure in this account.
#   Type the account ID (261421447505) to confirm:
#   > _
```

### Workflow Mode (ADP-Managed)

The workflow accepts `account_id` as a required input. The workflow's first step
validates it matches `aws sts get-caller-identity`. This prevents dispatch from
wrong context (e.g., workflow re-run after profile switch).

---

## 4. Survive-by-Design Contract

### Resources That MUST NEVER Be Deleted

| Resource Class | Path Pattern | Enforcement Mechanism |
|----------------|--------------|----------------------|
| Terraform state backend | S3: `adp-terraform-state-*`, DynamoDB: `adp-terraform-locks` | Not in any phase. Separate opt-in script (`bootstrap-destroy.sh`) with account-ID gate. |
| GitHub App secrets (legacy) | `adp/gh-app-*`, `adp/*/gh-app-*` | `force-delete-secrets.sh` protect-list (lines 53-55) |
| GitHub App secrets (webhook-ingress) | `adp/*/github-app/*` | **NEW**: Add to `force-delete-secrets.sh` protect-list (#2629 fix) |
| AWS-managed RDS secrets | `rds!*` | `force-delete-secrets.sh` protect-list (line 61) |
| GitHub Apps themselves | (not in AWS — live in GitHub org settings) | Not touched by any AWS-side script. Documentation note only. |

### How the Design Enforces Survival

1. **Protect-list in force-delete-secrets.sh** — the shared helper is the ONLY
   path for secrets deletion. All workflows and the orchestrator call it. One
   protect-list, one enforcement point.

2. **Bootstrap-destroy.sh is NEVER called by the orchestrator** — it's a separate,
   intentional manual step. The orchestrator's Phase 5 (platform) output explicitly
   says: "State backend intact. Run `bootstrap-destroy.sh` separately if intended."

3. **Webhook-ingress secrets survive because they're terraform-managed** — the
   webhook-ingress workflow's terraform destroy removes the Secrets Manager
   *resource* from state but the `force_delete_without_recovery = false` default
   means AWS retains the secret for 7 days. The protect-list ensures
   `force-delete-secrets.sh` (called elsewhere) won't force-delete them.

4. **Contract documented in script header + dry-run output** — both the script and
   workflow explicitly list what survives, so operators know before confirming.

---

## 5. Idempotency / Resumability / Partial-Failure

### State File

The orchestrator maintains `.adp-undeploy-state.json` in the repo root (or a
temp location for workflow mode):

```json
{
  "account_id": "261421447505",
  "environment": "dev",
  "started_at": "2026-07-02T09:30:00Z",
  "phases": {
    "agent_context":    { "status": "complete", "completed_at": "..." },
    "webhook_ingress":  { "status": "complete", "completed_at": "..." },
    "agent_factory":    { "status": "failed",   "error": "terraform timeout", "attempts": 1 },
    "gateway":          { "status": "pending" },
    "platform":         { "status": "pending" },
    "bootstrap":        { "status": "skipped" }
  }
}
```

### Resume Behavior

On re-run:
1. Read state file (if exists)
2. Skip phases with `status: "complete"`
3. Retry phases with `status: "failed"` (up to 2 attempts)
4. Continue from first non-complete phase
5. If all phases complete → print summary + survival contract reminder

### Idempotency Guarantees

Every phase is idempotent by construction:
- **kubectl delete** — no-op if resource doesn't exist (`|| true`)
- **empty-s3-buckets.sh** — no-op on empty/missing buckets
- **force-delete-secrets.sh** — no-op if secrets already gone
- **terraform destroy** — idempotent (removes from state what's already gone)
- **CloudFront disable** — no-op if already disabled or doesn't exist
- **ENI cleanup** — only deletes `available` ENIs (already-detached)

### Exit Code Handling

```bash
# Every phase follows this pattern:
run_phase "phase_name" phase_function
# Where run_phase:
#   1. Sets status to "running" in state file
#   2. Calls phase_function
#   3. On success: sets status to "complete"
#   4. On failure (non-zero exit): sets status to "failed", records error
#   5. Returns the exit code (caller decides whether to continue)
```

**Critical**: No `| tail` or `2>/dev/null` on terraform commands — exit codes
must propagate. The existing deploy-all.sh has this right; the orchestrator
mirrors it.

### Partial Destroy Safety

If the orchestrator fails mid-run (e.g., Phase 3 fails):
- Phases 1-2 are complete and won't re-run
- Phase 3 will retry (idempotent)
- Phases 4-5 haven't started (safe)
- The state file records exactly where to resume

---

## 6. Dry-Run / Plan Mode

### Design

```bash
./platform/scripts/undeploy.sh --dry-run
# OR
./platform/scripts/undeploy.sh --plan
```

### What Dry-Run Does

For each phase, instead of destroying:

1. **Shows what exists** (queries current state):
   - K8s namespaces that would be deleted
   - S3 buckets that would be emptied (and their object counts)
   - Secrets that would be force-deleted (calls `force-delete-secrets.sh --dry-run`)
   - CloudFront distributions that would be disabled
   - Terraform resource count (`terraform plan -destroy` output summary)

2. **Shows what's protected** (survive-by-design):
   - Lists protected secrets that WON'T be touched
   - Confirms state backend path exists and won't be touched

3. **Shows phase order** with estimated time per phase

### Output Format (Dry-Run)

```
=== ADP Undeploy — DRY RUN ===
Account: 261421447505 | Environment: dev | Region: us-east-1

Phase 1: Agent Context
  K8s: namespace 'agent-context' exists (3 pods running)
  Terraform: 47 resources would be destroyed
  Estimated time: ~3 min

Phase 2: Webhook Ingress
  S3: lambda-artifacts/webhook-ingress/ (2 objects)
  Terraform: 38 resources would be destroyed
  Estimated time: ~2 min

Phase 3: Agent Factory
  K8s: namespace 'adp-gateway-agents' exists (0 pods)
  K8s: namespace 'arc-runners' exists (2 pods)
  S3: adp-dev-agent-beads-state-* (1 bucket, 142 objects)
  Terraform: 29 resources would be destroyed
  Estimated time: ~3 min

Phase 4: Gateway
  K8s: Ingress 'gateway-ingress' in adp-gateway (ALB: arn:aws:...)
  K8s: namespace 'adp-gateway' exists (4 pods)
  S3: bedrockgw-dev-frontend-* (1 bucket, 847 objects)
  Secrets: 3 would be deleted, 0 protected
  CloudFront: d1234567890 (Enabled, must disable first)
  Terraform: 155 resources would be destroyed
  Estimated time: ~15 min (CloudFront disable dominates)

Phase 5: Platform
  K8s: namespace 'arc-systems' exists (3 pods)
  K8s: namespace 'keda' exists (2 pods)
  ENIs: 4 orphaned ENIs in VPC
  Terraform: 94 resources would be destroyed
  Estimated time: ~10 min

PROTECTED (will not be touched):
  - State backend: s3://adp-terraform-state-261421447505 ✓
  - GitHub App secrets: adp/dev/github-app/* (2 secrets) ✓
  - GitHub App secrets: adp/adp-test-ml/gh-app-* (6 secrets) ✓
  - RDS-managed secrets: rds!* (1 secret) ✓

Total estimated time: ~33 min
Run without --dry-run to execute.
```

### Workflow Mode Dry-Run

The GitHub Actions workflow exposes a `dry_run` boolean input (default: false).
When true, it runs `terraform plan -destroy` instead of `terraform destroy` and
skips all kubectl/S3/secrets operations (report only).

---

## 7. Script vs. Workflow Split (Two-Track Rule)

### Principle

The ADP platform supports two deployment tracks:
- **ADP-managed** (GitHub Actions workflows, `customer_account` inputs, OIDC assume-role)
- **Self-managed** (local scripts, operator's `AWS_PROFILE`, direct AWS access)

The undeploy orchestration must support both WITHOUT conflating them (per the
two-track rule established in the deploy side).

### Architecture

```
┌───────────────────────────────────────────────────────────────────┐
│                    Shared Orchestration Logic                       │
│         platform/scripts/undeploy-phases.sh (sourced)              │
│                                                                   │
│  Exports functions: phase_agent_context(), phase_webhook_ingress(),│
│  phase_agent_factory(), phase_gateway(), phase_platform()         │
│  Each function: idempotent, uses env vars, returns exit code      │
└───────────────────────────────────────────────────────────────────┘
            ▲                                    ▲
            │ sources                            │ sources
┌───────────────────────────┐     ┌─────────────────────────────────┐
│  Self-Managed Track        │     │  ADP-Managed Track              │
│                           │     │                                 │
│  platform/scripts/        │     │  .github/workflows/             │
│    undeploy.sh            │     │    undeploy.yml                 │
│                           │     │                                 │
│  - Interactive prompts    │     │  - workflow_dispatch inputs      │
│  - Reads AWS_PROFILE      │     │  - OIDC assume-role             │
│  - Local state file       │     │  - Calls per-module workflows   │
│  - Typed account-ID gate  │     │  - OR runs phases inline        │
│  - Supports --dry-run     │     │  - Supports dry_run input       │
│  - Supports --skip-phase  │     │  - Sequential job dependencies  │
└───────────────────────────┘     └─────────────────────────────────┘
```

### Self-Managed Track: `platform/scripts/undeploy.sh`

```bash
# Interface:
./platform/scripts/undeploy.sh [OPTIONS]

Options:
  --dry-run              Preview what would be destroyed
  --skip <phase>         Skip a specific phase (repeatable)
  --from <phase>         Start from a specific phase (skip earlier ones)
  --bootstrap            Also destroy state backend (prompts separately)
  --yes                  Skip interactive confirmation (for CI/agent use)
  --environment <env>    Override environment (default: from config)
```

**Internal structure:**
1. Sources `load-deploy-config.sh` (resolves account, region, environment)
2. Sources `undeploy-phases.sh` (phase functions)
3. Runs account guard (Phase 0)
4. Iterates phases, calling phase functions directly
5. Updates local `.adp-undeploy-state.json`

### ADP-Managed Track: `.github/workflows/undeploy.yml`

```yaml
name: Undeploy All
on:
  workflow_dispatch:
    inputs:
      account_id:
        description: 'Type the 12-digit account ID to confirm destruction'
        required: true
        type: string
      dry_run:
        description: 'Plan only — show what would be destroyed'
        required: false
        type: boolean
        default: false
      skip_phases:
        description: 'Comma-separated phases to skip (e.g., "agent_context,bootstrap")'
        required: false
        type: string
      include_bootstrap:
        description: 'Also destroy state backend (DANGEROUS)'
        required: false
        type: boolean
        default: false

jobs:
  guard:
    # Validates account_id input matches caller identity
    ...
  agent-context:
    needs: [guard]
    if: ${{ !contains(inputs.skip_phases, 'agent_context') }}
    uses: ./.github/workflows/agent-context-infra-destroy.yml
    with:
      confirm: agent-context
  webhook-ingress:
    needs: [agent-context]
    if: ${{ !contains(inputs.skip_phases, 'webhook_ingress') }}
    uses: ./.github/workflows/webhook-ingress-destroy.yml
    with:
      confirm: webhook-ingress
  # ... continues for agent-factory, gateway, platform
```

**Key design choice**: The workflow **calls the existing per-module workflows**
(reusable workflow calls or `workflow_dispatch` + poll). This means:
- Per-module workflow internals don't need to change
- Each module's concurrency group still protects against races
- Module-level confirmation strings are passed programmatically
- Module teams can still run individual destroys independently

### What Differs Between Tracks

| Aspect | Self-Managed (script) | ADP-Managed (workflow) |
|--------|----------------------|------------------------|
| Authentication | `AWS_PROFILE` / env creds | OIDC assume-role |
| Account guard | Interactive typed account ID | Input parameter validated in guard job |
| Execution | Direct (bash functions) | Sequential workflow jobs calling per-module workflows |
| State file | Local `.adp-undeploy-state.json` | GitHub Actions job outputs + step summaries |
| Dry-run | `--dry-run` flag | `dry_run: true` input |
| Resume | Re-run script (reads state file) | Re-run workflow (skips via inputs) |
| Bootstrap | `--bootstrap` flag (prompts again) | `include_bootstrap: true` (separate job at end) |

### What's Shared

- **Phase logic** — `undeploy-phases.sh` contains the actual cleanup commands
  (kubectl, S3, secrets, CloudFront, terraform). Both tracks source it.
- **Helpers** — `empty-s3-buckets.sh`, `delete-ingress-and-wait.sh`,
  `force-delete-secrets.sh` are called identically by both tracks.
- **Phase order** — hardcoded in both (agent-context → webhook-ingress →
  agent-factory → gateway → platform). Single source of truth is the phase
  functions' numbering.

---

## 8. File-Level Plan

### New Files to Create

| File | Purpose |
|------|---------|
| `platform/scripts/undeploy.sh` | Self-managed track entry point (interactive, supports --dry-run, --skip, --from, --bootstrap) |
| `platform/scripts/undeploy-phases.sh` | Shared phase functions (sourced by both tracks). Each function: idempotent, env-var-driven, returns exit code. |
| `.github/workflows/undeploy.yml` | ADP-managed track entry point (workflow_dispatch, calls per-module destroy workflows sequentially) |
| `docs/adp-platform-deployment/undeploy-design.md` | This design document |

### Existing Files to Modify

| File | Change |
|------|--------|
| `platform/scripts/force-delete-secrets.sh` | Add `adp/*/github-app/*` to `is_protected()` function (#2629) |
| `.github/workflows/webhook-ingress-destroy.yml` | Add optional `kubectl delete ns adp-agents --timeout=120s \|\| true` pre-step (for pod cleanup before TF destroy) |
| `CLAUDE.md` | Update "Destroy / Teardown" section to reference `undeploy.sh` as primary entry point; note `deploy-all.sh --destroy` is legacy |
| `docs/adp-platform-deployment/deploy-quickstart.md` | Update teardown section to reference `undeploy.sh` |
| `docs/adp-platform-deployment/self-managed-deploy.md` | Update teardown section |

### Files NOT Modified (retained as-is)

| File | Why |
|------|-----|
| `platform/scripts/deploy-all.sh` | Retained as legacy reference. `--destroy` path still works but is not the recommended entry point. No patches. |
| Per-module destroy workflows (5 files) | Internals unchanged — the orchestrator calls them as-is |
| `platform/scripts/empty-s3-buckets.sh` | No changes needed |
| `platform/scripts/delete-ingress-and-wait.sh` | No changes needed |
| `platform/scripts/bootstrap-destroy.sh` | No changes needed |

### Proposed Implementation Child-Issue Breakdown

> To be filed after design sign-off — not now.

| # | Title | Scope | Depends On |
|---|-------|-------|------------|
| A | fix(secrets): add `adp/*/github-app/*` to force-delete-secrets.sh protect-list | 2-line fix in `is_protected()` | None (ship first) |
| B | feat(undeploy): implement `undeploy-phases.sh` shared phase functions | New file: phase functions sourced by both tracks | A |
| C | feat(undeploy): implement `undeploy.sh` self-managed entry point | New file: interactive script with account guard, dry-run, resume | B |
| D | feat(undeploy): implement `undeploy.yml` ADP-managed workflow | New workflow calling per-module destroys in sequence | B |
| E | feat(webhook-ingress): add `adp-agents` namespace cleanup to destroy workflow | 3-line addition to `webhook-ingress-destroy.yml` | None (can parallel with B) |
| F | docs: update CLAUDE.md + deploy docs to reference undeploy.sh | Doc updates across 3 files | C, D |

**Dependency graph:**
```
A ──→ B ──→ C (self-managed track)
      │  └─→ D (ADP-managed track)
      │       └──→ F (docs)
E (independent)──→ F
```

---

## Appendix A: Resource Coverage Matrix

Validating against #2562's deployed inventory (account `261421447505`):

| Resource Class | Phase That Destroys It | Covered? |
|----------------|----------------------|----------|
| EKS Cluster (Auto Mode) | Platform (5) | ✅ |
| VPC + Subnets + NAT | Platform (5) | ✅ |
| Security Groups (6) | Platform (5) | ✅ |
| ECR Repositories | Platform (5) | ✅ |
| KMS Keys | Platform (5) | ✅ |
| IAM Roles + OIDC Provider | Platform (5) | ✅ |
| CodeBuild Projects (4+retired) | Platform (5) | ✅ |
| RDS PostgreSQL | Gateway (4) | ✅ |
| ElastiCache Redis | Gateway (4) | ✅ |
| Cognito User Pool + Identity Pool | Gateway (4) | ✅ |
| CloudFront Distribution | Gateway (4) | ✅ |
| S3 Frontend Bucket | Gateway (4) | ✅ |
| ALB (Ingress-created) | Gateway (4) — via delete-ingress-and-wait.sh | ✅ |
| API Gateway (REST, gateway module) | Gateway (4) | ✅ |
| Lambda: budget, authorizer, broker | Gateway (4) | ✅ |
| Lambda layers (psycopg2, pyjwt) | Gateway (4) — S3 artifacts cleaned by TF | ✅ |
| SSM Parameters (gateway) | Gateway (4) — post-destroy cleanup | ✅ |
| API Gateway HTTP v2 (webhook) | Webhook Ingress (2) | ✅ |
| Lambda: github-webhook | Webhook Ingress (2) | ✅ |
| SQS FIFO: agent-submit + DLQ | Webhook Ingress (2) | ✅ |
| DynamoDB: tenant-registry, webhook-events, rate-limits | Webhook Ingress (2) | ✅ |
| KEDA ScaledJob + warm-pool | Webhook Ingress (2) | ✅ |
| S3: lambda-artifacts/webhook-ingress/ | Webhook Ingress (2) | ✅ |
| ARC Runner ScaleSet (Helm) | Agent Factory (3) | ✅ |
| DynamoDB: beads-manifest | Agent Factory (3) | ✅ |
| S3: agent-beads-state | Agent Factory (3) | ✅ |
| Agent Gateway (SQS, WebSocket API GW) | Agent Factory (3) | ✅ |
| Neptune Serverless | Agent Context (1) | ✅ |
| OpenSearch Serverless | Agent Context (1) | ✅ |
| SQS: ingestion queue | Agent Context (1) | ✅ |
| DynamoDB: ingestion state | Agent Context (1) | ✅ |
| S3: vectors/embeddings | Agent Context (1) | ✅ |
| Terraform state S3 + DynamoDB | Bootstrap (6, opt-in) | ✅ |

### Resources That Survive (by design)

| Resource | Why |
|----------|-----|
| GitHub App secrets (`adp/*/github-app/*`, `adp/*/gh-app-*`) | Recreating costs browser flow; protect-list enforced |
| AWS-managed RDS secrets (`rds!*`) | AWS manages lifecycle; recreated if RDS recreated |
| GitHub Apps (in GitHub org settings) | Not AWS resources; manual browser deletion |
| Terraform state backend (unless `--bootstrap`) | Needed for re-runs and audit trail |

---

## Appendix B: Timing Estimates

Based on observed destroy times from prior teardowns:

| Phase | Dominant Wait | Estimated Time |
|-------|---------------|---------------|
| Agent Context | Neptune cluster deletion | 3-5 min |
| Webhook Ingress | Lambda + API GW cleanup | 1-2 min |
| Agent Factory | S3 bucket emptying (if large) | 2-3 min |
| Gateway | CloudFront disable + ALB wait | 12-18 min |
| Platform | EKS cluster deletion | 8-12 min |
| Bootstrap | S3 bucket emptying | 1-2 min |
| **Total** | | **~30-40 min** |

Gateway dominates due to CloudFront's 15-minute disable propagation. The
orchestrator should print a progress indicator during that wait (not silent).

---

## Appendix C: Error Scenarios + Recovery

| Scenario | What Happens | Recovery |
|----------|--------------|----------|
| Terraform destroy hangs (SG has ENI dependency) | Phase exits after TF timeout (default 10min) | Re-run: delete-ingress-and-wait.sh cleans ENIs on retry |
| CloudFront disable timeout (>15min) | Script warns, continues to TF destroy | TF destroy will fail on distribution; re-run after CF deployed |
| EKS cluster unreachable (kubectl fails) | All kubectl steps skip (`\|\| true`) | TF destroy still works; orphaned K8s resources are gone with cluster |
| S3 bucket has deletion policy | empty-s3-buckets.sh fails on protected objects | Manual intervention: check bucket policy, remove protection |
| Webhook-ingress terraform state drift | TF destroy fails on resource not found | `terraform state rm` the drifted resource, retry |
| Mid-run network failure | Phase marked "failed" in state file | Re-run: resumes from failed phase (idempotent) |

---

## Design-Only Statement

**This document is a design deliverable only.** No code has been written, no
teardown has been executed, no infrastructure has been modified. Implementation
proceeds via the child-issue breakdown (Section 8) after design review and
sign-off.
