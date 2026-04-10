#!/bin/bash
REPO="PranavSharma1000/bedrock-gateway"

# Update MCP-3: Config now in monorepo folder, not separate repo
gh issue edit 183 --repo "$REPO" \
  --title "Unit MCP-3: MCP Server Config & Deployment Pipelines (Monorepo)" \
  --body "## Unit MCP-3: MCP Server Config & Deployment Pipelines

**Agent Unit** — can run in parallel with MCP-1, MCP-2, MCP-4 AND all BedrockGateway units.
**Scope**: \`mcp-servers/\` folder in this monorepo + \`.github/workflows/mcp-*.yml\`
**Depends on**: EKS cluster, ECR repos, EKS self-hosted runners.
**CHANGE: Now a folder in this monorepo, NOT a separate GitHub repo.**

### What You're Building
A \`mcp-servers/\` folder in the bedrock-gateway monorepo for MCP server catalog definitions and org deployment configs, plus GitHub Actions workflows (path-filtered) that deploy/remove MCP servers on EKS.

### Folder Structure to Create

\`\`\`
mcp-servers/
  catalogue/
    github-mcp/catalogue.json
    postgres-mcp/catalogue.json
    playwright-mcp/catalogue.json
    terraform-mcp/catalogue.json
    aws-mcp/catalogue.json
    context7-mcp/catalogue.json
    filesystem-mcp/catalogue.json
  deployments/
    _template/example.json
  README.md
\`\`\`

### GitHub Actions Workflows (in .github/workflows/, path-filtered)

\`\`\`
.github/workflows/
  mcp-sync-catalogue.yml      # on push to mcp-servers/catalogue/**: validate + sync to DB
  mcp-deploy-server.yml       # on push to mcp-servers/deployments/**: kubectl apply + callback
  mcp-remove-server.yml       # on file delete in mcp-servers/deployments/**: kubectl delete
  mcp-build-image.yml         # on push to mcp-servers/catalogue/*/Dockerfile: build + push ECR
  mcp-validate-pr.yml         # on PR touching mcp-servers/**: schema validation, dry-run
\`\`\`

### Path Filter Example
\`\`\`yaml
on:
  push:
    branches: [main]
    paths:
      - 'mcp-servers/catalogue/**'
\`\`\`

### Key Change from Previous Design
- NO separate GitHub repo — everything in this monorepo
- Workflows use path filters instead of being in a separate repo's .github/
- Same GITHUB_TOKEN permissions — no cross-repo access needed
- Agent onboarding (Epic MCP-16) works in same repo — agent commits to mcp-servers/ folder
- Same branch protection, same PR review process

### Workflows Call MCP Router API
All status callbacks go to the MCP Router service (not BedrockGateway):
- \`POST /admin/mcp/catalogue\` — sync catalog entry
- \`PUT /admin/mcp/deployments/{id}/status\` — update deployment status

### Stories: US-MCP-1.1, 2.2, 15.1-15.6

### Testing: JSON schema validation, actionlint, kubectl dry-run

### Rules
- Workflows run on EKS self-hosted runners
- Do NOT call kubectl from application code — only from workflows
- All K8s operations go through these workflows"

echo "Updated MCP-3"

# Update US-MCP-15.1 to reflect monorepo
gh issue edit 207 --repo "$REPO" \
  --body "## US-MCP-15.1: MCP Server Config Structure (Monorepo Folder)

**As a** Platform Admin (Priya),
**I want** a \`mcp-servers/\` folder in the bedrock-gateway monorepo with a defined structure for MCP server catalog definitions and org deployment configs,
**So that** all MCP server configuration is version-controlled alongside the application code.

**Epic**: MCP-15 | **Unit**: MCP-3

### Acceptance Criteria
- [ ] \`mcp-servers/\` folder created in the monorepo root with structure:
  \`\`\`
  mcp-servers/
    catalogue/
      github-mcp/catalogue.json
      ...7 initial entries...
    deployments/
      _template/example.json
    README.md
  \`\`\`
- [ ] catalogue.json schema defined and documented
- [ ] deployment.json schema defined and documented
- [ ] 7 initial Phase 1 catalog entries committed
- [ ] README with schema docs and onboarding instructions
- [ ] NO separate GitHub repo — this is a folder in bedrock-gateway monorepo"

echo "Updated US-MCP-15.1"

# Update US-MCP-15.3 to reflect monorepo path filters
gh issue edit 209 --repo "$REPO" \
  --body "## US-MCP-15.3: Server Deploy Workflow (Monorepo Path-Filtered)

**As an** Org Admin (Omar),
**I want** deployment configs committed to \`mcp-servers/deployments/\` to automatically deploy MCP servers to EKS,
**So that** server deployment is automated, auditable, and rollback-able via git revert.

**Epic**: MCP-15 | **Unit**: MCP-3

### Acceptance Criteria
- [ ] \`mcp-deploy-server.yml\` triggers on push to main with path \`mcp-servers/deployments/**\`
- [ ] Workflow reads the deployment JSON + corresponding catalogue JSON from \`mcp-servers/catalogue/\`
- [ ] For self_hosted servers:
  - [ ] Generates K8s Deployment + ClusterIP Service manifests
  - [ ] Runs \`kubectl apply\` in mcp-servers namespace
  - [ ] Waits for pod Ready (timeout 120s)
  - [ ] Calls \`tools/list\` on the new server
- [ ] Calls MCP Router API to update deployment status
- [ ] Runs on EKS self-hosted runners
- [ ] Same repo, same GITHUB_TOKEN — no cross-repo access needed"

echo "Updated US-MCP-15.3"

# Update agent onboarding stories to reflect monorepo
gh issue edit 202 --repo "$REPO" \
  --body "## US-MCP-16.1: Agent Researches MCP Server from GitHub Issue

**As a** Platform Admin (Priya),
**I want to** create a GitHub issue in this repo with an MCP server's repo URL and have an AI agent automatically research it,
**So that** I don't have to manually read docs, figure out config, and write catalogue.json.

**Epic**: MCP-16 | **Phase**: 2

### Trigger
Platform Admin creates issue in bedrock-gateway repo using the onboarding template with:
- MCP server GitHub repo URL
- Optional: website/docs URL, category hint, notes

### Agent Research Steps
- [ ] Agent triggered by issue label \`mcp-onboard\` via existing agent-trigger workflow pattern
- [ ] Agent clones the MCP server's external repo (not this repo — the server's source repo)
- [ ] Agent reads: README.md, Dockerfile, package.json/pyproject.toml
- [ ] Agent identifies: Docker image, port, transport, required credentials, credential level
- [ ] Agent posts research summary as issue comment
- [ ] If agent can't determine something, it asks the admin in the comment

### Key: Same Repo, Same Agent Infrastructure
- Uses the same agent trigger workflow pattern as BedrockGateway development agents
- Agent commits to \`mcp-servers/catalogue/\` folder in THIS repo (no cross-repo access)
- Same GITHUB_TOKEN, same EKS runners, same permissions"

echo "Updated US-MCP-16.1"

gh issue edit 204 --repo "$REPO" \
  --body "## US-MCP-16.3: Agent Deploys After Admin Approval

**As a** Platform Admin (Priya),
**I want** the agent to complete the onboarding automatically after I approve,
**So that** the new MCP server is available in the marketplace without any manual steps.

**Epic**: MCP-16 | **Phase**: 2

### Trigger
Admin comments \`approved\` on the onboarding issue.

### Agent Actions
- [ ] Agent detects approval comment
- [ ] Agent creates \`mcp-servers/catalogue/{server-name}/\` folder in THIS repo
- [ ] Agent commits catalogue.json (generated in previous step)
- [ ] If custom image: agent commits Dockerfile and source files to the catalogue folder
- [ ] Agent creates PR to main branch (same repo)
- [ ] On merge, existing pipeline workflows take over:
  - \`mcp-sync-catalogue.yml\` syncs to DB (triggered by path filter on mcp-servers/catalogue/**)
  - \`mcp-build-image.yml\` builds image if Dockerfile present
- [ ] Agent updates issue: '✅ Onboarded. Available in marketplace.'
- [ ] Agent closes the issue

### Key: Everything in One Repo
- No cross-repo commits — agent writes to mcp-servers/ folder in bedrock-gateway
- PR review process is the same as any other code change
- Pipeline workflows trigger automatically via path filters on merge"

echo "Updated US-MCP-16.3"

echo ""
echo "All monorepo updates done!"
