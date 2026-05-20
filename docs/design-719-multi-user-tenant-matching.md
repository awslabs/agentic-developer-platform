# Design: Multi-User Tenant Matching (Issue #719)

> **Status**: Proposal
> **Author**: @agent-architect
> **Date**: 2026-05-20
> **Issue**: #719 — handler creates new tenant per user instead of attaching to existing GitHub-org tenant

## Problem Statement

The onboarding handler (`modules/gateway/src/admin/onboarding/handler.py`) derives a tenant ID from the user's GitHub login and always creates a new `TenantAccessRequest`. It never checks whether the user belongs to a GitHub organization that already has an App installation registered to an existing ADP tenant.

Result: in a multi-user enterprise, each user from the same GitHub org ends up creating their own tenant instead of joining the shared one.

## Decision Summary

| Question | Decision |
|----------|----------|
| Match algorithm | By `github_installation_id` via `ChannelTenantMap` + `organizations.github_installation_ids` |
| Tenant admin assignment | Option C — install-time admin + GitHub org admins auto-elevated |
| Approval policy for matched users | Auto-approve by default (installation proves access); per-tenant opt-in for manual approval |
| Schema changes | Add `organizations.member_approval_policy` column (varchar, default `auto_approve_org_members`). No `tenant_members` table yet. |
| Migration impact on existing tenants | None — only new sign-ups go through the matching logic |

---

## 1. Match Algorithm

### Lookup chain when a user calls `POST /api/onboarding/access/request`:

```
1. Extract (github_login, github_id) from JWT/Cognito (existing code)
2. Call GitHub API: GET /user/installations (using user's OAuth token)
   → returns list of installation IDs the user can access
3. For each installation_id the user has access to:
   a. Query: SELECT org_id FROM channel_tenant_map
             WHERE provider = 'github' AND provider_scope_id = :installation_id
   b. If found → this user belongs to tenant org_id
4. If no match found via channel_tenant_map, fallback:
   Query: SELECT id FROM organizations
          WHERE github_installation_ids @> :installation_id_json
5. If still no match → proceed with current flow (new tenant creation)
```

### Why `github_installation_id` and not org slug?

- **Security**: GitHub org slugs can be renamed. A malicious user could claim a slug that was previously someone else's org.
- **Correctness**: `installation_id` is immutable and cryptographically tied to the GitHub App ↔ org relationship.
- **Already indexed**: The identity-index DynamoDB table has `identity_type=github_installation_id` entries (see `docs/onboard-hosted-tenant.md`).

### Edge case: user has access to multiple installations with different tenants

Pick the **first matching tenant** (by installation_id order from GitHub API response — typically the org the user belongs to most directly). If the user belongs to multiple orgs with ADP tenants, they can switch later (future work: tenant chooser UI). For now, `users.org_id` supports one tenant per user — this is adequate for 95% of cases.

### Getting the user's installation IDs without a GitHub API call

The preferred path avoids a GitHub API call during onboarding (latency, token management):

**Option A (preferred)**: When the GitHub App is installed, we receive the `installation:created` webhook with the `installation.id` and `installation.account.login`. The connections service already stores this in `channel_tenant_map` and `organizations.github_installation_ids`. At sign-in time, the user's GitHub org membership is visible from the JWT claim `custom:github_username`. We match the username to the org that owns the installation:

```
1. slug = slugify(github_login)
2. Check if slug collides with an existing org ID in organizations table
3. If collision exists → check if the colliding org has github_installation_ids populated
4. If yes → this is a "same org, new user" case → attach
```

**Problem with Option A**: It relies on slug matching, which the issue explicitly warns against.

**Option B (recommended)**: Add a lightweight GitHub API call using the platform's App credentials (not the user's token) to verify org membership:

```python
# Using GitHub App installation token for the candidate org
GET /orgs/{org_login}/members/{github_login}
# Returns 204 if member, 404 if not
```

This is more secure because:
- Uses the App's credentials (already available in Secrets Manager)
- Confirms actual org membership, not just installation access
- No user OAuth token needed

**Option C (simplest, recommended for Phase 1)**: Use the existing collision detection as the trigger. Today, when `_pick_tenant_id` finds the slug already exists in `organizations`, it returns `None` (collision). Instead:

```python
# In _pick_tenant_id or a new _match_existing_tenant helper:
existing_org = await db.get(Organization, base_slug)
if existing_org is not None:
    # Slug matches an existing org — check if this is a "join" scenario
    if existing_org.github_installation_ids:
        # Org has GitHub App installed — verify user belongs to this org
        # via GitHub API membership check
        return ("match", existing_org.id)
    else:
        return ("collision", None)  # True collision, not a join
```

### Recommendation: Option C for Phase 1

It's the simplest path with highest confidence:
1. Slug collision triggers the check (already happening today)
2. If the colliding org has `github_installation_ids`, verify membership via GitHub API
3. On confirmed membership → attach user to existing tenant
4. On failed membership → return collision (current behavior)

---

## 2. Tenant Admin Assignment

### Current state

The `approve_request` function in `approval.py` always sets `role="org_admin"` on the first user created for a tenant (line 127). There is no subsequent admin assignment logic.

### Proposed model (Option C from issue)

| Trigger | Role assigned |
|---------|--------------|
| First user to install the App (tenant creator) | `org_admin` |
| GitHub org admin who signs in later | `org_admin` |
| Regular org member who signs in | `member` |

### Implementation

In the new `_match_or_provision_tenant` helper:

```python
async def _determine_role_for_matched_user(
    github_login: str,
    org_login: str,
    github_client: GitHubAppClient,
) -> str:
    """Check if user is a GitHub org admin → org_admin, else member."""
    membership = await github_client.get_org_membership(org_login, github_login)
    if membership and membership.get("role") == "admin":
        return "org_admin"
    return "member"
```

Uses `GET /orgs/{org}/memberships/{username}` which returns `{"role": "admin"|"member"}`.

---

## 3. Approval Policy

### Current state

Every new user gets `status="pending"` in `TenantAccessRequest` and waits for a platform admin to approve.

### Proposed model

| Scenario | Approval |
|----------|----------|
| New user, no matching tenant | `pending` (platform admin approves, creates tenant) |
| New user, matching tenant, policy=`auto_approve_org_members` | `approved` immediately (auto-join) |
| New user, matching tenant, policy=`require_admin_approval` | `pending` (tenant admin approves) |

### Per-tenant policy column

```sql
ALTER TABLE organizations ADD COLUMN member_approval_policy VARCHAR(32)
  NOT NULL DEFAULT 'auto_approve_org_members';
-- Valid values: 'auto_approve_org_members', 'require_admin_approval'
```

Why auto-approve by default: The user has already proven they belong to the GitHub org (verified via App installation + API membership check). Requiring further approval adds friction for the common case. Enterprises that want stricter control can flip to `require_admin_approval`.

---

## 4. Schema Changes

### New column on `organizations`

```python
# In organization.py
member_approval_policy: Mapped[str] = mapped_column(
    String(32), nullable=False, default="auto_approve_org_members",
    server_default="auto_approve_org_members",
)
```

### Migration (013)

```python
"""Add member_approval_policy to organizations."""
# Alembic migration 013_member_approval_policy.py

def upgrade():
    op.add_column(
        "organizations",
        sa.Column(
            "member_approval_policy",
            sa.String(32),
            nullable=False,
            server_default="auto_approve_org_members",
        ),
    )

def downgrade():
    op.drop_column("organizations", "member_approval_policy")
```

### No `tenant_members` table

The issue considered a many-to-many `tenant_members` table. Deferred: `users.org_id` (single tenant per user) is sufficient for the foreseeable future. A user in two orgs picks one at onboarding time.

---

## 5. Handler Changes

### File: `modules/gateway/src/admin/onboarding/handler.py`

Replace the collision-returns-None logic with a match-or-provision helper:

```python
async def _match_or_provision_tenant(
    db: AsyncSession,
    github_login: str,
    github_id: str,
    cognito_sub: str,
) -> tuple[str, str | None]:
    """Determine if user should join an existing tenant or create a new one.

    Returns:
        ("new", tenant_id)    — no match, proceed with new tenant creation
        ("match", org_id)     — user belongs to existing org, should be attached
        ("collision", None)   — slug collision but not a verified member
    """
    base_slug = _slugify_tenant_id(github_login)

    # Validate slug
    if base_slug in RESERVED_TENANT_IDS or not TENANT_ID_PATTERN.match(base_slug):
        return ("collision", None)

    # Check if org already exists with this slug
    existing_org = await db.get(Organization, base_slug)
    if existing_org is None:
        # No collision — check for other pending requests (existing logic)
        stmt = select(TenantAccessRequest).where(
            TenantAccessRequest.proposed_tenant_id == base_slug,
            TenantAccessRequest.status == "pending",
        )
        result = await db.execute(stmt)
        other = result.scalar_one_or_none()
        if other is not None and other.cognito_sub != cognito_sub:
            return ("collision", None)
        return ("new", base_slug)

    # Org exists — is this a "join existing org" case?
    if not existing_org.github_installation_ids:
        # Org has no GitHub App installed — can't verify membership
        return ("collision", None)

    # Verify user is a member of the GitHub org via API
    is_member = await _verify_github_org_membership(
        org_login=existing_org.name,
        github_login=github_login,
        installation_ids=existing_org.github_installation_ids,
    )
    if not is_member:
        return ("collision", None)

    return ("match", existing_org.id)
```

### New helper: `_verify_github_org_membership`

```python
async def _verify_github_org_membership(
    org_login: str,
    github_login: str,
    installation_ids: list[str],
) -> bool:
    """Verify the user is a member of the org using GitHub App API.

    Uses the first available installation_id to generate an installation
    token, then checks org membership.
    """
    from src.admin.connections.github_client import GitHubAppClient

    try:
        client = GitHubAppClient()
        for install_id in installation_ids:
            try:
                is_member = await client.check_org_membership(
                    installation_id=int(install_id),
                    org_login=org_login,
                    username=github_login,
                )
                if is_member:
                    return True
            except Exception:
                continue  # Try next installation_id
        return False
    except Exception:
        logger.warning(
            "GitHub org membership check failed for %s in %s",
            github_login, org_login,
        )
        return False  # Fail closed — don't auto-attach on API errors
```

### Modified `submit_access_request` flow

```python
@router.post("/access/request", response_model=AccessRequestResponse)
async def submit_access_request(...):
    # ... existing idempotency check ...
    # ... existing GitHub identity extraction ...

    # NEW: match-or-provision logic
    action, tenant_id = await _match_or_provision_tenant(
        db, github_login, github_id, cognito_sub
    )

    if action == "collision":
        return AccessRequestResponse(
            status="collision",
            reason=f"A workspace named '{_slugify_tenant_id(github_login)}' already exists...",
        )

    if action == "match":
        # User belongs to existing tenant — attach them
        return await _attach_user_to_existing_tenant(
            db=db,
            org_id=tenant_id,
            cognito_sub=cognito_sub,
            github_login=github_login,
            github_id=github_id,
        )

    # action == "new" — proceed with existing creation flow
    request = TenantAccessRequest(...)
    # ... rest of existing code ...
```

### New helper: `_attach_user_to_existing_tenant`

```python
async def _attach_user_to_existing_tenant(
    db: AsyncSession,
    org_id: str,
    cognito_sub: str,
    github_login: str,
    github_id: str,
) -> AccessRequestResponse:
    """Attach a verified org member to an existing tenant.

    Respects the org's member_approval_policy.
    """
    org = await db.get(Organization, org_id)

    # Check approval policy
    policy = getattr(org, "member_approval_policy", "auto_approve_org_members")

    if policy == "require_admin_approval":
        # Create a pending request for the tenant admin to approve
        request = TenantAccessRequest(
            cognito_sub=cognito_sub,
            provider="github",
            provider_user_id=github_id,
            proposed_tenant_id=org_id,
            target_login=github_login,
            motivation=f"Auto-detected member of org '{org.name}'",
        )
        db.add(request)
        await db.commit()
        await db.refresh(request)
        return AccessRequestResponse(
            status="pending",
            request_id=request.id,
            eta_hours=24,
        )

    # Auto-approve: create user directly in the existing tenant
    # Find the default team for the org
    from sqlalchemy import select as sa_select
    from src.shared.models.organization import Team

    stmt = sa_select(Team).where(
        Team.org_id == org_id, Team.name == "Default"
    ).limit(1)
    result = await db.execute(stmt)
    default_team = result.scalar_one_or_none()

    if not default_team:
        # Fallback: get any team in the org
        stmt = sa_select(Team).where(Team.org_id == org_id).limit(1)
        result = await db.execute(stmt)
        default_team = result.scalar_one_or_none()

    if not default_team:
        logger.error("No team found for org %s during auto-attach", org_id)
        return AccessRequestResponse(
            status="collision",
            reason="Organization exists but has no teams configured. Contact an admin.",
        )

    # Determine role
    role = await _determine_role_for_matched_user(github_login, org.name)

    # Create user
    from src.shared.models.base import new_uuid
    user_id = new_uuid()
    user = User(
        id=user_id,
        org_id=org_id,
        team_id=default_team.id,
        email=f"{github_login}@github.onboard",
        name=github_login,
        cognito_sub=cognito_sub,
        role=role,
    )
    db.add(user)

    # Create user identities
    from src.shared.models.vault import UserIdentity
    from src.shared.models.base import utcnow
    now = utcnow()

    cognito_identity = UserIdentity(
        id=new_uuid(),
        user_id=user_id,
        org_id=org_id,
        team_id=default_team.id,
        provider="cognito",
        provider_user_id=cognito_sub,
        provider_username=github_login,
        verification_method="oauth",
        verified_at=now,
    )
    db.add(cognito_identity)

    github_identity = UserIdentity(
        id=new_uuid(),
        user_id=user_id,
        org_id=org_id,
        team_id=default_team.id,
        provider="github",
        provider_user_id=github_id,
        provider_username=github_login,
        verification_method="oauth",
        verified_at=now,
    )
    db.add(github_identity)

    # Also record as an approved access request for audit trail
    request = TenantAccessRequest(
        cognito_sub=cognito_sub,
        provider="github",
        provider_user_id=github_id,
        proposed_tenant_id=org_id,
        target_login=github_login,
        motivation=f"Auto-attached: member of org '{org.name}'",
        status="approved",
        decided_by="system:org-member-match",
        decided_at=now,
    )
    db.add(request)

    await db.commit()

    # Best-effort DDB write-through
    try:
        from src.admin.identity.identity_index_writer import IdentityIndexWriter
        writer = IdentityIndexWriter()
        await writer.put_user_identity(
            provider_user_id=cognito_sub,
            user_id=user_id,
            org_id=org_id,
            provider="cognito",
            provider_username=github_login,
        )
        await writer.put_user_identity(
            provider_user_id=github_id,
            user_id=user_id,
            org_id=org_id,
            provider="github",
            provider_username=github_login,
        )
    except Exception:
        logger.exception("DDB write-through failed during org-member auto-attach")

    return AccessRequestResponse(
        status="approved",
        tenant_id=org_id,
        redirect="/dashboard",
    )
```

---

## 6. GitHubAppClient Extension

### File: `modules/gateway/src/admin/connections/github_client.py`

Add a method:

```python
async def check_org_membership(
    self,
    installation_id: int,
    org_login: str,
    username: str,
) -> bool:
    """Check if username is a member of org_login using installation token.

    GET /orgs/{org}/members/{username}
    Returns True on 204, False on 404/302.
    """
    token = await self._get_installation_token(installation_id)
    resp = await self._http.get(
        f"https://api.github.com/orgs/{org_login}/members/{username}",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    return resp.status_code == 204
```

**Permissions note**: The GitHub App must have `Organization > Members: Read` permission. The existing ADP apps already have this for webhook routing.

---

## 7. Codebase Assumptions That Must Remain Consistent

Places that assume `users.org_id` is the user's only tenant:

| Location | Assumption | Impact |
|----------|-----------|--------|
| `src/auth/org_id_resolver.py` | `users.org_id` is the source of truth for org | **Safe** — still correct, user has one org |
| `src/shared/services/credential_resolver.py` | Vault paths keyed by `org_id` | **Safe** — user joins the existing org, shares the org's vault |
| `src/auth/dependencies.py` → JWT claims | `custom:org_id` claim | **Safe** — new user gets org_id written to Cognito on attach |
| `src/admin/identity/identity_index_writer.py` | DDB entries keyed by `org_id` | **Safe** — new user gets entries under the shared org_id |
| Webhook Lambda | Resolves `org_id` from installation_id in DDB | **Safe** — installation_id still maps to same org |
| `src/budget/` | Budget scoped to `org_id` | **Safe** — shared budget is the intended behavior for org members |

**No breaking changes** — the new user simply gets `org_id` set to the existing org's ID instead of a new one.

---

## 8. Migration Safety

### Existing single-user tenants (gagangoel, attilasimon72, iankouls-aws, etc.)

- Their `organizations.github_installation_ids` is `[]` (empty list) because they onboarded via the old flow that doesn't populate installations.
- The match logic only triggers when `github_installation_ids` is non-empty.
- Therefore: **no existing tenant is affected**. The matching logic only fires for tenants that were created through the admin API with channels configured (or through the connections install-callback flow).

### Timeline

1. Migration 013 adds `member_approval_policy` column with default `auto_approve_org_members` — no behavior change for existing tenants until an admin configures their installations.
2. When enterprise customer installs the App → their org gets `github_installation_ids` populated via connections flow.
3. Second user from that org signs in → match logic fires → auto-attaches.

---

## 9. Race Condition: Two Users Sign In Simultaneously

**Scenario**: User A and User B from org `acme` both sign in for the first time simultaneously. Neither is in the DB yet. Both hit `_match_or_provision_tenant`.

**Mitigation**:
- If the org already exists (created via admin API or connections flow), both users will match and both will call `_attach_user_to_existing_tenant`. The `uq_users_cognito_sub` unique index (migration 012) prevents duplicates.
- If the org doesn't exist yet (both trying to create new tenants), the existing `_pick_tenant_id` collision logic handles this: the first one gets the slug, the second gets a collision response.

No additional protection needed beyond what's already in the schema.

---

## 10. Sequence Diagram

```
User B (2nd user from acme org)
  │
  ├─→ POST /api/onboarding/access/request
  │     ├─ Extract github_login="bob", github_id="456"
  │     ├─ _match_or_provision_tenant("bob", "456", cognito_sub)
  │     │   ├─ slugify → "bob" (user's personal slug)
  │     │   ├─ SELECT organizations WHERE id = "bob" → NULL
  │     │   │   (no org with slug "bob")
  │     │   │
  │     │   ├─ ALTERNATIVE PATH: check channel_tenant_map
  │     │   │   Need user's installation IDs → can't get without API call
  │     │   │
  │     │   └─ Return ("new", "bob") → creates new tenant "bob"
  │     │
  │     ╰─ PROBLEM: User B's personal slug ≠ org slug "acme"
  │
  ╰─ The slug-based approach ONLY works when user's GitHub login == org login
     (i.e., personal accounts or the first user who matches the org slug)
```

### Critical insight: slug matching is insufficient

The slug is derived from the **user's personal GitHub login**, not their org. User `bob` from org `acme` gets slug `bob`, which won't collide with org `acme`. The collision-based matching only works when the GitHub login IS the org name (personal accounts acting as orgs).

### Revised approach: query by installation ID

We need an explicit step that checks the user's org memberships:

```
User B signs in
  │
  ├─→ POST /api/onboarding/access/request
  │     ├─ Extract github_login="bob", github_id="456"
  │     ├─ _match_or_provision_tenant("bob", "456", cognito_sub)
  │     │   ├─ Step 1: Get user's GitHub orgs (API call or from token)
  │     │   │   GET /users/bob/orgs → ["acme", "personal-stuff"]
  │     │   │
  │     │   ├─ Step 2: For each org, check if ADP has a tenant with that installation
  │     │   │   SELECT * FROM channel_tenant_map
  │     │   │     WHERE provider='github'
  │     │   │     AND provider_scope_id IN (select installation IDs for acme)
  │     │   │   → Found! org_id = "acme"
  │     │   │
  │     │   ├─ Step 3: Verify membership (defense-in-depth)
  │     │   │   GET /orgs/acme/members/bob → 204 ✓
  │     │   │
  │     │   └─ Return ("match", "acme")
  │     │
  │     ├─ _attach_user_to_existing_tenant(org_id="acme", ...)
  │     └─ Return: status="approved", tenant_id="acme"
```

### How to get the user's GitHub orgs without their OAuth token

**Problem**: At onboarding time, we only have:
- The user's `github_login` (from JWT/Cognito)
- The user's `github_id` (numeric)
- Platform App credentials

**Solution**: Use the GitHub API's public endpoint:
```
GET /users/{username}/orgs
```
This returns public org memberships. For private orgs, we need the App installation token:
```
GET /orgs/{org}/members/{username}  (using installation token)
```

**Recommended approach**:
1. Query all orgs that have installations registered in ADP: `SELECT DISTINCT name, github_installation_ids FROM organizations WHERE github_installation_ids != '[]'`
2. For each such org, check if the user is a member using the installation token
3. This is O(number of ADP tenants with installations) — small in practice (< 100)

For scale optimization (future): maintain a reverse index in DDB mapping `github_user_id → [org_ids]`.

---

## 11. Final Recommended Implementation

### Phase 1 (this issue)

1. **Add `member_approval_policy` column** to `organizations` (migration 013)
2. **Add `check_org_membership` method** to `GitHubAppClient`
3. **Add `_find_matching_tenant_for_user` helper** in handler.py:
   - Queries all orgs with non-empty `github_installation_ids`
   - For each, checks if `github_login` is a member via GitHub API
   - Returns the matching `org_id` or None
4. **Modify `submit_access_request`**:
   - Before slug derivation, call `_find_matching_tenant_for_user`
   - On match → call `_attach_user_to_existing_tenant`
   - On no match → proceed with existing flow (new tenant creation)
5. **Add `_attach_user_to_existing_tenant` helper** (as designed above)
6. **Unit tests** (as specified in issue)

### Phase 2 (follow-up)

- Tenant admin approval queue (when policy is `require_admin_approval`)
- Tenant admin UI to manage members
- `tenant_members` table for multi-org users (if needed)

---

## 12. Files to Create/Modify

### New files
| File | Purpose |
|------|---------|
| `modules/gateway/alembic/versions/013_member_approval_policy.py` | Add column to organizations |

### Modified files
| File | Change |
|------|--------|
| `modules/gateway/src/admin/onboarding/handler.py` | Add `_find_matching_tenant_for_user`, `_attach_user_to_existing_tenant`, modify `submit_access_request` |
| `modules/gateway/src/admin/connections/github_client.py` | Add `check_org_membership` method |
| `modules/gateway/src/shared/models/organization.py` | Add `member_approval_policy` column |
| `modules/gateway/src/admin/onboarding/schemas.py` | Add `"attached"` to AccessRequestResponse status options (documentation) |
| `modules/gateway/tests/admin/test_onboarding_handler.py` | Add 5 unit tests per issue spec |

---

## 13. API Contract Change

`POST /api/onboarding/access/request` response gains a new possible status:

```json
// New: user was matched and auto-attached to existing tenant
{
  "status": "approved",
  "tenant_id": "acme",
  "redirect": "/dashboard"
}
```

No new endpoints. No breaking changes to existing responses.

---

## 14. Security Considerations

| Risk | Mitigation |
|------|-----------|
| User claims membership in org they don't belong to | GitHub API membership check via installation token (App-level auth, not user-level) |
| Stale org membership (user was removed from GitHub org) | Check is at sign-in time; future: periodic re-verification or webhook on `org:member_removed` |
| Cross-tenant data leak if wrong match | Match is by installation_id (immutable) + API membership verification. Two independent signals. |
| Rate limiting on GitHub API | Cache membership checks for 5 min. ADP will have < 100 tenants with installations in near term. |

---

## 15. Cost / Performance

- **GitHub API calls**: 1 call per candidate org per sign-in. With < 100 orgs, worst case is 100 API calls. In practice, the loop short-circuits on first match (typically 1-3 calls).
- **Database queries**: 1 additional SELECT on organizations (cached in SQLAlchemy session).
- **Latency added to onboarding**: ~200-500ms for the GitHub API membership check.
- **No new AWS resources**: no new tables, no new DDB entries beyond what approval already creates.
