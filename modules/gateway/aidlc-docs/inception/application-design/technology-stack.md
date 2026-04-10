# Technology Stack — Pinned Versions

All dependency versions are pinned in the shared foundation (Unit 0). Agents MUST use these versions and MUST NOT add new dependencies without raising a clarification request.

Versions verified from PyPI, npm, and HashiCorp registry as of February 2026.

## Backend (Python)

**Runtime**: Python 3.12+

**File**: `pyproject.toml`

| Library | Version | Purpose | Source |
|---------|---------|---------|--------|
| fastapi | 0.115.6 | Web framework | pypi.org (0.116.x available but 0.115.x is latest stable non-breaking) |
| uvicorn | 0.34.0 | ASGI server | pypi.org |
| sqlalchemy[asyncio] | 2.0.46 | ORM + async support | pypi.org |
| asyncpg | 0.30.0 | PostgreSQL async driver | pypi.org |
| alembic | 1.14.1 | Database migrations | pypi.org |
| pydantic | 2.12.5 | Data validation / schemas | pypi.org |
| pydantic-settings | 2.12.0 | Settings from env vars | pypi.org |
| boto3 | 1.36.0 | AWS SDK (STS, Bedrock) | pypi.org (pinned to recent stable, boto3 releases daily) |
| httpx | 0.28.1 | Async HTTP client | pypi.org |
| redis | 5.2.1 | Redis client (optional rate limiting) | pypi.org |
| prometheus-client | 0.22.0 | Prometheus metrics | pypi.org |
| python-jose | 3.3.0 | Token utilities | pypi.org |

**AWS Services Used**:

| Service | Purpose |
|---------|---------|
| Cognito User Pool | User identity, IdP federation (Okta/Azure AD/Auth0), JWT tokens |
| Cognito Identity Pool | Issues temporary AWS credentials for SigV4 auth |
| STS | Validates SigV4 credentials, cross-account role assumption for Bedrock pool |
| Bedrock | LLM inference (via cross-account pool) |
| S3 + CloudFront | Frontend SPA hosting |

**Dev dependencies**:

| Library | Version | Purpose | Source |
|---------|---------|---------|--------|
| pytest | 8.3.4 | Test framework | pypi.org |
| pytest-asyncio | 0.25.0 | Async test support | pypi.org |
| pytest-cov | 6.0.0 | Coverage reporting | pypi.org |
| fakeredis | 2.26.2 | Redis mock for tests | pypi.org |
| moto | 5.0.27 | AWS service mocks (STS, Bedrock) | pypi.org |
| ruff | 0.9.6 | Linter + formatter | pypi.org (0.14.x available but 0.9.x is latest stable series) |

## Frontend (React)

**Runtime**: Node.js 22 LTS

**File**: `frontend/package.json`

| Library | Version | Purpose | Source |
|---------|---------|---------|--------|
| react | 19.1.0 | UI framework | npmjs.com (React 19 stable) |
| react-dom | 19.1.0 | React DOM renderer | npmjs.com |
| react-router-dom | 7.1.0 | Client-side routing | npmjs.com (v7 is latest stable) |
| @tanstack/react-query | 5.62.0 | Server state management | npmjs.com |
| tailwindcss | 4.0.0 | Utility-first CSS | npmjs.com (v4 released) |
| axios | 1.7.9 | HTTP client | npmjs.com |
| recharts | 2.15.0 | Charts for dashboards | npmjs.com |

**Dev dependencies**:

| Library | Version | Purpose | Source |
|---------|---------|---------|--------|
| vite | 6.1.0 | Build tool | npmjs.com |
| vitest | 3.0.0 | Test framework | npmjs.com |
| @testing-library/react | 16.1.0 | React testing utilities | npmjs.com |
| msw | 2.7.0 | Mock Service Worker (API mocking) | npmjs.com |
| typescript | 5.7.3 | Type checking | npmjs.com |
| eslint | 9.18.0 | Linter | npmjs.com |

## Infrastructure (Terraform)

**File**: `infra/versions.tf`

| Tool/Provider | Version | Purpose | Source |
|---------------|---------|---------|--------|
| terraform | >= 1.10.0 | IaC tool | hashicorp.com |
| aws provider | ~> 6.0 | AWS resources | registry.terraform.io (v6.0 GA) |
| kubernetes provider | ~> 2.36.0 | EKS resources | registry.terraform.io |
| helm provider | ~> 2.17.0 | Helm chart deployments | registry.terraform.io |

## CI/CD (GitHub Actions)

| Action | Version | Purpose |
|--------|---------|---------|
| actions/checkout | v4 | Checkout code |
| actions/setup-python | v5 | Python setup |
| actions/setup-node | v4 | Node.js setup |
| hashicorp/setup-terraform | v3 | Terraform setup |
| aws-actions/configure-aws-credentials | v4 | AWS auth |
| aws-actions/amazon-ecr-login | v2 | ECR login |

## Agent Rules

1. **DO NOT** add new dependencies without raising a ❓ Clarification Request
2. **DO NOT** change pinned versions
3. **DO** use `pip install -e ".[dev]"` for backend development
4. **DO** use `npm ci` (not `npm install`) for frontend development
5. If a needed library is missing, post a clarification comment explaining why it's needed
