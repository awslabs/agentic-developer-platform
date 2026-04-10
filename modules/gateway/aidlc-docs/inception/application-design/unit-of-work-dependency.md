# Unit of Work — Dependency Matrix

## Dependency Table

| Unit | Depends On | Can Start After |
|------|-----------|-----------------|
| Unit 0: Shared Foundation | — | Immediately (built on this machine) |
| Unit 1: Auth | Unit 0 | Unit 0 committed |
| Unit 2: Bedrock Pool | Unit 0 | Unit 0 committed |
| Unit 3: Proxy | Unit 0, IPoolService (ABC) | Unit 0 committed |
| Unit 4: Budget | Unit 0 | Unit 0 committed |
| Unit 5: Rate Limiting | Unit 0 | Unit 0 committed |
| Unit 6: Admin API + Usage | Unit 0, IBudgetService/IRateLimitService (ABCs) | Unit 0 committed |
| Unit 7: Admin UI | Admin API contract (OpenAPI spec) | Unit 0 committed |
| Unit 8: Infrastructure | — | Immediately (standalone Terraform) |
| Unit 9: DevOps Pipelines | Project structure knowledge | Unit 0 committed |
| Unit 10: CLI Tools | Auth API contract | Unit 0 committed |
| Unit 11: Test Generation | Unit 0, user stories | Unit 0 committed |
| Unit 12: Env Provisioning | Unit 8 (Terraform modules) | Unit 8 merged |
| Unit 13: Test Runner | All units merged, Unit 12 | All PRs merged + environments up |

## Parallelism

```
Unit 0 (this machine)
    |
    | commit to main
    |
    | WAVE 1 — All parallel:
    +---> Unit 1: Auth ─────────────────────────> PR
    +---> Unit 2: Bedrock Pool ─────────────────> PR
    +---> Unit 3: Proxy ────────────────────────> PR
    +---> Unit 4: Budget ───────────────────────> PR
    +---> Unit 5: Rate Limiting ────────────────> PR
    +---> Unit 6: Admin API + Usage ────────────> PR
    +---> Unit 7: Admin UI ─────────────────────> PR
    +---> Unit 8: Infrastructure ───────────────> PR
    +---> Unit 9: DevOps Pipelines ─────────────> PR
    +---> Unit 10: CLI Tools ───────────────────> PR
    +---> Unit 11: Test Generation ─────────────> PR
    |
    | All Wave 1 PRs reviewed & merged
    |
    | WAVE 2 — Sequential:
    +---> Unit 12: Environment Provisioning
    |       (terraform apply for Dev, Test, Prod)
    |
    | WAVE 3 — Sequential:
    +---> Unit 13: Test Runner
    |       (execute all tests against Dev/Test)
    |
    | All tests pass → Ready for Prod deployment
```

## Cross-Unit Interface Dependencies (Resolved via ABCs)

| Unit | Uses Interface | Provided By | How Resolved During Dev |
|------|---------------|-------------|------------------------|
| Unit 3 (Proxy) | `IPoolService` | Unit 2 (Pool) | Mock implementation in tests |
| Unit 6 (Admin API) | `IBudgetService` | Unit 4 (Budget) | Mock implementation in tests |
| Unit 6 (Admin API) | `IRateLimitService` | Unit 5 (Rate Limit) | Mock implementation in tests |
| Unit 6 (Admin API) | `IUsageService` | Unit 6 itself | Real implementation |
| Unit 7 (Admin UI) | Admin API endpoints | Unit 6 (Admin API) | MSW (Mock Service Worker) |
| Unit 10 (CLI Tools) | POST /auth/exchange | Unit 1 (Auth) | Mock curl responses |

## Integration Wiring (Post-Merge)

After all PRs are merged, `app.py` auto-discovers all routers and the dependency injection in `app.py` wires real service implementations:

```python
# Real wiring happens at app startup — no code changes needed
auth_service = AuthService(token_repo, sts_client, tenant_resolver)
pool_service = PoolService(pool_config, sts_client)
proxy_service = ProxyService(pool_service, format_translator, model_resolver)
budget_service = BudgetService(budget_repo, pricing_table)
rate_limit_service = RateLimitService(backend)
usage_service = UsageService(usage_repo)
```

## Merge Order Recommendation

No strict order required since all units are independent, but recommended:
1. Unit 8 (Infrastructure) — so deployment target exists
2. Unit 9 (DevOps Pipelines) — so CI/CD is ready
3. Units 1, 2, 4, 5 (core services with no cross-unit deps) — in any order
4. Units 3, 6 (depend on other unit interfaces) — after their dependencies
5. Unit 10 (CLI Tools) — after Auth is merged
6. Unit 7 (Admin UI) — after Admin API is merged
