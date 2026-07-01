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
   the END (`register-github-app.sh`), and gateway-only needs no GitHub at all.
2. **Maintain `.adp-deploy-state.json`** (see Deployment State above): update it
   after each phase; on startup, resume from the first non-complete phase.
   Note: a committed copy from a fresh clone is NOT a record of your deploy —
   verify against real AWS state, don't trust its statuses.
3. **Keep the user informed** between phases with brief status; only stop for
   genuine input (AWS account choice, Bedrock model access in the console, and
   the GitHub App browser install — the two human steps near the end; see the
   phase numbering in deploy-quickstart.md).
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
- Wrong VITE_API_URL during build. Rebuild with `VITE_API_URL="/api/gateway" npm run build`
- Stale cache: `aws cloudfront create-invalidation --distribution-id <id> --paths "/*"`

### CodeBuild fails
- Only 4 docker-build projects use CodeBuild (gateway-build, chat-agent, agent-gateway, arc-runner). They are Terraform-managed in `platform/infra/modules/codebuild/`. Everything else (terraform apply, npm build, kubectl apply) runs directly on the ARC runner.
- Check logs: `aws codebuild batch-get-builds --ids <build-id> --query 'builds[0].logs.deepLink' --output text`
- IAM propagation: if role was just created, wait 15 seconds and retry

---

## Destroy / Teardown

### Per-module destroy workflows

Each module has a destroy workflow mirroring its apply workflow. All require a typed `confirm` input matching the module name:

| Workflow | Destroys | Confirm input |
|----------|----------|---------------|
| `.github/workflows/agent-context-infra-destroy.yml` | `modules/agent-context/terraform/` | `agent-context` |
| `.github/workflows/agent-factory-infra-destroy.yml` | `modules/agent-factory/infra/` | `agent-factory` |
| `.github/workflows/gateway-infra-destroy.yml` | `modules/gateway/infra/` + pre-cleanup (Ingress/ALB, S3, Secrets, CloudFront) | `gateway` |
| `.github/workflows/platform-infra-destroy.yml` | `platform/infra/` (run last, after all modules) | `platform` |

### Full teardown

```bash
./platform/scripts/deploy-all.sh --destroy
```

Runs the per-module destroys in reverse deploy order: agent-context, agent-factory, gateway, platform. Prompts for `yes` confirmation. Uses the shared cleanup scripts for non-Terraform resources.

### Bootstrap destroy (separate step)

```bash
./platform/scripts/bootstrap-destroy.sh
```

Deletes the Terraform state backend (S3 bucket + DynamoDB lock table). Requires typing the AWS account ID to confirm. **Not called by the orchestrator** — only run after all module destroys have succeeded and you've verified everything is gone.

### Resources that survive by design

- **GitHub App secrets** (`adp/gh-app-*` in Secrets Manager) — manual browser step to delete apps
- **Terraform state backend** — only `bootstrap-destroy.sh` can delete it
- **AWS-managed RDS secrets** (`rds!*`) — AWS handles their lifecycle

### Shared cleanup scripts

| Script | Purpose |
|--------|---------|
| `platform/scripts/empty-s3-buckets.sh` | Empties S3 buckets (versioned + non-versioned). Idempotent. |
| `platform/scripts/delete-ingress-and-wait.sh` | Deletes K8s Ingress, waits for ALB removal. Run before gateway destroy. |
| `platform/scripts/force-delete-secrets.sh` | Force-deletes secrets by prefix. Protects gh-app-* and terraform-state-*. |
| `platform/scripts/bootstrap-destroy.sh` | Destroys Terraform state backend. Prompts for account ID. |

## Key Files Reference

| File | Purpose |
|------|---------|
| `platform/scripts/deploy-all.sh` | Automated deploy script (alternative to agent-driven deploy) |
| `platform/scripts/preflight-check.sh` | Environment validation |
| `platform/scripts/setup-org.sh` | Configure repo for your GitHub org |
| `platform/scripts/create-github-apps.sh` | Create GitHub Apps + store creds + install on repos |
| `platform/scripts/bootstrap.sh` | Creates Terraform state backend |
| `platform/scripts/bootstrap-destroy.sh` | Destroys Terraform state backend (separate intentional step) |
| `platform/scripts/empty-s3-buckets.sh` | Idempotent S3 bucket emptier (versioned + non-versioned) |
| `platform/scripts/delete-ingress-and-wait.sh` | Pre-destroy: delete Ingress, wait for ALB cleanup |
| `platform/scripts/force-delete-secrets.sh` | Pre-destroy: force-delete secrets by prefix (protects gh-app-*) |
| `modules/gateway/scripts/deploy-frontend.sh` | Phase 6: build the SPA with the full VITE_* env from SSM, sync to S3 (excluding cfn-templates/*), upload the CFN role template (required for "Add AWS account"), invalidate CloudFront. Manual equivalent of gateway-deploy.yml's frontend job |
| `platform/scripts/wire-gateway-alb.sh` | Discover internal ALB → SSM; `--apply` re-applies gateway-infra with ALB vars + redeploys API GW stage (gateway second pass — switches API GW from MOCK to real `/{proxy+}` + `/auth/github` routes) |
| `modules/gateway/scripts/deploy-broker.sh` | Publish the real github-auth-broker Lambda code (terraform ships a 503 placeholder); required for GitHub login |
| `modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh` | Deploy the ARC-free webhook agent path: build agent-runtime image + package/upload webhook Lambda zip + terraform apply (NOT covered by deploy-all.sh) |
| `modules/agent-factory/webhook-ingress/scripts/register-github-app.sh` | Create + wire the GitHub App (calls wire-github-app.sh); non-interactive flags; private-by-default visibility |
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

Every issue you file (or edit to complete) **must** include these five top-level sections, in this order, before any secondary content:

### 1. `## Description`
What we're trying to achieve and why. One paragraph stating the goal in plain language, one paragraph on motivation (the problem this solves or the gap it closes). No implementation detail here — a product manager should be able to understand this section without reading the rest.

### 2. `## Impact analysis`
Who benefits, who's impacted, what breaks if a bug slips through. Must include:
- **Who benefits** — which user types / personas / use cases get unblocked
- **Who's impacted** — billing, security, support, ops surfaces that this touches
- **What breaks if this ships with a bug** — worst-case scenarios as a table (bug class → blast radius). Forces thinking about failure modes before coding starts.
- **Cost / quota footprint** — new AWS resources, new DB rows, new compute. Explicit about what's bounded vs. unbounded.

### 3. `## Design`
The concrete technical shape of the solution. Must include, when relevant:
- **Database schema** — new tables, new columns, migrations needed (or explicit "no migrations"), FK/index/constraint decisions
- **API contracts** — endpoint paths, request body JSON, response body JSON, error cases, HTTP codes
- **File-level changes** — list of files to create + list of files to modify, with full paths
- **Integration points** — which existing services/tables/endpoints this piece plugs into and reuses (or explicitly forks)
- **Tenant isolation / authz** — how scoping is enforced (especially for multi-tenant features)
- **Reuse table** — "X lives in module Y, we call it here" to prevent duplicate implementations

Goal: a developer (human or agent) should be able to implement this issue without guessing where anything goes or duplicating existing code.

### 5. `## Deployment`
What must happen after the PR merges for the change to be effective. Must include:
- **Automatic on merge** — which CI workflows fire (`gateway-deploy.yml`, `agent-worker-image.yml`, `webhook-ingress-deploy.yml`, etc.), what each produces, typical timing
- **Explicit NOT-triggered** — what won't rebuild/redeploy that someone might expect (e.g. "no agent-runtime image rebuild needed; code is gateway-side only"). Prevents agents from trying to touch infra they don't need.
- **Manual follow-ups** — Terraform apply, migration workflow, secret seeding, IAM approvals, etc., each with the exact command or workflow name
- **Environment coverage** — "ships to dev on merge; prod promotion is a manual `workflow_dispatch`"
- **Rollback plan** — how to revert if something goes wrong

### 6. `## Validation`
How to verify the change is working. Must include:
- **Unit tests** to add (one bullet per test, stating what it proves)
- **Integration tests** — what runs in CI vs. what a human runs locally
- **Smoke test** — the one end-to-end check an operator runs after deploy to confirm it works. Should be a concrete command or URL, not a vague "verify the feature works."
- **Regression checks** — existing callers/flows that must keep working

### Secondary sections (use as needed)
After the five mandatory sections, the issue may include: `## Scope`, `## Non-goals`, `## Dependencies`, `## Acceptance`, `## References`, `## Related issues`. These are optional supplements, not replacements for the five mandatory sections.

### Enforcement

- **When filing an issue**: include all five sections from the first draft. Empty/placeholder sections are a code smell — if you don't know the design yet, file the issue as a *spike* (label: `architect`) and the design section explicitly says "spike — produces design note."
- **When reviewing an existing issue** (before labeling it to trigger an agent): if Description / Impact analysis / Design / Deployment / Validation are missing, add them before labeling. An agent without a Design section will invent one; without a Deployment section will not know whether Terraform must apply; without a Validation section will skip writing meaningful tests; without Impact analysis it will miss failure modes that should have been surfaced as test cases.
- **For EPIC-level issues** that aren't directly implementable: the five-section rule still applies but Deployment/Validation can be "see child issues."
- **For doc-only issues**: Deployment is "merge the PR, no service redeploys"; Validation is "PR review confirms the doc reads correctly."
- **For test-coverage issues** (adding tests for a feature that already shipped): use the template below instead of the five-section rule — the generic template doesn't fit because Deployment is trivial and Validation IS the work.

### Template for test-coverage issues

Test-coverage issues follow this shape instead of the five-section rule:

```markdown
## Description
One paragraph: what feature is this adding tests for, and why it matters (usually: feature shipped without coverage; first regression would have nothing to catch it).

## Why now
Trigger for filing — "PR #N shipped feature X with only backend tests", "audit showed UI component Y has 0% coverage", "regression bug #Z would have been caught", etc.

## What the feature does (brief recap)
2-3 sentences recapping the user-facing flow + key endpoints/components so the agent doesn't need to spelunk the parent issue.

## Tests to add
Grouped by test layer (E2E, unit, integration, component, etc.) with ONE bullet per test. Each bullet states what the test proves — not how to write it. Examples:
- "Install flow happy path: click button → POST fired → redirect URL has `state=` param"
- "Disconnect: click Disconnect → DELETE fired → card removed from list on refetch"
- "Non-admin sees Disconnect button hidden"

## Validation
- [ ] All specs pass in CI
- [ ] Coverage for the specific files ≥ N% (e.g. ≥85%)
- [ ] No flaky tests (retry threshold must not be raised to pass)

## Non-goals
Tests explicitly NOT in scope — usually visual regression, load testing, real-API integration, etc.

## Files to create
List of new test files with full paths.

## References
Parent feature issue, implementation PR, any post-merge fix PRs, existing test harness file to follow as a pattern.
```

This template drops Impact Analysis (tests don't change production behavior), drops Design (the design is "write tests for the named layers"), drops Deployment ("merge the PR; CI runs the tests"). Adds "What the feature does (brief recap)" so the agent has enough context without reading the parent issue end-to-end.

### Why this matters

Agents that implement issues (hosted `developer` flow) have no context beyond the issue body. Missing design detail → agent invents a design, often wrong (example: PR #449 introduced a table-name collision we spent two PRs recovering from because the issue didn't say "check for name collisions with existing `admin/models.py`"). Missing deployment detail → agent assumes wrong workflow fires, operator finds out days later when nothing works (example: migration 008 never auto-ran because nothing told the agent about `run-gateway-migrations.yml`). Missing validation → agent declares success on a broken feature. Missing impact analysis → agent ships a change that breaks a surface it didn't know existed.

Five explicit sections eliminate all four failure modes at the planning stage, before the agent ever starts coding.
