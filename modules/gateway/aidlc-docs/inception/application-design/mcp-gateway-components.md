# MCP Gateway — Application Components (Self-Contained, Separate Container)

MCP Gateway runs as its own container on EKS, completely independent from BedrockGateway. Same monorepo, separate service.

---

## Architecture Overview

```
ALB (existing, shared)
  |
  ├── /v1/*, /bedrock/*, /admin/*, /auth/*
  |     → BedrockGateway pods (existing container, existing ECR image)
  |
  └── /mcp/*, /admin/mcp/*
        → MCP Router pods (NEW container, separate ECR image)

Both services share:
  - PostgreSQL (RDS) — different tables, same DB
  - Cognito user pool — same JWT validation
  - EKS cluster — different K8s Deployments
  - ALB — different target groups
```

---

## MCP Router Service (`src/mcprouter/`)

**Purpose**: Self-contained FastAPI service for MCP Gateway. Runs as its own container.

**Owns everything it needs:**
- Its own models (MCPCatalogue, MCPDeployment, etc.)
- Its own schemas (Pydantic request/response)
- Its own service interfaces (ABCs)
- Its own routes (FastAPI router)
- Its own Dockerfile
- Its own Alembic migration
- Its own health endpoint

**Imports from BedrockGateway (read-only, never modifies):**
- `src/shared/database.py` — DB session factory
- `src/shared/models/base.py` — SQLAlchemy Base class
- `src/shared/models/organization.py` — Organization model (FK reference)
- `src/shared/config.py` — App settings
- `src/shared/exceptions.py` — Error classes

```
src/mcprouter/
  __init__.py
  app.py                   # Own FastAPI app (not shared with BedrockGateway)
  router.py                # All /mcp/* and /admin/mcp/* routes
  models.py                # MCPCatalogue, MCPDeployment, MCPToolGroup, etc.
  schemas.py               # All Pydantic schemas
  interfaces.py            # Service ABCs
  middleware.py             # Auth middleware (reuses Cognito JWT validation logic)
  catalogue_service.py     # CatalogueService
  deployment_service.py    # DeploymentService (triggers GitHub Actions)
  proxy_service.py         # MCPProxyService (forwards to K8s/remote)
  tool_group_service.py    # ToolGroupService
  discovery_service.py     # DiscoveryService
  health_service.py        # HealthService + background scheduler
  Dockerfile               # Own multi-stage build
  requirements.txt         # Own dependencies (subset of main pyproject.toml)
tests/mcprouter/
  __init__.py
  test_catalogue_service.py
  test_deployment_service.py
  test_proxy_service.py
  test_tool_group_service.py
  test_discovery_service.py
  test_health_service.py
  conftest.py
```

---

## Deployment Topology

```
EKS Cluster
│
├── namespace: bedrock-gateway
│   ├── Deployment: bedrock-gateway (existing)
│   │   └── Pods: BedrockGateway container
│   │       Image: <ecr>/bedrock-gateway:latest
│   │       Ports: 8000
│   │
│   └── Deployment: mcp-router (NEW)
│       └── Pods: MCP Router container
│           Image: <ecr>/mcp-router:latest
│           Ports: 8001
│
├── namespace: mcp-servers
│   ├── Deployment: github-mcp
│   ├── Deployment: postgres-mcp-acme
│   ├── Deployment: terraform-mcp
│   └── ...
│
└── ALB (shared)
    ├── Target Group: bedrock-gateway → port 8000
    │   Rules: /v1/*, /bedrock/*, /admin/*, /auth/*
    │
    └── Target Group: mcp-router → port 8001
        Rules: /mcp/*, /admin/mcp/*
```

---

## What MCP Router Does NOT Share with BedrockGateway

| Concern | BedrockGateway | MCP Router |
|---|---|---|
| FastAPI app | `src/app.py` | `src/mcprouter/app.py` (own app) |
| Dockerfile | `Dockerfile` | `src/mcprouter/Dockerfile` |
| ECR image | `<ecr>/bedrock-gateway` | `<ecr>/mcp-router` |
| K8s Deployment | `bedrock-gateway` | `mcp-router` |
| ALB target group | bedrock-gateway-tg | mcp-router-tg |
| CI/CD pipeline | `backend-deploy.yml` | `mcp-router-deploy.yml` (own pipeline) |
| DB tables | organizations, users, tokens, budgets, usage_logs, etc. | mcp_catalogue, mcp_deployment, mcp_tool_group, etc. |
| Health endpoint | `GET /health` | `GET /mcp/health` |

## What MCP Router DOES Share (read-only imports)

| Shared Resource | How Used |
|---|---|
| PostgreSQL (RDS) | Same DB instance, different tables |
| Cognito user pool | Same JWT validation logic (copied or imported) |
| `src/shared/database.py` | DB connection factory |
| `src/shared/models/base.py` | SQLAlchemy Base class |
| `src/shared/config.py` | Settings (DB URL, Cognito config) |
| `src/shared/exceptions.py` | Error classes |

---

## Admin UI Integration

The Admin UI (React SPA) is a single app that talks to BOTH backends:
- `/admin/*` routes → BedrockGateway API
- `/admin/mcp/*` routes → MCP Router API

From the browser's perspective, both are behind the same ALB domain. The ALB routes to the correct backend based on path.

---

## Extended Infrastructure (`infra/modules/mcp/`)

**New Terraform resources:**
- K8s namespace `mcp-servers`
- K8s Deployment + Service for `mcp-router`
- ECR repository for `mcp-router` image
- ECR repositories for MCP server images
- ALB target group for mcp-router with path rules (`/mcp/*`, `/admin/mcp/*`)
- IAM role for mcp-router pod (IRSA)
- AgentCore Identity credential providers
- CloudWatch alarms

---

## MCP Server Config Repo (`mcp-servers/` — folder in this monorepo)

**Structure:**
```
mcp-servers/
  catalogue/
    github-mcp/catalogue.json
    postgres-mcp/catalogue.json
    ...
  deployments/
    acme-corp/
      postgres-mcp.json
    globex-inc/
      github-mcp.json
```

GitHub Actions workflows in `.github/workflows/` trigger on path filters (`paths: ['mcp-servers/**']`). Same repo, same runners, same permissions — no cross-repo complexity.
