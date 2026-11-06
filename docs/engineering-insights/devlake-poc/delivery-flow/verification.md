# Verification and operations contract

This is a plan for future checks. None of the feature ACs has been executed by
preparing this flow. [Acceptance mapping](acceptance-mapping.json) preserves all
44 AC/SCN pairs and ten ECs from the reviewed designs. No original criterion is
waived by batching deployment.

## Shared implementation ownership

Each feature story creates its named `test_sNN.py` scenarios and independently
defined expected results from the canonical design. S01's scope/owner
decisions are attributed launch records accepted before execution, not approvals
manufactured by a test runner.
S09 owns source extraction, independent packaging and bootstrap artifact cleanup.
S02 owns runtime/IAM/migration/finalizer logic. S11 owns the live Profile API field
inventory and UI/CLI parity tests. S07 assembles one shared campaign, fixture ledger,
bounded waits, reports and independent evidence reader. S08 owns recommendation review against the initially accepted rubric; S07 owns
the idempotent post-verdict completion consumer. Evaluation issues only run the delivered checks; they
never implement missing harnesses or patch application behavior.

The proposed runner is `modules/insights/poc/devlake/campaign.py`; its independent
reader is `modules/insights/poc/devlake/verify_campaign.py`. S07 supplies both,
their schema, negative tests and `devlake-poc-qualify.yml`. These are **planned
files**, not currently runnable commands. The finalizer workflow is created by
S09 and extended by S02, separately reachable on failure/abort/expiry.

## Evidence and evaluation

The campaign manifest contains the exact source and workflow revisions, all
component image digests, schema/mapper/field-map versions, effective flag state,
source fixture and target references, attributed scope/owner decisions, CI and
verified merge receipts, timing ledger, `stage_reports` keyed by S01–S13 and
cleanup receipts. Private identities, credentials and raw evidence remain in the
protected evidence store; public reports contain only sanitized conclusions.

The independent reader resolves provider, deployed resource, database/evidence
and CI receipts instead of trusting a worker's success boolean. It rejects missing
or stale receipts, mixed incompatible releases, mutated sealed B1 bytes, missing
AC/SCN IDs, self-authored acceptance, unauthorized reads, and skipped/not-run
required checks. S07 tests those negative paths before release readiness passes.

| Evaluation | Required input | Mechanical result |
|---|---|---|
| Wave 1 code | All 12 exact verified merges and the per-story required CI/review checks declared in the proposal. | The native repository reader accepts every predecessor automatically. S07 runs harness/preflight self-checks in CI; the code evaluator dispatches no custom workflow and claims no live AC. |
| Wave 2 pilot | One coherent release/campaign, 41 non-S08 AC results, original SCNs, preaccepted S01 launch decisions and independent runtime readbacks. | `--phase pilot` exits 0 with `PASS pilot`; all 41 required ACs passed, zero skipped/not-run, ≥20 controlled updates over ≥48 hours and measured p95 ≤15 minutes. |
| Wave 3 closure | Wave 2 receipt, delegated review of three rubric-based recommendations, gap follow-ups and independent absence/hold readback. | `--phase closure` exits 0 with `PASS closure`; all 44 ACs and ten ECs accounted for, all S08 ACs passed and no unowned cleanup gap. |

Each command also takes `--manifest "$DEVLAKE_POC_MANIFEST" --plan
docs/engineering-insights/devlake-poc/delivery-flow/acceptance-mapping.json`. The manifest
variable is a private operator-provided path, not a checked-in target or token.
Exit 0 requires the specified counters; a printed `PASS` alone is insufficient.
The operations workflow executes these checks and independent readback before
its generic delivery receipt can pass. The existing machine repository reader
authenticates the exact operation run/attempt, artifact provenance and every AC
predicate. Its `live_attestation=false` describes the engine boundary: product
workflows provide live observations; the engine verifies their bound evidence.
No custom live adapter or human evaluation is assumed.

For live API/UI contracts S11 records every required field from the implemented
TypeScript interfaces in `profile-contract-fields.json`, with backend schema and
fixture provenance. S11/S07 automatically publish and consume that pinned inventory; the accepted
check is schema/interface equality, so documenting the resulting fields needs no
human gate or accepted-suite change. The pilot checker calls the real authorized
gateway endpoint using established `adp-cred` credentials, compares response keys
with that list and checks fixture keys are a subset. Missing fields fail. Planned
fields or mocks cannot establish a live contract.

The ≥48-hour soak runs in the admitted workflow on an existing qualified
self-hosted runner pool as twelve checkpointed four-hour jobs (300-minute job
limits) and a short final qualification job. Fresh job credentials and a >=56-hour
workflow/engine window are required. This reserves runner capacity/cost; it avoids depending on an interactive agent or inventing an
unimplemented scheduler adapter. S07 writes durable checkpoints, supports
reattachment/retry and qualifies worker/workflow loss. Final readback requires
the original private batch files: resume rechecks their digests and the
checkpoint's aggregated evidence before continuing or qualifying. If a worker
stops after saving a checkpoint but before saving its report, an exact replay
restores the report without replacing the sample. A new batch presented after
the approved 20-minute grace cannot be backfilled as an on-time observation.
Final readback also requires
fresh authority and coherent artifacts. The [unattended contract](unattended-execution.md)
specifies the actual launch limits; incompatible releases cannot manufacture a pass.

## Release and repair handoff

The operation resolves its final source from all 12 verified merge predecessors;
S07 is the workflow definition predecessor. Do not deploy an arbitrary main HEAD.
Use existing gateway/frontend/broker/Insights release procedures where changed
components require them; the campaign wrapper owns product checks and manifests,
not a second deployment engine. Source CI may compile/test on PRs; full release
packaging/publication is coalesced at the final candidate. Observe matching
automatic releases before dispatching an extra one.

S09/S02 qualify manual and scheduled finalization before artifacts/resources are
created. Record resource intents in a protected ledger outside removable state.
The finalizer executes while flags are off, preserves shared PostgreSQL instance,
ADP data, shared connections, holds and other tenants, and independently verifies
absence or authorized retention with expiry/cost. Missing authority or incomplete
cleanup remains an explicit operational block; it is never a passing result.

On a product check failure, keep the operations owner active through bounded
mediated repair and verification. The run-only evaluation itself cannot fix code.
The repository defect protocol records the failure:
new defect with exact command, redacted actual output and owning story; native
child of #8887; developer dispatch only under accepted execution authority; rerun
after verified repair. Deploy changed artifacts only, and rerun affected tests
plus the complete independent acceptance readback on a coherent candidate.
Preserve all prior failed/not-run results. No evaluation issue or merged feature
story becomes an implementation repair assignment.

Normal S08 disable/removal uses its operations node after the reviewed decision
report. For terminal failure, exhausted retries, operator abort, expiry or reuse
rejection, call the independent finalizer immediately without waiting for that
success node. Retention/removal choices and delegated recommendation review are accepted
at launch. Completion needs no new keep/delete or business-adoption decision;
code merges alone never grant deletion authority. Cleanup verification
and reviewed decisions are evidence, not additional application deployments.

## Executable pilot handoff (code contract, not live acceptance)

The pilot command now rejects missing/malformed input and derives its 41 required
non-S08 AC/SCN rows from `acceptance-mapping.json`. A protected dispatch accepts
`phase=pilot` and `launch_manifest` (an absolute 0600 file under the approved
campaign root, named `<campaign-id>/manifest.json`). The file must identify the
admitted campaign, selected source/workflow revision, exact Actions run/attempt,
component digests, and a `criteria` map keyed by the 41 original AC IDs. Each
criterion points to an independently produced GitHub Actions observation artifact
ID (`ref`) and the SHA-256 of its `observation.json` bytes. A separate
`native_receipt` has the same `ref`/`sha256` shape and supplies the independent
scope, authority, unattended recovery and live Profile readback results.

The protected `adp-devlake-campaign` environment must provide the existing
private campaign root and a separately approved
`ADP_DEVLAKE_SOURCE_BINDINGS` JSON secret. This is a map for every required AC
plus `native`; each entry names a reviewed, distinct live-probe workflow
(`workflow`), its successful job (`job`) and an artifact-name template ending
in `-{run_id}-{run_attempt}` (`artifact`). The qualifier uses its job-scoped
GitHub token with read-only Actions permission to retrieve each source run,
job, archive and observation, check the selected source revision and immutable
component/run pins, and reject missing or mismatched material. The source
workflows must actually check provider, IAM, DB, Profile API, fault and cleanup
behavior; an offline fixture workflow or a hand-written pass flag is **not**
a valid binding. The required producer workflows and their private approvals
have not been verified or supplied here. Without the complete approved binding,
the qualification job fails before uploading `summary.json`.

The separate native readback job measures the selected Actions run/attempt and
its twelve successful window jobs, an accepted gateway plan and decision, a
bounded execution page, a node recovery snapshot and the private campaign
checkpoint. Its protected 0600 JSON config must supply the selected source and
definition revisions and a `gateway` object with an HTTPS origin, bearer
authorization, `allowed_hosts`, `flow_id`, `node_id`, `node_cycle`,
`node_attempts`, `plan_version`, `plan_hash` and `decision_id`. The gateway plan
read requires its existing plan-approval permission; private access and consent
must be arranged by the operator, not inferred here. This job publishes only
digests and counts. The execution read intentionally withholds the full
authority binding and the checkpoint is only an offline consistency check, so
this artifact does **not** attest `authority_snapshot_valid` or unattended
recovery. It cannot satisfy the native pilot receipt until separately approved,
independent authority and recovery attestation is provided; do not fill those
predicates from the measurement or label an offline fixture live.

On a complete source readback, the `qualify` job uploads only a sanitized
`summary.json` under `devlake-pilot-{run_id}-{run_attempt}`. Its payload contains
outcomes and exact workflow/component pins, not private observation references;
the native evaluator separately downloads the artifact and checks the bound
job, source/definition revisions, run/attempt and accepted predicates. The
module's direct fixture evaluator is explicitly `offline-only` and cannot
write an accepted pilot artifact without the protected GitHub readback. Code
merge and fixture tests do not establish live consent, source availability,
protected runner access, native receipt acceptance or pilot success.

To reproduce the fail-closed input behavior without live access, run the pilot
command from the Wave 2 evaluation issue with a missing manifest path: it exits
nonzero with a missing-manifest diagnostic and emits no passing summary. The
protected operator must provide private launch and source bindings before any
live dispatch; do not provision test infrastructure merely to make this pass.
