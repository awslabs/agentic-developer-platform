# Agent Factory Setup Guide

End-to-end guide for deploying the ADP code agents on the shared EKS cluster.

## Prerequisites

- Shared platform deployed (`platform/infra/` — VPC, EKS, ECR, IAM)
- AWS CLI v2 configured with admin access
- Terraform >= 1.5
- kubectl configured for the shared EKS cluster (`adp-dev-eks`)
- Helm v3
- GitHub CLI (`gh`)
- Node.js >= 22

## Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Shared Platform                       │
│  VPC  │  EKS (adp-dev-eks)  │  ECR  │  IAM (OIDC/IRSA) │
└───────┴─────────────────────┴───────┴───────────────────┘
                      │
        ┌─────────────┴─────────────┐
        │                           │
   modules/gateway             modules/agent-factory
   (Bedrock proxy)             (Code agents)
                                    │
                    ┌───────────────┼───────────────┐
                    │               │               │
              Runner IAM      ARC Controller   Secrets Manager
              (IRSA role)     + Runner Sets    (GitHub App creds)
                    │               │
                    └───────┬───────┘
                            │
                     Runner Pods on EKS
                     (Claude SDK + Bedrock)
```

## Step 1: Create GitHub Apps

Each agent persona uses a separate GitHub App for rate-limit isolation (5000 req/hr per installation; see design note §3 in `docs/design-notes/github-integration-consolidation.md`).

You need three apps:

| App | Used by | Secrets Manager keys |
|-----|---------|---------------------|
| DEV app | @agent-developer, @agent-architect | `adp/gh-app-dev-id`, `adp/gh-app-dev-key` |
| PM app | @agent-pm | `adp/gh-app-pm-id`, `adp/gh-app-pm-key` |
| OPS app | @agent-reviewer, @agent-operations | `adp/gh-app-ops-id`, `adp/gh-app-ops-key` |

For each app, go to **GitHub Org Settings → Developer Settings → GitHub Apps → New GitHub App**:

- Homepage URL: `https://github.com/aws-e/adp`
- Webhook: uncheck "Active"
- Permissions:
  - Repository: Contents (Read & Write), Issues (Read & Write), Pull Requests (Read & Write), Workflows (Read & Write), Metadata (Read)
  - Organization: Members (Read)
- Install the app on your org, granting access to the repos you want agents to work on

After creating each app, note the App ID and download the private key `.pem` file.

> **Note:** The legacy `create-github-apps.sh` script has been removed. For the
> **webhook agent path** (recommended), use the Connections UI or
> `modules/agent-factory/webhook-ingress/scripts/register-github-app.sh` instead
> — it registers a single App with post-registration permission validation. The
> per-role App setup above is only needed for the ARC self-hosted runner path.

## Step 2: Store GitHub App Credentials in Secrets Manager

```bash
# DEV app (developer + architect agents)
aws secretsmanager create-secret --name adp/gh-app-dev-id --secret-string "<DEV_APP_ID>" --region us-east-1
aws secretsmanager create-secret --name adp/gh-app-dev-key --secret-string "$(cat /path/to/dev-app-private-key.pem)" --region us-east-1

# PM app
aws secretsmanager create-secret --name adp/gh-app-pm-id --secret-string "<PM_APP_ID>" --region us-east-1
aws secretsmanager create-secret --name adp/gh-app-pm-key --secret-string "$(cat /path/to/pm-app-private-key.pem)" --region us-east-1

# OPS app (reviewer + operations agents)
aws secretsmanager create-secret --name adp/gh-app-ops-id --secret-string "<OPS_APP_ID>" --region us-east-1
aws secretsmanager create-secret --name adp/gh-app-ops-key --secret-string "$(cat /path/to/ops-app-private-key.pem)" --region us-east-1
```

## Step 3: Deploy Agent Factory Infrastructure (Terraform)

This deploys the agent-specific resources onto the shared EKS cluster:

```bash
cd modules/agent-factory/infra

# Create backend config
cat > ../../../environments/dev/modules/agent-factory-backend.tfvars << EOF
bucket         = "adp-terraform-state-<ACCOUNT_ID>"
key            = "dev/modules/agent-factory/terraform.tfstate"
region         = "us-east-1"
encrypt        = true
dynamodb_table = "adp-terraform-locks"
EOF

# Create tfvars
cat > terraform.tfvars << EOF
environment      = "dev"
aws_region       = "us-east-1"
account_id       = "<ACCOUNT_ID>"
github_org       = "aws-e"
runner_namespace = "arc-runners"
EOF

# Init and apply
terraform init -backend-config=../../../environments/dev/modules/agent-factory-backend.tfvars
terraform plan -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars
```

This creates:
- IRSA role for runner pods (Bedrock, Secrets Manager, IAM, KMS, S3, etc.)
- ARC controller (Helm release in `arc-systems` namespace)
- ARC runner scale set (Helm release in `arc-runners` namespace)
- Kubernetes service account with IRSA annotation
- EKS access entry for runner pods (cluster-admin)
- DynamoDB table + S3 bucket for beads issue tracking
- Secrets Manager secret placeholders (if not already created in Step 2)

## Step 4: Install ARC Runners (Manual Alternative)

If you prefer Helm over Terraform, use the deploy script:

```bash
cd modules/agent-factory/runner-infra/scripts

# Configure kubectl for the shared cluster
aws eks update-kubeconfig --region us-east-1 --name adp-dev-eks

# Install cert-manager (ARC dependency)
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.13.2/cert-manager.yaml
kubectl wait --for=condition=Available deployment/cert-manager -n cert-manager --timeout=120s

# Install ARC controller
helm upgrade --install arc-controller \
    --namespace arc-systems --create-namespace \
    --values ../helm/arc-controller-values.yaml \
    --wait \
    oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set-controller

# Create runner namespace
kubectl create namespace arc-runners --dry-run=client -o yaml | kubectl apply -f -

# Create GitHub App secret for ARC authentication
kubectl create secret generic github-arc-secret \
    --namespace arc-runners \
    --from-literal=github_app_id=<APP_ID> \
    --from-literal=github_app_installation_id=<INSTALLATION_ID> \
    --from-literal=github_app_private_key="$(cat /path/to/private-key.pem)"

# Create IRSA service account
RUNNER_ROLE_ARN=$(terraform -chdir=../../../infra output -raw runner_role_arn 2>/dev/null || echo "arn:aws:iam::<ACCOUNT_ID>:role/adp-dev-agent-runner-role")
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: ServiceAccount
metadata:
  name: github-runner-sa
  namespace: arc-runners
  annotations:
    eks.amazonaws.com/role-arn: $RUNNER_ROLE_ARN
EOF

# Install org-level runner scale set
helm upgrade --install arc-runner-org \
    --namespace arc-runners \
    --set githubConfigUrl="https://github.com/aws-e" \
    --set githubConfigSecret=github-arc-secret \
    --set maxRunners=10 \
    --set minRunners=0 \
    --set template.spec.serviceAccountName=github-runner-sa \
    oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set
```

## Step 5: Verify Installation

```bash
# Check ARC controller is running
kubectl get pods -n arc-systems

# Check runner scale set is registered
kubectl get pods -n arc-runners

# Check IRSA is configured
kubectl describe sa github-runner-sa -n arc-runners
# Should show: eks.amazonaws.com/role-arn annotation

# Check GitHub sees the runner
gh api orgs/aws-e/actions/runners --jq '.runners[] | {name, status}'
```

## Step 6: Set GitHub Repository Variables

The agent workflows reference these variables. Set them on the `aws-e/adp` repo:

```bash
gh variable set AWS_REGION --body "us-east-1" --repo aws-e/adp
gh variable set BEADS_ENABLED --body "true" --repo aws-e/adp
gh variable set BEADS_DYNAMODB_TABLE --body "adp-dev-agent-beads-manifest" --repo aws-e/adp
gh variable set BEADS_S3_BUCKET --body "adp-dev-agent-beads-state-<ACCOUNT_ID>" --repo aws-e/adp
gh variable set BEADS_DATABASE --body "adp" --repo aws-e/adp
```

## Step 7: Test an Agent

Create a test issue and label it:

```bash
gh issue create --repo aws-e/adp \
  --title "Test: Add hello world endpoint" \
  --body "Add a /hello endpoint that returns {\"message\": \"hello world\"} to the gateway."

# Get the issue number from the output, then:
gh issue edit <NUMBER> --repo aws-e/adp --add-label "agent-developer"
```

Watch the workflow run:
```bash
gh run list --repo aws-e/adp --workflow "agent-developer.yml" --limit 5
gh run watch --repo aws-e/adp  # watch the latest run
```

## Step 8: Onboard Other Repos

To let agents work on other repos, copy the client workflows:

```bash
# Copy client workflows to the target repo
cp -r modules/agent-factory/client-workflows/.github/workflows/ /path/to/target-repo/.github/workflows/

# Or use the onboarding script
cd modules/agent-factory/runner-infra/scripts
./full-onboard-repo.sh <repo-name>
```

The client workflows use `workflow_call` to invoke the agent workflows in `aws-e/adp`, so the agent code stays centralized.

## Agent Personas

| Label | Workflow | What it does |
|-------|----------|-------------|
| `agent-developer` | `agent-developer.yml` | Code implementation, unit tests, PR creation |
| `agent-architect` | `agent-architect.yml` | Architecture design, technical decisions |
| `agent-pm` | `agent-pm.yml` | Issue decomposition, sub-issue creation, project management |
| `agent-reviewer` | `agent-reviewer.yml` | Code review, test validation, PR merge |
| `agent-product` | `agent-product.yml` | Requirements analysis, user stories, acceptance criteria |
| `agent-operations` | `agent-operations.yml` | Infrastructure, deployment, monitoring, runbooks |
| (auto) | `pr-review-trigger.yml` | Auto-triggers reviewer when agent creates a PR |

## Architecture

```
GitHub Issue (labeled "agent-developer")
        │
        ▼
.github/workflows/agent-developer.yml
        │
        ├── Checkout aws-e/adp (agent code)
        ├── Checkout target repo (workspace)
        ├── Get GitHub App token from Secrets Manager
        ├── npm ci + npm run build (agent runtime)
        │
        ▼
modules/agent-factory/agent/src/agent-worker.ts
        │
        ├── Claude SDK + Bedrock (IRSA credentials)
        ├── Analyze issue + repo context
        ├── Generate implementation plan
        ├── Execute code changes
        ├── Run tests
        │
        ▼
Creates PR on target repo
        │
        ▼
pr-review-trigger.yml → auto-labels "agent-reviewer"
        │
        ▼
agent-reviewer.yml → reviews, fixes, merges
```

## Cost

| Component | Monthly Cost | Notes |
|-----------|-------------|-------|
| ARC Controller | ~$5 | Small pod on shared EKS |
| Runner Pods | Variable | Scale to 0 when idle (EKS Auto Mode) |
| Secrets Manager | ~$2.40 | 6 secrets × $0.40 |
| DynamoDB (beads) | ~$0 | PAY_PER_REQUEST, minimal usage |
| S3 (beads state) | ~$0 | Minimal storage |
| Bedrock (Claude) | Per-token | Main cost driver — depends on usage |

Runner pods scale to zero when no jobs are running, so idle cost is minimal.

## Troubleshooting

**Workflow not triggering:**
- Verify the label name matches exactly (e.g., `agent-developer`)
- Check GitHub Actions is enabled for the repo
- Verify the runner is registered: `gh api orgs/aws-e/actions/runners`

**Runner pod not starting:**
- Check ARC controller logs: `kubectl logs -n arc-systems -l app.kubernetes.io/name=gha-runner-scale-set-controller`
- Check runner events: `kubectl get events -n arc-runners --sort-by='.lastTimestamp'`

**AWS permission errors:**
- Verify IRSA: `kubectl describe sa github-runner-sa -n arc-runners`
- Check the role trust policy includes the EKS OIDC provider
- Test from a runner pod: `aws sts get-caller-identity`

**Bedrock errors:**
- Verify model access is enabled in the Bedrock console
- Check the IRSA role has `bedrock:InvokeModel` permission
- Verify the model ID in the workflow env: `ANTHROPIC_MODEL`

**GitHub App token errors:**
- Verify secrets exist: `aws secretsmanager list-secrets --filter Key=name,Values=adp/gh-app`
- Check the app is installed on the target repo
- Verify the app has the required permissions
