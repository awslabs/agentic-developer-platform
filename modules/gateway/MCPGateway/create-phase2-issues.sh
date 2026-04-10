#!/bin/bash
REPO="PranavSharma1000/bedrock-gateway"

# Create phase-2 label
gh label create "phase-2" --description "Phase 2 enhancement" --color "D4C5F9" --repo "$REPO" 2>/dev/null

# Epic MCP-10: Virtual Server Session Multiplexing
gh issue create --repo "$REPO" \
  --title "Epic MCP-10: Virtual Server Session Multiplexing" \
  --label "epic,mcp-gateway,phase-2" \
  --body "## Epic MCP-10: Virtual Server Session Multiplexing

Aggregate tools from multiple backend MCP servers into a single virtual endpoint. Clients connect once, see merged tool catalog, Router multiplexes sessions transparently.

Inspired by: agentic-community/mcp-gateway-registry virtual server support with Lua-based tool aggregation.

### Stories
- US-MCP-10.1: Virtual MCP Server Endpoint"

echo "Created Epic MCP-10"

# US-MCP-10.1
gh issue create --repo "$REPO" \
  --title "US-MCP-10.1: Virtual MCP Server Endpoint" \
  --label "mcp-gateway,phase-2,unit:mcp-1" \
  --body "## US-MCP-10.1: Virtual MCP Server Endpoint

**As a** Developer (Dev),
**I want to** connect to a single virtual MCP server endpoint that aggregates tools from multiple backend servers,
**So that** my MCP client has one connection instead of N separate connections.

**Epic**: MCP-10 | **Phase**: 2

### Acceptance Criteria
- [ ] Admin can create a virtual server that combines tools from multiple deployments
- [ ] Client connects to one URL (\`/mcp/virtual/{name}/mcp\`) and sees merged tool catalog
- [ ] MCP Router multiplexes one client session to N backend sessions transparently
- [ ] Tool name conflicts resolved via aliasing (e.g., \`github__list_issues\` vs \`jira__list_issues\`)
- [ ] tools/list returns merged catalog; tools/call routes to correct backend based on tool name
- [ ] Backend session lifecycle managed by Router (create on first call, cleanup on disconnect)"

echo "Created US-MCP-10.1"

# Epic MCP-11: Security Scanning
gh issue create --repo "$REPO" \
  --title "Epic MCP-11: MCP Server Security Scanning" \
  --label "epic,mcp-gateway,phase-2" \
  --body "## Epic MCP-11: MCP Server Security Scanning

Automated security scanning for MCP servers on registration and periodically. Detect vulnerabilities, prompt injection risks, and unsafe tool definitions.

Inspired by: agentic-community/mcp-gateway-registry Cisco AI Defense scanner integration.

### Stories
- US-MCP-11.1: Security Scan on Registration
- US-MCP-11.2: Security Scan Dashboard"

echo "Created Epic MCP-11"

# US-MCP-11.1
gh issue create --repo "$REPO" \
  --title "US-MCP-11.1: Security Scan on Registration" \
  --label "mcp-gateway,phase-2,unit:mcp-1" \
  --body "## US-MCP-11.1: Security Scan on Registration

**As a** Platform Admin (Priya),
**I want** MCP servers to be automatically scanned for security vulnerabilities when registered in the catalog,
**So that** only safe, vetted servers are available for deployment.

**Epic**: MCP-11 | **Phase**: 2

### Acceptance Criteria
- [ ] Automated security scan runs on new catalog entry creation
- [ ] Scan checks for: known vulnerabilities, prompt injection patterns, data exfiltration risks, unsafe tool definitions
- [ ] Scan results stored and visible in Admin UI with severity levels (pass/warn/fail)
- [ ] Servers that fail scan flagged as security_status: failed and cannot be deployed
- [ ] Platform Admin can override with justification logged
- [ ] Periodic re-scans configurable (e.g., weekly)"

echo "Created US-MCP-11.1"

# US-MCP-11.2
gh issue create --repo "$REPO" \
  --title "US-MCP-11.2: Security Scan Dashboard" \
  --label "mcp-gateway,phase-2,unit:mcp-2" \
  --body "## US-MCP-11.2: Security Scan Dashboard

**As a** Platform Admin (Priya),
**I want to** see security scan results for all catalog entries in the Admin UI.

**Epic**: MCP-11 | **Phase**: 2

### Acceptance Criteria
- [ ] Security dashboard shows all catalog entries with scan status (pass/warn/fail/not-scanned)
- [ ] Click into entry shows detailed scan report with findings and remediation
- [ ] Rescan button triggers on-demand scan
- [ ] Alert on new vulnerabilities found in periodic scans"

echo "Created US-MCP-11.2"

# Epic MCP-12: External Registry Import
gh issue create --repo "$REPO" \
  --title "Epic MCP-12: External Registry Import" \
  --label "epic,mcp-gateway,phase-2" \
  --body "## Epic MCP-12: External Registry Import

Import curated MCP servers from external registries (Anthropic MCP Registry, Docker MCP Catalog, community registries) to bootstrap the catalog.

Inspired by: agentic-community/mcp-gateway-registry Anthropic Registry import and federation features.

### Stories
- US-MCP-12.1: Import from Anthropic MCP Registry
- US-MCP-12.2: Import from Custom External Registries"

echo "Created Epic MCP-12"

# US-MCP-12.1
gh issue create --repo "$REPO" \
  --title "US-MCP-12.1: Import from Anthropic MCP Registry" \
  --label "mcp-gateway,phase-2,unit:mcp-1" \
  --body "## US-MCP-12.1: Import from Anthropic MCP Registry

**As a** Platform Admin (Priya),
**I want to** import curated MCP servers from Anthropic's official MCP Registry into my catalog,
**So that** I can quickly bootstrap the catalog with vetted, community-maintained servers.

**Epic**: MCP-12 | **Phase**: 2

### Acceptance Criteria
- [ ] Admin UI 'Import from Registry' page shows available servers from Anthropic's MCP Registry API
- [ ] Platform Admin can select servers to import
- [ ] Imported entries created in mcp_catalogue with source tagged as 'anthropic-registry'
- [ ] Import includes: name, description, Docker image or remote URL, documentation link
- [ ] Imported servers require verification before org admins can deploy them
- [ ] Periodic sync option to check for updates to imported servers"

echo "Created US-MCP-12.1"

# US-MCP-12.2
gh issue create --repo "$REPO" \
  --title "US-MCP-12.2: Import from Custom External Registries" \
  --label "mcp-gateway,phase-2,unit:mcp-1" \
  --body "## US-MCP-12.2: Import from Custom External Registries

**As a** Platform Admin (Priya),
**I want to** import MCP servers from other external registries (Docker MCP Catalog, community registries),
**So that** I can aggregate servers from multiple sources into one catalog.

**Epic**: MCP-12 | **Phase**: 2

### Acceptance Criteria
- [ ] Configurable external registry sources with URL and auth credentials
- [ ] Import flow: browse external registry -> select servers -> import to local catalog
- [ ] Source registry tagged on each imported entry for traceability
- [ ] Conflict detection if imported server name already exists in catalog"

echo "Created US-MCP-12.2"


# Epic MCP-13: Server Versioning
gh issue create --repo "$REPO" \
  --title "Epic MCP-13: MCP Server Versioning" \
  --label "epic,mcp-gateway,phase-2" \
  --body "## Epic MCP-13: MCP Server Versioning

Run multiple versions of the same MCP server simultaneously. Test new versions before promoting. Instant rollback.

Inspired by: agentic-community/mcp-gateway-registry version routing with header-based pinning.

### Stories
- US-MCP-13.1: Deploy Multiple Versions
- US-MCP-13.2: Version Promotion and Rollback"

echo "Created Epic MCP-13"

# US-MCP-13.1
gh issue create --repo "$REPO" \
  --title "US-MCP-13.1: Deploy Multiple Versions of MCP Server" \
  --label "mcp-gateway,phase-2,unit:mcp-1,unit:mcp-3" \
  --body "## US-MCP-13.1: Deploy Multiple Versions

**As a** Platform Admin (Priya),
**I want to** run multiple versions of the same MCP server simultaneously,
**So that** I can test new versions before promoting them to production.

**Epic**: MCP-13 | **Phase**: 2

### Acceptance Criteria
- [ ] Catalog entry supports multiple image versions (e.g., github-mcp:1.0, github-mcp:2.0)
- [ ] Only one version is 'active' (receives traffic by default)
- [ ] Other versions are 'inactive' (deployed but not routed to unless explicitly requested)
- [ ] Client can pin to a specific version via X-MCP-Server-Version header
- [ ] Admin UI shows all versions with active/inactive status"

echo "Created US-MCP-13.1"

# US-MCP-13.2
gh issue create --repo "$REPO" \
  --title "US-MCP-13.2: Version Promotion and Rollback" \
  --label "mcp-gateway,phase-2,unit:mcp-1,unit:mcp-2" \
  --body "## US-MCP-13.2: Version Promotion and Rollback

**As a** Platform Admin (Priya),
**I want to** promote a new version to active and instantly rollback if issues are found,
**So that** version upgrades are safe and reversible.

**Epic**: MCP-13 | **Phase**: 2

### Acceptance Criteria
- [ ] 'Promote' action in Admin UI switches active version (single click)
- [ ] 'Rollback' action reverts to previous active version (single click)
- [ ] Version switch takes effect immediately
- [ ] Health check runs automatically after version switch
- [ ] Version history with timestamps visible in Admin UI"

echo "Created US-MCP-13.2"

# Epic MCP-14: A2A Protocol
gh issue create --repo "$REPO" \
  --title "Epic MCP-14: A2A (Agent-to-Agent) Protocol Support" \
  --label "epic,mcp-gateway,phase-2" \
  --body "## Epic MCP-14: A2A (Agent-to-Agent) Protocol Support

Register AI agents alongside MCP servers. Enable agent discovery and agent-to-agent communication through the unified registry.

Inspired by: agentic-community/mcp-gateway-registry A2A agent registry with peer-to-peer communication.

### Stories
- US-MCP-14.1: Agent Registration and Discovery
- US-MCP-14.2: Agent-to-Agent Communication"

echo "Created Epic MCP-14"

# US-MCP-14.1
gh issue create --repo "$REPO" \
  --title "US-MCP-14.1: Agent Registration and Discovery" \
  --label "mcp-gateway,phase-2,unit:mcp-1,unit:mcp-2" \
  --body "## US-MCP-14.1: Agent Registration and Discovery

**As a** Platform Admin (Priya),
**I want to** register AI agents in the gateway alongside MCP servers,
**So that** agents can discover and communicate with other agents through a unified registry.

**Epic**: MCP-14 | **Phase**: 2

### Acceptance Criteria
- [ ] Agents registered with: name, description, endpoint URL, agent card (capabilities, skills, auth schemes)
- [ ] Agents discoverable via semantic search alongside MCP servers
- [ ] Agent cards follow A2A protocol specification
- [ ] Access control: which teams/agents can discover and invoke which agents
- [ ] Agent health monitoring (periodic ping to agent endpoint)"

echo "Created US-MCP-14.1"

# US-MCP-14.2
gh issue create --repo "$REPO" \
  --title "US-MCP-14.2: Agent-to-Agent Communication" \
  --label "mcp-gateway,phase-2,unit:mcp-1" \
  --body "## US-MCP-14.2: Agent-to-Agent Communication

**As an** AI Agent,
**I want to** discover and communicate with other registered agents through the gateway,
**So that** I can delegate tasks to specialized agents.

**Epic**: MCP-14 | **Phase**: 2

### Acceptance Criteria
- [ ] Discovery API: POST /api/agents/discover/semantic returns matching agents with relevance scores
- [ ] Agent-to-agent communication follows A2A protocol (direct peer-to-peer after discovery)
- [ ] Gateway handles discovery and auth; agents communicate directly for low latency
- [ ] All discovery and communication events logged for audit"

echo "Created US-MCP-14.2"

echo ""
echo "All Phase 2 issues created!"
