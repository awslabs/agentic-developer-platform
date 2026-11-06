# Complete an interrupted saved proposal

This path finishes an initial dedicated managed workspace proposal that saved its
Terraform plan and three offline reports but stopped before final provider
preflights and publication. It is separate from unstarted planning recovery.
It cannot plan again, apply infrastructure, extend the attempt ceiling, or recover
an arbitrary failed Terraform command.

Pause dispatch and worker launch under the maintained installation lock. The
installation owner calls `planning_probe.probe_job(..., completion=True)` with
the original qualified image, then
`planning_recovery.record(..., completion=True, check_only=True)` using the
original source checkout. The inert probe has no execution credentials, network
authority or writable state mount. It verifies the exact historical recipe,
five retained files, create-only plan and absence of prior managed state; native
Terraform show and the maintained guard must reproduce all offline reports.
The owner additionally verifies protected original pod provenance, original
volume identity, installed policy, and the empty versioned backend. Credential
cache files are inspected only through filesystem metadata.

Check-only results authorize nothing. After source grants and every possible
session have expired, collect fresh evidence and publish through the same
maintained owner with `check_only=False`. Gateway independently rechecks protected
execution mappings, issuance history, original scope and evidence freshness.
The source maximum deadline plus 900 seconds remains the earliest owner-proof
time; elapsed worker failure time is not an authority expiry.

Using the original requester and selected approver, call:

1. `POST /workspaces/{workspace_id}/saved-plan-completion/review` with the original
   operation's observed `expected_version` and a fresh durable `idempotency_key`.
2. Inspect the unchanged original request, explicit completion scope and any
   unchanged-budget confirmation. Submit `/decision` with `reauthorization_id`
   and `result: allowed-once` as the originally selected human approver.
3. Submit `/resume` as the original requester. A fresh scope-bound decision and
   sufficient remaining session lifetime are required; expired rows stay immutable.

The shared owner retains and reconciles the incomplete original call, appends
one receipt per approved completion and advances the fence without resetting attempts. The
new execution archives the complete original directory, copies only verified
module/input/proposal files, and performs the outstanding current provider checks.
It publishes through the normal lifecycle artifact writer under its own actual
lease. Metadata retains the original saved-file hashes and source fence. No
operator inserts a result row or applies an unpublished artifact.

If that completion is itself interrupted before publication, the same procedure
may authorize another completion within the original attempt limit. It requires
new owner evidence for the latest source call after all its authority expires,
and a fresh authenticated decision. Every prior completion receipt must bind the
identical five proposal-file hashes, target and Terraform version. A different
plan, reused inspection, live source grant, published artifact, or exhausted
attempt limit is refused. Earlier directories, calls and approvals remain intact;
no init, plan or apply command is introduced by this recovery.

Authority and provider-preflight reads may retry one transport failure, refreshing
the workload credentials before the retry. Each read has a 25-second total bound,
and lifecycle authority plus preflight has a 30-second bound within its existing
45-second renewable lease. Credential issuance, lease mutations and provider
effects are never retried by this transport. Diagnostics report the fixed endpoint
path and exception type without tokens, request payloads or response bodies.

Deploy compatible Harness schema 14, domain migration `051_saved_plan_completion`,
Gateway/tick, API and paid-worker images before activating this scope. Maintain
the producer role's single added protected POST route through app-owned Terraform.
The API exposes the resulting proposal through the ordinary lifecycle flow;
infrastructure apply still needs its own exact-plan approval. A completed proposal
does not establish a running cluster or workspace readiness.


## Historical Oregon pricing refusal

A bounded variant covers the pinned historical Oregon guard's exact refusal
that pricing was available only for `us-east-1`. The original directory must
contain exactly the binary plan, its JSON rendering and inventory, plus the
original refusal diagnostics. Estimate and authorization reports are recorded
as derived outputs in `pricing_revalidation`; they are never described as
retained files.

The inert owner probe reproduces the historical refusal, then reruns the guard
with only `scripts/check_workspace_plan.py` and `regional-pricing.json` replaced.
Terraform sources, provider locks, ownership checks and all other recipe files
must be unchanged. The corrected guard validates the binary plan and must
reproduce the original inventory exactly. The observation binds both module
hashes, replacement hashes, original artifacts and derived report hashes.

Completion verifies this transition against its packaged maintained source and
reproduces the entire observation before archiving anything. It preserves the
original directory, copies the original plan and inventory, overlays only the
two approved pricing files, and writes only the missing reports. Normal final
provider checks and ordinary apply approval remain required. This variant is
limited to the recorded Oregon refusal, not arbitrary failures or regions.

For a workspace outside the management region, the owner uses enrollment's
`management_region` for EKS audit and the protected authority table, and the
provider region for workspace observations. Pass the completed regional runtime
maintenance receipt as `maintenance_receipt` to bind the installed immutable
policy, original worker image, installation and release. The policy must predate
and match the original pod's exact mount. All original provenance, backend
absence, expiry and fresh approval requirements still apply.
