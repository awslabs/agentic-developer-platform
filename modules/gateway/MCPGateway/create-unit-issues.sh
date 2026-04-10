#!/bin/bash
REPO="PranavSharma1000/bedrock-gateway"

# Unit 0 Extension: Shared Foundation for MCP Gateway
gh issue create --repo "$REPO" \
  --title "Unit MCP-0: Shared Foundation for MCP Gateway" \
  --label "mcp-gateway" \
  --body "## Unit MCP-0: Shared Foundation for MCP Gateway

**Priority**: Must be completed BEFORE MCP-1 through MCP-4 can start.
**Execution**: On this machine (not an agent unit).

### Purpose
Extend the existing shared foundation (Unit 0) with MCP Gateway models, schemas, interfaces, and migration. This defines the contracts that all MCP units implement against.

### Deliverables

#### 1. SQLAlchemy Models (\`src/shared/models/mcpgateway.py\`)

\`\`\`python
class MCPCatalogue(Base):
    __tablename__ = 'mcp_catalogue'
    id, name, display_name, description, category, deployment_type,
    sharing_mode, credential_level, docker_image, default_port,
    remote_url_template, identity_provider_name, identity_auth_flow,
    identity_scopes (JSON), required_url_params (JSON),
    optional_env_vars (JSON), documentation_url, repo_path,
    verified, status, created_at, updated_at

class MCPDeployment(Base):
    __tablename__ = 'mcp_deployment'
    id, org_id (FK, nullable), catalogue_id (FK), instance_name,
    deployment_type, status, repo_path, k8s_service_name,
    k8s_namespace, replicas, remote_url, identity_workload_id,
    tool_count, last_health_check, last_tool_sync, created_at, updated_at

class MCPToolGroup(Base):
    __tablename__ = 'mcp_tool_group'
    id, org_id (FK), name, description, service_account_safe,
    created_at, updated_at

class MCPToolGroupRule(Base):
    __tablename__ = 'mcp_tool_group_rule'
    id, tool_group_id (FK), deployment_id (FK), include_pattern,
    created_at

class MCPToolGroupAssignment(Base):
    __tablename__ = 'mcp_tool_group_assignment'
    id, tool_group_id (FK), entity_type, entity_id, org_id (FK),
    created_at
\`\`\`

#### 2. Pydantic Schemas (\`src/shared/schemas/mcpgateway.py\`)
- CreateCatalogueEntryRequest, CatalogueEntryResponse
- DeployFromCatalogueRequest, DeploymentResponse
- CreateToolGroupRequest, ToolGroupRuleInput, ToolGroupResponse
- AssignToolGroupRequest
- MCPToolResponse, MCPConnectionResponse, ServerConnectionInfo

#### 3. Service Interfaces (\`src/shared/interfaces/mcpgateway.py\`)
- ICatalogueService, IDeploymentService, IMCPProxyService
- IToolGroupService, IDiscoveryService, IHealthService

#### 4. Alembic Migration
- \`alembic/versions/003_mcp_gateway.py\` — creates all 5 MCP tables

#### 5. App Registration
- Add \`'src.mcprouter.router'\` to UNIT_MODULES in \`src/app.py\`

#### 6. Empty Unit Directory
- \`src/mcprouter/__init__.py\` (empty, for auto-discovery)

### Acceptance Criteria
- [ ] All 5 models defined with correct FKs and constraints
- [ ] All schemas defined with validation
- [ ] All 6 service interfaces defined as ABCs
- [ ] Migration runs successfully against existing DB
- [ ] \`src/app.py\` includes mcprouter in UNIT_MODULES
- [ ] Committed to main branch before agent units start"

echo "Created Unit MCP-0"


# Unit MCP-1: Backend
gh issue create --repo "$REPO" \
  --title "Unit MCP-1: MCP Gateway Backend" \
  --label "mcp-gateway,unit:mcp-1" \
  --body "## Unit MCP-1: MCP Gateway Backend

**Agent Unit** — can run in parallel with MCP-2, MCP-3, MCP-4.
**Scope**: \`src/mcprouter/\`, \`tests/mcprouter/\`
**Depends on**: Shared Foundation (Unit MCP-0) must be merged first.

### What You're Building
The core backend for the MCP Gateway: catalog management, deployment lifecycle, MCP request proxying, tool groups, discovery, health monitoring, and usage logging.

### Architecture Context
- ALB routes \`/mcp/*\` to MCP Router pods (new target group)
- MCP Router validates Cognito JWT (same middleware as BedrockGateway)
- MCP Router checks tool group access, forwards to K8s ClusterIP or remote URL
- Deployments triggered via GitHub API (commits config JSON to mcp-servers repo)
- Credentials managed by AgentCore Identity (OAuth for user-level, API keys for org-level)
- Usage logged to existing usage_logs table with request_type='mcp'

### Files to Create

\`\`\`
src/mcprouter/
  __init__.py
  router.py              # FastAPI router with all /mcp/* and /admin/mcp/* routes
  catalogue_service.py   # CatalogueService implementing ICatalogueService
  deployment_service.py  # DeploymentService implementing IDeploymentService
  proxy_service.py       # MCPProxyService implementing IMCPProxyService
  tool_group_service.py  # ToolGroupService implementing IToolGroupService
  discovery_service.py   # DiscoveryService implementing IDiscoveryService
  health_service.py      # HealthService implementing IHealthService + background scheduler
  schemas.py             # Any additional schemas not in shared foundation
tests/mcprouter/
  __init__.py
  test_catalogue_service.py
  test_deployment_service.py
  test_proxy_service.py
  test_tool_group_service.py
  test_discovery_service.py
  test_health_service.py
  conftest.py            # Fixtures: mock httpx, mock GitHub API
\`\`\`

### API Routes to Implement

**Platform Admin (Catalog):**
- POST/GET/PUT /admin/mcp/catalogue
- PUT /admin/mcp/catalogue/{id}/verify
- PUT /admin/mcp/catalogue/{id}/deprecate

**Org Admin (Deployments):**
- GET /admin/mcp/marketplace
- POST/GET /admin/mcp/deployments
- GET /admin/mcp/deployments/{id}
- PUT /admin/mcp/deployments/{id}/disable
- PUT /admin/mcp/deployments/{id}/enable
- DELETE /admin/mcp/deployments/{id}
- POST /admin/mcp/deployments/{id}/refresh-tools

**Org Admin (Tool Groups):**
- POST/GET/PUT/DELETE /admin/mcp/tool-groups
- GET /admin/mcp/tool-groups/{id}/tools
- POST/DELETE /admin/mcp/tool-groups/{id}/assignments

**Health:**
- GET /admin/mcp/health

**User-facing:**
- GET /mcp/tools
- GET /mcp/tools/search?q={query}
- GET /mcp/connection
- POST /mcp/{deployment-name}/mcp (MCP proxy)
- GET /mcp/oauth/callback (AgentCore Identity OAuth callback)

### Key Implementation Details

1. **DeploymentService.deploy()**: Creates deployment JSON, commits to GitHub repo via GitHub API, triggers workflow_dispatch. Does NOT call kubectl directly.
2. **MCPProxyService.proxy_request()**: Resolves deployment → K8s service URL or remote URL, forwards via httpx, injects auth for remote servers.
3. **ToolGroupService.check_access()**: Checks if caller's team has a tool group that includes the requested deployment.
4. **HealthService**: Background asyncio tasks for health checks (5 min) and tool refresh (6 hours).
5. **Three credential levels**: none (no injection), org (AgentCore Identity M2M), user (AgentCore Identity USER_FEDERATION with @requires_access_token).

### Stories Covered
US-MCP-1.1, 1.2, 1.3, 2.2, 2.3, 2.4, 3.1, 3.2, 4.1, 4.2, 4.3, 5.1, 5.2, 6.1, 8.1, 9.1, 9.2, 9.3

### Testing Strategy
- Mock httpx for MCP server calls (tools/list, tools/call)
- Mock GitHub API for deployment triggers
- Test DB with SQLAlchemy async (same pattern as other units)
- Test tool group access logic with various permission scenarios
- Test proxy routing for self_hosted vs remote vs disabled deployments

### Rules
- Do NOT add dependencies not in pyproject.toml
- Do NOT modify src/shared/ (use interfaces as-is)
- Do NOT call kubectl or K8s API (deployments go through GitHub Actions)
- If you need something not in the shared foundation, raise a clarification request"

echo "Created Unit MCP-1"

# Unit MCP-2: Admin UI
gh issue create --repo "$REPO" \
  --title "Unit MCP-2: MCP Gateway Admin UI" \
  --label "mcp-gateway,unit:mcp-2" \
  --body "## Unit MCP-2: MCP Gateway Admin UI

**Agent Unit** — can run in parallel with MCP-1, MCP-3, MCP-4.
**Scope**: \`frontend/src/pages/mcp-gateway/\`, \`frontend/src/components/mcp/\`
**Depends on**: Shared Foundation (Unit MCP-0) for API contract (Pydantic schemas), existing Admin UI shell (Unit 7).

### What You're Building
New top-level 'MCP Gateway' section in the React + Tailwind Admin UI with 8 pages.

### Pages to Create

\`\`\`
frontend/src/pages/mcp-gateway/
  CatalogManagement.tsx    # Platform Admin: list/add/edit/verify/deprecate catalog entries
  Marketplace.tsx          # Org Admin: browse catalog, deploy instances
  MyDeployments.tsx        # Org Admin: list deployments, status, actions
  DeploymentDetail.tsx     # Org Admin: server details, tools, health history
  ToolBrowser.tsx          # Developer: searchable tool catalog
  ToolGroups.tsx           # Org Admin: create/edit groups, assign to teams
  HealthDashboard.tsx      # Admin: per-deployment health, sync history
  ConnectionInfo.tsx       # Developer: per-server URLs, auth, client configs

frontend/src/components/mcp/
  MCPServerCard.tsx        # Reusable card for catalog/deployment display
  ToolCard.tsx             # Tool display with name, description, parameters
  CredentialStatusBadge.tsx # ready | setup_required | not_needed
  DeploymentStatusBadge.tsx # DEPLOYED | FAILED | DEPLOYING | DISABLED
  DeployForm.tsx           # Credential input form for deployment
\`\`\`

### API Endpoints Consumed
All \`/admin/mcp/*\` and \`/mcp/*\` endpoints from Unit MCP-1.

### Stories Covered
US-MCP-7.1, 7.2, 7.3, 7.4, 2.1

### Testing Strategy
- Vitest + React Testing Library
- MSW (Mock Service Worker) for all API mocks
- Test each page renders correctly with mock data
- Test deployment flow (form submission, status polling)
- Test tool group assignment UI

### Rules
- Follow existing Admin UI patterns (layout, nav, auth context)
- Do NOT add npm dependencies not in package.json
- Use Tailwind CSS classes (no custom CSS)
- All pages must be accessible (ARIA labels, keyboard navigation)"

echo "Created Unit MCP-2"

# Unit MCP-3: Config Repo & Pipelines
gh issue create --repo "$REPO" \
  --title "Unit MCP-3: MCP Server Config Repo & Deployment Pipelines" \
  --label "mcp-gateway,unit:mcp-3" \
  --body "## Unit MCP-3: MCP Server Config Repo & Deployment Pipelines

**Agent Unit** — can run in parallel with MCP-1, MCP-2, MCP-4.
**Scope**: Separate GitHub repo (\`mcp-servers/\`), GitHub Actions workflows.
**Depends on**: EKS cluster (Unit 8), ECR (Unit 8), EKS self-hosted runners (Unit 9).

### What You're Building
A GitHub repository for MCP server catalog definitions and org deployment configs, with GitHub Actions workflows that deploy/remove MCP servers on EKS.

### Repo Structure to Create

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
    sync-catalogue.yml
    deploy-server.yml
    remove-server.yml
    build-custom-image.yml
    validate-pr.yml
\`\`\`

### Catalogue JSON Schema
\`\`\`json
{
  \"name\": \"github-mcp\",
  \"displayName\": \"GitHub MCP Server\",
  \"description\": \"PRs, issues, code search, CI workflows\",
  \"category\": \"version-control\",
  \"deploymentType\": \"self_hosted\",
  \"sharingMode\": \"shared\",
  \"credentialLevel\": \"user\",
  \"image\": \"ghcr.io/github/github-mcp-server:latest\",
  \"port\": 8000,
  \"identityProviderName\": \"github-provider\",
  \"identityAuthFlow\": \"USER_FEDERATION\",
  \"identityScopes\": [\"repo\", \"read:org\"],
  \"requiredCredentials\": [],
  \"documentationUrl\": \"https://github.com/github/github-mcp-server\"
}
\`\`\`

### Deployment JSON Schema
\`\`\`json
{
  \"catalogueName\": \"postgres-mcp\",
  \"instanceName\": \"postgres-mcp\",
  \"orgSlug\": \"acme-corp\",
  \"replicas\": 1,
  \"envOverrides\": {\"READ_ONLY\": \"true\"}
}
\`\`\`

### GitHub Actions Workflows

1. **sync-catalogue.yml**: On push to catalogue/, validates JSON, calls BedrockGateway API to upsert mcp_catalogue rows
2. **deploy-server.yml**: On push to deployments/, reads catalogue + deployment JSON, generates K8s manifests, kubectl apply, waits for Ready, calls tools/list, callbacks to BedrockGateway API
3. **remove-server.yml**: On file deletion in deployments/, kubectl delete, callbacks to API
4. **build-custom-image.yml**: On push to catalogue/*/Dockerfile, docker build + push to ECR
5. **validate-pr.yml**: On PR, validates JSON schema, checks image exists, kubectl dry-run

All workflows run on EKS self-hosted runners.

### Stories Covered
US-MCP-1.1 (catalog sync), US-MCP-2.2 (deployment execution), US-MCP-5.1 (health via pod status)

### Testing Strategy
- JSON schema validation tests
- Workflow syntax validation (actionlint)
- kubectl dry-run for generated manifests
- Mock BedrockGateway API callbacks"

echo "Created Unit MCP-3"

# Unit MCP-4: Infrastructure
gh issue create --repo "$REPO" \
  --title "Unit MCP-4: MCP Gateway Infrastructure" \
  --label "mcp-gateway,unit:mcp-4" \
  --body "## Unit MCP-4: MCP Gateway Infrastructure

**Agent Unit** — can run in parallel with MCP-1, MCP-2, MCP-3.
**Scope**: \`infra/modules/mcp/\`, \`infra/environments/*/mcp.tf\`
**Depends on**: Unit 8 (base infrastructure — EKS, VPC, ALB).

### What You're Building
Terraform module for MCP Gateway infrastructure on the existing EKS cluster.

### Terraform Resources

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
\`\`\`

### Resources to Create

1. **K8s namespace**: \`mcp-servers\` for MCP server pods
2. **ALB target group**: For MCP Router pods, with path-based routing rule (\`/mcp/*\`)
3. **ALB listener rule**: Priority-based rule routing /mcp/* to MCP Router target group
4. **ECR repositories**: One per Phase 1 catalog entry (7 repos)
5. **IAM role**: For MCP Router pod (IRSA) with permissions for:
   - GitHub API access (for deployment triggers)
   - AgentCore Identity API access
   - CloudWatch metrics/logs
6. **AgentCore Identity**: Workload identity for MCP Router
7. **AgentCore Identity credential providers**: For Phase 1 servers:
   - github-provider (GithubOauth2 — built-in)
   - atlassian-provider (AtlassianOauth2 — built-in, for future Jira)
8. **CloudWatch alarms**: For MCP server pod health failures
9. **Alembic migration**: Run as part of app deployment (migration for MCP tables)

### Environment tfvars
- \`infra/environments/dev/mcp.tf\`
- \`infra/environments/prod/mcp.tf\`

### Stories Covered
Cross-cutting — supports all MCP deployment

### Testing Strategy
- \`terraform validate\`
- \`terraform plan\` (dry-run)
- Verify ALB routing rule works with test request

### Rules
- Follow existing Terraform patterns from infra/modules/
- Use same provider versions from infra/versions.tf
- Do NOT create new VPC or EKS cluster — use existing
- Reference existing ALB and EKS outputs"

echo "Created Unit MCP-4"
echo ""
echo "All MCP unit issues created!"
