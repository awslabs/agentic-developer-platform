# Deploy ADP with an AI agent

The canonical instructions for deploying ADP with an AI coding agent (Claude
Code, Kiro, Cursor, …). Point your agent at this file — `AGENTS.md`, `CLAUDE.md`,
and `.kiro/steering/deployment.md` all redirect here so there is one source of
truth.

You are the deployment agent for this platform. Your job is to deploy it
end-to-end from a freshly cloned repo, keep the user informed, and only ask them
when you genuinely need their input. Read this file, then **follow
[`deploy-quickstart.md`](./deploy-quickstart.md)** — that is the authoritative,
verified procedure (maintained against real end-to-end runs). This file is the
agent-behavior layer on top of it.

## Your Behavior

- Run each step yourself. Do not ask the user to run commands — you run them.
- After each step, verify it succeeded before moving on, using the validation
  commands in deploy-quickstart.md / `deployment-manifest.md`.
- If something fails, diagnose it, attempt a fix, and retry. Only escalate after
  2 failed attempts. Never guess on destructive operations (destroy, force-delete).
- Keep the user informed with brief status between phases. Summarize results —
  don't dump raw command output.
- Maintain `.adp-deploy-state.json` (below); update after each phase. If it
  exists at startup, resume from the first non-complete phase. **A committed copy
  from a fresh clone is NOT a record of your deploy** — verify against real AWS
  state, don't trust its statuses.
- **Never commit `agent_learning/*.md`** — that directory is gitignored; an
  explicit `git add` bypasses the ignore and has caused drift before.

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
wired at the **END** (`register-github-app.sh`, Phase 8), after the infra it
points at exists. Gateway-only needs no GitHub at all. (An older version of this
file opened with a "Phase 0: GitHub setup" / `setup-org.sh` + 3 org-owned apps
step — that was the legacy ARC onboarding track and is **superseded**; do not run
it. ARC runners remain useful as a deterministic-pipeline *execution* model — see
`modules/agent-factory/SETUP-GUIDE.md` — but not as the deploy path.)

## The phases (follow deploy-quickstart.md for exact commands)

Each step is idempotent and re-runnable.

| Phase | What it does | Script | Needed for |
|------:|--------------|--------|-----------|
| 1 | Terraform state backend (S3 + DynamoDB) | `platform/scripts/bootstrap.sh` | All |
| 2 | Environment / preflight validation | `platform/scripts/preflight-check.sh` | All |
| 3 | Platform infra (VPC, EKS, ECR, IAM) | `deploy-all.sh` (or terraform) | All |
| 4 | Gateway infra (RDS, Redis, Cognito, CloudFront, S3) | `deploy-all.sh` | Gateway |
| 5 | Gateway backend on EKS (image → ECR → pods) | `deploy-all.sh` | Gateway |
| 6 | Frontend (React → S3 → CloudFront) | `deploy-all.sh` | Gateway |
| 6b | Gateway second pass — wire ALB (MOCK API GW → real routes) | `platform/scripts/wire-gateway-alb.sh --apply` | Gateway |
| 6c | Broker Lambda code (real GitHub-login handler) | `modules/gateway/scripts/deploy-broker.sh` | Login |
| 6d | Seed the first admin (org/user/role + Cognito claims) | `modules/gateway/scripts/bootstrap-admin.sh` | Login |
| 7 | Webhook agent stack + agent-runtime image (incl. warm pool + image-prepull) | `modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh` | Agents |
| 8a | **Bedrock model access — HUMAN STEP** (console, not CLI) | *(escalate to user — see below)* | Agents |
| 8b | Create + wire the GitHub App | `modules/agent-factory/webhook-ingress/scripts/register-github-app.sh <org>` | Agents |

> The webhook agent path (Phases 7–8) is **verified end-to-end** (account
> `919157478356`): GitHub mention → webhook → SQS → KEDA → worker → gateway →
> Bedrock → PR. Two fixes from that run are now on `main` and assumed here:
> the `execute-api` VPC endpoint is removed (PR #1304) and the agent uses the
> `us.anthropic.claude-opus-4-6-v1` inference profile. The warm pool +
> image-prepull DaemonSet (PR #1316, on by default in Phase 7) make agents start
> in ~10–15s instead of 1–2 min.

### ⚠️ Two steps you (the agent) CANNOT do — escalate to the user

These have no CLI path. When you reach them, **stop and ask the user to do them**,
then continue once they confirm:

1. **Bedrock model access (Phase 8a).** Claude Opus 4.6 (and any model the agent
   uses) must be enabled in the **Bedrock console → Model access** of the target
   account — this performs an AWS Marketplace `Subscribe` that the CLI cannot do.
   Until it's enabled, the agent **hangs silently after "Session initialized"**
   (the gateway returns HTTP 200 with an empty `text/event-stream`; no error).
   Verify it's done with:
   ```bash
   aws bedrock-runtime invoke-model --model-id us.anthropic.claude-opus-4-6-v1 \
     --body '{"anthropic_version":"bedrock-2023-05-31","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}' \
     --cli-binary-format raw-in-base64-out /dev/stdout   # a real JSON message = enabled
   ```
2. **GitHub App browser steps (Phase 8b).** `register-github-app.sh` and the
   per-repo "Link GitHub" install involve a browser/OAuth flow. Run the script,
   then hand the user the install URL.

**Critical — the "placeholder artifact" rule.** `deploy-all.sh` chains Phases
1–6 (incl. 6b) but **NOT 6c, 6d, 7, or 8.** Terraform ships *placeholders* for
things a push-triggered CI workflow normally publishes — the MOCK API Gateway
body, a 503 broker Lambda stub, `:latest` image refs, the webhook Lambda zip. A
fresh manual deploy fires none of those workflows, so `deploy-all.sh` alone
leaves you **without working GitHub login, a first admin, or the agent path.**
The stage-by-stage scripts (6c/6d/7/8) are the manual equivalents.
deploy-quickstart.md sequences them — don't skip them.

`deploy-all.sh` flags: `--gateway-only` (no GitHub), `--agent-context-only`,
`--skip-frontend`, `--local` (Docker instead of CodeBuild), `--destroy`.

## Silent-failure gotchas — read these before the agent path

deploy-quickstart.md has two `⚠️` sections under the webhook stack that document
failures with **no clear error message** (the worst kind for an agent):
- **"Two account-level blockers that make the agent hang silently after Session
  initialized"** — the `execute-api` VPC-endpoint DNS hijack (403 on every
  `/agent` call) and Bedrock model access / `global.` vs `us.` profile. Both have
  diagnose + fix commands. On latest `main` the VPC-endpoint fix is already in
  IaC; model access is the human step above.
- **"Slow first agent (1–2 min)"** — cold-node + image-pull latency; fixed by the
  warm pool + image-prepull (Phase 7, default-on). Not a failure, just slow.

If an agent run stalls at "Session initialized" with no progress, it is almost
always one of these two — check them before anything else.

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
    "frontend":         {"status": "pending"},
    "wire_alb":         {"status": "pending"},
    "broker":           {"status": "pending"},
    "bootstrap_admin":  {"status": "pending"},
    "webhook_ingress":  {"status": "pending"},
    "bedrock_model_access": {"status": "pending"},
    "github_app":       {"status": "pending"},
    "verification":     {"status": "pending"}
  },
  "outputs": {},
  "validation": {}
}
```

Status values: `pending`, `running`, `complete`, `failed`, `skipped`.

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

Probe the live stack — full commands in deploy-quickstart.md's verification
sections. The essentials:

```bash
CF=$(aws ssm get-parameter --name /adp/dev/gateway/cloudfront-domain --query Parameter.Value --output text)
curl -s -o /dev/null -w "frontend: %{http_code}\n" "https://$CF/"          # 200
curl -s -o /dev/null -w "health:   %{http_code}\n" "https://$CF/api/health" # 200
kubectl get pods -n adp-gateway -l app=bedrockgateway                       # Running
aws rds describe-db-instances --query 'DBInstances[?starts_with(DBInstanceIdentifier,`bedrockgw`)].DBInstanceStatus' --output text  # available
```

For the agent path: confirm the GitHub App is installed on the target org, then
`@mention` an agent (e.g. `@agent-developer …`) or apply the `developer` label on
an issue and confirm an agent-worker pod spawns (`kubectl get pods -n adp-agents`).

## Teardown

```bash
./platform/scripts/deploy-all.sh --destroy   # reverse order: agent-context → agent-factory → gateway → platform
./platform/scripts/bootstrap-destroy.sh      # separate step — deletes the state backend; prompts for account id
```

Survive by design: GitHub App credentials/Apps (delete manually), and the
Terraform state backend (only `bootstrap-destroy.sh` removes it).

## Non-Interactive Shell Rules

- `cp -f`, `mv -f`, `rm -f`; `terraform apply -auto-approve`,
  `terraform init -input=false`; `apt-get -y`, `yum -y`.
- Never use interactive editors (vim, nano) — use `cat >` or `sed`.
- The EKS cluster name is **`adp-dev-eks-cluster`**, not `adp-dev-eks`.

## When to Call the User

Break silence ONLY when:
- Confirming the target AWS account/profile (before Phase 1).
- **Bedrock model access (Phase 8a)** — ask the user to enable the model in the
  console; you cannot. Don't proceed to summon an agent until they confirm (else
  it hangs silently).
- The GitHub App browser steps in Phase 8b (`register-github-app.sh`).
- A required CLI tool is missing at preflight.
- A failure has retried twice without success — explain what failed, what you
  tried, and ask for guidance.

For troubleshooting (EKS nodes, CrashLoopBackOff, CloudFront 502, CodeBuild,
Cognito drift, resource imports), use the Troubleshooting Reference in `CLAUDE.md`
and deploy-quickstart.md — don't show it to the user; use it to fix issues
yourself.
