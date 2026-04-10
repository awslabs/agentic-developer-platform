# MCP Gateway — Units of Work

These units extend the existing BedrockGateway unit plan. They can run in parallel with existing Wave 1 units since they depend only on the shared foundation (Unit 0).

---

## Unit MCP-1: MCP Gateway Backend

**Scope**: `src/mcprouter/`, `tests/mcprouter/`
**Agent**: Yes (parallel with existing Wave 1)
**Stories**: US-MCP-1.1, US-MCP-1.2, US-MCP-1.3, US-MCP-2.1, US-MCP-2.2, US-MCP-2.3, US-MCP-2.4, US-MCP-3.1, US-MCP-3.2, US-MCP-4.1, US-MCP-4.2, US-MCP-4.3, US-MCP-5.1, US-MCP-5.2, US-MCP-6.1, US-MCP-8.1, US-MCP-9.1, US-MCP-9.2, US-MCP-9.3

**Delivers**:
- SQLAlchemy models: `MCPCatalogue`, `MCPDeployment`, `MCPToolGroup`, `MCPToolGroupRule`, `MCPToolGroupAssignment` (4 tables + 1 join table)
- Alembic migration for MCP tables
- Pydantic schemas for all MCP API requests/responses
- `CatalogueService` — platform catalog CRUD (register, verify, deprecate)
- `DeploymentService` — org deployment lifecycle (deploy via GitHub API, disable, enable, remove)
- `MCPProxyService` — forward MCP JSON-RPC to target server (K8s ClusterIP or remote URL)
- `ToolGroupService` — tool group CRUD, team assignment, access checks
- `DiscoveryService` — aggregate tools from accessible servers, keyword search, connection info
- `HealthService` — periodic health checks (pod status / remote ping), tool refresh (tools/list)
- `src/mcprouter/router.py` with FastAPI routes:
  - Admin: `/admin/mcp/catalogue/*`, `/admin/mcp/marketplace`, `/admin/mcp/deployments/*`, `/admin/mcp/tool-groups/*`, `/admin/mcp/health`
  - User: `/mcp/tools`, `/mcp/tools/search`, `/mcp/connection`, `/mcp/{deployment-name}/mcp`
  - OAuth callback: `/mcp/oauth/callback` (AgentCore Identity session binding)
- MCP usage logging to existing `usage_logs` table (request_type='mcp')
- Prometheus metrics: `mcp_tool_calls_total`, `mcp_tool_call_duration_seconds`, `mcp_tool_call_errors_total`
- Background scheduler for periodic health checks (5 min) and tool refresh (6 hours)
- GitHub API client for creating/deleting deployment config files and triggering workflows
- Unit tests with mocked GitHub API, mocked httpx for MCP server calls

**Interfaces Implemented**: `ICatalogueService`, `IDeploymentService`, `IMCPProxyService`, `IToolGroupService`, `IDiscoveryService`, `IHealthService`
**Depends On**: Shared foundation (Unit 0) — models, schemas, interfaces, database, app factory
**Independently Testable**: Yes — mock GitHub API, mock httpx for MCP server responses, test DB

**Shared Foundation Changes Required** (must be added to Unit 0 before this agent starts):
- New SQLAlchemy models in `src/shared/models/mcpgateway.py`
- New Pydantic schemas in `src/shared/schemas/mcpgateway.py`
- New service interfaces in `src/shared/interfaces/mcpgateway.py`
- Add `"src.mcprouter.router"` to `UNIT_MODULES` in `src/app.py`
- Alembic migration for MCP tables

---

## Unit MCP-2: MCP Gateway Admin UI

**Scope**: `frontend/src/pages/mcp-gateway/`, `frontend/src/components/mcp/`, `tests/frontend/mcp/`
**Agent**: Yes (after MCP-1 API is defined — can start once schemas are in shared foundation)
**Stories**: US-MCP-7.1, US-MCP-7.2, US-MCP-7.3, US-MCP-7.4

**Delivers**:
- New top-level "MCP Gateway" nav item in Admin UI sidebar
- Pages:
  - `CatalogManagement.tsx` — Platform Admin: list/add/edit/verify/deprecate catalog entries
  - `Marketplace.tsx` — Org Admin: browse catalog, deploy instances, see "already deployed" indicators
  - `MyDeployments.tsx` — Org Admin: list org's deployments with status, tools, health; disable/enable/remove actions
  - `DeploymentDetail.tsx` — Org Admin: server details, tool list, health history, credential status
  - `ToolBrowser.tsx` — Developer: searchable catalog of all accessible tools with descriptions and parameter schemas
  - `ToolGroups.tsx` — Org Admin: create/edit groups, inclusion rules, team assignments, service_account_safe toggle
  - `HealthDashboard.tsx` — Platform/Org Admin: per-deployment health status, last sync, alerts
  - `ConnectionInfo.tsx` — Developer: per-server URLs, auth token, copy-paste configs for Claude Desktop/Cursor/Strands
- Shared components:
  - `MCPServerCard.tsx` — reusable card for catalog/deployment display
  - `ToolCard.tsx` — tool display with name, description, parameters
  - `CredentialStatusBadge.tsx` — shows "ready", "setup_required", "not_needed"
  - `DeploymentStatusBadge.tsx` — DEPLOYED (green), FAILED (red), DEPLOYING (yellow), DISABLED (grey)
- API client functions for all `/admin/mcp/*` and `/mcp/*` endpoints
- Unit tests (Vitest + React Testing Library) with MSW mocks

**Interfaces Implemented**: None (consumes MCP Gateway API via HTTP)
**Depends On**: MCP-1 API contract (Pydantic schemas from shared foundation), existing Admin UI shell (Unit 7 — layout, nav, auth context)
**Independently Testable**: Yes — MSW mocks for all API calls

---

## Unit MCP-3: MCP Server Config Repo & Deployment Pipelines

**Scope**: `mcp-servers/` repo (separate GitHub repo), `.github/workflows/` within that repo
**Agent**: Yes (parallel with MCP-1 and MCP-2)
**Stories**: US-MCP-1.1, US-MCP-2.2 (deployment execution), US-MCP-5.1, US-MCP-5.2

**Delivers**:
- GitHub repository structure:
  ```
  mcp-servers/
  ├── catalogue/
  │   ├── github-mcp/catalogue.json
  │   ├── postgres-mcp/catalogue.json
  │   ├── playwright-mcp/catalogue.json
  │   ├── terraform-mcp/catalogue.json
  │   ├── aws-mcp/catalogue.json
  │   ├── context7-mcp/catalogue.json
  │   └── filesystem-mcp/catalogue.json
  ├── deployments/          (org-specific, created by Admin UI via GitHub API)
  │   └── _template/
  │       └── example.json
  └── .github/workflows/
      ├── sync-catalogue.yml      — on push to catalogue/: validate + sync to DB
      ├── deploy-server.yml       — on push to deployments/: kubectl apply + status callback
      ├── remove-server.yml       — on delete in deployments/: kubectl delete + status callback
      ├── build-custom-image.yml  — on push to catalogue/*/Dockerfile: build + push to ECR
      └── validate-pr.yml        — on PR: schema validation, dry-run, image check
  ```
- Initial 7 catalog entries (Phase 1 servers) as catalogue.json files
- GitHub Actions workflows running on EKS self-hosted runners:
  - `sync-catalogue.yml`: reads catalogue.json, calls BedrockGateway API to upsert mcp_catalogue rows
  - `deploy-server.yml`: reads deployment JSON + catalogue JSON, generates K8s manifests, kubectl apply, waits for Ready, calls tools/list, calls BedrockGateway API to update status
  - `remove-server.yml`: reads deployment name, kubectl delete, calls BedrockGateway API to update status=REMOVED
  - `build-custom-image.yml`: docker build + push to ECR for catalogue entries with Dockerfiles
  - `validate-pr.yml`: JSON schema validation, image existence check, kubectl apply --dry-run
- K8s manifest templates (Deployment + ClusterIP Service) used by deploy workflow
- Webhook/callback mechanism: workflows call BedrockGateway API with deployment status updates

**Interfaces Implemented**: None
**Depends On**: EKS cluster (Unit 8), ECR (Unit 8), BedrockGateway API (MCP-1 — for status callbacks), EKS self-hosted runners (Unit 9)
**Independently Testable**: Yes — workflow syntax validation, JSON schema validation, kubectl dry-run

---

## Unit MCP-4: MCP Gateway Infrastructure

**Scope**: `infra/modules/mcp/`, `infra/environments/*/mcp.tf`
**Agent**: Yes (parallel with MCP-1, MCP-2, MCP-3)
**Stories**: (cross-cutting — supports MCP deployment)

**Delivers**:
- Terraform module `infra/modules/mcp/`:
  - K8s namespace `mcp-servers`
  - ECR repositories for MCP server images (one per catalog entry)
  - ALB target group for MCP Router pods with path-based routing rule (`/mcp/*`)
  - ALB listener rule priority configuration
  - IAM role for MCP Router pod (K8s service account → IAM via IRSA)
  - IAM policy for MCP Router: access to GitHub API (for deployment triggers), AgentCore Identity API
  - AgentCore Identity workload identity for the MCP Router
  - AgentCore Identity credential providers for Phase 1 servers (GitHub OAuth, Atlassian OAuth, etc.)
  - CloudWatch alarms for MCP server pod health
  - Alembic migration execution for MCP tables (as part of app deployment)
- Environment-specific tfvars for dev/test/prod
- GitHub repo creation for mcp-servers config repo (or reference if manually created)

**Interfaces Implemented**: None (Terraform)
**Depends On**: Unit 8 (base infrastructure — EKS, VPC, ALB), Unit 12 (environment provisioning)
**Independently Testable**: Yes — `terraform validate`, `terraform plan`

---

## Dependency & Parallelism

```
Shared Foundation (Unit 0) — must be built first with MCP models/schemas/interfaces added
    |
    ├── MCP-1 (Backend)      ─── parallel ───┐
    ├── MCP-2 (Admin UI)     ─── parallel ───┤  Wave: MCP
    ├── MCP-3 (Config Repo)  ─── parallel ───┤  (runs alongside existing Wave 1)
    └── MCP-4 (Infrastructure) ── parallel ──┘
```

All 4 MCP units can run in parallel with each other AND with the existing Wave 1 units (Units 1-10), since they all depend only on the shared foundation.

**Merge order**: MCP-4 (infra) first → MCP-1 (backend) → MCP-3 (config repo, needs API for callbacks) → MCP-2 (UI, needs API running)

---

## Story-to-Unit Mapping

| Story | Unit |
|-------|------|
| US-MCP-1.1 (Register catalog) | MCP-1, MCP-3 |
| US-MCP-1.2 (Verify catalog) | MCP-1 |
| US-MCP-1.3 (Deprecate catalog) | MCP-1 |
| US-MCP-2.1 (Browse marketplace) | MCP-2 |
| US-MCP-2.2 (Deploy for org) | MCP-1, MCP-3 |
| US-MCP-2.3 (Manage deployments) | MCP-1, MCP-2 |
| US-MCP-2.4 (Rotate credentials) | MCP-1 (via AgentCore Identity) |
| US-MCP-3.1 (Create tool groups) | MCP-1 |
| US-MCP-3.2 (Assign to teams) | MCP-1, MCP-2 |
| US-MCP-4.1 (Discover tools) | MCP-1 |
| US-MCP-4.2 (Connection info) | MCP-1, MCP-2 |
| US-MCP-4.3 (Invoke tools) | MCP-1 |
| US-MCP-5.1 (Health monitoring) | MCP-1, MCP-2 |
| US-MCP-5.2 (Tool refresh) | MCP-1 |
| US-MCP-6.1 (Rate limits) | MCP-1 |
| US-MCP-7.1 (Catalog UI) | MCP-2 |
| US-MCP-7.2 (Marketplace UI) | MCP-2 |
| US-MCP-7.3 (Deployments UI) | MCP-2 |
| US-MCP-7.4 (Tool browser UI) | MCP-2 |
| US-MCP-8.1 (Usage logging) | MCP-1 |
| US-MCP-9.1 (Server unavailable) | MCP-1 |
| US-MCP-9.2 (Not authorized) | MCP-1 |
| US-MCP-9.3 (Rate limited) | MCP-1 |

All stories mapped. No orphans.
