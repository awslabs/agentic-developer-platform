# Design Note: GitHub-Integration Consolidation

> **Status**: Design note (spike output)
> **Author**: @agent-architect
> **Date**: 2026-07-10
> **Issue**: #3534
> **Mode**: Per-issue (spike — deliverable is this document, not code)

---

## 1. The Integration Map

### 1.1 Credential Topology

ADP's GitHub integration uses **one GitHub App** serving two purposes (agent pipeline + user login) with credentials distributed across four storage tiers:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        GITHUB APP (adp-agent-platform)                       │
│                                                                             │
│  Identity: App ID + Private Key (RS256)                                      │
│  OAuth:    Client ID + Client Secret  (user authorization)                   │
│  Webhook:  Webhook Secret (HMAC-SHA256)                                      │
└────────────┬────────────────────────┬──────────────────────┬────────────────┘
             │                        │                      │
    ┌────────▼────────┐    ┌──────────▼─────────┐   ┌───────▼────────┐
    │  AGENT PIPELINE │    │    USER LOGIN       │   │  WEBHOOK HMAC  │
    │  (JWT → install │    │  (OAuth code flow)  │   │  (signature    │
    │   token → API)  │    │                     │   │   validation)  │
    └─────────────────┘    └─────────────────────┘   └────────────────┘
```

### 1.2 Secret Storage Map

| Secret | Secrets Manager Path | Payload Shape | Written By | Read By | Chain Link |
|--------|---------------------|---------------|------------|---------|------------|
| App ID | `adp/<env>/github-app/adp-agent-platform-id` | Plain string (numeric) | Manifest callback / register script / BYO manual | `GitHubAppCredsProvider` (gateway pods), webhook Lambda (via tenant secret) | App JWT minting |
| Private Key | `adp/<env>/github-app/adp-agent-platform-key` | PEM string | Manifest callback / register script / BYO manual | `GitHubAppCredsProvider` (gateway pods), webhook Lambda (via tenant secret) | App JWT minting |
| App Metadata | `adp/<env>/github-app/adp-agent-platform-meta` | JSON: `{app_id, app_slug, client_id, client_secret, webhook_secret}` | Manifest callback / register script / BYO manual | `GitHubAppCredsProvider` (slug resolution), `get_app_status()` | Install URL generation, status display |
| OAuth Creds | `adp/<env>/cognito/github-oauth-credentials` | JSON: `{client_id, client_secret}` | `_store_app_credentials()` write-through, `wire-github-app.sh` | Auth broker Lambda (`_get_github_oauth_creds()`) | GitHub OAuth code exchange (user login) |
| Webhook Secret | `adp/<env>/webhook-ingress/github-webhook-secret` | Plain string (hex) | Terraform placeholder, then `_store_app_credentials()` write-through or `register-github-app.sh` | Webhook ingress Lambda (`_resolve_webhook_secret()`) | HMAC-SHA256 signature validation |
| Per-tenant copy | `adp/<env>/tenants/<org_id>/github-app` | JSON: `{app_id, private_key}` | `seed_tenant_github_app_secret()` at install-callback, Lambda auto-provision | `resolve_tenant_app_credentials()` in gateway + agent-context | Per-tenant installation token minting |
| Per-app slug path | `adp/<env>/github-app/<slug>-{id,key,meta}` | Same as singleton | `_store_app_credentials()` (parallel write) | `wire-github-app.sh` reads `<slug>-id` | Multi-App registry seed (#2952 D11) |

### 1.3 Consumer Map (which component reads which secret)

| Component | Secret(s) Read | Purpose |
|-----------|----------------|---------|
| **Gateway pods** (`GitHubAppCredsProvider`) | `-id`, `-key`, `-meta` (SM) → BG_ env fallback | Mint App JWT for install-flow API calls, install URL slug |
| **Auth broker Lambda** | `cognito/github-oauth-credentials` (SM) | OAuth code exchange → Cognito session |
| **Webhook ingress Lambda** | `webhook-ingress/github-webhook-secret` (SM ARN via env) | HMAC signature validation of incoming deliveries |
| **Agent worker pods** (`lib/github_token.py`) | Per-tenant `tenants/<org>/github-app` (via gateway credential client) | Mint installation token for repo clone/push/check-runs |
| **Agent-context service** (`github_app_service.py`) | Per-tenant `tenants/<org>/github-app` (SM direct) | Mint installation token for repo listing, registration |
| **Gateway knowledge** (`github_app_service.py`) | Per-tenant `tenants/<org>/github-app` (SM direct) | Same as agent-context (relocated copy, Issue #2045) |
| **Identity resolver** (webhook Lambda) | Reads DDB `identity-index` (not SM directly) | Maps `installation_id → tenant_id` for routing |

### 1.4 The Four-Link Runtime Chain

```
Link 1: App Registration       Link 2: Org Installation
  ┌──────────────────┐           ┌──────────────────────┐
  │ Manifest flow OR │           │ GitHub redirects to   │
  │ register script  │ ────────► │ setup_url with        │
  │ OR BYO manual    │           │ ?installation_id=&    │
  │                  │           │  setup_action=&state= │
  └──────────────────┘           └──────────┬───────────┘
                                            │
Link 3: Tenant Attachment       Link 4: User-to-Tenant Match
  ┌──────────────────────┐        ┌─────────────────────────┐
  │ install_callback()   │        │ Login flow: broker →     │
  │ consumes nonce,      │ ──────►│ onboarding handler →     │
  │ writes DDB + PG +   │        │ _find_matching_tenant →  │
  │ per-tenant secret    │        │ org membership check     │
  └──────────────────────┘        └─────────────────────────┘
```

### 1.5 Failure Mode Table

| Link | Failure | Symptom | Root Cause | Resolution |
|------|---------|---------|------------|------------|
| 1 | Missing `setup_url` on App | Install succeeds on GitHub but Connections UI never shows it | GitHub leaves user on github.com, never calls back to ADP | Set Setup URL + "Redirect on update" (#2823) |
| 1 | Placeholder still in SM | `_is_placeholder()` returns true → 503 on install-start | Terraform seeds `PLACEHOLDER_SET_BY_REGISTER_SCRIPT`, never overwritten | Complete registration flow (any path) |
| 2 | Private App + external org tries to install | 404 on the App's install page | Private apps are only installable by the owning org | Set App to public, or create per-org Apps (#3085) |
| 2 | Uncovered repo (selected-repos install) | Agent receives webhook but installation token lacks access | Repo not in the installation's selected set | Re-configure installation to include the repo |
| 3 | Nonce expired (>15 min between install-start and callback) | `TokenExpiredError` on callback | Slow operator, browser-tab left open | Retry install-start to get a fresh nonce |
| 3 | Cross-tenant conflict | `PermissionError: already connected to another tenant` | GitHub org's numeric ID already in `channel_tenant_map` for different `org_id` | Contact support / delete stale mapping |
| 3 | DDB write fails (non-fatal) | `webhook routing will fail until backfilled` log warning | DDB throttling or IAM permission missing | Backfill via `_write_installation_identity_index()` retry or manual DDB put |
| 4 | `check_org_membership: 302` | Login succeeds but user doesn't match to org tenant | App lacks `Organization members: Read` permission | Add permission in GitHub App settings |
| 4 | Pending-approval installation | Installation exists but org hasn't approved new permissions | Permission upgrade sent consent prompt | Org admin approves in GitHub org settings |
| 4 | OAuth secret is placeholder | Broker returns 500 / "Sign in with GitHub" button hidden | `_store_app_credentials()` write-through to `cognito/github-oauth-credentials` failed | Manually wire via `wire-github-app.sh --client-secret` |

---

## 2. Single Source of Truth for App Shape

### 2.1 Current State — Three Provisioning Paths

| Path | Permission Set | Events | Storage Convention | Status |
|------|---------------|--------|-------------------|--------|
| **Manifest flow** (`_build_app_manifest` in `service.py:1294`) | contents:write, issues:write, pull_requests:write, checks:write, metadata:read | issues, issue_comment, pull_request, pull_request_review, pull_request_review_comment, label | `adp/<env>/github-app/adp-agent-platform-*` | **Active, primary** |
| **Register script** (`register-github-app.sh`) | Operator manually configures in browser; docs say "match the manifest" | Operator-selected | Same `adp/<env>/github-app/adp-agent-platform-*` | Active, CLI fallback |
| **Legacy `create-github-apps.sh`** | contents:write, issues:write, pull_requests:write, checks:write, **workflows:write, administration:write, actions:write**, metadata:read, members:read, organization_projects:write | issues, issue_comment, pull_request | `adp/<org>/gh-app-{role}-{id,key}` (different path!) | **Superseded** — creates 3 role-split Apps for the ARC runner path |

### 2.2 Permission Drift Analysis

The manifest flow is intentionally minimal (5 repo permissions, no org permissions) to minimize enterprise-admin approval friction. The legacy script requests 8 repo + 2 org permissions because it was designed for ARC runners that need Administration:write to self-register.

The register script has **no programmatic permission enforcement** — it opens a browser and relies on documentation (`docs/bring-your-own-github-app.md`) to tell the operator what to set. This means the operator can under- or over-provision without detection until something fails at runtime.

### 2.3 Recommendation: Manifest is Authoritative; Legacy Script Deleted; Register Script Gets a Drift Gate

**Decision:**

1. **`_build_app_manifest()` is the single source of truth** for the base permission set (the "core tier" in `bring-your-own-github-app.md` §2.2). All other paths must either generate from it or validate against it.

2. **Delete `platform/scripts/create-github-apps.sh`** — it creates a completely different topology (3 Apps, different SM paths, role-split), targets the superseded ARC-only path, and its permission set diverges. The ARC runner path that still uses per-role Apps is documented in `modules/agent-factory/SETUP-GUIDE.md` as a separate, complementary execution model; operators following that path already know to set up Apps manually.

3. **`register-github-app.sh` gains a post-registration validation step** — after credentials are stored, call `GET /app` (App JWT → 200 response includes `permissions` + `events`), diff against the manifest's `default_permissions` and `default_events`, and emit warnings for missing/insufficient entries. This is the "assert in CI" option but triggered at registration time rather than as a separate workflow. Rationale: a CI drift gate requires the private key in CI; a registration-time check already holds it.

4. **BYO manual flow (`register_app_manual`)** already performs this validation (see `service.py:1843-1869`). No change needed — it already compares live App permissions against the expected set and returns warnings.

5. **`bring-your-own-github-app.md`** should declare that it derives its permission table from `_build_app_manifest()` and add a note that the manifest is authoritative. PR #3523 already aligned the doc; add a single sentence citing the code as source.

**Sequencing:** File a single issue to delete `create-github-apps.sh` and add the validation step to `register-github-app.sh`. Low-risk, no behavioral change to deployed systems.

---

## 3. The Fusion Decision

### 3.1 Context

Since #2607, ADP uses **one GitHub App** for both:
- **Agent pipeline**: webhooks → HMAC validation → identity resolution → SQS → agent worker → installation token → repo operations + check runs
- **User login**: App's OAuth user-authorization feature → auth broker → Cognito session

This means every App registration yields two credential pairs:
- **App identity**: App ID + Private Key (JWT signing for installation tokens)
- **OAuth identity**: Client ID + Client Secret (OAuth code flow for user login)

### 3.2 Evaluation Criteria

| Criterion | Keep Fused (one App) | Split (agent App + login App) |
|-----------|---------------------|-------------------------------|
| **Enterprise-admin approval friction** | One approval request, one installation consent prompt | Two approval requests; the login App needs only `user:email` but admins still see two Apps in their list |
| **Private-App login-404 (#3085)** | A private App's OAuth flow works for users in the owning org but 404s for outsiders. Since the webhook path also requires the App be installed on the target org's repos, this is the same audience — no extra restriction | If split, the login App could be public (lower friction for multi-org) while the agent App stays private (tighter blast radius). But: a public login App with no permissions is also achievable as a standard OAuth App, not a GitHub App |
| **Credential-rotation blast radius** | Private key and client secret are independent credentials: rotating the private key affects only agent operations (JWT signing); rotating the client secret affects only login (OAuth). Only deleting the App entirely is a shared blast surface (see §3.3 rationale 3) | Independent rotation: agent key rotation doesn't affect login; client_secret rotation doesn't affect agents |
| **Multi-App registry (#2985)** | The registry stores per-org App credentials already (via per-tenant secret `adp/<env>/tenants/<org>/github-app`). Fusion means each registered App is a full App carrying both concerns. Complexity: when an org brings its own App, it must also wire OAuth if it wants GitHub login to work | If split, the agent registry holds per-org agent Apps; the login App is global (one per deployment) and never per-org. Simpler mental model for BYO: "bring your agent App, login just works" |
| **Setup complexity** | One App, one set of docs, one creation flow. BUT: requires explaining "configure both halves" (the most common BYO mistake per #3360 warnings) | Two Apps, two creation flows, two docs. More total steps but each is simpler and self-contained |
| **Code complexity** | Single credential provider (`GitHubAppCredsProvider`), single SM write in register callback. Already built and working | New module: a "login provider" reading a separate secret path. Broker Lambda needs updating. Manifest flow changes. More code |

### 3.3 Recommendation: Keep Fused — with an Explicit "Login Optional" Stance

**Keep the single dual-purpose App.** The fusion is already implemented, documented, and working. The benefits of splitting are marginal and the migration cost is non-trivial.

**Rationale:**

1. **Enterprise friction is the dominant cost.** GitHub enterprise admins review App installations. One App = one review. Two Apps = two reviews, possibly by different admin teams (security reviews the agent permissions; identity team reviews the OAuth flow). The enterprise sales motion is simpler with one App.

2. **The #3085 login-404 problem is an App-visibility issue, not a fusion issue.** The fix is making the App public for multi-org deployments (already implemented in the manifest builder, `public` param, and `register-github-app.sh --visibility`). A public App's OAuth flow works for everyone regardless of installation status.

3. **Credential rotation blast radius is acceptable.** In practice:
   - Private key rotation affects agent operations (JWT signing). Login uses `client_secret`, a separate credential. They don't share a secret value.
   - Client secret rotation affects login only. Agent operations use the private key.
   - The only shared blast surface is "delete the App entirely" — which breaks everything regardless of fusion.

4. **Multi-App registry (#2985) is not blocked by fusion.** The per-tenant secret (`adp/<env>/tenants/<org>/github-app`) already carries only `{app_id, private_key}` — it's agent-only. The OAuth credentials remain global (one login path per deployment, not per-tenant). This separation already exists in code without requiring a split at the GitHub App level.

5. **The "configure both halves" pain is solvable with better UX, not App topology.**
   - The manifest flow already wires both halves atomically (GitHub returns client_id + client_secret in the conversions response; `_store_app_credentials` writes both).
   - The BYO path now validates and warns when OAuth creds are missing (`register_app_manual`, `service.py:1876-1880`).
   - A split would not eliminate the "forgot to configure" class of bugs — it would just change what gets forgotten.

**Caveat (reviewer decision point):** If ADP's multi-org story evolves to a model where each customer org manages its own GitHub App AND its own login identity (e.g., enterprise SSO per org rather than ADP-global Cognito), then a split becomes attractive. That's the #3074 invisible-tenancy world. Until then, fusion is correct.

---

## 4. Consolidation Sequencing

### 4.1 Dependency Graph

```
                ┌─────────────────────────────────────────┐
                │  THIS SPIKE (#3534) — design note       │
                │  Delivers: integration map, decisions    │
                └────────┬────────────────────────────────┘
                         │ (informational dependency)
         ┌───────────────┼───────────────────────────┐
         │               │                           │
         ▼               ▼                           ▼
  ┌──────────────┐ ┌───────────────┐  ┌─────────────────────────┐
  │ App-shape    │ │ #3134 trigger │  │ #3136 PAT-execution     │
  │ cleanup      │ │ policy (per-  │  │ hybrid (children C1-C14)│
  │ (new issue)  │ │ install)      │  │                         │
  └──────┬───────┘ └──────┬────────┘  └────────────┬────────────┘
         │                │                         │
         │         (no dependency between           │
         │          3134 and 3136)                   │
         │                │                         │
         ▼                ▼                         ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ #2985 Multi-App Registry                                      │
  │ (requires: trigger policy model settled, PAT model settled,   │
  │  App-shape source of truth locked)                            │
  └──────────────────────────────────────────────────────────────┘
```

### 4.2 Recommended Sequence

| Order | Issue | What It Does | Why This Order | Vocabulary Retired |
|-------|-------|-------------|----------------|-------------------|
| 1 | **App-shape cleanup** (new, ~1 sprint) | Delete `create-github-apps.sh`; add validation to `register-github-app.sh`; add "manifest is authoritative" callout to BYO doc | Zero behavioral change, pure cleanup. Removes the "which script do I use?" confusion. Prerequisite for #2985 (registry must know what shape an App should be) | "3-App topology", `adp/<org>/gh-app-*` secret convention, "adp-agent-{dev,pm,ops}" App names |
| 2 | **#3134 — Trigger policy** (per-installation) | Admin sets who can trigger agents per installation (`trigger_policy` + `min_author_association` on DDB identity row) | Already partially shipped (DDB schema + Lambda read path exist). Standalone — doesn't depend on PAT or multi-App. Lands vocabulary that #2985 reads from at registration time | None retired; adds `trigger_policy` vocabulary (intentional, fills a gap) |
| 3 | **#3136 — PAT-execution hybrid** (children C1-C14) | Users supply a GitHub PAT as an alternative to App installation tokens for agent repo operations | Depends on the fusion decision (this note: keep fused ∴ PATs are a per-user credential layer ON TOP of the App, not a replacement). Can land in parallel with #3134 since they touch different runtime paths (PAT is credential-resolution; trigger policy is ingress-filter) | Retires nothing directly, but its "credential source" enum subsumes the current single-path assumption |
| 4 | **#2985 — Multi-App registry** | Admin registers multiple GitHub Apps (per-org or per-purpose); install flow routes to the correct one | Depends on: App-shape cleanup (registry validates against the manifest), trigger policy (#3134 model is per-installation which naturally extends to per-App), PAT model (#3136 clarifies what "credential source" means so the registry can say "this install uses App tokens" vs "this user uses a PAT"). This is the vocabulary-unifying issue | Retires "global singleton App" assumption; subsumes the per-app SM paths from #2952 D11 into a first-class registry table |

### 4.3 What Gets Retired vs Added

| Current Vocabulary | After Sequence | Disposition |
|-------------------|----------------|-------------|
| `adp/<org>/gh-app-{role}-{id,key}` (3-App paths) | Deleted in step 1 | Script deletion; secrets in SM survive until operator cleans up (protected by `force-delete-secrets.sh` exclusion pattern) |
| `create-github-apps.sh` | Deleted in step 1 | N/A |
| "BG_GITHUB_APP_*" env-var fallback | Deprecated after step 4; retained for backward compat | Mark as deprecated in settings schema; remove in a future major |
| Singleton `adp/<env>/github-app/adp-agent-platform-*` | Becomes "default App" in the registry (step 4) | Path unchanged; semantics shift from "the only App" to "the primary App" |
| `trigger_policy` (DDB attr) | Added in step 2, read by step 4 | New vocabulary (intentional) |
| PAT credential source | Added in step 3, read by step 4 | New vocabulary (intentional) |
| Multi-App registry table (Postgres) | Added in step 4 | New vocabulary; subsumes `per_app_*_path` dual-writes |

### 4.4 Non-Sequenced (Explicitly Out of Scope)

- **#3074 invisible-tenancy** — per-action tenant resolution. Independent design space; doesn't interact with credential topology.
- **#3136 C1-C14 individual children** — scoped within #3136; this note sequences #3136 as a unit, not its children.
- **#2985 registry UI/UX** — ships with #2985; no separate issue needed.

---

## 5. Consistency Check Against `bring-your-own-github-app.md`

The BYO doc (as merged in PR #3523) is **consistent** with this integration map, with one minor gap:

- **Gap**: The BYO doc's §4 ("Store credentials in ADP") references `wire-github-app.sh` but does not mention that `register_app_manual` (the API endpoint at `POST /api/admin/connections/github/app/register-manual`) performs the same wiring via `_store_app_credentials()`. The doc should note that the UI manual-registration path is equivalent and preferred when a browser session is available.

- **No contradictions**: The permission table in the BYO doc (§2.2) matches `_build_app_manifest()`'s `default_permissions` exactly (verified: contents:write, issues:write, pull_requests:write, checks:write, metadata:read). The tiered structure (core → optional workflows → optional ARC) is correctly documented as beyond-manifest extensions, not divergences.

---

## 6. Open Questions for Reviewer

1. **Delete timing for `create-github-apps.sh`**: The script is referenced in `CLAUDE.md` under "Key Files Reference" and in `modules/agent-factory/SETUP-GUIDE.md`. Should the child issue include updating those references, or is the SETUP-GUIDE still correct for operators choosing the ARC path?

2. **Multi-App registry schema location**: #2985 deferred the registry table design. Should it live in gateway Postgres (near `channel_tenant_map`) or in a new DynamoDB table (near `identity-index`)? This note's position: Postgres — it's admin-managed, relational (App ↔ installations ↔ tenants), and the gateway admin UI queries it. Flag for the #2985 design phase.

---

## References

- #2607 — One-App fusion (agent + login)
- #3354 / PR #3360 — BYO recovery / manual registration
- #2823 — Setup URL fix
- #3085 — App visibility incident (private App + external user)
- #2985 — Multi-App registry (deferred)
- #3136 — PAT-execution hybrid
- #3134 — Per-installation trigger policy
- #3068 / #3074 — Tenant model (v1 / invisible-tenancy)
- PR #3523 — Permission tiering doc fix
- `docs/design-notes/2951-github-org-to-adp-tenant.md` — Org-to-tenant model (decisions D1-D12)
- `docs/design-notes/3136-pat-onboarding-flow.md` — PAT onboarding design
