# Repository evidence evaluations

`repository-evaluation/v1` is an explicitly accepted machine specification. The
engine reads evidence itself; it does not launch a reviewer/coordinator worker,
claim an external dependency issue, or create a fictitious deployment receipt.
The existing tick admits ready stories first, then observes at most one
repository evaluation within a 15-second budget including authentication and
settlement. Oldest-observed evaluations receive the next turn. Its
typed receipt is recorded in the existing orchestration decision ledger under
`system:repository-evaluation`.

The current implementation requires the flow's accepted shared-worker
continuation, `evaluate` authority, machine acceptance for the exact evaluation
address, current principal membership, available budget accounting, unexpired
authority, and the existing attempt/concurrency limits. The observer obtains a
short-lived, repository-scoped read token. It never uses the worker IAM role to
read GitHub and never reserves a model run. Missing evidence remains a visible
blocker; it does not become a human acceptance gate.

The specification is defined in
`modules/gateway/src/orchestration/repository_evaluation_contract.py`. Its runner
is `engine-repository-evidence-v1`, with repository name/numeric identity and the
SHA-256 digest of the shipped verifier. Obtain the actual digest from the exact
gateway source being deployed:

```sh
cd modules/gateway
python -c 'from src.orchestration.repository_evaluation_contract import harness_digest; print(harness_digest())'
```

Do not substitute a placeholder digest. For read-only `repository-evaluation/v1`
specifications without a producer, the versioned evidence schema and runner
adapter define compatibility. A routine gateway update can change the byte
fingerprint without requiring a plan amendment or a new acceptance. This applies
to existing accepted specifications too, preserving their serialized form and
plan hashes. Receipts retain the accepted `harness_sha256` and record the actual
installed verifier in `collector_harness_sha256`.

Set `runner.harness_policy: "exact"` when an evaluation must use that exact
implementation. The default is `"contract"` (omitted in serialized contracts).
Unsupported schemas/adapters and explicit exact-pin mismatches still block;
breaking verification semantics require a new contract version. Producer
dispatch, CLI live qualification and multi-repository contracts retain their
existing implementation checks. Compatibility never waives source, permission,
check, workflow, artifact, freshness or predicate requirements. Proposal
validation, bounded evaluation acceptance and observation use the same policy.

An accepted specification also declares the following evidence:

- Every direct story predecessor's graph address and mandatory check names/App
  IDs. The observer resolves that story's current binding and genuine successful
  engine merge action, retaining its original plan version, execution identity,
  reviewed head, merge SHA, and review artifact reference. A later evaluation-only
  amendment can consume earlier code only while the accepted story scope still
  matches its binding.
- Optional external PRs by criterion ID, issue number, exact PR number, head
  SHA, merge SHA, and mandatory checks. These are evidence references, never
  worker assignments or ownership transfers.
- Optional workflows by path, immutable source and definition revisions (or a
  named direct predecessor's verified merge revision), mandatory jobs, artifact
  names/files, and explicit machine predicates. Every source merge must be
  included in the evaluated workflow revision. Default `dispatch_only: true`
  requires the committed YAML to declare only `workflow_dispatch`; the actual
  run must always have that event. The observer dispatches no workflow.

A required check may include an explicit GitHub Actions workflow selector:

```json
{"name":"verify","app_id":15368,"workflow":{"workflow_id":123,"workflow_path":".github/workflows/tools-ci.yml"}}
```

Use the repository's actual numeric workflow identity and reviewed path. Declare
every intended workflow separately when jobs share a name. The observer resolves
each check suite through GitHub's authenticated workflow-runs API, verifies the
repository and exact source revision, and binds the check to one job in the
current run attempt. It rechecks that run before recording workflow, suite, run,
attempt and job identities in the check receipt. A failed namesake cannot be
replaced by a successful check from another workflow. Legacy name/App selectors
keep their existing serialized form and refuse multiple matches; adding a
workflow selector requires explicit evaluation-contract acceptance. This does
not reinterpret or waive historical failed checks.

For an already-running shared-worker flow, use
`POST /orchestration/flows/{flow_id}/evaluation/preview`, then the matching
`/evaluation/accept`. Both require the existing human session and `PLAN_APPROVE`
permission. The request names `node_id`, `expected_plan_version`,
`expected_plan_hash`, the exact `specification`, and a `reason`. Acceptance also
requires the preview's `snapshot` as `expected_snapshot`. If the original policy
does not permit evaluation, the reviewed request must explicitly set
`authorize_evaluate: true`; the machine-mode map and a deferred-action marker
never imply that permission.

Acceptance uses the flow/plan locks and a non-waiting evaluation-node lock. If
dispatch already holds that node, acceptance rolls back its savepoint and returns
HTTP 409 with `code: evaluation_dispatch_in_progress` and `retryable: true`.
The conflict writes no acceptance and releases the flow/plan locks so dispatch
can continue. Recheck the current node and refresh preview before retrying.

This records one immutable `evaluation_contract_accepted` decision for the exact
current plan and evaluation node. It carries only evaluate authority, with the
same repositories, limits and expiry and the existing budget meter reference.
It changes no worker policy, plan version, continuation marker, run assignment,
claim, or budget counter. The evaluator rechecks the attributed contract and its
limits at settlement. A plan amendment invalidates the attachment. An existing
live deployment contract or explicit human gate cannot be replaced through this
repository-only API.

A predecessor completed by an explicit owner acceptance of an existing merge
can name `accepted_merge_decision_id` in its specification. The observer requires
that exact immutable decision to belong to the same tenant, flow and story, name
the current bound PR/head, and have been recorded by the policy owner with human
platform-admin authority after the current execution started. That execution
must be concluded. The receipt carries the owner decision ID instead of
inventing an engine merge action or review receipt. GitHub still independently
verifies the exact merged commit, approved current-head review and every required
check. A passed node alone, worker report, stale decision or another principal's
acceptance cannot supply this provenance.

Workflow artifacts are selected from the exact latest run/attempt. Artifact
creation must follow that attempt's start. The GitHub archive digest, bounded ZIP
contents, file SHA-256, job identities, workflow blob, source commit, repository
identity, and run attempt are recorded and checked. A later failed or pending run
cannot be skipped in favor of an earlier success. Required checks/jobs must be
successful, not neutral or skipped. A stale run, missing artifact, changed
source, malformed JSON, duplicate key, or mismatched producer cannot pass.

Artifact predicates support type-sensitive JSON equality, exact set equality,
and exact inventory records keyed by accepted identifiers with required field
allowlists. They do not judge free-text claims. For example, inventory acceptance
should name the expected source occurrence IDs and allowed attributed
dispositions, rather than merely trusting a producer's `passed: true` field.
The trusted workflow's reviewed source must actually collect the evidence those
predicates inspect. Security scans remain one-off producer work; this observer
does not enable PR or scheduled scans.

The receipt binds the current accepted plan/hash, specification/hash, verifier
digest, source snapshot, provider evidence, and mandatory verdict. Settlement
rechecks current policy, plan, predecessor states, and bindings before passing
the graph node and releasing successors. Missing or failed evidence keeps the
evaluation ready with its blocker for the next tick.

The flow graph projects a current `repository-evaluation-refusal/v1` as an
`evidence_wait` progress entry while retaining the real node state. Its safe
diagnostic includes the responsible `actor`, `required_input`, `next_action` and
`observed_at`; raw provider prose remains in the audit ledger. The refusal binds
the tenant, flow, node, attempt, current accepted plan ID/version/hash,
specification hash and validated acceptance-overlay ID. A replacement overlay,
changed scope or passed node removes the old diagnostic immediately. Historical
unscoped refusals require a fresh observer check before they can be displayed.
Identical scoped refusals remain deduplicated, so `observed_at` is the first
recorded occurrence of the retained refusal, not a claim about the latest tick.
A newer typed repository receipt for the same scope supersedes an older refusal.
If its mandatory criteria failed, progress names that failure and its evidence
producer action instead of retaining a resolved connection or timeout error.
Malformed, wrong-actor or mismatched receipts cannot clear a current refusal.

Migration `107_repository_eval_display` adds a V2 classification function and
concurrent index, leaving the existing V1 function/index intact for rolling
readers and rollback. Apply the migration before serving the new graph reader.
It depends on `105_flow_cost_covering`; any independently prepared migration
branch must be reconciled at integration. Graph selection adds at most one
repository refusal and one repository result per selected node and never loads
its full audit history.

Repository receipts always state `live_attestation: false`. They do not establish
deployment readiness or the new CLI uplift's complete live acceptance. The older
CLI E01–E17 report and its `full_acceptance` flag cannot prove newly added story
criteria absent from that report. Those require an explicit live contract with
the actual target, deployed revision, installed CLI evidence, complete accepted
case mapping, and cleanup evidence.
