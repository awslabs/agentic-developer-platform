# Lightweight Install (Agents Only)

Run ADP's autonomous code agents on your own AWS infrastructure — without deploying the gateway, chat widget, or any other platform module. Label a GitHub issue, and an AI agent picks it up, writes code, and opens a PR.

## What this enables

The ADP agent workflows (`@agent-developer`, `@agent-pm`, `@agent-operations`, `@agent-architect`, `@agent-product`, `@agent-reviewer`, `@agent-pt-superpower`) run on your own EKS cluster via ARC self-hosted runners, using Bedrock Claude for AI and your own Secrets Manager for GitHub App credentials. This install covers **agents only** — it does not include the Bedrock gateway proxy, chat widget, user vault, budget/quota enforcement, or the Agent Context (code search/wiki) module.

## Prerequisites

Before running the setup script, you need:

- [ ] **AWS account** with [credentials configured](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html) (`aws sts get-caller-identity` succeeds)
- [ ] **Bedrock model access** for [`anthropic.claude-opus-4-6`](https://console.aws.amazon.com/bedrock/home#/modelaccess) and optionally `anthropic.claude-haiku-4-5` in your target region — [request access here](https://console.aws.amazon.com/bedrock/home#/modelaccess)
- [ ] **EKS cluster** with [OIDC provider](https://docs.aws.amazon.com/eks/latest/userguide/enable-iam-roles-for-service-accounts.html) enabled (for IRSA)
- [ ] **ARC runners** installed on the cluster, with a known label (e.g. `arc-runner-org`) — [ARC quickstart](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners-with-actions-runner-controller/quickstart-for-actions-runner-controller)
- [ ] **GitHub App** created with these permissions:
  - Repository permissions: **Issues** (Read & Write), **Pull Requests** (Read & Write), **Contents** (Read & Write), **Metadata** (Read)
  - [Create a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app)
  - After creating, generate a private key (downloads a `.pem` file)
- [ ] **GitHub App installed** on the target repo — go to your App's settings page → Install App → select your org → select the repo
- [ ] **CLI tools installed locally**: [`aws`](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), [`gh`](https://cli.github.com/), [`kubectl`](https://kubernetes.io/docs/tasks/tools/)

## Quick start

Clone the repo and run the setup script (~10 minutes interactive, plus ~5 minutes for runner restart):

```bash
git clone https://github.com/<your-org>/adp.git
cd adp
./platform/scripts/lightweight-setup.sh
```

The script is fully interactive — it prompts for every value with sensible defaults.

## What the script does

1. **Validates prerequisites** — checks `aws`, `gh`, `kubectl` are installed and authenticated; warns if Bedrock model access is missing
2. **Collects configuration** — GitHub org, repo name, AWS region, EKS cluster, ARC runner label, Secrets Manager prefix, GitHub App credentials
3. **Creates IAM role** (`adp-agent-runner-role`) with a trust policy for IRSA from your ARC runner service account, with inline permissions for Bedrock invoke and Secrets Manager read
4. **Stores GitHub App credentials** in Secrets Manager under your chosen prefix (`<prefix>/gh-app-dev-id`, `<prefix>/gh-app-dev-key`, etc.)
5. **Annotates the runner service account** with `eks.amazonaws.com/role-arn` so pods get AWS credentials via IRSA
6. **Restarts ARC runner pods** to pick up the new IRSA annotation
7. **Sets GitHub repo variables** (`SECRET_PREFIX`, `ARC_RUNNER_LABEL`, `EKS_CLUSTER`, `BEADS_ENABLED=false`, and `AWS_REGION` if non-default)
8. **Creates issue labels** (`agent-developer`, `agent-pm`, etc.) on the repo
9. **Runs a smoke test** — creates a test issue labelled `agent-developer` and checks that the workflow triggers

## The two manual browser steps

These are the only parts that require a browser. Do them **before** running the setup script.

### 1. Create a GitHub App

1. Go to `https://github.com/organizations/<your-org>/settings/apps/new`
2. Fill in:
   - **Name**: `<your-org>-adp-agent-dev` (must be globally unique)
   - **Homepage URL**: `https://github.com/<your-org>/adp` (or any URL)
   - **Webhook**: uncheck "Active" (not needed)
3. Under **Repository permissions**, set:
   - Issues → Read & Write
   - Pull requests → Read & Write
   - Contents → Read & Write
   - Metadata → Read (auto-selected)
4. Click **Create GitHub App**
5. Note the **App ID** shown at the top of the page
6. Scroll down to **Private keys** → click **Generate a private key** (saves a `.pem` file)

For a minimal setup, one App is sufficient for all agent personas. For production, create separate Apps for `dev`, `pm`, and `ops` to get independent rate limits (5000 requests/hour each).

### 2. Install the App on your repo

1. Go to `https://github.com/organizations/<your-org>/settings/apps/<app-name>/installations`
2. Click **Install**
3. Select **Only select repositories** → choose your `adp` repo (or whichever repo has the workflows)
4. Click **Install**

## After setup: how to use

Label any issue with `agent-developer` to trigger the developer agent:

```bash
# Create an issue and trigger the agent
gh issue create --title "feat: add hello world endpoint" \
  --body "Add a GET /hello endpoint that returns 'Hello, World!'" \
  --label agent-developer

# Watch the workflow run
gh run list --workflow agent-developer.yml --limit 5

# Tail the latest run
gh run watch $(gh run list --workflow agent-developer.yml --limit 1 --json databaseId --jq '.[0].databaseId')
```

The agent will:
1. Post a "Started" comment on the issue
2. Read the issue, plan the implementation
3. Write code and tests
4. Open a PR linking to the issue
5. Post a "Complete" comment

Other available labels: `agent-pm`, `agent-operations`, `agent-architect`, `agent-product`, `agent-reviewer`, `agent-pt-superpower`, `skill-agent`.

## Troubleshooting

### Runner queued indefinitely (never starts)

**Symptom**: Workflow shows "Waiting for a runner to pick up this job" and never starts.

**Root cause**: The `runs-on:` label doesn't match your ARC runner label.

**Fix**: Check your ARC RunnerScaleSet label and update the repo variable:
```bash
gh variable set ARC_RUNNER_LABEL --body "<your-actual-label>"
```

### AccessDenied on SecretsManager

**Symptom**: `An error occurred (AccessDeniedException) when calling the GetSecretValue operation`

**Root cause**: IRSA annotation not applied or runner pods not restarted after annotation.

**Fix**:
```bash
# Verify annotation
kubectl describe sa <runner-sa> -n <runner-ns> | grep eks.amazonaws.com/role-arn

# If missing, annotate and restart
kubectl annotate sa <runner-sa> -n <runner-ns> eks.amazonaws.com/role-arn=<role-arn> --overwrite
kubectl delete pods -n <runner-ns> --all
```

### AccessDenied on Bedrock

**Symptom**: `An error occurred (AccessDeniedException) when calling the InvokeModel operation`

**Root cause**: Bedrock model access not enabled in your region.

**Fix**: Go to [Bedrock Model Access](https://console.aws.amazon.com/bedrock/home#/modelaccess) and enable `Anthropic Claude (Opus 4, Haiku 4.5)` for your region.

### 404 on GitHub App token mint

**Symptom**: `Error: An installation was not found for this app`

**Root cause**: App ID is wrong or the App is not installed on the repo.

**Fix**:
1. Verify the App ID matches what's in Secrets Manager:
   ```bash
   aws secretsmanager get-secret-value --secret-id "<prefix>/gh-app-dev-id" --query SecretString --output text
   ```
2. Verify the App is installed on the repo: go to `https://github.com/organizations/<org>/settings/installations`

### BEADS_S3_BUCKET error

**Symptom**: Job fails at the "Setup Beads" step with S3 or DynamoDB errors.

**Root cause**: `BEADS_ENABLED=false` repo variable is not set.

**Fix**:
```bash
gh variable set BEADS_ENABLED --body "false"
```

### `gh variable set` auth failure

**Symptom**: `HTTP 403: Must have admin rights to Repository`

**Root cause**: Your `gh` auth token doesn't have the `repo` scope.

**Fix**:
```bash
gh auth login --scopes repo
```

## Tear-down

To remove everything created by the setup script:

```bash
./platform/scripts/lightweight-teardown.sh <github-org> <repo-name> <secret-prefix> [aws-region]
# Example:
./platform/scripts/lightweight-teardown.sh acmecorp adp adp/acmecorp us-east-1
```

This deletes:
- IAM role `adp-agent-runner-role` and its inline policies
- Secrets Manager secrets under your prefix
- GitHub repo variables (`SECRET_PREFIX`, `ARC_RUNNER_LABEL`, `EKS_CLUSTER`, `BEADS_ENABLED`, `AWS_REGION`)
- Optionally, issue labels

It does **not** delete:
- The GitHub App itself (delete from `https://github.com/organizations/<org>/settings/apps`)
- Your EKS cluster, ARC installation, or any other infrastructure

## What's NOT included

This lightweight install does **not** set up:

- **Bedrock Gateway** — the multi-tenant Bedrock proxy with Cognito auth, budgets, and admin UI (`modules/gateway/`)
- **Chat widget** — the AG-UI-based agent chat interface
- **Agent Context** — semantic search, code search, wikis, memory via MCP (`modules/agent-context/`)
- **User Services** — per-user vault, knowledge repo, bespoke agents (`modules/user-services/`)
- **Budget/quota enforcement** — per-tenant usage tracking and limits
- **Beads task management** — the DynamoDB + S3 shared state system (disabled by default in lightweight installs)

For the full platform install, see the main [README.md](../README.md) and [CLAUDE.md](../CLAUDE.md) deployment playbook.

## Variables reference

See [docs/lightweight-install-variables.md](./lightweight-install-variables.md) for the complete table of every `vars.*` the workflows read, with defaults and what to set for a lightweight install.
