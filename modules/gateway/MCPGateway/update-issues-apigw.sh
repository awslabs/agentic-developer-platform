#!/bin/bash
REPO="PranavSharma1000/bedrock-gateway"

# Update MCP-1: Backend — add back rate limiting middleware, clarify API Gateway role
gh issue edit 181 --repo "$REPO" \
  --body "## Unit MCP-1: MCP Router Service (Self-Contained, Own Container)

**Agent Unit** — can run in parallel with MCP-2, MCP-3, MCP-4 AND all BedrockGateway units.
**Scope**: \`src/mcprouter/\`, \`tests/mcprouter/\`
**Depends on**: Nothing — fully self-contained. No shared foundation changes needed.

### Architecture
- API Gateway REST API (with response streaming) is the public entry point
- API Gateway validates Cognito JWT and enforces global rate limits (infrastructure protection)
- API Gateway routes /mcp/* via VPC Link v2 → Internal ALB → MCP Router pods
- MCP Router enforces app-level rate limits (per-org/team/user from DB, managed via Admin UI)
- MCP Router checks tool group access, forwards to K8s ClusterIP or remote URL, logs usage
- Existing CloudFront → ALB path continues unchanged (no disruption)

### Own Container
- Own FastAPI app (\`src/mcprouter/app.py\`)
- Own Dockerfile, own ECR image (\`<ecr>/mcp-router\`)
- Own K8s Deployment + Service (port 8001)
- Own ALB target group
- Own DB tables (mcp_catalogue, mcp_deployment, mcp_tool_group, etc.)
- Shares: same PostgreSQL instance, same Cognito user pool, same EKS cluster

### Read-Only Imports from BedrockGateway
- \`src/shared/database.py\` — DB session factory
- \`src/shared/models/base.py\` — SQLAlchemy Base class
- \`src/shared/models/organization.py\` — Organization model (FK reference)
- \`src/shared/config.py\` — App settings
- \`src/shared/exceptions.py\` — Error classes

### Files to Create
\`\`\`
src/mcprouter/
  __init__.py
  app.py                   # Own FastAPI app with middleware chain
  router.py                # All /mcp/* and /admin/mcp/* routes
  models.py                # MCPCatalogue, MCPDeployment, MCPToolGroup, etc.
  schemas.py               # All Pydantic schemas
  interfaces.py            # Service ABCs
  middleware.py             # Rate limiting middleware (app-level, reads config from DB)
  catalogue_service.py     # Platform catalog CRUD
  deployment_service.py    # Org deployment lifecycle (triggers GitHub Actions)
  proxy_service.py         # Forwards MCP JSON-RPC to K8s/remote
  tool_group_service.py    # Tool group CRUD + team assignment + access checks
  discovery_service.py     # Aggregates tools, keyword search, connection info
  health_service.py        # Background health checks + tool refresh
  ratelimit_service.py     # App-level rate limiting (per-org/team/user, from DB)
  Dockerfile               # Multi-stage build
  requirements.txt         # Own dependencies
alembic/versions/
  003_mcp_gateway.py       # Migration for MCP tables
tests/mcprouter/
  __init__.py
  test_catalogue_service.py
  test_deployment_service.py
  test_proxy_service.py
  test_tool_group_service.py
  test_discovery_service.py
  test_health_service.py
  test_ratelimit_service.py
  conftest.py
\`\`\`

### Two-Tier Rate Limiting
1. **API Gateway** (infrastructure): Global throttle, DDoS protection, hard ceiling — configured in Terraform
2. **MCP Router** (application): Per-org/team/user rate limits — stored in PostgreSQL, managed via Admin UI

Both Bedrock and MCP rate limits visible in one Admin UI, independently configurable.

### DB Models (all in src/mcprouter/models.py)
- **mcp_catalogue**: Platform-level registry
- **mcp_deployment**: Org-level instances
- **mcp_tool_group**: Named tool collections
- **mcp_tool_group_rule**: Inclusion rules
- **mcp_tool_group_assignment**: Team assignments
- **mcp_rate_limit_config**: App-level rate limits per entity per tool group

### API Routes
**Platform Admin (Catalog):** POST/GET/PUT /admin/mcp/catalogue, verify, deprecate
**Org Admin (Deployments):** GET /admin/mcp/marketplace, POST/GET/PUT/DELETE /admin/mcp/deployments
**Org Admin (Tool Groups):** POST/GET/PUT/DELETE /admin/mcp/tool-groups, assignments
**Org Admin (Rate Limits):** POST/GET/PUT /admin/mcp/tool-groups/{id}/rate-limits
**Health:** GET /admin/mcp/health
**User-facing:** GET /mcp/tools, /mcp/tools/search, /mcp/connection, POST /mcp/{name}/mcp, GET /mcp/oauth/callback

### Key Details
1. **Auth**: API Gateway validates JWT. MCP Router receives pre-authenticated requests. Extracts claims from API Gateway context headers.
2. **Rate limiting**: MCP Router reads rate limit config from DB, enforces per request. Same token bucket pattern as BedrockGateway.
3. **DeploymentService**: Commits config JSON to GitHub repo via GitHub API, triggers workflow_dispatch.
4. **Three credential levels**: none, org (AgentCore Identity M2M), user (AgentCore Identity USER_FEDERATION)
5. **Background scheduler**: Health checks (5 min) + tool refresh (6 hours)

### Stories: US-MCP-1.1-1.3, 2.2-2.4, 3.1-3.2, 4.1-4.3, 5.1-5.2, 6.1, 8.1, 9.1-9.3

### Rules
- Do NOT modify any file outside src/mcprouter/ and tests/mcprouter/
- Import from src/shared/ is OK (read-only)
- Do NOT call kubectl or K8s API
- If you need something not available, raise a clarification request"

echo "Updated MCP-1"

# Update MCP-4: Infrastructure — add API Gateway module
gh issue edit 184 --repo "$REPO" \
  --body "## Unit MCP-4: MCP Gateway Infrastructure

**Agent Unit** — can run in parallel with MCP-1, MCP-2, MCP-3 AND all BedrockGateway units.
**Scope**: \`infra/modules/mcp/\`, \`infra/modules/apigateway/\`, \`infra/environments/*/mcp.tf\`
**Depends on**: Unit 8 (base EKS, VPC, ALB modules).

### What You're Building
Terraform modules for MCP Gateway infrastructure AND the shared API Gateway that serves both BedrockGateway and MCP Router.

### Architecture
\`\`\`
API Gateway REST API (public)
  → Cognito authorizer
  → WAF (optional)
  → Global throttling
  → Response streaming on proxy routes
  |
  | VPC Link v2
  v
Internal ALB (private VPC)
  ├── Target Group: bedrock-gateway (port 8000)
  └── Target Group: mcp-router (port 8001)
\`\`\`

Existing CloudFront → ALB path continues unchanged.

### Terraform Modules

**infra/modules/apigateway/** (NEW — shared by BG and MCP):
- REST API with stages
- Cognito authorizer
- VPC Link v2 to internal ALB
- Route definitions with path-based routing
- Response streaming config (STREAM on proxy routes, BUFFERED on admin routes)
- WAF association (optional)
- Global throttling / usage plans
- CloudWatch access logging

**infra/modules/mcp/** (NEW — MCP-specific):
- K8s namespace \`mcp-servers\`
- K8s Deployment + Service for \`mcp-router\` (port 8001)
- ECR repository for \`mcp-router\` image
- ECR repositories for Phase 1 MCP server images (7 repos)
- ALB target group \`mcp-router-tg\` with listener rules (/mcp/*, /admin/mcp/*)
- IAM role for mcp-router pod (IRSA) — GitHub API, AgentCore Identity, CloudWatch
- AgentCore Identity workload identity + credential providers (github, atlassian)
- CloudWatch alarms for MCP server pod health

### Files
\`\`\`
infra/modules/apigateway/
  main.tf, variables.tf, outputs.tf
  authorizer.tf, vpc_link.tf, routes.tf
  streaming.tf, waf.tf, throttling.tf

infra/modules/mcp/
  main.tf, variables.tf, outputs.tf
  iam.tf, alb.tf, ecr.tf
  agentcore_identity.tf, cloudwatch.tf

infra/environments/dev/mcp.tf
infra/environments/prod/mcp.tf
\`\`\`

### Key Details
- API Gateway routes: /v1/*, /bedrock/*, /auth/*, /admin/* → BG target group; /mcp/*, /admin/mcp/* → MCP Router target group
- Streaming routes: POST /v1/chat/completions, POST /bedrock/invoke-with-response-stream, POST /v1/messages, POST /mcp/*/mcp
- VPC Link v2 supports one link → multiple ALB targets
- ALB changes from internet-facing to internal (or keep public with security group restrictions for zero-disruption migration)

### References
- [API Gateway Response Streaming](https://aws.amazon.com/blogs/compute/building-responsive-apis-with-amazon-api-gateway-response-streaming/)
- [API Gateway Private ALB Integration](https://aws.amazon.com/blogs/compute/build-scalable-rest-apis-using-amazon-api-gateway-private-integration-with-application-load-balancer/)

### Rules
- Follow existing Terraform patterns
- Do NOT modify existing modules (eks, rds, etc.)
- Reference existing ALB and EKS outputs"

echo "Updated MCP-4"
echo "Done!"
