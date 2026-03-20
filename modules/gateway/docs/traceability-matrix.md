# Requirements Traceability Matrix

This document maps requirements to user stories, implementation, tests, and current status.

## Legend

| Status | Meaning |
|--------|---------|
| :white_check_mark: | Fully implemented and tested |
| :large_orange_diamond: | Partially implemented |
| :x: | Not started |

---

## FR1: Authentication

| Req ID | Requirement | User Story | Implementation | Tests | Status |
|--------|------------|------------|----------------|-------|--------|
| FR1.1 | Credential exchange for gateway token | US-1.4, US-1.6 | `src/auth/routes.py` (exchange endpoint) | `tests/auth/test_routes.py` | :white_check_mark: |
| FR1.2 | STS GetCallerIdentity validation | US-1.4 | `src/auth/auth_service.py` | `tests/auth/test_auth_service.py` | :white_check_mark: |
| FR1.3 | User identity extraction from session | US-1.4 | `src/auth/cognito_jwt.py` | `tests/auth/test_cognito_jwt.py` | :white_check_mark: |
| FR1.4 | Tenant resolution from STS response | US-1.4 | `src/auth/auth_service.py` (resolve_tenant) | `tests/auth/test_auth_service.py` | :white_check_mark: |
| FR1.5 | Configurable token expiry | US-1.4, US-1.6 | `src/auth/token_manager.py` | `tests/auth/test_token_manager.py` | :white_check_mark: |
| FR1.6 | Tokens stored hashed | US-1.4 | `src/shared/models/token.py` | `tests/auth/test_token_manager.py` | :white_check_mark: |
| FR1.7 | Single auth mechanism | US-1.4, US-6.1 | `src/auth/middleware.py` | `tests/auth/test_middleware.py` | :white_check_mark: |
| FR1.8 | Admin access via role mapping | US-1.4, US-7.1 | `src/auth/cognito_jwt.py` (admin claim) | `tests/auth/test_cognito_jwt.py` | :white_check_mark: |
| FR1.9 | Admin UI Cognito authentication | US-7.1 | `src/auth/cognito_jwt.py` | `tests/auth/test_cognito_jwt.py` | :white_check_mark: |

---

## FR2: Multi-Tenant Organization Management

| Req ID | Requirement | User Story | Implementation | Tests | Status |
|--------|------------|------------|----------------|-------|--------|
| FR2.1 | Onboard new organizations | US-1.3 | `src/admin/routes.py` (POST /organizations) | `tests/admin/test_routes.py` | :white_check_mark: |
| FR2.2 | Cognito integration for user sync | US-1.3, US-1.4 | `src/admin/service.py` | `tests/admin/test_service.py` | :white_check_mark: |
| FR2.3 | Hierarchy read from Cognito | US-1.4 | `src/auth/cognito_jwt.py` | `tests/auth/test_cognito_jwt.py` | :white_check_mark: |
| FR2.4 | Tenant resolution via session tags | US-1.4 | `src/auth/auth_service.py` | `tests/auth/test_auth_service.py` | :white_check_mark: |
| FR2.5 | Tenant data isolation (org_id) | US-1.3 | `src/shared/models/base.py` (TenantMixin) | All model tests | :white_check_mark: |
| FR2.6 | Admin roles via Cognito claims | US-7.1 | `src/auth/cognito_jwt.py` | `tests/auth/test_cognito_jwt.py` | :white_check_mark: |

---

## FR3: Budget Management (Cascading)

| Req ID | Requirement | User Story | Implementation | Tests | Status |
|--------|------------|------------|----------------|-------|--------|
| FR3.1 | Budgets at all hierarchy levels | US-2.1 | `src/budget/routes.py`, `src/budget/service.py` | `tests/budget/test_service.py` | :white_check_mark: |
| FR3.2 | Cascading budget enforcement | US-2.1, US-2.3 | `src/budget/service.py` (check_budget_hierarchy) | `tests/budget/test_service.py` | :white_check_mark: |
| FR3.3 | Budget periods (daily/weekly/monthly) | US-2.1 | `src/shared/schemas/budget.py` (PeriodType) | `tests/budget/test_service.py` | :white_check_mark: |
| FR3.4 | Enforcement modes (soft/hard) | US-2.3 | `src/budget/service.py`, `src/budget/middleware.py` | `tests/budget/test_middleware.py` | :white_check_mark: |
| FR3.5 | Token and cost tracking | US-2.3 | `src/budget/service.py` (record_usage) | `tests/budget/test_service.py` | :white_check_mark: |
| FR3.6 | Cost attribution | US-2.1 | `src/usage/service.py` | `tests/usage/test_service.py` | :white_check_mark: |
| FR3.7 | Admin UI budget views | US-7.3 | `frontend/src/pages/BudgetDashboard.tsx` | `frontend/tests/` | :white_check_mark: |

---

## FR4: Rate Limiting

| Req ID | Requirement | User Story | Implementation | Tests | Status |
|--------|------------|------------|----------------|-------|--------|
| FR4.1 | Rate limits at all levels | US-3.1 | `src/ratelimit/routes.py`, `src/ratelimit/service.py` | `tests/ratelimit/test_service.py` | :white_check_mark: |
| FR4.2 | RPM, TPM, concurrent limits | US-3.1 | `src/ratelimit/models.py` (LimitType) | `tests/ratelimit/test_service.py` | :white_check_mark: |
| FR4.3 | Token bucket algorithm | US-3.2 | `src/ratelimit/token_bucket.py` | `tests/ratelimit/test_token_bucket.py` | :white_check_mark: |
| FR4.4 | 429 with Retry-After header | US-3.2 | `src/ratelimit/middleware.py` | `tests/ratelimit/test_middleware.py` | :white_check_mark: |
| FR4.5 | In-memory + Redis backends | US-3.1 | `src/ratelimit/backends/` | `tests/ratelimit/test_backends.py` | :white_check_mark: |

---

## FR5: Proxy API (Multi-Format)

| Req ID | Requirement | User Story | Implementation | Tests | Status |
|--------|------------|------------|----------------|-------|--------|
| FR5.1 | OpenAI chat completions format | US-4.1 | `src/proxy/routes.py` (/v1/chat/completions) | `tests/proxy/test_routes.py` | :white_check_mark: |
| FR5.2 | Streaming responses (SSE) | US-4.1, US-4.2 | `src/proxy/stream_handler.py` | `tests/proxy/test_stream_handler.py` | :white_check_mark: |
| FR5.3 | OpenAI to Bedrock translation | US-4.1 | `src/proxy/format_translator.py` | `tests/proxy/test_format_translator.py` | :white_check_mark: |
| FR5.4 | Model aliases per organization | US-4.1 | `src/proxy/model_resolver.py` | `tests/proxy/test_model_resolver.py` | :white_check_mark: |
| FR5.5 | Model access control | US-9.6 | `src/proxy/service.py` | `tests/proxy/test_service.py` | :white_check_mark: |
| FR5.6 | GET /v1/models | US-4.1 | `src/proxy/routes.py` | `tests/proxy/test_routes.py` | :white_check_mark: |
| FR5.7 | Anthropic Messages format | US-4.2 | `src/proxy/routes.py` (/v1/messages) | `tests/proxy/test_routes.py` | :white_check_mark: |
| FR5.8 | Bedrock pass-through | US-4.3 | `src/proxy/routes.py` (/bedrock/*) | `tests/proxy/test_routes.py` | :white_check_mark: |

---

## FR6: Cross-Account Bedrock Pool

| Req ID | Requirement | User Story | Implementation | Tests | Status |
|--------|------------|------------|----------------|-------|--------|
| FR6.1 | Pool of Bedrock accounts | US-1.2, US-5.1 | `src/pool/service.py` | `tests/pool/test_service.py` | :white_check_mark: |
| FR6.2 | Static pool configuration | US-1.2 | `src/pool/config.py` | `tests/pool/test_config.py` | :white_check_mark: |
| FR6.3 | Round-robin routing | US-5.1 | `src/pool/service.py` (get_next_account) | `tests/pool/test_service.py` | :white_check_mark: |
| FR6.4 | Cross-account STS AssumeRole | US-5.1 | `src/pool/service.py` | `tests/pool/test_service.py` | :white_check_mark: |
| FR6.5 | Failover on throttling | US-5.1 | `src/pool/service.py` | `tests/pool/test_service.py` | :white_check_mark: |
| FR6.6 | Pool health tracking | US-1.2, US-5.1 | `src/pool/service.py` (health_check) | `tests/pool/test_service.py` | :white_check_mark: |

---

## FR7: Claude Code Integration

| Req ID | Requirement | User Story | Implementation | Tests | Status |
|--------|------------|------------|----------------|-------|--------|
| FR7.1 | Claude Code via Bedrock pass-through | US-4.3 | `src/proxy/routes.py` (/bedrock/*) | `tests/proxy/test_routes.py` | :white_check_mark: |
| FR7.2 | ANTHROPIC_BEDROCK_BASE_URL config | US-6.1 | `cli/bg-cognito-auth.sh` | CLI integration tests | :white_check_mark: |
| FR7.3 | Anthropic Messages format support | US-4.2 | `src/proxy/routes.py` (/v1/messages) | `tests/proxy/test_routes.py` | :white_check_mark: |
| FR7.4 | apiKeyHelper integration | US-6.1 | `cli/bg-cognito-auth.sh` (token command) | CLI integration tests | :white_check_mark: |
| FR7.5 | CLI helper script | US-6.1 | `cli/bg-cognito-auth.sh` | Manual testing | :white_check_mark: |
| FR7.6 | Helper exchanges AWS creds for token | US-6.1 | `cli/bg-cognito-auth.sh` (login command) | Manual testing | :white_check_mark: |
| FR7.7 | Token auto-refresh | US-6.1 | `cli/bg-cognito-auth.sh` (refresh) | Manual testing | :white_check_mark: |
| FR7.8 | Model alias support | US-4.1 | `src/proxy/model_resolver.py` | `tests/proxy/test_model_resolver.py` | :white_check_mark: |
| FR7.9 | Preserve anthropic_beta/version | US-4.2, US-4.3 | `src/proxy/service.py` | `tests/proxy/test_service.py` | :white_check_mark: |
| FR7.10 | Forward anthropic headers | US-4.2 | `src/proxy/routes.py` | `tests/proxy/test_routes.py` | :white_check_mark: |
| FR7.11 | Seamless SSO experience | US-6.1 | `cli/bg-cognito-auth.sh` | Manual testing | :white_check_mark: |

---

## FR8: Admin UI

| Req ID | Requirement | User Story | Implementation | Tests | Status |
|--------|------------|------------|----------------|-------|--------|
| FR8.1 | Cognito authentication | US-7.1 | `frontend/src/auth/` | `frontend/tests/auth/` | :white_check_mark: |
| FR8.2 | Platform admin view | US-7.2 | `frontend/src/pages/PlatformDashboard.tsx` | `frontend/tests/pages/` | :white_check_mark: |
| FR8.3 | Org admin view | US-7.3 | `frontend/src/pages/OrgDashboard.tsx` | `frontend/tests/pages/` | :white_check_mark: |
| FR8.4 | Dashboard views | US-7.2, US-7.3 | `frontend/src/components/Dashboard/` | `frontend/tests/components/` | :white_check_mark: |
| FR8.5 | Configuration management | US-7.3 | `frontend/src/pages/Settings.tsx` | `frontend/tests/pages/` | :white_check_mark: |
| FR8.6 | Log viewer | US-7.4 | `frontend/src/pages/LogViewer.tsx` | `frontend/tests/pages/` | :white_check_mark: |
| FR8.7 | React + Tailwind CSS | US-7.1 | `frontend/` | Build passes | :white_check_mark: |

---

## FR9: Usage Tracking and Logging

| Req ID | Requirement | User Story | Implementation | Tests | Status |
|--------|------------|------------|----------------|-------|--------|
| FR9.1 | Log every request | US-8.1 | `src/usage/service.py` (log_request) | `tests/usage/test_service.py` | :white_check_mark: |
| FR9.2 | PostgreSQL storage | US-8.1 | `src/shared/models/usage.py` (UsageLog) | `tests/usage/test_service.py` | :white_check_mark: |
| FR9.3 | CloudWatch Logs (optional) | US-8.1 | Not implemented | - | :x: |
| FR9.4 | Prometheus /metrics endpoint | US-8.2 | `src/shared/metrics.py` | `tests/shared/test_metrics.py` | :white_check_mark: |
| FR9.5 | CloudWatch custom metrics | US-8.2 | Not implemented | - | :x: |

---

## Non-Functional Requirements

| Req ID | Requirement | Implementation | Tests | Status |
|--------|------------|----------------|-------|--------|
| NFR1.1 | <50ms p99 latency overhead | Async FastAPI, connection pooling | Load tests | :white_check_mark: |
| NFR1.2 | 1000+ concurrent connections | uvicorn workers, async handlers | Load tests | :white_check_mark: |
| NFR1.3 | 10,000+ RPM | EKS HPA, multi-pod deployment | Load tests | :large_orange_diamond: |
| NFR2.1 | Stateless application | No server-side sessions | Architecture | :white_check_mark: |
| NFR2.2 | Health check endpoint | `src/shared/routes.py` (/health) | CI/CD | :white_check_mark: |
| NFR2.3 | Graceful degradation | Circuit breakers (partial) | - | :large_orange_diamond: |
| NFR3.1 | Credentials never stored | `src/auth/auth_service.py` | Security review | :white_check_mark: |
| NFR3.2 | Tokens stored hashed | `src/shared/models/token.py` | `tests/auth/` | :white_check_mark: |
| NFR3.3 | HTTPS everywhere | CloudFront + ACM | Infrastructure | :white_check_mark: |
| NFR3.4 | Tenant data isolation | TenantMixin, org_id filters | All tests | :white_check_mark: |
| NFR3.5 | Admin action audit | `src/admin/service.py` (logging) | - | :large_orange_diamond: |
| NFR4.1 | Docker container | `Dockerfile` | CI builds | :white_check_mark: |
| NFR4.2 | EKS Auto Mode | `infra/modules/eks/` | Terraform apply | :white_check_mark: |
| NFR4.3 | Terraform IaC | `infra/` | `terraform plan` | :white_check_mark: |
| NFR4.4 | GitHub Actions CI/CD | `.github/workflows/` | CI runs | :white_check_mark: |
| NFR5.1 | Structured JSON logging | `src/shared/logging.py` | All modules | :white_check_mark: |
| NFR5.2 | Distributed tracing (X-Ray) | Not implemented | - | :x: |
| NFR5.3 | Prometheus metrics | `src/shared/metrics.py` | `/metrics` | :white_check_mark: |
| NFR5.4 | Health/readiness endpoints | `src/shared/routes.py` | K8s probes | :white_check_mark: |

---

## Error Handling User Stories

| Story ID | Scenario | Implementation | Tests | Status |
|----------|----------|----------------|-------|--------|
| US-9.1 | Expired credentials | `cli/bg-cognito-auth.sh` error handling | Manual testing | :white_check_mark: |
| US-9.2 | Unknown organization | `src/auth/auth_service.py` (403 response) | `tests/auth/` | :white_check_mark: |
| US-9.3 | Budget exceeded | `src/budget/middleware.py` (429 response) | `tests/budget/` | :white_check_mark: |
| US-9.4 | All accounts unhealthy | `src/pool/service.py` (503 response) | `tests/pool/` | :white_check_mark: |
| US-9.5 | Unregistered service account | `src/auth/service_account_service.py` | `tests/auth/` | :white_check_mark: |
| US-9.6 | Model not allowed | `src/proxy/service.py` (403 response) | `tests/proxy/` | :white_check_mark: |

---

## Summary

| Category | Total | Implemented | Partial | Not Started |
|----------|-------|-------------|---------|-------------|
| FR1: Authentication | 9 | 9 | 0 | 0 |
| FR2: Organization Management | 6 | 6 | 0 | 0 |
| FR3: Budget Management | 7 | 7 | 0 | 0 |
| FR4: Rate Limiting | 5 | 5 | 0 | 0 |
| FR5: Proxy API | 8 | 8 | 0 | 0 |
| FR6: Bedrock Pool | 6 | 6 | 0 | 0 |
| FR7: Claude Code | 11 | 11 | 0 | 0 |
| FR8: Admin UI | 7 | 7 | 0 | 0 |
| FR9: Usage Tracking | 5 | 3 | 0 | 2 |
| NFRs | 15 | 11 | 2 | 2 |
| Error Handling | 6 | 6 | 0 | 0 |
| **Total** | **85** | **79** | **2** | **4** |

**Overall Completion: 93%**

### Not Implemented Features

1. **FR9.3: CloudWatch Logs integration** - Optional feature, structured JSON logs go to stdout and are collected by EKS
2. **FR9.5: CloudWatch custom metrics** - Prometheus metrics available, CloudWatch integration deferred
3. **NFR5.2: X-Ray distributed tracing** - Deferred to future iteration
4. **NFR3.5: Full admin audit logging** - Basic logging in place, structured audit trail pending

### Architecture Changes from Original Requirements

| Original | Current | Reason |
|----------|---------|--------|
| IAM Identity Center integration | Cognito User Pool + Identity Pool | Simpler setup, better Claude Code integration |
| STS-only authentication | Cognito JWT + STS fallback | JWT validation is faster (no network call) |
| Token exchange for all clients | Direct Cognito authentication | Reduced latency, standard OAuth 2.0 flows |
| Terraform-managed ALB | EKS Ingress Controller ALB | ALB created dynamically, supports Auto Mode |
