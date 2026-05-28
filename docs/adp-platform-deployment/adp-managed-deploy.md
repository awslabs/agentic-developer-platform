# ADP-Managed Deploy

> **Status:** Work in progress. Being built phase-by-phase via deploy-instance issues (see EPIC #684 + #966). Sections below get filled in as each deploy-test phase is unblocked and verified end-to-end. Do not assume any section is complete until the deploy-instance for that phase has posted a `## VERDICT: PASS`.

## When to use this track

Choose the **ADP-managed** deploy if:

- You already have an account on the hosted ADP platform.
- You've linked your AWS account in the ADP dashboard (Settings → AWS Access).
- You want agents running in **ADP's** infrastructure to do the deploy on your behalf.

You will not run `terraform`, `aws`, or `kubectl` yourself. You'll click a button (eventually — see [UI-driven deploy EPIC](#)) or comment on a GitHub issue, and an agent runs it.

If you don't have an ADP account, or you don't want to give ADP cross-account role access, use [self-managed-deploy.md](./self-managed-deploy.md) instead.

## Prerequisites

- An ADP dashboard account.
- An AWS account linked in the dashboard with a verified `aws_role` credential. The role's `MaxSessionDuration` should be ≥ 3600s (longer is better — single phases can exceed an hour).
- The label of your linked credential (e.g. `Dep-testing`). You can confirm via the dashboard's Settings → AWS Access page.

## How it works (high level)

```
┌──────────────────────┐                                ┌────────────────────┐
│  You                 │  1. file deploy-instance       │  Your AWS account  │
│  (ADP dashboard      │ ────────────────────────────►  │  (linked, verified)│
│   or GitHub)         │                                │                    │
└──────────────────────┘                                │  ▲                 │
                                                        │  │ STS AssumeRole  │
                                                        │  │ (role from vault)│
┌──────────────────────────┐                            │  │                 │
│  ADP platform account    │                            │  │                 │
│  (879318057152)          │  2. agent pod auto-assumes │  │                 │
│                          │ ─────────────────────────► │  ▼                 │
│  agent-scaledjob runs    │                            │  Resources         │
│  orchestrator + workers  │                            │  appear here       │
└──────────────────────────┘                            └────────────────────┘
```

The agent pod has STS credentials for **your** account in env at startup (auto-assumed via the gateway's `/internal/v1/credential-assume-role` endpoint, which reads your vaulted role). Every `aws`, `terraform`, `kubectl` command the agent runs lands in **your** account, not ADP's.

## Deploy phases

Same dependency order as the [self-managed deploy](./self-managed-deploy.md), but each phase runs inside an ADP agent pod whose STS env is your linked-account's assumed role. Status moves from "pending" → "code ready, awaiting end-to-end verification" → "verified" as each phase is run successfully against a real customer account.

| # | Phase | Status | Template | Notes |
|---|-------|--------|----------|-------|
| 1 | Bootstrap (state bucket + lock table) | code ready (PR #967) | #685 | Pod auto-assumes customer creds at startup; `bootstrap.sh` creates `adp-terraform-state-<customer>` in your account and rewrites `environments/dev/backend.tfvars` in pod's working dir. |
| 2 | Preflight | code ready (no changes needed) | #686 | `preflight-check.sh` already derives `ACCOUNT_ID` from `aws sts get-caller-identity`, so it inspects your account directly. |
| 3 | Platform infra (VPC + EKS + ECR + IAM + CodeBuild) | _blocked — workflow runs in platform account, not yours_ | #687 | The `platform-infra-apply.yml` workflow today uses platform IRSA + platform state. Needs a per-workflow customer-account role + STS chain-assume step. Tracked in [#966](https://github.com/aws-e/adp/issues/966). |
| 4 | Gateway infra (RDS + Cognito + ElastiCache + CloudFront + API GW + KMS) | _pending Phase 3_ | #688 | Same workflow-side refactor as Phase 3. |
| 5 | Gateway backend (FastAPI on EKS + ALB) | _pending Phase 3_ | #689 | |
| 6 | Gateway frontend (S3 + CloudFront SPA) | _pending Phase 3_ | #690 | |
| 7 | Webhook ingress (API GW + Lambda + SQS + DynamoDB) | _pending Phase 3_ | #691 | |
| 8 | Agent delivery (KEDA + ARC + WebSocket API + chat infra) | _pending Phase 3_ | #692 | |
| 9 | Smoke test | _pending all earlier phases_ | #693 | |

## What to expect: Phase 1 (Bootstrap)

When the orchestrator dispatches Phase 1:

- A sub-agent pod spins up with your assumed-role STS creds in env. (You can confirm this in CloudTrail: a `sts:AssumeRole` call to your `ADP-Agent-<label>` role from ADP's gateway role.)
- The pod runs `aws sts get-caller-identity` as a fail-fast check. If it returns the wrong account (e.g. ADP's platform account because auto-assume failed), the phase posts `## VERDICT: FAIL` and exits without touching anything.
- It runs `./platform/scripts/bootstrap.sh`. This creates two resources **in your account**:
  - `s3://adp-terraform-state-<your-account-id>` — versioned, encrypted (AES256), public access blocked.
  - DynamoDB table `adp-terraform-locks` — PAY_PER_REQUEST, partition key `LockID`.
- The script also rewrites `environments/dev/backend.tfvars` in the pod's working dir to point at your bucket. (The rewrite is local to the pod and never committed back to the repo.)
- Sub-agent posts `## VERDICT: PASS` on the phase child issue.

**Verify in your account** (after Phase 1 PASSes):

```bash
# In your AWS account, with your own credentials:
aws s3api head-bucket --bucket "adp-terraform-state-$(aws sts get-caller-identity --query Account --output text)"
# exits 0

aws dynamodb describe-table --table-name adp-terraform-locks --query 'Table.TableStatus' --output text
# ACTIVE
```

If either fails, the orchestrator's PASS verdict was wrong — open an issue with the deploy-instance number.

## What to expect: Phase 2 (Preflight)

When the orchestrator dispatches Phase 2:

- Sub-agent pod spins up with your assumed-role STS creds (same mechanism as Phase 1).
- Runs `./platform/scripts/preflight-check.sh`.
- The script does ~22 checks: AWS credentials valid, region set, IAM permissions on S3 / DynamoDB / EKS / ECR / RDS / ElastiCache / Cognito / CloudFront / Secrets Manager / IAM / CodeBuild, plus **state-bucket reachability** and **lock-table status** (which depend on Phase 1 having succeeded).
- Posts `## VERDICT: PASS` if 0 failures, or `## VERDICT: FAIL` with the list of missing permissions if any.

Some checks emit warnings rather than failures — e.g. "EKS cluster not yet created" is expected before Phase 3.

**No verification needed in your account post-Phase-2** — the script's exit code IS the verification. If Phase 1 PASSed and Phase 2 FAILed with permission errors, your linked role is missing some IAM grants. Update your role's permissions (re-run the CFN template or modify the role policy directly) and re-trigger Phase 2.

## Validation per phase

See [`deployment-manifest.md`](./deployment-manifest.md) for the canonical resource → validation-command mapping. The same validation suite applies to both tracks.

## Common failure modes

_To be filled in as each phase is verified. Each entry will name a real symptom seen during deploy-instance #946 (or successors), the root cause, and the fix that's been merged._

## Triggering a deploy

_The dashboard "Deploy ADP" button is a separate EPIC — see [#TBD]. Today, deploys are triggered by spawning a deploy-instance issue via the `Spawn Deploy Instance` workflow, which clones the canonical templates into per-deploy children. The orchestrator agent reads its assigned issue and walks the phases._

## References

- [`self-managed-deploy.md`](./self-managed-deploy.md) — the do-it-yourself track
- [`deployment-manifest.md`](./deployment-manifest.md) — what gets deployed + validation
- [`customer-aws-setup.md`](./customer-aws-setup.md) — connecting your AWS account to the ADP dashboard
- EPIC #684 — Minimum Viable Platform deploy test (parent EPIC for this work)
- Issue #966 — Per-workflow customer-account roles design
