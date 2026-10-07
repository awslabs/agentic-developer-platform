# Deploy ADP with an AI agent

For interrupted runs and explicit recovery flags, see [deployment recovery](deployment-recovery.md).

The canonical instructions for deploying ADP with an AI coding agent (Claude
Code, Kiro, Cursor, …). Point your agent at this file — `AGENTS.md`, `CLAUDE.md`,
and `.kiro/steering/deployment.md` defer to this guide for deployment behavior.

You are the deployment agent for this platform. Your job is to deploy it
end-to-end from a freshly cloned repo, keep the user informed, and only ask them
when you genuinely need their input. Read this file, then **follow
[`deploy-quickstart.md`](./deploy-quickstart.md)** — that is the customer entry point for installation and upgrades. For manual
phase commands and troubleshooting, use [the phase reference](deployment-reference.md). This file is the
agent-behavior layer on top of it.

## Select the source and install mode

For a published GitHub Release, follow the quickstart's `./deploy.sh --release`
path; add `--update` only for an existing deployment. Confirm the AWS account
before launching. Do not re-run the manual phases after the full script
succeeds. Release checkout paths and receipts are printed by the launcher;
maintain and inspect the deployment journal in that selected checkout, not a
stale journal in the original working tree. An update never becomes a fresh
install automatically.

Prefer the published-release path for customer installations. It creates an
isolated checkout, runs `platform/scripts/prepare-release-config.py` to select
portable defaults, and sets `ADP_PORTABLE_RELEASE_CONFIG=true` automatically.
The bare source launcher does not perform that preparation.

For a fresh installation from a reviewed, unpublished commit, first create a
new isolated checkout in a private directory. Before adding any target-specific
overrides or retained-resource import blocks, prepare its portable inputs:

```bash
# Run only in the new isolated source checkout, before target customization:
python3 platform/scripts/prepare-release-config.py \
  --root "$PWD" --env dev --region us-east-1

# Review portable inputs and apply any required target-specific overrides first.
ADP_PORTABLE_RELEASE_CONFIG=true ./deploy.sh \
  --aws-profile customer --env dev --region us-east-1
```

The preparation script replaces the selected environment's platform, gateway
and webhook `.tfvars` files. It refuses corresponding JSON overlays and archives
the original inputs in `original-environment-config` beside the checkout (existing
backups are not overwritten). Use a dedicated private parent directory. Never
run it casually over existing customer configuration or rerun it after adding
target overrides. These source commands are for a new install, not a reset of an
existing deployment's configuration.

For a direct source upgrade, retain the reviewed target-specific configuration
and follow [advanced upgrades](platform_upgrades.md):

```bash
./deploy.sh --aws-profile customer --env dev --region us-east-1 --update
```

The source path uses that checkout directly and writes account configuration
into it. Record the reviewed commit and target privately; do not describe an
unreleased commit as a published release. The full launcher handles bootstrap;
do not run the manual phases again. `--dry-run` checks identity and selected
prerequisites, but does not create a Terraform plan or establish readiness.

### Check environment overlays before provisioning

Inspect `environments/<environment>/modules/gateway.tfvars` and
`webhook-ingress.tfvars` (including JSON overlays) for account-specific Task API
bindings, API URLs, image digests, probe registry identifiers, activation flags
and worker qualification attestations. The unprepared checked-in `dev` overlay includes
settings for an existing platform deployment; selecting `--env dev` in a new
account does not make those settings portable. Published release deployment
replaces these overlays with `config/release-defaults` automatically; source
deployment requires the preparation above. Rewriting account IDs alone does
not create the referenced resources or establish runtime qualification.

For a new account, use the reusable modules' documented portable defaults and
the [worker preparation stage](../security/terraform-worker-rollout.md#stages)
until account-local dependencies and acceptance checks exist. Do not copy
activation or readiness attestations from another environment to make a plan
pass. Record which Task API/protected-worker features remain inactive; a core
install does not establish their live acceptance. For an upgrade, preserve the
observed live configuration through the documented update path instead of
resetting an existing installation to fresh-account defaults.

### Reinstall after teardown

A teardown can retain the backend, GitHub credentials and their encryption key,
and optionally a VPC for independent resources. Reinstallation therefore requires
checking the teardown retention record, current AWS resources and each module's
Terraform state before provisioning. Preserve unrelated resources and the
existing credential values. Resources still in state can be reused; retained
resources removed from state need reviewed imports into their current Terraform
addresses before their module applies. Do not assume the launcher automatically
adopts them, delete them to resolve a name collision, or create replacement
secrets. Some module imports require platform dependencies to exist first.

Use the fresh-install command only after the intended teardown is verified.
For a partial deployment that must be preserved, use the upgrade/recovery path.
Record every manual import or workaround privately so it can become a script
fix. Do not discard state or use a fresh install to bypass an upgrade refusal.

## Your Behavior

- Run each step yourself. Do not ask the user to run commands — you run them.
- After each step, verify it succeeded before moving on, using the validation
  commands in deployment-reference.md / `deployment-manifest.md`.
- If something fails, diagnose it, attempt a fix, and retry. Only escalate after
  2 failed attempts. Never guess on destructive operations (destroy, force-delete).
- Keep the user informed with brief status between phases. Summarize results —
  don't dump raw command output.
- Maintain `.adp-deploy-state.json` (below); update after each phase. If it
  exists at startup, reconcile it with live resources and the checkpoint journal
  before choosing the documented recovery flags. Do not automatically replay
  completed phases. **A committed copy
  from a fresh clone is NOT a record of your deploy** — verify against real AWS
  state, don't trust its statuses.
- Agent factory is required for a full platform deployment. Webhook workers
  alone do not establish that the separate factory module is installed.
  Verify factory state and worker readiness before reporting full completion.
  Full `--update` runs include a missing factory through the saved-plan gate;
  explicit scope flags describe partial maintenance only.
- **Never commit `agent_learning/*.md`** — that directory is gitignored; an
  explicit `git add` bypasses the ignore and has caused drift before.
- **Never make a Lambda publicly invocable.** Do not create unauthenticated
  Function URLs or grant world-accessible Lambda invocation permissions, including
  for temporary test fixtures. Use authenticated, explicitly scoped access.
  A need for browser-test content does not permit an exception, and account
  security automation that removes public permissions must not be bypassed.

## Confirm the target account first

Everything keys off the account `aws sts get-caller-identity` resolves to (via
the active `AWS_PROFILE`). Before Phase 1, show the account + ARN and get the
user's confirmation:

```bash
aws configure list-profiles                # show options
export AWS_PROFILE=<chosen>                 # skip for default
export AWS_REGION=us-east-1
aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output table
```

**There is no upfront GitHub setup.** For the agent (webhook) path, GitHub is
wired at the **END** (Phase 8b), after the infra it points at exists.
Gateway-only needs no GitHub at all. The **primary path** is the UI flow:
the Phase-6d `platform_admin` opens Settings → Connections → "Set up GitHub App"
(manifest flow). The CLI fallback (`register-github-app.sh`) remains for
headless environments. (An older version of this file opened with a "Phase 0:
GitHub setup" / `setup-org.sh` + 3 org-owned apps step — that was the legacy ARC
onboarding track and is **superseded**; do not run it. ARC runners remain useful
as a deterministic-pipeline *execution* model — see
`modules/agent-factory/SETUP-GUIDE.md` — but not as the deploy path.)

## The phases (manual commands are in deployment-reference.md)

Use the full launcher for normal installation. The table is in execution order;
phase labels are retained for cross-references to the manual guide. The script's
printed step numbers are historical labels, not a reliable ordering contract.
Use [deployment recovery](deployment-recovery.md) for retries and checkpoints.

| Phase | What it does | Script | Needed for |
|------:|--------------|--------|-----------|
| 2 | Environment / preflight validation | `platform/scripts/preflight-check.sh` | All |
| 8a | Bedrock first-use registration, agreements and readiness | `platform/scripts/enable-bedrock-models.sh` (automatic before provisioning) | Default runtime models |
| 1 | Terraform state backend (S3 + DynamoDB) | `deploy-all.sh` bootstrap stage; `platform/scripts/bootstrap.sh` for manual setup | All |
| 3 | Platform infra (VPC, EKS, ECR, IAM) | `deploy-all.sh` | All |
| 4 | Gateway infra (RDS, Redis, Cognito, CloudFront, S3) | `deploy-all.sh` | Gateway |
| 5 | Gateway backend and orchestration engine (build/publish source image, deploy and sync) | `deploy-all.sh` | Gateway |
| 6b | Wire internal ALB to API Gateway and CloudFront | `deploy-all.sh`; `platform/scripts/wire-gateway-alb.sh --apply` for manual wiring | Gateway |
| 6c | Publish broker Lambda code | `modules/gateway/scripts/deploy-broker.sh` | Login |
| 6d | Seed the first admin (skipped on update) | `modules/gateway/scripts/bootstrap-admin.sh` | Login |
| 7 | Webhook stack, KEDA and agent-runtime image | `modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh` | Agents |
| 7b | Separate agent-factory infra, agent gateway and chat agent | `deploy-all.sh` factory stage | Full platform |
| 7c | Agent context | `deploy-all.sh` context stage | Optional; enabled explicitly |
| — | Finalize network policy and gateway reconciliation | `deploy-all.sh` finalize stage | Updates |
| 6 | Publish frontend and account-connection templates after module deployment | `modules/gateway/scripts/deploy-frontend.sh` | Gateway |
| — | Verify deployed state and default model invocations | `deploy-all.sh`, then launcher verification | Selected scope |
| 8b | Connect/install the GitHub App after deployment | **UI:** Settings → Connections → "Set up GitHub App". **CLI fallback:** `register-github-app.sh <org>` | GitHub agents |

Webhook ingress installs KEDA before the factory stage creates resources that
need its CRDs. Frontend publication runs near the end so account-connection
templates use the completed infrastructure. Superplane is a separate optional
module; a core ADP install does not require it.

Model defaults must come from the selected checkout's runtime configuration and
the Bedrock readiness helper, not an older deployment report or a model name
copied from this guide. Historical success does not establish acceptance for the
current account: verify the running deployment and an authorized agent smoke test.

### Phase 8a is CLI-automated

`platform/scripts/enable-bedrock-models.sh` prepares Anthropic first-use
registration when needed and accepts missing Marketplace agreements. It reads
required defaults from the runtime sources and checks authorization, entitlement,
agreement and region availability. Both deployment entrypoints verify bounded
model invocations before reporting success. See
[Bedrock readiness during deployment](./bedrock-first-run.md).

Use an operator-supplied organization form through `--anthropic-use-case FILE`
(on `deploy.sh`) or `ADP_BEDROCK_USE_CASE_FILE`. Never invent registration details
or overwrite an existing submission. Effective organization-inherited access
needs no per-account submission. If the account needs registration and no form
is supplied, request the real organization details. Private Marketplace policies
must also permit the model; the helper reports restrictions without changing them.

### ⚠️ One step you (the agent) CANNOT do — escalate to the user

**GitHub App browser steps (Phase 8b).** The primary path is the UI: the
   `platform_admin` opens Settings → Connections → "Set up GitHub App" (manifest
   flow). For headless environments, fall back to `register-github-app.sh`. Both
   involve a browser/OAuth flow. Hand the user the install URL afterward if the
   App wasn't installed during the manifest flow.

**Critical — the "placeholder artifact" rule.** Terraform ships *placeholders*
for things a push-triggered CI workflow normally publishes — the MOCK API
Gateway body, a 503 broker Lambda stub, `:latest` image refs, the webhook
Lambda zip. A fresh manual deploy fires none of those workflows, so each
placeholder must be replaced by its publish script. If a fresh gateway plan
requires an ECR image that has not been built yet, treat that as an installer
ordering defect. The real selected-source image must exist before a plan that
resolves it; do not satisfy the dependency with a dummy image or report success
while the plan is blocked. `deploy-all.sh` chains
bootstrap, platform, gateway, broker, first-admin bootstrap, webhook, the
separate factory, optional context, finalization, frontend and verification.
Bedrock readiness is automated; GitHub App browser wiring (Phase 8b) remains
manual when needed. When deploying module-by-module instead, use the manual
reference and account for every selected module and runtime artifact.

`deploy-all.sh` flags: `--gateway-only` (no GitHub), `--agent-context-only`,
`--skip-frontend`, `--skip-broker`, `--skip-admin-bootstrap`,
`--skip-webhook-ingress`, `--local` (Docker instead of CodeBuild).
Use `platform/scripts/undeploy.sh` for teardown, as described below.

Worker security prerequisites are Terraform-managed across environments. See
[the worker rollout procedure](../security/terraform-worker-rollout.md) for
preparation, activation, admission pause and legacy-role retirement settings.
Preparation defaults on; activation requires compatible runtimes and live
acceptance. The standard deploy includes the tick's preparation pass after
webhook-owned resource identifiers become available.

## Silent-failure gotchas — read these before the agent path

deployment-reference.md has two `⚠️` sections under the webhook stack that document
failures with **no clear error message** (the worst kind for an agent):
- **"Two account-level blockers that make the agent hang silently after Session
  initialized"** — the `execute-api` VPC-endpoint DNS hijack (403 on every
  `/agent` call) and Bedrock model access / `global.` vs `us.` profile. Both have
  diagnose + fix commands. On latest `main` the VPC-endpoint fix is already in
  IaC; model access is automated by `enable-bedrock-models.sh` (Phase 8a above).
- **"Slow first agent (1–2 min)"** — cold-node + image-pull latency; fixed by the
  warm pool + image-prepull (Phase 7, default-on). Not a failure, just slow.

If an agent run stalls at "Session initialized" with no progress, check these
conditions along with current worker logs, queue delivery and gateway health.

## Deployment State

Maintain `.adp-deploy-state.json` in the repo root. Create at start, update after
each phase:

```json
{
  "environment": "dev",
  "account_id": "",
  "aws_profile": "",
  "github_org": "",
  "scope": "",
  "phases": {
    "bootstrap":        {"status": "pending"},
    "preflight":        {"status": "pending"},
    "platform_infra":   {"status": "pending"},
    "gateway_infra":    {"status": "pending"},
    "gateway_backend":  {"status": "pending"},
    "wire_alb":         {"status": "pending"},
    "broker":           {"status": "pending"},
    "bootstrap_admin":  {"status": "pending"},
    "webhook_ingress":  {"status": "pending"},
    "agent_factory":    {"status": "pending"},
    "agent_context":    {"status": "pending"},
    "finalize":         {"status": "pending"},
    "frontend":         {"status": "pending"},
    "bedrock_model_access": {"status": "pending"},
    "github_app":       {"status": "pending"},
    "verification":     {"status": "pending"}
  },
  "outputs": {},
  "validation": {}
}
```

Status values: `pending`, `running`, `complete`, `failed`, `skipped`.
Mark optional or scope-excluded phases `skipped` with the reason. This private
agent summary supplements the scripts' checkpoint journal; editing it does not
change checkpoint eligibility. Never commit either journal or raw state/plan
files, which can contain deployment identities and secrets.

## What This Repo Contains

| Module | Path | Purpose |
|--------|------|---------|
| Gateway | `modules/gateway/` | Multi-tenant Bedrock proxy (FastAPI + React) |
| Agent Factory | `modules/agent-factory/` | Autonomous agents — webhook (GitHub), Conversational Gateway (Slack/WS/CLI), self-hosted ARC runners |
| Agent Context | `modules/agent-context/` | Code intelligence via one MCP endpoint (OpenViking, Sourcebot, DeepWiki, LiteLLM). `--agent-context-only` |
| Domain Apps | `modules/domain-apps/` | Domain capability packs (cyber/malware is the first) |
| MCP Hub | `modules/harness/mcp-hub/` | MCP tools surface of the harness (in progress; see `ARCHITECTURE.md`) |
| User Services | `modules/user-services/` | Per-user products (vault, knowledge repo, …); design only |

Shared infra: `platform/infra/` (VPC, EKS, ECR, IAM, the 4 docker-build
CodeBuild projects).

## Verification (after deploy)

Probe the live stack — full commands in deployment-reference.md's verification
sections. The essentials:

```bash
ENVIRONMENT=dev # use the confirmed environment
CF=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/cloudfront-domain" --query Parameter.Value --output text)
curl -s -o /dev/null -w "frontend: %{http_code}\n" "https://$CF/"          # 200
curl --fail --silent --show-error "https://$CF/api/health" \
  | python3 -c 'import json,sys; assert json.load(sys.stdin).get("status") == "healthy"'
kubectl get pods -n adp-gateway -l app=bedrockgateway                       # Running
aws rds describe-db-instances --query 'DBInstances[?starts_with(DBInstanceIdentifier,`bedrockgw`)].DBInstanceStatus' --output text  # available
```

Verify factory state and worker readiness separately from webhook ingress.
An HTTP 200 alone is insufficient: the frontend fallback can mask a broken API.

For the agent path, obtain explicit authorization for an external GitHub/model
smoke test. Confirm the GitHub App is installed on the target org, then
`@mention` an agent (e.g. `@agent-developer …`) or apply the `developer` label on
an issue and confirm an agent-worker pod spawns (`kubectl get pods -n adp-agents`).

## Teardown

Use `platform/scripts/undeploy.sh --dry-run` to inspect the target before an
explicitly authorized teardown. Follow the account-confirmation gate and
[teardown reference](deployment-reference.md#teardown). GitHub App credentials
and the Terraform backend survive by default; remove them only when separately
requested. Do not substitute the legacy `deploy-all.sh --destroy` path.

## Non-Interactive Shell Rules

- `cp -f`, `mv -f`, `rm -f`; `terraform apply -auto-approve`,
  `terraform init -input=false`; `apt-get -y`, `yum -y`.
- Never use interactive editors (vim, nano) — use `cat >` or `sed`.
- Derive names from the confirmed environment: the default cluster is
  `adp-${ENVIRONMENT}-eks-cluster` (for example, `adp-dev-eks-cluster`).

## When to Call the User

Keep providing progress updates. Request user input when:
- Confirming the target AWS account/profile (before Phase 1).
- **Bedrock model access (Phase 8a)** — request real organization registration
  details only if first-use registration is needed and no form was supplied.
  Escalate organization Marketplace restrictions that the deployment identity
  cannot resolve. Use the helper's readiness and invocation checks before
  summoning an agent.
- The GitHub App browser steps in Phase 8b (UI flow: Settings → Connections →
  "Set up GitHub App"; or CLI fallback `register-github-app.sh`).
- A required CLI tool is missing at preflight.
- A failure has retried twice without success — explain what failed, what you
  tried, and ask for guidance.

For troubleshooting (EKS nodes, CrashLoopBackOff, CloudFront 502, CodeBuild,
Cognito drift, resource imports), use the Troubleshooting Reference in `CLAUDE.md`
and deployment-reference.md — don't show it to the user; use it to fix issues
yourself.
