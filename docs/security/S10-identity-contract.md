# S10 — stable verified identity contract for downstream consumers

Reference for S11 (tenant/installation resolution, #5610), S13 (roles/admin
enforcement, #5612), S15 (execution containment, #5614) and any new internal-plane
consumer. Derived from the S10 revalidation (PR #5890) and continuation
(#5972). The route-coverage regression test in
`tests/auth/test_mounted_route_identity_coverage.py` checks the mounted
authentication inventory. It does not prove the implementation of each listed
exception; the mechanism-specific behavioral suites below provide that evidence.

## Rules

### 1. Never read `X-Caller-Identity` to establish identity

Call `verified_caller_identity(request, settings=get_settings())` from
`src/auth/caller_provenance.py`. Pass your own module's `get_settings` — this
is a load-bearing seam, not cosmetic. Each consumer resolves configuration
through its own module import; omitting it was the cause of the first A01 draft
rejecting the legitimate IRSA path with a 403.

The helper returns the asserted ARN only when:
- `trust_apigw_headers` is on (`:89`),
- a provenance secret is configured (`:96`), and
- the supplied `X-Adp-Edge-Provenance` matches it via `secrets.compare_digest` (`:99`).

Each failure emits a distinct metric reason. `None` means no trustworthy
assertion — either none was made, or none can be believed.

### 2. Preserve each authentication surface's fallback contract

In `get_current_user` (the ~18 public/admin routers), an unproven IAM
assertion is **ignored** — the caller may still authenticate via Cognito JWT
(`dependencies.py:203-205`). Only a proven IAM assertion commits that path to
IAM resolution.

In `verify_internal_or_irsa` (the /internal/* guard), raw assertion presence
is **terminal**: a failed provenance check rejects the request; it never falls
back to the shared secret. The two states (assertion-absent vs
assertion-present-but-failed) are deliberately distinguishable.

### 3. IAM contexts derive tenant/org identity from the registry

`agent_entry_to_token_context` in `src/auth/agent_registry.py` constructs the
registry-backed IAM context. It carries `scope`,
`requires_run_identity` and `credential_scopes` from the DynamoDB registry row.
Never read a caller header for authenticated org/tenant identity on the IAM path.
`X-Agent-OrgId` can affect `attributed_org_id` for billing; it must never
replace authenticated `org_id` or authorize access. Internal-plane principals
are platform services permitted to resolve tenant identities; S10 does not
pretend they are tenant-scoped users. A tenant-scoped registry principal with
`shared` or `personal` scope cannot enter this plane by claiming another org.

### 4. Authorize after verification, never before

The `auth_deps.py:178 → :200 → :227` ordering: provenance check, then
registry resolution, then scope enforcement. S13 (roles/admin) inherits this
requirement directly.

### 5. New `/internal/*` routers must have explicit authentication

Each new route must either:
- Depend on `verify_internal_or_irsa` (directly or via a reviewed wrapper), or
- Carry independently reviewed verification (STS SigV4, Kubernetes TokenReview,
  GitHub OIDC), or
- Have an explicit narrowly public contract (e.g. publishing public signing keys).

The route-coverage test (`test_mounted_route_identity_coverage.py`) will fail
on an unclassified route. Adding an exception requires updating the
`REVIEWED_EXCEPTIONS` set with a review of the independent mechanism. The
Terraform postcondition (`api-gateway/main.tf:476-512`) separately fails the
deploy if an edge path omits the header mapping.

### 6. Internal / platform scopes must stay non-self-assignable

The agent registry admin API constrains caller-settable scope to
`^(shared|personal)$`, so `internal` and `platform` are written only by
Terraform seeds. Any change relaxing this constraint defeats the internal-plane
gate entirely.

## Scope limitations

This contract covers the gateway application's authentication layer. It does
not establish:
- Live environment enforcement (NetworkPolicy, API Gateway resource policy)
- Production rollout of A01/A05 beyond dev
- Rate limiting of the `/internal/` path prefix (R2, coordinated with A14 #5670)
- Timing indistinguishability of the constant-time comparison

These items are tracked in the
[live acceptance checklist](runs/2026-09-21/S10-live-acceptance-checklist.md).

## Behavioral evidence behind the inventory

Run these gateway suites together with the two S10 auth suites; registration or
function names alone are not proof of authentication behavior:

| Surface | Behavioral suite under `modules/gateway/tests/` |
|---|---|
| STS work admission and root admission | `agentauth/test_work_producer.py` and `agentauth/test_external_roots.py` |
| Task admission (producer proof and caller submit scope) | `agentauth/test_task_admission_integration.py`, `internal/test_task_admission_proof.py` |
| Task dispatch/recovery | `agentauth/test_task_dispatch_routes.py` plus mounted invalid-proof cases in the S10 caller suite |
| Persona selection / probe wrappers | `internal/test_persona_model_probe_routes.py` and `admin/persona_models/test_dispatch_selection.py` |
| Pod-bound chat decisions | `agentauth/test_chat_model.py` |
| Registered workflow OIDC and owner identity | `agentauth/test_arc_model.py` |
| Cyber jobs and result ownership | `agentauth/test_cyber_jobs.py` |
| Public signing keys and retired legacy chat | `agentauth/test_model_policy_keys.py` |

S10 mounted caller cases execute valid bodies and require exact successful
responses, persisted magic-link nonces, and the downstream registry-derived
context. Rejected cases include unproven assertions and registered tenant-b
principals attempting to claim the internal scope through headers. Local tests
are source evidence; the separate live checklist remains pending until executed.
