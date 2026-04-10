# ADP Client Workflows

Drop-in GitHub Actions workflows for any repo that wants to use ADP agents.

## Prerequisites

1. Your repo must be in the **same GitHub organization** as the `adp` repo (e.g. `aws-innovate`)
2. The `adp` repo's reusable workflows must allow calls from your org (Settings → Actions → Access)
3. GitHub App credentials must be stored in AWS Secrets Manager (the adp workflows handle this)
4. An ARC runner labeled `arc-runner-org` must be available to the org

## Setup

1. Copy the `.github/workflows/` files from this folder into your repo's `.github/workflows/` directory:

```bash
# From your repo root
cp -r path/to/adp/adp-client-workflows/.github/workflows/ .github/workflows/
```

2. (Optional) If your org name is not `aws-innovate`, do a find-and-replace:

```bash
# Replace org name in all workflow files
sed -i '' 's|aws-innovate/adp|YOUR_ORG/adp|g' .github/workflows/call-*.yml .github/workflows/pr-review-trigger.yml
```

3. Commit and push. That's it.

## What's Included

| Workflow | Trigger | What it does |
|----------|---------|-------------|
| `call-agent-developer.yml` | Label `agent-developer` on issue | Calls adp's developer agent to implement code |
| `call-agent-architect.yml` | Label `agent-architect` on issue | Calls adp's architect agent for design work |
| `call-agent-reviewer.yml` | Label `agent-reviewer` on issue or `workflow_dispatch` | Calls adp's reviewer agent for code review |
| `call-agent-operations.yml` | Label `agent-operations` on issue | Calls adp's operations agent for infra tasks |
| `call-agent-pm.yml` | Label `agent-pm` on issue | Calls adp's PM agent for project management |
| `call-agent-product.yml` | Label `agent-product` on issue | Calls adp's product agent |
| `call-agent-pt-superpower.yml` | Label `agent-pt-superpower` on issue | Calls adp's PT superpower agent |
| `call-skill-agent.yml` | Label `skill-agent` on issue | Calls adp's skill agent |
| `pr-review-trigger.yml` | PR opened/updated | Auto-triggers reviewer agent on PRs |

## How It Works

Each `call-*.yml` is a thin wrapper that:
1. Listens for a label event on issues
2. Calls the corresponding reusable workflow in `adp` via `workflow_call`
3. Passes the issue number, repo owner, repo name, and target repo
4. Uses `secrets: inherit` so the adp workflow can access org-level secrets

The adp reusable workflows handle everything else: checking out your repo, running the agent, creating PRs, posting comments, etc.

## Customization

- To disable an agent, simply don't copy its workflow file
- To pin to a specific adp version, change `@main` to a tag like `@v1.0.0`
