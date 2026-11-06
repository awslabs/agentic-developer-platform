# Consistency and verification record

This design is a proposal. Offline checks establish document/contract consistency,
not provider acceptance, implementation completeness or live deployment. The
governing source baseline is recorded in [the audit](audit.md); actual review and
publication evidence is recorded after the design commit is published.

## Cross-story consistency review

| Contradiction or ambiguity | Resolution and affected slices |
|---|---|
| Pinning in worker versus at admission | SA05 resolves immutable identities before acceptance; SA06 only materializes/hashes those identities and seals the manifest before provider effects. |
| Setup requires ready binding | Setup operation has optional binding ID and its own lifecycle; SA04 can create/check before run eligibility. SA06 provides generic bounded execution. |
| MCP narrower than CLI/binding lifecycle | SA14 covers the full operation matrix, including update/suspend/resume/unlink/cleanup, reconcile/triage and publication recovery. |
| Proposed MCP names and run IDs conflict with existing host guards | Use existing `l5___snake_case` namespace convention and `assessment_ref` target parameter; keep the global authority-argument rejection intact. |
| Completion versus publication | SA10 stores result and SA11 publication intent atomically; retry never repeats assessment. |
| Persistent spaces versus run cleanup | SA04 owns persistent assets; run cancellation never deletes them; separate owned-only cleanup requires authorization/readback. |
| Gateway filters versus new module | SA15 updates both workflow trigger filters and internal change detection, wheel staging, migration search and explicit Lambda jobs. |
| Profile reference versus duplicate Product storage | SA03 stores immutable references/provenance and conditional handoff; SA02 is the sole delegated Product API boundary. No direct Profile SQL. |
| Human attribution versus service authority | SA02 requires registered verified service identity and current scope/operation delegation; root-human IDs do not grant access. |
| Scope history versus current revocation | All slices recheck current source/visibility for reads and effects, including aggregate/artifact/Context paths. |
| Document spike success versus code-review source failure | SA07 starts from reported document proof; SA04/SA08 must qualify source access. Neither open PR is assumed merged. |
| Claiming exactly-once despite uncertain native response | SQL dedup gives semantic admission, not provider omniscience. Unknown submits stay blocked from new starts until reconciled. |
| Shared L5 epic versus Security completion | #8150 references the reusable design but keeps other service deliverables and acceptance open. |
| Review-ready design versus live-only evaluation | Pre-merge contracts/fixtures do not require paid/live checks; SA15/SA16 retain deployment/live rows and incomplete prerequisites. |
| Trigger lacks capability/binding selection | SA03/SA09 save explicit targets; SA05 admits them deterministically, including two-capability/two-binding fixtures. |
| Product source metadata mistaken for content | SA02 implements the proposed pinned-content API/connector boundary; SA05 stores receipts and SA06/SA07/SA08 verify exact bytes and manifests. |
| Setup adapter only receives status projection | SA04 persists `SetupIntent`; SA06 passes it with pinned config/binding, current attempt and verified authority. |
| MCP edits lack an obtainable conditional token | SA14 copies HTTP ETag into the typed JSON/text reply envelope; SA12/SA13 share the token and explicit stale-edit behavior. |
| Ten-minute polling could delay new runs | SA05/SA06 publish immediately after commit; SQS delay applies to future polls. The one-minute EventBridge sweep is recovery only. |
| A timestamp was treated as a heartbeat | SA07/SA08 preserve available AWS phases/tasks/errors with coverage; SA06 distinguishes poll freshness, suspected stalls, provider failure and runtime limits. |

## Acceptance coverage

| Epic criterion | Existing story acceptance / scenarios | Required independent verification |
|---|---|---|
| EC-01 configuration | SA01 AC-01–03; SA03 AC-01–03 / corresponding SCN-01–03 | Real API/version readback, stale edit race, zero provider calls on save/refresh; contract fixture validation. |
| EC-02 scope and binding | SA02/SA04 AC-01–03 / SCN-01–03 | Two-tenant/Product denial, current service/human rights, native linked/owned resource readback and lost-create reconciliation. |
| EC-03 execution/recovery | SA05/SA06/SA09 AC-01–03 / SCN-01–03 | SQL transaction faults, real SQS redelivery/ack loss, failed repair tick, source order, deadline/revoke/cancel and no second native job. |
| EC-04 threat model | SA07 AC-01–03; SA16 AC-02 / corresponding scenarios | Real native job plus exact consumed documents and independent claim rubric. |
| EC-05 static review | SA08 AC-01–03; SA16 AC-02 / corresponding scenarios | Exact ZIP/manifest, native job/source consumption, both static-only mode fields, complete/partial coverage and independent controls. |
| EC-06 findings | SA10 AC-01–03 / SCN-01–03 | Native IDs/uncertainty, current list/count/artifact visibility, human triage history and compatible comparison or explicit incomparable result. |
| EC-07 Context reuse | SA11 AC-01–03; SA16 AC-04 / corresponding scenarios | Stored result survives failure; durable consumer receipt; later authorized agent retrieval and revocation invalidation, without duplicate provider work. |
| EC-08 clients | SA12/SA13/SA14 AC-01–03; SA16 AC-01 / corresponding scenarios | Served UI, installed CLI and actual MCP discovery/dispatch read the same revisions; stale-client conflicts and replay recover original receipt. |
| EC-09 delivery | SA15 AC-01–03; SA16 AC-03 / corresponding scenarios | L5-only release selection, same wheel digest, queued Lambda invocation, rollback/repair, effective privilege rejection and owned-only cleanup. |

All original issue AC/SCN identities remain authoritative; this table groups them,
not renumbers them. Every attempt must retain source/package/schema revisions,
fixture identity, selected scope, input hashes, outcome and evidence. Keep private
deployment identities and raw evidence out of this public repository.

## Test boundaries

1. **Design/offline:** strict JSON Schema and OpenAPI validation, valid/invalid
   examples, client matrix parity, link/anchor checks, deterministic inventory,
   six-section issue-format checks and identity preservation. Cross-scope/state
   fixtures are specification examples, not tests of real handlers.
2. **Module pre-merge:** use deterministic clock, isolated tenant/Product data,
   controlled transport faults, fixed source bytes and recorded native mock pages.
   Independent assertions inspect stored versions/work/receipts and provider call
   counts, rather than comparing two serializers from the same implementation.
3. **Host pre-merge:** exercise real auth/router/broker boundaries and installer/tool
   transports; SQL rollback/lease races; artifact revocation and Profile compatibility.
   Build/import the wheel outside the checkout in gateway and Lambda layouts.
4. **Live SA15/SA16:** explicitly authorized private fixtures and spend/time/attempt
   limits; real queue/provider/source readback; all findings if fewer than three,
   otherwise at least three independently assessed; cleanup and retained resources
   listed. A mock, source inspection or successful deployment workflow is not this proof.

## Reproduction

From `/work/repo` (or the checkout root), Python 3.12+:

```sh
python3 -m venv /tmp/l5-design-venv
/tmp/l5-design-venv/bin/pip install PyYAML jsonschema openapi-spec-validator
/tmp/l5-design-venv/bin/python docs/l5-services/audit_inventory.py --check
/tmp/l5-design-venv/bin/python docs/l5-services/contracts/v1/validate.py
python3 scripts/check-public-docs.py
git diff --check
```

The inventory requires the pinned main Git object. Run the existing issue checker
against each prepared/current body with
`python3 modules/insights/authoring/validate_issue.py BODY --stage draft`.
Draft is the truthful authoring stage while registry mappings/live prerequisites
are unresolved; a ready **design PR** does not imply dispatch-ready implementation
issues. Verify native parent links separately using GitHub's sub-issue API.

## Execution record

Pre-publication checks completed in `/work/repo`:

- 58 JSON Schema definitions, OpenAPI 3.1 and 29 API/MCP operation mappings pass.
- Nine positive/negative schema fixtures and 15 specification scenarios pass;
  these are not real-handler/provider tests.
- 32 local Markdown links/anchors pass; 47 inline baseline source paths and all
  13 changed paths in the two pinned spike PR heads were checked separately.
- Full pinned-tree inventory regeneration matches: 12,019 artifacts, 2,651 entry
  artifacts, 155 workflows, 148 migrations and 2,004 Terraform declarations.
- Repository public-doc checker passes (1,686 files, zero findings). Its default
  scope excludes this module, so the contract validator additionally applies the
  same pattern scanner to all 22 L5 artifact files; zero findings.
- All 18 proposed issue bodies pass draft-format/8 KiB checks; original Validation
  sections and acceptance/scenario identities are preserved. Native child and
  parent-epic relationships were independently read from GitHub.
- Reading main's branch-protection configuration returned HTTP 403, “Resource not
  accessible by integration.” This limits visibility into repository review rules;
  it does not authorize bypass. The PR remains subject to normal review/checks.

Publication/readback evidence is added after execution. No deployment, runtime
implementation tests, paid provider run or Context reuse is claimed. Normal design
approval, registry mapping, delegated service/broker integration, native code
source access, Context receipt/read/invalidation and private live targets remain
with the owners listed in the architecture.

## Original published checkpoint

[Ready design PR #8725](https://github.com/aws-e/adp/pull/8725) targets `main` from
`agent/issue-8682`. The canonical architecture/contracts revision is
`181951c6b3e18d074ab50787030407f02497ca0a`; subsequent evidence-only commits do not
silently change that governing design. Approval is pending, not assumed.

Both epics (#8150/#8682) and all 16 native Security children now link that immutable
revision. [Issue alignment receipts](issue-alignment.json) record each public
issue's before/after body hash, size, acceptance identities, format result and
successful publication readback. Every original Validation section is byte-for-byte
preserved; all bodies remain under 8 KiB. Native Security child membership and
its parent-epic link are verified independently of body text. No story was closed,
no label changed and no implementation agent dispatched.

Validation uses PyYAML 6.0.3, jsonschema 4.26.0 and openapi-spec-validator 0.9.0 in
an isolated local environment. The inventory/contract checks are rerun with this
evidence-only update. GitHub initially reported its Superplane selection check
passed and core check running; that is not a completed CI or design approval claim.
The PR's current Checks/Review tabs remain the authority for subsequent status.

## Review corrections — 2026-10-25

[Review #5640168210](https://github.com/aws-e/adp/pull/8725#pullrequestreview-5640168210)
requested four interface corrections. This revision updates the architecture,
generated schemas/OpenAPI/MCP descriptors and affected backlog slices together:

- Enabled triggers select capability/binding explicitly; disabled drafts can wait
  for setup. Events preserve the selected target and reject stale/unavailable choices.
- Product-owned source snapshots define connector authorization, captured revision,
  bounded content/manifest reads, hashes, retention, revocation and integration owners.
- Immutable `SetupIntent` preserves original link/cleanup input and verified scope;
  worker adapters receive it with pinned configuration/binding and attempt context.
- All MCP tools define success output schemas; conditional clients receive the
  exact HTTP ETag in JSON, including the first empty triage head.

Offline validation passes with 67 schemas, 29 operation/input/output mappings,
nine schema fixtures, 15 specification scenarios and 11 new independent contract
regression checks. The regression checks cover complete/missing trigger targets,
two bindings/capabilities, distinct link targets, cleanup intent, source bounds and
receipt structure, and MCP read/edit/stale-edit transport examples. They do not
implement the runtime integrity, authorization or conditional-write handlers;
the backlog assigns those integration assertions explicitly.

The pinned-tree inventory, local links, public documentation scan and supplemental
scan of all 24 L5 artifacts pass. The corrected immutable design revision is
`89c3ab6046434aa042c67fd0ffade9af4e133f72`. Both epics and all 16 stories now reference
that revision; all 18 bodies pass draft-format and 8 KiB checks. Acceptance/scenario
identities and original Validation sections are unchanged. Native child membership,
Security's parent #8150 and L5's parent #5411 were read back independently.
[Updated publication receipts](issue-alignment.json) retain before/after body hashes.
The original checkpoint above remains historical evidence; these are substantive
contract corrections and still require normal design review.

## Dispatch and monitoring refinement — 2026-10-25

The canonical design now specifies immediate post-commit SQS publication for new
runs and control work, a default 600-second delay for provider status checks, and
a separate one-minute recovery sweep. It retains owed work after failed sends and
uses due times, dispatch/execution leases and idempotency to recover safely.

AWS API documentation and the pinned botocore 1.43.114 service model establish
job status, error, phase/task and timestamp availability. The architecture links
those sources and records their limits: optional fields and no promised heartbeat
cadence. Observation snapshots preserve native failures separately from poll
errors, progress uncertainty, advisory stalls and local deadline expiry.

`MonitoringPolicy`, `EffectiveMonitoringPolicy`, `ProviderObservation` and
`RunMonitoring` align configuration, admission, adapters and all client schemas.
The policy defaults are 600-second polling and 300-second lateness grace; inactivity
advisories require an explicit threshold. Cross-field constraints and actual
dispatch/timing/provider behavior remain implementation checks owned in the backlog.

Offline contract checks include independent fixtures for resolved/default policy,
invalid bounds, optional/unknown/partial AWS observations and a failed poll retaining
the last known running job. These fixtures check interface shape and separation;
they do not claim a deployed dispatcher or live heartbeat qualification. The
publication checkpoint and issue alignment receipts identify the governing revision.

Validation for this refinement passes: 74 schemas, 29 API/MCP operation mappings,
nine schema fixtures, 15 specification scenarios, 15 independent contract regression
checks and 41 local links. Pinned-tree inventory regeneration and the public-data
scans also pass. Published design revision
`4eacbd97edfd6f832f056796bab71745d44ba1ad` now governs both epics and all 16 Security
stories. All 18 bodies passed draft validation and publication readback, preserving
acceptance/scenario IDs, original Validation sections and the 8 KiB limit. Native
child membership and the #8682 → #8150 → #5411 hierarchy were independently read
back. Current body hashes and revision are retained in `issue-alignment.json`.
This checkpoint supersedes the prior design revision; approval remains pending.

## Documentation location — 2026-10-25

All 24 design artifacts now live under `docs/l5-services/`, including the
architecture, backlog, contract specifications, audit inventories and offline
validation tools. `modules/L5-services/README.md` is a short entry-point link;
runtime implementation, infrastructure, migrations and integration tests remain
module-owned. Schema packaging and consumed-contract build selectors now reference
the documentation folder as their versioned source.

Relative links, repository-root discovery and reproduction commands were updated.
The existing contract payloads, examples and pinned inventory data are unchanged.
Validation from the new paths passes: 74 schemas, 29 operations, nine schema
fixtures, 15 specification scenarios, 15 regression checks and 43 local links.
Inventory regeneration also matches. The repository public-document checker now
covers this folder; the supplemental scan covers all 24 design artifacts plus the
module entry point.

Both epics and all 16 stories now reference the relocated files at revision
`5d67d1bc4b1a92f104847f87602996d7080f73bd`. All 18 bodies pass draft-format and
publication readback checks. Acceptance/scenario IDs and acceptance text remain
stable. SA01's three planned runtime integration-test selectors move into the
module's `tests/contracts` directory; all other Validation content is preserved.
Native child membership and the #8682 → #8150 → #5411 hierarchy were read back.
Current body hashes, the selector-only exception and the governing revision are
recorded in `issue-alignment.json`. The repository public-document scan now checks
1,710 files with zero findings. This relocation checkpoint supersedes the prior
location; normal design approval remains pending.
