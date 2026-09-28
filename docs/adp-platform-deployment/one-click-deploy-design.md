# One-Click Deploy — design

Design for the customer-facing, self-service deployment experience described in
EPIC **#969** (Deployment Lifecycle — one-click deploy, continuous monitoring,
and upgrades). This is the design-of-record for the sub-issue **#1143** (H:
one-click deploy SPA + gateway endpoints + orchestrator side-channel) and the
data/versioning model the monitoring (#1140) and upgrade (#1141) pillars build on.

**Scope of this doc:** the API-first contract, the multi-tenant data model, the
version model, progress streaming, and how all of it composes with pipelines
that **already exist** today. It does *not* re-specify the per-phase verification
checks — those live in [`phase-verification.md`](./phase-verification.md).

---

## 1. Framing — a deploy API, rendered as a thin UI

The product is **`POST /deploys`**, not the button. The dashboard SPA is one
client; a CLI, a CI job, Terraform, or an autonomous agent are equal clients of
the same endpoint. Everything the button does is a JSON body.

> **Design rule:** anything the UI can do, a token-authenticated script can do
> with the same API call. If we honor this, "automate it instead of clicking"
> is free — it's the same endpoint with a service-account credential instead of
> a human Cognito session.

The GitHub-issue orchestration (spawn a deploy-instance issue + tag
`@agent-operations`) stays an **internal implementation detail behind the API**.
Callers never file issues themselves. When we later want fleet deploys (deploy
to 50 accounts from a script), you loop over `POST /deploys` — you never script
GitHub issue creation.

### What already exists (reused, not rebuilt)

| Capability | Where it lives today | Status |
|---|---|---|
| Connect an AWS account (CFN quick-create → cross-account role) | `modules/gateway/src/auth/aws_connect_routes.py`, `ConnectAws.tsx` (#562) | ✅ ships |
| Spawn a deploy-instance (parent + 11 phase children, sub-issue links) | `.github/workflows/spawn-deploy-instance.yml` | ✅ ships |
| Cross-account credential assume for a phase pipeline | `.github/actions/load-deploy-config` → `assume-customer-creds.py` → gateway `/internal/v1/credential-assume-role` | ✅ ships |
| Phase apply pipelines | `platform-infra-apply.yml`, `gateway-infra-apply.yml`, `gateway-deploy.yml`, `webhook-ingress-deploy.yml`, … | ✅ ships |
| Orchestrator agent | `@agent-operations` persona | ✅ ships |
| Deterministic phase verification (Phase 1) | `platform-deploy-mgmt-verify.yml`, `modules/platform-deploy-mgmt/` | 🟡 Phase 1 only; 2–9 stubbed (#1137/#1138) |
| SSE streaming primitive | `modules/gateway/src/proxy/stream_handler.py` (Bedrock token stream) | ✅ reuse |
| Evidence storage | S3 `adp-platform-deploy-evidence` + DynamoDB `adp-platform-deployments` | ✅ keep as evidence buffer |

### The credential → pipeline-input mapping (why this is mostly plumbing)

The three inputs every phase pipeline requires are exactly the three fields the
Connect-AWS flow already stores:

| Pipeline input (`load-deploy-config` / `*-apply.yml`) | Connect-AWS stored field (`user_credentials`) |
|---|---|
| `customer_account_id` | `metadata.account_id` |
| `customer_aws_label` | `label` (the nickname) |
| `customer_user_id` | `user_id` (resolved Postgres `users.id` UUID) |

So a deploy is: *look up the linked-account credential the user picked → derive
those three values → spawn + orchestrate.* No new credential machinery.

---

## 1a. Architecture placement — a separate, removable module

**DECISION: deployment-management is its own top-level code module
(`modules/platform-deploy-mgmt/`) with its own container image, its own CI/deploy
pipeline, its own k8s pod set, and its own API Gateway route. It is
never mounted inside the gateway app, and the entire platform runs correctly
when it is absent.**

Two hard requirements drove this:

1. **Separate module in the code folder** — it lives beside `gateway`,
   `agent-factory`, `agent-context` as a first-class top-level module, not as a
   subpackage of the gateway. (The folder already exists:
   `modules/platform-deploy-mgmt/`.)
2. **Optional / removable** — an operator may choose *not* to deploy it, and the
   rest of ADP (proxy, admin, auth, budget, …) must work unchanged.

### What makes this cheap rather than a full service split

The trap to avoid is conflating **"separate image"** with **"separate service."**
They are independent axes:
- **Separate image + CI + pod set** buys independent build/deploy/scaling/blast-radius.
  This is what requirement #1 and the "don't reship the gateway on every deploy-code
  change" concern need — and it is *pure upside* here.
- **Separate service (own DB, own auth stack)** is the expensive part, because the
  hard dependencies are **shared state**: Cognito/tenant auth (`auth/`+`shared/`)
  and the FK'd Postgres data. We do **not** take that split.

So `platform-deploy-mgmt` is a **separate image built from the same repo**, and it
gets what it needs from the gateway as **shared source packages**, not over the
wire:
- **Auth + tenant resolution** — imported as a library (the gateway's `auth`/`shared`
  code, packaged for reuse — see "Packaging work" below). No JWT re-validation,
  no call-the-gateway-for-auth latency.
- **Data** — the deploy tables live in a **separate `deploy` schema in the same
  control-plane Postgres** (§2), so FKs into `public.users`/`organizations`/
  `user_credentials` still hold. No DB split, no cross-service reads.
- **Assume-role** — `/internal/v1/credential-assume-role` stays a gateway internal
  endpoint; `platform-deploy-mgmt` calls it (it is already an internal API).
- **API Gateway** — shared. A `/api/deploys/*` route → the deploy module's own ALB
  target group. Routing is the cheapest thing to share (CloudFront `/api/*` and the
  existing proxy routes already demonstrate the seam).

### How "optional / removable" works

- **Runtime:** the gateway app **never imports** deploy code, so the deploy module's
  presence is purely a matter of whether its pod set + `/api/deploys/*` route are
  applied. "Don't deploy it" = don't apply its Terraform/k8s. Physical absence, not
  a feature flag inside the gateway.
- **SPA:** the Deployments tab renders only when the deploy API answers (probe a
  capability endpoint; a `404`/absent route hides the tab). No dead UI.
- **One-way dependency rule (INVARIANT):** nothing outside `platform-deploy-mgmt`
  may import or hard-depend on it at runtime. The dependency arrow points *into*
  the module (it consumes auth/DB/assume-role); never *out*. Any cross-surface
  nicety (e.g. a deploy-health widget on the main dashboard) must **degrade to
  "unavailable"** when the module is absent, never error.
- **Manual path stays first-class:** the issue-driven `spawn-deploy-instance.yml`
  flow works with or without this module. The module is a UI/API convenience over
  a substrate that stands alone.

### The packaging work this adds (the one real cost)

Today the gateway's auth/shared code is `src.auth` / `src.shared` inside the
`bedrockgateway` package. For a sibling module to import it, that code must become
**importable as a shared internal package** — either published as a small internal
library both images depend on, or `platform-deploy-mgmt` declaring a dependency on
the gateway package. This is a **packaging task, not an auth re-implementation** —
bounded and one-time. It is the price of requirement #1, and it is worth paying to
get physical optionality + independent build/deploy.

### Escalation (only if a real force appears)

The design stops at "separate image + shared source + shared DB instance (separate
schema)." Escalate to a **fully separate service with its own database** only when:
- deployment-management gets its own team + independent release cadence beyond what
  a shared repo allows, OR
- a regulatory/compliance need to back up or access-wall deploy data independently
  (then: separate DB **instance**, not a separate database on the shared one — §2), OR
- the control-plane gateway becomes self-upgrading (a deploy component that upgrades
  the very app it depends on has a bootstrapping/availability wrinkle — the strongest
  long-term argument for full extraction).

Because the module boundary + separate image are in place from day one, that
escalation stays a mechanical extraction, not a rewrite. If it ever happens, the
deploy service **owns its own schema/DB** and the gateway exposes what it needs via
API — never two services reaching into one database.

---

## 2. Data model — where deployment state lives

### 2.1 Two stores, two questions

| Concern | Store | Why |
|---|---|---|
| **Deployment as a domain entity** — owner, org/team, linked account, lifecycle state, run history, module versions | **Control-plane gateway Postgres** | Relational, tenant-scoped, FK'd to `users`/`organizations`/`user_credentials`, queried by the SPA through the gateway. This is a domain aggregate, not a metrics stream. |
| **Per-phase verification evidence written from the cross-account runner pod** | **S3 + DynamoDB** (`adp-platform-deploy-evidence`, `adp-platform-deployments`) | Written by a pod that holds only platform STS creds and has no Postgres DSN. High-write, append-y, keyed by `deployment_id`. Deliberately separated as a tamper-evident audit trail. |

**Postgres is the source of truth the SPA reads.** DynamoDB stays as the runner
pod's write buffer; the authoritative phase verdict is **reconciled back into
Postgres** (§4). This also fixes the multi-tenancy hole in the existing
DynamoDB table — the relational tables carry the standard `TenantMixin`
(`org_id` + denormalized `team_id`) like every other gateway table, so tenant
scoping and authz come for free.

> **"Which Postgres?"** The *control-plane* gateway DB — the platform-account
> gateway behind the dashboard everyone logs into (`users`, `organizations`,
> `user_credentials`). **Not** the per-install gateway DB a deployment *creates*
> in the customer's account. Deployment records belong to the control plane
> because that's where the FKs and the SPA live.

> **Separate schema, shared database — NOT a separate database.** The deploy
> tables live in their own Postgres **schema** (`deploy.*`) inside the same
> control-plane gateway database. This is the data-layer twin of the
> "separate image, shared codebase" runtime shape (§1a): a clean namespace
> boundary without a shared-state split.
>
> This is a deliberate choice on a ladder of options:
>
> | Level | FK/join to `users`/`orgs` | Independent migrations | Independent backup/access | Verdict |
> |---|---|---|---|---|
> | Same schema (`public`) | ✅ | ❌ shared | ❌ | simplest; not chosen |
> | **Separate schema (`deploy.*`)** | ✅ | ✅ | partial (role grants) | **CHOSEN** |
> | Separate database, same instance | ❌ | ✅ | ✅ | rejected — see below |
> | Separate instance | ❌ | ✅ | ✅✅ | only when it's a separate *service* |
>
> **Why separate schema, not separate database:** in Postgres, separate
> *databases* on one instance are fully isolated at the query layer — **no
> cross-database FKs, joins, or transactions.** A separate `adp_deploy` database
> would throw away referential integrity to `users`/`organizations`/
> `user_credentials` (the whole reason we chose Postgres), force two-DB stitching
> for a trivial "owner name" join, and still share the instance's failure domain
> — i.e. it pays the coupling cost of a separate service without buying real
> operational independence. A separate **schema** keeps FKs, joins, and
> single-transaction spawn writes working, while giving the deploy module its own
> namespace, its own Alembic version branch (`version_table_schema='deploy'`),
> and the option to `GRANT` a narrower DB role to the deploy runtime later.
>
> **When to escalate past separate-schema:** a genuine need to back up or
> access-wall deployment data independently (regulatory/compliance), or a full
> separate-service split (§1a Option C). At that point the answer is a separate
> **instance**, not a separate database on the shared one — because if you need
> instance-level isolation you need a separate instance, and if you don't, the
> schema boundary already suffices.

### 2.2 Postgres schema

All tables live in the **`deploy` schema** of the control-plane gateway database,
owned by the `modules/platform-deploy-mgmt/` module (§1a), with their own Alembic
version branch. Tables use `TenantMixin` (`org_id`, `team_id`); FKs cross into
`public` (`users`, `user_credentials`) — permitted and intended, since it's one
database.

#### `deployments` — the long-lived install (survives every upgrade)

```
id                  uuid pk
org_id              str          # TenantMixin — tenant boundary
team_id             str          # denormalized (matches UserIdentity pattern)
owner_user_id       str  fk users.id          # who created it
credential_id       str  fk user_credentials.id   # the linked account (→ account_id + label + user_id)
customer_account_id str          # denormalized target AWS account (informational, NOT the authz key)
environment         str          # dev | staging | prod
region              str
modules_requested   json         # e.g. ["platform","gateway","agent-factory"] — everything or a subset
visibility          str          # private | team | org   (default from org policy)
current_version     str null     # platform bundle currently targeted (see §3)
lifecycle_state     str          # provisioning | active | upgrading | degraded | destroying | destroyed
health              str          # green | yellow | red   (from monitoring, Pillar 2)
created_at, updated_at
```

#### `deployment_runs` — every deploy/upgrade/rollback/destroy attempt

This table is what makes **upgrades first-class**: an upgrade is not a new
deployment, it's a new run against an existing one.

```
id                    uuid pk
deployment_id         uuid fk deployments.id
run_type              str          # initial_deploy | upgrade | rollback | destroy
from_version          str null     # null for initial_deploy; both set for upgrade/rollback
to_version            str null
status                str          # running | succeeded | failed | halted | needs_human
deploy_instance_issue int null     # the spawned GitHub parent issue number
spawn_workflow_run_url str null
triggered_by_user_id  str  fk users.id   # who clicked (may differ from deployment.owner)
idempotency_key       str null unique     # dedupe machine retries (§1)
started_at, ended_at
```

#### `deployment_phases` — per-phase state within a run (the SPA progress pills)

```
id                 uuid pk
run_id             uuid fk deployment_runs.id
phase_number       int          # 1..10
phase_name         str
status             str          # pending | running | passed | failed
                                #  | blocked_self_healing | needs_human | skipped
verify_run_url     str null     # platform-deploy-mgmt-verify.yml run for this phase
evidence_s3_key    str null     # pointer into the S3/DDB evidence
child_issue_number int null     # the phase's GitHub sub-issue
fix_issue_number   int null     # set when blocked_self_healing (e.g. #2913 for #2899 Phase 4)
started_at, ended_at
```

#### `deployment_module_versions` — per-module version truth (see §3)

```
deployment_id     uuid fk deployments.id
module            str          # platform | gateway | agent-factory | agent-context | webhook-ingress | ...
version           str          # git SHA / image tag actually deployed for that module
applied_by_run_id uuid fk deployment_runs.id
state             str          # deployed | drifted | upgrading | not_installed
applied_at
  pk (deployment_id, module)
```

#### `platform_versions` — the release catalog (a bundle of module pins)

```
version          str pk        # 'v1.4.0' / release tag
channel          str          # stable | beta
git_sha          str
released_at
is_yanked        bool          # pulled due to a bad release
min_upgrade_from str null      # earliest version that can jump straight to this
module_pins      json          # { gateway: <sha>, agent-factory: <sha>, platform: <sha>, ... }
notes            str           # changelog pointer
```

### 2.3 The phase-state machine

The pending/running/done/failed model is insufficient — the #2899 deploy proved
it. Phase 4 there is legitimately **blocked, with a fix (PR #2913) in flight,
and will auto-resume**. That is a self-healing state, not a failure.

```
pending ─▶ running ─┬─▶ passed ─▶ (next phase)
                    ├─▶ failed ─────────────▶ (retry, ≤N) ─▶ running
                    ├─▶ blocked_self_healing ─▶ (fix issue merges) ─▶ running
                    └─▶ needs_human ─────────▶ (operator acts) ─▶ running
                    skipped  (operator-gated)
```

- `blocked_self_healing` carries `fix_issue_number` — the agent filed a child
  fix issue and will resume when it merges. The SPA links to it; no human needed.
- `needs_human` is the genuine escalation — the SPA surfaces the decision the
  agent posted (the "⏸️ Decision needed" comment) and the operator acts.

### 2.4 Data captured from the target account

A deploy produces a rich set of identifiers in the customer's account (terraform
outputs, SSM params). The governing rule for what we persist in the control plane:

> **Store references and health facts, never secrets or standing access.** The
> cross-account model is "assume a short-lived role on demand." Store the
> ARN/endpoint/ID that lets us *find or reach* a resource; resolve the secret
> value at use-time by assuming into the account. Copying secrets into the
> control-plane DB would recreate the multi-tenant blast radius this design
> avoids and hold stale copies of values that rotate.

**What to store — five categories:**

1. **Deployment outputs (the connection map)** — the non-secret identifiers each
   phase emits: `vpc_id`, `eks_cluster_name/arn/endpoint`, `eks_oidc_provider_arn`,
   `ecr_repository_urls`, `rds_endpoint/port/database_name`, `redis_endpoint`,
   `frontend_cloudfront_domain_name/distribution_id`, `cognito_user_pool_id`,
   `cognito_domain`, `cognito_hosted_ui_url`, `github_sign_in_url`,
   `gateway_ws_endpoint`, queue URLs, `runner_role_arn`, table names. These are
   the URLs the SPA renders, what monitoring probes, and the inputs later
   phases/upgrades consume. **Stored as JSONB on the run** (§2.5), not S3.
2. **Secret *pointers*, not values** — the deploy emits Secrets Manager ARNs
   (`rds_master_user_secret_arn`, `cognito_agent_credentials_secret_arn`,
   `test_admin_credentials_secret_arn`). **Store the ARN; never the value.** Fetch
   live by assuming into the account when needed. (This is exactly how the #2899
   admin login was retrieved — the value was pulled from Secrets Manager on
   demand, never persisted.)
3. **State-backend coordinates** — `adp-terraform-state-<account_id>` bucket +
   per-module state keys + `adp-terraform-locks`. Needed so upgrade/rollback/destroy
   runs re-`init` against the right state without rediscovery.
4. **Account/environment context** — `account_id`, `region`, `environment`,
   `partition`; the `credential_id` + assumed role ARN used (audit: which identity
   deployed); and **account guardrail facts discovered during deploy**
   (`account_constraints` JSON). The #2899 lesson: the SCP-locked Lambda
   concurrency floor, the org ID, SCP IDs, and quota ceilings are account metadata
   that explain phase behavior and should persist so a re-deploy/upgrade doesn't
   rediscover them the hard way.
5. **Per-phase evidence pointers** — `verify_run_url`, `evidence_s3_key`, PASS/FAIL
   summary. Raw evidence stays in S3/DDB; Postgres holds the pointer + summary (§2.1).

**Deny-list — never stored:**
- Any secret material (RDS passwords, Cognito client secrets, GitHub App private
  keys, session tokens) — ARNs only.
- Long-lived AWS credentials — assume-on-demand; never cache STS creds beyond a run.
- Customer application data — anything inside their RDS/S3 workloads. We track the
  *deployment*, not what runs on it.
- Durable copies of `kubeconfig` / `eks_cluster_ca_certificate` — regenerate at
  use-time (`aws eks update-kubeconfig`) rather than storing a copy that goes
  stale on cluster rotation.

### 2.5 Where outputs and logs live (JSONB vs S3)

Two data classes with opposite profiles — do not conflate them:

| Data | Store | Why |
|---|---|---|
| **Deployment outputs** (connection map, ~2 KB, hot-read, queryable, changes per run) | **Postgres JSONB on `deployment_runs`** | Small, joined with the run in one transaction, queryable (`outputs->'gateway'->>'cloudfront_domain'`), versioned per run for free. S3 would add a second round-trip and lose transactional consistency. |
| **Pipeline logs** (terraform/kubectl firehose, tens of MB, cold, append-only, forensic) | **S3 evidence bucket + pointer in PG** | Large, write-once, read-only-on-failure, already tamper-evident (versioned, delete-denied, Glacier). Postgres would bloat with cold blobs never queried relationally. |

**Outputs:**
- `deployment_runs.outputs jsonb` — the connection map this run produced, keyed by
  module. Flexible shape (a module adds an output → no migration) while still
  queryable/indexable (GIN if ever needed).
- `deployments.current_outputs_run_id` — pointer to the run whose outputs are "live."
- Promote the 3–4 hottest fields (CloudFront domain, RDS endpoint, EKS name) to
  typed columns for common SPA queries.
- Attaching outputs to the **run** (not the deployment) means the upgrade-diff can
  show "your endpoint changed," and rollback knows the prior connection map.

**Pipeline logs:**
- **Do not** store log bodies in Postgres. Store a **pointer per phase**
  (`deployment_phases.apply_run_url`, `deployment_phases.log_stream`).
- **Archive bodies to the S3 evidence bucket** rather than relying on GitHub
  Actions retention (bounded, default 90d) or exposing internal `github.com/aws-e/adp`
  run URLs to customers. A per-phase workflow step uploads the captured log to
  `s3://adp-platform-deploy-evidence/<account>/logs/<run>/phase-<n>.log`.
- The gateway/deploy module serves logs to the SPA as **tenant-scoped presigned
  S3 URLs** behind "View logs ↗" — the customer never gets a raw GitHub link or
  standing S3 access.

**Full storage picture across all data classes:**

| Data | Store | In SPA? |
|---|---|---|
| Deployment/run/phase state, outputs JSON | Postgres (`deploy` schema) | Rendered |
| Verification evidence (check dumps) | S3 + pointer in PG | Link |
| Pipeline logs (tf/kubectl firehose) | S3 archive + pointer in PG (presigned) | "View logs" link |
| Live cross-account check status | DynamoDB → reconciled to PG | Via PG |
| Agent narration (plan/heartbeats/verdicts) | GitHub issue | Link |

---

## 3. Version management — module-level truth, platform-level bundle

ADP is **not** one atomic unit: 7 modules, each with its **own** `terraform.tfstate`
(`dev/modules/gateway/…`, `…/agent-factory/…`), its **own** deploy workflow, and
its **own** independently-built image(s). A customer may deploy a subset
(gateway-only, agent-context-only). A single flat "platform version" cannot
honestly describe an install where 3 of 5 modules updated.

**Two tiers:**

- **Tier 1 — module versions are the source of truth.**
  `deployment_module_versions` records what version each installed module
  actually runs. This is the granular, queryable truth; drift is a first-class
  state, not a surprise terraform discovers.

- **Tier 2 — a platform version is a named bundle** (`platform_versions`) that
  **pins a version per module** (`module_pins`). `deployments.current_version`
  names the bundle the install *targets*.

**Sync status is derived, not stored:**
- **in sync** — every `deployment_module_versions` row matches the bundle's `module_pins`.
- **drifted / partially upgraded** — a module diverges (failed upgrade, or an
  out-of-band patch).

**What this enables:**
- **Whole-platform upgrade** — pick bundle `v1.4.0` → diff each module's current
  version vs the bundle pin → apply only the changed modules → advance their
  rows. Modules already at target are skipped (smaller blast radius, faster).
- **Targeted module upgrade** — "patch gateway only" is an upgrade run scoped to
  one module; the install is marked off-bundle until the next full upgrade.
- **Honest status in the SPA** — "Platform v1.3.0 — ✅ in sync" vs "v1.3.0 →
  gateway drifted (running hotfix-abc)".
- **Per-module rollback** — roll back only the module that regressed.

`min_upgrade_from` lets the API reject illegal version jumps (e.g. "must be on
≥v1.2 before v2.0") up front, rather than letting terraform find out the hard way.

---

## 4. Flow — from click to running platform

```
┌─ SPA ── Deployments page ────────────────────────────────────────────────┐
│  "Deploy ADP" next to each verified linked account.                       │
│  Dialog: environment, region, modules, visibility (private/team/org),     │
│          version (default = latest stable).                               │
└───────────────┬───────────────────────────────────────────────────────────┘
    POST /deploys { credential_id, environment, region, modules,
                    visibility, version, Idempotency-Key }
                ▼
┌─ platform-deploy-mgmt (FastAPI, control plane) ───────────────────────────┐
│  1. Resolve org_id/team_id/user_id from TokenContext (human) OR service    │
│     account (automation) — SAME endpoint, SAME tenant stamping.            │
│  2. Look up credential_id → (account_id, label, user_id).                  │
│  3. Insert deployments + deployment_runs + 10 deployment_phases in ONE txn.│
│  4. Enqueue a deploy task to the agent-submit SQS FIFO (AWS SDK / IRSA —   │
│     NO GitHub token) with tenant-sealed payload: owner_user_id, org_id,    │
│     team_id, credential_id, deployment_id, customer_account_id, label,     │
│     customer_user_id, version.   (§4a — workflow_dispatch is the fallback) │
│  5. Return { deployment_id, run_id }.                                      │
└───────────────┬───────────────────────────────────────────────────────────┘
    SQS FIFO (adp-<env>-agent-submit.fifo), MessageGroupId = deployment_id
                ▼
┌─ agent substrate → spawn-deploy-instance (EXISTS — extended) ──────────────┐
│  The queued task starts the orchestrator; the spawn clones parent + phase  │
│  children, links sub-issues. The parent issue body carries the sealed      │
│  tenant identity + deployment_id.                                          │
└───────────────┬───────────────────────────────────────────────────────────┘
                ▼
┌─ @agent-operations orchestrator (EXISTS) ─────────────────────────────────┐
│  Walks phases; each infra phase = gh workflow run <phase>-apply.yml with   │
│  the customer inputs → load-deploy-config assumes cross-account → tf apply │
│  into the customer account.                                                │
│  NEW side-channel: after each phase verdict, POST                          │
│  /internal/deploy-phase-update { deployment_id, run_id, phase, status,     │
│  verify_run_url, fix_issue } → gateway writes the deployment_phases row.   │
│  HARD RULE (#1139): no PASS without a green platform-deploy-mgmt-verify run.│
└───────────────┬───────────────────────────────────────────────────────────┘
                ▼
┌─ Verification (platform-deploy-mgmt-verify.yml) ──────────────────────────┐
│  Deterministic per-phase checks → S3 evidence + DynamoDB status.           │
│  Verdict + evidence pointer reconciled into Postgres via the side-channel. │
└───────────────────────────────────────────────────────────────────────────┘
```

### 4a. Pipeline triggering — no customer GitHub, no GitHub token to start a deploy

**The pipelines run in the platform account and reach the customer account via
AWS-native cross-account assume-role — GitHub credentials are NOT involved in
giving a pipeline its AWS access.** Two facts anchor this:

- Phase pipelines run on **ARC runners in the platform account** with **IRSA**
  (`adp-dev-agent-runner-role`) and assume into the customer account via the
  gateway's SigV4 `/internal/v1/credential-assume-role` (`load-deploy-config`).
  All AWS-native.
- There is **no per-customer GitHub.** The customer side is purely an AWS account
  reached by the CFN-created `ADP-Agent-<label>` role. No customer GitHub, no
  customer GitHub token, ever.

GitHub enters only because the *orchestration substrate* happens to be GitHub
Actions + Issues. The **one platform GitHub App token** (`github_client.py`)
authenticates to ADP's **own** `aws-e/adp` org — it is orchestration plumbing used
by the agent to talk to Issues/Actions, **not customer credentials**.

**Two axes, fully orthogonal:**

| Axis | What | Auth | Per-customer? |
|---|---|---|---|
| Orchestration substrate | issues, Actions, agent comments | one platform GitHub App token | No — single platform identity |
| Deploy target | the customer's AWS account | cross-account STS assume-role | Yes — one linked account per deploy |

**How the deploy module triggers a deploy (no GitHub token needed):** it
**enqueues a task to the existing `adp-<env>-agent-submit.fifo` SQS queue via the
AWS SDK under IRSA** — pure in-account AWS. The same substrate agents already run
on (GitHub webhook → SQS FIFO → agent pod) picks it up; the deploy is just another
queued agent task (`MessageGroupId = deployment_id` for per-deploy ordering/isolation).
The agent then does all GitHub-side orchestration using the platform App token it
already holds.

- **Fallback:** `gh workflow run spawn-deploy-instance.yml` via a platform App
  installation token (the gateway already mints these) works too, but the SQS path
  is preferred — no GitHub token on the trigger path, and it reuses the agent
  substrate verbatim.
- **Idempotency across the async boundary:** `POST /deploys` generates the
  `deployment_id` **before** enqueueing and includes it in the payload; the
  `Idempotency-Key` (and the unique `deployment_runs.idempotency_key`) guard against
  a double-enqueue on retry.
- **The two-layer rule:** the deploy module triggers **only the start** (one SQS
  task per deploy/upgrade). All **phase-level** dispatch is the orchestrator's job
  (`gh workflow run <phase>-apply.yml`). Retry/halt/upgrade from the SPA **signal
  the orchestrator** (enqueue a control task / issue comment) — the module never
  dispatches a phase workflow directly, so there is exactly one driver for phases
  and no split-brain.

### Tenant sealing (the isolation guarantee)

The deploy module stamps `org_id`/`owner_user_id` at enqueue time from the
authenticated caller and passes them in the sealed SQS payload → the orchestrator.
The agent **never chooses the tenant** — it only echoes what was sealed in.
Write-side rows are therefore always attributed correctly even though the
orchestrator/verify pods run under trusted platform creds.

### Read-side authz (per-deployment visibility)

`visibility` is a column, not a platform-wide policy — set per deployment,
defaulting from an org-level policy.

`GET /deploys` returns rows where **I am the owner**, OR `visibility='team'` and
I share `team_id`, OR `visibility='org'` and I share `org_id`. `GET /deploys/{id}`
additionally asserts the row's tenant matches the caller (defense-in-depth
against a guessed `deployment_id`). Never a bare `Scan` — always tenant-keyed.

- **org** (default) — anyone in the org sees/acts on the org's deployments;
  `owner_user_id` records who launched it. Matches how teams share infra and
  means a stuck deploy (like #2899) can be picked up by a teammate if the
  launcher is offline.
- **team** — bounded by `team_id` within the org.
- **private** — only the owner (org admins get a superset view).

---

## 5. Streaming progress

Deploy/upgrade progress streams to the SPA over **Server-Sent Events**, reusing
the gateway's existing SSE primitive (`proxy/stream_handler.py`). SSE (not
WebSocket) because updates are server→client only, low-frequency (a phase
transition every 1–15 min plus heartbeats), and SSE already lives in the gateway
the SPA talks to — no new infra, no WebSocket auth dance.

```
GET /deploys/{id}/events   →  text/event-stream
```

- **DB-backed, not in-memory.** The gateway has multiple replicas and the
  *writer* is an orchestrator pod elsewhere, so state cannot live in process
  memory. The SSE endpoint is a **live tail of the `deployment_phases` table**
  (poll-the-DB behind the connection, or Postgres `LISTEN/NOTIFY`). The DB is
  the single source; SSE just pushes deltas.
- **Two access modes, one source:** refresh the page → `GET /deploys/{id}`
  returns full current state; keep it open → SSE pushes transitions. Non-UI
  callers simply poll `GET /deploys/{id}` — no SSE required.
- **Granularity:** phase-level transitions (10 per deploy) + heartbeats. Raw
  agent token output is **not** streamed to the SPA — it stays in the GitHub
  issue; the SPA shows structured phase state and links out to the issue /
  workflow run for detail.

---

## 6. API surface

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/deploys` | Start a deploy (initial or into a subset of modules). Body: `credential_id`, `environment`, `region`, `modules`, `visibility`, `version`. Honors `Idempotency-Key`. |
| `GET` | `/deploys` | List deployments visible to the caller (tenant-scoped). |
| `GET` | `/deploys/{id}` | Full state: deployment + latest run + phases + module versions. |
| `GET` | `/deploys/{id}/events` | SSE live tail of phase transitions. |
| `POST` | `/deploys/{id}/upgrade` | Upgrade to a target `version` (bundle) or a per-module pin. Spawns an `upgrade` run. |
| `POST` | `/deploys/{id}/rollback` | Roll back to a prior known-good version (whole or per-module). |
| `POST` | `/deploys/{id}/halt` | Halt the running run. |
| `POST` | `/deploys/{id}/phases/{n}/retry` | Retry a failed/blocked phase. |
| `POST` | `/deploys/{id}/phases/{n}/skip` | Operator-gated skip. |
| `POST` | `/internal/deploy-phase-update` | **Internal.** Orchestrator/verify side-channel → reconcile a phase verdict + evidence pointer into Postgres. |
| `GET` | `/platform-versions` | The release catalog (bundles + channels). |

---

## 7. Upgrade & rollback (Pillar 3 mechanics)

An upgrade reuses the entire deploy flow — it's a `deployment_run` with
`run_type='upgrade'`, so the same spawn → orchestrate → verify → reconcile path
applies. The upgrade-specific logic:

1. **Pre-upgrade baseline** — run the full verify suite against the live install,
   capture as the baseline run's `deployment_phases`.
2. **Apply** — only the modules whose current version differs from the target
   bundle's `module_pins`.
3. **Post-upgrade verify** — run the suite again.
4. **Regression diff** — a plain SQL diff over `deployment_phases` between the
   baseline run and the post run on the same `deployment_id`: any check that was
   `passed` in baseline and `failed` in post is a regression → the run fails and
   the SPA offers rollback.
5. **Rollback** — a `run_type='rollback'` run targeting the prior known-good
   `to_version` (whole platform or a single module).

The run history (`deployment_runs` ordered by `started_at`, each with
`from_version → to_version`) **is** the version timeline per install.

---

## 8. What's new vs. reused (build inventory)

**New (this design / #1143 + version+upgrade extensions):**
- `deploy`-schema Postgres tables + Alembic branch: `deployments`,
  `deployment_runs`, `deployment_phases`, `deployment_module_versions`,
  `platform_versions` (§2).
- **`modules/platform-deploy-mgmt/`** — its own FastAPI app (routes + service, the
  API in §6), own container image, own CI/deploy pipeline, own k8s pod set, own
  `/api/deploys/*` API Gateway route (§1a). Never mounted in the gateway.
- Shared-package extraction of the gateway's `auth`/`shared` code so the deploy
  module imports it as a library (§1a "Packaging work").
- Gateway internal `/internal/deploy-phase-update` reconcile endpoint (written to
  by the orchestrator/verify side-channel; may live on either app — see §4).
- SSE `/deploys/{id}/events` (over the existing stream primitive).
- SPA `modules/gateway/frontend/src/pages/Deployments/` — list, deploy dialog
  (with visibility + version), progress view (phase-state machine), upgrade view;
  the tab renders only when the deploy API is present (§1a graceful degradation).
- Orchestrator side-channel write (a change in `agent-worker`) + the
  verify-before-PASS hard rule (#1139).

**Reused unchanged (or minimally extended):**
- `spawn-deploy-instance.yml` — **extended** to accept + seal the tenant inputs
  (`owner_user_id`, `org_id`, `team_id`, `credential_id`, `deployment_id`,
  `version`); mechanics unchanged.
- Connect-AWS flow, `load-deploy-config`, all phase apply pipelines,
  `@agent-operations`, S3/DynamoDB evidence, the SSE primitive.

**Depended-on, tracked elsewhere:**
- Phases 2–9 verification checks (#1137/#1138) — the verify-before-PASS rule is
  only as strong as the checks that exist.
- Fresh-account hardening (#2571) — the deploy engine the API wraps must reach
  zero-touch; until then the `needs_human` phase state carries the gaps.
- GitHub App linking (#1125 / #2592) — Phase 9 must become non-interactive (or
  the SPA models it as an explicit `needs_human` step).

---

## 9. Open questions

1. **Org-level visibility default** — org-shared is the recommended default, but
   should the platform ship a hard org policy that can *forbid* `org` visibility
   for regulated customers?
2. **Service-account issuance** — the automation path assumes token-authenticated
   service accounts (`ServiceAccount` exists in the org model); the token
   issuance/scoping UX for "give my CI a deploy token" is unspecified.
3. **`LISTEN/NOTIFY` vs poll** for the SSE tail — start with a short DB poll
   (simplest, correct); revisit `NOTIFY` only if fan-out cost warrants.
4. **Version catalog source** — start `platform_versions` as a checked-in
   manifest updated by release CI, or a table populated by a release workflow?
5. **Deploy-capable IAM tier** — the current Connect-AWS role is `ReadOnlyAccess`;
   a deploy needs write. Tracked separately (permission-tier decision deferred).

---

## References

- EPIC #969 (Deployment Lifecycle) · sub-issues #1136 (framework, closed),
  #1137/#1138 (phase checks), #1139 (orchestrator rules), #1140 (monitor),
  #1141 (upgrade), #1142 (module + admin SPA), #1143 (one-click SPA), #1125
  (GitHub App orchestration).
- [`phase-verification.md`](./phase-verification.md) — per-phase check spec.
- Connect-AWS: `modules/gateway/src/auth/aws_connect_routes.py`, `ConnectAws.tsx` (#562).
- Spawn + cross-account assume: `.github/workflows/spawn-deploy-instance.yml`,
  `.github/actions/load-deploy-config`, `platform/scripts/assume-customer-creds.py`.
- Evidence infra: `modules/platform-deploy-mgmt/infra/main.tf`.
- Fresh-account reliability EPIC #2571; deploy-instance example #2899.
