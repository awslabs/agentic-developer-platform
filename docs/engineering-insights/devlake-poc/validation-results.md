# Design package validation results

This report concerns local validation of the [architecture package](architecture.md), not live POC acceptance. The requesting owner authorized merging the corrected design on 2026-10-27. The ready design PR and required epic handoff record publication and remote issue readback; the immutable design links there identify the published revision without a self-referencing commit hash in this file.

## Architect validation at original PR head

The following records are historical results from the architect at `28a23f12664bc12e2e4a106ae080fb589b6bffcd`. The coordinating reviewer did not rerun the 151 application/schema/authoring tests for these documentation amendments.

Inspected application revision: `aeba89070ec685b41a4902a4441bfe47449e48ba`; working directory `/work/repo`, branch `agent/issue-8887`, 2026-10-27. Documentation checks exercise the working design package; the publication commit is available from the PR and immutable issue design links.

| Check actually run | Result / evidence boundary |
|---|---|
| Existing Insights unit/release-contract suite | 93 tests passed, Python 3.13; includes local synthetic HTTP/CLI fixtures, not deployed service proof. |
| Existing issue-authoring suite | 25 tests passed. |
| Initial schema test run | Failed to import five modules because system Python lacked `jsonschema`; no live feature failure inferred. Isolated temporary environment with declared module requirements used for recovery. |
| Schema tests after dependency recovery | 33 tests passed using `/tmp/issue-8887/schema-venv/bin/python` after creating that venv and installing `modules/insights/schemas/requirements.txt`. No repository dependency files changed. |
| Markdown/source references and traceability | 129 local links/anchors and 117 pinned ADP/upstream source links resolved; 13 designs cover EC-01–10, 41 ACs and 41 SCNs. External pgx source was separately fetched over HTTPS successfully. |
| Acceptance preservation and issue formats | All original child AC/SCN tables and epic EC table compare verbatim. All 14 prepared bodies pass existing planning-stage format lint; sizes 7,528–8,119 bytes, below 8 KiB. Advisories concern planned tests and independently verified readiness, not format failure. |
| Dependency audit | Native POC edges plus declared shared-capability prerequisites are acyclic. Controlled S12-before-S02 and S13-before-S06 boundaries are preserved. External capability leaves remain explicit owner handoffs. |
| Frozen inventory reproduction | Full ADP/upstream tracked-artifact, entry-candidate, database-client and migration inventories reproduced from pinned Git objects. Gateway migration graph has one source head, `143_flow_read_revisions`; no migration was run. |
| Public documentation | `python3 scripts/check-public-docs.py` checked 1,860 tracked documentation files with zero findings, including the staged design artifacts. Manual content review retains only public source links/path/hash evidence and fictional fixtures. |
| Whitespace/serialization | Initial staged whitespace check caught empty trailing TSV fields. The generator now writes explicit dash sentinels; inventory regeneration/reproduction and the final staged whitespace check pass. |

Reproduce the checks using [the verification procedure](verification.md#reproduce-the-documentation-checks). Inventory classifications are exhaustive for tracked paths and conservative for possible entry points; dynamic/generated/external build inputs and actual deployment/resource state are not asserted verified. No Terraform plan/apply, AWS operation, source-provider collection, proposed Go build, native live fixture, deployment or pilot acceptance was run. S01 named readiness facts, custom package/dialect proof and shared #8373/#8374/#8375 live handoff remain pending.

## Focused validation of review amendments

The coordinating reviewer resolved R1-R3 as recorded in [review amendments](review-amendments.md), retaining the pinned application/upstream evidence. Changed documentation and prepared issue bodies were checked on 2026-10-27 in an isolated checkout of PR #9075.

| Check actually run | Result / evidence boundary |
|---|---|
| Design/issue validator with pinned upstream checkout | Passed: 13 stories, 10 ECs, 41 ACs, 41 SCNs, local references and pinned source references; all 14 prepared issue bodies valid and below 8 KiB (7,133–8,166 bytes). One external pgx reference remains the separately verified reference from the initial review. |
| Exact acceptance/scenario comparison | Every prepared story table equals its revised design table. Seven stories have only the AC/SCN row amendments enumerated in the amendment record; remaining rows and epic EC table unchanged. IDs and test selectors retained. |
| Dependency review | Native plus declared-capability graph remains acyclic. Compensation is separate metadata, not a native prerequisite: S09 artifact-finalizer bootstrap precedes S02 runtime handlers; neither waits for S07/S08 acceptance. |
| Source inventory reproduction | Pinned ADP/upstream inventories checked independently; no application source, migrations or generated inventory changed. |
| Public documentation and whitespace | Public-documentation scan and git diff whitespace checks passed; new amendment record included in final staged scan. |
| Focused semantic re-review | B1/B2 crash/replay reads durable sealed bytes; runtime schema checks cannot require worker DDL; failed/aborted/expired pilots have a named independently reachable finalizer and evidence path. These are design resolutions, not implemented test results. |

Immutable publication links identify the amended commit without a self-referencing hash in this file. Remote publication verification checks exact issue-body equality, open state, unchanged native parents/dependencies and resolvable design links before merge. Actual final-head CI and merge status are available in PR #9075. No cloud/provider operations, custom Go builds, migrations, flow activation or live acceptance were performed.

## Optional deployment amendment validation

The owner subsequently required connector reuse to be independently optional, with a deployment switch and separate infrastructure. The [amendment record](optional-deployment.md) identifies source evidence and all changed/new criteria; the earlier results above remain historical.

| Check actually run | Result / evidence boundary |
|---|---|
| Design/source-reference and issue validator | Passed with the pinned upstream Git objects: 13 stories, 10 ECs, 44 ACs and 44 SCNs. All prepared bodies pass planning-format validation and remain below 8 KiB (7,099–8,176 bytes). |
| Exact story/issue table comparison | All 13 tables match. Existing 41 AC/SCN pairs remain; three new pairs belong to S02, S10 and S11. Six other stories strengthen the rows recorded in the amendment. Existing selectors/states remain; new tests are planned. |
| Epic/dependency review | All EC IDs and native parent/dependency sets remain. EC-01/02/08/09 now cover optional modes and new child criteria. Connector-only adopter deployment does not erase full POC acceptance requirements. |
| Public documentation / whitespace | Final staged public-documentation scan and whitespace checks pass, including the new optional-deployment document. |
| Semantic review | Default-off baseline, connector-only independence, separate root/state ownership, direct-call flag enforcement, disable fencing and removal while off are explicit. Existing IAM-only authentication, immutable seals and private-evidence restrictions remain. |

No application tests, Terraform plan/apply, proposed Go builds or live mode-transition tests were run for this documentation change. Historical inventory TSVs are unchanged; the supplemental feature/deployment references are pinned separately. Publication verifies immutable files and exact issue-body/native-relationship readback; the follow-up PR records CI and merge state separately from the already merged PR #9075.

## Engineering Insights consolidation validation

The POC design and delivery flow now share
`docs/engineering-insights/devlake-poc/`, with links from the Engineering Insights
and delivery-flow indexes. Relative links, commands, story mappings and planned
documentation paths use this location. Historical source URLs and the seven
frozen inventory TSVs retain their exact bytes.

The relocated package passes validation for 13 stories, ten ECs, 44 AC/SCN pairs,
269 local links and 130 pinned source links. All 17 prepared epic, feature and
evaluation bodies remain below 8,192 bytes (largest: 8,175); feature/epic bodies
pass the repository's planning-format check. Native parents and dependencies
remain unchanged. Flow validation preserves 18 nodes, 42 edges, three machine
evaluations and 135 planned implementation files without unordered writers.
Public-documentation and whitespace checks pass. These are authoring checks;
the delivery flow remains inactive and live acceptance is still outstanding.
