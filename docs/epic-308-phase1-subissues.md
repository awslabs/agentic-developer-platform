# EPIC #308 Phase 1 — sub-issues to file

This document lists the sub-issues that must be created under EPIC #308 to complete Phase 1 of the hosted multi-tenant ADP platform as described in `docs/hosted-platform-design.md`.

Referenced from the tagging comment on #308. Agent-pm: fetch this file in full before filing sub-issues.

## Current deployment state (2026-05-02)

Live component audit against the design:

| # | Component | State |
|---|-----------|-------|
| 1 | API Gateway HTTP API v2 (`adp-dev-webhook-ingress`) | ✅ Live at `ppor9iu1h3.execute-api.us-east-1.amazonaws.com`, route `POST /github` |
| 2 | Webhook Lambda (`adp-dev-github-webhook`) | ⚠️ Function Active but **returns a stub response**. Live env vars (`SQS_QUEUE_URL`, `WEBHOOK_SECRET_ARN`) don't match `handler.py` reads (`SUBMIT_QUEUE_URL`, `WEBHOOK_SECRET`). |
| 3 | Webhook HMAC secret | ✅ `adp/dev/webhook-ingress/github-webhook-secret` |
| 4 | SQS FIFO submit queue | ✅ `adp-dev-agent-submit.fifo` |
| 5 | SQS DLQ | ✅ `adp-dev-agent-submit-dlq.fifo` |
| 6 | DynamoDB `tenant-registry` | ✅ `adp-dev-tenant-registry` |
| 7 | DynamoDB `webhook-events` | ✅ `adp-dev-webhook-events` |
| 8 | DynamoDB `rate-limits` | ✅ `adp-dev-rate-limits` |
| 9 | WAF | ✅ Removed by decision (PR #338) — WAFv2 doesn't support HTTP API v2; rate limiting done in Lambda |
| 10 | KEDA installed | ✅ operator running in `keda` namespace |
| 11 | **ScaledJob `agent-scaledjob`** on submit queue | ❌ **does not exist** |
| 12 | **Agent image `adp-agent:<tag>`** | ❌ **does not exist** |
| 13 | **Pod entrypoint (`entrypoint.py`)** | ❌ does not exist |
| 14 | **Public GitHub App `ADP Agent Platform`** | ❌ not registered (only per-org dev/pm/ops Apps for the self-hosted model exist) |
| 15 | Vault per-tenant creds | ❌ blocked on EPIC #132 |
| 16 | Customer AWS role CloudFormation templates | ❌ not built |
| 17 | Onboarding UI | ❌ Phase 2 per design, out of scope for Phase 1 |
| 18 | Live-status edit-in-place comment UX | ❌ not built |

## Currently-filed sub-issues

Six closed sub-issues (#317–#322), all focused on `modules/agent-factory/webhook-ingress/` ingress scaffolding. **No sub-issues exist for the consumer-side Kubernetes workload, GitHub App registration, or customer AWS access.** Phase 1 as described in the EPIC requires all of these.

## Sub-issues to create

Each sub-issue below should be filed by agent-pm with:
- The title as shown
- The acceptance criteria, file paths, design refs, and dependencies from this document
- Link to EPIC #308 as parent (GraphQL `addSubIssue` mutation, not text mention)
- Scope estimate (S / M / L)
- Appropriate agent assignee label (`agent-developer` / `agent-operations` / `agent-reviewer`)

### Track A — fix what's partially delivered

#### A-1. `hosted-webhook: reconcile live Lambda env vars with handler.py and redeploy real handler`

**Scope:** S (half day)
**Assignee:** `agent-developer`

**Problem:** The deployed Lambda returns `{"status":"ok","message":"webhook-ingress stub"}` — not the full handler. Env vars in AWS don't match what the code reads.

**Acceptance:**
- `POST /github` with a valid HMAC-signed GitHub webhook payload returns `200 {"status":"accepted","message_id":"..."}` and an envelope lands in `adp-dev-agent-submit.fifo`
- Integration test (see A-2) passes end-to-end against the live endpoint
- HMAC secret is fetched from Secrets Manager at Lambda cold start using `WEBHOOK_SECRET_ARN`, not stored in Lambda env

**Files:**
- `modules/agent-factory/webhook-ingress/infra/lambdas.tf` — env var reconciliation
- `modules/agent-factory/webhook-ingress/lambda/github/handler.py` — support secret-ARN lookup (or align variable names)
- `modules/agent-factory/webhook-ingress/lambda/common/secrets.py` — new helper if not already present

**Dependencies:** none

#### A-2. `hosted-webhook: wire conftest fixtures (endpoint, secret, queue url) so #321 integration tests actually run`

**Scope:** S (few hours)
**Assignee:** `agent-developer`

**Problem:** Tests in `modules/agent-factory/webhook-ingress/tests/test_e2e_webhook.py` reference fixtures `webhook_endpoint`, `webhook_secret`, `webhook_sqs_queue_url` that don't exist in `conftest.py`. Every test ERRORs at setup with `fixture 'webhook_endpoint' not found`.

**Acceptance:**
- Integration Tests job in `.github/workflows/webhook-ingress-deploy.yml` passes against the live environment
- Fixtures resolve from Terraform outputs via SSM parameters or env vars set by the deploy workflow

**Files:**
- `modules/agent-factory/webhook-ingress/tests/conftest.py` — add the three missing fixtures
- `modules/agent-factory/webhook-ingress/tests/helpers.py` — already present, may need small additions
- `.github/workflows/webhook-ingress-deploy.yml` — pass endpoint/secret-arn/queue-url as env to the tests job
- `modules/agent-factory/webhook-ingress/infra/outputs.tf` — emit Terraform outputs the workflow reads

**Dependencies:** A-1 (so the real handler is live to test against)

### Track B — agent pod worker (the largest gap)

#### B-1. `hosted-agent-worker: Dockerfile for adp-agent image (Node 22 + Python 3.12 + persona/skill staging)`

**Scope:** M (1-2 days)
**Assignee:** `agent-developer`

**Acceptance:**
- Image builds in CI and pushes to ECR as `adp-agent:<git-sha>` and `adp-agent:latest`
- Image contains:
  - `/app/dist/agent-worker.js` (compiled from `modules/agent-factory/agent/src/agent-worker.ts`)
  - `/app/dist/skill-agent.js` (compiled skill-agent runtime)
  - `/app/node_modules/` pre-installed
  - `/app/personas/` — all persona markdown (developer.md, pm.md, operations.md, reviewer.md, malware-analysis-agent.md, etc.)
  - `/app/skills/` — all skill directories with SKILL.md + helpers
  - `/app/entrypoint.py` — from B-2 (this sub-issue includes the Dockerfile-side copy, B-2 implements the script)
- Tools installed: `git`, `gh` CLI, `aws` CLI v2, `kubectl`, `jq`, `python3.12`
- Base: `ubuntu:24.04`
- Image size < 2 GB

**Files:**
- new `modules/agent-factory/agent-worker-image/Dockerfile`
- new `modules/agent-factory/agent-worker-image/stage-personas.sh` — copies `/modules/agent-factory/agent/personas/` into the image build context at bake time
- new ECR repo `adp-agent-runtime` (if not already present — currently an untagged repo by that name exists)

**Design ref:** `docs/hosted-platform-design.md` §Worker image contents and §Persona + skill staging

**Dependencies:** none (can start in parallel with B-2)

#### B-2. `hosted-agent-worker: pod entrypoint.py (SQS → vault → token mint → clone → agent exec → PR)`

**Scope:** M (1-2 days)
**Assignee:** `agent-developer`

**Acceptance:**
- Given a valid envelope in `$SQS_MESSAGE_BODY`, the entrypoint performs the 12-step sequence from the design:
  1. Parse envelope → extract `tenant_id`, `persona`, `installation_id`, `repo`, `issue`
  2. Fetch GitHub App credentials from vault (path: `tenants/<tenant_id>/github-app`)
  3. Mint installation token via JWT + `POST /app/installations/<id>/access_tokens`
  4. Set env: `GITHUB_TOKEN`, `GH_TOKEN`, `AGENT_TYPE`, `ISSUE_NUMBER`, `REPO_OWNER`, `REPO_NAME`, `WORK_DIR`, `TENANT_ID`, `CLAUDE_CODE_USE_BEDROCK=1`, `ANTHROPIC_MODEL`
  5. Clone customer repo: `git clone --depth=20 https://x-access-token:$TOKEN@github.com/<repo> /work/repo`
  6. Configure git identity (`adp-agent[bot]`)
  7. If persona needs AWS: assume customer role via STS with session tags — read `role_arn` + `external_id` from vault at `tenants/<tenant_id>/aws-access`
  8. Remove trigger label via `gh` CLI
  9. Post "started" comment on the originating issue
  10. `exec node /app/dist/agent-worker.js`
  11. On success: commit + push branch `agent/issue-<N>`, `gh pr create`, post completion comment
  12. On failure: post failure comment with error summary, exit nonzero
- Script is idempotent: re-running the same SQS message must not create duplicate comments or branches (use envelope `message_id` as idempotency key)
- ~150 lines per design

**Files:**
- new `modules/agent-factory/agent-worker-image/entrypoint.py`
- new `modules/agent-factory/agent-worker-image/lib/vault_client.py`
- new `modules/agent-factory/agent-worker-image/lib/github_token.py`
- new `modules/agent-factory/agent-worker-image/lib/sts_assume.py`
- tests under `modules/agent-factory/agent-worker-image/tests/`

**Design ref:** `docs/hosted-platform-design.md` §Agent pod entrypoint

**Dependencies:** B-1 (image), C-2 (STS helper), EPIC #132 (vault — can ship with a stub vault client initially)

#### B-3. `hosted-agent-worker: KEDA ScaledJob targeting adp-dev-agent-submit.fifo`

**Scope:** M (1 day)
**Assignee:** `agent-operations`

**Acceptance:**
- Namespace `adp-agents` created via Terraform
- Service account `agent-scaledjob-sa` with IRSA for:
  - SQS (receive + delete on `adp-dev-agent-submit.fifo`, send on customer response channels)
  - Bedrock invoke (via gateway)
  - Secrets Manager read (for vault access + GitHub App key)
  - STS assume-role (to target customer roles)
- ScaledJob `agent-scaledjob` with:
  - `minReplicaCount: 0`
  - `maxReplicaCount: 50`
  - `pollingInterval: 5s`
  - Trigger `aws-sqs-queue` at `adp-dev-agent-submit.fifo`, `queueLength: 1`
  - Image: `adp-agent:<tag>` (pulled from ECR using IRSA)
  - Pod resources: `requests.cpu: 1, requests.memory: 4Gi`; `limits.cpu: 4, limits.memory: 8Gi`; `ephemeral-storage: 50Gi`
  - `successfulJobsHistoryLimit: 5`, `failedJobsHistoryLimit: 5`
  - KEDA authentication via IRSA (`TriggerAuthentication` + `aws-eks` provider), never access keys
- NetworkPolicy: pods may egress to (a) GitHub API, (b) npm/pypi registries, (c) Bedrock gateway, (d) customer AWS APIs; all other egress denied

**Files:**
- new `modules/agent-factory/webhook-ingress/infra/scaledjob.tf` OR new sibling module `modules/agent-factory/agent-scaledjob/`
- Terraform resources: `kubernetes_namespace`, `kubernetes_service_account`, `aws_iam_role`, `aws_iam_role_policy`, `kubernetes_manifest` (for the ScaledJob YAML since provider doesn't have a native resource for it)

**Design ref:** `docs/hosted-platform-design.md` §SQS queue + KEDA ScaledJob

**Dependencies:** B-1 (image must exist before ScaledJob points at it), B-2 (entrypoint must exist to be invoked)

### Track C — GitHub App + customer AWS access

#### C-1. `hosted-github-app: register public ADP Agent Platform App + wire webhook + store private key`

**Scope:** S (half day)
**Assignee:** `agent-operations`

**Acceptance:**
- Public GitHub App `ADP Agent Platform` created (name globally unique — may need prefix if taken)
- Webhook URL: `https://ppor9iu1h3.execute-api.us-east-1.amazonaws.com/github` (or the production equivalent when DNS is wired)
- Webhook secret: value from `adp/dev/webhook-ingress/github-webhook-secret`
- App private key stored in `adp/<env>/github-app/adp-agent-platform-key`
- App ID stored in `adp/<env>/github-app/adp-agent-platform-id`
- Permissions per design:
  - `contents: write`
  - `issues: write`
  - `pull_requests: write`
  - `checks: write`
  - `metadata: read`
- Subscribed events: `issues`, `issue_comment`, `pull_request`, `pull_request_review`, `pull_request_review_comment`, `label`
- One end-to-end smoke test: install on a test repo, label an issue, confirm webhook arrives at the API Gateway endpoint (visible in webhook-events DDB table)

**Files:**
- new `modules/agent-factory/webhook-ingress/scripts/register-github-app.sh` — wizard-style flow like `platform/scripts/create-github-apps.sh` already does for the self-hosted apps
- Terraform additions for the two Secrets Manager entries

**Design ref:** `docs/hosted-platform-design.md` §The GitHub App

**Dependencies:** A-1 (real handler must be live for the smoke test to publish to SQS)

#### C-2. `hosted-customer-aws: CloudFormation templates (read-only / scoped-write / full-admin) + STS helper + session tagging`

**Scope:** M (1-2 days)
**Assignee:** `agent-developer`

**NOTE:** CloudFormation (not Terraform) is deliberate here. The templates are customer-facing artifacts rendered into the customer's own AWS account via the AWS Console's one-click "Launch Stack" flow — standard AWS SaaS onboarding UX (Datadog / Wiz / Snyk / Lacework pattern). Terraform would require customers to install TF first and contradicts the EPIC's "zero-infra" framing. This is the one intentional exception to ADP's "everything is Terraform" convention, and it's scoped to customer-facing artifacts only — our own infra for hosting these templates is still Terraform.

**Acceptance:**
- Three CFN templates published (hosted at a public S3 bucket `adp-public-cfn`):
  - `readonly.yaml` — describe / list / get IAM policies attached
  - `scoped-write.yaml` — customer-defined services/resources in parameters
  - `full-admin.yaml` — `*:*` (customer explicitly opts in)
- Each template creates an IAM role in the customer's account with:
  - Trust policy allowing our hosted platform's assume-role principal (ARN configurable via CFN parameter)
  - `sts:ExternalId` condition (per-tenant random 64-char string, provided as a CFN parameter from our onboarding UI)
  - `sts:AssumeRole` session duration default 1h, max 12h
- STS helper in the agent runtime:
  - Reads `role_arn` + `external_id` from vault at `tenants/<tenant_id>/aws-access`
  - Calls `sts:AssumeRole` with session tags:
    - `adp:tenant_id`
    - `adp:agent` (persona)
    - `adp:run_id`
    - `adp:github_issue` (URL)
    - `adp:actor` (GitHub login of the user who triggered the run)
  - Exports `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` into the agent's environment
- Documentation for customers: `docs/customer-aws-setup.md` — 3-step install flow with "Launch Stack" URL per tier

**Files:**
- new `modules/agent-factory/agent-worker-image/aws/readonly.cfn.yaml`
- new `modules/agent-factory/agent-worker-image/aws/scoped-write.cfn.yaml`
- new `modules/agent-factory/agent-worker-image/aws/full-admin.cfn.yaml`
- new `modules/agent-factory/agent-worker-image/lib/sts_assume.py` (implementation lives with entrypoint in B-2)
- new `docs/customer-aws-setup.md`
- Terraform: S3 bucket `adp-public-cfn` for hosting the templates; CloudFront distribution for stable URLs (optional but best practice)

**Design ref:** `docs/hosted-platform-design.md` §Customer AWS role access

**Dependencies:** EPIC #132 (vault) — can ship templates independently but STS helper integration blocks on vault

### Track D — UX

#### D-1. `hosted-agent-ux: edit-in-place live status comment pattern`

**Scope:** S (1 day)
**Assignee:** `agent-developer`

**Acceptance:**
- When an agent pod starts, it posts a "live status" comment on the triggering issue via the App installation token
- Every stage transition (pre-defined stages per persona) calls `PATCH /issues/comments/{id}` to update the comment body with:
  - Stage checklist: `[x]` completed, `[ ]` pending, `[~]` in progress
  - Elapsed time per stage
  - Relative timestamp ("last update 3s ago")
- On success: comment is replaced with a final summary (links to PR, artifacts, duration)
- On failure: comment is replaced with failure summary (error, stack trace excerpt, suggested next steps)
- Comment updates are rate-limited to max 1 per 5s to avoid GitHub secondary rate limits

**Files:**
- new `modules/agent-factory/agent/src/github-comments.ts` — helper module
- update `modules/agent-factory/agent/src/agent-worker.ts` — thread status-comment ID through the run, call helper at stage boundaries
- update `modules/agent-factory/agent/src/skill-agent.ts` — emit stage-transition events

**Design ref:** `docs/hosted-platform-design.md` §Live progress UX (Phase 1)

**Dependencies:** none (can be built on the self-hosted agents first and inherited by hosted)

## Out of scope for Phase 1 (do NOT file sub-issues)

The following are tracked elsewhere or deferred per the design:

| Item | Why not now |
|------|-------------|
| Per-tenant vault build-out | Tracked under EPIC #132 — reference as a dependency where relevant |
| Public onboarding UI (`adp.example.com`) | Phase 2 per design |
| Billing / Stripe integration | Phase 2 per design |
| Check Runs API integration (PR streaming UX) | Phase 2 UX per design |
| SOC 2 Type 1 readiness | Phase 2 per design |
| Marketplace listings (GitHub / AWS) | Phase 3 per design |
| AWS Identity Center / SSO variant | Phase 2 per design |

## Suggested sequencing

Tracks that can start immediately in parallel: **A-1**, **A-2** (after A-1), **B-1**, **C-1**, **D-1**.

Chain: **B-1** → **B-2** → **B-3** (image → entrypoint → orchestrator).

Blocked on vault (#132): **B-2** vault reads, **C-2** STS helper integration.

Once all tracks A, B, C are green, Phase 1 acceptance test per the EPIC:
> Pick one internal repo, install the App, connect a test AWS account via the CFN template, label an issue, watch an agent complete a full 7-stage cyber analysis AND run a deploy-style task that touches the connected AWS account — with nothing customer-side beyond the install.

## Acceptance criteria for this sub-issue batch

When all 8 sub-issues above are created, linked as children of #308 via GraphQL, and labeled with appropriate agent assignees, this batch is complete.

Do **not** close #308 when sub-issues are filed — the EPIC stays open until the Phase 1 acceptance test above passes end-to-end.
