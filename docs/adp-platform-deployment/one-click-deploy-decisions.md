# One-Click Deploy — Resolved Design Questions (§9a)

Architect decisions for the open questions in
[`one-click-deploy-design.md`](./one-click-deploy-design.md) §9, resolved in
issue **#3033**. To be folded into the design doc when PR #3032 merges.

---

## Decisions

### Q1. Org-level visibility policy

**Decision:** Ship a hard org policy stored in `organizations.settings` JSON
(existing column). Values: `allow_all` (default) | `force_private` |
`force_team_or_private`. Enforced on write (`POST /deploys`,
`POST /deploys/{id}/upgrade`) — not retroactive on reads.

**Rationale:** The `Organization` model already carries a `settings: JSON` column
used for extensible config (`plan`, `channels`, `user_auto_provision_mode`). Adding
a `deploy_visibility_policy` key follows the established pattern. A separate table
is over-engineered for one enum value.

**Follow-up:** #3036

---

### Q2. Service-account token issuance (deploy-scoped)

**Decision:** Extend the existing `TokenManager.generate_token()` flow with a
`scopes` claim array. A deploy token is a service-account JWT with
`scopes: ["deploy:write"]`. No new issuance mechanism.

**Scope vocabulary (v1):** `*` (full, default), `deploy:write`, `deploy:read`,
`proxy:invoke`, `admin:*`.

**Backward compat:** existing service accounts have `scopes = NULL` → treated as
`["*"]`. No behavior change until explicitly scoped.

**Rationale:** `ServiceAccount` model and `TokenManager` already exist. Adding
scope restriction is additive — a new column, a new JWT claim, enforcement in
middleware. The deploy module checks `deploy:write`; the gateway proxy checks
`proxy:invoke`.

**Follow-up:** #3037

---

### Q3. SSE tail mechanism

**Decision:** Start with short DB poll (2-second interval). Confirmed.

**Revisit triggers:**
- **Fan-out:** >50 concurrent SSE connections per pod
- **Latency:** P99 phase-transition-to-client > 5 seconds

**When revisiting:** move to `pg_notify('deploy_phase_update', deployment_id)` +
a single listener connection per pod that fans out to in-memory subscribers. No
external pub/sub (Redis/SNS) needed at foreseeable scale.

**Rationale:** No `LISTEN/NOTIFY` exists anywhere in the codebase today. Phase
transitions happen ~10 times per deploy at 1–15 min intervals. A 2s poll on an
indexed table is trivial for Postgres. Adding persistent notification channels
introduces reconnection/failover complexity for a problem that doesn't exist yet.

---

### Q4. Version-catalog source

**Decision:** A Postgres table (`deploy.platform_versions`) populated by a release
workflow (`cut-platform-release.yml`, `workflow_dispatch`). Not a checked-in
manifest.

**Release ownership:**
- **Cutting:** `cut-platform-release.yml` (repo admins, workflow_dispatch)
- **Yanking:** same workflow with `action=yank` OR admin API
  `PATCH /admin/platform-versions/{version}`
- **Channel promotion:** admin API (beta → stable)

**Rationale:** A DB table makes new releases immediately queryable by
`GET /platform-versions` without redeploying the deploy module. Mutable operations
(yank, channel promotion) are natural on a DB row, awkward on a committed file.

**Follow-up:** #3038

---

### Q5. Deploy-capable IAM tier

**Decision:** Ship a new `deploy-write.cfn.yaml` template granting
`AdministratorAccess` (no tag-scoped conditions). Safety lives at the trust
boundary + an ADP-authored permission boundary — NOT tag-scoped permissions.
Offer tier selection at connect time (not a separate upgrade step). The Connect-AWS
dialog gains a "Permission level" radio: **Read-only** (default, existing) |
**Deploy-capable** (new template).

**Why NOT tag-scoped `AdministratorAccess`:** A provisioning role creates resources
that don't have tags yet. `aws:ResourceTag` conditions fail on `Create*` calls
(the tag doesn't exist at evaluation time). `aws:RequestTag` only works for services
that support tag-on-create with that condition key — many don't (verified: the
platform creates 53+ IAM roles, EKS clusters, RDS instances, VPCs — all must be
created before they can be tagged). This is the fundamental reason tag-scoped
permissions don't work for infra provisioning.

**Minimum viable policy shape:**
- **Identity policy:** `AdministratorAccess` (AWS managed policy). Matches the
  existing `full-admin.cfn.yaml` pattern at
  `modules/agent-factory/agent-worker-image/aws/full-admin.cfn.yaml:94-95`.
- **Permission boundary (optional, customer-inspectable):** An ADP-authored
  boundary policy (`ADP-Deploy-Boundary`) embedded in the same CFN template.
  Modeled after the runner boundary at
  `modules/agent-factory/runner-infra/infrastructure/iam.tf:83-176`:
  - `DenyDangerousActions`: IAM user creation, Organizations, billing, account
    settings (same deny list as runner boundary lines 156-174).
  - Region lock (optional param): `aws:RequestedRegion` condition restricts to
    customer-specified deploy region(s).
  - The boundary is **advisory** (customer can inspect/modify it in their account)
    and **does not break deploy flows** — it only prevents escalation paths ADP
    will never use.

**Trust-boundary controls (existing, no changes needed):**
- `sts:ExternalId` (confused-deputy guard) — `aws_role_v1.yaml:44`
- `aws:PrincipalArn` locked to gateway IRSA role — `aws_role_v1.yaml:45`
- `aws:RequestTag/adp:user_id` session-tag requirement — `aws_role_v1.yaml:46`
- CFN-installed (customer owns the stack, can uninstall/revoke instantly)
- Customer-side SCPs and service control policies (proven enforced on #2899)

**Storage:** `user_credentials.scopes.permission_tier` = `"readonly"` | `"deploy"`.
Deploy module validates this on `POST /deploys`.

**Upgrade path for existing connections:** "Upgrade to deploy-capable" button in
Settings → Connections → CFN stack-update URL → re-verify → update scopes.

**Rationale:** Industry norm for SaaS-deploys-into-your-account (Spacelift, env0,
Terraform Cloud) is broad permissions with trust-boundary safety. The existing
`full-admin.cfn.yaml` already grants plain `AdministratorAccess` for the hosted
agent path. The runner-infra boundary policy proves the deny-list boundary pattern
works in production. A separate "enable deploy" upgrade step is worse UX (two
clicks, confusing state).

**Follow-up:** #3039 (revised to AdministratorAccess + permission boundary approach)

---

### Q6. `/internal/deploy-phase-update` placement

**Decision:** Place it on the deploy module (`platform-deploy-mgmt`), not the
gateway.

**Endpoint:** cluster-internal
`http://platform-deploy-mgmt.adp-deploy:8000/internal/phase-update` — reachable by
the orchestrator pod via K8s DNS. Auth: IRSA (validate `X-Caller-Identity` against
agent-registry, same pattern as gateway internal endpoints).

**Rationale:** The module that owns the schema owns its write paths. Putting a
`deploy` schema writer on the gateway violates the one-way dependency invariant
(§1a). The gateway's `/internal/v1/*` endpoints are services it provides to others
(credential-assume-role, resolve-user); the phase-update is the reverse direction.

---

### Q7. Auth/shared packaging

**Decision:** Extract into a single internal Python package
(`packages/adp-gateway-core/`). Both `bedrockgateway` and `platform-deploy-mgmt`
declare a monorepo path dependency. No PyPI publication.

**Contents:** `shared/models/`, `shared/schemas/`, `shared/config.py`,
`shared/database.py`, `shared/exceptions.py`, `auth/middleware.py`,
`auth/token_manager.py`, `auth/schemas.py`, `auth/dependencies.py`,
`auth/cognito_jwt.py`, `auth/exceptions.py`.

**Migration:** compatibility shim in gateway initially (re-exports from
`adp_gateway_core`), followed by a bulk import rename.

**Rationale:** Having `platform-deploy-mgmt` depend on the full `bedrockgateway`
package would pull in proxy/admin/LiteLLM — bloated and tightly coupled. Two
separate packages (auth, shared) adds inter-package dependency for no gain. One
extracted package is the minimum correct unit.

**Follow-up:** #3040

---

### Q8. Phase 9 (Link GitHub) non-interactive

**Decision:** Model Phase 9 as `needs_human` in the SPA from day one. Land #2592
in parallel — when it ships, the manifest-flow trigger embeds in the Phase 9 panel.
**No hard dependency of #1143 on #2592.**

**Rationale:**
- Bug #2682 proves the manifest callback is broken today.
- Even when fixed, GitHub's manifest flow **requires human confirmation** (a
  browser page the user must click). "Fully non-interactive" is unachievable.
- The `needs_human` state already exists in the design's state machine (§2.3).
  Phase 9 is its canonical use case.
- Blocking #1143 on #2592 (which is an EPIC with sub-bugs) would delay
  indefinitely.

**When to revisit:** when/if GitHub ships a fully server-side App creation API.

---

### Q9. Zero-touch deploy engine — customer exposure gate

**Decision:** Gate on **≥3 consecutive fresh-account deploys (different accounts)
where Phases 1–8 succeed without `needs_human`**. Phase 9 may remain `needs_human`
(interactive by design). Track as a reliability gate within EPIC #2571.

**Internal-only until gate met:** the SPA (#1143) can ship before the engine hits
the bar — it transparently shows `needs_human` states, which is honest UX. "Not
ready for customers" means we don't market it or make it the default onboarding
path.

**Current state:** deploy #2899 (979157915401) succeeded but with mid-flight fixes.
EPIC #2571 / #684 still OPEN. The gate is NOT yet met.

---

### Q10. Control-plane self-upgrade

**Decision:** Explicitly deferred. Not in scope for #1143 or any near-term work.

**Escalation trigger (documented):** if a future requirement says "the
control-plane must self-upgrade via the deploy module" (e.g., air-gapped
environments), THEN the deploy module must become a fully separate service (own DB,
own auth, own lifecycle independent of the gateway).

**Rationale:** Today the deploy module upgrades *customer* accounts, not the control
plane. The control plane is deployed by GitHub Actions (PR merge → workflow). Self-
upgrade would require targeting the platform's own account — a fundamentally
different trust model. The module boundary + separate image (§1a) mean the
extraction is mechanical when needed; designing for it now adds complexity with no
user benefit.
