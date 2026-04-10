# MCP Gateway — Service Layer Design (Final Architecture)

## Architecture: API Gateway + EKS + GitHub Configs + AgentCore Identity

```
Developer (Claude Desktop, Cursor)
    |
    | Cognito JWT
    v
ALB (existing, shared) ──→ TLS termination, path routing
    |
    | /mcp/* → MCP Router target group
    v
MCP Router (FastAPI on EKS)
    |  - auth validation (same Cognito middleware as BG)
    |  - rate limiting (same token bucket as BG)
    |  - route by path
    |  - tool group access check
    |  - usage logging
    |
    ├──→ Self-hosted MCP pod (K8s ClusterIP)
    |      uses @requires_access_token (AgentCore Identity)
    |      → token from vault, auto-refresh
    |
    └──→ Remote MCP server (external URL)
           auth injected by AgentCore Identity SDK

Config source of truth: GitHub repo (mcp-servers/)
Credential source of truth: AgentCore Identity (token vault)
State/access control: PostgreSQL
Runtime: EKS (existing cluster)
```

## Catalog Registration Flow (Platform Admin)

```
Platform Admin (Priya)
    |
    | 1. Commits catalogue/jira-mcp/catalogue.json to GitHub repo
    | 2. If OAuth provider needed: creates AgentCore Identity credential provider
    |      identity_client.create_oauth2_credential_provider({
    |          "name": "atlassian-provider",
    |          "credentialProviderVendor": "AtlassianOauth2",  # built-in!
    |          "oauth2ProviderConfigInput": {
    |              "atlassianOauth2ProviderConfig": {
    |                  "clientId": "<platform-app-client-id>",
    |                  "clientSecret": "<platform-app-client-secret>"
    |              }
    |          }
    |      })
    |
    v
GitHub Actions: validates JSON, syncs to mcp_catalogue table
AgentCore Identity: provider ready for user OAuth flows
```

## Deployment Flow (Org Admin deploys from catalog)

```
Org Admin (Omar) → Admin UI → Marketplace → "Deploy Jira MCP"
    |
    | For credential_level=none or user (shared servers):
    |   No deployment needed — shared pod already running
    |   Just enable in tool group
    |
    | For credential_level=org (per-org servers):
    |   Prompted for org credentials (e.g., DB connection string)
    |   → Stored as AgentCore Identity API key provider
    |   → Admin UI creates deployment config JSON
    |   → Commits to GitHub: deployments/acme/postgres-mcp.json
    |   → GitHub Actions deploys K8s pod with org-specific config
    |
    v
Deployment status tracked in mcp_deployment table
```

## MCP Request Flow — User-Level Credentials (Jira, GitHub)

```
Sarah (Developer) asks Claude: "Show my open Jira bugs"
    |
    | POST https://api.company.com/mcp/github/mcp
    | Headers: Authorization: Bearer <sarah's-cognito-jwt>
    v
[ALB] → TLS termination, routes /mcp/* to MCP Router target group
    |
    v
[MCP Router]
    +-- Validate Cognito JWT (same auth middleware as BedrockGateway)
    +-- Apply rate limit (same token bucket as BedrockGateway)
    +-- Extract: server=jira-mcp, user=sarah, org=acme, team=platform-eng
    +-- Check tool group access ✓
    +-- Forward to shared jira-mcp pod
    |
    v
[Jira MCP pod] — server code uses AgentCore Identity SDK:

    @requires_access_token(
        provider_name="atlassian-provider",
        scopes=["read:jira-work", "offline_access"],
        auth_flow="USER_FEDERATION",
        on_auth_url=lambda url: stream_auth_url(url),
        force_authentication=False,
        callback_url="https://api.company.com/mcp/oauth/callback"
    )
    async def search_issues(*, access_token: str, jql: str):
        # access_token auto-injected by AgentCore Identity
        return await jira_api.search(jql, token=access_token)

    FIRST TIME: AgentCore Identity returns auth URL
        → Sarah's browser opens Atlassian consent screen
        → Sarah clicks "Allow"
        → AgentCore Identity stores token + refresh token in vault
        → Tool call completes

    SUBSEQUENT TIMES: AgentCore Identity retrieves from vault
        → Auto-refreshes if expired (using refresh token)
        → Tool call completes immediately, no user interaction
    |
    v
[MCP Router] → logs to usage_logs → returns response
    |
    v
Claude shows Sarah her Jira bugs
```

## MCP Request Flow — Org-Level Credentials (PostgreSQL)

```
Developer calls /mcp/postgres-mcp/mcp
    |
    v
[MCP Router] → routes to org-specific pod: postgres-mcp-acme
    |
    v
[postgres-mcp-acme pod] — server code uses AgentCore Identity SDK:

    @requires_api_key(provider_name="acme-postgres-key")
    async def query(*, api_key: str, sql: str):
        # api_key = the connection string, retrieved from AgentCore Identity
        return await pg_client.execute(sql, dsn=api_key)

    AgentCore Identity retrieves the API key stored by Org Admin
    No user interaction needed — M2M pattern
```

## MCP Request Flow — No Credentials (Terraform)

```
Developer calls /mcp/terraform-mcp/mcp
    |
    v
[MCP Router] → routes to shared terraform-mcp pod
    |
    v
[terraform-mcp pod] — no credentials needed
    Just calls Terraform Registry API (public, no auth)
```

## Tool Discovery Flow

```
GET /mcp/tools
    |
    v
[DiscoveryService]
    +-- Get team's tool group assignments
    +-- For each accessible deployment:
    |     +-- Get cached tool list (from periodic tools/list calls)
    |     +-- Filter by group rules
    +-- Merge and return
    |
    v
Response includes credential_status per server:
  - "ready" (no creds needed, or user already authorized)
  - "setup_required" (user needs to complete OAuth consent)
  - "org_managed" (org admin handles credentials)
```

## OAuth Callback Flow

```
AgentCore Identity redirects user's browser after OAuth consent:
    |
    | GET /mcp/oauth/callback?session_id=xxx
    v
[BedrockGateway callback endpoint]
    +-- Validate user's active session (Cognito JWT from cookie)
    +-- Call AgentCore Identity: complete_resource_token_auth(
    |       session_uri=session_id,
    |       user_identifier=UserIdIdentifier(user_id=sarah_id)
    |   )
    +-- Redirect user back to Admin UI or show "Authorization complete"
    |
    v
Token stored in AgentCore Identity vault
Next tool call will use it automatically
```

## GitHub-Based Deployment Pipeline

```
Config committed to mcp-servers repo:
    |
    | deployments/acme/postgres-mcp.json
    v
GitHub Actions (deploy-server.yml):
    |
    +-- Detect changed files
    +-- Read catalogue entry for the server type
    +-- If self_hosted:
    |     +-- kubectl apply Deployment + Service
    |     +-- Wait for pod Ready
    |     +-- Call tools/list to discover tools
    +-- Call BedrockGateway API to update mcp_deployment status
    |
    v
Deployment READY
```

## Health & Tool Refresh (Background)

```python
# Runs in MCP Router process

async def health_check_loop():
    """Every 5 minutes"""
    for deployment in get_active_deployments():
        if deployment.deployment_type == "self_hosted":
            healthy = await check_k8s_pod(deployment)
        else:
            healthy = await ping_remote_url(deployment)
        update_status(deployment, "DEPLOYED" if healthy else "FAILED")

async def tool_refresh_loop():
    """Every 6 hours"""
    for deployment in get_deployed():
        tools = await call_tools_list(deployment)
        update_tool_count(deployment, len(tools))
```

## Dependency Injection

```python
# src/app.py startup

app.state.catalogue_service = CatalogueService(catalogue_repo)
app.state.deployment_service = DeploymentService(
    deployment_repo, catalogue_repo, github_client, httpx_client
)
app.state.mcp_proxy_service = MCPProxyService(httpx_client, deployment_repo)
app.state.tool_group_service = ToolGroupService(
    tool_group_repo, rule_repo, assignment_repo
)
app.state.discovery_service = DiscoveryService(
    tool_group_service, deployment_service, httpx_client
)
app.state.health_service = HealthService(deployment_repo, httpx_client)

# Note: No CredentialService — AgentCore Identity handles all credentials
# Note: No K8s client — GitHub Actions handles K8s deployments
```
