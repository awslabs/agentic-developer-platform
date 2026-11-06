# Well-Architected implementation backlog

## Delivery contract

This backlog implements [epic #9078](https://github.com/aws-e/adp/issues/9078)
against the [approved architecture](architecture.md). The [review record](review.json)
identifies the accepted revision and actual approval. Native children and their pinned design revision are recorded
in [issue-alignment.json](issue-alignment.json) after filing. Do not dispatch
agents or close implementation stories because the design is published.

PranavSharma1000 coordinates/accepts; each assigned story implementer owns delivery
and findings, WA07 owns integration and live release acceptance. Every story's
completion boundary is deployed/live-verified owned behavior (WA01 also supplies
pre-review contracts and live qualification); mocks and merged code alone do not
close the epic. Catalog IDs remain WA01/coordinator-owned and unmapped until
verified. All seven children are in roadmap project 4; coordinator readback verified
membership on 2026-10-27.

The seven independent outcomes use dependency interfaces, not whole sibling epic
closure. WA01 produces typed contracts first; WA02 setup precedes WA03 admitted
source handoff; WA04 executes; WA05 publishes results; WA06 integrates client/event
surfaces; WA07 coordinates shared edits throughout and performs the compatible
release. Independent client/error-fixture preparation need not wait for live
Support eligibility. Required live acceptance stays incomplete if blocked.

## Story-to-design mapping

| Group | Outcome | Depends on | Canonical sections | Native issue |
|---|---|---|---|---|
| WA01 | Qualify AWS Agent eligibility and shared architecture-review contracts | None; qualifies first | [section](architecture.md#configuration-and-interfaces), [section](architecture.md#generation-recovery-and-cancellation), [section](architecture.md#qualification-register) | [#9098](https://github.com/aws-e/adp/issues/9098) |
| WA02 | Configure and create or adopt source-only Agent Profile bindings | WA01 | [section](architecture.md#configuration-and-interfaces), [section](architecture.md#sql-and-lifecycle), [section](architecture.md#identity-and-provider-setup) | [#9099](https://github.com/aws-e/adp/issues/9099) |
| WA03 | Pin bounded architecture sources with exhaustive coverage and retention | WA01, WA02 | [section](architecture.md#source-packaging-and-provenance), [section](architecture.md#sql-and-lifecycle) | [#9100](https://github.com/aws-e/adp/issues/9100) |
| WA04 | Run native recommendation generations with durable uncertain-outcome recovery | WA02, WA03 | [section](architecture.md#generation-recovery-and-cancellation), [section](architecture.md#sql-and-lifecycle) | [#9101](https://github.com/aws-e/adp/issues/9101) |
| WA05 | Publish attributable recommendations and prove later entitled design reuse | WA04 | [section](architecture.md#results-publication-and-cleanup), [section](architecture.md#sql-and-lifecycle) | [#9102](https://github.com/aws-e/adp/issues/9102) |
| WA06 | Expose shared UI CLI MCP reviews and opt-in scheduled admission | WA01, WA04, WA05 | [section](architecture.md#configuration-and-interfaces), [section](architecture.md#rollout-and-verification-responsibilities) | [#9104](https://github.com/aws-e/adp/issues/9104) |
| WA07 | Integrate compatible release and prove portable live recovery reuse and cleanup | WA01, WA02, WA03, WA04, WA05, WA06 | [section](architecture.md#rollout-and-verification-responsibilities), [section](architecture.md#qualification-register), [section](architecture.md#results-publication-and-cleanup) | [#9105](https://github.com/aws-e/adp/issues/9105) |

## Shared edit and release ownership

- WA01 proposes common catalog/schema/registration changes with SA01 #8683 and
  DevOps admission PR #9081. One compatible discriminated contract, not conflicting
  service branches with renamed common fields.
- WA03 coordinates source retention with Product source owner #8684; WA04 owns
  the late-native-outcome lifecycle fix with shared worker #8688; WA05 integrates
  SA11 #8693 and Context #8149/Insights contracts rather than inventing a new sink.
- WA06 coordinates shared UI/CLI/MCP owners #8694/#8695/#8696. WA07 coordinates
  gateway/factory/broker/migration changes and release #8698 / open PR #8932.
- Deliver in compatible contracts/setup, execution/results/clients, then release
  batches. Retain normal gateway/frontend auto-deploys and one shared L5 worker/
  infra release per compatible batch. No per-story deployment or new approval gate.
- File groups below are advisory implementation scope, not permission boundaries,
  node failure gates or a substitute for behavior/acceptance. Runtime stays in
  `modules/L5-services/`; design/runbook documentation stays in `docs/l5-services/`.

## Acceptance and scenarios

Stable issue-local AC/SCN IDs are qualified by WA group/native issue. They are not
parent #8150 EC IDs. All rows are **planned**. The independent assertion is in
Expected result; evidence cannot merely repeat the production decision function.
`story-specs.json` preserves exact acceptance content for issue-body alignment.

Determinism controls C1 apply to every scenario: immutable source/fixture and SDK
revision; isolated tenant/Product/project/run namespace; injected UTC clock and
seeded jitter for offline tests; explicit transaction barriers for races; bounded
timeouts/pagination/retries; fresh owned resource receipts and verified cleanup.
Live tests use monotonic observed timestamps and injected failures at named write
boundaries, not arbitrary sleep timing. Provider output is nondeterministic: check
attribution/coverage/substantive source comparison, never golden prose or a fixed
finding count. Each live case has separate foreign/sibling actors and restores only
its own fixture state. Archive tests independently enumerate Git/document bytes.
Provider effect assertions use a separate call journal/native readback, not the
adapter's reported status. A deliberately duplicated Start, wrong generation,
changed hash, false remote cancellation or denied reader obtaining content must
fail its mapped test.

### WA01

**Qualify AWS Agent eligibility and shared architecture-review contracts**

Teams can tell which architecture-review operations are supported, what input they require and why live activation is blocked, without buying Support or guessing provider behavior.

**Implementation contract:** Register proposed well_architected/architecture_review as typed service/capability variants; mixed immutable document/repository_commit inputs, five supported Agent pillars and source_only mode. Record current SDK model hash, exact Support/region/source-size/additionalContext/role/readback semantics and safe no-effect/reconciliation rules. Native Start has no client token in the inspected model; local timeout is not cancellation. Public requests cannot carry authority or arbitrary SDK arguments. Existing Security/DevOps callers remain compatible.

**Owned/advisory files:** Shared contracts/v1 catalog, schemas, OpenAPI, operation/MCP matrices and compatibility fixtures; L5 run_input/configuration registration; proposed adapters/well_architected qualification fixtures and docs. Coordinate common edits with #8683 and DevOps PR #9081; no platform constants.

**Prerequisites:** Eligible private provider fixture and authoritative Module/Feature catalog mapping are unverified. WA01 records qualification evidence; WA07 supplies eligible access; PranavSharma1000 resolves catalog IDs. Live contract qualification/activation and dependent dispatch are affected, not independent schema work.

| AC ID | Epic criteria | Setup/action | Expected result | Evidence | Phase / owner |
|---|---|---|---|---|---|
| AC-01 | EC-01 | Given selected eligible and ineligible private fixtures, run only the admitted provider qualification procedure. | Record current region/Support/source/API limits and exact affected-capability reason; denied IAM is not misreported as Support status; no purchase or unrequested resource is created. | Versioned qualification table, official model/doc revision and redacted native positive/negative responses plus resource readback. | Before review: table/schema; live qualification: WA01 with WA07 fixture |
| AC-02 | EC-01, EC-04 | Given current SDK shapes and scripted lost Start responses, missing/list-delayed/multiple matches and unsupported stop, qualify recovery decisions. | No nonexistent clientToken/stop call; only a unique attributable generation reconciles. Zero matches are not no-effect proof; local expiry is distinct from native state. Current live readback limits are explicit. | SDK/model fixtures, independent provider call-count assertions and qualified native identity/read-consistency record. | Before review + live semantics: WA01; integration WA04/WA07 |
| AC-03 | EC-01, EC-06 | Given WA and existing Security/DevOps payloads, validate all shared client/schema variants and catalog references. | Valid mixed pinned sources pass; unknown fields, resource inspection, unsupported pillars and authority overrides fail. Existing fixtures still pass; unmapped catalog IDs remain explicitly unresolved before dispatch. | Contract validator output, negative fixtures, operation parity matrix and authoritative catalog mapping or scoped block. | Before review: WA01 |

| Scenario ID | Acceptance IDs | Case contract | Verification | Test reference | State |
|---|---|---|---|---|---|
| SCN-01 | AC-01 | Given/When/Then: acceptance row AC-01; Controls: C1, isolated scope and exact native/source identity | manual | [versioned qualification and evidence procedure](qualification.md#manual-scn-01-evidence-and-disposition) | manual/not-run: private fixtures required |
| SCN-02 | AC-02 | Given/When/Then: acceptance row AC-02; Controls: C1, isolated scope and exact native/source identity | automatic-integration | `modules/L5-services/tests/well_architected/test_wa01.py::test_scn_02` ([pinned scripted fixture](../../../modules/L5-services/tests/well_architected/fixtures/scn_02.json)) | offline adapter passed; installed-worker integration pending WA04/WA07 |
| SCN-03 | AC-03 | Given/When/Then: acceptance row AC-03; Controls: C1, isolated scope and exact native/source identity | automatic-integration | `modules/L5-services/tests/well_architected/test_wa01.py::test_scn_03` ([pinned contract fixture](../../../modules/L5-services/tests/well_architected/fixtures/scn_03.json)) | offline schema/L5 validator passed; installed clients pending WA06/WA07 |

### WA02

**Configure and create or adopt source-only Agent Profile bindings**

Users can save selected architecture inputs and pillars, then explicitly create or adopt an Agent Profile without enabling unwanted resource discovery or deleting customer-owned setup.

**Implementation contract:** Profile/source APIs supply authorized candidates; preserve overrides and conditional handoff on refresh. POST binding-operations selects create/link/check with pinned config and current connection. Persist stable setup token, exact native identity and ownership receipt separately from finite runs. Source-only requires aggregationConfiguration=[], qualified execution role, pillars and no unwanted native contexts/scheduled discovery. Readiness distinguishes eligibility, input and authority. Adopt never mutates/deletes foreign or customer-owned setup; serialize profile/source changes with active runs.

**Owned/advisory files:** Existing L5 configuration_population/readiness, api/configuration, api/bindings, models/setup and workers/bindings; proposed adapters/well_architected/binding.py; gateway l5_integration service authorization. Coordinate shared setup/broker owners #8684/#8686 and WA07.

**Prerequisites:** WA01 qualifies native source-only setup/trust and eligibility. Approved selected connection/role/store comes from WA07 under existing authority; no IAM/billing change authorized by this story filing. Catalog IDs remain WA01/coordinator-owned.

| AC ID | Epic criteria | Setup/action | Expected result | Evidence | Phase / owner |
|---|---|---|---|---|---|
| AC-01 | EC-01 | Given two Profile revisions and explicit source/pillar overrides, save and refresh configuration then check readiness. | Overrides and immutable provenance survive, stale ETag fails, scope expansion is not silent, disabled triggers remain disabled and saving makes zero provider calls; only relevant missing fields block readiness. | API/config version readback, Profile handoff conflict/retry receipt and independent provider-call counter. | Before review + deployed API: WA02/WA07 |
| AC-02 | EC-02 | Given owned/adopted profiles, set up/check each; race two project/tenant claims of one native Profile, repeat after unlink, and try foreign sources/wrong roles. | Ready only with exact permitted role and source-only settings. Concurrent cross-project/tenant claims of one native Profile are denied, including after unlink. Denials create no setup; adopted context/settings remain unchanged. | Native Profile/role/source permission readback, cross-scope API denial and source-only capability receipts. | Before review controls; live setup WA02/WA07 |
| AC-03 | EC-02, EC-04 | Lose setup response after native create, restart worker, replay and request owned cleanup alongside an adopted binding. | One owned Profile is reconciled with original identity; cleanup removes only receipt-owned unreferenced resources after terminal safety checks, preserves adopted setup and records partial cleanup debt. | Fault trace, independent create count, native owned absence and adopted presence readback. | Before review + live recovery: WA02/WA07 |
| AC-04 | EC-02, EC-03 | With one admitted generation, attempt concurrent source-role/profile changes and introduce external profile drift. | No active run's native scope is overwritten; second active run/change is fenced, drift suspends new admission and retains pinned readback evidence. | Deterministic transaction/barrier test, pinned profile hash and native drift/readback trace. | Before review + live readback: WA02/WA07 |

| Scenario ID | Acceptance IDs | Case contract | Verification | Test reference | State |
|---|---|---|---|---|---|
| SCN-01 | AC-01 | Given/When/Then: acceptance row AC-01; Controls: C1, isolated scope and exact native/source identity | automatic-integration | `modules/L5-services/tests/well_architected/test_wa02.py::test_ac_01` (planned) | planned |
| SCN-02 | AC-02 | Given/When/Then: acceptance row AC-02; Controls: C1, isolated scope and exact native/source identity | automatic-e2e | `modules/L5-services/tests/well_architected/test_wa02.py::test_ac_02` (planned) | planned |
| SCN-03 | AC-03 | Given/When/Then: acceptance row AC-03; Controls: C1, isolated scope and exact native/source identity | automatic-e2e | `modules/L5-services/tests/well_architected/test_wa02.py::test_ac_03` (planned) | planned |
| SCN-04 | AC-04 | Given/When/Then: acceptance row AC-04; Controls: C1, isolated scope and exact native/source identity | automatic-integration | `modules/L5-services/tests/well_architected/test_wa02.py::test_ac_04` (planned) | planned |

### WA03

**Pin bounded architecture sources with exhaustive coverage and retention**

Every review names the exact IaC, documents and goals sent for analysis, shows exclusions honestly and keeps those sources available while remote work still needs them.

**Implementation contract:** Admission pins source RecordRefs/snapshot hashes, byte/file bounds, goals/context and selected-tree coverage. Deterministic archive/manifest maps every selected artifact and entry to component or explicit exclusion; reject unsafe paths, secrets, oversized or unsupported inputs, never execute IaC or silently truncate. Read back immutable private source bytes and provider consumption evidence. Persist tenant/artifact/run hold until remote terminal proof; revoked access hides reuse but does not trigger destructive deletion. No Profile SQL or new crawler.

**Owned/advisory files:** Existing ProductClient/source_manifest and gateway Product snapshot integration; proposed WA source packaging/provider handoff tests and shared source-retention model/migration. WA03 owns retention semantics; #8684 owns Product source APIs; WA07 integrates shared schema/store changes.

**Prerequisites:** WA01 must qualify additionalContext/file/byte/role/provider readback semantics; WA02 supplies ready source-only binding. Unknown provider ingestion limits block activation, not local packager/denial tests. WA07 supplies authorized private store/fixtures.

| AC ID | Epic criteria | Setup/action | Expected result | Evidence | Phase / owner |
|---|---|---|---|---|---|
| AC-01 | EC-03 | Package mixed IaC/docs/goals at fixed revisions twice, then independently enumerate selected Git trees/doc hashes. | Identical bytes/hashes; every selected tracked path and deployment/build entry has component or exclusion; remote modules/submodules and unsupported formats are visible gaps. A changed/missing path fails comparison. | Independent enumeration diff, deterministic archive/manifest digests and complete selected-scope coverage report. | Before review: WA03; live manifest WA07 |
| AC-02 | EC-02, EC-03 | Try traversal/symlink/duplicate/secret/oversize/foreign-source inputs; for valid input verify private object bytes and provider source receipt. | Unsafe or unauthorized input cannot reach provider; exact pinned bytes reach qualified provider scope. Upload-only or missing provider consumption evidence cannot pass EC-03. | Negative byte fixtures, independent store digest, effective read-policy denial and provider ingestion evidence. | Before review + live source qualification: WA03/WA07 |
| AC-03 | EC-03, EC-04 | Expire local run/snapshot policy while remote state is running/unknown, revoke reader and attempt cleanup; later observe terminal proof. | Source hold prevents destructive expiry while required; unauthorized reuse is denied; only terminal/policy-safe release permits cleanup. Conflicting mandatory retention rejects admission before Start. | Controlled-clock retention test, object presence/absence readback, revoked read denial and hold-release receipt. | Before review + live retention: WA03/WA07 |

| Scenario ID | Acceptance IDs | Case contract | Verification | Test reference | State |
|---|---|---|---|---|---|
| SCN-01 | AC-01 | Given/When/Then: acceptance row AC-01; Controls: C1, isolated scope and exact native/source identity | automatic-integration | `modules/L5-services/tests/well_architected/test_wa03.py::test_ac_01` (planned) | planned |
| SCN-02 | AC-02 | Given/When/Then: acceptance row AC-02; Controls: C1, isolated scope and exact native/source identity | automatic-e2e | `modules/L5-services/tests/well_architected/test_wa03.py::test_ac_02` (planned) | planned |
| SCN-03 | AC-03 | Given/When/Then: acceptance row AC-03; Controls: C1, isolated scope and exact native/source identity | automatic-e2e | `modules/L5-services/tests/well_architected/test_wa03.py::test_ac_03` (planned) | planned |

### WA04

**Run native recommendation generations with durable uncertain-outcome recovery**

An admitted review starts promptly, survives interruption without duplicate paid work, and accurately distinguishes local timeout from a generation still running in AWS.

**Implementation contract:** Commit pinned prepared provider request, stable correlation name/profile/model hash and attempt before Start; disable SDK mutation retries. Immediate after-commit dispatch plus existing bounded SQL recovery handles lost queue sends. Lost Start response reconciles original profile/name/scope identity, never blind retry. Due observation preserves last known native state; unsupported cancellation/local expiry retains source holds and allows later collection. Extend shared terminal-local-state recovery, not just the adapter enum. Native completion starts collection, not a new paid run.

**Owned/advisory files:** Existing admission/dispatcher/work_store, workers/execution and recovery, adapter protocol; proposed adapters/well_architected/generation.py and installed-worker fault tests. WA07 coordinates common worker and PR #9081 changes; retain Security cancellation compatibility.

**Prerequisites:** WA01 safe native identity/no-effect semantics, WA02 ready binding, WA03 provider-readable archive/holds. #8698 release transport/factory and WA07 eligible fixture enable installed live fault proof; absence blocks only dependent phase.

| AC ID | Epic criteria | Setup/action | Expected result | Evidence | Phase / owner |
|---|---|---|---|---|---|
| AC-01 | EC-03, EC-04 | Submit an eligible pinned review through gateway and deployed queue/worker, with observation due later. | Committed intent is dispatched immediately without waiting for sweep; one native generation uses recorded profile/source scope and reaches independently read terminal status/results. | API-to-SQL/SQS/worker/provider correlation, source receipt and native generation/readback timestamps. | Before review controls; live WA04/WA07 |
| AC-02 | EC-04 | Crash after Start but before SQL receipt, lose SQS acknowledgements, expire leases, delay list visibility and present multiple candidates. | At most one Start attempt per uncertain run; unique attributable readback reconciles; zero/multiple candidates stay unknown; stale fences cannot commit; SDK retries do not add native calls. | Independent provider call counter, installed-worker fault journal, SQL attempt/fence readback and native generation list. | Before review + live recovery: WA04/WA07 |
| AC-03 | EC-04 | Cancel/expire locally during running generation, deny or fail status reads, exhaust observation limit and later restore authorized readback of completion. | No false remote stop/failure; last successful state/freshness and bounded blocked status persist; sources retained; later result is collected from original generation with zero resubmission. | Clock-controlled worker trace plus native status, retained-source and late-result readbacks. | Before review + live recovery: WA04/WA07 |
| AC-04 | EC-04, EC-05 | Fail collection after a page/SQL write and restart after provider terminal completion. | Collection resumes by durable cursor/identity; one result version is committed, wrong-generation data rejected, and Start is never called again. | Page/cursor fixture, result uniqueness readback and independent native call-count trace. | Before review + live collection: WA04/WA07 |

| Scenario ID | Acceptance IDs | Case contract | Verification | Test reference | State |
|---|---|---|---|---|---|
| SCN-01 | AC-01 | Given/When/Then: acceptance row AC-01; Controls: C1, isolated scope and exact native/source identity | automatic-e2e | `modules/L5-services/tests/well_architected/test_wa04.py::test_ac_01` | local fixture passed; installed gateway/worker/native proof remains WA04/WA07 |
| SCN-02 | AC-02 | Given/When/Then: acceptance row AC-02; Controls: C1, isolated scope and exact native/source identity | automatic-e2e | `modules/L5-services/tests/well_architected/test_wa04.py::test_ac_02` | local lost-response, ambiguous-readback, lease and replay fixtures passed; installed worker proof remains WA04/WA07 |
| SCN-03 | AC-03 | Given/When/Then: acceptance row AC-03; Controls: C1, isolated scope and exact native/source identity | automatic-e2e | `modules/L5-services/tests/well_architected/test_wa04.py::test_ac_03` | local expiry, denied readback, capped polling and restored result passed; deployed proof remains WA04/WA07 |
| SCN-04 | AC-04 | Given/When/Then: acceptance row AC-04; Controls: C1, isolated scope and exact native/source identity | automatic-integration | `modules/L5-services/tests/well_architected/test_wa04.py::test_ac_04` | local wrong-generation, page and SQL-write recovery passed; deployed proof remains WA04/WA07 |

### WA05

**Publish attributable recommendations and prove later entitled design reuse**

Users receive source-backed recommendations with honest limitations, and a later authorized design task can use the same version even after owned provider setup is cleaned up.

**Implementation contract:** Paginate profile recommendations, Get each and filter exact generationId; preserve native IDs/raw private artifacts and strict source-only result variant. Independent source/goals comparison records supported/contradicted/not_verifiable/not_checked with real citations. Scope-compatible comparisons only. Durable per-result/version/destination/schema publication keys recover lost acknowledgement without paid reruns. Current source visibility and revocation watermark govern artifacts/query/counts and later reuse.

**Owned/advisory files:** Existing result/findings/triage APIs/models; proposed WA recommendation normalizer/comparison tests; shared SA11 publication worker/models and Context/Insights adapters, coordinated with #8693/#8149. Do not reuse DevOps space-knowledge rows or orchestration tracker outbox as a new result sink.

**Prerequisites:** WA04 native results and WA03 manifest; shared SA11 #8693/Context #8149/Insights owners must expose admitted schema, receipt readback and denied-reuse behavior. Unsupported consumers block publication/EC-07, not result persistence/comparison work. WA07 integrates live later-task proof.

| AC ID | Epic criteria | Setup/action | Expected result | Evidence | Phase / owner |
|---|---|---|---|---|---|
| AC-01 | EC-05 | Collect paginated native recommendations with mixed generation IDs, missing citations and known supported/contradicted fixture claims. | Only attributable generation results enter accepted output; unsupported/missing source evidence is labeled, not invented. Independent verifier compares substantive recommendations against pinned IaC/goals; source-only makes no deployed-state claim. | Native page/get receipts, normalized version and independent claim-to-source comparison with deliberately wrong-case failures. | Before review + native comparison: WA05/WA07 |
| AC-02 | EC-05, EC-07 | Persist result, perform safe owned cleanup, then fail/replay destination publication and lose its acknowledgement. | Results survive cleanup; each Context/Insights destination eventually has one durable attributable receipt or visible blocked debt; replay reads existing admission and never invokes provider Start. | Post-cleanup result readback, consumer receipt/recovery and provider call-count trace. | Before review + live consumer proof: WA05/WA07 |
| AC-03 | EC-07 | A distinct later entitled design task queries/uses the versioned review; a foreign actor and subsequently revoked source actor try the same result/query/artifact. | Later task cites/uses the admitted result revision with retrieval receipt; denied readers receive no derived content or private counts. Upload-only evidence cannot pass. | Context and Insights consumer receipts, later task retrieval/use trace and controlled deny/revocation readback. | Live: WA05/WA07; acceptance PranavSharma1000 |
| AC-04 | EC-05 | Compare result versions with identical versus changed source/pillar/goal coverage; replay triage from another Product. | Comparable scope yields attributable diff; incompatible scope returns not_comparable reasons; foreign triage/results are denied without scope leakage. | API comparison/triage tests with independent expected scope identities and pagination/count denial checks. | Before review + deployed API: WA05/WA07 |

| Scenario ID | Acceptance IDs | Case contract | Verification | Test reference | State |
|---|---|---|---|---|---|
| SCN-01 | AC-01 | Given/When/Then: acceptance row AC-01; Controls: C1, isolated scope and exact native/source identity | automatic-integration | `modules/L5-services/tests/well_architected/test_wa05.py::test_ac_01` (planned) | planned |
| SCN-02 | AC-02 | Given/When/Then: acceptance row AC-02; Controls: C1, isolated scope and exact native/source identity | automatic-e2e | `modules/L5-services/tests/well_architected/test_wa05.py::test_ac_02` (planned) | planned |
| SCN-03 | AC-03 | Given/When/Then: acceptance row AC-03; Controls: C1, isolated scope and exact native/source identity | automatic-e2e | `modules/L5-services/tests/well_architected/test_wa05.py::test_ac_03` (planned) | planned |
| SCN-04 | AC-04 | Given/When/Then: acceptance row AC-04; Controls: C1, isolated scope and exact native/source identity | automatic-integration | `modules/L5-services/tests/well_architected/test_wa05.py::test_ac_04` (planned) | planned |

### WA06

**Expose shared UI CLI MCP reviews and opt-in scheduled admission**

Humans and agents can configure, check, run and read the same review through their preferred client; recurring work happens only when explicitly enabled and admitted.

**Implementation contract:** Product Services tab and shared adp service/MCP matrix invoke one Product-scoped L5 API. Show inherited configuration, exact source/pillars/readiness, setup receipt, local/provider/freshness state, coverage/results and publication debt. Preserve shared conditional-write/error/JSON receipt semantics. Extend Security-only event service discrimination with scope-bound event/tick deduplication; enabled schedules use common manual admission and current budget/delegation. No native weekly discovery or browser federation copy.

**Owned/advisory files:** Existing gateway Product frontend, installed cli/adp packaging and MCP/tool catalog transport; shared contracts operation matrix and events/schedules receiver. Proposed Well-Architected service tab and shared adp service/MCP implementation; coordinate SA12–SA14 #8694/#8695/#8696 and WA07. No second public API or scheduler.

**Prerequisites:** WA01 typed client contracts plus WA04/WA05 run/results; shared client surfaces may still be unimplemented. WA07 supplies served frontend, installed CLI, authenticated MCP and deployed schedule fixture. Missing live eligibility blocks live journeys, not client error-state development.

| AC ID | Epic criteria | Setup/action | Expected result | Evidence | Phase / owner |
|---|---|---|---|---|---|
| AC-01 | EC-06 | Use served UI, installed CLI and authenticated MCP to save/check/run/read the same eligible Product review and replay its mutation key. | Each client sees the same config/binding/run/result revision and errors; one replayed intent, no hidden native calls. Foreign-project requests are denied consistently. | Three real client journeys, HTTP receipts and independent API/SQL/provider identity readback. | Before review client tests; live WA06/WA07 |
| AC-02 | EC-06 | Leave schedule disabled, then explicitly enable one; replay equivalent ticks/events and revoke current authority or budget. | Disabled produces zero runs; enabled deduplicates through common admission; revoked/over-budget request creates no native generation. Neither setup nor schedule enables AWS discovery. | Controlled-clock event replay/SQL receipts, provider count and native profile/settings readback. | Before review + deployed schedule: WA06/WA07 |
| AC-03 | EC-04, EC-06 | Expose unknown/expired/cancel-unsupported, stale monitoring, not-comparable result and publication-blocked states through all clients. | No client labels local timeout as confirmed AWS cancellation or source-only findings as deployment verification; JSON/exit/error semantics match shared operation matrix. | Installed-client state/exit assertions and UI/MCP output linked to authoritative API response. | Before review + installed clients: WA06/WA07 |

| Scenario ID | Acceptance IDs | Case contract | Verification | Test reference | State |
|---|---|---|---|---|---|
| SCN-01 | AC-01 | Given/When/Then: acceptance row AC-01; Controls: C1, isolated scope and exact native/source identity | automatic-e2e | `modules/L5-services/tests/well_architected/test_wa06.py::test_ac_01` (planned) | planned |
| SCN-02 | AC-02 | Given/When/Then: acceptance row AC-02; Controls: C1, isolated scope and exact native/source identity | automatic-e2e | `modules/L5-services/tests/well_architected/test_wa06.py::test_ac_02` (planned) | planned |
| SCN-03 | AC-03 | Given/When/Then: acceptance row AC-03; Controls: C1, isolated scope and exact native/source identity | automatic-integration | `modules/L5-services/tests/well_architected/test_wa06.py::test_ac_03` (planned) | planned |

### WA07

**Integrate compatible release and prove portable live recovery reuse and cleanup**

Two distinct onboarded projects can use one released service safely, with real native recommendations, recoverable work, reusable results and verified owned-only cleanup.

**Implementation contract:** Integrate contracts/schema/retention, scoped broker/provider roles, private SQS worker factory and recovery, gateway/front-end/CLI/MCP and consumer publication in compatible batches. Retain normal gateway/frontend auto-deploys and one coordinated L5 worker/infra release per batch. Additive migrations preserve old service rows; rollback disables admissions but retains unknown native work and recoverable results. Verify exact installed artifacts and two non-ADP architectures/goals without code edits or redeployment. Own integration fixes and EC-to-AC evidence closure.

**Owned/advisory files:** Shared L5 package/SDK lock, migrations, worker factory/infra/tests/live and service runbook; gateway/CodeBuild/platform release inputs and workflow triggers, coordinated with #8698 PR #8932. Own cross-story/common-host integration rather than overwriting sibling designs. File lists are advisory.

**Prerequisites:** Eligible private fixtures, selected account/exact role and existing policy/budget must be supplied by WA07 with PranavSharma1000 coordination. Shared release #8698/PR #8932, Context/Insights receipt/reuse and catalog readiness are unverified. This issue authorizes no deployment, Support purchase, resource/budget expansion or new human checkpoint by filing.

| AC ID | Epic criteria | Setup/action | Expected result | Evidence | Phase / owner |
|---|---|---|---|---|---|
| AC-01 | EC-01, EC-06, EC-08 | Release one compatible package/worker/schema/client set; inspect actual main filters, migration/infra ordering, worker factory and rollback with prior service rows. | Installed release identities agree; gateway/frontend normal triggers preserved; private workers/queue repair work; old Security/DevOps rows and callers remain compatible, rollback retains recovery records. No public Lambda URL. | Release/workflow/installed-probe receipts, migration compatibility/rollback and effective least-privilege transport readback. | Before review plans/checks; authorized deployment WA07 |
| AC-02 | EC-01, EC-03, EC-05, EC-08 | Run native reviews for two non-ADP projects with distinct IaC/goals using configuration only and the exact same release artifacts. | Both consume admitted source/context and produce attributable recommendations independently compared to goals; artifact digests unchanged and no platform code edit/redeployment between onboardings. ADP-only or mock evidence fails. | Two provider/source/result/comparison receipts, private fixture manifests, exact release digests and configuration-change trace. | Live: WA07; acceptance PranavSharma1000 |
| AC-03 | EC-02, EC-04, EC-08 | Exercise foreign tenant/sibling project/source and wrong-role denial, lost Start response/restart, local timeout and owned/adopted cleanup on eligible fixtures. | No unauthorized provider effect or duplicate generation; remote unknown/running sources retained, adopted resources unchanged; owned terminal cleanup independently absent and results retained. | Installed-worker fault/deny trace, native ID/call count, source presence, owned absence and adopted/result presence readback. | Live: WA07 |
| AC-04 | EC-05, EC-06, EC-07 | Complete all client journeys, acknowledge Context/Insights publication and run a later distinct entitled design task plus denied reuse case. | Same result revision is usable across clients and later task, both consumers acknowledge, denied actor cannot retrieve derived content; cleanup does not erase accepted result. | Client/API parity, consumer receipts, later retrieval/use and denied-reuse trace at release identity. | Live: WA07 with WA05/WA06 and peer owners |
| AC-05 | EC-01, EC-02, EC-03, EC-04, EC-05, EC-06, EC-07, EC-08 | Reconcile every epic/story criterion and resource disposition at tested revisions after the live batch. | EC-to-AC/scenario evidence complete or explicitly fail/blocked/not-run; costs distinguish ADP accounting, provider/ancillary charges and unknowns; no renewed allowance. Required blocked rows keep epic incomplete. | Versioned private acceptance/cost/cleanup ledger and sanitized public summary with owners and remaining phase-specific blockers. | Live handoff: WA07; PranavSharma1000 accepts |

| Scenario ID | Acceptance IDs | Case contract | Verification | Test reference | State |
|---|---|---|---|---|---|
| SCN-01 | AC-01 | Given/When/Then: acceptance row AC-01; Controls: C1, isolated scope and exact native/source identity | automatic-e2e | `modules/L5-services/tests/well_architected/test_wa07.py::test_ac_01` (planned) | planned |
| SCN-02 | AC-02 | Given/When/Then: acceptance row AC-02; Controls: C1, isolated scope and exact native/source identity | automatic-e2e | `modules/L5-services/tests/well_architected/test_wa07.py::test_ac_02` (planned) | planned |
| SCN-03 | AC-03 | Given/When/Then: acceptance row AC-03; Controls: C1, isolated scope and exact native/source identity | automatic-e2e | `modules/L5-services/tests/well_architected/test_wa07.py::test_ac_03` (planned) | planned |
| SCN-04 | AC-04 | Given/When/Then: acceptance row AC-04; Controls: C1, isolated scope and exact native/source identity | automatic-e2e | `modules/L5-services/tests/well_architected/test_wa07.py::test_ac_04` (planned) | planned |
| SCN-05 | AC-05 | Given/When/Then: acceptance row AC-05; Controls: C1, isolated scope and exact native/source identity | manual | [manual reconciliation steps](#manual-scn-05-acceptance-and-cleanup-reconciliation) | manual/not-run |

## Epic-to-story consistency

| Epic #9078 criterion | Required story assertions (not a replacement for live integration) | Parent #8150 contribution |
|---|---|---|
| EC-01 | WA01/AC-01, WA01/AC-02, WA01/AC-03, WA02/AC-01, WA07/AC-01, WA07/AC-02, WA07/AC-05 | EC-01 (Well-Architected evidence only) |
| EC-02 | WA02/AC-02, WA02/AC-03, WA02/AC-04, WA03/AC-02, WA07/AC-03, WA07/AC-05 | EC-02 (Well-Architected evidence only) |
| EC-03 | WA02/AC-04, WA03/AC-01, WA03/AC-02, WA03/AC-03, WA04/AC-01, WA07/AC-02, WA07/AC-05 | EC-03 (Well-Architected evidence only) |
| EC-04 | WA01/AC-02, WA02/AC-03, WA03/AC-03, WA04/AC-01, WA04/AC-02, WA04/AC-03, WA04/AC-04, WA06/AC-03, WA07/AC-03, WA07/AC-05 | EC-04 (Well-Architected evidence only) |
| EC-05 | WA04/AC-04, WA05/AC-01, WA05/AC-02, WA05/AC-04, WA07/AC-02, WA07/AC-04, WA07/AC-05 | EC-03/EC-04 (Well-Architected evidence only) |
| EC-06 | WA01/AC-03, WA06/AC-01, WA06/AC-02, WA06/AC-03, WA07/AC-01, WA07/AC-04, WA07/AC-05 | EC-03/EC-06 (Well-Architected evidence only) |
| EC-07 | WA05/AC-02, WA05/AC-03, WA07/AC-04, WA07/AC-05 | EC-05/EC-06 (Well-Architected evidence only) |
| EC-08 | WA07/AC-01, WA07/AC-02, WA07/AC-03, WA07/AC-05 | EC-03/EC-04/EC-06 (Well-Architected evidence only) |

Shared parent EC IDs and sibling content remain untouched. Security or DevOps
acceptance cannot establish this service's native generation/results/reuse.

## Qualification procedure

WA01's [versioned manual procedure](qualification.md#model-and-documentation-baseline) must start with the qualified SDK/model/docs
revision and admitted private fixture account/exact role/region. Through the
selected broker connection, verify identity and read provider eligibility/profile
settings without creation; distinguish denied/unknown/ineligible. For explicitly
authorized eligible setup only, record exact source-only roles/aggregation,
additionalContext format, byte/file limits and native consumption/readback semantics.
Run no Support purchase, discovery, remediation or unrequested setup. Qualification
of uncertain Start must preserve original correlation identity and disable SDK
retries. Record no-token/no-stop limitations and eventual-read ambiguity; never
rerun paid work simply to produce a green acceptance row. WA07 independently
checks setup/resources/cost dispositions. Private values and raw receipts remain
in an authorized evidence store, with sanitized immutable references in the report.
This is a procedure to implement/qualify, not a command already run by this design.

## Release evidence procedure

WA07 pins release/package/worker/schema/client digests and reconciles actual merged
shared dependencies and triggers. Apply the existing deployment authority and
canonical playbook, not instructions inferred from this backlog. Verify private
worker transport/factory, migration compatibility, per-target least privilege and
no public Lambda URL. Execute two non-ADP source-only reviews and fault/denial/client/
publication/later-reuse cases at the same artifacts. Independently read provider
identity/status/source/results, SQL receipts, consumer acknowledgements and owned
absence/adopted presence after cleanup. Record unknown remote work and retained
sources/cost exposure separately. Map every EC/AC/SCN to pass/fail/blocked/not-run,
exact tested revision and evidence. A required blocked/not-run row is incomplete;
PranavSharma1000 accepts only the documented boundary. Run no budget reset or
unrequested recurring work. This outline is not evidence of live execution.

### Manual SCN-05 acceptance and cleanup reconciliation

This case is a manual handoff, not a selector or permission to deploy, start paid
work, expand IAM, or delete resources. WA07 performs these steps only after the
release owner confirms the authorized target and budget, eligible private
fixtures, and the exact selected connection, account **and role**. Before each
target operation, use the selected connection; first read identity through
`adp-cred assume --service aws --label LABEL --exec aws sts get-caller-identity`
and reject an account or role mismatch. `LABEL` is the approved private
connection label, not a value to guess. Broker failure, an unverified
target or missing fixture stops dependent live work; keep those rows blocked or
not-run and continue only independent pre-review checks. Never substitute pod or
ambient credentials. Follow the canonical deployment guide for release authority.

1. Pin the candidate Git commit and verify the installed gateway, worker,
   migrations and client artifacts against the release receipts **before** any
   live case. Record exact digests, installed revision, selected-role identity,
   source/fixture revisions, time and private readback references outside this
   repository. A local build, a supplied receipt or a matching account alone
   cannot establish installed identity.
2. Reconcile AC-01 through AC-04 and SCN-01 through SCN-04 from the installed
   traces and independent native, SQL, client and consumer readbacks. Check two
   isolated eligible non-ADP projects with different goals and unchanged release
   digests; compare native IDs, source and recommendations. For denial and lost
   Start cases, retain the original intent and native call-count evidence. Do
   not retry uncertain paid work, infer absence from a denied read, or clean up
   unknown, running or adopted remote resources.
3. After authorized terminal cleanup, independently read back owned absence,
   adopted Profile and immutable source presence, and the retained accepted
   result. Keep a private disposition entry per resource: ownership, state,
   owner alias, evidence reference and, for any removed **owned** resource,
   terminal, absence and retained-result references. Record unknown disposition
   explicitly rather than guessing removal.
4. In a private ledger **outside the repository**, set `schema_version=1`,
   `candidate_revision`, `installed_revision` and `installed_readback_ref`
   (both installed fields null until independently verified), plus `rows`,
   `resources`, `costs` and `allowance_renewed`. Revisions are 40 lowercase
   hexadecimal digits; the installed and candidate revisions must match. List
   every EC, WA01–WA07 acceptance and paired scenario ID from
   `story-specs.json`. Start unexecuted rows as `not_run` with
   `blocker_code=not_executed`; record `blocked` with a
   specific blocker, `fail` with a revision and private failure reference, or
   `pass` only after independent evidence at the candidate revision. Set
   `tested_revision`, `evidence_ref`, `phase` (`before_review`, `installed` or
   `handoff`) and `owner_alias` per row. WA07 installed cases must match the
   verified installed revision; WA07 AC-05/SCN-05 are handoff-phase rows. Epic
   rows cannot pass while mapped children remain incomplete. Name unresolved
   owners and phase-specific blockers rather than upgrading them to pass.
   The [fictional ledger test fixture](../../../modules/L5-services/tests/well_architected/test_wa07_acceptance.py)
   shows the accepted JSON field shape; never replace its markers with private
   data inside this repository.
5. Reconcile `adp_accounting`, `provider_charges`, `ancillary_charges` and
   `unattributed` costs separately. A priced receipt permits `known`, including
   a receipted `0.00`; otherwise use `unknown` with a reason, never invented
   zero. Record `allowance_renewed=false` only if no allowance was renewed;
   stop and escalate any contrary fact. Preserve private provider charges and
   resource identities outside public documentation.
6. From `modules/L5-services/`, with the authorized private JSON ledger path
   substituted for `PRIVATE_LEDGER`, run
   `uv run --no-sync python scripts/wa07-acceptance.py --private-ledger PRIVATE_LEDGER`.
   Check the output contains only expected status, cost/resource states and
   owner/blocker aliases. An incomplete report is the correct outcome when
   required live rows lack evidence. The checker validates the ledger's shape;
   it cannot authenticate supplied receipts or grant acceptance. Send the
   sanitized summary and private evidence references through the authorized
   handoff to PranavSharma1000 for the final decision; retain every blocked
   item with its next owner. Until this actually runs, SCN-05 is manual/not-run.
