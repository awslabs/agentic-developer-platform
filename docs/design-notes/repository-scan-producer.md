# One-off repository scan producers (S19/S21)

The final evaluator can dispatch one correlated repository scan after all direct
story predecessors, including S21, have merged. This uses the existing execution
runner, issue claim and action ledger. It does not create a developer worker,
change a story owner, enable a schedule, or claim a deployment/live attestation.
The native read-only observer remains the default.

S19 owns adapting the existing scanner workflow and collecting trustworthy
coverage. S21 owns the final image/provenance pins, dispositions and reconciliation
report. Implement the protocol below in those story PRs. No separate operator
dispatcher or issue-comment trigger is needed after the exact contract has been
accepted. A missing protocol field is an actionable technical block.

## Accepted contract

Add `producer` to the existing `repository-evaluation/v1` specification. It has
`mode: dispatch_once`, the selected `workflow_criterion_id`, `target` (actual AWS
account, region, `resource_kind: repository_scan`, repository as `resource_id`),
exact non-secret `inputs`, `receipt_artifact`, `receipt_path`, and the complete
`images` map. Each image declares its immutable `sha256:` digest and the SHA-256
of its real provenance file. Do not put credentials in the inputs or evidence.

The inputs must include `expected_account_id` and `region` matching the target.
They must name every business input declared by the workflow; unreviewed defaults
are refused. Correlation/source/definition transport inputs are engine-owned.
There must be exactly one selected dispatch-only workflow, and its accepted
artifact list must include the typed scan receipt. Existing per-finding predicates
remain mandatory: collection and cleanup alone are not a clean security verdict.

Use the existing evaluation preview/accept endpoints with
`authorize_workflow_dispatch: true` in addition to explicit `authorize_evaluate`
when the original policy lacks that action. The ordinary read-only acceptance
cannot dispatch a workflow. Acceptance preserves the original worker plan/version,
base meter, policy limits and expiry. One accepted producer contract can be admitted
only once. Lost POST responses remain unresolved and never trigger another POST.

The engine takes a real claim for the final eval issue and creates its own
`evaluation_pending` execution. It counts against the flow's existing concurrency.
No worker invocation or PR is fabricated. The model-spend meter is not advertised
as a limit on AWS scanning charges; the reviewed workflow must enforce its own
existing scan time/resource bounds.

## Workflow interface

Keep only `workflow_dispatch` in the selected workflow. Do not enable the old
PR/push/scheduled scanner by adding this interface. Declare string transport
inputs `adp_correlation`, `adp_source_revision`, `adp_definition_revision`, plus
the exact accepted business inputs including `expected_account_id` and `region`.
Retain an appropriate shared concurrency lock and bounded jobs/child scans.

Reuse the existing WorkflowProvider identity protocol:

```yaml
run-name: ${{ format('ADP deployment {0}', inputs.adp_correlation) }}
```

This existing transport prefix identifies a provider operation; the resulting
engine receipt still states `live_attestation: false`. The workflow source and
definition are pinned and compared before dispatch. Checkout
`${{ inputs.adp_source_revision }}` for the code under scan. The dispatch revision
is `${{ inputs.adp_definition_revision }}` and must equal `${{ github.sha }}`.
An unrelated main advance does not imply tree equivalence: authenticated context
must prove which immutable source was actually checked out and scanned.

Before starting scan work, configure the existing approved scan role, then run
`python modules/gateway/scripts/repository-scan-receipt.py` with:

| Variable | Actual source |
|---|---|
| `CONTEXT_WORKFLOW` | This repository-relative workflow path |
| `CONTEXT_SOURCE` | `inputs.adp_source_revision` |
| `CONTEXT_REVISION` | `inputs.adp_definition_revision` |
| `CONTEXT_CORRELATION` | `inputs.adp_correlation` |
| `CONTEXT_REPOSITORY_ID` | `github.repository_id` |
| `SCAN_EXPECTED_ACCOUNT` | `inputs.expected_account_id` |
| `AWS_REGION` | `inputs.region` |
| `SCAN_INPUTS_JSON` | JSON object of all actual business inputs as strings |

The helper also reads standard GitHub run/repository/event variables. It compares
`git rev-parse HEAD`, workflow identity and `aws sts get-caller-identity`; a wrong
source/account/event refuses context before scanning. It reads credentials only
through AWS CLI and publishes no credentials. Upload its single
`$RUNNER_TEMP/adp-deployment-context/deployment-context.json` file as
`adp-deployment-context-<workflow-basename>-<github.run_attempt>` immediately. A
rerun must have its own context artifact.

## Observed scan results

After actual scanner and cleanup checks finish, the scanner supplies a JSON file
with exactly `source_revision`, boolean `coverage_complete`, boolean
`cleanup_complete`, and `images`. Each image has its actual immutable `digest`
and a relative `provenance_path` into the evidence directory. The helper computes
the provenance digest from those real bytes; missing files, traversal, unpinned
images, source mismatch and non-boolean status are refused. False status stays
false. Do not construct successful results merely to satisfy this interface.

Run the same helper with `--results <observed-results.json> --provenance-dir
<evidence-directory>`. It writes
`$RUNNER_TEMP/adp-repository-scan/scan-receipt.json` using
`repository-scan-receipt/v1`. Upload this file and the actual source/provenance
evidence in the artifact named by the accepted specification. Upload the full
S21 finding/disposition reconciliation as another accepted JSON file in that run.
Its predicates must cover all required occurrences, old tickets, unrated records,
four Superplane images, AWS Security Agent outcome, and residual rollout owners.

The evaluator authenticates the run/context, required successful jobs, latest
attempt, archive/file digests and exact source/account/region/image/provenance
records. Verified collection/cleanup plus failed acceptance predicates records a
failed evaluation. A failed workflow without cleanup evidence retains its claim
and an explicit blocker; GitHub job termination alone does not prove an async AWS
scan was cleaned up. Uncertain dispatch likewise retains ownership for recovery.

Neither this protocol nor the read-only repository receipt closes pending rollout
or establishes the CLI's full live acceptance. CLI final #5644 and integrated
journey #5630 still require their actual installed-client/target/release criteria.

## Concrete S19/S21 engine handoff

S19 ([#5618](https://github.com/aws-e/adp/issues/5618)) owns the workflow/scanner
integration. Reconcile draft [#5594](https://github.com/aws-e/adp/pull/5594), whose
starting reference at this handoff is `3940a796ee46cd88cb5c42a99103c10e35420548`, rather
than creating another inventory implementation. That reference adds four-image
Superplane coverage and dispatch-only triggers, but does not implement this
producer protocol. In particular, `Security Summary` is still restricted to
`pull_request`, so its manual run can succeed while skipping reconciliation.

Before handing the workflow to S21, S19 must provide:

- Only `workflow_dispatch`, the exact run-name above, explicit accepted business
  inputs (including `scan_scope: all` for this whole-repository evaluation), and a
  shared concurrency group with `cancel-in-progress: false`. A Superplane-only
  run cannot prove the full S19 inventory or the other scanner criteria.
- Checkout of `inputs.adp_source_revision` in **every** job that scans, builds,
  prepares an archive, or assembles evidence. Verify `git rev-parse HEAD` before
  scanning. Preserve `GITHUB_SHA` as the workflow definition/dispatch identity.
  The existing `.github/actions/codebuild-run/action.yml` derives its source-key
  label and `ADP_SOURCE_SHA` from `GITHUB_SHA`; supply a separately verified source
  revision through a narrowly scoped action input or scan wrapper. CodeBuild's
  source metadata/provenance must match the actual archived checkout, even when
  main advances between S21's merge and dispatch. Do not relabel the dispatch SHA
  as scan evidence or overwrite `GITHUB_SHA` to hide the distinction.
- An initial successful context job that runs the helper and uploads exactly
  `adp-deployment-context-security-scan.yml-<run_attempt>` with its single
  `deployment-context.json`. Scan jobs must depend on this identity check.
- A final summary/export job that runs for `workflow_dispatch`, verifies all
  expected tool/image outputs (including all four Superplane targets and pinned
  SkyPilot), and invokes the helper with real observed results/provenance. Upload
  `scan-receipt.json` to `adp-repository-scan-<run_attempt>` and sanitized
  `security-reconciliation.json` to `security-reconciliation-<run_attempt>`. The accepted
  specification must use these exact names, paths and required job names. Private
  S3 artifacts alone are not available to the repository evidence reader.
- Recorded IDs and terminal states for every CodeBuild build and asynchronous
  scan started by this invocation. The cleanup job must query those actual IDs,
  stop and recheck outstanding children when needed, and remove invocation-owned
  temporary resources. Set `cleanup_complete` only after those checks pass.
  A GitHub job timeout/cancellation, an empty child list caused by missing state,
  or successful upload is not proof of cleanup. Preserve evidence on failure.

S19's focused validation must cover distinct source/dispatch SHAs, missing tool or
image outputs, invalid detect-secrets/cfn-nag output, dispatch summary execution,
rerun artifact names, and unproven child cleanup. The shared CodeBuild action
change, if needed, must preserve its existing callers' behavior.

S21 ([#5620](https://github.com/aws-e/adp/issues/5620)) integrates the real immutable
image digests/provenance and prepares the reconciliation inputs in its PR. Keep
the original requirements explicit: all 33 source-rated occurrences, 16 older
primary ticket mappings, 860 S20 dispositions, severity disagreements/suppressions,
the actual AWS Security Agent result for run `35547077368`, and residual rollout
owners. Preserve the AWS run's own source/target identity; an older result is not
evidence that the final merged source was scanned. Pending/failed review remains
unresolved, and unresolved rollout remains owned.

The final E01 evaluation runs the accepted producer **after S21 and every other
direct predecessor merge**. S21 therefore supplies the validated workflow, pins,
provenance and reconciliation inputs before that merge; its PR must not invent a
future scan receipt or declare the dated epic clean. The engine records the actual
post-merge run and evaluates its artifacts against the accepted per-finding
criteria. A pre-merge diagnostic run does not replace that final observation.
No additional workflow or recurring scan is implicitly authorized by this handoff.
