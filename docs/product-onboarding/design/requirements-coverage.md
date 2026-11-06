# Requirements and ownership coverage

Current [#8141](https://github.com/aws-e/adp/issues/8141) EC-01–15 and the
[#5411](https://github.com/aws-e/adp/issues/5411) owner amendments take precedence
over historical navigation. The [preserved baseline](../requirements-2026-10-13.md)
contains exactly 74 checkbox obligations and the stable PO/PON aliases. They are
all enumerated below, including obligations whose implementation belongs elsewhere.
**Mapping is not acceptance.** No historical obligation is silently dropped or
declared delivered by this docs PR.

## Current epic criteria

| #8141 ID | Design contract / owner | Delivery and independent proof |
|---|---|---|
| EC-01 | Atomic minimal identity and governed Project, Profile | I1/D1/C1/U1, V01 |
| EC-02 | Typed fields, revision preconditions, provenance, Profile | I1/I4, V02/V13 |
| EC-03 | Reuse/discovery/bind/unlink without asset deletion, Profile + existing connection services | I2/I3, V05/V08/V11 |
| EC-04 | Current tenant/Project/source authorization everywhere in Profile; consumers #8148 | I1–I5, V03/V04/V12 |
| EC-05 | Shared UI/CLI/agent operations, existing memberships, no-model direct use, Profile | I1/I2, V01/V06/V10 |
| EC-06 | Durable domain revisions, operations, change feed and replay, Profile | I1/I3/I5, V02/V12/V15 |
| EC-07 | Outage, archive/restore, owned-only cleanup and no implicit execution, Profile + peer lifecycle owners | I1/I3/I5, V09/V10/V11 |
| EC-08 | Three guided journeys with explicitly selected effects, Profile | I2/I3/I4, V05/V08/V10/V11 |
| EC-09 | Bidirectional resume and credential/admin handoff, Profile + connection producers | I2/I3, V06/V12 |
| EC-10 | Versioned denominator/N/A/levels and no authority from maturity, Profile | I2/I4, V07/V13 |
| EC-11 | Real scoped revision-preserving handoff and result display, Profile side; Context/L5 own results | A1/I5/D5/C5/U5, V14/V15; peer contracts unavailable now |
| EC-12 | Architecture/DB/API/UI/CLI/CI-CD design and backlog | A0/#8153 AC-01–05; reviewed merge pending |
| EC-13 | Artifact/migration/pipeline/runtime/recovery proof | D0–D5; no deployment in this assignment |
| EC-14 | Installed CLI live acceptance, replay/denial/regressions | C1–C5, V01–V15 applicable rows |
| EC-15 | Served browser live acceptance and both resume directions | U1–U5, V01–V15 applicable rows |

## Preserved PO criteria (not work-package aliases)

| Stable ID | #8141 contribution / epic criteria | Other owner and acceptance boundary |
|---|---|---|
| PO-01 | Minimal identity, EC-01/05 | Governed consumer use later #8148 |
| PO-02 | Four sections/provenance, EC-02/03 | Ongoing fact ingestion/synthesis #8149 |
| PO-03 | Shared direct/guided resume/race/replay, EC-05/06/09 | Peer operation durability stays with its service |
| PO-04 | Explainable completeness/hints, EC-10 | Capability-specific prerequisites from L5/release/test owners |
| PO-05 | Maturity does not grant authority, EC-04/07/10 | Actual execution/budget enforcement existing services and #8150 |
| PO-06 | Registry/source/session isolation, EC-04 | Flow/task/results/search/aggregates #8148, Context #8149, Insights #7295 |
| PO-07 | Three starting paths and selected writes, EC-08 | Provider execution/operating adoption #8150 and release owner |
| PO-08 | Handoff input/status/result display, EC-11 | Real provider capability/run/result/cleanup #8150; no configuration-only pass |
| PO-09 | Optional repo save, explicit connect/create/environment offers, EC-01/03/08/10 | Provider setup eligibility and actual readiness #8150 |
| PO-10 | Direct/import/extraction confirmation and override, EC-02/10 | Extraction/ongoing understanding #8149; allocation approval finance authority |
| PO-11 | Typed targets/periods/parent references, unavailable vs known spend, EC-02/05 | Ledger/attribution/forecast/limits Insights + existing budget/finance services; owner assignment gap for new project finance adapter |
| PO-12 | Workspace and scoped revision handoff, EC-11 | Actual sourced Context synthesis/refresh/correction #8149, not replaced by Profile summary |
| PO-13 | Versioned Product/Project/source/event lineage, EC-06/11 | Applicable flow/task/call/PR/test/cost lineage #8148/#7295/#7356, integrated #5411 |

## Preserved PON work-package aliases

PON numbers describe the earlier plan, not filed child IDs and not the same scope
as the correspondingly numbered PO criterion. Native child links remain GitHub's
authority. Their retained work is allocated as follows:

| Alias | Retained work and Profile delivery | Owning peer / unresolved contribution |
|---|---|---|
| PON-01 | Vocabulary/minimal identity/Project binding → I1 | Consumer lineage #8148 |
| PON-02 | Agent/shared UI/CLI/progress/resume → I2/I4 | No separate onboarding scheduler |
| PON-03 | Discovery/provenance/confirmation → I2/I4 | Ongoing reconciliation #8149 |
| PON-04 | Existing repo binding → I2 | Actual agent round trip #8148 / existing agent services |
| PON-05 | Explicit greenfield repo/PR bootstrap → I3 | Approved template/baseline-policy owner assigned by #5411 |
| PON-06 | Environment/connection selection → I2/I3 | Account vending adapter owner unresolved; not implemented in Profile |
| PON-07 | Existing team/access/admin handoffs → I1/I3 | Execution policy consumers #8148/#8150 |
| PON-08 | Financial input/source/limits references → I4 | Cost attribution/alerts/exports #7295 + finance/budget owner |
| PON-09 | Read-only running-product adoption → I3 | Govern/Operate and cutover #8150 / release authority |
| PON-10 | Profile lifecycle and full evaluation pack → I1–I5/Cn/Un | Provider/consumer cleanup separately owned |
| PON-11 | Selected assurance inputs and handoff → I5 | Suite/execution/results #7356 |
| PON-12 | Selected release input and handoff → I5 | Release service #7628; ADP #5408 is reference, not customer implementation |
| PON-13 | Source/lineage/evidence handoff → I5 | Evidence ownership/storage/federation #7295 and release/test owners |

## All 74 baseline obligations

B01–B74 are **audit row labels only**, not new acceptance IDs. Order is exactly
the baseline's checkbox order. `Profile` means #8141; `shared` names each portion,
not duplicate implementation ownership. `gap` is deliberately unresolved work in
the release umbrella, not a postponed inventory exercise. All rows remain planned
or peer-owned; none asserts runtime completion.

| Row / source line | Retained obligation (short title) | Concrete allocation / design or delivery evidence |
|---|---|---|
| B01 / 459 | Canonical vocabulary | Profile architecture glossary/identity table; I1 docs; repo/environment/account distinction retained. |
| B02 / 460 | Team-owned governed model and provider bindings | Profile database + scope resolver; I1/I2; V02/V03/V05. |
| B03 / 461 | Ownership modes, profiles, lifecycle and deletion | Profile selections/lifecycle/retention; I1/I3; service-specific ownership/policy #7628/#7356/#8150. |
| B04 / 462 | Separate Knowledge Layer grouping | Architecture explicitly excludes soft #1728 project filter from authority; I1 negative tests. |
| B05 / 463 | Shared authorized CRUD/audit/idempotency | OpenAPI + database operations; I1; V01–V04. |
| B06 / 467 | Durable step actor/evidence/recovery | Session/operation schema and UI/CLI resume; I2/I3; V06/V12. |
| B07 / 468 | Dry-run before external effects | Exact scoped repo plan and existing credential/admin authority; I3; V08. |
| B08 / 469 | Retry/callback no duplication | Operation hash/key/sequence/generation; I3; V08/V12. |
| B09 / 470 | Desired/observed drift | Profile on-demand metadata differences I2; ongoing Context reconciliation #8149; V05. |
| B10 / 471 | Useful minimal profile, no readiness lie | I1/I2; Saved/Getting Started distinct from configuration/demo; V01/V07/V10. |
| B11 / 475 | Existing GitHub/GitLab immutable bindings | Reuse both existing provider services, verified native IDs; I2; V05. Unsupported adapter must remain explicit, not falsely completed. |
| B12 / 476 | Optional versioned-template repository | Explicit selected effect, owner/visibility/protections; I3; V08. |
| B13 / 477 | Preserve CI, introduce changes via PR | I3 import invariance and proposed pipeline PR; V08/V11. |
| B14 / 478 | Profile-specific delivery/assurance policy | Profile stores selected service/policy references; #7628/#7356 implement checks/gates; I5 handoff. |
| B15 / 479 | Mark legacy ARC repo-onboarding guidance | I3 documentation explicitly points canonical connection path; do not remove ARC execution capability. |
| B16 / 480 | Real issue/comment→reviewed PR | Existing agent services/#8148; V14/V15 release integration; not claimed by profile API tests. |
| B17 / 484 | Product vs platform environments | Architecture/source binding schema; I2, separate from ADP platform target; V11. |
| B18 / 485 | Exact verified cloud target and tier | Existing credential verification + typed environment observation; I2/I3; V03/V11; no ambient-role substitution. |
| B19 / 486 | Shared/dedicated/reused/vended accounts | Reuse/connect I3; **gap:** account-vending adapter owner/contract assigned by #5411, no new account on save. |
| B20 / 487 | Production read-before-write | I3 read-only as-is baseline; provider elevation #8150; V11. |
| B21 / 488 | Reject stale/free-text cloud target | Current credential/account/role verification and reference validation; I2/I3; V03/V11. |
| B22 / 489 | Nonprod effect and prod denial | #8150/release operator owns actual effect; Profile provides validated scope; V11/V14 plus peer evaluation. |
| B23 / 493 | Organization/team/roles selection | Reuse membership and existing roles, I1/I2; Project sharing is not role creation; V03/V06. |
| B24 / 494 | Human/service access review | Profile stores owner/review reference; existing identity and #8148 enforce; **gap:** periodic review scheduling owner not assigned. |
| B25 / 495 | Least-privilege action/credential gates | Existing auth/budgets plus #8148/#8150 consumer checks; Profile resolver never expands them; V03/V04. |
| B26 / 496 | Admin handoffs without credentials | I3 connection callbacks and UI/CLI continuation; V06/V12. |
| B27 / 497 | Project-private denial (historical wording) | Owner clarification supersedes private-by-default profile configuration: I1 enforces inherited Product-team and existing source access; #8148 preserves private individual runs. V03/V04 retain denial coverage without a second Project privacy model. |
| B28 / 501 | Budget/rate limit before billable agent | Existing metered gateway enforced in guide, I2; provider execution #8150; V10/V13. |
| B29 / 502 | Cloud resource cost tags | #8150/release/provider operator; Profile passes Project/owner/cost-center refs; I5. |
| B30 / 503 | Cloud cost source vs model budget | I4 financial links, #7295 ingestion/analytics; V13 shows unknown instead of zero. |
| B31 / 504 | Spend/forecast/coverage | #7295/finance owns projection; I4 consumes only attributable output; V13. |
| B32 / 505 | Showback/chargeback/escalation | #7295 + budget/finance owner; **gap:** project finance integration owner unresolved, not Profile execution. |
| B33 / 506 | Known-cost isolation | Peer ledger fixture #7295/finance; I4 scoped display V13, #5411 integrated acceptance. |
| B34 / 510 | Read-only signed-off as-is inventory | I3 accepted snapshot/source refs and actor; V11. |
| B35 / 511 | Reused/gap/unsupported/unknown/exception | I2/I3 discovery/coverage/readiness presentation; exception requires policy reference; V05/V11. |
| B36 / 512 | Observe→Assist→Govern→Operate | Profile exposes selected status/handoff only; #8150/release own independent approvals/rollback; V14. |
| B37 / 513 | No production import mutation | I3, provider before/after hashes; V11. |
| B38 / 514 | Nonprod evidence before prod authority | #7628/#8150 execution gate; Profile cannot approve exception; V11/V14. |
| B39 / 515 | Exact production coordinates/evidence | #7628/#8148 producer/consumer lineage; Profile refs I5, V15. |
| B40 / 519 | Truthful drift/events/audit export | Profile history/change feed I1/I5, Context drift #8149, alert analytics #7295; V05/V15. |
| B41 / 520 | Criticality-dependent operational requirements | Profile typed governance/docs refs I2/I3; peer policy/service owners define enforcement; V11. |
| B42 / 521 | Restrict/suspend/transfer/archive/offboard | Product-team access + Project state/owner + Product lifecycle I1/I3; V04/V09. |
| B43 / 522 | Revoke before delete, retain externals | Existing connection services/#8148/#8150 stop owned effects; Profile fences operations I3; V04/V09. |
| B44 / 523 | Interruption/timeout/drift/recovery | I2/I3 fault boundaries; V05/V06/V09/V12. |
| B45 / 527 | Versioned three-journey fixtures | Evaluation manifest and Cn/Un fixture owner; no invented live fixtures here. |
| B46 / 528 | Separate deterministic and live proof | Dn/Cn/Un; actual GitHub/AWS readback required; mocks labelled. |
| B47 / 529 | Revision and target-bound evidence | Deployment compatibility receipt + private fixture manifest; Dn/Cn/Un. |
| B48 / 530 | Resume/idempotency proof | V01/V06/V08/V12 via installed CLI and served browser. |
| B49 / 531 | Time to agent PR/governed deployment | #5411 integrator after #8148/#7628 actual outcomes; no substitute metric from profile save. |
| B50 / 535 | Technology-neutral customer service | #7628/#7356/#7295 contracts; I5 adapters, never copy ADP's infrastructure target. |
| B51 / 536 | Independent service ownership selections | Profile typed capability selections I2/I5; peers implement managed/integrated/observed modes. |
| B52 / 537 | Versioned multi-domain suite manifest | #7356 shared Test Director, not a Profile suite engine; I5 reference. |
| B53 / 538 | Reusable PR/nightly/release orchestrators | #7356/#7628; existing ADP pipelines are reference only, not delivered customer orchestration. |
| B54 / 539 | Schedules/budgets/concurrency/windows | Owning selected service contract #7356/#7628/#8150; Profile stores versioned config refs. |
| B55 / 540 | Project artifact coverage/adapters | #7628; Profile supplies selected source/component refs I5. Repository audit here covers ADP source entry points, not a customer's future artifact universe. |
| B56 / 541 | Immutable build/promotion/authority | #7628; no permission from Profile maturity; V14/V15 + release evaluation. |
| B57 / 542 | Isolated evidence namespace/catalog | #7295/#7356/#7628 evidence owners; no new Profile bucket. |
| B58 / 543 | Evidence exact lineage | #8148/#7295/#7356/#7628; Profile change/snapshot refs I5, V15. |
| B59 / 544 | Reject invalid/partial/stale evidence | Owning execution/release gate; Profile display distinguishes unavailable/partial; I5. |
| B60 / 545 | Classification/residency/KMS/retention | Profile governance references; evidence service enforces before run; no new storage policy invented. |
| B61 / 546 | Attribute evidence/compute/agent costs | #7295/finance/budget/provider owners; scoped Profile metadata I4/I5. |
| B62 / 547 | Real greenfield release/assurance outcome | #7628/#7356 with #5411; separate from I1 save and I3 repo creation. |
| B63 / 548 | Brownfield shadow, history not certification | Profile read-only baseline I3; service cutover #7628/#8150; V11/V14. |
| B64 / 549 | Cross-tenant evidence/runner/policy denial | #8148 plus service owners; Profile current-source denial V03 is only its portion. |
| B65 / 550 | Evidence offboard/export/retention | Evidence owner #7295/#7356/#7628; Profile archive preserves refs and fences handoffs; V09. |
| B66 / 673 | Dated optional relationships | Product–Project table I1; optional Product/Epic/Initiative/BusinessCase refs use existing Context relationship schema, #8148/#8149 own cross-service materialization; never compulsory fabricated chain. |
| B67 / 674 | Mandatory flow Project and legacy mapping | #8148 owns flow→Project integration, reuse #7298/#7314; Profile supplies registry/resolver, no auto legacy reassignment. |
| B68 / 675 | Typed node lineage | #8148/#7298; Profile publishes IDs/version references I5. |
| B69 / 676 | Task/call/PR lineage join | #8148/#7295 actual consumer integration; V15 + release end-to-end proof. |
| B70 / 677 | Source registry CRUD/verify/audit | Existing connection registry + Product bindings I2/I3; no duplicate vault; V05/V06/V12. |
| B71 / 678 | Release/deployment/incident IDs | #7628/#8148/#7295 source owners; Profile refs/handoff I5. |
| B72 / 679 | Scoped lineage emitter/data trust | Profile change feed I5, #7295 maps/publishes evidence records and coverage; V15. |
| B73 / 680 | No Insights direct database query | Profile change-feed boundary; #7295 consumer acceptance must reject direct-query shortcut. |
| B74 / 681 | Document where IDs are minted | I1 registry docs + I5/#8148 lineage/delivery-flow guide updates; keep native issue/run IDs. |

## Explicit release-level gaps, not hidden Profile prerequisites

Account vending, periodic access-review scheduling, approved repository-template
policy and new Project financial/attribution integration need named owners under
#5411. Context/L5 exact transport/deployment contracts remain unsettled (A1).
These are concrete preserved contributions, not a claim that their systems do not
exist. The respective owner supplies the contract/authority/evidence before that
capability is enabled or accepted. Core profile creation and direct onboarding
remain independently deliverable; the umbrella release cannot close these rows
using this design PR as evidence.
