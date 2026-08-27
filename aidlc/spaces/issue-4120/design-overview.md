# Design overview — unified component-change map

**Intent:** [#4120](https://github.com/aws-e/adp/issues/4120) · **EPIC:**
[#4191](https://github.com/aws-e/adp/issues/4191) · **Commissioned by ruling
D-R21** · **Created by** [#4192](https://github.com/aws-e/adp/issues/4192) (S1,
wave 1).

**What this is.** One table answering "what in this platform changes, and which
stories touch it." Before this file existed the answer was scattered across
`inception/requirements.md`, `inception/delivery-plan.md`,
`inception/delivery-plan-amendment.md`, `construction/loop-proposal/wave-map.md`
and fourteen issue bodies — so nobody could see the blast radius in one place.

**This file is maintained, not one-shot.** Every wave's evaluation asserts it was
updated for that wave. See [§Maintenance protocol](#maintenance-protocol).

---

## Story → issue → wave

Story number is the stable identifier; issue numbers are non-monotonic by design
(see `wave-map.md` §1).

| Story | Issue | Wave | Title |
|---|---|---|---|
| S1 | [#4192](https://github.com/aws-e/adp/issues/4192) | 1 | Design contract: commit chosen mockups + extract binding UI contract |
| S2 | [#4193](https://github.com/aws-e/adp/issues/4193) | 1 | Engine state vocabulary + transition table (rules in code, no I/O) |
| S3 | [#4196](https://github.com/aws-e/adp/issues/4196) | 1 | Orchestration graph store: migration + internal-plane CI guard |
| S4 | [#4199](https://github.com/aws-e/adp/issues/4199) | 2 | Loop proposal as schema-as-code: model, CLI validator, in-transaction compile |
| S5 | [#4200](https://github.com/aws-e/adp/issues/4200) | 2 | Plan amendment / re-plan as a first-class attributed engine operation |
| S6 | [#4203](https://github.com/aws-e/adp/issues/4203) | 3 | Engine tick: EventBridge → VPC Lambda → RDS, concurrency-safe |
| S8 | [#4207](https://github.com/aws-e/adp/issues/4207) | 3 | Cost by graph address: migration + three-valued aggregation |
| S7 | [#4204](https://github.com/aws-e/adp/issues/4204) | 4 | Engine dispatch with gate-approver genesis + deviation detection |
| S11 | [#4211](https://github.com/aws-e/adp/issues/4211) | 4 | Stall/halt detection, bounded defect cycles, notification |
| S9 | [#4208](https://github.com/aws-e/adp/issues/4208) | 5 | Intent-intake chat: draft panel, pinned persona, `update_draft` |
| S10 | [#4209](https://github.com/aws-e/adp/issues/4209) | 5 | GitHub input adapter + fail-closed feature flag |
| S12 | [#4212](https://github.com/aws-e/adp/issues/4212) | 6 | Graph view: full journey, pending look-ahead, parallel branches, live |
| S13 | [#4213](https://github.com/aws-e/adp/issues/4213) | 6 | Gate approval + resume controls: permission, attribution, decisions |
| S14 | [#4214](https://github.com/aws-e/adp/issues/4214) | 7 | Exception-diagnoser (propose-never-dispose) — optional, cut-safe |

**Critical path:** S2/S3 → S4 → S6 → S7 → S12/S13 — five waves deep. **S1's
human review hard-gates wave 6** (P-R3: a human sits on the critical path).
**Deploy target:** `adp-dev-embark1`, by reference only per D-R11 — literals live
solely in [`deploy-target.md`](deploy-target.md).

---

## The component-change map

| # | Component area | What changes | Stories | Waves |
|---|---|---|---|---|
| 1 | **Gateway backend** (Python, `modules/gateway/src/`) | **One new package, `src/orchestration/`** (does not exist today). New modules: `state.py` (9-state enum + `LEGAL_TRANSITIONS` constant + single guarded `transition()`; `TERMINAL_STATES` **derived**, never hand-listed) · `models.py` (SQLAlchemy over the existing `Base` + `TenantMixin`) · `proposal.py` (shared Pydantic v2 `LoopProposal`; `ConfigDict`, not the deprecated `class Config`) · `compile.py` (the **only** path that creates nodes; validate + insert in one transaction) · `amend.py` · `routes.py` · `tick.py` + `tick_handler.py` · `cost.py` · `dispatch.py` · `deviation.py` · `stall.py` · `notify.py` · `adapters/github_comments.py` · `controls.py` · `diagnose.py`. New endpoints all under `/api/orchestration/*` — **none on the internal plane**. **Modified:** `src/features/routes.py` (new flag via `_is_enabled_strict`, **not** fail-open `_is_enabled`); `src/activity/cost_service.py` (grouped query supersedes the `run_ids IN (…)` shape, old signature kept until callers migrate). New permission added at **three** sites — `Permission` enum, `ROLE_PERMISSIONS`, **and `_ORG_SCOPED_PERMISSIONS`** (omission from the frozenset is an authz bypass, not a lint miss — AC-17, P-R7). Cross-org ids return **404, never 403**. | S2, S3, S4, S5, S6, S7, S8, S10, S11, S13, S14 | 1–7 |
| 2 | **Gateway DB / migrations** (`modules/gateway/alembic/versions/`) | **Two new migrations.** (a) **Orchestration graph** — five tables: `orchestration_flows`, `orchestration_nodes`, `orchestration_edges`, `orchestration_accepted_plans` (immutable, versioned), `orchestration_decisions` (append-only: node id, decision kind, actor identity, **actor role copied at decision time**, **actor kind `human`/`service`**, spec revision, timestamp — no update/delete path at route or service layer). (b) **Cost by graph address** — nullable column on `usage_logs` + **partial index** `WHERE col IS NOT NULL`, explicit **no-backfill**. Shape discipline follows `018_agent_run_cost_traceability.py` (raw `op.execute`, nullable, no backfill), **not** `025_org_created_via` (`NOT NULL` + `server_default`, where the default *is* the backfill). Graph address `flow/epic/wave/node`. **Forbidden write targets for attribution:** `audit_logs` (has **no alembic DDL at all** — exists only under `BG_DB_AUTO_CREATE=true`, so it is `UndefinedTable` in any migrated env) and `security_audit_logs` (closed magic-link `event_type` vocabulary). ⚠️ **See [Live issues](#live-issues-and-stale-premises) — the numbers `026`/`027` named in the story bodies are now taken on `main`.** | S3, S8 | 1, 3 |
| 3 | **Gateway infra** (Terraform, `modules/gateway/infra/`) | **One new module,** `modules/orchestration-tick/{main.tf, variables.tf, iam.tf, outputs.tf}`: 1 in-VPC Lambda, 1 EventBridge schedule rule, 1 Lambda invoke permission, 1 IAM role (`rds-db:connect` scoped to a single dbuser), 1 security group (egress → RDS), 1 CloudWatch log group. Precedent to copy: `modules/budget-lambda/main.tf`, the **`pricing_refresh`** (scheduled) variant — **not** its `usage_tracker` sibling (S3-event-driven). Budget-lambda itself is **not edited**; the new module is a sibling. Why Lambda: gateway runs 8 stateless replicas with no leader election; the agent pod has a 6 h hard kill with no resume; the sync path caps at 15 min; there is **zero** `aws_sfn_state_machine` repo-wide. Kill switch: `aws events disable-rule` stops the tick in seconds with no deploy. **Also modified:** `modules/gateway/k8s/deployment.yaml` — the new feature-flag env var. S11 adds Terraform **only if** the notification channel needs a resource. | S6, S10, (S11 conditional) | 3, 5, (4) |
| 4 | **Gateway frontend** (`modules/gateway/frontend/`) | **New graph view** implementing mockup **D "Portfolio"** against S1's contract (`design-contract.md`): rollup, origin strip, engine visibility, EPIC×wave agreed-plan grid, story tables, run log drawer; renders `pending`/look-ahead nodes, parallel branches, three-valued cost with scope label, stalls distinguishable from halts, deep links throughout. **New controls component** — approve / reject / resume, **hidden** (not disabled) without the permission. **Modified:** `src/types/index.ts` (mirror `Permission` enum) **and** `src/services/auth.ts` (`ROLE_PERMISSIONS` map) — X4: both in the same PR, since `auth.ts` only *imports* `Permission`; `src/services/features.ts` (`FeatureFlags` interface **and** the `ALL_FEATURES_ENABLED` default). **Cost formatter consolidation:** five duplicated formatters collapse to one and the `null → $0.00` coercion is **removed** (AC-22); `SpendTodayTile`'s no-data pattern is the reuse target. **Chat draft panel** (`DraftPanel`) driven by AG-UI `STATE_DELTA`. Route guard reuses `FeatureGate.tsx`. **Polling contract (binding):** react-query `refetchInterval` set **explicitly** (house pattern `AgentActivity.tsx` 30 s); **MUST NOT** use `usePollingStatus` (asset-coupled, raw `setInterval`, interval computed once); **MUST NOT** inherit the global `staleTime: 5 min` — that omission is why `InvocationChain` is static for 5 minutes inside a 30 s-polling parent. **No graph/DAG library exists** → S1 recommends **BUILD**, zero new deps (`design-contract.md` §9). Test harness: vitest, tests **must** live under `src/`; zero existing `src/__tests__/` tests use MSW and `setup.ts` sets `onUnhandledRequest: 'error'`, so S12/S13 must declare `vi.mock` vs MSW; any fixture derives from the **backend** schema, never the frontend type. | S1 (contract), S8, S9, S10, S12, S13 | 1, 3, 5, 6 |
| 5 | **The dispatch path** | The engine **reuses `POST /agent/trigger`** (the SigV4 seam behind the pre-installed `adp-trigger` CLI). It is **not** replaced; writing to the submit queue directly was explicitly **rejected** (loses server-side lineage and guard checks). **The genesis problem:** the trigger handler requires `correlation_id` + `parent_invocation_id` and returns **422 `unknown_chain`** rather than ever minting a new root — but the engine is a scheduled Lambda, not a child of a run, so it has no chain. Compounding it, correlation pointers default to a **7-day** TTL that every continuation refreshes, so the engine's worst case (a human parked at a review) is exactly the case that breaks resumption. **Ruling D-R12 — "gate-approver genesis":** engine dispatch is **human-rooted via the SSO-attributed gate approver**. The engine supplies a `decision_id`; **the handler resolves the approver server-side and never accepts a client-supplied `root_human_id` — that distinction is the whole security design.** A decision row whose `actor_kind` is `service` **cannot root a chain**. The mode is **additive**: existing verification stays fail-closed. **Engine-side authority, not envelope-side:** IAM cannot express per-persona authority (one shared role, one service account — every persona's pod presents an identical ARN), so authority is decided **inside** the engine against a server-resolved identity, never from the envelope `persona`, `AGENT_TYPE`, or a caller ARN. **Compensating control:** `deviation.py` reconciles observed runs against dispatched nodes — **detection, not prevention** (accepted residual RES-1); off-plan runs render flagged on the graph. | S7, (S13 supplies the decision rows) | 4, (6) |
| 6 | **Webhook ingress** (`modules/agent-factory/webhook-ingress/`) | **Changes — and it is the security-critical edit of the EPIC.** The trigger handler (`lambda/github/agent_trigger.py`, chain-resolution path) accepts an **engine genesis** whose root human is the gate approver in `orchestration_decisions`. Framed in the plan as *"a new genesis path in a **deliberately genesis-free** lambda — security-sensitive, needs its own review."* `lambda/common/marker_verify.py` and `handler.py` are **extended, never bypassed**; **existing marker-verification tests must stay green UNCHANGED** — the wave-4 eval asserts `git diff --stat` over `*marker_verify*`/`*test_marker*` is **zero**; if any needed editing, the genesis path weakened them and the PR is reworked (AC-30). **Deploy consequence:** S7 needs **two deploys beyond `gateway-deploy.yml`** — the agent-runtime image rebuild **and** `webhook-ingress-deploy.yml`; *"without the latter, no dispatch."* **Not changed:** the dispatch/execution split (the Lambda never reads `issue.body`; the pod re-fetches) and the `webhook_events` DDB status vocabulary — the engine's 9 states are a **separate** vocabulary that maps to the DDB one explicitly, never replacing it. | S7 | 4 |
| 7 | **Chat module** (`modules/agent-factory/gateway/lambdas/ingest/`, `modules/agent-factory/agent/src/complex-task-chat/`, gateway frontend chat) | Intent-intake chat implementing mockup **E "Inception"**. Four pieces, **two net-new**: (a) **Persona pinning — NET-NEW** (correction X8): persona is chosen **server-side by an LLM classifier on every message** today, the client payload is only `{action, text, session_id, attachments?}`, and the classifier prompt has a hard rule *against* carrying persona across topics. Tractable because the worker already honours `agent_type`, so **only ingest changes**, and `persona-loader.ts` already carries `PERSONA_NAME_PATTERN` + `ALLOWED_PERSONAS` written in anticipation of an untrusted value — the change is *plumb a validated field*, not *invent trust* (adversarial test includes a path-traversal-shaped value). (b) **AG-UI `STATE_DELTA` draft panel** — payload is RFC-6902 JSON Patch; **binding constraint:** patch-path parsing is **top-level-only** today, so either flatten the draft to top-level keys or extend the parser — a nested path must render or **fail loudly**, never be silently dropped. (c) **`update_draft` MCP tool** via the `publish_artifact` **closure-factory** pattern (handler closes over `sessionId`/`taskId`/`identity`, never reads them from LLM input); **gotcha:** the input-sanitizer map is built only from `vaultTools` — widen that loop or the new tool's args go unsanitized. (d) **Transcript hand-off — NET-NEW and the weakest premise:** no full-transcript fetch API exists in either store (`getFullTranscript()` does not exist; `replaceRangeWithSummary` **destructively** evicts turns; the gateway's only read is `load_recent_history(limit=10)`), so S9 ships an explicit ordered-retrieval path. **Cold start 10–18 s** is real and named as a UX risk; the existing throwaway "Thinking…" bubble mitigation is kept. The `direct_response` path is **deliberately narrow** (no tools, 1–2 sentence replies) and must not be leaned on as a low-latency path. **No WebSocket/SSE is introduced for the graph UI** (R-O1b); chat's existing AG-UI streaming is unchanged in kind. | S9 | 5 |
| 8 | **Engine behaviors** | **Nine states, exact spellings:** `pending` · `ready` · `running` · `awaiting_gate` · `passed` · `rejected_at_gate` · `failed` · `halted` · `superseded`. Both new names verified collision-free repo-wide. Fixed transition table; plus the **amendment class** (D-R15): any non-terminal state → `superseded`, a **normal event, not an exception**. Vocabulary and table declared **once** — a second copy anywhere is a requirement violation (empirically justified: two existing "non-triggering status" sets drifted, 4 members vs 2, under a comment claiming drift was impossible). **`rejected` and `skipped` are NOT in the vocabulary** — `NodeState("rejected")` must raise (AC-23). Illegal transitions are **rejected and recorded** (attempted-from, attempted-to, actor), never silently dropped — the primary detector for the deferred-capability residual. **Tick:** select flows with ≥1 non-terminal node (**bounded and paginated**), check predecessor edges, transition `pending → ready` **through `transition()`** never a direct UPDATE, record rejections, emit metrics; **the tick performs no dispatch** — that split is what makes it land testable. **Concurrency safety:** every write is `UPDATE … WHERE id = :id AND state = :observed_state`, so an overlapping tick affects 0 rows and the loser no-ops — no advisory locks, no leader election. **No silent degradation:** a failed write surfaces (error log + metric + non-success return). **Stall detection:** threshold **derived from and asserted against** the 6 h agent-pod deadline and strictly below it, with a test pinning the relationship so raising the deadline cannot silently invert it; the existing 24 h `stale_count` cutoff is unusable and must not be reused. **Defect cycles:** bound configurable, **default 3**, engine configuration not a call-site constant, and **strictly below `MAX_CHAIN_DEPTH = 8`** so the diagnosable `halted` fires before the guard's opaque `blocked` — a config violating the invariant is **rejected at load**; granularity per-node/per-wave, never per-EPIC. `halted` is **terminal for the engine**; **only an explicit human override resumes it — the engine must never self-clear a halt**, asserted both at source level and behaviourally. **Notification:** today **nobody** is notified (zero SNS topics / alarm actions in agent-factory); v1 requires a path that **actually delivers**, target is configuration not a hard-coded address, emitted **once per event** not per tick — *"a log line alone does not satisfy this check."* **Cost:** `usage_logs` is the **only** source; `budget_usage` is **never** read as a cost source (its `Numeric(10,2)` per-request column structurally drops the sub-cent long tail); **one Postgres query grouped by graph address**, not enumerate-then-`IN`, which also removes the DynamoDB dependency and its 30-day cliff. **The join-key trap (P-R4, AC-19):** join on `usage_logs.agent_run_id` == DDB **`event_id`**; the attribute literally named `run_id` is the KEDA pod name and is what the UI labels "Run / Job ID" — joining by name yields **zero rows, silently, and a $0.00 EPIC**, so the test must fail loudly. Cost is **three-valued** (`known`/`none_incurred`/`unknown`) and **computed, not read** (the cost column is `nullable=False`, so absence is row-nonexistence and `SUM` over zero rows returns 0); `unknown` surfaces its reason; partial aggregates are **labelled partial**; every figure carries *"agent run costs only; excludes build/infra."* **Prompt-level change folded into S7:** remove the words "decisions" and "approvals" from the comment-history preamble in `agent-worker.ts` and the identical string in `skill-agent.ts` — two lines, outsized effect, because that preamble is the prompt-level statement contradicting the engine's authority model. | S2, S6, S7, S8, S11 | 1, 3, 4 |
| 9 | **AIDLC skill** (`.claude/skills/aidlc-emit-issues/`) | **One change, deliberately NOT part of any story.** Emission-lint **Rule 2** currently requires every story's `## Deployment` and every orchestrator to carry the literal 12-digit AWS account ID and `adp-cred` label — in direct contradiction with the intent's definition of done (*"Deploy config lives once in the spec, by reference — never retyped into issue prose"*; the motivating incident was a **14-issue configuration hand-patch**). **Ruling D-R11** amends Rule 2 to a **by-reference** form: issues cite `deploy-target: adp-dev-embark1`; the literals live in exactly one committed place, [`deploy-target.md`](deploy-target.md). Rule 2's *intent* is preserved in full — credentials never ambient, target explicit, named, unambiguous, resolving to exactly one account; only the *location* of the literal changes. **Delivery:** *"ships as a separate small documentation PR; it is not bundled into any story in this EPIC."* Verified at Run A: 14/14 stories cite by reference, **0** contain an account ID, region, or cred label. **Unchanged:** the orchestrator template's ops-persona-drives-the-loop pattern remains the **bootstrap** mechanism (D-C2) because the engine cannot orchestrate its own construction. | — (separate doc PR) | — |
| 10 | **CI** (`.github/workflows/`) | **No workflow is created or modified by any story** — emission-lint Rule 1 passed *"with no new story"* because both required dispatch paths already exist. Automatic on merge: **`gateway-ci.yml`** (`Lint` = ruff check + format check; `Test` = pytest, collects the whole tree so new files need no registration **but must finish inside 60 s**; `Frontend Unit Tests` = vitest; container build; smoke assertions) and **`gateway-deploy.yml`** (image + rollout + frontend build + CloudFront invalidation). **Dispatch-only, never automatic:** `run-gateway-migrations.yml` (**image first, then dispatch** — it `kubectl exec`s `alembic upgrade head` inside a Running pod), `gateway-infra-apply.yml`, `webhook-ingress-deploy.yml`, and the agent-runtime image rebuild. **The internal-plane CI guard** (S3, the single highest-value control in the EPIC) is **plain pytest under the existing `Test` job — no workflow change needed**: it asserts **no route on any internal-plane router references an orchestration model or table**, written as **equality against an allowlist** so it **fails closed on a newly added internal route**. It is not greenfield — two in-test precedents exist in the repo. It is load-bearing because the agent pod is a registered `scope="internal"` principal holding `execute-api:Invoke` on an `ANY`-method route whose integration strips the prefix, and `scope=="internal"` may **override `org_id`** via a header; internal-plane opt-in is **per-route and manual**, so nothing prevents a new route from omitting it. If promotion state ever landed on an internal route, agents would gain write access **with no IAM change, no agent-side code change, and nothing to trigger a security review.** It **must co-land** with the first promotion-state table — *"never split the guard out"* (D-R8, P-R1). **Migration CI gap (X9, hard requirement):** `modules/gateway/alembic/**` is **absent** from `gateway-ci.yml`'s trigger paths, so a migration-only PR gets **zero CI signal** — every migration story therefore ships a test under `modules/gateway/tests/migrations/`, which *is* in the trigger. Engine tests live under `modules/gateway/tests/orchestration/`. **Rule 5 (live API-contract check) fires on wave 6 only** — the sole cross-boundary wave — plus a fixture-provenance sub-check (no invented fields). | S3, S8 (test paths), S4 (validator-in-CI pattern), all code stories | 1–7 |
| 11 | **UNCHANGED — explicitly not touched** | **The GitHub-issue AIDLC experience** (D-R18 + D-R20, binding): it *"keeps working end-to-end **unchanged** for users who never open the dashboard."* **Legacy mode is the DEFAULT and is supported INDEFINITELY**; engine mode is **per-flow opt-in**; **no story may remove, deprecate, or degrade legacy emission**; retiring it would be a separate future intent, never a side effect. No deprecation warning and no "legacy" log line anywhere in the GitHub path. Enforced by **AC-27** (byte-identical tracker output + artifact paths vs a recorded baseline) and **AC-31** (the flag-off issue set is **byte-for-byte** identical — same count, titles, bodies, labels, sub-issue links; **a single differing byte fails**). Also unchanged: existing **DDB `webhook-events` run-status literals** (*"Do NOT extend any existing run-status literal set"*) · **`modules/budget-lambda/`** (sibling module added, not edited) · **existing marker-verification tests** and **existing gate-comment parsing tests** (must stay green **unchanged**; if any needed editing, semantics changed and the PR is reworked) · **`shared/schemas/`** deprecated `class Config` (not migrated) · the flag-gated **PAT clone/push path** (removal out of scope; "PAT-free" scopes to **new** surfaces only) · **`InvocationChain`/`AgentActivity`** (S12 **replaces**, does not extend, the chain view — but their existing tests must stay green) · **`usePollingStatus`** (untouched and explicitly not reused) · **`audit_logs`** and **`security_audit_logs`** (not written to, not extended) · **`budget_usage`** (never read as a cost source) · dead **`budget/middleware.py`** and **`ratelimit/middleware.py`** (do not extend). **Declared out of v1, stated rather than silently dropped:** per-run **pause/steer/abort** (bounded v1 controls = gate-approval + loop-resume only; ships as *"a named interface with no partial implementation"* — an explicit not-implemented response, because *"a half-built control that appears in the UI but does nothing is worse than its absence"*) and **capability confinement** (deferred: *"noted, not designed"*). **25 side-findings remain held** — not this EPIC's scope, file separately. **Accepted residuals, documented not hidden:** RES-1 (agents retain off-graph capability — mitigated by deviation *detection*), RES-2 (pod can write the loop guards' own state), RES-3 (`is_admin` is claim-derived), RES-4 (a broad `secretsmanager:GetSecretValue` — *"flag as fragile; do not cite as a boundary"*). | — | — |
| 12 | **S14 — cut-safe by construction** | The exception-diagnoser is **optional and cut-safe**: *"nothing else in this EPIC imports this module… Do not add a caller in any other story."* Enforced by a source-level assertion that no other `src/orchestration/` module imports `diagnose`. It is dispatched **like any worker via the existing seam, with no elevated genesis**, and holds **no promotion authority** — **propose, never dispose** (D-R10). If wave 7 is cut, nothing else changes. | S14 | 7 |

---

## Rulings that bind more than one row

| Ruling | Effect | Rows |
|---|---|---|
| **D-R8** | The honest v1 guarantee — *"an agent cannot fake having been approved, and off-plan activity is visible on the graph"* — **is** the intended v1; RES-1 accepted as a documented residual. The CI guard lands with the **first** promotion-state story, never retrofitted. | 1, 5, 10, 11 |
| **D-R9** | UX work is wave 1 and human-gated **before** any dashboard story is cut. **A generic table-and-badges UI is a declared failure outcome.** | 4 |
| **D-R10** | The deterministic engine **replaces** the orchestrator-persona pattern. No agent persona drives the loop; personas are workers at nodes. At most an engine-summoned diagnoser that **proposes, never disposes**. | 8, 12 |
| **D-C1 / D-C2** | Negative constraint enforced at emission lint: no story may ship a persona as loop driver, **and none may implement the engine as "observer" of a persona-driven loop** (AC-26). The wave orchestrator issues are **scaffolding** and say so in their own bodies, so no later reader can cite them as the intended design. | 8, 9, 12 |
| **D-R11** | Deploy literals **by reference**; Rule 2 amendment ships as a separate doc PR. | 9 |
| **D-R12** | Engine genesis is human-rooted via the SSO-attributed gate approver; the handler resolves the approver **server-side**. | 5, 6 |
| **D-R13** | Mockups **D** (execution) + **E** (intake) chosen; eight requirements normative; **node taxonomy binding on the store schema** — executable nodes = story/eval/gate; containers = wave/EPIC/flow, **derived state, never rows**; graph address `flow/epic/wave/node`; **the story is the graph floor, runs attach via the ledger, never as nodes**. | 1, 2, 4 |
| **D-R14** | Rules in code, instance in Postgres, tick = EventBridge → VPC Lambda → RDS. Build-vs-adopt settled **with recorded rationale** so an implementing agent does not re-litigate it. | 1, 2, 3, 4 |
| **D-R15** | Plan mutability is first-class: amendment/re-plan is a cheap **validated, attributed** operation with history; `superseded` is normal; **flexibility wins ties**. Amendment is a **second write path into promotion state**, so it needs its own authz, attribution and history. | 1, 8 |
| **D-R16** | Proposal format is **schema-as-code, double-validated**: shared Pydantic model, an advisory repo CLI the skill runs before gating, re-validated **authoritatively at approval inside the same transaction that compiles it to rows**. **Agents never write engine tables directly.** | 1, 10 |
| **D-R17** | Intent-intake chat on the existing chat substrate; 10–18 s cold start named as a UX risk. Premises corrected by X8 (persona pinning net-new) and the missing transcript API. | 7 |
| **D-R18 / D-R20** | Backward compatibility is **permanent, not transitional**; engine flows + graph UI ship **feature-flagged and opt-in**; comment-driven gate answers become a **first-class input adapter, never deprecated**. | 11 |
| **D-R19** | Mockups landed on `main` at `238c49ae` (PR #4189), 6 HTML files. Also corrected #4192's expected file count from 5 → 6 — a check that would otherwise have failed on *correct* work. | 4 |
| **D-R21** | **This file.** One unified component-change map, **kept current per wave**. | all |
| **D-R22** | Every generated issue states its origin in a one-line `> **Origin:**` header. | 9 |

---

## Maintenance protocol

D-R21 is explicit that *"a doc that is only checked at birth goes stale by wave
3."* So:

- **Wave 1's evaluation** asserts this file **exists** and has **≥ 8** table rows.
- **Waves 2–7 each** assert (a) this file's **last-touching commit differs** from
  the one the prior wave's evaluation recorded, **and** (b) the rows **name that
  wave's stories**.

**What that means for an implementing agent.** When you finish a story, update the
row for every component area you actually touched — replace *planned* language
with *what shipped*, and record any correction. Then log the wave below. Updating
the log without updating a row does not satisfy the check.

| Wave | Updated by | Commit | What changed in this file |
|---|---|---|---|
| 1 | S1 ([#4192](https://github.com/aws-e/adp/issues/4192)) | this PR | File created. All 12 rows populated from `requirements.md`, `delivery-plan.md`, `delivery-plan-amendment.md` and `wave-map.md`. Recorded the migration-number collision and the wave-1 dispatch failure under [Live issues](#live-issues-and-stale-premises). |
| 2 | S4, S5 | _pending_ | |
| 3 | S6, S8 | _pending_ | |
| 4 | S7, S11 | _pending_ | |
| 5 | S9, S10 | _pending_ | |
| 6 | S12, S13 | _pending_ | |
| 7 | S14 | _pending_ | |

---

## Live issues and stale premises

Recorded here rather than left to be rediscovered. These are **findings about the
plan**, not changes to it — S1 ships documentation only and does not amend other
stories' bodies.

1. **⚠️ Migration numbers `026` and `027` are already taken on `main`.** The
   plan's head assumption (`025_org_created_via` is head, so *"the next is
   `026`"*) was verified on 2026-08-25 and is now out of date: `main` already
   contains `026_channel_tenant_map_installation_id.py` and
   `027_installation_tenant_uniqueness.py` (current head), both landed
   2026-08-26. As written, **S3 and S8 would collide by filename prefix and fork
   the alembic history**, and two evaluation checks that assert on the revision
   names would fail. **Renumbering forward with `down_revision` set to the real
   head is required.** Row 2 above therefore names the migrations by *purpose*
   rather than by number. Whoever implements S3 must re-derive the head with
   `alembic heads` rather than trusting any number in an issue body.
2. **Wave-1 dispatch was blocked at the time of writing.**
   `adp-trigger --persona operations --issue 4245` returned
   `422 parent_invocation_id does not belong to this chain` on two attempts. The
   `@agent-operations` comment fallback was deliberately **not** used, because a
   bot mention breaks correlation lineage. Worth noting that this is the same
   class of invisible stall the EPIC exists to make visible.
3. **`orchestrator-template.md` still lacks the hotfix-branch protocol text** —
   the recorded root cause of emission-lint Rule 4 existing at all. It was
   repaired only in the seven wave drafts, and **no story owns fixing the
   template**, so the next EPIC's drafts will fail Rule 4 by default again. Held
   side-finding.
4. **Run-B dispatch guidance contradicts itself** across `SKILL.md` (mandates
   `adp-trigger`, forbids a bot mention) and two consumer docs (say post an
   `@agent-operations` mention). Held side-finding.
5. **A cost-figure precondition that is easy to miss:** per environment, confirm
   chat logging is enabled **before trusting any cost figure** — the function
   that bridges cost into `usage_logs` is reachable **only** from the chat-log
   processing path, so chat logging is the cost **computation** pipeline, not an
   observability toggle. The code default and the Terraform default currently
   disagree with no reconciling assertion.

---

## Sources

- [`inception/problem-frame.md`](inception/problem-frame.md) ·
  [`inception/reverse-engineering.md`](inception/reverse-engineering.md) ·
  [`inception/requirements.md`](inception/requirements.md) ·
  [`inception/delivery-plan.md`](inception/delivery-plan.md) ·
  [`inception/delivery-plan-amendment.md`](inception/delivery-plan-amendment.md)
- [`construction/loop-proposal/wave-map.md`](construction/loop-proposal/wave-map.md)
  and the per-wave orchestrator + evaluation drafts
- [`deploy-target.md`](deploy-target.md)
- [`docs/orchestration-graph-mockups/design-contract.md`](../../../docs/orchestration-graph-mockups/design-contract.md)
  — the UI half of this map, also from S1
