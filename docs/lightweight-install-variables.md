# Lightweight Install Variables Reference

This document lists every GitHub Actions repository variable (`vars.*`) that the ADP agent workflows read, along with default values and what a lightweight installer should set.

## Variables

| Variable | Default | Description | Lightweight install sets to |
|----------|---------|-------------|---------------------------|
| `ARC_RUNNER_LABEL` | `arc-runner-org` | The `runs-on:` label for ARC self-hosted runners. Must match the label configured in your ARC RunnerScaleSet. | Your ARC runner label (e.g. `arc-runner-org`) |
| `SECRET_PREFIX` | `adp/aws-e` | Prefix for AWS Secrets Manager secret IDs. Secrets are stored as `<prefix>/gh-app-dev-id`, `<prefix>/gh-app-dev-key`, etc. | `adp/<your-github-org>` |
| `AWS_REGION` | `us-east-1` | AWS region for Secrets Manager reads, EKS cluster, and Bedrock API calls. | Your AWS region (e.g. `us-west-2`) |
| `EKS_CLUSTER` | `bedrockgw-dev-eks-cluster` | EKS cluster name used by `aws eks update-kubeconfig` in the operations and skill-agent workflows. Only needed if agents require kubectl access. | Your EKS cluster name |
| `BEADS_ENABLED` | `true` | Set to `false` to skip the Beads task-management system (DynamoDB + S3 state store). Lightweight installs should always set this to `false`. | `false` |
| `BEADS_DYNAMODB_TABLE` | `adp-beads-manifest` | DynamoDB table for Beads manifests. Irrelevant when `BEADS_ENABLED=false`. | _(not set)_ |
| `BEADS_S3_BUCKET` | `adp-beads-state-193832579677` | S3 bucket for Beads state. Irrelevant when `BEADS_ENABLED=false`. | _(not set)_ |
| `BEADS_DATABASE` | `adp` | Beads database name. Irrelevant when `BEADS_ENABLED=false`. | _(not set)_ |

## How workflows use these variables

All variables use the GitHub Actions expression `${{ vars.X || 'default' }}`, which falls back to the default when the variable is unset or empty. This means:

- **On `aws-e/adp`** (the original deployment): no variables need to be set. Defaults match the existing hardcoded values.
- **On a cloned repo**: set `SECRET_PREFIX`, `ARC_RUNNER_LABEL`, `AWS_REGION`, `EKS_CLUSTER`, and `BEADS_ENABLED=false` to point at your infrastructure.

## Secrets Manager layout

The workflows expect these secrets under your chosen prefix:

| Secret ID | Contains |
|-----------|----------|
| `<SECRET_PREFIX>/gh-app-dev-id` | GitHub App ID (numeric) for the dev persona |
| `<SECRET_PREFIX>/gh-app-dev-key` | GitHub App private key (PEM) for the dev persona |
| `<SECRET_PREFIX>/gh-app-pm-id` | GitHub App ID for the PM persona |
| `<SECRET_PREFIX>/gh-app-pm-key` | GitHub App private key for the PM persona |
| `<SECRET_PREFIX>/gh-app-ops-id` | GitHub App ID for the ops persona |
| `<SECRET_PREFIX>/gh-app-ops-key` | GitHub App private key for the ops persona |

For a minimal install, you can use a single GitHub App for all three personas. Store the same App ID and key under all three prefixes (`dev`, `pm`, `ops`). The only trade-off is shared rate limits (5000 requests/hour across all agents instead of per-persona).

## Which workflows read which variables

| Workflow | `ARC_RUNNER_LABEL` | `SECRET_PREFIX` | `AWS_REGION` | `EKS_CLUSTER` | `BEADS_ENABLED` |
|----------|-------------------|-----------------|-------------|--------------|----------------|
| `agent-developer.yml` | Yes | Yes (dev) | Yes | - | Yes |
| `agent-pm.yml` | Yes | Yes (pm) | Yes | - | Yes |
| `agent-operations.yml` | Yes | Yes (ops) | Yes | Yes | Yes |
| `agent-architect.yml` | Yes | Yes (dev) | Yes | - | Yes |
| `agent-product.yml` | Yes | Yes (dev) | Yes | - | Yes |
| `agent-reviewer.yml` | Yes | Yes (ops) | Yes | - | Yes |
| `agent-pt-superpower.yml` | Yes | Yes (ops) | Yes | - | Yes |
| `skill-agent.yml` | Yes | Yes (ops) | Yes | Yes | - |
