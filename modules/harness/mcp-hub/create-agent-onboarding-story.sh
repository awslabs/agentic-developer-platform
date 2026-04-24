#!/bin/bash
REPO="PranavSharma1000/bedrock-gateway"

# Epic MCP-16: Agent-Driven MCP Server Onboarding
gh issue create --repo "$REPO" \
  --title "Epic MCP-16: Agent-Driven MCP Server Onboarding" \
  --label "epic,mcp-gateway,phase-2" \
  --body "## Epic MCP-16: Agent-Driven MCP Server Onboarding

An AI agent automates the entire MCP server onboarding process. Platform Admin creates a GitHub issue with minimal info (repo URL, website), and the agent researches the server, generates deployment config, tests it, and deploys — with admin approval gates.

This replaces manual catalogue.json creation and makes onboarding new MCP servers as easy as filing an issue.

### Why
- MCP ecosystem is growing fast — new servers appear weekly
- Each server has different config (image, port, env vars, credentials, transport)
- Manual research + config creation is tedious and error-prone
- An agent can read docs, test the server, and generate correct config automatically

### Stories
- US-MCP-16.1: Agent Researches MCP Server from GitHub Issue
- US-MCP-16.2: Agent Generates and Tests Deployment Plan
- US-MCP-16.3: Agent Deploys After Admin Approval
- US-MCP-16.4: Agent Onboarding Issue Template"

echo "Created Epic MCP-16"

# US-MCP-16.1: Agent researches
gh issue create --repo "$REPO" \
  --title "US-MCP-16.1: Agent Researches MCP Server from GitHub Issue" \
  --label "mcp-gateway,phase-2,unit:mcp-3" \
  --body "## US-MCP-16.1: Agent Researches MCP Server from GitHub Issue

**As a** Platform Admin (Priya),
**I want to** create a GitHub issue with an MCP server's repo URL and have an AI agent automatically research it,
**So that** I don't have to manually read docs, figure out config, and write catalogue.json.

**Epic**: MCP-16 | **Phase**: 2

### Trigger
Platform Admin creates issue using the onboarding template with:
- MCP server GitHub repo URL
- Optional: website/docs URL
- Optional: category hint
- Optional: notes/requirements

### Agent Research Steps
- [ ] Agent triggered by issue label \`mcp-onboard\` via GitHub Actions
- [ ] Agent clones the MCP server repo
- [ ] Agent reads: README.md, Dockerfile, package.json/pyproject.toml, docker-compose.yml
- [ ] Agent identifies:
  - Docker image (public registry or needs custom build)
  - Port number
  - Transport type (streamable-http, SSE, stdio)
  - Required environment variables / credentials
  - Whether it supports stateless_http
- [ ] Agent reads docs/README to determine:
  - What tools the server provides
  - What external services it connects to (GitHub API, Jira API, database, etc.)
  - What credential level is needed (none, org, user)
  - What OAuth scopes are needed (if user-level)
- [ ] Agent posts research summary as issue comment with all findings
- [ ] If agent can't determine something, it asks the admin in the comment"

echo "Created US-MCP-16.1"

# US-MCP-16.2: Agent generates and tests
gh issue create --repo "$REPO" \
  --title "US-MCP-16.2: Agent Generates and Tests Deployment Plan" \
  --label "mcp-gateway,phase-2,unit:mcp-3" \
  --body "## US-MCP-16.2: Agent Generates and Tests Deployment Plan

**As a** Platform Admin (Priya),
**I want** the agent to generate a complete deployment plan and test it before asking for approval,
**So that** I can review a working, validated config rather than a guess.

**Epic**: MCP-16 | **Phase**: 2

### Agent Actions
- [ ] Agent generates catalogue.json from research findings
- [ ] Agent builds Docker image (if custom build needed) or pulls public image
- [ ] Agent runs the container on the EKS runner
- [ ] Agent calls MCP \`initialize\` to verify protocol compatibility
- [ ] Agent calls \`tools/list\` to discover all available tools
- [ ] Agent optionally runs security scan
- [ ] Agent posts deployment plan as issue comment:

\`\`\`markdown
## Deployment Plan: Sentry MCP

**Image**: ghcr.io/getsentry/sentry-mcp:latest
**Port**: 8000
**Transport**: streamable-http
**Category**: observability
**Sharing Mode**: per_org
**Credential Level**: org
**Required Credentials**: SENTRY_AUTH_TOKEN

### Tools Discovered (12):
| Tool | Description |
|------|-------------|
| list_issues | List Sentry issues with filters |
| get_issue | Get issue details by ID |
| resolve_issue | Mark issue as resolved |
| ... | ... |

### Security Scan: PASS

### Generated catalogue.json:
(full JSON shown)

---
**⛔ Approval Required**
Comment \`approved\` to proceed with onboarding.
Comment \`changes: <description>\` to request modifications.
\`\`\`

- [ ] Agent waits for admin response"

echo "Created US-MCP-16.2"

# US-MCP-16.3: Agent deploys after approval
gh issue create --repo "$REPO" \
  --title "US-MCP-16.3: Agent Deploys After Admin Approval" \
  --label "mcp-gateway,phase-2,unit:mcp-3" \
  --body "## US-MCP-16.3: Agent Deploys After Admin Approval

**As a** Platform Admin (Priya),
**I want** the agent to complete the onboarding automatically after I approve,
**So that** the new MCP server is available in the marketplace without any manual steps.

**Epic**: MCP-16 | **Phase**: 2

### Trigger
Admin comments \`approved\` on the onboarding issue.

### Agent Actions
- [ ] Agent detects approval comment
- [ ] Agent creates \`catalogue/{server-name}/\` folder in mcp-servers repo
- [ ] Agent commits catalogue.json (generated in previous step)
- [ ] If custom image: agent commits Dockerfile and source files
- [ ] Agent creates PR to mcp-servers repo (or direct commit if configured)
- [ ] Existing pipeline workflows take over:
  - sync-catalogue.yml syncs to DB
  - build-custom-image.yml builds image if needed
- [ ] Agent updates issue comment: '✅ Onboarded. Available in marketplace.'
- [ ] Agent closes the issue
- [ ] If admin comments \`changes: <description>\`, agent modifies the plan and re-posts for approval

### Error Handling
- [ ] If pipeline fails, agent re-opens issue with error details
- [ ] If image build fails, agent posts build logs and asks for guidance
- [ ] Agent never deploys without explicit approval"

echo "Created US-MCP-16.3"

# US-MCP-16.4: Issue template
gh issue create --repo "$REPO" \
  --title "US-MCP-16.4: MCP Server Onboarding Issue Template" \
  --label "mcp-gateway,phase-2,unit:mcp-3" \
  --body "## US-MCP-16.4: MCP Server Onboarding Issue Template

**As a** Platform Admin (Priya),
**I want** a GitHub issue template for MCP server onboarding requests,
**So that** I can trigger the agent with minimal effort and consistent format.

**Epic**: MCP-16 | **Phase**: 2

### Acceptance Criteria
- [ ] Issue template \`.github/ISSUE_TEMPLATE/mcp-server-onboard.yml\` created in mcp-servers repo
- [ ] Template fields:
  - Server Name (required)
  - GitHub Repo URL (required)
  - Website/Docs URL (optional)
  - Category (dropdown: version-control, testing, database, infrastructure, cloud, observability, project-management, search, ai-coding, data-warehouse, other)
  - Notes (optional free text)
- [ ] Template auto-applies \`mcp-onboard\` label (triggers the agent workflow)
- [ ] Template includes instructions: 'An AI agent will research this server and propose a deployment plan. You will be asked to approve before anything is deployed.'

### Example Issue Created from Template
\`\`\`
Title: Onboard MCP Server: Sentry
Labels: mcp-onboard

Server Name: sentry-mcp
GitHub Repo: https://github.com/getsentry/sentry-mcp
Website: https://sentry.io
Category: observability
Notes: We need this for error tracking integration with our dev workflow.
\`\`\`"

echo "Created US-MCP-16.4"

echo ""
echo "All agent onboarding stories created!"
