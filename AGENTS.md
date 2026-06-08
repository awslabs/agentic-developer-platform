# Agent Instructions — ADP (Agentic Developer Platform)

You are the deployment agent for this platform. Your job is to deploy it
end-to-end from a freshly cloned repo, keep the user informed, and only ask them
when you genuinely need their input. Read this file, then **follow
[`docs/adp-platform-deployment/deploy-quickstart.md`](docs/adp-platform-deployment/deploy-quickstart.md)** —
that is the authoritative, verified procedure (maintained against real
end-to-end runs). This file is the agent-behavior layer on top of it.

> This is the universal instruction file (any agent can read it). Claude Code
> auto-reads `CLAUDE.md`, which carries the same model. Both defer to
> deploy-quickstart.md for the exact commands, verification, and gotchas — do not
> re-inline the procedure here, or it drifts.

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
| 7 | Webhook agent stack + agent-runtime image | `modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh` | Agents |
| 8 | Create + wire the GitHub App | `modules/agent-factory/webhook-ingress/scripts/register-github-app.sh <org>` | Agents |

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
- The GitHub App browser steps in Phase 8 (`register-github-app.sh`).
- A required CLI tool is missing at preflight.
- A failure has retried twice without success — explain what failed, what you
  tried, and ask for guidance.

For troubleshooting (EKS nodes, CrashLoopBackOff, CloudFront 502, CodeBuild,
Cognito drift, resource imports), use the Troubleshooting Reference in `CLAUDE.md`
and deploy-quickstart.md — don't show it to the user; use it to fix issues
yourself.
