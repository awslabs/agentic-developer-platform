# User Identity Linking & Per-User Credentials — Design

**Status:** Approved for implementation
**Author:** Pranav + Claude
**Last updated:** 2026-04-27

### Architect Refresh — Delta Notes (2026-04-26, Issue #133)

This section captures findings from the Phase 0 architect pass validating the design against the current repo state. Six areas were reviewed; three required corrections, three were confirmed as-is.

**Corrections applied:**

1. **Migration numbering (was wrong).** The doc stated latest migration was `002_cognito_fields.py` and vault would be `003`. The latest is now `003_model_pricing_columns.py` (revision `003_model_pricing`, added for Issue #234). **Vault migration must be `004_user_identities_and_credentials.py`.**

2. **No FK constraints exist in the schema today.** The design specifies `FK -> organizations` and `FK -> users.id ON DELETE CASCADE`. However, the existing schema uses **no ForeignKey constraints at all** — `001_initial_schema.py` creates all tables without FK references, and the SQLAlchemy models have no `ForeignKey()` declarations. The vault tables would be the **first real FK constraints** in this database. This is fine and desirable (FK integrity for cascade deletes is critical for vault security), but implementers must be aware that Alembic autogenerate will not detect FK relationships from the existing models. The vault migration must explicitly create the FK constraints with `ON DELETE CASCADE`, and should also add FK constraints on the vault's `org_id` columns pointing to `organizations.id`.

3. **PK type mismatch.** The design says `id: UUID PK`. The existing tables use `String(255)` with a `new_uuid()` default (see `base.py:new_uuid`). Vault tables should follow the established pattern: `String(255)` PK with `default=new_uuid`, not a native UUID column. This preserves ORM consistency.

**Confirmed as-is:**

4. **Module placement — same-pod-new-router is correct for v1.** The vault's user-facing endpoints (`/auth/credentials`, `/auth/identities`) and internal endpoints (`/internal/v1/*`) should run on the gateway FastAPI process. Rationale: (a) vault shares the gateway's Postgres for FK integrity to `users`/`organizations`; (b) no network hop for `/internal/v1/*` calls; (c) the gateway service account (`gateway-service` in `adp-gateway` namespace) already has IRSA — only needs a policy addition for Secrets Manager. The `user-services-overview.md` says each service has "its own Dockerfile" — this applies to v2+ services that justify separation. For v1 the vault is a router addition to the gateway, not a standalone deployment. The `modules/user-services/vault/` directory holds the *source code* (models, routes, tests), but it is imported and mounted by the gateway's `app.py`.

5. **TenantMixin usage is compatible.** `TenantMixin` adds `org_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)` — matches the design's intent. The vault models should inherit `Base, TenantMixin` exactly like `Department`, `Team`, `User`, and `ServiceAccount` do.

6. **Delivery paths are sound with one IAM fix required.** The three delivery paths (HTTP proxy, file materialization, raw-value escape hatch) are architecturally correct. However, the agent runner IAM role (`adp-dev-runner-role` in `modules/agent-factory/infra/modules/runner-iam/main.tf`) currently grants `secretsmanager:GetSecretValue` on `arn:aws:secretsmanager:*:*:secret:adp/*` — which **includes** `adp/users/*`. The design's security invariant ("agent pods do NOT have direct Secrets Manager access") is not enforced by current IAM. **The runner IAM policy must be tightened to exclude `adp/users/*`** before vault secrets are stored there. Recommended fix: change the runner's Secrets Manager resource from `adp/*` to `adp/gh-app-*` (the only secrets runners currently need), or add an explicit Deny on `adp/users/*`.

**New gaps surfaced:**

7. **AG-UI event protocol (Issue #97).** The agent now emits AG-UI events (`TOOL_CALL_START`, `TOOL_CALL_ARGS`, `TOOL_CALL_END`) for every tool invocation. Vault MCP tools (`http_request_with_credential`, `materialize_user_credential`, `get_user_credential_raw`) will generate these events. **The `TOOL_CALL_ARGS` delta must never contain credential values.** The tool implementations must sanitize their input summaries before they reach the AG-UI event emission in `complex-task-chat-agent.ts`. The `inputSummary` for vault tools should show `{service, label, url}` but never `{value}`.

8. **Context store path has changed.** The design references `modules/agent-factory/agent/src/complex-task-chat/context-store.ts` for the LCM scrubber integration. This file no longer exists — context management was refactored into `context/factory.ts` and the `context/` subdirectory. The LCM scrubber registration point should be updated to target the context factory's record path.

9. **Internal endpoint routing infrastructure does not exist yet.** The gateway currently has no `/internal/*` route namespace and no service-to-service IAM auth middleware. The vault implementation must create: (a) an `internal` router with IAM signature verification middleware, (b) registration in `app.py`'s `UNIT_MODULES` list. This is net-new infrastructure, not a slot-in.

10. **`/auth/exchange` endpoint collision.** The existing `/auth/exchange` route (Issue #133 security fix, now disabled by default) uses the same `#133` issue number as this vault review. This is a numbering coincidence, not a conflict — but implementers should note that the `/auth` router already has deprecated endpoints. New vault routes under `/auth/credentials` and `/auth/identities` will coexist cleanly; no namespace collision.

---

## Position in the platform

The vault is the **first service in a new module cluster: `modules/user-services/`** — per-user products the user owns, tenant-isolated, self-service. See `docs/user-services-overview.md` for the cluster-wide invariants and the planned sibling services (personal knowledge repo, user-scoped bespoke agents, chief-of-staff).

All v1 code for the vault lives under `modules/user-services/vault/` — backend, migrations, frontend pages, and infra together in one place. The one unavoidable exception: the three MCP tools (`http_request_with_credential`, `materialize_user_credential`, `get_user_credential_raw`) register inside the agent bundle at `modules/agent-factory/agent/src/complex-task-chat/tools.ts`, because that is where agent tools actually run. They are thin shims that call the vault's `/internal/v1/*` endpoints.

The vault shares the gateway's Postgres for `users` / `organizations` foreign keys — every service in `modules/user-services/` will make the same pragmatic call until a database split is justified. That keeps FK integrity without forcing a microservice-y data-duplication conversation on day one.

Practical consequences for this v1:

- The `/internal/*` endpoints are versioned (`/internal/v1/...`) from day one so future promotion does not break clients.
- The three MCP tools are designed as **platform-level tools** — they are not specific to the chat agent. In v1 they register under the chat agent's tool list for expediency; future agents register the same closure-wrapped tools in their own bundles.
- The identity resolver (`/internal/v1/resolve-user`) conceptually belongs to the invocation surface. For v1 it is called by channel adapter Lambdas. Later it becomes an explicit middleware stage in the invocation pipeline so no channel adapter can silently skip it.

Everything below remains the v1 design; the framing above clarifies ownership and prevents this code from sprawling across modules.

## The user vault

User-facing name for what this feature delivers: **each user has a personal vault**. The vault is a per-user, tenant-scoped store of secrets the user chooses to make available to agents acting on their behalf.

- **Per-user namespace.** Every user's vault lives at `adp/users/<cognito_sub>/*` in AWS Secrets Manager. No other user can see or use it; admins cannot read values (only delete under break-glass, audited).
- **Self-service.** Users add, update, and remove their own credentials via the admin UI (`/settings/credentials`) and REST API (`/auth/credentials`). No out-of-band provisioning.
- **Agent use, not exposure.** Agents acting on the user's behalf can *invoke* the vault (default: via the gateway-side proxy, so the raw value never enters the agent process) but cannot enumerate or exfiltrate it.
- **Tenant-isolated.** `org_id NOT NULL` on every row; a vault entry belongs to exactly one org.
- **Clean lifecycle.** Deleting a credential synchronously removes the Secrets Manager secret; deleting a user cascades to all their vault entries; a nightly sweeper cleans up orphans.

"Vault" throughout this document is synonymous with the per-user credential store described below. The schema uses `user_credentials`; the user-facing terminology is *vault* and *vault entry*.

## Problem

Today the agent gateway only knows users that arrive through the web chat (Cognito-authenticated WebSocket). The same human may also contact the platform from Slack, GitHub, or WhatsApp — but there is no way to resolve those channel identities back to the same internal user, so:

- Each channel starts a cold session; no continuity.
- The agent cannot call third-party APIs on the user's behalf (GitHub, Jira, internal tools) because it has no place to look up their credentials.
- Tenant isolation (`org_id`) cannot be enforced on inbound Slack/GitHub events because we don't know which org the sender belongs to.

## Goal

1. Every inbound message, regardless of channel, resolves to a single internal `users.id` (and therefore `org_id`).
2. Users can securely register credentials for external services (GitHub PAT, Jira API token, etc.) once, and any agent acting on their behalf can use them for the duration of a task — by default through a gateway-side proxy so the raw value never crosses the agent process boundary.
3. Identity linking and credential registration are self-service through the gateway admin UI; no out-of-band provisioning.

## Non-goals (for v1)

- SSO / SAML federation beyond what Cognito already provides.
- OAuth app publishing for each third-party service. v1 accepts user-pasted tokens; OAuth flows are a v2 concern.
- Automatic secret rotation. v1 stores what the user gives us; rotation is deferred.
- Cross-tenant identity (same human belongs to two orgs). v1 treats `(org_id, provider, provider_user_id)` as unique.
- **App-scoped / tenant-scoped credentials** (e.g. "the org's VirusTotal Enterprise key", "the team's deploy bot PAT"). v1 covers *user* credentials only. The schema's `user_id NOT NULL` is a deliberate v1 constraint; app-scoped credentials are v2 and will relax this with a separate review.

## What already exists in the gateway

The gateway module (`modules/gateway/`) already owns user and tenant management — this design extends it rather than introducing parallel stores.

- **Postgres tables** (`modules/gateway/src/shared/models/organization.py`): `organizations`, `departments`, `teams`, `users`, `service_accounts`, `tokens`. `users.cognito_sub` is indexed and is our canonical user key.
- **TenantMixin** (`modules/gateway/src/shared/models/base.py:12`): every tenant-scoped table carries `org_id` NOT NULL. New tables will follow this.
- **Pre-token-generation Lambda** (`modules/gateway/infra/modules/cognito/lambda/pre_token_generation.py`): injects `custom:org_id`, `custom:team_id`, `custom:department_id` into JWTs on login — this is how the backend knows which tenant a caller belongs to.
- **Alembic migrations** (`modules/gateway/alembic/versions/`): additive-only pattern, latest is `003_model_pricing_columns.py` (revision `003_model_pricing`). New migration for this feature will be `004_user_identities_and_credentials.py`.
- **FastAPI auth routes** (`modules/gateway/src/auth/routes.py`): `/auth/me`, service-account CRUD. New identity/credential endpoints slot in here.

## Relationship to the agent-side identity epic (#181)

Epic #181 ("User identity + per-tenant isolation across the agent platform") propagates `TokenContext` (`{user_id, org_id, team_id, department_id, account_type}`) end-to-end through the agent module — WebSocket `$connect` → ingest Lambda → SQS task payload → worker pod. Today the agent module has `tenant_id` in its types but never validates against a real identity source.

The vault depends on this. The MCP tools (`http_request_with_credential` etc.) look up credentials by `user_id` — that value is only trustworthy if the worker received it from a validated JWT, not from an attacker-controlled session ID. Concretely:

- **Vault Phase 1** (schema + secrets substrate) can land independently — the DB tables and Secrets Manager namespace don't need the agent-side plumbing.
- **Vault Phase 4** (MCP tools in the agent) **depends on epic #181 Stage A** (JWT claim propagation to the worker's `TokenContext`). Without Stage A, the MCP tools would read `user_id` from an unvalidated SQS payload — a trust boundary violation.
- **Vault Phase 5** (ingest identity resolution) overlaps with #181 Stage A and should be coordinated as a single PR if possible — both extend the ingest Lambda's token-extraction path.

## Design

### Two new Postgres tables

Both inherit `TenantMixin` (→ `org_id NOT NULL`), both FK to `users.id` with `ON DELETE CASCADE`.

#### `user_identities`

Links a platform-specific user id (e.g. Slack `U123`, GitHub user id `456`) to an internal `users.id`.

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `org_id` | UUID FK → organizations | TenantMixin |
| `user_id` | UUID FK → users.id | ON DELETE CASCADE |
| `provider` | enum(`slack`, `github`, `whatsapp`, `discord`, ...) | extensible |
| `provider_user_id` | string | e.g. `T01ABC:U987` for Slack (workspace-scoped), `12345` for GitHub |
| `provider_username` | string nullable | human-readable handle, for display/logs |
| `verification_method` | enum(`oauth`, `magic_link`, `admin_manual`) | how the link was established |
| `verified_at` | timestamp | |
| `created_at`, `updated_at` | timestamp | |

**Indexes:**
- `UNIQUE (provider, provider_user_id)` — global, prevents the same Slack user being linked to two different internal users.
- `(user_id)` — list all links for a user.
- `(org_id, provider)` — admin queries.

**Why `UNIQUE` is not scoped by `org_id`:** a Slack user id belongs to exactly one human. If two orgs both claim it, something is wrong — reject the second link attempt loudly.

#### `user_credentials`

One row per (user, service) credential. The **actual secret value lives in AWS Secrets Manager**; this table stores only the reference.

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `org_id` | UUID FK → organizations | TenantMixin |
| `user_id` | UUID FK → users.id | ON DELETE CASCADE → also deletes the secret (see rollback) |
| `service` | string | free-form tag, e.g. `github`, `jira`, `linear`, `custom-api-foo` |
| `credential_type` | enum(`api_key`, `oauth_token`, `basic_auth`, `bearer`, `ssh_key`, `certificate`, `config_file`) | informs how the agent uses it; HTTP types use the proxy path, file types use the file-materialization path |
| `label` | string | user-facing name, e.g. "Personal GitHub PAT" |
| `secret_arn` | string | full ARN, e.g. `arn:aws:secretsmanager:us-east-1:ACCT:secret:adp/users/<sub>/github-abc123` |
| `scopes` | JSONB nullable | scopes/permissions the user granted; informational only |
| `expires_at` | timestamp nullable | if the user told us; we don't auto-refresh in v1 |
| `last_used_at` | timestamp nullable | updated by the runtime resolver |
| `created_at`, `updated_at` | timestamp | |

**Indexes:**
- `UNIQUE (user_id, service, label)` — a user can have multiple credentials per service (e.g. "Work GitHub", "Personal GitHub") but must label them.
- `(org_id, service)` — admin queries.

### AWS Secrets Manager layout

One secret per (user, service, label):

```
adp/users/<cognito_sub>/<service>-<short_uuid>
```

Example: `adp/users/378cm2j.../github-abc123`

**Secret payload (JSON).** Single shape across all credential types; `value` carries the payload, `encoding` tells the delivery path how to interpret it.

```json
{
  "type": "api_key",
  "encoding": "plain",
  "value": "ghp_xxxxxxxxxxxxxxxx",
  "scopes": ["repo", "read:org"],
  "metadata": {
    "added_by_ip": "…",
    "added_at": "2026-04-20T13:14:00Z"
  }
}
```

For file-oriented types (`ssh_key`, `certificate`, `config_file`):

```json
{
  "type": "ssh_key",
  "encoding": "pem",
  "value": "<PEM-framed OpenSSH private key, verbatim from user input>",
  "filename_hint": "id_ed25519",
  "mode_hint": "0600",
  "metadata": { "added_at": "…" }
}
```

`encoding` values: `plain` (HTTP token types), `pem` (SSH/certs), `base64` (binary blobs), `json` (structured config). `filename_hint` and `mode_hint` are consumed by the file-materialization path; they are hints, not trusted — the delivery path validates and sanitizes both.

**Size cap:** vault entries are for credentials, not arbitrary file storage. Max payload size is 64 KB (matches AWS Secrets Manager's efficient range and covers every credential file we've seen). Larger files are rejected at registration time with a clear error.

**Why one-secret-per-credential instead of one blob per user:**
- IAM can scope by ARN pattern — agent pods get `secretsmanager:GetSecretValue` on `adp/users/*/<service>-*` only for services they need.
- Rotation (future) is per-credential, not global.
- Audit trail (CloudTrail) clearly shows which credential was read, not "user blob opened".
- Cost is fine at expected scale (<10 secrets/user × $0.40/mo).

### Cross-platform user resolution

Replaces the current `webchat.py` fallback behavior and gives Slack/GitHub the same treatment.

**The flow, per inbound message:**

1. Channel adapter parses the event, extracts `provider` + `provider_user_id` (e.g. Slack `T01ABC:U987`).
2. Ingest Lambda calls gateway `POST /internal/resolve-user` (service-to-service, signed with the existing gateway IAM auth) with `{provider, provider_user_id}`.
3. Gateway looks up `user_identities`:
   - **Hit** → return `{user_id, cognito_sub, org_id, team_id, department_id}`. Ingest injects `cognito_sub` as `user_id` into the UnifiedMessage and the existing LCM ownership checks Just Work.
   - **Miss** → return 404 with a signed magic-link URL the ingest Lambda sends back to the channel: *"Link your account at https://gateway.example.com/link?token=…"*. The user clicks, authenticates with Cognito in the browser, confirms the provider identity, and the gateway writes the `user_identities` row.
4. WebChat is already solved: its `provider` is `cognito` and `provider_user_id == cognito_sub`, so the link is implicit (no row needed, or we pre-create one on first login).

**Caching:** the resolver response is safe to cache for ~5 minutes in the ingest Lambda's in-memory dict — identity changes are rare.

### Agent access to credentials (runtime)

The chat agent must be able to use a credential **only for the user whose task it is currently processing**, and never accidentally one belonging to a different user. The safety invariant comes from **closure injection** (same pattern as the existing artifact store): the current task's `user_id` is hardcoded into the tool, so the tool can only ever resolve credentials for that one user.

**Default path: gateway-side proxy (no raw value in the agent process).**

1. Orchestrator receives a task from SQS. Task payload carries `user_id` (Cognito sub).
2. Orchestrator registers an MCP tool `http_request_with_credential(service, label?, method, url, headers?, body?)` as a closure that hardcodes `user_id`.
3. On invocation the tool posts to gateway `POST /internal/proxy-request` with `{user_id, service, label?, method, url, headers, body}`. The gateway:
   - Looks up the credential's `secret_arn` in `user_credentials`.
   - Fetches the secret value from Secrets Manager.
   - Injects it into the outbound request per `credential_type` (e.g. `Authorization: Bearer <token>` for `oauth_token`/`bearer`, `Authorization: Basic …` for `basic_auth`, or a service-specific header).
   - Makes the HTTP call, returns status + headers + body to the agent.
   - Updates `user_credentials.last_used_at` and writes an audit log row.
4. The agent only ever sees the API response, never the token.

**Escape hatch: raw-value tool for non-HTTP cases.**

For CLI tools and non-HTTP protocols, a second MCP tool `get_user_credential_raw(service, label?)` returns the raw value. Two mitigations:

- **LCM scrubber** runs before every DDB write. Values returned by this tool are registered with the scrubber so they're masked (`<<redacted:github:default>>`) in any message/tool-result text written to context history.
- **Audit log** records every raw-value retrieval (who, when, which credential). Usage dashboards can flag anomalies.

**File-materialization path: for `ssh_key`, `certificate`, `config_file` types.**

Returning an SSH private key as a string into an agent's message buffer is worse than the raw-value tool — the key ends up in LLM context and tool results. The file-materialization path avoids that by writing the credential to an isolated, task-scoped filesystem location the tool can reference by *path*, not by value.

1. Orchestrator registers MCP tool `materialize_user_credential(service, label?)` as a `user_id`-bound closure.
2. On invocation the tool posts to gateway `POST /internal/v1/credential-materialize` with `{user_id, agent_id, task_id, service, label?}`.
3. The gateway:
   - Looks up the credential, confirms the type is file-oriented.
   - Verifies the calling agent's manifest declares `permissions.credentials.materialize: [<service>]` (per-tool scoping, same pattern as raw-read).
   - Returns a short-lived, signed URL or token the agent runtime uses to write the file into a task-scoped tmpfs volume (ephemeral, per-run, auto-deleted on task completion).
   - Emits a provenance record.
4. The tool returns only the *path* (e.g. `/task/creds/github-deploy-key`) with the hinted mode applied. The value never enters the agent process's readable memory space outside the tmpfs write.

This is the right shape for:
- SSH keys used by CLI git / rsync / scp / ssh from inside an agent sandbox.
- X.509 client certs for mTLS to internal APIs.
- kubeconfig / AWS credentials config files for scoped admin CLIs.

It is **not** a substitute for the proxy path — if the credential is an HTTP token, the proxy is strictly better because the agent never even sees the path to a file containing the token.

**Three delivery paths, summarized:**

| Path | Credential types | Agent sees | Default? |
|---|---|---|---|
| HTTP proxy | `api_key`, `oauth_token`, `basic_auth`, `bearer` | API response body | ✅ Default for HTTP |
| File materialization | `ssh_key`, `certificate`, `config_file` | File path (not value) | ✅ Default for files |
| Raw-value | Any type, escape hatch only | The raw value | ❌ Opt-in, per-service, per-agent |

**IAM on the gateway service (not the agent):**
- `secretsmanager:GetSecretValue` on `arn:aws:secretsmanager:*:*:secret:adp/users/*/*`.
- Agent pods do **not** have direct Secrets Manager access — they must go through the gateway. This is a deliberate boundary.

### REST endpoints (gateway backend, under `/auth`)

Following the existing pattern in `modules/gateway/src/auth/routes.py`.

**User-facing (Cognito JWT required):**
- `GET /auth/identities` — list my linked identities.
- `POST /auth/identities/{provider}/link` — start a link (returns a flow-specific payload: OAuth URL or a code to paste in Slack).
- `DELETE /auth/identities/{id}` — unlink.
- `GET /auth/credentials` — list my credentials (metadata only, never values).
- `POST /auth/credentials` — register: `{service, label, credential_type, value, scopes?, expires_at?}`. Server writes to Secrets Manager, returns metadata.
- `DELETE /auth/credentials/{id}` — deletes row AND the Secrets Manager secret.
- `PATCH /auth/credentials/{id}` — update label/expires_at (not value — that requires re-register for audit clarity).

**Service-to-service (IAM-signed, internal only):**

All internal endpoints carry a `/v1/` prefix so they can evolve without breaking callers when they are promoted to harness contracts in v2.

- `POST /internal/v1/resolve-user` — `{provider, provider_user_id, invocation_context?}` → user context or 404+magic-link. `invocation_context` carries the channel adapter's correlation id for provenance. Conceptually part of the invocation surface.
- `GET /internal/v1/user-credentials` — `?user_id=<sub>&service=<svc>` → list of `{id, service, label, expires_at, last_used_at}`. Metadata only; **never returns `secret_arn` or values** — agents must go through the proxy.
- `POST /internal/v1/proxy-request` — `{user_id, agent_id, task_id, service, label?, method, url, headers?, body?}` → `{status, headers, body, provenance_id}`. Gateway injects the credential server-side and proxies the call; agent never touches the raw value. `agent_id` + `task_id` correlate the call into the provenance DAG so any produced artifact can trace back to the credential used.
- `POST /internal/v1/credential-raw-read` — `{user_id, agent_id, task_id, service, label?, purpose?}` → `{value, credential_type, provenance_id}`. Escape hatch for non-HTTP tooling; every call is audit-logged, its value registered with the LCM scrubber, and a provenance record written. Gated by a per-org feature flag *and* by the caller's agent manifest (`permissions.credentials.raw: [<service>]`) — an agent can only raw-read services listed in its manifest, regardless of the tenant flag. `purpose` is an optional free-text reason captured for audit.
- `POST /internal/v1/credential-materialize` — `{user_id, agent_id, task_id, service, label?}` → `{materialize_url, expires_at, provenance_id}`. Returns a short-lived signed URL the agent runtime uses to write the credential into a task-scoped tmpfs volume. Only file-oriented types (`ssh_key`, `certificate`, `config_file`) are accepted. Gated by agent manifest scope (`permissions.credentials.materialize: [<service>]`). The value never transits the agent process memory — it flows tmpfs-write-side only.

## Decisions

These were open questions in the draft; now resolved. Rationale is in the prior review notes — this section captures the final call only.

1. **Linking UX for non-Cognito users — hybrid auto-provision + magic link.**
   If the inbound channel identity (Slack workspace, GitHub org, WhatsApp business number) is pre-mapped to an ADP `org_id`, auto-provision a shadow `users` row scoped to that org; the user can claim/augment it later via magic-link login. If there is no mapping, reply in-channel with a magic link and refuse to enqueue the message until they link. Requires a small `channel_tenant_map` table (`provider`, `provider_scope_id`, `org_id`) populated by admins during tenant onboarding. Matches onboarding flows in Linear / Intercom / PagerDuty.

2. **Identity uniqueness — global `UNIQUE (provider, provider_user_id)`.**
   One human = one external identity. Enforce strictly. The contractor-across-two-orgs case is deferred; if it ever surfaces, the resolution is "unlink from old org first", not a schema change.

3. **Credential exposure to agents — proxy-first, raw as scoped escape hatch.**
   Default path: a gateway-side proxy tool `http_request_with_credential(service, label?, method, url, headers?, body?)` executes the call server-side using the credential, returns only the response body to the agent. The raw secret never crosses the agent process boundary — CloudTrail and the gateway's own audit log capture every use. For the narrow class of tools that genuinely need the raw value (shell CLI invocations, non-HTTP protocols), a separate `get_user_credential_raw(service, label?)` tool returns the value, and LCM persistence runs a scrubber that redacts anything matching known credential patterns before writing to DDB. The handle-and-inject intermediate option (opaque token swapped at HTTP-call time) is **explicitly rejected** — it combines the complexity of proxying with the leak surface of raw exposure.

4. **Credential registration — end users only in v1.**
   Only the owner of a `users` row can create, update, or read-metadata on their credentials. Admins can **delete** a user's credential as a break-glass operation (audited), but cannot create on-behalf-of. Admin-on-behalf-of is v2 and will require dual-control approval for SOC2/ISO defensibility.

5. **Services in v1 — `github`, `generic_bearer_token`, `generic_api_key`.**
   `service` stays a free-form string in the schema (no enum, no migration required to add a new provider). These three cover the immediate ask; Jira, Linear, and others will be enabled by UI/UX affordances later without touching the database. Slack user tokens are dropped from v1 — the Slack identity is already linked for inbound messaging, and user-scoped Slack tokens rarely unlock capabilities the bot token doesn't already have.

6. **Secret deletion — synchronous delete + nightly sweeper.**
   On user deletion (or credential delete), the gateway synchronously calls `secretsmanager:DeleteSecret` with retries and surfaces failure to the caller. A nightly Lambda sweeps `adp/users/<sub>/*` for any `sub` that no longer has a `users` row and cleans up orphans. This is the pattern standard at Stripe / Segment / most compliance-focused shops; pure async is not defensible under "right to be forgotten" scrutiny, pure sync leaks on partial failure.

7. **Non-HTTP credentials — file materialization, not raw return.**
   SSH keys, X.509 client certificates, and structured config files (kubeconfig, AWS config profiles) are supported in v1 via a third delivery path: `materialize_user_credential` writes the value to a task-scoped tmpfs volume and returns a file *path* to the agent. The value never enters the agent process's readable memory buffers and therefore cannot leak into LLM context or tool-result text. This is the right shape for CLI tools that expect a file on disk (ssh, kubectl, aws) and is preferred over `get_user_credential_raw` whenever the tool accepts a file. Raw-return remains available as the final escape hatch for protocols that genuinely need the value as a string.

8. **Magic-link token shape — signed, TTL 15 min, single-use, binding to `(provider, provider_user_id, channel_context)`.**
   Tokens are JWT-like with a gateway-controlled signing key; payload carries `{provider, provider_user_id, channel_context, nonce, exp}` where `channel_context` is the Slack team/channel/thread or GitHub installation id the message came from. A token consumed for a provider identity different from the one it was issued for is rejected. Single-use enforced via a short-lived DDB entry keyed on `nonce`. If a Cognito session at link time belongs to an `org_id` different from the `channel_tenant_map` mapping for that provider scope, the link is rejected with a clear error rather than silently binding to whichever org the clicker is in. Replay in a shared Slack channel is blocked by the `channel_context` binding — a click from a different user/thread than the token was issued to fails validation.

## Rollout plan

1. **Schema + migration (gateway backend).** Alembic `004_user_identities_and_credentials.py` adds `user_identities`, `user_credentials`, and `channel_tenant_map` tables. (Updated from 003 — see delta notes at top.)
2. **Secrets Manager write path + IAM.** Gateway service role gets `secretsmanager:CreateSecret / GetSecretValue / UpdateSecret / DeleteSecret` on `adp/users/*/*`. Agent pods are **not** granted Secrets Manager access — the gateway is the boundary.
3. **User-facing endpoints.** `/auth/credentials` CRUD and `/auth/identities` CRUD under `modules/gateway/src/auth/routes.py`. Values are written straight to Secrets Manager; the DB row holds only the ARN.
4. **Internal service-to-service endpoints.** `/internal/v1/resolve-user`, `/internal/v1/user-credentials` (metadata only), `/internal/v1/proxy-request` (gateway-side HTTP proxy), `/internal/v1/credential-raw-read` (escape hatch, per-org feature-flag gated), `/internal/v1/credential-materialize` (file materialization for `ssh_key` / `certificate` / `config_file` types). IAM-signed and restricted to ingest Lambda + agent service account ARNs.
5. **LCM scrubber.** Extend the existing LCM persistence layer (`modules/agent-factory/agent/src/complex-task-chat/context-store.ts`) with a registerable scrubber that masks raw credential values before any DDB write. Populated by `get_user_credential_raw` on read so masking is automatic for the remainder of the task.
6. **Ingest Lambda integration.** In `modules/agent-factory/gateway/lambdas/ingest/handler.py`, replace any remaining fallback identity logic with a call to `/internal/resolve-user` at message-parse time. Webchat already resolves via Cognito `$connect`-persisted claims; Slack/GitHub/WhatsApp adapters switch to the new resolver.
7. **Agent MCP tools.** Register `http_request_with_credential` (proxy, default for HTTP types), `materialize_user_credential` (default for file types), and `get_user_credential_raw` (escape hatch) in `modules/agent-factory/agent/src/complex-task-chat/tools.ts`. Closure-inject `user_id` from the current task so the tools can only act for that user. Task runtime mounts a per-task tmpfs volume at `/task/creds/` for materialized credentials; volume is auto-deleted on task completion.
8. **Slack adapter linking flow.** When `/internal/resolve-user` returns 404, post a magic-link message in-channel ("Link your account at https://…") instead of proceeding.
9. **Admin UI.** React pages under `modules/gateway/frontend/` for `/settings/identities` (linked accounts, add/remove) and `/settings/credentials` (per-service secrets, add/rotate/delete).
10. **Nightly sweeper Lambda.** Scheduled job that deletes `adp/users/<sub>/*` secrets whose `sub` no longer has a `users` row. Backstop for failed synchronous deletes.

Steps 1–6 deliver the identity layer (resolver + linking + credentials API + secret storage + scrubber). Steps 7–10 depend on that layer being live: `ENABLE_USER_IDENTITIES` must be on before `ENABLE_USER_CREDENTIALS` produces useful agent behavior. The feature flags are independent but the natural rollout is identity first, credentials second, adapters and UI third.

## Out of scope / deferred

- OAuth app publishing (vs. user-pasted tokens).
- Credential rotation.
- Team-shared credentials (e.g. "the team's deploy bot PAT").
- App-scoped / tenant-scoped credentials (e.g. an org's VirusTotal Enterprise key used by a threat-research agent, a SageMaker execution role for an ML agent). Schema relaxation from `user_id NOT NULL` plus proxy-path changes to accept an app identity instead of a user identity.
- Auditing secret reads per-tool-call beyond what CloudTrail gives us.
- Cross-region secret replication.
- Promotion of the `/internal/v1/*` endpoints and the two MCP tools into `modules/harness/` as versioned harness contracts consumable by any app.
- HITL-gated raw-reads in high-trust tenants (first raw-read in a task opens an approval ticket before returning the value).

## v1.5 follow-ups for multi-team SaaS self-serve

v1 targets hand-onboarded tenants (small number of teams, admin pre-maps channels). These items become material as we onboard more self-serve teams:

1. **App-scoped / team-scoped credentials.** The `user_id NOT NULL` constraint on `user_credentials` is a v1 simplification — expect teams to want shared credentials within days of onboarding (team deploy bot PAT, team-owned GitHub App install token, shared internal API keys). Relax to `(user_id XOR team_id XOR org_id) NOT NULL` with a CHECK constraint, extend the resolver to walk user → team → org, and add an admin UI for team/org-scoped credential management.
2. **Self-serve `channel_tenant_map` creation.** Today the doc assumes admins pre-populate this at tenant onboarding. For self-serve SaaS, a new team admin needs to link their own Slack workspace / GitHub org during onboarding — without an ops team round-trip. Approach: OAuth flow where the team admin installs the ADP Slack app in their workspace; the install callback populates `channel_tenant_map` with the `team_id` → `org_id` binding, gated by the admin's Cognito session org.
3. **Cross-org user identity.** v1's `(provider, provider_user_id)` uniqueness rejects the consultant / contractor case (same GitHub account used across two client orgs). Relax uniqueness and add a separate `user_identity_memberships` table mapping one identity to N `(user_id, org_id)` pairs, with the inbound resolver asking the user to disambiguate ("You're linked to two orgs — which one is this message for?") when the channel context is ambiguous.
4. **Domain-based auto-team-assignment.** Optional convenience: a new Cognito signup with `@company.com` email can auto-join the `company.com` team if the admin has enabled domain-whitelist signup. Removes the per-user invite step for larger teams. Requires a `team_domain_claims` table and a `post-confirmation` Cognito Lambda.

None of these block v1. Each becomes a separate issue when the concrete customer pain arrives — don't build them preemptively.
