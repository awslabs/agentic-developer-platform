# Unattended execution contract

Initial owner acceptance permits implementation, review, CI and merge using the
registered repository. One release campaign, evidence acceptance, recommendations
and cleanup proceed unattended only once their private launch bindings and
authority are present. Missing runtime access must not prevent independent code
work. No routine per-story human decision or deployment gate is introduced.

This is an **unattended design**, not a claim of a qualified live run. Private
launch bindings remain unresolved. The new connector code and product-specific
runner are implementation deliverables of the flow; the installed engine/actor
capabilities must be verified before admitting their effects. Never dispatch an
unbound operation or replace missing evidence with a human pass. A code-start
approval with an empty connection grant does not authorize cloud effects.

## Code admission and live launch admission

Before code admission, verify tenant/Product/Project and repository identity,
initial approval and code actor authority. S01 consumes the repository design;
it produces the scope, field map, measurement contract and a readiness matrix.
Record missing facts with an owner/action and the precise dependent stage. Do
not require a runtime connection merely to write or review those artifacts.

Resolve the [launch contract](launch-contract.json) before live operations:
tenant/Product/Project, registered target and connection, actual source/provider
consents, scoped identities, accountable owners, delegated independent review,
evaluation rubric, resource retention/removal, budgets and expiry. Credentials
remain private references. Prefer resolving these while code development runs.
The coordinator owns the binding handoff; S07 revalidates it before effects.
Agents generate disposable test data under actual source consents. Unresolved
inputs keep the affected operation blocked; never label the whole campaign
unattended-ready merely because its code phase can start.

Preview must show only the registration-inserted acceptance gate. Set
`ORCHESTRATION_AUTONOMY_GATE_EVERY_WAVE` off; verify all three evaluations and their
policy entries are `machine`. Verify the actual branch/environment protections
admit the configured automated actors. Do not bypass protections or treat the
coordinator's access as a worker grant. Repository rules, provider authority and
runtime capability checks must use the current registered installation/identity.

Both operations nodes now carry the existing generic v2 delivery contract.
Resolve its public `unresolved-*` values before live admission. Its fixed
definition revision is an authoring baseline; the existing
`definition_predecessor` resolver obtains the future reviewed workflow from S07
or the preceding verified release. Future SHAs are not guessed and no routine
human plan amendment is needed when that code merges. Preserve accepted component,
workflow, scope and check identities across those handoffs.

Use [prepare_launch.py](prepare_launch.py) to materialize an inert private draft.
The private JSON must contain exactly `org_id`, `product_id`, `installation_id`,
`connection_id`, `target`, `resource_id`, `launch_manifest` and `expires_at`.
Values come from the verified launch context, not examples in another flow.
This materializer is for a fully bound launch draft. An earlier inert registration
may bind only verified tenant/Product/repository identity and retain unresolved
runtime values; initial acceptance can start code work with no environment grant.
For an already accepted flow, use a native plan amendment against the current
version to bind verified runtime values and authority; never overwrite it with
the materializer's fresh authored draft or infer a grant from earlier approval.
Run it with `--config` pointing to that private file; add `--output` for a new
file outside the repository, written with mode 0600. Without `--output` it only
checks the document. It rejects placeholder bindings and an expiry that cannot
cover the soak/recovery window. It never registers, approves or starts the flow.
The operations nodes' named environment inputs resolve through the registered launch
manifest; server readiness must authenticate those bindings and receipts. A
well-formed local configuration is not proof of permission or installed capability.

## Execution and evidence owners

| Work | Actual mechanism | Automatic completion boundary |
|---|---|---|
| Code, review and merge | Existing developer/reviewer/controller lifecycle; `code_only` on the 13 code nodes. | Native verified merges with the real repository CI checks named in the proposal. |
| Code evaluation | Tick-owned `engine-repository-evidence-v1`, contract-compatible verifier digest. | Reads all 12 predecessor merge/check receipts; no worker dispatch or human evaluation. |
| Release and repair | Existing operations worker, generic v2 workflow dispatch, mediated repair PR/CI/merge and durable continuation. | Operation remains active until the complete pilot suite and independent readback pass. Health alone cannot finish it. |
| Pilot/closure evaluation | Same native reader, bound to the operation's exact Actions run/attempt and workflow artifact provenance. | Mandatory AC predicates and generic delivery checks agree; no arbitrary successful run or self-written success flag. |
| S08 report | Developer plus delegated independent reviewer applies the rubric accepted at launch. | Separate connector-code, normalized-model and full-runtime recommendations with measured costs/gaps; no new business adoption approval. |
| Finalization | Protected scheduled/manual S09/S02 finalizer, default disposition accepted at launch. | Exact owned resources removed or retained under already approved holds; shared resources preserved. |
| Issue closure | S07 idempotent completion consumer, using existing scoped issue/orchestration integration. | After final machine acceptance, close only matching current requirement revisions; replay safely after lost acknowledgement. |

The native repository receipt deliberately keeps `live_attestation=false`: the
engine authenticates merge/runtime/workflow provenance and artifact predicates.
The product workflow supplies the independent live observations. This plan uses
`trusted-evaluation`, not an invented generic live-probe adapter. No custom
DevLake check is added to the engine.

The artifact is `devlake-pilot-{run_id}-{run_attempt}` or
`devlake-closure-{run_id}-{run_attempt}`, containing `summary.json`. Its provenance
fields `source_revision`, `definition_revision`, string `run_id` and string
`run_attempt` match the operation. The native reader also requires its legacy
`gateway_revision` provenance field to equal that operation's source; actual
gateway and other component revisions are recorded separately in runtime readbacks.
In a cleanup-only operation this field does not claim the gateway was redeployed.
Every original criterion has `/criteria/<ID>/outcome: pass`; full independent
readback, zero required gaps and the explicit unattended predicates must also pass.
S07 tests this exact envelope and the generic delivery envelope together.

S09 adds the `DevLake POC / verify` CI check and proves trigger coverage for every
allowed repair file, including workflow-only changes, and S08's decision/completion
documents that require this check. Early S01/S12 use existing
Public Documentation/Gateway/Insights checks, so they do not depend on a CI job
created later by S09. New workflow IDs are not fabricated: the future check uses
the actual GitHub Actions app ID plus its required name; existing workflow IDs
are explicitly bound where available.

S11 derives the actual response-field inventory from reviewed TypeScript/backend
schemas. S07 consumes that pinned artifact, checks real endpoint response keys
and fixture provenance, and updates the issue's documentation automatically.
Editing an issue is not a readiness gate or a source of acceptance authority.

## Parallel work and prerequisite resolution

Use the [parallel execution contract](parallel-execution.md), with at most two POC
actions. Engine `repository:` inputs grant source availability, not issue or PR
completion. Engine `environment_connection:` inputs identify admitted connections,
not arbitrary readiness documents. S09 CI and S07 campaign checks supply the
actual merge/capability/target evidence; their implementation is required before
the respective shared merge or runtime effect.

## Repairs stay with the active operation

The proposed policy is version 3 with explicit
[repair paths and checks](repair-scope.json), rather than merely listing `repair`
as an allowed action. S07 demonstrates the actual actor path: failure -> diagnosis
-> allowed file edit -> repair PR -> required CI -> verified merge -> changed
component release -> full independent verification -> automatic successor.
Repeat observation after losing the agent/worker to prove durable continuation.

The mediated API refuses credential/policy/approval files, unlisted workflows,
authority changes and oversized edits. Such work follows its real reviewed owner;
it cannot be smuggled into operations repair. CI/deployment definitions, registry
permissions, runner availability and the repair surface must qualify automatically
before the campaign mutates cloud resources. An external approval requirement or
scope expansion is a real exception, not a routine human step.

Qualification has two boundaries. Before the first cloud effect, check installed
actor capability, permissions, branch rules, CI, contract schemas, deadlines and
independently reachable finalization. Then, within the already admitted campaign,
use bounded owned fixtures to prove actual repair/redeployment and worker restart
before starting the timed soak. That exercise is part of the required fault
qualification, not a prerequisite requiring its own completed deployment. Keep
its real mutations in the campaign ledger and never claim CI proves this live path.

The operations workflow runs the same required predicates consumed by its later
machine evaluation. It cannot publish a successful generic delivery receipt while
those predicates fail. This keeps ordinary application/fixture/report repairs
with a still-active owner. The read-only evaluator only retries observation; it
does not implement repairs or reopen a passed operation. A later provenance or
authority refusal remains an explicit exception with the original evidence intact.

## Forty-eight-hour soak without an interactive operator

Use one admitted campaign workflow with a sequence of checkpointed jobs on
existing qualified self-hosted runners. Plan twelve four-hour measurement blocks
with each job capped at 300 minutes, followed by the short `qualify` job. All
jobs use the same accepted source/configuration and durable campaign ID; each
gets fresh job-scoped credentials and renews workload/provider credentials through
the existing broker/IAM paths. Do not rely on one job token surviving the soak.
Qualify the workflow/engine authority window for at least 56 hours, runner capacity
and job deadlines before launch. This reserves runner capacity during measurement
blocks; its cost belongs in the accepted budget. It adds no per-story deployment
or new deployment engine.

S07 checkpoints each bounded update/readback to the protected campaign ledger,
keyed by accepted plan, campaign, source/configuration and component digests.
The operations agent may exit/restart; scheduled continuation reattaches to the
existing correlated workflow and reads its journal. Do not start a second release
because a polling call or acknowledgement was lost. If a job/workflow/runner is
lost, an admitted retry uses the same compatible checkpoint ledger and proves the
required elapsed time and all 20 observations; incompatible artifacts restart the
affected measurement window. Never backdate, count unexecuted updates, or combine incompatible observations.
A missed required observation remains failed; restart the affected measurement
window when required instead of synthesizing a catch-up pass.

S07 must qualify worker restart, workflow loss, queued CI, token refresh and
checkpoint recovery before relying on them. A missing qualified job/credential or
continuation path is a launch blocker, not a promise of unattended execution.
No workflow reports pilot acceptance before the soak completes. Verify the final
runtime state and publish fresh evidence after measurements; a seven-day artifact
lookup window does not permit stale runtime authority.

## Recommendations, cleanup and exceptions

Initial acceptance delegates S08's recommendation review against a fixed rubric.
The final report may recommend adopt, defer or reject each reuse option, but does
not authorize production adoption or a full implementation epic. A business owner
can decide on that later without holding the POC open or retaining idle resources.
This changes review timing/ownership, not the need for measured recommendations,
full traceability, explicit gaps or independent review.

Default cleanup follows the preaccepted ownership ledger on success, terminal
failure, exhausted retries, abort or expiry. Pilot-stage cleanup covers test
fixtures and temporary artifacts; the final stage removes remaining pilot
resources or records the preapproved holds. No final "keep or delete?" question
is required. Unknown ownership/authority still blocks that deletion and records
the unresolved resource; it cannot be reported cleaned. The independent finalizer
remains reachable if the success graph, worker or feature flag is stopped.

S07's completion consumer observes the final machine verdict through existing
protected integration, then publishes the sanitized report and closes eligible
issues idempotently. It does not wait inside the cleanup workflow for the verdict
that that workflow must itself produce. A changed requirement revision leaves
that issue open with a recorded exception, rather than silently accepting it.

Unexpected consent loss, revoked authority, a required scope change, an unsupported
repair or exhausted accepted bounds stops the affected work and invokes authorized
finalization. Report the exception through existing flow/issue status. Normal CI
queues, recoverable faults, report generation, recommendations and cleanup are
agent work and must not cause a routine request to the user.
