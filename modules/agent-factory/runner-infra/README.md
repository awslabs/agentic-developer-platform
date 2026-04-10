# GitHub Actions AI Agent Infrastructure

This project provides a complete infrastructure for running AI-powered GitHub agents on Amazon EKS. When you create an issue in a connected repository and add a specific label, an AI agent (powered by Claude via Amazon Bedrock) automatically works on the issue and creates a Pull Request.

## What This Project Does

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  You create an issue → Add label → AI Agent works on it → Creates PR       │
└─────────────────────────────────────────────────────────────────────────────┘

Example:
1. Create issue: "Add user authentication endpoint"
2. Add label: "my-repo-agent"
3. AI agent reads the issue, writes code, runs tests
4. PR appears with the implementation
```

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Your GitHub Org                                 │
│                                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                          │
│  │   Repo A    │  │   Repo B    │  │   Repo C    │                          │
│  │             │  │             │  │             │                          │
│  │ Issue +     │  │ Issue +     │  │ Issue +     │                          │
│  │ Label       │  │ Label       │  │ Label       │                          │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘                          │
│         │                │                │                                  │
│         └────────────────┼────────────────┘                                  │
│                          │                                                   │
│                          ▼                                                   │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │                    GitHub Actions Workflow                             │  │
│  │                    (triggered by label)                                │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Amazon EKS Cluster                                   │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                    Actions Runner Controller (ARC)                   │    │
│  │                    Manages runner pods                               │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                              │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐              │
│  │ arc-runners-    │  │ arc-runners-    │  │ arc-runners-    │              │
│  │ repo-a          │  │ repo-b          │  │ repo-c          │              │
│  │                 │  │                 │  │                 │              │
│  │ Runner Pod      │  │ Runner Pod      │  │ Runner Pod      │              │
│  │ ┌─────────────┐ │  │ ┌─────────────┐ │  │ ┌─────────────┐ │              │
│  │ │ AI Agent    │ │  │ │ AI Agent    │ │  │ │ AI Agent    │ │              │
│  │ │ (Claude)    │ │  │ │ (Claude)    │ │  │ │ (Claude)    │ │              │
│  │ └─────────────┘ │  │ └─────────────┘ │  │ └─────────────┘ │              │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘              │
│                                                                              │
│  IRSA: Pods have IAM permissions for Bedrock, S3, etc.                      │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Prerequisites

Before starting, ensure you have:

| Tool | Version | Installation |
|------|---------|--------------|
| AWS CLI | 2.x | `brew install awscli` or [AWS docs](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |
| Terraform | >= 1.0 | `brew install terraform` or [Terraform docs](https://developer.hashicorp.com/terraform/downloads) |
| kubectl | Latest | `brew install kubectl` or [K8s docs](https://kubernetes.io/docs/tasks/tools/) |
| Helm | >= 3.0 | `brew install helm` or [Helm docs](https://helm.sh/docs/intro/install/) |
| GitHub CLI | Latest | `brew install gh` or [GitHub CLI docs](https://cli.github.com/) |

### AWS Requirements

- An AWS account with permissions to create:
  - VPC, Subnets, NAT Gateway
  - EKS Cluster
  - IAM Roles and Policies
  - Secrets Manager secrets

### GitHub Requirements

- A GitHub account or organization
- A Personal Access Token (PAT) with scopes:
  - `repo` (full control of private repositories)
  - `workflow` (update GitHub Action workflows)
  - `admin:org` (if using organization-level runners)

---

## Complete Setup Guide

### Step 1: Clone This Repository

```bash
git clone https://github.com/YOUR_ORG/YOUR_REPO.git
cd YOUR_REPO/github-actions-runner
```

### Step 2: Configure AWS Credentials

```bash
# Configure AWS CLI with your credentials
aws configure

# Verify access
aws sts get-caller-identity
```

### Step 3: Configure Terraform Variables

```bash
cd infrastructure

# Copy the example file
cp terraform.tfvars.example terraform.tfvars

# Edit with your values
```

Edit `terraform.tfvars`:

```hcl
# Your AWS region
aws_region = "us-east-1"

# Name for your EKS cluster
cluster_name = "github-runners"

# Your GitHub organization or username
github_org = "YourGitHubOrg"

# VPC CIDR (change if conflicts with existing VPCs)
vpc_cidr = "10.0.0.0/16"
```

### Step 4: Deploy the Infrastructure

```bash
cd ../scripts

# Make scripts executable
chmod +x *.sh

# Deploy everything (VPC, EKS, IAM, ARC)
./deploy.sh
```

This takes approximately 15-20 minutes and creates:
- VPC with public/private subnets
- EKS cluster with Auto Mode (auto-scaling nodes)
- IAM roles with IRSA for runner pods
- Actions Runner Controller (ARC)

### Step 5: Configure kubectl

After deployment, configure kubectl to access your cluster:

```bash
# Get the kubeconfig command from Terraform output
cd ../infrastructure
terraform output kubeconfig_command

# Run the output command, e.g.:
aws eks update-kubeconfig --region us-east-1 --name github-runners

# Verify connection
kubectl get nodes
```

### Step 6: Store GitHub PAT in AWS Secrets Manager

```bash
cd ../scripts

# Create a GitHub PAT at: https://github.com/settings/tokens
# Required scopes: repo, workflow, admin:org (if using org)

./setup-secrets.sh ghp_your_github_pat_here
```

### Step 7: Verify Installation

```bash
# Check ARC controller is running
kubectl get pods -n arc-systems

# Should show:
# NAME                                     READY   STATUS    RESTARTS   AGE
# arc-gha-runner-scale-set-controller-xxx  1/1     Running   0          5m
```

---

## Onboarding Repositories

Once the infrastructure is set up, you can onboard repositories to use the AI agent.

### Quick Onboard (Recommended)

Use the full onboarding script that does everything:

```bash
cd scripts

# Onboard a repository
./full-onboard-repo.sh my-repo-name

# Or with a custom agent label
./full-onboard-repo.sh my-repo-name custom-agent-label
```

This script:
1. Creates Kubernetes namespace for the repo
2. Sets up IRSA (IAM Roles for Service Accounts)
3. Installs the runner scale set via Helm
4. Clones the repository
5. Copies the AI agent code (`.github-agent/`)
6. Creates the GitHub Actions workflow
7. Creates the trigger label
8. Pushes everything to GitHub

### Manual Onboard

If you need more control, see [REPO-ONBOARDING.md](REPO-ONBOARDING.md) for step-by-step instructions.

---

## Using the AI Agent

After onboarding a repository:

### 1. Create an Issue

```bash
gh issue create --repo YourOrg/your-repo \
  --title "Add user authentication endpoint" \
  --body "## Objective
Add a /api/auth endpoint that handles user login.

## Requirements
- Accept username and password
- Return JWT token
- Add rate limiting

## Success Criteria
- [ ] Endpoint works
- [ ] Tests pass"
```

### 2. Add the Agent Label

```bash
gh issue edit 1 --repo YourOrg/your-repo --add-label "your-repo-agent"
```

### 3. Watch the Magic

The agent will:
1. Pick up the issue
2. Analyze the codebase
3. Plan the implementation
4. Write the code
5. Run tests
6. Create a Pull Request

### 4. Retry if Needed

If the agent fails, comment `/retry` on the issue:

```bash
gh issue comment 1 --repo YourOrg/your-repo --body "/retry"
```

---

## Project Structure

```
github-actions-runner/
├── infrastructure/           # Terraform code for AWS resources
│   ├── main.tf              # Provider configuration
│   ├── vpc.tf               # VPC, subnets, NAT gateway
│   ├── eks.tf               # EKS cluster configuration
│   ├── iam.tf               # IAM roles and policies
│   ├── variables.tf         # Input variables
│   ├── outputs.tf           # Output values
│   └── terraform.tfvars     # Your configuration (git-ignored)
│
├── helm/                     # Helm chart configurations
│   ├── arc-controller-values.yaml
│   └── arc-runner-set-values.yaml.tpl
│
├── scripts/                  # Automation scripts
│   ├── deploy.sh            # Deploy all infrastructure
│   ├── setup-secrets.sh     # Store GitHub PAT
│   ├── onboard-repo.sh      # EKS-only repo setup
│   ├── full-onboard-repo.sh # Complete repo onboarding
│   └── offboard-repo.sh     # Remove a repository
│
├── README.md                 # This file
└── REPO-ONBOARDING.md       # Detailed onboarding guide
```

---

## IAM Permissions

Each onboarded repository gets its own IAM role for fine-grained access control.

### Per-Repository Roles

When you onboard a repo, the script creates:
- IAM Role: `github-runner-<repo-name>`
- Inline Policy: `github-runner-<repo-name>-policy`

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         IAM Role Architecture                                │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                    Permissions Boundary                              │    │
│  │                    (Shared, prevents dangerous actions)              │    │
│  │                                                                      │    │
│  │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐     │    │
│  │  │ github-runner-  │  │ github-runner-  │  │ github-runner-  │     │    │
│  │  │ repo-a          │  │ repo-b          │  │ repo-c          │     │    │
│  │  │                 │  │                 │  │                 │     │    │
│  │  │ Custom policy   │  │ Custom policy   │  │ Custom policy   │     │    │
│  │  │ for repo-a      │  │ for repo-b      │  │ for repo-c      │     │    │
│  │  └─────────────────┘  └─────────────────┘  └─────────────────┘     │    │
│  │                                                                      │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Default Permissions

New repos get a broad default policy including:

| Service | Permissions | Purpose |
|---------|-------------|---------|
| Bedrock | `InvokeModel` | AI agent uses Claude |
| S3 | Full access | Store artifacts, data |
| EC2 | Full access | Infrastructure tasks |
| Lambda | Full access | Serverless deployments |
| DynamoDB | Full access | Database operations |
| CloudFormation | Full access | IaC deployments |
| Secrets Manager | Read-only | Access secrets |
| IAM | Create roles (limited) | Create service roles |

### Customizing Permissions

After onboarding, customize the IAM policy for your project's needs:

```bash
# View current policy
aws iam get-role-policy \
  --role-name github-runner-<repo-name> \
  --policy-name github-runner-<repo-name>-policy

# Create a custom policy file
cat > custom-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockAccess",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": "*"
    },
    {
      "Sid": "S3LimitedAccess",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::my-specific-bucket/*"
    }
  ]
}
EOF

# Update the policy
aws iam put-role-policy \
  --role-name github-runner-<repo-name> \
  --policy-name github-runner-<repo-name>-policy \
  --policy-document file://custom-policy.json
```

### Permissions Boundary (Guardrails)

All roles have a permissions boundary that blocks dangerous actions:

### Permissions Boundary (Guardrails)

All roles have a permissions boundary that blocks dangerous actions:

- `iam:CreateUser`, `iam:DeleteUser`
- `iam:CreateAccessKey`
- `organizations:*`
- `billing:*`
- `account:*`

Even if you add these to a role's policy, the boundary will deny them.

---

## Cost Estimation

| Component | Monthly Cost | Notes |
|-----------|-------------|-------|
| EKS Control Plane | ~$73 | Fixed cost |
| NAT Gateway | ~$32 + data transfer | Required for private subnets |
| EC2 Nodes | Variable | $0 when idle (Auto Mode) |
| Secrets Manager | ~$0.40/secret | Per secret stored |

**Total when idle**: ~$105/month
**Total when active**: Depends on usage

---

## Troubleshooting

### Check if runners are registered

```bash
kubectl get pods -n arc-runners-<repo-name>
```

### Check ARC controller logs

```bash
kubectl logs -n arc-systems -l app.kubernetes.io/name=gha-runner-scale-set-controller
```

### Check runner pod logs

```bash
kubectl logs -n arc-runners-<repo-name> -l app.kubernetes.io/component=runner
```

### Verify IRSA is working

```bash
kubectl describe sa github-runner-sa -n arc-runners-<repo-name>
# Should show: eks.amazonaws.com/role-arn annotation
```

### Workflow not triggering

1. Check the label name matches exactly
2. Verify the workflow file exists in `.github/workflows/`
3. Check GitHub Actions is enabled for the repo

### Agent fails with AWS errors

1. Verify IRSA annotation on service account
2. Check IAM role trust policy includes the OIDC provider
3. Verify permissions boundary allows the action

---

## Cleanup

### Remove a single repository

```bash
./scripts/offboard-repo.sh <repo-name>
```

### Remove all repositories

```bash
kubectl get ns | grep arc-runners | awk '{print $1}' | \
  sed 's/arc-runners-//' | \
  xargs -I {} ./scripts/offboard-repo.sh {}
```

### Destroy all infrastructure

```bash
# First remove all repos (above)

# Uninstall ARC controller
helm uninstall arc-controller -n arc-systems
kubectl delete ns arc-systems

# Destroy Terraform resources
cd infrastructure
terraform destroy
```

---

## For AI Agents

If you're an AI agent reading this to help set up the infrastructure:

### Quick Setup Commands

```bash
# 1. Navigate to the infrastructure directory
cd github-actions-runner

# 2. Configure Terraform (edit terraform.tfvars with user's values)
cd infrastructure
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars

# 3. Deploy infrastructure
cd ../scripts
./deploy.sh

# 4. Configure kubectl (get command from terraform output)
cd ../infrastructure
terraform output kubeconfig_command
# Run the output command

# 5. Store GitHub PAT
cd ../scripts
./setup-secrets.sh <github-pat>

# 6. Onboard a repository
./full-onboard-repo.sh <repo-name>
```

### Key Files to Modify

| File | What to Change |
|------|----------------|
| `infrastructure/terraform.tfvars` | AWS region, cluster name, GitHub org |
| `scripts/onboard-repo.sh` | GitHub org (if different from tfvars) |

### Verification Commands

```bash
# Check EKS cluster
kubectl get nodes

# Check ARC controller
kubectl get pods -n arc-systems

# Check runner namespace
kubectl get all -n arc-runners-<repo-name>

# Check GitHub runner registration
gh api repos/<org>/<repo>/actions/runners
```

---

## Auto-Fix on CI Failure

You can configure the agent to automatically fix CI failures. When a CI pipeline fails, an issue is created with the error logs, and the agent picks it up to create a fix PR.

### Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     Auto-Fix Architecture                                    │
└─────────────────────────────────────────────────────────────────────────────┘

  Any Repo's CI Fails                         EKS Cluster
  ──────────────────                          ───────────

  Repo A: CI fails ─────┐
  Repo B: CI fails ─────┼──── runs-on: arc-runner-auto-fix ────→  Shared Pod
  Repo C: CI fails ─────┘     (org-level runner)                  (creates issue)
                                                                       │
                                                                       ▼
                                                              Issue in failed repo
                                                              with agent label
                                                                       │
                                                                       ▼
                                                              Per-repo agent
                                                              picks up & fixes
```

The `arc-runner-auto-fix` is an **organization-level runner** that can serve any repo. This allows unlimited parallel auto-fix jobs without consuming GitHub Actions minutes.

### Add Auto-Fix to a Repository

```bash
cd scripts

# Add to all workflows in a repo
./add-auto-fix-to-repo.sh my-repo

# Add to a specific workflow
./add-auto-fix-to-repo.sh my-repo ci.yml
```

### How It Works

1. Your existing CI workflow fails
2. The auto-fix job runs on the shared EKS runner
3. It creates an issue with:
   - Detailed instructions for the agent
   - Error logs from the failed run
   - Link to the failed workflow
4. The issue is labeled with your repo's agent label
5. Your existing per-repo agent picks up the issue
6. Agent analyzes logs, fixes code, creates PR

### Manual Setup

Add this job to any workflow:

```yaml
jobs:
  build:
    # ... your existing CI ...

  auto-fix-on-failure:
    needs: [build]  # List all jobs that should trigger auto-fix
    if: failure()
    runs-on: arc-runner-auto-fix  # Shared org-level EKS runner
    steps:
      - name: Create fix issue
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          # Compute agent label from repo name
          REPO_NAME=$(echo "${{ github.repository }}" | cut -d'/' -f2)
          REPO_NAME_LOWER=$(echo "$REPO_NAME" | tr '[:upper:]' '[:lower:]' | tr '_' '-')
          AGENT_LABEL="${REPO_NAME_LOWER}-agent"
          
          # Fetch logs
          gh api repos/${{ github.repository }}/actions/runs/${{ github.run_id }}/logs > /tmp/logs.zip
          unzip /tmp/logs.zip -d /tmp/logs
          cat /tmp/logs/*/*.txt | tail -c 15000 > /tmp/logs.txt
          
          # Create issue with agent label
          gh issue create \
            --title "🔧 Auto-fix: CI failure in ${{ github.workflow }}" \
            --body "## Instructions
          Analyze the logs and fix the error.
          
          **Run**: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}
          
          \`\`\`
          $(cat /tmp/logs.txt)
          \`\`\`" \
            --label "$AGENT_LABEL"
```

---

## Additional Resources

- [REPO-ONBOARDING.md](REPO-ONBOARDING.md) - Detailed repository onboarding guide
- [AGENT-INSTRUCTIONS.md](../AGENT-INSTRUCTIONS.md) - How to create issues for agents
- [Actions Runner Controller Docs](https://github.com/actions/actions-runner-controller)
- [EKS Auto Mode Docs](https://docs.aws.amazon.com/eks/latest/userguide/automode.html)
- [Amazon Bedrock Docs](https://docs.aws.amazon.com/bedrock/)
