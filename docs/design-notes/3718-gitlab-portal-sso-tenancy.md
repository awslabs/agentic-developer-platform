# Design Note: GitLab Portal-SSO + Tenancy Architecture

**Issue**: #3718 (spike for EPIC #3717)  
**Author**: @agent-architect  
**Date**: 2026-07-11 (revised 2026-07-12)  
**Status**: PROPOSED (rev 2 — owner decision applied)  
**Parent**: #3717 (GitLab Portal SSO EPIC) > #3320 (Source Control)

---

## Summary

This design note settles three open architecture decisions for integrating
portal-initiated SSO from the ADP dashboard into hosted GitLab CE, defines
the JWT handoff contract, and provides the child-issue breakdown for
EPIC #3717.

**Revision note (2026-07-12):** Owner ruled on Decision 1 — shared CE instance
with group-per-tenant (Option A) is the v1 architecture. Instance-per-tenant
(Option B) is documented as a future migration target. This revision re-scopes
tenancy enforcement, threat model, provisioning flow, and child-issue waves
accordingly.

---

## Decision 1: Tenancy Model

### Options Evaluated

| Option | Isolation | Cost per tenant | CVE blast radius | Operational complexity |
|--------|-----------|-----------------|------------------|------------------------|
| **A. Shared CE instance, group-per-tenant** | Soft (same Postgres, same admin plane) | ~$0 marginal (shared EC2) | All tenants affected | Low infra, high app-level enforcement |
| **B. Instance-per-tenant-account** | Hard (separate EC2, separate data) | ~$170/mo/tenant (t3.large + EBS) | Single tenant | High infra (N instances), but stampable via existing TF module |

### Recommendation: **Option A — Shared CE instance, group-per-tenant** (owner decision)

**Rationale:**

1. **Operational simplicity for v1.** A single GitLab CE instance per environment
   eliminates the N-instance management burden (patching, backups, monitoring
   multiplied by tenant count). The existing `modules/source-control/gitlab/infra/`
   module is already structured for a single instance (`ec2.tf:19`:
   `user_data_replace_on_change = false`; no `tenant_id` variable in
   `variables.tf`).

2. **Group-per-tenant provides adequate soft isolation for v1.** GitLab's group
   system supports private visibility, member-only access, and per-group settings.
   Combined with the enforcement inventory below, this provides a usable tenant
   boundary without EE licensing.

3. **App-level enforcement is now load-bearing scope.** With a shared instance,
   tenant isolation is NOT inherited from infrastructure — it MUST be explicitly
   enforced at the application layer. The enforcement inventory (next section) is
   the tenant boundary.

4. **Design does NOT foreclose instance-per-tenant migration.** The JWT includes
   `tenant_id`; the gateway resolves GitLab URL from SSM per-environment (trivially
   extensible to per-tenant). Secret paths use an `{environment}` dimension that
   can gain a `{tenant_id}` dimension when the migration occurs.

### Future: Instance-per-tenant migration path

Instance-per-tenant (Option B) remains the documented long-term target when:
- Tenant count is low enough that $170/mo/tenant is acceptable
- CVE blast radius isolation becomes a hard requirement
- GitLab EE licensing is not viable

**Migration mechanics (preserved for future reference):**
- State key: `{env}/gitlab/{tenant_id}/terraform.tfstate`
- Secret path: `adp/{env}/tenants/{tenant_id}/gitlab-jwt-private-key`
- SSM discovery: `/adp/{env}/tenants/{tenant_id}/gitlab/url`
- The existing TF module is stampable by adding a `tenant_id` variable
- JWT `aud` claim already includes `tenant_id` — per-tenant audience validation
  requires only a config change on each instance
- Per-tenant key pairs (Decision 3) become the default at that point

**Design constraints that preserve this migration path:**
- JWT always includes `tenant_id` claim (even though v1 shared instance ignores it
  for routing)
- Gateway SSO code resolves GitLab URL from SSM (not hardcoded) — adding a
  tenant dimension to the SSM path is a one-line change
- Group naming convention (`tenant-{tenant_id}`) is stable across both models

### v1 Implications

- **State key**: `{env}/gitlab/terraform.tfstate` (single instance)
- **Secret paths**: `adp/{env}/gitlab-jwt-private-key` (per-environment, not per-tenant)
- **SSM discovery**: `/adp/{env}/gitlab/url` (existing; `ssm.tf:44`)
- **Provisioning**: Group creation is a tenant-onboarding step (reconciler creates
  top-level group when tenant is added to ADP)

---

## Shared-Instance Tenancy Enforcement Inventory

With a shared CE instance, the following controls ARE the tenant boundary. Every
item is load-bearing — removing any one creates a cross-tenant data path.

### Top-Level Group Per Tenant

| Control | Setting | Enforcement point |
|---------|---------|-------------------|
| One top-level group per tenant | `tenant-{tenant_id}` | Created by reconciler at tenant onboarding |
| Group visibility | `private` | Group setting (API: `visibility: "private"`) |
| Project default visibility | `private` | Instance-wide (`default_project_visibility`) + group-level override |
| Subgroup creation restricted | Only group owners (reconciler bot) can create subgroups | Group setting: `subgroup_creation_level: "owner"` |

### Visibility + Discovery Lockdown

| Control | Setting | Why |
|---------|---------|-----|
| `restricted_visibility_levels` | `['public']` | No public projects on the shared instance |
| `default_project_visibility` | `private` | Belt + suspenders with group-level enforcement |
| `default_group_visibility` | `private` | Top-level groups invisible to non-members |
| `user_default_external` | `true` | JIT-created users are external by default; cannot see internal projects/groups |
| Restricted user directory | `admin_mode = true` + restricted `/users` API | Prevents tenant A users from enumerating tenant B users |

### Permission Lockdown

| Control | Setting | Why |
|---------|---------|-----|
| `can_create_group` | `false` (instance-wide) | Only reconciler/admin creates groups; prevents namespace pollution and cross-tenant group creation |
| `signup_enabled` | `false` | No local signups; all users arrive via SSO |
| `password_authentication_enabled_for_web` | `false` | Force SSO; no local passwords (except root break-glass) |
| `password_authentication_enabled_for_git` | `false` | Force token-based git auth (PAT or deploy keys scoped to group) |

### Per-Tenant Agent Bot Users + Tokens

**Current state** (verified `secrets.tf:99`): Single instance-wide
`adp/${environment}/gitlab-api-token` used by all agent workers
(`entrypoint.py:488-489`).

**Target state:** Per-tenant bot users with group-scoped tokens:

| Resource | Path | Scope | Who uses |
|----------|------|-------|----------|
| Bot user | `adp-bot-{tenant_id}` (GitLab user) | Owner of `tenant-{tenant_id}` group | Agent workers for that tenant |
| Group access token | `adp/{env}/tenants/{tenant_id}/gitlab-group-token` (Secrets Manager) | `api` scope, limited to group | Agent workers (resolved by `tenant_id` from job context) |
| Instance admin token | `adp/{env}/gitlab-admin-token` (Secrets Manager) | `admin` scope | Reconciler only (user block/unblock, group creation) |

**Migration from instance-wide token:**
1. Reconciler creates per-tenant bot users + group tokens during onboarding
2. Agent worker entrypoint resolves token by `tenant_id` (already has tenant context
   from webhook-ingress identity resolution)
3. Instance-wide `adp/{env}/gitlab-api-token` retained temporarily for backward
   compat; deprecated once all tenants have bot tokens
4. Admin token restricted to reconciler IAM role only

### Tenant-Group Membership as the ONLY Access Grant

Users access GitLab resources ONLY through their tenant group membership:
- JIT-created users start with ZERO group memberships (external + blocked)
- Reconciler adds user to `tenant-{tenant_id}` group (as Reporter or Developer)
- User can only see/access projects within their tenant group
- No instance-wide project discovery (external users + private groups = invisible)
- Removing user from group = instant loss of access to all tenant projects

---

## Decision 2: Provisioning Authority

### Options Evaluated

| Option | Mechanism | Latency on first login | Failure mode | Complexity |
|--------|-----------|------------------------|--------------|------------|
| **A. JIT off JWT claims** | GitLab `jwt` provider auto-creates user from claims | 0 (GitLab handles) | Claim-as-write-path risk; `block_auto_created_users=false` required | Lowest |
| **B. Gateway-side provisioning call** | Gateway calls GitLab API to create user + group before redirect | +200-500ms per SSO click | Synchronous dep on GitLab availability; SSO fails if GitLab is down | Medium |
| **C. Async reconciler** | Background worker syncs user roster to GitLab periodically | 0 at click time (user pre-exists) | Eventual consistency; user may not exist on first login if reconciler hasn't run | Highest |

### Recommendation: **Option A — JIT off JWT claims** (with hardening)

**Rationale:**

1. **Simplicity.** GitLab's `jwt` OmniAuth provider natively supports JIT user
   creation from claims. The gateway's only job is to mint a valid JWT and
   redirect — no API client, no retry logic, no availability coupling.

2. **The GitHub auth broker precedent.** The existing GitHub SSO flow
   (`cognito_provisioner.py`) similarly delegates user creation to the target
   system (Cognito) with attributes from the source. JIT follows the same pattern.

3. **Hardening eliminates the claim-as-write-path risk:**
   - `block_auto_created_users = true` — new users are blocked by default
   - Reconciler (async, <5s) unblocks users who are valid ADP tenant members
   - Group membership assigned by reconciler (NOT by `groups` claim alone — see
     shared-instance note below)

4. **Shared-instance change to group handling:** On a shared instance,
   `groups_attribute` in the JWT provider config auto-assigns group membership
   based on the `groups` claim. However, this is a SUPPLEMENTARY mechanism —
   the reconciler is the authoritative source of group membership. The `groups`
   claim seeds the initial assignment; the reconciler enforces it, handles
   removals, and corrects drift.

**Note on shared-instance impact on Decision 2:** JIT-with-hardening works
identically on shared instance vs per-tenant instance. The key difference is
that group membership becomes the access control boundary (not instance
boundary). The reconciler's role expands from "unblock users" to "unblock users
AND enforce group membership."

### Hardening: `block_auto_created_users` posture

**Current state** (verified in `user_data.sh:102`):
```ruby
gitlab_rails['omniauth_block_auto_created_users'] = false
```

**Target state:**
```ruby
gitlab_rails['omniauth_block_auto_created_users'] = true
```

With blocking enabled, the JIT-created user is immediately blocked. The
reconciler then:
1. Verifies user is a valid ADP tenant member (checks gateway Postgres)
2. Adds user to the correct `tenant-{tenant_id}` group
3. Unblocks the user: `PUT /api/v4/users/:id/unblock`

This is NOT a synchronous dependency — if the reconciler is slow, the user sees
GitLab's "Your account is pending approval" page and can retry in seconds.
The happy path is <5s from JIT creation to full access.

### Offboarding / Tenant Switch

- **Offboarding**: When a user is removed from an ADP tenant, the reconciler:
  1. Removes user from `tenant-{tenant_id}` group
  2. Blocks the user: `PUT /api/v4/users/:id/block`
  3. Active GitLab sessions invalidated (GitLab behavior on block)
- **Tenant switch**: On a shared instance, user may belong to multiple tenant
  groups (if ADP supports multi-tenancy for a user). The JWT `tenant_id` claim
  determines which group context the login applies to; the reconciler manages
  all group memberships.

### Pre-existing OIDC accounts (`omniauth_auto_link_user`)

**Current state**: `omniauth_auto_link_user` is not set (defaults to `false`).

**Target state**: Set to `["jwt"]` — auto-link JWT identity to existing OIDC
accounts by matching `email`. Since both providers (Cognito OIDC and ADP JWT)
source identity from the same Cognito user pool, email matching is safe and
prevents duplicate accounts on the shared instance.

---

## Decision 3: Trust Topology (Secret + Algorithm)

### Options Evaluated

| Option | Key management | Rotation story | GitLab config | Secret leak blast radius |
|--------|---------------|----------------|---------------|--------------------------|
| **A. HS256 shared secret** | Single symmetric key in both gateway + GitLab | Rotate = update both simultaneously | `algorithm: "HS256"` | Leaked key = anyone can forge tokens for that tenant |
| **B. RS256 asymmetric** | Gateway holds private key; GitLab holds public key | Rotate private key, publish new public key; old tokens still valid until expiry | `algorithm: "RS256"` | Leaked public key = no impact; leaked private key = forgery (but private key never leaves gateway) |

### Recommendation: **Option B — RS256 asymmetric**

**Rationale:**

1. **Reduced blast radius.** The public key lives on the GitLab instance (in
   `gitlab.rb` or fetched from a JWKS endpoint). If the GitLab instance is
   compromised, the attacker gets only the public key — useless for forging
   tokens. The private key stays in Secrets Manager, accessible only to the
   gateway's IAM role.

2. **Rotation without coordination.** To rotate:
   - Generate new RSA-2048 key pair
   - Store new private key in Secrets Manager (versioned)
   - Publish new public key to GitLab's config (SSM -> reconfigure, or JWKS endpoint)
   - Old tokens (signed with old key) are only valid for 60s anyway (see `valid_within`)
   - No simultaneous update required; brief overlap window is safe

3. **GitLab's `jwt` provider supports RS256.** The `algorithm` field in the
   provider config accepts `"RS256"` and the `secret` field accepts a PEM-encoded
   public key (or path to `.pem` file).

4. **Aligns with Cognito pattern.** Cognito JWTs are already RS256 with JWKS
   discovery. Using the same algorithm family for GitLab SSO tokens simplifies
   the mental model.

**Note on shared-instance impact on Decision 3:** RS256 works identically. The
only change is key scoping — v1 uses per-environment keys (one key pair for the
single shared instance). Per-tenant keys become relevant only with
instance-per-tenant migration.

### Key Storage (v1 — per-environment)

| Secret | Path | Who reads | Who writes |
|--------|------|-----------|------------|
| RSA private key (PEM) | `adp/{env}/gitlab-jwt-private-key` | Gateway service | Key rotation script |
| RSA public key (PEM) | SSM `/adp/{env}/gitlab/jwt-public-key` | GitLab instance (at reconfigure) | Key rotation script |

**Future (per-tenant, post-migration):**
- Private key: `adp/{env}/tenants/{tenant_id}/gitlab-jwt-private-key`
- Public key: SSM `/adp/{env}/tenants/{tenant_id}/gitlab/jwt-public-key`
- Each tenant instance validates its own key; cross-tenant token use impossible

### Validity Window

- **JWT `exp`**: `iat + 60s` (one-minute validity)
- **GitLab `valid_within`**: `65` (5-second grace for clock skew)
- **Replay mitigation**: 60s window + HTTPS-only transport makes replay
  impractical. GitLab does not natively track JWT `jti` for replay prevention,
  so we accept the 60s replay window as tolerable (same as Cognito's token
  exchange window).

---

## JWT Claim Schema

```json
{
  "iss": "urn:adp:gateway:{environment}",
  "sub": "{cognito_sub}",
  "aud": "adp-gitlab-{environment}",
  "iat": 1720000000,
  "exp": 1720000060,
  "jti": "{uuid4}",
  "tenant_id": "{tenant_id}",
  "uid": "{cognito_sub}",
  "name": "{user_display_name}",
  "email": "{user_email}",
  "username": "{github_username_or_email_prefix}",
  "groups": ["tenant-{tenant_id}"],
  "pre_authorized": true
}
```

### Claim semantics

| Claim | Purpose | GitLab mapping |
|-------|---------|----------------|
| `sub` | Stable user identifier (Cognito subject) | `uid_field: "sub"` -> GitLab `extern_uid` |
| `aud` | Audience restriction — environment-scoped for v1 (shared instance) | Validated by GitLab if `required_claims` includes `aud` |
| `uid` | Redundant with `sub` for GitLab compatibility | Maps to `uid_field` in provider |
| `name` | Display name | `info_map: { name: "name" }` |
| `email` | Email address | `info_map: { email: "email" }` |
| `username` | Preferred GitLab username | `info_map: { nickname: "username" }` |
| `groups` | Group membership seed (reconciler is authoritative) | `groups_attribute: "groups"` |
| `tenant_id` | ADP tenant — drives group assignment + future routing | Custom; consumed by reconciler; preserved for instance-per-tenant migration |
| `pre_authorized` | Signals this user should be auto-unblocked | Custom; consumed by reconciler |
| `jti` | Unique token ID (for audit trail) | Not consumed by GitLab |

**v1 audience note:** `aud` is `adp-gitlab-{environment}` (environment-scoped,
shared across all tenants on that instance). For instance-per-tenant migration,
this becomes `adp-gitlab-{tenant_id}` (per-tenant audience).

---

## Endpoint Contract: `GET /auth/gitlab-sso`

### Request

```
GET /api/auth/gitlab-sso
Authorization: Bearer <cognito_access_token>
```

No request body. The user's identity is derived from the Cognito access token
(existing gateway auth middleware).

### Response (success — GitLab configured for this environment)

```
HTTP/1.1 302 Found
Location: https://{gitlab_url}/users/auth/jwt/callback?jwt={signed_token}
Cache-Control: no-store
X-Content-Type-Options: nosniff
```

### Response (GitLab not configured)

```
HTTP/1.1 404 Not Found
Content-Type: application/json

{
  "detail": "GitLab is not configured for this environment. Contact your administrator to enable source control.",
  "error_code": "GITLAB_NOT_CONFIGURED"
}
```

### Response (secret not found — lazy discovery failure)

```
HTTP/1.1 503 Service Unavailable
Content-Type: application/json

{
  "detail": "GitLab SSO is temporarily unavailable. The signing key has not been provisioned.",
  "error_code": "GITLAB_SSO_KEY_MISSING"
}
```

### Implementation location

New router: `modules/gateway/src/auth/gitlab_sso.py`

```python
@router.get("/auth/gitlab-sso")
async def gitlab_sso_redirect(
    current_user: TokenContext = Depends(get_current_user),
):
    """Mint a JWT and redirect user to their tenant's GitLab instance."""
    tenant_id = current_user.org_id

    # 1. Discover GitLab URL (lazy — SSM lookup, cached 5min)
    gitlab_url = await _discover_gitlab_url()
    if not gitlab_url:
        raise HTTPException(404, detail="GitLab is not configured for this environment...")

    # 2. Load RSA private key (lazy — Secrets Manager, cached 5min)
    private_key = await _load_signing_key()
    if not private_key:
        raise HTTPException(503, detail="GitLab SSO signing key not provisioned...")

    # 3. Mint JWT (includes tenant_id for group assignment)
    token = _mint_gitlab_jwt(current_user, tenant_id, private_key)

    # 4. Redirect
    return RedirectResponse(
        url=f"{gitlab_url}/users/auth/jwt/callback?jwt={token}",
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )
```

### Caching strategy

- **GitLab URL**: Cached in-process for 5 minutes (SSM `GetParameter` on
  `/adp/{env}/gitlab/url`). Miss = feature disabled for this environment.
- **RSA private key**: Cached in-process for 5 minutes (Secrets Manager
  `GetSecretValue` on `adp/{env}/gitlab-jwt-private-key`). Miss = 503.
  Rotation takes effect within 5 minutes without restart.
- **Cache pattern**: Uses `time.monotonic() + TTL_SECONDS` pattern (consistent
  with `admin/connections/service.py` and `agent_registry.py`).

---

## GitLab Hardening Checklist

These settings must be applied to the shared GitLab instance (via `user_data.sh`
updates):

| Setting | Current | Target | Why |
|---------|---------|--------|-----|
| `omniauth_block_auto_created_users` | `false` | `true` | Prevent uncontrolled user creation; reconciler unblocks authorized users |
| `omniauth_allow_single_sign_on` | `['openid_connect']` | `['openid_connect', 'jwt']` | Add JWT provider alongside existing Cognito OIDC |
| `omniauth_auto_link_user` | unset | `['jwt']` | Auto-link JWT identity to existing OIDC accounts by email |
| `omniauth_auto_sign_in_with_provider` | `:openid_connect` | Remove or keep (JWT users arrive via callback URL, not login page) | JWT users bypass login page entirely |
| `signup_enabled` | default (`true`) | `false` | No local signups; all users come through SSO |
| `password_authentication_enabled_for_web` | default (`true`) | `false` | Force SSO; no local passwords (except root break-glass) |
| `password_authentication_enabled_for_git` | default (`true`) | `false` | Force token-based git auth |
| `restricted_visibility_levels` | none | `['public']` | Prevent public projects — shared instance means public = visible to all tenants |
| `default_project_visibility` | `private` | `private` | Confirm default; critical on shared instance |
| `default_group_visibility` | `private` | `private` | Confirm default; prevents cross-tenant group discovery |
| `user_default_external` | `false` | `true` | JIT users are external by default; cannot discover internal resources; reconciler grants group access |
| `can_create_group` | `true` | `false` | Only reconciler creates groups; prevents tenant namespace pollution |
| Root password | `ROTATE-ME-BEFORE-EXPOSURE` placeholder (#3595) | Generate + store in Secrets Manager on first deploy | Break-glass only; not used for SSO |

### JWT Provider Configuration (new block in `gitlab.rb`)

```ruby
gitlab_rails['omniauth_providers'] << {
  name: "jwt",
  label: "ADP Portal",
  args: {
    algorithm: "RS256",
    secret: File.read("/etc/gitlab/jwt-public-key.pem"),
    uid_field: "sub",
    required_claims: ["iss", "aud", "sub", "exp", "tenant_id"],
    valid_within: 65,
    info_map: {
      name: "name",
      email: "email",
      nickname: "username"
    },
    groups_attribute: "groups",
    auth_url: "https://{cloudfront_domain}/api/auth/gitlab-sso"
  }
}
```

The `auth_url` is where GitLab redirects users who visit the login page and click
"ADP Portal" — it points back to the gateway's SSO endpoint which validates their
session and mints a fresh JWT.

**Note on `groups_attribute` (GitLab CE):** GitLab CE supports `groups_attribute`
for the `jwt` OmniAuth provider. However, this only SEEDS initial group membership
from the claim. The reconciler is authoritative — it enforces membership state,
handles removals, and corrects drift. If CE drops `groups_attribute` support in a
future version, the reconciler still works (it just does the initial assignment
too).

---

## Zero-Deployment-Dependency Wiring

### Dependency direction: `gitlab -> gateway` only

```
                    +----------------+
                    |   Gateway      |
                    |                |
                    | /auth/gitlab   |--- reads SSM at request time --+
                    |   -sso         |                                |
                    +----------------+                                |
                          ^                                           v
                          |                              +-------------------+
                    no TF dependency                     |  SSM Parameters   |
                          |                              |  /adp/{env}/      |
                          |                              |  gitlab/url       |
                    +-----+----------+                   |  gitlab/jwt-      |
                    |   GitLab       |---- writes ------>|  public-key       |
                    |   Module       |    (TF output)    +-------------------+
                    +----------------+
```

### Rules (from #3440 / `[[feedback_core_modules_no_optional_deps]]`)

1. **No `data "terraform_remote_state"` from gateway into gitlab.** The gateway
   module MUST NOT read gitlab's state file. Discovery is lazy via SSM.

2. **No required boot-time environment variable.** The gateway pod starts and
   serves all other features without any `GITLAB_*` env var. The SSO endpoint
   discovers the URL and key at request time.

3. **Frontend fail-closed gate.** The `FEATURE_GITLAB_ENABLED` flag defaults to
   `false` (inverted from the existing fail-open pattern). The nav link renders
   ONLY when `/api/features` returns `gitlab: true`.

4. **Gateway Terraform has zero gitlab resources.** No `aws_ssm_parameter` data
   sources for gitlab paths. The SSO code uses the AWS SDK at runtime.

5. **GitLab module writes its outputs to SSM.** This is the ONLY coupling point:
   the GitLab TF module writes `/adp/{env}/gitlab/url` and the public key to SSM.
   The gateway reads these lazily.

### Feature Flag: `FEATURE_GITLAB_ENABLED`

**Why fail-closed (inverts the existing pattern):**

The existing features endpoint (`src/features/routes.py`) defaults all flags to
`true` — this is correct for core features that ship with every deployment. GitLab
is an **optional add-on** that requires:
- A GitLab instance to be deployed
- JWT keys to be provisioned
- The CloudFront VPC origin to be wired

Defaulting to `true` would show a broken nav link on every deployment that hasn't
set up GitLab. Fail-closed means the link is invisible until an operator explicitly
enables it (by setting `FEATURE_GITLAB_ENABLED=true` in the gateway pod env).

**Implementation:**

```python
# In src/features/routes.py — add to the features dict:
"gitlab": _is_disabled_unless_explicit("FEATURE_GITLAB_ENABLED"),
```

```python
def _is_disabled_unless_explicit(env_var: str) -> bool:
    """Inverted flag — returns False unless env var is explicitly 'true'."""
    value = os.environ.get(env_var, "")
    return value.lower() == "true"
```

**Frontend:**
```tsx
// In Navigation.tsx — replace the unconditional <a> with:
{features.gitlab && (
  <a href="/api/auth/gitlab-sso" className="...">
    <span className="text-xl" aria-hidden="true">🦊</span>
    <span>GitLab</span>
  </a>
)}
```

Note: The link now points to `/api/auth/gitlab-sso` (the gateway SSO endpoint)
instead of `/gitlab/` (the direct proxy). The SSO endpoint mints a JWT and
redirects to GitLab, establishing the authenticated session.

---

## Threat Model: JWT Handoff

| # | Threat | Attack vector | Mitigation | Residual risk |
|---|--------|---------------|------------|---------------|
| 1 | **Replay** | Attacker intercepts JWT and replays within 60s window | HTTPS-only transport; 60s `exp`; GitLab `valid_within: 65`; `jti` logged for forensics | Replay possible within 60s if TLS is compromised (accepted; same as Cognito token exchange) |
| 2 | **Secret leak (private key)** | Attacker compromises Secrets Manager or gateway memory | IAM scoping: only gateway role can read `gitlab-jwt-private-key`; key rotation script; single env-scoped key (v1) limits blast to one environment | If gateway IAM role is compromised, attacker can forge tokens for all tenants on that environment until key is rotated |
| 3 | **Claim forgery** | Attacker crafts JWT with elevated claims (e.g., `pre_authorized`, wrong `tenant_id`, wrong `groups`) | RS256 signature — requires private key; `required_claims` validation in GitLab; reconciler as authoritative group membership source (claim alone is insufficient for access) | Impossible without private key |
| 4 | **Open redirect on return URL** | Attacker substitutes a malicious `auth_url` in GitLab config | `auth_url` is hardcoded in `gitlab.rb` (not user-supplied); redirect target is the gateway's own CloudFront domain | None — no user-controlled redirect parameter |
| 5 | **Token leakage via Referer** | JWT appears in URL query string; Referer header leaks it to external resources | GitLab callback page should not load external resources; 60s expiry limits window; `Referrer-Policy: no-referrer` on the redirect | Minimal — GitLab login callback is self-contained |
| 6 | **Clock skew exploitation** | Attacker exploits clock drift to extend token validity | NTP on both gateway and GitLab; `valid_within: 65` gives only 5s grace; monitoring alerts on clock drift >10s | Negligible with NTP |
| 7 | **Denial of service (SSO endpoint)** | Attacker floods `/auth/gitlab-sso` | Endpoint requires valid Cognito token (existing auth middleware); rate limiting applies; SSM/Secrets Manager calls are cached | Same DDoS surface as any authenticated endpoint |
| 8 | **Tenant claim forgery -> wrong group** | Attacker forges JWT with `tenant_id: "victim"` and `groups: ["tenant-victim"]` to land in another tenant's group | RS256 signature prevents forgery; additionally, reconciler verifies user-tenant membership in gateway Postgres before granting group access — even if claim were accepted, reconciler would remove unauthorized membership within 60s | None with RS256; defense-in-depth via reconciler |
| 9 | **Groupless-but-active JIT user** | JIT creates user who is unblocked but has no group membership (accessing instance-wide resources) | `block_auto_created_users = true` ensures JIT users are BLOCKED until reconciler explicitly assigns group + unblocks; `user_default_external = true` means even if unblocked without group, user sees nothing | Residual: if reconciler unblocks without group assignment (bug), user is active but external with no group access — no data exposure |
| 10 | **Instance-wide token misuse by agents** | Agent worker uses `adp/{env}/gitlab-api-token` (instance-wide admin scope) to access other tenants' projects | Migration to per-tenant bot tokens with group-scoped access; deprecate instance-wide token; IAM policy on agent worker role restricts to per-tenant secret path only | During migration period, instance-wide token still exists — mitigated by agent identity resolution (webhook-ingress scopes job to tenant) |
| 11 | **Cross-tenant user directory enumeration** | Tenant A user calls GitLab `/users` API to discover tenant B users | `user_default_external = true` (external users cannot list other users); `restricted_visibility_levels = ['public']`; `/users` API returns only users in shared groups (private groups = no shared members visible); Admin mode required for full user list | External users with no shared groups see empty user list — verified against GitLab CE behavior |

---

## Tenant-to-Group Provisioning Flow

### Happy Path (first SSO login)

```
User clicks "GitLab" in ADP nav
         |
         v
GET /api/auth/gitlab-sso
(Cognito token validated by middleware)
         |
         v
Gateway resolves tenant_id from TokenContext.org_id
         |
         v
Gateway reads SSM: /adp/{env}/gitlab/url
         | (cached 5min; missing -> "not configured" 404)
         v
Gateway reads Secrets Manager: adp/{env}/gitlab-jwt-private-key
         | (cached 5min; missing -> 503)
         v
Gateway mints RS256 JWT (60s exp, claims include tenant_id + groups)
         |
         v
302 Redirect -> https://{gitlab_url}/users/auth/jwt/callback?jwt={token}
         |
         v
GitLab validates JWT signature (RS256, public key in config)
GitLab validates: iss, aud, exp, required_claims
         |
         v
GitLab JIT creates user (blocked by default, external by default):
  extern_uid = {sub}, provider = "jwt", email, name, username
         |
         v
GitLab seeds group membership from "groups" claim: tenant-{tenant_id}
(user is still BLOCKED at this point)
         |
         v
User sees "Account pending approval" page
         |
         v  (async, <5s)
Reconciler detects new blocked user with pre_authorized claim:
  1. Verifies user is valid ADP tenant member (gateway Postgres)
  2. Confirms tenant-{tenant_id} group membership exists
  3. PUT /api/v4/users/:id/unblock (Admin API)
         |
         v
User refreshes -> fully authenticated GitLab session
(scoped to tenant-{tenant_id} group only)
```

### Subsequent logins

Same flow, but GitLab finds existing user by `extern_uid` + provider. No creation,
no blocking. Group membership is re-synced from `groups` claim on every login.
Reconciler validates and corrects any drift.

### Offboarding

```
User removed from ADP tenant (org admin action)
         |
         v
Reconciler detects membership change (periodic scan or event-driven)
         |
         v
1. Remove user from tenant-{tenant_id} group
2. PUT /api/v4/users/:id/block
         |
         v
User's active GitLab sessions are invalidated (GitLab behavior on block)
         |
         v
SSO attempts fail: gateway still mints JWT (user still in Cognito)
but reconciler immediately re-blocks on next scan (user no longer
in ADP tenant membership table)
```

### Tenant Onboarding (reconciler creates group)

```
New tenant added to ADP
         |
         v
Reconciler (or onboarding automation) creates top-level group:
  POST /api/v4/groups
  { name: "tenant-{tenant_id}", path: "tenant-{tenant_id}",
    visibility: "private", subgroup_creation_level: "owner" }
         |
         v
Creates per-tenant bot user:
  POST /api/v4/users
  { username: "adp-bot-{tenant_id}", ... }
         |
         v
Adds bot user as Owner of tenant group
         |
         v
Creates group access token (api scope):
  POST /api/v4/groups/:id/access_tokens
         |
         v
Stores token in Secrets Manager:
  adp/{env}/tenants/{tenant_id}/gitlab-group-token
         |
         v
GitLab is ready for tenant users
```

---

## Child-Issue Breakdown for EPIC #3717

### Wave 1: Dependency-Gating (independently shippable, no GitLab infra needed)

#### Issue: Feature-gate the GitLab nav link (fixes #3600 defect)

**Description**: The GitLab nav link in `Navigation.tsx:149-161` renders
unconditionally on all deployments, including those without GitLab. This causes a
broken link (CloudFront 404 or S3 fallback) for any deployment that hasn't
configured the `/gitlab/*` VPC origin. Gate it behind a new `FEATURE_GITLAB_ENABLED`
flag (fail-closed: defaults to `false`).

**Impact analysis**:
- **Who benefits**: All ADP deployments without GitLab (no more broken nav link)
- **Who's impacted**: Deployments WITH GitLab must set `FEATURE_GITLAB_ENABLED=true`
- **What breaks if buggy**: GitLab link disappears on existing deployments that
  have it (rollback: revert the PR + set env var)
- **Cost**: Zero — feature flag only

**Design**:
- Backend: Add `"gitlab": _is_disabled_unless_explicit("FEATURE_GITLAB_ENABLED")`
  to `src/features/routes.py`
- Add helper `_is_disabled_unless_explicit(env_var)` that returns `False` unless
  env var is explicitly `"true"`
- Frontend: Replace unconditional `<a href="/gitlab/">` with
  `{features.gitlab && <a href="/gitlab/">...}` (keep existing href for now —
  the SSO endpoint doesn't exist yet; Wave 2 changes the href)
- Update `FeatureFlags` TypeScript interface to include `gitlab: boolean`
- Update `ALL_FEATURES_ENABLED` constant
- Update tests in `Navigation.test.tsx`

**Deployment**: Merge PR; gateway-deploy.yml fires. Existing GitLab deployments
must add `FEATURE_GITLAB_ENABLED=true` to the K8s ConfigMap
(`bedrockgateway-config`) before or after merge.

**Validation**:
- Unit test: features endpoint returns `gitlab: false` when env var unset
- Unit test: features endpoint returns `gitlab: true` when `FEATURE_GITLAB_ENABLED=true`
- Component test: Navigation does NOT render GitLab link when `features.gitlab` is false
- Component test: Navigation DOES render GitLab link when `features.gitlab` is true
- Smoke: Deploy to dev without env var -> confirm no GitLab link in nav

---

### Wave 2: SSO Endpoint + JWT Minting (gateway-side, no GitLab config changes)

#### Issue: Implement `GET /auth/gitlab-sso` endpoint with RS256 JWT minting

**Description**: Add the gateway endpoint that mints an RS256-signed JWT and
redirects to the environment's GitLab instance. Follows the lazy-discovery
pattern: reads GitLab URL from SSM and signing key from Secrets Manager at
request time, with 5-minute in-process cache. Also updates the nav link href
from `/gitlab/` to `/api/auth/gitlab-sso`.

**Impact analysis**:
- **Who benefits**: Users who click the GitLab link get seamless SSO
- **Who's impacted**: No existing flows affected (new endpoint only)
- **What breaks if buggy**: SSO redirect fails; user sees error; GitLab still
  accessible via direct Cognito OIDC flow (fallback)
- **Cost**: One new route, ~100 lines; no new AWS resources (reads existing SSM/SM)

**Design**:
- New file: `modules/gateway/src/auth/gitlab_sso.py`
- Router mounted at `/auth/gitlab-sso` in the auth router group
- Dependencies: `get_current_user` (existing), `boto3` (existing)
- JWT library: `PyJWT` with `cryptography` backend (already in gateway deps for
  Cognito JWKS validation)
- Cache: `time.monotonic() + TTL_SECONDS` pattern (from `admin/connections/service.py`)
- Claim schema: as defined in this design note
- Error responses: 404 (not configured), 503 (key missing)
- Frontend change: Update `Navigation.tsx` href from `/gitlab/` to `/api/auth/gitlab-sso`
- No database changes

**Deployment**: Merge PR; gateway-deploy.yml fires (pod restart picks up new route).
Endpoint is dormant until SSM params + Secrets Manager key are provisioned.

**Validation**:
- Unit test: JWT minting produces valid RS256 token with correct claims
- Unit test: SSM miss returns 404
- Unit test: Secrets Manager miss returns 503
- Unit test: `tenant_id` claim matches `current_user.org_id`
- Integration test: Mock SSM + SM -> verify redirect URL format
- Smoke: Call endpoint without GitLab provisioned -> confirm 404

---

### Wave 3: GitLab JWT Provider Configuration + Hardening

#### Issue: Add JWT OmniAuth provider to GitLab `user_data.sh` with full hardening

**Description**: Update the GitLab instance bootstrap to configure the `jwt`
OmniAuth provider alongside the existing `openid_connect` provider. Apply the
full hardening checklist (disable signups, disable password auth, block
auto-created users, restrict visibility, external-by-default users, disable group
creation).

**Impact analysis**:
- **Who benefits**: Enables the SSO flow end-to-end; hardens shared instance for
  multi-tenant use
- **Who's impacted**: Existing GitLab users via direct Cognito OIDC (still works —
  both providers coexist)
- **What breaks if buggy**: Users can't log into GitLab via JWT; Cognito OIDC remains
  functional as fallback. Hardening may affect existing users (e.g., `can_create_group=false`
  prevents existing users from creating new groups)
- **Cost**: User data change requires `gitlab-ctl reconfigure` on existing instance
  (no instance replacement — `user_data_replace_on_change = false` per #3557)

**Design**:
- Modify `modules/source-control/gitlab/infra/user_data.sh`: add JWT provider block
- New SSM parameter fetch: `/adp/{env}/gitlab/jwt-public-key`
- Write public key to `/etc/gitlab/jwt-public-key.pem` at boot
- Add hardening settings block (all items from hardening checklist above)
- New variable in GitLab TF module: `jwt_enabled` (default `false`) — controls
  whether the JWT provider block is templated into user_data
- Fix root password rotation (#3595) as part of hardening

**Deployment**: Terraform apply on gitlab module (new variables); then
`gitlab-ctl reconfigure` via SSM Run Command on existing instance. No instance
replacement.

**Validation**:
- Terraform plan shows only user_data template change (no instance replacement)
- After reconfigure: `curl -s https://{gitlab}/users/auth/jwt/callback?jwt=<invalid>`
  returns 401 (provider exists and rejects bad token)
- After reconfigure: direct Cognito OIDC login still works
- After reconfigure: `can_create_group` = false (existing users cannot create groups)
- After reconfigure: `user_default_external` = true (new users cannot discover
  internal resources)

---

### Wave 4: Key Provisioning + Reconciler + Bot Tokens

#### Issue: JWT key-pair provisioning + tenant group reconciler + per-tenant bot tokens

**Description**: Create a script that generates an RSA-2048 key pair (stores
private in Secrets Manager, public in SSM). Implement the reconciler that:
(a) creates tenant top-level groups, (b) unblocks JIT-created users after
verifying tenant membership, (c) manages group membership, (d) provisions
per-tenant bot users + group-scoped tokens.

**Impact analysis**:
- **Who benefits**: Completes the SSO flow; users are unblocked and placed in
  correct groups automatically; agents get per-tenant scoped tokens
- **Who's impacted**: Agent worker token resolution changes (per-tenant lookup)
- **What breaks if buggy**: Users stay blocked after first SSO login (manual
  unblock via admin panel is workaround); agents fall back to instance-wide token
- **Cost**: One script + one Lambda or CronJob (~200 lines); per-tenant secrets
  in Secrets Manager (~$0.40/secret/month)

**Design**:
- Script: `modules/source-control/gitlab/scripts/provision-jwt-keys.sh`
  - Generates RSA-2048 key pair via `openssl`
  - Stores private key: `aws secretsmanager create-secret --secret-id adp/{env}/gitlab-jwt-private-key`
  - Stores public key: `aws ssm put-parameter --name /adp/{env}/gitlab/jwt-public-key`
  - Idempotent (checks existence first)
- Reconciler: Lambda (scheduled every 60s via EventBridge) or K8s CronJob
  - **Tenant group management:**
    - Lists ADP tenants from gateway Postgres
    - Ensures each tenant has a `tenant-{tenant_id}` group (creates if missing)
    - Sets group visibility, subgroup_creation_level
  - **User lifecycle:**
    - Lists blocked users on GitLab via Admin API
    - Cross-references with ADP tenant membership (Postgres `users` table)
    - Unblocks + assigns group for valid members
    - Removes group membership + blocks for removed members
  - **Bot token management:**
    - Creates per-tenant bot user if missing
    - Creates/rotates group access token
    - Stores in `adp/{env}/tenants/{tenant_id}/gitlab-group-token`
- Agent worker change: resolve token by `tenant_id` from job context (falls back
  to instance-wide token during migration)

**Deployment**: Key provisioning script run manually during initial setup (or by
deploy-all.sh). Reconciler deployed via terraform (Lambda + EventBridge rule) in
the gitlab module. Agent worker change deployed via agent-worker-image workflow.

**Validation**:
- Script creates key pair; `aws secretsmanager get-secret-value` succeeds
- GitLab reconfigure picks up public key; JWT validation works
- Reconciler creates tenant group within 60s of tenant onboarding
- Reconciler unblocks a test user within 60s of JIT creation
- Reconciler blocks a user removed from ADP within 60s
- Per-tenant bot token resolves correctly in agent worker

---

### Wave 5: End-to-End Integration + Smoke Test

#### Issue: E2E test for GitLab SSO click-path through CloudFront

**Description**: Implement the full smoke test exercising the literal click path:
ADP dashboard -> GitLab nav link -> `/api/auth/gitlab-sso` -> 302 to GitLab ->
JWT validated -> user session established. Must go through CloudFront (per
`[[feedback_eval_click_paths_not_subpaths]]` from #3706).

**Impact analysis**:
- **Who benefits**: CI catches SSO regressions
- **Who's impacted**: None (test-only)
- **What breaks if buggy**: False negatives in CI (no production impact)
- **Cost**: One E2E test file

**Design**:
- Test file: `tests/e2e/gitlab/test_sso_flow.py`
- Uses real CloudFront URL (not localhost or direct ALB)
- Authenticates via existing E2E auth helper (Cognito tokens)
- Calls `GET /api/auth/gitlab-sso` via CloudFront, follows redirect
- Verifies GitLab responds with 200 (authenticated session) or "pending approval"
  (acceptable for JIT-created user before reconciler runs)
- Separate test for "not configured" case: calls endpoint on tenant without GitLab
  -> verifies 404
- Multi-tenant test: verify tenant A user cannot access tenant B group after
  SSO (cross-tenant isolation)

**Deployment**: Merge PR; test runs in CI on next E2E suite execution.

**Validation**:
- Test passes in CI against dev environment
- Test correctly fails if JWT signing key is rotated without updating GitLab
- Cross-tenant isolation test passes (tenant A user has no access to tenant B group)

---

## Appendix A: GitLab `jwt` Provider Reference

From GitLab CE docs (`doc/administration/auth/jwt.md`):

```ruby
gitlab_rails['omniauth_providers'] = [
  {
    name: "jwt",
    label: "Display Name",
    args: {
      algorithm: "RS256",        # or HS256
      secret: "public-key-or-shared-secret",
      uid_field: "sub",          # claim used as extern_uid
      required_claims: ["iss", "aud"],
      valid_within: 60,          # seconds — reject tokens older than this
      info_map: {
        name: "name",
        email: "email",
        nickname: "username"
      },
      groups_attribute: "groups", # auto-assign group membership from claim
      auth_url: "https://your-idp.example.com/auth"
    }
  }
]
```

The `auth_url` is where GitLab sends users who click the JWT button on the login
page. In our design, this points to `https://{cloudfront_domain}/api/auth/gitlab-sso`
which validates their ADP session and mints a JWT.

## Appendix B: Migration Path from Cognito OIDC to JWT SSO

The existing Cognito OIDC provider (`openid_connect`) remains active during the
transition. Users who visit GitLab directly (not through the ADP dashboard) will
continue to authenticate via Cognito OIDC. The JWT provider is additive — it
does not replace the existing auth path.

Once JWT SSO is validated end-to-end, the Cognito OIDC provider can optionally be
removed (reducing the attack surface to a single auth path). This is a separate
decision not in scope for #3717. A future issue should evaluate:
- Whether direct-visit users need the OIDC fallback
- Impact on existing sessions during provider removal
- Timeline relative to instance-per-tenant migration (where OIDC becomes unnecessary)

## Appendix C: Instance-per-Tenant Cost Model (preserved for future reference)

| Component | Monthly cost | Notes |
|-----------|-------------|-------|
| EC2 (m6i.xlarge) | ~$140 | 4 vCPU, 16 GiB; minimum for GitLab CE |
| EBS (100 GiB gp3) | ~$8 | OS + GitLab data |
| EBS snapshots (90-day retention) | ~$15 | Daily snapshots |
| Secrets Manager (2 secrets) | ~$0.80 | JWT key + admin token |
| SSM parameters (3 params) | $0 | Standard tier free |
| **Total per tenant** | **~$170/mo** | Acceptable for $500-2000/mo tenant spend |

## Appendix D: Relevant Issue References

- #3717 — Parent EPIC (GitLab Portal SSO)
- #3320 — Grandparent (Source Control)
- #3557 — CloudFront delivery (done; established `user_data_replace_on_change=false`)
- #3706 — Bare-path fix (lesson: test through CloudFront, not subpaths)
- #3693 — Error responses
- #3600 — Nav link renders unconditionally (defect; fixed in Wave 1)
- #3595 — Root password rotation (incorporated in Wave 3 hardening)
- #3566 — Features endpoint pattern (extended in Wave 1)
- #2213 — Lazy optional-secret pattern (followed in Wave 2)
- #313/#518/#519 — GitHub-as-IdP history (do not resurrect; lessons inform this design)
- #520 — GitHub auth broker (architectural precedent for SSO Lambda pattern)
- #309 — GitHub auth EPIC (pattern source)
