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
- The label of your linked credential (e.g. `Dep-testing`). Confirm via the dashboard's Settings → AWS Access page.
- Your **ADP user_id** (Postgres `users.id` UUID — not the email or Cognito sub). Find it on the same page, or query the gateway DB. Required by the gateway's `/internal/v1/credential-assume-role` endpoint to identify which vaulted credential to resolve.
- **Manually attach `AdministratorAccess`** (or equivalent) to your `ADP-Agent-<label>` role in IAM console. The CFN template that created your role attached only `ReadOnlyAccess`, which is too narrow for terraform deploy steps. Scoping the permissions tighter (`adp-*` prefix only) is a future hardening step — for now, admin-equivalent is required.

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
│  (<platform-account-id>) │  2. agent pod auto-assumes │  │                 │
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
| 1 | Bootstrap (state bucket + lock table) | code ready ([#967](https://github.com/aws-e/adp/pull/967)) | #685 | Pod auto-assumes customer creds at startup; `bootstrap.sh` creates `adp-terraform-state-<customer>` in your account and rewrites `environments/dev/backend.tfvars` in pod's working dir. |
| 2 | Preflight | code ready (no changes needed) | #686 | `preflight-check.sh` already derives `ACCOUNT_ID` from `aws sts get-caller-identity`, so it inspects your account directly. |
| 3 | Platform infra (VPC + EKS + ECR + IAM + CodeBuild) | code ready (Stage A–D: [#973](https://github.com/aws-e/adp/pull/973)–[#976](https://github.com/aws-e/adp/pull/976)) | #687 | `platform-infra-apply.yml` now reads the `customer_account` block from `config/deployment.yml` via the load-deploy-config composite action. The orchestrator commits a config file with your `account_id` + `user_id` + `aws_label` to the deploy-instance branch before triggering the workflow. The Load step calls the gateway, gets STS creds, and the `terraform init` step uses `-backend-config="bucket=$STATE_BUCKET"` so state lands in your account's bucket. |
| 4 | Gateway infra (RDS + Cognito + ElastiCache + CloudFront + API GW + KMS) | code ready (same Stage A–D) | #688 | Same workflow-side mechanism. |
| 5 | Gateway backend (FastAPI on EKS + ALB) | code ready (same Stage A–D) | #689 | |
| 6 | Gateway frontend (S3 + CloudFront SPA) | code ready (same Stage A–D) | #690 | |
| 7 | Webhook ingress (API GW + Lambda + SQS + DynamoDB) | code ready (same Stage A–D) | #691 | |
| 8 | Agent delivery (KEDA + ARC + WebSocket API + chat infra) | code ready (same Stage A–D) | #692 | |
| 9 | GitHub App (UI flow or CLI fallback) | _pending end-to-end verification_ | #693 | `platform_admin` (Phase 6d) opens Settings → Connections → "Set up GitHub App" (manifest flow). CLI fallback: `register-github-app.sh`. |
| 10 | Smoke test | _pending end-to-end verification_ | | |

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

The script runs **27 checks** across these sections (verified against `platform/scripts/preflight-check.sh`):

| Section | What's checked | Outcome on miss |
|---|---|---|
| CLI tools — deploy | aws, terraform, node ≥ 22 | FAIL |
| | docker daemon | WARN (CodeBuild fallback) |
| CLI tools — post-deploy | gh CLI | WARN |
| AWS configuration | `sts:GetCallerIdentity`, region | FAIL |
| AWS permissions (required) | s3, dynamodb, eks, ecr | FAIL — needed by Phase 1 + 3 |
| AWS permissions (recommended) | iam, codebuild, bedrock, secretsmanager, cognito | WARN — needed by Phases 3–8 |
| Existing infra | state bucket + lock table from Phase 1 | WARN if missing — Phase 1 didn't complete |
| | EKS cluster, ECR `adp-gateway` | WARN — created by Phase 3 |
| Environment config | `backend.tfvars` substituted (no `ACCOUNT_ID` placeholder), gateway tfvars present, kubeconfig writable | WARN |

- Posts `## VERDICT: PASS` if 0 failures, or `## VERDICT: FAIL` with the list of missing permissions if any.

**Critical for ADP-managed track:** the script runs against **your linked-role's** permissions, not ADP's platform role. If your role is missing any of the recommended-section permissions, **Phase 2 will pass with warnings** but the corresponding later phase will fail. Read the warnings carefully; they predict where Phases 3–8 will block.

**Permissions NOT explicitly checked here** (but exercised later): RDS, ElastiCache, CloudFront, CloudWatch Logs, Lambda, API Gateway. If your linked role doesn't have them, Phases 4–8 will fail with `AccessDenied`. If your role is admin-level, no concern.

**No verification needed in your account post-Phase 2** — the script's exit code IS the verification. If Phase 1 PASSed and Phase 2 FAILed with permission errors, your linked role is missing some IAM grants. Update your role's permissions (re-run the CFN template or modify the role policy directly) and re-trigger Phase 2.

## What to expect: Phases 3–8 (terraform-driven workflows)

Phases 3 through 8 each delegate to a CI workflow (`platform-infra-apply.yml`, `gateway-infra-apply.yml`, `gateway-deploy.yml`, etc.). The orchestrator commits a `config/deployment.yml` to the deploy-instance branch before triggering the workflow:

```yaml
# config/deployment.yml — committed to the deploy-instance branch
account_id: "<platform-account-id>"  # the platform account where the runner lives
region: us-east-1
environment: dev
github_org: aws-e

customer_account:
  account_id: "403685770643"        # YOUR linked account — terraform deploys land here
  aws_label: Dep-testing            # YOUR vaulted credential label
  user_id: "650f093f-..."           # YOUR Postgres users.id
```

When the workflow runs:

1. The runner (in ADP's platform account) checks out the deploy-instance branch — gets the committed `config/deployment.yml`.
2. The `Load deployment config` composite action sources `platform/scripts/load-deploy-config.sh`. The helper sees `customer_account.account_id` is set and invokes `assume-customer-creds.py`.
3. `assume-customer-creds.py` calls the gateway at `http://bedrockgateway.adp-gateway/internal/v1/credential-assume-role` with your `user_id` + `aws_label`. The gateway resolves your vaulted role, performs `sts:AssumeRole` server-side, returns short-lived STS creds (1-hour by default).
4. Helper exports those creds to `$GITHUB_ENV`. Subsequent `terraform`, `aws`, `kubectl` steps in the same job use them, landing operations on **your** account.
5. The `terraform init` step also passes `-backend-config="bucket=$STATE_BUCKET"`, so state goes to `adp-terraform-state-<your-account-id>` (created in Phase 1).

Before terraform, `platform-infra-apply.yml` runs `platform/scripts/enable-bedrock-models.sh` against the target account: it discovers all ACTIVE Anthropic models from the Bedrock API and accepts their marketplace agreements via CLI. Fresh customer accounts have no agreements — without this step every Claude call fails with `AccessDeniedException` (`aws-marketplace:Subscribe`) and agent runs silently end "no changes needed". The step is idempotent; only models the platform invokes at runtime are deploy-blocking, the rest (newly released models) are best-effort.

You can confirm your account is the deploy target by tailing CloudTrail in your AWS account during the workflow run — you should see `AssumeRole` from the gateway IRSA principal, then a flurry of `CreateVpc`, `CreateCluster`, etc. signed by the resulting session principal.

**Failure modes specific to this path**:

| Failure | Symptom | Fix |
|---|---|---|
| Vault role lacks admin perms | terraform fails with `AccessDenied: not authorized to perform iam:CreateRole` | Manually attach `AdministratorAccess` to your `ADP-Agent-<label>` role in IAM console (per Prerequisites above). |
| Vault role's `MaxSessionDuration` too short | Long-running terraform applies hit `ExpiredToken` partway through | Edit the role and set `MaxSessionDuration: 12h` (max). Re-trigger the phase. |
| Wrong `user_id` in deploy-instance config | Gateway returns `credential_not_found` | The orchestrator constructs the config from your dashboard profile; if it picks the wrong user_id, fix in the deploy-instance issue body and re-trigger. |
| Gateway down | Workflow's Load step warns `gateway unreachable`, falls through to platform IRSA, terraform tries to deploy to platform account | Ops issue — check `kubectl get pods -n adp-gateway`. Halt the deploy until gateway is healthy. |
| Bedrock agreement missing (step skipped/failed) | Agent runs on the new deployment end "no changes needed" with $0.0000 / 1 turn; pod logs show `AccessDeniedException` citing `aws-marketplace:Subscribe` | Run `platform/scripts/enable-bedrock-models.sh` with creds for the target account, or re-trigger the phase (the workflow runs it automatically). Org private-marketplace policies can block newly released models — org admin must whitelist the product. |

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
