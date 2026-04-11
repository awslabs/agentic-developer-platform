# Agent Instructions — ADP (Agentic Developer Platform)

You are the deployment agent for this platform. Your job is to deploy it end-to-end, keep the user informed, and only ask them when you genuinely need their input. Read this entire file, then execute the deployment.

## Your Behavior

- Run each step yourself. Do not ask the user to run commands — you run them.
- After each step, verify it succeeded before moving on.
- If something fails, diagnose it, attempt a fix, and retry. Only escalate to the user if you cannot resolve it after 2 attempts.
- Keep the user informed with brief status updates between steps. Do not dump raw command output — summarize results.
- When you need user input (AWS credentials, GitHub App setup), explain exactly what you need and why.

## What This Repo Contains

Three modules on a shared AWS platform:

| Module | Path | Purpose |
|--------|------|---------|
| Gateway | `modules/gateway/` | Multi-tenant Bedrock proxy (FastAPI + React) |
| Agent Factory | `modules/agent-factory/` | Autonomous code agents (Claude SDK + GitHub Actions) |
| MCP Gateway | `modules/mcp-gateway/` | MCP server gateway (in progress) |

Shared infrastructure: `platform/infra/` (VPC, EKS, ECR, IAM).

## Deployment Playbook

Execute these phases in order. Each phase has steps, verification, and troubleshooting.

---

### Phase 0: Fork & Configure Repository

**The Agent Factory requires this repo to be in your GitHub organization.** The agent workflows run via GitHub Actions, which needs the repo (and its workflows) in your org.

**Ask the user:** "What is your GitHub organization name?"

Then execute:

```bash
GITHUB_ORG="<user's org>"

# Check if repo already exists in their org
gh repo view "$GITHUB_ORG/adp" &>/dev/null && echo "EXISTS" || echo "MISSING"
```

**If MISSING**, fork or push the repo to their org:

```bash
# Option A: Fork (preserves link to upstream)
gh repo fork aws-e/adp --org "$GITHUB_ORG" --clone=false

# Option B: Create fresh repo and push (clean start)
gh repo create "$GITHUB_ORG/adp" --private --source=. --push
```

**After the repo is in their org**, update the agent workflow references:

```bash
# Update all agent workflows to reference the user's org instead of aws-e
for f in .github/workflows/agent-*.yml .github/workflows/pr-review-trigger.yml .github/workflows/skill-agent.yml; do
  if [ -f "$f" ]; then
    sed -i '' "s|repository: aws-e/adp|repository: $GITHUB_ORG/adp|g" "$f" 2>/dev/null || \
    sed -i "s|repository: aws-e/adp|repository: $GITHUB_ORG/adp|g" "$f"
  fi
done

# Also update client workflows if they'll onboard other repos
for f in modules/agent-factory/client-workflows/.github/workflows/*.yml; do
  if [ -f "$f" ]; then
    sed -i '' "s|aws-e/adp|$GITHUB_ORG/adp|g" "$f" 2>/dev/null || \
    sed -i "s|aws-e/adp|$GITHUB_ORG/adp|g" "$f"
  fi
done

# Commit and push the changes
git add -A
git commit -m "chore: update workflow references to $GITHUB_ORG/adp"
git push
```

**Tell the user:** "Repository configured in your org. Agent workflows will now reference $GITHUB_ORG/adp."

**Also update the agent-factory terraform.tfvars** to use their org:
```bash
# This will be done in Phase 6, but note the org name for later
echo "Will use github_org = \"$GITHUB_ORG\" for agent-factory infra"
```

---

### Phase 1: Preflight

**First, ask the user:** "Which modules do you want to deploy? Options:
1. Everything (gateway + agent factory) — full platform
2. Gateway only — Bedrock proxy with admin dashboard
3. Agent Factory only — autonomous code agents

The shared platform (VPC, EKS) is always deployed as a foundation."

Remember their choice — it determines which phases to execute.

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

### Phase 4: Deploy Gateway Infrastructure

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

### Phase 5: Build and Deploy Gateway Backend

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

### Phase 6: Build and Deploy Frontend

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

### Phase 7: Deploy Agent Factory

**Skip this phase if user chose "Gateway only".**

**If user chose "Agent Factory only" or "Everything":**

**Tell the user:** "Agent Factory needs GitHub Apps for authentication. I'll deploy the infrastructure first, then guide you through the GitHub App setup."

**Execute infrastructure:**
```bash
cd modules/agent-factory/infra

# Create backend config
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
github_org       = "GITHUB_ORG_HERE"
runner_namespace = "arc-runners"
EOF

terraform init -backend-config=../../../environments/dev/modules/agent-factory-backend.tfvars -input=false
terraform apply -var-file=terraform.tfvars -auto-approve
```

**Ask the user:** "What is your GitHub organization name?" — Update `github_org` in terraform.tfvars before applying.

**After infra deploys, tell the user:**
"Agent Factory infrastructure is deployed. To activate the agents, you need to:
1. Create 3 GitHub Apps (DEV, PM, OPS) — see `modules/agent-factory/SETUP-GUIDE.md` Step 1
2. Store their credentials in Secrets Manager — see Step 2
3. I cannot do this for you because it requires browser-based GitHub App creation.

Once you've created the apps and stored the credentials, tell me and I'll verify the setup."

**When user confirms GitHub Apps are set up, verify:**
```bash
aws secretsmanager get-secret-value --secret-id adp/gh-app-dev-id --query 'SecretString' --output text
aws secretsmanager get-secret-value --secret-id adp/gh-app-pm-id --query 'SecretString' --output text
aws secretsmanager get-secret-value --secret-id adp/gh-app-ops-id --query 'SecretString' --output text
```

If all return values, tell the user: "GitHub App credentials verified. Agent Factory is ready. Label any issue with `agent-developer` to test."

---

### Phase 8: Final Verification

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
- Check logs: `aws codebuild batch-get-builds --ids <build-id> --query 'builds[0].logs.deepLink' --output text`
- IAM propagation: if role was just created, wait 15 seconds and retry

---

## Key Files Reference

| File | Purpose |
|------|---------|
| `platform/scripts/deploy-all.sh` | Automated deploy script (alternative to agent-driven deploy) |
| `platform/scripts/preflight-check.sh` | Environment validation |
| `platform/scripts/bootstrap.sh` | Creates Terraform state backend |
| `platform/infra/main.tf` | Shared platform Terraform |
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
