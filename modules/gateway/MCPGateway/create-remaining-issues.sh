#!/bin/bash
REPO="PranavSharma1000/bedrock-gateway"

# US-MCP-2.2
gh issue create --repo "$REPO" \
  --title "US-MCP-2.2: Deploy MCP Server for Organization" \
  --label "mcp-gateway,unit:mcp-1,unit:mcp-3" \
  --body "## US-MCP-2.2: Deploy MCP Server for Organization

**As an** Org Admin (Omar),
**I want to** deploy an MCP server from the catalog with my org's credentials,
**So that** my teams can use it through the gateway.

**Epic**: MCP-2 (#148) | **Units**: MCP-1 (Backend), MCP-3 (Config Repo)

### Acceptance Criteria
- [ ] \`POST /admin/mcp/deployments\` accepts: catalogue_id, instance_name, credentials (key-value), replicas
- [ ] System prompts for each credential defined in catalog entry's required_credentials
- [ ] For credential_level=org: credentials stored via AgentCore Identity API key provider
- [ ] For credential_level=user: no org credentials needed (users authorize individually via OAuth)
- [ ] For self_hosted: BedrockGateway creates deployment JSON, commits to mcp-servers GitHub repo via GitHub API, triggers GitHub Actions workflow
- [ ] GitHub Actions: creates K8s Deployment + ClusterIP Service, waits for Ready, calls tools/list, callbacks to update status
- [ ] For remote: stores remote URL (from template), no K8s resources
- [ ] Status transitions: PENDING -> DEPLOYING -> DEPLOYED (or FAILED)
- [ ] Duplicate instance_name within same org returns 409"

# US-MCP-2.3
gh issue create --repo "$REPO" \
  --title "US-MCP-2.3: Manage Org Deployments" \
  --label "mcp-gateway,unit:mcp-1,unit:mcp-2" \
  --body "## US-MCP-2.3: Manage Org Deployments

**As an** Org Admin (Omar),
**I want to** view, disable, re-enable, and remove my org's MCP server deployments,
**So that** I can manage my org's MCP tool ecosystem.

**Epic**: MCP-2 (#148) | **Units**: MCP-1 (Backend), MCP-2 (Admin UI)

### Acceptance Criteria
- [ ] \`GET /admin/mcp/deployments\` lists org's deployments with: instance_name, catalog name, status, tool_count, last_health_check
- [ ] \`PUT /admin/mcp/deployments/{id}/disable\` sets status=DISABLED (stops routing, keeps pod)
- [ ] \`PUT /admin/mcp/deployments/{id}/enable\` sets status=DEPLOYED (resumes routing)
- [ ] \`DELETE /admin/mcp/deployments/{id}\` deletes config from GitHub repo, triggers removal workflow
- [ ] Org Admins can only manage their own org's deployments"

# US-MCP-2.4
gh issue create --repo "$REPO" \
  --title "US-MCP-2.4: Rotate Deployment Credentials" \
  --label "mcp-gateway,unit:mcp-1" \
  --body "## US-MCP-2.4: Rotate Deployment Credentials

**As an** Org Admin (Omar),
**I want to** rotate credentials for a deployed MCP server,
**So that** I can maintain security without redeploying.

**Epic**: MCP-2 (#148) | **Unit**: MCP-1 (Backend)

### Acceptance Criteria
- [ ] For credential_level=org: admin updates credential via AgentCore Identity API, triggers pod restart
- [ ] For credential_level=user: users re-authorize via OAuth flow (AgentCore Identity handles refresh)
- [ ] Credential values never returned in API responses
- [ ] Rotation timestamp recorded"

# US-MCP-3.1
gh issue create --repo "$REPO" \
  --title "US-MCP-3.1: Create Tool Groups" \
  --label "mcp-gateway,unit:mcp-1" \
  --body "## US-MCP-3.1: Create Tool Groups

**As an** Org Admin (Omar),
**I want to** create tool groups that curate tools from my org's deployed servers,
**So that** I can control which teams see which tools.

**Epic**: MCP-3 (#149) | **Unit**: MCP-1 (Backend)

### Acceptance Criteria
- [ ] \`POST /admin/mcp/tool-groups\` accepts: name, description, service_account_safe, rules (list of deployment_id + include_pattern)
- [ ] Rules reference org's own deployments only
- [ ] include_pattern: '*' for all tools, or regex like 'list_*'
- [ ] \`GET /admin/mcp/tool-groups\` lists groups with tool counts and assigned team counts"

# US-MCP-3.2
gh issue create --repo "$REPO" \
  --title "US-MCP-3.2: Assign Tool Groups to Teams" \
  --label "mcp-gateway,unit:mcp-1,unit:mcp-2" \
  --body "## US-MCP-3.2: Assign Tool Groups to Teams

**As an** Org Admin (Omar),
**I want to** assign tool groups to departments, teams, or service accounts,
**So that** each team only sees relevant MCP tools.

**Epic**: MCP-3 (#149) | **Units**: MCP-1 (Backend), MCP-2 (Admin UI)

### Acceptance Criteria
- [ ] \`POST /admin/mcp/tool-groups/{id}/assignments\` accepts: entity_type, entity_id
- [ ] A team with no assignments sees no MCP tools
- [ ] A team can have multiple tool groups (tools merged)
- [ ] Department-level assignment applies to all teams in that department
- [ ] Service accounts can only be assigned groups marked service_account_safe=true"

# US-MCP-4.1
gh issue create --repo "$REPO" \
  --title "US-MCP-4.1: Discover Available Tools" \
  --label "mcp-gateway,unit:mcp-1" \
  --body "## US-MCP-4.1: Discover Available Tools

**As a** Developer (Dev),
**I want to** see which MCP tools are available to me,
**So that** I can configure my AI assistant.

**Epic**: MCP-4 (#150) | **Unit**: MCP-1 (Backend)

### Acceptance Criteria
- [ ] \`GET /mcp/tools\` returns tools filtered by caller's tool group assignments
- [ ] Response includes: tool name, description, parameter schema, source server name, credential_status
- [ ] Cached, refreshed periodically
- [ ] credential_status per server: ready, setup_required (user OAuth needed), not_needed"

# US-MCP-4.2
gh issue create --repo "$REPO" \
  --title "US-MCP-4.2: Get MCP Client Connection Info" \
  --label "mcp-gateway,unit:mcp-1,unit:mcp-2" \
  --body "## US-MCP-4.2: Get MCP Client Connection Info

**As a** Developer (Dev),
**I want to** get connection URLs for my MCP client,
**So that** I can configure Claude Desktop or Cursor.

**Epic**: MCP-4 (#150) | **Units**: MCP-1 (Backend), MCP-2 (Admin UI)

### Acceptance Criteria
- [ ] \`GET /mcp/connection\` returns per-server URLs with auth header format
- [ ] Includes pre-formatted configs for Claude Desktop, Cursor, Strands
- [ ] Shows credential_status per server"

# US-MCP-4.3
gh issue create --repo "$REPO" \
  --title "US-MCP-4.3: Invoke MCP Tools Through Gateway" \
  --label "mcp-gateway,unit:mcp-1" \
  --body "## US-MCP-4.3: Invoke MCP Tools Through Gateway

**As a** Developer (Dev),
**I want to** call MCP tools through the gateway,
**So that** I get centralized auth, access control, and logging.

**Epic**: MCP-4 (#150) | **Unit**: MCP-1 (Backend)

### Acceptance Criteria
- [ ] \`POST /mcp/{deployment-name}/mcp\` forwards MCP JSON-RPC to correct backend
- [ ] Auth validated by MCP Router (same Cognito JWT middleware as BedrockGateway)
- [ ] Tool group access checked before forwarding
- [ ] For self_hosted: forwarded to K8s ClusterIP service
- [ ] For remote: forwarded to remote URL with auth from AgentCore Identity
- [ ] For credential_level=user: AgentCore Identity SDK injects user's OAuth token
- [ ] Every call logged to usage_logs with tenant attribution (request_type=mcp)"

# US-MCP-5.1
gh issue create --repo "$REPO" \
  --title "US-MCP-5.1: Health Monitoring" \
  --label "mcp-gateway,unit:mcp-1,unit:mcp-2" \
  --body "## US-MCP-5.1: Health Monitoring

**As a** Platform Admin (Priya),
**I want to** see health status of all MCP server deployments.

**Epic**: MCP-5 (#151) | **Units**: MCP-1 (Backend), MCP-2 (Admin UI)

### Acceptance Criteria
- [ ] Health check every 5 minutes: K8s pod status (self_hosted) or HTTP ping (remote)
- [ ] Status: DEPLOYED (green), FAILED (red), DISABLED (grey)
- [ ] Admin UI health dashboard shows all deployments with status
- [ ] FAILED status triggers alert"

# US-MCP-5.2
gh issue create --repo "$REPO" \
  --title "US-MCP-5.2: Tool Refresh" \
  --label "mcp-gateway,unit:mcp-1" \
  --body "## US-MCP-5.2: Tool Refresh

**As an** Org Admin (Omar),
**I want** tool lists to stay current.

**Epic**: MCP-5 (#151) | **Unit**: MCP-1 (Backend)

### Acceptance Criteria
- [ ] Periodic tool refresh every 6 hours (calls tools/list on each deployment)
- [ ] 'Refresh Tools' button in Admin UI for on-demand refresh
- [ ] Tool count updated after each refresh"


# US-MCP-6.1
gh issue create --repo "$REPO" \
  --title "US-MCP-6.1: MCP Rate Limits" \
  --label "mcp-gateway,unit:mcp-1" \
  --body "## US-MCP-6.1: MCP Rate Limits

**As an** Org Admin (Omar),
**I want to** set rate limits for MCP tool usage per tool group.

**Epic**: MCP-6 (#152) | **Unit**: MCP-1 (Backend)

### Acceptance Criteria
- [ ] Rate limits configurable per tool group at org/dept/team/user levels
- [ ] Types: RPM and concurrent requests
- [ ] Independent of Bedrock proxy rate limits
- [ ] 429 response with retry guidance when exceeded"

# US-MCP-7.1
gh issue create --repo "$REPO" \
  --title "US-MCP-7.1: Catalog Management UI (Platform Admin)" \
  --label "mcp-gateway,unit:mcp-2" \
  --body "## US-MCP-7.1: Catalog Management UI

**As a** Platform Admin (Priya),
**I want to** manage the MCP server catalog from the Admin UI.

**Epic**: MCP-7 (#153) | **Unit**: MCP-2 (Admin UI)

### Acceptance Criteria
- [ ] 'MCP Catalog' page: list entries, add new, edit, verify, deprecate
- [ ] Form for adding: name, image, description, category, required credentials, docs URL, credential_level, sharing_mode
- [ ] Verified/deprecated badges visible"

# US-MCP-7.2
gh issue create --repo "$REPO" \
  --title "US-MCP-7.2: Server Marketplace UI (Org Admin)" \
  --label "mcp-gateway,unit:mcp-2" \
  --body "## US-MCP-7.2: Server Marketplace UI

**As an** Org Admin (Omar),
**I want to** browse and deploy MCP servers from a marketplace UI.

**Epic**: MCP-7 (#153) | **Unit**: MCP-2 (Admin UI)

### Acceptance Criteria
- [ ] Marketplace page shows catalog entries with category filters
- [ ] 'Deploy' button opens form prompting for required credentials
- [ ] 'Already deployed' indicator for entries the org already has
- [ ] Deployment progress visible (DEPLOYING -> DEPLOYED)
- [ ] Shared servers show 'Available' with no deploy action needed"

# US-MCP-7.3
gh issue create --repo "$REPO" \
  --title "US-MCP-7.3: Deployment Management UI (Org Admin)" \
  --label "mcp-gateway,unit:mcp-2" \
  --body "## US-MCP-7.3: Deployment Management UI

**As an** Org Admin (Omar),
**I want to** manage my org's deployed MCP servers.

**Epic**: MCP-7 (#153) | **Unit**: MCP-2 (Admin UI)

### Acceptance Criteria
- [ ] 'My Deployments' page: list with status, tool count, health, last sync
- [ ] Click into deployment: tool list, credential status, health history
- [ ] Actions: disable, enable, remove, refresh tools"

# US-MCP-7.4
gh issue create --repo "$REPO" \
  --title "US-MCP-7.4: Tool Browser & Connection Info UI" \
  --label "mcp-gateway,unit:mcp-2" \
  --body "## US-MCP-7.4: Tool Browser & Connection Info UI

**As a** Developer (Dev),
**I want to** browse tools and get connection info from the Admin UI.

**Epic**: MCP-7 (#153) | **Unit**: MCP-2 (Admin UI)

### Acceptance Criteria
- [ ] Tool Browser: searchable list of all tools from assigned tool groups
- [ ] Connection Info: per-server URLs, auth token, copy-paste configs for Claude Desktop/Cursor
- [ ] credential_status shown per server (ready, setup_required, not_needed)"

# US-MCP-8.1
gh issue create --repo "$REPO" \
  --title "US-MCP-8.1: MCP Usage Logging & Dashboard" \
  --label "mcp-gateway,unit:mcp-1" \
  --body "## US-MCP-8.1: MCP Usage Logging & Dashboard

**As an** Org Admin (Omar),
**I want to** see MCP tool usage alongside Bedrock usage.

**Epic**: MCP-8 (#154) | **Unit**: MCP-1 (Backend)

### Acceptance Criteria
- [ ] Every MCP tool call logged with tenant attribution (request_type=mcp)
- [ ] Dashboard extended with MCP section: total calls, most-used tools, calls by team
- [ ] Prometheus metrics: mcp_tool_calls_total, mcp_tool_call_duration_seconds, mcp_tool_call_errors_total"

# US-MCP-9.1
gh issue create --repo "$REPO" \
  --title "US-MCP-9.1: MCP Server Unavailable Error" \
  --label "mcp-gateway,unit:mcp-1" \
  --body "## US-MCP-9.1: MCP Server Unavailable Error

**As a** Developer (Dev),
**When** an MCP server deployment is down,
**I want** a clear error identifying which server failed.

**Epic**: MCP-9 (#155) | **Unit**: MCP-1 (Backend)

### Acceptance Criteria
- [ ] Response: {\"error\": \"mcp_server_unavailable\", \"server\": \"postgres-mcp\", \"message\": \"...\"}
- [ ] Other servers continue working"

# US-MCP-9.2
gh issue create --repo "$REPO" \
  --title "US-MCP-9.2: Tool Group Not Assigned Error" \
  --label "mcp-gateway,unit:mcp-1" \
  --body "## US-MCP-9.2: Tool Group Not Assigned Error

**As a** Developer (Dev),
**When** I try to access a server my team doesn't have access to,
**I want** a clear error explaining what to do.

**Epic**: MCP-9 (#155) | **Unit**: MCP-1 (Backend)

### Acceptance Criteria
- [ ] Response: {\"error\": \"tool_not_authorized\", \"server\": \"terraform-mcp\", \"message\": \"Request access from your org admin.\"}
- [ ] GET /mcp/tools never returns tools the caller can't access"

# US-MCP-9.3
gh issue create --repo "$REPO" \
  --title "US-MCP-9.3: MCP Rate Limited Error" \
  --label "mcp-gateway,unit:mcp-1" \
  --body "## US-MCP-9.3: MCP Rate Limited Error

**As a** Developer (Dev),
**When** I exceed MCP rate limits,
**I want** clear retry guidance.

**Epic**: MCP-9 (#155) | **Unit**: MCP-1 (Backend)

### Acceptance Criteria
- [ ] 429 with: {\"error\": \"mcp_rate_limited\", \"retry_after_seconds\": 10}
- [ ] Headers: Retry-After, X-MCP-RateLimit-Remaining"

echo ""
echo "All MCP Gateway stories created!"
