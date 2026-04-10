# Repository Onboarding Guide

This guide explains how to onboard a new GitHub repository to use the AI agent workflow with the EKS-based GitHub Actions runner.

## Prerequisites

Before onboarding a new repo, ensure you have:

1. **Local tools installed:**
   - `kubectl` configured with EKS cluster access
   - `helm` v3+
   - `aws` CLI configured with appropriate credentials
   - `gh` CLI authenticated with GitHub

2. **EKS infrastructure deployed:**
   - GitHub Actions Runner Controller installed
   - Runner role ARN available in Terraform outputs

3. **GitHub repository created:**
   - The repo must exist in the `PranavSharma1000` organization

## Quick Start (Recommended)

Use the full onboarding script to set up everything in one command:

```bash
cd github-actions-runner/scripts
./full-onboard-repo.sh <repo-name> [agent-label-name]
```

**Examples:**
```bash
# Onboard with default label (repo-name-agent)
./full-onboard-repo.sh my-new-repo

# Onboard with custom label
./full-onboard-repo.sh my-new-repo custom-agent
```

**What this script does:**
1. Creates EKS namespace and IRSA service account
2. Creates a dedicated IAM role for this repo
3. Attaches default permissions policy
4. Installs GitHub Actions runner scale set via Helm
5. Clones the target repository
6. Copies `.github-agent/` folder (agent code)
7. Creates customized workflow file
8. Creates the trigger label in GitHub
9. Commits and pushes all changes

## Manual Onboarding (Step by Step)

If you need more control, you can run the steps manually:

### Step 1: EKS Runner Setup

```bash
./onboard-repo.sh <repo-name>
```

This creates:
- Kubernetes namespace: `arc-runners-<repo-name>`
- IAM role: `github-runner-<repo-name>`
- IAM policy: `github-runner-<repo-name>-policy`
- Service account with IRSA annotation
- Kubernetes secret with GitHub PAT
- Helm release for runner scale set

### Step 2: Clone Target Repository

```bash
gh repo clone PranavSharma1000/<repo-name> /tmp/<repo-name>
```

### Step 3: Copy Agent Files

```bash
cp -r /path/to/cc-sdk-agent/.github-agent /tmp/<repo-name>/
rm -rf /tmp/<repo-name>/.github-agent/agent/node_modules
```

### Step 4: Create Workflow File

Create `.github/workflows/agent-trigger.yml` with:

```yaml
name: AI Agent Trigger

on:
  issues:
    types: [labeled]
  issue_comment:
    types: [created]

concurrency:
  group: agent-issue-${{ github.event.issue.number }}
  cancel-in-progress: false

jobs:
  run-agent:
    if: |
      (github.event_name == 'issues' && github.event.label.name == '<your-label>') ||
      (github.event_name == 'issue_comment' &&
      contains(github.event.comment.body, '/retry') &&
      contains(github.event.issue.labels.*.name, '<your-label>'))
    runs-on: arc-runner-<repo-name>

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Configure Git
        run: |
          git config --global user.email "agent@<repo-name>.local"
          git config --global user.name "<Repo-Name> Agent"

      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: '24'
          cache: 'npm'
          cache-dependency-path: .github-agent/agent/package-lock.json

      - name: Install dependencies
        working-directory: ./.github-agent/agent
        run: npm ci

      - name: Build agent
        working-directory: ./.github-agent/agent
        run: npm run build

      - name: Run agent
        working-directory: ./.github-agent/agent
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          ISSUE_NUMBER: ${{ github.event.issue.number }}
          REPO_OWNER: ${{ github.repository_owner }}
          REPO_NAME: ${{ github.event.repository.name }}
          CLAUDE_CODE_USE_BEDROCK: "1"
          ANTHROPIC_MODEL: "us.anthropic.claude-sonnet-4-20250514-v1:0"
          SECRET_PREFIX: "<your-label>"
        run: npm start
```

**Replace:**
- `<your-label>` with your agent trigger label (e.g., `my-repo-agent`)
- `<repo-name>` with your repository name

### Step 5: Create GitHub Label

```bash
gh label create <your-label> \
    --repo PranavSharma1000/<repo-name> \
    --description "Trigger AI agent to work on this issue" \
    --color "0E8A16"
```

### Step 6: Commit and Push

```bash
cd /tmp/<repo-name>
git add .
git commit -m "Add AI agent workflow and configuration"
git push origin main
```

## Using the Agent

Once onboarded, trigger the agent by:

1. Create an issue in the repository
2. Add the agent label (e.g., `my-repo-agent`)
3. The workflow will start automatically
4. Agent will work on the issue and create a PR

To retry a failed run, comment `/retry` on the issue.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Your Machine (Local)                          │
│                                                                      │
│  full-onboard-repo.sh                                                │
│  ├── Runs onboard-repo.sh (EKS + IAM setup)                          │
│  │   ├── Creates IAM role: github-runner-<repo>                      │
│  │   ├── Attaches default policy                                     │
│  │   └── Creates K8s namespace + ServiceAccount                      │
│  ├── Clones repo                                                     │
│  ├── Copies .github-agent/                                           │
│  ├── Creates workflow                                                │
│  ├── Creates label                                                   │
│  └── Pushes to GitHub                                                │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          GitHub Repository                           │
│                                                                      │
│  .github/workflows/agent-trigger.yml  ← Workflow definition          │
│  .github-agent/                       ← Agent code                   │
│  └── agent/                                                          │
│      ├── src/                         ← TypeScript source            │
│      ├── package.json                                                │
│      └── ...                                                         │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                │ Issue labeled
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          EKS Cluster                                 │
│                                                                      │
│  Namespace: arc-runners-<repo-name>                                  │
│  ├── Runner Scale Set (Helm)                                         │
│  ├── Service Account (IRSA) ──────┐                                  │
│  └── Secret (GitHub PAT)          │                                  │
│                                   │                                  │
│  When workflow triggers:          │                                  │
│  1. Runner pod spins up           │                                  │
│  2. Checks out repo               │                                  │
│  3. Assumes IAM role via IRSA ◄───┘                                  │
│  4. Builds and runs agent                                            │
│  5. Agent works on issue                                             │
│  6. Creates PR                                                       │
│  7. Pod terminates                                                   │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                │ IRSA
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          AWS IAM                                     │
│                                                                      │
│  Role: github-runner-<repo-name>                                     │
│  ├── Trust: OIDC provider (EKS)                                      │
│  ├── Policy: github-runner-<repo-name>-policy                        │
│  └── Boundary: github-arc-runner-runner-boundary                     │
│                                                                      │
│  Each repo has isolated permissions!                                 │
└─────────────────────────────────────────────────────────────────────┘
```

## Customizing IAM Permissions

Each repo gets its own IAM role with default broad permissions. Customize for your project:

### View Current Policy

```bash
aws iam get-role-policy \
  --role-name github-runner-<repo-name> \
  --policy-name github-runner-<repo-name>-policy
```

### Update Policy

```bash
# Create custom policy
cat > my-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockOnly",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": "*"
    },
    {
      "Sid": "S3Specific",
      "Effect": "Allow",
      "Action": ["s3:*"],
      "Resource": ["arn:aws:s3:::my-bucket", "arn:aws:s3:::my-bucket/*"]
    }
  ]
}
EOF

# Apply it
aws iam put-role-policy \
  --role-name github-runner-<repo-name> \
  --policy-name github-runner-<repo-name>-policy \
  --policy-document file://my-policy.json
```

### Common Customizations

| Scenario | Change |
|----------|--------|
| S3 only for specific bucket | Replace `"Resource": "*"` with bucket ARN |
| No database access | Remove DynamoDB/RDS statements |
| Read-only S3 | Change `s3:*` to `s3:GetObject`, `s3:ListBucket` |
| Add SageMaker | Add `sagemaker:*` statement |

## Troubleshooting

### Runner not picking up jobs

Check if the runner is registered:
```bash
kubectl get pods -n arc-runners-<repo-name>
```

Check runner scale set status:
```bash
helm status arc-runner-<repo-name> -n arc-runners-<repo-name>
```

### Workflow fails with "no matching runner"

Ensure the `runs-on` value matches the helm release name:
```yaml
runs-on: arc-runner-<repo-name>  # Must match helm release
```

### Agent fails to authenticate

Check that the GitHub PAT in Secrets Manager has required permissions:
- `repo` (full control)
- `workflow` (if updating workflows)

### IRSA not working

Verify service account annotation:
```bash
kubectl get sa github-runner-sa -n arc-runners-<repo-name> -o yaml
```

Should show:
```yaml
annotations:
  eks.amazonaws.com/role-arn: arn:aws:iam::ACCOUNT:role/github-runner-<repo-name>
```

Verify the IAM role exists:
```bash
aws iam get-role --role-name github-runner-<repo-name>
```

## Onboarded Repositories

| Repository | Runner | Label | IAM Role |
|------------|--------|-------|----------|
| cc-sdk-agent | arc-runner-cc-sdk-agent | cc-sdk-agent | github-runner-cc-sdk-agent |
| litellm-e | arc-runner-litellm-e | litellm-agent | github-runner-litellm-e |
| bedrock-gateway | arc-runner-bedrock-gateway | bedrock-gateway-agent | github-runner-bedrock-gateway |
| AISuperPlane | arc-runner-aisuperplane | superplane-agent | github-runner-aisuperplane |
| ac-migrate | arc-runner-ac-migrate | ac-migrate-agent | github-runner-ac-migrate |

**Note:** Repos onboarded before per-repo IAM roles may still use the shared role. Re-run `onboard-repo.sh` to create dedicated roles.

## Scripts Reference

| Script | Purpose |
|--------|---------|
| `onboard-repo.sh` | EKS + IAM setup (namespace, per-repo IAM role, helm) |
| `full-onboard-repo.sh` | Complete onboarding (EKS + IAM + repo files + label) |
| `offboard-repo.sh` | Remove repo (helm, namespace, IAM role cleanup) |
| `add-auto-fix-to-repo.sh` | Add auto-fix on CI failure to a repo's workflows |
| `setup-secrets.sh` | Store GitHub PAT in Secrets Manager |
