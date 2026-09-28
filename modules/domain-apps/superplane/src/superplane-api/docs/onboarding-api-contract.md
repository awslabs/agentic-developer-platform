<!-- GENERATED FILE — DO NOT EDIT BY HAND.
     Regenerate: python scripts/generate-onboarding-contract.py
     Source of truth: app.main.app.openapi() + app.endpoint_inventory.classify()
     Verified by: tests/test_onboarding_contract.py -->

# Superplane onboarding API contract

Issue #5535, EPIC #4910. Shared with **#5730** (onboarding UI and CLI).

Every row below is read from this service's own OpenAPI schema and from
`app/endpoint_inventory.py`, the table the app-wide authorization guard actually
enforces. Nothing here is hand-maintained, so it cannot drift from the app
without `tests/test_onboarding_contract.py` failing.

## The ordering that makes a zero-workspace start possible

A control plane must come up with **no workspaces registered at all**, let a
verified organization administrator sign in, and list an empty set. Therefore:

1. Organization-administrator authority exists **before** any workspace grant
   does. Steps 1-3 in the table resolve entirely from organization scope.
2. `GET /workspaces` returning `[]` is a **success**, not an error state. A client
   that treats an empty list as a failure, or that fetches a workspace before
   rendering, cannot onboard anybody.
3. Creating the first workspace therefore cannot require a ready workspace as a
   prerequisite. `POST /workspaces` is `organization`-scoped for this reason.

## How authority is resolved

The caller's organization and principal are resolved **server-side** from the
authenticated ADP identity plus that identity's current grants. Request fields
never confer authority: a body key that asserts an identity
(`org_id`, `workspace_id`, `principal`, `role`, ... — plus the `x-`, `adp_`,
`auth_` and `caller_` prefixes) is **refused**, and it is refused even when its
value matches the caller's real organization. See
`app/services/provisioning.py:FORBIDDEN_PARAMETER_KEYS`.

## Error mapping

| Status | Meaning | Client action |
|--------|---------|---------------|
| `401` | No or invalid authentication. | Re-authenticate. |
| `403` | Authenticated, but **refused** — a grant is missing, or evidence could not be established. | Do not retry; the caller lacks authority. |
| `404` | Not found, or not visible to this caller. | Do not retry. |
| `409` | Conflict with existing state. | Reconcile, then retry. |
| `422` | Request failed validation. The rejected **value is never echoed** (`app/main.py` scrubs it), so the body names the field, not the input. | Fix the request. |
| `429` | Rate or quota limit. | Back off and retry. |
| `503` | **Unavailable** — a required integration is not configured or not reachable. | Retry; surface as a platform fault, not as the user's error. |

`403` and `503` are deliberately distinct and must not be collapsed by a client.
A refusal is an authorization answer; unavailability is an outage. Rendering one
as the other sends an operator to debug permissions on a healthy system, or to
debug an outage that is really a missing grant.

## Receipts

A mutating onboarding action returns the created or updated resource, carrying
the identifier a client must persist to follow the work. Provisioning is
**asynchronous**: `POST /workspaces` returns `201` with the workspace record, and
that `201` means *the operation was accepted*, not that infrastructure exists.
A client must poll `GET /workspaces/{workspace_id}` for state rather than
treating the `201` as completion.

Note the state vocabulary is three-valued, not two: an operation may be
`succeeded`, `failed`/`cancelled`, or **inconclusive** (`pending`, `running`,
`unknown`). `unknown` is **not** a failure — a client that renders it as one
tells a user their workspace is gone when it may be running and billable.

## The onboarding surface


| # | Method | Path | Scope | Permission | Request | Success | Codes |
|---|--------|------|-------|------------|---------|---------|-------|
| 1 | `POST` | `/auth/login` | _public_ | — | `LoginRequest` | `LoginResponse` | `200`, `422` |
| 2 | `GET` | `/orgs/current` | `organization` | `workspace:read` | — | `OrgResponse` | `200` |
| 3 | `GET` | `/workspaces` | `organization` | `workspace:read` | — | `WorkspaceListResponse` or `EligibleClusterListResponse` | `200`, `422` |
| 4 | `POST` | `/workspaces` | `organization` | `workspace:provision` | `CreateWorkspaceRequest` | `WorkspaceResponse` | `201`, `422` |
| 5 | `GET` | `/workspaces/{workspace_id}` | `workspace` | `workspace:read` | — | `WorkspaceResponse` | `200`, `422` |
| 6 | `DELETE` | `/workspaces/{workspace_id}` | `workspace` | `workspace:provision` | — | `WorkspaceDeleteResponse` | `200`, `422` |
| 7 | `GET` | `/users` | `organization` | `workspace:read` | — | `UserListResponse` | `200` |
| 8 | `POST` | `/users/invite` | `organization` | `workspace:administer` | `InviteUserRequest` | `UserResponse` | `201`, `422` |
| 9 | `GET` | `/accounts` | `organization` | `workspace:read` | — | `AccountListResponse` | `200` |
| 10 | `POST` | `/accounts` | `organization` | `workspace:provision` | `RegisterAccountRequest` | `AccountResponse` | `201`, `422` |
| 11 | `GET` | `/vault/credentials` | `organization` | `workspace:read` | — | `CredentialListResponse` | `200` |
| 12 | `POST` | `/vault/credentials` | `organization` | `workspace:renew_credential` | `RegisterCredentialRequest` | `CredentialResponse` | `201`, `422` |
| 13 | `POST` | `/workspaces/{workspace_id}/provider-connections` | `workspace` | `workspace:renew_credential` | — | inline | `201`, `422` |

### What each step is for

1. `POST /auth/login` — Establish a session. Cannot require a credential.
2. `GET /orgs/current` — Resolve the caller's organization, server-side.
3. `GET /workspaces` — List workspaces. MUST succeed with zero workspaces.
4. `POST /workspaces` — Create the first workspace.
5. `GET /workspaces/{workspace_id}` — Read one workspace's state.
6. `DELETE /workspaces/{workspace_id}` — Tear a workspace down.
7. `GET /users` — List organization members.
8. `POST /users/invite` — Invite a member.
9. `GET /accounts` — List registered provider accounts.
10. `POST /accounts` — Register a provider account.
11. `GET /vault/credentials` — List credential references (never values).
12. `POST /vault/credentials` — Register a credential reference.
13. `POST /workspaces/{workspace_id}/provider-connections` — Attach a provider connection to a workspace.

> The `Codes` column lists only what the schema declares. Every authenticated route can additionally answer `401`, `403` and `429` through the app-wide guard and the rate limiter, which are middleware and so are not enumerated per route in the schema.
