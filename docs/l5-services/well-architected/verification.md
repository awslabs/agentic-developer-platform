# Verification and design handoff

## Status

Canonical architecture: [architecture.md](architecture.md). Design review is
complete; [review.json](review.json) records the accepted revision and actual approval. The architecture,
complete audit, typed input proposal and seven implementation slices are prepared
for one ready design PR. Native child IDs, immutable canonical revision and body
hashes are recorded in [issue-alignment.json](issue-alignment.json) after filing.
Neither design publication nor story filing completes live acceptance.

## Reproducible pre-review checks

Working directory: repository root (`/work/repo` in the authoring run). Source
baseline: `388584bdd80a4a5ff142abe3234e7d1eac1cfd64`. Use Python 3.12+ with PyYAML,
jsonschema and openapi-spec-validator. The audit also requires the pinned main and
open/historical PR Git objects listed in `audit_inventory.py`. No cloud credentials
are needed. The authoring run uses an isolated `/tmp/wa-design-venv`; it is not a
runtime dependency or a tracked artifact.

```sh
python3 docs/l5-services/well-architected/audit_inventory.py --check
python3 docs/l5-services/well-architected/validate_design.py
python3 docs/l5-services/contracts/v1/validate.py
python3 scripts/check-public-docs.py
git diff --check
```

The first check regenerates and compares the complete pinned inventory, including
all overlay changed paths. The second independently compares tracked path sets,
resolves local links/headings and immutable repository source links, checks the
proposed typed inputs/negative cases, dependency acyclicity and all epic/story
acceptance mappings. Filed body snapshots additionally require six sections,
8 KiB limit, matching canonical revision, stable AC/SCN IDs and body hashes.
The shared validator verifies existing schemas/examples/operation matrices and
regression contract fixtures. These prove source/format consistency only, not
provider eligibility, authorization enforcement, live source consumption or client
availability. Publication scans are complemented by manual review for private
identities; example references use fictional names and no operational targets.

[contract-proposal.json](contract-proposal.json) is a service-owned **proposal**
for strict RunInput and the `well_architected` settings namespace, not an installed
registration or a replacement shared schema. Draft settings may omit readiness
prerequisites. The [request example](request-example.json) combines a repository
commit and architecture document; it is schema-checked synthetic input and must
not be submitted as a real target. RecordRef shape does not grant access; semantic
scope, role, source and policy tests remain implementation acceptance. WA01 owns
integrating these types into shared catalog/OpenAPI/MCP/compatibility artifacts.

## Check record

Initial construction check caught a missing verification link before this file
was added. The pinned overlay scan also exposed shallow-history ancestry for the
historical helper; the audit now records explicit inspected PR base revisions
instead of guessing a merge base. These are authoring-check findings, not runtime
faults. Pre-review results:

- Pinned inventory regeneration/check passed: 12,710 tracked paths, 2,806 entry
  artifacts, 161 workflows and 116 changed overlay paths.
- Service validator passed: 84 local/immutable source links, 11 input cases,
  26 stable acceptance rows and independent tracked-path equality.
- Shared contract validator passed: 97 schemas, 31 operations, 11 schema fixtures,
  20 specification scenarios, 21 regression tests and 172 local links.
- Public documentation scan passed: 1,830 files, zero findings; supplemental L5
  artifact scan passed. `git diff --check` passed.
- Seven prepared issue bodies passed the repository's **draft** format check.
  Execution-ready lineage is intentionally not claimed: Module/Feature IDs are
  still unmapped. The ready-stage check reports those missing mappings; WA01's
  catalog-negative acceptance row also intentionally mentions unresolved mapping.
  All stories now reference actual approval; phase-specific prerequisites remain explicit.

Native filing is verified: WA01 [#9098](https://github.com/aws-e/adp/issues/9098),
WA02 [#9099](https://github.com/aws-e/adp/issues/9099),
WA03 [#9100](https://github.com/aws-e/adp/issues/9100),
WA04 [#9101](https://github.com/aws-e/adp/issues/9101),
WA05 [#9102](https://github.com/aws-e/adp/issues/9102),
WA06 [#9104](https://github.com/aws-e/adp/issues/9104),
WA07 [#9105](https://github.com/aws-e/adp/issues/9105). All seven native parents and
exact issue body readbacks match the local snapshots/hashes; every story remains
open. The epic's eight original acceptance-rule texts are preserved, with real
child/AC links. The canonical architecture/input proposal is pinned at
`109fd8df85783557ea0efc4bba0ada53355f5c74`; the final PR also includes the native
alignment and complete newer-main delta. No approval is inferred from that pin.

After filing, the service validator passed 101 links, 11 input cases and 26 ACs;
the shared validator again passed all 21 tests, 97 schemas and 31 operations
(173 local links). All seven actual body snapshots passed draft format checks;
public documentation scan passed 1,849 files with zero findings, and supplemental
L5 scan passed 72 files. The complete follow-up main tree of 12,747 artifacts is
accounted for by the original inventory plus all 55 delta paths. The final
publication commit/PR is reported in the issue outcome.

The architect connection could not resolve project 4. The coordinator subsequently
added all seven children and independently verified membership on 2026-10-27;
that administrative gap is resolved.

The public botocore model was fetched on 2026-10-27; its last file revision is
`3d3e64b320672d27f348375a1b4620a5625e25d8` (SHA-256
`bcba9531f89a168c5fa4f725bea65f8fab67d021f5aa201d6c89b6e9e814fceb`);
the immutable fetch matched the inspected develop-branch bytes. Inspected operations include Agent
Profile/context/goal setup, Start/Get/List generation, Get/List recommendations
and item listing. `StartAgentRecommendationGeneration` has no clientToken; no
Agent generation stop operation appears. Source `additionalContext` remains an
untyped Document, so its source wire format, limits and ingestion evidence are
unqualified here. No AWS call or provider generation was performed.

## Review and remaining ownership

| Outstanding item | Owner / next action | What remains unproven |
|---|---|---|
| Canonical approval | Recorded in [review.json](review.json); all seven stories reference the accepted revision | Design approved; runtime/live acceptance remains pending |
| Roadmap membership | Coordinator verified all seven children in project 4 on 2026-10-27 | Resolved; original connection lookup failure is historical |
| Catalog IDs | WA01 with shared #8683 and PranavSharma1000 verifies registered Module/Feature mapping | Dependent dispatch readiness, not document completeness |
| Support/region/source API qualification | WA01; WA07 supplies eligible private fixture under existing authority | Affects live activation and EC-01/03/08; independent development can continue |
| Provider identity/read consistency and unsupported cancellation | WA01/WA04 native fault proof; WA03/WA07 retention proof | EC-04, including late outcome collection and no duplicate Start |
| Consumer/release integration | WA05 with #8693/#8149 and Insights; WA07 with #8698 release owner | Context/Insights acknowledgement/revocation and later reuse, installed private worker/client parity |
| Portable results and cleanup | WA07 at exact installed artifact identities, accepted by PranavSharma1000 | Two non-ADP native reviews, independent comparisons, retained results/sources, owned absence and costs |

No production implementation, deployment, merge, provider job, Support purchase,
IAM/billing change, new allowance or agent dispatch is part of this design run.
Unknown costs and blocked/not-run live acceptance remain unknown/incomplete.

## Installed compatibility observations

`modules/L5-services/scripts/qualification_well_architected_compatibility.py`
collects a bounded part of WA07/AC-01 and SCN-01 (original board tasks
`AC-01-t2` and `SCN-01-t1`). It reads one existing Security run/result and one
DevOps run/result through their authenticated Product APIs, plus both services'
scoped recovery-state APIs. Protected Gateway schema readbacks bracket each
collection. It does not deploy, restart, invoke workers or submit provider work.
It retains and compares the complete `run`, `attempts`, `work` and `publications`
envelope. Only the exact DevOps recovery request permits up to 8 MiB; ordinary
reads and Security recovery retain their 64 KiB limit. Response sizes are checked
both while streaming and after compact ASCII-escaped serialization, matching
the DevOps export's bound. Compatibility journals permit 9 MiB for that envelope
and the other bounded records; other journals retain their 256 KiB default.

The private selection contains `schema_version: 1`, the target `source_revision`,
`environment`, an `expires_at` within 24 hours, `records`, and `stages`. Each of
the two records pins `service_id`, `gateway_url`, `token_env`, the three-part
`scope` (`tenant_id`, `product_id`, `project_id`), `run_id`, `result_id` and
`result_revision`. Each stage pins `fixture_path`, `fixture_sha256` and the
existing installed-artifact `runtime` selection. The fixtures use the existing
`InstalledFixture` contract. The first stage must identify the prior release;
the later stages identify the target release. Store all selections and evidence
outside the checkout with private permissions.

Invoke the collector with a stage, private selection path and evidence directory:
`before-transition`, `after-transition`, `after-recovery`, then `verify`.
Capture the first checkpoint before the independently authorized deployment
transition. The collector performs no transition or recovery operation itself.
It refuses missing historical baselines, changed fixture pins, reordered
checkpoints, different scopes or records, and runtime drift during collection.
Retain the directory as `compatibility-checkpoints` within the WA evidence
directory so the shared WA emitter can recheck it against installed artifacts.
New checkpoints use `wa-installed-compatibility-checkpoint/v2` and require both
services' recovery records. Original v1 checkpoints remain readable with their
original selection, hashes and absent DevOps evidence. They are never rewritten
or backfilled. A legacy series without original DevOps recovery remains explicitly
incomplete; mixed old/new records cannot verify as unchanged history. Retain that
journal rather than replacing its historical baseline with current observations.

The resulting `compatibility_progress` is partial evidence. It keeps the original
criterion incomplete: API readability does not prove all old client journeys,
the Product origin is not independently bound to the protected Gateway probe,
and replaced pods do not establish a selected rollback operation. These limits,
worker/transport proof and remaining native acceptance need their own evidence;
the collector does not synthesize them or change task-board statuses.

The remaining source work for complete WA07 compatibility is finite:

1. Collect the pinned old API/CLI/MCP caller journeys and consume manual UI
   observations against the same original records and installed release.
2. Bind the selected Product origin to the protected Gateway runtime proof.
3. Verify an actual selected rollback/recovery receipt against the original
   before/after runtime and durable records, including retained history.
4. Compose those proofs with installed worker, queue repair, transport and
   workflow-ordering evidence under the unchanged WA07/AC-01 and SCN-01.

Completing this intermediate collector alone does not resolve that criterion's
blocker or justify a native release retry.
