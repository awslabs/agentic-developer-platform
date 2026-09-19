# Design Note: Agent Models UI for humans and managed service accounts (Issue #5422, PMM-04)

**Status:** **Proposed**, pending the epic operator's cross-story synthesis for #5417. **Not architecture-approved and not implementation-approved.** An earlier revision of this note described itself as architecture-approved; that was wrong. The synthesis gate on #5417 (2026-09-18) reserves to the operator the publication of one unified, versioned design reconciling all nine PMM notes, and states that story-local documents "may provide detail, but may not override the canonical design silently." This note is therefore story-local detail offered *into* that synthesis, not a settled contract. Where §5's shapes disagree with the canonical design once published, the canonical design wins.
**Story:** #5422 [PMM-04], native child of EPIC #5417.
**Depends on:** #5419 (PMM-02, API) and #5420 (PMM-03, catalogue) for data; #5418 (PMM-01) for vocabulary and the six locked decisions.
**Binding inputs:** the #5417 **unified architecture rulings** of 2026-09-18 (U1 canonical service principal, U2 compatibility ownership, U3 probe safety, U4 snapshot authority, U5 ARC root identity, U6 one API contract). They "supersede conflicting story-local recommendations", so where an earlier revision of this note recommended otherwise, the recommendation is **removed**, not retained alongside the ruling.
**All repository citations checked at revision `c4809bb1`** (current default-branch tip). The previous revision cited `ae598410`; every file this note cites is **byte-identical between the two** (verified: `git diff ae598410 c4809bb1` over the cited paths is empty), so all line numbers carry forward. Where a claim could not be verified it is marked **unverified** rather than asserted.

---

## 0. Executive summary

### 0.1 What this screen is

One authenticated page listing every agent persona the platform actually registers, showing for each one the model the viewer has saved, the model that will really be used and why, whether that model is permitted and **proven callable for that persona's execution harness**, and what it costs. Per-row save and reset. A scope selector, shown only when the viewer manages at least one service principal, that switches the page to administering that machine principal's mappings.

### 0.2 Verdict

**Not ready for implementation, but no decision remains open.** All four decisions are now closed (§1), and this note is a complete design input awaiting the operator's #5417 synthesis and merge of its two data dependencies. It is **not architecture-approved and not implementation-approved**: the synthesis gate reserves publication of the unified design to the operator.

The four closed decisions:

- **D-A (closed by U1/U6):** the page consumes **`GET /me/persona-models/manageable-service-principals`**, whose entries are keyed by **`canonical_service_principal_id`**; administered reads and writes go to **`GET|PUT|DELETE /service-principals/{canonical_id}/persona-models[/{persona_key}]`**, tenant-checked server-side. The page stays agnostic to backing namespaces and performs no alias joining (§1.1).
- **D-B:** **Agent Models** at **`/settings/agent-models`**.
- **D-C:** **one** current-UI page, plus a `journeys.ts` entry pointing at that same route — explicitly *not* a duplicate page under `next/`.
- **D-D:** **gated** by a **strict backend-served `agent_models` flag defaulting false**, enabled only in PMM-09 after enforcement readiness.

**What actually gates implementation now is data, not decisions.** Under U2 and U3 the page's own construction is unblocked, but the screen cannot accept a saved preference until PMM-09 enables probing (§0.4). That is the single most consequential fact in this note and it is new in this revision.

### 0.3 The findings that change the story as written

| # | Finding | Consequence if not resolved |
|---|---|---|
| F1 | **Three service-principal identifier spaces exist, not one.** An IAM/SigV4 caller resolves to `agent_name` from the DynamoDB agent registry; a Postgres-registered service account resolves to `service_accounts.id`; a Cognito client-credentials caller resolves to `client_id`. The story names a fourth, unrelated thing (`service_authority.py`) as the entitlement source | A selector bound to any one namespace omits the service principals the epic promises to support. **The page is namespace-agnostic and consumes the U1/U6 server-reconciled list keyed by `canonical_service_principal_id`** (§1.1) |
| F2 | **Availability is a property of the (model, harness) pair, not of the model** — and the platform's D4 default is a **candidate**, not a proven default, until PMM-09 runs a bounded invocation with the real harness request shape (U2) | A page that renders availability per model, or renders the candidate default as verified, tells a person their policy was checked when nothing was invoked — the inert-config defect at platform scale (#2300). **The page filters per persona compatibility class and renders `candidate` as its own state, distinct from proven and from refused** (§4.3, §4.4, §7) |
| F3 | **Gating this page is materially more than the five files an earlier revision of this note claimed** — thirteen touch points across **three** modules, including **two independent manifest renderers** (CI and self-managed) and four exhaustive test fixtures that fail outright when a flag key is added | The earlier "five-file" shorthand would have shipped a flag that is permanently stuck off on the `deploy-all.sh` path, and a branch that fails checks the developer did not expect. Full traced inventory in §1.4.1; which check catches which omission in §8.4 |
| F4 | **The shared `Table` component scrolls horizontally and cannot satisfy AC-10 as-is**, and the shared `Select` cannot express *why* an option is disabled as AC-03 requires | Two ACs fail against the obvious reuse choices. Both need a stated deviation, not a discovery mid-implementation |
| F5 | **No usable HTTP precondition path exists.** The shared client's `get`/`put`/`delete` verbs expose no way to set a request header, so `If-Match` is unreachable without widening them for every caller | AC-08's conflict signal must ride in the request/response body. This confirms the story's own reading and matches PMM-02's §4.4 (§5.3) |

### 0.4 The consequence the operator should see plainly — nothing is selectable on day one

U3 ships probing **disabled with a zero spend budget**, and U2 makes the D4 identifier a candidate rather than a proven default. PMM-03's §4.7 states the consequence for this page directly: with probing off, **every model fails admission gate 5, so PMM-02's save path refuses every save.**

So on first deploy the page can honestly show a person what their effective model *is* and why — and **cannot accept a change**. That is correct fail-closed behaviour, not a defect, but it is the strongest argument for D-D's flag and it must not be discovered during acceptance:

- The page must render the unproven state — `invocable: null` with **no** `evidence{}` and reason `not_yet_certified` (§5.5) — as the **normal** state, not an edge case (§4.4 state 4, §7).
- A refusal on save must read as "not yet certified", never as "this model is broken" — those are different operator actions, and only one of them is anybody's to fix (§4.4).
- **AC-05's save path cannot be proven live until PMM-09.** Against fixtures it is provable now; that limit is recorded in §9 rather than left for the acceptance run to find.
- **PMM-09 must certify before it enables the flag** (§8.3). Enabling first yields a reachable page that refuses every save — technically correct, and the worst possible first impression.

---

## 1. Decisions

All four are **closed**. Each records the ruling, what it authorizes, and the evidence a developer needs. Superseded alternatives are **removed**; §11.1 records that they existed and why they were withdrawn.

### 1.1 D-A (CLOSED by U1 + U6) — The canonical service-principal contract

**Ruling.** U1 assigns PMM-02 (#5419) "an opaque immutable ADP `canonical_service_principal_id`, the alias registry, canonical resolution in authentication, and the manageable-service-principals endpoint". U6 fixes the exact surfaces. **The page therefore has no store to choose and no path to invent** — both are named in §5.4.

U1's operative constraint for this page: *"Raw `service_accounts.id`, `agent_name`, `client_id`, ARN or caller-supplied text never owns a preference."* The browser consumes canonical IDs only.

**Why the question was hard, which the developer still needs.** The Postgres namespace is real and administrable, but it is not the only service principal used at runtime. Three distinct identifier spaces exist, re-verified at `c4809bb1`:

**Labelling follows PMM-02's, not this note's earlier numbering.** PMM-02 owns the identity slice under U1, and its §3.2.1 numbers these S1/S2/S3 in a different order than an earlier revision of this note did. Two notes numbering the same three things differently is how a developer mis-reads a cross-reference, so this note adopts PMM-02's numbering verbatim:

| # | How a machine caller authenticates | Subject in `TokenContext.user_id` | Evidence |
|---|---|---|---|
| S1 | **SigV4 → STS → Postgres role registration** | `service_accounts.id` (row UUID) | `auth/tenant_resolver.py:283-304` selects `ServiceAccount` by `iam_role_arn` + `org_id` and returns `entity_id=service_account.id` |
| S2 | **API Gateway SigV4 → DynamoDB Agent Registry** | `agent_name` | `auth/middleware.py:395-418` parses the assumed-role ARN and calls `get_agent_by_role_arn`; `auth/agent_registry.py:245-256` builds `TokenContext(user_id=entry["agent_name"], account_type="service", auth_source="iam")` |
| S3 | **Cognito `client_credentials`** | `client_id` — "client_credentials tokens use client_id as the subject" | `auth/auth_service.py:308-312`: `if account_type == "service" and not claims.username: user_id = claims.client_id or claims.sub` |

None is derivable from the others in the browser, so **a selector bound to any single one silently omits the rest.** That is why U1 puts reconciliation server-side and gives the page one canonical ID.

The story's prerequisite table cites `modules/gateway/src/agentauth/service_authority.py` as the entitlement source. That file is real but is a **fourth and unrelated** thing: `service_authority.py:51` constrains `service_identity` to `^eventbridge:[A-Za-z0-9_.-]{1,128}$`, and `ServiceApproval` carries `root_personas` / `child_personas` with an expiry the store caps at 30 days, gated on a human holding `Permission.PLAN_APPROVE`. That is **bounded standing delegation for event-driven dispatch** — persona *authorization*, time-boxed — not a principal that holds durable model preferences. It is out of scope for PMM-04 as an entitlement source. Its superficial attraction is that it is *already persona-scoped*, which is precisely why the confusion is easy.

#### 1.1.1 The five fields the page consumes

The page is **agnostic to backing namespaces** and performs **no alias joining or inference**. Each entry in the §5.4 list carries, after **server-side identity reconciliation**:

| Field | Why the UI needs it |
|---|---|
| **`canonical_service_principal_id`** | The single opaque identifier the page echoes back on an administered write. The page never constructs, completes, parses or splits it, and never learns which of S1/S2/S3 produced it. U1: opaque means it "encodes nothing about its aliases" |
| **Display name** | AC-06's "show the account name … before the save commits" |
| **Tenant** | AC-06's tenant disclosure. Display-only; never sent on a request (§6.3) |
| **Source / provenance** | So a person administering two principals with similar names can tell them apart, and so an operator debugging a missing entry can see which namespace it came from. Rendered as a label; **never** used by the page to branch behaviour or to reconstruct an identifier |
| **Management entitlement** | Whether *this viewer* may manage *this* principal. Server-computed; never derived in the browser from a role claim (§6.2) |

**The naive implementation the developer will find, and must not use.** AC-06 requires a name and tenant before the save commits, and AC-07 requires that a viewer managing nothing causes no selector to render and only self rows to be requested. Both are properties of *what the server returns*. A developer searching for "a list of service accounts" will find `GET /admin/organizations/{org_id}/service-accounts` (`admin/routes.py:1205-1240`, gated on `Permission.ORG_READ`) and `GET /auth/service-accounts` (`auth/routes.py:346-370`). Neither is the picker: both answer for one namespace (S1) only, and PMM-02's §5.3.1 records that the latter **has no authorization check beyond authentication**, so reusing it would inherit a missing gate. The picker is the U6 endpoint in §5.4 and nothing else.

**The honest limit on "manageable", which PMM-04 inherits rather than decides.** PMM-02's §5.3.1 records that `service_accounts` has no owner column and `department_id`/`team_id` are bare strings with no foreign key (`shared/models/organization.py:213-214`), so **no per-caller predicate is expressible today**. Under the chosen authority, "principals you may manage" therefore means *every service principal in your tenant, if you hold org-admin authority over that tenant* — tenant enumeration, not per-principal entitlement. The page's design is unaffected, because entitlement is server-supplied either way. But **AC-06's acceptance evidence must state which authority was in force**, or it will read as proving delegated ownership when it proved admin access.

**Owner:** epic operator, with PMM-02's developer, settled once across #5419/#5422/#5423/#5424. **Blocks:** AC-06, AC-07, and the scope-selector contract in §5.4. **Under every possible answer, the UI must not infer entitlement client-side** (§6.2).

### 1.2 D-B (CLOSED) — Page name and route

**Ruling: the page is "Agent Models" at `/settings/agent-models`.** Operator comment of 2026-09-18 on this PR. The collision evidence below is retained because a developer needs it to avoid re-creating the ambiguity; no alternative is proposed.

Verified state of the collision at `c4809bb1`:

- `/model-access` renders `modules/gateway/frontend/src/pages/ModelAccess.tsx` — 7 lines, confirmed. It renders `BedrockAccountRouting` for platform admins and tells everyone else "Model routing is managed by a platform admin." Registered at `App.tsx:116`. It is about **which AWS account serves the call**, not which model runs.
- `modules/gateway/frontend/src/components/next/journeys.ts:286-292` carries `model-access-personal`, labelled "Model access", described "Choose the AWS account used for your own model calls, or inherit the default. On the Credentials page today." Its `to` is `/settings/credentials` — the entry deliberately points at the page where the capability works rather than at a stub, per the comment at lines 282-285. A second entry `model-access-admin` at `journeys.ts:431-432` points at `/model-access`, scope `platform`.
- #5092 [NUI-14] "Migrate Model access into personal and scoped administrator journeys" is **OPEN** (last updated 2026-09-14). #5089 [NUI-11], which is to finalize model-access authority and precedence, is **OPEN**. #5078 is **OPEN**.

So three surfaces would be called "Model access" or a near-variant, two of them already are, and the effort that would disambiguate them is itself unfinished.

**Why this name.** It is the only one of the three whose subject is *which model runs*, and the word "agent" is the distinguishing noun a person can act on: the existing pages answer "who pays", this one answers "what runs". `/settings/models` was rejected — #1309 reserved it for a never-built per-user use-case mapping, and it reads as a sibling of "Model access".

**Consequence for the developer:** the route is registered at `/settings/agent-models` in `App.tsx` alongside the existing `/settings/*` routes (`App.tsx:120-122`), and the page component belongs in `pages/settings/` to match `Connections.tsx` and `SettingsCredentials.tsx`. **Unblocks:** route registration, AC-01's fixture.

### 1.3 D-C (CLOSED) — One current-UI page, with a `journeys.ts` entry pointing at it

**Ruling: implement one current-UI page and add a `journeys.ts` entry pointing to that same route — not a duplicate page.** Operator comment of 2026-09-18 on this PR.

#5078's coexistence contract keeps the current UI the default, with the new shell opt-in and fail-closed. `journeys.ts` is explicitly the single source of truth for new-UI navigation and is deliberately pure — no React, no component imports (`journeys.ts:1-10`).

**The design as ruled:** a route in `App.tsx`, a gated entry in `Navigation.tsx`, and **one** `journeys.ts` entry pointing at that same route with `currentUi: true`. The established precedent is exact: `journeys.ts:280-292` already does this for `model-access-personal`, whose own comment says it "points at the page where the capability actually works, labelled as such, rather than at a stub". Following it satisfies AC-01 in one place, avoids a second implementation, and leaves #5092 free to re-home the entry later without touching the page.

**Consequences the file inventory must carry:**

- The `journeys.ts` entry is a *navigation* change in the preview shell, **not** a second page. Nothing else under `next/` is touched.
- The entry must be gated on the same flag as the route (§1.4). `journeys.ts` already reads `features.*` for exactly this purpose at `:207`, `:230`, `:243`, `:263`, `:272`, `:294`, `:307`, `:317`, `:442`, `:484`, `:493` — so `if (features.agent_models)` is the existing shape, not a new one. Its module docstring states the reason this matters: two tables that must agree, where drift produces "a nav entry the server will refuse with 403, or a capability that silently disappears for the people who hold its permission" (`journeys.ts:11-21`).
- `journeys.ts` has its own test file asserting the gating model directly (`journeys.test.tsx`, per `journeys.ts:27-28`). A new gated entry needs an assertion there, which is part of the "extra cross-module tests" the operator ruled intentional.

**Unblocks:** route registration, AC-01.

### 1.4 D-D (CLOSED) — Gated by a strict backend-served `agent_models` flag, default false, enabled in PMM-09

**Ruling (operator comment of 2026-09-18 on this PR):** "gate the page/edit capability with a strict backend-served `agent_models` feature flag defaulting false and enable it only in PMM-09 after enforcement readiness. The extra cross-module tests are intentional."

**Why the gate is right, stated once.** The epic's enforcing flip is PMM-09's (#5427), and §0.4 establishes that until then **no save can succeed at all** — every model fails admission gate 5 while probing is off. A page reachable before that point invites a person to set a policy the platform will refuse to store. The flag is how the page and the enforcement arrive together, and it is a rollback lever that needs no revert and no CloudFront invalidation.

**What the story got wrong, and still must be corrected before implementation.** The story says: "If the page is feature-gated, the flag default is part of this story and must be stated in the PR." A flag default in the frontend is not what gates a page. Verified mechanism:

1. Flags are served by the **backend**: `modules/gateway/src/features/routes.py` — `get_features` at line 56 returns a `features` object, with `connections` and `credentials` read via `_is_enabled(...)` (fail-open unless explicitly `"false"`, lines 67-68), while newer rollout flags use `_is_enabled_strict(...)` (fail-closed; true only on an explicit `"true"`).
2. The frontend `FeatureFlags` interface and the `ALL_FEATURES_ENABLED` fallback live in `modules/gateway/frontend/src/services/features.ts`. That fallback is what renders **while the fetch is in flight and whenever it errors** — `useFeatures` returns `data ?? ALL_FEATURES_ENABLED` (`hooks/useFeatures.ts`). `FeatureGate` (`components/FeatureGate.tsx`) redirects to `/` when the flag is false, and withholds the screen with a spinner while a fail-closed flag is pending, deliberately without discarding the URL.
3. **The env var must be set in `modules/gateway/k8s/deployment.yaml`.** That file carries a standing warning, written twice: *"it has to be set HERE rather than with `kubectl set env`, because gateway-deploy.yml re-applies this file on every run and would silently revert an out-of-band change on the next deploy"* (`deployment.yaml:170-177`, and again at `:166-167`). Per-environment flags use a `__FEATURE_X__` placeholder rendered from SSM (`deployment.yaml:168-169`, `:198-199`).

#### 1.4.1 The full flag inventory — corrected, and traced rather than estimated

**An earlier revision of this note called this a "five-file change". That was wrong and the shorthand is withdrawn.** It was reached by listing the files a flag *conceptually* needs, not by tracing an existing one. Tracing `agent_control` (#3960), `budget_spend` (#4402) and `new_ui` (#5079) end to end at `c4809bb1` shows **thirteen** touch points across **three** modules. The two most consequential omissions are marked ⚠ — one makes the flag unusable on a whole deploy path, the other fails four checks the developer did not expect.

Grouped by what happens if the entry is omitted, which is the distinction that matters when planning the PR.

**Group A — the flag does not work at all if omitted.**

| # | File | Change |
|---|---|---|
| A1 | `modules/gateway/src/features/routes.py` | Add `"agent_models": _is_enabled_strict("FEATURE_AGENT_MODELS_ENABLED")` to the `features` dict in `get_features` (`:54-96`). **`_is_enabled_strict` (`:23-33`), not `_is_enabled` (`:36-52`)** — the latter returns true unless explicitly `"false"`, which would enable the page in every environment the gateway ships to |
| A2 | `modules/gateway/frontend/src/services/features.ts` | Add `agent_models: boolean` to `FeatureFlags` (`:9-27`) and **`agent_models: false`** to `ALL_FEATURES_ENABLED` (`:38-56`). The `false` is load-bearing: `useFeatures` returns `data ?? ALL_FEATURES_ENABLED`, so this value renders **while the `/features` fetch is in flight and whenever it errors** |
| A3 | `modules/gateway/frontend/src/App.tsx` | Wrap the route in `<FeatureGate feature="agent_models">`, mirroring `:120-122` |
| A4 | `modules/gateway/k8s/deployment.yaml` | Add `- name: FEATURE_AGENT_MODELS_ENABLED` with value `"__FEATURE_AGENT_MODELS_ENABLED__"`, following `FEATURE_AGENT_CONTROL_ENABLED` (`:198-199`) — **not** `FEATURE_BUDGET_SPEND_ENABLED`'s committed literal `"true"` (`:178-179`), which would arm every environment on its next deploy |
| A5 | `.github/workflows/gateway-deploy.yml` | Add the `get_ssm "/adp/${ENVIRONMENT}/gateway/feature-agent-models" "false"` read with its `None`/empty guard (pattern at `:487-491`, `:502-506`, `:519-523`) **and** the matching `-e "s|__FEATURE_AGENT_MODELS_ENABLED__|...|g"` clause in the `sed` at `:526-529`. Without it the literal placeholder reaches the pod and `_is_enabled_strict` reads it as false — fails safe, but the flag can never be turned on and the failure is silent |
| **A6** ⚠ | `platform/scripts/deploy-all.sh` | **The entry the five-file shorthand missed entirely.** `deploy-all.sh` renders the *same* placeholders independently at `:1088-1095` — its own `_get_ssm` reads and its own `sed`. It is the self-managed deploy path per `CLAUDE.md`. Omitting it means the flag works on the CI path and is permanently stuck off on the self-managed one, with no error either place |

**Group B — a required check fails if omitted.** ⚠ These are why the operator's "extra cross-module tests are intentional" is not a throwaway line: four fixtures enumerate every flag **exhaustively** and fail outright on a new one.

| # | File | Why it fails |
|---|---|---|
| B1 | `modules/gateway/tests/features/test_routes.py` | `test_all_enabled_by_default` asserts **`data == {...}`** on the whole payload (`:64`) — exact dict equality, so a new key fails it. Also add the var to the `delenv` list (`:43-58`) and a default-false case mirroring `:256-264` |
| B2 | `modules/gateway/frontend/src/__tests__/services/features.test.ts` | The `required: Record<keyof FeatureFlags, true>` exhaustiveness map (`:68-80`) has **no index signature**, so `tsc` reports the missing key. Its own comment explains that this shape exists *because* an array-of-keys check let `budget_spend` drift past it |
| B3 | `modules/gateway/frontend/src/__tests__/components/FeatureGate.test.tsx` | `mockFeatures: FeatureFlags` (`:22-33`) is an annotated literal. Note its comment: four flags had already silently drifted out of it, and `tsconfig.json` **excludes `__tests__`** (`:24-29`), so `npm run build` never reports this — only `vitest` does. Same for `FeatureGate.loading.test.tsx` |
| B4 | `modules/gateway/frontend/src/mocks/handlers/features.ts` | The MSW handler returns a literal flag set (`:11-31`). A missing key makes every test see `undefined` rather than `false` — gates the same way, but silently, which is the drift B3's comment documents |
| B5 | `modules/domain-apps/superplane/superplane_acceptance/features.py` | **A third module.** `REQUIRED_FIELDS` (`:18-31`) plus a **SHA256-pinned** fixture (`FIXTURE_SHA256`, `:33`; `tests/fixtures/features.json`). The live check is `set(expected) <= fields.keys()` (`:196`) — a *subset* test, so a new flag in the response does **not** break it. Verified: no change is strictly required here. Listed because a developer who edits the fixture without re-pinning the hash gets "Reviewed features fixture changed; review provenance before updating its pin" |

**Group C — consequential but non-breaking.**

| # | File | Change |
|---|---|---|
| C1 | `modules/gateway/frontend/src/components/Navigation.tsx` | The gated nav entry, `if (features.agent_models)` — existing shape at `:109`, `:140`, `:145` |
| C2 | `modules/gateway/frontend/src/components/next/journeys.ts` | The gated journeys entry (§1.3) |
| C3 | `modules/gateway/frontend/src/__tests__/components/next/journeys.test.tsx` | An assertion for the new entry, per the registry's own gating-parity discipline |

**What this does *not* require.** No Terraform: the SSM parameter is created by `aws ssm put-parameter` at enablement time, not declared in infra — verified, no `feature-agent-control`, `feature-budget-spend` or `feature-new-ui` parameter appears in any `*.tf` file. And no dedicated CI workflow: `agent-control-ci.yml` exists because that flag opens a channel into a running pod, which a settings page does not.

**The manifest warning is standing and applies here.** `deployment.yaml` states twice that the value "has to be set HERE rather than with `kubectl set env`, because gateway-deploy.yml re-applies this file on every run and would silently revert an out-of-band change on the next deploy" (`:166-167`, `:170-177`). `kubectl set env` is not an enablement mechanism for this flag.

#### 1.4.2 Consequences for the implementation PR

- **Both gateway module check suites apply**, because the diff spans Python and frontend: `cd modules/gateway && ruff check src/ tests/ && ruff format --check src/ tests/ && python3 -m pytest tests/ -q` **and** the frontend lint plus `npx vitest run`. §8.4 records the matrix.
- **`npm run build` alone does not catch the B-group failures.** `tsconfig.json` excludes `__tests__` and `src/mocks`, so the exhaustiveness guards only fire under `vitest`. A developer who checks the build and skips the suite will believe the flag work is complete.
- **Enabling it in an environment is a manual operator action** — `aws ssm put-parameter` against one environment, then re-run the deploy job — **owned by PMM-09 (#5427)**, not by this story and not by merging code. PMM-04's PR ships the flag **off everywhere**.
- **Rollback is flipping the flag off**, which removes the route and both nav entries with no SPA redeploy.
- **The page must not be reachable when the flag is false**, including by deep link. `FeatureGate` provides this; a nav-entry-only gate would leave `/settings/agent-models` typeable.

**Owner of the flip:** PMM-09 / epic operator.

### 1.5 D-E (CLOSED by U2) — Availability is rendered per persona compatibility class, not per model

**This decision is new in this revision** and is the substantive correction the latest review asks for. Earlier revisions of this note treated "permitted and callable" as a property of a **model**. U2 establishes it is a property of the **(model, execution harness) pair**, and PMM-03 (#5420) owns the registry.

**Why this is not a relabelling.** Proof that a model is callable is obtained by invoking it *the way one particular harness invokes it*. PMM-03's §2.4 states the consequence: **"No cross-class fallback, ever. A model proven invocable for `claude-agent-sdk` is not admissible for `codex-sdk`, because the proof used the Claude harness request shape."** A page that rendered one availability answer per model would offer a persona a model whose proof came from a harness that persona does not run — the one failure mode the compatibility contract exists to prevent.

**The contract the page consumes** (PMM-03 §2.4, §6.1, §6.2):

| Element | Shape | What the page does with it |
|---|---|---|
| Persona row's **`compatibility_class`** | Stable, **unversioned** ID: `claude-agent-sdk` today for all twelve personas; `codex-sdk` reserved for #5433's native GPT personas with no personas mapped yet | Selects which models the row may offer. The page **sends `persona_key`** and the server filters; the page never filters by class itself |
| **`harness_contract_revision`** | A **separate versioned** field on each model row | Rendered as part of the evidence provenance. **Never** concatenated into the class ID — U2 keeps them separate so a harness upgrade revises evidence without renaming a class and invalidating stored keys |
| **Candidate vs proven** | `us.anthropic.claude-sonnet-4-6` is a Claude-class **candidate**, not an active proven default, until PMM-09 records a bounded invocation with the real harness request shape | A distinct rendered state (§4.4, §7). **The page must never render a candidate as verified** |
| **`configurable` / `not_configurable_reason`** | PMM-03 §2.3: a persona may be listed but non-configurable — `pt-superpower` is, pending #4037 | A disabled row **with its reason shown**, not an unexplained absence. AC-02's key still appears |

**What this changes in the design.** The page sends `persona_key` on the catalogue read so the server filters by that row's class (§5.5), and the global `selectable_models` array is removed from the list response (§5.1); the availability column renders four states rather than three (§4.4); the state machine gains the nothing-certified and non-configurable rows (§7); and §9's AC-03, AC-05 and AC-09 limits are restated. **Locked D6 is unchanged and is the same rule** — harness compatibility is a server-validated property and the page hard-codes no family. U2 makes D6 *more* specific by naming the class as the unit.

**One divergence resolved in PMM-03's favour.** An earlier revision of this note flattened availability evidence to `evidence_at` + `stale`. PMM-03's §6.2 keeps it **nested with the destination in it** (`evidence{account_id, region, verified_at, expires_at, stale}`) and is right: invocability is a property of a destination, and an operator debugging "not invocable" cannot act without knowing which account refused. **This note adopts the nested shape.** The page may still *render* only the age; the fields must exist in the contract (§5.1).

### 1.6 Non-blocking but must be stated in the PR

| Item | Status | Handling |
|---|---|---|
| Fable 5.1 selectability | **Unverified, with contrary evidence for Fable 5.** #5420 records both alias maps excluding `claude-fable-5` as listed-but-non-invocable pending a non-default data-retention mode, and Fable 5.1 appearing only in pricing artifacts | The page **renders whatever the catalogue returns** and hard-codes no family (§4.3). Under U3, Fable is not a special case: *every* model is unproven at PMM-03's completion. Fable's narrower distinguishing fact is that it has **contrary** evidence, not merely absent evidence |
| "Claude-only" is a statement about today, not a design constraint | All twelve personas resolve to `claude-agent-sdk` today, but #5433 registers `gpt-*` personas with a **separately proven** `codex-sdk` default | The page must not hard-code the Claude class or assume one class exists. It renders whatever classes the catalogue returns (§4.3). This was a non-blocking handoff correction in the second-pass review and is now design |
| PMM-02 / PMM-03 not built | Confirmed not built at `c4809bb1` | Build against the §5 contract behind a typed service module; AC-02..AC-09 are provable against fixtures, live behaviour is PMM-09's (§8.2) |

---

## 2. Scope, and the one thing this page must never become

**In scope:** the page, its route, its navigation entry, persona rows with saved-versus-effective model and source, **rendering** the persona-filtered selectable set with per-option disabled reasons, per-row save and reset, the service-account scope selector, the loading / default-only / nothing-certified / error / stale / conflict / partial-save states, price and availability context, keyboard operability and a usable narrow-width layout, and the `agent_models` flag plumbing (§1.4.1 — cross-module, and not frontend-only).

**Out of scope:** backend endpoints (#5419), catalogue construction and the class/compatibility rules (#5420), CLI (#5423), certification of any model (#5427/PMM-09), and editing persona definitions or prompts — excluded by the epic. Note especially that **deciding** what is selectable is out of scope even though **rendering** it is in scope; §4.3 explains why that line is load-bearing rather than pedantic.

**The invariant that governs every decision below:** *this page is an affordance, never an authority.* The server decides what may be read, what may be selected, and what may be written. Every filter, disable and hidden control here exists to explain, not to enforce. §6 states this as testable properties.

---

## 3. Information architecture

### 3.1 Page shell

Two renderings, because the state the page **ships in** is not the state it eventually reaches. Getting these the wrong way round is how the unproven case becomes an unhandled edge case.

**As it ships (probing disabled — U3, §0.4). This is the normal state, not an error:**

```
Agent Models                                    /settings/agent-models
──────────────────────────────────────────────────────────────────────
[ Scope: ● My own agents  ○ svc-nightly-triage (acme) ]   ← only when entitled
──────────────────────────────────────────────────────────────────────
A change here applies to the next agent run you start, not to a run
already in progress.

No model has been certified for use yet, so selections cannot be saved.
Your effective model below is correct and is unaffected.
──────────────────────────────────────────────────────────────────────
PERSONA         EFFECTIVE MODEL        AVAILABILITY       PRICE   ACTIONS
architect       Sonnet 4.6             ◦ not yet          $x/$y   [Save] [Reset]
  Designs …     (platform default,       certified                (disabled)
                 candidate)              no probe has run
pt-superpower   Sonnet 4.6             ◦ not yet          $x/$y   —
  Runs …        (platform default)       certified                not configurable
                                                                  (see #4037)
```

**Once PMM-09 has certified models:**

```
PERSONA         EFFECTIVE MODEL        AVAILABILITY       PRICE   ACTIONS
architect       Sonnet 4.6             ✓ verified 2h ago  $x/$y   [Save] [Reset]
  Designs …     (platform default)       claude-agent-sdk
developer       Opus 4.6               ✓ verified 2h ago  $x/$y   [Save] [Reset]
  Implements …  (your choice)            claude-agent-sdk
```

Three things in these mockups are requirements, not illustration:

- **The standing sentence about *when* a change takes effect.** Required by the story's outputs clause and load-bearing: the epic's snapshot semantics mean a save cannot alter a chain already running (#5417; locked D5 binds the snapshot at root dispatch). Without it a person reasonably reads a successful save as retroactive.
- **"Not yet certified" is never rendered as a failure, and never as verified.** It is the third availability state (§4.4) and the one the page ships in.
- **The class is shown beside the evidence, not instead of it.** "Verified" alone does not say verified *for what harness*, and under U2 that is the whole content of the claim.

### 3.2 Per-row content

Each row carries persona key and display name; purpose; **`compatibility_class`** (§1.5); `configurable` with `not_configurable_reason` where false; **saved model** (or "not set"); **effective model with its source** — "your choice" or "platform default", and whether that default is a **candidate**; model family and canonical version; availability as a four-state with its evidence age and harness revision; price context; and Save / Reset.

**Saved and effective are rendered as separate facts and never collapsed.** The saved-versus-effective distinction is the whole reason the list endpoint returns both (#5419). Collapsing them reproduces the `#4511` inert-config defect the epic exists to prevent: a stored value displayed as though it governs. The precedent is explicit — `BedrockAccountSelector.tsx` reads `own_selection_active` from the server precisely so a stale-but-stored pick is never shown as active, and `bedrockRoutingSelf.ts`'s docstring states *"showing the user's own stale pick as active is the inert-config defect."*

### 3.3 Why "Agent Models" and not a variant of "Model access"

Three surfaces, three different questions:

| Surface | Question it answers | State |
|---|---|---|
| `/model-access` (`ModelAccess.tsx`, 7 lines) | Which **AWS account** serves the call | Shipped, platform-admin-gated |
| `journeys.ts` `model-access-personal` | Which **AWS account** serves *my* calls | Entry exists, points at `/settings/credentials` |
| **This page** | Which **model** runs each kind of agent | Proposed |

"Agent Models" names the subject (models, per agent) rather than the verb ("access"), which is what makes it distinguishable at a glance from the two account-routing surfaces. Rejected: `/settings/models` (reserved by #1309, reads as a sibling of "Model access"); "Model preferences" (does not say *whose* or *for what*); anything containing "access" (the collision itself).

---

## 4. Reuse, and where reuse does not reach

### 4.1 Precedents verified at `c4809bb1`

| Component | Path | Verified | Reuse |
|---|---|---|---|
| Personal scoped selector | `components/aws/BedrockAccountSelector.tsx` | 370 lines ✓ | **Closest analogue.** The five patterns in §4.2 |
| Self-scoped service module | `services/bedrockRoutingSelf.ts` | 102 lines ✓ | No-target-parameter discipline (§6.1) |
| Admin table with row edit | `pages/RateLimitManagement.tsx` | 304 lines ✓ | `Column<T>[]`, empty state, refetch-after-mutation |
| Row-edit modal | `components/ratelimit/RateLimitFormModal.tsx` | 227 lines ✓ | Only if row-inline editing proves insufficient |
| Settings page conventions | `pages/settings/Connections.tsx` (539), `pages/settings/SettingsCredentials.tsx` (228) | ✓ | `useState` + `useCallback` loader + `useEffect` + `useToast()`; **no react-query** — confirmed by reading both files |
| Error extraction | `services/credentials.ts:130` `extractCredentialError` | ✓ | Refusal rendering |
| Tri-state rule | `services/connections.ts:23-24` | ✓ verbatim | Availability column (§4.4) — **extended to four states**, not adopted as-is |
| Route + nav registration | `App.tsx` (218), `components/Navigation.tsx` (231) | ✓ | Extend both. There is no settings hub or tab shell to plug into |

**One story citation is wrong:** the story lists `pages/SettingsCredentials.tsx`. The file is `pages/settings/SettingsCredentials.tsx` (`App.tsx:37` confirms the import path). Minor, but the developer's file inventory must be right.

### 4.2 The five patterns to carry over from `BedrockAccountSelector`

Each exists because of a specific defect it prevents, and each maps onto an AC here:

1. **Separate `loadError` from `saveError`** (`BedrockAccountSelector.tsx`, state block). Its comment: collapsing them "would let a refusal blank out the effective-destination line that is still true." → AC-04, AC-05.
2. **`applyResult` re-renders from the server's own answer**, never an optimistic guess — "a save can succeed while something else still governs." → AC-05.
3. **Per-row busy id** (`busyId`), so one row saving never disables the rest. → AC-11.
4. **`refusalMessage` parses `{reason, message}`** out of the thrown body and falls back through a shared reason vocabulary before the transport message. `apiClient` throws the parsed body, so a 422 arrives as `{detail: {reason, message}}`. → AC-03, AC-05.
5. **A load failure is never rendered as a default.** Its error branch says so out loud: *"This is not a statement that your calls go to the platform's account."* → **AC-04, the single most important behaviour on this page.**

### 4.3 Model selection: the shared `Select` does not reach (F3a)

`components/ui/Select.tsx` takes `options: SelectOption[]` where `SelectOption = {value, label, disabled?}` — verified at lines 3-7. A `disabled` boolean can hide a choice but cannot say **why**, and AC-03 requires each disabled choice to explain itself ("not permitted by your organization" and "not currently callable" are different facts with different remedies, and only one of them is the person's to fix).

**Recommendation:** render the model chooser as a **listbox of rows** following the `ConnectionChoice`/`AwsConnectionRow` shape already used by `BedrockAccountSelector`, where an unselectable row renders dimmed *with its reason beside it*, rather than a native `<select>`. That component solves this exact problem for AWS connections, and its comment records why the reason cannot live in the status pill: *"`verified` + unroutable is a real and confusing state … and one field cannot say both."*

Do **not** hide unselectable models. Hiding them yields an empty list and no explanation — the same reasoning `BedrockAccountSelector`'s docstring gives for rendering unselectable connections (most personal connections are legitimately unselectable, so hiding would show nothing).

**Filtering is the catalogue's answer, never a local list — and under U2 it is the *persona-filtered* answer.** The page does not fetch a global model list and filter it. It calls `GET /me/persona-models/catalog?persona_key=<key>` (§5.5) and renders exactly the rows that come back, hard-coding no family — required by AC-02, by the Fable prerequisite, and by locked D6, under which harness compatibility is a server-validated property.

Why the persona must be a server-side parameter rather than a client-side filter: the filter predicate is *"does this model's compatibility class match this persona's class, at this harness contract revision"*. The revision is the server's, changes without a frontend deploy, and has no client-visible representation. A page that fetched everything and filtered locally would be re-implementing the class-match rule in TypeScript — a second, drifting copy of a rule U2 makes authoritative in one place. `evidence` being nested per (model, class) pair (§1.5) is the same point in the data: there is no single "is this model available" fact for the page to read.

**Where a refusal needs more than a row can hold, the page links to the explain endpoint.** `GET /me/persona-models/explain/{persona_key}` (§5.5) returns the ordered admission-gate trace PMM-03 owns. The page does not compute or paraphrase that trace; AC-03 requires each disabled choice to explain itself, and for the common refusals the row's `reason` suffices, but "why is *nothing* selectable" (§0.4, the shipping state) is a five-gate answer that belongs to the server.

### 4.4 Availability: four states, because the tri-state rule does not cover "not yet certified"

The repo's rule is real and is inherited, not replaced. `services/connections.ts:23-24`: *"Every field is TRI-STATE: true = verified working, false = verified broken, null/undefined = could not determine. null MUST render amber/grey, never red."*

But that vocabulary has three values and this page has **four distinguishable conditions**, because U2 and U3 together introduce one the connections surface never had: a model that is the *published default* and has *never been probed*, on a platform where probing is off by default. Folding that into `null`/"could not determine" would be a small lie with a large consequence — "we could not determine" invites a retry, and there is nothing to retry.

| State | Source fields | Rendering | Selectable | AC |
|---|---|---|---|---|
| **Verified** | `invocable: true`, fresh `evidence{}` for *this persona's class* | Green, evidence age, class + `harness_contract_revision` | Yes | AC-03 |
| **Broken** | `invocable: false` | Red, reason shown | No | AC-03 |
| **Unknown / stale** | `invocable: null` **with** prior `evidence{}` now `stale: true` | Amber — **never red** | Per catalogue | AC-09 |
| **Not yet certified** ⚠ | `invocable: null`, **no** `evidence{}` at all; `candidate: true` on a default | Neutral/grey, *"not yet certified — no probe has run"*, never amber-as-warning and never red | No | §0.4, AC-09 |

The fourth state is the one the page ships in (§0.4) and the one a developer will most plausibly collapse into the third. Two properties keep them apart:

- **Stale means "was true, may have changed" — certified-never means "was never asserted".** The first is decayed evidence; the second is absent evidence. Only the first is a reason to show an age.
- **A candidate default is still rendered as the effective model.** `us.anthropic.claude-sonnet-4-6` genuinely governs today, so §7's AC-01 row still applies: the effective-model line is a complete and correct answer. What is *not* available is the ability to change it. Rendering the whole row as unavailable because its default is uncertified would be the mirror-image defect of the one AC-04 forbids.

AC-09's "new selections are not presented as verified" is satisfied by the third and fourth states both, for different reasons; the acceptance evidence must say which one it exercised.

### 4.5 Layout: the shared `Table` does not reach AC-10 (F3b)

`components/ui/Table.tsx` wraps its `<table>` in `<div className="overflow-x-auto">` (verified, line 42). AC-10 requires "no horizontal overflow or clipped controls at normal mobile width". A six-column table with two buttons per row inside a horizontally scrolling container **fails that AC by construction** — the container's whole purpose is to permit the overflow the AC forbids.

I also found **no existing responsive-table precedent to copy**. Responsive visibility is used in the tree — `layouts/NextLayout.tsx:268` hides the journey switch below `sm:` and repeats it in the drawer — but no page swaps a table for stacked cards at a breakpoint. So the pattern below is new to this repo, which is why it carries the justification in this section.

**Recommendation:** render each persona as a **stacked row/card** that becomes columnar at `md:` and above, rather than reusing `Table`. Reuse `Column<T>[]`-style declarative field definitions if helpful, but not the `Table` component itself. **State this deviation in the PR**, since the story's reuse table names `RateLimitManagement`'s table shape and a reviewer will otherwise read the deviation as drift. This is a new pattern in this repo, so per repository convention it needs the justification above recorded — which is what this section is.

---

## 5. Contracts

These are the shapes PMM-04 consumes. **PMM-02 and PMM-03 own them, and the operator's synthesis settles them**; this section states what the UI *requires*, so the three can be built in parallel (§8.2) and so a divergence is visible rather than discovered. Where the canonical design differs, it wins and this section is amended. Snake_case is kept verbatim so the types stay diffable against the server schemas, following `bedrockRoutingSelf.ts`.

**Alignment checked against PMM-02's note at `agent/issue-5419`.** Its self surface is `APIRouter(prefix="/me/persona-models")` (§5.2), its read model returns one row per catalogue persona with saved value, effective value and a `source` of `principal-mapping` or `system-default` (§4.5), and its concurrency fence is a body-carried revision with a 409 (§4.4) — all three agree with what follows. Two naming details differ and PMM-02's win: it calls the write field **`expected_revision`** (§4.4), and it specifies that an **absent** `expected_revision` means "create only — if a row exists, refuse", which the page must send deliberately rather than omit by accident.

### 5.1 List — self

`GET /me/persona-models` → no parameters, at any position.

```
{
  "rows": [
    {
      "persona_key": "architect",
      "display_name": "Architect",
      "purpose": "Designs systems and reviews designs.",
      "configurable": true,
      "not_configurable_reason": null,
      "compatibility_class": "claude-agent-sdk",
      "harness_contract_revision": "2026-09-01",
      "saved_model_id": "us.anthropic.claude-opus-4-6-v1" | null,
      "effective_model_id": "us.anthropic.claude-sonnet-4-6",
      "effective_source": "principal-mapping" | "system-default",
      "effective_is_candidate": true,
      "model_family": "Sonnet",
      "canonical_version": "4.6",
      "permitted": true | false | null,
      "invocable": true | false | null,
      "evidence": {
        "account_id": "…", "region": "us-west-2",
        "verified_at": "2026-09-18T11:00:00Z",
        "expires_at": "2026-09-25T11:00:00Z",
        "stale": false
      } | null,
      "retired": false,
      "price_context": {...} | null,
      "revision": 7
    }
  ],
  "catalogue_stale": false,
  "policy_revision": "..."
}
```

Six requirements on this shape:

- **`saved_model_id` and `effective_model_id` are both present.** §3.2.
- **`compatibility_class` and `harness_contract_revision` are per row**, because under U2 the persona's class is what makes any availability claim meaningful (§1.5). The class is stable and unversioned; the revision moves independently. Both are rendered (§3.2), and neither is ever inferred from the model ID.
- **`evidence` is a nested object or `null`, never a bare timestamp.** Adopted from PMM-03 §6.2 (§1.5). `evidence: null` with `invocable: null` is the "not yet certified" state; `evidence.stale: true` is the "unknown/stale" state. A flat `evidence_at` cannot distinguish them, which is why the earlier flat shape in this note is withdrawn.
- **`effective_is_candidate` marks a default that has never been proven** (§1.5). Needed because the effective model is still rendered as governing (§4.4) while being uncertified.
- **`permitted` and `invocable` are independently tri-state**, not folded into one boolean. §4.4. Folding them makes "your org forbids this" indistinguishable from "this cannot currently run", which are different remedies.
- **`revision` is per row.** §5.3.

**`selectable_models` is deliberately *not* part of this response.** An earlier revision of this note embedded a global selectable-model array here. U2 supersedes it: selectability is per (model, persona class) and cannot be expressed once for the whole page. The list moves to the persona-filtered catalogue endpoint in §5.5. A developer who finds the old embedded array in the PR history should treat it as withdrawn, not as an alternative.

### 5.2 Save and reset

```
PUT    /me/persona-models/{persona_key}   { "canonical_model_id": "...", "expected_revision": 7 }
DELETE /me/persona-models/{persona_key}   { "expected_revision": 7 }
```

Field name and semantics per PMM-02 §4.4: `expected_revision` is an integer compare-and-set fence, and **omitting it means "create only"** — if a row already exists the server refuses. The page therefore always sends the revision it rendered when editing an existing row, and omits it only when the row genuinely has no saved value. Sending nothing on an edit is not a lenient default; it is a refusal.

Both return **the same row shape as §5.1** so the page re-renders from the server's answer (§4.2 pattern 2). A refusal is `422` with `{reason, message}`, matching `self_routes.py:108-115` as cited by #5419. Reset removes the row so the default becomes effective — and the copy must say the row **falls back to the platform default, it does not stop working**, mirroring the reasoning in `bedrockRoutingSelf.ts`'s `clearMySelection` docstring ("my calls will now fail" is the natural and wrong reading).

### 5.3 Conflict (AC-08) — why it rides in the body

**Verified (F4):** `services/api.ts` builds `requestHeaders` from `Content-Type` plus `Authorization` (lines 56-66). `ApiClient.request` does accept a `headers` option and spreads it (`types/api.ts:25`, `api.ts:61`), but the convenience verbs the service modules actually use — `get`/`put`/`post`/`delete` — take only `(endpoint, body?, signal?)` and expose no way to pass one. So a service module calling `apiClient.put(...)` cannot send `If-Match` without either widening those signatures for every caller or dropping to raw `fetch` (the escape hatch `services/activity.ts:185` uses for a transcript read).

So optimistic concurrency uses **an `expected_revision` in the request body and a `409` in response**, carrying the current row so the page can offer an accurate reload:

```
409 { "reason": "revision_conflict", "message": "...", "current": { <row shape> } }
```

PMM-02 independently reaches the same conclusion and supplies a precedent to copy rather than a new mechanism: a hand-rolled integer compare-and-set inside the transaction, as in `orchestration/execution_store.py:788`, with 409 already established as the gateway's conflict code (`bedrock_routing/routes.py:554`, `:612`). Its §4.4 specifies the 409 carries the current revision and current `canonical_model_id`; **this note additionally requires the full row shape**, because the page renders effective model and source alongside the saved value and a partial body would leave it re-rendering from stale fields. That is a requirement on PMM-02, flagged here rather than assumed.

The page then reports that the value changed elsewhere and **offers a reload without overwriting** — it must not auto-retry with the new revision, which would be the silent-overwrite AC-08 forbids. The CLI (#5423) writing while the page is open is the motivating case and is exactly what AC-08 tests. Note PMM-05's note records that the CLI's shared transport drops the 409 body and it must re-read to report the current revision (`agent/issue-5423` §3.3); the page has no such limitation, which is why it can show a real diff and the CLI shows a freshly-read value.

### 5.4 Scope selector (AC-06, AC-07) — one server-reconciled list, namespace-agnostic

**This is the D-A contract (§1.1) stated as a shape, and it is CLOSED (U1 + U6).** PMM-02 owns the implementation; the path and field names below are fixed by the unified rulings and are no longer this note's to propose.

```
GET /me/persona-models/manageable-service-principals   → no parameters, at any position

{ "principals": [
    { "canonical_service_principal_id": "<opaque, server-minted>",
      "display_name": "svc-nightly-triage",
      "tenant_label": "acme",
      "source": "service-accounts" | "agent-registry" | "cognito-client" | ...,
      "manageable": true,
      "principal_kind": "service_account" }
  ] }
```

Administered reads and writes, tenant-checked server-side on every call, echoing the opaque ID verbatim:

```
GET    /service-principals/{canonical_service_principal_id}/persona-models
PUT    /service-principals/{canonical_service_principal_id}/persona-models/{persona_key}
DELETE /service-principals/{canonical_service_principal_id}/persona-models/{persona_key}
```

The administered routes carry the same body shapes and the same `expected_revision`/409 fence as their self counterparts (§5.2, §5.3), and the same row shape as §5.1. They differ from the self routes in exactly one way — the target in the path — which is the whole reason they live in a separate module (§6.1).

Two corrections this closure makes to earlier text in this note, stated rather than quietly dropped:

- The paragraph that said *"the endpoint path is PMM-02's to name and is deliberately left unspecified here"* is **withdrawn**. Deferring was right before U6; repeating it now would leave the developer with an open question the operator has closed.
- The field was previously called `canonical_principal_id`. It is **`canonical_service_principal_id`**. The longer name is the ruled one and the difference matters, because a shorter name invites reuse for human principals — which this endpoint does not return.

The five fields the review names are all present: canonical service principal ID, display name, tenant, source/provenance, and management entitlement (§1.1.1). `principal_kind` is a sixth, optional field the page would use only to label a future non-service-account principal type; it is not required by any AC and PMM-02 may omit it.

Six properties, each a security or correctness requirement rather than a convenience:

1. **The list comes entirely from the server.** The page never constructs, completes, parses, splits or accepts an arbitrary identifier, and never computes entitlement from a role claim. Client-side role reads are cosmetic in this codebase — `Navigation.tsx:157-159` says so explicitly of its own admin check ("This check is COSMETIC only … The real control is server-side").
2. **The browser never joins or infers aliases.** `canonical_service_principal_id` is opaque and is echoed back verbatim on an administered request. The page must not attempt to relate an entry to a role ARN, an `agent_name`, a `client_id` or a `service_accounts.id`, and must not merge two entries it believes are the same principal. Alias reconciliation is server-side by definition (§1.1); a browser-side join would be the page asserting an identity equivalence it cannot verify.
3. **`source` is a label, never a branch.** It exists so a person can distinguish two similarly named principals and so an operator can see which namespace a missing entry would have come from. The page must not switch endpoint, field mapping, or rendering logic on its value — that would re-couple the UI to the namespaces this contract exists to hide.
4. **`manageable` is server-computed.** The page renders entitlement; it never derives it. An entry with `manageable: false` should not be returned at all under AC-06's "only entitled accounts"; if the endpoint returns one anyway, the page must not offer it as a scope.
5. **An empty list means no selector renders at all**, and the page then requests only self rows. That is AC-07, and it must be asserted on the *requests made*, not on the rendered output (§6.4).
6. **Administered reads and writes use a distinct route** carrying the canonical principal ID as its target, kept in a **separate service module** from the self functions. This mirrors the platform's existing split — `self_routes.py` versus `routes.py` server-side, `bedrockRoutingSelf.ts` versus `bedrockRouting.ts` client-side — and §6.1 explains why the split *is* the security property. PMM-02's note independently reaches the same split (`agent/issue-5419` §5.2/§5.3).

**Note on who may administer, which PMM-04 inherits rather than decides.** PMM-02's §5.3 records that no ownership column and no service-account-management `Permission` exist today, so the only gate enforceable now is platform-admin plus a tenant check — meaning "platform administrator", not "the service account's owner". If the operator adopts that, `manageable` is true only for platform admins, the selector is empty for everyone else, and **AC-06 is satisfied but narrower than its wording implies**. The page's design is unaffected either way, because entitlement is server-supplied; but the acceptance evidence must say which gate was in force, or AC-06 will read as proving delegated ownership when it proved admin access.

### 5.5 Catalogue and explain (U2, U6) — the two PMM-03 endpoints this page consumes

Named by U6 and owned by PMM-03 (`agent/issue-5420` §6.0). The page consumes both and computes neither.

```
GET /me/persona-models/catalog?persona_key=architect
GET /me/persona-models/explain/{persona_key}
```

**The catalogue is persona-filtered at the server.** `persona_key` is required, not optional; there is no unfiltered form this page may call. Response rows carry the per-(model, class) shape:

```
{ "persona_key": "architect",
  "compatibility_class": "claude-agent-sdk",
  "harness_contract_revision": "2026-09-01",
  "models": [
    { "canonical_model_id": "us.anthropic.claude-sonnet-4-6",
      "family": "Sonnet", "version": "4.6",
      "selectable": false,
      "reason": "not_yet_certified",
      "candidate": true,
      "permitted": true, "invocable": null,
      "evidence": null,
      "retired": false,
      "price_context": {...} }
  ],
  "catalogue_stale": false }
```

`reason` must include **`not_yet_certified`** alongside `not_permitted`, `not_invocable`, `harness_incompatible` and `retired`. Without it the shipping state (§0.4) has no honest encoding and the page would have to render "not invocable" — a claim that a probe ran and failed, when none ran at all. This is a requirement on PMM-03, flagged here rather than assumed.

**No cross-class fallback appears anywhere in this response, by construction** (§1.5): a model of another class is absent from the list, not present-and-disabled. The page therefore cannot offer one even by mistake, and an empty `models` array is a legitimate answer the page must render as such (§0.4) rather than as a load failure (§7).

**The explain endpoint is fetched on demand, never on load.** It is what the "why is nothing selectable?" affordance opens (§4.3), and it returns PMM-03's ordered admission-gate trace. Two constraints: it must not be fetched for every row on render (that is N requests for information the rows already summarise), and it triggers **no probe** — §6.5 applies to it identically, which is worth stating because an endpoint named "explain" reads like something that would go and check.

---

## 6. Security and tenancy boundary

### 6.1 No target parameter on any self path

`bedrockRoutingSelf.ts`'s docstring states the rule and the reason: *"No function here takes a target, at any position. Not a person, not a scope, not a destination id. … A target parameter reachable from a self-service component is how authority leaks; the absence of one is why it cannot."*

Applied here as a structural property, not a convention:

- Self functions take **at most** a persona key and a revision. Never a principal identifier.
- Administered functions live in a **different module**, take the target as their first argument, and are never imported by the self-scope code path.
- The viewer is derived from the session server-side. There is nothing for the page to pass and therefore nothing it can pass wrongly.

### 6.2 Entitlement is never computed in the browser

The selector renders the server's `principals` list and nothing else. No client-side derivation from a role claim, no filtering of a broader list, no "admin therefore all accounts", and no alias reconciliation across namespaces (§5.4 properties 1–4). The server authorizes every administered read and write independently; the selector only shows what it was told.

This is also why the page must not substitute the existing admin service-account listing for `GET /me/persona-models/manageable-service-principals`. `GET /admin/organizations/{org_id}/service-accounts` exists and is gated on `Permission.ORG_READ` (`admin/routes.py:1223-1240`), so a developer looking for "a list of service accounts" will find it. It answers a different question — *which service accounts exist in this org, in one namespace* — and using it would put the browser in the business of deciding which of those the viewer may administer. That decision is the server's. The unguarded `auth/routes.py:346-370` listing is the more dangerous find and is ruled out for the same reason plus one more: it applies no permission check at all (§1.1.1).

### 6.3 Tenancy

`tenant_label` is display-only. The page sends no tenant identifier on any request; tenant scoping is the server's, and cross-tenant reads fail closed (#5417's authorization clause). An unfiltered scope list is the story's own AC-07 disclosure risk — which is precisely why §5.4 forbids the page from assembling that list itself.

### 6.4 The test that actually proves this

AC-07's intent is not "the page looks right". The story says so: a page that renders correctly against a fixture while requesting the viewer's own mappings *using a client-supplied identifier* would pass a naive render test and fail AC-07's intent.

**So the required assertion is on the request, not the render.** Tests must assert the mocked service function was called with the expected arguments — `toHaveBeenCalledWith`, a pattern already used across the suite (`__tests__/components/ApprovalPolicyToggle.test.tsx`, `InvocationChain.test.tsx`, `RegisterGitHubPat.test.tsx`) — and specifically that no principal identifier appears in any self-scope call.

### 6.5 No probe on load

The page consumes recorded evidence from PMM-03 and **never triggers the invocability probe**. #5420 is explicit that the probe must not run per page load, and the epic's cost clause binds this page to reads only. A page-load-triggered probe would mean every visit spends Bedrock tokens.

---

## 7. State machine

Every state below is required by an AC. The two that are easiest to get wrong are marked.

| State | Rendering | AC |
|---|---|---|
| Loading | Skeleton/spinner. No model values, no "default" claim | — |
| Loaded, no saved rows | Every row shows the platform default with source "platform default", **presented as a complete and correct answer, not as "unconfigured"** | AC-01 |
| **Load failed** ⚠ | Error naming what failed, **plus an explicit statement that this is not a claim about the effective model**, plus Retry. Silently showing "system default" **fails AC-04** | AC-04 |
| Catalogue stale | Rows remain visible, marked stale/amber; new selections not presented as verified | AC-09 |
| **Nothing certified yet** ⚠ | The state the page **ships in** (§0.4). Rows render with their effective model, marked *"not yet certified"*; the page-level explanation says no model has been certified and selections cannot be saved; Save is disabled. **Not an error, not amber-as-warning, and the effective-model line is still a complete and correct answer** | §0.4, AC-01, AC-09 |
| Persona not configurable | Row renders read-only with `not_configurable_reason`; no Save/Reset controls at all. Distinct from "certified nothing" — this persona is never configurable, not configurable-later | AC-02 |
| Save refused | Prior value **stays visible**; reason from `{reason, message}`; nothing changed | AC-05 |
| **Save conflicted** ⚠ | "This changed elsewhere", offer reload, **do not overwrite and do not auto-retry** | AC-08 |
| Partial multi-row save | Succeeded rows persist and show it; failed rows show their reasons; **the page must not imply all rows saved** — no single global success toast | AC-11 |
| Model retired | Flagged, not selectable; an existing mapping pointing at it stays visible so its owner can be told (alerting is #5426) | AC-03 |
| No manageable principals | **No selector renders**; only self rows requested. The page decides this from `principals: []` in the server's response (§5.4), never from a role claim | AC-07 |
| `agent_models` flag off or undetermined | The route is not reachable and the nav entry is absent. `FeatureGate` renders its spinner while the flag lookup is pending and `<Navigate to="/" replace />` once it resolves false (`FeatureGate.tsx:25-37`), so an errored lookup never exposes the page | §1.4 |

Row state is per row (`busyId`-style, §4.2 pattern 3) so AC-11's partial outcome is representable at all — a single page-level `isSaving` cannot express "row A saved, row B refused".

### 7.1 Accessibility (AC-10)

Every control reachable and operable by keyboard: the scope selector as a labelled radio group or listbox with roving focus; each model chooser a labelled listbox; Save/Reset real `<button>`s. Errors announced via `role="alert"`, as `BedrockAccountSelector` does for both its error branches. Disabled options carry their reason as text, not as a title attribute — a tooltip is not reachable by keyboard and would fail AC-03's explanation requirement for a keyboard user.

Narrow width per §4.5. **Component tests cannot establish real accessibility or real rendering**; both remain PMM-09's, and AC-10's own wording says so.

---

## 8. Dependencies, parallelism, deployment

### 8.1 Dependency graph

```
#5418 PMM-01 (vocabulary, 6 locked decisions) ──┐
                                                ├─→ #5419 PMM-02 (API) ──┐
                                                └─→ #5420 PMM-03 (catalogue) ─┤
                                                                             ├─→ #5422 PMM-04 (this page)
                                                                             └─→ #5423 PMM-05 (CLI)
```

The PMM-01 design record was being authored concurrently on #5418 by the codex supervisor persona; **no `docs/design-notes/5417-*.md` or `5418-*.md` file exists on the default branch at `c4809bb1`** (re-verified at this head — `git log --all` shows no commit touching those paths). Its six decisions are nonetheless locked in operator comments on #5418, and U1–U6 in operator comments on #5417; both are treated as binding here, and U1–U6 win where they overlap.

### 8.2 What can run in parallel

**Can start now — every decision is closed (§0.2):** the page shell, row rendering, every state in §7 including the two new ones, the four-state availability column (§4.4), the scope selector against §5.4's now-named endpoint, keyboard and narrow-width work, the `agent_models` flag plumbing across the thirteen touch points in §1.4.1, and the full vitest suite against fixtures. The §5 contracts are sufficient to build and test against.

**Nothing is blocked on an open decision any more.** What remains is ordinary dependency on sibling *implementations*, not on unsettled design:

| Needs | Before what becomes real |
|---|---|
| PMM-02 merged | Save/reset/conflict against a real server; the real `manageable-service-principals` list (§5.4); administered routes |
| PMM-03 merged | The persona-filtered catalogue and explain endpoints (§5.5); real evidence; real price context |
| PMM-09 | Any model becoming selectable at all (§0.4), and flag enablement |

This is why the flag default of `false` (§1.4) is load-bearing rather than ceremonial, and why the ordering is safe: the page can merge complete, against fixtures, and stay invisible until each of those lands. The honest reading is that **PMM-04 is mergeable well before it is useful**, and that is by design rather than a gap.

**The seam that makes this parallel:** a typed service module (`services/personaModels.ts`) whose functions are the only thing the page calls. Fixtures implement the §5 shapes; swapping in real endpoints changes no component. If PMM-02/03 diverge from §5, the divergence lands in that one module — which is also why §5 must be agreed *before* the page is built, not discovered after.

**Honest limit:** AC-02..AC-09 are provable against fixtures, and that is what the story asks for ("vitest"). They establish the page's behaviour given a contract; they do **not** establish that the server behaves that way. Live cross-principal acceptance is PMM-09's, by the story's own completion boundary.

### 8.3 Deployment, migration, rollback

- **No data migration.** No schema change, no backfill. But the ruled design is **not** frontend-only: it is a cross-module change spanning the **thirteen touch points across three modules** traced in §1.4.1 — including the second, easily-missed manifest renderer in `platform/scripts/deploy-all.sh:1088-1095` (A6), without which the flag is permanently stuck off on the self-managed deploy path with no error anywhere.
- **Ordering.** The gateway image carrying the new `agent_models` key in `GET /features` must be deployed **before or with** the SPA that reads it. If the SPA ships first, `useFeatures` gets a response with no `agent_models` key, `data.agent_models` is `undefined`, and the `=== true` check in `FeatureGate` resolves false — so the page is hidden, which is the safe outcome, not a broken one. The reverse order is also safe: the key answers `false` and nothing renders. **Both orders fail closed**, which is the property the strict reader buys.
- **Enablement is a deliberate operator action in PMM-09, not a deploy side-effect.** Set the SSM parameter that **both** renderers read (§1.4.1 A5 and A6) and redeploy. Per `k8s/deployment.yaml:166-167` — a warning the manifest states twice — an out-of-band `kubectl set env` is reverted by the next deploy and must not be used as the enablement mechanism.
- **Enablement is necessary but not sufficient.** Turning the flag on while probing is off yields a reachable page on which nothing can be saved (§0.4). PMM-09 must sequence certification *before* flag enablement, or the first thing users see is a page that refuses every save. This ordering constraint is PMM-09's to honour and is recorded here because it is invisible from PMM-09's own story text.
- **Build and publish:** `gateway-deploy.yml` on merge; `modules/gateway/scripts/deploy-frontend.sh` is the manual equivalent. Per `CLAUDE.md`, the build must use `VITE_API_URL="/api"` — the wrong prefix makes every SPA call hit the S3 HTML fallback with HTTP 200 and crashes the dashboard. A CloudFront invalidation is required for the new asset to appear.
- **Rollback: flip the `agent_models` flag off.** The page and its nav entry vanish with no code revert and no CloudFront invalidation, which is the whole reason the operator ruled the flag in (§1.4.2 — this supersedes the earlier "revert the PR" answer in this note). **Flipping the flag off leaves saved mappings intact and effective**, because resolution does not depend on the UI — that claim is checkable and true precisely because #5425 (PMM-07), not this page, is what connects the store to dispatch. Note the corollary: the flag is a **rollout control, not a security boundary**. Hiding the page does not stop a caller from writing mappings via the PMM-02 API or the PMM-05 CLI; authorization is the server's, on every request (§6.2).
- **No new IAM, no new identity, no new data store.** The page adds no server permission.

### 8.4 Checks that apply

Because the ruled design touches three modules, all three matter — unconditional now, not contingent on D-D. Mapped to the §1.4.1 groups so a developer can see which check catches which omission:

| Check | Catches |
|---|---|
| `cd modules/gateway && ruff check src/ tests/ && ruff format --check src/ tests/ && python3 -m pytest tests/ -q` | A1, and **B1** — `test_routes.py:64`'s whole-payload `assert data == {...}` fails on an unlisted new key |
| `cd modules/gateway/frontend && npx vitest run` | B3, B4, and **the `journeys.test.tsx` guard** on the new `/settings/agent-models` entry (§1.2, §3.1) |
| `cd modules/gateway/frontend && npx tsc --noEmit` | **B2** — `features.test.ts:68-80`'s `Record<keyof FeatureFlags, true>` has no index signature, so a missing key is a type error |
| `npm run build` | A2, A3 — **and nothing in group B** (see below) |
| superplane acceptance (`superplane_acceptance/features.py`) | B5 — **not** broken by a new flag (`:196` is a `<=` subset check), but the `FIXTURE_SHA256` pin at `:33` breaks if the fixture is regenerated |

**`npm run build` alone does not catch the B-group failures**, because `frontend/tsconfig.json:23-29` excludes `src/**/__tests__/**` from the `include` — so the very fixtures designed to catch flag drift are invisible to the production type-check. This is not hypothetical: `FeatureGate.test.tsx:22-33`'s own comment records that `budget_spend` drifted for exactly this reason. Run `vitest` and `tsc --noEmit`; a green build is not evidence.

**No check at all catches A6.** There is no test, lint or type-check over `deploy-all.sh`'s placeholder rendering. It is caught by review or by a deploy that silently does nothing — which is why §1.4.1 lists it first among the easily-missed touch points rather than last.

**For this note itself, no module suite applies.** This PR changes one Markdown file under `docs/design-notes/` and no module source; `git diff --stat` is the applicable check (§11). The line-number citations above were verified by reading the files at `c4809bb1`; `pytest` and `vitest` were **not executed** in the authoring environment (no `pytest` module present), and this note makes no claim that they were.

---

## 9. Acceptance coverage

How each AC is satisfied, and what it does **not** establish.

| AC | Design provision | Limit |
|---|---|---|
| AC-01 ordinary user, no admin gate | §1.3, §3.1. No *permission* gate on the route; no "managed by a platform admin" copy — that copy belongs to `ModelAccess.tsx:6` and must not be imitated | The `agent_models` flag gate (§1.4) is a rollout control, not an admin gate: while off, the page is hidden from **everyone including platform admins**, so it does not reintroduce the asymmetry AC-01 forbids |
| AC-02 unknown persona renders | §4.3. Rows come from the catalogue; no local persona list | — |
| AC-03 unavailable / disallowed not selectable, each explained | §4.3 (row-listbox, not `<select>` — F3a; persona-filtered catalogue + explain endpoint, §5.5), §4.4 four states | Requires PMM-03 to carry `not_yet_certified` as a `reason` value (§5.5); without it the shipping state has no honest encoding |
| AC-04 load failure never claims a default | §7 ⚠ row; §4.2 pattern 5 | **The highest-value test on the page** |
| AC-05 save re-renders from server; reset; refusal keeps prior value | §4.2 patterns 1-2-4, §5.2 | **Cannot be demonstrated live until PMM-09 certifies a model** (§0.4): until then every save is refused, so the refusal path is exercisable and the success path is not |
| AC-06 principal selector lists only manageable principals; name+tenant shown before commit | §5.4, §6.2 — renders the server's `principals` list from `GET /me/persona-models/manageable-service-principals` verbatim, including `display_name`, `tenant_label` and `source` | Buildable against fixtures now; real behaviour needs PMM-02 merged. The page cannot prove the server reconciled identities correctly — that is PMM-02's and PMM-09's. **The evidence must also state which authority was in force** (platform-admin-plus-tenant-check, not owner — §5.4), or this AC reads as proving delegated ownership when it proved admin access |
| AC-07 no selector when none manageable; only own rows requested | §5.4(2), §6.4 | **Assert the request, not the render** — the empty-selector case must be driven by `principals: []`, not by a role claim |
| AC-08 conflict reported, no overwrite | §5.3 (body-carried revision + 409 — F4) | No auto-retry |
| AC-09 stale catalogue: rows visible, not verified | §4.4 (states 3 and 4), §7 | Two distinct states satisfy this — decayed evidence (`evidence.stale`) and never-asserted evidence (`evidence: null`). **The evidence must say which was exercised**; a fixture with no `evidence{}` at all does not prove the stale path |
| AC-10 keyboard + narrow width | §7.1, §4.5 (**deviation from `Table` — F3b**) | Component tests do **not** establish real accessibility or rendering; PMM-09's |
| AC-11 partial bulk save | §7, per-row state (§4.2 pattern 3) | No global success toast |

---

## 10. Summary of what a developer must be handed

**All four decisions are closed** (§0.2). Nothing on this list is a question; every item is an instruction.

1. **D-B, D-C, D-D** (§1.2, §1.3, §1.4): **Agent Models** at `/settings/agent-models`, one current-UI page plus a `journeys.ts` entry pointing at that same route, gated by a strict backend-served `agent_models` flag defaulting `false` and enabled only in PMM-09.
2. **D-A, closed by U1 + U6** (§1.1, §5.4): consume `GET /me/persona-models/manageable-service-principals`, keyed on **`canonical_service_principal_id`**, with tenant-checked administered routes `GET|PUT|DELETE /service-principals/{canonical_service_principal_id}/persona-models[/{persona_key}]`. Identity reconciliation across the three live machine-subject forms (S1 Postgres `service_accounts.id`, S2 DynamoDB `agent_name`, S3 Cognito `client_id` — PMM-02's numbering) is **server-side**; the browser never joins or infers aliases. Two findable-but-wrong implementations are named in §1.1.1 — `admin/routes.py:1205-1240` and the unguarded `auth/routes.py:346-370` — and the earlier suggestion that PMM-04 target `service_accounts.id` directly is **withdrawn**.
3. **D-E, closed by U2** (§1.5, §4.4, §5.5): availability is a property of a **(model, compatibility class)** pair at a given `harness_contract_revision`, never of a model alone. No cross-class fallback. Four availability states, not three — the fourth being **candidate / not-yet-certified**, which is what the page ships in. Consume the **persona-filtered** `GET /me/persona-models/catalog?persona_key=…` and the on-demand `GET /me/persona-models/explain/{persona_key}`; the page filters nothing locally. The formerly-embedded global `selectable_models` array and the flat `evidence_at` field are both **withdrawn** in favour of PMM-03's nested `evidence{}`.
4. **The consequence to state out loud before writing code** (§0.4): under U3 probing is off, so on day one **nothing is selectable and every save is refused**. The page must render that as a correct, explained state — not an error, not an empty list, not a spinner.
5. The **§5 contracts**, including the body-carried `expected_revision` compare-and-set and 409 shape (§5.2, §5.3), which follows PMM-02's owned design and is constrained by F4 (the shared client's convenience verbs expose no headers, so `If-Match` is unavailable).
6. The **thirteen flag touch points across three modules** (§1.4.1), grouped by consequence. The five-file shorthand earlier in this note's history is **withdrawn as an inventory**. Two in particular: `deploy-all.sh:1088-1095` (A6), a second independent renderer that no check covers, and `gateway-deploy.yml`'s `get_ssm` + `sed` (A5) — without either, the literal `__FEATURE_AGENT_MODELS_ENABLED__` reaches the pod on that path and the flag can never be turned on.
7. The **check matrix** (§8.4) and the reason `npm run build` is not sufficient: `tsconfig.json:23-29` excludes `__tests__`, which is how `budget_spend` drifted before.
8. The **two stated deviations** from the story's reuse table: not the shared `Table` (§4.5), not the shared `Select` (§4.3) — both with reasons recorded here.
9. The **correction** to the cited path `pages/settings/SettingsCredentials.tsx` (§4.1).

There is **no unresolved cross-story conflict** as of the sibling heads cited in §11.2.

Filing or merging this note authorizes no implementation, no deployment and no dispatch. This note is **proposed**; U1–U6 are binding on it.

---

## 11. Revision record

### 11.1 What this revision changed and why

#### Revision 3 — 2026-09-18, in response to the focused `CHANGES_REQUESTED` review at `5fc0fb4f`

The prior revision was approved at 16:44 and returned to `CHANGES_REQUESTED` at 18:09 because U1/U2/U6 landed in between and supersede that approval. **Citations re-verified at `c4809bb1`**; `git diff ae598410 c4809bb1` over every path this note cites is empty, so the previous revision's line numbers remain valid at the new head rather than being re-dated on faith.

Four required changes, plus the review's standing instruction to **delete superseded alternatives rather than keep them beside the rulings**:

| # | Change | Cause |
|---|---|---|
| 1 | **D-A closed** with exact names: `GET /me/persona-models/manageable-service-principals`, the field `canonical_service_principal_id` (was `canonical_principal_id`), and tenant-checked `GET\|PUT\|DELETE /service-principals/{canonical_id}/persona-models[/{persona_key}]` (§0.2, §1.1, §5.4, §6.2, §9, §10) | U1 + U6. §5.4's paragraph deferring the path to PMM-02 was correct before U6 and is now **withdrawn in the text**, not merely overridden. Also realigned S1/S2/S3 to PMM-02's numbering, which this note had inverted |
| 2 | **New D-E (§1.5): availability is per (model, persona compatibility class)** at a `harness_contract_revision`, with candidate/proven state and no cross-class fallback. Propagated to §3.1, §3.2, §4.1, §4.3, §4.4, §5.1, §5.5, §7, §9 | U2. Three consequences the note previously got wrong: the tri-state rule is **extended to four states** (the fourth is "not yet certified"); the global `selectable_models` array is **deleted** in favour of the persona-filtered catalogue endpoint; the flat `evidence_at` is **replaced** by PMM-03 §6.2's nested `evidence{}`, which the flat shape could not distinguish stale-from-never-asserted within |
| 3 | **Flag inventory corrected from five files to thirteen traced touch points** across three modules, grouped by consequence, with a "what this does not require" section (§0.3 F3, §1.4.1, §8.3, §8.4, §10) | The approving review's non-blocking correction, which was right. Traced the three existing flags end to end instead of reasoning about what a flag "should" need. That surfaced **`deploy-all.sh:1088-1095`** — a second, independent manifest renderer on the self-managed path that no check covers — and the four exhaustive test fixtures. The five-file shorthand is withdrawn **as an inventory** and labelled as such |
| 4 | **New §0.4: nothing is selectable on day one.** Under U3 probing is off, so every model fails admission gate 5 and PMM-02's save path refuses every save (§0.4, §3.1, §4.4, §7, §8.3, §9 AC-05) | Derived from U2 + U3 while validating against PMM-03's §4.7. Not raised in review. The page can show the effective model but cannot accept a change, and PMM-09 must certify **before** enabling the flag or the first thing users meet is a page that refuses everything |

Two smaller corrections: §8.4 now says plainly that `pytest`/`vitest` were **not executed** here and that `npm run build` cannot catch the B-group failures (`tsconfig.json:23-29` excludes `__tests__` — the documented cause of the earlier `budget_spend` drift); and §1.6 now qualifies "Claude-only" as a statement about today rather than a design constraint, for #5433's Codex/GPT classes.

#### Revision 2 — 2026-09-18, in response to the first blocking review and the synthesis-rulings comment

Citations verified at `ae598410`. Four substantive corrections, two of them to errors this note previously made:

| # | Change | Cause |
|---|---|---|
| 1 | Status changed from "architecture-approved" to **proposed, pending the #5417 synthesis** (header, §0.2, §10) | The review; and the #5417 synthesis gate, which reserves publication of the unified design to the operator. A story-local note cannot self-certify past that gate |
| 2 | **D-A's two-store framing withdrawn**, replaced by the three-identifier-space finding and a namespace-agnostic server-reconciled contract (§0.2, §0.3 F1, §1.1, §1.1.1, §5.4, §6.2, §9 AC-06/AC-07) | The review. **This note was wrong**: it posed a choice between two service-account stores and recommended one. The reviewer's claim was checked against source and is correct — S1 `agent_name` (`auth/agent_registry.py:245-256`), S2 `service_accounts.id` (`auth/tenant_resolver.py:283-304`), S3 `client_id` (`auth/auth_service.py:308-312`). The recommendation is withdrawn rather than quietly edited |
| 3 | **D-D reversed from un-gated to gated** by a strict `agent_models` flag defaulting false, enabled in PMM-09 (§0.2, §0.3 F2, §1.4, §7, §8.3, §8.4, §9 AC-01) | The operator's ruling. This note recommended *not* gating. The cost analysis it gave was accurate and is retained; the ruling accepts that cost for a rollback lever that needs no revert, and §1.4 now explains why the ruling is sound |
| 4 | **A fifth required file added** to the flag plumbing: `gateway-deploy.yml`'s `get_ssm` read and `sed` substitution (§0.3 F2, §1.4.1) | Found while re-verifying, not raised in review. Without it the literal `__FEATURE_AGENT_MODELS_ENABLED__` reaches the pod, `_is_enabled_strict` reads it as false, and the flag can never be enabled — a failure that is safe but silent |

Also reconciled against PMM-02's owned shapes at `agent/issue-5419`: the write field is `expected_revision`, an absent `expected_revision` means create-only, and the self prefix is `/me/persona-models` (§5.1–§5.3, §5 preamble). One new requirement is placed on PMM-02 by this note and is flagged as such rather than assumed: the 409 body should carry the current row shape so the page can offer a reload without a second round trip (§5.3).

### 11.2 Cross-story validation against current sibling heads

Re-checked at the heads below rather than at the heads the previous revision saw. **No unresolved conflict remains.**

| Sibling | Head checked | Outcome |
|---|---|---|
| #5419 PMM-02 | `agent/issue-5419` @ `e2c7d099` | **Agreed.** §3.2.1 supplies the authoritative S1/S2/S3 numbering this note now follows; §5.3.1 confirms the manageable-principals endpoint, adopts this note's five-field shape, and independently records both the no-owner-column limit and the unguarded `auth/routes.py:346-370` |
| #5420 PMM-03 | `agent/issue-5420` @ `25ece717` | **Agreed, with one divergence resolved in PMM-03's favour** (§1.5): its nested `evidence{account_id, region, verified_at, expires_at, stale}` replaces this note's flat `evidence_at`. Its §2.4 class registry, §4.7 nothing-selectable state and §6.0 endpoint paths are adopted |
| #5418 PMM-01 | operator comments; no design-note file exists | Six locked decisions treated as binding (§8.1) |
| #5417 EPIC | operator comments U1–U6 | Binding, and superseding where they overlap story-local recommendations (header) |

**The previous revision's one unresolved conflict is retired as resolved upstream.** Revision 2 disputed PMM-02's claim that the Postgres `service_accounts` table has "no production reader or writer", citing live CRUD on two unconditionally registered routers and a live read on the authentication path. **PMM-02's §3.2 at `e2c7d099` now states the corrected position itself**, including the narrower true statement — that `POST /auth/exchange` returns HTTP 410 unless `BG_ENABLE_LEGACY_AUTH_EXCHANGE=true` (`auth/routes.py:54`, `:119-133`), a var present in no manifest, Terraform file or workflow. There is nothing left for the epic synthesis to settle here, so the entry is closed rather than carried forward. The underlying evidence remains in §1.1 because the "findable but wrong" routes it names are still findable.

Two requirements this note places on siblings, flagged rather than assumed — neither is a conflict, both are asks:

1. **PMM-02:** the 409 body should carry the full current row shape, not just revision and model ID, so the page can offer an accurate reload without a second round trip (§5.3).
2. **PMM-03:** the catalogue's `reason` vocabulary needs **`not_yet_certified`** (§5.5). Without it the day-one state must be rendered as `not_invocable` — asserting that a probe ran and failed when none ran.

### 11.3 Checks run for this revision

This PR changes **one Markdown file** under `docs/design-notes/` and no module source, so no module suite applies to *this* diff; `git diff --stat` confirming single-file scope is the applicable check. What was actually done: every line-number citation was re-read at `c4809bb1`; `git diff ae598410 c4809bb1` was run over all cited paths and is empty; the two sibling notes were read at the heads tabulated in §11.2. `pytest` is not installed in the authoring environment and **no test suite was executed** — the B-group claims in §8.4 rest on reading those fixtures, not on running them, and are stated that way. The three-module check matrix in §8.4 governs the **implementation** PR, which this is not.
