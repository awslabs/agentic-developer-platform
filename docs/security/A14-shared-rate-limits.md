# A14: durable configuration and shared rate limits

Scope: #5670. Merge after A13 (#5669), preserving auth → approval → model identity → budget → rate order. No live gateway deployment is claimed.

The `/ratelimits` mutation API now uses the existing AdminService database writer, the same `rate_limit_configs` records as `/admin` and enforcement. Read-back and admission reload committed records rather than waiting for process-local cache expiry. Configuration reads have a three-second deadline; two reads can occur for check and consumption. This deliberately adds database load to the admission path. Per-entity `burst_size` was never persisted/enforced and is explicitly rejected instead of acknowledged; the configured global burst policy still applies. The mutation API uses S13 durable audit intent and terminal receipts.

The application wires one limiter into its router and middleware. Redis is the default, reads the existing `BG_REDIS_URL` (or explicit `RATELIMIT_REDIS_URL`), and uses the existing authenticated Redis factory. Missing configuration or failed startup ping prevents startup. Process-local counters require explicit `RATELIMIT_ALLOW_MEMORY_BACKEND=true` plus memory backend, or the explicit test harness. Shipped ConfigMap selects Redis and disables local mode. No new cloud resources are provisioned.

Redis operations have two-second connect/read timeouts and never allow requests on counter failure. Token/concurrency failures refuse admission; configuration/count failures return 503 before provider invocation. ErrorCount metrics and structured errors expose backend unavailability. A failed partial hierarchy acquisition releases only slots it actually acquired. Concurrent decrement floors at zero atomically. Successful admissions retain existing completion release and expiry behavior.

Validation:

- Existing limiter and proxy enforcement regression suites passed; final totals are recorded on the PR.
- Durable SQLite regression exercises independent service instances, the existing admin writer, immediate read-back, tenant separation and deletion.
- Audit regression proves attributable pending/success receipts and zero configuration writes on admission-audit failure.
- Four independent spawned Python processes against real Redis 7.4: 32 token attempts produce exactly seven successes; 16 concurrent acquisitions produce exactly three successes. Redis ran with network disabled, exposed only a Unix socket, image `redis@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499`.
- Outage tests prove no quota is granted; missing production configuration refuses construction; explicit local mode remains available.
- The budget/rate eval no longer tolerates multiplied per-worker capacity or inconclusive concurrency results.

Optional real-process regression command: set `RATELIMIT_TEST_REDIS_URL` to a fresh disposable Redis Unix socket and run `tests/ratelimit/test_shared_acceptance.py`. Its namespace must start empty. Normal CI runs the deterministic persistence/failure/audit tests and skips only the external Redis case.

Rollout must coordinate the shared gateway image owner and A13 request/accounting behavior. Operators should verify the existing authenticated Redis endpoint and review tenant ceilings because removing the worker multiplier makes the configured limit effective. Rolling back to the earlier memory-default build restores the known defect; no permissive flag is a safe mitigation for backend outages.
