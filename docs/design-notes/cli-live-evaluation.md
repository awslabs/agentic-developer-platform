# CLI live evidence adapter and producer handoff

The engine accepts a source-pinned, complete CLI qualification through
`cli-live-evaluation/v1`. It verifies GitHub source/PR/check evidence and
authenticated workflow artifacts, then records the observed live qualification
in a distinct `cli-live-evaluation-receipt/v1` with `live_attestation: true`.
The node passes only when every required assertion passes. This adapter does not implement the scenarios,
create deployment fixtures, or turn an existing partial report into acceptance.

The normative wire models are
[`cli_live_contract.py`](../../modules/gateway/src/orchestration/cli_live_contract.py).
Generate schemas from `CliLiveSpecification.model_json_schema()`,
`QualificationManifest.model_json_schema()` and `SuiteReport.model_json_schema()`
using the gateway environment. Extra fields are forbidden. All references to
SHA below mean full immutable hashes, not branch names or abbreviated commits.

## Scope and dependency order

`qualification.owner_issue` is explicit and must equal the accepted evaluation
node's `issue_ref`. The allowed shapes are:

| Owner issue | Complete required criterion set | Purpose |
| --- | --- | --- |
| `5329` | `5329/validation-01` through `5329/validation-08` | Existing external prerequisite's live acceptance |
| `5331` | `5331/validation-01` through `5331/validation-07` | Existing external prerequisite's live acceptance |
| `5644` | All 124 identifiers below | Final CLI qualification |

The two prerequisite receipts unblock their existing evaluation nodes before
the integrated CLI journey #5630 can run. They cannot satisfy #5644. External
#5564 uses the ordinary repository evaluator for its six offline implementation
criteria; this CLI adapter does not authorize live execution for #5564 alone.

The final set is 103 story criteria plus 21 external criteria:

- #5516: `5516/AC-01` through `5516/AC-09`.
- #5589: `5589/AC-01` through `5589/AC-10`.
- #5621 through #5641: four criteria each, named
  `<issue>/CLI-<issue minus 5613, two digits>-AC-01` through `-AC-04`.
- #5329: eight `validation-NN` identifiers; #5331: seven.
- #5564: `5564/AC-01` through `5564/AC-06`.

`validation-NN` are explicit mapping anchors for the eight/seven source
checklist statements, not newly invented official AC labels. Every criterion
includes the original whole-issue body hash, exact statement hash, accepted
phase(s), and case mappings. Original prose and shared/live requirements still
apply. The configuration reviewer must verify that mapping; the engine checks
its immutable identity and execution evidence rather than interpreting prose.

#5516 AC05–09, #5589 AC05–10 and CLI08–28 AC04 require live mappings. #5564 is
strictly pre/live-spend-free implementation evidence. Other phase assignments
must be explicit and reviewed. #5568 is the profile story, not the integrated
CLI journey owner. Retained live holds on #5181–#5185, #5199 and #5413 remain
separate and are not closed by this adapter.

## Accepted source and execution configuration

The runner adapter is `engine-cli-live-evidence-v1`. Its
`qualification_config_path` and `requirements_path` must be safe JSON paths
under `tests/e2e/cli_regression/` at the accepted workflow source revision.

The configuration file is exactly the accepted `Qualification` model. The
requirements file has exactly these two fields:

```json
{
  "evidence_schema": "cli-live-requirements/v1",
  "criteria": [
    {"criterion_id": "<accepted ID>", "source_text": "<exact source statement>"}
  ]
}
```

Its exact bytes hash is accepted in `requirements_sha256`. The file must contain
the whole required set for the selected owner. The observer fetches every
referenced GitHub issue and verifies the current whole-body hash and exact
statement text/hash. A changed source issue requires a new reviewed contract.

Each mapped case binds suite, phase, actor ID, exact redacted command, typed JSON
expected result and whether inference is required. Live operations use the
served `adp` CLI. One shared case cannot claim different contracts for different
criteria. Both the approved ordinary user and admin identity must be exercised;
additional service-account actors can be explicitly mapped. At least one live
case must exercise metered inference. The producer must use real approved
fixtures with those exact identities; dynamically generated unknown identities
cannot be substituted after acceptance.

Every source issue in the mapping must also have delivered PR/check evidence,
through a direct story source or an explicit `external_pull_requests` binding.
For final qualification this includes all 23 story issues and #5329/#5331/#5564.
All delivered merges must be ancestors of both the workflow source and the
accepted deployed gateway revision. Reading an external PR does not acquire its
coding claim or redispatch the implementation.

The accepted deployment includes account, region, HTTPS gateway, deployment ID,
full gateway revision/image digest, served CLI release hash, hashes of actual
installed files, worker revisions, tenant and ordinary/admin fixture IDs.
Fresh runtime evidence must prove that target, the actual shared EC2 instance,
and unchanged gateway revision before/after execution. A Lambda/ECR revision
assumption is not proof of the gateway or installed CLI revision.

## Independent guard and cleanup evidence

All live suites share one EC2 instance and the accepted `shared_meter_ref`.
Bounds permit at most 60 minutes and $5 daily inference spend, with explicit
request, input-token, output-token and per-request output-token ceilings.
Prerequisite and final runs must use the same real daily meter for the target;
creating a fresh run or acceptance cannot reset daily spend. The producer owner
must implement an independently enforced guard that refuses new activity at
these limits and preserves cleanup authority. A declared budget, scenario-side
counter, workflow timeout, or summary label alone is insufficient enforcement.

Each guard snapshot records its actual enforcement identity, exact accepted
limits, UTC day/window, before/after daily spend, peak instance count and full
operation/request inventory. Operations bind suite/case/actor/command and
execution timestamps. Requests bind actual request IDs, terminal timestamps,
input/output tokens and accounted cost. Totals must join the request inventory;
required-inference cases must have requests. Operations and request IDs cannot
be duplicated or silently omitted from the parent/child join. The producer
must collect these from the real guard and provider operations, including
created resources and changed configuration keys.

Every case has execution timestamps, actual expected/observed typed JSON and a
first evidence file containing its complete command record without the
`evidence` field. Remaining referenced files are hash checked too. A `passed`
label with a different observed result is a failed criterion. Changing the
accepted expected result, actor, command or execution path is rejected.

Each suite's cleanup observation must follow every case's completion and prove
disposition of every created resource and restoration of every changed
configuration key. Parent cleanup follows all child completions, covers their
combined inventories, has no unresolved operations, and explicitly proves the
shared EC2 instance terminated. Offline suites have no live runtime/guard and
no live operation references. Failures and partial runs retain diagnostics and
cleanup evidence without claiming full acceptance.

The engine reads GitHub, not AWS credentials. Runtime/guard/cleanup files are
attestations emitted by the reviewed immutable producer source. Their structure,
hashes and cross-file joins prevent contradictory summaries, but cannot turn a
producer that fabricates provider observations into trustworthy evidence. Source
review must verify the actual external observations and independent enforcement.

## Parent/child artifacts and dispatch

The existing parent is `.github/workflows/nightly-cli-regression.yml` with its
unchanged `0 5 * * *` schedule, manual dispatch and pull-request checks. Both
observation and producer preflight enforce those events and the existing cron.
Security/repository scan producers remain dispatch-only.

The parent publishes `cli-live-qualification/v1`, with `partial: false`, exact
repository/run/attempt/workflow/requirements/qualification identities and all
child references. Each child publishes `cli-live-suite/v1`. The engine verifies
the child workflow path/revision against GitHub's referenced reusable workflows,
the provider job ID/name, archive identity, exact report bytes hash, complete
case set and shared runtime/guard/cleanup. Every artifact name includes
`{run_attempt}` and must be accepted with its exact report path. The parent and
all child reports are mandatory artifacts, not optional Markdown summaries.

Observation-only acceptance can adopt the latest applicable scheduled or manual
qualification at the exact source SHA. It never dispatches, and rechecks the
latest run after downloading artifacts. A newer failed/pending run prevents
selection of an older green run. Pull-request diagnostic runs are ineligible.

An optional one-shot dispatch requires the existing acceptance preview/commit
API with explicit `authorize_cli_qualification_dispatch: true`, the exact
`producer` target/inputs/manifest and approved evaluate authority. The scan grant
`authorize_workflow_dispatch` cannot authorize CLI qualification, or vice versa.
The original worker plan, policy expiry and spend meter remain in force.

Required producer inputs include `expected_account_id`, `region`,
`qualification_contract: cli-live-qualification/v1` and `qualification_sha256`,
the SHA-256 of canonical JSON of the accepted Qualification. The reviewed workflow
also implements the existing correlation transport (`adp_correlation`,
`adp_source_revision`, `adp_definition_revision`), noncancelling concurrency,
run-name and context artifact protocol described in
[`repository-scan-producer.md`](repository-scan-producer.md).
`cli_qualification_dispatch` and `cli_qualification_context` use the existing
durable action/execution ledger. An unresolved POST is reconciled, never blindly
resent. Dispatch recovery accepts only its correlated manual run. Cleanup and
truthful terminal settlement can be recorded after authority/spend expiry without
authorizing another dispatch. Replacing an accepted CLI contract with ordinary
repository evidence requires a plan amendment.

## Remaining producer work

The current nightly reports do not satisfy this contract. The inspected run
35514767448 was partial, login-scoped and passed only E01/C01. Its preparation,
EC2 and deployed revisions did not establish this contract's exact identity.
The present harness defaults/allowed instance-duration configuration and declared
daily budget do not demonstrate the required independent shared limits.

The CLI producer owners still need to implement the full mapped scenarios and
fixtures (including hosted/destination/GitHub/three-deployment cases and E16/E17
hard token/request limits), the immutable accepted configuration/requirements
files, real guard observations, complete JSON parent/child reports, cleanup
verification and optional correlated dispatch transport. Existing schedules and
reports remain operational; they must not be relabelled as full evidence.
Accept fresh harness/config/source hashes after deploying the adapter. Until the
producer and real evidence exist, prerequisite/final evaluations stay blocked.
