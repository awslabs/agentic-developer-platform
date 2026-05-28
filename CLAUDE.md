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
    "preflight":        {"status": "pending"},
    "bootstrap":        {"status": "pending"},
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

Before executing any phase, present this briefing to the user:

---

**Tell the user:**

"Here's how the deployment will work:

I'll handle the entire deployment for you. There are 10 phases, and only the first one needs your input — after that, everything is automated.

**Phase 0 — GitHub Setup (~10 min, needs you)**
I'll ask for your GitHub org name, repo name, and which modules you want. Then I'll open your browser 3 times to create GitHub Apps — you just click approve each time. This is the only part that needs you.

**Phases 1-2 — Preflight + Bootstrap (~2 min, automated)**
I'll check your tools and AWS permissions, then create the Terraform state backend (S3 bucket + DynamoDB table).

**Phase 3 — Shared Platform (~15 min, automated)**
I'll deploy the VPC, EKS cluster, ECR repos, and IAM roles via CodeBuild. This is the longest step — I'll poll the build and keep you updated.

**Phases 4-5 — Agent Factory + Gateway (~10 min, automated)**
I'll deploy the ARC runner infrastructure, KEDA, SQS queues, and WebSocket API for the agent delivery pipeline.

**Phases 6-8 — Bedrock Gateway (~18 min, automated)**
I'll deploy the database (RDS), auth (Cognito), CDN (CloudFront), build the Docker image, deploy to EKS, build the React frontend, and push to S3.

**Phase 9 — Verification (~2 min, automated)**
I'll validate every component using AWS CLI and kubectl, then give you a summary with URLs.

**Total time: ~45 minutes. Estimated AWS cost for a test run: ~$5-10.**

After Phase 0, you can step away — I'll handle everything and report back when it's done.

Shall I proceed?"

Wait for the user to confirm before starting Phase 0.

---

Execute these phases in order. Each phase has steps, verification, and troubleshooting.

---

### Phase 0: GitHub Setup (Interactive — all user input happens here)

**This is the only phase that needs the user's attention. Everything after is automated.**

**Pre-check:** Before starting, verify GitHub CLI is installed and authenticated:
```bash
command -v gh && gh auth status
```
If `gh` is not installed, tell the user: "Install GitHub CLI: https://cli.github.com/ then run `gh auth login`."
If not authenticated, tell the user: "Run `gh auth login` and follow the prompts."
Do not proceed until both pass.

**Step 1: Ask the user:**
- "What is your GitHub organization name?"
- "What should the repo be called in your org? (default: adp)"
- "Which AWS profile do you want to use? (I'll list your available profiles)"

Run `aws configure list-profiles` to show available profiles. If the user picks one, set it for all subsequent commands:
```bash
export AWS_PROFILE=<chosen-profile>
```
If they say "default" or press Enter, don't set it (uses the default profile).

Then verify the profile works:
```bash
aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output table
```
Show the account ID and ARN to the user and confirm: "This will deploy to AWS account XXXX. Is that correct?"

Continue asking:
- "Which modules do you want to deploy? (1) Everything, (2) Gateway only, (3) Agent Factory only"
- "Do you have other repos where you want the agents to work? List the repo names."

Remember their answers for all subsequent phases.

**Step 2: Configure the repo for their org:**

```bash
./platform/scripts/setup-org.sh <GITHUB_ORG> <REPO_NAME>
```

**Step 3: Create GitHub Apps (if deploying Agent Factory):**

**First, check if apps are already set up:**
```bash
# Check Secrets Manager for existing credentials
for role in dev pm ops; do
  aws secretsmanager get-secret-value --secret-id "adp/gh-app-${role}-id" --query 'SecretString' --output text --region us-east-1 2>/dev/null && echo "${role}: EXISTS" || echo "${role}: MISSING"
done

# Check GitHub for installations
gh api "/orgs/<GITHUB_ORG>/installations" --jq '.installations[] | "\(.app_slug): installed"'
```

If all 3 secrets exist AND all 3 apps are installed, tell the user: "GitHub Apps are already configured. Skipping creation." and move on.

If any are missing, run the creation script:

**Tell the user:** "I need to create 3 GitHub Apps for the agents. This will open your browser 3 times. For each app:
1. Click 'Create GitHub App' (permissions are pre-filled)
2. Note the App ID shown at the top of the page
3. Scroll down and click 'Generate a private key' (downloads a .pem file)
4. Come back here and enter the App ID
5. Then I'll open the browser again to install the app — select your org, choose 'Only select repositories', pick 'adp', and click Install
6. Press Enter when done

The apps will be named `<your-org>-adp-agent-dev`, `<your-org>-adp-agent-pm`, `<your-org>-adp-agent-ops`. GitHub App names are globally unique, so they're prefixed with your org name."

```bash
./platform/scripts/create-github-apps.sh <GITHUB_ORG> [extra-repo1 extra-repo2 ...]
```

The script handles: browser open → App ID prompt → auto-detect .pem in ~/Downloads → store in Secrets Manager → browser open for installation → verify installation → final validation of all 3 apps.

If the user chose "Gateway only", skip this step.

**Step 4: Verify:**
```bash
# Repo exists
gh repo view <GITHUB_ORG>/adp --json name

# GitHub Apps (if created)
aws secretsmanager list-secrets --filter Key=name,Values=adp/gh-app --query 'SecretList[].Name'
```

**Tell the user:** "GitHub setup complete. Everything from here is automated. This will take about 30-45 minutes. I'll keep you updated."

---

### Phase 1: Preflight (Automated)

Run the preflight check to validate the environment:

```bash
./platform/scripts/preflight-check.sh
```

**If preflight fails:**
- Missing CLI tools → Tell the user which tools to install and provide the install links. Wait for them to confirm.
- AWS credentials invalid → Ask the user to run `aws configure` or set `AWS_REGION`. Wait for confirmation.
- Missing IAM permissions → Tell the user which permissions are missing. They may need to contact their AWS admin.

**If preflight passes with warnings:**
- Report the warnings to the user but proceed. Warnings are non-blocking.

**Once preflight passes**, inform the user: "Environment validated. Starting deployment. This will take approximately 30-45 minutes. I'll keep you updated at each step."

---

### Phase 2: Bootstrap Terraform State Backend

**What:** Creates an S3 bucket and DynamoDB table for Terraform state storage.

**Execute:**
```bash
export AWS_REGION=us-east-1
export ENVIRONMENT=dev
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"
```

Check if already bootstrapped:
```bash
aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null && echo "EXISTS" || echo "MISSING"
```

If MISSING, run:
```bash
./platform/scripts/bootstrap.sh
```

**Verify:**
```bash
aws s3 ls | grep adp-terraform-state
aws dynamodb describe-table --table-name adp-terraform-locks --query 'Table.TableStatus' --output text
```

Expected: bucket listed, table status `ACTIVE`.

**Tell the user:** "Terraform state backend ready. S3 bucket: adp-terraform-state-XXXX."

---

### Phase 3: Deploy Shared Platform

**What:** VPC, EKS cluster, ECR repositories, base IAM roles. Takes ~15 minutes.

**Tell the user:** "Deploying shared platform infrastructure (VPC, EKS, ECR). This takes about 15 minutes."

**Execute:**
```bash
# Detect operator's public IP and restrict EKS API endpoint to it (portable — works for any cloner).
# Respect an already-exported value if the caller set one.
if [ -z "${TF_VAR_eks_public_access_cidrs:-}" ]; then
  MY_IP=$(curl -fsS --max-time 5 https://checkip.amazonaws.com | tr -d '[:space:]')
  export TF_VAR_eks_public_access_cidrs="[\"${MY_IP}/32\"]"
fi

cd platform/infra
terraform init -backend-config=../../environments/dev/backend.tfvars -input=false
terraform apply -var-file=../../environments/dev/platform.tfvars -auto-approve
```

**Verify:**
```bash
aws eks describe-cluster --name adp-dev-eks --query 'cluster.{status:status,version:version}' --output table
```

Expected: status `ACTIVE`.

Then configure kubectl and wait for nodes:
```bash
aws eks update-kubeconfig --name adp-dev-eks --region us-east-1
kubectl get nodes
```

If no nodes yet, wait — EKS Auto Mode takes 3-5 minutes to provision. Check every 30 seconds.

**Tell the user:** "Platform deployed. EKS cluster adp-dev-eks is active with N nodes."

---

### Phase 4: Deploy Agent Factory Infrastructure

**Skip this phase if user chose "Gateway only".**

**Tell the user:** "Deploying Agent Factory infrastructure (runner IAM, ARC controller, secrets, beads state). About 5 minutes."

**Execute:**
```bash
cd modules/agent-factory/infra

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
cat > ../../../environments/dev/modules/agent-factory-backend.tfvars << EOF
bucket         = "adp-terraform-state-${ACCOUNT_ID}"
key            = "dev/modules/agent-factory/terraform.tfstate"
region         = "us-east-1"
encrypt        = true
dynamodb_table = "adp-terraform-locks"
EOF

cat > terraform.tfvars << EOF
environment      = "dev"
aws_region       = "us-east-1"
account_id       = "${ACCOUNT_ID}"
github_org       = "<GITHUB_ORG>"
runner_namespace = "arc-runners"
EOF

terraform init -backend-config=../../../environments/dev/modules/agent-factory-backend.tfvars -input=false
terraform apply -var-file=terraform.tfvars -auto-approve
```

Use the `github_org` from Phase 0.

**Verify (from `docs/adp-platform-deployment/deployment-manifest.md`):**
```bash
aws iam get-role --role-name adp-dev-agent-runner-role --query 'Role.Arn'
kubectl get pods -n arc-systems
kubectl describe sa github-runner-sa -n arc-runners | grep eks.amazonaws.com/role-arn
aws secretsmanager list-secrets --filter Key=name,Values=adp/gh-app --query 'SecretList[].Name'
```

**Tell the user:** "Agent Factory deployed. ARC controller running, runner IAM configured, GitHub App credentials verified."

---

### Phase 5: Deploy Agent Gateway

**Skip this phase if user chose "Gateway only".**

**Tell the user:** "Deploying Agent Gateway (WebSocket API, SQS queues, KEDA). About 5 minutes."

**Execute:**
```bash
cd modules/agent-factory/scripts
./deploy-gateway.sh
```

**Verify:**
```bash
aws apigatewayv2 get-apis --query 'Items[?starts_with(Name,`adp`)].{Name:Name,Endpoint:ApiEndpoint}'
aws sqs list-queues --queue-name-prefix adp --query 'QueueUrls'
kubectl get pods -n keda
kubectl get scaledjobs -n adp-gateway-agents
```

**Tell the user:** "Agent Gateway deployed. WebSocket API, SQS queues, and KEDA ScaledJob ready."

---

### Phase 6: Deploy Gateway Infrastructure

**Skip this phase if user chose "Agent Factory only".**

**What:** RDS PostgreSQL, ElastiCache Redis, Cognito, CloudFront, S3, ALB, ECR, CloudTrail. Takes ~10 minutes.

**Tell the user:** "Deploying gateway infrastructure (database, auth, CDN). About 10 minutes."

**Execute:**
```bash
cd modules/gateway/infra
terraform init -backend-config=../../../environments/dev/modules/gateway-backend.tfvars -input=false
terraform apply -var-file=../../../environments/dev/modules/gateway.tfvars -auto-approve
```

**Verify:**
```bash
# Check RDS
aws rds describe-db-instances --query 'DBInstances[?starts_with(DBInstanceIdentifier,`bedrockgw`)].{id:DBInstanceIdentifier,status:DBInstanceStatus}' --output table

# Check Cognito
aws cognito-idp list-user-pools --max-results 5 --query 'UserPools[?starts_with(Name,`bedrockgw`)].{Name:Name,Id:Id}' --output table
```

**Tell the user:** "Gateway infrastructure deployed. RDS, Cognito, CloudFront all provisioned."

---

### Phase 7: Build and Deploy Gateway Backend

**Skip this phase if user chose "Agent Factory only".**

**What:** Build Docker image, push to ECR, deploy to EKS.

**Tell the user:** "Building and deploying the gateway backend."

**Execute:**
```bash
cd modules/gateway
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com"

# Build
docker build -t adp-gateway .

# Push to ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $REGISTRY
docker tag adp-gateway:latest $REGISTRY/adp-gateway:latest
docker push $REGISTRY/adp-gateway:latest

# Deploy to EKS
kubectl create namespace adp-gateway --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s/ -n adp-gateway
kubectl rollout status deployment/bedrockgateway -n adp-gateway --timeout=300s
```

**Verify:**
```bash
kubectl get pods -n adp-gateway
kubectl port-forward -n adp-gateway svc/bedrockgateway 8080:8080 &
sleep 3
curl -s http://localhost:8080/health
kill %1 2>/dev/null
```

Expected: pods Running, health returns 200.

**If pods are CrashLoopBackOff:**
```bash
kubectl logs -n adp-gateway -l app=bedrockgateway --tail=30
kubectl describe pod -n adp-gateway -l app=bedrockgateway
```
Common causes: missing configmap, missing secrets, RDS not reachable. Check and fix.

**Tell the user:** "Gateway backend running. N pods healthy."

---

### Phase 8: Build and Deploy Frontend

**Skip this phase if user chose "Agent Factory only".**

**What:** Build React app, upload to S3, invalidate CloudFront.

**Tell the user:** "Building and deploying the admin dashboard frontend."

**Execute:**
```bash
cd modules/gateway/frontend
npm ci
VITE_API_URL="/api/gateway" npm run build

BUCKET=$(aws ssm get-parameter --name "/adp/dev/gateway/frontend-bucket" --query "Parameter.Value" --output text)
aws s3 sync dist/ "s3://${BUCKET}/" --delete

DIST_ID=$(aws ssm get-parameter --name "/adp/dev/gateway/cloudfront-id" --query "Parameter.Value" --output text)
aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*"
```

**Verify:**
```bash
CF_DOMAIN=$(aws ssm get-parameter --name "/adp/dev/gateway/cloudfront-domain" --query "Parameter.Value" --output text 2>/dev/null || echo "")
if [ -n "$CF_DOMAIN" ]; then
  curl -s -o /dev/null -w "%{http_code}" "https://${CF_DOMAIN}/"
fi
```

Expected: 200.

**If SSM parameters not found:** The Terraform may not have stored them. Check `terraform output` in `modules/gateway/infra/` for the values and use them directly.

**Tell the user:** "Frontend deployed at https://DOMAIN. Admin dashboard is accessible."

---

### Phase 9: Final Verification

Run a comprehensive check of all deployed components:

```bash
echo "=== Platform ==="
aws eks describe-cluster --name adp-dev-eks --query 'cluster.status' --output text
kubectl get nodes --no-headers | wc -l

echo "=== Gateway Pods ==="
kubectl get pods -n adp-gateway -o wide

echo "=== Gateway Health ==="
kubectl exec -n adp-gateway deploy/bedrockgateway -- curl -s http://localhost:8080/health 2>/dev/null || echo "Cannot exec into pod"

echo "=== Frontend ==="
CF_DOMAIN=$(aws ssm get-parameter --name "/adp/dev/gateway/cloudfront-domain" --query "Parameter.Value" --output text 2>/dev/null)
[ -n "$CF_DOMAIN" ] && curl -s -o /dev/null -w "CloudFront: %{http_code}\n" "https://${CF_DOMAIN}/"

echo "=== Database ==="
aws rds describe-db-instances --query 'DBInstances[?starts_with(DBInstanceIdentifier,`bedrockgw`)].DBInstanceStatus' --output text

echo "=== ARC Runners (if deployed) ==="
kubectl get pods -n arc-systems 2>/dev/null || echo "Not deployed"
kubectl get pods -n arc-runners 2>/dev/null || echo "Not deployed"
```

**Tell the user a summary:**
"Deployment complete. Here's the status:
- Platform: EKS cluster active, N nodes
- Gateway: N pods running, health OK
- Frontend: https://DOMAIN (status 200)
- Database: available
- Agent Factory: [deployed/not deployed]

You can access the admin dashboard at https://DOMAIN.
To use Claude Code with the gateway, see `modules/gateway/README.md` Step 7."

---

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
