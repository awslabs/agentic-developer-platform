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

Do not substitute a placeholder digest. Proposal validation and observation
both compare it against the installed verifier. An accepted specification also
declares the following evidence:

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

Repository receipts always state `live_attestation: false`. They do not establish
deployment readiness or the new CLI uplift's complete live acceptance. The older
CLI E01–E17 report and its `full_acceptance` flag cannot prove newly added story
criteria absent from that report. Those require an explicit live contract with
the actual target, deployed revision, installed CLI evidence, complete accepted
case mapping, and cleanup evidence.
