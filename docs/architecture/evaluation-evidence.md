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
