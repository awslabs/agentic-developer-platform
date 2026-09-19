# Agent Instructions — ADP (Agentic Developer Platform)

You are the deployment agent for this platform. Your job is to deploy it end-to-end, keep the user informed, and only ask them when you genuinely need their input. Read this entire file, then execute the deployment.

## Your Behavior

- Run each step yourself. Do not ask the user to run commands — you run them.
- After each step, verify it succeeded before moving on using the validation commands in `docs/adp-platform-deployment/deployment-manifest.md`.
- If something fails, diagnose it, attempt a fix, and retry. Only escalate to the user if you cannot resolve it after 2 attempts.
- Keep the user informed with brief status updates between steps. Do not dump raw command output — summarize results.
- When you need user input (AWS credentials, GitHub App setup), explain exactly what you need and why.
- Maintain a deployment state file at `.adp-deploy-state.json` in the repo root. Update it after each phase. If this file exists when you start, resume from the last incomplete phase.
- Read `docs/adp-platform-deployment/deployment-manifest.md` for the full list of what gets deployed in each module and the exact validation commands.

## Deployment State

Maintain `.adp-deploy-state.json` in the repo root. Create it at the start, update after each phase:

```json
{
  "environment": "dev",
  "account_id": "",
  "github_org": "",
  "modules": [],
  "phases": {
    "org_setup":        {"status": "pending"},
    "bootstrap":        {"status": "pending"},
    "preflight":        {"status": "pending"},
    "platform_infra":   {"status": "pending"},
    "gateway_infra":    {"status": "pending"},
    "gateway_backend":  {"status": "pending"},
    "gateway_frontend": {"status": "pending"},
    "agent_factory":    {"status": "pending"},
    "agent_gateway":    {"status": "pending"},
    "github_apps":      {"status": "pending"},
    "verification":     {"status": "pending"}
  },
  "outputs": {},
  "validation": {}
}
```

Status values: `pending`, `running`, `complete`, `failed`, `skipped`.

On startup, if this file exists:
1. Read it and show the user current progress
2. Resume from the first non-complete, non-skipped phase
3. If a phase is `failed`, retry it

## Resource Map

Read `docs/adp-platform-deployment/deployment-manifest.md` for the complete mapping of every resource to its AWS service, module, and validation command. Use it to validate each phase after completion.

## What This Repo Contains

Three modules on a shared AWS platform:

| Module | Path | Purpose |
|--------|------|---------|
| Gateway | `modules/gateway/` | Multi-tenant Bedrock proxy (FastAPI + React) |
| Agent Factory | `modules/agent-factory/` | Autonomous code agents (Claude SDK + GitHub Actions) |
| Agent Context | `modules/agent-context/` | Code Intelligence Platform: semantic search, code search, wikis, memory via single MCP endpoint (5 tools). Fronts OpenViking, Sourcebot, DeepWiki, LiteLLM proxy. Deploy with `--agent-context-only`. |
| MCP Hub | `modules/harness/mcp-hub/` | MCP tools surface of the harness (in progress; see `ARCHITECTURE.md`) |
| User Services | `modules/user-services/` | Per-user products (vault, knowledge repo, bespoke agents, chief-of-staff); design only |

Shared infrastructure: `platform/infra/` (VPC, EKS, ECR, IAM).

## Deployment Playbook

> **The canonical agent-deploy guide is
> [`docs/adp-platform-deployment/deploy-with-agent.md`](docs/adp-platform-deployment/deploy-with-agent.md)**
> (the agent-behavior layer — phase table, placeholder-artifact rule, state
> file, when to call the user). It defers to **[`deploy-quickstart.md`](docs/adp-platform-deployment/deploy-quickstart.md)**,
> the authoritative verified procedure (phase sequence, exact scripts, gotchas;
> maintained against real end-to-end runs).
> `docs/adp-platform-deployment/self-managed-deploy.md` is the longer canonical
> reference; `deployment-manifest.md` is the resource→validation mapping. The
> notes below are CLAUDE-specific behaviors on top of those docs.

When driving a deployment, your job is to **execute the phases in
deploy-quickstart.md in order**, verifying each before moving on. Key agent
behaviors that still apply on top of that doc:

1. **Confirm the target AWS account first.** Everything keys off the account
   `aws sts get-caller-identity` resolves to (via the active `AWS_PROFILE`).
   Show the account + ARN and get the user's confirmation before Phase 1. There
   is **no upfront GitHub setup** — for the webhook agent path GitHub is wired at
   the END (UI flow: Settings → Connections → "Set up GitHub App"; or CLI
   fallback `register-github-app.sh`), and gateway-only needs no GitHub at all.
2. **Maintain `.adp-deploy-state.json`** (see Deployment State above): update it
   after each phase; on startup, resume from the first non-complete phase.
   Note: a committed copy from a fresh clone is NOT a record of your deploy —
   verify against real AWS state, don't trust its statuses.
3. **Keep the user informed** between phases with brief status; only stop for
   genuine input (AWS account choice and the GitHub App setup — UI flow
   preferred, CLI fallback for headless; see the phase numbering in
   deploy-quickstart.md). Bedrock model access is NOT a stop: it's automated
   via `platform/scripts/enable-bedrock-models.sh` (CLI-only; runs inside
   deploy-all.sh and platform-infra-apply.yml).
4. **The "placeholder artifact" rule:** Terraform ships placeholders for things a
   separate push-triggered CI workflow normally publishes (broker Lambda code,
   agent-runtime image, webhook Lambda zip, the ALB-gated API GW body). A fresh
   manual deploy fires none of those, so the stage-by-stage scripts
   (`wire-gateway-alb.sh --apply`, `deploy-broker.sh`, `deploy-webhook-ingress.sh`,
   `register-github-app.sh`) are the manual equivalents. deploy-quickstart.md
   sequences them; don't skip them.

The phase summary (timing/scope) and per-phase commands, verification, and
troubleshooting are all in deploy-quickstart.md — do not duplicate them here.

> **Deploy-path note:** earlier versions of this file inlined a 10-phase playbook
> with an upfront "Phase 0: GitHub setup" (`setup-org.sh` + 3 org-owned apps) that
> stood up ARC self-hosted runners as the *agent onboarding/deploy path*. That
> **onboarding path is superseded** by the webhook-ingress flow (GitHub webhook →
> Lambda → SQS → KEDA → agent-worker; see deploy-quickstart.md) — for summoning an
> agent to do open-ended work, use webhook-ingress, not ARC.
>
> **ARC runners are NOT deprecated as an execution model**, though. They remain a
> first-class, complementary capability: deterministic GitHub Actions pipelines on
> EKS that an agent can **trigger and monitor** — the right tool when you need a
> known, auditable, repeatable sequence of steps (e.g. complex multi-stage
> deployments) rather than open-ended agent reasoning. Setup + usage for that path
> lives in `modules/agent-factory/SETUP-GUIDE.md`.

## Troubleshooting Reference

Use this when things go wrong. Do not show this to the user — use it to diagnose and fix issues yourself.

### Terraform init fails
- ACCOUNT_ID placeholder not replaced → run `sed -i "s/ACCOUNT_ID/$(aws sts get-caller-identity --query Account --output text)/g"` on the tfvars file
- S3 bucket doesn't exist → run bootstrap.sh first

### EKS nodes not appearing
- Auto Mode takes 3-5 min. Wait and retry `kubectl get nodes`.
- If still empty after 5 min, check: `kubectl get events --all-namespaces --sort-by='.lastTimestamp' | tail -20`

### Gateway pods CrashLoopBackOff
- `kubectl logs -n adp-gateway -l app=bedrockgateway --previous --tail=50`
- Missing configmap: `kubectl get configmap bedrockgateway-config -n adp-gateway`
- Missing secret: `kubectl get secret bedrockgateway-secrets -n adp-gateway`
- RDS not reachable: check security groups allow EKS → RDS on port 5432

### CloudFront 502
- ALB not yet created by Ingress controller. Check: `kubectl get ingress -n adp-gateway`
- Wait 2-3 minutes for ALB provisioning, then check again.

### Frontend blank page
- Wrong VITE_API_URL during build. Rebuild with `VITE_API_URL="/api" npm run build` — must match `gateway-deploy.yml` (`/api`, NOT `/api/gateway`); the wrong prefix makes every SPA call hit the S3 HTML fallback with HTTP 200 and crash the dashboard.
- Stale cache: `aws cloudfront create-invalidation --distribution-id <id> --paths "/*"`

### All GitHub logins denied after a broker deploy
- Symptom: every "Sign in with GitHub" (including yours) fails, but `/api/health` is healthy and gateway pods are Running — so it is NOT the CloudFront `/api` fail-destroy class.
- Broker logs show: `ALLOWLIST_MODE=open without ALLOW_OPEN_SIGNUP=true is a misconfiguration; denying sign-in` (`aws logs tail /aws/lambda/bedrockgw-<env>-github-auth-broker --since 15m`).
- Cause: the #3986 fail-closed broker CODE publishes on merge, but the `ALLOW_OPEN_SIGNUP` env var only lands via `gateway-infra-apply.yml`. On an env still set to `ALLOWLIST_MODE=open`, the gap between the two is a total login outage.
- Emergency fix + durable remediation: `docs/runbooks/github-auth-allowlist-remediation.md` (adds `ALLOW_OPEN_SIGNUP=true` to the live Lambda to restore login in ~30s, then move the env to `mode=org`).

### CodeBuild fails
- Only 4 docker-build projects use CodeBuild (gateway-build, chat-agent, agent-gateway, arc-runner). They are Terraform-managed in `platform/infra/modules/codebuild/`. Everything else (terraform apply, npm build, kubectl apply) runs directly on the ARC runner.
- Check logs: `aws codebuild batch-get-builds --ids <build-id> --query 'builds[0].logs.deepLink' --output text`
- IAM propagation: if role was just created, wait 15 seconds and retry

---

## Destroy / Teardown

### Full teardown (primary)

Two tracks depending on your environment:

| Track | Entry point | When to use |
|-------|-------------|-------------|
| Self-managed | `./platform/scripts/undeploy.sh` | Running from your terminal against your own AWS account |
| ADP-managed | `.github/workflows/undeploy.yml` (workflow_dispatch) | Tearing down via GitHub Actions (CI/CD or operator portal) |

Both destroy modules in reverse dependency order (agent-context → webhook-ingress → agent-factory → gateway → platform), require a **typed 12-digit account ID** as a destruction guard, and support dry-run and phase skipping.

#### Self-managed (`undeploy.sh`)

```bash
./platform/scripts/undeploy.sh                      # Interactive teardown (typed-account-ID gate)
./platform/scripts/undeploy.sh --dry-run            # Show what would be destroyed
./platform/scripts/undeploy.sh --from gateway       # Resume from a specific phase
./platform/scripts/undeploy.sh --skip agent_context # Skip a phase
./platform/scripts/undeploy.sh --bootstrap          # Also destroy state backend
```

Maintains `.adp-undeploy-state.json` for resume. Retries failed phases up to 2×. Pass `--bootstrap` to include the Terraform state backend (prompts separately).

#### ADP-managed (`undeploy.yml`)

Dispatch via GitHub Actions UI or `gh workflow run undeploy.yml`. Inputs:
- `account_id` (required) — typed 12-digit account ID
- `dry_run` — plan-only, no destruction
- `skip_phases` — comma-separated phases to skip
- `include_bootstrap` — also destroy state backend (irreversible)

### Legacy path (retained)

```bash
./platform/scripts/deploy-all.sh --destroy          # LEGACY — use undeploy.sh instead
```

> **Note:** `deploy-all.sh --destroy` is retained for backward compatibility but is no longer the recommended path. It lacks the typed-account-ID gate, dry-run, resume, and the webhook-ingress phase. Prefer `undeploy.sh` or `undeploy.yml` for all new teardowns.

### Resources that survive by design

- **Terraform state backend** (S3 + DynamoDB) — only destroyed with `--bootstrap` / `include_bootstrap`
- **GitHub App secrets** (`adp/gh-app-*`, `adp/*/gh-app-*` in Secrets Manager) — manual browser step to delete apps
- **Webhook-ingress GitHub App secrets** (`adp/*/github-app/*` in Secrets Manager) — manual deletion
- **AWS-managed RDS secrets** (`rds!*`) — AWS handles their lifecycle

### Per-module destroy workflows

Individual module destroy workflows remain available for targeted teardowns:

| Workflow | Destroys | Confirm input |
|----------|----------|---------------|
| `.github/workflows/agent-context-infra-destroy.yml` | `modules/agent-context/terraform/` | `agent-context` |
| `.github/workflows/agent-factory-infra-destroy.yml` | `modules/agent-factory/infra/` | `agent-factory` |
| `.github/workflows/gateway-infra-destroy.yml` | `modules/gateway/infra/` + pre-cleanup (Ingress/ALB, S3, Secrets, CloudFront) | `gateway` |
| `.github/workflows/platform-infra-destroy.yml` | `platform/infra/` (run last, after all modules) | `platform` |

### Shared cleanup scripts

| Script | Purpose |
|--------|---------|
| `platform/scripts/empty-s3-buckets.sh` | Empties S3 buckets (versioned + non-versioned). Idempotent. |
| `platform/scripts/delete-ingress-and-wait.sh` | Deletes K8s Ingress, waits for ALB removal. Run before gateway destroy. |
| `platform/scripts/force-delete-secrets.sh` | Force-deletes secrets by prefix. Protects gh-app-*, github-app/*, and terraform-state-*. |
| `platform/scripts/bootstrap-destroy.sh` | Destroys Terraform state backend. Prompts for account ID. |

## Key Files Reference

| File | Purpose |
|------|---------|
| `platform/scripts/undeploy.sh` | Primary teardown entry point (typed-account-ID gate, dry-run, resume) |
| `.github/workflows/undeploy.yml` | ADP-managed teardown workflow (workflow_dispatch) |
| `platform/scripts/deploy-all.sh` | Automated deploy script (alternative to agent-driven deploy); `--destroy` flag is legacy |
| `platform/scripts/preflight-check.sh` | Environment validation |
| `platform/scripts/setup-org.sh` | Configure repo for your GitHub org |
| `modules/agent-factory/webhook-ingress/scripts/register-github-app.sh` | Register GitHub App (CLI fallback) + post-registration permission validation |
| `platform/scripts/bootstrap.sh` | Creates Terraform state backend |
| `platform/scripts/bootstrap-destroy.sh` | Destroys Terraform state backend (separate intentional step) |
| `platform/scripts/empty-s3-buckets.sh` | Idempotent S3 bucket emptier (versioned + non-versioned) |
| `platform/scripts/delete-ingress-and-wait.sh` | Pre-destroy: delete Ingress, wait for ALB cleanup |
| `platform/scripts/force-delete-secrets.sh` | Pre-destroy: force-delete secrets by prefix (protects gh-app-*) |
| `modules/gateway/scripts/deploy-frontend.sh` | Phase 6: build the SPA with the full VITE_* env from SSM, sync to S3 (excluding cfn-templates/*), upload the CFN role template (required for "Add AWS account"), invalidate CloudFront. Manual equivalent of gateway-deploy.yml's frontend job |
| `platform/scripts/wire-gateway-alb.sh` | Discover internal ALB → SSM; `--apply` re-applies gateway-infra with ALB vars + redeploys API GW stage (gateway second pass — switches API GW from MOCK to real `/{proxy+}` + `/auth/github` routes) |
| `modules/gateway/scripts/deploy-broker.sh` | Publish the real github-auth-broker Lambda code (terraform ships a 503 placeholder); required for GitHub login |
| `modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh` | Deploy the ARC-free webhook agent path: build agent-runtime image + package/upload webhook Lambda zip + terraform apply (NOT covered by deploy-all.sh) |
| `modules/agent-factory/webhook-ingress/scripts/register-github-app.sh` | CLI fallback for GitHub App registration (the primary path is the UI: Settings → Connections → "Set up GitHub App"); calls wire-github-app.sh; non-interactive flags; private-by-default visibility |
| `platform/infra/main.tf` | Shared platform Terraform |
| `platform/infra/modules/codebuild/` | CodeBuild projects (4 docker builds only) |
| `modules/gateway/README.md` | Gateway detailed documentation |
| `modules/gateway/Dockerfile` | Gateway container build |
| `modules/gateway/docker-compose.yml` | Local dev stack (no AWS needed) |
| `modules/gateway/infra/main.tf` | Gateway Terraform (15 modules) |
| `modules/gateway/k8s/deployment.yaml` | K8s deployment manifest |
| `modules/agent-factory/SETUP-GUIDE.md` | Agent factory setup guide |
| `modules/agent-factory/README.md` | Agent factory overview |
| `modules/agent-factory/infra/main.tf` | Agent factory Terraform |
| `environments/dev/` | Environment-specific Terraform vars |

## Non-Interactive Shell Rules

Always use non-interactive flags to avoid hanging:
- `cp -f`, `mv -f`, `rm -f`
- `terraform apply -auto-approve`, `terraform init -input=false`
- `apt-get -y`, `yum -y`
- Never use interactive editors (vim, nano) — use `cat >` or `sed`
- `kubectl apply` (already non-interactive)

## Issue-authoring convention (MANDATORY for every new issue)

Use the canonical [developer issue template](modules/agent-factory/rules/templates/developer-issue.md)
and [issue-authoring guide](modules/agent-factory/rules/agents/issue-authoring.md).
The GitHub `Developer task` template exposes the same body for manual filing.
The guide owns the requirements; do not create another inline template here.

Keep these sections in order: **The problem in plain terms**, **Description**,
**Impact analysis**, **Design**, **Deployment**, **Validation**. Use stable
acceptance IDs with action, expected result, evidence, phase and owner. Distinguish
implementation readiness from unresolved live access and deployment prerequisites.
Name the completion boundary explicitly; a merged PR is not live acceptance.

The guide also covers compact test-coverage issues, discovery, and run-only
versus build-and-run evaluations. Keep established AIDLC approval gates and
branch protections. Filing or editing an issue does not authorize deployment or
dispatch; use core-workflow's current dispatch mechanism when dispatch is requested.
