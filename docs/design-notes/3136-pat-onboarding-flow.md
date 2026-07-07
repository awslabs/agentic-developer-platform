# Design Note: PAT-Based Onboarding — Cognito Login + User PAT for Agent Execution (Issue #3136)

> **Status**: Design-complete (spike output)
> **Author**: @agent-architect
> **Date**: 2026-07-07
> **Issue**: #3136 — PAT-based onboarding flow
> **Mode**: Per-issue spike
> **Verdict**: Design-complete with recommended v1 scope (hybrid model).

---

## 0. Executive Summary

Today's agent onboarding requires an org owner to install a GitHub App and a
per-tenant `tenants/<tenant>/github-app` secret in Secrets Manager. This is
fragile (#3116), org-owner-gated, and blocks personal-repo users entirely.

A PAT-based flow reduces onboarding to "log in, paste token." The PAT replaces
the **execution leg** (clone/push/PR) of the GitHub App flow, but **cannot**
replace the **trigger leg** (webhook events require an installed App).

This note recommends a **hybrid model (Option B)** for v1: keep the GitHub App
for trigger (webhook → Lambda → SQS), but allow users to register a PAT in
the vault that the worker uses *instead of* minting an App installation token.
This is the smallest shippable slice: zero new trigger infrastructure, reuses
existing vault CRUD, and unblocks personal-repo users when combined with a
dashboard-dispatch path (gateway → SQS) for users without an App installed.

---

## 1. Trigger Model Without an App Installation

### The Problem

The GitHub App does **two jobs**:

1. **Trigger (inbound)**: `@agent-<persona>` comments arrive as webhooks —
   only possible because the App is installed on the target repo. A PAT cannot
   receive webhooks.
2. **Execution (outbound)**: The worker fetches `tenants/<tenant>/github-app`
   from Secrets Manager, mints a 1-hour installation token, and uses it for
   clone/push/PR/comments (`entrypoint.py:427-508`).

A PAT replaces only job 2. For job 1, we need a trigger path that doesn't
depend on an installed App.

### Evaluated Options

| Option | Shape | Pros | Cons |
|--------|-------|------|------|
| **(A)** Dashboard dispatch | Gateway REST → SQS `agent-submit.fifo` | No App needed at all; Cognito-authenticated; clean for personal repos | New gateway route + envelope factory; no webhook-based @mention flow; must build dashboard UI |
| **(B)** Hybrid: App for triggers, PAT for execution | Keep App installed for webhooks; PAT replaces per-tenant `github-app` secret at execution time | Smallest change; webhook flow unchanged; PAT optional (fallback to App token if present) | Still need App install for @mention trigger; personal repos without App need Option A too |
| **(C)** Polling | Worker polls for new issues/comments | N/A | Non-starter: latency, rate limit burn, unjustifiable complexity |

### Recommendation: Hybrid (B) as primary, Dashboard-dispatch (A) as v1.1

**v1**: Hybrid — App stays installed for trigger. Worker resolves PAT from vault
before falling back to App installation token. This covers the "org has App
installed but individual user wants PRs attributed to them" use case, which is
the 80% case.

**v1.1**: Dashboard-dispatch — gateway gains a `POST /agent/dispatch` endpoint
that produces an `agent-submit.fifo` message for users who have NO App installed
(personal repos). This is additive and does not change the webhook path.

### Dashboard-Dispatch Envelope Contract (v1.1)

The existing envelope schema (`webhook-ingress/lambda/common/envelope.py`) is
sufficient with these adaptations:

```python
WebhookEnvelope(
    version="1.0",
    channel="dashboard",                    # NEW: distinguishes from "github"
    tenant_id=caller.org_id,               # From Cognito JWT
    persona=request.persona,               # Caller-chosen
    actor=Actor(
        user_id=caller.user_id,
        org_id=caller.org_id,
        github_id=caller.github_numeric_id,
        github_login=caller.github_login,
        is_bot=False,
    ),
    source_ref=SourceRef(
        installation_id=0,                  # SENTINEL: signals PAT-mode
        repo=request.repo,
        issue=request.issue,
        pr=request.pr,
    ),
    intent=Intent(
        trigger="dashboard_dispatch",       # NEW trigger type
        persona=request.persona,
    ),
    correlation=Correlation(
        correlation_id=new_uuid(),
        root_human_id=caller.user_id,
        is_human_rooted=True,
    ),
    payload={},                             # No webhook payload for dashboard dispatch
)
```

**Critical change**: `installation_id=0` currently triggers the poison-message
guard (`entrypoint.py:343-361`). The worker must be updated to treat
`installation_id=0` + `channel="dashboard"` as a **PAT-mode signal** rather
than a poison message.

Worker logic (pseudo):
```python
if installation_id in (0, None, "0"):
    if envelope.get("channel") == "dashboard":
        # PAT mode: resolve token from vault instead of App
        token = resolve_pat_from_vault(user_id, tenant_id, repo)
    else:
        # Original poison guard: delete and exit
        _delete_message(...)
        return 1
```

---

## 2. Token Selection in the Worker

### Resolution Algorithm

The worker must decide: **PAT vs App installation token**. The decision uses a
**fallback chain** with per-envelope signals:

```
1. If envelope.channel == "dashboard" (PAT-dispatch):
     → MUST use PAT (no installation_id exists)
     → Fetch via gateway credential client: service="github", scope="user"
     → If no PAT found → fail with clear error (user hasn't registered one)

2. If envelope.channel == "github" (webhook-triggered):
     → Check user preference (envelope field: `token_source`):
       a. "pat"  → use PAT (user explicitly chose)
       b. "app"  → use App installation token (default)
       c. absent → use App installation token (backward compat)
     → PAT fetch failure with fallback to App is acceptable if
       `token_source` is absent (graceful degradation)
```

### Where the Decision Happens

**In `entrypoint.py`**, between Step 1 (parse envelope) and Step 2 (vault
fetch). New Step 2 becomes:

```
Step 2a: Determine token source (channel + token_source field)
Step 2b: If PAT mode:
           → Call gateway_credential_client.raw_read(
               user_id=actor.user_id,
               service="github",
               label="github-pat"    # Convention: well-known label
             )
           → Set GITHUB_TOKEN = PAT value
           → Skip Step 3 (no App token mint needed)
         If App mode:
           → Existing flow (vault.get_secret → mint_installation_token)
```

### Envelope Extension

The `token_source` field is **optional** and only set when a user explicitly
configures PAT-mode in the dashboard for webhook-triggered runs:

```python
# In WebhookEnvelope (addition):
token_source: str | None = None  # "pat" | "app" | None (default=app)
```

This field is set by the webhook-ingress Lambda when the identity-index row for
the installation has a `token_source_override` attribute (see Q5 for policy
storage).

---

## 3. Attribution

### PAT Runs Act as the Human

When using a PAT, all GitHub operations (commits, PRs, comments) are attributed
to the **human user**, not a `[bot]` identity. Consequences:

| Aspect | App Token (today) | PAT |
|--------|-------------------|-----|
| Commit author | `<app-id>+adp-agent[bot]@...` | User's GitHub identity |
| PR author | App bot account | User's GitHub account |
| Self-review | User CAN approve bot's PR | User CANNOT approve their own PR |
| Branch protection | Bot exempt from CODEOWNERS | User must satisfy all rules |
| Provenance (#779) | `user_kind=bot` markers | `user_kind=human` — breaks bot-identity metrics |

### Self-Review Problem

For **solo users** (personal repos, no team), the self-review problem is moot:
personal repos typically don't have branch protection requiring approvals.

For **team contexts**, the agent should:
1. Open the PR as the user (PAT).
2. Request review from a teammate (configurable in persona/org settings).
3. The user does NOT need to approve — another team member does.

If the org requires CODEOWNERS approval and the PAT user is a CODEOWNER, this
is a real blocker. Mitigation: document that PAT mode is **not suitable** for
repos where the PAT holder is the sole CODEOWNER and approvals are required.

### Provenance Markers

The correlation system (`ADP_CORRELATION_ID`, `ADP_ROOT_HUMAN_ID`,
`ADP_IS_HUMAN_ROOTED`) is envelope-sourced and survives regardless of token
type. The issue is **attribution in GitHub's view** (commits/PRs) vs **ADP's
internal provenance** (DDB correlation records).

Recommendation:
- **ADP-internal provenance**: Unchanged. Correlation IDs, lineage, and
  webhook-events DDB records still track that an agent ran. Add a field
  `token_mode: "pat" | "app"` to the webhook-events row.
- **GitHub-visible attribution**: Accept that PRs look human-authored.
  Mitigate with a standard PR footer: `> This PR was generated by ADP agent
  @<persona> (run: <correlation_id>) using a personal access token.`
- **Metrics**: Filter dashboards by `token_mode` field to separate
  bot-authored vs PAT-authored agent actions.

---

## 4. Security Posture

### Fine-Grained PAT Permissions (Minimum)

GitHub fine-grained PATs scope to specific repos (unlike classic PATs). The
minimum permissions for agent execution:

| Permission | Access | Why |
|------------|--------|-----|
| Contents | Read & Write | Clone, push, branch creation |
| Issues | Read & Write | Read issue body, post comments, update labels |
| Pull requests | Read & Write | Create PR, push to PR branch, request reviewers |
| Metadata | Read | Required for all fine-grained PATs |

Optional (persona-dependent):
- **Checks** (Read & Write): If the agent posts check-run annotations.
- **Actions** (Read): If the agent monitors workflow runs.

**Enforce at registration**: The vault UI should validate PAT permissions at
registration time by calling `GET /repos/{owner}/{repo}/installation` or
equivalent scope-check endpoint. If the PAT lacks required permissions, warn
(non-blocking for v1; blocking in v2).

### Storage

Use the **existing vault path** — no new storage infrastructure:

- **Postgres**: `UserCredential` row with `credential_type="bearer"`,
  `service="github"`, `label="github-pat"`, `owner_scope="user"`.
- **Secrets Manager**: Raw PAT value at `adp/users/<cognito_sub>/github/<label>`
  (existing SM namespace from vault design — see
  `docs/user-identity-and-credentials-design.md:59`).
- **Strict mode**: Set `strict=True` so the PAT is ONLY returned to the owning
  user's scope — prevents accidental escalation to team/org.

### Redaction in Worker Logs/Transcripts

The existing `GITHUB_TOKEN` is **already redacted** in agent transcripts:
- `entrypoint.py` uses `GIT_ASKPASS` helper (line 486) — token never appears in
  git CLI args.
- Claude Code's `--dangerouslySkipPermissions` logs tool calls but not env vars.
- The `adp-cred` CLI returns values to stdout which are consumed programmatically,
  not logged.

For PAT mode, the same redaction applies: the PAT is set as `GITHUB_TOKEN` env
var and consumed identically to the App token. **No additional redaction work
needed** — the worker doesn't distinguish between token types after Step 2.

### Expiry and Rotation UX

- **`expires_at` field exists** on `UserCredential` — set when PAT is registered.
- **Nag mechanism**: The dashboard should show a warning banner when a PAT's
  `expires_at` is within 7 days. Implementation: query
  `GET /auth/credentials?scope=user` and filter client-side (or add a
  `GET /auth/credentials/expiring?days=7` convenience endpoint).
- **Push notification**: Extend the existing notification system (if any) or use
  email. For v1, dashboard banner is sufficient.
- **Rotation**: User deletes old credential, registers new PAT. No zero-downtime
  rotation needed for v1 (agent runs are short-lived; if a run starts with a
  valid PAT and the PAT is rotated mid-run, the 1-2h run lifetime is within
  GitHub's revocation grace period).

### Revocation Handling Mid-Run

When a PAT is revoked while a run is in-flight:
- GitHub API calls return `401 Unauthorized`.
- The worker's `TokenManager` (JS-side) will attempt refresh — but for PAT mode,
  there is no refresh mechanism (unlike App tokens which re-mint from
  `GH_APP_PRIVATE_KEY`).
- **Behavior**: Worker fails with a clear error: "GitHub token expired or
  revoked. Please register a new PAT in Settings > Credentials."
- **Zero-token guard equivalent**: Before clone (Step 5), validate the PAT with
  a lightweight API call (`GET /user` — costs 1 rate-limit point). If it fails,
  abort immediately with a user-facing error instead of proceeding to clone and
  failing cryptically.

---

## 5. Interplay with #3134 Trigger Lockdown

### Current State

Trigger policy (#3134) operates on the **installation** as the trust anchor:
- `identity-index` DDB rows key on `installation_id` → `tenant_id` mapping.
- Rate limits are per-`tenant_id` in the `rate-limits` DDB table.
- Bot-loop guards track per-`correlation_id` in `correlation-pointers`.

### PAT-Triggered Runs Have No Installation

For dashboard-dispatch (v1.1), there is no `installation_id`. But the trust
model is **arguably stronger**:

| Trust property | Webhook (App) | Dashboard (PAT) |
|----------------|---------------|-----------------|
| Who can trigger? | Anyone who can @mention in an installed repo | Only Cognito-authenticated users with a valid JWT |
| Tenant resolution | Derived from installation_id → DDB lookup | Directly from JWT `custom:org_id` claim (server-verified) |
| Rate limiting | Per-tenant (via identity-index) | Per-tenant + per-user (JWT gives both) |
| Bot-loop risk | High (agents trigger each other via comments) | None (human initiates from dashboard) |

**Recommendation**: Dashboard-dispatched runs **bypass installation-based
lockdown** because they have a stronger trust signal (Cognito JWT). The
equivalent gate is:

1. **Cognito authentication** (mandatory — gateway middleware).
2. **Tenant membership verification** (`TenantMembership.is_active=True`).
3. **PAT ownership check** (credential resolver enforces `user_id` match).
4. **Rate limit** (reuse `rate-limits` DDB table, keyed by `tenant_id` or
   `user_id` — configurable).

For webhook-triggered runs using PAT as `token_source`:
- The installation-based lockdown still applies (the trigger came through the
  App webhook path).
- The only difference is the *execution token* — lockdown is about **who can
  trigger**, not **what token executes**.

### Hard dependency: #3142 credential-authorization binding

Gate 3 above ("PAT ownership check — credential resolver enforces `user_id`
match") does **not hold on main today**. Per the #3142 spike
(`docs/design/credential-authorization-binding.md`), the `user_id` reaching
`/internal/v1/credential-raw-read` is a bearer parameter: the worker's
`adp-cred` client reads it from the pod's `ADP_USER_ID` env var, and the only
gate (`verify_internal_or_irsa`) authenticates the pod's shared IRSA role, not
the human. A prompt-injected agent that sets `ADP_USER_ID=<victim>` pulls the
victim's credential — and where today that yields a scoped App installation
token, in PAT mode it yields the victim's PAT, whose blast radius is
**everything the victim's token reaches across GitHub**.

Consequences for this design:

1. **C1 (worker PAT resolution) MUST NOT ship before #3142 Phase 2
   (enforcement)** — credential reads bound to the run's server-resolved
   `authorized_user_id` via `invocation_id`, body `user_id` ignored. The PAT
   fetch in Step 2b then resolves for the registry-bound user, not whatever
   the pod environment claims.
2. **Dashboard dispatch (C7) must write the same run-registry row** —
   `spawn` equivalent sets `authorized_user_id` from the Cognito JWT so
   dashboard-dispatched runs get the same binding as webhook runs. The
   `installation_id=0` envelope variant does not exempt it.
3. **Adversarial tests A1/A2 from #3142 gain PAT variants** — env-forged
   raw-read of another user's `github-pat` must be denied, and the live-agent
   E2E (injection payload in issue body) must be re-run with a registered PAT
   in the sandbox tenant.

Cross-link: #3136 is blocked on #3142 (noted on both issues).

---

## 6. Rate Limits and Scale

### GitHub Rate Limits

| Token type | Limit | Scope |
|------------|-------|-------|
| App installation token | 5,000 req/h per installation | Shared across all users of that App on that org |
| Fine-grained PAT | 5,000 req/h per user | Pooled across ALL repos the PAT reaches |
| Classic PAT | 5,000 req/h per user | Same as fine-grained |

### Analysis for Agent Workloads

A typical agent run consumes:
- Clone: 5-20 API calls (refs, pack negotiation)
- Issue read: 1-3 calls
- File operations: 10-50 calls (tree reads, blob fetches via REST)
- Push: 5-10 calls
- PR creation: 3-5 calls
- Comments: 2-5 calls
- **Total per run: ~30-100 API calls**

At 5,000 req/h, a single PAT supports **50-160 concurrent runs per hour** per
user. For most individual users (1-5 concurrent runs), this is well within
limits.

### Backoff

The existing `TokenManager` (JS worker) implements exponential backoff on 403
rate-limit responses. For PAT mode:
- Same backoff applies (HTTP 403 with `X-RateLimit-Remaining: 0`).
- **No re-mint path**: Unlike App tokens (which can be re-minted from the
  private key against a different installation), a PAT is a single token.
  Backoff is the only strategy.
- Add `X-RateLimit-Remaining` monitoring: if remaining < 100 at any point
  during a run, log a warning and reduce parallelism in git operations.

### Scale Concern

For **team/org PATs** (shared token across multiple users — NOT recommended
for v1 but will be requested):
- 5,000 req/h shared across all team members' runs is tight.
- Mitigation for v2: per-user PATs only in v1; team-shared PATs get a rate
  budget check before dispatch.

---

## 7. Scope of v1 — Recommended Smallest Shippable Slice

### v1.0: PAT as Execution Token (Hybrid)

**Scope**: Worker resolves PAT from vault when available, falls back to App
installation token. Webhook trigger unchanged.

**What ships**:
1. Worker `entrypoint.py` gains PAT resolution branch (Steps 2a/2b above).
2. Vault UI gains "Register GitHub PAT" flow (service="github",
   credential_type="bearer", label convention, permission guidance).
3. Envelope gains optional `token_source` field (backward-compatible).
4. Zero-token guard updated for PAT mode (validate before clone).
5. Webhook-events DDB gains `token_mode` attribute for provenance.
6. Documentation: PAT permission requirements, self-review limitations.

**What doesn't ship (deferred)**:
- Dashboard dispatch (v1.1).
- PAT permission validation at registration.
- Expiry nag notifications.
- Team-shared PATs.

### v1.1: Dashboard Dispatch (No App Required)

**Scope**: Gateway `POST /agent/dispatch` endpoint produces SQS messages for
users with no App installed.

**What ships**:
1. Gateway route `POST /agent/dispatch` (Cognito-authenticated).
2. SQS publisher in gateway (reuse `sqs_publisher.py` pattern from
   webhook-ingress — or inline `boto3.client('sqs').send_message`).
3. Worker poison-guard update (channel="dashboard" bypass).
4. Dashboard UI: "Run agent on repo/issue" button.
5. Rate limiting for dashboard-dispatch (per-user, per-tenant).

### v1.2: Polish

- PAT permission validation at registration time.
- Expiry nag in dashboard.
- PR footer with agent provenance for PAT-authored PRs.
- Metrics dashboards with `token_mode` dimension.

---

## 8. Child Issue Breakdown

### v1.0 Issues

| # | Title | Module | Depends on |
|---|-------|--------|------------|
| C1 | Worker: PAT resolution branch in entrypoint.py | agent-worker-image | #3142 Phase 2 (enforcement) |
| C2 | Vault UI: "Register GitHub PAT" credential flow | gateway (frontend + backend) | — |
| C3 | Envelope: add optional `token_source` field | webhook-ingress + worker | C1 |
| C4 | Worker: zero-token guard for PAT mode (validate before clone) | agent-worker-image | C1 |
| C5 | Webhook-events: add `token_mode` attribute | webhook-ingress Lambda | C1 |
| C6 | Docs: PAT onboarding guide + permission matrix | docs/ | C2 |

### v1.1 Issues

| # | Title | Module | Depends on |
|---|-------|--------|------------|
| C7 | Gateway: `POST /agent/dispatch` SQS producer | gateway | C1 |
| C8 | Worker: poison-guard bypass for channel=dashboard | agent-worker-image | C7 |
| C9 | Dashboard UI: "Run agent" dispatch button | gateway (frontend) | C7 |
| C10 | Rate limiting: per-user dispatch throttle | gateway | C7 |

### v1.2 Issues

| # | Title | Module | Depends on |
|---|-------|--------|------------|
| C11 | PAT permission validation at registration | gateway | C2 |
| C12 | Expiry nag: dashboard warning for expiring PATs | gateway (frontend) | C2 |
| C13 | PR footer: agent provenance for PAT-authored PRs | agent-worker-image | C1 |
| C14 | Metrics: `token_mode` dimension in OTEL + dashboards | webhook-ingress + worker | C5 |

---

## 9. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| PAT leaked from pod env/logs | Low (same redaction as App token) | High (full user access) | Existing GIT_ASKPASS pattern; audit logging; short-lived runs |
| User stores classic PAT (all repos) instead of fine-grained | Medium | High (blast radius) | UI guidance; v1.2 permission validation; docs |
| Self-review blocks team workflows | Medium | Medium | Document limitation; recommend fine-grained PAT scoped to non-CODEOWNER repos |
| Rate limit exhaustion (heavy user) | Low (50+ runs/h needed) | Medium (runs queue/fail) | Pre-run rate check; `X-RateLimit-Remaining` monitoring |
| Expired PAT causes cryptic failure | Medium | Low (clear error path) | Zero-token guard validates before clone; expiry nag in UI |

---

## 10. File References (Verified on origin/main)

| Component | File | Relevant Lines |
|-----------|------|----------------|
| Envelope schema | `modules/agent-factory/webhook-ingress/lambda/common/envelope.py` | 1-107 (full dataclass) |
| Poison guard | `modules/agent-factory/agent-worker-image/entrypoint.py` | 340-361 |
| Vault fetch + token mint | `modules/agent-factory/agent-worker-image/entrypoint.py` | 427-508 |
| UserCredential model | `modules/gateway/src/shared/models/vault.py` | 91-179 |
| CredentialType enum | `modules/gateway/src/shared/models/vault.py` | 31-39 |
| Credential resolver | `modules/gateway/src/shared/services/credential_resolver.py` | 77-187 |
| Vault CRUD routes | `modules/gateway/src/auth/vault_routes.py` | 108-205 |
| Gateway credential client | `modules/agent-factory/agent-worker-image/lib/gateway_credential_client.py` | 51-220 |
| Agent-trigger handler | `modules/agent-factory/webhook-ingress/lambda/github/agent_trigger.py` | 1-100 |
| Identity-index DDB | `modules/agent-factory/webhook-ingress/infra/dynamodb.tf` | (identity-index table) |
| KEDA ScaledJob | `modules/agent-factory/webhook-ingress/infra/scaledjob.tf` | (pod env vars) |
| User identity linking | `docs/user-identity-and-credentials-design.md` | Full doc |
| Invisible tenancy (v2) | `docs/design-notes/3074-invisible-tenancy-per-action-resolution.md` | Context for tenant resolution |

---

## 11. Decision Log

| Decision | Chosen | Rejected | Rationale |
|----------|--------|----------|-----------|
| Trigger model | Hybrid (B) | Full dashboard-only (A), Polling (C) | Smallest delta; webhook flow unchanged; A is additive v1.1 |
| Token selection | Envelope field + vault fallback | Per-tenant config, per-user global pref | Envelope-level gives per-run control; config would require new DDB schema |
| PAT storage | Existing vault (UserCredential + SM) | New DDB table, new SM namespace | Zero infrastructure work; vault designed for this |
| PAT scope | User-only, strict=True | Team/org shared PATs | Security: PAT = personal identity; sharing defeats attribution |
| Dashboard-dispatch authz | Cognito JWT (stronger than App) | New installation-like DDB row | JWT is server-verified, per-user, per-tenant; no new trust model needed |
| Self-review mitigation | Document limitation | Auto-switch to App token for PR creation | Complexity not justified; solo users don't need approvals |

---

*End of spike. No code ships from this issue. Child issues above get their own
five-section specs (Description / Impact / Design / Deployment / Validation)
when filed.*
