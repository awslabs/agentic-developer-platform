# Independent review amendments

Historical record for the corrections merged in PR #9075. Later owner requirements and criterion additions are recorded separately in [optional deployment](optional-deployment.md). Counts and verbatim-table statements below describe that earlier revision.

The requesting owner instructed the coordinating assistant to merge PR [#9075](https://github.com/aws-e/adp/pull/9075) and update affected stories on 2026-10-27. The independent review of head `28a23f12664bc12e2e4a106ae080fb589b6bffcd` required the following three corrections before merge. This record describes their design resolution; the proposed runtime tests remain unexecuted.

The [canonical architecture](architecture.md), subordinate story designs and published issue tables form one revised baseline. All 10 EC, 41 AC and 41 SCN IDs, native parent/dependency relationships and live completion boundaries remain. Seven story tables are strengthened before implementation; they are no longer described as verbatim copies of the historical baseline. Historical `original_body_sha256` values in [story contracts](story-contracts.json) remain historical, not hashes of the amended issues.

| Finding | Resolution and ownership | Focused re-review / future proof |
|---|---|---|
| R1, high: completed collection referred to mutable DevLake rows | [§5.1](architecture.md#51-collection-admission-and-persistence): S03 transactionally fences all workspace writers and atomically seals bounded exact payloads/links/versions/policy in the cell before completion. [§6](architecture.md#6-immutable-admission-and-exact-lineage): S04 reads only that durable seal with a pinned mapper/schema and journaled checkpoint; acknowledgement/retention controls retirement. | B2 can overwrite/delete workspace without changing B1. Crash before seal commit stays incomplete; after commit recovery retains original bytes. Native byte/relationship oracles, stale writer denial, page/ack barriers and retention checks are specified in the story scenarios and verification procedure. |
| R2, medium: upstream runtime AutoMigrate conflicts with restricted writer | [§5.2](architecture.md#52-postgresql-on-the-existing-instance): S09 pins schema/dynamic-name/guard manifests and runtime-DDL removal patches; S02 migrates before scope/job admission. S03 runtime performs read-only compatibility checks and returns migration_required before provider work/mutation. | First/new-scope DML succeeds after migration. Missing/incompatible schema or guards fail safely; CREATE/ALTER/DROP are denied to workers. A no-op AutoMigrate on an existing schema is insufficient proof. |
| R3, medium: cleanup was reachable only after successful integration | [§9](architecture.md#9-failure-retention-and-cleanup): S09 delivers bootstrap scheduled/manual finalizer and protected ledger before artifact creation; S02 adds runtime handlers and verifies authority before provisioning. Failure, abort, expiry and early rejection invoke finalization independently of S07/S08 success. | The split preserves the acyclic S09-before-S02 boundary. Partial install and pre-S07 failures exercise actual finalization, interruptions, holds, missing authority and escalation. S08 consumes receipts; successful compensation never converts failed/not-run criteria into passes. |

Source-backed evidence for the first two findings is retained in the [upstream audit](audit.md#upstream-package-and-client-closure). The review examined their failure cases against the revised handoff, SQL privilege and lifecycle mechanisms; all three are resolved at the design level. Implementation qualification remains required.

## Exact story amendments

Each listed scenario retains its original issue-local ID and test/procedure selector. The strengthened case contracts name independent observations and relevant deterministic barriers. Detailed controls are in the [verification procedure](verification.md#review-correction-procedures).

| Story | AC rows amended | SCN rows amended | Amendment |
|---|---|---|---|
| [S01 #8888](stories/s01.md) | AC-01 | SCN-01 | Name finalizer authority, ledger, deadlines, expiry and staging limits. |
| [S02 #8889](stories/s02.md) | AC-01, AC-02, AC-03 | SCN-01, SCN-02, SCN-03 | Manifest/migration readiness, DML/DDL boundary and independently reachable runtime finalization. AC-04 IAM obligations unchanged. |
| [S03 #8890](stories/s03.md) | AC-01, AC-03 | SCN-01, SCN-03 | DML-only first/new-scope collection; immutable seal and stale-generation/crash cases. |
| [S04 #8891](stories/s04.md) | AC-01, AC-03 | SCN-01, SCN-03 | Sealed input, pinned mapper, B1/B2 paginated replay and acknowledgement/retirement. |
| [S07 #8894](stories/s07.md) | AC-02, AC-03 | SCN-02, SCN-03 | Integrated seal/schema/finalizer fault and compatibility proof. |
| [S08 #8895](stories/s08.md) | AC-03 | SCN-03 | Cleanup independent of S07 success; preserve holds and original outcomes. |
| [S09 #8901](stories/s09.md) | AC-01, AC-02, AC-03 | SCN-01, SCN-02, SCN-03 | Schema/DDL patch closure plus bootstrap artifact finalization before publication. |

S05, S06, S10, S11, S12 and S13 acceptance/scenario tables and the epic EC table remain unchanged. All 14 issues receive the same immutable design revision and an accurate design-authorization status. Full original implementation requirements remain in linked story designs; issue narrative is condensed where necessary to stay within the repository's 8 KiB authoring limit.

## Authority and evidence limits

The user's instruction authorizes this corrected design merge and issue amendments. It does not approve #7295's pending requirements gate, rescind the V13 live deferral, register/activate a flow, establish named S01 fixtures/owners, authorize cloud provisioning or accept any live criterion. No custom Go implementation, image build, migration or POC rollout occurs in this change. The [validation report](validation-results.md) separates the architect's earlier offline checks from focused amendment validation.
