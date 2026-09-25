# S12 Registry Credential Capability Gate — Acceptance & Remaining Work

Issue #6050 (S12 continuation from #5611). Original findings: #4702, #4724.

## What this PR covers

Replaced the header-based `_check_agent_scope` helper (which trusted the
caller-supplied `X-Agent-Scopes` header) with `require_credential_capability()`
in `src/internal/credential_authorization.py`. The two sensitive credential
delivery endpoints — `credential-raw-read` and `credential-materialize` — now
require the exact `credential_scopes` capability from the server-resolved
`request.state.token_context`, set at authentication time from the agent
registry DynamoDB entry.

### Denial behavior

| Caller type | Outcome |
|---|---|
| Registered IRSA identity with correct `credential_scopes` | Allowed |
| Registered IRSA identity with empty/wrong `credential_scopes` | 403 |
| Shared-key-only caller (no `token_context`) | 403 |
| Any caller with forged `X-Agent-Scopes` header but no registry scope | 403 |
| Any caller with forged body fields | 403 |

All denials occur before secret fetch, S3 upload, or presigned URL generation.

### Credential delivery siblings — full inventory

| Endpoint | Path | Scope gate (this PR) | Notes |
|---|---|---|---|
| user-credentials | `GET /internal/v1/user-credentials` | None (metadata only) | No secrets returned |
| proxy-request | `POST /internal/v1/proxy-request` | None | URL allowlist + host binding |
| **credential-raw-read** | `POST /internal/v1/credential-raw-read` | **`credential:raw-read` (registry)** | Changed in this PR |
| **credential-materialize** | `POST /internal/v1/credential-materialize` | **`credential:materialize` (registry)** | Changed in this PR |
| credential-assume-role | `POST /internal/v1/credential-assume-role` | None (separate STS module) | Uses own auth |
| github-installation-token | `POST /internal/v1/github-installation-token` | None | Broker repo binding |
| worker-task-credentials | `POST /internal/v1/worker-task-credentials` | None | Task-specific |
| vault-evidence | various | `DELIVERY_SCOPE` (separate) | Uses `domain_operation_runtime` |

### Caller/grant sources

| Source | Grants | Notes |
|---|---|---|
| Legacy registry seeds (gateway-infra + agent-factory-infra) | `["credential:raw-read"]` only | Verified by `test_credential_scopes_seed.py` |
| Protected worker registry | `["credential:raw-read", "credential:materialize"]` + `requires_run_identity` | Full broker path |
| Shared-key callers | No `credential_scopes` | No `token_context` |

### Preserved behavior

- Raw-read feature flag (`BG_VAULT_RAW_READ_ENABLED`) still fires before scope gate
- Credential-authorization binding (issue #3175) unchanged
- Owner verification (`verify_selected_user_credential`) and TOCTOU revalidation unchanged
- Tenant constraints (`worker_tenant`) unchanged
- Egress host binding unchanged
- Audit logging with provenance unchanged
- File delivery semantics (presigned S3 URL for materialize) unchanged
- Broker identity verification in `broker_identity.py` (separate, stronger layer) unchanged

## Remaining work (NOT covered by this PR)

### Policy/owner interface changes
- The `owner` flag default is not changed in this PR; coordinated handoff required
- `proxy-request` and `credential-assume-role` do not yet have registry capability
  gates — they use URL allowlists and broker policy respectively

### Rollout ordering
1. Reconcile legitimate caller registry grants first (legacy seeds grant only
   `raw-read`; protected registry grants both). A caller that currently reaches
   `credential-materialize` via the legacy registry seed + the old header gate
   will be denied after this change until their registry entry is updated
2. Deploy gateway with this change
3. Verify no bypass: forged headers must produce 403

### Shared-key migration impact
- Shared-key-only callers are now categorically denied on `raw-read` and
  `materialize` endpoints. In practice, the IRSA path has been the live path
  for protected workers since the broker auth rollout. The agent-context
  ingestion callback (the only remaining shared-key caller) does not call
  these endpoints
- If a non-IRSA caller needs raw-read or materialize in future, it must be
  registered in the agent registry with appropriate `credential_scopes`

### Fail-closed recovery
- If a legitimate registered worker is denied, check its `credential_scopes`
  in the agent registry DynamoDB table. Add the required scope and redeploy
  (no code change needed — registry is live)
- The broker identity check in `broker_identity.py` (lines 68-77) is a
  *separate* layer that also verifies `credential_scopes`. Both layers must
  pass for the full protected worker path

### Related tracking
- #5195 owns worker IAM/progress-mediation rollout
- #5611 (parent S12) — broader credential authorization hardening
- The rest of S12 remains open for: assume-role/list/proxy permission mapping,
  full owner interface changes, and gateway-level verification
