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
- Bedrock model access enabled (Claude Sonnet, Claude Opus, Titan V2 embeddings)
- Region: `us-east-1` (default; configurable)

## Quick Start (Automated)

The fastest path from clone to running platform:

```bash
git clone https://github.com/<your-org>/adp.git
cd adp

# 1. Authenticate
aws configure  # or export AWS_PROFILE=<your-profile>
gh auth login

# 2. Configure for your org
./platform/scripts/setup-org.sh <YOUR_GITHUB_ORG> adp

# 3. Create GitHub Apps (opens browser 3 times — interactive)
./platform/scripts/create-github-apps.sh <YOUR_GITHUB_ORG>

# 4. Deploy everything (~30-45 minutes)
./platform/scripts/deploy-all.sh
```

That's it. The script handles bootstrap, infrastructure, image builds, K8s deployment, frontend, and ALB wiring.

## What Gets Deployed

```
deploy-all.sh execution order:
│
├── Step 1: Bootstrap
│   └── S3 bucket (Terraform state) + DynamoDB table (state locking)
│
├── Step 2: Platform Infrastructure
│   └── VPC, EKS cluster (Auto Mode), ECR repos, IAM roles, CodeBuild projects
│
├── Step 3: Gateway Infrastructure
│   └── RDS PostgreSQL, ElastiCache Redis, Cognito, CloudFront, S3, API Gateway
│
├── Step 4: Gateway Backend
│   ├── Docker image build (CodeBuild or local)
│   ├── K8s deployment (configmap, deployment, service, ingress)
│   └── ALB wiring (discover ALB → re-apply Terraform with VPC Link)
│
├── Step 5: Frontend
│   └── npm build → S3 upload → CloudFront invalidation
│
├── Step 6: Agent Factory
│   └── ARC controller, runner scale set, IRSA, secrets, beads state
│
├── Step 7: Agent Gateway
│   ├── Docker image build (CodeBuild or local)
│   └── KEDA ScaledJob deployment
│
└── Step 8: Agent Context (opt-in)
    └── OpenViking, Sourcebot, DeepWiki, LiteLLM, ingestion CronJob
```

## Detailed Walkthrough

### Phase 0: GitHub Setup (Interactive — ~10 minutes)

This is the only phase that requires your attention. Everything after is automated.

#### Authenticate

```bash
# Verify AWS access
aws sts get-caller-identity
# Should show your account ID and role/user

# Verify GitHub CLI
gh auth status
# Should show "Logged in to github.com"
```

#### Choose your AWS profile

```bash
# List available profiles
aws configure list-profiles

# Set the one you want (skip if using default)
export AWS_PROFILE=<chosen-profile>

# Confirm the account
aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output table
```

#### Configure the repo for your org

```bash
./platform/scripts/setup-org.sh <YOUR_GITHUB_ORG> adp
```

This replaces org references throughout the repo. Idempotent — safe to run multiple times.

#### Create GitHub Apps

Required for Agent Factory. Skip if deploying gateway only.

Three apps are created (`<org>-adp-agent-dev`, `-pm`, `-ops`). Each one:
1. Opens your browser to the GitHub App creation page (permissions pre-filled)
2. You click "Create GitHub App"
3. Note the App ID, click "Generate a private key" (downloads `.pem`)
4. Enter the App ID in the terminal
5. Browser reopens for installation — select your org, pick the `adp` repo, click Install

```bash
./platform/scripts/create-github-apps.sh <YOUR_GITHUB_ORG>
```

The script auto-detects the `.pem` in `~/Downloads` and stores credentials in Secrets Manager at `adp/<org>/gh-app-{dev,pm,ops}-{id,key}`.

### Phase 1: Preflight

Validates your environment. Runs automatically as part of `deploy-all.sh`, or standalone:

```bash
./platform/scripts/preflight-check.sh
```

Checks: AWS CLI, Terraform, kubectl, Node.js, Docker, AWS credentials, IAM permissions.

### Phase 2: Bootstrap

Creates the Terraform state backend. Also runs automatically, or standalone:

```bash
export AWS_REGION=us-east-1
export ENVIRONMENT=dev
./platform/scripts/bootstrap.sh
```

Creates:
- S3 bucket: `adp-terraform-state-<account-id>` (versioned, encrypted, public access blocked)
- DynamoDB table: `adp-terraform-locks` (PAY_PER_REQUEST)

Verify:
```bash
aws s3 ls | grep adp-terraform-state
aws dynamodb describe-table --table-name adp-terraform-locks --query 'Table.TableStatus'
```

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
- Detects your public IP and locks the EKS API to it (`/32` CIDR)
- Runs Terraform directly for all infrastructure
- Uses CodeBuild for Docker image builds (or local Docker with `--local`)
- Applies K8s manifests directly via kubectl
- Performs a two-pass Terraform apply for the gateway (first creates MOCK API Gateway, second wires the real ALB after the Ingress controller provisions it)

Duration: ~30-45 minutes. Longest step is EKS Auto Mode provisioning (~15 min).

### Phase 4: Verification

After deployment completes, verify:

```bash
# EKS cluster
aws eks describe-cluster --name adp-dev-eks-cluster --query 'cluster.status'
# Expected: ACTIVE

# Gateway pods
kubectl get pods -n adp-gateway
# Expected: 2/2 Running

# Health endpoint
CF_DOMAIN=$(aws ssm get-parameter --name /adp/dev/gateway/cloudfront-domain --query Parameter.Value --output text)
curl -s "https://${CF_DOMAIN}/api/health"
# Expected: 200 OK

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

```bash
# Destroy all module infrastructure (reverse order)
./platform/scripts/deploy-all.sh --destroy

# Then destroy the state backend (separate step, requires typed confirmation)
./platform/scripts/bootstrap-destroy.sh
```

Destroy order: agent-context → agent-factory → gateway → platform.

Resources that survive by design:
- Terraform state backend (until you run `bootstrap-destroy.sh`)
- GitHub App credentials in Secrets Manager
- GitHub Apps themselves (delete manually in GitHub org settings)

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
