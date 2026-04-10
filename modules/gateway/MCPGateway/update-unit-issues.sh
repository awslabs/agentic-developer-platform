#!/bin/bash
REPO="PranavSharma1000/bedrock-gateway"

# Update MCP-1: Backend — now fully self-contained with own container
gh issue edit 181 --repo "$REPO" \
  --title "Unit MCP-1: MCP Router Service (Self-Contained, Own Container)" \
  --body "## Unit MCP-1: MCP Router Service

**Agent Unit** — can run in parallel with MCP-2, MCP-3, MCP-4 AND all BedrockGateway units.
**Scope**: \`src/mcprouter/\`, \`tests/mcprouter/\`
**Depends on**: Nothing — fully self-contained. No shared foundation changes needed.
**No waiting**: Zero file overlaps with BedrockGateway development.

### Architecture
MCP Router runs as its OWN container on EKS, separate from BedrockGateway:
- Own FastAPI app (\`src/mcprouter/app.py\`)
- Own Dockerfile (\`src/mcprouter/Dockerfile\`)
- Own ECR image (\`<ecr>/mcp-router\`)
- Own K8s Deployment + Service
- Own ALB target group (\`/mcp/*\`, \`/admin/mcp/*\`)
- Own DB tables (mcp_catalogue, mcp_deployment, etc.)
- Shares: same PostgreSQL instance, same Cognito user pool, same EKS cluster

### Read-Only Imports from BedrockGateway
These files are imported but NEVER modified:
- \`src/shared/database.py\` — DB session factory
- \`src/shared/models/base.py\` — SQLAlchemy Base class
- \`src/shared/models/organization.py\` — Organization model (FK reference)
- \`src/shared/config.py\` — App settings (DB URL, Cognito config)
- \`src/shared/exceptions.py\` — Error classes

### Files to Create

\`\`\`
src/mcprouter/
  __init__.py
  app.py                   # Own FastAPI app with middleware chain
  router.py                # All /mcp/* and /admin/mcp/* routes
  models.py                # MCPCatalogue, MCPDeployment, MCPToolGroup, MCPToolGroupRule, MCPToolGroupAssignment
  schemas.py               # All Pydantic request/response schemas
  interfaces.py            # Service ABCs (ICatalogueService, IDeploymentService, etc.)
  middleware.py            # Auth middleware (Cognito JWT validation, reuses same logic as BG)
  catalogue_service.py     # Platform catalog CRUD
  deployment_service.py    # Org deployment lifecycle (triggers GitHub Actions via GitHub API)
  proxy_service.py         # Forwards MCP JSON-RPC to K8s ClusterIP or remote URL
  tool_group_service.py    # Tool group CRUD + team assignment + access checks
  discovery_service.py     # Aggregates tools, keyword search, connection info
  health_service.py        # Background health checks (5 min) + tool refresh (6 hours)
  Dockerfile               # Multi-stage build for mcp-router image
  requirements.txt         # Own dependencies
alembic/versions/
  003_mcp_gateway.py       # Migration for all 5 MCP tables (independent revision)
tests/mcprouter/
  __init__.py
  test_catalogue_service.py
  test_deployment_service.py
  test_proxy_service.py
  test_tool_group_service.py
  test_discovery_service.py
  test_health_service.py
  conftest.py
\`\`\`

### DB Models (all in src/mcprouter/models.py)

**mcp_catalogue**: id, name, display_name, description, category, deployment_type, sharing_mode, credential_level, docker_image, default_port, remote_url_template, identity_provider_name, identity_auth_flow, identity_scopes, required_url_params, optional_env_vars, documentation_url, repo_path, verified, status, created_at, updated_at

**mcp_deployment**: id, org_id (FK nullable), catalogue_id (FK), instance_name, deployment_type, status, repo_path, k8s_service_name, k8s_namespace, replicas, remote_url, identity_workload_id, tool_count, last_health_check, last_tool_sync, created_at, updated_at

**mcp_tool_group**: id, org_id (FK), name, description, service_account_safe, created_at, updated_at

**mcp_tool_group_rule**: id, tool_group_id (FK), deployment_id (FK), include_pattern, created_at

**mcp_tool_group_assignment**: id, tool_group_id (FK), entity_type, entity_id, org_id (FK), created_at

### API Routes

**Platform Admin (Catalog):**
POST/GET/PUT /admin/mcp/catalogue, PUT .../verify, PUT .../deprecate

**Org Admin (Deployments):**
GET /admin/mcp/marketplace, POST/GET /admin/mcp/deployments, GET/PUT/DELETE .../deployments/{id}, POST .../refresh-tools

**Org Admin (Tool Groups):**
POST/GET/PUT/DELETE /admin/mcp/tool-groups, GET .../tools, POST/DELETE .../assignments

**Health:** GET /admin/mcp/health

**User-facing:**
GET /mcp/tools, GET /mcp/tools/search, GET /mcp/connection, POST /mcp/{name}/mcp, GET /mcp/oauth/callback

### Key Implementation Details
1. **Own FastAPI app** in app.py — not shared with BedrockGateway
2. **DeploymentService**: commits config JSON to GitHub repo via GitHub API, triggers workflow_dispatch. Does NOT call kubectl.
3. **MCPProxyService**: resolves deployment to K8s service URL or remote URL, forwards via httpx
4. **Three credential levels**: none (no injection), org (AgentCore Identity M2M), user (AgentCore Identity USER_FEDERATION)
5. **Background scheduler**: asyncio tasks for health checks and tool refresh
6. **Own Dockerfile**: multi-stage build, separate ECR image

### Stories Covered
US-MCP-1.1, 1.2, 1.3, 2.2, 2.3, 2.4, 3.1, 3.2, 4.1, 4.2, 4.3, 5.1, 5.2, 6.1, 8.1, 9.1, 9.2, 9.3

### Testing Strategy
- Mock httpx for MCP server calls
- Mock GitHub API for deployment triggers
- Own test DB (same PostgreSQL, own tables)
- Test tool group access with various permission scenarios

### Rules
- Do NOT modify any file outside src/mcprouter/ and tests/mcprouter/
- Do NOT modify src/shared/, src/app.py, or any BedrockGateway file
- Import from src/shared/ is OK (read-only)
- Do NOT call kubectl or K8s API
- If you need something not available, raise a clarification request"

echo "Updated MCP-1"

# Update MCP-2: Admin UI
gh issue edit 182 --repo "$REPO" \
  --body "## Unit MCP-2: MCP Gateway Admin UI

**Agent Unit** — can run in parallel with MCP-1, MCP-3, MCP-4 AND all BedrockGateway units.
**Scope**: \`frontend/src/pages/mcp-gateway/\`, \`frontend/src/components/mcp/\`
**Depends on**: MCP-1 API contract (schemas) for mock data shapes. Can start immediately using the schema definitions from the MCP-1 issue.

### What You're Building
New top-level 'MCP Gateway' section in the React + Tailwind Admin UI. The UI talks to the MCP Router service (separate container) via the same ALB domain — ALB routes /admin/mcp/* to the MCP Router.

### Pages to Create
\`\`\`
frontend/src/pages/mcp-gateway/
  CatalogManagement.tsx    # Platform Admin: list/add/edit/verify/deprecate
  Marketplace.tsx          # Org Admin: browse catalog, deploy instances
  MyDeployments.tsx        # Org Admin: list deployments, status, actions
  DeploymentDetail.tsx     # Org Admin: server details, tools, health
  ToolBrowser.tsx          # Developer: searchable tool catalog
  ToolGroups.tsx           # Org Admin: create/edit groups, assign to teams
  HealthDashboard.tsx      # Admin: per-deployment health, sync history
  ConnectionInfo.tsx       # Developer: per-server URLs, auth, client configs

frontend/src/components/mcp/
  MCPServerCard.tsx
  ToolCard.tsx
  CredentialStatusBadge.tsx
  DeploymentStatusBadge.tsx
  DeployForm.tsx
\`\`\`

### API Endpoints Consumed
All \`/admin/mcp/*\` and \`/mcp/*\` endpoints — routed by ALB to MCP Router container.

### Stories: US-MCP-7.1, 7.2, 7.3, 7.4, 2.1

### Testing: Vitest + React Testing Library + MSW mocks

### Rules
- Follow existing Admin UI patterns
- Do NOT add npm dependencies not in package.json
- Use Tailwind CSS (no custom CSS)
- All pages must be accessible"

echo "Updated MCP-2"

# Update MCP-3: Config Repo
gh issue edit 183 --repo "$REPO" \
  --body "## Unit MCP-3: MCP Server Config Repo & Deployment Pipelines

**Agent Unit** — can run in parallel with MCP-1, MCP-2, MCP-4 AND all BedrockGateway units.
**Scope**: Separate GitHub repo (\`mcp-servers/\`)
**Depends on**: EKS cluster, ECR repos, EKS self-hosted runners.
**Zero overlap with BedrockGateway code.**

### What You're Building
A GitHub repository for MCP server catalog definitions and org deployment configs, with GitHub Actions workflows that deploy/remove MCP servers on EKS.

### Repo Structure
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
  .github/workflows/
    sync-catalogue.yml       # On push to catalogue/: validate + sync to DB via MCP Router API
    deploy-server.yml        # On push to deployments/: kubectl apply + callback to MCP Router API
    remove-server.yml        # On delete in deployments/: kubectl delete + callback
    build-custom-image.yml   # On push to catalogue/*/Dockerfile: build + push to ECR
    validate-pr.yml          # On PR: schema validation, dry-run
\`\`\`

### Key Detail
Workflows call back to the MCP Router API (not BedrockGateway API) to update deployment status. The MCP Router is the sole owner of MCP state.

### Stories: US-MCP-1.1 (catalog sync), US-MCP-2.2 (deployment execution)

### Testing: JSON schema validation, actionlint, kubectl dry-run"

echo "Updated MCP-3"

# Update MCP-4: Infrastructure
gh issue edit 184 --repo "$REPO" \
  --body "## Unit MCP-4: MCP Gateway Infrastructure

**Agent Unit** — can run in parallel with MCP-1, MCP-2, MCP-3 AND all BedrockGateway units.
**Scope**: \`infra/modules/mcp/\`, \`infra/environments/*/mcp.tf\`
**Depends on**: Unit 8 (base EKS, VPC, ALB modules).
**Zero overlap with BedrockGateway Terraform modules.**

### What You're Building
Terraform module for MCP Gateway infrastructure. All resources are NEW — no modifications to existing BedrockGateway infra modules.

### Resources

1. **K8s namespace**: \`mcp-servers\`
2. **K8s Deployment + Service**: \`mcp-router\` (port 8001)
3. **ECR repository**: \`mcp-router\` (for the MCP Router container image)
4. **ECR repositories**: One per Phase 1 catalog entry (7 repos for MCP server images)
5. **ALB target group**: \`mcp-router-tg\` → port 8001
6. **ALB listener rules**: \`/mcp/*\` and \`/admin/mcp/*\` → mcp-router-tg
7. **IAM role**: For mcp-router pod (IRSA) — GitHub API access, AgentCore Identity API, CloudWatch
8. **AgentCore Identity**: Workload identity + credential providers (github-provider, atlassian-provider)
9. **CloudWatch alarms**: MCP server pod health failures

### Files
\`\`\`
infra/modules/mcp/
  main.tf
  variables.tf
  outputs.tf
  iam.tf
  alb.tf
  ecr.tf
  agentcore_identity.tf
  cloudwatch.tf
infra/environments/dev/mcp.tf
infra/environments/prod/mcp.tf
\`\`\`

### Stories: Cross-cutting

### Testing: terraform validate, terraform plan

### Rules
- Follow existing Terraform patterns from infra/modules/
- Use same provider versions from infra/versions.tf
- Do NOT modify existing modules (eks, rds, alb, etc.)
- Reference existing ALB and EKS outputs via data sources or module outputs"

echo "Updated MCP-4"
echo ""
echo "All unit issues updated!"
