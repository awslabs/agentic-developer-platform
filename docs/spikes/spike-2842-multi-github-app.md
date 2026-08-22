# SPIKE-2842: Any number of private GitHub Apps per deployment (tenant-owned, per-GitHub-org)

> **Issue**: #2842
> **Date**: 2026-07-03
> **Agent**: @agent-architect
> **EPIC**: #421 (User-authored agents / self-serve) · absorbs #467 (BYO App via manifest)
> **Builds on**: #2593 (register flow, CLOSED), #2755/#2769 (Postgres-authoritative
> installation→tenant mapping), #2732 (sibling detection, CLOSED)
> **Must respect**: #2823 (`setup_url`), #2824 (webhook_secret write-through), #2677
> (globally-unique App names), #2724 (public-App auto-register hole)

## Verdict: **PASS — build it. Postgres registry + per-App secret paths + header-routed webhook validation.**

The singleton is not a data-model limitation; it is three hardcoded assumptions that can be
lifted independently and shipped in order:

1. **"The App exists" == a fixed secret path is populated.** Replace with a
   `github_apps` Postgres table (authoritative registry). The secret paths become
   per-App (`adp/<env>/github-apps/<app_id>/{key,meta}`); the table row is the index.
2. **Webhook validation reads ONE `WEBHOOK_SECRET_ARN`.** Replace with per-App secret
   selected by the `X-GitHub-Hook-Installation-Target-ID` header (the App id GitHub
   stamps on every delivery), cached per-container keyed by App id.
3. **Credential resolution reads the platform App's fixed paths.** Make every reader
   `(tenant, app_id)`-scoped, resolving the App via the registry.

GitHub login (OAuth broker) stays pinned to exactly ONE App via a `login_app` boolean on
the registry — today's behavior preserved, first-registered-with-OAuth wins.

This design is **additive**: embark1's existing public App keeps working as "row zero"; per-org
private Apps are new rows. It does **not** require ripping out the `adp-agent-platform-*` paths on
day one — a grandfather shim reads the legacy paths as a synthetic registry row until migrated.

---

## Current state (verified against live code, 2026-07-03)

The single-App assumption is spread across exactly the readers #2842 lists. Confirmed by reading:

| Reader | File:sym | What it hardcodes |
|---|---|---|
| Install slug | `service.py:107` `_get_github_app_slug` → provider | `adp-agent-platform-meta` slug |
| Install/delete creds | `service.py:131` `_get_github_app_credentials` → provider | `adp-agent-platform-{id,key}` |
| Register dup-guard | `service.py:656` `_check_existing_app_secret` | reads `-id`; short-circuits `already_registered` |
| Store creds | `service.py:1057` `_store_app_credentials` | writes the 3 fixed paths + broker OAuth secret |
| App status | `service.py:1277` `get_app_status` | reads `-id`/`-meta` |
| Rotate / disconnect | `service.py:1367`/`1472` | mutate the 3 fixed paths |
| Runtime creds provider | `github_app_provider.py:126` `_fetch_from_sm` | `adp/<env>/github-app/adp-agent-platform-{id,key,meta}` |
| Per-tenant seed (gateway) | `tenant_secret.py:39` `_seed_secret_sync` | copies gateway's OWN `BG_GITHUB_APP_*` into `adp/<env>/tenants/<t>/github-app` |
| Webhook HMAC | `handler.py:63` `_resolve_webhook_secret` | single `WEBHOOK_SECRET_ARN` |
| Webhook auto-provision | `handler.py:209` `_auto_provision_tenant_github_app_secret` | copies PLATFORM `adp-agent-platform-{id,key}` into the tenant path |
| Sibling own-slug | `handler.py:294` `_get_own_app_slug` | reads `adp-agent-platform-meta` slug (single own App) |

**Data model** (`vault.py:203`, migration 013): `ChannelTenantMap` maps `(provider,
provider_scope_id=github account id) → org_id`, unique on `(provider, provider_scope_id)`.
It maps **GitHub account → tenant**. It does **not** know which App a given installation
belongs to. There is no App registry today; "the App" is implicit in the fixed secret path.

**IAM** (`gateway/infra/main.tf:457`): gateway already wildcards `adp/*/github-app/*` — a
per-App path family under that prefix needs **no new gateway grant**. Webhook Lambda
(`webhook-ingress/infra/iam.tf:159`) enumerates the **two fixed platform ARNs** and writes
`adp/<env>/tenants/*` — this is the tightest coupling and the one Terraform change with teeth.

**Latest Postgres migration**: `019_knowledge_assets`. New migration would be `020_github_apps`.

---

## Q1 — App registry: where does the set of registered Apps live?

**Decision: new Postgres table `github_apps`.** Postgres, not DDB: this is relational,
admin-UI-queried (list Apps for a tenant), join-heavy (App ↔ tenant ↔ installations),
low-write, and needs a UNIQUE constraint on the GitHub app_id for duplicate protection —
exactly the storage-fit rule in the persona guide. It sits alongside `channel_tenant_map`
and `organizations` in the same DB the gateway already owns.

```sql
CREATE TABLE github_apps (
    id            VARCHAR(36)  PRIMARY KEY,            -- internal uuid
    github_app_id VARCHAR(32)  NOT NULL,               -- GitHub's numeric App id (string)
    slug          VARCHAR(255) NOT NULL,               -- github.com/apps/<slug>
    owner_login   VARCHAR(255) NOT NULL,               -- org/user that owns the App on GitHub
    owner_type    VARCHAR(16)  NOT NULL,               -- 'org' | 'user'
    tenant_id     VARCHAR(255) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    is_public     BOOLEAN      NOT NULL DEFAULT FALSE, -- embark1 public App = TRUE
    login_app     BOOLEAN      NOT NULL DEFAULT FALSE, -- see Q6 (exactly one TRUE per deployment)
    secret_prefix VARCHAR(255) NOT NULL,               -- adp/<env>/github-apps/<github_app_id>
    created_by    VARCHAR(255),                        -- users.id of registering admin
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_github_apps_github_app_id ON github_apps (github_app_id);
CREATE INDEX ix_github_apps_tenant ON github_apps (tenant_id);
-- Exactly one login App per deployment (Postgres partial unique index):
CREATE UNIQUE INDEX uq_github_apps_single_login ON github_apps (login_app) WHERE login_app;
```

**Relationship to `ChannelTenantMap`** (installation → App → tenant): keep
`ChannelTenantMap` as the **installation→tenant** authority (it already is, post-#2769). Add
a nullable `github_app_id` column to `channel_tenant_map.metadata` (JSON — no schema change
needed) recording which registered App produced that installation. Resolution chain becomes:

```
webhook delivery
  ├─ X-GitHub-Hook-Installation-Target-ID  → github_apps row  (which App)
  └─ payload.installation.id               → ChannelTenantMap  (which tenant)
```

Both must agree; a mismatch (installation belongs to tenant A but delivered via tenant B's
App) is the cross-tenant bug class from Impact analysis and must hard-fail (see Q3).

**Rejected**: storing the App set only in DDB identity-index (no relational integrity, no
UNIQUE on app_id, can't drive the admin list UI cleanly). **Rejected**: keeping "secret
exists == registered" (that IS the singleton; enumerating SM by prefix on every request is
slow and racy).

## Q2 — Secret layout + migration + IAM/KMS

**Per-App paths** (replaces the three fixed `adp-agent-platform-*` paths):

```
adp/<env>/github-apps/<github_app_id>/key    → private key PEM
adp/<env>/github-apps/<github_app_id>/meta   → {app_slug, client_id, client_secret, webhook_secret}
```

`github_app_id` (not slug) as the path segment — it is immutable and is exactly what the
webhook header (`X-GitHub-Hook-Installation-Target-ID`) carries, so validation reads the
path with zero extra lookups. The App id itself lives in the Postgres row (no separate `-id`
secret needed; the meta secret + row are sufficient).

**Migration for the 3 live accounts** (zero downtime — the grandfather shim below means no
account breaks on deploy):

| Account | Today | Registry row |
|---|---|---|
| embark1 | public `aws-e-adp-agent-dev` at `adp-agent-platform-*` | 1 row, `is_public=TRUE`, `login_app=TRUE`; `secret_prefix` points at LEGACY path (see shim) |
| 261 | pending re-register | no row until it registers; register flow creates the row |
| 812 | private App 4210311 at `adp-agent-platform-*` | 1 row, `is_public=FALSE`, `login_app=TRUE`, legacy path |

**Grandfather shim (the key to zero-downtime):** the data migration `020_github_apps` does
NOT move any secret bytes. It inserts a registry row whose `secret_prefix` is the **legacy**
`adp/<env>/github-app/adp-agent-platform` path for any deployment where that secret is
populated (detected at migration run-time by a small bootstrap script, since Alembic can't
read SM — see Deployment). Readers resolve the path from `secret_prefix`, so the legacy App
keeps working unchanged. New Apps get the new `github-apps/<id>/` layout. A later optional
housekeeping job can copy legacy→new and flip `secret_prefix`, but it is not required for
correctness.

**IAM/KMS:**
- Gateway: `adp/*/github-app/*` (main.tf:457) already covers legacy. **Add** `adp/*/github-apps/*`
  (note the plural) to the same `VaultSecretsCRUD` Resource list. KMS unchanged (#2798 grants
  Encrypt/Decrypt/GenerateDataKey on the CMK).
- Webhook Lambda (iam.tf:159): today enumerates two fixed ARNs. **Replace** with the wildcard
  `adp/<env>/github-apps/*` (read) **plus** keep the legacy two ARNs during the grandfather
  window. Add `kms:Decrypt` already present. This is the one Terraform change that gates
  multi-App webhook validation.

**Cost**: 2 SM secrets/App × ~$0.40/mo = ~$0.80/App/mo. Bounded by admin registration action.
Postgres: 1 row/App. No new compute.

## Q3 — Webhook routing

**Single ingress URL for all Apps** (unchanged — GitHub delivers every App's webhooks to the
URL baked into each App's manifest, which is the same `WEBHOOK_URL` for this deployment).

**Select the HMAC secret by App id, not a single ARN.** GitHub stamps every delivery with
`X-GitHub-Hook-Installation-Target-Type: integration` and
`X-GitHub-Hook-Installation-Target-ID: <app_id>`. Change `_resolve_webhook_secret`
(`handler.py:63`) to:

```
app_id = headers.get("x-github-hook-installation-target-id")
secret = _webhook_secret_cache.get(app_id) or read adp/<env>/github-apps/<app_id>/meta → webhook_secret
```

- **Per-container cache keyed by app_id** (dict, not the single module global `_webhook_secret`).
  Same cold-start reset semantics as today; bounded by the number of Apps installed.
- **Unknown app_id** (past deletions, sibling deployments' Apps per #2732 — GitHub fans a
  delivery to every App installed on a repo, and orphaned Apps can't be deleted via API per
  the #2795 gotcha): **return 401 `invalid_signature` / `unknown_app`** and emit a
  `WebhookUnknownApp` metric. Do NOT fall back to a default secret (that reopens #2724 and the
  cross-tenant class). An unknown App id means "not one of ours" — reject cleanly.
- **Grandfather**: if the header is absent (very old deliveries) or the app_id maps to the
  legacy row, fall back to the legacy `WEBHOOK_SECRET_ARN`. Keep this branch only during the
  grandfather window.

**This subsumes #2824**: with per-App meta secrets, the webhook secret is written where the
validator reads it *by construction* (validator reads `github-apps/<id>/meta.webhook_secret`;
register writes the same). The #2824 fix (write-through to the ingress secret) becomes moot
for new Apps but is still needed for the legacy grandfather path until 261/812/embark1 migrate.

## Q4 — Registration authz + tenancy

- **Who may register**: **platform_admin OR a tenant admin registering for their own tenant.**
  The register endpoint already runs behind admin auth; extend it so a tenant admin's
  registration sets `github_apps.tenant_id = caller's tenant` and cannot target another tenant.
  platform_admin may register on behalf of any tenant (needed for multi-customer deployments).
- **Remove the `already_registered` short-circuit** (`register_app_start` `service.py:791`).
  Duplicate protection moves to the DB: `UNIQUE (github_app_id)`. If a manifest conversion
  returns an app_id already in the table → 409 `app_already_registered`. This is stronger than
  today (which only knew about the one fixed slot).
- Registration remains the manifest-conversion flow (#2593); `_derive_app_name` owner-prefixing
  (#2677) already yields globally-unique names, which is mandatory for N Apps.

## Q5 — Resolution changes (per reader)

Every reader becomes `(tenant, app_id)`-scoped, resolving the App via the registry:

| Reader | Change |
|---|---|
| `github_app_provider.py` `_fetch_from_sm` | Take `app_id` (or `secret_prefix`) param; read `<prefix>/{key,meta}`. Cache keyed by app_id. The singleton `get_github_app_provider()` becomes a per-app_id registry of providers (dict), or a provider that takes app_id per call. |
| `service.py` `install_start` slug | Resolve the App for the caller's tenant (the tenant's registered App; if >1, UI passes app_id). Slug from that App's meta. |
| `service.py` `_get_github_app_credentials` (install_callback, delete) | Resolve App id from the ChannelTenantMap row's `metadata.github_app_id`, then creds from that App's prefix. |
| `service.py` `_store_app_credentials` | Write per-App paths + insert the `github_apps` row. Broker OAuth write only when `login_app=TRUE` (Q6). |
| `service.py` `get_app_status`/`rotate`/`disconnect` | Operate on a specified app_id (list becomes plural; UI passes app_id). |
| `tenant_secret.py` `seed_tenant_github_app_secret` | Seed from the TENANT's registered App creds, not the gateway's own `BG_GITHUB_APP_*`. Takes app_id. |
| **webhook `_auto_provision_tenant_github_app_secret`** | **The critical semantic fix**: today copies the PLATFORM App's key into `adp/<env>/tenants/<t>/github-app`. With per-tenant Apps it must copy the **App that received the installation** (resolved via the `X-GitHub-Hook-Installation-Target-ID` header → `github-apps/<id>/`), so a tenant's agents act under that tenant's own App identity. |
| webhook `_get_own_app_slug` (#2732) | Becomes "is this bot login one of OUR registered App slugs?" — set-membership over `github_apps.slug WHERE tenant matches`, not a single slug (see Q7). |

The `adp/<env>/tenants/<t>/github-app` per-tenant worker path is **unchanged in shape** — the
worker (`vault_client.py`) keeps reading `tenants/<t>/github-app`. Only the *source* of the
bytes changes (tenant's App, not platform App). Worker pod code needs **no change**.

## Q6 — GitHub login

**Stays bound to exactly one App** — the OAuth broker reads one client_id/secret. Add
`login_app BOOLEAN` to the registry with a **partial unique index** (`WHERE login_app`) so at
most one row is the login App. The register flow sets `login_app=TRUE` on the **first App
registered that carries OAuth creds**, preserving today's behavior. On single-customer
deployments where the customer registers the only App, that App becomes the login App
automatically. `_store_app_credentials` writes the broker OAuth secret
(`adp/<env>/cognito/github-oauth-credentials`) **only** for the `login_app` row — subsequent
Apps do not clobber it. Changing the login App is an explicit admin action (flip the flag +
re-write the broker secret), out of scope for the first child issues.

## Q7 — Bot identity / provenance (#779, #2732)

Per-App bot logins: each registered App has its own `<slug>[bot]` identity. Two changes:
- **Provenance markers** (`adp-correlation:` etc.) are App-agnostic — they identify ADP-family
  traffic, not a specific App. No change needed; the marker still means "an ADP agent wrote this."
- **Sibling detection** (`_detect_sibling_app`, #2732): today "sibling == bot login != OUR one
  slug." With N own-Apps, change the own-slug gate to **set membership**: build the set
  `{f"{slug}[bot]" for slug in own_registered_slugs}` (from the registry, cached per container).
  A bot comment is a sibling only if its login is NOT in that set. This correctly treats all N
  legitimate own-Apps as "ours" and only flags a genuinely foreign deployment's App.

## Q8 — Coexistence with the public-App model

- embark1's public `aws-e-adp-agent-dev` = registry row `is_public=TRUE`. Nothing about the
  per-org private path removes it. Public and private Apps coexist as rows.
- **Interaction with #2724**: #2724's fix (a tenant-existence check before webhook
  auto-register) must **not** block a properly-registered private App. Because a private App is
  physically installable only on its owner org, an install through a *registered* private App
  is inherently trusted — the auto-register gate should be: "allow if the delivery's App id is a
  registered private App for a known tenant; apply the #2724 tenant-existence check only to the
  public App path." This is precisely why the spike de-risks #2724: private Apps can't be
  installed by strangers.

## Q9 — Relationship to EPIC #421 / #467 / #468

- **#467** (BYO App via manifest flow) describes essentially this feature but predates the
  #2593 register flow that already shipped the manifest conversion. **Recommendation: fold #467
  in as the implementation umbrella** for this spike's child issues (re-scope its body to "make
  the #2593 flow multi-App") rather than closing it — it already carries EPIC #421 linkage.
- **#468** (manual credential upload for pre-existing Apps) is a **complementary input path**,
  not superseded: it lets an admin paste an existing App's id+key+webhook_secret. Under this
  design that just becomes "insert a `github_apps` row + write the per-App secrets without the
  manifest round-trip." Keep #468 open; it reuses the same registry.

---

## Migration plan (zero-downtime, 3 live accounts)

1. `020_github_apps` forward migration: create table + indexes. **Down-migration**: drop table
   (code-compatible — readers keep the legacy grandfather path if the table is empty).
2. Bootstrap script (runs post-migrate, can read SM — Alembic can't): for each deployment where
   `adp/<env>/github-app/adp-agent-platform-id` is populated & non-placeholder, insert one row
   with `secret_prefix` = legacy path, `is_public` per deployment, `login_app=TRUE`.
   Idempotent (ON CONFLICT on `github_app_id` DO NOTHING).
3. Deploy webhook Lambda with header-based resolution + legacy fallback. Deploy gateway with
   registry-aware readers + legacy fallback. Both fall back to legacy paths when a row's
   `secret_prefix` is the legacy path → **embark1/261/812 keep working with zero changes**.
4. New registrations write new `github-apps/<id>/` paths + rows. Optional later housekeeping
   copies legacy→new.

## Smoke test (end state)

1. On one deployment, register two private Apps for two different tenants (A owns org-a, B owns
   org-b). Assert two `github_apps` rows, `login_app` set on exactly one.
2. Install each App on its org. Assert each `ChannelTenantMap` row carries the right
   `metadata.github_app_id`; each `adp/<env>/tenants/<t>/github-app` holds **that tenant's** App
   key (not the platform App's).
3. Fire a webhook from each App. Assert both validate (each via its own `github-apps/<id>/meta`
   webhook_secret; `sig OK`), and a delivery with an unknown App id gets 401 `unknown_app`.
4. Trigger an agent in each tenant; assert the PR/comment bot login is that tenant's `<slug>[bot]`.
5. Connections UI: tenant A sees only org-a's App; tenant B sees only org-b's.

## Rollback

- Code: revert the gateway/webhook PRs — grandfather fallback means legacy paths still resolve.
- Schema: `020` down-migration drops `github_apps`; readers fall back to legacy singleton path.
  Safe because no legacy secret bytes were moved.

---

## Child issues (five-section convention; each single-agent-sized)

Filed against this spike / re-homed under #467:

1. **C1 — Postgres `github_apps` registry + `020` migration + grandfather bootstrap**
   (schema, ChannelTenantMap `metadata.github_app_id`, backfill script). Foundation; blocks the rest.
2. **C2 — Per-App secret layout + IAM/KMS** (new `adp/<env>/github-apps/<id>/{key,meta}`;
   gateway `adp/*/github-apps/*` grant; webhook Lambda `github-apps/*` grant; keep legacy).
3. **C3 — Webhook validation by `X-GitHub-Hook-Installation-Target-ID`** (per-App cache,
   unknown-app 401, legacy fallback). Subsumes #2824 for new Apps.
4. **C4 — Multi-App registration** (remove `already_registered`, UNIQUE dup-guard, tenant-admin
   authz, write registry row + per-App secrets).
5. **C5 — (tenant,App)-scoped resolution** (provider dict, tenant_secret from tenant's App,
   `_auto_provision_tenant_github_app_secret` copies the RIGHT App's key).
6. **C6 — `login_app` pinning** (partial unique index, broker OAuth write only for login App).
7. **C7 — Multi-own-App sibling detection + Connections list-by-tenant** (#2732 set-membership;
   UI lists a tenant's own Apps only).
