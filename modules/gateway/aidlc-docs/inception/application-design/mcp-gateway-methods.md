# MCP Gateway — Component Methods (Final Architecture)

## Database Models

```python
# src/shared/models/mcpgateway.py

class MCPCatalogue(Base):
    """Platform-level registry of available MCP servers"""
    __tablename__ = "mcp_catalogue"

    id: int                        # PK
    name: str                      # Unique slug ("github-mcp")
    display_name: str              # "GitHub MCP Server"
    description: str
    category: str                  # "version-control", "database", etc.
    deployment_type: str           # "self_hosted" or "remote"
    sharing_mode: str              # "shared" or "per_org"
    credential_level: str          # "none", "org", "user"
    docker_image: str              # ECR URI (nullable for remote)
    default_port: int              # 8000 (nullable for remote)
    remote_url_template: str       # "https://{account}.snowflake.com/mcp" (nullable for self_hosted)
    identity_provider_name: str    # AgentCore Identity provider name (nullable for none)
    identity_auth_flow: str        # "USER_FEDERATION" or "M2M" (nullable for none)
    identity_scopes: list          # JSON: ["read:jira-work", "offline_access"]
    required_url_params: list      # JSON: ["account"] for remote URL template
    optional_env_vars: list        # JSON: [{"name":"READ_ONLY","default":"true"}]
    documentation_url: str
    repo_path: str                 # "catalogue/github-mcp" — path in GitHub repo
    verified: bool
    status: str                    # REGISTERED, VERIFIED, DEPRECATED
    created_at: datetime
    updated_at: datetime

class MCPDeployment(Base):
    """Org-level instance of a catalog entry"""
    __tablename__ = "mcp_deployment"

    id: int                        # PK
    org_id: int | None             # FK → Organization (NULL = shared/platform)
    catalogue_id: int              # FK → MCPCatalogue
    instance_name: str             # Unique within org
    deployment_type: str           # Inherited from catalogue
    status: str                    # PENDING, DEPLOYING, DEPLOYED, FAILED, DISABLED, REMOVED
    repo_path: str                 # "deployments/acme/postgres-mcp.json" (nullable for shared)
    # Self-hosted:
    k8s_service_name: str          # "postgres-mcp-acme" (nullable for remote)
    k8s_namespace: str             # "mcp-servers" (nullable for remote)
    replicas: int                  # (nullable for remote)
    # Remote:
    remote_url: str                # Resolved from template (nullable for self_hosted)
    # AgentCore Identity:
    identity_workload_id: str      # Workload identity ID (nullable for credential_level=none)
    # Runtime state:
    tool_count: int
    last_health_check: datetime
    last_tool_sync: datetime
    created_at: datetime
    updated_at: datetime

class MCPToolGroup(Base):
    """Named collection of tools from one or more deployments"""
    __tablename__ = "mcp_tool_group"

    id: int
    org_id: int                    # FK → Organization
    name: str
    description: str
    service_account_safe: bool
    created_at: datetime
    updated_at: datetime

class MCPToolGroupRule(Base):
    """Which tools from which deployment"""
    __tablename__ = "mcp_tool_group_rule"

    id: int
    tool_group_id: int             # FK → MCPToolGroup
    deployment_id: int             # FK → MCPDeployment
    include_pattern: str           # "*" or regex
    created_at: datetime

class MCPToolGroupAssignment(Base):
    """Assigns tool group to department/team/service_account"""
    __tablename__ = "mcp_tool_group_assignment"

    id: int
    tool_group_id: int             # FK → MCPToolGroup
    entity_type: str               # "department", "team", "service_account"
    entity_id: int
    org_id: int                    # FK → Organization
    created_at: datetime
```

## ER Diagram

```
mcp_catalogue (1) ──────< (N) mcp_deployment
                                    |
                                    └──< mcp_tool_group_rule >── mcp_tool_group
                                                                      |
                                                                      └──< mcp_tool_group_assignment
                                                                                  |
                                                                                  └── department / team / service_account

External (not in our DB):
  AgentCore Identity ── credential providers (per service)
                     ── token vault (per user per provider)
                     ── workload identities (per deployment)
```

4 tables total. Credentials managed entirely by AgentCore Identity.

## Pydantic Schemas

```python
class CreateCatalogueEntryRequest(BaseModel):
    name: str
    display_name: str
    description: str
    category: str
    deployment_type: str           # "self_hosted" or "remote"
    sharing_mode: str              # "shared" or "per_org"
    credential_level: str          # "none", "org", "user"
    docker_image: str | None = None
    default_port: int = 8000
    remote_url_template: str | None = None
    identity_provider_name: str | None = None
    identity_auth_flow: str | None = None
    identity_scopes: list[str] = []
    documentation_url: str = ""

class CatalogueEntryResponse(BaseModel):
    id: int
    name: str
    display_name: str
    description: str
    category: str
    deployment_type: str
    sharing_mode: str
    credential_level: str
    verified: bool
    status: str

class DeployFromCatalogueRequest(BaseModel):
    catalogue_id: int
    instance_name: str
    replicas: int = 1
    env_overrides: dict[str, str] = {}
    remote_url_params: dict[str, str] = {}  # For remote URL template

class DeploymentResponse(BaseModel):
    id: int
    instance_name: str
    catalogue_name: str
    status: str
    tool_count: int
    credential_status: str         # "ready", "setup_required", "org_managed", "not_needed"
    last_health_check: datetime | None

class MCPToolResponse(BaseModel):
    name: str
    description: str
    parameters: dict
    source_server: str

class MCPConnectionResponse(BaseModel):
    base_url: str
    servers: list[ServerConnectionInfo]
    auth_header: str

class ServerConnectionInfo(BaseModel):
    name: str
    url: str
    tool_count: int
    credential_status: str         # "ready", "setup_required", "not_needed"
```

## Service Interfaces

```python
class ICatalogueService(ABC):
    async def create_entry(self, request: CreateCatalogueEntryRequest) -> CatalogueEntryResponse
    async def list_entries(self, include_deprecated: bool = False) -> list[CatalogueEntryResponse]
    async def verify_entry(self, entry_id: int) -> None
    async def deprecate_entry(self, entry_id: int) -> None

class IDeploymentService(ABC):
    async def deploy(self, request: DeployFromCatalogueRequest, org_id: int) -> DeploymentResponse
    async def list_deployments(self, org_id: int) -> list[DeploymentResponse]
    async def disable(self, deployment_id: int, org_id: int) -> None
    async def enable(self, deployment_id: int, org_id: int) -> None
    async def remove(self, deployment_id: int, org_id: int) -> None
    async def trigger_github_deploy(self, deployment: MCPDeployment) -> None
    async def trigger_github_remove(self, deployment: MCPDeployment) -> None

class IMCPProxyService(ABC):
    async def proxy_request(self, deployment_name: str, request: Request, context: TokenContext) -> Response

class IToolGroupService(ABC):
    async def create_group(self, request: CreateToolGroupRequest, org_id: int) -> ToolGroupResponse
    async def list_groups(self, org_id: int) -> list[ToolGroupResponse]
    async def assign_group(self, group_id: int, assignment: AssignToolGroupRequest, org_id: int) -> None
    async def check_access(self, deployment_name: str, context: TokenContext) -> bool

class IDiscoveryService(ABC):
    async def list_tools(self, context: TokenContext) -> list[MCPToolResponse]
    async def search_tools(self, query: str, context: TokenContext) -> list[MCPToolResponse]
    async def get_connection_info(self, context: TokenContext) -> MCPConnectionResponse

class IHealthService(ABC):
    async def check_deployment(self, deployment_id: int) -> str
    async def check_all(self, org_id: int) -> list[HealthStatus]
    async def refresh_tools(self, deployment_id: int) -> int
```

Note: No ICredentialService — AgentCore Identity handles all credential operations via SDK decorators in MCP server code.

## API Routes

```python
# === Platform Admin: Catalog ===
POST   /admin/mcp/catalogue                           # Add to catalog
GET    /admin/mcp/catalogue                           # List catalog
PUT    /admin/mcp/catalogue/{id}                      # Update
PUT    /admin/mcp/catalogue/{id}/verify               # Mark verified
PUT    /admin/mcp/catalogue/{id}/deprecate            # Deprecate

# === Org Admin: Marketplace & Deployments ===
GET    /admin/mcp/marketplace                         # Browse catalog
POST   /admin/mcp/deployments                         # Deploy from catalog
GET    /admin/mcp/deployments                         # List org's deployments
GET    /admin/mcp/deployments/{id}                    # Details
PUT    /admin/mcp/deployments/{id}/disable
PUT    /admin/mcp/deployments/{id}/enable
DELETE /admin/mcp/deployments/{id}
POST   /admin/mcp/deployments/{id}/refresh-tools

# === Org Admin: Tool Groups ===
POST   /admin/mcp/tool-groups
GET    /admin/mcp/tool-groups
PUT    /admin/mcp/tool-groups/{id}
DELETE /admin/mcp/tool-groups/{id}
GET    /admin/mcp/tool-groups/{id}/tools
POST   /admin/mcp/tool-groups/{id}/assignments
DELETE /admin/mcp/tool-groups/{id}/assignments/{type}/{eid}

# === Health ===
GET    /admin/mcp/health

# === User-facing ===
GET    /mcp/tools                                     # List accessible tools
GET    /mcp/tools/search?q={query}                    # Search
GET    /mcp/connection                                # Connection info
POST   /mcp/{deployment-name}/mcp                     # MCP proxy
GET    /mcp/oauth/callback                            # AgentCore Identity OAuth callback
```
