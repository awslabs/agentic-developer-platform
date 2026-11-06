# FinOps implementation backlog

Canonical architecture: [F1](architecture.md). Status: **approved implementation design**;
see [accepted revision and actual review](review.json). Seven implementation stories
are natively linked to [#9079](https://github.com/aws-e/adp/issues/9079). They remain
open; this design does not dispatch implementers or satisfy live EC acceptance.
[stories.json](stories.json) is the traceable source for the mapping below.

## Delivery ownership and order

PranavSharma1000 coordinates assignment and acceptance. Each FO implementer owns its
slice and defects; FO07's release implementer integrates compatible code, executes
authorized deployment/live verification and consolidates evidence. FO01 owns shared
contract extension coordination, not sibling design replacement. FO02 owns binding
migration; FO04 owns collection migration; FO06 coordinates consumer contracts.

| Story | Owned outcome | Depends on | Canonical slice |
|---|---|---|---|
| [FO01 #9083](https://github.com/aws-e/adp/issues/9083) | Qualify a supported native cost-inquiry and read-only recovery path, and freeze the scoped request/result contract without inventing a provider API. | First | [qualification-and-billing-boundary](architecture.md#qualification-and-billing-boundary), [configuration-and-typed-interfaces](architecture.md#configuration-and-typed-interfaces), [sql-entities-and-lifecycle](architecture.md#sql-entities-and-lifecycle) |
| [FO02 #9084](https://github.com/aws-e/adp/issues/9084) | Let an entitled project select a billing scope and create or adopt its own native agent without widening access or losing resource ownership. | [FO01](https://github.com/aws-e/adp/issues/9083) | [configuration-and-typed-interfaces](architecture.md#configuration-and-typed-interfaces), [qualification-and-billing-boundary](architecture.md#qualification-and-billing-boundary), [sql-entities-and-lifecycle](architecture.md#sql-entities-and-lifecycle), [rollout-verification-and-cleanup](architecture.md#rollout-verification-and-cleanup) |
| [FO03 #9085](https://github.com/aws-e/adp/issues/9085) | Submit an admitted native cost question once and recover its answer without automatically sending another turn after interruption or session expiry. | [FO01](https://github.com/aws-e/adp/issues/9083), [FO02](https://github.com/aws-e/adp/issues/9084) | [components-and-request-flow](architecture.md#components-and-request-flow), [native-inquiry-and-recovery](architecture.md#native-inquiry-and-recovery), [sql-entities-and-lifecycle](architecture.md#sql-entities-and-lifecycle) |
| [FO04 #9086](https://github.com/aws-e/adp/issues/9086) | Return trustworthy native cost results with independent billing comparison, preserving precision, credits, scope and missing-data limitations. | [FO01](https://github.com/aws-e/adp/issues/9083), [FO03](https://github.com/aws-e/adp/issues/9085) | [independent-reconciliation-and-provenance](architecture.md#independent-reconciliation-and-provenance), [sql-entities-and-lifecycle](architecture.md#sql-entities-and-lifecycle) |
| [FO05 #9087](https://github.com/aws-e/adp/issues/9087) | Let users and agents configure, ask, observe and read the same scoped FinOps inquiry through the Product UI, installed CLI and authenticated MCP. | [FO01](https://github.com/aws-e/adp/issues/9083), [FO04](https://github.com/aws-e/adp/issues/9086) | [client-parity](architecture.md#client-parity), [configuration-and-typed-interfaces](architecture.md#configuration-and-typed-interfaces) |
| [FO06 #9088](https://github.com/aws-e/adp/issues/9088) | Publish cost results for later entitled work and issue explicitly enabled periodic reports without duplicating paid turns. | [FO03](https://github.com/aws-e/adp/issues/9085), [FO04](https://github.com/aws-e/adp/issues/9086) | [publication-and-periodic-reports](architecture.md#publication-and-periodic-reports), [independent-reconciliation-and-provenance](architecture.md#independent-reconciliation-and-provenance) |
| [FO07 #9089](https://github.com/aws-e/adp/issues/9089) | Integrate one compatible FinOps release and prove its native results, recovery, reuse and cleanup on two non-ADP projects. | [FO01](https://github.com/aws-e/adp/issues/9083), [FO02](https://github.com/aws-e/adp/issues/9084), [FO03](https://github.com/aws-e/adp/issues/9085), [FO04](https://github.com/aws-e/adp/issues/9086), [FO05](https://github.com/aws-e/adp/issues/9087), [FO06](https://github.com/aws-e/adp/issues/9088) | [rollout-verification-and-cleanup](architecture.md#rollout-verification-and-cleanup), [prerequisites-and-review-handoff](architecture.md#prerequisites-and-review-handoff) |

Build waves: FO01 → FO02 → FO03 → FO04 → FO05 + FO06 → FO07 acceptance.
FO07 participates early in migration/executor/release compatibility; this dependency
order is not an instruction to delay integration until the end. Independent schema,
client and fixture preparation can proceed without live provider access. A missing
supported executor blocks its dependent native implementation, not unrelated work.
Normal gateway/frontend auto-deploys remain; use one coordinated L5 release per
compatible batch, not seven per-story deployments. Keep `Refs` until live acceptance.

All seven children are in [ADP Platform Roadmap project 4](https://github.com/orgs/aws-e/projects/4); coordinator readback verified membership on 2026-10-27.
PranavSharma1000 must provide the integration's project access or add the seven
children. Module/Feature IDs remain owned qualification work with SA01 #8683.

## Story-to-design and EC-to-AC consistency

Every child references the same F1 architecture revision; section ownership is in
the table above. After ordinary review, record the **actual accepted commit and
review URL** in every child's Approved design entry and the epic. No author-only
claim, issue comment or schema pass counts as approval.

| Epic criterion | Contributing child criteria | Required integration proof |
|---|---|---|
| EC-01 | [FO01 AC-01](https://github.com/aws-e/adp/issues/9083#AC-01), [FO01 AC-02](https://github.com/aws-e/adp/issues/9083#AC-02), [FO01 AC-04](https://github.com/aws-e/adp/issues/9083#AC-04), [FO02 AC-01](https://github.com/aws-e/adp/issues/9084#AC-01), [FO07 AC-02](https://github.com/aws-e/adp/issues/9089#AC-02) | Qualified current provider automation/recovery and Product readiness; no hardcoded target or unrelated prerequisites. |
| EC-02 | [FO01 AC-03](https://github.com/aws-e/adp/issues/9083#AC-03), [FO02 AC-02](https://github.com/aws-e/adp/issues/9084#AC-02), [FO02 AC-03](https://github.com/aws-e/adp/issues/9084#AC-03), [FO05 AC-02](https://github.com/aws-e/adp/issues/9087#AC-02), [FO07 AC-02](https://github.com/aws-e/adp/issues/9089#AC-02), [FO07 AC-04](https://github.com/aws-e/adp/issues/9089#AC-04) | Owned-view success and controlled foreign-view/tenant/project denial on real execution; no scope widening; owned-only cleanup. |
| EC-03 | [FO03 AC-01](https://github.com/aws-e/adp/issues/9085#AC-01), [FO04 AC-01](https://github.com/aws-e/adp/issues/9086#AC-01), [FO04 AC-03](https://github.com/aws-e/adp/issues/9086#AC-03), [FO07 AC-02](https://github.com/aws-e/adp/issues/9089#AC-02) | Native answer and complete independent CE evidence for identical scope/interval/metric/currency/Estimated, with signed Decimal groups. |
| EC-04 | [FO01 AC-01](https://github.com/aws-e/adp/issues/9083#AC-01), [FO03 AC-01](https://github.com/aws-e/adp/issues/9085#AC-01), [FO03 AC-02](https://github.com/aws-e/adp/issues/9085#AC-02), [FO03 AC-03](https://github.com/aws-e/adp/issues/9085#AC-03), [FO04 AC-03](https://github.com/aws-e/adp/issues/9086#AC-03), [FO07 AC-03](https://github.com/aws-e/adp/issues/9089#AC-03) | Lost acknowledgment, restart and session expiry retain one native inquiry identity; read-only recovery or explicit uncertainty. |
| EC-05 | [FO02 AC-03](https://github.com/aws-e/adp/issues/9084#AC-03), [FO03 AC-03](https://github.com/aws-e/adp/issues/9085#AC-03), [FO04 AC-02](https://github.com/aws-e/adp/issues/9086#AC-02), [FO04 AC-03](https://github.com/aws-e/adp/issues/9086#AC-03), [FO05 AC-03](https://github.com/aws-e/adp/issues/9087#AC-03), [FO07 AC-03](https://github.com/aws-e/adp/issues/9089#AC-03), [FO07 AC-04](https://github.com/aws-e/adp/issues/9089#AC-04) | Missing/partial/stale and account-attribution counterexamples; explicit zero; results survive cleanup. |
| EC-06 | [FO01 AC-02](https://github.com/aws-e/adp/issues/9083#AC-02), [FO05 AC-01](https://github.com/aws-e/adp/issues/9087#AC-01), [FO05 AC-02](https://github.com/aws-e/adp/issues/9087#AC-02), [FO05 AC-03](https://github.com/aws-e/adp/issues/9087#AC-03), [FO06 AC-03](https://github.com/aws-e/adp/issues/9088#AC-03), [FO07 AC-01](https://github.com/aws-e/adp/issues/9089#AC-01), [FO07 AC-03](https://github.com/aws-e/adp/issues/9089#AC-03) | Served UI, installed CLI, MCP and opted-in periodic replay share scoped admission/result semantics. |
| EC-07 | [FO06 AC-01](https://github.com/aws-e/adp/issues/9088#AC-01), [FO06 AC-02](https://github.com/aws-e/adp/issues/9088#AC-02), [FO06 AC-03](https://github.com/aws-e/adp/issues/9088#AC-03), [FO07 AC-04](https://github.com/aws-e/adp/issues/9089#AC-04) | Context/Insights consumer receipts, later distinct entitled retrieval/use and controlled denied reuse. |
| EC-08 | [FO07 AC-01](https://github.com/aws-e/adp/issues/9089#AC-01), [FO07 AC-02](https://github.com/aws-e/adp/issues/9089#AC-02), [FO07 AC-04](https://github.com/aws-e/adp/issues/9089#AC-04) | Two non-ADP projects with distinct effective scopes and unchanged release digests; native reconciliation and owned/adopted disposition. |

The `#AC-XX` issue-link suffix is a criterion key, not a promised GitHub heading
anchor. Parent #8150's EC IDs/mapping are preserved through #9079; this assignment
does not modify sibling service criteria or claim parent completion.

## Advisory files and shared edits

Existing paths below are relative to `modules/L5-services/src/l5_services/` unless
host/repository-prefixed. Proposed paths are explicitly marked. An unexpected but
necessary adjacent edit is coordinated with the owner, not a node failure gate.

### FO01 files and integration

Existing shared contracts/v1, run_input.py, adapters/protocol.py, configuration registration and host discriminators; proposed FinOps qualification record and typed payload variant. Coordinate shared edits with DA09 #8874 / PR #9081 and SA01 #8683; do not replace sibling contracts.

Input: current provider documentation, authorized view/data-role fixture and Product contract. Output: dated support/scope/recovery matrix, FinopsRunInput/FinopsCostResult schemas, capability-specific readiness, catalog mapping and compatible client/worker variants. Existing router accepts Security/DevOps, not FinOps.

Contract preparation is independent of live access. A qualified provider path is required before its dependent native implementation/activation; FO07 integrates schema/host/worker release. No provider setup or paid inquiry is authorized by filing.

### FO02 files and integration

Existing configuration_population.py, configuration_readiness.py, models/binding.py, models/setup.py, binding APIs/workers and gateway l5_integration.py; proposed adapters/finops/setup.py and additive shared retained-agent scope uniqueness migration.

Input: Product API Profile revision, explicit connection/region/view/data-role selections and create/adopt intent. Output: configuration ETag/readiness, persistent binding generation and durable setup/retirement receipts. Save/check have no provider effects; startBindingOperation is the shared setup interface.

FO02 owns setup/cleanup semantics and migrations; FO07 deploys compatible batch. Setup is separate from inquiries; retain adopted agents/views/roles and all results. Unknown partial setup is reconciled, never blindly recreated.

### FO03 files and integration

Existing admission.py, workers/execution.py, workers/recovery.py, work_store.py, models/attempt.py and gateway host; proposed adapters/finops/inquiry.py and qualified protected executor. Share manifest/dispatch extensions with DevOps DA09, not a second worker engine.

Input: frozen FinopsRunInput plus pinned binding/configuration and current delegation/budget. Output: 202 run/receipt, durable attempt/question hash/native conversation-turn identity, observations and native answer. Existing AssessmentAdapter methods are reused; FinOps executor selection depends on FO01 qualification.

FO03 owns private executor integration and bounded actions; FO07 packages/releases it with shared transport. No public Lambda URL or client-visible session. Preserve repair and native identity on rollback.

### FO04 files and integration

Existing result/evidence models, api/results.py and collection worker transitions; proposed adapters/finops/billing.py, result normalizer and l5_service_cost_collections migration. Do not coerce cost data into Security findings.

Input: original native answer/identity and exact pinned scope/query. Output: FinopsCostResult with separate native and CE provenance, complete page evidence, signed decimal strings, Estimated/freshness/coverage and comparison status. CE-only output is not a native answer.

FO04 owns additive collection/result schema; FO07 deploys readers before producers. Native result survives billing/publication failures and owned cleanup; rollback retains immutable evidence/checkpoints.

### FO05 files and integration

Existing gateway Product workspace, frontend/src/services/products.ts, CLI command manifest/install path and tools facade catalog; proposed shared adp service FinOps commands and l5___ operation registrations. Domain logic stays in L5.

Input: explicit Product/project/service route scope and shared schema arguments. Output: identical configuration/setup/run/result/publication envelopes and errors. Show pending/blocked/partial/unknown honestly; no native session or direct provider client.

FO05 owns host/UI/installed CLI/tool integration. Keep normal gateway/frontend auto-deploys; FO07 checks all installed artifacts against compatible worker/schema release. No per-project deployment.

### FO06 files and integration

Existing events/schedules.py, event receipts/recovery and shared publication/peer contracts; proposed FinOps result contribution variant and scheduled cost-query snapshot. Coordinate SA11 #8693 and Context/Insights owners; no new scheduler/control plane.

Input: immutable result/version/scope and enabled schedule with UTC tick/template. Output: independent consumer receipts and later authorized retrieval, or explicit retry/block; periodic tick freezes the same FinopsRunInput used by manual admission.

FO06 owns producer/consumer contract integration with peer owners; FO07 wires/verifies actual schedule ingress and release. Result/publication repair never repeats native work; revoked destinations remain denied.

### FO07 files and integration

Existing L5 scripts/infra/tests, gateway migrations/packaging/workflows and platform launcher/release integration; proposed tests/live/FinOps cases and finops/runbook.md. Reuse Security SA15 #8698 / PR #8932; exact file lists are advisory, never node failure gates.

Input: prior slices, selected private target/access/budget, compatible artifact identities and two distinct controlled cost scopes. Output: installed release identity, EC-to-AC evidence, costs/disposition and recovery/runbook. No new AWS authority from the issue.

FO07 release implementer owns integration, allowed deployment, live tests and defect closure; PranavSharma1000 coordinates/accepts. Retain normal host auto-deploys, one coordinated L5 release per compatible batch; never equate CI/merge with live acceptance.

## Verification contract

All implementation tests are **planned**, not executed by the architect. A pre-review
schema/unit/integration pass is not live provider acceptance. Use the real gateway
route, installed worker/CLI/tool transport or served UI for the required layer;
helper-only tests cannot replace missing entry points. Proposed automated selectors
are `modules/L5-services/tests/finops/test_foNN.py::test_NAME`, as mapped in each
child. Implementers may put host/browser selectors in their owning existing test
suite and update the mapping without changing AC/SCN identities.

Prerequisites: FO01-qualified supported provider path, authorized private fixtures,
current delegated connection/roles/view scope and existing budgets/limits. Live work
runs only under existing authority, with FO07 recording the selected release and
scope privately. No live commands/targets or credentials are included here.

Determinism and independent oracle:
- Fixture run uses fixed UTC clock `2026-10-27T00:00:00Z`; query is September 2026
  inclusive start / October 2026 exclusive end. Live equivalent uses an explicitly
  recorded provider-supported closed interval, not a moving “last month” at retry.
- Isolate two tenants, two projects in one Product, and two non-ADP billing scopes;
  use run-owned fixture identifiers and teardown ledger, not existing customer data.
- Test positive scope plus denial with separate expected policies/readbacks. A role
  simulation is supporting evidence only; actual selected-path denial is required.
- Freeze response pages, group/currency/Estimated fields and query digests. Independently
  expected `10.125 + -0.125 = 10.000`, `0.00000001 != 0`, and explicit `0.000 = 0` catch
  float conversion, dropped credits and false zero. Missing fields expect null.
- Inject interruption at specified durable boundaries (after SQL commit/before send,
  after provider acceptance/before acknowledgment, after page save/before checkpoint,
  after consumer accept/before acknowledgment). Assert SQL receipts, native turn count
  and consumer projection independently of the submitted request/helper return.
- Advance clock explicitly for session expiry, deadlines, retry due-times and a month
  boundary; no sleeps or reset budgets. Assert bounded attempts, no duplicate effects,
  no mixed collection pages and no stale indexed access after revocation.
- Pin adapter/package/schema/source revisions; preserve every attempt and private
  evidence digest. Clean up only owned resources and report retained/adopted/unknown
  dispositions. No raw sessions, account identities or billing artifacts in GitHub.

For each manual scenario below: (1) FO07/FO01 records fixture/release/authority and
initial state; (2) performs the stated action through the qualified real interface;
(3) collects independent provider/SQL/client/consumer readbacks; (4) compares every
expected predicate; (5) records pass/fail/blocked/not-run and cleanup/cost disposition.
An unsupported action records blocked and the missing prerequisite, never a fabricated
pass. These are versioned procedures; commands/selectors are implementation deliverables.

## FO01 acceptance and scenarios

### FO01 SCN-01

- **Criterion:** AC-01 → EC-01, EC-04.
- **Given/when:** Inspect the supported provider surface and use an authorized qualification fixture for identity/read-only recovery, including expiry.
- **Then, independently asserted:** Record versioned evidence of supported automation, agent/conversation/turn correlation, read-only resume and limits. Missing proof is explicit unsupported/blocked; no fabricated SDK or hidden endpoint. A native-ready declaration requires actual qualified observations.
- **Evidence:** qualification record + private readbacks; unqualified native ECs remain incomplete.
- **Phase/owner:** live qualification; FO01; manual; planned, not executed.
- **Controls:** verification contract above; test key `qualification`.

**Manual qualification procedure (not executed):**

1. **Authorize and pin:** FO01 records the explicit private fixture, selected account/role/region, Product/project and billing-view scope, provider documentation/interface version, terms, entitlement, existing request/time/spend limits, clock and intended closed-period question. Verify the exact selected role through the approved credential path. If a supported automation or read-only interface, permission or spending authority is unverified, stop before any paid turn; leave its fact `unverified` or mark it `unsupported`, and mark the live test `blocked`/`not_run`, never `qualified`.
2. **Identify before effect:** Using only a documented and authorized supported interface, record the agent identity and a durable scoped inquiry/question hash before one expressly authorized native submission. Record the resulting conversation, turn and provider receipt/status independently of the client's response, plus the observed request count and limits. Never infer an API from a console workflow or an IAM action.
3. **Interrupt and observe:** After native acceptance, withhold the response/acknowledgment and restart the observer. From the saved inquiry identity, use the qualified *read-only* provider history/status/answer path to locate the same conversation and unique turn. If an ID or acknowledgment was lost, a timestamp or matching answer text alone cannot establish uniqueness: keep `outcome_unknown` and do not resubmit.
4. **Expire and reauthorize:** Record the actual session expiry and repeat the *read-only* observation after refreshing the selected connection under current delegation and exact role/scope. Separately check that expired or revoked delegation blocks observation. Neither expiry, restart nor a failed read permits a new conversation, turn, budget or automatic paid retry.
5. **Reconcile and retain:** Compare independent provider readbacks before and after restart/expiry for agent, conversation, unique turn, scoped question, status/answer and paid turn count; record provider limits and pricing against current policy. Keep raw sessions, target identifiers, receipts and cost evidence in the private store. In `qualification.json`, populate tested revision and private evidence references only for facts actually observed; leave unproven facts unverified/unsupported and `native_ready` false. Record attempted calls, cost if known (otherwise unknown), owned-only cleanup and the disposition of each missing observation. Never substitute this procedure or a mock for the live SCN-01 readbacks.

### FO01 SCN-02

- **Criterion:** AC-02 → EC-01, EC-06.
- **Given/when:** Validate FinOps payloads through shared route/runtime schemas alongside unchanged Security/DevOps fixtures.
- **Then, independently asserted:** Accept scoped fixed-period cost queries without a repository; reject unknown fields, caller view/role, invalid dates and unsupported metrics. Same old fixtures retain their prior behavior.
- **Evidence:** schema/host contract tests and independent error assertions.
- **Phase/owner:** pre-review; FO01; automatic-integration; planned, not executed.
- **Controls:** verification contract above; test key `contract_variants`.

### FO01 SCN-03

- **Criterion:** AC-03 → EC-02.
- **Given/when:** Inspect selected view contents and provider data-role policy; exercise owned and controlled foreign-view paths.
- **Then, independently asserted:** Only selected billing is readable; foreign view denied through actual provider/data-role execution, not merely a CE filter or IAM simulation. No default primary view or scope widening.
- **Evidence:** scope matrix + permitted/denied native readbacks.
- **Phase/owner:** live qualification; FO01/FO07; manual; planned, not executed.
- **Controls:** verification contract above; test key `billing_boundary`.

**Manual billing-isolation procedure (not executed):**

1. **Authorize and pin scope:** FO01/FO07 record the approved private, controlled owned and foreign Billing View fixtures, two projects/tenants, supported provider path, exact selected connection/account/role/region, closed period, clock, existing limits and expected account sets. Verify the selected identity and explicit authority before any inquiry; an unqualified interface or missing permission leaves the dependent live test `blocked`/`not_run`. Do not use real foreign billing, create roles/views implicitly or fall back to the primary view or ambient role.
2. **Read the boundary:** Independently read selected view health, filters, contents and revision/scope generation (`selected_view_contents`), agent-to-view binding (`provider_view_attachment`), effective data-role actions/resource constraints and policies (`data_role_permissions`) and qualified provider principal/trust conditions (`data_role_trust`). Identify actions that require wildcard resources and their compensating constraints instead of assuming they are view-scoped. A changed filter or view contents under an unchanged identifier invalidates prior qualification.
3. **Prove owned access:** Through the documented, authorized native provider path and the *selected data role*, run only an expressly approved inquiry against the owned view. Obtain an independent native/provider readback for `owned_view_read` with the actual view, role, agent, closed period, scope and observed result; compare the account set to the pinned owned-view expectation, not to a CE-filtered answer alone.
4. **Prove foreign denial:** Through that same native/provider data-role path attempt the separately authorized controlled foreign view and retain the explicit denial plus provider/role identity for `foreign_view_denial`. No selected-view fallback, default primary view or partial foreign result counts as denial. Independently verify `foreign_tenant_denial` and `other_project_denial` at the Product host's binding/result routes. An IAM policy simulation or CE filter is supporting information only, never proof of native denial.
5. **Compare and retain:** Check each of the eight named observations separately against its expected policy and readback; record tested revision, attempt count, cost if known (otherwise unknown), scope generation and private evidence reference only for checks actually performed. Missing native read or denial stays `not_run`/`blocked`/`failed` and keeps `native_ready` false. Recheck both paths after any view/policy/role change; keep identifiers, raw billing, roles and receipts private, and clean up owned-only fixtures under separate authority. Never turn these written steps or a mock into live SCN-03 evidence.

### FO01 SCN-04

- **Criterion:** AC-04 → EC-01.
- **Given/when:** Resolve Product capability/catalog mapping with SA01 and inspect Profile inputs.
- **Then, independently asserted:** Registered Module/Feature IDs or owned unresolved status, not invented IDs; only inquiry-relevant prerequisites block readiness. Allocation proposals never become spending authority.
- **Evidence:** catalog readback, Profile mapping and readiness counterexamples.
- **Phase/owner:** pre-review; FO01/coordinator; automatic-integration; planned, not executed.
- **Controls:** verification contract above; test key `readiness`.

## FO02 acceptance and scenarios

### FO02 SCN-01

- **Criterion:** AC-01 → EC-01.
- **Given/when:** Save/check/refresh a FinOps configuration with optional Profile fields absent and explicit choices present.
- **Then, independently asserted:** Keep authorized choices and immutable Profile provenance; stale ETag fails; no setup effect from save/check. Missing repository never blocks account inquiry; missing qualified billing does.
- **Evidence:** API/SQL and provider-call-counter assertions.
- **Phase/owner:** pre-review + live; FO02; automatic-integration; planned, not executed.
- **Controls:** verification contract above; test key `configuration`.

### FO02 SCN-02

- **Criterion:** AC-02 → EC-02.
- **Given/when:** Create/adopt through startBindingOperation using two projects and concurrent claims on the same native agent.
- **Then, independently asserted:** Correct selected role/view bound and verified; foreign tenant/project/view denied. One context-bearing agent cannot bind to two project scopes, including cross-tenant attempts; masked conflict, no credential leak.
- **Evidence:** PostgreSQL race test + private native scope readback.
- **Phase/owner:** pre-review + live; FO02/FO07; automatic-integration; planned, not executed.
- **Controls:** verification contract above; test key `bindings`.

### FO02 SCN-03

- **Criterion:** AC-03 → EC-02, EC-05.
- **Given/when:** Interrupt setup after each owned resource, replay intent, then retire owned and adopted fixtures.
- **Then, independently asserted:** Same intent reconciles without duplicate creation; only owned unreferenced resources are removed and absence verified. Adopted resources/results persist; cleanup uncertainty remains visible and repairable.
- **Evidence:** fault-injection receipts + owned/adopted disposition report.
- **Phase/owner:** pre-review + live; FO02/FO07; automatic-integration; planned, not executed.
- **Controls:** verification contract above; test key `setup_cleanup`.

## FO03 acceptance and scenarios

### FO03 SCN-01

- **Criterion:** AC-01 → EC-03, EC-04.
- **Given/when:** Submit through the real L5 API, duplicate the request, lose queue acknowledgment and restart execution after native acceptance.
- **Then, independently asserted:** One run/receipt and at most one native submit; SQL attempt precedes effect. Reconcile the same native identity or expose outcome_unknown. Same key/different body conflicts.
- **Evidence:** SQL/SQS fault trace + independent native turn count/readback.
- **Phase/owner:** pre-review + live; FO03/FO07; automatic-integration; planned, not executed.
- **Controls:** verification contract above; test key `submit_recovery`.

### FO03 SCN-02

- **Criterion:** AC-02 → EC-04.
- **Given/when:** Expire a session or revoke delegation between submit and observation; then restart or explicitly reconcile.
- **Then, independently asserted:** No new conversation/turn. Reauthorized refresh observes same inquiry; denied/ambiguous readback stays blocked/unknown. Persisted limits do not reset. No session material in public artifacts/logs.
- **Evidence:** expiry/revocation trace, provider count and redaction checks.
- **Phase/owner:** pre-review + live; FO03/FO07; automatic-integration; planned, not executed.
- **Controls:** verification contract above; test key `session_recovery`.

### FO03 SCN-03

- **Criterion:** AC-03 → EC-04, EC-05.
- **Given/when:** Cancel/expire an active inquiry and inject lost answer-storage acknowledgment.
- **Then, independently asserted:** Local cancellation never claims unsupported native stop; saved answer reuses identity. Collection/publication repair cannot call submit; refusal is not automatically rephrased.
- **Evidence:** worker transition tests + native status/answer readback.
- **Phase/owner:** pre-review + live; FO03/FO07; automatic-integration; planned, not executed.
- **Controls:** verification contract above; test key `terminal_recovery`.

## FO04 acceptance and scenarios

### FO04 SCN-01

- **Criterion:** AC-01 → EC-03.
- **Given/when:** Collect a native answer and all CE pages for identical scope/period/metric/currency/Estimated, including signed credit groups.
- **Then, independently asserted:** 10.125 plus -0.125 equals 10.000 exactly; 0.00000001 stays nonzero. Every group and total reconciles with Decimal; rounded display is distinct from exact and mismatched dimensions never compare equal.
- **Evidence:** independent fixed-value oracle + native/CE raw evidence.
- **Phase/owner:** pre-review + live; FO04/FO07; automatic-integration; planned, not executed.
- **Controls:** verification contract above; test key `decimal_reconciliation`.

### FO04 SCN-02

- **Criterion:** AC-02 → EC-05.
- **Given/when:** Feed missing/partial/stale billing, empty pages, unknown Estimated/freshness and account-only attribution.
- **Then, independently asserted:** Unavailable amount is null, not zero; complete explicit zero remains zero. Unknown freshness/Estimated stay unknown; account costs never labelled Product-attributed without proof. Results retain original scope/time.
- **Evidence:** negative result/API assertions with independently defined fixtures.
- **Phase/owner:** pre-review + live; FO04/FO07; automatic-integration; planned, not executed.
- **Controls:** verification contract above; test key `truthful_results`.

### FO04 SCN-03

- **Criterion:** AC-03 → EC-03, EC-04, EC-05.
- **Given/when:** Lose a CE-page acknowledgment; repeat a token; exceed admitted limit; expire a cursor and resume collection.
- **Then, independently asserted:** Bounded calls/checkpointed pages, no cross-collection mixing or false completeness. New collection supersedes invalid cursor; native turn count unchanged; result/evidence survives provider cleanup.
- **Evidence:** page-sequence fault report + request ledger/native count.
- **Phase/owner:** pre-review + live; FO04/FO07; automatic-integration; planned, not executed.
- **Controls:** verification contract above; test key `collection_recovery`.

## FO05 acceptance and scenarios

### FO05 SCN-01

- **Criterion:** AC-01 → EC-06.
- **Given/when:** Use served UI, installed CLI and authenticated MCP to configure, set up, submit and read one inquiry.
- **Then, independently asserted:** Same scoped state/ETag/receipt/run/result across clients; no direct provider calls or credentials in responses. 202 shown as accepted, not completed.
- **Evidence:** three-client journeys + API and provider readback.
- **Phase/owner:** pre-review + live; FO05/FO07; automatic-e2e; planned, not executed.
- **Controls:** verification contract above; test key `client_parity`.

### FO05 SCN-02

- **Criterion:** AC-02 → EC-02, EC-06.
- **Given/when:** Switch to another project/tenant and replay IDs/cursors/keys; exercise stale ETag and invalid query.
- **Then, independently asserted:** Masked inaccessible resource/authorization failure; no cross-scope cache or artifact leak. 412/422/428/409 semantics match; idempotent replay remains reauthorized.
- **Evidence:** host/client negative assertions and artifact access denial.
- **Phase/owner:** pre-review + live; FO05/FO07; automatic-e2e; planned, not executed.
- **Controls:** verification contract above; test key `client_denial`.

### FO05 SCN-03

- **Criterion:** AC-03 → EC-05, EC-06.
- **Given/when:** Read partial/unknown/refused results; request explicit wait and read-only recovery in installed clients.
- **Then, independently asserted:** Original answer and independent evidence remain distinct; null/Decimal preserved. Documented wait exit/error states; no hidden resubmit, rephrase or DevOps federation reuse.
- **Evidence:** installed JSON/exit-code and UI/MCP response checks.
- **Phase/owner:** pre-review + live; FO05/FO07; automatic-e2e; planned, not executed.
- **Controls:** verification contract above; test key `client_outcomes`.

## FO06 acceptance and scenarios

### FO06 SCN-01

- **Criterion:** AC-01 → EC-07.
- **Given/when:** Publish a result, lose consumer acknowledgment, replay, then read consumer projection.
- **Then, independently asserted:** One semantic contribution per result/version/consumer; independent receipts/readback for Context and Insights. Stored result survives outage, no new provider turn.
- **Evidence:** consumer receipts + projection and provider-count assertions.
- **Phase/owner:** pre-review + live; FO06/FO07; automatic-integration; planned, not executed.
- **Controls:** verification contract above; test key `publication`.

### FO06 SCN-02

- **Criterion:** AC-02 → EC-07.
- **Given/when:** Run a later distinct entitled task, then revoke source/Product access and repeat retrieval.
- **Then, independently asserted:** Later task retrieves and uses original cost scope/period/Estimated/freshness with citations; revoked/foreign task cannot retrieve indexed or artifact copies.
- **Evidence:** later-task retrieval/use trace + controlled denial.
- **Phase/owner:** live; FO06/FO07; manual; planned, not executed.
- **Controls:** verification contract above; test key `entitled_reuse`.

### FO06 SCN-03

- **Criterion:** AC-03 → EC-06, EC-07.
- **Given/when:** Enable a monthly report; replay one tick across month boundary; disable/revoke before another tick.
- **Then, independently asserted:** One frozen closed interval and inquiry per scoped tick; no retry-time interval drift or catch-up burst. Disabled/revoked tick rejects; existing budget and dedup apply.
- **Evidence:** fixed-clock schedule/API/worker integration + live replay.
- **Phase/owner:** pre-review + live; FO06/FO07; automatic-integration; planned, not executed.
- **Controls:** verification contract above; test key `periodic_reports`.

## FO07 acceptance and scenarios

### FO07 SCN-01

- **Criterion:** AC-01 → EC-06, EC-08.
- **Given/when:** Release additive schema, compatible package/workers/clients and enabled fixtures; test fresh/existing/pending work and rollback.
- **Then, independently asserted:** Installed components match manifest; queue/repair/schedule ingress work. Rollback disables new admissions but preserves observations/results/cleanup; no schema/evidence loss.
- **Evidence:** release identity + migration/worker/rollback readbacks.
- **Phase/owner:** pre-review + live; FO07; automatic-integration; planned, not executed.
- **Controls:** verification contract above; test key `release`.

### FO07 SCN-02

- **Criterion:** AC-02 → EC-01, EC-02, EC-03, EC-08.
- **Given/when:** Use same release on two non-ADP projects with distinct effective billing scopes; ask fixed native inquiries and attempt controlled foreign access.
- **Then, independently asserted:** Both reconcile with complete CE evidence without platform edits/redeploy. Foreign view/tenant/project denied through real path; no unrequested enrollment or widening.
- **Evidence:** two native answers, CE comparison, denial and unchanged artifact digests.
- **Phase/owner:** live; FO07; manual; planned, not executed.
- **Controls:** verification contract above; test key `portable_native`.

### FO07 SCN-03

- **Criterion:** AC-03 → EC-04, EC-05, EC-06.
- **Given/when:** Interrupt worker/submit acknowledgment/session, CE collection and publication; exercise installed clients and zero/missing/credit cases.
- **Then, independently asserted:** Same inquiry identity, no duplicate native turn; bounded repair; unknown/partial explicit. Clients retain scope, exact Decimal and original answer, not synthetic success.
- **Evidence:** fault matrix + real queue/provider/client observations.
- **Phase/owner:** live; FO07; manual; planned, not executed.
- **Controls:** verification contract above; test key `fault_acceptance`.

### FO07 SCN-04

- **Criterion:** AC-04 → EC-02, EC-05, EC-07, EC-08.
- **Given/when:** Publish and reuse results, retire owned resources, verify adopted retention and attempt denied reuse.
- **Then, independently asserted:** Consumer acknowledgments and later entitled use proven; owned absence verified, adopted/result retention preserved; current denial enforced. Costs distinguish measured/estimated/unknown; every EC mapped to proof.
- **Evidence:** consumer/task traces + cleanup/cost report at tested revision.
- **Phase/owner:** live; FO07; manual; planned, not executed.
- **Controls:** verification contract above; test key `cleanup_reuse`.
