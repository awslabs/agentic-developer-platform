# Agent Instructions — ADP (Agentic Developer Platform)

You are deploying a multi-module platform. Read this entire file before starting.

## What This Repo Contains

Three modules on a shared AWS platform:

| Module | Path | Purpose |
|--------|------|---------|
| Gateway | `modules/gateway/` | Multi-tenant Bedrock proxy (FastAPI + React) |
| Agent Factory | `modules/agent-factory/` | Autonomous code agents (Claude SDK + GitHub Actions) |
| MCP Gateway | `modules/mcp-gateway/` | MCP server gateway (in progress) |

Shared infrastructure lives in `platform/infra/` (VPC, EKS, ECR, IAM).

## Deployment

### One-Command Deploy

```bash
# First, validate your environment
./platform/scripts/preflight-check.sh

# Then deploy everything
./platform/scripts/deploy-all.sh
```

The preflight check validates: CLI tools (aws, terraform, docker, node, kubectl), AWS credentials, IAM permissions (S3, DynamoDB, EKS, ECR, IAM, CodeBuild, Bedrock, Secrets Manager, Cognito), existing infrastructure state, and environment config files. Run it before deploying to catch issues early.

For local deploys, check with `--local` flag:
```bash
./platform/scripts/preflight-check.sh --local
```

This runs everything in AWS via CodeBuild. The deployer only needs AWS CLI configured with admin access. The script handles: bootstrap → platform infra → gateway infra → Docker build → k8s deploy → frontend build → agent-factory infra.

Options:
- `--gateway-only` — skip agent-factory
- `--skip-frontend` — skip frontend build
- `--local` — run Terraform/Docker/npm locally instead of CodeBuild
- `--destroy` — tear down everything

### Manual Step-by-Step Deploy

If the one-command script fails or you need to deploy incrementally:

#### Step 1: Bootstrap (creates Terraform state backend)
```bash
export AWS_REGION=us-east-1
export ENVIRONMENT=dev
cd platform/scripts
./bootstrap.sh
```
Verify: `aws s3 ls | grep adp-terraform-state`

#### Step 2: Platform infrastructure
```bash
cd platform/infra
terraform init -backend-config=../../environments/dev/backend.tfvars
terraform apply -var-file=../../environments/dev/platform.tfvars
```
Verify: `aws eks describe-cluster --name adp-dev-eks --query 'cluster.status'` → should return `ACTIVE`

#### Step 3: Gateway infrastructure
```bash
cd modules/gateway/infra
terraform init -backend-config=../../../environments/dev/modules/gateway-backend.tfvars
terraform apply -var-file=../../../environments/dev/modules/gateway.tfvars
```
Verify: check Terraform outputs for `cloudfront_domain_name`, `cognito_user_pool_id`

#### Step 4: Gateway backend
```bash
cd modules/gateway
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com"
docker build -t adp-gateway .
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $REGISTRY
docker tag adp-gateway:latest $REGISTRY/adp-gateway:latest
docker push $REGISTRY/adp-gateway:latest
aws eks update-kubeconfig --name adp-dev-eks --region us-east-1
kubectl apply -f k8s/ -n adp-gateway
```
Verify: `kubectl get pods -n adp-gateway` → pods should be Running

#### Step 5: Frontend
```bash
cd modules/gateway/frontend
npm ci
VITE_API_URL="/api/gateway" npm run build
BUCKET=$(aws ssm get-parameter --name "/adp/dev/gateway/frontend-bucket" --query "Parameter.Value" --output text)
aws s3 sync dist/ "s3://${BUCKET}/" --delete
```
Verify: open `https://<cloudfront_domain>` in browser

#### Step 6: Agent Factory (optional)
```bash
cd modules/agent-factory/infra
terraform init -backend-config=../../../environments/dev/modules/agent-factory-backend.tfvars
terraform apply -var-file=terraform.tfvars
```
Then store GitHub App credentials in Secrets Manager. See `modules/agent-factory/SETUP-GUIDE.md`.

### Local Development (no AWS)
```bash
cd modules/gateway
docker compose up
# Backend at http://localhost:8080, Postgres at :5432, Redis at :6379
```

## Monitoring and Verification

After deployment, verify each component:

### Platform
```bash
# EKS cluster status
aws eks describe-cluster --name adp-dev-eks --query 'cluster.{status:status,version:version,endpoint:endpoint}'

# Nodes
kubectl get nodes

# All namespaces
kubectl get pods --all-namespaces
```

### Gateway
```bash
# Pod health
kubectl get pods -n adp-gateway
kubectl logs -n adp-gateway -l app=bedrockgateway --tail=50

# Health endpoint (via port-forward if CloudFront not ready)
kubectl port-forward -n adp-gateway svc/bedrockgateway 8080:8080 &
curl http://localhost:8080/health

# Health endpoint (via CloudFront)
curl https://<cloudfront_domain>/api/health

# Database migrations
kubectl exec -n adp-gateway deploy/bedrockgateway -- alembic current
```

### Frontend
```bash
# Check S3 bucket
BUCKET=$(aws ssm get-parameter --name "/adp/dev/gateway/frontend-bucket" --query "Parameter.Value" --output text)
aws s3 ls "s3://${BUCKET}/" --summarize

# Check CloudFront distribution
DIST_ID=$(aws ssm get-parameter --name "/adp/dev/gateway/cloudfront-id" --query "Parameter.Value" --output text)
aws cloudfront get-distribution --id "$DIST_ID" --query 'Distribution.{Status:Status,DomainName:DomainName}'
```

### Agent Factory
```bash
# ARC controller
kubectl get pods -n arc-systems

# Runner scale set
kubectl get pods -n arc-runners

# IRSA verification
kubectl describe sa github-runner-sa -n arc-runners | grep eks.amazonaws.com/role-arn

# GitHub runner registration
gh api orgs/aws-e/actions/runners --jq '.runners[] | {name, status}'

# Secrets Manager
aws secretsmanager list-secrets --filter Key=name,Values=adp/gh-app --query 'SecretList[].Name'
```

### Infrastructure
```bash
# Terraform state
aws s3 ls s3://adp-terraform-state-$(aws sts get-caller-identity --query Account --output text)/

# RDS
aws rds describe-db-instances --query 'DBInstances[?starts_with(DBInstanceIdentifier,`bedrockgw`)].{id:DBInstanceIdentifier,status:DBInstanceStatus}'

# Redis
aws elasticache describe-replication-groups --query 'ReplicationGroups[?starts_with(ReplicationGroupId,`bedrockgw`)].{id:ReplicationGroupId,status:Status}'

# Cognito
aws cognito-idp list-user-pools --max-results 10 --query 'UserPools[?starts_with(Name,`bedrockgw`)].{Name:Name,Id:Id}'
```

## Troubleshooting

### Terraform init fails
- Check that `bootstrap.sh` was run and `ACCOUNT_ID` placeholders are replaced in `environments/**/*.tfvars`
- Verify S3 bucket exists: `aws s3 ls | grep adp-terraform-state`

### EKS nodes not ready
- EKS Auto Mode takes 3-5 minutes to provision nodes after cluster creation
- Check: `kubectl get nodes -w` (watch mode)

### Gateway pods CrashLoopBackOff
- Check logs: `kubectl logs -n adp-gateway -l app=bedrockgateway --previous`
- Common causes: missing configmap, missing secrets, RDS not reachable
- Check configmap: `kubectl get configmap bedrockgateway-config -n adp-gateway -o yaml`

### CloudFront returns 502
- The ALB is created by the EKS Ingress controller, not Terraform. It takes a few minutes.
- Check: `kubectl get ingress -n adp-gateway`
- The ALB DNS must be set in CloudFront origin (done by the deploy workflow)

### Frontend shows blank page
- Check browser console for errors
- Verify `VITE_API_URL` was set correctly during build
- Invalidate CloudFront cache: `aws cloudfront create-invalidation --distribution-id <id> --paths "/*"`

### Agent workflow doesn't trigger
- Verify the label matches exactly (e.g., `agent-developer`)
- Check runner is registered: `gh api orgs/aws-e/actions/runners`
- Check ARC controller logs: `kubectl logs -n arc-systems -l app.kubernetes.io/name=gha-runner-scale-set-controller`

### CodeBuild fails
- Check build logs: `aws codebuild batch-get-builds --ids <build-id> --query 'builds[0].logs.deepLink'`
- Common: IAM propagation delay (wait 15s after role creation), ECR login failure

## Key Files

| File | Purpose |
|------|---------|
| `platform/scripts/deploy-all.sh` | One-command deploy script |
| `platform/scripts/bootstrap.sh` | Creates Terraform state backend |
| `platform/infra/main.tf` | Shared platform Terraform |
| `modules/gateway/README.md` | Gateway detailed docs |
| `modules/gateway/Dockerfile` | Gateway container build |
| `modules/gateway/docker-compose.yml` | Local dev stack |
| `modules/gateway/infra/main.tf` | Gateway Terraform (15 modules) |
| `modules/gateway/k8s/deployment.yaml` | K8s deployment manifest |
| `modules/agent-factory/SETUP-GUIDE.md` | Agent factory setup guide |
| `modules/agent-factory/README.md` | Agent factory overview |
| `modules/agent-factory/infra/main.tf` | Agent factory Terraform |
| `environments/dev/` | Environment-specific Terraform vars |

## Environment Variables

| Variable | Default | Used by |
|----------|---------|---------|
| `AWS_REGION` | `us-east-1` | All scripts |
| `ENVIRONMENT` | `dev` | All scripts |
| `BG_DATABASE_URL` | (from configmap) | Gateway backend |
| `BG_REDIS_URL` | (from configmap) | Gateway backend |
| `CLAUDE_CODE_USE_BEDROCK` | `1` | Agent workflows |
| `ANTHROPIC_MODEL` | `global.anthropic.claude-opus-4-6-v1` | Agent workflows |

## Non-Interactive Shell Rules

When running commands, always use non-interactive flags:
- `cp -f`, `mv -f`, `rm -f` (avoid `-i` prompts)
- `terraform apply -auto-approve` (avoid interactive confirmation)
- `apt-get -y`, `yum -y` (avoid package manager prompts)
- Never use interactive editors (vim, nano) — use `cat >` or `sed` instead
