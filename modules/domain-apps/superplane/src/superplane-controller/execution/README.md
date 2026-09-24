# Governed execution transport

This package is the Go consumer of the existing
`harness_jobs.execution_rpc.ExecutionRPCServer` Unix-socket protocol. It sends
only `renew`, `status`, `cancel`, and `execute_step` requests. Execution arguments
contain a step ID, never a provider, target, credentials or worker success report.
The server authenticates each request, reads the immutable admitted plan, commits
provider intent before I/O, and owns duplicate suppression and recovery.

`New(socketPath, tokenFile, binding)` requires explicit absolute paths and the
trusted assignment's operation, organization, workspace, holder, attempt and fence.
It reads the token file again on every request and checks the entire binding against
the authenticated lease before execution/cancellation. `ExecuteStep` requests a
finite 900-second lease and uses the earlier of lease expiry and the persisted
runtime deadline. It never retries an uncertain request or obtains another attempt.

Use the returned provider handle and exact budget disposition. A nil error does
not mean provisioning succeeded: an uncertain provider call returns an `intended`
record, no outcome, and `retain`. `ErrCancelled` can also return an observed call
and disposition when cancellation raced provider I/O. Cleanup, budget settlement,
allocation retirement and recovery remain trusted-service responsibilities.

## Integration status for #5536

This is a transport checkpoint, **not a completed controller implementation**.
`main.go` does not instantiate this client. Management startup, preflight and runtime
continue to report governed provisioning unavailable; SkyPilot authentication alone
does not change that. No shared contracts, API routes or deployment manifests are
changed by this checkpoint.

The following code work remains before #5536 can close:

1. #5535 must compose the real ADP run-credential verifier, admission/attempt
   authority, immutable SkyPilot plan and provider hook. The current registry
   returns `governed_provisioning: false` and has no run-assignment delivery contract.
2. Wire the Go client into per-workspace reconcilers only after durable registration
   and verification of that workspace's credentials, namespace and CRDs. Bind
   assignment revocation and controller ownership to the registry lease. Do not
   replace these missing inputs with local manifests or invented attempt IDs.
3. Exercise real SkyPilot provisioning, workspace-EKS join, scheduling, signed
   observations, and distinct batch/serving cleanup through admitted steps. Apply
   returned dispositions through the domain's ledger integration. The tests here
   use an explicit provider double and do not establish this behavior.
4. Align full-installation capability/readiness, obtain review, pass the final
   required CI checks and merge normally. Live acceptance remains a separate gate.

| Story criterion | Checkpoint status |
| --- | --- |
| AC-01 full governed controller integration | Blocked; transport coverage is a subset |
| AC-02 preflight/runtime agree for executable controller | Blocked; capability remains unavailable |
| AC-03 final-head affected CI | Run through the PR workflows; see their results |
| AC-04 reviewed complete implementation and normal merge | Blocked; keep PR draft and issue open |

## Remote verification

`Superplane Domain CI` runs ordinary Go tests. A parallel execution-contract job
builds the Go test-worker binary and invokes `execution/integration/test_shared_transport.py`
in a separate Python environment with `harness-jobs[dev]`. That suite uses the real
shared service and disposable PostgreSQL. It covers duplicate requests, step order,
worker restart, stale fences, expired runtime, revoked/missing tokens, cross-workspace
and cross-attempt binding, cancellation and uncertain provider outcomes. The job
fails on skipped database coverage. The race detector is not part of this lane:
ARC has CGO disabled, so the initial attempt refused before executing it.

Dispatch the existing offline workflows against the exact PR branch:

```sh
gh workflow run superplane-domain-ci.yml --repo aws-e/adp --ref <branch>
gh workflow run harness-jobs-ci.yml --repo aws-e/adp --ref <branch>
```

The domain lane includes the affected Gateway registration and authorization
checks. No Gateway source is changed here; Gateway CI itself has no manual
dispatch trigger.

These checks install no product CLI, run no live workloads and deploy nothing. Keep
product CLI verification on the existing disposable EC2 regression harness. There
are no deployment/rollback inputs for this checkpoint because it activates no runtime
path; the eventual executable integration requires coordinated #5535/#5538 packaging.
