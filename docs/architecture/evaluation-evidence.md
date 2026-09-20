# Trusted evaluation evidence (ENGINE-E1, #5153)

An evaluation node may carry an `evaluation` specification in the accepted plan
document. Its default acceptance mode is `human`. Machine mode requires the
existing execution policy to permit that node, harness repository and registered
environment connection. Establishing, changing or removing a suite requires
human acceptance through the existing compile/amendment path. Proposal and
amendment drafting remain available; a service actor cannot adopt its own suite.
The #4529 authoring base preserves suites. Explicit suites participate in the
plan hash, while absent suites preserve pre-E1 hashes and retry behavior.

The normative models live in `contracts/orchestration-evaluation/v1/models.py`.
The runner and gateway load that same validator. The receipt JSON schema is
shipped alongside it; the gateway build and smoke paths check the staged copy
before publishing. The golden file is synthetic protocol test data, including
its visual placeholder. It is not a live evaluation or approved visual baseline.

## Producer and consumer boundary

The supported adapter is `github-orchestration-harness-v1`, pinned to an accepted
repository ID, harness commit and `.github/workflows/orchestration-live-tests.yml`.
The workflow is dispatch-only. Its optional `evaluation_context` supplies the
requested identity, final D3 operation/revision and accepted specification;
it never supplies outcomes. The runner validates the context against the actual
GitHub environment and qualification target before inventory/provisioning.
Scenario adapters return live observations and relative evidence paths. The
emitter hashes bounded files, refuses symlinks and path escapes, and publishes
`orchestration-evaluation-<run_id>-<run_attempt>` as a separate artifact.

Real scenario adapters remain the responsibility of ENGINE-Q2 (#5157). Empty,
unregistered, non-live or unmatched observations cannot emit a passing report.
No live qualification is launched by E1's tests or pull-request workflow.

`EvaluationProvider.observe(binding, expected, run_id=...)` uses the existing
tenant-scoped GitHub installation service with read-only Actions/contents
permissions. It verifies repository/head repository, workflow, commit, dispatch
event, run attempt and provider artifact digest before interpreting the receipt.
The producer ID is resolved from those authenticated fields. A pending run
returns `None`; unavailable or invalid evidence raises `EvaluationEvidenceError`
with an `EvidenceRefusal` reason. Archive size, expanded size, entry count, paths
and per-file sizes are bounded. There is no receipt-write endpoint.

ENGINE-E2 constructs `EvaluationExpectation` from protected current accepted
plan, policy, claim and execution state, resolving the final D3 receipt from its
successful deployment-verification action. It must not construct this authority
from caller-submitted JSON. The final receipt must have `delivery_complete=true`;
intermediate D3 entry receipts and docs-only deployment cannot qualify this live
AWS EKS adapter. E1 binds the actual containing release, target and registered
connection, not just the source merge SHA.

## Eligibility and acceptance

E1 validates current tenant/flow/evaluation/execution/cycle, accepted plan version,
policy hash and claim generation; normalized specification hash; D3 operation and
actual revision; trusted run/attempt; fixture population and artifact integrity.
Evaluation must start inside D3's validity window. Its duration is bounded by the
accepted specification (at most two hours), and completed evidence remains fresh
for at most one hour (ten minutes by default). ENGINE-E2 must also reverify the
current target/revision immediately before its acceptance transaction; a valid
start observation does not prove the target stayed unchanged throughout a run.

Every required criterion must have a `pass` or `fail` result and evidence of its
accepted kind. Missing/skipped/not-run required criteria are ineligible. An
authenticated required failure is eligible evidence with `mandatory_passed=false`,
so E2 can distinguish observed failure from missing evidence. Optional omitted
or skipped criteria are allowed only when the accepted suite marks them optional.

A visual pass needs its approved baseline hash, visual evidence and separate API
and data invariance proofs. Both proofs must affirm the actual release and the
approved populated multi-org/multi-role fixtures. Screenshot similarity alone
cannot pass. Unsupported visual/scenario capture is a prerequisite, never an
inferred result.

`ValidatedEvaluation` includes the authenticated receipt, artifact reference/hash
and required failures; `evidence_summary` supplies bounded frontend fields.
Eligibility itself creates no decision, graph acceptance, dependency release,
correction dispatch or phase transition. Those are E2/E3 responsibilities, and
parent live acceptance and two-run qualification remain separate.

## E2 acceptance and runner handoff (#5154)

The execution runner admits a separate evaluation execution only after every
story predecessor has a final D3 receipt. It retains the source delivery
continuation while the evaluation or its human approval is outstanding. The
accepted suite and current predecessor receipts determine the request; a worker
transcript cannot supply it or conclude the evaluation.

The existing qualification workflow remains a reviewed manual dispatch. An
operator with access to the gateway's protected execution environment can export
the current request, using the evaluation execution ID shown by K4:

```sh
umask 077
python -m src.orchestration.evaluation_request \
  --org-id "$ADP_EVALUATION_ORG" --execution-id "$ADP_EVALUATION_EXECUTION" \
  > /tmp/adp-evaluation-context.json
```

This read-only command rechecks current claim, plan, protected grant, machine
mode and verified deployment window. The output contains claim identifiers;
keep it in the operator environment, not in public UI, issue comments or logs.
Pass the file content as the workflow's existing `evaluation_context` input,
with an explicitly reviewed `config_path`, environment and `mode=run`. Dispatch
at a ref resolving to the accepted harness SHA. No config path or paid dispatch
is inferred by the acceptance controller. The pinned workflow must contain the
E2 run-name correlation (`ADP evaluation <execution_id>`). An older pin requires
the existing accepted amendment process before use; old specification hashes
are unchanged. E3 corrections and Q2 scenario adapters retain their named scope.

The provider examines at most 20 recent workflow runs and four correlated,
completed runs at the accepted harness SHA. It authenticates artifacts through
E1 before any decision. Missing evidence waits within the original K2 deadline;
invalid evidence produces a typed block. Export and dispatch must occur within
the D3 start window; expiration requires an authorized deployment/evaluation
recovery, not a timestamp refresh.

Acceptance rechecks current version, cycle, held claim, policy and protected
authority, then rereads authenticated deployment artifacts and actual runtime.
It refreshes authority again after that bounded read. Evidence, the attributed
decision and satisfied dependency releases commit together under the K1 flow
lock. Concurrent observers cannot commit a second decision. A failed required
criterion records its evidence once and remains blocked for E3; runtime or
authority refusal records a block without an acceptance decision. Human suites
use the existing approval controls. Rejected, halted and failed nodes do not
restart automatically, and passed predecessors never reopen.

K4 returns only bounded criterion outcomes, release/harness revisions and evidence
references. Its summary requires an actual recorded decision and complete required
criterion metadata. The existing execution panel displays those fields without
clearing outstanding human gates. Existing flow rollup remains incomplete while
any mandatory node is queued, running, stalled or awaiting a human.
