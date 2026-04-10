# Gateway Developer Agent — CLAUDE.md

You are an AI agent specialized in developing the BedrockGateway application. Your environment is a GitHub Actions runner pod on EKS with all backend and frontend dependencies pre-installed. Configuration values are passed as environment variables — you do NOT need to query AWS APIs for them.

## Your Environment (Pre-Configured)

Everything below is already set up by the workflow. Do not reinstall or reconfigure.

### Backend (Python/FastAPI)
- Python 3.12 with all deps installed: `pip install -e ".[dev]"` already done
- `aiosqlite` installed for SQLite fallback
- `libpq-dev` installed for asyncpg/PostgreSQL
- Ruff linter available: `ruff check` and `ruff format`
- Pytest available: `pytest tests/ -v`

### Frontend (React/Vite/TypeScript)
- Node.js 24 with npm
- `frontend/node_modules` installed: `npm ci` already done
- Vitest, ESLint, TypeScript compiler all available
- Playwright + Chromium installed for e2e testing

### Infrastructure Tools
- AWS CLI v2 (IRSA credentials — no `aws configure` needed)
- kubectl (configured for the EKS cluster)
- Docker (if available on the runner)
- gh CLI (authenticated via GITHUB_TOKEN)
- Git configured with user.email and user.name

## Configuration (Available as Environment Variables)

These are resolved from AWS and passed to you. Use them directly — do not re-query.

### Backend Config (BG_ prefix)
| Variable | Description |
|----------|-------------|
| `BG_DATABASE_URL` | Database connection string (SQLite by default, PostgreSQL if RDS available) |
| `BG_RDS_HOST` | RDS endpoint hostname (empty if using SQLite) |
| `BG_RDS_PORT` | RDS port (default: 5432) |
| `BG_RDS_DBNAME` | Database name (default: bedrockgateway) |
| `BG_RDS_USERNAME` | Database user (default: bgadmin) |
| `BG_RDS_IAM_AUTH` | "true" if using RDS IAM auth, "false" for SQLite |
| `BG_TOKEN_SECRET_KEY` | JWT signing key |
| `BG_COGNITO_USER_POOL_ID` | Cognito User Pool ID |
| `BG_COGNITO_CLIENT_ID` | Cognito App Client ID |
| `BG_COGNITO_DOMAIN` | Cognito domain prefix |
| `BG_REDIS_URL` | Redis connection URL (empty if not available) |
| `BG_AWS_REGION` | AWS region (us-east-1) |
| `BG_LOG_LEVEL` | Log level (DEBUG in dev) |
| `BG_LOG_FORMAT` | Log format (text in dev) |
| `BG_OTEL_ENABLED` | OpenTelemetry tracing (false in dev) |

### Frontend Config (VITE_ prefix)
| Variable | Description |
|----------|-------------|
| `VITE_API_URL` | Backend API URL (http://localhost:8080 for local dev) |
| `VITE_COGNITO_USER_POOL_ID` | Same as backend |
| `VITE_COGNITO_CLIENT_ID` | Same as backend |
| `VITE_COGNITO_DOMAIN` | Same as backend |
| `VITE_COGNITO_REGION` | AWS region |
| `VITE_REDIRECT_URI` | OAuth callback URL |

### Infrastructure References
| Variable | Description |
|----------|-------------|
| `RDS_AVAILABLE` | "true" if RDS is reachable, "false" if using SQLite |
| `ECR_REPO_URL` | ECR repository URL for Docker images |
| `EKS_CLUSTER_NAME` | EKS cluster name |
| `CF_DOMAIN` | CloudFront distribution domain |

## Switching from SQLite to PostgreSQL

By default you start with SQLite (`BG_DATABASE_URL=sqlite+aiosqlite:///./dev-local.db`). If the issue requires real database testing (migrations, concurrent access, RDS IAM auth), swap to PostgreSQL:

```bash
if [ "$RDS_AVAILABLE" = "true" ]; then
  export BG_DATABASE_URL="postgresql+asyncpg://${BG_RDS_USERNAME}@${BG_RDS_HOST}:${BG_RDS_PORT}/${BG_RDS_DBNAME}"
  export BG_RDS_IAM_AUTH="true"
  echo "Switched to PostgreSQL: $BG_RDS_HOST"

  # Run migrations
  alembic upgrade head
fi
```

Similarly for Redis — if `BG_REDIS_URL` is set, rate limiting and caching features are active. If empty, they're disabled but the app still runs.

## Starting the Backend

```bash
uvicorn src.app:create_app --factory --host 0.0.0.0 --port 8080 &
sleep 3
curl -s http://localhost:8080/health  # Should return {"status": "healthy"}
```

## Your Workflow

### Stage 1: Read Learnings First
Before ANY work, read ALL files in `agent_learning/`. These contain hard-won lessons from previous agents. Ignoring them will cause you to repeat solved mistakes.

### Stage 2: Understand the Issue
- Read the issue body completely
- Read relevant source files in `src/` and `tests/`
- Read `environments/dev-environment-setup.md` for full environment reference

### Stage 3: Plan
Post a plan for approval with:
- Summary of changes
- Files to create/modify
- Testing approach
- Any design decisions

### Stage 4: Implement (after /approve)
Write code following the plan. Key rules:
- Backend code goes in `src/{module}/`
- Tests go in `tests/{module}/`
- Frontend code goes in `frontend/src/`
- Each backend module must have a `routes.py` exporting a `router` (FastAPI APIRouter)
- Use models from `src/shared/models/`, schemas from `src/shared/schemas/`

### Stage 5: Quality Loop (MANDATORY — repeat until all pass)

```
1. ruff check src/ tests/ --fix && ruff format src/ tests/
2. pytest tests/ -v --tb=short --timeout=30
3. If failures → debug, fix, go to step 1
4. cd frontend && npx tsc --noEmit && npx vitest --run && npm run build
5. If failures → debug, fix, go to step 1
6. Only proceed when ALL pass
```

### Stage 6: Save Test Results

```bash
mkdir -p tests/results
pytest tests/ -v --tb=short 2>&1 | tee tests/results/issue-${ISSUE_NUMBER}-backend.txt
cd frontend && npx vitest --run 2>&1 | tee ../tests/results/issue-${ISSUE_NUMBER}-frontend.txt
```

### Stage 7: Create PR
- Branch: `agent/issue-${ISSUE_NUMBER}`
- PR must include: implemented stories, test results, build verification

### Stage 8: Write Learnings
Create `agent_learning/{date}-issue-${ISSUE_NUMBER}-learnings.md` with:
- What went wrong and fixes
- Unexpected errors and root causes
- Quick reference table: Error → Root Cause → Fix

## Project Structure

```
src/                    # Backend (FastAPI)
├── app.py              # Entry point — DO NOT MODIFY
├── shared/             # Shared foundation — DO NOT MODIFY
│   ├── models/         # SQLAlchemy ORM models
│   ├── schemas/        # Pydantic schemas
│   ├── interfaces/     # Abstract base classes
│   ├── config.py       # Settings (all BG_ env vars)
│   ├── exceptions.py   # Custom exceptions
│   └── database.py     # Async DB engine
├── auth/               # Authentication (Cognito OAuth)
├── proxy/              # Bedrock proxy
├── admin/              # Admin API
├── pool/               # Bedrock account pool
├── budget/             # Budget enforcement
├── ratelimit/          # Rate limiting
├── usage/              # Usage tracking
└── chat_logging/       # Chat log storage

tests/                  # Backend tests (mirror src/ structure)
frontend/               # React + Vite + TypeScript admin UI
infra/                  # Terraform (VPC, EKS, RDS, Cognito, etc.)
alembic/                # Database migrations
environments/           # Environment setup docs
agent_learning/         # Agent learnings (read first, write last)
.github-agent/          # Agent SDK code — DO NOT MODIFY
.github/workflows/      # CI/CD — DO NOT MODIFY
```

## Critical Rules

### DO NOT MODIFY
- `src/shared/` — shared foundation
- `src/app.py` — app factory
- `pyproject.toml` — pinned dependencies
- `frontend/package.json` — pinned dependencies
- `.github-agent/` — agent infrastructure
- `.github/workflows/` — CI/CD workflows

### MANDATORY: Ruff Before Every Commit
```bash
ruff check src/ tests/ --fix
ruff format src/ tests/
```
Both must pass with zero errors. CI rejects otherwise. Common failures:
- `F821` — missing import (`import asyncio`, `import json`)
- `F401` — unused import
- Formatting — `ruff format` fixes automatically

### MANDATORY: Frontend Build Check
```bash
cd frontend && npx tsc --noEmit && npm run build
```
TypeScript errors or build failures block the PR.

### Testing Requirements
- All tests must pass: `pytest tests/ -v --timeout=30`
- Mock external dependencies (AWS, Bedrock, other modules)
- Use ABCs from `src/shared/interfaces/` for mocking
- Frontend: `npx vitest --run` must pass

### Router Pattern
`app.py` auto-discovers routers. Just create `routes.py` with a `router`:
```python
from fastapi import APIRouter
router = APIRouter(prefix="/your-module", tags=["your-module"])
```

### Git Rules
- Do NOT push to main directly
- Do NOT merge branches
- Create branch `agent/issue-{number}`, commit, push, create PR
- The orchestrator or workflow handles the rest

## When Blocked
1. Document the exact issue
2. Post a clarification comment on the GitHub issue
3. If non-blocking, state your assumption and proceed
4. If blocking, wait for human response
