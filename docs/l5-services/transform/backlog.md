# Transform implementation backlog

Canonical design: [TF-R1 architecture](architecture.md), **approved for implementation**;
see [accepted revision and actual review](review.json).
This backlog serves every TF01–TF07 group and EC-01–EC-08 in epic #9077.
The seven native children are filed and linked below; filing is not dispatch.
PranavSharma1000 coordinates, assigned slice implementers deliver,
and TF07 owns shared integration/release/live acceptance. Module/feature catalog
IDs remain unresolved with TF01/coordinator; do not invent registered IDs.

## Work and ownership

| Group | Owned outcome / architecture sections | Depends on | Advisory artifact homes |
|---|---|---|---|
| [TF01 #9090](https://github.com/aws-e/adp/issues/9090) / [body](stories/TF01.md) | Supported provider/executor qualification; strict configuration/run/patch/test contracts; catalog mapping. [Qualification](architecture.md#executor-and-provider-qualification), [API](architecture.md#api-and-client-contract), [SQL](architecture.md#sql-entities-and-concurrency). | First; coordinate shared Security/DevOps schema owners. | Proposed `docs/l5-services/transform/qualification.md`; existing `docs/l5-services/contracts/v1/`, L5 validators/adapter protocol and executor boundary. |
| [TF02 #9091](https://github.com/aws-e/adp/issues/9091) / [body](stories/TF02.md) | Product autofill, repo/revision choice, executor/connection binding, owned setup/cleanup. [Configuration](architecture.md#configuration-and-product-handoff), [ownership](architecture.md#setup-ownership-and-cleanup), [identity](architecture.md#identity-and-isolation). | TF01. | Existing L5 configuration/bindings/setup models and gateway Product/broker host; proposed `adapters/transform/`. |
| [TF03 #9092](https://github.com/aws-e/adp/issues/9092) / [body](stories/TF03.md) | Immediate durable run dispatch, isolated native session, bounded observation, unknown-write/cancel recovery. [Execution](architecture.md#execution-and-recovery), [executor](architecture.md#executor-and-provider-qualification). | TF01–TF02; integrate open DevOps admission work. | L5 admission/workers/work store; proposed Transform adapter and executor bridge; peer-owned validation primitives. |
| [TF04 #9093](https://github.com/aws-e/adp/issues/9093) / [body](stories/TF04.md) | Original patch, independent scope/dependency checks, before/after test evidence and retained results. [Provenance](architecture.md#patch-test-and-result-provenance). | TF03; TF01 schema. | Existing L5 results/evidence/artifact host; proposed Transform normalizer/verifier. |
| [TF05 #9094](https://github.com/aws-e/adp/issues/9094) / [body](stories/TF05.md) | Product UI, installed CLI/MCP configuration/run/status/diff/tests; separately authorized optional SCM publication. [Clients](architecture.md#api-and-client-contract), [identity](architecture.md#identity-and-isolation). | TF01/TF04; can build clients against pinned contract earlier. | Gateway Product frontend/client host, CLI, existing MCP operation catalog; no direct provider client. |
| [TF06 #9095](https://github.com/aws-e/adp/issues/9095) / [body](stories/TF06.md) | Context/Insights receipt-based publication, later entitled reuse, opt-in recurrence/shared admission. [Events/reuse](architecture.md#events-publication-and-reuse). | TF03–TF04. | Existing L5 events/work outbox and peer contribution transport; no separate scheduler/database. |
| [TF07 #9096](https://github.com/aws-e/adp/issues/9096) / [body](stories/TF07.md) | Compatible release, two non-ADP fixtures, real deny/recovery/client/reuse/cleanup proof and costs. [Deployment](architecture.md#deployment-and-compatibility), [verification](architecture.md#verification-and-acceptance), [audit](audit.md). | TF01–TF06; build live harness before final deployment. | L5 tests/infra/build, shared SA15 release integration and named host/executor publishers; proposed service runbook. |

These paths explain human ownership only. Shared schema changes go through TF01;
host/worker/migration/executor deployment changes through TF07 with existing
owners. A missing/unlisted path never fails a work node. Do not overwrite sibling
Security/DevOps designs. No automatic merge/deploy or recurring human gate.

## Acceptance consistency

Story-local AC and SCN IDs use the same numbers (for example TF03 AC-02 maps to
TF03 SCN-02); they are stable across issue edits. Detailed independent assertions
are in the seven story bodies. All scenarios are planned, not executed.

| Epic criterion | Producer story assertions | TF07 integrated live proof |
|---|---|---|
| EC-01 | TF01 AC-01/02/03; TF02 AC-01/04 | AC-01: qualified contract/config readback, capability-relevant missing input and portable setup. |
| EC-02 | TF01 AC-02; TF02 AC-02/03; TF03 AC-04; TF05 AC-02 | AC-02: actual-entry cross-tenant/project/source/credential denials and credential-free executor negative controls. |
| EC-03 | TF03 AC-01; TF04 AC-01/02/03 | AC-03: both native operations, original patches, exact base/candidate provenance and independent tests/dependency comparison. |
| EC-04 | TF01 AC-03; TF03 AC-02/03/04 | AC-04: lost ack/restart/replay/cancel with native identity continuity, no duplicate paid work, unknown distinctly retained. |
| EC-05 | TF02 AC-03; TF04 AC-01/04; TF05 AC-02/03 | AC-05: retained readable result after owned scratch cleanup, adopted resources retained, no implicit SCM merge/deploy. |
| EC-06 | TF05 AC-01/02/03; TF06 AC-03 | AC-06: one real journey per client, same contract/scope, recurrence dedup and separate SCM authorization. |
| EC-07 | TF06 AC-01/02 | AC-07: Context/Insights receipts and later entitled task retrieval/use; denied reuse. |
| EC-08 | TF07 AC-01/08 | AC-08: two non-ADP projects with different repo/test config on identical artifacts, costs/retention/cleanup reported. |

## Delivery sequence and completion

1. Qualify the provider/executor and merge strict contracts (TF01). Unsupported
   provider recovery or safe tool isolation leaves dependent live work disabled;
   contract/client work can continue. Resolve catalog identifiers before dispatch.
2. Integrate configuration/setup, then native execution and results (TF02–TF04).
   Client work can proceed against the pinned contract; TF05 acceptance needs the
   real integrated API, not only mocks. TF06 reuses finalized versioned results.
3. TF07 builds deterministic/live harness and shared release changes during these
   slices, then coordinates the compatible release under existing authority.
   Gateway/frontend keep normal auto-deploys. Main-only evaluation is post-merge,
   not a prerequisite for review of its enabling code.
4. Each implementer reports AC/SCN → test/run/attempt → outcome/evidence → tested
   revision. TF07 reports EC → child AC → integrated proof, native IDs, actual
   tests, costs, retention and cleanup. PranavSharma1000 accepts the evidence.
   Required blocked/not-run cases remain incomplete; merge does not close the epic.

## Review and publication record

The [review record](review.json) records the accepted architecture revision and
actual approval. Every child references that revision; design approval does not
complete implementation or live acceptance.

All seven issue bodies reference the same immutable TF-R1 architecture revision:
[`f02387465846acbb9eda244f5815c0ab224ea87a`](https://github.com/aws-e/adp/blob/f02387465846acbb9eda244f5815c0ab224ea87a/docs/l5-services/transform/architecture.md).
The checked-in story bodies mirror the published issue bodies for design review;
implementation agents preserve AC/SCN IDs when refining them. The native parent
for #9090–#9096 is #9077. No implementation story is closed or dispatched.

All seven children are in [ADP Platform Roadmap](https://github.com/orgs/aws-e/projects/4); coordinator readback verified membership on 2026-10-27.
