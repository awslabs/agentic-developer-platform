# ADP master module, feature and test-scenario register

## Stored test identities and module selection

The [versioned test registry](../../tests/regression/catalog.json) stores the module, feature, scenario and test IDs used by executable selection and report tags. See [run tests by module](module-selection.md) for storage paths, inputs, supported runners and gap handling. This dated audit records confirmed mappings; tagging a test does not establish full scenario coverage.

**Authoritative audit register: 48 logical modules, 133 features, 629 individually identified scenarios.** Created 5 October 2026. Source baseline: [main `169d361604`](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256).

This file consolidates the reviewed module inventory, features, required test scenarios, existing test evidence, UI/API/flag mappings, edge transports, CLI cases and inspected execution results. It supersedes the split reports as the master register. It covers every feature in the current 133-row source audit; it does not promise exhaustive combinations of inputs, roles, models or future functionality. Uncommitted working-branch changes are outside the pinned source baseline.

**No product or live tests were executed to create this register.** Scenarios describe required acceptance behavior, including gaps and roadmap capabilities. A scenario's presence does not mean its implementation or test exists. Existing harnesses, local tests, historical evidence, known blocks and unmapped scenarios are distinguished below. No scenario is labelled currently passing.

## ID rules and use

| Entity | ID format | Example |
| --- | --- | --- |
| Logical module | `MOD-nnn` | `MOD-001` — Agent Models |
| Feature within module | `MOD-nnn-Fnnn` | `MOD-001-F002` — personal model preference |
| Scenario within feature | `MOD-nnn-Fnnn-Snnn` | `MOD-001-F002-S003` — stale revision conflict |
| Test/evidence source | `EV-nnn` | Evidence catalogue entry |
| Counted test/case/check | `TEST-nnn` | Exact definition in the counted catalogue |

IDs are permanent from this register onward: append new numbers, never renumber for display order, and retain removed IDs as retired. A feature has one owning module; other surfaces cross-reference it. Existing audit aliases (`AM-*`, `ORG-*`, `BACK-*`) and native issue/requirement IDs are preserved per feature. Those aliases are not extra features. Existing CLI `E/C/D` identifiers remain test-case IDs; they do not replace scenario IDs.

Every scenario has an action/setup context, an observable expected result and a coverage/evidence mapping. Unless stated otherwise, use an owned disposable fixture, record the source/deployed revision, and restore mutations afterward. Browser scenarios use real authenticated UI/API boundaries for E2E qualification; fixtures/mocks only establish local behavior. Live mutation/provider fixtures require the suite's existing authorization and recovery contract; this document does not run or authorize deployments.

## Coverage legend

| Scenario status | Meaning | Count |
| --- | --- | --- |
| PARTIAL-HARNESS | A live-capable or mixed harness is mapped, with its exact scope/limitations stated. It is not a current pass or proof of the entire scenario. | 100 |
| LOCAL-ONLY | A mapped component/in-process/fixture check supports some or all behavior, without the full deployed boundary. | 19 |
| HISTORICAL | Related retained qualification exists; fresh scenario-level execution is not established. | 16 |
| BLOCKED | A known prerequisite/capability prevents the corresponding feature qualification. | 12 |
| MISSING-DRIVER | A named acceptance journey lacks its executable driver. | 20 |
| PLACEHOLDER | Related tests skip/xfail or lack the required acceptance assertion. | 12 |
| UNMAPPED | No exact scenario-level E2E mapping established by this audit. Tests may exist in the feature evidence; do not equate this with proven absence of all tests. | 450 |

Counts are register/mapping counts, not a feature coverage percentage. “Feature coverage” below preserves the earlier broader audit assessment; the scenario status is the narrower claim. No inherited feature label automatically qualifies a scenario. A directory reference is discovery evidence; it is not an executable test selector.

## Test counts per scenario

**These are confirmed mapped counts, not exhaustive totals of every test that may exist.** Each of the 629 scenario rows now shows its mapped implemented test count, split into E2E-capable and local definitions, and links to exact counted cases below. The exhaustive existing count remains **unknown** until scenario-to-test mapping is completed. A zero means **zero mapped implemented tests**, not proof that the repository contains none.

| Count | Verified inventory |
| --- | --- |
| Scenarios with at least one mapped implemented test | 110 |
| Scenarios without a mapped implemented test count | 519 |
| Distinct mapped E2E-capable definitions/cases | 68 |
| Distinct mapped local definitions | 18 |
| Distinct blocked definitions/cases (excluded above) | 2 |
| Distinct placeholder definitions/cases (excluded above) | 9 |
| Script-level checks (separate from tests) | 2 |

A Python/Playwright source test definition counts once, even when parameterized or generated across multiple devices/fixtures. A registered executable CLI case or named shell acceptance case counts once; assertions, helper functions, setup and cleanup do not add tests. One test can support several scenarios, so **do not sum scenario counts to obtain a suite total**. The catalogue deduplicates source identity. Conditional live tests may still skip when fixtures are absent; existence does not establish a run or complete assertion coverage.

Source review while counting found two important limits: the mapped KEDA file has no scale-down test, so that scenario is now unmapped; the new-repository ingestion/search test does not assert that search results are nonempty. Budget cap cases seed exhausted balances; real spend-through qualification remains separate from their denial assertions.

## Shared fixture and completion rules

| Area | Required setup / completion evidence |
| --- | --- |
| Identity/authorization | Ordinary member, org admin and platform admin where applicable; two owned tenants for isolation; legitimate and revoked/expired credentials. |
| UI | Feature enabled/disabled as applicable; correct role/workspace; real page-initiated API responses, reload/readback and actionable failure behavior. |
| Mutations | Owned resources, current revisions, stale/conflicting inputs, durable readback, cleanup and restoration. Do not use shared production App/default changes as casual smoke tests. |
| Provider/model execution | Approved provider fixture, bounded spend/resources, actual provider/model evidence and settled/cleaned terminal outcome. Unknown evidence stays unknown. |
| Recovery | Inject the declared fault at a recorded boundary; verify idempotency, lease/authority expiry and absence of duplicate external actions. |
| Passing acceptance | Identify scenario ID, commit/deployment, executable selector, fixtures and expected assertions; attach observed result and cleanup evidence. A skip, xfail, NOT_RUN, preview or retained-report verification cannot substitute for fresh live acceptance. |

## Module index

| Module ID | Module | Features | Scenarios |
| --- | --- | --- | --- |
| [MOD-001](#mod-001) | Agent Models | 7 | 34 |
| [MOD-002](#mod-002) | Organizations | 10 | 49 |
| [MOD-003](#mod-003) | Agents and service accounts | 3 | 15 |
| [MOD-004](#mod-004) | Task policies | 1 | 6 |
| [MOD-005](#mod-005) | Identity and access | 6 | 30 |
| [MOD-006](#mod-006) | GitHub integration | 4 | 18 |
| [MOD-007](#mod-007) | Credentials and AWS connections | 3 | 15 |
| [MOD-008](#mod-008) | GitLab integration | 3 | 13 |
| [MOD-009](#mod-009) | Model access and routing | 2 | 10 |
| [MOD-010](#mod-010) | Budgets and spend | 4 | 22 |
| [MOD-011](#mod-011) | Rate limits | 2 | 9 |
| [MOD-012](#mod-012) | Dashboards | 2 | 10 |
| [MOD-013](#mod-013) | Logs and audit | 1 | 5 |
| [MOD-014](#mod-014) | Usage and cost reporting | 1 | 5 |
| [MOD-015](#mod-015) | Agent activity and controls | 2 | 11 |
| [MOD-016](#mod-016) | Chat | 3 | 17 |
| [MOD-017](#mod-017) | Knowledge and indexing | 3 | 16 |
| [MOD-018](#mod-018) | Delivery flows | 11 | 51 |
| [MOD-019](#mod-019) | Task API | 6 | 28 |
| [MOD-020](#mod-020) | Domain MRI | 1 | 6 |
| [MOD-021](#mod-021) | Agent identity and internal services | 4 | 20 |
| [MOD-022](#mod-022) | Model gateway | 1 | 6 |
| [MOD-023](#mod-023) | Navigation and feature gates | 1 | 6 |
| [MOD-024](#mod-024) | CLI distribution and setup | 3 | 13 |
| [MOD-025](#mod-025) | Service health | 1 | 4 |
| [MOD-026](#mod-026) | Superplane | 11 | 58 |
| [MOD-027](#mod-027) | Agent Context | 4 | 17 |
| [MOD-028](#mod-028) | Model gateway protocols | 1 | 4 |
| [MOD-029](#mod-029) | Multi-deployment sessions | 1 | 4 |
| [MOD-030](#mod-030) | Usage, costs and pricing | 1 | 4 |
| [MOD-031](#mod-031) | Assistant and durable sessions | 5 | 20 |
| [MOD-032](#mod-032) | Hosted coding and runtimes | 3 | 12 |
| [MOD-033](#mod-033) | Other channel adapters | 1 | 4 |
| [MOD-034](#mod-034) | Vulnerability remediation | 1 | 4 |
| [MOD-035](#mod-035) | Personal memory | 1 | 4 |
| [MOD-036](#mod-036) | Artifacts | 1 | 4 |
| [MOD-037](#mod-037) | Research / gbrain | 2 | 8 |
| [MOD-038](#mod-038) | Shared tools and Task SDK | 2 | 8 |
| [MOD-039](#mod-039) | Validation service | 1 | 5 |
| [MOD-040](#mod-040) | Harness jobs | 2 | 8 |
| [MOD-041](#mod-041) | Human approvals / HITL | 1 | 4 |
| [MOD-042](#mod-042) | Provenance and audit | 1 | 4 |
| [MOD-043](#mod-043) | Security scanning | 2 | 8 |
| [MOD-044](#mod-044) | Cyber investigations | 2 | 9 |
| [MOD-045](#mod-045) | Platform deployment verification | 1 | 4 |
| [MOD-046](#mod-046) | Platform release and upgrades | 2 | 9 |
| [MOD-047](#mod-047) | Observability and performance | 1 | 4 |
| [MOD-048](#mod-048) | User-service and harness roadmap | 1 | 4 |

## Modules, features and scenarios

<a id="mod-001"></a>

### MOD-001 — Agent Models

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-001-F001](#mod-001-f001) | Persona/model catalogue, availability, compatibility and prices | AM-01 | #5420; #5422; #5636 |
| [MOD-001-F002](#mod-001-f002) | Personal model preference: read, save, reset, stale revision conflict | AM-02 | #5419; #5422 |
| [MOD-001-F003](#mod-001-f003) | Managed service-account catalogue and model preferences | AM-03 | #5419; #5422 |
| [MOD-001-F004](#mod-001-f004) | Platform persona defaults: select model, reason and revision-safe save | AM-04 | Not found; master ID is canonical |
| [MOD-001-F005](#mod-001-f005) | Compatibility-class defaults, preview, posture revision/history/rollback | AM-05 | #5425; PMM-07 |
| [MOD-001-F006](#mod-001-f006) | Model probes, evidence persistence, runtime model selection | AM-06 | #5420; PMM-03 |
| [MOD-001-F007](#mod-001-f007) | Task budget compatibility and reservation preview | AM-07 | Not found; master ID is canonical |

<a id="mod-001-f001"></a>

#### MOD-001-F001 — Persona/model catalogue, availability, compatibility and prices

Module: [MOD-001](#mod-001). Previous audit ID: `AM-01`. Native references: #5420; #5422; #5636.

Surface: /settings/agent-models. **Feature coverage: Partial E2E.** E38 reads architect catalogue/costs; UI availability states use mocked service clients. No all-persona browser acceptance.

Related feature evidence: [EV-001](#ev-001), [EV-002](#ev-002), [EV-003](#ev-003). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-001-f001-s001"></a>`MOD-001-F001-S001` | Read catalogue for every exposed persona | Persona identity, compatibility, selectability and effective default are returned consistently. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-087](#test-087) | [EV-002](#ev-002) [model_policy](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py#L431) — E38 reads architect catalogue and cost shape; it does not cover every persona. |
| <a id="mod-001-f001-s002"></a>`MOD-001-F001-S002` | Open model prices with missing or partial prices | Unknown prices remain unknown; UI does not display zero cost as a substitute. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-087](#test-087) | [EV-002](#ev-002) [model_policy](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py#L431) — E38 preserves cost certainty/readback; full UI model-price behavior remains unmapped. |
| <a id="mod-001-f001-s003"></a>`MOD-001-F001-S003` | Inspect stale, retired, disallowed and incompatible models | Each reason is visible and unavailable choices cannot be saved. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-001-f001-s004"></a>`MOD-001-F001-S004` | Change workspace while catalogue request is pending | Only the newly selected workspace's models and costs are displayed. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-001-f002"></a>

#### MOD-001-F002 — Personal model preference: read, save, reset, stale revision conflict

Module: [MOD-001](#mod-001). Previous audit ID: `AM-02`. Native references: #5419; #5422.

Surface: /settings/agent-models. **Feature coverage: Local only.** Component and in-process API tests; no browser save → new run uses chosen model → reset journey found.

Related feature evidence: [EV-001](#ev-001), [EV-004](#ev-004), [EV-005](#ev-005). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-001-f002-s001"></a>`MOD-001-F002-S001` | Save a personal preference then reload | The selected model and revision persist for the authenticated person. | LOCAL-ONLY | **1** (E2E 0, local 1); existing total unknown | [TEST-034](#test-034) | [EV-004](#ev-004) [test_ac02_save_and_list](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py#L421) — In-process API save/read; browser persistence remains unqualified. |
| <a id="mod-001-f002-s002"></a>`MOD-001-F002-S002` | Reset a saved preference | Effective selection returns to the applicable default and survives reload. | LOCAL-ONLY | **3** (E2E 0, local 3); existing total unknown | [TEST-037](#test-037), [TEST-042](#test-042), [TEST-043](#test-043) | [EV-004](#ev-004) [test_ac08_reset_and_audit_survives](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py#L864) — API reset and audit preservation; not a real worker/model round trip. |
| <a id="mod-001-f002-s003"></a>`MOD-001-F002-S003` | Submit a preference with a stale revision | Conflict is reported without overwriting the newer choice. | LOCAL-ONLY | **1** (E2E 0, local 1); existing total unknown | [TEST-036](#test-036) | [EV-004](#ev-004) [test_ac06_stale_revision_conflict](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py#L623) — API rejects stale preference writes. |
| <a id="mod-001-f002-s004"></a>`MOD-001-F002-S004` | Inject another person's identity into the self request | The request cannot change another principal's preference. | LOCAL-ONLY | **1** (E2E 0, local 1); existing total unknown | [TEST-035](#test-035) | [EV-004](#ev-004) [test_ac04_self_endpoint_ignores_injected_target](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py#L531) — Injected target does not alter self scope. |
| <a id="mod-001-f002-s005"></a>`MOD-001-F002-S005` | Start a real run after saving and after resetting | Both runs use the corresponding server-selected model and retain selection evidence. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-001-f003"></a>

#### MOD-001-F003 — Managed service-account catalogue and model preferences

Module: [MOD-001](#mod-001). Previous audit ID: `AM-03`. Native references: #5419; #5422.

Surface: /settings/agent-models. **Feature coverage: Local only.** Self/admin separation and permission tests exist; no real managed-agent model-change journey found.

Related feature evidence: [EV-001](#ev-001), [EV-006](#ev-006). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-001-f003-s001"></a>`MOD-001-F003-S001` | Select a manageable service account and load preferences | Catalogue and preferences belong to that canonical principal. | LOCAL-ONLY | **1** (E2E 0, local 1); existing total unknown | [TEST-041](#test-041) | [EV-004](#ev-004) [test_ac10_managed_catalogue_uses_target_service_principal](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py#L1255) — Managed catalogue targets the authorized principal. |
| <a id="mod-001-f003-s002"></a>`MOD-001-F003-S002` | Save and reset a service-account preference | Readback and subsequent service runs follow the administered preference/default. | LOCAL-ONLY | **1** (E2E 0, local 1); existing total unknown | [TEST-039](#test-039) | [EV-004](#ev-004) [test_ac10_admin_in_tenant_succeeds](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py#L1191) — Admin API save is tested; real service-run selection is not covered by this test. |
| <a id="mod-001-f003-s003"></a>`MOD-001-F003-S003` | Target a service account in another organization | Read and write are denied without exposing that account's model policy. | LOCAL-ONLY | **2** (E2E 0, local 2); existing total unknown | [TEST-038](#test-038), [TEST-040](#test-040) | [EV-004](#ev-004) [test_ac10_admin_cross_tenant_refused](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py#L1348) — Cross-tenant administration is refused in-process. |
| <a id="mod-001-f003-s004"></a>`MOD-001-F003-S004` | Switch between self and service scope during pending requests | Responses cannot overwrite the currently selected scope. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-001-f003-s005"></a>`MOD-001-F003-S005` | Lose administration permission before save | Server rejects the mutation and the UI explains the refusal. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-001-f004"></a>

#### MOD-001-F004 — Platform persona defaults: select model, reason and revision-safe save

Module: [MOD-001](#mod-001). Previous audit ID: `AM-04`. Native references: none found.

Surface: /settings/agent-models (platform admin). **Feature coverage: Local only.** Defaults component/API tests; E38 explicitly holds platform-default changes.

Related feature evidence: [EV-007](#ev-007), [EV-008](#ev-008), [EV-002](#ev-002). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-001-f004-s001"></a>`MOD-001-F004-S001` | Save a platform persona default with a reason | Model, actor, reason and new revision are retained. | LOCAL-ONLY | **1** (E2E 0, local 1); existing total unknown | [TEST-044](#test-044) | [EV-008](#ev-008) [test_create_replay_update_reset_and_conflict](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_platform_defaults.py#L96) — Local default API assertions; deployed selection propagation remains unqualified. |
| <a id="mod-001-f004-s002"></a>`MOD-001-F004-S002` | Attempt a platform-default change as org admin or member | The change is denied and the existing default remains intact. | LOCAL-ONLY | **1** (E2E 0, local 1); existing total unknown | [TEST-046](#test-046) | [EV-008](#ev-008) [test_non_admin_denied](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_platform_defaults.py#L121) — Local default API assertions; deployed selection propagation remains unqualified. |
| <a id="mod-001-f004-s003"></a>`MOD-001-F004-S003` | Race two platform-default writes | A stale author cannot silently overwrite the accepted revision. | LOCAL-ONLY | **1** (E2E 0, local 1); existing total unknown | [TEST-044](#test-044) | [EV-008](#ev-008) [test_create_replay_update_reset_and_conflict](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_platform_defaults.py#L96) — Local default API assertions; deployed selection propagation remains unqualified. |
| <a id="mod-001-f004-s004"></a>`MOD-001-F004-S004` | Choose a retired or incompatible default | Validation refuses promotion and preserves the last usable default. | LOCAL-ONLY | **1** (E2E 0, local 1); existing total unknown | [TEST-045](#test-045) | [EV-008](#ev-008) [test_incompatible_or_unknown_model_refused](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_platform_defaults.py#L128) — Local default API assertions; deployed selection propagation remains unqualified. |
| <a id="mod-001-f004-s005"></a>`MOD-001-F004-S005` | Start runs with and without personal overrides after promotion | Default consumers change; explicit valid overrides retain their documented precedence. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-001-f005"></a>

#### MOD-001-F005 — Compatibility-class defaults, preview, posture revision/history/rollback

Module: [MOD-001](#mod-001). Previous audit ID: `AM-05`. Native references: #5425; PMM-07.

Surface: API / CLI. **Feature coverage: Local only.** Posture authorization, promotion and concurrency tests; rollout evidence validator is not a live mutation driver.

Related feature evidence: [EV-009](#ev-009), [EV-010](#ev-010), [EV-011](#ev-011), [EV-002](#ev-002). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-001-f005-s001"></a>`MOD-001-F005-S001` | Preview and promote a compatibility-class default | Preview identifies the exact change; acceptance persists only the reviewed revision. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-001-f005-s002"></a>`MOD-001-F005-S002` | Change runtime posture as platform admin | New admissions use the accepted posture and record its revision. | LOCAL-ONLY | **1** (E2E 0, local 1); existing total unknown | [TEST-047](#test-047) | [EV-009](#ev-009) [test_platform_admin_can_read_and_change](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_posture_authz.py#L208) — In-process posture boundary assertions; live worker propagation is not established. |
| <a id="mod-001-f005-s003"></a>`MOD-001-F005-S003` | Read posture history and roll back to an eligible revision | History remains intact and new decisions use the restored policy. | LOCAL-ONLY | **1** (E2E 0, local 1); existing total unknown | [TEST-048](#test-048) | [EV-009](#ev-009) [test_rollback_is_the_same_audited_operation](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_posture_authz.py#L271) — In-process posture boundary assertions; live worker propagation is not established. |
| <a id="mod-001-f005-s004"></a>`MOD-001-F005-S004` | Attempt rollback from a stale revision or unauthorized role | Request is refused without changing global posture. | LOCAL-ONLY | **1** (E2E 0, local 1); existing total unknown | [TEST-049](#test-049) | [EV-009](#ev-009) [test_stale_revision_is_a_conflict_that_writes_nothing](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_posture_authz.py#L229) — In-process posture boundary assertions; live worker propagation is not established. |
| <a id="mod-001-f005-s005"></a>`MOD-001-F005-S005` | Attempt promotion without required readiness evidence | Enforcement is not enabled on incomplete or stale proof. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-001-f006"></a>

#### MOD-001-F006 — Model probes, evidence persistence, runtime model selection

Module: [MOD-001](#mod-001). Previous audit ID: `AM-06`. Native references: #5420; PMM-03.

Surface: Internal API. **Feature coverage: Local only.** Probe/selection tests and rollout checks exist; current end-to-end selected-model inference acceptance is not established.

Related feature evidence: [EV-012](#ev-012), [EV-013](#ev-013). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-001-f006-s001"></a>`MOD-001-F006-S001` | Claim, start and complete a bounded model probe | One valid slot records destination-specific invocability evidence. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-001-f006-s002"></a>`MOD-001-F006-S002` | Read probe evidence in a fresh database session | Evidence timestamps and status survive persistence correctly. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-001-f006-s003"></a>`MOD-001-F006-S003` | Replay completion or use another worker's slot | Evidence is not overwritten by an unauthorized or stale execution. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-001-f006-s004"></a>`MOD-001-F006-S004` | Expire or invalidate probe evidence before selection | Catalogue and admission honor stale-evidence restrictions. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-001-f006-s005"></a>`MOD-001-F006-S005` | Resolve a persona for a real worker invocation | Decision binds the authorized persona, model, destination and policy revision. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-001-f007"></a>

#### MOD-001-F007 — Task budget compatibility and reservation preview

Module: [MOD-001](#mod-001). Previous audit ID: `AM-07`. Native references: none found.

Surface: /settings/agent-models. **Feature coverage: Local only.** UI shows limits and estimated reservation; policy API tests override identity/storage. No paid task reconciled against this preview found.

Related feature evidence: [EV-014](#ev-014), [EV-015](#ev-015). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-001-f007-s001"></a>`MOD-001-F007-S001` | Open task-budget compatibility for self and service account | Limits and effective models match the selected principal. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-001-f007-s002"></a>`MOD-001-F007-S002` | Preview reservation for a chosen model/output bound | Estimate includes known bounds or explicitly reports unavailable inputs. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-001-f007-s003"></a>`MOD-001-F007-S003` | Change model or limits after preview | Stale compatibility results cannot authorize a newer request. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-001-f007-s004"></a>`MOD-001-F007-S004` | Attempt to view a foreign principal's policy | Policy and spending limits are not disclosed. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-001-f007-s005"></a>`MOD-001-F007-S005` | Execute a bounded task after preview | Admission, actual execution and final settlement reconcile with the applicable limits. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-002"></a>

### MOD-002 — Organizations

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-002-F001](#mod-002-f001) | List/search and create native organization with default department/team | ORG-01 | #4841; #4842; #5623 |
| [MOD-002-F002](#mod-002-f002) | Department create/read/rename/delete and explicit parent scope | ORG-02 | #4841; #5623 |
| [MOD-002-F003](#mod-002-f003) | Team create/read/edit/delete across departments | ORG-03 | #4841; #5623 |
| [MOD-002-F004](#mod-002-f004) | Team assignment, multiple memberships, primary team, removal | ORG-04 | #4840; #4847 |
| [MOD-002-F005](#mod-002-f005) | Add existing person, cross-org confirmation, roster search/pagination, removal | ORG-05 | #4847; #4943 |
| [MOD-002-F006](#mod-002-f006) | Assign roles, role choices, Cognito user/team/department projections | ORG-06 | #4847 |
| [MOD-002-F007](#mod-002-f007) | Enroll GitHub username into organization and selected team | ORG-07 | Not found; master ID is canonical |
| [MOD-002-F008](#mod-002-f008) | Platform-admin member spend/limit column | ORG-08 | #4620; #4847 |
| [MOD-002-F009](#mod-002-f009) | Attach/list/detach organization GitHub installation | ORG-09 | #4842 |
| [MOD-002-F010](#mod-002-f010) | Link/unlink GitHub organizations to tenant, preview and pagination | ORG-10 | #2954 |

<a id="mod-002-f001"></a>

#### MOD-002-F001 — List/search and create native organization with default department/team

Module: [MOD-002](#mod-002). Previous audit ID: `ORG-01`. Native references: #4841; #4842; #5623.

Surface: /admin/organizations. **Feature coverage: Partial E2E.** E29 reads; D03 creates owned organization via CLI and tests conflict/cleanup. Browser create flow is mocked.

Related feature evidence: [EV-016](#ev-016), [EV-017](#ev-017), [EV-018](#ev-018). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-002-f001-s001"></a>`MOD-002-F001-S001` | List and search accessible organizations | Results contain only organizations the caller may read, including GitHub-free organizations. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f001-s002"></a>`MOD-002-F001-S002` | Create an organization with a valid identifier | Organization, default department and default team are available together. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-067](#test-067) | [EV-017](#ev-017) — D03 owned CLI organization creation/conflict; browser flow remains unmapped. |
| <a id="mod-002-f001-s003"></a>`MOD-002-F001-S003` | Repeat creation with the same identifier | Conflict leaves the original organization and hierarchy unchanged. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-067](#test-067) | [EV-017](#ev-017) — D03 owned CLI organization creation/conflict; browser flow remains unmapped. |
| <a id="mod-002-f001-s004"></a>`MOD-002-F001-S004` | Attempt organization creation as an org admin | Platform-only creation is refused without partial records. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f001-s005"></a>`MOD-002-F001-S005` | Reload the Organizations page after creation | Persisted structure appears through real API responses. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-002-f002"></a>

#### MOD-002-F002 — Department create/read/rename/delete and explicit parent scope

Module: [MOD-002](#mod-002). Previous audit ID: `ORG-02`. Native references: #4841; #5623.

Surface: /admin/organizations; /org/:orgId. **Feature coverage: Partial E2E.** D03 asserts parentage, stale writes and populated-delete refusal; frontend CRUD is component-level.

Related feature evidence: [EV-016](#ev-016), [EV-017](#ev-017), [EV-019](#ev-019). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-002-f002-s001"></a>`MOD-002-F002-S001` | Create a department in an owned organization | Department readback retains the explicit organization parent. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-067](#test-067) | [EV-017](#ev-017) — D03 hierarchy parentage/mutation/refusal assertions; no real browser acceptance. |
| <a id="mod-002-f002-s002"></a>`MOD-002-F002-S002` | Rename a department then reload | Name changes persist without changing its identity or parent. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-067](#test-067) | [EV-017](#ev-017) — D03 hierarchy parentage/mutation/refusal assertions; no real browser acceptance. |
| <a id="mod-002-f002-s003"></a>`MOD-002-F002-S003` | Delete an empty owned department | Subsequent list/detail reads show its documented removal state. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f002-s004"></a>`MOD-002-F002-S004` | Delete a populated department | Unsafe removal is refused without cascading into teams or members. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-067](#test-067) | [EV-017](#ev-017) — D03 hierarchy parentage/mutation/refusal assertions; no real browser acceptance. |
| <a id="mod-002-f002-s005"></a>`MOD-002-F002-S005` | Supply a foreign organization or stale revision | Mutation is rejected and existing hierarchy is unchanged. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-067](#test-067) | [EV-017](#ev-017) — D03 hierarchy parentage/mutation/refusal assertions; no real browser acceptance. |

<a id="mod-002-f003"></a>

#### MOD-002-F003 — Team create/read/edit/delete across departments

Module: [MOD-002](#mod-002). Previous audit ID: `ORG-03`. Native references: #4841; #5623.

Surface: /admin/organizations; /org/:orgId/department/:deptId. **Feature coverage: Partial E2E.** D03 exercises owned team lifecycle/foreign-parent refusal; no real browser CRUD journey found.

Related feature evidence: [EV-016](#ev-016), [EV-017](#ev-017). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-002-f003-s001"></a>`MOD-002-F003-S001` | Create a team under a selected department | Team is listed under the correct department and organization. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-067](#test-067) | [EV-017](#ev-017) — D03 owned team lifecycle/parent refusal; UI assertions require separate mapping. |
| <a id="mod-002-f003-s002"></a>`MOD-002-F003-S002` | Rename or edit a team then reload | Updated values persist and unrelated memberships remain unchanged. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-067](#test-067) | [EV-017](#ev-017) — D03 owned team lifecycle/parent refusal; UI assertions require separate mapping. |
| <a id="mod-002-f003-s003"></a>`MOD-002-F003-S003` | Switch department and list org-wide teams | Department filtering and organization-wide membership choices remain distinct. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f003-s004"></a>`MOD-002-F003-S004` | Delete an owned team and retry deletion | Removal and subsequent absence/conflict responses follow the API contract. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-067](#test-067) | [EV-017](#ev-017) — D03 owned team lifecycle/parent refusal; UI assertions require separate mapping. |
| <a id="mod-002-f003-s005"></a>`MOD-002-F003-S005` | Supply a team belonging to another parent | Read/write cannot escape the caller's organization hierarchy. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-067](#test-067) | [EV-017](#ev-017) — D03 owned team lifecycle/parent refusal; UI assertions require separate mapping. |

<a id="mod-002-f004"></a>

#### MOD-002-F004 — Team assignment, multiple memberships, primary team, removal

Module: [MOD-002](#mod-002). Previous audit ID: `ORG-04`. Native references: #4840; #4847.

Surface: /admin/organizations → Members. **Feature coverage: Partial E2E.** D03 covers two-team removal; component/API/PostgreSQL tests cover primary membership rules. No full browser/Cognito propagation journey established.

Related feature evidence: [EV-016](#ev-016), [EV-017](#ev-017), [EV-020](#ev-020). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-002-f004-s001"></a>`MOD-002-F004-S001` | Assign a member to a second team | Both membership rows are visible without replacing the first. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f004-s002"></a>`MOD-002-F004-S002` | Choose a new primary team using the replacement set | Exactly one primary remains and readback reflects the change. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f004-s003"></a>`MOD-002-F004-S003` | Submit zero or multiple primaries where prohibited | Validation rejects the invalid set without partially changing membership. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f004-s004"></a>`MOD-002-F004-S004` | Remove one of two team memberships | The remaining membership survives and primary selection follows the documented rule. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-067](#test-067) | [EV-017](#ev-017) — D03 removes one of two teams; all primary/claim transitions are not qualified. |
| <a id="mod-002-f004-s005"></a>`MOD-002-F004-S005` | Refresh real Cognito claims after membership change | Authorized sessions receive the updated team scope; stale access is refused. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-002-f005"></a>

#### MOD-002-F005 — Add existing person, cross-org confirmation, roster search/pagination, removal

Module: [MOD-002](#mod-002). Previous audit ID: `ORG-05`. Native references: #4847; #4943.

Surface: /admin/organizations → Members. **Feature coverage: Partial E2E.** D03 revokes/restores owned membership and checks tenant denial. Existing-person enrollment UI is mocked; role-change qualification is limited by cleanup.

Related feature evidence: [EV-016](#ev-016), [EV-017](#ev-017), [EV-021](#ev-021). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-002-f005-s001"></a>`MOD-002-F005-S001` | Add an existing person from the same organization | Membership is created once with the selected team and role. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f005-s002"></a>`MOD-002-F005-S002` | Add a person from another organization with explicit confirmation | The returned org-local identity is used for team assignment. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f005-s003"></a>`MOD-002-F005-S003` | Search and paginate a roster larger than one page | No permitted member is silently omitted or repeated across continuation. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f005-s004"></a>`MOD-002-F005-S004` | Remove an owned tenant membership | Access to that tenant is denied while another valid membership remains usable. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-067](#test-067) | [EV-017](#ev-017) — D03 tenant revocation/restoration and owned-resource cleanup. |
| <a id="mod-002-f005-s005"></a>`MOD-002-F005-S005` | Restore a removed test membership and clean up | Original access is restored and disposable hierarchy resources are absent. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-067](#test-067) | [EV-017](#ev-017) — D03 tenant revocation/restoration and owned-resource cleanup. |

<a id="mod-002-f006"></a>

#### MOD-002-F006 — Assign roles, role choices, Cognito user/team/department projections

Module: [MOD-002](#mod-002). Previous audit ID: `ORG-06`. Native references: #4847.

Surface: /admin/organizations; /org/:orgId. **Feature coverage: Partial E2E.** Component/API checks plus optional hierarchy role-transition driver; D03 explicitly does not claim universal role-change qualification.

Related feature evidence: [EV-016](#ev-016), [EV-017](#ev-017), [EV-022](#ev-022). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-002-f006-s001"></a>`MOD-002-F006-S001` | Read assignable roles for each administration role | Only roles the caller can assign are offered and accepted. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f006-s002"></a>`MOD-002-F006-S002` | Change a member's role then perform a protected operation | New authority takes effect server-side rather than only in UI labels. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f006-s003"></a>`MOD-002-F006-S003` | Attempt privilege escalation or a foreign-org role change | Mutation is denied and existing claims remain unchanged. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f006-s004"></a>`MOD-002-F006-S004` | Compare Cognito projections with canonical team membership | Users, teams and departments agree with the authoritative membership rows. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f006-s005"></a>`MOD-002-F006-S005` | Revoke an elevated role and refresh/reuse sessions | Former elevated operations are refused under the documented session policy. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-002-f007"></a>

#### MOD-002-F007 — Enroll GitHub username into organization and selected team

Module: [MOD-002](#mod-002). Previous audit ID: `ORG-07`. Native references: none found.

Surface: /admin/organizations → Add member. **Feature coverage: Local only.** GitHub enrollment API tests found; no provider lookup → enrollment → real sign-in E2E found.

Related feature evidence: [EV-023](#ev-023). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-002-f007-s001"></a>`MOD-002-F007-S001` | Enroll an existing GitHub username into an owned team | Canonical user and requested membership are created or safely reused. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f007-s002"></a>`MOD-002-F007-S002` | Enter an unknown username or fail the provider lookup | UI reports the error and no partial enrollment is granted. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f007-s003"></a>`MOD-002-F007-S003` | Repeat enrollment for the same provider identity | No duplicate person or conflicting primary membership is created. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f007-s004"></a>`MOD-002-F007-S004` | Enroll into another organization's team | Authorization and parent validation reject the request. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f007-s005"></a>`MOD-002-F007-S005` | Sign in with the enrolled GitHub identity | The real login resolves to the intended organization and role. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-002-f008"></a>

#### MOD-002-F008 — Platform-admin member spend/limit column

Module: [MOD-002](#mod-002). Previous audit ID: `ORG-08`. Native references: #4620; #4847.

Surface: /admin/organizations → Members. **Feature coverage: Local only.** UI permission/request tests; no cross-org real billing acceptance for this column.

Related feature evidence: [EV-016](#ev-016), [EV-024](#ev-024). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-002-f008-s001"></a>`MOD-002-F008-S001` | Load member budgets as platform admin | Spend and applicable person limits match authorized ledger data. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f008-s002"></a>`MOD-002-F008-S002` | Open the Members tab as org admin | Protected spend data is neither requested by the UI nor disclosed by direct API access. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f008-s003"></a>`MOD-002-F008-S003` | Inspect one person with memberships in multiple organizations | Cross-org cap semantics are preserved without exposing unrelated tenant details. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f008-s004"></a>`MOD-002-F008-S004` | Compare displayed spend with settled owned runs | Values reconcile and missing amounts are not silently treated as zero. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-002-f009"></a>

#### MOD-002-F009 — Attach/list/detach organization GitHub installation

Module: [MOD-002](#mod-002). Previous audit ID: `ORG-09`. Native references: #4842.

Surface: API; organization connection badges. **Feature coverage: Local only.** Provider/ownership lifecycle API tests; browser badges do not establish real installation lifecycle.

Related feature evidence: [EV-025](#ev-025). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-002-f009-s001"></a>`MOD-002-F009-S001` | Attach an owned GitHub installation to an organization | Connection and authoritative ownership mapping agree. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f009-s002"></a>`MOD-002-F009-S002` | List connection badges after attaching and reloading | UI reflects the persisted installation binding. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f009-s003"></a>`MOD-002-F009-S003` | Attempt to bind an installation owned by another tenant | Ownership checks refuse the attachment. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f009-s004"></a>`MOD-002-F009-S004` | Detach the installation then deliver a provider event | Removed binding cannot grant the previous organization access. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f009-s005"></a>`MOD-002-F009-S005` | Retry attach/detach around a provider failure | No duplicate binding or misleading success survives partial failure. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-002-f010"></a>

#### MOD-002-F010 — Link/unlink GitHub organizations to tenant, preview and pagination

Module: [MOD-002](#mod-002). Previous audit ID: `ORG-10`. Native references: #2954.

Surface: /admin/tenant-links. **Feature coverage: Local only.** Tenant-link API tests exist; no complete real link → event routed to tenant → unlink journey found.

Related feature evidence: [EV-026](#ev-026). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-002-f010-s001"></a>`MOD-002-F010-S001` | Link and list a GitHub organization under a tenant | Link identity and pagination retain tenant scope. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f010-s002"></a>`MOD-002-F010-S002` | Preview unlink before accepting it | Preview identifies the exact link and impact without changing routing. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f010-s003"></a>`MOD-002-F010-S003` | Unlink an owned organization | Subsequent resolution follows the remaining authorized links only. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f010-s004"></a>`MOD-002-F010-S004` | Attempt foreign-tenant linking or a conflicting organization link | Request is refused without changing either tenant. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-002-f010-s005"></a>`MOD-002-F010-S005` | Deliver events before and after a link change | Events resolve using the current authorized tenant mapping. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-003"></a>

### MOD-003 — Agents and service accounts

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-003-F001](#mod-003-f001) | Organization SQL IAM, registry and Cognito identities; registration/edit/removal | ORG-11 | #5624 |
| [MOD-003-F002](#mod-003-f002) | Cognito agent create/edit/delete, list and credentials | AG-01 | #119 |
| [MOD-003-F003](#mod-003-f003) | BYO IAM onboarding, hierarchy policy preview and agent type catalogue | AG-02 | #250; #255 |

<a id="mod-003-f001"></a>

#### MOD-003-F001 — Organization SQL IAM, registry and Cognito identities; registration/edit/removal

Module: [MOD-003](#mod-003). Previous audit ID: `ORG-11`. Native references: #5624.

Surface: /admin/organizations → Structure → Service accounts. **Feature coverage: Partial E2E.** E31 reads and D05 owned machine lifecycle; UI service identity tests are mocked. Different identity kinds are not interchangeable.

Related feature evidence: [EV-027](#ev-027), [EV-028](#ev-028), [EV-002](#ev-002). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-003-f001-s001"></a>`MOD-003-F001-S001` | List SQL IAM, registry and Cognito identities independently | Identity kinds retain their canonical IDs and organization scope. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-081](#test-081) | [EV-002](#ev-002) — E31 bounded identity-kind metadata reads; no machine mutation. |
| <a id="mod-003-f001-s002"></a>`MOD-003-F001-S002` | Register and read back an owned machine identity | Persisted identity matches the selected kind, owner and hierarchy. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-071](#test-071) | [EV-028](#ev-028) — D05 owned machine lifecycle; complete behavior across all identity kinds remains partial. |
| <a id="mod-003-f001-s003"></a>`MOD-003-F001-S003` | Edit allowed identity metadata with a current revision | New metadata persists without widening authority accidentally. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-071](#test-071) | [EV-028](#ev-028) — D05 owned machine lifecycle; complete behavior across all identity kinds remains partial. |
| <a id="mod-003-f001-s004"></a>`MOD-003-F001-S004` | Delete or disable the owned machine identity | Subsequent authentication/use is refused and cleanup is verifiable. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-071](#test-071) | [EV-028](#ev-028) — D05 owned machine lifecycle; complete behavior across all identity kinds remains partial. |
| <a id="mod-003-f001-s005"></a>`MOD-003-F001-S005` | Attempt cross-org registration or identity-kind substitution | Authorization fails without creating a usable machine principal. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-003-f002"></a>

#### MOD-003-F002 — Cognito agent create/edit/delete, list and credentials

Module: [MOD-003](#mod-003). Previous audit ID: `AG-01`. Native references: #119.

Surface: /agents. **Feature coverage: Partial E2E.** Legacy admin probes and D05 machine lifecycle cover selected API behavior; no deployed browser CRUD/credential consumption E2E found.

Related feature evidence: [EV-033](#ev-033), [EV-028](#ev-028). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-003-f002-s001"></a>`MOD-003-F002-S001` | Create a Cognito agent from the Agents page | Persisted client appears with its intended organization scope. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-003-f002-s002"></a>`MOD-003-F002-S002` | Read agent credentials as an authorized administrator | Only the intended client credentials are disclosed through the supported flow. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-003-f002-s003"></a>`MOD-003-F002-S003` | Update an agent and reload the page | New configuration persists without creating another client. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-003-f002-s004"></a>`MOD-003-F002-S004` | Delete the agent then authenticate with its previous credentials | The deleted identity cannot continue using the platform. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-003-f002-s005"></a>`MOD-003-F002-S005` | Attempt credential access as an unauthorized user | Credentials are not returned or exposed in error messages. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-003-f003"></a>

#### MOD-003-F003 — BYO IAM onboarding, hierarchy policy preview and agent type catalogue

Module: [MOD-003](#mod-003). Previous audit ID: `AG-02`. Native references: #250; #255.

Surface: API. **Feature coverage: Local only.** Onboarding/policy security and service tests; no real IAM → K8s → registry → inference lifecycle established.

Related feature evidence: [EV-034](#ev-034). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-003-f003-s001"></a>`MOD-003-F003-S001` | Preview hierarchy-derived IAM policies | Resource boundaries and actions match the requested agent type and allowed scope. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-003-f003-s002"></a>`MOD-003-F003-S002` | Onboard a BYO role with a valid owner/team | Registry, budget and Kubernetes service account are linked consistently. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-003-f003-s003"></a>`MOD-003-F003-S003` | Onboard with a foreign owner/team or unauthorized org-level reach | Request is refused before authority is created. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-003-f003-s004"></a>`MOD-003-F003-S004` | Interrupt onboarding between external operations | Failure is visible and partial resources are recoverable without duplicate authority. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-003-f003-s005"></a>`MOD-003-F003-S005` | Run the onboarded agent through real inference | Pod identity, role trust, model access and usage attribution agree. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-004"></a>

### MOD-004 — Task policies

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-004-F001](#mod-004-f001) | Service and human task limits, allowed personas/tools/scopes, policy revision | ORG-12 | Not found; master ID is canonical |

<a id="mod-004-f001"></a>

#### MOD-004-F001 — Service and human task limits, allowed personas/tools/scopes, policy revision

Module: [MOD-004](#mod-004). Previous audit ID: `ORG-12`. Native references: none found.

Surface: Organizations → Service accounts → Task policy; API. **Feature coverage: Local only.** Task-policy API tests exist; no real UI write → admitted task obeys tools/turns/duration/dollars journey found.

Related feature evidence: [EV-014](#ev-014), [EV-029](#ev-029), [EV-030](#ev-030). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-004-f001-s001"></a>`MOD-004-F001-S001` | Read task policy for human and service principals | Policy, platform ceilings and effective model versions match the target. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-004-f001-s002"></a>`MOD-004-F001-S002` | Save limits, tools, personas and scopes with expected revision | Exact accepted values persist and concurrent stale edits fail. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-004-f001-s003"></a>`MOD-004-F001-S003` | Submit an over-ceiling or malformed task policy | No partial policy is stored and the refusal is actionable. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-004-f001-s004"></a>`MOD-004-F001-S004` | Execute an allowed task after policy save | Allowed tools and model selection work within the configured bounds. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-004-f001-s005"></a>`MOD-004-F001-S005` | Exceed a tool, turn, duration or dollar constraint | Enforcement stops/refuses the operation and records the correct terminal/settlement outcome. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-004-f001-s006"></a>`MOD-004-F001-S006` | Attempt a foreign-org policy mutation | The canonical principal cannot be reassigned or administered without authority. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-005"></a>

### MOD-005 — Identity and access

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-005-F001](#mod-005-f001) | Canonical user/identity CRUD, Cognito provisioning and recovery | ORG-13 | #387 |
| [MOD-005-F002](#mod-005-f002) | GitHub/email sign-in, callback, token exchange, current user and logout | ID-01 | FR1; #5185 |
| [MOD-005-F003](#mod-005-f003) | Browser approval, native password/MFA, refresh and admin session | ID-02 | #5184; #5185 |
| [MOD-005-F004](#mod-005-f004) | Discover/switch workspace, context, session isolation and refresh | ID-03 | #5622 |
| [MOD-005-F005](#mod-005-f005) | Request access, pending/denied state, approve/deny with review revision | ID-04 | #538; #545; #5625 |
| [MOD-005-F006](#mod-005-f006) | Review/revoke user sessions, token cleanup and auth service accounts | ID-05 | #5624 |

<a id="mod-005-f001"></a>

#### MOD-005-F001 — Canonical user/identity CRUD, Cognito provisioning and recovery

Module: [MOD-005](#mod-005). Previous audit ID: `ORG-13`. Native references: #387.

Surface: API. **Feature coverage: Local only.** Identity API/security tests; E18 recovery acceptance is blocked by missing durable recovery capability.

Related feature evidence: [EV-031](#ev-031), [EV-032](#ev-032). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-005-f001-s001"></a>`MOD-005-F001-S001` | Create, list and remove a canonical organization user | Identity and membership state remain consistent across readback and removal. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-005-f001-s002"></a>`MOD-005-F001-S002` | Link and unlink a provider identity | Real authentication resolves only active authorized bindings. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-005-f001-s003"></a>`MOD-005-F001-S003` | Provision or recover a Cognito identity | Successful recovery restores intended access without duplicating the person. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-005-f001-s004"></a>`MOD-005-F001-S004` | Repeat a failed provisioning/recovery request | Recovery is idempotent or explicitly blocked, with no false success. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-005-f001-s005"></a>`MOD-005-F001-S005` | Attempt another tenant's identity or recovery mutation | Server refuses the operation before changing provider state. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-005-f002"></a>

#### MOD-005-F002 — GitHub/email sign-in, callback, token exchange, current user and logout

Module: [MOD-005](#mod-005). Previous audit ID: `ID-01`. Native references: FR1; #5185.

Surface: /login; /auth/callback. **Feature coverage: Partial E2E.** Chat PKCE/session tests and CLI C01 native login exist; gateway live auth tests are fixture-dependent.

Related feature evidence: [EV-035](#ev-035), [EV-036](#ev-036), [EV-037](#ev-037). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-005-f002-s001"></a>`MOD-005-F002-S001` | Complete GitHub sign-in and OAuth callback | Session is bound to the authenticated person and authorized workspace. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-005-f002-s002"></a>`MOD-005-F002-S002` | Complete email/native sign-in then restore the session | Current-user readback agrees with the restored identity. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-063](#test-063) | [EV-035](#ev-035) [test_supplied_tokens_restore_authenticated_session](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/chat/test_auth.py#L86) — Browser restores supplied session; this test does not complete native email login. |
| <a id="mod-005-f002-s003"></a>`MOD-005-F002-S003` | Supply expired, malformed or unsigned tokens | Protected endpoints refuse access without treating token contents as trusted claims. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-005-f002-s004"></a>`MOD-005-F002-S004` | Replay or tamper with callback/exchange state | Session cannot be issued to the wrong browser or caller. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-005-f002-s005"></a>`MOD-005-F002-S005` | Log out then revisit protected UI and APIs | The documented logout/revocation behavior is enforced consistently. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-005-f003"></a>

#### MOD-005-F003 — Browser approval, native password/MFA, refresh and admin session

Module: [MOD-005](#mod-005). Previous audit ID: `ID-02`. Native references: #5184; #5185.

Surface: /cli-auth; CLI. **Feature coverage: Partial E2E.** C01 native login driver exists; E11 github_login remote driver is missing. Native login success does not cover browser approval.

Related feature evidence: [EV-036](#ev-036), [EV-038](#ev-038), [EV-039](#ev-039). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-005-f003-s001"></a>`MOD-005-F003-S001` | Install CLI and complete native password login | Authenticated commands work with the returned session. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-069](#test-069) | [EV-036](#ev-036) — E01/C01 served installation and native-login harness; browser approval is separate. |
| <a id="mod-005-f003-s002"></a>`MOD-005-F003-S002` | Complete an MFA challenge and test a rejected challenge | Only a valid challenge creates an authenticated admin session. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-005-f003-s003"></a>`MOD-005-F003-S003` | Approve a browser/device login for the correct CLI request | Tokens reach only the matching pending client. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-005-f003-s004"></a>`MOD-005-F003-S004` | Replay, expire or approve another request's login | Exchange is refused without leaking credentials. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-005-f003-s005"></a>`MOD-005-F003-S005` | Refresh the CLI session after expiry | Identity and selected tenant remain correct; revoked refresh credentials fail. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-005-f004"></a>

#### MOD-005-F004 — Discover/switch workspace, context, session isolation and refresh

Module: [MOD-005](#mod-005). Previous audit ID: `ID-03`. Native references: #5622.

Surface: Header WorkspaceSelector; /next; Connections. **Feature coverage: Partial E2E.** E23/E27 cover owned memberships and concurrent CLI reads; New UI tests check shared identity. Browser writes and model isolation are separate.

Related feature evidence: [EV-040](#ev-040), [EV-041](#ev-041). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-005-f004-s001"></a>`MOD-005-F004-S001` | Discover memberships and select an owned workspace | Current workspace and claims identify the selected membership. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-090](#test-090) | [EV-040](#ev-040) — E23/E27 owned membership/concurrent-read fixture; no model-inference isolation claim. |
| <a id="mod-005-f004-s002"></a>`MOD-005-F004-S002` | Choose an unknown or unowned workspace | Selection is refused and the prior context stays valid. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-090](#test-090) | [EV-040](#ev-040) — E23/E27 owned membership/concurrent-read fixture; no model-inference isolation claim. |
| <a id="mod-005-f004-s003"></a>`MOD-005-F004-S003` | Read two owned workspaces concurrently | Each request retains its explicit tenant scope. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-091](#test-091) | [EV-040](#ev-040) — E23/E27 owned membership/concurrent-read fixture; no model-inference isolation claim. |
| <a id="mod-005-f004-s004"></a>`MOD-005-F004-S004` | Refresh credentials after changing local workspace default | Refresh does not silently move another concurrent session. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-091](#test-091) | [EV-040](#ev-040) — E23/E27 owned membership/concurrent-read fixture; no model-inference isolation claim. |
| <a id="mod-005-f004-s005"></a>`MOD-005-F004-S005` | Switch workspace during browser navigation | Page data and remembered navigation remain scoped to the selected tenant. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-005-f005"></a>

#### MOD-005-F005 — Request access, pending/denied state, approve/deny with review revision

Module: [MOD-005](#mod-005). Previous audit ID: `ID-04`. Native references: #538; #545; #5625.

Surface: /onboarding/*; /admin/access-requests. **Feature coverage: Partial E2E.** E33 reads status/reviews. No complete new-person browser request → approval → usable services E2E found.

Related feature evidence: [EV-002](#ev-002), [EV-042](#ev-042). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-005-f005-s001"></a>`MOD-005-F005-S001` | Submit a new access request from onboarding | User sees the pending state and cannot use approval-gated services. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-005-f005-s002"></a>`MOD-005-F005-S002` | Read a request as an authorized reviewer | Review content is scoped to the reviewer and carries the current decision revision. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-083](#test-083) | [EV-002](#ev-002) — E33 bounded authorized request-review read; decisions are not exercised. |
| <a id="mod-005-f005-s003"></a>`MOD-005-F005-S003` | Approve a pending request then refresh the applicant session | Applicant receives intended access and a stable membership. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-005-f005-s004"></a>`MOD-005-F005-S004` | Deny a request with a reason | Denied state is visible and service admission remains blocked. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-005-f005-s005"></a>`MOD-005-F005-S005` | Race approve/deny with a stale revision | Only an authorized current decision takes effect. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-005-f006"></a>

#### MOD-005-F006 — Review/revoke user sessions, token cleanup and auth service accounts

Module: [MOD-005](#mod-005). Previous audit ID: `ID-05`. Native references: #5624.

Surface: API. **Feature coverage: Partial E2E.** D05 covers owned session/identity boundaries; not every token revocation and client lifecycle path.

Related feature evidence: [EV-028](#ev-028), [EV-043](#ev-043). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-005-f006-s001"></a>`MOD-005-F006-S001` | Read a session-revocation review as an authorized admin | Review identifies the intended target and current revision. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-005-f006-s002"></a>`MOD-005-F006-S002` | Revoke a user's tokens and reuse an existing session | Revoked credentials fail under the supported enforcement window. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-005-f006-s003"></a>`MOD-005-F006-S003` | Manage an owned auth service account | Create/update/delete preserve client identity and authorized scope. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-005-f006-s004"></a>`MOD-005-F006-S004` | Run token cleanup while another user remains active | Cleanup does not revoke unrelated valid sessions. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-005-f006-s005"></a>`MOD-005-F006-S005` | Attempt token revocation across an unauthorized tenant | No foreign session state changes. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-006"></a>

### MOD-006 — GitHub integration

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-006-F001](#mod-006-f001) | App install/callback, installation list/disconnect and tenant switch | CN-01 | #465; #477 |
| [MOD-006-F002](#mod-006-f002) | Register/import App, setup guide, validation, key rotation and disconnect | CN-02 | #5634 |
| [MOD-006-F003](#mod-006-f003) | App installation, CLI OAuth and task after onboarding | BACK-050 | #5183/#5184; #5634 |
| [MOD-006-F004](#mod-006-f004) | Webhook admission and invalid-signature refusal | BACK-051 | AUD-GH-WEBHOOK |

<a id="mod-006-f001"></a>

#### MOD-006-F001 — App install/callback, installation list/disconnect and tenant switch

Module: [MOD-006](#mod-006). Previous audit ID: `CN-01`. Native references: #465; #477.

Surface: /settings/connections. **Feature coverage: Partial E2E.** Connections browser tests mock APIs; New UI verifies real list response only. No complete GitHub installation E2E established.

Related feature evidence: [EV-044](#ev-044), [EV-041](#ev-041). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-006-f001-s001"></a>`MOD-006-F001-S001` | Start and complete a real GitHub App installation | Callback binds the installation to the authorized user/tenant. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-006-f001-s002"></a>`MOD-006-F001-S002` | List installations from the Connections page | Page data comes from the current tenant and real API. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-096](#test-096) | [EV-041](#ev-041) — Real Connections list response is observed; installation lifecycle is not exercised. |
| <a id="mod-006-f001-s003"></a>`MOD-006-F001-S003` | Tamper with installation callback state | Wrong or expired state cannot bind an installation. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-006-f001-s004"></a>`MOD-006-F001-S004` | Switch tenant from Connections | Session and connection list agree with the new authorized tenant. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-006-f001-s005"></a>`MOD-006-F001-S005` | Disconnect an owned installation | Provider-backed access through the removed connection is denied. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-006-f002"></a>

#### MOD-006-F002 — Register/import App, setup guide, validation, key rotation and disconnect

Module: [MOD-006](#mod-006). Previous audit ID: `CN-02`. Native references: #5634.

Surface: /settings/connections. **Feature coverage: Partial E2E.** E28 only reads maintenance/previews, explicitly avoids keys/shared App writes. Component tests do not establish live App maintenance.

Related feature evidence: [EV-002](#ev-002), [EV-045](#ev-045). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-006-f002-s001"></a>`MOD-006-F002-S001` | Read App maintenance status and setup guide | Supported registration state is shown without exposing keys. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-079](#test-079) | [EV-002](#ev-002) — E28 maintenance reads/previews only; guide UI and App mutation remain separate. |
| <a id="mod-006-f002-s002"></a>`MOD-006-F002-S002` | Register through manifest flow or manual import | Valid App configuration is verified and persisted. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-006-f002-s003"></a>`MOD-006-F002-S003` | Revalidate configuration after provider settings drift | Actionable mismatch is returned rather than a false healthy state. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-006-f002-s004"></a>`MOD-006-F002-S004` | Rotate an owned App key and test subsequent provider calls | New key works and retired key authority follows the documented rotation policy. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-006-f002-s005"></a>`MOD-006-F002-S005` | Disconnect the App with a reviewed current revision | The intended configuration is removed; stale maintenance requests cannot remove newer settings. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-006-f003"></a>

#### MOD-006-F003 — App installation, CLI OAuth and task after onboarding

Module: [MOD-006](#mod-006). Previous audit ID: `BACK-050`. Native references: #5183/#5184; #5634.

Surface: Backend / CLI / integration. **Feature coverage: Missing driver.** E10/E11/E12 remote drivers are absent; E28 only reads/previews maintenance. Mocked Connections browser checks cannot fill this gap.

Existing execution selection/limits: E28 daily; full native onboarding not covered.

Related feature evidence: [EV-039](#ev-039), [EV-002](#ev-002), [EV-044](#ev-044). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-006-f003-s001"></a>`MOD-006-F003-S001` | Install a real GitHub App through onboarding | Installation completes and binds to the intended tenant. | MISSING-DRIVER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-039](#ev-039) — E10/E11/E12 remote drivers are absent; E28 only reads/previews maintenance. Mocked Connections browser checks cannot fill this gap. |
| <a id="mod-006-f003-s002"></a>`MOD-006-F003-S002` | Complete CLI GitHub OAuth using the installed connection | Browser approval and token exchange authorize the correct client. | MISSING-DRIVER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-039](#ev-039) — E10/E11/E12 remote drivers are absent; E28 only reads/previews maintenance. Mocked Connections browser checks cannot fill this gap. |
| <a id="mod-006-f003-s003"></a>`MOD-006-F003-S003` | Submit an owned repository task after onboarding | Task uses the installed connection and produces attributable provider work. | MISSING-DRIVER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-039](#ev-039) — E10/E11/E12 remote drivers are absent; E28 only reads/previews maintenance. Mocked Connections browser checks cannot fill this gap. |
| <a id="mod-006-f003-s004"></a>`MOD-006-F003-S004` | Revoke the connection then repeat the task | Previously granted provider authority is no longer usable. | MISSING-DRIVER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-039](#ev-039) — E10/E11/E12 remote drivers are absent; E28 only reads/previews maintenance. Mocked Connections browser checks cannot fill this gap. |

<a id="mod-006-f004"></a>

#### MOD-006-F004 — Webhook admission and invalid-signature refusal

Module: [MOD-006](#mod-006). Previous audit ID: `BACK-051`. Native references: AUD-GH-WEBHOOK.

Surface: Backend / CLI / integration. **Feature coverage: Partial.** Two real-endpoint tests exist; both share the GitLab integration environment gating. Latest integration run skipped all six contract tests.

Existing execution selection/limits: Current push/PR; PR daily adds completeness gate.

Related feature evidence: [EV-125](#ev-125), [EV-126](#ev-126). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-006-f004-s001"></a>`MOD-006-F004-S001` | Deliver a valid signed GitHub webhook | Intended supported event is accepted and routed correctly. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-027](#test-027) | [EV-125](#ev-125) [test_github_webhook_still_processes_events](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_github_regression.py#L107) — Real endpoint harness exists; latest combined contract run skipped. |
| <a id="mod-006-f004-s002"></a>`MOD-006-F004-S002` | Deliver an invalid or missing signature | Webhook is refused before execution is admitted. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-026](#test-026) | [EV-125](#ev-125) [test_github_invalid_signature_still_rejected](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_github_regression.py#L198) — Invalid-signature refusal; latest combined contract run skipped. |
| <a id="mod-006-f004-s003"></a>`MOD-006-F004-S003` | Replay a delivery | Deduplication prevents unintended repeated work. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-006-f004-s004"></a>`MOD-006-F004-S004` | Deliver an event for an unowned installation/repository | Event cannot create work in another tenant. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-007"></a>

### MOD-007 — Credentials and AWS connections

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-007-F001](#mod-007-f001) | List/register/update/delete credentials; GitHub PAT and linked identities | CR-01 | #135; #5631 |
| [MOD-007-F002](#mod-007-f002) | Connect/import AWS role, setup instructions, verify and remove | CR-02 | #562; #481 |
| [MOD-007-F003](#mod-007-f003) | Workspace grants, validation, scoped delivery/evidence/revocation and role sessions | CR-03 | #5528 |

<a id="mod-007-f001"></a>

#### MOD-007-F001 — List/register/update/delete credentials; GitHub PAT and linked identities

Module: [MOD-007](#mod-007). Previous audit ID: `CR-01`. Native references: #135; #5631.

Surface: /settings/credentials; API. **Feature coverage: Partial E2E.** E24 reads/previews; D02 owned credential lifecycle. Browser → provider-authenticated use is not covered universally.

Related feature evidence: [EV-046](#ev-046), [EV-002](#ev-002), [EV-047](#ev-047). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-007-f001-s001"></a>`MOD-007-F001-S001` | List credential and identity metadata | Responses contain authorized metadata without secret values. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-077](#test-077) | [EV-002](#ev-002) — E24 metadata reads and previews; does not read secret values. |
| <a id="mod-007-f001-s002"></a>`MOD-007-F001-S002` | Register and update an owned credential | The new version is usable through the supported delivery path. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-093](#test-093) | [EV-046](#ev-046) — D02 owned vault lifecycle; real provider consumption is not universal. |
| <a id="mod-007-f001-s003"></a>`MOD-007-F001-S003` | Register a GitHub PAT then use an authorized provider operation | Token capability is verified without returning it in logs/results. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-007-f001-s004"></a>`MOD-007-F001-S004` | Delete a credential and retry delivery | Deleted or revoked material is unavailable to subsequent operations. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-093](#test-093) | [EV-046](#ev-046) — D02 owned vault lifecycle; real provider consumption is not universal. |
| <a id="mod-007-f001-s005"></a>`MOD-007-F001-S005` | Link/unlink an identity including magic-link flow | Binding requires valid single-use proof and cannot cross owners. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-007-f002"></a>

#### MOD-007-F002 — Connect/import AWS role, setup instructions, verify and remove

Module: [MOD-007](#mod-007). Previous audit ID: `CR-02`. Native references: #562; #481.

Surface: /settings/credentials/aws/connect; /settings/credentials. **Feature coverage: Partial E2E.** AWS component/integration tests and routing qualification tooling exist; no full browser wizard → verified role → billed inference E2E found.

Related feature evidence: [EV-047](#ev-047), [EV-048](#ev-048). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-007-f002-s001"></a>`MOD-007-F002-S001` | Start AWS connect and display trust setup | Setup references the intended credential binding and verified target. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-007-f002-s002"></a>`MOD-007-F002-S002` | Verify a correctly configured connected role | Usable connection metadata is persisted. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-007-f002-s003"></a>`MOD-007-F002-S003` | Import an existing connection and reload | Existing role configuration is represented without duplicate authority. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-007-f002-s004"></a>`MOD-007-F002-S004` | Verify a wrong account, missing trust or missing permissions | Connection remains unverified with an actionable error. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-007-f002-s005"></a>`MOD-007-F002-S005` | Remove a connection and retry role-based use | Removed binding cannot authorize new sessions. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-007-f003"></a>

#### MOD-007-F003 — Workspace grants, validation, scoped delivery/evidence/revocation and role sessions

Module: [MOD-007](#mod-007). Previous audit ID: `CR-03`. Native references: #5528.

Surface: API / internal. **Feature coverage: Partial E2E.** Adversarial credential harness exists but latest run failed before A1/A3 due missing deployment role; scoped contract tests are separate.

Related feature evidence: [EV-049](#ev-049). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-007-f003-s001"></a>`MOD-007-F003-S001` | Grant a credential to a workspace then validate delivery | Only an authorized operation receives the required scoped material. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-007-f003-s002"></a>`MOD-007-F003-S002` | Use another run/pod identity to request credential delivery | Delivery is refused even when worker transport identity is shared. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-007-f003-s003"></a>`MOD-007-F003-S003` | Revoke the grant between preview and delivery | Fresh revocation checks refuse use of the stale grant. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-007-f003-s004"></a>`MOD-007-F003-S004` | Exercise proxy, raw-read, materialization and assume-role paths | Each path enforces its distinct privilege and returns only permitted material. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-007-f003-s005"></a>`MOD-007-F003-S005` | Replay an operation-bound delivery request outside its binding | Replayed evidence cannot authorize a different executor or operation. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-008"></a>

### MOD-008 — GitLab integration

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-008-F001](#mod-008-f001) | Provider connect/disconnect/configure/revalidate and SSO/JWKS | GL-01 | #3775; #5635 |
| [MOD-008-F002](#mod-008-f002) | Webhook token validation and ignored event types | BACK-052 | #5635; AUD-GL-CONTRACT |
| [MOD-008-F003](#mod-008-f003) | Mention routes through worker to acknowledgement | BACK-053 | AUD-GL-FLEET |

<a id="mod-008-f001"></a>

#### MOD-008-F001 — Provider connect/disconnect/configure/revalidate and SSO/JWKS

Module: [MOD-008](#mod-008). Previous audit ID: `GL-01`. Native references: #3775; #5635.

Surface: External /gitlab/ link; API. **Feature coverage: Partial E2E.** E30 discovery/refusal; latest integration run skipped six contracts and two live fleet tests. No current real provider lifecycle acceptance.

Related feature evidence: [EV-002](#ev-002). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-008-f001-s001"></a>`MOD-008-F001-S001` | Read approved GitLab provider discovery and status | CLI/UI identifies only supported configured provider capabilities. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-080](#test-080) | [EV-002](#ev-002) — E30 provider discovery/invalid-project refusal; SSO and provider writes not included. |
| <a id="mod-008-f001-s002"></a>`MOD-008-F001-S002` | Configure/connect an owned GitLab provider | Revalidation confirms the usable binding without leaking provider credentials. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-008-f001-s003"></a>`MOD-008-F001-S003` | Request SSO and verify the published signing key | Valid authorized user reaches the intended GitLab identity. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-008-f001-s004"></a>`MOD-008-F001-S004` | Supply an invalid project/provider or expired SSO token | Request is refused without provider writes or foreign access. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-080](#test-080) | [EV-002](#ev-002) — E30 provider discovery/invalid-project refusal; SSO and provider writes not included. |
| <a id="mod-008-f001-s005"></a>`MOD-008-F001-S005` | Disconnect the provider | Previously linked operations cannot continue through the removed connection. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-008-f002"></a>

#### MOD-008-F002 — Webhook token validation and ignored event types

Module: [MOD-008](#mod-008). Previous audit ID: `BACK-052`. Native references: #5635; AUD-GL-CONTRACT.

Surface: Backend / CLI / integration. **Feature coverage: Partial.** Four real webhook contract tests exist; latest run skipped all six combined GitHub/GitLab contract tests. E30 only checks CLI provider discovery/refusal.

Existing execution selection/limits: Current push/PR; PR daily live lane plus E30.

Related feature evidence: [EV-127](#ev-127), [EV-002](#ev-002). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-008-f002-s001"></a>`MOD-008-F002-S001` | Deliver a GitLab webhook with valid provider proof | Supported event reaches the intended ingress handler. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-008-f002-s002"></a>`MOD-008-F002-S002` | Deliver wrong or missing provider token | Event is rejected without worker dispatch. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-028](#test-028) | [EV-127](#ev-127) [test_invalid_token_rejected](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_gitlab_roundtrip.py#L94) — Wrong token refusal; latest combined contract run skipped. |
| <a id="mod-008-f002-s003"></a>`MOD-008-F002-S003` | Deliver an unsupported event type | Handler ignores/refuses it according to contract without triggering work. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-029](#test-029) | [EV-127](#ev-127) [test_non_mention_event_ignored](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_gitlab_roundtrip.py#L111) — Ignored non-mention; other ignored-type tests exist in the same file. |
| <a id="mod-008-f002-s004"></a>`MOD-008-F002-S004` | Attempt project/tenant substitution in the event | Canonical provider binding governs routing rather than untrusted payload identity. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-008-f003"></a>

#### MOD-008-F003 — Mention routes through worker to acknowledgement

Module: [MOD-008](#mod-008). Previous audit ID: `BACK-053`. Native references: AUD-GL-FLEET.

Surface: Backend / CLI / integration. **Feature coverage: Partial.** Two live fleet tests exist; latest run skipped both due absent integration configuration. A worker acknowledgement does not prove a completed code-delivery journey.

Existing execution selection/limits: Current main push; PR daily but fixtures required.

Related feature evidence: [EV-128](#ev-128), [EV-126](#ev-126). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-008-f003-s001"></a>`MOD-008-F003-S001` | Post an owned GitLab mention through the real provider | Worker receives authorized task and emits an attributable acknowledgement. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-008-f003-s002"></a>`MOD-008-F003-S002` | Measure acknowledgement latency with declared start/end events | Evidence measures the actual provider-to-worker round trip. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-008-f003-s003"></a>`MOD-008-F003-S003` | Post a mention from an unauthorized project/context | No unintended agent execution is triggered. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-008-f003-s004"></a>`MOD-008-F003-S004` | Follow beyond acknowledgement to terminal delivery | Completion/repository outcome is measured separately from acknowledgement success. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-009"></a>

### MOD-009 — Model access and routing

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-009-F001](#mod-009-f001) | Destination setup/verify, AWS connection links, hierarchy mappings and effective account | BR-01 | #4745; #4692 |
| [MOD-009-F002](#mod-009-f002) | Select/reset personal AWS billing account and admin override refusal | BR-02 | #4746; #5633 |

<a id="mod-009-f001"></a>

#### MOD-009-F001 — Destination setup/verify, AWS connection links, hierarchy mappings and effective account

Module: [MOD-009](#mod-009). Previous audit ID: `BR-01`. Native references: #4745; #4692.

Surface: /model-access. **Feature coverage: Partial E2E.** Routing eval/CLI previews exist; account-landing inference qualification is incomplete; E09 hosted driver missing.

Related feature evidence: [EV-048](#ev-048), [EV-050](#ev-050), [EV-039](#ev-039). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-009-f001-s001"></a>`MOD-009-F001-S001` | Register a Bedrock destination and follow setup/verification | Destination becomes usable only after account and trust verification. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-009-f001-s002"></a>`MOD-009-F001-S002` | Link and unlink an existing AWS connection | Destination ownership and routing eligibility follow the active link. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-009-f001-s003"></a>`MOD-009-F001-S003` | Set, read and delete hierarchy mappings | Effective account follows documented precedence and reset behavior. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-009-f001-s004"></a>`MOD-009-F001-S004` | Attempt routing changes as a non-platform administrator | Global billing mappings cannot be changed. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-009-f001-s005"></a>`MOD-009-F001-S005` | Run real inference through each supported mapping scope | Actual destination account and usage attribution match the resolved mapping. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-009-f002"></a>

#### MOD-009-F002 — Select/reset personal AWS billing account and admin override refusal

Module: [MOD-009](#mod-009). Previous audit ID: `BR-02`. Native references: #4746; #5633.

Surface: /settings/credentials. **Feature coverage: Partial E2E.** E34 preview/refusal; E08 personal-inference driver requires fixtures. UI selector tests are local.

Related feature evidence: [EV-002](#ev-002), [EV-051](#ev-051), [EV-052](#ev-052). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-009-f002-s001"></a>`MOD-009-F002-S001` | Read current personal billing selection | Effective destination and source distinguish personal choice from administrator policy. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-084](#test-084) | [EV-002](#ev-002) — E34 selection/reset preview and admin-pinned refusal; no selection mutation acceptance. |
| <a id="mod-009-f002-s002"></a>`MOD-009-F002-S002` | Select an owned AWS destination then reset | Readback follows personal choice and then inherited routing. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-009-f002-s003"></a>`MOD-009-F002-S003` | Attempt to replace an administrator-pinned mapping | Request is refused and the administrator mapping persists. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-084](#test-084) | [EV-002](#ev-002) — E34 selection/reset preview and admin-pinned refusal; no selection mutation acceptance. |
| <a id="mod-009-f002-s004"></a>`MOD-009-F002-S004` | Select another person's destination or unsupported scope | Personal selection cannot create foreign billing authority. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-009-f002-s005"></a>`MOD-009-F002-S005` | Execute personal inference after selection | The intended account receives the call and its usage is attributable. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-074](#test-074) | [EV-051](#ev-051) — E08 personal inference requires an owned destination fixture; current pass not established. |

<a id="mod-010"></a>

### MOD-010 — Budgets and spend

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-010-F001](#mod-010-f001) | Monthly/per-org spend, own period restrictions and runs | BU-01 | #4402; #5589 |
| [MOD-010-F002](#mod-010-f002) | Person caps/defaults, hierarchy editing, people spend and cap diagnostics | BU-02 | #4620; #4629; #5626 |
| [MOD-010-F003](#mod-010-f003) | Managed budget periods/configuration, allocation, status, summaries and cost calculation | BU-03 | FR3; #4401; #5626 |
| [MOD-010-F004](#mod-010-f004) | Platform/flow enforcement state and mutation | BU-04 | Not found; master ID is canonical |

<a id="mod-010-f001"></a>

#### MOD-010-F001 — Monthly/per-org spend, own period restrictions and runs

Module: [MOD-010](#mod-010). Previous audit ID: `BU-01`. Native references: #4402; #5589.

Surface: /budget. **Feature coverage: Partial E2E.** E26 own period reads; current UI monthly overview uses different API. No spend reconciliation proved by read smoke.

Related feature evidence: [EV-002](#ev-002), [EV-053](#ev-053). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-010-f001-s001"></a>`MOD-010-F001-S001` | Read daily, weekly and monthly own-budget envelopes | Period boundaries and uncapped semantics are retained. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-078](#test-078) | [EV-002](#ev-002) — E26 daily/weekly/monthly own reads; monthly overview/settlement is separate. |
| <a id="mod-010-f001-s002"></a>`MOD-010-F001-S002` | Open monthly spend and per-org detail | Displayed totals preserve scope and monetary precision. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-010-f001-s003"></a>`MOD-010-F001-S003` | Paginate own runs during the selected period | Only matching runs are returned without duplicate continuation results. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-010-f001-s004"></a>`MOD-010-F001-S004` | Load a budget with unavailable or partial cost evidence | Unknown spend remains explicit and is not shown as zero. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-010-f001-s005"></a>`MOD-010-F001-S005` | Complete owned paid runs and compare the dashboard | Settled totals reconcile with their applicable periods and organizations. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-010-f002"></a>

#### MOD-010-F002 — Person caps/defaults, hierarchy editing, people spend and cap diagnostics

Module: [MOD-010](#mod-010). Previous audit ID: `BU-02`. Native references: #4620; #4629; #5626.

Surface: /budget. **Feature coverage: Partial E2E.** E35 reads/refusal; D06 owned limit mutation/cleanup without inference. New UI only checks hierarchy response.

Related feature evidence: [EV-054](#ev-054), [EV-002](#ev-002), [EV-041](#ev-041). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-010-f002-s001"></a>`MOD-010-F002-S001` | Read person caps and source hierarchy | Effective limit identifies the applicable individual/default source. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-085](#test-085) | [EV-002](#ev-002) — E35 source/readback and self-write refusal; org-admin/cross-person matrix not fully qualified. |
| <a id="mod-010-f002-s002"></a>`MOD-010-F002-S002` | Set or delete an owned cap/default as an authorized administrator | New effective limit persists with documented precedence. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-066](#test-066) | [EV-054](#ev-054) — D06 owned limits/config changes and cleanup; no paid enforcement. |
| <a id="mod-010-f002-s003"></a>`MOD-010-F002-S003` | Attempt unsupported self-write or org-admin cross-person mutation | Partition-free caps cannot be widened by an unauthorized actor. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-085](#test-085) | [EV-002](#ev-002) — E35 source/readback and self-write refusal; org-admin/cross-person matrix not fully qualified. |
| <a id="mod-010-f002-s004"></a>`MOD-010-F002-S004` | Search and paginate spend by person | Multi-org membership does not double-count the same person's spend. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-010-f002-s005"></a>`MOD-010-F002-S005` | Run the mis-partitioned-cap diagnostic | Report identifies anomalies without mutating budgets. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-010-f002-s006"></a>`MOD-010-F002-S006` | Execute spend across two owned organizations | Shared person ceiling and each organization's accounting remain correct. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-010-f003"></a>

#### MOD-010-F003 — Managed budget periods/configuration, allocation, status, summaries and cost calculation

Module: [MOD-010](#mod-010). Previous audit ID: `BU-03`. Native references: FR3; #4401; #5626.

Surface: Embedded budget controls; API / CLI. **Feature coverage: Partial E2E.** D06 mutation driver; clean-room budget cases 1–7 exercise selected cascading spend/denials. Does not prove every admin endpoint.

Related feature evidence: [EV-054](#ev-054), [EV-055](#ev-055). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-010-f003-s001"></a>`MOD-010-F003-S001` | Create/update/delete a managed budget period | Exact entity, period and revision are preserved through readback. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-066](#test-066) | [EV-054](#ev-054) — D06 owned budget mutation/revision driver; every period/entity combination is not established. |
| <a id="mod-010-f003-s002"></a>`MOD-010-F003-S002` | Allocate budgets at organization, department, team and user scope | Effective restriction follows documented cascading semantics. | PARTIAL-HARNESS | **5** (E2E 5, local 0); existing total unknown | [TEST-050](#test-050), [TEST-051](#test-051), [TEST-052](#test-052), [TEST-053](#test-053), [TEST-054](#test-054) | [EV-055](#ev-055) — Clean-room cases 1–7 exercise selected cascade/precedence/denial paths on Claude/Codex wires. |
| <a id="mod-010-f003-s003"></a>`MOD-010-F003-S003` | Spend through a configured limit on supported model protocols | Admission denies excess spend and settles accepted usage correctly. | PARTIAL-HARNESS | **6** (E2E 6, local 0); existing total unknown | [TEST-050](#test-050), [TEST-051](#test-051), [TEST-052](#test-052), [TEST-053](#test-053), [TEST-054](#test-054), [TEST-055](#test-055) | [EV-055](#ev-055) — Clean-room cases 1–7 exercise selected cascade/precedence/denial paths on Claude/Codex wires. |
| <a id="mod-010-f003-s004"></a>`MOD-010-F003-S004` | Inspect status, alerts, summaries and calculated costs | Responses agree on scope, period and monetary units. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-010-f003-s005"></a>`MOD-010-F003-S005` | Race stale budget mutation with an accepted update | Stale write is rejected without changing the newer budget. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-066](#test-066) | [EV-054](#ev-054) — D06 owned budget mutation/revision driver; every period/entity combination is not established. |
| <a id="mod-010-f003-s006"></a>`MOD-010-F003-S006` | Attempt management of an unowned entity | Budget read and mutation cannot cross authorized scope. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-010-f004"></a>

#### MOD-010-F004 — Platform/flow enforcement state and mutation

Module: [MOD-010](#mod-010). Previous audit ID: `BU-04`. Native references: none found.

Surface: /budget; /flows/:flowId. **Feature coverage: Local only.** Enforcement API/component coverage is separate from spend-through harness. No live UI toggle → worker enforcement transition found.

Related feature evidence: [EV-056](#ev-056), [EV-057](#ev-057). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-010-f004-s001"></a>`MOD-010-F004-S001` | Read platform and flow enforcement state | UI shows the persisted state for the exact scope. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-010-f004-s002"></a>`MOD-010-F004-S002` | Change enforcement state as an authorized operator | Subsequent admissions use the accepted state and revision. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-010-f004-s003"></a>`MOD-010-F004-S003` | Attempt a flow toggle against a foreign flow | Enforcement for another tenant cannot be altered. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-010-f004-s004"></a>`MOD-010-F004-S004` | Toggle while work is already running | Existing work follows the documented transition policy and new work follows the new setting. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-010-f004-s005"></a>`MOD-010-F004-S005` | Fail or race an enforcement update | UI does not report success for an uncommitted/stale state. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-011"></a>

### MOD-011 — Rate limits

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-011-F001](#mod-011-f001) | Rate-limit CRUD, effective hierarchy/status, RPM and concurrency | RL-01 | FR4; #185; #5627 |
| [MOD-011-F002](#mod-011-f002) | Tokens-per-minute enforcement | RL-02 | FR4.2 |

<a id="mod-011-f001"></a>

#### MOD-011-F001 — Rate-limit CRUD, effective hierarchy/status, RPM and concurrency

Module: [MOD-011](#mod-011). Previous audit ID: `RL-01`. Native references: FR4; #185; #5627.

Surface: /ratelimits; API. **Feature coverage: Partial E2E.** D06 configuration lifecycle; budget eval cases 8/10/11 enforcement. Legacy burst test accepts non-5xx and ignores exceptions.

Related feature evidence: [EV-054](#ev-054), [EV-055](#ev-055), [EV-058](#ev-058). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-011-f001-s001"></a>`MOD-011-F001-S001` | Create/read/update/delete an owned rate-limit configuration | Effective hierarchy and status reflect the persisted configuration. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-066](#test-066) | [EV-054](#ev-054) — D06 owned saved-limit lifecycle; inference enforcement is a separate driver. |
| <a id="mod-011-f001-s002"></a>`MOD-011-F001-S002` | Burst above RPM and concurrent-request limits | Expected throttling occurs and is asserted rather than inferred from non-5xx responses. | PARTIAL-HARNESS | **3** (E2E 3, local 0); existing total unknown | [TEST-056](#test-056), [TEST-058](#test-058), [TEST-059](#test-059) | [EV-055](#ev-055) — Cases 8/10/11 enforce selected RPM/concurrency paths; TPM case 9 skips. |
| <a id="mod-011-f001-s003"></a>`MOD-011-F001-S003` | Wait for the configured window/capacity to recover | Requests resume within the documented limit rules. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-011-f001-s004"></a>`MOD-011-F001-S004` | Exercise two tenants at once | One tenant's throttling cannot consume another tenant's independent allowance. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-011-f001-s005"></a>`MOD-011-F001-S005` | Attempt an unauthorized or malformed limit change | Existing limits remain effective and the request fails clearly. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-011-f002"></a>

#### MOD-011-F002 — Tokens-per-minute enforcement

Module: [MOD-011](#mod-011). Previous audit ID: `RL-02`. Native references: FR4.2.

Surface: API / model request. **Feature coverage: No E2E found.** Budget eval case 9 unconditionally skips; E36 reports unavailable TPM. Successful workflow does not prove enforcement.

Related feature evidence: [EV-055](#ev-055), [EV-002](#ev-002). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-011-f002-s001"></a>`MOD-011-F002-S001` | Exceed the configured token-per-minute allowance | A real enforcement refusal is observed for excess tokens. | PLACEHOLDER | **0** (E2E 0, local 0); existing total unknown | [TEST-057](#test-057); excluded: 1 placeholder | [EV-055](#ev-055) — TPM case 9 unconditionally skips; no implemented TPM enforcement assertion established. |
| <a id="mod-011-f002-s002"></a>`MOD-011-F002-S002` | Keep requests below TPM while varying request sizes | Valid requests remain usable within the token window. | PLACEHOLDER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-055](#ev-055) — TPM case 9 unconditionally skips; no implemented TPM enforcement assertion established. |
| <a id="mod-011-f002-s003"></a>`MOD-011-F002-S003` | Cross a token-window boundary during streaming | Accounting and replenishment follow the documented enforcement contract. | PLACEHOLDER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-055](#ev-055) — TPM case 9 unconditionally skips; no implemented TPM enforcement assertion established. |
| <a id="mod-011-f002-s004"></a>`MOD-011-F002-S004` | Combine TPM and RPM pressure | Refusal identifies the applicable restriction without bypassing either limit. | PLACEHOLDER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-055](#ev-055) — TPM case 9 unconditionally skips; no implemented TPM enforcement assertion established. |

<a id="mod-012"></a>

### MOD-012 — Dashboards

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-012-F001](#mod-012-f001) | Organization usage, pool health/account management and system metrics | DS-01 | FR8 |
| [MOD-012-F002](#mod-012-f002) | Usage charts, departments, admin users, top models, budgets and approval policy | DS-02 | FR8 |

<a id="mod-012-f001"></a>

#### MOD-012-F001 — Organization usage, pool health/account management and system metrics

Module: [MOD-012](#mod-012). Previous audit ID: `DS-01`. Native references: FR8.

Surface: /admin/system#organizations; #pool; #metrics. **Feature coverage: Partial E2E.** Legacy dashboard/pool live probes exist but weak non-5xx assertions do not prove balancing/failover or accurate metrics.

Related feature evidence: [EV-033](#ev-033), [EV-059](#ev-059). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-012-f001-s001"></a>`MOD-012-F001-S001` | Load system dashboard as platform admin | Organization usage, pool and metrics reflect authorized backend data. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-012-f001-s002"></a>`MOD-012-F001-S002` | Attempt system dashboard/API access as an ordinary user | Platform-only data is not disclosed. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-012-f001-s003"></a>`MOD-012-F001-S003` | Add/remove an owned pool account and refresh health | Pool membership and health agree with persisted state. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-012-f001-s004"></a>`MOD-012-F001-S004` | Distribute real inference then fail one pool destination | Remaining destinations serve traffic and observed distribution/failover matches policy. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-012-f001-s005"></a>`MOD-012-F001-S005` | Simulate unavailable metrics or pool health | UI reports unavailable/stale evidence rather than healthy zero values. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-012-f002"></a>

#### MOD-012-F002 — Usage charts, departments, admin users, top models, budgets and approval policy

Module: [MOD-012](#mod-012). Previous audit ID: `DS-02`. Native references: FR8.

Surface: /org/:orgId; /org/:orgId/department/:deptId. **Feature coverage: Partial E2E.** API/story tests plus hierarchy/budget drivers cover portions; no complete browser dashboard/approval-policy lifecycle found.

Related feature evidence: [EV-033](#ev-033), [EV-060](#ev-060), [EV-061](#ev-061). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-012-f002-s001"></a>`MOD-012-F002-S001` | Load organization/department dashboards as their administrators | Charts, budgets and hierarchy refer to the permitted scope. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-012-f002-s002"></a>`MOD-012-F002-S002` | Attempt a dashboard for another organization | Usage, users and budget information are denied. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-012-f002-s003"></a>`MOD-012-F002-S003` | Read top models and timeseries after known owned calls | Aggregates reflect the selected window and actual settled usage. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-012-f002-s004"></a>`MOD-012-F002-S004` | Change organization approval policy | Newly onboarded users follow the accepted policy server-side. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-012-f002-s005"></a>`MOD-012-F002-S005` | Navigate between department teams and budget allocations | UI retains the correct parent and renders persisted changes. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-013"></a>

### MOD-013 — Logs and audit

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-013-F001](#mod-013-f001) | Request-log filtering and privileged audit retrieval | DS-03 | #3747; #6037 |

<a id="mod-013-f001"></a>

#### MOD-013-F001 — Request-log filtering and privileged audit retrieval

Module: [MOD-013](#mod-013). Previous audit ID: `DS-03`. Native references: #3747; #6037.

Surface: /logs; API. **Feature coverage: Partial E2E.** Gateway story probes and CLI audit reads exist; no live action → complete attributed immutable audit trail acceptance established.

Related feature evidence: [EV-033](#ev-033), [EV-002](#ev-002). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-013-f001-s001"></a>`MOD-013-F001-S001` | Filter request logs by permitted scope/time/status | Matching results preserve continuation and attribution. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-013-f001-s002"></a>`MOD-013-F001-S002` | Read audit events as an authorized actor | Only the permitted audit scope is returned. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-013-f001-s003"></a>`MOD-013-F001-S003` | Perform an admin mutation and locate its audit record | Actor, target, operation, outcome and request identity correlate. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-013-f001-s004"></a>`MOD-013-F001-S004` | Attempt unauthorized log/audit retrieval | Foreign or privileged records are not disclosed. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-013-f001-s005"></a>`MOD-013-F001-S005` | Read during backend failure or ingestion delay | UI distinguishes failure/delay from an empty successful result. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-014"></a>

### MOD-014 — Usage and cost reporting

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-014-F001](#mod-014-f001) | Scoped summaries, models, timeline, users, departments and exports | US-01 | FR9; #5628 |

<a id="mod-014-f001"></a>

#### MOD-014-F001 — Scoped summaries, models, timeline, users, departments and exports

Module: [MOD-014](#mod-014). Previous audit ID: `US-01`. Native references: FR9; #5628.

Surface: Dashboards; API / CLI. **Feature coverage: Partial E2E.** E21 bounded export shapes/scopes; budget case 7 settles usage. Export smoke alone does not reconcile real spend.

Related feature evidence: [EV-062](#ev-062), [EV-055](#ev-055). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-014-f001-s001"></a>`MOD-014-F001-S001` | Read summaries by organization/model/user/department | Grouping and time windows preserve caller scope. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-014-f001-s002"></a>`MOD-014-F001-S002` | Export bounded usage as JSON, NDJSON and CSV | Shapes, continuation and exit status remain consistent. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-075](#test-075) | [EV-062](#ev-062) — E21 bounded JSON/NDJSON/CSV scope/shape/continuation; no billed-spend reconciliation. |
| <a id="mod-014-f001-s003"></a>`MOD-014-F001-S003` | Page through changing usage results | Continuation does not silently lose or duplicate rows within its contract. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-014-f001-s004"></a>`MOD-014-F001-S004` | Attempt managed usage export for an unowned organization | Export is refused without leaking foreign rows. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-014-f001-s005"></a>`MOD-014-F001-S005` | Compare exports with known settled inference | Attribution and billed amounts reconcile rather than only matching response shape. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-015"></a>

### MOD-015 — Agent activity and controls

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-015-F001](#mod-015-f001) | Run dashboard, filters/pagination, invocation chains, details and transcripts | AC-01 | #1457; #3633; #5629 |
| [MOD-015-F002](#mod-015-f002) | Ping/state/events, pause/resume/steer/abort and streamed explanations | AC-02 | #4539 |

<a id="mod-015-f001"></a>

#### MOD-015-F001 — Run dashboard, filters/pagination, invocation chains, details and transcripts

Module: [MOD-015](#mod-015). Previous audit ID: `AC-01`. Native references: #1457; #3633; #5629.

Surface: /runs; /activity. **Feature coverage: Partial E2E.** New UI observes real stats/invocation responses; E22 reads/refusals. All filters/transcripts/task views not proven through live browser.

Related feature evidence: [EV-041](#ev-041), [EV-063](#ev-063). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-015-f001-s001"></a>`MOD-015-F001-S001` | Load the run dashboard and invocation list | Real stats and list responses correspond to the selected tenant. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-096](#test-096) | [EV-041](#ev-041) — Browser observes real runs/activity responses; filters and terminal task progression remain separate. |
| <a id="mod-015-f001-s002"></a>`MOD-015-F001-S002` | Filter by status, channel, persona, date and liveness | Result rows satisfy each selected predicate and pagination remains consistent. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-015-f001-s003"></a>`MOD-015-F001-S003` | Open a chain, invocation detail and transcript | Related records retain correlation and authorized visibility. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-015-f001-s004"></a>`MOD-015-F001-S004` | Read missing or foreign runs | Missing/refused states are structured without leaking details. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-076](#test-076) | [EV-063](#ev-063) — E22 missing-run structured errors; universal foreign-run authorization remains separate. |
| <a id="mod-015-f001-s005"></a>`MOD-015-F001-S005` | Follow a newly started task to a terminal state | Dashboard, activity, transcript and final result converge consistently. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-015-f002"></a>

#### MOD-015-F002 — Ping/state/events, pause/resume/steer/abort and streamed explanations

Module: [MOD-015](#mod-015). Previous audit ID: `AC-02`. Native references: #4539.

Surface: /activity; flow/run controls. **Feature coverage: Partial E2E.** agent-control browser suite defaults to mocks with opt-in live. Explanations use local SSE fixture; E22 explicitly no active-control claim.

Related feature evidence: [EV-064](#ev-064), [EV-065](#ev-065), [EV-063](#ev-063). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-015-f002-s001"></a>`MOD-015-F002-S001` | Ping/read state for an owned active agent | Returned identity and state correspond to the target run. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-015-f002-s002"></a>`MOD-015-F002-S002` | Pause and resume active work | Worker observes each accepted control and execution changes accordingly. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-015-f002-s003"></a>`MOD-015-F002-S003` | Steer and abort an active run | Steering is bound to the run and abort prevents further unauthorized work. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-015-f002-s004"></a>`MOD-015-F002-S004` | Send a control for a stale, terminal or foreign run | Request is refused or safely treated according to the control contract. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-015-f002-s005"></a>`MOD-015-F002-S005` | Stream implementation explanations during execution | Content arrives before completion and remains correlated to the intended run. | LOCAL-ONLY | **1** (E2E 0, local 1); existing total unknown | [TEST-033](#test-033) | [EV-065](#ev-065) — Browser SSE fixture tests streaming-before-completion; cloud transport is not exercised. |
| <a id="mod-015-f002-s006"></a>`MOD-015-F002-S006` | Disconnect and reconnect control/event streaming | State recovery does not invent control acknowledgements or duplicate actions. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-016"></a>

### MOD-016 — Chat

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-016-F001](#mod-016-f001) | Authenticated sessions, send/stream/tool calls, attachments and conversation switching | CH-01 | #53; #97 |
| [MOD-016-F002](#mod-016-f002) | Filter request history by model/date and inspect request/response detail | CH-02 | #179 |
| [MOD-016-F003](#mod-016-f003) | Authorized history, compaction, memory, drafts, session ACLs and artifacts | CH-03 | Not found; master ID is canonical |

<a id="mod-016-f001"></a>

#### MOD-016-F001 — Authenticated sessions, send/stream/tool calls, attachments and conversation switching

Module: [MOD-016](#mod-016). Previous audit ID: `CH-01`. Native references: #53; #97.

Surface: /chat. **Feature coverage: Partial E2E.** Real Chat browser/WS harness exists; latest run 3 passed/25 deselected in disabled-target mode. Known durability xfails remain.

Related feature evidence: [EV-066](#ev-066), [EV-067](#ev-067). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-016-f001-s001"></a>`MOD-016-F001-S001` | Sign in and create a real chat session | Server owns session identity and WebSocket connection is authenticated. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-064](#test-064) | [EV-035](#ev-035) [test_ws_created_on_chat_page](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/chat/test_auth.py#L110) — WebSocket creation is checked; no full session-creation lifecycle in this assertion. |
| <a id="mod-016-f001-s002"></a>`MOD-016-F001-S002` | Send a message that invokes a supported tool | Acknowledgement, tool progress and correlated final response arrive. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-016-f001-s003"></a>`MOD-016-F001-S003` | Stream a long reply through completion | Ordered content and terminal state render without truncation or duplicate completion. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-016-f001-s004"></a>`MOD-016-F001-S004` | Create/select/delete conversations and reload | Session ownership and history follow the selected conversation. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-016-f001-s005"></a>`MOD-016-F001-S005` | Upload and send an attachment | Authorized worker can use the attached content and reports upload failures clearly. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-016-f001-s006"></a>`MOD-016-F001-S006` | Disconnect/reconnect during a turn | Supported recovery preserves session isolation and does not attribute late replies to another conversation. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-016-f002"></a>

#### MOD-016-F002 — Filter request history by model/date and inspect request/response detail

Module: [MOD-016](#mod-016). Previous audit ID: `CH-02`. Native references: #179.

Surface: /my-chats. **Feature coverage: Local only.** Gateway/frontend tests exist; agent conversation-history tests are not evidence for this separate request-log screen.

Related feature evidence: [EV-034](#ev-034), [EV-068](#ev-068). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-016-f002-s001"></a>`MOD-016-F002-S001` | Filter My Chats by model and date | Only authorized request-log conversations in the selected range appear. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-016-f002-s002"></a>`MOD-016-F002-S002` | Open request/response detail | Detail matches the selected request and preserves content formatting. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-016-f002-s003"></a>`MOD-016-F002-S003` | Navigate a history larger than one page | Continuation returns each accessible request according to the list contract. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-016-f002-s004"></a>`MOD-016-F002-S004` | Request another user's chat detail directly | Private request/response content is refused. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-016-f002-s005"></a>`MOD-016-F002-S005` | Load an empty history or failed history request | UI distinguishes no records from an unavailable service. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-016-f003"></a>

#### MOD-016-F003 — Authorized history, compaction, memory, drafts, session ACLs and artifacts

Module: [MOD-016](#mod-016). Previous audit ID: `CH-03`. Native references: none found.

Surface: Internal chat API. **Feature coverage: Partial E2E.** Agent-factory live memory/artifact harnesses are opt-in; API boundary unit tests do not establish all current deployed paths.

Related feature evidence: [EV-069](#ev-069), [EV-070](#ev-070). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-016-f003-s001"></a>`MOD-016-F003-S001` | Read/append/compact history through an authorized worker | Messages and summaries remain scoped to the permitted session. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-016-f003-s002"></a>`MOD-016-F003-S002` | Write and recall a personal memory | Readback is scoped to the intended owner across sessions. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-016-f003-s003"></a>`MOD-016-F003-S003` | Create/read a draft then resume it | Draft content persists without crossing user or workspace boundaries. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-016-f003-s004"></a>`MOD-016-F003-S004` | Read/change session ACLs using authorized authority | Access follows current ACLs and invalid grants are refused. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-016-f003-s005"></a>`MOD-016-F003-S005` | Create/list/read a session artifact | Artifact ownership, metadata and content remain consistent. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-016-f003-s006"></a>`MOD-016-F003-S006` | Attempt another session's history/memory/artifact access | Forged session identifiers cannot widen worker authority. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-017"></a>

### MOD-017 — Knowledge and indexing

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-017-F001](#mod-017-f001) | Personal/tenant assets, GitHub repo picker, add/upload, list/delete/reindex/status | KN-01 | #1794; #2045; #5632 |
| [MOD-017-F002](#mod-017-f002) | Bulk upload preview/commit, validation and partial result handling | KN-02 | #2045 |
| [MOD-017-F003](#mod-017-f003) | Indexing run list/details/tool status and ingestion callback | KN-03 | #1424; #2049 |

<a id="mod-017-f001"></a>

#### MOD-017-F001 — Personal/tenant assets, GitHub repo picker, add/upload, list/delete/reindex/status

Module: [MOD-017](#mod-017). Previous audit ID: `KN-01`. Native references: #1794; #2045; #5632.

Surface: /knowledge. **Feature coverage: Partial E2E.** Browser CRUD mocks APIs; E32 read/previews; D04 owns asset lifecycle. Live indexing/retrieval qualification remains separate.

Related feature evidence: [EV-071](#ev-071), [EV-072](#ev-072), [EV-002](#ev-002). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-017-f001-s001"></a>`MOD-017-F001-S001` | Register personal and tenant knowledge assets | Each asset is visible only to its permitted scope. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-017-f001-s002"></a>`MOD-017-F001-S002` | Choose an accessible GitHub repository | Picker excludes inaccessible installations/repositories. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-017-f001-s003"></a>`MOD-017-F001-S003` | Upload/watch/reindex an owned asset | Registry state progresses through dispatch and indexing status. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-070](#test-070) | [EV-072](#ev-072) — D04 owned asset lifecycle; complete live indexing/retrieval is separately held. |
| <a id="mod-017-f001-s004"></a>`MOD-017-F001-S004` | Delete an owned asset | Removed asset stops appearing as active and cannot grant retrieval access. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-070](#test-070) | [EV-072](#ev-072) — D04 owned asset lifecycle; complete live indexing/retrieval is separately held. |
| <a id="mod-017-f001-s005"></a>`MOD-017-F001-S005` | Read missing assets and preview removal through CLI | Errors/previews are structured and preview itself makes no mutation. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-082](#test-082) | [EV-002](#ev-002) — E32 discovery/status-error/removal preview; no mutation or live retrieval claim. |
| <a id="mod-017-f001-s006"></a>`MOD-017-F001-S006` | Retrieve indexed content as permitted and forbidden users | End-to-end retrieval honors current source permissions. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-017-f002"></a>

#### MOD-017-F002 — Bulk upload preview/commit, validation and partial result handling

Module: [MOD-017](#mod-017). Previous audit ID: `KN-02`. Native references: #2045.

Surface: /knowledge → Bulk upload. **Feature coverage: Local only.** Asset/bulk API and local browser tests; no complete bulk → all indexes → scoped retrieval acceptance found.

Related feature evidence: [EV-073](#ev-073), [EV-071](#ev-071). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-017-f002-s001"></a>`MOD-017-F002-S001` | Preview a valid multi-asset upload | Parsed rows and quota impact are shown without registry writes. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-017-f002-s002"></a>`MOD-017-F002-S002` | Preview malformed, duplicate and over-quota rows | Validation identifies each affected row before commit. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-017-f002-s003"></a>`MOD-017-F002-S003` | Commit the reviewed batch | Accepted assets are persisted and dispatched exactly according to the batch contract. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-017-f002-s004"></a>`MOD-017-F002-S004` | Retry a commit after interrupted acknowledgement | No unintended duplicate assets or dispatches are created. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-017-f002-s005"></a>`MOD-017-F002-S005` | Wait for all committed assets to index then retrieve them | Per-row outcome and authorized searchable content agree. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-017-f003"></a>

#### MOD-017-F003 — Indexing run list/details/tool status and ingestion callback

Module: [MOD-017](#mod-017). Previous audit ID: `KN-03`. Native references: #1424; #2049.

Surface: /admin/indexing. **Feature coverage: Partial E2E.** Agent-context integration/live tooling exists; current browser-to-queue-to-index-complete chain is not established by run-status reads.

Related feature evidence: [EV-074](#ev-074), [EV-075](#ev-075). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-017-f003-s001"></a>`MOD-017-F003-S001` | List indexing runs and expand a run | Tool-specific status corresponds to the selected persisted run. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-017-f003-s002"></a>`MOD-017-F003-S002` | Deliver a valid ingestion status callback | The correct asset/run moves to its accepted next state. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-017-f003-s003"></a>`MOD-017-F003-S003` | Replay a stale or unauthorized status callback | Callback cannot regress state or mutate another tenant's run. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-017-f003-s004"></a>`MOD-017-F003-S004` | Dispatch a new asset and follow it to completion | UI, queue processing and index outcome agree. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-017-f003-s005"></a>`MOD-017-F003-S005` | Disable Context or remove its queue dependency | Readiness/error state is explicit; no false completed indexing is shown. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-018"></a>

### MOD-018 — Delivery flows

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-018-F001](#mod-018-f001) | Flow list/graph, plans, costs, run reports, execution and rollups | FL-01 | #4869; #4212; #5630 |
| [MOD-018-F002](#mod-018-f002) | Intake conversation, draft generation/registration/revision and plan acceptance | FL-02 | #4528; #5331 |
| [MOD-018-F003](#mod-018-f003) | Gate approve/reject, node recovery/resume and execution start/pause | FL-03 | #4200; #4213 |
| [MOD-018-F004](#mod-018-f004) | Continuation, amendment, wave dependencies and PR recovery | FL-04 | Not found; master ID is canonical |
| [MOD-018-F005](#mod-018-f005) | Budget, concurrency, retry and execution-window preview/accept | FL-05 | Not found; master ID is canonical |
| [MOD-018-F006](#mod-018-f006) | Evaluation acceptance and waiver preview/accept | FL-06 | Not found; master ID is canonical |
| [MOD-018-F007](#mod-018-f007) | Plan amendments and implementation PR recovery | FL-07 | #4200 |
| [MOD-018-F008](#mod-018-f008) | Develop, review/repair, merge/deploy and dependency progression | BACK-042 | #5157; A6-2.* |
| [MOD-018-F009](#mod-018-f009) | Worker loss, retry/recovery, tenant/policy/budget and human refusal | BACK-043 | A6-3.*; A6-4.* |
| [MOD-018-F010](#mod-018-f010) | Complete autonomous intervention accounting | BACK-044 | A6-6.interventions; #4539 |
| [MOD-018-F011](#mod-018-f011) | CLI flow reads and recovery | BACK-045 | #5630 |

<a id="mod-018-f001"></a>

#### MOD-018-F001 — Flow list/graph, plans, costs, run reports, execution and rollups

Module: [MOD-018](#mod-018). Previous audit ID: `FL-01`. Native references: #4869; #4212; #5630.

Surface: /flows; /flows/:flowId. **Feature coverage: Partial E2E.** E37 reads/refusal; orchestration live harness exists but no recent main-filtered run returned. UI graph is not full delivery acceptance.

Related feature evidence: [EV-002](#ev-002), [EV-076](#ev-076). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-018-f001-s001"></a>`MOD-018-F001-S001` | List flows and open a graph | Nodes, dependencies, plans and owner match the selected flow. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-086](#test-086) | [EV-002](#ev-002) — E37 bounded flow reads/refusal; browser graph and full permission matrix remain separate. |
| <a id="mod-018-f001-s002"></a>`MOD-018-F001-S002` | Read costs and execution rollups | Unknown/estimated values stay distinguishable and totals use documented scope. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f001-s003"></a>`MOD-018-F001-S003` | Read run reports and linked work | Each report belongs to the correct node/run and tenant. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f001-s004"></a>`MOD-018-F001-S004` | Open missing/foreign flows | Refusal or absence is structured without disclosing another tenant's graph. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-086](#test-086) | [EV-002](#ev-002) — E37 bounded flow reads/refusal; browser graph and full permission matrix remain separate. |
| <a id="mod-018-f001-s005"></a>`MOD-018-F001-S005` | Refresh a progressing flow | Graph and rollups converge on actual execution/terminal states. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-018-f002"></a>

#### MOD-018-F002 — Intake conversation, draft generation/registration/revision and plan acceptance

Module: [MOD-018](#mod-018). Previous audit ID: `FL-02`. Native references: #4528; #5331.

Surface: Chat draft panel; API / CLI. **Feature coverage: Partial E2E.** Contract tests and live orchestration tooling cover portions; E37 does not accept real plans.

Related feature evidence: [EV-076](#ev-076), [EV-002](#ev-002). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-018-f002-s001"></a>`MOD-018-F002-S001` | Create an intake session and add turns | Planning conversation persists with the authorized author and tenant. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f002-s002"></a>`MOD-018-F002-S002` | Generate/register a draft plan | Output remains inert until an authorized acceptance step. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f002-s003"></a>`MOD-018-F002-S003` | Revise a draft from its current revision | New draft preserves version history and rejects stale edits. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f002-s004"></a>`MOD-018-F002-S004` | Attempt to approve a plan using draft-only authority | Authoring permission cannot promote execution. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f002-s005"></a>`MOD-018-F002-S005` | Accept an eligible reviewed plan | Accepted identity/hash binds the plan used for subsequent dispatch. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-018-f003"></a>

#### MOD-018-F003 — Gate approve/reject, node recovery/resume and execution start/pause

Module: [MOD-018](#mod-018). Previous audit ID: `FL-03`. Native references: #4200; #4213.

Surface: /flows/:flowId. **Feature coverage: Partial E2E.** Control contracts and opt-in live tests; no recurring complete browser gate → worker → result journey established.

Related feature evidence: [EV-076](#ev-076), [EV-064](#ev-064). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-018-f003-s001"></a>`MOD-018-F003-S001` | Approve or reject a gate with its current plan hash | Exactly the reviewed decision changes the gate's execution state. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f003-s002"></a>`MOD-018-F003-S002` | Submit stale or foreign gate decisions | No execution authority is granted from invalid review context. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f003-s003"></a>`MOD-018-F003-S003` | Preview recovery and resume a recoverable node | The accepted recovery restores only the intended continuation. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f003-s004"></a>`MOD-018-F003-S004` | Start/pause/resume a flow | Persisted execution control is observed by real workers. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f003-s005"></a>`MOD-018-F003-S005` | Attempt worker self-approval through an internal route | Worker authority cannot substitute for human/operator approval. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-018-f004"></a>

#### MOD-018-F004 — Continuation, amendment, wave dependencies and PR recovery

Module: [MOD-018](#mod-018). Previous audit ID: `FL-04`. Native references: none found.

Surface: /flows/:flowId where exposed; API / CLI. **Feature coverage: Local only.** API/state-machine tests exist; no complete live lifecycle for every preview/accept pair established.

Related feature evidence: [EV-076](#ev-076). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-018-f004-s001"></a>`MOD-018-F004-S001` | Preview and accept a continuation | Accepted work extends the reviewed flow without duplicating prior execution. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f004-s002"></a>`MOD-018-F004-S002` | Append work through an amendment | New nodes preserve valid ownership and graph dependencies. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f004-s003"></a>`MOD-018-F004-S003` | Change wave dependencies | Cycles or invalid references fail; valid changes apply to the reviewed revision only. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f004-s004"></a>`MOD-018-F004-S004` | Recover a missing or incorrect implementation PR binding | Recovery verifies the intended repository/story/run before reuse. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f004-s005"></a>`MOD-018-F004-S005` | Repeat or race acceptance of the same preview | No duplicate continuation or stale graph update is admitted. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-018-f005"></a>

#### MOD-018-F005 — Budget, concurrency, retry and execution-window preview/accept

Module: [MOD-018](#mod-018). Previous audit ID: `FL-05`. Native references: none found.

Surface: /flows/:flowId where exposed; API / CLI. **Feature coverage: Local only.** API/state-machine tests exist; no complete live lifecycle for every preview/accept pair established.

Related feature evidence: [EV-076](#ev-076). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-018-f005-s001"></a>`MOD-018-F005-S001` | Preview/accept a flow budget change | New admissions use the accepted limit and retain its revision. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f005-s002"></a>`MOD-018-F005-S002` | Preview/accept concurrency changes | Workers observe the configured ceiling without exceeding allowed parallelism. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f005-s003"></a>`MOD-018-F005-S003` | Preview/accept a retry policy | Retryable failures obey the accepted attempt/backoff rules. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f005-s004"></a>`MOD-018-F005-S004` | Preview/accept an execution window | Work waits/runs according to the accepted schedule and timezone rules. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f005-s005"></a>`MOD-018-F005-S005` | Submit stale or unauthorized policy acceptance | The effective flow policy remains unchanged. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-018-f006"></a>

#### MOD-018-F006 — Evaluation acceptance and waiver preview/accept

Module: [MOD-018](#mod-018). Previous audit ID: `FL-06`. Native references: none found.

Surface: /flows/:flowId where exposed; API / CLI. **Feature coverage: Local only.** API/state-machine tests exist; no complete live lifecycle for every preview/accept pair established.

Related feature evidence: [EV-076](#ev-076). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-018-f006-s001"></a>`MOD-018-F006-S001` | Preview and accept sufficient evaluation evidence | Gate state binds the verified evidence and exact reviewed revision. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f006-s002"></a>`MOD-018-F006-S002` | Submit absent, stale or mismatched evidence | Evaluation acceptance is refused without promoting the flow. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f006-s003"></a>`MOD-018-F006-S003` | Preview/accept a permitted evaluation waiver | Waiver records the authorized actor, reason and bounded scope. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f006-s004"></a>`MOD-018-F006-S004` | Attempt a waiver from an unauthorized role | No evaluation requirement is bypassed. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f006-s005"></a>`MOD-018-F006-S005` | Replay acceptance after plan/evidence changes | Old evidence or waiver cannot silently qualify new work. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-018-f007"></a>

#### MOD-018-F007 — Plan amendments and implementation PR recovery

Module: [MOD-018](#mod-018). Previous audit ID: `FL-07`. Native references: #4200.

Surface: API / CLI. **Feature coverage: Local only.** Orchestration contract tests; no recurring real provider recovery journey established.

Related feature evidence: [EV-076](#ev-076). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-018-f007-s001"></a>`MOD-018-F007-S001` | Submit a valid plan amendment | Plan lineage and accepted scope include the exact intended change. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f007-s002"></a>`MOD-018-F007-S002` | Amend with stale or invalid graph references | Current plan remains unchanged. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f007-s003"></a>`MOD-018-F007-S003` | Recover an implementation PR after a known delivery failure | Binding and provider state correspond to the intended node. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f007-s004"></a>`MOD-018-F007-S004` | Supply a foreign repository/PR during recovery | Recovery does not attach unrelated code or widen scope. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f007-s005"></a>`MOD-018-F007-S005` | Retry recovery after lost acknowledgement | One durable binding is retained without duplicate provider actions. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-018-f008"></a>

#### MOD-018-F008 — Develop, review/repair, merge/deploy and dependency progression

Module: [MOD-018](#mod-018). Previous audit ID: `BACK-042`. Native references: #5157; A6-2.*.

Surface: Backend / CLI / integration. **Feature coverage: Blocked.** Real scenario adapters exist, but prerequisites and approved fixture manifest are required; Q3 live acceptance is explicitly outstanding. No workflow runs returned in inspected main history.

Existing execution selection/limits: Manual orchestration-live-tests; PR runs harness tests only.

Related feature evidence: [EV-111](#ev-111), [EV-112](#ev-112). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-018-f008-s001"></a>`MOD-018-F008-S001` | Execute a bounded develop/review flow on an owned repository | Implemented change and review evidence attach to the intended plan. | BLOCKED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-111](#ev-111) — Real scenario adapters exist, but prerequisites and approved fixture manifest are required; Q3 live acceptance is explicitly outstanding. No workflow runs returned in inspected main history. |
| <a id="mod-018-f008-s002"></a>`MOD-018-F008-S002` | Require repair after review findings | Repair addresses the findings before promotion proceeds. | BLOCKED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-111](#ev-111) — Real scenario adapters exist, but prerequisites and approved fixture manifest are required; Q3 live acceptance is explicitly outstanding. No workflow runs returned in inspected main history. |
| <a id="mod-018-f008-s003"></a>`MOD-018-F008-S003` | Merge/deploy only after required approvals | Real provider/deployment outcome matches the accepted workflow. | BLOCKED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-111](#ev-111) — Real scenario adapters exist, but prerequisites and approved fixture manifest are required; Q3 live acceptance is explicitly outstanding. No workflow runs returned in inspected main history. |
| <a id="mod-018-f008-s004"></a>`MOD-018-F008-S004` | Execute dependent nodes/waves | Dependencies prevent early execution and release eligible work on actual completion. | BLOCKED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-111](#ev-111) — Real scenario adapters exist, but prerequisites and approved fixture manifest are required; Q3 live acceptance is explicitly outstanding. No workflow runs returned in inspected main history. |

<a id="mod-018-f009"></a>

#### MOD-018-F009 — Worker loss, retry/recovery, tenant/policy/budget and human refusal

Module: [MOD-018](#mod-018). Previous audit ID: `BACK-043`. Native references: A6-3.*; A6-4.*.

Surface: Backend / CLI / integration. **Feature coverage: Partial.** Fault adapters exist with mandatory NOT_RUN semantics; halt/stop requires deployed capability. Offline tests cannot prove durable runtime recovery.

Existing execution selection/limits: Manual qualification; not enabled by PR.

Related feature evidence: [EV-113](#ev-113), [EV-114](#ev-114), [EV-111](#ev-111). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-018-f009-s001"></a>`MOD-018-F009-S001` | Terminate a worker mid-node | Durable recovery respects leases and prevents duplicate execution authority. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f009-s002"></a>`MOD-018-F009-S002` | Trigger a retryable and a terminal failure | Retry policy distinguishes them and records attempts correctly. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f009-s003"></a>`MOD-018-F009-S003` | Refuse work because of tenant/model/budget policy | No unauthorized provider action occurs after refusal. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f009-s004"></a>`MOD-018-F009-S004` | Reject a required human approval or request stop | Flow halts as documented and unexecuted adapters report NOT_RUN honestly. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-018-f010"></a>

#### MOD-018-F010 — Complete autonomous intervention accounting

Module: [MOD-018](#mod-018). Previous audit ID: `BACK-044`. Native references: A6-6.interventions; #4539.

Surface: Backend / CLI / integration. **Feature coverage: Blocked.** Bounded worker acknowledgement journal cannot prove complete pause/resume/steer history; A6-6 stays NOT_RUN by documented contract.

Existing execution selection/limits: No complete acceptance.

Related feature evidence: [EV-115](#ev-115), [EV-111](#ev-111). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-018-f010-s001"></a>`MOD-018-F010-S001` | Pause/resume/steer during autonomous work | Durable journal records every accepted intervention with actor and time. | BLOCKED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-115](#ev-115) — Bounded worker acknowledgement journal cannot prove complete pause/resume/steer history; A6-6 stays NOT_RUN by documented contract. |
| <a id="mod-018-f010-s002"></a>`MOD-018-F010-S002` | Compare journal with worker acknowledgements | Missing or unacknowledged controls are identified rather than assumed complete. | BLOCKED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-115](#ev-115) — Bounded worker acknowledgement journal cannot prove complete pause/resume/steer history; A6-6 stays NOT_RUN by documented contract. |
| <a id="mod-018-f010-s003"></a>`MOD-018-F010-S003` | Recover the journal after failure | Intervention history survives the required restart/recovery boundary. | BLOCKED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-115](#ev-115) — Bounded worker acknowledgement journal cannot prove complete pause/resume/steer history; A6-6 stays NOT_RUN by documented contract. |
| <a id="mod-018-f010-s004"></a>`MOD-018-F010-S004` | Produce complete intervention accounting | Qualification remains blocked/NOT_RUN until required history is authoritative. | BLOCKED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-115](#ev-115) — Bounded worker acknowledgement journal cannot prove complete pause/resume/steer history; A6-6 stays NOT_RUN by documented contract. |

<a id="mod-018-f011"></a>

#### MOD-018-F011 — CLI flow reads and recovery

Module: [MOD-018](#mod-018). Previous audit ID: `BACK-045`. Native references: #5630.

Surface: Backend / CLI / integration. **Feature coverage: Partial.** E37 asserts flow reads and malformed recovery refusal; owned accepted-flow recovery is fixture-gated.

Existing execution selection/limits: Current nightly/PR daily.

Related feature evidence: [EV-002](#ev-002). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-018-f011-s001"></a>`MOD-018-F011-S001` | Read flows through the served CLI | Result shape and scope match actual accessible flows. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-086](#test-086) | [EV-002](#ev-002) — E37 reads and malformed recovery refusal; accepted-flow mutation remains fixture-gated. |
| <a id="mod-018-f011-s002"></a>`MOD-018-F011-S002` | Submit malformed recovery input | Structured refusal occurs without changing the flow. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-086](#test-086) | [EV-002](#ev-002) — E37 reads and malformed recovery refusal; accepted-flow mutation remains fixture-gated. |
| <a id="mod-018-f011-s003"></a>`MOD-018-F011-S003` | Recover an owned accepted flow using required fixtures | Recovery changes only the reviewed target and persists its outcome. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-018-f011-s004"></a>`MOD-018-F011-S004` | Attempt recovery against another tenant's flow | No foreign state or provider action is changed. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-019"></a>

### MOD-019 — Task API

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-019-F001](#mod-019-f001) | Public task submission, snapshots/events, messages/cancel, upload/download artifacts and reports | TK-01 | #5799; T2; T5 |
| [MOD-019-F002](#mod-019-f002) | Admission, dispatch/recovery claim/settle, worker acquisition/heartbeat and tool authority | TK-02 | #5796 |
| [MOD-019-F003](#mod-019-f003) | Contracts and component integration | BACK-046 | T0–T8 #5793–#5801; V0 |
| [MOD-019-F004](#mod-019-f004) | Admission, durable storage, dispatch and execution authority | BACK-047 | V1 #5802; V2 #5803 |
| [MOD-019-F005](#mod-019-f005) | SSE/replay, clarification, cancellation and external client | BACK-048 | V3 #5804; V4 #5805 |
| [MOD-019-F006](#mod-019-f006) | Coexistence, admission drain and rollout/rollback | BACK-049 | V5 #5806 |

<a id="mod-019-f001"></a>

#### MOD-019-F001 — Public task submission, snapshots/events, messages/cancel, upload/download artifacts and reports

Module: [MOD-019](#mod-019). Previous audit ID: `TK-01`. Native references: #5799; T2; T5.

Surface: /domain-mri; activity; CLI / API. **Feature coverage: Partial E2E.** Task acceptance manifest: 92 criteria, 83 runnable and 9 not implemented; runnable includes retained-evidence checks. E42 hosted coding covers one fixture journey.

Related feature evidence: [EV-077](#ev-077), [EV-078](#ev-078). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-019-f001-s001"></a>`MOD-019-F001-S001` | Submit a public task with an idempotency key | One authorized task is admitted and returned with stable identity. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-068](#test-068) | [EV-078](#ev-078) — E42 bounded owned repository-task submit/replay; not every Task persona/input. |
| <a id="mod-019-f001-s002"></a>`MOD-019-F001-S002` | Replay identical and conflicting submissions | Identical replay reuses identity; conflicting payload is refused. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-068](#test-068) | [EV-078](#ev-078) — E42 bounded owned repository-task submit/replay; not every Task persona/input. |
| <a id="mod-019-f001-s003"></a>`MOD-019-F001-S003` | Read task snapshot and resume its event stream | State, sequence and terminal outcome remain consistent. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-019-f001-s004"></a>`MOD-019-F001-S004` | Send a clarification/message and request cancellation | Commands apply to the intended task with documented terminal behavior. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-019-f001-s005"></a>`MOD-019-F001-S005` | Upload and download an authorized task artifact | Content, metadata and integrity headers agree with recorded ownership. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-019-f001-s006"></a>`MOD-019-F001-S006` | Attempt foreign-task events/messages/artifacts | Every public surface enforces the task's tenant and caller scope. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-019-f002"></a>

#### MOD-019-F002 — Admission, dispatch/recovery claim/settle, worker acquisition/heartbeat and tool authority

Module: [MOD-019](#mod-019). Previous audit ID: `TK-02`. Native references: #5796.

Surface: Internal API. **Feature coverage: Partial E2E.** Task qualification and worker contract/integration tests; no blanket current live coverage for all dispatch, recovery and tool operations.

Related feature evidence: [EV-077](#ev-077), [EV-079](#ev-079). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-019-f002-s001"></a>`MOD-019-F002-S001` | Claim and settle dispatch for an admitted task | Exactly one valid publication/attempt is recognized. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-019-f002-s002"></a>`MOD-019-F002-S002` | Acquire and heartbeat a worker lease | Only the authorized worker holds current task execution authority. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-019-f002-s003"></a>`MOD-019-F002-S003` | Lose a worker and invoke bounded recovery | Expired authority cannot continue; recovery does not duplicate admitted work. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-019-f002-s004"></a>`MOD-019-F002-S004` | Bootstrap/advance task turns with current credentials | Runtime decisions bind the task, attempt, principal and policy. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-019-f002-s005"></a>`MOD-019-F002-S005` | Authorize and execute a tool operation | Tool parameters and receipts stay within the admitted task scope. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-019-f002-s006"></a>`MOD-019-F002-S006` | Attempt repository publication or completion from a stale worker | No unauthorized source/result is published. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-019-f003"></a>

#### MOD-019-F003 — Contracts and component integration

Module: [MOD-019](#mod-019). Previous audit ID: `BACK-046`. Native references: T0–T8 #5793–#5801; V0.

Surface: Backend / CLI / integration. **Feature coverage: Contract/local only.** Versioned 92-criterion manifest exists: 83 commands marked runnable and 9 not_implemented. Runnable includes unit checks and recorded-evidence verifiers, not 83 live tests.

Existing execution selection/limits: Component CI; no full recurring live Task API suite in PR.

Related feature evidence: [EV-077](#ev-077), [EV-116](#ev-116), [EV-117](#ev-117). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-019-f003-s001"></a>`MOD-019-F003-S001` | Validate published Task API requests/responses against versioned contracts | External client and service agree on required types and fields. | LOCAL-ONLY | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-077](#ev-077) — Manifest/check commands distinguish runnable from not_implemented; runnable does not mean live execution. |
| <a id="mod-019-f003-s002"></a>`MOD-019-F003-S002` | Run each runnable evaluation-manifest criterion | Result records actual execution mode, outcome and evidence. | LOCAL-ONLY | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-077](#ev-077) — Manifest/check commands distinguish runnable from not_implemented; runnable does not mean live execution. |
| <a id="mod-019-f003-s003"></a>`MOD-019-F003-S003` | Select a not-implemented criterion | It stays explicitly unqualified rather than counting as a successful test. | LOCAL-ONLY | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-077](#ev-077) — Manifest/check commands distinguish runnable from not_implemented; runnable does not mean live execution. |
| <a id="mod-019-f003-s004"></a>`MOD-019-F003-S004` | Verify external-client malformed/error cases | Unsupported inputs and service refusals preserve stable public error semantics. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-019-f004"></a>

#### MOD-019-F004 — Admission, durable storage, dispatch and execution authority

Module: [MOD-019](#mod-019). Previous audit ID: `BACK-047`. Native references: V1 #5802; V2 #5803.

Surface: Backend / CLI / integration. **Feature coverage: Historical evidence.** September 25 criterion reports record qualification. Current commands verify retained reports or run component tests; they do not re-execute that deployment qualification.

Existing execution selection/limits: Retained evidence and CI; not daily live replay.

Related feature evidence: [EV-118](#ev-118), [EV-119](#ev-119), [EV-120](#ev-120). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-019-f004-s001"></a>`MOD-019-F004-S001` | Admit a task and verify durable persistence | Accepted identity/input survive the defined failure boundary. | HISTORICAL | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-118](#ev-118) — Related retained feature qualification exists; this entire scenario is not independently verified on the current deployment. |
| <a id="mod-019-f004-s002"></a>`MOD-019-F004-S002` | Dispatch the admitted task to an authorized worker | Task authority and actual execution agree. | HISTORICAL | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-118](#ev-118) — Related retained feature qualification exists; this entire scenario is not independently verified on the current deployment. |
| <a id="mod-019-f004-s003"></a>`MOD-019-F004-S003` | Replay admission after a lost response | Durable idempotency prevents a second independent task. | HISTORICAL | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-118](#ev-118) — Related retained feature qualification exists; this entire scenario is not independently verified on the current deployment. |
| <a id="mod-019-f004-s004"></a>`MOD-019-F004-S004` | Re-run qualification against the current deployed revision | Fresh evidence identifies revision, fixtures, outcome and cleanup; retained reports alone cannot pass it. | HISTORICAL | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-118](#ev-118) — Related retained feature qualification exists; this entire scenario is not independently verified on the current deployment. |

<a id="mod-019-f005"></a>

#### MOD-019-F005 — SSE/replay, clarification, cancellation and external client

Module: [MOD-019](#mod-019). Previous audit ID: `BACK-048`. Native references: V3 #5804; V4 #5805.

Surface: Backend / CLI / integration. **Feature coverage: Historical evidence.** Retained V3/V4 reports and public-transport observations exist. Integrity verification is separate from exercising current public endpoints.

Existing execution selection/limits: Manual qualification/history; outside PR daily.

Related feature evidence: [EV-121](#ev-121), [EV-122](#ev-122), [EV-123](#ev-123). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-019-f005-s001"></a>`MOD-019-F005-S001` | Consume and reconnect a public SSE task stream | Sequence/replay contract preserves coherent client state. | HISTORICAL | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-121](#ev-121) — Related retained feature qualification exists; this entire scenario is not independently verified on the current deployment. |
| <a id="mod-019-f005-s002"></a>`MOD-019-F005-S002` | Send clarification then resume task work | Message and resulting execution bind to the intended task. | HISTORICAL | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-121](#ev-121) — Related retained feature qualification exists; this entire scenario is not independently verified on the current deployment. |
| <a id="mod-019-f005-s003"></a>`MOD-019-F005-S003` | Cancel a task through an external client | Worker/terminal state reflects the accepted cancellation. | HISTORICAL | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-121](#ev-121) — Related retained feature qualification exists; this entire scenario is not independently verified on the current deployment. |
| <a id="mod-019-f005-s004"></a>`MOD-019-F005-S004` | Access stream/artifacts from another identity | Public transport refuses unauthorized task evidence. | HISTORICAL | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-121](#ev-121) — Related retained feature qualification exists; this entire scenario is not independently verified on the current deployment. |

<a id="mod-019-f006"></a>

#### MOD-019-F006 — Coexistence, admission drain and rollout/rollback

Module: [MOD-019](#mod-019). Previous audit ID: `BACK-049`. Native references: V5 #5806.

Surface: Backend / CLI / integration. **Feature coverage: Historical evidence.** Recorded rollout assessment states V1–V5 passed with explicit timing and cleanup limits. Public evidence is sanitized and is not current-release proof.

Existing execution selection/limits: Separate release qualification; not nightly.

Related feature evidence: [EV-124](#ev-124), [EV-120](#ev-120). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-019-f006-s001"></a>`MOD-019-F006-S001` | Publish a Task route while admission remains disabled | Availability and admission gates behave independently. | HISTORICAL | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-124](#ev-124) — Related retained feature qualification exists; this entire scenario is not independently verified on the current deployment. |
| <a id="mod-019-f006-s002"></a>`MOD-019-F006-S002` | Drain admission during a supported rollout | New work follows the gate and already accepted work retains documented handling. | HISTORICAL | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-124](#ev-124) — Related retained feature qualification exists; this entire scenario is not independently verified on the current deployment. |
| <a id="mod-019-f006-s003"></a>`MOD-019-F006-S003` | Upgrade and roll back the Task runtime | Supported old/new clients remain compatible through the declared rollout window. | HISTORICAL | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-124](#ev-124) — Related retained feature qualification exists; this entire scenario is not independently verified on the current deployment. |
| <a id="mod-019-f006-s004"></a>`MOD-019-F006-S004` | Verify cleanup and timing claims from current evidence | Qualification reports actual observations and unresolved bounds explicitly. | HISTORICAL | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-124](#ev-124) — Related retained feature qualification exists; this entire scenario is not independently verified on the current deployment. |

<a id="mod-020"></a>

### MOD-020 — Domain MRI

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-020-F001](#mod-020-f001) | Cyber task submission, progress, cancellation and downloadable report | MRI-01 | Not found; master ID is canonical |

<a id="mod-020-f001"></a>

#### MOD-020-F001 — Cyber task submission, progress, cancellation and downloadable report

Module: [MOD-020](#mod-020). Previous audit ID: `MRI-01`. Native references: none found.

Surface: /domain-mri. **Feature coverage: Local only.** Task-client unit tests; prior controlled-site Cyber qualification is separate. No running Domain MRI browser → task → report journey found.

Related feature evidence: [EV-080](#ev-080), [EV-081](#ev-081). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-020-f001-s001"></a>`MOD-020-F001-S001` | Submit a valid public domain through Domain MRI | A cyber task is created with the intended bounded investigation inputs. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-020-f001-s002"></a>`MOD-020-F001-S002` | Enter a URL with credentials, path, port or private-domain suffix | Input is rejected before task submission. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-020-f001-s003"></a>`MOD-020-F001-S003` | Observe investigation progress and reconnect | Progress resumes for the same task without inventing completed actions. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-020-f001-s004"></a>`MOD-020-F001-S004` | Cancel a running investigation | UI and task eventually agree on the documented cancellation outcome. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-020-f001-s005"></a>`MOD-020-F001-S005` | Download a completed HTML report | Artifact type, byte limit and digest are verified before download. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-020-f001-s006"></a>`MOD-020-F001-S006` | Receive missing, wrong-type or tampered report bytes | Download is refused with a recoverable explanation. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-021"></a>

### MOD-021 — Agent identity and internal services

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-021-F001](#mod-021-f001) | Pod/run bootstrap, model decisions/keys, external root admission and revalidation | AU-01 | #5028 |
| [MOD-021-F002](#mod-021-f002) | Mediated GitHub operations, PR binding, run report/status/handoff/control registration | AU-02 | #5223; #5301 |
| [MOD-021-F003](#mod-021-f003) | Artifacts/review results/checks, scoped knowledge, run markers and service delegation | AU-03 | Not found; master ID is canonical |
| [MOD-021-F004](#mod-021-f004) | Resolve user/installation, issue magic links, installation tokens, provenance and audit | AU-04 | #446; #785 |

<a id="mod-021-f001"></a>

#### MOD-021-F001 — Pod/run bootstrap, model decisions/keys, external root admission and revalidation

Module: [MOD-021](#mod-021). Previous audit ID: `AU-01`. Native references: #5028.

Surface: Internal API. **Feature coverage: Partial E2E.** Agent identity security tests and hosted workers; per-route live coverage not demonstrated by browser or catalogue smoke.

Related feature evidence: [EV-070](#ev-070), [EV-079](#ev-079). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-021-f001-s001"></a>`MOD-021-F001-S001` | Bootstrap a real authorized pod/run identity | Credential is bound to its server-owned execution record. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-021-f001-s002"></a>`MOD-021-F001-S002` | Admit an external root with valid producer proof | Root ownership is created before work becomes executable. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-021-f001-s003"></a>`MOD-021-F001-S003` | Resolve a model decision and verification keys | Decision binds the authorized run/model/policy context. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-021-f001-s004"></a>`MOD-021-F001-S004` | Replay a credential from another pod or expired attempt | Protected operations fail despite shared transport credentials. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-021-f001-s005"></a>`MOD-021-F001-S005` | Revalidate after revocation or policy change | Stale authority cannot continue protected execution. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-021-f002"></a>

#### MOD-021-F002 — Mediated GitHub operations, PR binding, run report/status/handoff/control registration

Module: [MOD-021](#mod-021). Previous audit ID: `AU-02`. Native references: #5223; #5301.

Surface: Internal API. **Feature coverage: Partial E2E.** Hosted task E42 and worker integration cover selected paths; fixture GitHub transports are local integration.

Related feature evidence: [EV-078](#ev-078), [EV-079](#ev-079). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-021-f002-s001"></a>`MOD-021-F002-S001` | Perform a mediated GitHub operation from an authorized run | Only allowed repository actions are executed and attributable. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-021-f002-s002"></a>`MOD-021-F002-S002` | Bind a PR and report start/terminal/block outcomes | Durable run report points to the intended story/repository/PR. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-021-f002-s003"></a>`MOD-021-F002-S003` | Register, renew and clear agent control state | Control endpoints track only the owning active execution. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-021-f002-s004"></a>`MOD-021-F002-S004` | Handoff work using valid execution authority | New work retains protected lineage and valid ownership. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-021-f002-s005"></a>`MOD-021-F002-S005` | Replay a report or use another run's identity | Duplicate/foreign updates cannot replace authoritative state. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-021-f003"></a>

#### MOD-021-F003 — Artifacts/review results/checks, scoped knowledge, run markers and service delegation

Module: [MOD-021](#mod-021). Previous audit ID: `AU-03`. Native references: none found.

Surface: API / internal. **Feature coverage: Local only.** Gateway authority/orchestration tests found; no complete live proof for every delegated service path.

Related feature evidence: [EV-070](#ev-070), [EV-076](#ev-076). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-021-f003-s001"></a>`MOD-021-F003-S001` | Publish artifacts/review results through an authorized agent | Result metadata and content bind to the owning execution. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-021-f003-s002"></a>`MOD-021-F003-S002` | Use scoped knowledge through the agent service | Only authorized knowledge resources are retrievable. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-021-f003-s003"></a>`MOD-021-F003-S003` | Write a run marker and read associated provenance | Marker cannot claim another run's identity. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-021-f003-s004"></a>`MOD-021-F003-S004` | Create and revoke a standing service delegation as a human | Only authorized human authority grants it and revocation blocks later use. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-021-f003-s005"></a>`MOD-021-F003-S005` | Submit review checks with mismatched evidence | Checks do not qualify unrelated artifacts or work. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-021-f004"></a>

#### MOD-021-F004 — Resolve user/installation, issue magic links, installation tokens, provenance and audit

Module: [MOD-021](#mod-021). Previous audit ID: `AU-04`. Native references: #446; #785.

Surface: Internal API. **Feature coverage: Partial E2E.** Credential/security/integration tests exercise portions; no blanket real provider/audit lifecycle acceptance.

Related feature evidence: [EV-049](#ev-049). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-021-f004-s001"></a>`MOD-021-F004-S001` | Resolve user/installation with valid internal authority | Resolution returns only authorized canonical bindings. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-021-f004-s002"></a>`MOD-021-F004-S002` | Issue and redeem a magic link | Proof binds the intended identity and cannot be replayed beyond its contract. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-021-f004-s003"></a>`MOD-021-F004-S003` | Request an installation token for a permitted operation | Credential scope matches the authorized installation and use. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-021-f004-s004"></a>`MOD-021-F004-S004` | Write provenance and query allowed audit records | Records retain server-owned identity and bounded visibility. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-021-f004-s005"></a>`MOD-021-F004-S005` | Run a controller domain operation under its lease | Dispatch, bootstrap, recovery and completion verify the current executor rather than supplied identity fields. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-022"></a>

### MOD-022 — Model gateway

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-022-F001](#mod-022-f001) | OpenAI/Anthropic requests and streaming, Responses API, Bedrock invoke, model list and token count | PX-01 | FR5 |

<a id="mod-022-f001"></a>

#### MOD-022-F001 — OpenAI/Anthropic requests and streaming, Responses API, Bedrock invoke, model list and token count

Module: [MOD-022](#mod-022). Previous audit ID: `PX-01`. Native references: FR5.

Surface: API / external clients. **Feature coverage: Partial E2E.** Live gateway/inference harness exists for selected models; latest gateway run skipped all 86 selected live tests. No complete protocol/model cross-product.

Related feature evidence: [EV-082](#ev-082), [EV-083](#ev-083). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-022-f001-s001"></a>`MOD-022-F001-S001` | Call supported OpenAI chat and Anthropic messages protocols | Response content, errors and usage follow their respective wire contracts. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-022-f001-s002"></a>`MOD-022-F001-S002` | Consume streaming responses through completion/cancellation | Stream framing and terminal usage remain correct. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-022-f001-s003"></a>`MOD-022-F001-S003` | Invoke Bedrock direct and streaming model paths | Payload translation and destination selection preserve requested semantics. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-022-f001-s004"></a>`MOD-022-F001-S004` | Use the Responses API and tool interaction paths | Supported client receives correctly correlated model/tool events. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-022-f001-s005"></a>`MOD-022-F001-S005` | List permitted models and count tokens | Results respect model access and supported request validation. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-022-f001-s006"></a>`MOD-022-F001-S006` | Exercise an upstream refusal, timeout and throttling response | Error mapping is explicit and no success or double accounting is fabricated. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-023"></a>

### MOD-023 — Navigation and feature gates

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-023-F001](#mod-023-f001) | Legacy routes, Use ADP/Administration preview, permissions, fallbacks and workspace context | UI-01 | #5078; #5079; #5080; #5135 |

<a id="mod-023-f001"></a>

#### MOD-023-F001 — Legacy routes, Use ADP/Administration preview, permissions, fallbacks and workspace context

Module: [MOD-023](#mod-023). Previous audit ID: `UI-01`. Native references: #5078; #5079; #5080; #5135.

Surface: /next; /next/admin; shared navigation. **Feature coverage: Partial E2E.** New UI browser suite checks four current page responses and navigation/flag behavior; does not mutate every linked feature.

Related feature evidence: [EV-041](#ev-041), [EV-084](#ev-084). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-023-f001-s001"></a>`MOD-023-F001-S001` | Open every visible legacy route as its authorized role | Intended page loads and initiates its expected real data request. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-096](#test-096) | [EV-041](#ev-041) [test_legacy_routes_unchanged](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/new_ui/test_coexistence.py#L136) — Only four current page response paths are selected, not every route. |
| <a id="mod-023-f001-s002"></a>`MOD-023-F001-S002` | Enter/return from Use ADP and Administration preview | Session identity and permitted navigation remain consistent. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-095](#test-095) | [EV-041](#ev-041) [test_entry_return_and_shared_identity](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/new_ui/test_coexistence.py#L171) — Live preview entry/return/shared-identity check. |
| <a id="mod-023-f001-s003"></a>`MOD-023-F001-S003` | Disable a feature and visit its direct URL | Route/control follows its documented fallback without exposing protected data. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-097](#test-097) | [EV-041](#ev-041) [test_live_flag_off_returns_current_ui](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/new_ui/test_coexistence.py#L235) — Covers new_ui off state when deployed accordingly, not every feature flag. |
| <a id="mod-023-f001-s004"></a>`MOD-023-F001-S004` | Expire a session or fail a lazy-loaded bundle | Login/recovery route remains usable without losing the safe return path. | LOCAL-ONLY | **1** (E2E 0, local 1); existing total unknown | [TEST-098](#test-098) | [EV-041](#ev-041) [test_simulated_failed_new_bundle](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/new_ui/test_coexistence.py#L289) — Bundle failure is deliberately simulated in-browser. |
| <a id="mod-023-f001-s005"></a>`MOD-023-F001-S005` | Switch workspace and use browser back/reload | Remembered routes and displayed data remain tenant-scoped. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-023-f001-s006"></a>`MOD-023-F001-S006` | Use unknown current and preview routes | Appropriate fallback offers a working navigation escape. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-099](#test-099) | [EV-041](#ev-041) [test_unknown_next_path_has_return](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/new_ui/test_coexistence.py#L262) — Preview fallback/return only; global fallback remains separately mapped. |

<a id="mod-024"></a>

### MOD-024 — CLI distribution and setup

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-024-F001](#mod-024-f001) | Served CLI downloads, setup instructions, capabilities and request diagnostics | CLI-01 | #4146; #5621 |
| [MOD-024-F002](#mod-024-f002) | Clean laptop onboarding and Claude/Codex setup | BACK-002 | #4157; #5185 |
| [MOD-024-F003](#mod-024-f003) | CLI upgrade, rollback and interrupted installation | BACK-003 | E14 (owner: all CLI stories) |

<a id="mod-024-f001"></a>

#### MOD-024-F001 — Served CLI downloads, setup instructions, capabilities and request diagnostics

Module: [MOD-024](#mod-024). Previous audit ID: `CLI-01`. Native references: #4146; #5621.

Surface: /setup; API / CLI. **Feature coverage: Partial E2E.** E01/C01 install/auth plus E20 capability checks; onboarding eval tests selected external tools.

Related feature evidence: [EV-036](#ev-036), [EV-002](#ev-002), [EV-085](#ev-085). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-024-f001-s001"></a>`MOD-024-F001-S001` | Download and install the served CLI on a clean host | Executable and supplied hashes agree with the served release. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-024-f001-s002"></a>`MOD-024-F001-S002` | Read capabilities and run bounded doctor checks | Supported operations are explicit and unsupported ones do not appear executable. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-024-f001-s003"></a>`MOD-024-F001-S003` | Follow setup instructions for an available client | Resulting configuration can authenticate against the intended deployment. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-024-f001-s004"></a>`MOD-024-F001-S004` | Request diagnostics for an owned request ID | Diagnostic scope and identity match the caller. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-024-f001-s005"></a>`MOD-024-F001-S005` | Request an unknown download or capability | Error is actionable and does not silently install/use another artifact. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-024-f002"></a>

#### MOD-024-F002 — Clean laptop onboarding and Claude/Codex setup

Module: [MOD-024](#mod-024). Previous audit ID: `BACK-002`. Native references: #4157; #5185.

Surface: Backend / CLI / integration. **Feature coverage: Live harness.** Phases A/B/C exercise approval enforcement and a clean Node pod; actual Claude/Codex conversations. Requires deployment identity and cleanup.

Existing execution selection/limits: Current nightly and PR daily; current parent failed before live execution.

Related feature evidence: [EV-085](#ev-085). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-024-f002-s001"></a>`MOD-024-F002-S001` | Run clean-host installation and sign-in | Supported clients are configured without relying on an existing developer environment. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-061](#test-061) | [EV-085](#ev-085) — Clean-host phases A/B/C with approval and real supported clients; deployment fixtures required. |
| <a id="mod-024-f002-s002"></a>`MOD-024-F002-S002` | Attempt inference before approval | Approval enforcement refuses the unapproved identity. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-060](#test-060) | [EV-085](#ev-085) — Clean-host phases A/B/C with approval and real supported clients; deployment fixtures required. |
| <a id="mod-024-f002-s003"></a>`MOD-024-F002-S003` | Complete approved Claude and Codex conversations | Both clients reach actual supported model responses through the Gateway. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-061](#test-061) | [EV-085](#ev-085) — Clean-host phases A/B/C with approval and real supported clients; deployment fixtures required. |
| <a id="mod-024-f002-s004"></a>`MOD-024-F002-S004` | Clean up onboarding fixtures | Original identity/configuration state is restored and disposable resources are removed. | PARTIAL-HARNESS | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-085](#ev-085) — Clean-host phases A/B/C with approval and real supported clients; deployment fixtures required. |

<a id="mod-024-f003"></a>

#### MOD-024-F003 — CLI upgrade, rollback and interrupted installation

Module: [MOD-024](#mod-024). Previous audit ID: `BACK-003`. Native references: E14 (owner: all CLI stories).

Surface: Backend / CLI / integration. **Feature coverage: Live harness.** On-instance update/rollback, executable installation and launcher checks; not a platform infrastructure upgrade.

Existing execution selection/limits: Manual parity/full; outside PR daily/weekly.

Related feature evidence: [EV-099](#ev-099). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-024-f003-s001"></a>`MOD-024-F003-S001` | Upgrade an installed CLI to the served release | Executable version and hashes match the intended artifact. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-092](#test-092) | [EV-099](#ev-099) — E14 CLI update/rollback driver exists; not platform infrastructure upgrade qualification. |
| <a id="mod-024-f003-s002"></a>`MOD-024-F003-S002` | Roll back CLI to a supported previous artifact | Previous executable and configuration remain usable. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-092](#test-092) | [EV-099](#ev-099) — E14 CLI update/rollback driver exists; not platform infrastructure upgrade qualification. |
| <a id="mod-024-f003-s003"></a>`MOD-024-F003-S003` | Interrupt download/install before completion | Existing working executable is preserved or a documented recovery path succeeds. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-092](#test-092) | [EV-099](#ev-099) — E14 CLI update/rollback driver exists; not platform infrastructure upgrade qualification. |
| <a id="mod-024-f003-s004"></a>`MOD-024-F003-S004` | Run authenticated commands after update/rollback | Session/config compatibility is preserved without silently switching deployment. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-092](#test-092) | [EV-099](#ev-099) — E14 CLI update/rollback driver exists; not platform infrastructure upgrade qualification. |

<a id="mod-025"></a>

### MOD-025 — Service health

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-025-F001](#mod-025-f001) | Gateway health/readiness | HL-01 | Not found; master ID is canonical |

<a id="mod-025-f001"></a>

#### MOD-025-F001 — Gateway health/readiness

Module: [MOD-025](#mod-025). Previous audit ID: `HL-01`. Native references: none found.

Surface: API. **Feature coverage: Partial E2E.** Gateway live checks and deployment acceptance exist; shallow health does not prove downstream dependencies.

Related feature evidence: [EV-082](#ev-082), [EV-086](#ev-086). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-025-f001-s001"></a>`MOD-025-F001-S001` | Request Gateway liveness and readiness | Mounted endpoints return their documented status and body. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-025-f001-s002"></a>`MOD-025-F001-S002` | Compare shallow health with a failed downstream dependency | Monitoring does not treat shallow success as proof of complete service readiness. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-025-f001-s003"></a>`MOD-025-F001-S003` | Access health during process restart/deployment | Unavailable service is detectable and recovers after startup. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-025-f001-s004"></a>`MOD-025-F001-S004` | Check deployment acceptance alongside health | Intended deployed revision is verified separately from simple process response. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-026"></a>

### MOD-026 — Superplane

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-026-F001](#mod-026-f001) | Domain proxy, installation support and scoped forwarding | SP-01 | #4910; #5730 |
| [MOD-026-F002](#mod-026-f002) | List/create/adopt workspace, lifecycle/readiness, kubeconfig and bootstrap observation | SP-02 | #4910; #5730 |
| [MOD-026-F003](#mod-026-f003) | Approval request/read/decision and phase continuation | SP-03 | Not found; master ID is canonical |
| [MOD-026-F004](#mod-026-f004) | Provider connection create/validate/rotate/delete; account/vault registry | SP-04 | Not found; master ID is canonical |
| [MOD-026-F005](#mod-026-f005) | Serving deployments and batch jobs: preview/create/list/status/result/cancel/teardown | SP-05 | Not found; master ID is canonical |
| [MOD-026-F006](#mod-026-f006) | Retirement preview and execution | SP-06 | Not found; master ID is canonical |
| [MOD-026-F007](#mod-026-f007) | Workspace/org cost, budgets, quota reads/writes and reconciliation | SP-07 | Not found; master ID is canonical |
| [MOD-026-F008](#mod-026-f008) | Findings, sources, scans, stats and proposal create/approve/reject | SP-08 | Not found; master ID is canonical |
| [MOD-026-F009](#mod-026-f009) | Native signup/login, organization SSO/settings and user invitation/roles/removal | SP-09 | Not found; master ID is canonical |
| [MOD-026-F010](#mod-026-f010) | Reconcile/recovery, heartbeat leases, inventory/events and credential evidence | SP-10 | Not found; master ID is canonical |
| [MOD-026-F011](#mod-026-f011) | Private authenticated SkyPilot sidecar method/path allowlist | SP-11 | Not found; master ID is canonical |

<a id="mod-026-f001"></a>

#### MOD-026-F001 — Domain proxy, installation support and scoped forwarding

Module: [MOD-026](#mod-026). Previous audit ID: `SP-01`. Native references: #4910; #5730.

Surface: /superplane. **Feature coverage: Local only.** UI/browser tests use fixture HTTP; API/worker tests and live qualification tooling do not establish a current complete provider lifecycle.

Related feature evidence: [EV-087](#ev-087), [EV-088](#ev-088). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-026-f001-s001"></a>`MOD-026-F001-S001` | Read installation support and forward an allowed domain request | Response reflects configured Superplane capability and caller scope. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f001-s002"></a>`MOD-026-F001-S002` | Attempt an unsupported forwarded path/method | Proxy refuses operations outside its allowlist. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f001-s003"></a>`MOD-026-F001-S003` | Forge tenant or actor headers through the domain proxy | Downstream authority remains derived from authenticated identity. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f001-s004"></a>`MOD-026-F001-S004` | Disable Superplane or remove its backend | UI/API report unavailable capability without misleading success. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-026-f002"></a>

#### MOD-026-F002 — List/create/adopt workspace, lifecycle/readiness, kubeconfig and bootstrap observation

Module: [MOD-026](#mod-026). Previous audit ID: `SP-02`. Native references: #4910; #5730.

Surface: /superplane. **Feature coverage: Partial E2E.** E39 preview/read-only; fixture browser onboarding exists. Live U1/U6/U12 qualification has producer/target/evidence prerequisites.

Related feature evidence: [EV-089](#ev-089), [EV-088](#ev-088). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-026-f002-s001"></a>`MOD-026-F002-S001` | List/create/adopt an owned workspace | Returned workspace and operation identity belong to the authorized organization. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-088](#test-088) | [EV-002](#ev-002) — E39 reads/previews only; actual workspace create/adopt remains unqualified. |
| <a id="mod-026-f002-s002"></a>`MOD-026-F002-S002` | Preview creation then obtain approval and continue | Only the reviewed request proceeds to provider work. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f002-s003"></a>`MOD-026-F002-S003` | Retry after a lost creation acknowledgement | Operation lookup resumes the same request without duplicate allocation. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f002-s004"></a>`MOD-026-F002-S004` | Observe bootstrap/lifecycle until Ready | Readiness depends on real registered components and usable access. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f002-s005"></a>`MOD-026-F002-S005` | Obtain kubeconfig as a permitted actor | Access is scoped to the intended workspace and current membership. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f002-s006"></a>`MOD-026-F002-S006` | Remove/revoke workspace access | Former membership cannot continue obtaining new access. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-026-f003"></a>

#### MOD-026-F003 — Approval request/read/decision and phase continuation

Module: [MOD-026](#mod-026). Previous audit ID: `SP-03`. Native references: none found.

Surface: /superplane. **Feature coverage: Local only.** UI/browser tests use fixture HTTP; API/worker tests and live qualification tooling do not establish a current complete provider lifecycle.

Related feature evidence: [EV-090](#ev-090), [EV-088](#ev-088). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-026-f003-s001"></a>`MOD-026-F003-S001` | Create and read a bounded operation approval | Approval identifies the exact operation parameters and actor scope. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f003-s002"></a>`MOD-026-F003-S002` | Approve or reject as an eligible reviewer | Decision persists with its revision and is visible to the requester. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f003-s003"></a>`MOD-026-F003-S003` | Attempt self-approval where separation is required | Requester cannot bypass the approval authority boundary. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f003-s004"></a>`MOD-026-F003-S004` | Replay expired or mismatched approval on another operation | Approval cannot authorize unreviewed work. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f003-s005"></a>`MOD-026-F003-S005` | Continue an approved lifecycle phase | Exact reviewed phase starts once and records its result. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-026-f004"></a>

#### MOD-026-F004 — Provider connection create/validate/rotate/delete; account/vault registry

Module: [MOD-026](#mod-026). Previous audit ID: `SP-04`. Native references: none found.

Surface: /superplane. **Feature coverage: Local only.** UI/browser tests use fixture HTTP; API/worker tests and live qualification tooling do not establish a current complete provider lifecycle.

Related feature evidence: [EV-091](#ev-091), [EV-088](#ev-088). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-026-f004-s001"></a>`MOD-026-F004-S001` | Create a provider connection in an owned workspace | Registry and credential reference bind to the intended owner. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f004-s002"></a>`MOD-026-F004-S002` | Validate a provider connection | Verified capability is tied to the current credential/evidence version. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f004-s003"></a>`MOD-026-F004-S003` | Rotate credentials then perform provider work | Current credentials work and stale/revoked references cannot authorize new work. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f004-s004"></a>`MOD-026-F004-S004` | Delete a connection/account/vault entry | Removed binding is unavailable to subsequent provider operations. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f004-s005"></a>`MOD-026-F004-S005` | Attempt foreign-workspace connection reads or writes | No credential metadata or provider authority leaks across scope. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-026-f005"></a>

#### MOD-026-F005 — Serving deployments and batch jobs: preview/create/list/status/result/cancel/teardown

Module: [MOD-026](#mod-026). Previous audit ID: `SP-05`. Native references: none found.

Surface: /superplane. **Feature coverage: Local only.** UI/browser tests use fixture HTTP; API/worker tests and live qualification tooling do not establish a current complete provider lifecycle.

Related feature evidence: [EV-092](#ev-092), [EV-088](#ev-088). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-026-f005-s001"></a>`MOD-026-F005-S001` | Preview and create a serving deployment | Approved profile/image/resources match the admitted deployment. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f005-s002"></a>`MOD-026-F005-S002` | Preview and create a batch job | Command, inputs and resource bounds match the reviewed request. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f005-s003"></a>`MOD-026-F005-S003` | List/status and inspect an actual workload | Observed provider state matches the recorded deployment/job. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f005-s004"></a>`MOD-026-F005-S004` | Retrieve a completed batch result or serving endpoint | Returned result/access belongs to the authorized workload. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f005-s005"></a>`MOD-026-F005-S005` | Cancel active workloads and observe completion | Cancellation is enacted and does not report success before the supported terminal state. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f005-s006"></a>`MOD-026-F005-S006` | Preview teardown then delete workload | Owned resources are cleaned and settlement records reflect actual release. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f005-s007"></a>`MOD-026-F005-S007` | Reject stale approval, invalid profile or another workspace's workload | No unintended provider allocation or disclosure occurs. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-026-f006"></a>

#### MOD-026-F006 — Retirement preview and execution

Module: [MOD-026](#mod-026). Previous audit ID: `SP-06`. Native references: none found.

Surface: /superplane. **Feature coverage: Local only.** UI/browser tests use fixture HTTP; API/worker tests and live qualification tooling do not establish a current complete provider lifecycle.

Related feature evidence: [EV-093](#ev-093), [EV-088](#ev-088). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-026-f006-s001"></a>`MOD-026-F006-S001` | Preview retirement of an owned workspace | Plan describes retained dependencies, cleanup and required approvals. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f006-s002"></a>`MOD-026-F006-S002` | Accept retirement with current plan and authority | Only the reviewed workspace/resources are retired. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f006-s003"></a>`MOD-026-F006-S003` | Retry retirement after interrupted execution | Recovery continues safely without duplicate destructive work. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f006-s004"></a>`MOD-026-F006-S004` | Retire while resources or obligations remain unsettled | Completion is withheld until required cleanup/settlement evidence exists. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f006-s005"></a>`MOD-026-F006-S005` | Attempt retirement without proper scope or approval | Workspace and provider resources remain unchanged. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-026-f007"></a>

#### MOD-026-F007 — Workspace/org cost, budgets, quota reads/writes and reconciliation

Module: [MOD-026](#mod-026). Previous audit ID: `SP-07`. Native references: none found.

Surface: /superplane. **Feature coverage: Local only.** UI/browser tests use fixture HTTP; API/worker tests and live qualification tooling do not establish a current complete provider lifecycle.

Related feature evidence: [EV-094](#ev-094), [EV-088](#ev-088). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-026-f007-s001"></a>`MOD-026-F007-S001` | Read workspace/org costs and budget | Scope, currency and evidence freshness remain explicit. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f007-s002"></a>`MOD-026-F007-S002` | Change quota as an authorized administrator | New requests honor the persisted ceiling. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f007-s003"></a>`MOD-026-F007-S003` | Attempt allocation above quota or budget | Admission refuses excess resource/spend authorization. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f007-s004"></a>`MOD-026-F007-S004` | Reconcile completed workload costs | Actual provider observations and internal settlement agree or report unresolved differences. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f007-s005"></a>`MOD-026-F007-S005` | Read another organization's costs or mutate its quota | Cross-org request is denied. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-026-f008"></a>

#### MOD-026-F008 — Findings, sources, scans, stats and proposal create/approve/reject

Module: [MOD-026](#mod-026). Previous audit ID: `SP-08`. Native references: none found.

Surface: Domain API / CLI. **Feature coverage: Partial E2E.** E25 real read-only discovery; scan/proposal mutation/approval intentionally excluded.

Related feature evidence: [EV-002](#ev-002), [EV-088](#ev-088). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-026-f008-s001"></a>`MOD-026-F008-S001` | Read scoped findings, sources, proposals and statistics | IDs, continuation and visibility remain within authorized scope. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-089](#test-089) | [EV-177](#ev-177) — E25 scoped research reads; scan and proposal decisions are excluded. |
| <a id="mod-026-f008-s002"></a>`MOD-026-F008-S002` | Start a bounded research scan | Work executes with the intended sources and produces attributable findings. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f008-s003"></a>`MOD-026-F008-S003` | Create/generate a proposal from findings | Proposal preserves source evidence and its current decision state. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f008-s004"></a>`MOD-026-F008-S004` | Approve or reject a proposal | Only an authorized decision changes the intended proposal revision. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f008-s005"></a>`MOD-026-F008-S005` | Submit stale/foreign proposal decisions | No unrelated research or downstream work is authorized. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-026-f009"></a>

#### MOD-026-F009 — Native signup/login, organization SSO/settings and user invitation/roles/removal

Module: [MOD-026](#mod-026). Previous audit ID: `SP-09`. Native references: none found.

Surface: Domain API / CLI. **Feature coverage: Local only.** UI/browser tests use fixture HTTP; API/worker tests and live qualification tooling do not establish a current complete provider lifecycle.

Related feature evidence: [EV-094](#ev-094), [EV-088](#ev-088). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-026-f009-s001"></a>`MOD-026-F009-S001` | Sign up/sign in using supported native identity flow | Session binds to the intended organization membership. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f009-s002"></a>`MOD-026-F009-S002` | Read/update organization SSO configuration | Only authorized administration can alter authentication settings. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f009-s003"></a>`MOD-026-F009-S003` | Invite a user then complete enrollment | Invitation grants only the intended role and organization. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f009-s004"></a>`MOD-026-F009-S004` | Change or remove a user's role/membership | Subsequent protected operations reflect current authority. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f009-s005"></a>`MOD-026-F009-S005` | Replay an invitation or target a foreign user | Stale/foreign proof cannot grant additional access. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-026-f010"></a>

#### MOD-026-F010 — Reconcile/recovery, heartbeat leases, inventory/events and credential evidence

Module: [MOD-026](#mod-026). Previous audit ID: `SP-10`. Native references: none found.

Surface: Domain API / CLI. **Feature coverage: Local only.** UI/browser tests use fixture HTTP; API/worker tests and live qualification tooling do not establish a current complete provider lifecycle.

Related feature evidence: [EV-095](#ev-095), [EV-088](#ev-088). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-026-f010-s001"></a>`MOD-026-F010-S001` | Reconcile an owned workspace from observed state | Controller performs only authorized necessary work. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f010-s002"></a>`MOD-026-F010-S002` | Acquire/release observation leases and record heartbeats | Stale observers cannot replace fresher authoritative observations. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f010-s003"></a>`MOD-026-F010-S003` | Recover inventory, lifecycle, bootstrap and settlement work | Recovery honors original operation identity and bounded authority. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f010-s004"></a>`MOD-026-F010-S004` | Deliver installation credential evidence to the active operation | Evidence is bound to its intended workspace/provider/executor. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f010-s005"></a>`MOD-026-F010-S005` | Read scoped events and cost history | Observations remain attributable without foreign-workspace disclosure. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f010-s006"></a>`MOD-026-F010-S006` | Assess/release provider allocations | Release requires current ownership and cleanup proof rather than mere missing heartbeats. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-026-f011"></a>

#### MOD-026-F011 — Private authenticated SkyPilot sidecar method/path allowlist

Module: [MOD-026](#mod-026). Previous audit ID: `SP-11`. Native references: none found.

Surface: Private sidecar API. **Feature coverage: Local only.** SkyPilot startup/transport contract tests exist; no complete provider execution/retirement E2E established by these checks.

Related feature evidence: [EV-097](#ev-097), [EV-098](#ev-098). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-026-f011-s001"></a>`MOD-026-F011-S001` | Forward an allowed method/path with the service credential | Request reaches the pinned SkyPilot transport as authorized. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f011-s002"></a>`MOD-026-F011-S002` | Send an unsupported method/path or encoded traversal | Sidecar rejects the request before forwarding. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f011-s003"></a>`MOD-026-F011-S003` | Call with missing or invalid service token | No provider response or credential metadata is disclosed. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f011-s004"></a>`MOD-026-F011-S004` | Read provider identity while provider configuration is unavailable | Sidecar reports unavailable evidence rather than a configured identity. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-026-f011-s005"></a>`MOD-026-F011-S005` | Cancel or fail an upstream stream | Response and resources terminate according to the transport contract. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-027"></a>

### MOD-027 — Agent Context

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-027-F001](#mod-027-f001) | Tool discovery/call, health/readiness and MCP transport | CTX-01 | Not found; master ID is canonical |
| [MOD-027-F002](#mod-027-f002) | Platform health, MCP tools, search and ingestion | BACK-058 | #21 tests 1–16 |
| [MOD-027-F003](#mod-027-f003) | Repository ACL and cross-tenant knowledge isolation | BACK-059 | P7/P8/P9; #1356 |
| [MOD-027-F004](#mod-027-f004) | Neptune/OpenSearch and graph-backed retrieval | BACK-060 | #21 GraphRAG checks |

<a id="mod-027-f001"></a>

#### MOD-027-F001 — Tool discovery/call, health/readiness and MCP transport

Module: [MOD-027](#mod-027). Previous audit ID: `CTX-01`. Native references: none found.

Surface: Agent Context service. **Feature coverage: Partial E2E.** Live health/MCP/search/ACL harness exists; normal CI excludes live integration; mounted MCP tools are a separate protocol surface.

Related feature evidence: [EV-074](#ev-074), [EV-096](#ev-096). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-027-f001-s001"></a>`MOD-027-F001-S001` | Discover tools and invoke a supported Context operation | Tool schema, identity and result scope agree. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-027-f001-s002"></a>`MOD-027-F001-S002` | Connect through the mounted MCP transport | Session authenticates and exposes only supported tools. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-027-f001-s003"></a>`MOD-027-F001-S003` | Invoke search/understand/impact/browse on permitted source | Result references authorized indexed content. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-027-f001-s004"></a>`MOD-027-F001-S004` | Invoke remember/experience/secure with permitted inputs | Operation obeys its documented storage and authority boundaries. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-027-f001-s005"></a>`MOD-027-F001-S005` | Use invalid identity or unsupported tool input | Server refuses without disclosing knowledge or executing unrelated work. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-027-f002"></a>

#### MOD-027-F002 — Platform health, MCP tools, search and ingestion

Module: [MOD-027](#mod-027). Previous audit ID: `BACK-058`. Native references: #21 tests 1–16.

Surface: Backend / CLI / integration. **Feature coverage: Live harness.** Dual-mode tests and real ingestion jobs exist. Default CI excludes live_only; some state/search checks can skip on absent deployment data.

Existing execution selection/limits: Manual live/verification; PR only reuses offline agent-context CI.

Related feature evidence: [EV-131](#ev-131), [EV-132](#ev-132), [EV-133](#ev-133), [EV-134](#ev-134). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-027-f002-s001"></a>`MOD-027-F002-S001` | Probe real Context dependencies and MCP endpoint | Health reflects the actual configured service boundaries. | PARTIAL-HARNESS | **7** (E2E 7, local 0); existing total unknown | [TEST-009](#test-009), [TEST-010](#test-010), [TEST-011](#test-011), [TEST-012](#test-012), [TEST-013](#test-013), [TEST-014](#test-014), [TEST-015](#test-015) | [EV-131](#ev-131) — Live health/dependency probes; MCP checks are separate feature evidence. |
| <a id="mod-027-f002-s002"></a>`MOD-027-F002-S002` | Ingest an owned repository and wait for completion | Real indexed state becomes available to search. | PARTIAL-HARNESS | **3** (E2E 3, local 0); existing total unknown | [TEST-001](#test-001), [TEST-002](#test-002), [TEST-004](#test-004) | [EV-133](#ev-133) — Opt-in ingestion tests exist. The new-repository search test checks response shape, not that known content was indexed; retrieval acceptance remains unproven. |
| <a id="mod-027-f002-s003"></a>`MOD-027-F002-S003` | Search for known ingested content | Returned evidence matches the source and actual index. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-003](#test-003) | [EV-133](#ev-133) — Opt-in ingestion tests exist. The new-repository search test checks response shape, not that known content was indexed; retrieval acceptance remains unproven. |
| <a id="mod-027-f002-s004"></a>`MOD-027-F002-S004` | Run with missing deployment fixtures | Live checks record blocked/skip state explicitly rather than pretending fixture data is real. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-027-f003"></a>

#### MOD-027-F003 — Repository ACL and cross-tenant knowledge isolation

Module: [MOD-027](#mod-027). Previous audit ID: `BACK-059`. Native references: P7/P8/P9; #1356.

Surface: Backend / CLI / integration. **Feature coverage: Live harness.** Live authorized/unauthorized/semantic search and permission-change checks exist; require real users, repositories and re-ingestion.

Existing execution selection/limits: Manual live; not in PR daily/weekly.

Related feature evidence: [EV-135](#ev-135). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-027-f003-s001"></a>`MOD-027-F003-S001` | Search as a user authorized for the repository | Relevant private content is returned within permitted scope. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-008](#test-008) | [EV-135](#ev-135) [test_authorized_search_returns_results](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_knowledge_isolation.py#L25) — Requires real repository/user fixture; search assertion is narrower than all source permissions. |
| <a id="mod-027-f003-s002"></a>`MOD-027-F003-S002` | Search the same content as an unauthorized user | No private snippet/reference is disclosed. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-007](#test-007) | [EV-135](#ev-135) [test_unauthorized_search_returns_empty](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_knowledge_isolation.py#L56) — Live opt-in unauthorized search checks. |
| <a id="mod-027-f003-s003"></a>`MOD-027-F003-S003` | Change repository permission and re-ingest/refresh access | Retrieval follows the updated authorization state. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-005](#test-005) | [EV-135](#ev-135) [test_permission_change_reflected](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_knowledge_isolation.py#L104) — Permission-change check requires real users/repository and re-ingestion. |
| <a id="mod-027-f003-s004"></a>`MOD-027-F003-S004` | Search across two tenants with overlapping terms | Results retain each caller's authorized repository set. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-006](#test-006) | [EV-135](#ev-135) [test_cross_tenant_no_leakage](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_knowledge_isolation.py#L72) — Live opt-in cross-tenant isolation check. |

<a id="mod-027-f004"></a>

#### MOD-027-F004 — Neptune/OpenSearch and graph-backed retrieval

Module: [MOD-027](#mod-027). Previous audit ID: `BACK-060`. Native references: #21 GraphRAG checks.

Surface: Backend / CLI / integration. **Feature coverage: Partial.** Opt-in graph tests mostly probe reachability/response shape. Graph search accepts a dict without traceback; it does not require graph-backed evidence.

Existing execution selection/limits: Excluded from ordinary CI; manual configured GraphRAG.

Related feature evidence: [EV-136](#ev-136). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-027-f004-s001"></a>`MOD-027-F004-S001` | Reach the configured Neptune/OpenSearch services | Connectivity and authentication are actually validated. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-027-f004-s002"></a>`MOD-027-F004-S002` | Ingest data with known relationships | Required graph/index records are created in the real backend. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-027-f004-s003"></a>`MOD-027-F004-S003` | Ask a relationship-dependent query | Result proves use of expected graph evidence rather than merely returning a dictionary. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-027-f004-s004"></a>`MOD-027-F004-S004` | Remove access or a backend dependency | Query does not leak unauthorized evidence or invent successful graph results. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-028"></a>

### MOD-028 — Model gateway protocols

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-028-F001](#mod-028-f001) | Kimi adapter and coding-tool regression | BACK-012 | AUD-GW-KIMI |

<a id="mod-028-f001"></a>

#### MOD-028-F001 — Kimi adapter and coding-tool regression

Module: [MOD-028](#mod-028). Previous audit ID: `BACK-012`. Native references: AUD-GW-KIMI.

Surface: Backend / CLI / integration. **Feature coverage: Partial.** Standalone proxy regression smoke exists; no invocation from the proposed regression coordinator was found.

Existing execution selection/limits: Manual script; outside PR daily/weekly.

Related feature evidence: [EV-100](#ev-100). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-028-f001-s001"></a>`MOD-028-F001-S001` | Run the supported Kimi adapter through proxy smoke | Adapter and Gateway agree on wire shape and usable response. | PARTIAL-HARNESS | **0** (E2E 0, local 0); existing total unknown | [TEST-094](#test-094); excluded: 1 check | [EV-100](#ev-100) — Standalone Kimi proxy smoke; not selected by the proposed coordinator. |
| <a id="mod-028-f001-s002"></a>`MOD-028-F001-S002` | Send supported tool/streaming interactions from the adapter | Event ordering and terminal response remain client-compatible. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-028-f001-s003"></a>`MOD-028-F001-S003` | Receive an upstream authentication/model refusal | Adapter preserves actionable failure rather than appearing successful. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-028-f001-s004"></a>`MOD-028-F001-S004` | Install/update the adapter on a clean client | Correct configuration can execute the smoke against the intended deployment. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-029"></a>

### MOD-029 — Multi-deployment sessions

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-029-F001](#mod-029-f001) | Three gateways, concurrent sessions and logout isolation | BACK-018 | #5413; E16/E17 |

<a id="mod-029-f001"></a>

#### MOD-029-F001 — Three gateways, concurrent sessions and logout isolation

Module: [MOD-029](#mod-029). Previous audit ID: `BACK-018`. Native references: #5413; E16/E17.

Surface: Backend / CLI / integration. **Feature coverage: Blocked.** Remote drivers exist but preflight deliberately withholds model-limit capability. Supplying three gateway URLs cannot satisfy the missing enforcement prerequisite.

Existing execution selection/limits: Manual multi-deployment/full; excluded from PR profiles.

Related feature evidence: [EV-101](#ev-101), [EV-032](#ev-032). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-029-f001-s001"></a>`MOD-029-F001-S001` | Configure three independent Gateway deployments | Each profile retains its own identity and endpoint selection. | BLOCKED | **0** (E2E 0, local 0); existing total unknown | [TEST-072](#test-072); excluded: 1 blocked | [EV-101](#ev-101) — Remote drivers exist but preflight deliberately withholds model-limit capability. Supplying three gateway URLs cannot satisfy the missing enforcement prerequisite. |
| <a id="mod-029-f001-s002"></a>`MOD-029-F001-S002` | Run concurrent sessions against all three deployments | Sessions and request attribution do not bleed across profiles. | BLOCKED | **0** (E2E 0, local 0); existing total unknown | [TEST-072](#test-072); excluded: 1 blocked | [EV-101](#ev-101) — Remote drivers exist but preflight deliberately withholds model-limit capability. Supplying three gateway URLs cannot satisfy the missing enforcement prerequisite. |
| <a id="mod-029-f001-s003"></a>`MOD-029-F001-S003` | Log out from one deployment | Remaining deployments' authorized sessions stay usable. | BLOCKED | **0** (E2E 0, local 0); existing total unknown | [TEST-073](#test-073); excluded: 1 blocked | [EV-101](#ev-101) — Remote drivers exist but preflight deliberately withholds model-limit capability. Supplying three gateway URLs cannot satisfy the missing enforcement prerequisite. |
| <a id="mod-029-f001-s004"></a>`MOD-029-F001-S004` | Qualify model limits across deployment profiles | Required enforcement capability is demonstrated before acceptance can pass. | BLOCKED | **0** (E2E 0, local 0); existing total unknown | [TEST-072](#test-072), [TEST-073](#test-073); excluded: 2 blocked | [EV-101](#ev-101) — Remote drivers exist but preflight deliberately withholds model-limit capability. Supplying three gateway URLs cannot satisfy the missing enforcement prerequisite. |

<a id="mod-030"></a>

### MOD-030 — Usage, costs and pricing

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-030-F001](#mod-030-f001) | Price refresh, model prices and rollout migrations | BACK-024 | AUD-PRICE |

<a id="mod-030-f001"></a>

#### MOD-030-F001 — Price refresh, model prices and rollout migrations

Module: [MOD-030](#mod-030). Previous audit ID: `BACK-024`. Native references: AUD-PRICE.

Surface: Backend / CLI / integration. **Feature coverage: Contract/local only.** Pricing-policy and migration/rollout tests exist. No recurring live price-source-to-billed-invoice E2E was identified.

Existing execution selection/limits: Gateway CI; source/fixture and rollout checks.

Related feature evidence: [EV-102](#ev-102), [EV-103](#ev-103). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-030-f001-s001"></a>`MOD-030-F001-S001` | Refresh the model pricing source | Valid versioned prices become available with source/revision metadata. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-030-f001-s002"></a>`MOD-030-F001-S002` | Receive missing, invalid or stale price data | Unknown/unusable prices are explicit and cannot be silently treated as free. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-030-f001-s003"></a>`MOD-030-F001-S003` | Apply a pricing migration/rollout then reload | Persisted price schema remains compatible with consumers. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-030-f001-s004"></a>`MOD-030-F001-S004` | Bill a known model request under the effective price version | Recorded costs reconcile with token usage and applicable price revision. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-031"></a>

### MOD-031 — Assistant and durable sessions

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-031-F001](#mod-031-f001) | Durable turns, replay and user/tenant isolation | BACK-032 | SEC/DATA/SES #6931/#6932/#147 |
| [MOD-031-F002](#mod-031-f002) | Grounded activity, external sources and citations | BACK-033 | ACT/EXT #6933/#6934 |
| [MOD-031-F003](#mod-031-f003) | Session lifecycle, faults, latency and idle costs | BACK-034 | SES/QUAL #147/#6937; WARM #183 |
| [MOD-031-F004](#mod-031-f004) | Installation diagnostics and upgrade ledgers | BACK-035 | DEP/EXT #6935 |
| [MOD-031-F005](#mod-031-f005) | Current authenticated WebSocket baseline | BACK-036 | HEAD01/HEAD03/HEAD04 #6939 |

<a id="mod-031-f001"></a>

#### MOD-031-F001 — Durable turns, replay and user/tenant isolation

Module: [MOD-031](#mod-031). Previous audit ID: `BACK-032`. Native references: SEC/DATA/SES #6931/#6932/#147.

Surface: Backend / CLI / integration. **Feature coverage: Missing driver.** E43/E45 are registered, but assistant_stream and assistant_isolation are absent from the dispatcher; contract code does not supply these journeys.

Existing execution selection/limits: No executable acceptance for these IDs; assistant scope is manual.

Related feature evidence: [EV-104](#ev-104), [EV-038](#ev-038), [EV-039](#ev-039). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-031-f001-s001"></a>`MOD-031-F001-S001` | Submit and durably record an assistant turn | Turn identity and accepted input survive the supported persistence boundary. | MISSING-DRIVER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-039](#ev-039) — E43/E45 are registered, but assistant_stream and assistant_isolation are absent from the dispatcher; contract code does not supply these journeys. |
| <a id="mod-031-f001-s002"></a>`MOD-031-F001-S002` | Reconnect and replay turn events | Events resume without duplicating the assistant's external actions. | MISSING-DRIVER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-039](#ev-039) — E43/E45 are registered, but assistant_stream and assistant_isolation are absent from the dispatcher; contract code does not supply these journeys. |
| <a id="mod-031-f001-s003"></a>`MOD-031-F001-S003` | Read another user's or tenant's assistant session | Session content and execution authority remain isolated. | MISSING-DRIVER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-039](#ev-039) — E43/E45 are registered, but assistant_stream and assistant_isolation are absent from the dispatcher; contract code does not supply these journeys. |
| <a id="mod-031-f001-s004"></a>`MOD-031-F001-S004` | Crash/restart during a turn then recover | Durable state yields one coherent terminal outcome rather than a fabricated response. | MISSING-DRIVER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-039](#ev-039) — E43/E45 are registered, but assistant_stream and assistant_isolation are absent from the dispatcher; contract code does not supply these journeys. |

<a id="mod-031-f002"></a>

#### MOD-031-F002 — Grounded activity, external sources and citations

Module: [MOD-031](#mod-031). Previous audit ID: `BACK-033`. Native references: ACT/EXT #6933/#6934.

Surface: Backend / CLI / integration. **Feature coverage: Missing driver.** E44 assistant_sources driver is absent.

Existing execution selection/limits: Outside PR daily/weekly.

Related feature evidence: [EV-104](#ev-104), [EV-039](#ev-039). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-031-f002-s001"></a>`MOD-031-F002-S001` | Request activity grounded in platform records | Response cites actual authorized activity sources. | MISSING-DRIVER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-039](#ev-039) — E44 assistant_sources driver is absent. |
| <a id="mod-031-f002-s002"></a>`MOD-031-F002-S002` | Use an allowed external source | Retrieval evidence is attributable and distinguished from model inference. | MISSING-DRIVER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-039](#ev-039) — E44 assistant_sources driver is absent. |
| <a id="mod-031-f002-s003"></a>`MOD-031-F002-S003` | Open cited evidence | Citation resolves to the intended source/version within caller permissions. | MISSING-DRIVER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-039](#ev-039) — E44 assistant_sources driver is absent. |
| <a id="mod-031-f002-s004"></a>`MOD-031-F002-S004` | Remove source access before retrieval | Assistant cannot disclose revoked/foreign evidence. | MISSING-DRIVER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-039](#ev-039) — E44 assistant_sources driver is absent. |

<a id="mod-031-f003"></a>

#### MOD-031-F003 — Session lifecycle, faults, latency and idle costs

Module: [MOD-031](#mod-031). Previous audit ID: `BACK-034`. Native references: SES/QUAL #147/#6937; WARM #183.

Surface: Backend / CLI / integration. **Feature coverage: Missing driver.** E46/E47/E49 driver entries have no remote implementation. They must not be counted from unrelated chat happy paths.

Existing execution selection/limits: Outside PR daily/weekly; full assistant scope cannot pass.

Related feature evidence: [EV-038](#ev-038), [EV-039](#ev-039). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-031-f003-s001"></a>`MOD-031-F003-S001` | Create/resume/end a durable assistant session | Supported lifecycle transitions preserve ownership and history. | MISSING-DRIVER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-039](#ev-039) — E46/E47/E49 driver entries have no remote implementation. They must not be counted from unrelated chat happy paths. |
| <a id="mod-031-f003-s002"></a>`MOD-031-F003-S002` | Inject transport/runtime failure during work | Recovery reports actual incomplete/terminal state. | MISSING-DRIVER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-039](#ev-039) — E46/E47/E49 driver entries have no remote implementation. They must not be counted from unrelated chat happy paths. |
| <a id="mod-031-f003-s003"></a>`MOD-031-F003-S003` | Measure first-event and terminal latency under declared conditions | Recorded timings meet only explicitly configured acceptance bounds. | MISSING-DRIVER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-039](#ev-039) — E46/E47/E49 driver entries have no remote implementation. They must not be counted from unrelated chat happy paths. |
| <a id="mod-031-f003-s004"></a>`MOD-031-F003-S004` | Observe idle session runtime and costs | Idle behavior is measured against documented resource policy. | MISSING-DRIVER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-039](#ev-039) — E46/E47/E49 driver entries have no remote implementation. They must not be counted from unrelated chat happy paths. |

<a id="mod-031-f004"></a>

#### MOD-031-F004 — Installation diagnostics and upgrade ledgers

Module: [MOD-031](#mod-031). Previous audit ID: `BACK-035`. Native references: DEP/EXT #6935.

Surface: Backend / CLI / integration. **Feature coverage: Missing driver.** E48 assistant_installations driver is absent.

Existing execution selection/limits: Outside PR daily/weekly.

Related feature evidence: [EV-104](#ev-104), [EV-039](#ev-039). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-031-f004-s001"></a>`MOD-031-F004-S001` | Read installation diagnostics for the active deployment | Diagnostic values identify the correct installed revision and capabilities. | MISSING-DRIVER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-039](#ev-039) — E48 assistant_installations driver is absent. |
| <a id="mod-031-f004-s002"></a>`MOD-031-F004-S002` | Upgrade and inspect the installation ledger | Ledger records the attempted and accepted release transitions. | MISSING-DRIVER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-039](#ev-039) — E48 assistant_installations driver is absent. |
| <a id="mod-031-f004-s003"></a>`MOD-031-F004-S003` | Fail an upgrade between stages | Incomplete state remains visible and supported recovery can resume safely. | MISSING-DRIVER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-039](#ev-039) — E48 assistant_installations driver is absent. |
| <a id="mod-031-f004-s004"></a>`MOD-031-F004-S004` | Inspect diagnostics from another tenant/profile | Private installation/session state is not disclosed. | MISSING-DRIVER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-039](#ev-039) — E48 assistant_installations driver is absent. |

<a id="mod-031-f005"></a>

#### MOD-031-F005 — Current authenticated WebSocket baseline

Module: [MOD-031](#mod-031). Previous audit ID: `BACK-036`. Native references: HEAD01/HEAD03/HEAD04 #6939.

Surface: Backend / CLI / integration. **Feature coverage: Live harness.** E50 creates a server-owned session and requires a correlated final response using current protocol; explicitly not future durable/replay qualification.

Existing execution selection/limits: Manual assistant scope with ordinary-user fixture; not in PR profiles.

Related feature evidence: [EV-067](#ev-067). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-031-f005-s001"></a>`MOD-031-F005-S001` | Create a server-owned session through authenticated WebSocket | Session identity is returned by the server rather than chosen by the caller. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-065](#test-065) | [EV-067](#ev-067) — E50 current protocol/server-owned session and correlated final response; no durable-replay claim. |
| <a id="mod-031-f005-s002"></a>`MOD-031-F005-S002` | Send a bounded baseline request | Correlated terminal response arrives on the current protocol. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-065](#test-065) | [EV-067](#ev-067) — E50 current protocol/server-owned session and correlated final response; no durable-replay claim. |
| <a id="mod-031-f005-s003"></a>`MOD-031-F005-S003` | Send with expired or wrong-session authority | Request cannot use another user's session. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-031-f005-s004"></a>`MOD-031-F005-S004` | Close the session/connection and verify cleanup | Baseline resources do not remain indefinitely active. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-032"></a>

### MOD-032 — Hosted coding and runtimes

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-032-F001](#mod-032-f001) | Enrolled human repository Task | BACK-037 | #5516 |
| [MOD-032-F002](#mod-032-f002) | Native Codex SDK/worker/gateway integration | BACK-038 | #5433; AUD-CODEX-HOST |
| [MOD-032-F003](#mod-032-f003) | Queue-to-pod scaling and runtime isolation | BACK-039 | AUD-KEDA; OC-38 |

<a id="mod-032-f001"></a>

#### MOD-032-F001 — Enrolled human repository Task

Module: [MOD-032](#mod-032). Previous audit ID: `BACK-037`. Native references: #5516.

Surface: Backend / CLI / integration. **Feature coverage: Live harness.** E42 implements bounded submit/replay/monitor/control and terminal readback. It needs an owned repository task fixture.

Existing execution selection/limits: Current nightly/PR daily.

Related feature evidence: [EV-078](#ev-078). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-032-f001-s001"></a>`MOD-032-F001-S001` | Submit a task against an enrolled owned repository | Task is accepted under the intended principal/repository capability. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-068](#test-068) | [EV-078](#ev-078) — E42 bounded hosted-coding fixture; only supported tested controls/provider outcome are qualified. |
| <a id="mod-032-f001-s002"></a>`MOD-032-F001-S002` | Replay the same bounded submission | No duplicate task or unintended repository action is created. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-068](#test-068) | [EV-078](#ev-078) — E42 bounded hosted-coding fixture; only supported tested controls/provider outcome are qualified. |
| <a id="mod-032-f001-s003"></a>`MOD-032-F001-S003` | Monitor and control the accepted task | Status/events and accepted controls remain bound to the task. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-068](#test-068) | [EV-078](#ev-078) — E42 bounded hosted-coding fixture; only supported tested controls/provider outcome are qualified. |
| <a id="mod-032-f001-s004"></a>`MOD-032-F001-S004` | Read terminal result and repository outcome | Result corresponds to actual authorized work; fixture cleanup is verified. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-068](#test-068) | [EV-078](#ev-078) — E42 bounded hosted-coding fixture; only supported tested controls/provider outcome are qualified. |

<a id="mod-032-f002"></a>

#### MOD-032-F002 — Native Codex SDK/worker/gateway integration

Module: [MOD-032](#mod-032). Previous audit ID: `BACK-038`. Native references: #5433; AUD-CODEX-HOST.

Surface: Backend / CLI / integration. **Feature coverage: Contract/local only.** Real worker and SDK processes run against fixture inference/gateway/provider transports. Strong integration coverage, not deployed paid-model or GitHub acceptance.

Existing execution selection/limits: CI and PR offline sweep.

Related feature evidence: [EV-105](#ev-105), [EV-106](#ev-106), [EV-107](#ev-107), [EV-108](#ev-108). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-032-f002-s001"></a>`MOD-032-F002-S001` | Start the real Codex SDK/worker against fixture transports | Worker obeys SDK and Gateway runtime contracts. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-032-f002-s002"></a>`MOD-032-F002-S002` | Complete a task/GitHub host interaction through fixture providers | Host emits the expected request, result and lifecycle messages. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-032-f002-s003"></a>`MOD-032-F002-S003` | Reject malformed or unauthorized transport responses | Worker cannot progress using invalid identity or incomplete result data. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-032-f002-s004"></a>`MOD-032-F002-S004` | Repeat against real deployed inference and GitHub | Provider-bound behavior is independently qualified before calling it live E2E. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-032-f003"></a>

#### MOD-032-F003 — Queue-to-pod scaling and runtime isolation

Module: [MOD-032](#mod-032). Previous audit ID: `BACK-039`. Native references: AUD-KEDA; OC-38.

Surface: Backend / CLI / integration. **Feature coverage: Partial.** Live KEDA tests and separate validation-isolation qualifiers exist; scheduling/image tests are not proof of full worker teardown under every fault.

Existing execution selection/limits: Manual live checks; not added to daily by PR.

Related feature evidence: [EV-109](#ev-109), [EV-110](#ev-110). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-032-f003-s001"></a>`MOD-032-F003-S001` | Enqueue work and observe KEDA scale-up | Eligible workers become available to consume the actual queue. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-030](#test-030) | [EV-109](#ev-109) — Opt-in live scaler harness exists; runtime-failure cleanup is separate. |
| <a id="mod-032-f003-s002"></a>`MOD-032-F003-S002` | Drain work and observe scale-down | Idle workers retire according to configured scaling policy. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-032-f003-s003"></a>`MOD-032-F003-S003` | Run two isolated workload scopes | Worker resources and credentials cannot cross their admitted scopes. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-032-f003-s004"></a>`MOD-032-F003-S004` | Crash a worker during allocation/execution | Recovery and teardown are observed without leaking worker resources. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-033"></a>

### MOD-033 — Other channel adapters

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-033-F001](#mod-033-f001) | Slack and other messaging integrations | BACK-054 | OC-02/03/04/05/06/33/42/49/50 |

<a id="mod-033-f001"></a>

#### MOD-033-F001 — Slack and other messaging integrations

Module: [MOD-033](#mod-033). Previous audit ID: `BACK-054`. Native references: OC-02/03/04/05/06/33/42/49/50.

Surface: Backend / CLI / integration. **Feature coverage: Missing assertion.** OpenClaw parity marks partial channels xfail and absent channels skip; these rows are catalogued aspirations, not shipped or covered features.

Existing execution selection/limits: Manual parity; not daily coverage.

Related feature evidence: [EV-129](#ev-129), [EV-130](#ev-130). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-033-f001-s001"></a>`MOD-033-F001-S001` | Attempt a configured supported messaging channel | Ingress authenticates identity and binds the correct tenant. | PLACEHOLDER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-129](#ev-129) — Partial channels xfail and absent channels skip; implementation status varies by channel. |
| <a id="mod-033-f001-s002"></a>`MOD-033-F001-S002` | Reply to an existing channel conversation | Response correlates to the intended authorized thread. | PLACEHOLDER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-129](#ev-129) — Partial channels xfail and absent channels skip; implementation status varies by channel. |
| <a id="mod-033-f001-s003"></a>`MOD-033-F001-S003` | Replay or forge a channel event | No duplicate or foreign execution authority is created. | PLACEHOLDER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-129](#ev-129) — Partial channels xfail and absent channels skip; implementation status varies by channel. |
| <a id="mod-033-f001-s004"></a>`MOD-033-F001-S004` | Select an unimplemented channel | Capability is reported unavailable and placeholder skips cannot count as acceptance. | PLACEHOLDER | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-129](#ev-129) — Partial channels xfail and absent channels skip; implementation status varies by channel. |

<a id="mod-034"></a>

### MOD-034 — Vulnerability remediation

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-034-F001](#mod-034-f001) | Ingest → SBOM → CVE → reachability → issue/fix | BACK-061 | #1355/#1357/#1358/#1359/#1360 |

<a id="mod-034-f001"></a>

#### MOD-034-F001 — Ingest → SBOM → CVE → reachability → issue/fix

Module: [MOD-034](#mod-034). Previous audit ID: `BACK-061`. Native references: #1355/#1357/#1358/#1359/#1360.

Surface: Backend / CLI / integration. **Feature coverage: Missing assertion.** All eight tests in test_vuln_e2e.py unconditionally skip implementation-deferred steps. This is a placeholder suite.

Existing execution selection/limits: No implemented complete E2E path in this suite.

Related feature evidence: [EV-137](#ev-137). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-034-f001-s001"></a>`MOD-034-F001-S001` | Ingest a repository and produce its SBOM | Dependency inventory matches the submitted source revision. | PLACEHOLDER | **0** (E2E 0, local 0); existing total unknown | [TEST-016](#test-016), [TEST-017](#test-017); excluded: 2 placeholder | [EV-137](#ev-137) — All eight suite tests skip implementation-deferred steps. |
| <a id="mod-034-f001-s002"></a>`MOD-034-F001-S002` | Match known vulnerable components to CVEs | Findings retain affected component/version and evidence. | PLACEHOLDER | **0** (E2E 0, local 0); existing total unknown | [TEST-018](#test-018), [TEST-019](#test-019); excluded: 2 placeholder | [EV-137](#ev-137) — All eight suite tests skip implementation-deferred steps. |
| <a id="mod-034-f001-s003"></a>`MOD-034-F001-S003` | Evaluate reachability for the fixture vulnerability | Classification is supported by actual analysis. | PLACEHOLDER | **0** (E2E 0, local 0); existing total unknown | [TEST-020](#test-020), [TEST-023](#test-023); excluded: 2 placeholder | [EV-137](#ev-137) — All eight suite tests skip implementation-deferred steps. |
| <a id="mod-034-f001-s004"></a>`MOD-034-F001-S004` | Create an authorized issue/fix and verify remediation | Published artifact addresses the intended finding and passes an independent recheck. | PLACEHOLDER | **0** (E2E 0, local 0); existing total unknown | [TEST-021](#test-021), [TEST-022](#test-022); excluded: 2 placeholder | [EV-137](#ev-137) — All eight suite tests skip implementation-deferred steps. |

<a id="mod-035"></a>

### MOD-035 — Personal memory

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-035-F001](#mod-035-f001) | Preference/learning persistence and cross-session recall | BACK-062 | #53 §§2.5–2.6; OC-11 |

<a id="mod-035-f001"></a>

#### MOD-035-F001 — Preference/learning persistence and cross-session recall

Module: [MOD-035](#mod-035). Previous audit ID: `BACK-062`. Native references: #53 §§2.5–2.6; OC-11.

Surface: Backend / CLI / integration. **Feature coverage: Live harness.** Four real WS-to-DynamoDB/recall tests exist. Separate costly/live opt-in is required.

Existing execution selection/limits: Manual Make target; outside PR daily/weekly.

Related feature evidence: [EV-138](#ev-138), [EV-139](#ev-139). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-035-f001-s001"></a>`MOD-035-F001-S001` | Persist an explicit personal preference through chat | Memory is stored under the authenticated owner. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-032](#test-032) | [EV-138](#ev-138) [test_save_preference_creates_memory_row](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_memory_e2e.py#L52) — Opt-in live WS-to-DynamoDB preference persistence. |
| <a id="mod-035-f001-s002"></a>`MOD-035-F001-S002` | Recall the preference in a separate session | New session retrieves the persisted value rather than fixture-only state. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-031](#test-031) | [EV-138](#ev-138) [test_cross_session_preference_recall](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_memory_e2e.py#L201) — Opt-in live cross-session recall. |
| <a id="mod-035-f001-s003"></a>`MOD-035-F001-S003` | Update learning/preference and recall it again | Latest supported memory semantics are observable across sessions. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-035-f001-s004"></a>`MOD-035-F001-S004` | Attempt to recall another user's memory | Personal memory remains isolated across users and tenants. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-036"></a>

### MOD-036 — Artifacts

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-036-F001](#mod-036-f001) | Publish, retrieve, edit and superseding lineage | BACK-063 | #53 §§2.7–2.8; OC-17 |

<a id="mod-036-f001"></a>

#### MOD-036-F001 — Publish, retrieve, edit and superseding lineage

Module: [MOD-036](#mod-036). Previous audit ID: `BACK-063`. Native references: #53 §§2.7–2.8; OC-17.

Surface: Backend / CLI / integration. **Feature coverage: Live harness.** Two real WS-to-S3/catalog tests exist with cleanup. Not automatically exercised by browser Chat or Task API unit tests.

Existing execution selection/limits: Manual costly live suite; outside PR profiles.

Related feature evidence: [EV-140](#ev-140), [EV-139](#ev-139). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-036-f001-s001"></a>`MOD-036-F001-S001` | Publish an artifact from a real chat/run | Object and catalogue entry exist with correct identity and content. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-025](#test-025) | [EV-140](#ev-140) [test_publish_artifact_creates_s3_and_catalog](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_artifacts_e2e.py#L55) — Opt-in live object/catalog publication and cleanup. |
| <a id="mod-036-f001-s002"></a>`MOD-036-F001-S002` | Retrieve the artifact as its authorized owner | Content and metadata correspond to the published artifact. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-036-f001-s003"></a>`MOD-036-F001-S003` | Edit/supersede an artifact | Lineage retains the prior version and identifies the new current version. | PARTIAL-HARNESS | **1** (E2E 1, local 0); existing total unknown | [TEST-024](#test-024) | [EV-140](#ev-140) [test_fetch_edit_publish_with_supersedes](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_artifacts_e2e.py#L171) — Opt-in live fetch/edit/superseding lineage. |
| <a id="mod-036-f001-s004"></a>`MOD-036-F001-S004` | Attempt foreign access then clean up owned artifacts | Access is refused and test objects/catalogue state are removed. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-037"></a>

### MOD-037 — Research / gbrain

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-037-F001](#mod-037-f001) | MCP authentication, migrations and persistent memory | BACK-064 | AUD-GBRAIN-MCP |
| [MOD-037-F002](#mod-037-f002) | Scheduled memory consolidation | BACK-065 | OC-31; AUD-GBRAIN-DREAM |

<a id="mod-037-f001"></a>

#### MOD-037-F001 — MCP authentication, migrations and persistent memory

Module: [MOD-037](#mod-037). Previous audit ID: `BACK-064`. Native references: AUD-GBRAIN-MCP.

Surface: Backend / CLI / integration. **Feature coverage: Partial.** Real image plus disposable PostgreSQL tests prove local container boundaries; separate manual service smoke exists. They are not current AWS-deployment proof.

Existing execution selection/limits: Opt-in Docker runtime/manual smoke; not called by PR.

Related feature evidence: [EV-141](#ev-141), [EV-142](#ev-142). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-037-f001-s001"></a>`MOD-037-F001-S001` | Start the gbrain container with a fresh PostgreSQL database | Required migrations complete and runtime becomes usable. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-037-f001-s002"></a>`MOD-037-F001-S002` | Authenticate an MCP client and persist a memory | Memory survives a new session/container connection. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-037-f001-s003"></a>`MOD-037-F001-S003` | Use invalid credentials or foreign scope | Private memory cannot be read or modified. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-037-f001-s004"></a>`MOD-037-F001-S004` | Repeat the smoke against the intended deployed service | Real deployment identity/storage are qualified separately from disposable-container tests. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-037-f002"></a>

#### MOD-037-F002 — Scheduled memory consolidation

Module: [MOD-037](#mod-037). Previous audit ID: `BACK-065`. Native references: OC-31; AUD-GBRAIN-DREAM.

Surface: Backend / CLI / integration. **Feature coverage: Contract/local only.** Container dream test uses real PostgreSQL and a mock embedding endpoint; does not execute a production scheduled task or validate real model semantics.

Existing execution selection/limits: Opt-in container test; separate production scheduler.

Related feature evidence: [EV-143](#ev-143), [EV-144](#ev-144). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-037-f002-s001"></a>`MOD-037-F002-S001` | Run memory consolidation over known input memories | Output is persisted with traceable source relationships. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-037-f002-s002"></a>`MOD-037-F002-S002` | Re-run consolidation on the same input | Supported deduplication semantics avoid uncontrolled duplicate memories. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-037-f002-s003"></a>`MOD-037-F002-S003` | Fail the embedding/model dependency | Job records failure/partial work without claiming semantic success. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-037-f002-s004"></a>`MOD-037-F002-S004` | Execute the real scheduled job with production-compatible inference | Schedule, model semantics and storage outcomes are observed together. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-038"></a>

### MOD-038 — Shared tools and Task SDK

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-038-F001](#mod-038-f001) | Task-scoped tool grants, identity, revocation and receipts | BACK-066 | AUD-TOOLS-AUTH |
| [MOD-038-F002](#mod-038-f002) | Web search, browser and code interpreter tools | BACK-067 | AUD-TOOLS-EXEC |

<a id="mod-038-f001"></a>

#### MOD-038-F001 — Task-scoped tool grants, identity, revocation and receipts

Module: [MOD-038](#mod-038). Previous audit ID: `BACK-066`. Native references: AUD-TOOLS-AUTH.

Surface: Backend / CLI / integration. **Feature coverage: Contract/local only.** Transport/SDK tests exercise real handlers with mocked AWS/provider responses. No full external admission-to-tool authority qualification in daily.

Existing execution selection/limits: Shared tools CI and PR offline sweep.

Related feature evidence: [EV-145](#ev-145), [EV-146](#ev-146), [EV-147](#ev-147). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-038-f001-s001"></a>`MOD-038-F001-S001` | Acquire task-scoped tool grants for an authorized task | Grant binds principal, task, tools and allowed parameters. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-038-f001-s002"></a>`MOD-038-F001-S002` | Use a grant through the SDK/transport | Operation and receipt reference the admitted task authority. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-038-f001-s003"></a>`MOD-038-F001-S003` | Revoke the grant then repeat the operation | Fresh use is refused even if an old SDK handle remains available. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-038-f001-s004"></a>`MOD-038-F001-S004` | Forge task identity or reuse another task's grant | No cross-task tool operation is authorized. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-038-f002"></a>

#### MOD-038-F002 — Web search, browser and code interpreter tools

Module: [MOD-038](#mod-038). Previous audit ID: `BACK-067`. Native references: AUD-TOOLS-EXEC.

Surface: Backend / CLI / integration. **Feature coverage: Partial.** Provider/runtime packaging checks exist; selected chat/Cyber paths exercise tools. This is not a systematic live matrix of all shared tool operations and cancellation.

Existing execution selection/limits: CI plus selected manual/domain/live journeys.

Related feature evidence: [EV-148](#ev-148), [EV-146](#ev-146), [EV-149](#ev-149). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-038-f002-s001"></a>`MOD-038-F002-S001` | Execute bounded web search/browser/code operations | Each supported tool produces attributable results within task scope. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-038-f002-s002"></a>`MOD-038-F002-S002` | Read only allowed files/network destinations | Tool isolation prevents access outside the admitted contract. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-038-f002-s003"></a>`MOD-038-F002-S003` | Cancel or time out a tool operation | Executor terminates/cleans up and reports the actual outcome. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-038-f002-s004"></a>`MOD-038-F002-S004` | Run a full admitted task using the real shared tool provider | Authority, tool result and terminal task settlement correlate. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-039"></a>

### MOD-039 — Validation service

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-039-F001](#mod-039-f001) | Isolated checks, source reconstruction and execution cleanup | BACK-068 | AUD-VALIDATION |

<a id="mod-039-f001"></a>

#### MOD-039-F001 — Isolated checks, source reconstruction and execution cleanup

Module: [MOD-039](#mod-039). Previous audit ID: `BACK-068`. Native references: AUD-VALIDATION.

Surface: Backend / CLI / integration. **Feature coverage: Partial.** Moto/service tests and operator EKS isolation/executor qualifiers exist. Qualifier explicitly sets taskAuthorityQualified=false; it does not prove authenticated Task admission.

Existing execution selection/limits: CI plus manual operator qualification; not daily live.

Related feature evidence: [EV-150](#ev-150), [EV-110](#ev-110), [EV-151](#ev-151). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-039-f001-s001"></a>`MOD-039-F001-S001` | Reconstruct source for a validation request | Exact intended source revision is used by the isolated executor. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-039-f001-s002"></a>`MOD-039-F001-S002` | Execute allowed checks in an isolated workspace | Results reflect actual commands and bounded runtime. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-039-f001-s003"></a>`MOD-039-F001-S003` | Attempt cross-workspace credentials/files/network access | Isolation prevents prohibited access. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-039-f001-s004"></a>`MOD-039-F001-S004` | Fail/cancel validation and verify cleanup | Temporary resources are removed or explicitly retained for governed recovery. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-039-f001-s005"></a>`MOD-039-F001-S005` | Invoke validation through authenticated Task admission | Full task authority is proven separately from the existing isolation/executor qualifiers. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-040"></a>

### MOD-040 — Harness jobs

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-040-F001](#mod-040-f001) | Atomic admission, durable outbox, retry and delivery | BACK-069 | #5526 |
| [MOD-040-F002](#mod-040-f002) | Resource inventory, recovery and cleanup authority | BACK-070 | #5529 |

<a id="mod-040-f001"></a>

#### MOD-040-F001 — Atomic admission, durable outbox, retry and delivery

Module: [MOD-040](#mod-040). Previous audit ID: `BACK-069`. Native references: #5526.

Surface: Backend / CLI / integration. **Feature coverage: Contract/local only.** Real PostgreSQL integration is mandatory in CI with zero skips and execution floor. It proves storage/contract behavior, not deployed provider delivery end to end.

Existing execution selection/limits: CI and PR offline sweep.

Related feature evidence: [EV-152](#ev-152), [EV-153](#ev-153), [EV-154](#ev-154). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-040-f001-s001"></a>`MOD-040-F001-S001` | Admit a job and write its outbox transaction | Job and durable publication intent commit atomically. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-040-f001-s002"></a>`MOD-040-F001-S002` | Crash between commit and delivery then retry | Exactly the documented at-least-once/idempotent delivery behavior occurs. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-040-f001-s003"></a>`MOD-040-F001-S003` | Race workers for the same eligible job | Only a valid lease/attempt can record completion. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-040-f001-s004"></a>`MOD-040-F001-S004` | Execute the job through a real deployed provider | Storage contract and external side effects are reconciled end to end. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-040-f002"></a>

#### MOD-040-F002 — Resource inventory, recovery and cleanup authority

Module: [MOD-040](#mod-040). Previous audit ID: `BACK-070`. Native references: #5529.

Surface: Backend / CLI / integration. **Feature coverage: Contract/local only.** Database/receipt and recovery tests exist; complete live provider allocation-through-cleanup remains domain qualification.

Existing execution selection/limits: CI; domain live checks separate.

Related feature evidence: [EV-152](#ev-152), [EV-153](#ev-153). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-040-f002-s001"></a>`MOD-040-F002-S001` | Record provider resource inventory and receipts | Allocation ownership and operation identity are durable. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-040-f002-s002"></a>`MOD-040-F002-S002` | Recover an interrupted allocation/cleanup attempt | Recovery uses retained authority and original resource identities. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-040-f002-s003"></a>`MOD-040-F002-S003` | Request cleanup with stale or foreign authority | Unauthorized provider resources are not released. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-040-f002-s004"></a>`MOD-040-F002-S004` | Complete release and reconcile remaining inventory | No unaccounted owned resources remain; unresolved releases stay visible. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-041"></a>

### MOD-041 — Human approvals / HITL

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-041-F001](#mod-041-f001) | Ticket contracts and human approval/refusal | BACK-071 | AUD-HITL; A6-4.human-refusal |

<a id="mod-041-f001"></a>

#### MOD-041-F001 — Ticket contracts and human approval/refusal

Module: [MOD-041](#mod-041). Previous audit ID: `BACK-071`. Native references: AUD-HITL; A6-4.human-refusal.

Surface: Backend / CLI / integration. **Feature coverage: Partial.** Golden contract fixtures plus orchestration human-refusal adapter. No successful current end-to-end approval/delivery run established by this audit.

Existing execution selection/limits: Contracts CI; live orchestration manual/blocked.

Related feature evidence: [EV-155](#ev-155), [EV-156](#ev-156), [EV-111](#ev-111). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-041-f001-s001"></a>`MOD-041-F001-S001` | Create a human-approval ticket from a pending operation | Ticket contract binds target, evidence, action and permitted reviewers. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-041-f001-s002"></a>`MOD-041-F001-S002` | Approve as an eligible reviewer | Approved operation receives only the intended bounded authority. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-041-f001-s003"></a>`MOD-041-F001-S003` | Reject or expire the ticket | Pending execution cannot proceed using absent approval. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-041-f001-s004"></a>`MOD-041-F001-S004` | Replay the decision after operation/evidence changes | Stale approval cannot authorize revised work. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-042"></a>

### MOD-042 — Provenance and audit

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-042-F001](#mod-042-f001) | Identity propagation, lineage and event audit | BACK-072 | AUD-PROVENANCE; NFR3.5 |

<a id="mod-042-f001"></a>

#### MOD-042-F001 — Identity propagation, lineage and event audit

Module: [MOD-042](#mod-042). Previous audit ID: `BACK-072`. Native references: AUD-PROVENANCE; NFR3.5.

Surface: Backend / CLI / integration. **Feature coverage: Partial.** Cross-module contracts and selected task/artifact journeys exist; no single recurring origin-to-final-artifact proof across all six harness surfaces.

Existing execution selection/limits: CI; selected manual live journeys.

Related feature evidence: [EV-157](#ev-157), [EV-158](#ev-158), [EV-140](#ev-140). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-042-f001-s001"></a>`MOD-042-F001-S001` | Carry identity from ingress through worker to final artifact | Server-owned actor/run/tenant lineage is preserved throughout. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-042-f001-s002"></a>`MOD-042-F001-S002` | Write an event with forged provenance fields | Caller cannot replace authoritative execution identity. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-042-f001-s003"></a>`MOD-042-F001-S003` | Inspect lineage after retry or handoff | New attempts/child work link to the correct parent without losing attribution. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-042-f001-s004"></a>`MOD-042-F001-S004` | Query audit/provenance as a scoped reader | Only authorized evidence is visible and missing segments are explicit. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-043"></a>

### MOD-043 — Security scanning

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-043-F001](#mod-043-f001) | Source, dependency, IaC and secret scanners | BACK-073 | AUD-SOURCE-SECURITY |
| [MOD-043-F002](#mod-043-f002) | Adversarial checks and model-assisted triage/delivery | BACK-074 | S8/T8; AUD-SECURITY-AGENT |

<a id="mod-043-f001"></a>

#### MOD-043-F001 — Source, dependency, IaC and secret scanners

Module: [MOD-043](#mod-043). Previous audit ID: `BACK-073`. Native references: AUD-SOURCE-SECURITY.

Surface: Backend / CLI / integration. **Feature coverage: Tooling, not product E2E.** Scanner workflows test source/image findings rather than a user journey. Proposed source scan lane excludes authenticated image/engine qualification.

Existing execution selection/limits: Current manual full scan; PR adds daily source scans.

Related feature evidence: [EV-159](#ev-159), [EV-160](#ev-160). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-043-f001-s001"></a>`MOD-043-F001-S001` | Run source/dependency/IaC/secret scanners on controlled fixtures | Expected findings are detected with tool/version evidence. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-043-f001-s002"></a>`MOD-043-F001-S002` | Scan a clean fixture and an intentionally failing fixture | Exit/report semantics distinguish success from findings/tool failure. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-043-f001-s003"></a>`MOD-043-F001-S003` | Run image scanning on the intended built artifact | Findings identify the actual digest rather than an unrelated image. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-043-f001-s004"></a>`MOD-043-F001-S004` | Publish scanner results without secret values | Reports preserve useful findings without leaking scanned credentials. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-043-f002"></a>

#### MOD-043-F002 — Adversarial checks and model-assisted triage/delivery

Module: [MOD-043](#mod-043). Previous audit ID: `BACK-074`. Native references: S8/T8; AUD-SECURITY-AGENT.

Surface: Backend / CLI / integration. **Feature coverage: Partial.** Adversarial harness is separate from manual Security Agent and image scans; latest Security Agent/full scan workflows failed. No unified successful current security E2E.

Existing execution selection/limits: Security Agent remains manual; adversarial PR daily.

Related feature evidence: [EV-161](#ev-161), [EV-159](#ev-159), [EV-162](#ev-162). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-043-f002-s001"></a>`MOD-043-F002-S001` | Run an authenticated adversarial probe against owned fixtures | Real protected boundary enforces the expected refusal. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-043-f002-s002"></a>`MOD-043-F002-S002` | Triage a controlled security finding using the Security Agent | Analysis references the finding and preserves uncertainty. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-043-f002-s003"></a>`MOD-043-F002-S003` | Publish/remediate through authorized provider paths | Changes are attributable and independently revalidated. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-043-f002-s004"></a>`MOD-043-F002-S004` | Fail a required scanner/model/deployment prerequisite | Overall result cannot claim complete security acceptance. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-044"></a>

### MOD-044 — Cyber investigations

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-044-F001](#mod-044-f001) | Adaptive URL investigation and hosted analyst | BACK-075 | AUD-CYBER-URL |
| [MOD-044-F002](#mod-044-f002) | File triage, static analysis and sandbox detonation | BACK-076 | AUD-CYBER-MALWARE |

<a id="mod-044-f001"></a>

#### MOD-044-F001 — Adaptive URL investigation and hosted analyst

Module: [MOD-044](#mod-044). Previous audit ID: `BACK-075`. Native references: AUD-CYBER-URL.

Surface: Backend / CLI / integration. **Feature coverage: Historical evidence.** AWS model/browser acceptance scripts and September 24 controlled-site evidence exist. CI live_evaluation tests use a protocol model; historical acceptance excludes public-network and verdict-quality claims.

Existing execution selection/limits: Manual AWS qualification; PR offline checks only.

Related feature evidence: [EV-163](#ev-163), [EV-164](#ev-164), [EV-165](#ev-165). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-044-f001-s001"></a>`MOD-044-F001-S001` | Investigate a controlled site with real model/browser tools | Findings cite actual observed network/content evidence. | HISTORICAL | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-163](#ev-163) — Related retained feature qualification exists; this entire scenario is not independently verified on the current deployment. |
| <a id="mod-044-f001-s002"></a>`MOD-044-F001-S002` | Adapt investigation based on intermediate observations | Subsequent tool use is bounded and grounded in prior evidence. | HISTORICAL | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-163](#ev-163) — Related retained feature qualification exists; this entire scenario is not independently verified on the current deployment. |
| <a id="mod-044-f001-s003"></a>`MOD-044-F001-S003` | Remove a required source or live-network capability | Report states the actual investigation limitation. | HISTORICAL | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-163](#ev-163) — Related retained feature qualification exists; this entire scenario is not independently verified on the current deployment. |
| <a id="mod-044-f001-s004"></a>`MOD-044-F001-S004` | Produce an analyst verdict and confidence | Verdict-quality/public-network claims require their own explicit acceptance evidence. | HISTORICAL | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | [EV-163](#ev-163) — Related retained feature qualification exists; this entire scenario is not independently verified on the current deployment. |

<a id="mod-044-f002"></a>

#### MOD-044-F002 — File triage, static analysis and sandbox detonation

Module: [MOD-044](#mod-044). Previous audit ID: `BACK-076`. Native references: AUD-CYBER-MALWARE.

Surface: Backend / CLI / integration. **Feature coverage: Partial.** Worker handler, script-guard, isolation and VM smoke tooling exist. No scheduled full upload-to-detonation-to-published-verdict E2E was found.

Existing execution selection/limits: CI/VM/operator checks; outside PR full live coverage.

Related feature evidence: [EV-166](#ev-166), [EV-167](#ev-167), [EV-168](#ev-168). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-044-f002-s001"></a>`MOD-044-F002-S001` | Upload an allowed file into the triage workflow | File identity/digest and ownership persist through dispatch. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-044-f002-s002"></a>`MOD-044-F002-S002` | Run static analysis against a known sample fixture | Findings match actual file behavior/evidence. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-044-f002-s003"></a>`MOD-044-F002-S003` | Execute an approved sandbox detonation | Isolation, bounded runtime and collected artifacts are verified. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-044-f002-s004"></a>`MOD-044-F002-S004` | Attempt a prohibited script or cross-sandbox operation | Guard/isolation rejects it without uncontrolled execution. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-044-f002-s005"></a>`MOD-044-F002-S005` | Publish result and clean up sandbox resources | Final verdict cites collected evidence and owned runtime resources are released. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-045"></a>

### MOD-045 — Platform deployment verification

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-045-F001](#mod-045-f001) | AWS deployment prerequisite/phase checks | BACK-082 | AUD-DEPLOY-P1 |

<a id="mod-045-f001"></a>

#### MOD-045-F001 — AWS deployment prerequisite/phase checks

Module: [MOD-045](#mod-045). Previous audit ID: `BACK-082`. Native references: AUD-DEPLOY-P1.

Surface: Backend / CLI / integration. **Feature coverage: Partial.** Integration test accepts run_phase exit 0 or 1: it proves no crash, not successful verification. Manual verify workflow runs phase 1 only.

Existing execution selection/limits: Manual; no main workflow runs returned in inspected history.

Related feature evidence: [EV-169](#ev-169), [EV-170](#ev-170). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-045-f001-s001"></a>`MOD-045-F001-S001` | Run deployment prerequisite verification against an owned target | Each required dependency is actually checked with explicit pass/fail. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-045-f001-s002"></a>`MOD-045-F001-S002` | Inject a failed phase check | Verification records failure rather than counting any non-crashing exit as success. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-045-f001-s003"></a>`MOD-045-F001-S003` | Execute all advertised verification phases | Report identifies executed and unexecuted phases separately. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-045-f001-s004"></a>`MOD-045-F001-S004` | Run with missing account/role prerequisites | Verification refuses misleading success and explains the missing prerequisite. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-046"></a>

### MOD-046 — Platform release and upgrades

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-046-F001](#mod-046-f001) | Artifact deployment, digest readiness and integration preservation | BACK-083 | AUD-RELEASE |
| [MOD-046-F002](#mod-046-f002) | Fresh install, cross-account upgrade, rollback and teardown | BACK-084 | #5641; AUD-DEPLOY-LIFECYCLE |

<a id="mod-046-f001"></a>

#### MOD-046-F001 — Artifact deployment, digest readiness and integration preservation

Module: [MOD-046](#mod-046). Previous audit ID: `BACK-083`. Native references: AUD-RELEASE.

Surface: Backend / CLI / integration. **Feature coverage: Live harness.** Mandatory release acceptance checks deployed images/Lambdas/frontend, health and preservation, with WebSocket smoke. Separate from generic CLI/readiness tests.

Existing execution selection/limits: Release/upgrade workflow; PR daily reuses offline release tests only.

Related feature evidence: [EV-086](#ev-086), [EV-171](#ev-171), [EV-172](#ev-172). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-046-f001-s001"></a>`MOD-046-F001-S001` | Deploy an intended release and verify service/Lambda/frontend digests | Observed deployed artifacts match the accepted release manifest. | PARTIAL-HARNESS | **0** (E2E 0, local 0); existing total unknown | [TEST-062](#test-062); excluded: 1 check | [EV-086](#ev-086) — Release acceptance gate checks deployed revision/services and WS/preservation; no new execution in this audit. |
| <a id="mod-046-f001-s002"></a>`MOD-046-F001-S002` | Run health and real WebSocket acceptance | Current deployed revision serves the required actual interaction. | PARTIAL-HARNESS | **0** (E2E 0, local 0); existing total unknown | [TEST-062](#test-062); excluded: 1 check | [EV-086](#ev-086) — Release acceptance gate checks deployed revision/services and WS/preservation; no new execution in this audit. |
| <a id="mod-046-f001-s003"></a>`MOD-046-F001-S003` | Upgrade while preserving configured integrations | Connections and supported workflows still function afterward. | PARTIAL-HARNESS | **0** (E2E 0, local 0); existing total unknown | [TEST-062](#test-062); excluded: 1 check | [EV-086](#ev-086) — Release acceptance gate checks deployed revision/services and WS/preservation; no new execution in this audit. |
| <a id="mod-046-f001-s004"></a>`MOD-046-F001-S004` | Fail required release acceptance | Release cannot be reported qualified solely from build/deploy command success. | PARTIAL-HARNESS | **0** (E2E 0, local 0); existing total unknown | [TEST-062](#test-062); excluded: 1 check | [EV-086](#ev-086) — Release acceptance gate checks deployed revision/services and WS/preservation; no new execution in this audit. |

<a id="mod-046-f002"></a>

#### MOD-046-F002 — Fresh install, cross-account upgrade, rollback and teardown

Module: [MOD-046](#mod-046). Previous audit ID: `BACK-084`. Native references: #5641; AUD-DEPLOY-LIFECYCLE.

Surface: Backend / CLI / integration. **Feature coverage: Partial.** Extensive script/upgrade contracts and release gate exist; E41 reports metadata without deploying. No complete recurring fresh-account install→upgrade→rollback→teardown suite found.

Existing execution selection/limits: Offline CI, E41 daily and separate operator release qualification.

Related feature evidence: [EV-173](#ev-173), [EV-002](#ev-002), [EV-086](#ev-086). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-046-f002-s001"></a>`MOD-046-F002-S001` | Install into an approved clean target account | Required phases complete and the newly installed product passes acceptance. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-046-f002-s002"></a>`MOD-046-F002-S002` | Upgrade an existing deployment across supported versions | State and integrations survive the supported migration path. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-046-f002-s003"></a>`MOD-046-F002-S003` | Interrupt upgrade then roll back/recover | A supported working revision and consistent state are restored. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-046-f002-s004"></a>`MOD-046-F002-S004` | Teardown the disposable deployment | Resource inventory confirms owned resources are removed or documented retained items remain. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-046-f002-s005"></a>`MOD-046-F002-S005` | Repeat in a distinct target account | Account-specific wiring and authority are verified rather than inferred from one environment. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-047"></a>

### MOD-047 — Observability and performance

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-047-F001](#mod-047-f001) | Metrics, latency, scaling and trace completeness | BACK-085 | FR9.4; NFR1/NFR5 |

<a id="mod-047-f001"></a>

#### MOD-047-F001 — Metrics, latency, scaling and trace completeness

Module: [MOD-047](#mod-047). Previous audit ID: `BACK-085`. Native references: FR9.4; NFR1/NFR5.

Surface: Backend / CLI / integration. **Feature coverage: Partial.** Health/metrics probes, GitLab acknowledgement latency and selected browser timings exist. Assistant E49 is unimplemented; no full recurring load/SLO/distributed-trace acceptance found.

Existing execution selection/limits: Selected live/CI checks; not complete performance coverage.

Related feature evidence: [EV-033](#ev-033), [EV-174](#ev-174), [EV-039](#ev-039). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-047-f001-s001"></a>`MOD-047-F001-S001` | Measure request/agent latency under declared fixture load | Report includes actual sample distribution and acceptance bounds. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-047-f001-s002"></a>`MOD-047-F001-S002` | Correlate one request across Gateway, workers and downstream services | Trace links preserve identity without missing required spans. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-047-f001-s003"></a>`MOD-047-F001-S003` | Saturate a bounded workload and observe scaling | Capacity/throttling measurements correspond to actual runtime behavior. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-047-f001-s004"></a>`MOD-047-F001-S004` | Measure idle assistant/runtime cost | Evidence supports the stated resource/cost claim; absent driver stays unqualified. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

<a id="mod-048"></a>

### MOD-048 — User-service and harness roadmap

| Feature ID | Feature | Previous audit ID | Native IDs |
| --- | --- | --- | --- |
| [MOD-048-F001](#mod-048-f001) | Standalone personal agents, chief-of-staff, generic MCP hub/events and future channels | BACK-086 | AUD-ROADMAP; OC-21/29/30/32/33/34/48 |

<a id="mod-048-f001"></a>

#### MOD-048-F001 — Standalone personal agents, chief-of-staff, generic MCP hub/events and future channels

Module: [MOD-048](#mod-048). Previous audit ID: `BACK-086`. Native references: AUD-ROADMAP; OC-21/29/30/32/33/34/48.

Surface: Backend / CLI / integration. **Feature coverage: No implemented E2E found.** User-services and MCP-hub roots contain design/contract material rather than complete services; OpenClaw aspirational cases skip. Existing vault/knowledge implementations are credited in their actual gateway/context rows.

Existing execution selection/limits: Roadmap scope; not counted as delivered functionality.

Related feature evidence: [EV-175](#ev-175), [EV-176](#ev-176), [EV-130](#ev-130). These references qualify scenarios only where the scenario row explicitly maps them.

| Scenario ID | Scenario / action | Expected result | Coverage | Mapped tests | Counted definitions / checks | Mapped evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="mod-048-f001-s001"></a>`MOD-048-F001-S001` | Discover standalone personal-agent/chief-of-staff capability | Only implemented supported capabilities are advertised as usable. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-048-f001-s002"></a>`MOD-048-F001-S002` | Run a personal-agent lifecycle where implemented | Identity, persistence and termination are verified through actual services. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-048-f001-s003"></a>`MOD-048-F001-S003` | Connect a generic MCP hub/event integration where implemented | Tool/event authority and routing are tested through the real boundary. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |
| <a id="mod-048-f001-s004"></a>`MOD-048-F001-S004` | Select a future channel or unimplemented service | UI/API identifies unavailable capability; roadmap placeholders cannot pass E2E. | UNMAPPED | **0** (E2E 0, local 0); existing total unknown | No exact counted definition mapped | No scenario-level E2E mapping established. Related feature evidence is not proof of this full scenario. |

## UI route register

Each route links to owning features and their scenarios above. Counts include redirects, nested preview index/layout, and fallbacks; GitLab is a provider link, not a React page. The inherited authentication/onboarding wrappers and backend permission checks still apply.

| UI route | Type | Feature IDs | Gate / source |
| --- | --- | --- | --- |
| `/login` | page | [MOD-005-F002](#mod-005-f002) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L81) |
| `/auth/callback` | page | [MOD-005-F002](#mod-005-f002) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L85) |
| `/onboarding/welcome` | page | [MOD-005-F005](#mod-005-f005) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L95) |
| `/onboarding/pending` | page | [MOD-005-F005](#mod-005-f005) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L96) |
| `/onboarding/denied` | page | [MOD-005-F005](#mod-005-f005) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L97) |
| `/domain-mri` | page | [MOD-020-F001](#mod-020-f001), [MOD-019-F001](#mod-019-f001) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L108) |
| `/` | redirect | [MOD-023-F001](#mod-023-f001) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L110) |
| `/dashboard` | redirect | [MOD-023-F001](#mod-023-f001) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L111) |
| `/admin/system` | page | [MOD-012-F001](#mod-012-f001) | system_dashboard; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L112) |
| `/org/:orgId` | page | [MOD-012-F002](#mod-012-f002), [MOD-002-F002](#mod-002-f002), [MOD-002-F006](#mod-002-f006) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L113) |
| `/org/:orgId/department/:deptId` | page | [MOD-012-F002](#mod-012-f002), [MOD-002-F003](#mod-002-f003), [MOD-010-F003](#mod-010-f003) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L114) |
| `/logs` | page | [MOD-013-F001](#mod-013-f001) | logs; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L115) |
| `/setup` | page | [MOD-024-F001](#mod-024-f001) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L116) |
| `/cli-auth` | page | [MOD-005-F003](#mod-005-f003) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L117) |
| `/agents` | page | [MOD-003-F002](#mod-003-f002) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L118) |
| `/budgets` | redirect | [MOD-010-F001](#mod-010-f001), [MOD-010-F002](#mod-010-f002), [MOD-010-F003](#mod-010-f003), [MOD-010-F004](#mod-010-f004) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L119) |
| `/model-access` | page | [MOD-009-F001](#mod-009-f001) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L120) |
| `/ratelimits` | page | [MOD-011-F001](#mod-011-f001), [MOD-011-F002](#mod-011-f002) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L121) |
| `/my-chats` | page | [MOD-016-F002](#mod-016-f002) | chat; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L122) |
| `/chat` | page | [MOD-016-F001](#mod-016-f001), [MOD-018-F002](#mod-018-f002) | chat; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L123) |
| `/settings/connections` | page | [MOD-006-F001](#mod-006-f001), [MOD-006-F002](#mod-006-f002), [MOD-005-F004](#mod-005-f004) | connections; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L124) |
| `/settings/credentials` | page | [MOD-007-F001](#mod-007-f001), [MOD-007-F002](#mod-007-f002), [MOD-009-F002](#mod-009-f002) | credentials; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L125) |
| `/settings/credentials/aws/connect` | page | [MOD-007-F002](#mod-007-f002) | credentials; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L126) |
| `/settings/agent-models` | page | [MOD-001-F001](#mod-001-f001), [MOD-001-F002](#mod-001-f002), [MOD-001-F003](#mod-001-f003), [MOD-001-F004](#mod-001-f004), [MOD-001-F007](#mod-001-f007) | agent_models; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L127) |
| `/admin/access-requests` | page | [MOD-005-F005](#mod-005-f005) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L128) |
| `/admin/indexing` | page | [MOD-017-F003](#mod-017-f003) | indexing; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L129) |
| `/admin/tenant-links` | page | [MOD-002-F010](#mod-002-f010) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L130) |
| `/admin/organizations` | page | [MOD-002-F001](#mod-002-f001), [MOD-002-F002](#mod-002-f002), [MOD-002-F003](#mod-002-f003), [MOD-002-F004](#mod-002-f004), [MOD-002-F005](#mod-002-f005), [MOD-002-F006](#mod-002-f006), [MOD-002-F007](#mod-002-f007), [MOD-002-F008](#mod-002-f008), [MOD-002-F009](#mod-002-f009), [MOD-003-F001](#mod-003-f001), [MOD-004-F001](#mod-004-f001) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L135) |
| `/activity` | page | [MOD-015-F001](#mod-015-f001), [MOD-015-F002](#mod-015-f002), [MOD-019-F001](#mod-019-f001) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L136) |
| `/runs` | page | [MOD-015-F001](#mod-015-f001) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L137) |
| `/budget` | page | [MOD-010-F001](#mod-010-f001), [MOD-010-F002](#mod-010-f002), [MOD-010-F003](#mod-010-f003), [MOD-010-F004](#mod-010-f004) | budget_spend for members; admin manage access uses permissions; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L142) |
| `/knowledge` | page | [MOD-017-F001](#mod-017-f001), [MOD-017-F002](#mod-017-f002) | knowledge; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L143) |
| `/flows` | page | [MOD-018-F001](#mod-018-f001), [MOD-018-F003](#mod-018-f003) | orchestration_engine; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L151) |
| `/flows/:flowId` | page | [MOD-018-F001](#mod-018-f001), [MOD-018-F003](#mod-018-f003), [MOD-018-F004](#mod-018-f004), [MOD-018-F005](#mod-018-f005), [MOD-018-F006](#mod-018-f006) | orchestration_engine; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L152) |
| `/superplane` | page | [MOD-026-F001](#mod-026-f001), [MOD-026-F002](#mod-026-f002), [MOD-026-F003](#mod-026-f003), [MOD-026-F004](#mod-026-f004), [MOD-026-F005](#mod-026-f005), [MOD-026-F006](#mod-026-f006) | superplane; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L159) |
| `/next` | layout + index page | [MOD-023-F001](#mod-023-f001), [MOD-005-F004](#mod-005-f004) | new_ui; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L190) |
| `/next/admin` | page | [MOD-023-F001](#mod-023-f001) | new_ui; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L206) |
| `/next/*` | fallback | [MOD-023-F001](#mod-023-f001) | new_ui; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L211) |
| `*` | fallback | [MOD-023-F001](#mod-023-f001) | See inherited auth/onboarding and page permission checks; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/App.tsx#L216) |
| `/gitlab/` | external/provider link; not an App.tsx route | [MOD-008-F001](#mod-008-f001) | gitlab; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/components/next/journeys.ts#L305) |

## Frontend feature flags

Client fallback values are not observed deployed server values. Embedded controls may use different guards from their parent route.

| Flag | Client fallback | Feature IDs |
| --- | --- | --- |
| `chat` | `true` | [MOD-016-F001](#mod-016-f001), [MOD-016-F002](#mod-016-f002) |
| `knowledge` | `true` | [MOD-017-F001](#mod-017-f001), [MOD-017-F002](#mod-017-f002) |
| `indexing` | `true` | [MOD-017-F003](#mod-017-f003) |
| `tenant_org_links` | `false` | [MOD-002-F010](#mod-002-f010) |
| `connections` | `true` | [MOD-006-F001](#mod-006-f001), [MOD-006-F002](#mod-006-f002) |
| `credentials` | `true` | [MOD-007-F001](#mod-007-f001), [MOD-007-F002](#mod-007-f002), [MOD-009-F002](#mod-009-f002) |
| `system_dashboard` | `true` | [MOD-012-F001](#mod-012-f001) |
| `logs` | `true` | [MOD-013-F001](#mod-013-f001) |
| `gitlab` | `false` | [MOD-008-F001](#mod-008-f001) |
| `orchestration_engine` | `false` | [MOD-018-F001](#mod-018-f001), [MOD-018-F002](#mod-018-f002), [MOD-018-F003](#mod-018-f003), [MOD-018-F004](#mod-018-f004), [MOD-018-F005](#mod-018-f005), [MOD-018-F006](#mod-018-f006) |
| `budget_spend` | `false` | [MOD-010-F001](#mod-010-f001) |
| `agent_control` | `false` | [MOD-015-F002](#mod-015-f002) |
| `agent_explanations` | `false` | [MOD-015-F002](#mod-015-f002) |
| `new_ui` | `false` | [MOD-023-F001](#mod-023-f001) |
| `superplane` | `false` | [MOD-026-F001](#mod-026-f001), [MOD-026-F002](#mod-026-f002), [MOD-026-F003](#mod-026-f003), [MOD-026-F004](#mod-026-f004), [MOD-026-F005](#mod-026-f005), [MOD-026-F006](#mod-026-f006), [MOD-026-F007](#mod-026-f007), [MOD-026-F008](#mod-026-f008), [MOD-026-F009](#mod-026-f009), [MOD-026-F010](#mod-026-f010) |
| `agent_models` | `false` | [MOD-001-F001](#mod-001-f001), [MOD-001-F002](#mod-001-f002), [MOD-001-F003](#mod-001-f003), [MOD-001-F004](#mod-001-f004), [MOD-001-F005](#mod-001-f005), [MOD-001-F006](#mod-001-f006), [MOD-001-F007](#mod-001-f007) |

## HTTP API register

647 statically reachable FastAPI method/path registrations: 524 Gateway, 114 Superplane, 4 Agent Context, 5 private SkyPilot sidecar. Imports/runtime feature gates can make routes unavailable on a deployment. Paths are router paths; external reverse-proxy prefixes are not added. Includes and the controller's direct route-append loop were followed. Per-route feature assignment does not prove per-endpoint test coverage; use the scenario evidence above. Broader provider/MCP/SDK protocol operations are not enumerated as FastAPI routes.

### gateway

| API ID | Method / path | Owning feature IDs | Handler / source | Declared dependency and permission hints |
| --- | --- | --- | --- | --- |
| `API-0001` | `GET /health` | [MOD-025-F001](#mod-025-f001) | [health](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/app.py#L460) | See handler and middleware |
| `API-0002` | `GET /ready` | [MOD-025-F001](#mod-025-f001) | [ready](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/app.py#L464) | See handler and middleware |
| `API-0003` | `GET /admin/indexing/runs` | [MOD-017-F003](#mod-017-f003) | [list_indexing_runs](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/agent_context/api/indexing_router.py#L52) | [Depends(require_admin)]; get_indexing_db |
| `API-0004` | `GET /admin/indexing/runs/{run_id}` | [MOD-017-F003](#mod-017-f003) | [get_indexing_run_detail](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/agent_context/api/indexing_router.py#L130) | [Depends(require_admin)]; get_indexing_db |
| `API-0005` | `GET /superplane/installation-support` | [MOD-026-F001](#mod-026-f001) | [installation_support](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/domain_proxy/superplane.py#L158) | See handler and middleware |
| `API-0006` | `POST /superplane/v1/accounts` | [MOD-026-F001](#mod-026-f001) | [register_account](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/domain_proxy/superplane.py#L224) | get_current_user_context; get_db; get_secrets_manager |
| `API-0007` | `GET /superplane/v1/{path:path}` | [MOD-026-F001](#mod-026-f001) | [forward](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/domain_proxy/superplane.py#L336) | See handler and middleware |
| `API-0008` | `POST /superplane/v1/{path:path}` | [MOD-026-F001](#mod-026-f001) | [forward](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/domain_proxy/superplane.py#L336) | See handler and middleware |
| `API-0009` | `PUT /superplane/v1/{path:path}` | [MOD-026-F001](#mod-026-f001) | [forward](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/domain_proxy/superplane.py#L336) | See handler and middleware |
| `API-0010` | `PATCH /superplane/v1/{path:path}` | [MOD-026-F001](#mod-026-f001) | [forward](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/domain_proxy/superplane.py#L336) | See handler and middleware |
| `API-0011` | `DELETE /superplane/v1/{path:path}` | [MOD-026-F001](#mod-026-f001) | [forward](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/domain_proxy/superplane.py#L336) | See handler and middleware |
| `API-0012` | `HEAD /superplane/v1/{path:path}` | [MOD-026-F001](#mod-026-f001) | [forward](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/domain_proxy/superplane.py#L336) | See handler and middleware |
| `API-0013` | `OPTIONS /superplane/v1/{path:path}` | [MOD-026-F001](#mod-026-f001) | [forward](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/domain_proxy/superplane.py#L336) | See handler and middleware |
| `API-0014` | `POST /auth/exchange` | [MOD-005-F002](#mod-005-f002) | [exchange_credentials](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/routes.py#L57) | get_db |
| `API-0015` | `GET /auth/me` | [MOD-005-F002](#mod-005-f002) | [get_current_user](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/routes.py#L153) | get_current_user_context |
| `API-0016` | `GET /auth/login-options` | [MOD-005-F002](#mod-005-f002) | [login_options](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/routes.py#L178) | See handler and middleware |
| `API-0017` | `POST /auth/logout` | [MOD-005-F002](#mod-005-f002) | [logout](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/routes.py#L250) | get_current_user_context; get_db |
| `API-0018` | `POST /auth/revoke` | [MOD-005-F002](#mod-005-f002), [MOD-005-F006](#mod-005-f006) | [revoke_token](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/routes.py#L274) | get_current_user_context; get_db |
| `API-0019` | `POST /auth/service-accounts` | [MOD-005-F006](#mod-005-f006) | [create_service_account](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/routes.py#L304) | get_current_user_context; get_db |
| `API-0020` | `GET /auth/service-accounts` | [MOD-005-F006](#mod-005-f006) | [list_service_accounts](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/routes.py#L346) | get_current_user_context; get_db |
| `API-0021` | `GET /auth/service-accounts/{service_account_id}` | [MOD-005-F006](#mod-005-f006) | [get_service_account](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/routes.py#L373) | get_current_user_context; get_db |
| `API-0022` | `PUT /auth/service-accounts/{service_account_id}` | [MOD-005-F006](#mod-005-f006) | [update_service_account](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/routes.py#L397) | get_current_user_context; get_db |
| `API-0023` | `DELETE /auth/service-accounts/{service_account_id}` | [MOD-005-F006](#mod-005-f006) | [delete_service_account](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/routes.py#L431) | get_current_user_context; get_db |
| `API-0024` | `POST /auth/admin/cleanup-tokens` | [MOD-005-F006](#mod-005-f006) | [cleanup_expired_tokens](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/routes.py#L471) | get_current_user_context; get_db |
| `API-0025` | `POST /auth/admin/revoke-user-tokens/{user_id}` | [MOD-005-F002](#mod-005-f002), [MOD-005-F006](#mod-005-f006) | [revoke_all_user_tokens](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/routes.py#L494) | get_current_user_context; get_db |
| `API-0026` | `GET /auth/workspaces` | [MOD-005-F004](#mod-005-f004) | [list_workspaces](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/workspaces.py#L141) | get_current_user; get_db |
| `API-0027` | `POST /auth/workspaces/select` | [MOD-005-F004](#mod-005-f004) | [switch_workspace](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/workspaces.py#L256) | get_current_user; get_db; get_workspace_claims |
| `API-0028` | `POST /auth/workspaces/context` | [MOD-005-F004](#mod-005-f004) | [tenant_context_exchange](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/workspaces.py#L272) | get_current_user; get_db |
| `API-0029` | `GET /.well-known/cognito-config` | [MOD-005-F002](#mod-005-f002) | [cognito_config](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/routes.py#L203) | See handler and middleware |
| `API-0030` | `GET /workspaces` | [MOD-005-F004](#mod-005-f004) | [list_workspaces](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/workspaces.py#L141) | get_current_user; get_db |
| `API-0031` | `POST /workspaces/select` | [MOD-005-F004](#mod-005-f004) | [switch_workspace](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/workspaces.py#L256) | get_current_user; get_db; get_workspace_claims |
| `API-0032` | `POST /workspaces/context` | [MOD-005-F004](#mod-005-f004) | [tenant_context_exchange](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/workspaces.py#L272) | get_current_user; get_db |
| `API-0033` | `GET /auth/admin/revoke-user-tokens/{user_id}/review` | [MOD-005-F006](#mod-005-f006) | [review_user_sessions](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/session_admin.py#L67) | get_current_user; get_db |
| `API-0034` | `POST /auth/admin/revoke-user-tokens/{user_id}/revision` | [MOD-005-F006](#mod-005-f006) | [revoke_reviewed_sessions](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/session_admin.py#L72) | get_current_user; get_db |
| `API-0035` | `POST /auth/cli/start` | [MOD-005-F003](#mod-005-f003) | [start_cli_login](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/cli_login.py#L377) | get_db |
| `API-0036` | `POST /auth/cli/approve` | [MOD-005-F003](#mod-005-f003) | [approve_cli_login](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/cli_login.py#L410) | _get_cognito_claims; get_db |
| `API-0037` | `POST /auth/cli/token` | [MOD-005-F003](#mod-005-f003) | [redeem_cli_login](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/cli_login.py#L457) | get_db; get_token_minter |
| `API-0038` | `POST /auth/cli/refresh` | [MOD-005-F003](#mod-005-f003) | [refresh_cli_login](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/cli_login.py#L516) | get_token_minter |
| `API-0039` | `POST /auth/cli/password` | [MOD-005-F003](#mod-005-f003) | [password_login](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/cli_native_login.py#L195) | get_db; get_native_client |
| `API-0040` | `POST /auth/cli/challenge` | [MOD-005-F003](#mod-005-f003) | [challenge_login](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/cli_native_login.py#L214) | get_db; get_native_client |
| `API-0041` | `GET /auth/cli/admin-session` | [MOD-005-F003](#mod-005-f003) | [admin_session](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/cli_native_login.py#L270) | require_admin; admin |
| `API-0042` | `GET /auth/credentials` | [MOD-007-F001](#mod-007-f001) | [list_credentials_endpoint](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/vault_routes.py#L164) | get_current_user_context; get_db |
| `API-0043` | `POST /auth/credentials` | [MOD-007-F001](#mod-007-f001) | [create_credential_endpoint](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/vault_routes.py#L187) | get_current_user_context; get_db; get_secrets_manager |
| `API-0044` | `PUT /auth/credentials/{credential_id}` | [MOD-007-F001](#mod-007-f001) | [put_credential_endpoint](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/vault_routes.py#L226) | get_current_user_context; get_db; get_secrets_manager |
| `API-0045` | `PATCH /auth/credentials/{credential_id}` | [MOD-007-F001](#mod-007-f001) | [update_credential_endpoint](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/vault_routes.py#L267) | get_current_user_context; get_db |
| `API-0046` | `PATCH /auth/credentials/{credential_id}/metadata` | [MOD-007-F001](#mod-007-f001) | [update_credential_metadata_endpoint](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/vault_routes.py#L303) | get_current_user_context; get_db |
| `API-0047` | `DELETE /auth/credentials/{credential_id}` | [MOD-007-F001](#mod-007-f001) | [delete_credential_endpoint](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/vault_routes.py#L314) | get_current_user_context; get_db; get_secrets_manager |
| `API-0048` | `GET /auth/identities` | [MOD-007-F001](#mod-007-f001) | [list_identities_endpoint](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/vault_routes.py#L350) | get_current_user_context; get_db |
| `API-0049` | `DELETE /auth/identities/{identity_id}` | [MOD-007-F001](#mod-007-f001) | [unlink_identity_endpoint](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/vault_routes.py#L365) | get_current_user_context; get_db |
| `API-0050` | `POST /auth/identities/{provider}/link` | [MOD-007-F001](#mod-007-f001) | [issue_identity_magic_link](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/vault_routes.py#L500) | get_current_user_context; get_db |
| `API-0051` | `GET /auth/link/magic` | [MOD-007-F001](#mod-007-f001) | [magic_link_landing_get](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/vault_routes.py#L654) | get_current_user_context; get_db |
| `API-0052` | `POST /auth/link/magic` | [MOD-007-F001](#mod-007-f001) | [magic_link_landing_post](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/vault_routes.py#L736) | get_current_user_context; get_db |
| `API-0053` | `PUT /auth/credentials/{credential_id}/workspaces/{workspace_id}` | [MOD-007-F003](#mod-007-f003) | [delegate_workspace](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/vault_authority_routes.py#L28) | get_current_user_context; get_db |
| `API-0054` | `DELETE /auth/credentials/{credential_id}/workspaces/{workspace_id}` | [MOD-007-F003](#mod-007-f003) | [withdraw_workspace](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/vault_authority_routes.py#L39) | get_current_user_context; get_db |
| `API-0055` | `POST /auth/credentials/{credential_id}/workspaces/{workspace_id}/validation` | [MOD-007-F003](#mod-007-f003) | [validate_workspace](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/vault_authority_routes.py#L50) | get_current_user_context; get_db; get_secrets_manager; get_settings |
| `API-0056` | `POST /auth/credentials/aws/connect` | [MOD-007-F002](#mod-007-f002) | [connect_start](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/aws_connect_routes.py#L236) | get_current_user_context; get_db; get_secrets_manager |
| `API-0057` | `POST /auth/credentials/aws/verify` | [MOD-007-F002](#mod-007-f002) | [connect_verify](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/aws_connect_routes.py#L328) | get_current_user_context; get_db; get_secrets_manager |
| `API-0058` | `POST /auth/credentials/aws/import` | [MOD-007-F002](#mod-007-f002) | [connect_import](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/aws_connect_routes.py#L501) | get_current_user_context; get_db; get_secrets_manager |
| `API-0059` | `GET /auth/credentials/aws/{credential_id}/setup` | [MOD-007-F002](#mod-007-f002) | [connect_setup](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/aws_connect_routes.py#L623) | get_current_user_context; get_db; get_secrets_manager |
| `API-0060` | `POST /internal/v1/issue-magic-link` | [MOD-021-F004](#mod-021-f004) | [issue_magic_link](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/routes.py#L244) | get_db; verify_internal_or_irsa |
| `API-0061` | `POST /internal/v1/resolve-user` | [MOD-021-F004](#mod-021-f004) | [resolve_user](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/routes.py#L347) | get_db; verify_internal_or_irsa |
| `API-0062` | `POST /internal/v1/resolve-installation` | [MOD-021-F004](#mod-021-f004) | [resolve_installation](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/routes.py#L601) | get_db; verify_internal_or_irsa |
| `API-0063` | `POST /internal/v1/github-installation-token` | [MOD-021-F004](#mod-021-f004) | [github_installation_token](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/routes.py#L832) | get_db; verify_internal_or_irsa |
| `API-0064` | `GET /internal/v1/user-credentials` | [MOD-007-F003](#mod-007-f003) | [list_user_credentials](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/credential_routes.py#L372) | get_db; verify_internal_or_irsa |
| `API-0065` | `POST /internal/v1/proxy-request` | [MOD-007-F003](#mod-007-f003) | [proxy_request](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/credential_routes.py#L440) | get_db; get_secrets_manager; verify_internal_or_irsa |
| `API-0066` | `POST /internal/v1/credential-materialize` | [MOD-007-F003](#mod-007-f003) | [credential_materialize](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/credential_routes.py#L628) | get_db; get_secrets_manager; verify_internal_or_irsa |
| `API-0067` | `POST /internal/v1/credential-raw-read` | [MOD-007-F003](#mod-007-f003) | [credential_raw_read](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/credential_routes.py#L768) | get_db; get_secrets_manager; verify_internal_or_irsa |
| `API-0068` | `POST /internal/v1/credential-evidence` | [MOD-007-F003](#mod-007-f003) | [credential_evidence](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/vault_evidence_routes.py#L329) | get_db; get_secrets_manager; verify_internal_or_irsa |
| `API-0069` | `POST /internal/v1/credential-revocation-state` | [MOD-007-F003](#mod-007-f003) | [credential_revocation_state](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/vault_evidence_routes.py#L380) | get_db; verify_internal_or_irsa |
| `API-0070` | `POST /internal/v1/credential-delivery` | [MOD-007-F003](#mod-007-f003) | [credential_delivery](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/vault_evidence_routes.py#L415) | get_db; get_secrets_manager; verify_internal_or_irsa |
| `API-0071` | `POST /internal/v1/credential-delivery/preflight` | [MOD-007-F003](#mod-007-f003) | [credential_delivery_preflight](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/vault_evidence_routes.py#L439) | get_db; get_secrets_manager; verify_internal_or_irsa |
| `API-0072` | `POST /internal/v1/controller-execution/authority` | [MOD-021-F004](#mod-021-f004) | [execution_authority](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/controller_execution_routes.py#L38) | get_operation_db; verify_internal_or_irsa |
| `API-0073` | `POST /internal/v1/controller-execution/producer-readiness` | [MOD-021-F004](#mod-021-f004) | [producer_readiness](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/domain_operation_routes.py#L139) | [Depends(verify_internal_or_irsa)] |
| `API-0074` | `POST /internal/v1/controller-execution/dispatch` | [MOD-021-F004](#mod-021-f004) | [publish](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/domain_operation_routes.py#L162) | [Depends(verify_internal_or_irsa)] |
| `API-0075` | `POST /internal/v1/controller-execution/verify-run` | [MOD-021-F004](#mod-021-f004) | [verify_run](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/domain_operation_routes.py#L170) | [Depends(verify_internal_or_irsa)] |
| `API-0076` | `POST /internal/v1/controller-execution/task/acquire` | [MOD-021-F004](#mod-021-f004) | [acquire_task](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/domain_operation_routes.py#L230) | [Depends(verify_internal_or_irsa)] |
| `API-0077` | `POST /internal/v1/controller-execution/bootstrap` | [MOD-021-F004](#mod-021-f004) | [bootstrap](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/domain_operation_routes.py#L242) | [Depends(verify_internal_or_irsa)] |
| `API-0078` | `POST /internal/v1/controller-execution/task/heartbeat` | [MOD-021-F004](#mod-021-f004) | [heartbeat_task](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/domain_operation_routes.py#L267) | [Depends(verify_internal_or_irsa)] |
| `API-0079` | `POST /internal/v1/controller-execution/renew` | [MOD-021-F004](#mod-021-f004) | [renew_run](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/domain_operation_routes.py#L275) | [Depends(verify_internal_or_irsa)] |
| `API-0080` | `POST /internal/v1/controller-execution/task/ack` | [MOD-021-F004](#mod-021-f004) | [acknowledge_task](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/domain_operation_routes.py#L291) | [Depends(verify_internal_or_irsa)] |
| `API-0081` | `POST /internal/v1/controller-execution/lease` | [MOD-021-F004](#mod-021-f004) | [acquire_lease](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/domain_operation_routes.py#L330) | [Depends(verify_internal_or_irsa)] |
| `API-0082` | `POST /internal/v1/controller-execution/task/status` | [MOD-021-F004](#mod-021-f004) | [task_status](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/domain_operation_routes.py#L363) | [Depends(verify_internal_or_irsa)] |
| `API-0083` | `POST /internal/v1/controller-execution/recovery/scope` | [MOD-021-F004](#mod-021-f004) | [recovery_scope](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/domain_operation_routes.py#L369) | [Depends(verify_internal_or_irsa)] |
| `API-0084` | `POST /internal/v1/controller-execution/recovery/authority` | [MOD-021-F004](#mod-021-f004) | [recovery_authority](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/domain_operation_routes.py#L387) | [Depends(verify_internal_or_irsa)] |
| `API-0085` | `POST /internal/v1/controller-execution/recovery/observe` | [MOD-021-F004](#mod-021-f004) | [observe](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/domain_operation_routes.py#L413) | [Depends(verify_internal_or_irsa)] |
| `API-0086` | `POST /internal/v1/controller-execution/recovery/inventory` | [MOD-021-F004](#mod-021-f004) | [inventory](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/domain_operation_routes.py#L418) | [Depends(verify_internal_or_irsa)] |
| `API-0087` | `POST /internal/v1/controller-execution/recovery/lifecycle` | [MOD-021-F004](#mod-021-f004) | [lifecycle](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/domain_operation_routes.py#L423) | [Depends(verify_internal_or_irsa)] |
| `API-0088` | `POST /internal/v1/controller-execution/recovery/account-creation` | [MOD-021-F004](#mod-021-f004) | [account_creation](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/domain_operation_routes.py#L428) | [Depends(verify_internal_or_irsa)] |
| `API-0089` | `POST /internal/v1/controller-execution/recovery/bootstrap` | [MOD-021-F004](#mod-021-f004) | [bootstrap_recovery](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/domain_operation_routes.py#L433) | [Depends(verify_internal_or_irsa)] |
| `API-0090` | `POST /internal/v1/controller-execution/recovery/settlement` | [MOD-021-F004](#mod-021-f004) | [settlement](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/domain_operation_routes.py#L438) | [Depends(verify_internal_or_irsa)] |
| `API-0091` | `POST /internal/v1/credential-assume-role` | [MOD-007-F003](#mod-007-f003) | [credential_assume_role](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/assume_role_routes.py#L151) | get_db; get_secrets_manager; verify_internal_or_irsa |
| `API-0092` | `POST /internal/v1/worker-task-credentials` | [MOD-007-F003](#mod-007-f003) | [worker_task_credentials](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/task_credentials.py#L162) | get_db; verify_internal_or_irsa |
| `API-0093` | `POST /internal/v1/provenance` | [MOD-021-F004](#mod-021-f004) | [create_provenance](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/provenance_routes.py#L236) | get_db; verify_internal_or_irsa |
| `API-0094` | `POST /internal/v1/knowledge-assets/status-callback` | [MOD-017-F003](#mod-017-f003) | [status_callback](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/status_callback_routes.py#L93) | get_agent_context_db |
| `API-0095` | `GET /internal/v1/admin/tenant-config/{tenant}` | [MOD-021-F004](#mod-021-f004) | [get_tenant_config](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/admin_routes.py#L29) | verify_internal_or_irsa |
| `API-0096` | `GET /internal/v1/admin/audit-entries` | [MOD-021-F004](#mod-021-f004) | [get_audit_entries](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/admin_routes.py#L53) | get_db; verify_internal_or_irsa |
| `API-0097` | `POST /internal/v1/persona-model-probes/claim` | [MOD-001-F006](#mod-001-f006) | [claim](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/persona_model_probe_routes.py#L132) | get_db; verify_model_probe_irsa |
| `API-0098` | `POST /internal/v1/persona-model-probes/{slot_id}/start` | [MOD-001-F006](#mod-001-f006) | [start](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/persona_model_probe_routes.py#L153) | get_db; verify_model_probe_irsa |
| `API-0099` | `POST /internal/v1/persona-model-probes/{slot_id}/complete` | [MOD-001-F006](#mod-001-f006) | [complete](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/persona_model_probe_routes.py#L177) | get_db; verify_model_probe_irsa |
| `API-0100` | `POST /internal/v1/agent/persona-model/resolve` | [MOD-001-F006](#mod-001-f006) | [resolve_dispatch_selection](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/internal/persona_model_selection.py#L26) | get_db |
| `API-0101` | `POST /internal/v1/agent/bootstrap` | [MOD-021-F001](#mod-021-f001) | [bootstrap](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/routes.py#L427) | [Depends(require_agent_transport)]; get_agent_runtime; get_db |
| `API-0102` | `POST /internal/v1/agent/model-decision` | [MOD-021-F001](#mod-021-f001) | [model_decision](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/routes.py#L589) | [Depends(require_agent_transport)]; get_agent_runtime; get_db |
| `API-0103` | `GET /internal/v1/agent/status` | [MOD-021-F001](#mod-021-f001) | [status](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/routes.py#L624) | [Depends(require_agent_transport)]; get_agent_runtime |
| `API-0104` | `POST /internal/v1/agent/dispatch` | [MOD-021-F001](#mod-021-f001) | [dispatch](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/routes.py#L631) | [Depends(require_agent_transport)]; get_agent_runtime |
| `API-0105` | `POST /internal/v1/agent/waves` | [MOD-021-F001](#mod-021-f001) | [bind_wave](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/routes.py#L638) | [Depends(require_agent_transport)]; get_agent_runtime |
| `API-0106` | `POST /internal/v1/agent/revalidate` | [MOD-021-F001](#mod-021-f001) | [revalidate](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/routes.py#L643) | [Depends(require_agent_transport)]; get_agent_runtime |
| `API-0107` | `POST /internal/v1/agent/control/{run_id}/{action}` | [MOD-021-F001](#mod-021-f001) | [control](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/routes.py#L648) | [Depends(require_agent_transport)]; get_agent_runtime |
| `API-0108` | `POST /internal/v1/agent/codex-persona-operation` | [MOD-021-F002](#mod-021-f002) | [codex_persona_operation](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/codex_github_session.py#L250) | [Depends(require_agent_transport)]; get_agent_runtime |
| `API-0109` | `POST /internal/v1/agent/codex-persona-session` | [MOD-021-F002](#mod-021-f002) | [codex_persona_session](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/codex_github_session.py#L263) | [Depends(require_agent_transport)]; get_agent_runtime; get_db |
| `API-0110` | `POST /internal/v1/agent/arc/model-decision` | [MOD-021-F001](#mod-021-f001) | [arc_model_decision](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/arc_model.py#L143) | get_db; root_store |
| `API-0111` | `GET /internal/v1/agent/model-policy-keys` | [MOD-021-F001](#mod-021-f001) | [model_policy_keys](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/model_policy_keys.py#L15) | See handler and middleware |
| `API-0112` | `POST /internal/v1/agent/legacy-chat-preflight` | [MOD-021-F001](#mod-021-f001) | [legacy_chat_preflight](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/model_policy_keys.py#L27) | get_db |
| `API-0113` | `POST /internal/v1/agent/roots/admit` | [MOD-021-F001](#mod-021-f001) | [admit_root](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/external_roots.py#L159) | get_db; root_store |
| `API-0114` | `POST /internal/v1/agent/chat/model-decision` | [MOD-021-F001](#mod-021-f001) | [chat_model_decision](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/chat_model.py#L35) | chat_runtime; get_db |
| `API-0115` | `POST /v1/chat/data/history/read` | [MOD-016-F003](#mod-016-f003) | [read_history](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/chat_data_routes.py#L227) | enabled; runtime |
| `API-0116` | `POST /v1/chat/data/history/messages` | [MOD-016-F003](#mod-016-f003) | [read_messages](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/chat_data_routes.py#L236) | enabled; runtime |
| `API-0117` | `POST /v1/chat/data/history/summary` | [MOD-016-F003](#mod-016-f003) | [read_summary](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/chat_data_routes.py#L245) | enabled; runtime |
| `API-0118` | `POST /v1/chat/data/history/turn` | [MOD-016-F003](#mod-016-f003) | [accepted_user_turn](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/chat_data_routes.py#L254) | enabled; runtime |
| `API-0119` | `POST /v1/chat/data/history/append` | [MOD-016-F003](#mod-016-f003) | [append_history](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/chat_data_routes.py#L263) | enabled; runtime |
| `API-0120` | `POST /v1/chat/data/history/summary/append` | [MOD-016-F003](#mod-016-f003) | [append_summary](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/chat_data_routes.py#L273) | enabled; runtime |
| `API-0121` | `POST /v1/chat/data/history/compact` | [MOD-016-F003](#mod-016-f003) | [compact_history](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/chat_data_routes.py#L283) | enabled; runtime |
| `API-0122` | `POST /v1/chat/data/memory/read` | [MOD-016-F003](#mod-016-f003) | [read_memory](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/chat_data_routes.py#L293) | enabled; memory_table; runtime |
| `API-0123` | `POST /v1/chat/data/memory/search` | [MOD-016-F003](#mod-016-f003) | [search_memory](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/chat_data_routes.py#L302) | enabled; memory_table; runtime |
| `API-0124` | `POST /v1/chat/data/memory/write` | [MOD-016-F003](#mod-016-f003) | [write_memory](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/chat_data_routes.py#L312) | enabled; memory_table; runtime |
| `API-0125` | `POST /v1/chat/data/draft/read` | [MOD-016-F003](#mod-016-f003) | [read_draft](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/chat_data_routes.py#L322) | enabled; runtime |
| `API-0126` | `POST /v1/chat/data/draft/write` | [MOD-016-F003](#mod-016-f003) | [write_draft](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/chat_data_routes.py#L331) | enabled; runtime |
| `API-0127` | `POST /v1/chat/data/session/acl/read` | [MOD-016-F003](#mod-016-f003) | [read_session_acl](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/chat_data_routes.py#L341) | enabled; runtime |
| `API-0128` | `POST /v1/chat/data/session/acl/write` | [MOD-016-F003](#mod-016-f003) | [write_session_acl](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/chat_data_routes.py#L350) | enabled; runtime |
| `API-0129` | `POST /v1/chat/data/artifact/list` | [MOD-016-F003](#mod-016-f003) | [list_artifacts](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/chat_data_routes.py#L360) | artifact_storage; enabled; runtime |
| `API-0130` | `POST /v1/chat/data/artifact/create` | [MOD-016-F003](#mod-016-f003) | [create_artifact](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/chat_data_routes.py#L369) | artifact_storage; enabled; runtime |
| `API-0131` | `GET /v1/chat/data/artifact/{session_id}/{artifact_id}` | [MOD-016-F003](#mod-016-f003) | [download_artifact](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/chat_data_routes.py#L397) | artifact_storage; enabled; runtime |
| `API-0132` | `POST /internal/v1/agent/chat/data/admit` | [MOD-016-F003](#mod-016-f003) | [admit_chat](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/chat_data_routes.py#L423) | enabled; get_db; runtime |
| `API-0133` | `POST /v1/chat/data/bootstrap` | [MOD-016-F003](#mod-016-f003) | [exchange_chat](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/chat_data_routes.py#L454) | enabled; runtime |
| `API-0134` | `POST /internal/v1/agent/work/admit` | [MOD-019-F002](#mod-019-f002) | [admit_work](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/work_routes.py#L71) | get_agent_runtime |
| `API-0135` | `POST /internal/v1/tasks/admit` | [MOD-019-F002](#mod-019-f002) | [admit](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/task_admission_routes.py#L65) | get_db |
| `API-0136` | `POST /internal/v1/agent/self/status` | [MOD-021-F002](#mod-021-f002) | [record_status](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/registration_routes.py#L321) | [Depends(require_agent_transport)]; get_registration_runtime |
| `API-0137` | `POST /internal/v1/agent/self/handoff` | [MOD-021-F002](#mod-021-f002) | [commit_handoff_route](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/registration_routes.py#L375) | [Depends(require_agent_transport)]; get_registration_runtime |
| `API-0138` | `POST /internal/v1/agent/self/control/registration` | [MOD-021-F002](#mod-021-f002) | [register_control](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/registration_routes.py#L505) | [Depends(require_agent_transport)]; get_registration_runtime |
| `API-0139` | `POST /internal/v1/agent/self/control/registration/clear` | [MOD-021-F002](#mod-021-f002) | [clear_control](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/registration_routes.py#L514) | [Depends(require_agent_transport)]; get_registration_runtime |
| `API-0140` | `POST /internal/v1/agent/self/control/registration/renew` | [MOD-021-F002](#mod-021-f002) | [renew_control](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/registration_routes.py#L523) | [Depends(require_agent_transport)]; get_registration_runtime |
| `API-0141` | `POST /internal/v1/agent/self/control/registration/state` | [MOD-021-F002](#mod-021-f002) | [control_registration_state](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/registration_routes.py#L532) | [Depends(require_agent_transport)]; get_registration_runtime |
| `API-0142` | `POST /internal/v1/agent/self/marker` | [MOD-021-F003](#mod-021-f003) | [own_marker](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/run_services.py#L122) | [Depends(require_agent_transport)]; get_agent_runtime |
| `API-0143` | `GET /internal/v1/agent/self/knowledge/{path:path}` | [MOD-021-F003](#mod-021-f003) | [own_knowledge](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/knowledge_service.py#L146) | [Depends(require_agent_transport)]; get_agent_runtime |
| `API-0144` | `POST /internal/v1/agent/self/knowledge/{path:path}` | [MOD-021-F003](#mod-021-f003) | [own_knowledge](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/knowledge_service.py#L146) | [Depends(require_agent_transport)]; get_agent_runtime |
| `API-0145` | `POST /internal/v1/agent/task/acquire` | [MOD-019-F002](#mod-019-f002) | [acquire](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/task_routes.py#L78) | [Depends(require_agent_transport)]; get_agent_runtime; task_delivery |
| `API-0146` | `POST /internal/v1/agent/task/heartbeat` | [MOD-019-F002](#mod-019-f002) | [heartbeat](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/task_routes.py#L85) | [Depends(require_agent_transport)]; get_agent_runtime; task_delivery |
| `API-0147` | `POST /internal/v1/agent/task/ack` | [MOD-019-F002](#mod-019-f002) | [acknowledge](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/task_routes.py#L92) | [Depends(require_agent_transport)]; get_agent_runtime; task_delivery |
| `API-0148` | `POST /internal/v1/tasks/dispatch/claim` | [MOD-019-F002](#mod-019-f002) | [dispatch_claim](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/task_dispatch_routes.py#L119) | [Depends(require_adapters_enabled)]; get_agent_runtime; work_store |
| `API-0149` | `POST /internal/v1/tasks/dispatch/settle` | [MOD-019-F002](#mod-019-f002) | [dispatch_settle](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/task_dispatch_routes.py#L149) | [Depends(require_adapters_enabled)]; get_agent_runtime; work_store |
| `API-0150` | `POST /internal/v1/tasks/recovery/claim` | [MOD-019-F002](#mod-019-f002) | [recovery_claim](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/task_dispatch_routes.py#L186) | [Depends(require_adapters_enabled)]; get_agent_runtime; work_store |
| `API-0151` | `POST /internal/v1/tasks/recovery/settle` | [MOD-019-F002](#mod-019-f002) | [recovery_settle](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/task_dispatch_routes.py#L236) | [Depends(require_adapters_enabled)]; get_agent_runtime; work_store |
| `API-0152` | `POST /internal/v1/agent/task/bootstrap` | [MOD-019-F002](#mod-019-f002) | [bootstrap](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/task_runtime_routes.py#L106) | [Depends(require_agent_transport)]; get_agent_runtime |
| `API-0153` | `POST /internal/v1/agent/task/attempt` | [MOD-019-F002](#mod-019-f002) | [attempt](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/task_runtime_routes.py#L120) | [Depends(require_agent_transport)]; get_agent_runtime |
| `API-0154` | `POST /internal/v1/agent/task/turn` | [MOD-019-F002](#mod-019-f002) | [turn](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/task_runtime_routes.py#L176) | [Depends(require_agent_transport)]; get_agent_runtime |
| `API-0155` | `POST /internal/v1/agent/task/model` | [MOD-019-F002](#mod-019-f002) | [model](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/task_runtime_routes.py#L354) | [Depends(require_agent_transport)]; get_agent_runtime; get_db |
| `API-0156` | `POST /internal/v1/agent/task/tool-authorize` | [MOD-019-F002](#mod-019-f002) | [tool_authorize](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/task_tool_routes.py#L117) | [Depends(require_agent_transport)]; get_db |
| `API-0157` | `POST /internal/v1/agent/task/tool-operation` | [MOD-019-F002](#mod-019-f002) | [tool_operation](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/task_tool_routes.py#L197) | [Depends(require_agent_transport)] |
| `API-0158` | `POST /internal/v1/agent/task/repository-source` | [MOD-019-F002](#mod-019-f002) | [repository_source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/task_tool_routes.py#L237) | [Depends(require_agent_transport)]; get_db |
| `API-0159` | `POST /internal/v1/agent/task/repository-publication` | [MOD-019-F002](#mod-019-f002) | [repository_publication](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/task_tool_routes.py#L291) | [Depends(require_agent_transport)]; get_db |
| `API-0160` | `POST /internal/v1/agent/task/repository-completion` | [MOD-019-F002](#mod-019-f002) | [repository_completion](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/task_tool_routes.py#L394) | [Depends(require_agent_transport)]; get_db |
| `API-0161` | `GET /v1/tasks/{task_id}` | [MOD-019-F001](#mod-019-f001) | [read_task](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/tasks/routes.py#L159) | get_db |
| `API-0162` | `GET /v1/tasks/{task_id}/events` | [MOD-019-F001](#mod-019-f001) | [read_events](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/tasks/routes.py#L174) | get_db |
| `API-0163` | `POST /v1/task-artifacts` | [MOD-019-F001](#mod-019-f001) | [upload_artifact](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/tasks/artifacts.py#L215) | get_db |
| `API-0164` | `GET /v1/tasks/{task_id}/artifacts/{artifact_id}` | [MOD-019-F001](#mod-019-f001) | [download_artifact](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/tasks/artifacts.py#L277) | get_db |
| `API-0165` | `POST /internal/v1/agent/task/artifact` | [MOD-019-F001](#mod-019-f001) | [artifact](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/tasks/internal_artifacts.py#L21) | [Depends(require_agent_transport)] |
| `API-0166` | `POST /internal/v1/agent/task/report` | [MOD-019-F001](#mod-019-f001) | [report](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/tasks/report_routes.py#L249) | [Depends(require_agent_transport)] |
| `API-0167` | `POST /v1/tasks/{task_id}/messages` | [MOD-019-F001](#mod-019-f001) | [message](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/tasks/command_routes.py#L261) | get_db |
| `API-0168` | `POST /v1/tasks/{task_id}/cancel` | [MOD-019-F001](#mod-019-f001) | [cancel](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/tasks/command_routes.py#L267) | get_db |
| `API-0169` | `POST /internal/v1/agent/task/control` | [MOD-019-F001](#mod-019-f001) | [control](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/tasks/command_routes.py#L280) | require_agent_transport |
| `API-0170` | `POST /internal/v1/agent/task/finalize` | [MOD-019-F001](#mod-019-f001) | [finalize](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/tasks/command_routes.py#L293) | get_db; require_agent_transport |
| `API-0171` | `POST /internal/v1/agent/task/settlement` | [MOD-019-F001](#mod-019-f001) | [settlement](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/tasks/command_routes.py#L319) | require_agent_transport |
| `API-0172` | `POST /internal/v1/agent/self/artifacts/{kind}` | [MOD-021-F003](#mod-021-f003) | [upload_artifact](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/artifact_service.py#L45) | [Depends(require_agent_transport)]; artifact_storage; get_agent_runtime |
| `API-0173` | `POST /internal/v1/agent/self/review-checks` | [MOD-021-F003](#mod-021-f003) | [reviewer_checks](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/artifact_service.py#L176) | [Depends(require_agent_transport)]; get_agent_runtime |
| `API-0174` | `POST /internal/v1/agent/arc/cyber/jobs` | [MOD-020-F001](#mod-020-f001) | [create_job](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/cyber_jobs.py#L194) | cyber_clients; get_db |
| `API-0175` | `POST /internal/v1/agent/arc/cyber/result` | [MOD-020-F001](#mod-020-f001) | [result](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/cyber_jobs.py#L214) | cyber_clients; get_db |
| `API-0176` | `POST /internal/v1/agent/report/review-result` | [MOD-021-F003](#mod-021-f003) | [upload_shared_review](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/shared_review.py#L110) | [Depends(require_agent_transport)]; shared_review_storage |
| `API-0177` | `POST /internal/v1/agent/report/review-checks` | [MOD-021-F003](#mod-021-f003) | [review_checks](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/shared_review.py#L125) | [Depends(require_agent_transport)] |
| `API-0178` | `POST /internal/v1/agent/self/github-operation` | [MOD-021-F002](#mod-021-f002) | [perform_github_operation](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/github_operation_routes.py#L201) | [Depends(require_agent_transport)]; get_agent_runtime |
| `API-0179` | `POST /internal/v1/agent/self/pull-request` | [MOD-021-F002](#mod-021-f002) | [bind_pull_request](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/pr_binding_routes.py#L139) | [Depends(require_agent_transport)]; get_agent_runtime |
| `API-0180` | `GET /internal/v1/agent/report` | [MOD-021-F002](#mod-021-f002) | [read_report](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/run_report_routes.py#L45) | [Depends(require_agent_transport)] |
| `API-0181` | `POST /internal/v1/agent/report/pull-request` | [MOD-021-F002](#mod-021-f002) | [register_pull_request](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/run_report_routes.py#L52) | [Depends(require_agent_transport)] |
| `API-0182` | `POST /internal/v1/agent/report/pull-request/retry` | [MOD-021-F002](#mod-021-f002) | [retry_pull_request](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/run_report_routes.py#L80) | [Depends(require_agent_transport)] |
| `API-0183` | `POST /internal/v1/agent/report/terminal` | [MOD-021-F002](#mod-021-f002) | [terminal_report](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/run_report_routes.py#L181) | [Depends(require_agent_transport)] |
| `API-0184` | `POST /internal/v1/agent/report/block` | [MOD-021-F002](#mod-021-f002) | [reporting_block](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/run_report_routes.py#L204) | [Depends(require_agent_transport)] |
| `API-0185` | `POST /internal/v1/agent/report/started` | [MOD-021-F002](#mod-021-f002) | [worker_started](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/run_report_routes.py#L220) | [Depends(require_agent_transport)] |
| `API-0186` | `POST /agent-authorities/service-grants` | [MOD-021-F003](#mod-021-f003) | [approve_service](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/service_authority.py#L243) | approving_human; get_service_authorities |
| `API-0187` | `POST /agent-authorities/service-grants/{reference}/revoke` | [MOD-021-F003](#mod-021-f003) | [revoke_service](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/agentauth/service_authority.py#L261) | approving_human; get_service_authorities |
| `API-0188` | `POST /v1/chat/completions` | [MOD-022-F001](#mod-022-f001) | [create_chat_completion](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/proxy/routes.py#L388) | get_proxy_service; get_token_context; set_agent_run_id_from_header; set_client_tool_from_header |
| `API-0189` | `GET /v1/models` | [MOD-022-F001](#mod-022-f001) | [list_models](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/proxy/routes.py#L444) | get_model_resolver; get_token_context |
| `API-0190` | `POST /v1/messages` | [MOD-022-F001](#mod-022-f001) | [create_message](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/proxy/routes.py#L467) | get_proxy_service; get_token_context; set_agent_run_id_from_header; set_client_tool_from_header |
| `API-0191` | `POST /v1/messages/count_tokens` | [MOD-022-F001](#mod-022-f001) | [count_tokens](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/proxy/routes.py#L583) | get_model_resolver; get_token_context |
| `API-0192` | `POST /bedrock/invoke` | [MOD-022-F001](#mod-022-f001) | [invoke_model](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/proxy/routes.py#L631) | get_proxy_service; get_token_context; set_agent_run_id_from_header; set_client_tool_from_header |
| `API-0193` | `POST /bedrock/invoke-with-response-stream` | [MOD-022-F001](#mod-022-f001) | [invoke_model_with_response_stream](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/proxy/routes.py#L685) | get_proxy_service; get_token_context; set_agent_run_id_from_header; set_client_tool_from_header |
| `API-0194` | `POST /model/{model_id}/invoke` | [MOD-022-F001](#mod-022-f001) | [invoke_model_by_path](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/proxy/routes.py#L744) | get_proxy_service; get_token_context; set_agent_run_id_from_header; set_client_tool_from_header |
| `API-0195` | `POST /model/{model_id}/invoke-with-response-stream` | [MOD-022-F001](#mod-022-f001) | [invoke_model_stream_by_path](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/proxy/routes.py#L862) | get_proxy_service; get_token_context; set_agent_run_id_from_header; set_client_tool_from_header |
| `API-0196` | `POST /openai/v1/responses` | [MOD-022-F001](#mod-022-f001) | [create_openai_response](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/proxy/routes.py#L994) | get_mantle_service; get_model_resolver; get_token_context; set_agent_run_id_from_header; set_client_tool_from_header |
| `API-0197` | `GET /v1/health` | [MOD-022-F001](#mod-022-f001) | [health_check](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/proxy/routes.py#L1085) | See handler and middleware |
| `API-0198` | `POST /admin/organizations` | [MOD-002-F001](#mod-002-f001) | [create_organization_gone](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L165) | get_current_user |
| `API-0199` | `GET /admin/organizations` | [MOD-002-F001](#mod-002-f001) | [list_organizations](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L203) | get_access_control; get_admin_service; get_current_user; permission; ORG_READ |
| `API-0200` | `GET /admin/organizations/{org_id}` | [MOD-002-F001](#mod-002-f001) | [get_organization](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L232) | get_access_control; get_admin_service; get_current_user; permission; ORG_READ |
| `API-0201` | `PUT /admin/organizations/{org_id}` | [MOD-002-F001](#mod-002-f001) | [update_organization](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L244) | get_access_control; get_admin_service; get_current_user; permission; ORG_UPDATE |
| `API-0202` | `DELETE /admin/organizations/{org_id}` | [MOD-002-F001](#mod-002-f001) | [delete_organization](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L260) | get_access_control; get_admin_service; get_current_user; permission; ORG_DELETE |
| `API-0203` | `GET /admin/organizations/{org_id}/budget/{entity_type}/{entity_id}` | [MOD-010-F003](#mod-010-f003) | [get_budget_config](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L280) | get_access_control; get_admin_service; get_current_user; permission; BUDGET_READ |
| `API-0204` | `GET /admin/organizations/{org_id}/budgets/{entity_type}/{entity_id}/status` | [MOD-010-F003](#mod-010-f003) | [get_budget_status](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L294) | get_access_control; get_admin_service; get_current_user; permission; BUDGET_READ |
| `API-0205` | `PUT /admin/organizations/{org_id}/budget/{entity_type}/{entity_id}` | [MOD-010-F003](#mod-010-f003) | [update_budget_config](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L311) | get_access_control; get_admin_service; get_current_user; permission; BUDGET_UPDATE |
| `API-0206` | `GET /admin/organizations/{org_id}/budgets` | [MOD-010-F003](#mod-010-f003) | [list_budgets](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L344) | get_access_control; get_admin_service; get_cognito_service; get_current_user; permission; BUDGET_READ |
| `API-0207` | `POST /admin/organizations/{org_id}/budgets` | [MOD-010-F003](#mod-010-f003) | [create_budget](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L369) | get_access_control; get_admin_service; get_current_user; permission; BUDGET_UPDATE |
| `API-0208` | `DELETE /admin/organizations/{org_id}/budget/{entity_type}/{entity_id}/{period_type}` | [MOD-010-F003](#mod-010-f003) | [delete_budget](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L395) | get_access_control; get_admin_service; get_current_user; permission; BUDGET_UPDATE |
| `API-0209` | `DELETE /admin/organizations/{org_id}/budget/{entity_type}/{entity_id}/{period_type}/revision` | [MOD-010-F003](#mod-010-f003) | [delete_exact_budget_config](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L438) | get_access_control; get_admin_service; get_current_user; BUDGET_UPDATE |
| `API-0210` | `GET /admin/organizations/{org_id}/budget/{entity_type}/{entity_id}/{period_type}` | [MOD-010-F003](#mod-010-f003) | [get_exact_budget_config](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L462) | get_access_control; get_admin_service; get_current_user; BUDGET_READ |
| `API-0211` | `PUT /admin/organizations/{org_id}/budget/{entity_type}/{entity_id}/{period_type}` | [MOD-010-F003](#mod-010-f003) | [set_exact_budget_config](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L477) | get_access_control; get_admin_service; get_current_user; BUDGET_UPDATE |
| `API-0212` | `GET /admin/organizations/{org_id}/budgets/{entity_type}/{entity_id}/{period_type}/status` | [MOD-010-F003](#mod-010-f003) | [get_exact_budget_status](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L497) | get_access_control; get_admin_service; get_current_user; BUDGET_READ |
| `API-0213` | `GET /admin/organizations/{org_id}/ratelimit/{entity_type}/{entity_id}` | [MOD-011-F001](#mod-011-f001) | [get_ratelimit_config](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L514) | get_access_control; get_admin_service; get_current_user; permission; RATELIMIT_READ |
| `API-0214` | `PUT /admin/organizations/{org_id}/ratelimit/{entity_type}/{entity_id}` | [MOD-011-F001](#mod-011-f001) | [update_ratelimit_config](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L528) | get_access_control; get_admin_service; get_current_user; permission; RATELIMIT_UPDATE |
| `API-0215` | `GET /admin/organizations/{org_id}/ratelimits` | [MOD-011-F001](#mod-011-f001) | [list_ratelimits](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L556) | get_access_control; get_admin_service; get_current_user; permission; RATELIMIT_READ |
| `API-0216` | `POST /admin/organizations/{org_id}/ratelimits` | [MOD-011-F001](#mod-011-f001) | [create_ratelimit](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L575) | get_access_control; get_admin_service; get_current_user; permission; RATELIMIT_UPDATE |
| `API-0217` | `DELETE /admin/organizations/{org_id}/ratelimit/{entity_type}/{entity_id}` | [MOD-011-F001](#mod-011-f001) | [delete_ratelimit](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L601) | get_access_control; get_admin_service; get_current_user; permission; RATELIMIT_UPDATE |
| `API-0218` | `GET /admin/pool/status` | [MOD-012-F001](#mod-012-f001) | [get_pool_status](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L630) | get_access_control; get_admin_service; get_current_user; permission; POOL_READ |
| `API-0219` | `POST /admin/pool/accounts` | [MOD-012-F001](#mod-012-f001) | [add_pool_account](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L644) | get_access_control; get_admin_service; get_current_user; permission; POOL_MANAGE |
| `API-0220` | `DELETE /admin/pool/accounts/{account_id}` | [MOD-012-F001](#mod-012-f001) | [remove_pool_account](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L662) | get_access_control; get_admin_service; get_current_user; permission; POOL_MANAGE |
| `API-0221` | `GET /admin/logs` | [MOD-013-F001](#mod-013-f001) | [query_logs](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L682) | get_access_control; get_current_user; get_log_service; permission; LOGS_READ |
| `API-0222` | `GET /admin/dashboard/platform` | [MOD-012-F001](#mod-012-f001) | [get_platform_dashboard](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L748) | get_access_control; get_admin_service; get_current_user; platform_admin |
| `API-0223` | `GET /admin/dashboard/org/{org_id}` | [MOD-012-F002](#mod-012-f002) | [get_org_dashboard](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L798) | get_access_control; get_admin_service; get_current_user; permission; USAGE_READ |
| `API-0224` | `POST /admin/organizations/{org_id}/departments` | [MOD-002-F002](#mod-002-f002) | [create_department](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L848) | get_access_control; get_admin_service; get_cognito_service; get_current_user; permission; ORG_UPDATE |
| `API-0225` | `GET /admin/organizations/{org_id}/departments` | [MOD-002-F002](#mod-002-f002) | [list_departments](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L868) | get_access_control; get_admin_service; get_current_user; permission; ORG_READ |
| `API-0226` | `GET /admin/organizations/{org_id}/departments/{dept_id}` | [MOD-002-F002](#mod-002-f002) | [get_department](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L891) | get_access_control; get_admin_service; get_current_user; permission; ORG_READ |
| `API-0227` | `PUT /admin/organizations/{org_id}/departments/{dept_id}` | [MOD-002-F002](#mod-002-f002) | [update_department](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L904) | get_access_control; get_admin_service; get_current_user; permission; ORG_UPDATE |
| `API-0228` | `DELETE /admin/organizations/{org_id}/departments/{dept_id}` | [MOD-002-F002](#mod-002-f002) | [delete_department](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L921) | get_access_control; get_admin_service; get_cognito_service; get_current_user; permission; ORG_UPDATE |
| `API-0229` | `POST /admin/organizations/{org_id}/departments/{dept_id}/teams` | [MOD-002-F002](#mod-002-f002), [MOD-002-F003](#mod-002-f003) | [create_team](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L940) | get_access_control; get_admin_service; get_current_user; permission; ORG_UPDATE |
| `API-0230` | `GET /admin/organizations/{org_id}/departments/{dept_id}/teams` | [MOD-002-F002](#mod-002-f002), [MOD-002-F003](#mod-002-f003) | [list_teams](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L960) | get_access_control; get_admin_service; get_current_user; permission; ORG_READ |
| `API-0231` | `PUT /admin/organizations/{org_id}/teams/{team_id}` | [MOD-002-F003](#mod-002-f003) | [update_team](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L984) | get_access_control; get_admin_service; get_current_user; permission; ORG_UPDATE |
| `API-0232` | `DELETE /admin/organizations/{org_id}/teams/{team_id}` | [MOD-002-F003](#mod-002-f003) | [delete_team](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1001) | get_access_control; get_admin_service; get_current_user; permission; ORG_UPDATE |
| `API-0233` | `GET /admin/organizations/{org_id}/teams` | [MOD-002-F003](#mod-002-f003) | [list_org_teams](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1016) | get_access_control; get_admin_service; get_current_user; permission; ORG_READ |
| `API-0234` | `GET /admin/organizations/{org_id}/users/{user_id}/teams` | [MOD-002-F003](#mod-002-f003), [MOD-002-F004](#mod-002-f004) | [list_user_teams](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1055) | get_access_control; get_current_user; get_db; permission; ORG_READ |
| `API-0235` | `PUT /admin/organizations/{org_id}/users/{user_id}/teams` | [MOD-002-F003](#mod-002-f003), [MOD-002-F004](#mod-002-f004) | [replace_user_teams](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1073) | get_access_control; get_current_user; get_db; permission; ORG_UPDATE |
| `API-0236` | `POST /admin/organizations/{org_id}/members` | [MOD-002-F005](#mod-002-f005) | [add_org_member](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1109) | get_access_control; get_current_user; get_db; platform_admin |
| `API-0237` | `POST /admin/organizations/{org_id}/teams/{team_id}/members` | [MOD-002-F003](#mod-002-f003), [MOD-002-F004](#mod-002-f004) | [add_team_member](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1177) | get_access_control; get_current_user; get_db; permission; ORG_UPDATE |
| `API-0238` | `DELETE /admin/organizations/{org_id}/teams/{team_id}/members/{user_id}` | [MOD-002-F003](#mod-002-f003), [MOD-002-F004](#mod-002-f004) | [remove_team_member](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1220) | get_access_control; get_current_user; get_db; permission; ORG_UPDATE |
| `API-0239` | `POST /admin/organizations/{org_id}/teams/{team_id}/users` | [MOD-002-F003](#mod-002-f003), [MOD-002-F006](#mod-002-f006) | [add_user](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1253) | get_access_control; get_admin_service; get_cognito_service; get_current_user; permission; ORG_UPDATE |
| `API-0240` | `GET /admin/organizations/{org_id}/users` | [MOD-002-F005](#mod-002-f005) | [list_users_org](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1279) | get_access_control; get_admin_service; get_current_user; permission; ORG_READ |
| `API-0241` | `GET /admin/organizations/{org_id}/teams/{team_id}/users` | [MOD-002-F003](#mod-002-f003), [MOD-002-F005](#mod-002-f005) | [list_users_team](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1302) | get_access_control; get_admin_service; get_current_user; permission; ORG_READ |
| `API-0242` | `PUT /admin/organizations/{org_id}/users/{user_id}` | [MOD-002-F006](#mod-002-f006) | [update_user](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1326) | get_access_control; get_admin_service; get_current_user; permission; USER_MANAGE |
| `API-0243` | `DELETE /admin/organizations/{org_id}/users/{user_id}` | [MOD-002-F005](#mod-002-f005) | [remove_user](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1393) | get_access_control; get_admin_service; get_cognito_service; get_current_user; permission; ORG_UPDATE |
| `API-0244` | `POST /admin/organizations/{org_id}/service-accounts` | [MOD-003-F001](#mod-003-f001) | [create_service_account](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1424) | get_access_control; get_admin_service; get_current_user; permission; ORG_UPDATE |
| `API-0245` | `GET /admin/organizations/{org_id}/service-accounts` | [MOD-003-F001](#mod-003-f001) | [list_service_accounts](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1452) | get_access_control; get_admin_service; get_current_user; permission; ORG_READ |
| `API-0246` | `DELETE /admin/organizations/{org_id}/service-accounts/{sa_id}` | [MOD-003-F001](#mod-003-f001) | [delete_service_account](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1475) | get_access_control; get_admin_service; get_current_user; permission; ORG_UPDATE |
| `API-0247` | `POST /admin/agents` | [MOD-003-F002](#mod-003-f002) | [create_agent](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1524) | get_access_control; get_agent_service; get_current_user; permission; ORG_UPDATE |
| `API-0248` | `GET /admin/agents` | [MOD-003-F002](#mod-003-f002) | [list_agents](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1554) | get_access_control; get_agent_service; get_current_user; permission; ORG_READ |
| `API-0249` | `GET /admin/agents/{client_id}` | [MOD-003-F002](#mod-003-f002) | [get_agent](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1579) | get_access_control; get_agent_service; get_current_user; permission; ORG_READ |
| `API-0250` | `GET /admin/agents/{client_id}/credentials` | [MOD-003-F002](#mod-003-f002) | [get_agent_credentials](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1596) | get_access_control; get_agent_service; get_current_user; permission; ORG_UPDATE |
| `API-0251` | `PUT /admin/agents/{client_id}` | [MOD-003-F002](#mod-003-f002) | [update_agent](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1620) | get_access_control; get_agent_service; get_current_user; permission; ORG_UPDATE |
| `API-0252` | `DELETE /admin/agents/{client_id}` | [MOD-003-F002](#mod-003-f002) | [delete_agent](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1660) | get_access_control; get_agent_service; get_current_user; permission; ORG_UPDATE |
| `API-0253` | `GET /admin/users/roles` | [MOD-002-F006](#mod-002-f006) | [get_available_roles](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1695) | get_access_control; get_current_user |
| `API-0254` | `GET /admin/users` | [MOD-002-F005](#mod-002-f005) | [list_platform_users](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1734) | get_access_control; get_admin_service; get_current_user; platform_admin |
| `API-0255` | `GET /admin/organizations/{org_id}/usage/timeseries` | [MOD-012-F002](#mod-012-f002) | [get_usage_timeseries](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1783) | get_access_control; get_admin_service; get_current_user; permission; USAGE_READ |
| `API-0256` | `GET /admin/users/me/chats` | [MOD-016-F002](#mod-016-f002) | [get_my_chats](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1824) | get_admin_service; get_current_user |
| `API-0257` | `GET /admin/users/me/chats/{request_id}` | [MOD-016-F002](#mod-016-f002) | [get_my_chat_detail](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1862) | get_admin_service; get_current_user |
| `API-0258` | `GET /admin/organizations/{org_id}/cognito/users` | [MOD-002-F006](#mod-002-f006) | [list_cognito_users](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1931) | get_access_control; get_cognito_service; get_current_user; permission; ORG_READ |
| `API-0259` | `GET /admin/organizations/{org_id}/cognito/teams` | [MOD-002-F003](#mod-002-f003), [MOD-002-F006](#mod-002-f006) | [list_cognito_teams](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L1971) | get_access_control; get_cognito_service; get_current_user; permission; ORG_READ |
| `API-0260` | `GET /admin/organizations/{org_id}/cognito/departments` | [MOD-002-F002](#mod-002-f002), [MOD-002-F006](#mod-002-f006) | [list_cognito_departments](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L2018) | get_access_control; get_cognito_service; get_current_user; permission; ORG_READ |
| `API-0261` | `POST /admin/registry/agents` | [MOD-003-F001](#mod-003-f001) | [create_registry_agent](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L2066) | get_access_control; get_agent_registry_service; get_current_user; permission; AGENT_REGISTER |
| `API-0262` | `GET /admin/registry/agents` | [MOD-003-F001](#mod-003-f001) | [list_registry_agents](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L2111) | get_access_control; get_agent_registry_service; get_current_user; permission; ORG_READ |
| `API-0263` | `GET /admin/registry/agents/{agent_id}` | [MOD-003-F001](#mod-003-f001) | [get_registry_agent](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L2167) | get_access_control; get_agent_registry_service; get_current_user; permission; ORG_READ |
| `API-0264` | `PATCH /admin/registry/agents/{agent_id}` | [MOD-003-F001](#mod-003-f001) | [update_registry_agent](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L2187) | get_access_control; get_agent_registry_service; get_current_user; permission; AGENT_REGISTER |
| `API-0265` | `DELETE /admin/registry/agents/{agent_id}` | [MOD-003-F001](#mod-003-f001) | [delete_registry_agent](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L2230) | get_access_control; get_agent_registry_service; get_current_user; permission; ORG_UPDATE |
| `API-0266` | `GET /admin/registry/agents/{agent_id}/usage` | [MOD-003-F001](#mod-003-f001) | [get_registry_agent_usage](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L2268) | get_access_control; get_agent_registry_service; get_current_user; permission; USAGE_READ |
| `API-0267` | `POST /admin/agents/onboard` | [MOD-003-F003](#mod-003-f003) | [onboard_agent](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L2317) | get_access_control; get_agent_onboarding_service; get_current_user; get_db; permission; AGENT_REGISTER |
| `API-0268` | `POST /admin/policies/preview` | [MOD-003-F003](#mod-003-f003) | [preview_policies](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L2456) | get_access_control; get_current_user |
| `API-0269` | `GET /admin/policies/agent-types` | [MOD-003-F003](#mod-003-f003) | [list_agent_types](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/routes.py#L2568) | get_current_user |
| `API-0270` | `GET /admin/audit-events` | [MOD-013-F001](#mod-013-f001) | [list_admin_audit_events](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/audit_routes.py#L56) | _get_access_control; get_current_user; get_db; platform_admin |
| `API-0271` | `GET /admin/organizations/{org_id}/ratelimit-cli` | [MOD-011-F001](#mod-011-f001) | [list_configs](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/ratelimit_cli.py#L152) | get_current_user; get_db; permission; RATELIMIT_READ |
| `API-0272` | `GET /admin/organizations/{org_id}/ratelimit-cli/{scope}/{key}` | [MOD-011-F001](#mod-011-f001) | [show_config](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/ratelimit_cli.py#L177) | get_current_user; get_db; RATELIMIT_READ |
| `API-0273` | `PUT /admin/organizations/{org_id}/ratelimit-cli/{scope}/{key}` | [MOD-011-F001](#mod-011-f001) | [set_config](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/ratelimit_cli.py#L207) | get_current_user; get_db; RATELIMIT_UPDATE |
| `API-0274` | `DELETE /admin/organizations/{org_id}/ratelimit-cli/{scope}/{key}` | [MOD-011-F001](#mod-011-f001) | [delete_config](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/ratelimit_cli.py#L240) | get_current_user; get_db; RATELIMIT_UPDATE |
| `API-0275` | `GET /admin/organizations/{org_id}/hierarchy/{kind}/{key}` | [MOD-002-F002](#mod-002-f002) | [read](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/hierarchy.py#L138) | get_current_user; get_db; ORG_READ |
| `API-0276` | `PATCH /admin/organizations/{org_id}/hierarchy/{kind}/{key}` | [MOD-002-F002](#mod-002-f002) | [patch](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/hierarchy.py#L146) | get_current_user; get_db; ORG_UPDATE; USER_MANAGE |
| `API-0277` | `DELETE /admin/organizations/{org_id}/hierarchy/{kind}/{key}` | [MOD-002-F002](#mod-002-f002) | [remove](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/hierarchy.py#L206) | get_current_user; get_db; ORG_DELETE; ORG_UPDATE |
| `API-0278` | `GET /admin/organizations/{org_id}/hierarchy/{kind}` | [MOD-002-F002](#mod-002-f002) | [listing](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/hierarchy.py#L246) | get_current_user; get_db; permission; ORG_READ |
| `API-0279` | `GET /admin/organizations/{org_id}/service-accounts/{account_id}/identity` | [MOD-003-F001](#mod-003-f001) | [get_machine_account](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/machine_accounts.py#L83) | See handler and middleware |
| `API-0280` | `POST /admin/organizations/{org_id}/service-accounts/register` | [MOD-003-F001](#mod-003-f001) | [register_machine_account](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/machine_accounts.py#L89) | See handler and middleware |
| `API-0281` | `PATCH /admin/organizations/{org_id}/service-accounts/{account_id}/identity` | [MOD-003-F001](#mod-003-f001) | [update_machine_account](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/machine_accounts.py#L111) | See handler and middleware |
| `API-0282` | `DELETE /admin/organizations/{org_id}/service-accounts/{account_id}/identity` | [MOD-003-F001](#mod-003-f001) | [delete_machine_account](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/machine_accounts.py#L126) | See handler and middleware |
| `API-0283` | `GET /admin/machine-agents/{kind}/{key}/identity` | [MOD-003-F001](#mod-003-f001) | [get_machine_agent](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/machine_agents.py#L85) | See handler and middleware |
| `API-0284` | `GET /admin/machine-agents/cognito-client/page` | [MOD-003-F001](#mod-003-f001) | [list_cognito_machine_agents](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/machine_agents.py#L91) | See handler and middleware |
| `API-0285` | `POST /admin/machine-agents/{kind}/register` | [MOD-003-F001](#mod-003-f001) | [register_machine_agent](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/machine_agents.py#L119) | See handler and middleware |
| `API-0286` | `PATCH /admin/machine-agents/{kind}/{key}/identity` | [MOD-003-F001](#mod-003-f001) | [update_machine_agent](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/machine_agents.py#L183) | See handler and middleware |
| `API-0287` | `POST /api/admin/identity/organizations` | [MOD-002-F001](#mod-002-f001) | [create_organization](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/identity/router.py#L54) | [Depends(require_admin)]; get_current_user; get_db |
| `API-0288` | `GET /api/admin/identity/organizations` | [MOD-002-F001](#mod-002-f001) | [list_organizations](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/identity/router.py#L106) | [Depends(require_admin)]; get_current_user; get_db |
| `API-0289` | `GET /api/admin/identity/organizations/{org_id}` | [MOD-002-F001](#mod-002-f001) | [get_organization](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/identity/router.py#L117) | [Depends(require_admin)]; get_current_user; get_db |
| `API-0290` | `PATCH /api/admin/identity/organizations/{org_id}` | [MOD-002-F001](#mod-002-f001) | [update_organization](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/identity/router.py#L131) | [Depends(require_admin)]; get_current_user; get_db; platform_admin |
| `API-0291` | `DELETE /api/admin/identity/organizations/{org_id}` | [MOD-002-F001](#mod-002-f001) | [delete_organization](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/identity/router.py#L188) | [Depends(require_admin)]; get_current_user; get_db |
| `API-0292` | `POST /api/admin/identity/organizations/{org_id}/users` | [MOD-005-F001](#mod-005-f001) | [create_user](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/identity/router.py#L215) | [Depends(require_admin)]; get_current_user; get_db; platform_admin |
| `API-0293` | `GET /api/admin/identity/organizations/{org_id}/users` | [MOD-005-F001](#mod-005-f001) | [list_users](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/identity/router.py#L255) | [Depends(require_admin)]; get_current_user; get_db |
| `API-0294` | `DELETE /api/admin/identity/organizations/{org_id}/users/{user_id}` | [MOD-005-F001](#mod-005-f001) | [delete_user](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/identity/router.py#L267) | [Depends(require_admin)]; get_current_user; get_db |
| `API-0295` | `POST /api/admin/identity/users/{user_id}/identities` | [MOD-005-F001](#mod-005-f001) | [add_identity](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/identity/router.py#L295) | [Depends(require_admin)]; get_current_user; get_db |
| `API-0296` | `GET /api/admin/identity/users/{user_id}/identities` | [MOD-005-F001](#mod-005-f001) | [list_identities](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/identity/router.py#L319) | [Depends(require_admin)]; get_current_user; get_db |
| `API-0297` | `DELETE /api/admin/identity/users/{user_id}/identities/{identity_id}` | [MOD-005-F001](#mod-005-f001) | [delete_identity](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/identity/router.py#L331) | [Depends(require_admin)]; get_current_user; get_db |
| `API-0298` | `POST /admin/identity/organizations/{org_id}/github-members` | [MOD-002-F007](#mod-002-f007) | [add_github_member](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/identity/github_enrollment_routes.py#L17) | [Depends(require_admin)]; get_current_user; get_db |
| `API-0299` | `POST /admin/identity/organizations/{org_id}/users/{user_id}/provision` | [MOD-005-F001](#mod-005-f001) | [provision_user](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/identity/recovery_routes.py#L23) | [Depends(require_admin)]; get_current_user; get_db; platform_admin |
| `API-0300` | `PUT /admin/identity/organizations/{org_id}/users/{user_id}/cognito` | [MOD-005-F001](#mod-005-f001) | [link_cognito_user](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/identity/recovery_routes.py#L46) | [Depends(require_admin)]; get_current_user; get_db; platform_admin |
| `API-0301` | `POST /admin/connections/github/install-start` | [MOD-006-F001](#mod-006-f001) | [github_install_start](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/connections/routes.py#L165) | get_current_user; get_db |
| `API-0302` | `GET /admin/connections/github/install-callback` | [MOD-006-F001](#mod-006-f001) | [github_install_callback](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/connections/routes.py#L206) | get_db |
| `API-0303` | `GET /admin/connections` | [MOD-006-F001](#mod-006-f001) | [get_connections](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/connections/routes.py#L314) | get_current_user; get_db |
| `API-0304` | `POST /admin/connections/switch-tenant` | [MOD-006-F001](#mod-006-f001) | [switch_tenant](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/connections/routes.py#L362) | get_current_user; get_db |
| `API-0305` | `DELETE /admin/connections/github/{installation_id}` | [MOD-006-F001](#mod-006-f001) | [disconnect_github](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/connections/routes.py#L385) | get_current_user; get_db |
| `API-0306` | `POST /admin/connections/github/app/register-start` | [MOD-006-F002](#mod-006-f002) | [github_app_register_start](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/connections/routes.py#L447) | _get_access_control; get_current_user; get_db; platform_admin |
| `API-0307` | `GET /admin/connections/github/app/register-callback` | [MOD-006-F002](#mod-006-f002) | [github_app_register_callback](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/connections/routes.py#L498) | _app_lifecycle_lock; get_db |
| `API-0308` | `POST /admin/connections/github/app/register-manual` | [MOD-006-F002](#mod-006-f002) | [github_app_register_manual](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/connections/routes.py#L565) | _app_lifecycle_lock; _get_access_control; get_current_user; get_db; platform_admin |
| `API-0309` | `GET /admin/connections/github/app/setup-guide` | [MOD-006-F002](#mod-006-f002) | [github_app_setup_guide](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/connections/routes.py#L615) | _get_access_control; get_current_user; platform_admin |
| `API-0310` | `GET /admin/connections/github/app/status` | [MOD-006-F002](#mod-006-f002) | [github_app_status](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/connections/routes.py#L628) | _get_access_control; get_current_user; platform_admin |
| `API-0311` | `POST /admin/connections/github/app/revalidate` | [MOD-006-F002](#mod-006-f002) | [github_app_revalidate](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/connections/routes.py#L654) | _app_lifecycle_lock; _get_access_control; get_current_user; get_db; platform_admin |
| `API-0312` | `POST /admin/connections/github/app/rotate-key` | [MOD-006-F002](#mod-006-f002) | [github_app_rotate_key](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/connections/routes.py#L695) | _app_lifecycle_lock; _get_access_control; get_current_user; get_db; platform_admin |
| `API-0313` | `POST /admin/connections/github/app/disconnect` | [MOD-006-F002](#mod-006-f002) | [github_app_disconnect](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/connections/routes.py#L733) | _app_lifecycle_lock; _get_access_control; get_current_user; get_db; platform_admin |
| `API-0314` | `GET /admin/connections/github/app/maintenance` | [MOD-006-F002](#mod-006-f002) | [github_app_maintenance_status](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/connections/routes.py#L782) | _maintenance_admin |
| `API-0315` | `POST /admin/connections/github/app/maintenance/disconnect` | [MOD-006-F002](#mod-006-f002) | [github_app_disconnect_reviewed](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/connections/routes.py#L805) | _app_lifecycle_lock; _maintenance_admin; get_db |
| `API-0316` | `POST /admin/connections/github/app/maintenance/rotate-key` | [MOD-006-F002](#mod-006-f002) | [github_app_activate_supplied_key](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/connections/routes.py#L820) | [Depends(_app_lifecycle_lock)] |
| `API-0317` | `GET /admin/organizations/{org_id}/connections/github` | [MOD-002-F009](#mod-002-f009) | [list_github_connections](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/org_connections/routes.py#L81) | [Depends(require_admin)]; get_current_user; get_db; platform_admin |
| `API-0318` | `POST /admin/organizations/{org_id}/connections/github` | [MOD-002-F009](#mod-002-f009) | [attach_github_connection](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/org_connections/routes.py#L99) | [Depends(require_admin)]; get_current_user; get_db; platform_admin |
| `API-0319` | `DELETE /admin/organizations/{org_id}/connections/github/{installation_id}` | [MOD-002-F009](#mod-002-f009) | [detach_github_connection](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/org_connections/routes.py#L136) | [Depends(require_admin)]; get_current_user; get_db; platform_admin |
| `API-0320` | `POST /admin/tenants/{tenant_id}/orgs` | [MOD-002-F010](#mod-002-f010) | [link_org_to_tenant](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/tenants/routes.py#L98) | _get_access_control; get_current_user; get_db; platform_admin |
| `API-0321` | `DELETE /admin/tenants/{tenant_id}/orgs/{github_org_id}` | [MOD-002-F010](#mod-002-f010) | [unlink_org_from_tenant](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/tenants/routes.py#L241) | _get_access_control; get_current_user; get_db; platform_admin |
| `API-0322` | `GET /admin/tenants/{tenant_id}/orgs` | [MOD-002-F010](#mod-002-f010) | [list_linked_orgs](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/tenants/routes.py#L321) | _get_access_control; get_current_user; get_db; platform_admin |
| `API-0323` | `GET /admin/tenants/{tenant_id}/orgs/{github_org_id}/preview` | [MOD-002-F010](#mod-002-f010) | [preview_org_link](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/tenants/routes.py#L365) | _get_access_control; get_current_user; get_db; platform_admin |
| `API-0324` | `GET /admin/tenants/{tenant_id}/orgs/page` | [MOD-002-F010](#mod-002-f010) | [linked_org_page](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/tenants/routes.py#L387) | _get_access_control; get_current_user; get_db; platform_admin |
| `API-0325` | `GET /access/status` | [MOD-005-F005](#mod-005-f005) | [get_access_status](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/onboarding/handler.py#L681) | get_current_user; get_db |
| `API-0326` | `POST /access/request` | [MOD-005-F005](#mod-005-f005) | [submit_access_request](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/onboarding/handler.py#L779) | get_current_user; get_db; submit_lock |
| `API-0327` | `GET /admin/access-requests` | [MOD-005-F005](#mod-005-f005) | [list_access_requests](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/onboarding/handler.py#L1061) | _get_access_control; get_current_user; get_db; permission; USER_MANAGE |
| `API-0328` | `POST /admin/access-requests/{request_id}/approve` | [MOD-005-F005](#mod-005-f005) | [approve_access_request](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/onboarding/handler.py#L1150) | _get_access_control; get_current_user; get_db; request_lock |
| `API-0329` | `POST /admin/access-requests/{request_id}/deny` | [MOD-005-F005](#mod-005-f005) | [deny_access_request](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/onboarding/handler.py#L1339) | _get_access_control; get_current_user; get_db; request_lock |
| `API-0330` | `GET /admin/access-requests/review` | [MOD-005-F005](#mod-005-f005) | [review_list](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/onboarding/cli_contract.py#L90) | get_current_user; get_db; permission; USER_MANAGE |
| `API-0331` | `GET /admin/access-requests/{request_id}/review` | [MOD-005-F005](#mod-005-f005) | [review_one](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/onboarding/cli_contract.py#L113) | get_current_user; get_db |
| `API-0332` | `POST /admin/access-requests/{request_id}/approve/revision` | [MOD-005-F005](#mod-005-f005) | [approve_reviewed](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/onboarding/cli_contract.py#L164) | get_current_user; get_db; request_lock |
| `API-0333` | `POST /admin/access-requests/{request_id}/deny/revision` | [MOD-005-F005](#mod-005-f005) | [deny_reviewed](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/onboarding/cli_contract.py#L169) | get_current_user; get_db; request_lock |
| `API-0334` | `GET /budgets/{budget_id}` | [MOD-010-F003](#mod-010-f003) | [get_budget](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/routes.py#L63) | get_budget_service; get_current_user; get_db |
| `API-0335` | `GET /budgets/entity/{entity_type}/{entity_id}` | [MOD-010-F003](#mod-010-f003) | [get_budgets_for_entity](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/routes.py#L79) | get_budget_service; get_current_user; get_db |
| `API-0336` | `GET /budgets/status/{entity_type}/{entity_id}` | [MOD-010-F003](#mod-010-f003) | [get_budget_status](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/routes.py#L96) | get_budget_service; get_current_user; get_db |
| `API-0337` | `GET /budgets/usage/{entity_type}/{entity_id}` | [MOD-010-F003](#mod-010-f003) | [get_budget_usage](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/routes.py#L116) | get_budget_service; get_current_user; get_db |
| `API-0338` | `POST /budgets/calculate-cost` | [MOD-010-F003](#mod-010-f003) | [calculate_cost](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/routes.py#L139) | get_budget_service |
| `API-0339` | `GET /budgets/summary/{entity_type}/{entity_id}` | [MOD-010-F003](#mod-010-f003) | [get_budget_summary](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/routes.py#L151) | get_budget_service; get_current_user; get_db |
| `API-0340` | `GET /budgets/organization/overview` | [MOD-010-F003](#mod-010-f003) | [get_organization_budget_overview](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/routes.py#L165) | get_budget_service; get_current_user; get_db |
| `API-0341` | `GET /budgets/organization/alerts` | [MOD-010-F003](#mod-010-f003) | [get_budget_alerts](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/routes.py#L181) | get_budget_service; get_current_user; get_db |
| `API-0342` | `GET /budget/enforcement` | [MOD-010-F004](#mod-010-f004) | [get_global](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/enforcement_routes.py#L106) | permission; BUDGET_READ |
| `API-0343` | `POST /budget/enforcement` | [MOD-010-F004](#mod-010-f004) | [set_global](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/enforcement_routes.py#L112) | See handler and middleware |
| `API-0344` | `GET /budget/enforcement/flows/{flow_id}` | [MOD-010-F004](#mod-010-f004) | [get_flow](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/enforcement_routes.py#L117) | permission; BUDGET_READ |
| `API-0345` | `POST /budget/enforcement/flows/{flow_id}` | [MOD-010-F004](#mod-010-f004) | [set_flow](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/enforcement_routes.py#L124) | See handler and middleware |
| `API-0346` | `GET /me/budget` | [MOD-010-F001](#mod-010-f001) | [get_my_budget](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/me_routes.py#L871) | get_current_user; get_db |
| `API-0347` | `GET /me/budget/runs` | [MOD-010-F001](#mod-010-f001) | [get_my_budget_runs](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/me_routes.py#L1277) | get_activity_service; get_current_user; get_db |
| `API-0348` | `GET /budget/scope/{entity_type}/{entity_id}` | [MOD-010-F003](#mod-010-f003) | [get_managed_scope_budget](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/managed_scope_routes.py#L552) | get_current_user; get_db |
| `API-0349` | `GET /budget/scope/{entity_type}/{entity_id}/runs` | [MOD-010-F003](#mod-010-f003) | [get_managed_scope_budget_runs](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/managed_scope_routes.py#L628) | get_activity_service; get_current_user; get_db |
| `API-0350` | `GET /budget/reports/mis-partitioned-caps` | [MOD-010-F002](#mod-010-f002) | [get_mis_partitioned_cap_report](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/report_routes.py#L281) | get_current_user; get_db |
| `API-0351` | `GET /me/budget/person-cap` | [MOD-010-F002](#mod-010-f002) | [get_my_person_cap](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/person_cap_routes.py#L721) | get_current_user; get_db |
| `API-0352` | `GET /budget/person-cap/{person_anchor}` | [MOD-010-F002](#mod-010-f002) | [get_person_cap](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/person_cap_routes.py#L829) | get_current_user; get_db; platform_admin |
| `API-0353` | `PUT /budget/person-cap/{person_anchor}` | [MOD-010-F002](#mod-010-f002) | [put_person_cap](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/person_cap_routes.py#L866) | get_current_user; get_db; permission; platform_admin |
| `API-0354` | `DELETE /budget/person-cap/{person_anchor}` | [MOD-010-F002](#mod-010-f002) | [delete_person_cap](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/person_cap_routes.py#L939) | get_current_user; get_db; platform_admin |
| `API-0355` | `GET /budget/person-default/{scope}` | [MOD-010-F002](#mod-010-f002) | [get_person_default](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/person_cap_routes.py#L985) | get_current_user; get_db; platform_admin |
| `API-0356` | `PUT /budget/person-default/{scope}` | [MOD-010-F002](#mod-010-f002) | [put_person_default](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/person_cap_routes.py#L1027) | get_current_user; get_db; platform_admin |
| `API-0357` | `DELETE /budget/person-default/{scope}` | [MOD-010-F002](#mod-010-f002) | [delete_person_default](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/person_cap_routes.py#L1103) | get_current_user; get_db; platform_admin |
| `API-0358` | `GET /admin/organizations/{org_id}/member-budgets` | [MOD-002-F008](#mod-002-f008) | [list_member_budgets](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/person_cap_routes.py#L1213) | get_current_user; get_db; platform_admin |
| `API-0359` | `GET /me/budget/monthly-spend` | [MOD-010-F001](#mod-010-f001) | [get_monthly_spend](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/overview_routes.py#L118) | See handler and middleware |
| `API-0360` | `GET /budget/hierarchy` | [MOD-010-F002](#mod-010-f002) | [get_hierarchy](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/overview_routes.py#L173) | platform_admin |
| `API-0361` | `GET /budget/people-spend` | [MOD-010-F002](#mod-010-f002) | [get_people_spend](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/budget/overview_routes.py#L259) | platform_admin |
| `API-0362` | `GET /admin/bedrock-routing/mappings` | [MOD-009-F001](#mod-009-f001) | [list_mappings](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/bedrock_routing/routes.py#L192) | get_current_user; get_db; platform_admin |
| `API-0363` | `PUT /admin/bedrock-routing/mappings/{scope}` | [MOD-009-F001](#mod-009-f001) | [put_mapping](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/bedrock_routing/routes.py#L240) | get_current_user; get_db; get_secrets_manager; platform_admin |
| `API-0364` | `DELETE /admin/bedrock-routing/mappings/{scope}` | [MOD-009-F001](#mod-009-f001) | [delete_mapping](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/bedrock_routing/routes.py#L372) | get_current_user; get_db; platform_admin |
| `API-0365` | `GET /admin/bedrock-routing/effective/{user_id}` | [MOD-009-F001](#mod-009-f001) | [get_effective_mapping](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/bedrock_routing/routes.py#L425) | get_current_user; get_db; platform_admin |
| `API-0366` | `GET /admin/bedrock-routing/destinations` | [MOD-009-F001](#mod-009-f001) | [list_destinations](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/bedrock_routing/routes.py#L468) | get_current_user; get_db; platform_admin |
| `API-0367` | `GET /admin/bedrock-routing/connections` | [MOD-009-F001](#mod-009-f001) | [list_existing_connections](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/bedrock_routing/routes.py#L503) | get_current_user; get_db; platform_admin |
| `API-0368` | `POST /admin/bedrock-routing/connection-links` | [MOD-009-F001](#mod-009-f001) | [link_existing_connection](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/bedrock_routing/routes.py#L542) | get_current_user; get_db; get_secrets_manager; platform_admin |
| `API-0369` | `DELETE /admin/bedrock-routing/connection-links/{destination_id}` | [MOD-009-F001](#mod-009-f001) | [unlink_existing_connection](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/bedrock_routing/routes.py#L642) | get_current_user; get_db; platform_admin |
| `API-0370` | `POST /admin/bedrock-routing/destinations` | [MOD-009-F001](#mod-009-f001) | [register_destination](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/bedrock_routing/routes.py#L674) | get_current_user; get_db; get_secrets_manager; platform_admin |
| `API-0371` | `GET /admin/bedrock-routing/destinations/{destination_id}/setup` | [MOD-009-F001](#mod-009-f001) | [destination_setup](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/bedrock_routing/routes.py#L819) | get_current_user; get_db; get_secrets_manager; platform_admin |
| `API-0372` | `POST /admin/bedrock-routing/destinations/{destination_id}/verify` | [MOD-009-F001](#mod-009-f001) | [verify_destination](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/bedrock_routing/routes.py#L874) | get_current_user; get_db; get_secrets_manager; platform_admin |
| `API-0373` | `GET /me/bedrock-routing/selection` | [MOD-009-F002](#mod-009-f002) | [get_my_selection](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/bedrock_routing/self_routes.py#L241) | get_current_user; get_db |
| `API-0374` | `PUT /me/bedrock-routing/selection` | [MOD-009-F002](#mod-009-f002) | [put_my_selection](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/bedrock_routing/self_routes.py#L277) | get_current_user; get_db; get_secrets_manager |
| `API-0375` | `DELETE /me/bedrock-routing/selection` | [MOD-009-F002](#mod-009-f002) | [delete_my_selection](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/bedrock_routing/self_routes.py#L438) | get_current_user; get_db |
| `API-0376` | `GET /me/persona-models` | [MOD-001-F003](#mod-001-f003) | [list_my_preferences](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/self_routes.py#L133) | get_db; get_persona_model_current_user |
| `API-0377` | `GET /me/persona-models/costs` | [MOD-001-F001](#mod-001-f001), [MOD-001-F003](#mod-001-f003) | [get_my_persona_costs](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/self_routes.py#L148) | get_db; get_persona_model_current_user |
| `API-0378` | `GET /me/persona-models/explain/{persona_key}` | [MOD-001-F001](#mod-001-f001), [MOD-001-F003](#mod-001-f003) | [explain_my_preference](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/self_routes.py#L176) | get_db; get_persona_model_current_user |
| `API-0379` | `PUT /me/persona-models/{persona_key}` | [MOD-001-F002](#mod-001-f002), [MOD-001-F003](#mod-001-f003) | [set_my_preference](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/self_routes.py#L191) | get_db; get_persona_model_current_user |
| `API-0380` | `DELETE /me/persona-models/{persona_key}` | [MOD-001-F002](#mod-001-f002), [MOD-001-F003](#mod-001-f003) | [reset_my_preference](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/self_routes.py#L328) | get_db; get_persona_model_current_user |
| `API-0381` | `GET /me/persona-models/manageable-service-principals` | [MOD-001-F003](#mod-001-f003) | [list_manageable_service_principals](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/self_routes.py#L425) | get_db; get_persona_model_current_user; permission; ORG_UPDATE |
| `API-0382` | `GET /service-principals/{canonical_id}/persona-models` | [MOD-001-F003](#mod-001-f003) | [list_service_principal_preferences](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/routes.py#L68) | get_current_user; get_db |
| `API-0383` | `GET /service-principals/{canonical_id}/persona-models/costs` | [MOD-001-F003](#mod-001-f003) | [get_service_principal_persona_costs](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/routes.py#L91) | get_current_user; get_db |
| `API-0384` | `GET /service-principals/{canonical_id}/persona-models/catalog` | [MOD-001-F003](#mod-001-f003) | [get_service_principal_catalogue](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/routes.py#L125) | get_current_user; get_db |
| `API-0385` | `GET /service-principals/{canonical_id}/persona-models/explain/{persona_key}` | [MOD-001-F003](#mod-001-f003) | [explain_service_principal_preference](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/routes.py#L199) | get_current_user; get_db |
| `API-0386` | `PUT /service-principals/{canonical_id}/persona-models/{persona_key}` | [MOD-001-F003](#mod-001-f003) | [set_service_principal_preference](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/routes.py#L224) | get_current_user; get_db |
| `API-0387` | `DELETE /service-principals/{canonical_id}/persona-models/{persona_key}` | [MOD-001-F003](#mod-001-f003) | [reset_service_principal_preference](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/routes.py#L414) | get_current_user; get_db |
| `API-0388` | `GET /service-principals/registration-contract` | [MOD-003-F001](#mod-003-f001) | [get_registration_contract](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/routes.py#L534) | get_current_user; get_db |
| `API-0389` | `GET /service-principals/{canonical_id}/identity` | [MOD-003-F001](#mod-003-f001) | [get_service_principal_identity](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/routes.py#L543) | get_current_user; get_db |
| `API-0390` | `POST /service-principals/register` | [MOD-003-F001](#mod-003-f001) | [register_service_principal](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/routes.py#L554) | get_current_user; get_db |
| `API-0391` | `POST /service-principals/{canonical_id}/aliases` | [MOD-003-F001](#mod-003-f001) | [link_alias](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/routes.py#L619) | get_current_user; get_db |
| `API-0392` | `PATCH /service-principals/{canonical_id}/status` | [MOD-003-F001](#mod-003-f001) | [transition_service_principal_status](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/routes.py#L669) | get_current_user; get_db |
| `API-0393` | `DELETE /service-principals/{canonical_id}/aliases/{alias_row_id}` | [MOD-003-F001](#mod-003-f001) | [revoke_alias](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/routes.py#L742) | get_current_user; get_db |
| `API-0394` | `GET /service-principals/{canonical_id}/task-policy` | [MOD-004-F001](#mod-004-f001) | [get_task_policy](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/routes.py#L797) | get_current_user; get_db; task_policy_store |
| `API-0395` | `PUT /service-principals/{canonical_id}/task-policy` | [MOD-004-F001](#mod-004-f001) | [put_task_policy](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/routes.py#L817) | get_current_user; get_db; task_policy_store |
| `API-0396` | `GET /human-principals/{user_id}/task-policy` | [MOD-004-F001](#mod-004-f001) | [get_policy](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/human_task_routes.py#L64) | get_current_user; get_db; task_policy_store |
| `API-0397` | `PUT /human-principals/{user_id}/task-policy` | [MOD-004-F001](#mod-004-f001) | [put_policy](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/human_task_routes.py#L81) | get_current_user; get_db; task_policy_store |
| `API-0398` | `GET /admin/organizations/{org_id}/task-policy-identity` | [MOD-004-F001](#mod-004-f001) | [resolve_identity](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/task_policy_ui_routes.py#L74) | See handler and middleware |
| `API-0399` | `GET /admin/organizations/{org_id}/task-policies/{canonical_id}` | [MOD-004-F001](#mod-004-f001) | [get_policy_view](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/task_policy_ui_routes.py#L111) | See handler and middleware |
| `API-0400` | `PUT /admin/organizations/{org_id}/task-policies/{canonical_id}` | [MOD-004-F001](#mod-004-f001) | [save_policy](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/task_policy_ui_routes.py#L118) | See handler and middleware |
| `API-0401` | `GET /me/task-policy-view` | [MOD-001-F007](#mod-001-f007), [MOD-004-F001](#mod-004-f001) | [self_view](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/task_policy_ui_routes.py#L137) | get_persona_model_current_user |
| `API-0402` | `GET /service-principals/{canonical_id}/task-policy-view` | [MOD-001-F007](#mod-001-f007), [MOD-004-F001](#mod-004-f001) | [service_view](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/task_policy_ui_routes.py#L146) | See handler and middleware |
| `API-0403` | `GET /task-reservation-preview` | [MOD-001-F007](#mod-001-f007) | [reservation_preview](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/task_policy_ui_routes.py#L153) | See handler and middleware |
| `API-0404` | `GET /admin/persona-models/posture/{compatibility_class}` | [MOD-001-F005](#mod-001-f005) | [get_runtime_posture](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/posture_routes.py#L66) | get_current_user; get_db; platform_admin |
| `API-0405` | `PUT /admin/persona-models/posture/{compatibility_class}` | [MOD-001-F005](#mod-001-f005) | [set_runtime_posture](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/posture_routes.py#L94) | get_current_user; get_db; platform_admin |
| `API-0406` | `GET /admin/persona-models/posture/{compatibility_class}/history/{revision}` | [MOD-001-F005](#mod-001-f005) | [get_posture_history](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/posture_routes.py#L214) | get_current_user; get_db; platform_admin |
| `API-0407` | `POST /admin/persona-models/posture/{compatibility_class}/rollback` | [MOD-001-F005](#mod-001-f005) | [rollback_runtime_posture](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/posture_routes.py#L225) | get_current_user; get_db; platform_admin |
| `API-0408` | `GET /admin/persona-models/default/{compatibility_class}` | [MOD-001-F005](#mod-001-f005) | [get_class_default](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/default_routes.py#L56) | get_current_user; get_db; platform_admin |
| `API-0409` | `PUT /admin/persona-models/default/{compatibility_class}` | [MOD-001-F005](#mod-001-f005) | [set_class_default](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/default_routes.py#L70) | get_current_user; get_db; platform_admin |
| `API-0410` | `GET /admin/persona-models/default/{compatibility_class}/preview` | [MOD-001-F005](#mod-001-f005) | [preview_class_default](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/default_routes.py#L211) | get_current_user; get_db; platform_admin |
| `API-0411` | `GET /admin/persona-defaults` | [MOD-001-F004](#mod-001-f004) | [list_persona_defaults](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/persona_default_routes.py#L62) | get_current_user; get_db; platform_admin |
| `API-0412` | `PUT /admin/persona-defaults/{persona_key}` | [MOD-001-F004](#mod-001-f004) | [set_persona_default](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/persona_default_routes.py#L89) | get_current_user; get_db; platform_admin |
| `API-0413` | `GET /me/persona-models/catalog` | [MOD-001-F001](#mod-001-f001) | [get_catalogue](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/persona_models/catalogue_routes.py#L186) | get_db; get_persona_model_current_user |
| `API-0414` | `GET /ratelimits/me` | [MOD-011-F001](#mod-011-f001) | [own_effective_limits](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/ratelimit/routes.py#L60) | get_current_user; get_db |
| `API-0415` | `GET /ratelimits` | [MOD-011-F001](#mod-011-f001) | [list_rate_limits](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/ratelimit/routes.py#L92) | get_current_user; get_rate_limit_service; admin |
| `API-0416` | `GET /ratelimits/{entity_type}/{entity_id}` | [MOD-011-F001](#mod-011-f001) | [get_rate_limits](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/ratelimit/routes.py#L133) | get_current_user; get_rate_limit_service; admin |
| `API-0417` | `PUT /ratelimits/{entity_type}/{entity_id}` | [MOD-011-F001](#mod-011-f001) | [configure_rate_limits](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/ratelimit/routes.py#L170) | get_current_user; get_db; get_rate_limit_service; admin |
| `API-0418` | `DELETE /ratelimits/{entity_type}/{entity_id}` | [MOD-011-F001](#mod-011-f001) | [delete_rate_limits](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/ratelimit/routes.py#L209) | get_current_user; get_db; get_rate_limit_service; admin |
| `API-0419` | `GET /ratelimits/{entity_type}/{entity_id}/status` | [MOD-011-F001](#mod-011-f001) | [get_rate_limit_status](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/ratelimit/routes.py#L243) | get_current_user; get_rate_limit_service |
| `API-0420` | `GET /usage/summary` | [MOD-014-F001](#mod-014-f001) | [get_usage_summary](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/usage/routes.py#L38) | get_access_control; get_current_user; get_usage_service; permission; USAGE_READ |
| `API-0421` | `GET /usage/organizations` | [MOD-014-F001](#mod-014-f001) | [get_usage_by_organization](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/usage/routes.py#L83) | get_access_control; get_current_user; get_usage_service; permission; USAGE_READ |
| `API-0422` | `GET /usage/organizations/{org_id}` | [MOD-014-F001](#mod-014-f001) | [get_organization_usage](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/usage/routes.py#L105) | get_access_control; get_current_user; get_usage_service; permission; USAGE_READ |
| `API-0423` | `GET /usage/models` | [MOD-014-F001](#mod-014-f001) | [get_usage_by_model](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/usage/routes.py#L126) | get_access_control; get_current_user; get_usage_service; permission; USAGE_READ |
| `API-0424` | `GET /usage/timeline` | [MOD-014-F001](#mod-014-f001) | [get_usage_timeline](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/usage/routes.py#L156) | get_access_control; get_current_user; get_usage_service; permission; USAGE_READ |
| `API-0425` | `GET /usage/users` | [MOD-014-F001](#mod-014-f001) | [get_usage_by_user](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/usage/routes.py#L186) | get_access_control; get_current_user; get_usage_service; permission; USAGE_READ |
| `API-0426` | `GET /usage/departments` | [MOD-014-F001](#mod-014-f001) | [get_usage_by_department](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/usage/routes.py#L210) | get_access_control; get_current_user; get_usage_service; permission; USAGE_READ |
| `API-0427` | `GET /usage/logs` | [MOD-014-F001](#mod-014-f001) | [get_usage_logs](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/usage/routes.py#L233) | get_access_control; get_current_user; get_usage_service; permission; LOGS_READ |
| `API-0428` | `GET /usage/me/{view}` | [MOD-014-F001](#mod-014-f001) | [own_read](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/usage/cli_reads.py#L331) | get_current_user; get_db |
| `API-0429` | `GET /usage/managed/{org_id}/{view}` | [MOD-014-F001](#mod-014-f001) | [managed_read](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/usage/cli_reads.py#L349) | get_current_user; get_db |
| `API-0430` | `GET /me/agent-run-stats` | [MOD-015-F001](#mod-015-f001) | [get_my_stats](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/activity/routes.py#L93) | get_current_user; get_db; get_stats_service |
| `API-0431` | `GET /admin/agent-run-stats` | [MOD-015-F001](#mod-015-f001) | [get_admin_stats](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/activity/routes.py#L124) | get_access_control; get_current_user; get_db; get_stats_service; permission; ACTIVITY_READ_ALL |
| `API-0432` | `GET /me/agent-invocations` | [MOD-015-F001](#mod-015-f001) | [get_my_invocations](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/activity/routes.py#L352) | get_activity_service; get_current_user; get_db |
| `API-0433` | `GET /admin/agent-invocations` | [MOD-015-F001](#mod-015-f001) | [get_admin_invocations](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/activity/routes.py#L430) | get_access_control; get_activity_service; get_current_user; get_db; permission; ACTIVITY_READ_ALL |
| `API-0434` | `GET /me/agent-invocations/chain/{correlation_id}` | [MOD-015-F001](#mod-015-f001) | [get_my_invocation_chain](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/activity/routes.py#L493) | get_activity_service; get_current_user; get_db |
| `API-0435` | `GET /me/agent-invocations/tasks` | [MOD-015-F001](#mod-015-f001) | [get_my_task_invocations](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/activity/routes.py#L528) | get_current_user; get_db |
| `API-0436` | `GET /me/agent-invocations/{invocation_id}` | [MOD-015-F001](#mod-015-f001) | [get_my_invocation_detail](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/activity/routes.py#L550) | get_activity_service; get_current_user; get_db |
| `API-0437` | `GET /admin/agent-invocations/chain/{correlation_id}` | [MOD-015-F001](#mod-015-f001) | [get_admin_invocation_chain](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/activity/routes.py#L600) | get_access_control; get_activity_service; get_current_user; get_db; permission; ACTIVITY_READ_ALL |
| `API-0438` | `GET /admin/agent-invocations/{invocation_id}` | [MOD-015-F001](#mod-015-f001) | [get_admin_invocation_detail](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/activity/routes.py#L636) | get_access_control; get_activity_service; get_current_user; get_db; permission; ACTIVITY_READ_ALL |
| `API-0439` | `GET /me/agent-invocations/{invocation_id}/transcript` | [MOD-015-F001](#mod-015-f001) | [get_my_invocation_transcript](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/activity/routes.py#L722) | get_activity_service; get_current_user; get_db |
| `API-0440` | `GET /admin/agent-invocations/{invocation_id}/transcript` | [MOD-015-F001](#mod-015-f001) | [get_admin_invocation_transcript](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/activity/routes.py#L758) | get_access_control; get_activity_service; get_current_user; get_db; permission; ACTIVITY_READ_ALL |
| `API-0441` | `GET /activity/invocations/{invocation_id}/agent/ping` | [MOD-015-F002](#mod-015-f002) | [ping_invocation_agent](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/activity/routes.py#L843) | get_control_service; get_current_user; get_db |
| `API-0442` | `GET /activity/invocations/{invocation_id}/agent/state` | [MOD-015-F002](#mod-015-f002) | [get_invocation_agent_state](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/activity/routes.py#L863) | get_control_service; get_current_user; get_db |
| `API-0443` | `GET /activity/invocations/{invocation_id}/agent/events` | [MOD-015-F002](#mod-015-f002) | [stream_invocation_explanations](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/activity/routes.py#L883) | get_control_service; get_current_user |
| `API-0444` | `POST /activity/invocations/{invocation_id}/agent/{action}` | [MOD-015-F002](#mod-015-f002) | [command_invocation_agent](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/activity/routes.py#L915) | get_control_service; get_current_user; get_db |
| `API-0445` | `POST /api/agent-context/assets` | [MOD-017-F001](#mod-017-f001) | [register_asset](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/knowledge/routes.py#L93) | get_agent_context_db; get_current_user; get_db |
| `API-0446` | `GET /api/agent-context/assets` | [MOD-017-F001](#mod-017-f001) | [list_assets](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/knowledge/routes.py#L312) | get_agent_context_db; get_current_user; get_db |
| `API-0447` | `GET /api/agent-context/assets/{asset_id}` | [MOD-017-F001](#mod-017-f001) | [get_asset_detail](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/knowledge/routes.py#L402) | get_agent_context_db; get_current_user; get_db |
| `API-0448` | `DELETE /api/agent-context/assets/{asset_id}` | [MOD-017-F001](#mod-017-f001) | [delete_asset](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/knowledge/routes.py#L427) | get_agent_context_db; get_current_user |
| `API-0449` | `POST /api/agent-context/assets/{asset_id}/reindex` | [MOD-017-F001](#mod-017-f001) | [reindex_asset](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/knowledge/routes.py#L452) | get_agent_context_db; get_current_user |
| `API-0450` | `GET /api/agent-context/assets/{asset_id}/status` | [MOD-017-F001](#mod-017-f001) | [get_asset_status](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/knowledge/routes.py#L525) | get_agent_context_db; get_current_user; get_db |
| `API-0451` | `POST /api/agent-context/assets/bulk` | [MOD-017-F001](#mod-017-f001), [MOD-017-F002](#mod-017-f002) | [bulk_preview](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/knowledge/routes.py#L623) | get_agent_context_db; get_current_user; get_db |
| `API-0452` | `POST /api/agent-context/assets/bulk/commit` | [MOD-017-F001](#mod-017-f001), [MOD-017-F002](#mod-017-f002) | [bulk_commit](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/knowledge/routes.py#L779) | get_agent_context_db; get_current_user; get_db |
| `API-0453` | `POST /agent-context/assets/bulk/preview-json` | [MOD-017-F001](#mod-017-f001), [MOD-017-F002](#mod-017-f002) | [bulk_preview_json](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/knowledge/routes.py#L750) | get_agent_context_db; get_current_user; get_db |
| `API-0454` | `GET /api/agent-context/github/accessible-repos` | [MOD-017-F001](#mod-017-f001) | [get_accessible_repos](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/knowledge/github_repos.py#L49) | get_current_user; get_db |
| `API-0455` | `GET /features` | [MOD-023-F001](#mod-023-f001) | [get_features](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/features/routes.py#L59) | get_current_user |
| `API-0456` | `GET /gitlab/status` | [MOD-008-F001](#mod-008-f001) | [status](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/gitlab/routes.py#L61) | get_db; human |
| `API-0457` | `GET /gitlab/admin/status` | [MOD-008-F001](#mod-008-f001) | [admin_status](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/gitlab/routes.py#L66) | admin; get_db |
| `API-0458` | `POST /gitlab/admin/configure` | [MOD-008-F001](#mod-008-f001) | [configure](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/gitlab/routes.py#L71) | admin; get_current_user; get_db |
| `API-0459` | `GET /gitlab/admin/revalidate` | [MOD-008-F001](#mod-008-f001) | [revalidate](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/gitlab/routes.py#L87) | admin; get_db |
| `API-0460` | `POST /gitlab/connect` | [MOD-008-F001](#mod-008-f001) | [connect](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/gitlab/routes.py#L93) | get_current_user; get_db; human |
| `API-0461` | `POST /gitlab/disconnect` | [MOD-008-F001](#mod-008-f001) | [disconnect](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/gitlab/routes.py#L109) | get_current_user; get_db; human |
| `API-0462` | `GET /auth/gitlab-sso` | [MOD-008-F001](#mod-008-f001) | [gitlab_sso_redirect](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/gitlab_sso.py#L228) | get_current_user_context; get_db |
| `API-0463` | `GET /.well-known/jwks.json` | [MOD-008-F001](#mod-008-f001) | [jwks_endpoint](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/auth/gitlab_sso.py#L315) | See handler and middleware |
| `API-0464` | `GET /cli/{script_name}` | [MOD-024-F001](#mod-024-f001) | [download_cli_script](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/cli_download/routes.py#L167) | See handler and middleware |
| `API-0465` | `GET /me/cli-capabilities` | [MOD-024-F001](#mod-024-f001) | [get_my_cli_capabilities](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/cli_capabilities/routes.py#L63) | get_access_control; get_current_user |
| `API-0466` | `GET /me/cli-requests/{request_id}` | [MOD-024-F001](#mod-024-f001) | [get_my_cli_request](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/cli_capabilities/routes.py#L96) | get_access_control; get_current_user; get_db; permission; LOGS_READ |
| `API-0467` | `GET /orchestration/flows/{flow_id}/run-reports` | [MOD-018-F001](#mod-018-f001) | [get_flow_run_reports](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/routes.py#L144) | get_access_control; get_current_user; get_db; permission; USAGE_READ |
| `API-0468` | `POST /orchestration/flows` | [MOD-018-F002](#mod-018-f002) | [create_flow](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/routes.py#L384) | get_access_control; get_current_user; get_db; permission; PLAN_APPROVE |
| `API-0469` | `POST /orchestration/flows/{flow_id}/nodes/{node_id}/pull-request-recovery` | [MOD-018-F007](#mod-018-f007) | [recover_story_binding](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/routes.py#L552) | get_access_control; get_current_user; get_db; permission; PLAN_APPROVE |
| `API-0470` | `POST /orchestration/flows/{flow_id}/amendments` | [MOD-018-F007](#mod-018-f007) | [create_amendment](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/routes.py#L739) | get_access_control; get_current_user; get_db; permission; PLAN_APPROVE |
| `API-0471` | `GET /orchestration/flows/{flow_id}/plans` | [MOD-018-F001](#mod-018-f001) | [list_plans](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/routes.py#L816) | get_access_control; get_current_user; get_db; permission; PLAN_APPROVE |
| `API-0472` | `GET /orchestration/flows/{flow_id}/cost` | [MOD-018-F001](#mod-018-f001) | [get_flow_cost_route](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/routes.py#L973) | get_access_control; get_current_user; get_db; permission; USAGE_READ |
| `API-0473` | `GET /orchestration/flows` | [MOD-018-F001](#mod-018-f001) | [list_flows_route](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/routes.py#L1152) | get_access_control; get_current_user; get_db; permission; USAGE_READ |
| `API-0474` | `GET /orchestration/flows/{flow_id}` | [MOD-018-F001](#mod-018-f001) | [get_flow_graph](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/routes.py#L1435) | get_access_control; get_current_user; get_db; permission; USAGE_READ |
| `API-0475` | `GET /orchestration/flows/{flow_id}/execution` | [MOD-018-F001](#mod-018-f001) | [get_flow_execution](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/routes.py#L1838) | get_access_control; get_current_user; get_db; permission; USAGE_READ |
| `API-0476` | `POST /orchestration/flows/{flow_id}/continuation/preview` | [MOD-018-F004](#mod-018-f004) | [preview_existing_flow](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/continuation_routes.py#L71) | get_current_user; get_db; get_run_binding_resolver; permission; PLAN_APPROVE |
| `API-0477` | `POST /orchestration/flows/{flow_id}/continuation/accept` | [MOD-018-F004](#mod-018-f004) | [accept_existing_flow](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/continuation_routes.py#L84) | get_current_user; get_db; get_run_binding_resolver; permission; PLAN_APPROVE |
| `API-0478` | `POST /orchestration/flows/{flow_id}/execution` | [MOD-018-F003](#mod-018-f003) | [set_flow_execution](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/flow_controls.py#L33) | get_current_user; get_db; permission; PLAN_APPROVE |
| `API-0479` | `POST /orchestration/flows/{flow_id}/append/preview` | [MOD-018-F004](#mod-018-f004) | [preview_append](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/shared_amendment_routes.py#L56) | get_current_user; get_db; permission; PLAN_APPROVE |
| `API-0480` | `POST /orchestration/flows/{flow_id}/append/accept` | [MOD-018-F004](#mod-018-f004) | [accept_append](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/shared_amendment_routes.py#L68) | get_current_user; get_db; permission; PLAN_APPROVE |
| `API-0481` | `POST /orchestration/flows/{flow_id}/wave-dependencies/preview` | [MOD-018-F004](#mod-018-f004) | [preview_dependencies](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/shared_amendment_routes.py#L80) | get_current_user; get_db; permission; PLAN_APPROVE |
| `API-0482` | `POST /orchestration/flows/{flow_id}/wave-dependencies/accept` | [MOD-018-F004](#mod-018-f004) | [accept_dependencies](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/shared_amendment_routes.py#L92) | get_current_user; get_db; permission; PLAN_APPROVE |
| `API-0483` | `POST /orchestration/flows/{flow_id}/budget/preview` | [MOD-018-F005](#mod-018-f005) | [preview_budget](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/shared_budget_routes.py#L46) | get_current_user; get_db; permission; platform_admin; BUDGET_UPDATE; PLAN_APPROVE |
| `API-0484` | `POST /orchestration/flows/{flow_id}/budget/accept` | [MOD-018-F005](#mod-018-f005) | [accept_budget](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/shared_budget_routes.py#L62) | get_current_user; get_db; permission; platform_admin; BUDGET_UPDATE; PLAN_APPROVE |
| `API-0485` | `POST /orchestration/flows/{flow_id}/concurrency/preview` | [MOD-018-F005](#mod-018-f005) | [preview_concurrency](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/shared_concurrency_routes.py#L46) | get_current_user; get_db; permission; platform_admin; PLAN_APPROVE |
| `API-0486` | `POST /orchestration/flows/{flow_id}/concurrency/accept` | [MOD-018-F005](#mod-018-f005) | [accept_concurrency](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/shared_concurrency_routes.py#L59) | get_current_user; get_db; permission; platform_admin; PLAN_APPROVE |
| `API-0487` | `POST /orchestration/flows/{flow_id}/retry/preview` | [MOD-018-F005](#mod-018-f005) | [preview_retry](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/shared_retry_routes.py#L46) | get_current_user; get_db; permission; platform_admin; PLAN_APPROVE |
| `API-0488` | `POST /orchestration/flows/{flow_id}/retry/accept` | [MOD-018-F005](#mod-018-f005) | [accept_retry](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/shared_retry_routes.py#L59) | get_current_user; get_db; permission; platform_admin; PLAN_APPROVE |
| `API-0489` | `POST /orchestration/flows/{flow_id}/window/preview` | [MOD-018-F005](#mod-018-f005) | [preview_window](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/shared_window_routes.py#L46) | get_current_user; get_db; permission; platform_admin; PLAN_APPROVE |
| `API-0490` | `POST /orchestration/flows/{flow_id}/window/accept` | [MOD-018-F005](#mod-018-f005) | [accept_window](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/shared_window_routes.py#L59) | get_current_user; get_db; permission; platform_admin; PLAN_APPROVE |
| `API-0491` | `POST /orchestration/flows/{flow_id}/evaluation/preview` | [MOD-018-F006](#mod-018-f006) | [preview_contract](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/evaluation_acceptance_routes.py#L51) | get_current_user; get_db; permission; PLAN_APPROVE |
| `API-0492` | `POST /orchestration/flows/{flow_id}/evaluation/accept` | [MOD-018-F006](#mod-018-f006) | [accept_contract](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/evaluation_acceptance_routes.py#L63) | get_current_user; get_db; permission; PLAN_APPROVE |
| `API-0493` | `POST /orchestration/flows/{flow_id}/evaluation-waiver/preview` | [MOD-018-F006](#mod-018-f006) | [preview](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/evaluation_waiver_routes.py#L43) | get_current_user; get_db; permission; PLAN_APPROVE |
| `API-0494` | `POST /orchestration/flows/{flow_id}/evaluation-waiver/accept` | [MOD-018-F006](#mod-018-f006) | [accept](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/evaluation_waiver_routes.py#L50) | get_current_user; get_db; permission; PLAN_APPROVE |
| `API-0495` | `POST /orchestration/flows/{flow_id}/draft/preview` | [MOD-018-F002](#mod-018-f002) | [preview_revision](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/draft_revision_routes.py#L44) | get_access_control; get_current_user; get_db; permission; PLAN_APPROVE |
| `API-0496` | `POST /orchestration/flows/{flow_id}/draft/revise` | [MOD-018-F002](#mod-018-f002) | [save_revision](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/draft_revision_routes.py#L56) | get_access_control; get_current_user; get_db; permission; PLAN_APPROVE |
| `API-0497` | `POST /orchestration/gates/{gate_id}/approve` | [MOD-018-F003](#mod-018-f003) | [approve_gate](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/controls.py#L609) | get_access_control; get_current_user; get_db |
| `API-0498` | `POST /orchestration/gates/{gate_id}/reject` | [MOD-018-F003](#mod-018-f003) | [reject_gate](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/controls.py#L644) | get_access_control; get_current_user; get_db |
| `API-0499` | `POST /orchestration/nodes/{node_id}/review-recovery/preview` | [MOD-018-F003](#mod-018-f003) | [preview_review_recovery](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/controls.py#L709) | get_access_control; get_current_user; get_db |
| `API-0500` | `POST /orchestration/nodes/{node_id}/review-recovery/accept` | [MOD-018-F003](#mod-018-f003) | [accept_review_recovery](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/controls.py#L720) | get_access_control; get_current_user; get_db |
| `API-0501` | `POST /orchestration/nodes/{node_id}/resume-continuation` | [MOD-018-F003](#mod-018-f003) | [resume_current_continuation](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/controls.py#L731) | get_access_control; get_current_user; get_db; permission; PLAN_APPROVE |
| `API-0502` | `GET /orchestration/nodes/{node_id}/recovery` | [MOD-018-F003](#mod-018-f003) | [read_node_recovery](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/controls.py#L765) | get_access_control; get_current_user; get_db; permission; USAGE_READ |
| `API-0503` | `POST /orchestration/nodes/{node_id}/resume` | [MOD-018-F003](#mod-018-f003) | [resume_node](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/controls.py#L780) | get_access_control; get_current_user; get_db; get_run_binding_resolver; permission; PLAN_APPROVE |
| `API-0504` | `GET /orchestration/flows/{flow_id}/decisions` | [MOD-018-F003](#mod-018-f003) | [list_flow_decisions](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/controls.py#L969) | get_access_control; get_current_user; get_db; permission; USAGE_READ |
| `API-0505` | `POST /orchestration/runs/{run_id}/pause` | [MOD-015-F002](#mod-015-f002) | [pause_run](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/controls.py#L1124) | get_current_user; get_db; get_run_control_service |
| `API-0506` | `POST /orchestration/runs/{run_id}/resume` | [MOD-015-F002](#mod-015-f002) | [resume_run](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/controls.py#L1136) | get_current_user; get_db; get_run_control_service |
| `API-0507` | `POST /orchestration/runs/{run_id}/steer` | [MOD-015-F002](#mod-015-f002) | [steer_run](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/controls.py#L1156) | get_current_user; get_db; get_run_control_service |
| `API-0508` | `POST /orchestration/runs/{run_id}/abort` | [MOD-015-F002](#mod-015-f002) | [abort_run](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/controls.py#L1168) | get_current_user; get_db; get_run_control_service |
| `API-0509` | `GET /orchestration/runs/{run_id}/ping` | [MOD-015-F002](#mod-015-f002) | [ping_run](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/controls.py#L1180) | get_current_user; get_db; get_run_control_service |
| `API-0510` | `GET /orchestration/runs/{run_id}/state` | [MOD-015-F002](#mod-015-f002) | [get_run_control_state](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/controls.py#L1195) | get_current_user; get_db; get_run_control_service |
| `API-0511` | `GET /orchestration/gates/{gate_id}/execution-preview` | [MOD-018-F003](#mod-018-f003) | [gate_execution_preview](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/controls.py#L1210) | get_access_control; get_current_user; get_db; permission; PLAN_APPROVE |
| `API-0512` | `POST /orchestration/flows/drafts` | [MOD-018-F002](#mod-018-f002) | [register_draft](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/draft_routes.py#L406) | get_access_control; get_current_user; get_db; get_run_binding_resolver; permission; PLAN_DRAFT |
| `API-0513` | `POST /orchestration/flows/{flow_id}/amendments/drafts` | [MOD-018-F002](#mod-018-f002) | [register_amendment](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/draft_routes.py#L625) | get_access_control; get_current_user; get_db; get_run_binding_resolver; permission; PLAN_DRAFT |
| `API-0514` | `POST /orchestration/flows/drafts/preview` | [MOD-018-F002](#mod-018-f002) | [preview_draft](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/draft_routes.py#L766) | get_access_control; get_current_user; permission; PLAN_DRAFT |
| `API-0515` | `POST /orchestration/intake/sessions` | [MOD-018-F002](#mod-018-f002) | [start_intake_session](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/intake_routes.py#L264) | _dispatcher; get_access_control; get_current_user; get_db; permission; PLAN_DRAFT |
| `API-0516` | `POST /orchestration/intake/sessions/{session_id}/turns` | [MOD-018-F002](#mod-018-f002) | [send_intake_turn](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/intake_routes.py#L356) | _dispatcher; _session_reader; get_access_control; get_current_user; permission; PLAN_DRAFT |
| `API-0517` | `GET /orchestration/intake/sessions/latest` | [MOD-018-F002](#mod-018-f002) | [latest_intake_session](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/intake_routes.py#L425) | _session_reader; get_access_control; get_current_user; permission; USAGE_READ |
| `API-0518` | `GET /orchestration/intake/sessions/{session_id}` | [MOD-018-F002](#mod-018-f002) | [get_intake_session](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/intake_routes.py#L457) | _session_reader; get_access_control; get_current_user; permission; USAGE_READ |
| `API-0519` | `POST /orchestration/intake/sessions/{session_id}/plan` | [MOD-018-F002](#mod-018-f002) | [plan_from_session](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/intake_routes.py#L658) | _session_reader; get_access_control; get_current_user; get_db; permission; PLAN_DRAFT |
| `API-0520` | `GET /chat/capabilities` | [MOD-016-F001](#mod-016-f001) | [capabilities](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/chat_history.py#L171) | [Depends(no_store)]; get_current_user; get_db; store |
| `API-0521` | `GET /chat/sessions` | [MOD-016-F001](#mod-016-f001) | [list_sessions](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/chat_history.py#L197) | [Depends(no_store)]; get_current_user; store |
| `API-0522` | `GET /chat/sessions/{session_id}` | [MOD-016-F001](#mod-016-f001) | [show_session](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/chat_history.py#L245) | [Depends(no_store)]; get_current_user; get_db; store |
| `API-0523` | `POST /chat/sessions` | [MOD-016-F001](#mod-016-f001) | [start](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/chat_tasks.py#L304) | [Depends(history.no_store)]; get_db; history.store |
| `API-0524` | `POST /chat/sessions/{session_id}/turns` | [MOD-016-F001](#mod-016-f001) | [resume](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/orchestration/chat_tasks.py#L310) | [Depends(history.no_store)]; get_db; history.store |

### superplane

| API ID | Method / path | Owning feature IDs | Handler / source | Declared dependency and permission hints |
| --- | --- | --- | --- | --- |
| `API-0525` | `POST /internal/controller/reconcile` | [MOD-026-F010](#mod-026-f010) | [reconcile](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/controller_management.py#L38) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; _authenticated_submitter; get_session |
| `API-0526` | `GET /api/v1/workspaces/{workspace_id}/bootstrap-observation` | [MOD-026-F002](#mod-026-f002) | [bootstrap_observation](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/bootstrap_observation.py#L21) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_session |
| `API-0527` | `POST /internal/controller/recovery/inventory` | [MOD-026-F010](#mod-026-f010) | [inventory](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/controller_recovery.py#L265) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; _authenticated_submitter |
| `API-0528` | `POST /internal/controller/recovery/observe` | [MOD-026-F010](#mod-026-f010) | [observe](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/controller_recovery.py#L300) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; _authenticated_submitter |
| `API-0529` | `POST /internal/controller/recovery/lifecycle` | [MOD-026-F010](#mod-026-f010) | [lifecycle](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/controller_recovery.py#L387) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; _authenticated_submitter |
| `API-0530` | `POST /internal/controller/recovery/account-creation` | [MOD-026-F010](#mod-026-f010) | [account_creation](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/controller_recovery.py#L406) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; _authenticated_submitter |
| `API-0531` | `POST /internal/controller/recovery/bootstrap` | [MOD-026-F010](#mod-026-f010) | [bootstrap](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/controller_recovery.py#L425) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; _authenticated_submitter |
| `API-0532` | `POST /internal/controller/recovery/settlement` | [MOD-026-F010](#mod-026-f010) | [settlement](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/controller_recovery.py#L444) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; _authenticated_submitter |
| `API-0533` | `POST /operation-approvals` | [MOD-026-F003](#mod-026-f003) | [request_approval](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/operation_approvals.py#L38) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)] |
| `API-0534` | `GET /operation-approvals/{approval_id}` | [MOD-026-F003](#mod-026-f003) | [get_approval](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/operation_approvals.py#L57) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)] |
| `API-0535` | `POST /operation-approvals/{approval_id}/decision` | [MOD-026-F003](#mod-026-f003) | [decide_approval](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/operation_approvals.py#L62) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)] |
| `API-0536` | `GET /workspaces/{workspace_id}/lifecycle-proposals` | [MOD-026-F002](#mod-026-f002) | [lifecycle_proposals](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/onboarding.py#L42) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0537` | `POST /workspaces/{workspace_id}/lifecycle-proposals/{artifact_id}/preview` | [MOD-026-F002](#mod-026-f002) | [preview_lifecycle_continuation](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/onboarding.py#L85) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0538` | `POST /workspaces/{workspace_id}/lifecycle-proposals/{artifact_id}/continue` | [MOD-026-F002](#mod-026-f002) | [continue_workspace_lifecycle](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/onboarding.py#L115) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0539` | `GET /capabilities` | [MOD-026-F002](#mod-026-f002) | [onboarding_capabilities](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/onboarding.py#L147) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org |
| `API-0540` | `POST /workspaces/preview` | [MOD-026-F002](#mod-026-f002) | [preview_workspace](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/onboarding.py#L179) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0541` | `POST /workspaces/adopt` | [MOD-026-F002](#mod-026-f002) | [adopt_workspace](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/onboarding.py#L197) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0542` | `GET /operations/by-idempotency/{idempotency_key}` | [MOD-026-F002](#mod-026-f002) | [recover_operation](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/onboarding.py#L263) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0543` | `GET /operations/{operation_id}` | [MOD-026-F002](#mod-026-f002) | [get_operation](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/onboarding.py#L275) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0544` | `POST /workspaces/{workspace_id}/retirement/preview` | [MOD-026-F006](#mod-026-f006) | [retirement_preview](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/retirement.py#L48) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0545` | `POST /workspaces/{workspace_id}/retirement` | [MOD-026-F006](#mod-026-f006) | [retirement_admission](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/retirement.py#L66) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0546` | `GET /readyz` | [MOD-026-F010](#mod-026-f010) | [readiness](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/health.py#L15) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_session |
| `API-0547` | `GET /health` | [MOD-026-F010](#mod-026-f010) | [health_check](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/health.py#L33) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)] |
| `API-0548` | `POST /auth/login` | [MOD-026-F009](#mod-026-f009) | [login](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/auth.py#L56) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_session |
| `API-0549` | `POST /auth/token` | [MOD-026-F009](#mod-026-f009) | [create_api_key](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/auth.py#L99) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0550` | `POST /auth/signup` | [MOD-026-F009](#mod-026-f009) | [signup](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/auth.py#L191) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_session |
| `API-0551` | `GET /orgs/current` | [MOD-026-F009](#mod-026-f009) | [get_current_organization](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/orgs.py#L52) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0552` | `PATCH /orgs/current` | [MOD-026-F009](#mod-026-f009) | [update_org_settings](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/orgs.py#L81) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0553` | `GET /orgs/current/sso` | [MOD-026-F009](#mod-026-f009) | [get_sso_config](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/orgs.py#L136) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0554` | `PATCH /orgs/current/sso` | [MOD-026-F009](#mod-026-f009) | [configure_sso](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/orgs.py#L172) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0555` | `DELETE /orgs/current/sso` | [MOD-026-F009](#mod-026-f009) | [disable_sso](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/orgs.py#L260) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0556` | `POST /workspaces` | [MOD-026-F002](#mod-026-f002) | [create_workspace](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/workspaces.py#L286) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0557` | `GET /workspaces` | [MOD-026-F002](#mod-026-f002) | [list_workspaces](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/workspaces.py#L444) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0558` | `GET /workspaces/{workspace_id}` | [MOD-026-F002](#mod-026-f002) | [get_workspace](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/workspaces.py#L506) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0559` | `GET /workspaces/{workspace_id}/lifecycle` | [MOD-026-F002](#mod-026-f002) | [get_workspace_lifecycle](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/workspaces.py#L538) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0560` | `DELETE /workspaces/{workspace_id}` | [MOD-026-F002](#mod-026-f002) | [delete_workspace](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/workspaces.py#L549) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0561` | `POST /workspaces/{workspace_id}/kubeconfig` | [MOD-026-F002](#mod-026-f002) | [generate_kubeconfig](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/workspaces.py#L656) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0562` | `POST /workspaces/{workspace_id}/provider-connections` | [MOD-026-F004](#mod-026-f004) | [register_connection](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/provider_connections.py#L500) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0563` | `GET /workspaces/{workspace_id}/provider-connections/{connection_id}` | [MOD-026-F004](#mod-026-f004) | [get_connection](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/provider_connections.py#L582) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0564` | `POST /workspaces/{workspace_id}/provider-connections/{connection_id}/validation` | [MOD-026-F004](#mod-026-f004) | [record_validation](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/provider_connections.py#L608) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0565` | `POST /workspaces/{workspace_id}/provider-connections/{connection_id}/rotation` | [MOD-026-F004](#mod-026-f004) | [rotate_connection](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/provider_connections.py#L682) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0566` | `DELETE /workspaces/{workspace_id}/provider-connections/{connection_id}` | [MOD-026-F004](#mod-026-f004) | [disable_connection](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/provider_connections.py#L784) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0567` | `GET /workspaces/{workspace_id}/nodes` | [MOD-026-F005](#mod-026-f005) | [list_workspace_nodes](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L68) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0568` | `POST /workspaces/{workspace_id}/deployments/preview` | [MOD-026-F005](#mod-026-f005) | [preview_deployment](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L93) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0569` | `GET /workspaces/{workspace_id}/deployment-profiles` | [MOD-026-F005](#mod-026-f005) | [deployment_profiles](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L104) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0570` | `POST /workspaces/{workspace_id}/deployments` | [MOD-026-F005](#mod-026-f005) | [create_deployment](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L116) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0571` | `GET /workspaces/{workspace_id}/deployments` | [MOD-026-F005](#mod-026-f005) | [list_deployments](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L132) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0572` | `POST /workspaces/{workspace_id}/deployments/{dep_id}/teardown-preview` | [MOD-026-F005](#mod-026-f005) | [preview_deployment_teardown](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L177) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0573` | `DELETE /workspaces/{workspace_id}/deployments/{dep_id}` | [MOD-026-F005](#mod-026-f005) | [delete_deployment](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L192) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0574` | `GET /workspaces/{workspace_id}/batch-profiles` | [MOD-026-F005](#mod-026-f005) | [batch_profiles](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L227) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0575` | `POST /workspaces/{workspace_id}/batch-jobs/preview` | [MOD-026-F005](#mod-026-f005) | [preview_batch](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L241) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0576` | `POST /workspaces/{workspace_id}/batch-jobs` | [MOD-026-F005](#mod-026-f005) | [create_batch](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L252) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0577` | `GET /workspaces/{workspace_id}/batch-jobs` | [MOD-026-F005](#mod-026-f005) | [list_batch](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L265) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0578` | `GET /workspaces/{workspace_id}/batch-jobs/{job_id}` | [MOD-026-F005](#mod-026-f005) | [get_batch](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L299) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0579` | `POST /workspaces/{workspace_id}/batch-jobs/{job_id}/teardown-preview` | [MOD-026-F005](#mod-026-f005) | [preview_batch_teardown](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L319) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0580` | `DELETE /workspaces/{workspace_id}/batch-jobs/{job_id}` | [MOD-026-F005](#mod-026-f005) | [delete_batch](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L334) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0581` | `POST /workspaces/{workspace_id}/batch-jobs/{job_id}/cancellation` | [MOD-026-F005](#mod-026-f005) | [cancel_batch](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L350) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0582` | `POST /workspaces/{workspace_id}/deployments/{dep_id}/cancellation` | [MOD-026-F005](#mod-026-f005) | [cancel_deployment](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L367) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0583` | `GET /workspaces/{workspace_id}/batch-jobs/{job_id}/observation` | [MOD-026-F005](#mod-026-f005) | [observe_batch](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L383) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0584` | `GET /workspaces/{workspace_id}/deployments/{dep_id}/observation` | [MOD-026-F005](#mod-026-f005) | [observe_serving](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L411) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0585` | `GET /workspaces/{workspace_id}/batch-jobs/{job_id}/result` | [MOD-026-F005](#mod-026-f005) | [get_batch_result](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L439) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0586` | `GET /workspaces/{workspace_id}/batch-jobs/{job_id}/accounting` | [MOD-026-F005](#mod-026-f005) | [batch_accounting](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L454) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0587` | `GET /workspaces/{workspace_id}/deployments/{dep_id}/accounting` | [MOD-026-F005](#mod-026-f005) | [serving_accounting](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/proxy.py#L469) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0588` | `GET /workspaces/{workspace_id}/cost` | [MOD-026-F007](#mod-026-f007) | [get_cost](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/cost.py#L38) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0589` | `GET /workspaces/{workspace_id}/budget` | [MOD-026-F007](#mod-026-f007) | [get_budget_status](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/cost.py#L69) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0590` | `GET /orgs/cost` | [MOD-026-F007](#mod-026-f007) | [get_org_cost](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/cost.py#L88) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0591` | `POST /internal/cost-reconcile` | [MOD-026-F007](#mod-026-f007) | [trigger_cost_reconcile](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/cost.py#L109) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_session; verify_internal_token |
| `API-0592` | `POST /internal/heartbeat` | [MOD-026-F010](#mod-026-f010) | [ingest_heartbeat](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/heartbeat.py#L58) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_session; verify_internal_token |
| `API-0593` | `POST /internal/observations` | [MOD-026-F010](#mod-026-f010) | [submit_observation](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/heartbeat.py#L226) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_session |
| `API-0594` | `POST /internal/observations/leases` | [MOD-026-F010](#mod-026-f010) | [acquire_lease](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/heartbeat.py#L277) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; _authenticated_submitter; get_session |
| `API-0595` | `POST /internal/observations/leases/release` | [MOD-026-F010](#mod-026-f010) | [release_lease](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/heartbeat.py#L323) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; _authenticated_submitter; get_session |
| `API-0596` | `GET /internal/observations/clusters` | [MOD-026-F010](#mod-026-f010) | [list_observable_clusters](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/heartbeat.py#L395) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; _authenticated_submitter; get_session |
| `API-0597` | `GET /internal/observations/{cluster_id}/cost-history` | [MOD-026-F010](#mod-026-f010) | [read_cost_history](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/heartbeat.py#L435) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; _authenticated_submitter; get_session |
| `API-0598` | `POST /internal/observations/{cluster_id}/events` | [MOD-026-F010](#mod-026-f010) | [record_cluster_event](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/heartbeat.py#L467) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; _authenticated_submitter; get_session |
| `API-0599` | `GET /internal/observations/{cluster_id}` | [MOD-026-F010](#mod-026-f010) | [read_observation](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/heartbeat.py#L501) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; _authenticated_submitter; get_session |
| `API-0600` | `GET /api/v1/research/cli-support` | [MOD-026-F008](#mod-026-f008) | [cli_support](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/research.py#L70) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org |
| `API-0601` | `GET /api/v1/research/findings` | [MOD-026-F008](#mod-026-f008) | [list_findings](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/research.py#L180) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0602` | `GET /api/v1/research/findings/{finding_id}` | [MOD-026-F008](#mod-026-f008) | [get_finding](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/research.py#L253) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0603` | `POST /api/v1/research/scan` | [MOD-026-F008](#mod-026-f008) | [trigger_scan](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/research.py#L277) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0604` | `GET /api/v1/research/stats` | [MOD-026-F008](#mod-026-f008) | [scanner_stats](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/research.py#L322) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0605` | `GET /api/v1/research/sources` | [MOD-026-F008](#mod-026-f008) | [list_sources](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/research.py#L336) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org |
| `API-0606` | `GET /api/v1/research/proposals` | [MOD-026-F008](#mod-026-f008) | [list_proposals](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/research.py#L369) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0607` | `GET /api/v1/research/proposals/stats` | [MOD-026-F008](#mod-026-f008) | [proposal_stats](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/research.py#L418) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0608` | `GET /api/v1/research/proposals/{proposal_id}` | [MOD-026-F008](#mod-026-f008) | [get_proposal](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/research.py#L463) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0609` | `POST /api/v1/research/proposals` | [MOD-026-F008](#mod-026-f008) | [create_proposal](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/research.py#L486) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0610` | `POST /api/v1/research/proposals/generate` | [MOD-026-F008](#mod-026-f008) | [generate_proposals_endpoint](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/research.py#L616) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0611` | `PATCH /api/v1/research/proposals/{proposal_id}/approve` | [MOD-026-F008](#mod-026-f008) | [approve_proposal](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/research.py#L655) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_user_context; get_session |
| `API-0612` | `PATCH /api/v1/research/proposals/{proposal_id}/reject` | [MOD-026-F008](#mod-026-f008) | [reject_proposal](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/research.py#L722) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_user_context; get_session |
| `API-0613` | `PATCH /workspaces/{workspace_id}/quota` | [MOD-026-F007](#mod-026-f007) | [set_workspace_quota](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/quota.py#L47) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0614` | `GET /workspaces/{workspace_id}/quota` | [MOD-026-F007](#mod-026-f007) | [get_workspace_quota](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/quota.py#L117) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0615` | `PATCH /orgs/current/quota` | [MOD-026-F007](#mod-026-f007) | [set_org_quota](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/quota.py#L147) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0616` | `GET /orgs/current/quota` | [MOD-026-F007](#mod-026-f007) | [get_org_quota](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/quota.py#L211) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0617` | `GET /events` | [MOD-026-F010](#mod-026-f010) | [list_events](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/events.py#L26) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0618` | `GET /events/{event_id}` | [MOD-026-F010](#mod-026-f010) | [get_event](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/events.py#L112) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0619` | `GET /events/workspaces/{workspace_id}` | [MOD-026-F010](#mod-026-f010) | [workspace_events](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/events.py#L136) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0620` | `POST /accounts` | [MOD-026-F004](#mod-026-f004) | [register_account](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/accounts.py#L118) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0621` | `GET /accounts` | [MOD-026-F004](#mod-026-f004) | [list_accounts](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/accounts.py#L173) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0622` | `DELETE /accounts/{account_id}` | [MOD-026-F004](#mod-026-f004) | [delete_account](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/accounts.py#L191) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0623` | `POST /vault/credentials` | [MOD-026-F004](#mod-026-f004) | [register_credential](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/accounts.py#L251) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0624` | `GET /vault/credentials` | [MOD-026-F004](#mod-026-f004) | [list_credentials](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/accounts.py#L302) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0625` | `DELETE /vault/credentials/{credential_id}` | [MOD-026-F004](#mod-026-f004) | [delete_credential](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/accounts.py#L323) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0626` | `POST /users/invite` | [MOD-026-F009](#mod-026-f009) | [invite_user](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/users.py#L60) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session; require_role('org-admin' |
| `API-0627` | `GET /users` | [MOD-026-F009](#mod-026-f009) | [list_users](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/users.py#L131) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session |
| `API-0628` | `PATCH /users/{user_id}/role` | [MOD-026-F009](#mod-026-f009) | [update_user_role](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/users.py#L161) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session; require_role('org-admin' |
| `API-0629` | `DELETE /users/{user_id}` | [MOD-026-F009](#mod-026-f009) | [delete_user](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/users.py#L227) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_current_user_context; get_session; require_role('org-admin' |
| `API-0630` | `PATCH /internal/clusters/{cluster_id}/resources` | [MOD-026-F010](#mod-026-f010) | [update_cluster_resources](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/internal.py#L71) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_session; verify_internal_token |
| `API-0631` | `POST /internal/vault-sync/trigger` | [MOD-026-F010](#mod-026-f010) | [trigger_vault_sync](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/internal.py#L141) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; verify_internal_token |
| `API-0632` | `POST /internal/workspaces/{workspace_id}/reconcile` | [MOD-026-F010](#mod-026-f010) | [reconcile_workspace](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/internal.py#L179) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; verify_internal_token |
| `API-0633` | `GET /internal/installation` | [MOD-026-F010](#mod-026-f010) | [installation_readiness](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/installation.py#L19) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; _authenticated_submitter |
| `API-0634` | `GET /internal/installation/workspaces/{workspace_id}/credential-evidence/{connection_id}` | [MOD-026-F010](#mod-026-f010) | [installation_credential_evidence](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/installation.py#L48) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; get_current_org; get_session; RENEW_CREDENTIAL |
| `API-0635` | `POST /internal/provider-operations` | [MOD-026-F010](#mod-026-f010) | [record_provider_handle](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/provider_handles.py#L266) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; _authenticated_submitter; get_session |
| `API-0636` | `POST /internal/provider-operations/{idempotency_key}/conclude` | [MOD-026-F010](#mod-026-f010) | [conclude_provider_operation](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/provider_handles.py#L319) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; _authenticated_submitter; get_session |
| `API-0637` | `GET /internal/provider-operations` | [MOD-026-F010](#mod-026-f010) | [list_provider_operations](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/provider_handles.py#L371) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; _authenticated_submitter; get_session |
| `API-0638` | `POST /internal/provider-operations/allocations/{allocation_id}/release-assessment` | [MOD-026-F010](#mod-026-f010) | [assess_allocation_release](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/provider_handles.py#L436) | [Depends(enforce_domain_authorization), Depends(enforce_management_surface)]; _authenticated_submitter; get_session |

### agent-context

| API ID | Method / path | Owning feature IDs | Handler / source | Declared dependency and permission hints |
| --- | --- | --- | --- | --- |
| `API-0639` | `GET /tools` | [MOD-027-F001](#mod-027-f001) | [list_tools](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/door/server.py#L453) | See handler and middleware |
| `API-0640` | `GET /health` | [MOD-027-F001](#mod-027-f001) | [health_check](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/door/server.py#L459) | See handler and middleware |
| `API-0641` | `GET /ready` | [MOD-027-F001](#mod-027-f001) | [readiness_check](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/door/server.py#L474) | See handler and middleware |
| `API-0642` | `POST /call` | [MOD-027-F001](#mod-027-f001) | [call_tool](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/door/server.py#L526) | See handler and middleware |

### skypilot-sidecar

| API ID | Method / path | Owning feature IDs | Handler / source | Declared dependency and permission hints |
| --- | --- | --- | --- | --- |
| `API-0643` | `GET /{path:path}` | [MOD-026-F011](#mod-026-f011) | [forward](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/skypilot_proxy.py#L27) | See handler and middleware |
| `API-0644` | `POST /{path:path}` | [MOD-026-F011](#mod-026-f011) | [forward](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/skypilot_proxy.py#L27) | See handler and middleware |
| `API-0645` | `PUT /{path:path}` | [MOD-026-F011](#mod-026-f011) | [forward](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/skypilot_proxy.py#L27) | See handler and middleware |
| `API-0646` | `DELETE /{path:path}` | [MOD-026-F011](#mod-026-f011) | [forward](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/skypilot_proxy.py#L27) | See handler and middleware |
| `API-0647` | `PATCH /{path:path}` | [MOD-026-F011](#mod-026-f011) | [forward](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/skypilot_proxy.py#L27) | See handler and middleware |

## Edge routes and other protocol entries

These 27 entries describe proxy bindings, Lambda ingress, WebSocket dispatch and Context MCP tools; proxy bindings are not additional independent backend features. In particular, public Task submission and GitHub OAuth are Lambda routes outside the FastAPI count.

| Transport | Path / action | Feature IDs | Scope / source |
| --- | --- | --- | --- |
| HTTP ANY edge proxy | `/` | [MOD-022-F001](#mod-022-f001), [MOD-021-F001](#mod-021-f001) | Forwards to Gateway; do not double-count underlying FastAPI endpoints. IAM and human ingress use different route/auth bindings.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/infra/modules/api-gateway/main.tf) |
| HTTP ANY edge proxy | `/{proxy+}` | [MOD-022-F001](#mod-022-f001), [MOD-021-F001](#mod-021-f001) | Forwards to Gateway; do not double-count underlying FastAPI endpoints. IAM and human ingress use different route/auth bindings.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/infra/modules/api-gateway/main.tf) |
| HTTP ANY edge proxy | `/agent` | [MOD-022-F001](#mod-022-f001), [MOD-021-F001](#mod-021-f001) | Forwards to Gateway; do not double-count underlying FastAPI endpoints. IAM and human ingress use different route/auth bindings.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/infra/modules/api-gateway/main.tf) |
| HTTP ANY edge proxy | `/agent/{proxy+}` | [MOD-022-F001](#mod-022-f001), [MOD-021-F001](#mod-021-f001) | Forwards to Gateway; do not double-count underlying FastAPI endpoints. IAM and human ingress use different route/auth bindings.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/infra/modules/api-gateway/main.tf) |
| HTTP ANY edge proxy | `/internal/{proxy+}` | [MOD-022-F001](#mod-022-f001), [MOD-021-F001](#mod-021-f001) | Forwards to Gateway; do not double-count underlying FastAPI endpoints. IAM and human ingress use different route/auth bindings.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/infra/modules/api-gateway/main.tf) |
| HTTP GET infrastructure placeholder | `/status` | [MOD-025-F001](#mod-025-f001) | MOCK awaiting-backend response only when ALB is absent; not health acceptance.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/infra/modules/api-gateway/main.tf) |
| HTTP POST Lambda | `/v1/tasks` | [MOD-019-F001](#mod-019-f001), [MOD-019-F002](#mod-019-f002), [MOD-020-F001](#mod-020-f001) | Explicit API Gateway route, conditional on enable_task_api_route; Lambda admission independently gated. Other methods fall through to Gateway.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/webhook-ingress/lambda/task_api/handler.py) |
| HTTP Lambda dispatch | `/auth/github/start` | [MOD-005-F002](#mod-005-f002), [MOD-005-F003](#mod-005-f003) | Conditional /auth/github/{proxy+} edge route; handler dispatches by suffix and handles OPTIONS. Provider OAuth/token exchange is outside FastAPI.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/lambda/github-auth-broker/handler.py) |
| HTTP Lambda dispatch | `/auth/github/callback` | [MOD-005-F002](#mod-005-f002), [MOD-005-F003](#mod-005-f003) | Conditional /auth/github/{proxy+} edge route; handler dispatches by suffix and handles OPTIONS. Provider OAuth/token exchange is outside FastAPI.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/lambda/github-auth-broker/handler.py) |
| HTTP Lambda dispatch | `/auth/github/exchange` | [MOD-005-F002](#mod-005-f002), [MOD-005-F003](#mod-005-f003) | Conditional /auth/github/{proxy+} edge route; handler dispatches by suffix and handles OPTIONS. Provider OAuth/token exchange is outside FastAPI.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/lambda/github-auth-broker/handler.py) |
| HTTP POST webhook | `/github` | [MOD-006-F003](#mod-006-f003), [MOD-006-F004](#mod-006-f004) | HMAC verification in Lambda.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/webhook-ingress/infra/routes.tf) |
| HTTP POST webhook | `/gitlab` | [MOD-008-F001](#mod-008-f001), [MOD-008-F002](#mod-008-f002), [MOD-008-F003](#mod-008-f003) | Conditional provider ingress; verify provider token inside handler.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/webhook-ingress/infra/gitlab_apigw.tf) |
| HTTP POST trigger | `/agent/trigger` | [MOD-021-F001](#mod-021-f001), [MOD-021-F002](#mod-021-f002) | AWS_IAM/SigV4-authenticated agent spawn.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/webhook-ingress/infra/routes.tf) |
| WebSocket route | `$connect` | [MOD-016-F001](#mod-016-f001) | Cognito connect authorization; subsequent message identity restored by ingest handler.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/infra/modules/api-gateway-ws/main.tf) |
| WebSocket route | `$disconnect` | [MOD-016-F001](#mod-016-f001) | Cognito connect authorization; subsequent message identity restored by ingest handler.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/infra/modules/api-gateway-ws/main.tf) |
| WebSocket route | `sendMessage` | [MOD-016-F001](#mod-016-f001) | Cognito connect authorization; subsequent message identity restored by ingest handler.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/infra/modules/api-gateway-ws/main.tf) |
| WebSocket route | `$default` | [MOD-016-F001](#mod-016-f001) | Cognito connect authorization; subsequent message identity restored by ingest handler.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/infra/modules/api-gateway-ws/main.tf) |
| WebSocket message action | `create-session` | [MOD-016-F001](#mod-016-f001) | Dispatch action within message transport; not a separate HTTP endpoint.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/gateway/lambdas/ingest/handler.py) |
| WebSocket message action | `upload-token` | [MOD-016-F001](#mod-016-f001) | Dispatch action within message transport; not a separate HTTP endpoint.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/gateway/lambdas/ingest/handler.py) |
| WebSocket message action | `upload-complete` | [MOD-016-F001](#mod-016-f001) | Dispatch action within message transport; not a separate HTTP endpoint.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/gateway/lambdas/ingest/handler.py) |
| MCP tool mounted under /mcp | `search` | [MOD-027-F001](#mod-027-f001) | Tool-level protocol entry; live acceptance differs by backend/ACL fixtures.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/door/mcp_app.py) |
| MCP tool mounted under /mcp | `understand` | [MOD-027-F001](#mod-027-f001) | Tool-level protocol entry; live acceptance differs by backend/ACL fixtures.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/door/mcp_app.py) |
| MCP tool mounted under /mcp | `impact` | [MOD-027-F001](#mod-027-f001) | Tool-level protocol entry; live acceptance differs by backend/ACL fixtures.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/door/mcp_app.py) |
| MCP tool mounted under /mcp | `browse` | [MOD-027-F001](#mod-027-f001) | Tool-level protocol entry; live acceptance differs by backend/ACL fixtures.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/door/mcp_app.py) |
| MCP tool mounted under /mcp | `remember` | [MOD-027-F001](#mod-027-f001) | Tool-level protocol entry; live acceptance differs by backend/ACL fixtures.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/door/mcp_app.py) |
| MCP tool mounted under /mcp | `experience` | [MOD-027-F001](#mod-027-f001) | Tool-level protocol entry; live acceptance differs by backend/ACL fixtures.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/door/mcp_app.py) |
| MCP tool mounted under /mcp | `secure` | [MOD-027-F001](#mod-027-f001) | Tool-level protocol entry; live acceptance differs by backend/ACL fixtures.; [source](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/door/mcp_app.py) |

## Unmounted / obsolete declarations

These 14 declarations are excluded from the mounted HTTP count. The older Context asset/repository routers are superseded by Gateway-native mounts; richer Gateway health handlers are not mounted by `create_app`; Superplane retirement-access routes explicitly remain dormant. Paths below are declaration suffixes, not assertions of reachable public endpoints. `UNIT_MODULES` also names `src.pool.routes`, a missing file at this revision; actual `/admin/pool` routes are included above.

| Method / declaration suffix | Handler / source |
| --- | --- |
| `POST (empty suffix)` | [register_asset](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/agent_context/api/assets_router.py#L116) |
| `GET (empty suffix)` | [list_assets](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/agent_context/api/assets_router.py#L294) |
| `GET /{asset_id}` | [get_asset_detail](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/agent_context/api/assets_router.py#L379) |
| `DELETE /{asset_id}` | [delete_asset](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/agent_context/api/assets_router.py#L401) |
| `POST /{asset_id}/reindex` | [reindex_asset](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/agent_context/api/assets_router.py#L426) |
| `GET /{asset_id}/status` | [get_asset_status](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/agent_context/api/assets_router.py#L474) |
| `POST /bulk` | [bulk_preview](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/agent_context/api/assets_router.py#L583) |
| `POST /bulk/commit` | [bulk_commit](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/agent_context/api/assets_router.py#L709) |
| `GET /accessible-repos` | [get_accessible_repos](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/agent_context/api/github_repos.py#L58) |
| `GET /health` | [health_check](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/health.py#L148) |
| `GET /ready` | [readiness_check](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/health.py#L160) |
| `GET /health/detailed` | [detailed_health_check](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/src/admin/health.py#L174) |
| `POST /workspaces/{workspace_id}/retirement/access/preview` | [retirement_access_preview](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/retirement_access.py#L20) |
| `POST /workspaces/{workspace_id}/retirement/access` | [retirement_access_admission](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/app/routers/retirement_access.py#L40) |

## Evidence catalogue

References below are pinned to the audited source revision. Directories indicate related suite/source material, not a verified per-scenario executable. Component tests, contract checks, acceptance drivers, manifests and product-source references are deliberately not all called E2E tests.

| Evidence ID | Source | Kind |
| --- | --- | --- |
| <a id="ev-001"></a>`EV-001` | [modules/gateway/frontend/src/__tests__/pages/settings/AgentModels.test.tsx](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/__tests__/pages/settings/AgentModels.test.tsx) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-002"></a>`EV-002` | [tests/e2e/cli_uplift/remote/story_reads.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-003"></a>`EV-003` | [modules/gateway/tests/admin/persona_models/test_model_catalogue.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_model_catalogue.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-004"></a>`EV-004` | [modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-005"></a>`EV-005` | [modules/gateway/tests/admin/persona_models/test_pmm02_postgres_concurrency.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_pmm02_postgres_concurrency.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-006"></a>`EV-006` | [modules/gateway/tests/admin/persona_models/test_principal_restrictions.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_principal_restrictions.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-007"></a>`EV-007` | [modules/gateway/frontend/src/__tests__/pages/settings/PlatformPersonaDefaults.test.tsx](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/__tests__/pages/settings/PlatformPersonaDefaults.test.tsx) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-008"></a>`EV-008` | [modules/gateway/tests/admin/persona_models/test_persona_platform_defaults.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_platform_defaults.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-009"></a>`EV-009` | [modules/gateway/tests/admin/persona_models/test_posture_authz.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_posture_authz.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-010"></a>`EV-010` | [modules/gateway/tests/admin/persona_models/test_default_promotion.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_default_promotion.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-011"></a>`EV-011` | [platform/evals/persona-model-rollout/rollout_gate.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/evals/persona-model-rollout/rollout_gate.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-012"></a>`EV-012` | [modules/gateway/tests/internal/test_persona_model_probe_routes.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/internal/test_persona_model_probe_routes.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-013"></a>`EV-013` | [modules/gateway/tests/admin/persona_models/test_dispatch_selection.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_dispatch_selection.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-014"></a>`EV-014` | [modules/gateway/tests/admin/persona_models/test_task_policy_ui_routes.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_task_policy_ui_routes.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-015"></a>`EV-015` | [modules/gateway/frontend/src/components/org/AgentTaskBudget.tsx](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/components/org/AgentTaskBudget.tsx) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-016"></a>`EV-016` | [modules/gateway/frontend/src/__tests__/pages/admin/Organizations.test.tsx](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/__tests__/pages/admin/Organizations.test.tsx) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-017"></a>`EV-017` | [tests/e2e/cli_uplift/remote/hierarchy_lifecycle.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/hierarchy_lifecycle.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-018"></a>`EV-018` | [modules/gateway/tests/admin/identity/test_organizations.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/identity/test_organizations.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-019"></a>`EV-019` | [modules/gateway/tests/admin/test_hierarchy_postgres.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/test_hierarchy_postgres.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-020"></a>`EV-020` | [modules/gateway/tests/admin/test_team_membership_claims_postgres.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/test_team_membership_claims_postgres.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-021"></a>`EV-021` | [modules/gateway/tests/admin/test_org_member_add.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/test_org_member_add.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-022"></a>`EV-022` | [tests/e2e/cli_uplift/remote/hierarchy_role.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/hierarchy_role.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-023"></a>`EV-023` | [modules/gateway/tests/admin/identity/test_github_enrollment.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/identity/test_github_enrollment.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-024"></a>`EV-024` | [modules/gateway/tests/budget/test_cross_org_person_budget.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/budget/test_cross_org_person_budget.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-025"></a>`EV-025` | [modules/gateway/tests/admin/test_org_github_connection_lifecycle.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/test_org_github_connection_lifecycle.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-026"></a>`EV-026` | [modules/gateway/tests/admin/tenants/test_tenant_org_links.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/tenants/test_tenant_org_links.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-027"></a>`EV-027` | [modules/gateway/frontend/src/__tests__/components/ServiceIdentityList.test.tsx](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/__tests__/components/ServiceIdentityList.test.tsx) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-028"></a>`EV-028` | [tests/e2e/cli_uplift/remote/machine_lifecycle.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/machine_lifecycle.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-029"></a>`EV-029` | [modules/gateway/tests/admin/persona_models/test_task_policy_routes.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_task_policy_routes.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-030"></a>`EV-030` | [modules/gateway/tests/admin/persona_models/test_human_task_policy.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_human_task_policy.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-031"></a>`EV-031` | [modules/gateway/tests/admin/identity](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/identity) | Directory / related suite material |
| <a id="ev-032"></a>`EV-032` | [tests/e2e/cli_uplift/preflight.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/preflight.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-033"></a>`EV-033` | [modules/gateway/tests/e2e/test_admin_stories.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/e2e/test_admin_stories.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-034"></a>`EV-034` | [modules/gateway/tests/admin](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin) | Directory / related suite material |
| <a id="ev-035"></a>`EV-035` | [tests/e2e/chat/test_auth.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/chat/test_auth.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-036"></a>`EV-036` | [tests/e2e/cli_uplift/remote/install_auth.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/install_auth.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-037"></a>`EV-037` | [modules/gateway/tests/e2e/test_authentication_stories.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/e2e/test_authentication_stories.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-038"></a>`EV-038` | [tests/e2e/cli_uplift/stages.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/stages.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-039"></a>`EV-039` | [tests/e2e/cli_uplift/remote/dispatcher.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/dispatcher.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-040"></a>`EV-040` | [tests/e2e/cli_uplift/remote/tenant_isolation.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/tenant_isolation.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-041"></a>`EV-041` | [tests/e2e/new_ui/test_coexistence.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/new_ui/test_coexistence.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-042"></a>`EV-042` | [modules/gateway/tests/admin/test_onboarding_routes.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/test_onboarding_routes.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-043"></a>`EV-043` | [modules/gateway/tests/auth](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/auth) | Directory / related suite material |
| <a id="ev-044"></a>`EV-044` | [modules/gateway/frontend/tests/e2e/connections.spec.ts](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/tests/e2e/connections.spec.ts) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-045"></a>`EV-045` | [modules/gateway/frontend/src/__tests__/pages/settings/Connections.test.tsx](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/__tests__/pages/settings/Connections.test.tsx) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-046"></a>`EV-046` | [tests/e2e/cli_uplift/remote/vault_lifecycle.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/vault_lifecycle.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-047"></a>`EV-047` | [modules/gateway/frontend/src/__tests__/pages/settings/ConnectAws.test.tsx](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/__tests__/pages/settings/ConnectAws.test.tsx) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-048"></a>`EV-048` | [platform/evals/bedrock-routing/run-eval.sh](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/evals/bedrock-routing/run-eval.sh) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-049"></a>`EV-049` | [modules/gateway/tests/internal](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/internal) | Directory / related suite material |
| <a id="ev-050"></a>`EV-050` | [modules/gateway/frontend/src/__tests__/components/bedrock/BedrockAccountRouting.test.tsx](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/__tests__/components/bedrock/BedrockAccountRouting.test.tsx) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-051"></a>`EV-051` | [tests/e2e/cli_uplift/remote/personal_inference.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/personal_inference.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-052"></a>`EV-052` | [modules/gateway/frontend/src/__tests__/components/aws/BedrockAccountSelector.test.tsx](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/__tests__/components/aws/BedrockAccountSelector.test.tsx) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-053"></a>`EV-053` | [modules/gateway/frontend/src/components/budget/MonthlySpend.tsx](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/components/budget/MonthlySpend.tsx) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-054"></a>`EV-054` | [tests/e2e/cli_uplift/remote/budget_lifecycle.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/budget_lifecycle.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-055"></a>`EV-055` | [platform/evals/budget-ratelimit/run-eval.sh](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/evals/budget-ratelimit/run-eval.sh) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-056"></a>`EV-056` | [modules/gateway/tests/budget](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/budget) | Directory / related suite material |
| <a id="ev-057"></a>`EV-057` | [modules/gateway/frontend/src/components/budget/BudgetEnforcementControl.tsx](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/components/budget/BudgetEnforcementControl.tsx) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-058"></a>`EV-058` | [modules/gateway/tests/e2e/test_ratelimit_stories.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/e2e/test_ratelimit_stories.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-059"></a>`EV-059` | [modules/gateway/tests/e2e/test_pool_stories.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/e2e/test_pool_stories.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-060"></a>`EV-060` | [modules/gateway/frontend/src/pages/OrgDashboard.tsx](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/pages/OrgDashboard.tsx) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-061"></a>`EV-061` | [modules/gateway/frontend/src/pages/DepartmentDashboard.tsx](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/pages/DepartmentDashboard.tsx) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-062"></a>`EV-062` | [tests/e2e/cli_uplift/remote/usage_exports.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/usage_exports.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-063"></a>`EV-063` | [tests/e2e/cli_uplift/remote/agent_terminal_controls.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/agent_terminal_controls.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-064"></a>`EV-064` | [modules/gateway/frontend/tests/e2e/agent-control.spec.ts](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/tests/e2e/agent-control.spec.ts) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-065"></a>`EV-065` | [modules/gateway/frontend/tests/e2e/explanations.spec.ts](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/tests/e2e/explanations.spec.ts) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-066"></a>`EV-066` | [tests/e2e/chat](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/chat) | Directory / related suite material |
| <a id="ev-067"></a>`EV-067` | [tests/e2e/cli_uplift/remote/assistant_baseline.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/assistant_baseline.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-068"></a>`EV-068` | [modules/gateway/frontend/src/pages/MyChats.tsx](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/pages/MyChats.tsx) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-069"></a>`EV-069` | [modules/agent-factory/tests/e2e](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e) | Directory / related suite material |
| <a id="ev-070"></a>`EV-070` | [modules/gateway/tests/agentauth](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/agentauth) | Directory / related suite material |
| <a id="ev-071"></a>`EV-071` | [modules/gateway/frontend/tests/e2e/knowledge.spec.ts](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/tests/e2e/knowledge.spec.ts) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-072"></a>`EV-072` | [tests/e2e/cli_uplift/remote/knowledge_lifecycle.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/knowledge_lifecycle.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-073"></a>`EV-073` | [modules/gateway/tests/knowledge](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/knowledge) | Directory / related suite material |
| <a id="ev-074"></a>`EV-074` | [modules/agent-context/tests/e2e](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e) | Directory / related suite material |
| <a id="ev-075"></a>`EV-075` | [modules/gateway/frontend/src/pages/admin/IndexingStatus.tsx](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/pages/admin/IndexingStatus.tsx) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-076"></a>`EV-076` | [modules/gateway/tests/orchestration](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/orchestration) | Directory / related suite material |
| <a id="ev-077"></a>`EV-077` | [docs/task-api/evaluation-manifest.json](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/docs/task-api/evaluation-manifest.json) | Report / manifest / documentation |
| <a id="ev-078"></a>`EV-078` | [tests/e2e/cli_uplift/remote/hosted_coding.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/hosted_coding.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-079"></a>`EV-079` | [modules/agent-factory/codex-harness/integration](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/codex-harness/integration) | Directory / related suite material |
| <a id="ev-080"></a>`EV-080` | [modules/gateway/frontend/src/pages/domain-mri/task-client.test.ts](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/pages/domain-mri/task-client.test.ts) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-081"></a>`EV-081` | [modules/gateway/frontend/src/pages/domain-mri/task-client.js](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/pages/domain-mri/task-client.js) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-082"></a>`EV-082` | [modules/gateway/tests/integration/test_live_api.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/integration/test_live_api.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-083"></a>`EV-083` | [modules/gateway/tests/e2e/test_proxy_stories.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/e2e/test_proxy_stories.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-084"></a>`EV-084` | [modules/gateway/frontend/src/components/next/journeys.ts](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/src/components/next/journeys.ts) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-085"></a>`EV-085` | [platform/evals/cli-onboarding/run-eval.sh](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/evals/cli-onboarding/run-eval.sh) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-086"></a>`EV-086` | [platform/scripts/release/acceptance.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/scripts/release/acceptance.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-087"></a>`EV-087` | [modules/gateway/tests/features/test_superplane_proxy.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/features/test_superplane_proxy.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-088"></a>`EV-088` | [modules/domain-apps/superplane/ui/browser-tests/verify_onboarding_browser.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/ui/browser-tests/verify_onboarding_browser.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-089"></a>`EV-089` | [modules/domain-apps/superplane/ui/__tests__/OnboardingView.test.tsx](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/ui/__tests__/OnboardingView.test.tsx) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-090"></a>`EV-090` | [modules/domain-apps/superplane/ui/__tests__/ApprovalPanel.test.tsx](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/ui/__tests__/ApprovalPanel.test.tsx) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-091"></a>`EV-091` | [modules/domain-apps/superplane/ui/__tests__/ProviderConnectionPanel.test.tsx](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/ui/__tests__/ProviderConnectionPanel.test.tsx) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-092"></a>`EV-092` | [modules/domain-apps/superplane/ui/browser-tests/verify_browser.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/ui/browser-tests/verify_browser.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-093"></a>`EV-093` | [modules/domain-apps/superplane/ui/__tests__/RetirementPanel.test.tsx](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/ui/__tests__/RetirementPanel.test.tsx) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-094"></a>`EV-094` | [modules/domain-apps/superplane/src/superplane-api/tests](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-api/tests) | Directory / related suite material |
| <a id="ev-095"></a>`EV-095` | [modules/domain-apps/superplane/workspace_bootstrap/tests](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/workspace_bootstrap/tests) | Directory / related suite material |
| <a id="ev-096"></a>`EV-096` | [modules/agent-context/door/server.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/door/server.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-097"></a>`EV-097` | [modules/domain-apps/superplane/tests/test_skypilot_startup_contract.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/tests/test_skypilot_startup_contract.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-098"></a>`EV-098` | [modules/domain-apps/superplane/src/superplane-controller/skypilot/client_test.go](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/superplane/src/superplane-controller/skypilot/client_test.go) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-099"></a>`EV-099` | [tests/e2e/cli_uplift/remote/update_rollback.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/update_rollback.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-100"></a>`EV-100` | [tests/e2e/kimi/regression_smoke.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/kimi/regression_smoke.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-101"></a>`EV-101` | [tests/e2e/cli_uplift/remote/multi_deployment.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/multi_deployment.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-102"></a>`EV-102` | [modules/gateway/tests/pricing_policy](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/pricing_policy) | Directory / related suite material |
| <a id="ev-103"></a>`EV-103` | [modules/gateway/pricing_policy](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/pricing_policy) | Directory / related suite material |
| <a id="ev-104"></a>`EV-104` | [tests/e2e/cli_uplift/cases.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/cases.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-105"></a>`EV-105` | [modules/agent-factory/codex-harness/integration/test_task_codex_host.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/codex-harness/integration/test_task_codex_host.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-106"></a>`EV-106` | [modules/agent-factory/codex-harness/integration/test_github_codex_host.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/codex-harness/integration/test_github_codex_host.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-107"></a>`EV-107` | [modules/agent-factory/codex-harness/integration/test_gateway_runtime.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/codex-harness/integration/test_gateway_runtime.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-108"></a>`EV-108` | [.github/workflows/codex-harness-tests.yml](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/.github/workflows/codex-harness-tests.yml) | Workflow definition |
| <a id="ev-109"></a>`EV-109` | [modules/agent-factory/tests/e2e/test_keda_scaler.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_keda_scaler.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-110"></a>`EV-110` | [modules/tools/validation/qualify-isolation.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/tools/validation/qualify-isolation.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-111"></a>`EV-111` | [tests/e2e/orchestration/SCENARIOS.md](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/orchestration/SCENARIOS.md) | Report / manifest / documentation |
| <a id="ev-112"></a>`EV-112` | [tests/e2e/orchestration/scenarios/definitions.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/orchestration/scenarios/definitions.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-113"></a>`EV-113` | [tests/e2e/orchestration/scenarios/runtime_faults.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/orchestration/scenarios/runtime_faults.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-114"></a>`EV-114` | [tests/e2e/orchestration/scenarios/controls.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/orchestration/scenarios/controls.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-115"></a>`EV-115` | [tests/e2e/orchestration/scenarios/audit.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/orchestration/scenarios/audit.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-116"></a>`EV-116` | [scripts/task-api/check-contracts.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/scripts/task-api/check-contracts.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-117"></a>`EV-117` | [tests/task-api/test_external_client.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/task-api/test_external_client.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-118"></a>`EV-118` | [docs/task-api/evidence/2026-09-25-v1-v2/v1-criterion-report.json](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/docs/task-api/evidence/2026-09-25-v1-v2/v1-criterion-report.json) | Report / manifest / documentation |
| <a id="ev-119"></a>`EV-119` | [docs/task-api/evidence/2026-09-25-v1-v2/v2-criterion-report.json](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/docs/task-api/evidence/2026-09-25-v1-v2/v2-criterion-report.json) | Report / manifest / documentation |
| <a id="ev-120"></a>`EV-120` | [scripts/task-api/verify-qualified-report.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/scripts/task-api/verify-qualified-report.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-121"></a>`EV-121` | [docs/task-api/evidence/2026-09-25-v3/README.md](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/docs/task-api/evidence/2026-09-25-v3/README.md) | Report / manifest / documentation |
| <a id="ev-122"></a>`EV-122` | [docs/task-api/evidence/2026-09-25-v4/README.md](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/docs/task-api/evidence/2026-09-25-v4/README.md) | Report / manifest / documentation |
| <a id="ev-123"></a>`EV-123` | [examples/task-api/observe.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/examples/task-api/observe.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-124"></a>`EV-124` | [docs/task-api/evidence/2026-09-25-v5/README.md](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/docs/task-api/evidence/2026-09-25-v5/README.md) | Report / manifest / documentation |
| <a id="ev-125"></a>`EV-125` | [modules/agent-factory/tests/e2e/test_github_regression.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_github_regression.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-126"></a>`EV-126` | [modules/agent-factory/tests/conftest.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/conftest.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-127"></a>`EV-127` | [modules/agent-factory/tests/e2e/test_gitlab_roundtrip.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_gitlab_roundtrip.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-128"></a>`EV-128` | [modules/agent-factory/tests/e2e/test_gitlab_live_fleet.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_gitlab_live_fleet.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-129"></a>`EV-129` | [modules/agent-factory/tests/e2e/test_openclaw_parity.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_openclaw_parity.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-130"></a>`EV-130` | [docs/openclaw-use-cases.md](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/docs/openclaw-use-cases.md) | Report / manifest / documentation |
| <a id="ev-131"></a>`EV-131` | [modules/agent-context/tests/e2e/test_platform_health.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_platform_health.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-132"></a>`EV-132` | [modules/agent-context/tests/e2e/test_mcp_endpoint.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_mcp_endpoint.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-133"></a>`EV-133` | [modules/agent-context/tests/e2e/test_ingestion.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_ingestion.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-134"></a>`EV-134` | [modules/agent-context/tests/conftest.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/conftest.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-135"></a>`EV-135` | [modules/agent-context/tests/e2e/test_knowledge_isolation.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_knowledge_isolation.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-136"></a>`EV-136` | [modules/agent-context/tests/e2e/test_graphrag.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_graphrag.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-137"></a>`EV-137` | [modules/agent-context/tests/e2e/test_vuln_e2e.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_vuln_e2e.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-138"></a>`EV-138` | [modules/agent-factory/tests/e2e/test_memory_e2e.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_memory_e2e.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-139"></a>`EV-139` | [modules/agent-factory/Makefile](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/Makefile) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-140"></a>`EV-140` | [modules/agent-factory/tests/e2e/test_artifacts_e2e.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_artifacts_e2e.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-141"></a>`EV-141` | [modules/research/gbrain/tests/test_container_runtime.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/research/gbrain/tests/test_container_runtime.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-142"></a>`EV-142` | [modules/research/gbrain/scripts/smoke-test.sh](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/research/gbrain/scripts/smoke-test.sh) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-143"></a>`EV-143` | [modules/research/gbrain/tests/test_container_dream.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/research/gbrain/tests/test_container_dream.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-144"></a>`EV-144` | [modules/research/gbrain/tests/RUNTIME.md](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/research/gbrain/tests/RUNTIME.md) | Report / manifest / documentation |
| <a id="ev-145"></a>`EV-145` | [modules/tools/agentcore/tests/test_transport.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/tools/agentcore/tests/test_transport.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-146"></a>`EV-146` | [modules/tools/task-sdk/test](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/tools/task-sdk/test) | Directory / related suite material |
| <a id="ev-147"></a>`EV-147` | [modules/tools/README.md](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/tools/README.md) | Report / manifest / documentation |
| <a id="ev-148"></a>`EV-148` | [modules/tools/agentcore/tests](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/tools/agentcore/tests) | Directory / related suite material |
| <a id="ev-149"></a>`EV-149` | [tests/e2e/chat/test_bash.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/chat/test_bash.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-150"></a>`EV-150` | [modules/tools/validation/tests](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/tools/validation/tests) | Directory / related suite material |
| <a id="ev-151"></a>`EV-151` | [modules/tools/validation/qualify-executor.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/tools/validation/qualify-executor.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-152"></a>`EV-152` | [modules/harness/jobs/tests](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/harness/jobs/tests) | Directory / related suite material |
| <a id="ev-153"></a>`EV-153` | [modules/harness/jobs/README.md](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/harness/jobs/README.md) | Report / manifest / documentation |
| <a id="ev-154"></a>`EV-154` | [.github/workflows/harness-jobs-ci.yml](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/.github/workflows/harness-jobs-ci.yml) | Workflow definition |
| <a id="ev-155"></a>`EV-155` | [contracts/hitl-ticket/v1](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/contracts/hitl-ticket/v1) | Directory / related suite material |
| <a id="ev-156"></a>`EV-156` | [.github/workflows/hitl-ticket-contract-tests.yml](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/.github/workflows/hitl-ticket-contract-tests.yml) | Workflow definition |
| <a id="ev-157"></a>`EV-157` | [modules/gateway/tests/internal/test_provenance_contract.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/internal/test_provenance_contract.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-158"></a>`EV-158` | [.github/workflows/provenance-contract-tests.yml](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/.github/workflows/provenance-contract-tests.yml) | Workflow definition |
| <a id="ev-159"></a>`EV-159` | [.github/workflows/security-scan.yml](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/.github/workflows/security-scan.yml) | Workflow definition |
| <a id="ev-160"></a>`EV-160` | [.github/security](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/.github/security) | Directory / related suite material |
| <a id="ev-161"></a>`EV-161` | [.github/workflows/security-agent-nightly.yml](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/.github/workflows/security-agent-nightly.yml) | Workflow definition |
| <a id="ev-162"></a>`EV-162` | [platform/scripts/adversarial-test-assert.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/scripts/adversarial-test-assert.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-163"></a>`EV-163` | [modules/domain-apps/cyber/agent/skills/url-analysis/tests/run_adaptive_acceptance.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/cyber/agent/skills/url-analysis/tests/run_adaptive_acceptance.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-164"></a>`EV-164` | [modules/domain-apps/cyber/agent/skills/url-analysis/tests/run_analyst_acceptance.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/cyber/agent/skills/url-analysis/tests/run_analyst_acceptance.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-165"></a>`EV-165` | [modules/domain-apps/cyber/docs/adaptive-investigation-acceptance-2026-09-24.md](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/cyber/docs/adaptive-investigation-acceptance-2026-09-24.md) | Report / manifest / documentation |
| <a id="ev-166"></a>`EV-166` | [modules/domain-apps/cyber/workers/tests](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/cyber/workers/tests) | Directory / related suite material |
| <a id="ev-167"></a>`EV-167` | [modules/domain-apps/cyber/bootstrap-scripts/03-hypervisor-smoke.sh](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/cyber/bootstrap-scripts/03-hypervisor-smoke.sh) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-168"></a>`EV-168` | [modules/domain-apps/cyber/ci](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/domain-apps/cyber/ci) | Directory / related suite material |
| <a id="ev-169"></a>`EV-169` | [modules/platform-deploy-mgmt/tests/integration/test_verify_e2e.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/platform-deploy-mgmt/tests/integration/test_verify_e2e.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-170"></a>`EV-170` | [.github/workflows/platform-deploy-mgmt-verify.yml](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/.github/workflows/platform-deploy-mgmt-verify.yml) | Workflow definition |
| <a id="ev-171"></a>`EV-171` | [platform/scripts/release/websocket-smoke.mjs](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/scripts/release/websocket-smoke.mjs) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-172"></a>`EV-172` | [.github/workflows/adp-release-upgrade.yml](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/.github/workflows/adp-release-upgrade.yml) | Workflow definition |
| <a id="ev-173"></a>`EV-173` | [platform/scripts/tests](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/scripts/tests) | Directory / related suite material |
| <a id="ev-174"></a>`EV-174` | [modules/agent-factory/tests/e2e/helpers/live_fleet_latency.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/helpers/live_fleet_latency.py) | Source / test / acceptance driver (scope stated at use) |
| <a id="ev-175"></a>`EV-175` | [modules/user-services/README.md](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/user-services/README.md) | Report / manifest / documentation |
| <a id="ev-176"></a>`EV-176` | [modules/harness/mcp-hub](https://github.com/aws-e/adp/tree/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/harness/mcp-hub) | Directory / related suite material |
| <a id="ev-177"></a>`EV-177` | [tests/e2e/cli_uplift/remote/story_research.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_research.py) | Source / test / acceptance driver (scope stated at use) |

## Counted test and check definitions

Counts reference these exact source definitions or registered cases. This catalogue includes only explicitly mapped items; it is not a repository-wide test census. Blocked and placeholder items remain visible but do not count as mapped implemented E2E/local tests. No live tests or collection imports were run while counting.

| Test ID | Kind | Definition / registered case | Notes |
| --- | --- | --- | --- |
| <a id="test-001"></a>`TEST-001` | E2E | [modules/agent-context/tests/e2e/test_ingestion.py::TestManualIngestionTrigger::test_manual_trigger_completes](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_ingestion.py#L29) | Ingestion/job-state checks; complete searchable-content assertion remains separate. |
| <a id="test-002"></a>`TEST-002` | E2E | [modules/agent-context/tests/e2e/test_ingestion.py::TestManualIngestionTrigger::test_manual_trigger_logs_no_errors](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_ingestion.py#L37) | Ingestion/job-state checks; complete searchable-content assertion remains separate. |
| <a id="test-003"></a>`TEST-003` | E2E | [modules/agent-context/tests/e2e/test_ingestion.py::TestNewRepoIngest::test_new_repo_appears_in_search](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_ingestion.py#L124) | Weak assertion: explicitly does not require search results > 0; one existing test, not acceptance of the full scenario. |
| <a id="test-004"></a>`TEST-004` | E2E | [modules/agent-context/tests/e2e/test_ingestion.py::TestStatePersistence::test_repo_state_json_exists_and_valid](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_ingestion.py#L57) | Ingestion/job-state checks; complete searchable-content assertion remains separate. |
| <a id="test-005"></a>`TEST-005` | E2E | [modules/agent-context/tests/e2e/test_knowledge_isolation.py::TestLiveACLFreshness::test_permission_change_reflected](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_knowledge_isolation.py#L104) | Permission-change check requires real users/repository and re-ingestion. |
| <a id="test-006"></a>`TEST-006` | E2E | [modules/agent-context/tests/e2e/test_knowledge_isolation.py::TestLiveIsolationCrossTenant::test_cross_tenant_no_leakage](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_knowledge_isolation.py#L72) | Live opt-in cross-tenant isolation check. |
| <a id="test-007"></a>`TEST-007` | E2E | [modules/agent-context/tests/e2e/test_knowledge_isolation.py::TestLiveIsolationCrossTenant::test_unauthorized_search_returns_empty](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_knowledge_isolation.py#L56) | Live opt-in unauthorized search checks. |
| <a id="test-008"></a>`TEST-008` | E2E | [modules/agent-context/tests/e2e/test_knowledge_isolation.py::TestLiveIsolationHappyPath::test_authorized_search_returns_results](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_knowledge_isolation.py#L25) | Requires real repository/user fixture; search assertion is narrower than all source permissions. |
| <a id="test-009"></a>`TEST-009` | E2E | [modules/agent-context/tests/e2e/test_mcp_endpoint.py::TestAuthPosture::test_tools_endpoint_accessible](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_mcp_endpoint.py#L190) | Endpoint-access check; fixture mode and permissions determine live execution. |
| <a id="test-010"></a>`TEST-010` | E2E | [modules/agent-context/tests/e2e/test_platform_health.py::TestDeployments::test_deployment_has_ready_replicas](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_platform_health.py#L51) | Selected health/infrastructure definitions; parameter expansion is not counted. |
| <a id="test-011"></a>`TEST-011` | E2E | [modules/agent-context/tests/e2e/test_platform_health.py::TestPVCs::test_pvc_bound](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_platform_health.py#L88) | Selected health/infrastructure definitions; parameter expansion is not counted. |
| <a id="test-012"></a>`TEST-012` | E2E | [modules/agent-context/tests/e2e/test_platform_health.py::TestPodHealth::test_no_pods_in_bad_state](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_platform_health.py#L135) | Selected health/infrastructure definitions; parameter expansion is not counted. |
| <a id="test-013"></a>`TEST-013` | E2E | [modules/agent-context/tests/e2e/test_platform_health.py::TestServiceAccount::test_service_account_exists](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_platform_health.py#L152) | Selected health/infrastructure definitions; parameter expansion is not counted. |
| <a id="test-014"></a>`TEST-014` | E2E | [modules/agent-context/tests/e2e/test_platform_health.py::TestServiceAccount::test_service_account_has_irsa_annotation](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_platform_health.py#L156) | Selected health/infrastructure definitions; parameter expansion is not counted. |
| <a id="test-015"></a>`TEST-015` | E2E | [modules/agent-context/tests/e2e/test_platform_health.py::TestServices::test_service_exists_with_port](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_platform_health.py#L69) | Selected health/infrastructure definitions; parameter expansion is not counted. |
| <a id="test-016"></a>`TEST-016` | PLACEHOLDER | [modules/agent-context/tests/e2e/test_vuln_e2e.py::TestPlantedCVEEndToEnd::test_step1_ingest_fixture_repo](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_vuln_e2e.py#L34) | Unconditionally skipped implementation-deferred definitions. |
| <a id="test-017"></a>`TEST-017` | PLACEHOLDER | [modules/agent-context/tests/e2e/test_vuln_e2e.py::TestPlantedCVEEndToEnd::test_step2_sbom_generated](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_vuln_e2e.py#L54) | Unconditionally skipped implementation-deferred definitions. |
| <a id="test-018"></a>`TEST-018` | PLACEHOLDER | [modules/agent-context/tests/e2e/test_vuln_e2e.py::TestPlantedCVEEndToEnd::test_step3_vulnerability_detected](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_vuln_e2e.py#L63) | Unconditionally skipped implementation-deferred definitions. |
| <a id="test-019"></a>`TEST-019` | PLACEHOLDER | [modules/agent-context/tests/e2e/test_vuln_e2e.py::TestPlantedCVEEndToEnd::test_step4_reverse_lookup_identifies_repos](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_vuln_e2e.py#L73) | Unconditionally skipped implementation-deferred definitions. |
| <a id="test-020"></a>`TEST-020` | PLACEHOLDER | [modules/agent-context/tests/e2e/test_vuln_e2e.py::TestPlantedCVEEndToEnd::test_step5_reachability_confirmed](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_vuln_e2e.py#L82) | Unconditionally skipped implementation-deferred definitions. |
| <a id="test-021"></a>`TEST-021` | PLACEHOLDER | [modules/agent-context/tests/e2e/test_vuln_e2e.py::TestPlantedCVEEndToEnd::test_step6_triage_files_issue](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_vuln_e2e.py#L91) | Unconditionally skipped implementation-deferred definitions. |
| <a id="test-022"></a>`TEST-022` | PLACEHOLDER | [modules/agent-context/tests/e2e/test_vuln_e2e.py::TestPlantedCVEEndToEnd::test_step7_fix_pr_opened](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_vuln_e2e.py#L102) | Unconditionally skipped implementation-deferred definitions. |
| <a id="test-023"></a>`TEST-023` | PLACEHOLDER | [modules/agent-context/tests/e2e/test_vuln_e2e.py::TestPlantedCVEUnreachable::test_dead_import_no_issue](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-context/tests/e2e/test_vuln_e2e.py#L119) | Unconditionally skipped implementation-deferred definitions. |
| <a id="test-024"></a>`TEST-024` | E2E | [modules/agent-factory/tests/e2e/test_artifacts_e2e.py::TestArtifactFetchEditRoundTrip::test_fetch_edit_publish_with_supersedes](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_artifacts_e2e.py#L171) | Opt-in live fetch/edit/superseding lineage. |
| <a id="test-025"></a>`TEST-025` | E2E | [modules/agent-factory/tests/e2e/test_artifacts_e2e.py::TestArtifactPublish::test_publish_artifact_creates_s3_and_catalog](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_artifacts_e2e.py#L55) | Opt-in live object/catalog publication and cleanup. |
| <a id="test-026"></a>`TEST-026` | E2E | [modules/agent-factory/tests/e2e/test_github_regression.py::TestGitHubWebhookRegression::test_github_invalid_signature_still_rejected](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_github_regression.py#L198) | Invalid-signature refusal; latest combined contract run skipped. |
| <a id="test-027"></a>`TEST-027` | E2E | [modules/agent-factory/tests/e2e/test_github_regression.py::TestGitHubWebhookRegression::test_github_webhook_still_processes_events](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_github_regression.py#L107) | Real endpoint harness exists; latest combined contract run skipped. |
| <a id="test-028"></a>`TEST-028` | E2E | [modules/agent-factory/tests/e2e/test_gitlab_roundtrip.py::TestGitLabRoundTrip::test_invalid_token_rejected](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_gitlab_roundtrip.py#L94) | Wrong token refusal; latest combined contract run skipped. |
| <a id="test-029"></a>`TEST-029` | E2E | [modules/agent-factory/tests/e2e/test_gitlab_roundtrip.py::TestGitLabRoundTrip::test_non_mention_event_ignored](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_gitlab_roundtrip.py#L111) | Ignored non-mention; other ignored-type tests exist in the same file. |
| <a id="test-030"></a>`TEST-030` | E2E | [modules/agent-factory/tests/e2e/test_keda_scaler.py::TestPodSpawnOnEnqueue::test_pod_spawns_on_message](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_keda_scaler.py#L69) | Actual worker scale-up check. No scale-down test was found in this mapped file. |
| <a id="test-031"></a>`TEST-031` | E2E | [modules/agent-factory/tests/e2e/test_memory_e2e.py::TestSavePreferenceAndRecall::test_cross_session_preference_recall](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_memory_e2e.py#L201) | Opt-in live cross-session recall. |
| <a id="test-032"></a>`TEST-032` | E2E | [modules/agent-factory/tests/e2e/test_memory_e2e.py::TestSavePreferenceAndRecall::test_save_preference_creates_memory_row](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/agent-factory/tests/e2e/test_memory_e2e.py#L52) | Opt-in live WS-to-DynamoDB preference persistence. |
| <a id="test-033"></a>`TEST-033` | LOCAL | [modules/gateway/frontend/tests/e2e/explanations.spec.ts::delivery before completion (desktop/mobile template)](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/frontend/tests/e2e/explanations.spec.ts#L6) | One source test definition, producing desktop/mobile variants; fixture HTTP only. |
| <a id="test-034"></a>`TEST-034` | LOCAL | [modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py::test_ac02_save_and_list](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py#L421) | In-process API save/read; browser persistence remains unqualified. |
| <a id="test-035"></a>`TEST-035` | LOCAL | [modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py::test_ac04_self_endpoint_ignores_injected_target](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py#L531) | Injected target does not alter self scope. |
| <a id="test-036"></a>`TEST-036` | LOCAL | [modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py::test_ac06_stale_revision_conflict](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py#L623) | API rejects stale preference writes. |
| <a id="test-037"></a>`TEST-037` | LOCAL | [modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py::test_ac08_reset_and_audit_survives](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py#L864) | API reset and audit preservation; not a real worker/model round trip. |
| <a id="test-038"></a>`TEST-038` | LOCAL | [modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py::test_ac10_admin_cross_tenant_refused](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py#L1348) | Cross-tenant administration is refused in-process. |
| <a id="test-039"></a>`TEST-039` | LOCAL | [modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py::test_ac10_admin_in_tenant_succeeds](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py#L1191) | Admin API save is tested; real service-run selection is not covered by this test. |
| <a id="test-040"></a>`TEST-040` | LOCAL | [modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py::test_ac10_managed_catalogue_refuses_cross_tenant_target](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py#L1312) | Additional directly related in-process API definition; no deployed UI/model inference claim. |
| <a id="test-041"></a>`TEST-041` | LOCAL | [modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py::test_ac10_managed_catalogue_uses_target_service_principal](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py#L1255) | Managed catalogue targets the authorized principal. |
| <a id="test-042"></a>`TEST-042` | LOCAL | [modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py::test_reset_idempotent](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py#L1582) | Additional directly related in-process API definition; no deployed UI/model inference claim. |
| <a id="test-043"></a>`TEST-043` | LOCAL | [modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py::test_reset_requires_and_atomically_enforces_observed_revision](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_model_preferences.py#L1011) | Additional directly related in-process API definition; no deployed UI/model inference claim. |
| <a id="test-044"></a>`TEST-044` | LOCAL | [modules/gateway/tests/admin/persona_models/test_persona_platform_defaults.py::test_create_replay_update_reset_and_conflict](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_platform_defaults.py#L96) | Local default API assertions; deployed selection propagation remains unqualified. |
| <a id="test-045"></a>`TEST-045` | LOCAL | [modules/gateway/tests/admin/persona_models/test_persona_platform_defaults.py::test_incompatible_or_unknown_model_refused](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_platform_defaults.py#L128) | Local default API assertions; deployed selection propagation remains unqualified. |
| <a id="test-046"></a>`TEST-046` | LOCAL | [modules/gateway/tests/admin/persona_models/test_persona_platform_defaults.py::test_non_admin_denied](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_persona_platform_defaults.py#L121) | Local default API assertions; deployed selection propagation remains unqualified. |
| <a id="test-047"></a>`TEST-047` | LOCAL | [modules/gateway/tests/admin/persona_models/test_posture_authz.py::TestOnlyPlatformAdminsReach::test_platform_admin_can_read_and_change](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_posture_authz.py#L208) | In-process posture boundary assertions; live worker propagation is not established. |
| <a id="test-048"></a>`TEST-048` | LOCAL | [modules/gateway/tests/admin/persona_models/test_posture_authz.py::TestRefusalsThroughTheRoute::test_rollback_is_the_same_audited_operation](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_posture_authz.py#L271) | In-process posture boundary assertions; live worker propagation is not established. |
| <a id="test-049"></a>`TEST-049` | LOCAL | [modules/gateway/tests/admin/persona_models/test_posture_authz.py::TestRefusalsThroughTheRoute::test_stale_revision_is_a_conflict_that_writes_nothing](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/modules/gateway/tests/admin/persona_models/test_posture_authz.py#L229) | In-process posture boundary assertions; live worker propagation is not established. |
| <a id="test-050"></a>`TEST-050` | E2E | [platform/evals/budget-ratelimit/run-eval.sh::case_01](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/evals/budget-ratelimit/run-eval.sh#L1008) | Cases 1–5 seed exhausted balances; case 7 checks real accrual. These do not prove real spend-through to the cap. |
| <a id="test-051"></a>`TEST-051` | E2E | [platform/evals/budget-ratelimit/run-eval.sh::case_02](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/evals/budget-ratelimit/run-eval.sh#L1029) | Cases 1–5 seed exhausted balances; case 7 checks real accrual. These do not prove real spend-through to the cap. |
| <a id="test-052"></a>`TEST-052` | E2E | [platform/evals/budget-ratelimit/run-eval.sh::case_03](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/evals/budget-ratelimit/run-eval.sh#L1048) | Cases 1–5 seed exhausted balances; case 7 checks real accrual. These do not prove real spend-through to the cap. |
| <a id="test-053"></a>`TEST-053` | E2E | [platform/evals/budget-ratelimit/run-eval.sh::case_04](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/evals/budget-ratelimit/run-eval.sh#L1064) | Cases 1–5 seed exhausted balances; case 7 checks real accrual. These do not prove real spend-through to the cap. |
| <a id="test-054"></a>`TEST-054` | E2E | [platform/evals/budget-ratelimit/run-eval.sh::case_05](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/evals/budget-ratelimit/run-eval.sh#L1092) | Cases 1–5 seed exhausted balances; case 7 checks real accrual. These do not prove real spend-through to the cap. |
| <a id="test-055"></a>`TEST-055` | E2E | [platform/evals/budget-ratelimit/run-eval.sh::case_07](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/evals/budget-ratelimit/run-eval.sh#L1129) | Cases 1–5 seed exhausted balances; case 7 checks real accrual. These do not prove real spend-through to the cap. |
| <a id="test-056"></a>`TEST-056` | E2E | [platform/evals/budget-ratelimit/run-eval.sh::case_08](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/evals/budget-ratelimit/run-eval.sh#L1219) |  |
| <a id="test-057"></a>`TEST-057` | PLACEHOLDER | [platform/evals/budget-ratelimit/run-eval.sh::case_09](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/evals/budget-ratelimit/run-eval.sh#L1239) | TPM case always skips; it is excluded from implemented test count. |
| <a id="test-058"></a>`TEST-058` | E2E | [platform/evals/budget-ratelimit/run-eval.sh::case_10](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/evals/budget-ratelimit/run-eval.sh#L1258) |  |
| <a id="test-059"></a>`TEST-059` | E2E | [platform/evals/budget-ratelimit/run-eval.sh::case_11](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/evals/budget-ratelimit/run-eval.sh#L1296) |  |
| <a id="test-060"></a>`TEST-060` | E2E | [platform/evals/cli-onboarding/run-eval.sh::run_phase_b](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/evals/cli-onboarding/run-eval.sh#L766) | Named acceptance phase; setup/cleanup helpers and internal assertions are not separate tests. |
| <a id="test-061"></a>`TEST-061` | E2E | [platform/evals/cli-onboarding/run-eval.sh::run_phase_c](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/evals/cli-onboarding/run-eval.sh#L871) | Named acceptance phase; setup/cleanup helpers and internal assertions are not separate tests. |
| <a id="test-062"></a>`TEST-062` | CHECK | [platform/scripts/release/acceptance.py::check](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/platform/scripts/release/acceptance.py#L32) | One acceptance/check entry point. This is not a pytest test count or a count of its individual assertions. |
| <a id="test-063"></a>`TEST-063` | E2E | [tests/e2e/chat/test_auth.py::TestSessionRestoration::test_supplied_tokens_restore_authenticated_session](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/chat/test_auth.py#L86) | Browser restores supplied session; this test does not complete native email login. |
| <a id="test-064"></a>`TEST-064` | E2E | [tests/e2e/chat/test_auth.py::TestWebSocketOpens::test_ws_created_on_chat_page](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/chat/test_auth.py#L110) | WebSocket creation is checked; no full session-creation lifecycle in this assertion. |
| <a id="test-065"></a>`TEST-065` | E2E | [tests/e2e/cli_uplift/remote/assistant_baseline.py::CLI E50](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/assistant_baseline.py#L131) | Registered E50: assistant_baseline; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-066"></a>`TEST-066` | E2E | [tests/e2e/cli_uplift/remote/budget_lifecycle.py::CLI D06](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/budget_lifecycle.py#L291) | Registered D06: budget_lifecycle; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-067"></a>`TEST-067` | E2E | [tests/e2e/cli_uplift/remote/hierarchy_lifecycle.py::CLI D03](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/hierarchy_lifecycle.py#L38) | Registered D03: hierarchy_lifecycle; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-068"></a>`TEST-068` | E2E | [tests/e2e/cli_uplift/remote/hosted_coding.py::CLI E42](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/hosted_coding.py#L406) | Registered E42: hosted_coding; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-069"></a>`TEST-069` | E2E | [tests/e2e/cli_uplift/remote/install_auth.py::CLI C01](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/install_auth.py#L568) | Registered C01: install_auth; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-070"></a>`TEST-070` | E2E | [tests/e2e/cli_uplift/remote/knowledge_lifecycle.py::CLI D04](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/knowledge_lifecycle.py#L78) | Registered D04: knowledge_lifecycle; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-071"></a>`TEST-071` | E2E | [tests/e2e/cli_uplift/remote/machine_lifecycle.py::CLI D05](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/machine_lifecycle.py#L56) | Registered D05: machine_lifecycle; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-072"></a>`TEST-072` | BLOCKED | [tests/e2e/cli_uplift/remote/multi_deployment.py::CLI E16](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/multi_deployment.py#L969) | Registered E16: multi_deployment_concurrency; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-073"></a>`TEST-073` | BLOCKED | [tests/e2e/cli_uplift/remote/multi_deployment.py::CLI E17](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/multi_deployment.py#L969) | Registered E17: multi_deployment_lifecycle; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-074"></a>`TEST-074` | E2E | [tests/e2e/cli_uplift/remote/personal_inference.py::CLI E08](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/personal_inference.py#L228) | Registered E08: personal_inference; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-075"></a>`TEST-075` | E2E | [tests/e2e/cli_uplift/remote/story_reads.py::CLI E21](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py#L868) | Registered E21: story_usage; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-076"></a>`TEST-076` | E2E | [tests/e2e/cli_uplift/remote/story_reads.py::CLI E22](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py#L868) | Registered E22: story_activity; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-077"></a>`TEST-077` | E2E | [tests/e2e/cli_uplift/remote/story_reads.py::CLI E24](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py#L868) | Registered E24: story_vault; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-078"></a>`TEST-078` | E2E | [tests/e2e/cli_uplift/remote/story_reads.py::CLI E26](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py#L868) | Registered E26: story_budget; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-079"></a>`TEST-079` | E2E | [tests/e2e/cli_uplift/remote/story_reads.py::CLI E28](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py#L868) | Registered E28: story_github_maintenance; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-080"></a>`TEST-080` | E2E | [tests/e2e/cli_uplift/remote/story_reads.py::CLI E30](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py#L868) | Registered E30: story_gitlab; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-081"></a>`TEST-081` | E2E | [tests/e2e/cli_uplift/remote/story_reads.py::CLI E31](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py#L868) | Registered E31: story_machine; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-082"></a>`TEST-082` | E2E | [tests/e2e/cli_uplift/remote/story_reads.py::CLI E32](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py#L868) | Registered E32: story_knowledge; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-083"></a>`TEST-083` | E2E | [tests/e2e/cli_uplift/remote/story_reads.py::CLI E33](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py#L868) | Registered E33: story_access; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-084"></a>`TEST-084` | E2E | [tests/e2e/cli_uplift/remote/story_reads.py::CLI E34](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py#L868) | Registered E34: story_bedrock_lifecycle; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-085"></a>`TEST-085` | E2E | [tests/e2e/cli_uplift/remote/story_reads.py::CLI E35](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py#L868) | Registered E35: story_person_budget; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-086"></a>`TEST-086` | E2E | [tests/e2e/cli_uplift/remote/story_reads.py::CLI E37](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py#L868) | Registered E37: story_recovery; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-087"></a>`TEST-087` | E2E | [tests/e2e/cli_uplift/remote/story_reads.py::CLI E38](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py#L868) | Registered E38: story_model_policy; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-088"></a>`TEST-088` | E2E | [tests/e2e/cli_uplift/remote/story_reads.py::CLI E39](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py#L868) | Registered E39: story_superplane_lifecycle; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-089"></a>`TEST-089` | E2E | [tests/e2e/cli_uplift/remote/story_research.py::CLI E25](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_research.py#L36) | Registered E25: story_research; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-090"></a>`TEST-090` | E2E | [tests/e2e/cli_uplift/remote/tenant_isolation.py::CLI E23](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/tenant_isolation.py#L214) | Registered E23: tenant_smoke; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-091"></a>`TEST-091` | E2E | [tests/e2e/cli_uplift/remote/tenant_isolation.py::CLI E27](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/tenant_isolation.py#L214) | Registered E27: tenant_isolation; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-092"></a>`TEST-092` | E2E | [tests/e2e/cli_uplift/remote/update_rollback.py::CLI E14](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/update_rollback.py#L588) | Registered E14: update_rollback; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-093"></a>`TEST-093` | E2E | [tests/e2e/cli_uplift/remote/vault_lifecycle.py::CLI D02](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/vault_lifecycle.py#L47) | Registered D02: vault_lifecycle; execute entry point. One case, regardless of internal assertions or scenario reuse. |
| <a id="test-094"></a>`TEST-094` | CHECK | [tests/e2e/kimi/regression_smoke.py::main](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/kimi/regression_smoke.py#L81) | One acceptance/check entry point. This is not a pytest test count or a count of its individual assertions. |
| <a id="test-095"></a>`TEST-095` | E2E | [tests/e2e/new_ui/test_coexistence.py::test_entry_return_and_shared_identity](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/new_ui/test_coexistence.py#L171) | Live preview entry/return/shared-identity check. |
| <a id="test-096"></a>`TEST-096` | E2E | [tests/e2e/new_ui/test_coexistence.py::test_legacy_routes_unchanged](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/new_ui/test_coexistence.py#L136) | Only four current page response paths are selected, not every route. |
| <a id="test-097"></a>`TEST-097` | E2E | [tests/e2e/new_ui/test_coexistence.py::test_live_flag_off_returns_current_ui](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/new_ui/test_coexistence.py#L235) | Covers new_ui off state when deployed accordingly, not every feature flag. |
| <a id="test-098"></a>`TEST-098` | LOCAL | [tests/e2e/new_ui/test_coexistence.py::test_simulated_failed_new_bundle](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/new_ui/test_coexistence.py#L289) | Bundle failure is deliberately simulated in-browser. |
| <a id="test-099"></a>`TEST-099` | E2E | [tests/e2e/new_ui/test_coexistence.py::test_unknown_next_path_has_return](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/new_ui/test_coexistence.py#L262) | Preview fallback/return only; global fallback remains separately mapped. |

## Existing CLI cases and selection

All 57 registered CLI cases are retained here for traceability. Suite/test-case IDs are not scenario IDs. “Current nightly” reflects the earlier audit's workflow selection at the pinned baseline; proposed daily/weekly profiles refer to draft PR #6971, not a merged active schedule. Selection does not establish execution or passing assertions. No schedule changes are made by this register.

| Case | Native owner / suite | Test purpose | Existing coverage | Fixtures | Current nightly / proposed daily / proposed weekly | Definition |
| --- | --- | --- | --- | --- | --- | --- |
| `E01` | #5185; install | Unauthenticated discovery/download return 200; fresh EC2 install is immediately executable with matching hashes | Live harness; execution not established | ec2;platform | True / True / True | [stages.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/stages.py) |
| `E02` | #5185; admin | Real adp admin login completes fresh-password and MFA challenges; bad credentials and non-admin operations fail; refresh works | Live harness; execution not established | ec2;cognito | False / False / False | [stages.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/stages.py) |
| `E03` | #5185; admin | adp admin setup reports ready/pending/failed accurately; interruption and rerun complete missing steps without duplicates | Live harness; execution not established | ec2;cognito | False / False / False | [stages.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/stages.py) |
| `E04` | #5182; personal-aws | adp aws connect provisions/imports compatible roles; list/verify match live records; mismatch fails; disconnect keeps the AWS role | Live harness; execution not established | ec2;destination | False / False / False | [stages.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/stages.py) |
| `E05` | #5182; personal-aws | Download then separate AWS-admin apply then resume connects with AWS access disabled in ADP; CloudTrail identifies the EC2 provisioner | Live harness; execution not established | ec2;destination | False / False / False | [stages.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/stages.py) |
| `E06` | #5181; routing | Bedrock CLI direct/reuse/download/apply/resume verifies before assignment; failure prevents assignment; rerun is idempotent | Live harness; execution not established | ec2;destination | False / False / False | [bedrock_routing.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/bedrock_routing.py) |
| `E07` | #5181; routing | User beats team beats org beats platform default; removing overrides exposes the next rung without affecting another user | Missing driver | ec2;destination;second_destination | False / False / False | [stages.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/stages.py) |
| `E08` | #5181; inference | Real personal Claude/Codex inference at each rung returns the unique marker with matching AWS and ADP usage evidence | Live harness; execution not established | ec2;destination | False / False / False | [personal_inference.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/personal_inference.py) |
| `E09` | #5181; inference | Real hosted Claude via authenticated ingress yields owner/tenant/task/run IDs with matching routing and usage at each rung | Missing driver | ec2;hosted | False / False / False | [stages.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/stages.py) |
| `E10` | #5183; github | Native-admin journey creates a fresh GitHub App and reuses an existing App in separate fixtures with real OAuth/webhook readiness | Missing driver | ec2;github_app | False / False / False | [stages.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/stages.py) |
| `E11` | #5184; github | Real adp login OAuth/browser approval connects user and repository; wrong repo, cross-tenant and nonce replay fail | Missing driver | ec2;github_app;github_repo | False / False / False | [stages.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/stages.py) |
| `E12` | #5183 #5184; github | Dedicated repo completes one bounded existing agent-development task after CLI onboarding with webhook/run and commit/PR evidence | Missing driver | ec2;github_app;github_repo | False / False / False | [stages.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/stages.py) |
| `E13` | all; parity | Live API field/type/ownership assertions derive from actual CLI/UI consumers; UI-created and CLI-created resources are equivalently usable | Live harness; execution not established | ec2;platform | False / False / False | [api_parity.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/api_parity.py) |
| `E14` | all; parity | Update/rollback/interrupted download preserve a usable installation; adp codex/claude forwarding, setup and launch pass | Live harness; execution not established | ec2;platform | False / False / False | [update_rollback.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/update_rollback.py) |
| `E15` | harness; harness | Two fresh full runs pass on one deployed revision; interrupt, resume and repeat cleanup leave no duplicates or unowned mutations | Full-run qualification blocked by missing mandatory cases | ec2;platform | False / False / False | [stages.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/stages.py) |
| `E16` | #5413; multi-deployment | One install, three deployments, three concurrent tool sessions (two Codex + Claude and the reverse mix): every marker has an authenticated request and usage receipt at its own deployment for its own user, and none at the other two | Blocked: model-limit capability deliberately unavailable | ec2;platform;three_deployments;multi_deployment_model_limits | False / False / False | [multi_deployment.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/multi_deployment.py) |
| `E17` | #5413; multi-deployment | Live default switch, refresh, and logout of one deployment leave the other two correctly routed; the logged-out one fails labelled without borrowing a session; teardown leaves no deployment state | Blocked: model-limit capability deliberately unavailable | ec2;platform;three_deployments;multi_deployment_model_limits | False / False / False | [multi_deployment.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/multi_deployment.py) |
| `E18` | #5637; superplane | Served CLI traverses the gateway to the real domain: workspace create/read/kubeconfig/cost/events/quota/deploy and the provider credential handoff carry both identifiers; a failed second-stage registration compensates only its own credential; account registration reports unavailable without writing | Blocked: durable recovery producer unimplemented | ec2;platform;superplane_domain | False / False / False | [superplane_domain.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/superplane_domain.py) |
| `E19` | #5621; parity | Freshly served CLI on EC2: adp capabilities distinguishes an enabled operation from an intentionally disabled one and from one the caller may not perform; adp doctor reports read-only bounded findings with no mutation and no paid inference; a foreign request ID is indistinguishable from an absent one | Live harness; execution not established | ec2;platform;capability_contrast | False / False / True | [capability_contrast.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/capability_contrast.py) |
| `E20` | #5621; story-reads | Served capabilities has distinct operation IDs; bounded auth/API doctor checks succeed | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito | True / True / True | [story_reads.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py) |
| `E21` | #5628; story-reads | Own usage views and bounded JSON/NDJSON/CSV exports preserve scope, shape and continuation/exit status; no spend reconciliation claim | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito | True / True / True | [story_reads.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py) |
| `E22` | #5629; story-reads | Own Activity pagination and missing-run status/state/detail errors are structured; no active-control claim | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito | True / True / True | [story_reads.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py) |
| `E23` | #5622; story-reads | Served tenant membership/current selection and unknown-selector refusal without global workspace changes | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito | True / True / True | [tenant_isolation.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/tenant_isolation.py) |
| `E24` | #5631; story-reads | Credential/identity metadata and mutation previews use the served CLI without reading secrets or writing provider claims | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito | True / True / True | [story_reads.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py) |
| `E25` | #5639; story-reads | Served research reads preserve scoped findings/proposal IDs and pagination; no scan, mutation or decision | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito;superplane_research_read_fixture | True / True / True | [story_research.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_research.py) |
| `E26` | #5589; story-reads | Own daily/weekly/monthly budget reads retain periods and uncapped semantics; no paid inference or enforcement claim | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito | True / True / True | [story_reads.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py) |
| `E27` | #5622; tenant-isolation | Two owned memberships retain explicit tenant scope during concurrent reads, local default changes and Cognito refresh; no model inference claim | Live harness; execution not established | ec2;platform;cognito;tenant_isolation | True / True / True | [tenant_isolation.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/tenant_isolation.py) |
| `E28` | #5634; story-reads | GitHub maintenance status and reviewed previews never read supplied keys or change the shared App | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito | True / True / True | [story_reads.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py) |
| `E29` | #5623; story-reads | Bounded administrator hierarchy reads preserve organization scope; no mutation lifecycle acceptance claim | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito | True / True / True | [story_reads.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py) |
| `E30` | #5635; story-reads | GitLab approved-provider discovery and invalid project refusal through the served CLI; no provider writes | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito | True / True / True | [story_reads.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py) |
| `E31` | #5624; story-reads | Explicit SQL IAM, IAM registry and Cognito client metadata reads retain tenant scope; no secret or mutation lifecycle claim | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito | True / True / True | [story_reads.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py) |
| `E32` | #5632; story-reads | Served knowledge discovery/status errors and soft-delete previews; live indexing and retrieval acceptance held | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito | True / True / True | [story_reads.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py) |
| `E33` | #5625; story-reads | Tenant access status and bounded authorized request review; decision and revocation fixtures remain separate | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito | True / True / True | [story_reads.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py) |
| `E34` | #5633; story-reads | Personal Bedrock reset preview preserves billing/source readback and exact team-target refusal; real routing inference remains held | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito | True / True / True | [story_reads.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py) |
| `E35` | #5626; story-reads | Own person limits retain source and self-write refusal; no spend-through or enforcement claim | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito | True / True / True | [story_reads.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py) |
| `E36` | #5627; story-reads | Own rate-limit hierarchy and unavailable TPM are explicit; no inference or saved-limit mutation | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito | True / True / True | [story_reads.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py) |
| `E37` | #5630; story-reads | Flow reads and malformed recovery refusal through served CLI; owned accepted-flow recovery remains fixture-gated | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito | True / True / True | [story_reads.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py) |
| `E38` | #5636; story-reads | Persona cost/catalog readback retains unknown amounts and capability evidence; no platform mutation/inference claim | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito | True / True / True | [story_reads.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py) |
| `E39` | #5638; story-reads | Superplane workspace lifecycle preview and scoped audit reads; no provider mutation or compute qualification | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito;superplane_domain | True / True / True | [story_reads.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py) |
| `E40` | #5640; story-reads | Hosted chat readiness and bounded own history; live multi-turn acceptance remains held | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito | True / True / True | [story_reads.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py) |
| `E41` | #5641; story-reads | Platform status distinguishes selected-gateway capability metadata from unverified AWS/artifact readiness; no deployment invocation | Partial: bounded reads/previews, not full feature lifecycle | ec2;platform;cognito | True / True / True | [story_reads.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/story_reads.py) |
| `E42` | #5516; hosted-coding | One enrolled human repository Task uses canonical submit, replay, monitor and control with terminal readback | Live harness; execution not established | ec2;platform;cognito;human_task_coding | True / True / True | [hosted_coding.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/hosted_coding.py) |
| `E43` | #6931/#6932/#147 SEC/DATA/SES; assistant | Authenticated WebSocket turn, durable acknowledgement, tool stream, replay and history refresh | Missing driver | ec2;platform;cognito;assistant_users | False / False / False | [stages.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/stages.py) |
| `E44` | #6933/#6934 ACT/EXT; assistant | Grounded ADP and connected human activity, source coverage and citations | Missing driver | ec2;platform;cognito;assistant_users | False / False / False | [stages.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/stages.py) |
| `E45` | #6931/#6932 SEC/DATA; assistant | Two-user/two-tenant history, memory, artifact and installation isolation | Missing driver | ec2;platform;cognito;assistant_users | False / False / False | [stages.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/stages.py) |
| `E46` | #147 SES; assistant | Persistent and ephemeral sessions, concurrency, lease fencing and independent sandbox destruction | Missing driver | ec2;platform;cognito;assistant_users | False / False / False | [stages.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/stages.py) |
| `E47` | #147/#6937 SES/QUAL; assistant | Bounded worker and queue faults, reconnect and accepted-turn reconciliation | Missing driver | ec2;platform;cognito;assistant_users | False / False / False | [stages.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/stages.py) |
| `E48` | #6935 DEP/EXT; assistant | Authorized installation diagnostics and upgrade ledger states | Missing driver | ec2;platform;cognito;assistant_users;assistant_installation_ledger | False / False / False | [stages.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/stages.py) |
| `E49` | #183/#6937 WARM/QUAL; assistant | Headless protocol latency and idle capacity/model cost baseline | Missing driver | ec2;platform;cognito;assistant_users | False / False / False | [stages.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/stages.py) |
| `E50` | #6939 HEAD01/HEAD03/HEAD04; assistant | Supported authenticated WebSocket baseline with server-owned session and correlated final response | Live harness; execution not established | ec2;platform;cognito;assistant_users | False / False / False | [assistant_baseline.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/assistant_baseline.py) |
| `C01` | #5199; login | Native Cognito admin login and refresh work on fresh EC2; no seeded session | Live harness; execution not established | ec2;cognito | True / True / True | [stages.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/stages.py) |
| `D04` | #5632; knowledge-lifecycle | Owned document registration, watch, same-key reindex and terminal cleanup | Live harness; execution not established | ec2;platform;cognito;knowledge_lifecycle | False / False / True | [knowledge_lifecycle.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/knowledge_lifecycle.py) |
| `D06` | #5589/#5627; budget-lifecycle | Owned ordinary budget periods/ledgers and rate-limit dimensions with revision-fenced cleanup; no inference | Live harness; execution not established | ec2;platform;cognito;budget_lifecycle | False / False / True | [budget_lifecycle.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/budget_lifecycle.py) |
| `D05` | #5624/#5625; machine-lifecycle | Owned canonical principal lifecycle with ordinary access and session review boundary | Live harness; execution not established | ec2;platform;cognito;machine_lifecycle | False / False / True | [machine_lifecycle.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/machine_lifecycle.py) |
| `D03` | #5623/#5622; hierarchy-lifecycle | Owned hierarchy lifecycle and ordinary tenant revocation/restoration | Live harness; execution not established | ec2;platform;cognito;hierarchy_lifecycle | False / False / True | [hierarchy_lifecycle.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/hierarchy_lifecycle.py) |
| `D01` | #5640; hosted-chat | Two bounded hosted chat turns with durable recovery and owned cleanup | Live harness; execution not established | ec2;platform;cognito;human_task_chat | False / False / True | [hosted_chat.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/hosted_chat.py) |
| `D02` | #5631; vault-lifecycle | Owned synthetic credential and unverified identity lifecycle with cleanup | Live harness; execution not established | ec2;platform;cognito;vault_lifecycle | False / False / True | [vault_lifecycle.py](https://github.com/aws-e/adp/blob/169d36160473c0c3e29ef6f51fcf9ee3c195f256/tests/e2e/cli_uplift/remote/vault_lifecycle.py) |

## Inspected execution evidence

These observations were fetched during the earlier audit. They are not new runs and are not a per-scenario result matrix. No scenario in this master file receives a current PASS from these aggregate workflow outcomes.

| Suite | Run | Observed outcome |
| --- | --- | --- |
| Chat | [37376326694](https://github.com/aws-e/adp/actions/runs/37376326694) | 2026-10-05: 3 passed, 25 deselected; enabled interaction coverage not run |
| GitLab | [37376326499](https://github.com/aws-e/adp/actions/runs/37376326499) | 6 contract tests and 2 live fleet tests skipped |
| Gateway live | [37312557677](https://github.com/aws-e/adp/actions/runs/37312557677) | 86 skipped, 22,224 deselected |
| CLI nightly | [37307290666](https://github.com/aws-e/adp/actions/runs/37307290666) | Account mismatch at deployed-revision preflight |
| Credential adversarial | [37299767654](https://github.com/aws-e/adp/actions/runs/37299767654) | Missing ADP_DEPLOY_ROLE_ARN before A1/A3 |
| CLI Uplift | [36676728047](https://github.com/aws-e/adp/actions/runs/36676728047) | Successful Sept 30 run selected login only |
| Agent Context verification | [28361015976](https://github.com/aws-e/adp/actions/runs/28361015976) | Last main run found: June 29, failure |

No main-filtered runs were returned for orchestration-live-tests or platform-deploy-mgmt-verify in the earlier inspected history; this does not establish absence of runs on other branches. The Task API manifest contains 92 criteria: 83 runnable and 9 not implemented. Runnable commands include local and retained-evidence checks. Historical Task/Cyber qualification retains its original timing, environment and scope limitations.

## Register integrity and maintenance

- Every baseline logical module and all 133 feature aliases have exactly one master owner and stable ID.
- Every feature has individually numbered scenarios with an expected result and explicit coverage status; no empty scenario sets are permitted.
- Every inventoried UI route, HTTP registration, feature flag and edge/protocol entry maps to existing master features.
- Feature evidence is retained separately from explicit scenario mappings, so a related suite cannot silently turn all scenarios green.
- Update a scenario's evidence only after reviewing the actual assertions and boundary. Record live execution separately with deployed revision and cleanup results.
- Append new features/scenarios and retire obsolete IDs instead of reusing or renumbering them. Extend the pinned baseline deliberately when auditing newer product changes.

This register is the master test design and coverage map. Implementing the unmapped scenarios and running qualification are subsequent work; they have not been represented as completed here.
