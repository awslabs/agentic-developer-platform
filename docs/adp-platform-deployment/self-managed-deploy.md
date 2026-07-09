# Deploying ADP

Step-by-step guide to deploy the Agentic Developer Platform from a fresh clone to a running system.

## Prerequisites

| Tool | Required | Install |
|------|----------|---------|
| AWS CLI v2 | Always | https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html |
| Terraform ≥ 1.14 | Always | https://developer.hashicorp.com/terraform/install |
| kubectl | Always | https://kubernetes.io/docs/tasks/tools/ |
| Node.js ≥ 22 | Frontend build | https://nodejs.org/ |
| GitHub CLI (`gh`) | GitHub Apps setup | https://cli.github.com/ |
| Docker | Local image builds only | https://docs.docker.com/get-docker/ |
| Helm v3 | Manual ARC install only | https://helm.sh/docs/intro/install/ |

AWS account requirements:
- Admin-level IAM access (or at minimum: EKS, ECR, RDS, ElastiCache, Cognito, CloudFront, S3, IAM, Secrets Manager, SQS, API Gateway, Lambda, DynamoDB, CodeBuild)
- Bedrock model access: enabled **automatically** by the deploy — `deploy-all.sh` runs `platform/scripts/enable-bedrock-models.sh` right after preflight, which discovers all ACTIVE Anthropic models from the Bedrock API and accepts their marketplace agreements via CLI (idempotent, no console step). Fresh accounts have no agreements; without them every Claude call fails with `AccessDeniedException` (`aws-marketplace:Subscribe`) and agents misreport it as "no changes needed". Requires `bedrock:ListFoundationModels`, `bedrock:GetFoundationModelAvailability`, `bedrock:ListFoundationModelAgreementOffers`, `bedrock:CreateFoundationModelAgreement`, `aws-marketplace:Subscribe`, `aws-marketplace:ViewSubscriptions`. If your AWS org runs a **private marketplace**, newly released models may fail with "private marketplace eligibility" until an org admin whitelists them — non-fatal unless it's a model the platform invokes at runtime.
- Region: `us-east-1` (default; configurable)

## Quick Start (Automated)

The fastest path from clone to running platform:

```bash
git clone https://github.com/<your-org>/adp.git
cd adp

# 1. Authenticate — the account this resolves to is the deploy target
export AWS_PROFILE=<your-profile>   # or aws configure
aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output table

# 2. Deploy the platform + gateway (~30-45 minutes)
./platform/scripts/deploy-all.sh [--gateway-only]
```

That handles bootstrap, infrastructure, image builds, K8s deployment, frontend,
broker Lambda, admin bootstrap, agent-factory, and webhook-ingress — the full
11-step sequence. **GitHub is not set up upfront** — wire GitHub at the end via
the UI (Settings → Connections → "Set up GitHub App" as the `platform_admin`)
or the CLI fallback `register-github-app.sh` (see Phase 3b). The script prints
next-steps guidance at the end.

### Deployment config

`config/deployment.yml` (gitignored) is **optional**. If absent, every script
resolves the target account from `aws sts get-caller-identity` (and region from
`AWS_REGION`). Write the file only to pin a specific target; scripts/workflows
read it via `platform/scripts/load-deploy-config.sh`. There's no `account_id` to
hand-edit in any script.

Schema (full reference in `config/deployment.yml.example`):

```yaml
account_id: "111122223333"      # your AWS account
region: us-east-1
environment: dev
github_org: your-org
```

**Note**: the `customer_account` block in the example file is for the **ADP-managed** track only (where ADP's platform pods deploy into a customer-linked account on the customer's behalf). Self-managed deploys should leave it commented out.

## What Gets Deployed

```
deploy-all.sh execution order (11 steps):
│
├── Step  1: Bootstrap
│   └── S3 bucket (Terraform state) + DynamoDB table (state locking)
│
├── Step  2: Platform Infrastructure
│   └── VPC, EKS cluster (Auto Mode), ECR repos, IAM roles, CodeBuild projects
│
├── Step  3: Gateway Infrastructure
│   └── RDS PostgreSQL, ElastiCache Redis, Cognito, CloudFront, S3, API Gateway
│
├── Step  4: Gateway Backend
│   ├── Docker image build (CodeBuild or local)
│   └── K8s deployment (configmap, deployment, service, ingress)
│
├── Step  5: Wire ALB
│   └── Discover internal ALB → re-apply Terraform with VPC Link + CloudFront VPC Origin
│
├── Step  6: Frontend
│   └── npm build → S3 upload → CloudFront invalidation
│
├── Step  7: Broker Lambda (--skip-broker to skip)
│   └── Package + upload real github-auth-broker code (required for GitHub login)
│
├── Step  8: Bootstrap First Admin (--skip-admin-bootstrap to skip)
│   └── Seed platform_admin DB rows (required for first login approval)
│
├── Step  9: Agent Factory
│   ├── Terraform (ARC controller, IRSA, secrets, beads state)
│   ├── Agent Gateway image build (CodeBuild or local)
│   └── KEDA ScaledJob deployment
│
├── Step 10: Webhook-Ingress (--skip-webhook-ingress to skip)
│   ├── Agent-runtime image build (CodeBuild)
│   ├── Webhook Lambda zip package + upload
│   └── Terraform apply (API GW → Lambda → SQS → KEDA → agent-worker)
│
└── Step 11: Agent Context (opt-in)
    └── OpenViking, Sourcebot, DeepWiki, LiteLLM, ingestion CronJob

Post-deploy (manual): GitHub App wiring — UI (Settings → Connections) or CLI
```

## Detailed Walkthrough

### Prerequisite: Authenticate + choose your AWS profile

There is **no upfront GitHub setup phase**. (An older "Phase 0" ran `setup-org.sh`
+ `create-github-apps.sh` to create 3 org-owned apps — that was the legacy **ARC**
track. The webhook agent path wires GitHub at the **end** via
`register-github-app.sh` (see Phase 3b / the GitHub App wiring step), after the
infrastructure it points at exists.) The only thing to set up first is your AWS
profile — everything keys off the account it resolves to.

```bash
# Verify AWS access + confirm the target account
export AWS_PROFILE=<chosen-profile>          # skip if using default
aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output table

# (GitHub is only needed later, for the agent path) verify gh is authed:
gh auth status
```

`config/deployment.yml` is optional — if absent, the account resolves from the
active profile (`aws sts get-caller-identity`). Write it by hand (4 lines:
`account_id`, `region`, `environment`, `github_org`) only if you want to pin a
specific target.

### Phase 1: Bootstrap

Creates the Terraform state backend in **the account `aws sts get-caller-identity` resolves to**. This phase is first because every later phase reads from the state bucket it creates. Runs as part of `deploy-all.sh`, or standalone:

```bash
export AWS_REGION=us-east-1
export ENVIRONMENT=dev
./platform/scripts/bootstrap.sh
```

Creates:
- S3 bucket: `adp-terraform-state-<account-id>` (versioned, encrypted, public access blocked)
- DynamoDB table: `adp-terraform-locks` (PAY_PER_REQUEST)

The script also rewrites `environments/dev/backend.tfvars` (and the per-module backend files in `environments/dev/modules/*-backend.tfvars`) in your working dir to point at the current account's bucket. **Do not commit this rewrite** — it's a per-operator local substitution, and committing it would lock the repo to one specific account.

Verify:
```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
aws s3api head-bucket --bucket "adp-terraform-state-${ACCOUNT_ID}"  # exits 0 on success
aws dynamodb describe-table --table-name adp-terraform-locks --query 'Table.TableStatus' --output text  # ACTIVE
grep "$ACCOUNT_ID" environments/dev/backend.tfvars  # bucket line should contain your account id
```

### Phase 2: Preflight

Validates your environment **after bootstrap**. Preflight checks the state bucket + lock table are reachable, on top of CLI tooling, AWS credentials, and IAM-permission checks. Runs as part of `deploy-all.sh`, or standalone:

```bash
./platform/scripts/preflight-check.sh
```

**What's checked** (verified against `platform/scripts/preflight-check.sh`):

| Section | Check | Outcome on fail |
|---|---|---|
| CLI tools | aws, terraform, node ≥ 22 | FAIL — must fix to proceed |
| | docker (daemon running), gh | WARN — agent uses CodeBuild fallback / GitHub features optional |
| AWS configuration | `aws sts get-caller-identity` succeeds | FAIL |
| | `AWS_REGION` set (env or `aws configure`) | FAIL |
| AWS permissions | s3, dynamodb, eks, ecr | FAIL — these are needed by Phase 1 + Phase 3 |
| | iam, codebuild, bedrock, secrets-manager, cognito | WARN — needed by Phases 3-8; fix before continuing past Phase 3 |
| Existing infra | state bucket + lock table from Phase 1 | WARN if missing — Phase 1 didn't complete |
| | EKS cluster, ECR `adp-gateway` | WARN if missing — these get created by Phase 3 |
| Environment config | `environments/dev/backend.tfvars` substituted | WARN if `ACCOUNT_ID` placeholder still present |
| | `environments/dev/modules/gateway.tfvars` exists | WARN |
| | kubeconfig path writable | WARN |

**Note:** RDS, ElastiCache, CloudFront, CloudWatch Logs, Lambda, API Gateway permissions are **not** explicitly checked here. They're exercised by later phases — if your role lacks them, Phase 4–8 will fail. If you're deploying with admin-level credentials, no concern. If your role is scoped, validate those permissions out-of-band before continuing.

If preflight fails (any FAIL line), fix and re-run. Warnings are reported and the script continues.

### Phase 3: Deploy

```bash
./platform/scripts/deploy-all.sh
```

Flags:
| Flag | Effect |
|------|--------|
| (none) | Deploy everything (platform + gateway + agent-factory) |
| `--gateway-only` | Platform + gateway only |
| `--agent-factory-only` | Platform + agent-factory only |
| `--agent-context-only` | Platform + agent-context only |
| `--skip-frontend` | Skip React frontend build |
| `--local` | Use local Docker for image builds (instead of CodeBuild) |

The script:
- Accepts Bedrock marketplace agreements for all ACTIVE Anthropic models (`enable-bedrock-models.sh` — see Prerequisites; fails the deploy only if a runtime-required model can't be enabled)
- Detects your public IP and locks the EKS API to it (`/32` CIDR)
- Runs Terraform directly for all infrastructure
- Uses CodeBuild for Docker image builds (or local Docker with `--local`)
- Applies K8s manifests directly via kubectl
- Performs a two-pass Terraform apply for the gateway (first creates MOCK API Gateway, second wires the real ALB after the Ingress controller provisions it)

Duration: ~30-45 minutes. Longest step is EKS Auto Mode provisioning (~15 min).

### Phase 3b: Stage-by-stage steps (when NOT using deploy-all.sh)

`deploy-all.sh` chains all 11 steps end-to-end, but operators deploying
module-by-module can run each script standalone. The scripts below publish
artifacts that Terraform only ships as **placeholders**. A fresh deploy never
fires the push-triggered CI workflows that normally publish them, so each has a
standalone, idempotent script:

| Step | Script | Also in deploy-all.sh? | Why it's needed |
|------|--------|:----------------------:|-----------------|
| **Bedrock model agreements** | `platform/scripts/enable-bedrock-models.sh` | ✅ (pre-step, runs before Step 1; preflight only warns) | Fresh accounts have no marketplace agreement for Anthropic models — every Claude call fails with `AccessDeniedException` and agent runs silently end "no changes needed". Idempotent, CLI-only. |
| **Gateway second pass (ALB)** | `platform/scripts/wire-gateway-alb.sh --apply` | ✅ Step 5 | The gateway API Gateway ships a MOCK OpenAPI body until the ALB is wired. This discovers the EKS Ingress ALB, re-applies gateway-infra with the ALB vars, and forces a stage redeploy so the real routes (backend `/{proxy+}` + `/auth/github`) go live. Run after the gateway backend pods are up. |
| **Broker Lambda code** | `modules/gateway/scripts/deploy-broker.sh` | ✅ Step 7 | Terraform creates the `github-auth-broker` Lambda with a 503 placeholder zip. This packages + uploads + updates the real code. Required for GitHub login. |
| **First-admin bootstrap** | `modules/gateway/scripts/bootstrap-admin.sh` | ✅ Step 8 | A fresh deploy has no `users` rows, so onboarding shows "request access" for everyone — including the seeded Cognito admin — with no one able to approve. `create_test_users=true` only makes the Cognito user, not the DB rows. This seeds the first admin's org/tenant/dept/team/user + cognito identity (role `platform_admin`) so they become "registered" and can approve real users. Idempotent; `--email`/`--pool-id`/`--org` overrides for an SSO admin. (Runs `python -m src.admin.onboarding.bootstrap_admin` in the gateway pod — the deployed image must contain that module.) |
| **Webhook-ingress stack** | `modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh` | ✅ Step 10 | One cohesive step: builds the `adp-agent-runtime` worker image (CodeBuild), packages + uploads the webhook Lambda zip, and `terraform apply`s the stack (API GW → Lambda → SQS → KEDA → agent-worker). |
| **GitHub App wiring** | **UI (primary):** Settings → Connections → "Set up GitHub App" (manifest flow; the Phase-6d `platform_admin` is the actor). **CLI fallback:** `modules/agent-factory/webhook-ingress/scripts/register-github-app.sh <org>` | ❌ (manual) | Final step. The UI flow (recommended) lets the `platform_admin` create + wire the App from the browser — creds are stored automatically. The CLI script is the fallback for headless / CI environments: creates the App (visibility prompt; private by default), stores creds, and calls `wire-github-app.sh`. Pass `--client-secret` to also wire GitHub login. Org owners get an org-owned App; non-owners get a user-owned App (both work). |

All scripts support `--dry-run` and `--skip-*` flags and are safe to re-run.
After GitHub App wiring (UI or CLI), install the App on the target repo(s)
(the UI prompts for this; for CLI use
`https://github.com/apps/<app-slug>/installations/new`) and `@mention` an agent
(e.g. `@agent-developer ...`) in an issue/PR comment to trigger it.

> **The "placeholder artifact" rule of thumb:** Terraform ships a placeholder for
> anything a separate push-triggered CI workflow normally publishes — the gateway
> image, agent-runtime image, broker Lambda code, webhook Lambda zip, Lambda
> layers, and the ALB-gated API GW body. The scripts above are the manual
> equivalents of those workflows for a from-clean deploy.

### Phase 4: Verification

After deployment completes, verify:

```bash
# EKS cluster
aws eks describe-cluster --name adp-dev-eks-cluster --query 'cluster.status'
# Expected: ACTIVE

# Gateway pods
kubectl get pods -n adp-gateway
# Expected: 2/2 Running

# Health endpoint — assert the JSON BODY, not the status code. When the
# CloudFront VPC origin is missing, /api/* falls through to the S3 SPA
# fallback which returns HTTP 200 with HTML (masked both 608-deploy
# incidents, #3085).
CF_DOMAIN=$(aws ssm get-parameter --name /adp/dev/gateway/cloudfront-domain --query Parameter.Value --output text)
curl -s "https://${CF_DOMAIN}/api/health" | grep -q '"status"[[:space:]]*:[[:space:]]*"healthy"' && echo HEALTH-OK || echo HEALTH-FAIL
# Expected: HEALTH-OK ({"status":"healthy"} body)

# Frontend
curl -s -o /dev/null -w "%{http_code}" "https://${CF_DOMAIN}/"
# Expected: 200

# Agent Factory (if deployed)
kubectl get pods -n arc-systems
# Expected: ARC controller running

# Database
aws rds describe-db-instances --query 'DBInstances[?starts_with(DBInstanceIdentifier,`bedrockgw`)].DBInstanceStatus' --output text
# Expected: available
```

## Deploy Scope Options

| I want to deploy... | Command |
|---------------------|---------|
| Everything | `./platform/scripts/deploy-all.sh` |
| Gateway only (no agents) | `./platform/scripts/deploy-all.sh --gateway-only` |
| Agent Factory only (no gateway) | `./platform/scripts/deploy-all.sh --agent-factory-only` |
| Agent Context only (code intelligence) | `./platform/scripts/deploy-all.sh --agent-context-only` |
| Everything + Agent Context | `AGENT_CONTEXT_ENABLED=true ./platform/scripts/deploy-all.sh` |
| Everything, build images locally | `./platform/scripts/deploy-all.sh --local` |

## Cost Estimates

| Component | Monthly Cost (idle) | Notes |
|-----------|-------------------|-------|
| EKS cluster | ~$70 | Control plane ($0.10/hr) |
| NAT Gateway | ~$30 | Data processing + hourly |
| RDS (db.t3.medium) | ~$50 | PostgreSQL, single-AZ dev |
| ElastiCache (cache.t3.micro) | ~$12 | Redis for rate limiting |
| CloudFront | ~$1-5 | Minimal traffic in dev |
| Secrets Manager | ~$5 | ~12 secrets |
| ARC runners | $0 idle | Scale to zero |
| Agent Context (opt-in) | ~$800 | GraphRAG + wiki generation |
| **Total (without Agent Context)** | **~$170/month** | |

Bedrock usage is pay-per-token on top of infrastructure costs.

## Teardown

### Primary: `undeploy.sh`

```bash
# Interactive teardown — requires typed 12-digit account ID
./platform/scripts/undeploy.sh

# Preview what would be destroyed (no changes made)
./platform/scripts/undeploy.sh --dry-run

# Resume from a specific phase (e.g., after a partial failure)
./platform/scripts/undeploy.sh --from gateway

# Skip a specific phase
./platform/scripts/undeploy.sh --skip agent_context

# Also destroy the Terraform state backend (prompts separately)
./platform/scripts/undeploy.sh --bootstrap
```

Destroy order: agent-context → webhook-ingress → agent-factory → gateway → platform.
Maintains `.adp-undeploy-state.json` for resume. Retries failed phases up to 2×.

For ADP-managed environments, use `.github/workflows/undeploy.yml` (workflow_dispatch)
which provides the same capabilities via GitHub Actions.

### Legacy path (retained)

```bash
# LEGACY — use undeploy.sh instead
./platform/scripts/deploy-all.sh --destroy
./platform/scripts/bootstrap-destroy.sh
```

> `deploy-all.sh --destroy` is retained for backward compatibility but lacks the
> typed-account-ID gate, dry-run mode, resume capability, and the webhook-ingress
> phase. Prefer `undeploy.sh` for all new teardowns.

### Resources that survive by design

- Terraform state backend (S3 + DynamoDB) — only destroyed with `--bootstrap`
- GitHub App secrets (`adp/gh-app-*`, `adp/*/gh-app-*` in Secrets Manager)
- Webhook-ingress GitHub App secrets (`adp/*/github-app/*` in Secrets Manager)
- GitHub Apps themselves (delete manually in GitHub org settings)
- AWS-managed RDS secrets (`rds!*`) — AWS handles their lifecycle

## CI/CD After Initial Deploy

Once the platform is running, ongoing changes are deployed via GitHub Actions:

| Module | Workflow | Trigger |
|--------|----------|---------|
| Platform infra | `platform-infra-apply.yml` | Manual dispatch after PR merge |
| Gateway infra | `gateway-infra-apply.yml` | Manual dispatch after PR merge |
| Gateway backend | `gateway-deploy.yml` | Push to main (`src/`, `Dockerfile`, `k8s/`) |
| Gateway frontend | (included in gateway-deploy) | Push to main (`frontend/`) |
| Agent Factory infra | `agent-factory-infra-apply.yml` | Manual dispatch after PR merge |

Infrastructure applies are intentionally manual-trigger (not auto-apply on merge) to separate "reviewed" from "deployed" and prevent surprise Friday deploys.

To validate that CI-managed infrastructure is in place:
```bash
./platform/scripts/deploy-all.sh --ci
```

## Troubleshooting

### Agent runs end "no changes needed" with $0.0000 / 1 turn

The model call never succeeded — the Claude SDK inside agent-worker swallows LLM failures as a graceful no-op. Most common cause on a fresh account: missing Bedrock marketplace agreement. Check and fix:
```bash
aws bedrock get-foundation-model-availability --model-id anthropic.claude-opus-4-6-v1 \
  --query 'agreementAvailability.status' --output text   # NOT_AVAILABLE = this is your problem
./platform/scripts/enable-bedrock-models.sh              # accepts agreements, polls until active
```
If agreements are fine, check the agent pod logs for 403 `agent_not_registered` (scaledjob role missing from the account's `bedrockgw-dev-agent-registry`) or other sigv4-chain errors.

### EKS nodes not appearing

Auto Mode takes 3-5 minutes. Wait and retry:
```bash
kubectl get nodes
kubectl get events --all-namespaces --sort-by='.lastTimestamp' | tail -20
```

### Gateway pods CrashLoopBackOff

```bash
kubectl logs -n adp-gateway -l app=bedrockgateway --previous --tail=50
kubectl get configmap bedrockgateway-config -n adp-gateway -o yaml
```

Common causes: missing configmap, RDS not reachable (check security groups), invalid `chat_logging_scrub_level` value.

### CloudFront 502/504

The ALB hasn't been provisioned yet by the Ingress controller. Check:
```bash
kubectl get ingress -n adp-gateway
```

If ADDRESS is empty, wait 2-3 minutes. If still empty after 5 minutes, re-run:
```bash
bash platform/scripts/wire-gateway-alb.sh
```

### Terraform wants to destroy Cognito domain

If Terraform shows a destroy+recreate for the Cognito domain, check that the domain name includes the account suffix: `bedrockgw-dev-auth-<account-suffix>`. If it doesn't, the code may have regressed.

### CodeBuild fails

```bash
# Get build logs URL
aws codebuild batch-get-builds --ids <build-id> --query 'builds[0].logs.deepLink' --output text
```

Common cause: Docker Hub rate limit on `python:3.12-slim`. The Dockerfile should use `public.ecr.aws/docker/library/python:3.12-slim`.

### `terraform init` fails

- "bucket does not exist" → run `./platform/scripts/bootstrap.sh` first
- "ACCOUNT_ID" placeholder in tfvars → bootstrap.sh should have replaced it; run it again
- "AccessDenied on adp-terraform-state-XXXXXXXXXXXX" where XXXXXXXXXXXX is **not** your current account → your local checkout still has another account's id baked into `environments/dev/backend.tfvars` (or per-module backend file). `bootstrap.sh` rewrites these files to match `aws sts get-caller-identity`, so re-running it fixes the file in-place. Never commit the rewrite back to the repo — each operator/agent gets their own substituted copy.

## File Reference

| File | Purpose |
|------|---------|
| `platform/scripts/deploy-all.sh` | Main deployment script |
| `platform/scripts/preflight-check.sh` | Environment validation |
| `platform/scripts/bootstrap.sh` | Create Terraform state backend |
| `platform/scripts/bootstrap-destroy.sh` | Destroy state backend |
| `platform/scripts/setup-org.sh` | Configure repo for your GitHub org |
| `platform/scripts/create-github-apps.sh` | Create + install GitHub Apps |
| `platform/scripts/wire-gateway-alb.sh` | Discover ALB, cache to SSM |
| `platform/scripts/empty-s3-buckets.sh` | Empty S3 buckets before destroy |
| `platform/scripts/delete-ingress-and-wait.sh` | Clean up ALB before destroy |
| `platform/scripts/force-delete-secrets.sh` | Force-delete Secrets Manager entries |
| `environments/dev/` | Environment-specific Terraform variables |
| `docs/adp-platform-deployment/deployment-manifest.md` | Complete resource → validation command mapping |
