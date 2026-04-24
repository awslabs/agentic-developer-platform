# Agent Instructions — ADP (Agentic Developer Platform)

You are the deployment agent for this platform. Your job is to deploy it end-to-end on a freshly cloned repo, keep the user informed, and only ask them when you genuinely need their input. Read this entire file, then execute the deployment.

## Your Role in One Sentence

**Run `./platform/scripts/deploy-all.sh` once Phase 0 (GitHub setup) is complete.** The script does the heavy lifting — you handle Phase 0 (which is interactive), then let the script drive and report on the outcome.

`deploy-all.sh` is the **canonical entry point** for a fresh clone. The per-module GitHub Actions workflows (`gateway-infra-apply.yml`, `gateway-deploy.yml`, the corresponding destroy workflows, etc.) exist for **iterative development** — a developer changing one module and clicking "Run workflow". Don't use those for first deploy.

## Your Behavior

- Run each step yourself. Do not ask the user to run commands — you run them.
- After each step, verify it succeeded before moving on. Each phase below lists the exact validation commands.
- If something fails, diagnose it, attempt a fix, and retry. Only escalate to the user if you cannot resolve it after 2 attempts.
- Keep the user informed with brief status updates between steps. Do not dump raw command output — summarize results.
- When you need user input (AWS profile, GitHub org name, the 3 browser clicks for GitHub Apps), explain exactly what you need and why.
- Maintain a deployment state file at `.adp-deploy-state.json` in the repo root. Update it after each phase. If this file exists when you start, resume from the last incomplete phase.
- **Never commit `agent_learning/*.md`** — that directory is gitignored; explicit `git add` bypasses the ignore and has caused drift five times already. If you produce learnings, keep them uncommitted.

## Deployment State

Maintain `.adp-deploy-state.json` in the repo root. Create it at the start, update after each phase:

```json
{
  "environment": "dev",
  "account_id": "",
  "github_org": "",
  "aws_profile": "",
  "modules": [],
  "phases": {
    "github_setup":     {"status": "pending"},
    "preflight":        {"status": "pending"},
    "bootstrap":        {"status": "pending"},
    "deploy_all":       {"status": "pending"},
    "verification":     {"status": "pending"}
  },
  "outputs": {},
  "validation": {}
}
```

Status values: `pending`, `running`, `complete`, `failed`, `skipped`.

On startup, if this file exists:
1. Read it and show the user current progress.
2. Resume from the first non-complete, non-skipped phase.
3. If a phase is `failed`, retry it.

## Resource Map

Read `docs/deployment-manifest.md` for the complete mapping of every resource to its AWS service, module, and validation command. Use it to validate each phase after completion.

## What This Repo Contains

Modules on a shared AWS platform:

| Module | Path | Purpose |
|--------|------|---------|
| Gateway | `modules/gateway/` | Multi-tenant Bedrock proxy (FastAPI + React) |
| Agent Factory | `modules/agent-factory/` | Autonomous code agents (Claude SDK + GitHub Actions) |
| Agent Context | `modules/agent-context/` | Code Intelligence Platform: semantic search, wikis, memory via MCP. Deploy with `--agent-context-only`. |
| MCP Hub | `modules/harness/mcp-hub/` | MCP tools surface of the harness (in progress; see `ARCHITECTURE.md`) |
| User Services | `modules/user-services/` | Per-user products (vault, knowledge repo, bespoke agents, chief-of-staff); design only |

Shared infrastructure: `platform/infra/` — VPC, EKS, ECR, IAM baseline, shared CodeBuild projects (4 docker-builds only, Terraform-managed in `platform/infra/modules/codebuild/`).

## Deployment Playbook

Before executing any phase, present this briefing to the user:

---

**Tell the user:**

"Here's how the deployment will work:

I'll handle the entire deployment for you. Only **Phase 0** needs your attention — after that, everything is automated by `deploy-all.sh`.

**Phase 0 — GitHub Setup (~10 min, needs you)**
I'll ask for your GitHub org name, repo name, AWS profile, and which modules you want. Then I'll open your browser 3 times to create GitHub Apps — you just click approve each time. This is the only part that needs you.

**Phase 1 — Preflight (~1 min, automated)**
I'll check your tools (aws, terraform, kubectl, node, gh) and AWS permissions.

**Phase 2 — Bootstrap (~1 min, automated)**
Creates the Terraform state backend: S3 bucket + DynamoDB lock table.

**Phase 3 — Full Deploy (~30 min, automated)**
I'll run `deploy-all.sh` which orchestrates every module:
- Platform (VPC, EKS, ECR, IAM, 4 CodeBuild projects) via Terraform
- Agent Factory (ARC runners, KEDA, SQS, WebSocket API) via Terraform
- Agent Gateway (routing/classifier Lambdas) via CodeBuild → ECR
- Gateway infra (RDS, Cognito, CloudFront, API Gateway) via Terraform (two-pass: first creates MOCK API, second wires to the EKS Ingress ALB)
- Gateway backend image build via CodeBuild → rollout to EKS
- Gateway frontend build on the runner → S3 → CloudFront invalidate

**Phase 4 — Verification (~2 min, automated)**
I'll probe /health, /agent/health (should 403 unsigned), and the CloudFront root, then report URLs.

**Total time: ~45 minutes. Estimated AWS cost for a test run: ~$5-10/day idle (RDS, NAT, CloudFront).**

After Phase 0, you can step away — I'll handle everything and report back when it's done.

Shall I proceed?"

Wait for the user to confirm before starting Phase 0.

---

Execute these phases in order. Each phase has steps, verification, and troubleshooting.

---

### Phase 0: GitHub Setup (Interactive — all user input happens here)

**This is the only phase that needs the user's attention. Everything after is automated.**

This phase was designed carefully. **Do not short-circuit it.** The GitHub Apps created here (`<org>-adp-agent-dev`, `-pm`, `-ops`) are how the Agent Factory impersonates different personas on your repo; without them, the agent-operations / agent-developer / agent-pm workflows cannot push commits, open PRs, or comment on issues. The credentials land in Secrets Manager at **org-namespaced paths** (`adp/<github_org>/gh-app-<role>-{id,key}`) so multiple installs can coexist on the same AWS account.

**Pre-check:** Before starting, verify GitHub CLI is installed and authenticated:
```bash
command -v gh && gh auth status
```
If `gh` is not installed, tell the user: "Install GitHub CLI: https://cli.github.com/ then run `gh auth login`."
If not authenticated, tell the user: "Run `gh auth login` and follow the prompts."
Do not proceed until both pass.

**Step 1: Gather user input.** Ask in this order:

1. "What is your GitHub organization name?" — the user's GH org (e.g. `aws-e`). Save as `GITHUB_ORG`.
2. "What should the repo be called in your org? (default: adp)" — save as `REPO_NAME`.
3. "Which AWS profile do you want to use?" — run `aws configure list-profiles` first to show options. If they pick one, `export AWS_PROFILE=<chosen>` for all subsequent commands. "default" / empty = don't set it.
4. Verify the profile resolves to the account they expect:
   ```bash
   aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output table
   ```
   Confirm with the user: "This will deploy to AWS account XXXX. Is that correct?" Do not proceed on mismatch.
5. "Which modules do you want? (1) Everything, (2) Gateway only, (3) Agent Factory only" — save as `DEPLOY_SCOPE`.
6. "Any additional repos where the agents should be installed?" — list, will be passed to `create-github-apps.sh`.

Save all answers in `.adp-deploy-state.json`.

**Step 2: Configure the repo for their org.**

```bash
./platform/scripts/setup-org.sh "$GITHUB_ORG" "$REPO_NAME"
```

This renames the `aws-e` org references in the repo to the user's org. Run it even if you're deploying to the same org it was last configured for — it's idempotent.

**Step 3: Create GitHub Apps (required unless DEPLOY_SCOPE is "Gateway only").**

First check if they already exist:

```bash
# Org-namespaced secret paths (not plain adp/gh-app-*)
for role in dev pm ops; do
  aws secretsmanager get-secret-value \
    --secret-id "adp/${GITHUB_ORG}/gh-app-${role}-id" \
    --query 'SecretString' --output text --region us-east-1 2>/dev/null \
    && echo "${role}: EXISTS" || echo "${role}: MISSING"
done

# Check that apps are actually installed on the org
gh api "/orgs/${GITHUB_ORG}/installations" --jq '.installations[] | "\(.app_slug): installed"'
```

If all 3 secrets exist AND all 3 apps are installed → **skip to Step 4.** Tell the user: "GitHub Apps already configured."

If any are missing, run:

```bash
./platform/scripts/create-github-apps.sh "$GITHUB_ORG" [extra-repo1 extra-repo2 ...]
```

**Tell the user before running this:** "I'll create 3 GitHub Apps for the agents. Your browser will open 3 times (once per app). For each app:
1. Click 'Create GitHub App' (permissions are pre-filled).
2. Note the App ID shown at the top of the resulting page.
3. Scroll down and click 'Generate a private key' — a `.pem` file downloads to ~/Downloads.
4. Come back to this terminal and enter the App ID when prompted.
5. Then the browser reopens for installation — select your org, choose 'Only select repositories', pick `${REPO_NAME}`, click Install.
6. Press Enter when done.

The apps are named `${GITHUB_ORG}-adp-agent-dev`, `-pm`, `-ops`. GitHub App names are globally unique — the org prefix ensures yours won't collide."

The script: browser-opens each app creation URL → prompts for App ID → auto-detects the `.pem` in `~/Downloads` → stores both in Secrets Manager at `adp/<org>/gh-app-<role>-{id,key}` → re-opens the browser for installation → verifies installation via `gh api /orgs/<org>/installations`.

If DEPLOY_SCOPE is "Gateway only", skip this step.

**Step 4: Verify.**
```bash
gh repo view "${GITHUB_ORG}/${REPO_NAME}" --json name
aws secretsmanager list-secrets --filter Key=name,Values="adp/${GITHUB_ORG}/gh-app" --query 'SecretList[].Name'
```
Expect: repo exists; 6 secrets listed (id + key × 3 roles) OR 0 if Gateway-only.

**Tell the user:** "GitHub setup complete. Everything from here is automated. This will take about 30-45 minutes. I'll keep you updated."

Mark `github_setup` phase `complete` in state file.

---

### Phase 1: Preflight (Automated)

Run the preflight check to validate the environment:

```bash
./platform/scripts/preflight-check.sh
```

**If preflight fails:**
- Missing CLI tools → tell the user which tools to install + install links. Wait for confirmation.
- AWS credentials invalid → ask the user to `aws configure` or set `AWS_REGION`. Wait for confirmation.
- Missing IAM permissions → tell the user which permissions are missing. They may need to contact their AWS admin.

**If preflight passes with warnings** → report the warnings, proceed. Warnings are non-blocking.

Once passing, mark `preflight` phase `complete`.

---

### Phase 2: Bootstrap Terraform State Backend

**What:** Creates `adp-terraform-state-<account_id>` S3 bucket and `adp-terraform-locks` DynamoDB table. The four per-module Terraform states live in this one bucket under keys like `dev/platform/terraform.tfstate`.

```bash
export AWS_REGION=us-east-1
export ENVIRONMENT=dev
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"

# Idempotent — run it always; the script no-ops if bucket + table exist
./platform/scripts/bootstrap.sh
```

**Verify:**
```bash
aws s3 ls | grep adp-terraform-state
aws dynamodb describe-table --table-name adp-terraform-locks --query 'Table.TableStatus' --output text
```
Expected: bucket listed, table status `ACTIVE`.

Mark `bootstrap` phase `complete`.

---

### Phase 3: Full Deploy (the main event)

**What:** Hand off to `deploy-all.sh`. It orchestrates platform → agent-factory → agent-gateway → gateway infra (two-pass ALB wiring) → gateway backend image build → k8s rollout → frontend build + upload. One command.

```bash
./platform/scripts/deploy-all.sh
```

Flags if not deploying everything:
- `--gateway-only` — platform + gateway only (skip agent-factory, agent-context)
- `--agent-factory-only` — platform + agent-factory only (skip gateway, agent-context)
- `--agent-context-only` — platform + agent-context only
- `--skip-agent-context` — full deploy minus agent-context (which is opt-in and costs ~$800/mo idle with GraphRAG)
- `--skip-frontend` — infra + backend only, no SPA

**What it does, in order:**

1. **Preflight** (runs `preflight-check.sh` inline)
2. **Detect operator public IP** and set `TF_VAR_eks_public_access_cidrs="[\"<ip>/32\"]"` so the EKS API is locked to the caller. Respects a pre-set env var if the caller wants to override.
3. **Platform infra** — `cd platform/infra && terraform apply`. Creates VPC, EKS (Auto Mode, 1.35), ECR repos, IAM baseline, and the 4 shared CodeBuild projects (Terraform-managed in `platform/infra/modules/codebuild/`).
4. **Agent Factory infra** (unless `--gateway-only` / `--agent-context-only`) — `cd modules/agent-factory/infra && terraform apply`. Creates runner IAM, ARC controller via helm_release, KEDA, secrets, beads state.
5. **Agent Gateway build + deploy** (same gate) — packages source zip, triggers `adp-dev-agent-gateway` CodeBuild project to build Lambda container → ECR, then applies K8s resources.
6. **Gateway infra, first apply** (unless `--agent-factory-only` / `--agent-context-only`) — `cd modules/gateway/infra && terraform apply`. This produces a working API Gateway with MOCK integrations because the internal ALB doesn't exist yet.
7. **ALB two-pass wiring** — `bash platform/scripts/wire-gateway-alb.sh` polls up to 10 min for the EKS Ingress controller to provision the internal ALB, caches ARN/DNS/SG IDs to SSM.
8. **Gateway infra, second apply** — same module, but now with `-var internal_alb_arn=... -var internal_alb_dns=... -var alb_security_group_ids=...`. This wires API Gateway's VPC Link v2 to the ALB and flips the OpenAPI body from MOCK to HTTP_PROXY.
9. **Gateway backend image** — packages source, triggers `adp-dev-gateway-build` CodeBuild → ECR, then `kubectl set image` to roll the deployment.
10. **Gateway frontend** (unless `--skip-frontend`) — `npm ci && npm run build` directly (runner has node22 pre-baked), `aws s3 sync dist/ s3://<frontend-bucket>/`, invalidate CloudFront.
11. **Agent Context** (only if `--agent-context-only` or `AGENT_CONTEXT_ENABLED=true`) — opt-in because it adds ~$800/month idle cost.

**Don't re-invent the workflow.** Do NOT start running `terraform apply` on individual modules yourself. If a phase inside `deploy-all.sh` fails, diagnose it using the script's own output and retry the single failing step — usually the script is idempotent on retry. Common failure modes are in the Troubleshooting section below.

**Expected duration:** 30-45 minutes. Longest steps are platform (EKS Auto Mode provisioning, ~15 min) and backend image build (~5 min for CodeBuild cold start + push).

Report progress to the user at each of the 11 sub-steps above. Summarize results — don't dump raw output.

Mark `deploy_all` phase `complete`.

---

### Phase 4: Verification

Probe live endpoints to confirm the stack is healthy end-to-end:

```bash
# 1. REST API Gateway routes (API_GATEWAY_URL from SSM)
API_URL=$(aws ssm get-parameter --name /adp/dev/gateway/api-gateway-url \
  --query Parameter.Value --output text 2>/dev/null || \
  aws apigateway get-rest-apis --query 'items[?contains(name, `bedrockgw`)].id' --output text | \
  head -1 | xargs -I{} echo "https://{}.execute-api.us-east-1.amazonaws.com/dev")

curl -sS -o /dev/null -w "GET /health: %{http_code}\n" "$API_URL/health"          # expect 200
curl -sS -o /dev/null -w "GET /agent/health unsigned: %{http_code}\n" "$API_URL/agent/health"   # expect 403 (AWS_IAM guard)

# 2. CloudFront (SPA)
CF_DOMAIN=$(aws ssm get-parameter --name /adp/dev/gateway/cloudfront-domain \
  --query Parameter.Value --output text 2>/dev/null)
[ -n "$CF_DOMAIN" ] && curl -sS -o /dev/null -w "GET https://$CF_DOMAIN : %{http_code}\n" "https://$CF_DOMAIN/"   # expect 200

# 3. Backend pods
kubectl --context "arn:aws:eks:us-east-1:$(aws sts get-caller-identity --query Account --output text):cluster/adp-dev-eks-cluster" \
  get pods -n adp-gateway -l app=bedrockgateway

# 4. RDS + Cognito
aws rds describe-db-instances --query 'DBInstances[?starts_with(DBInstanceIdentifier,`bedrockgw`)].DBInstanceStatus' --output text   # available
aws cognito-idp list-user-pools --max-results 5 --query 'UserPools[?starts_with(Name,`bedrockgw`)].Name' --output text

# 5. Agent Factory (if deployed)
kubectl get pods -n arc-systems 2>/dev/null || echo "Not deployed"
kubectl get pods -n arc-runners 2>/dev/null || echo "Not deployed"
```

**If the gateway was deployed with `--gateway-only` and GitHub Apps step was skipped**, also mint a Cognito JWT and probe a DB-backed endpoint to confirm `GRANT rds_iam TO bgadmin` completed:

```bash
ADMIN_SECRET=$(aws secretsmanager get-secret-value \
  --secret-id adp/dev/gateway/test-admin-credentials \
  --query SecretString --output text 2>/dev/null) || ADMIN_SECRET=""
if [ -n "$ADMIN_SECRET" ]; then
  USER=$(echo "$ADMIN_SECRET" | python3 -c "import json,sys; print(json.load(sys.stdin)['username'])")
  PW=$(echo "$ADMIN_SECRET" | python3 -c "import json,sys; print(json.load(sys.stdin)['password'])")
  POOL=$(echo "$ADMIN_SECRET" | python3 -c "import json,sys; print(json.load(sys.stdin)['cognito_user_pool_id'])")
  CLIENT=$(echo "$ADMIN_SECRET" | python3 -c "import json,sys; print(json.load(sys.stdin)['cognito_client_id'])")
  TOKEN=$(aws cognito-idp admin-initiate-auth \
    --user-pool-id "$POOL" --client-id "$CLIENT" \
    --auth-flow ADMIN_USER_PASSWORD_AUTH \
    --auth-parameters "USERNAME=$USER,PASSWORD=$PW" \
    --query 'AuthenticationResult.AccessToken' --output text)
  curl -sS -o /dev/null -w "GET /admin/organizations (admin JWT): %{http_code}\n" \
    -H "Authorization: Bearer $TOKEN" "$API_URL/admin/organizations"  # expect 200
fi
```

A 500 on the admin endpoint typically means the RDS bootstrap Job didn't successfully grant `rds_iam` to the `bgadmin` Postgres role — check `kubectl logs -n adp-gateway -l app=bedrockgw-dev-rds-bootstrap`.

**Tell the user a summary:**

"Deployment complete. Status:
- Platform: EKS cluster `adp-dev-eks-cluster` active, N nodes
- Gateway: N pods running, `/health` returns 200
- Frontend: https://$CF_DOMAIN (200 OK)
- API Gateway: routes via VPC Link v2 → ALB direct
- Database: available, IAM auth working
- Test credentials stored in Secrets Manager at `adp/dev/gateway/test-{user,admin}-credentials`
- Agent Factory: [deployed/not deployed]

Access the admin dashboard at https://$CF_DOMAIN. To use Claude Code with the gateway, see `modules/gateway/README.md`."

Mark `verification` phase `complete`.

---

## Undeploy / Teardown

For cleanup, use:

```bash
./platform/scripts/deploy-all.sh --destroy
```

Runs per-module destroys in reverse deploy order (agent-context → agent-factory → gateway → platform). Uses shared cleanup scripts for non-Terraform resources (ingress deletion + ALB wait, S3 bucket emptying, force-delete Secrets Manager entries, CloudFront two-phase disable-then-delete).

**State backend is a separate step** — `deploy-all.sh --destroy` does NOT delete the Terraform state bucket or the DynamoDB lock table. Run after module destroys succeed:

```bash
./platform/scripts/bootstrap-destroy.sh
```

Prompts for typed AWS account ID to confirm.

**Resources that survive by design:**
- GitHub App credentials (`adp/<org>/gh-app-*` in Secrets Manager) — apps live in your GH org, delete manually if you want
- GitHub Apps themselves — browser-based deletion, no CLI
- Terraform state backend — only `bootstrap-destroy.sh` deletes this

Per-module destroy workflows exist under `.github/workflows/<module>-infra-destroy.yml` for iterative development. Each requires a typed-name `confirm` input. Don't use them for full teardown — use `deploy-all.sh --destroy` which handles ordering.

---

## Troubleshooting Reference

Use this when things go wrong. Do not show this to the user — use it to diagnose and fix issues yourself.

### `terraform init` fails
- S3 bucket doesn't exist → run `bootstrap.sh` first.
- Backend config mismatch → check `environments/dev/backend.tfvars` or the module-scoped `environments/dev/modules/*-backend.tfvars` matches the state bucket.

### EKS nodes not appearing
- Auto Mode provisioning takes 3-5 minutes. Wait and retry `kubectl get nodes`.
- Still empty after 5 min: `kubectl get events --all-namespaces --sort-by='.lastTimestamp' | tail -20`.
- `aws eks update-kubeconfig --name adp-dev-eks-cluster --region us-east-1` — **cluster name is `adp-dev-eks-cluster`, NOT `adp-dev-eks`**. A bare `adp-dev-eks` in any script or command is a bug; fix it.

### Gateway pods CrashLoopBackOff
- `kubectl logs -n adp-gateway -l app=bedrockgateway --previous --tail=50`.
- Check configmap + secrets: `kubectl get configmap bedrockgateway-config -n adp-gateway -o yaml`.
- DB auth errors (`InvalidPasswordError: password authentication failed for user "bgadmin"`) → the RDS bootstrap Job didn't run or failed. Check `kubectl get jobs -n adp-gateway | grep rds-bootstrap` and its pod logs.
- `chat_logging_scrub_level` Pydantic error on startup → value must be one of `off|basic|standard` (the code now coerces `"none"` → `"off"` per PR #58, but an operator-edited configmap with another invalid value will still crash).

### CloudFront 502 or 504
- ALB not yet created by the Ingress controller. Check `kubectl get ingress -n adp-gateway`. Wait 2-3 minutes for ALB provisioning.
- If `deploy-all.sh` skipped ALB wiring ("ALB not found after 10 minutes"), the API Gateway body is still MOCK. Re-run `deploy-all.sh --gateway-only` or manually: `bash platform/scripts/wire-gateway-alb.sh` + re-apply gateway infra with the returned vars.

### REST API returns 500 on admin endpoints but /health is 200
- The RDS bootstrap Job didn't successfully run `GRANT rds_iam TO bgadmin`. The app's proxy path doesn't touch Postgres; admin / budget / ratelimit paths do.
- Fix: check the Job pod: `kubectl logs -n adp-gateway -l app=bedrockgw-dev-rds-bootstrap`. Common cause: AL2023 package name drift — should be `awscli-2`, not `aws-cli`.

### Frontend blank page
- Wrong `VITE_API_URL` during build. Rebuild with `VITE_API_URL="/api/gateway" npm run build`.
- Stale cache: `aws cloudfront create-invalidation --distribution-id <id> --paths "/*"`.

### CodeBuild fails
- Only 4 docker-build projects use CodeBuild (`adp-dev-gateway-build`, `adp-dev-chat-agent`, `adp-dev-agent-gateway`, `adp-dev-arc-runner`). Terraform-managed in `platform/infra/modules/codebuild/`. All other work (`terraform apply`, `npm build`, `kubectl apply`) runs directly on the caller.
- Check logs: `aws codebuild batch-get-builds --ids <build-id> --query 'builds[0].logs.deepLink' --output text`.
- Docker Hub 429 on `python:3.12-slim` pulls → Dockerfile should use `public.ecr.aws/docker/library/python:3.12-slim` (AWS mirror). Fixed in main; check the file if building fails here.
- IAM propagation: if role was just created, wait 15 seconds and retry.

### `terraform apply` wants to destroy+recreate Cognito domain
- Live has `bedrockgw-dev-auth-<account-suffix>`, code should too. If it says `bedrockgw-dev-auth` without the suffix, someone reverted PR #52; restore the `${name_prefix}-auth-${substr(data.aws_caller_identity.current.account_id, 4, 8)}` form before applying.

### Import existing resources (re-deploying to an account that already has some leftovers)
- `aws_cognito_user_group.admins`: `terraform import 'module.cognito.aws_cognito_user_group.admins' '<pool_id>/admins'`
- `aws_cognito_user.test_user[0]`: `'<pool_id>/<email>'`
- `aws_secretsmanager_secret.*`: `'<secret_name>'`
- `aws_codebuild_project.main["<key>"]`: `'adp-dev-<key>'`
- `aws_iam_role.codebuild`: `'adp-dev-codebuild-role'`

---

## Key Files Reference

| File | Purpose |
|------|---------|
| `platform/scripts/deploy-all.sh` | **Canonical deploy entry point — run this after Phase 0.** Also supports `--destroy`. |
| `platform/scripts/preflight-check.sh` | Environment validation (tools + IAM) |
| `platform/scripts/setup-org.sh` | Configure repo for your GitHub org |
| `platform/scripts/create-github-apps.sh` | Create 3 GitHub Apps + store creds in Secrets Manager + install on repos |
| `platform/scripts/bootstrap.sh` | Create Terraform state backend (S3 + DynamoDB) |
| `platform/scripts/bootstrap-destroy.sh` | Destroy Terraform state backend (separate intentional step) |
| `platform/scripts/wire-gateway-alb.sh` | Discover EKS Ingress ALB, cache to SSM — shared by deploy-all.sh and the module workflow |
| `platform/scripts/empty-s3-buckets.sh` | Idempotent bucket emptier (versioned + non-versioned) |
| `platform/scripts/delete-ingress-and-wait.sh` | `kubectl delete ingress` + poll ALB disappearance — runs before gateway destroy |
| `platform/scripts/force-delete-secrets.sh` | Force-delete Secrets Manager entries by prefix with protected-pattern allowlist |
| `platform/infra/main.tf` | Shared platform Terraform (VPC, EKS, ECR, IAM, CodeBuild projects) |
| `platform/infra/modules/codebuild/` | Terraform for the 4 shared docker-build CodeBuild projects |
| `modules/gateway/README.md` | Gateway detailed documentation |
| `modules/gateway/infra/main.tf` | Gateway Terraform (RDS, Cognito, CloudFront, API Gateway, Lambda authorizer) |
| `modules/gateway/k8s/deployment.yaml` | K8s deployment manifest for the FastAPI backend |
| `modules/agent-factory/infra/main.tf` | Agent factory Terraform (ARC, KEDA, SQS, WebSocket) |
| `environments/dev/` | Environment-specific Terraform vars |
| `.github/workflows/gateway-infra-apply.yml` | Per-module workflow for iterative dev — includes the two-pass ALB wire. NOT for first deploy. |
| `.github/workflows/gateway-deploy.yml` | Per-module build + deploy workflow for iterative dev |
| `.github/workflows/*-infra-destroy.yml` | Per-module destroy workflows with typed-name confirmation |

## Non-Interactive Shell Rules

Always use non-interactive flags to avoid hanging:
- `cp -f`, `mv -f`, `rm -f`
- `terraform apply -auto-approve`, `terraform init -input=false`
- `apt-get -y`, `yum -y`
- Never use interactive editors (vim, nano) — use `cat >` or `sed`
- `kubectl apply` is already non-interactive

## When to Call the User

Break silence and ask the user ONLY when:
- Phase 0 inputs (org, repo, AWS profile, deploy scope) — gathered at the start, not re-asked later
- The 3 GitHub App browser clicks in `create-github-apps.sh` — the only manual step in deploy
- Preflight tool installation (if a required CLI is missing)
- A failure has retried twice without success — explain what failed, what you tried, and ask for guidance. Do not guess on destructive operations (destroy, force-delete, rm -rf).
