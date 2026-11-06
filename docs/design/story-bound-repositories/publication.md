# Design publication and child synchronization

## Parallelism amendment — 2026-10-21

The [dependency review](../../delivery-flow/plans/product-epic-delivery-8394/parallelism-review.md)
removes the MR-12 → MR-13 implementation wait and gives migration and harness
work distinct owners. Stage 4 admits both after stage 3 human acceptance, and
MR-14 explicitly joins both reviewed merges with combined offline verification
before final live qualification. The UI/publication and evidence chains
remain ordered because their acceptance consumes predecessor behavior.

Native dependencies are now 36, with 18 of the original 19 feature edges retained.
Peak graph concurrency is still three stories, while stage-4 build concurrency
increases from one to two. All 18 stories, criterion IDs and human gates remain.
The earlier records below describe their dated snapshots, not the amended graph.
No flow registration, execution, deployment or human design approval is claimed.

GitHub readback verified the four updated bodies (#8394, #8406, #8407, #8408),
15 unchanged bodies, all 18 native parents and all 36 dependencies. Labels,
assignees, milestones and states are preserved. The largest body is 7,980 bytes.
Shared validation and pure gateway preview passed; five human reviews remain.
The [validation record](../../delivery-flow/plans/product-epic-delivery-8394/validation.json)
records the immutable amendment revision and verification counts.

## Staged delivery amendment — 2026-10-21

The [delivery plan](../../delivery-flow/plans/product-epic-delivery-8394/README.md)
now defines a dev-baseline checkpoint and four outcome stages, each with deployed
integration evidence and human review. The existing 14 feature stories remain;
new operational children are [MR-15 #8516](https://github.com/aws-e/adp/issues/8516),
[MR-16 #8517](https://github.com/aws-e/adp/issues/8517),
[MR-17 #8518](https://github.com/aws-e/adp/issues/8518) and
[MR-18 #8519](https://github.com/aws-e/adp/issues/8519). MR-14 retains final acceptance.

The updated contract preserves the 19 original native dependencies and adds 16
stage/operation dependencies, for 35. It maps 72 ACs/72 scenarios and six epic ECs.
Pure server preview and shared local validation passed for 27 transformed nodes
and 93 edges, with no state registered. Runtime/release/target bindings remain
required before activation. No human design approval or execution is claimed.

GitHub readback on 2026-10-21 matched all 19 published bodies: epic #8394,
14 updated implementation/qualification stories and four new operational stories.
All 18 native children and 35 native dependency edges were verified; original
labels, assignees, milestones and states were preserved. The largest issue body
is 7,948 UTF-8 bytes; all bodies pass the canonical draft-format check.

The immutable staged-plan revision is
[`2048d0033`](https://github.com/aws-e/adp/blob/2048d0033e7dc0adf076d4a0c4e42051e06a8faa/docs/delivery-flow/plans/product-epic-delivery-8394/README.md).
Issue bodies link to that revision. The
[publication verification](../../delivery-flow/plans/product-epic-delivery-8394/validation.json)
records the synchronization counts. This later receipt does not change the
canonical feature-design revision or claim stage execution.

The prior publication records below describe their dated revisions. The new
stage and story mapping supersedes their delivery sequencing and child counts.

## Current Product/epic amendment

The updated [design PR #8434](https://github.com/aws-e/adp/pull/8434) now describes
one Product-bound flow, usually for an epic in a Product planning repository,
with native child story issues and implementation PRs in their change repos.
The canonical architecture/backlog revision is `df61f74723fb2d78107166fbf8f5793d463b5445`:

- [Architecture](https://github.com/aws-e/adp/blob/df61f74723fb2d78107166fbf8f5793d463b5445/docs/design/story-bound-repositories/architecture.md)
- [All 14 medium story contracts](https://github.com/aws-e/adp/blob/df61f74723fb2d78107166fbf8f5793d463b5445/docs/design/story-bound-repositories/backlog.md)
- [Source audit and targeted Product/epic checks](https://github.com/aws-e/adp/blob/df61f74723fb2d78107166fbf8f5793d463b5445/docs/design/story-bound-repositories/audit.md)

On 2026-10-20, epic #8394 and all children #8395–#8408 were updated directly,
including Design sections, acceptance criteria and applicable titles/scope.
Readback matched every intended body and title. All 56 child ACs, their scenario
IDs and six epic EC IDs are retained. Existing labels, assignees, milestones and
open states are unchanged. All 14 native parents remain #8394.

Two native dependencies were added and verified: #8401 depends on #8400 for the
verified epic reference, and #8405 depends on #8401 for shared issue/native-link
publication recovery. All 17 previous dependency edges remain, for 19 in total.
No dependency on a new Project workflow or completion of #8148/#7314 was added.
The implementation stories stay in aws-e/adp; the qualification fixture proves
planning epic C with native children and PRs in A/B.

**Status:** Product/epic amendment reviewed and published for existing design
approval. No human approval, implementation, deployment or live qualification
is claimed. This record is a later documentation checkpoint; it does not change
the canonical architecture/backlog revision above.

| Child | Contract | Verified body bytes | Native dependencies |
|---|---|---:|---|
| [MR-01 / #8395](https://github.com/aws-e/adp/issues/8395) | [design](backlog.md#mr-01) | 7851 | None |
| [MR-02 / #8396](https://github.com/aws-e/adp/issues/8396) | [design](backlog.md#mr-02) | 7656 | #8395 |
| [MR-03 / #8397](https://github.com/aws-e/adp/issues/8397) | [design](backlog.md#mr-03) | 7304 | #8396 |
| [MR-04 / #8398](https://github.com/aws-e/adp/issues/8398) | [design](backlog.md#mr-04) | 7304 | #8397 |
| [MR-05 / #8399](https://github.com/aws-e/adp/issues/8399) | [design](backlog.md#mr-05) | 7146 | #8397 |
| [MR-06 / #8400](https://github.com/aws-e/adp/issues/8400) | [design](backlog.md#mr-06) | 7673 | #8396 |
| [MR-07 / #8401](https://github.com/aws-e/adp/issues/8401) | [design](backlog.md#mr-07) | 7910 | #8396, #8400 |
| [MR-08 / #8402](https://github.com/aws-e/adp/issues/8402) | [design](backlog.md#mr-08) | 7526 | #8401 |
| [MR-09 / #8403](https://github.com/aws-e/adp/issues/8403) | [design](backlog.md#mr-09) | 7400 | #8395 |
| [MR-10 / #8404](https://github.com/aws-e/adp/issues/8404) | [design](backlog.md#mr-10) | 7260 | #8398, #8403 |
| [MR-11 / #8405](https://github.com/aws-e/adp/issues/8405) | [design](backlog.md#mr-11) | 7832 | #8401, #8404 |
| [MR-12 / #8406](https://github.com/aws-e/adp/issues/8406) | [design](backlog.md#mr-12) | 7826 | #8399, #8400, #8402, #8405 |
| [MR-13 / #8407](https://github.com/aws-e/adp/issues/8407) | [design](backlog.md#mr-13) | 7859 | #8406 |
| [MR-14 / #8408](https://github.com/aws-e/adp/issues/8408) | [design](backlog.md#mr-14) | 7606 | #8407 |

Epic body: 7,799 bytes. Maximum body: 7,910 bytes. All 15 pass the existing
issue-format checker at draft stage and the stricter 8,000-byte limit. Draft
format validation does not imply dispatch readiness or approval. The design
bundle source/link validator, public-document scanner and whitespace checks
passed. No application or live tests were run for this documentation change.

The publication snapshot and exact readback were retained locally; no private
runtime or credential data is included here. The earlier publication below is
historical and its original sizes, scope and dependencies are not current.

## Original publication (historical)

[Ready design PR #8434](https://github.com/aws-e/adp/pull/8434) targets `main` from
`agent/issue-8394`; GitHub confirmed `OPEN` and `isDraft: false` on 2026-10-20.
The canonical architecture/backlog revision is
[`ed3e2b23f5e0ce9be7fb45060214b0fb4800b6fc`](https://github.com/aws-e/adp/commit/ed3e2b23f5e0ce9be7fb45060214b0fb4800b6fc).
This publication record is a subsequent documentation-only checkpoint; it does
not change that canonical design revision.

- [Architecture at the published revision](https://github.com/aws-e/adp/blob/ed3e2b23f5e0ce9be7fb45060214b0fb4800b6fc/docs/design/story-bound-repositories/architecture.md)
- [All 14 implementation contracts at that revision](https://github.com/aws-e/adp/blob/ed3e2b23f5e0ce9be7fb45060214b0fb4800b6fc/docs/design/story-bound-repositories/backlog.md)
- [Complete artifact/deployment audit](audit.md)

The epic and all 14 child **Design sections were edited**, not merely commented
on. GitHub readback exactly matches the intended bodies. Their non-Design sections,
titles/states, stable EC/AC/SCN IDs, native parents and dependency sets are unchanged.
The epic remains open with its deployed/live-verified completion boundary.
#5287 remains a separate child of #4191; no access hold or relationship was changed.

**Design status for every row:** architect-reviewed; pending existing design
review. **Human approval reference:** none recorded. The PR is ready for review,
not a claim of implementation, approval, deployment or live acceptance. After an
actual approval, synchronize its accepted revision and approval reference to each
child through the existing process. No additional approval gate is introduced.

## Per-story update matrix

All rows reference canonical revision `ed3e2b2` (full hash above). “None” in the
last column means no additional story-specific architectural decision is left
unspecified; it does not waive the shared design review or dispatch prerequisites.
Requester-designated executors/coordinator and authoritative consumer lineage
metadata remain unresolved across the backlog.

| Child / owned design | Revision / status | Native dependencies (unchanged) | Verified body bytes | Specific remaining input or integration check |
|---|---|---|---:|---|
| [MR-01 / #8395](https://github.com/aws-e/adp/issues/8395) — [contract](backlog.md#mr-01) | `ed3e2b2` / architect-reviewed, approval pending | None | 7345 | Recheck concurrent project-binding changes before integration. |
| [MR-02 / #8396](https://github.com/aws-e/adp/issues/8396) — [contract](backlog.md#mr-02) | `ed3e2b2` / architect-reviewed, approval pending | #8395 | 7218 | None; explicit verified connection selection is specified. |
| [MR-03 / #8397](https://github.com/aws-e/adp/issues/8397) — [contract](backlog.md#mr-03) | `ed3e2b2` / architect-reviewed, approval pending | #8396 | 7039 | None; compatible worker/consumer release must be tested. |
| [MR-04 / #8398](https://github.com/aws-e/adp/issues/8398) — [contract](backlog.md#mr-04) | `ed3e2b2` / architect-reviewed, approval pending | #8397 | 7093 | Reconcile PR #8428 recovery changes if merged. |
| [MR-05 / #8399](https://github.com/aws-e/adp/issues/8399) — [contract](backlog.md#mr-05) | `ed3e2b2` / architect-reviewed, approval pending | #8397 | 6885 | None; parent delegation intersection is specified. |
| [MR-06 / #8400](https://github.com/aws-e/adp/issues/8400) — [contract](backlog.md#mr-06) | `ed3e2b2` / architect-reviewed, approval pending | #8396 | 7161 | Actual origin/tracker references are supplied per accepted flow. |
| [MR-07 / #8401](https://github.com/aws-e/adp/issues/8401) — [contract](backlog.md#mr-07) | `ed3e2b2` / architect-reviewed, approval pending | #8396 | 7150 | None; legacy publication conflicts are held, not guessed. |
| [MR-08 / #8402](https://github.com/aws-e/adp/issues/8402) — [contract](backlog.md#mr-08) | `ed3e2b2` / architect-reviewed, approval pending | #8401 | 6979 | None; API-before-frontend rollout is specified. |
| [MR-09 / #8403](https://github.com/aws-e/adp/issues/8403) — [contract](backlog.md#mr-09) | `ed3e2b2` / architect-reviewed, approval pending | #8395 | 7168 | None; new contract family/modes and unavailable-adapter behavior are specified. |
| [MR-10 / #8404](https://github.com/aws-e/adp/issues/8404) — [contract](backlog.md#mr-10) | `ed3e2b2` / architect-reviewed, approval pending | #8398, #8403 | 7098 | None; exact-set/source verification is specified. |
| [MR-11 / #8405](https://github.com/aws-e/adp/issues/8405) — [contract](backlog.md#mr-11) | `ed3e2b2` / architect-reviewed, approval pending | #8404 | 7401 | Actual approved compatible environment contract is required for runtime execution. |
| [MR-12 / #8406](https://github.com/aws-e/adp/issues/8406) — [contract](backlog.md#mr-12) | `ed3e2b2` / architect-reviewed, approval pending | #8399, #8400, #8402, #8405 | 7392 | Live stored-record provenance remains uninspected; dry-run tooling is an implementation deliverable. |
| [MR-13 / #8407](https://github.com/aws-e/adp/issues/8407) — [contract](backlog.md#mr-13) | `ed3e2b2` / architect-reviewed, approval pending | #8406 | 7277 | Reviewed owned-fixture config/access and limits are required for a live run. |
| [MR-14 / #8408](https://github.com/aws-e/adp/issues/8408) — [contract](backlog.md#mr-14) | `ed3e2b2` / architect-reviewed, approval pending | #8407 | 7362 | Requester must name operator and verify target/role/access, installed revisions and budgets. |

Epic #8394's verified body is 7966 UTF-8 bytes; the maximum across all
15 bodies is 7966 bytes. The stricter 8,000-byte bound
was checked, not only 8 KiB. All children remain medium-sized. No scope or native
dependency change was necessary for this architecture.

## Coverage against the issue contract

| Required section | Design coverage and remaining boundary |
|---|---|
| Plain-terms problem | One flow coordinates repositories without conflating issue/PR identity; no flow execution repository or multi-repo story. |
| Description | All 14 owned outcomes and completion boundaries retained; architecture/docs/backlog only. Missing delivery/catalog metadata remains explicit. |
| Impact analysis | Tenant/access isolation, narrow grants, replay duplication, stale evidence, wrong-target commands and finite limits covered. |
| Design | Verified reusable fields/claims/PR bindings; concrete data/API contracts, alternatives, recovery and source citations; full artifact inventory delivered. |
| Deployment | Gateway/tick/worker/frontend packaging, contract trigger gaps, additive migration, fresh/upgrade config retirement and compatible rollback mapped. No deployment executed. |
| Validation | All child AC/SCN IDs retained; each mapped to independent entry-point assertions and pre-merge/live ownership. All six ECs mapped to integration oracles. Runtime results remain not run. |

## Verification performed

From `/work/repo` on `agent/issue-8394`:

- Source inventory regeneration and exact directory comparison: identical output
  for all 11,543 tracked artifacts, 2,106 entry candidates, 135 migration files and
  the global-reference inventory. No unclassified baseline artifact remains.
- `python3 docs/design/story-bound-repositories/validate.py`: validates inventory
  path/blob equality and all local/pinned-source links and section anchors.
- `python3 scripts/check-public-docs.py`: zero findings on the staged bundle.
- Python compilation of `audit.py` and `validate.py`: passed. No application code
  is imported/executed by these checks.
- `git diff --cached --check`: passed after quoting TSV fields to avoid trailing
  empty-column whitespace. The initial formatting failure was corrected.
- Existing `modules/insights/authoring/validate_issue.py --stage draft`: all 15
  prepared issue bodies passed. Draft-stage checking reflects unresolved dispatch
  metadata, not a draft PR or acceptance approval.
- GitHub readback: all 15 exact body matches; all 14 native parents and dependency
  sets match the initial snapshot; native children remain exactly #8395–#8408.
  Non-Design sections and stable criteria were compared before/after.
- PR state/base/head verified through GitHub: ready, open, targeting `main` from
  the requested existing branch. No merge or force push performed.

No application tests, image builds, migrations, AWS operations, paid model runs
or live qualification were performed. Static inventory is not runtime coverage;
external/generated artifacts and installed revisions remain unverified as stated
in the audit. The next action is the existing design review of PR #8434, followed
by approval-reference synchronization and separately authorized implementation.
