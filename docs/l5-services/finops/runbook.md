# FinOps compatible release and rollback

This procedure prepares the FO07 release; it is not a deployment receipt or native
acceptance evidence. Use only a selected private target, the existing protected
release authority and the current request, time and spend limits. Keep target
identities, provider answers, billing pages, credentials and costs in the private
evidence store, not in this repository or a pull request.

## Before enabling

1. Confirm the selected target account **and exact assumed role** by the
   [deployment guide](../../adp-platform-deployment/deploy-with-agent.md) and
   credential broker. Obtain the authorization required by that guide before a
   deployment. Confirm the FO01 native/provider qualification, the FO06 consumer
   route, the two controlled non-ADP fixtures and the existing budget. If any is
   unavailable, leave admissions off and record the gap rather than substituting
   a target or renewing historical POC limits.
2. Pin the reviewed source revision, gateway schema revision, package wheel
   digest, worker archive digest/version and protected workflow attempt in one
   private release manifest. The `finops` choice on the shared L5 artifact
   workflow publishes the **same** wheel/archive as other L5 services; its
   readback checks FinOps modules in both artifacts. Publication does not install
   the workers or qualify the provider. Keep the existing immutable artifact
   prefix so compatible rollback can find earlier artifacts.
3. Apply additive gateway and L5 SQL migrations before admitting new payloads.
   Check the actual migration heads and the installed gateway wheel; verify the
   new readers and consumers before the protected worker lifecycle rollout.
   The worker operation pauses both consumers, updates from the pinned archive,
   and requires explicit resume and readback. Keep configuration and schedules
   disabled until the worker, repair, gateway and installed clients agree on the
   manifest and queue mapping. Gateway auto-deploy is not a worker release.
4. The gateway's private `l5-finops-admission-enabled` setting defaults to
   `false`. The existing L5 worker Terraform `worker_environment` must carry
   `L5_FINOPS_ADMISSION_ENABLED` for **both** worker and repair Lambda functions.
   A missing value, `false`, or anything other than exact `true` disallows new
   FinOps admissions. Worker resume refuses mismatched worker/repair values.
   Once compatible code and scope are verified, enable the two workers first,
   then render and roll out the gateway with the private setting `true`, and
   enable only the authorized fixture configurations. Do not publish target
   values or add IAM authority to accomplish this.

## Rollback and recovery

Disable the gateway's private admission setting and verify its deployed value
before disabling the worker/repair settings. Disable fixture triggers and new
inquiries. Leave the shared queue, repair scheduler, result readers and retained
storage in place. A new scheduled tick is rejected explicitly while disabled;
a previously pending receipt remains pending without consuming retry attempts,
and a committed run receipt may still be replayed. Observe running/unknown native
turns and billing and publication work; do not submit another native turn after
an ambiguous provider acknowledgment. Read back both worker gate values before
resuming consumers on a previously pinned **compatible** archive. Do not roll
back SQL evidence, receipts or an in-use worker to a version unable to read
the installed payloads. Use documented migration down steps only after work
drains; never drop observations as a shortcut.

Retire only operation-owned agents, contexts, roles and views after reference
readback. Adopted resources and results remain under their existing retention
policy. Record cleanup failures as repair intents; revoked authority is an
explicit blocker, not permission to select another credential.

## Release acceptance selector and evidence record

For SCN-01 pre-review, run
`python -m pytest -q -s modules/L5-services/tests/finops/test_fo07.py::test_release`
from the repository root with the L5 and gateway development dependencies.
The selector disables AWS credential lookup for its subprocess and runs local
shared-artifact, gateway-wiring, worker-lifecycle, repair-schedule, migration,
pending-receipt and installed-wheel fixtures; it does not deploy or contact AWS.
Record its tested source revision, command, timestamp, reported pass/fail/skip
counts and a separately retained local build's wheel digest as **offline**
evidence. An optional local
PostgreSQL skip is not a passed migration on a live schema. The wheel fixture
builds a package and verifies its module origins; it is not the worker archive
or gateway installed in a selected target.

For live SCN-01, first verify the selected account **and exact role**, FO01/FO06
readiness and authorized fixtures as in “Before enabling.” Save a private
record with these fields, per target and attempted rollout:

| Field | Independent readback required |
| --- | --- |
| Release identity | Reviewed source, workflow run/attempt, installed gateway and client digests, gateway/L5 schema heads, selected wheel and worker archive digests and immutable storage versions; reject a mixed set. |
| Fresh/existing/pending | Before/after migration heads and fixture state; pending run/work and publication receipt ids, generations, native turn and original answer references. No duplicate turn, lost result or forced evidence downgrade. |
| Installed ingress | Gateway and both worker/repair admission values, queue bindings, repair schedule and scoped manual/event/schedule responses; check explicit denial when disabled and authorized work only when enabled. |
| Rollback | Disable gateway admissions then fixture triggers before worker gates; read back blocked new work, retained pending and committed receipts, provider observations, results, repair and cleanup work on a compatible pinned version. No schema/evidence deletion. |
| Disposition | Per-step UTC time, attempt count, expected/observed state, immutable private evidence digest, measured/estimated/unknown cost and failure owner/action. |

Mark each phase `passed`, `failed`, `blocked` or `not_run` from those readbacks;
map EC-06 to installed client/ingress and EC-08 to unchanged release identity,
portable scope and rollback disposition. Never populate this public table with
real target values or report an offline fixture as an installed live receipt.

## Two-project native comparison

Before submitting, verify that FO01 has qualified a supported native submission
and read-only observation interface, the delegated role and exact billing-view
scope, and the selected account's eligibility and limits. FO07 records one
installed release identity and two **non-ADP** fixture projects in the same
Product, with distinct effective billing scopes. Isolate two tenants as well
for the foreign-tenant check. Store fixture IDs, permitted role, budget,
configured view, scope digest, binding generation and expected billing groups
privately. Stop if any prerequisite cannot be verified; a simulated provider
response is not a native answer.

Use a provider-supported closed interval and the **same fixed question**, query,
currency, granularity and `Estimated` expectation in each project. The offline
regression fixes UTC at `2026-10-27T00:00:00Z` and queries September 2026,
inclusive start and exclusive end; a live run must explicitly record its own
supported closed interval instead of relying on a moving relative date.

1. Through the installed project-scoped run route, submit the first inquiry with
   its own idempotency key, then observe its native agent/conversation/turn
   identity and original answer via the qualified read-only interface. Record
   the request, attempt and provider evidence references privately. Never
   replace an unknown native outcome with another paid submission.
2. Collect **all** independent Cost Explorer pages under that project's
   effective delegated billing scope. Read back each raw page, cursor,
   artifact version/digest, group dimensions, interval, metric, currency and
   `Estimated`. Match the pinned scope digest and query hash; use exact signed
   decimal groups, including credits and tiny nonzero values. An empty or
   incomplete series is unknown/partial, not zero. Compare the independently
   recorded expected groups with both native and CE groups; do not derive the
   expected numbers from either answer.
3. Repeat for the second project **without** changing platform code, releasing
   another wheel, redeploying the gateway or widening the role/view. Verify the
   same release source, schema, wheel, worker and client digests and two
   different billing-scope digests. An answer matched only on account totals
   does not prove project-level attribution.
4. Using a controlled unauthorized fixture, attempt the first project's view,
   the sibling project's result in the same Product, and the other tenant's
   result through the real path. Independently read back denial, unchanged
   effective policy and absence of private credentials or data in responses.
   Keep each attempt and expected access decision in the private evidence
   ledger; do not try a different role to make a denial pass.

Record native and CE answer states separately, immutable evidence hashes,
reconciliation state and delta, every request/provider/billing attempt, actual
charges (measured/estimated/unknown), and resource ownership/disposition. Mark
the scenario blocked if the provider, billing pages, or scoped denial cannot be
observed. A passing offline two-project fixture establishes only the
comparison and route checks exercised locally, not live portability.
For pre-review regression, run
`python -m pytest modules/L5-services/tests/finops/test_fo07_portability.py`
with the L5 dev dependencies and `uv` installed. It builds one local wheel and
exercises both project routes from that wheel without calling a provider. Its
wheel digest is not a substitute for private installed gateway, worker and
client readbacks.

### SCN-02 private execution card

In the access-controlled evidence store create a card for **each** of two
selected non-ADP projects before making a native request. Record the selected
account **and exact assumed role**, tenant/Product/project, allowed actor and
controlled foreign actor, billing connection, selected view and effective
policy/scope digest, binding generation, query/question and idempotency key,
original budget and expiration, expected signed groups from an independent
fixture, and a private reference for the installed source/schema/wheel/worker/
client manifest. Do not put the card, provider responses, account identifiers,
view names or permission tokens in this repository.

Check that the projects are different, the effective billing scopes really
differ, the same frozen UTC interval/currency/metric/grouping and question are
supported for both, and the selected role is authorized. Record FO01's provider
qualification and supported **read-only** turn observation and FO06's consumer
qualification. If these cannot be verified, mark the card `blocked` and stop
native submission, rather than borrowing an account or renewing the budget.

For each card, use the installed scoped run route once, persist its request/
attempt/run/turn reference and original answer hash, then independently fetch
every CE page under **that card's** view. Retain private page digests, ordered
cursors, grouping, `Estimated`, coverage and query/scope hash; reconcile the
native and CE groups against the **pre-recorded** signed expectations using
exact decimal strings. A missing page, missing `Estimated` or a changed scope
is `unknown`/`partial`, not zero. Repeat the second card with a separate
idempotency key and without a source change, redeploy, view enrollment or role
widening; compare both manifest readbacks to the original digests.

Run three independent denials through installed paths after the successful
reads: the controlled actor requests the first card's foreign billing view,
the entitled actor requests the sibling project's result, and an actor in the
other tenant requests the first card's result. Capture each response status,
authorized-scope readback and redacted response digest; require no credential
or target-data disclosure. A route mock is not evidence of the foreign view
denial. Record separate provider, CE and denial outcomes with attempts, costs,
cleanup disposition, tested revision and private proof references. Only mark
SCN-02 passed when **all** independent live checks pass on one installed
release; otherwise record failed/blocked/not-run with the missing owner/action.

## Interrupted inquiry and exact-result acceptance

Use the **same selected release and authorized fixture** as the two-project
comparison. Record the UTC clock, original question/query hash, run and attempt
identity, native agent/conversation/turn reference (if known), binding generation,
scope digest, billing collection/page references, original answer artifact and
the pre-existing admitted time, request and spend limits **before** injecting a
fault. Record actual provider/SQL/queue readback and attempt count after each
step; a successful HTTP response alone does not prove that a native turn was
not duplicated. Keep raw sessions, provider answers and billing pages private.

| Interruption boundary | Recovery action and independent acceptance check |
| --- | --- |
| After inquiry receipt commits, before queue-send acknowledgment | Replay the same scoped idempotency key and payload, then run bounded repair. Compare the persisted receipt, work generation, queue send and run identity; a changed payload must conflict. No new run or paid turn may result. |
| After native submission is accepted, before its acknowledgment (including worker restart) | Inspect the original attempt before effects. Reconcile through FO01's qualified **read-only** provider observation with the same attempt and question hash. Compare provider turn counts and immutable answer references; if absence of effect cannot be proven, leave `outcome_unknown` and do not resubmit. |
| Session expiry or revoked delegation during observation | Refresh only the originally selected authorization, reauthorize at the current generation and resume observation within the admitted deadline. Revocation blocks effects; expired or unsupported readback is explicitly unknown, never a new native inquiry. |
| After a CE page saves, before its checkpoint; cursor expiry or lost billing acknowledgment | Repair only the existing collection. Compare persisted page digests, page order/cursors and request counts with independent CE readback. Bound attempts by the original `max_ce_requests`; a partial, stale or missing series is not complete or zero and pages from different collections must not mix. |
| After Context/Insights consumer acceptance, before publication acknowledgment | Reconcile the existing publication id and consumer receipt; compare the stored result revision and later entitled retrieval. Do not create a second contribution or treat an unobserved consumer receipt as success. |

For each fault, advance a controlled UTC clock across the relevant due time,
expiry and deadline without sleeps, then record the resulting queued, pending,
unknown, blocked or completed state. Inspect SQL receipts/attempts, queue and
provider observations **separately**. Keep existing request/time/spend caps and
retry counters: recovery cannot renew a historical budget or re-enroll a view.

Read the **same scoped result** through the served UI, installed CLI and MCP
clients, plus the opted-in periodic consumer when qualified. Compare returned
run id, project/tenant scope, result revision, native status, original answer
artifact and signed decimal text with the persisted result. Specifically check
an explicit `0.000` against complete zero-valued pages, a missing series against
null amount, an incomplete page set against partial/null, a signed credit against
its negative string, and an unknown native outcome against `outcome_unknown`.
An answered billing source never fabricates a native answer. Foreign-scope
reads and revoked delegation must fail through the real installed path; do not
publish credentials or interpret a masked client error as a successful result.

For EC-04 retain attempt/turn counts and read-only recovery evidence; for EC-05
retain separate native, billing and comparison statuses and page proofs; for
EC-06 retain per-client responses and exact-answer comparisons. Attribute each
step's cost as measured, estimated or unknown and record the tested release
digests, attempts, pass/fail/blocked/not-run and cleanup disposition privately.
The offline recovery checks in `test_fo03.py`, `test_fo04.py`, `test_fo06.py`,
`test_fo07_recovery.py` and gateway FinOps MCP tests support pre-review only;
none establish real provider, CE, queue or installed-client acceptance.
For the pre-review installed-package selector, run
`python -m pytest -q modules/L5-services/tests/finops/test_fo07_portability.py::test_scoped_route_uses_same_installed_wheel_for_both_projects`
from the repository root with L5 and gateway test dependencies installed and
the offline test AWS configuration (`AWS_CONFIG_FILE` and
`AWS_SHARED_CREDENTIALS_FILE` set to `/dev/null`, metadata disabled, region
set to `us-east-1`). This builds one local wheel and invokes the scoped route,
fixed native/CE cases, interrupted submit and session readback, bounded billing
collection, lost consumer acknowledgment and installed CLI/MCP fixtures against
the same L5 package; it checks module origins and that the wheel digest does not
change. It does **not** install a gateway, contact a provider or run the served
UI, and cannot replace the private live fault matrix.

### SCN-03 private interruption card

In the protected evidence store, make a **separate fixture attempt** for each
boundary in the matrix above; record selected release digests, authorized
tenant/project/billing scope, original idempotency key, run/attempt/work and
native turn refs, result/publication revisions, the approved request/time/spend
limits and expected outcome **before** injecting any fault. Pin a UTC clock and
the provider-supported closed query interval. The offline submit fixture starts
at `2026-10-28T00:00:00Z` and explicitly advances beyond its own admitted
session/repair times; the offline billing fixtures use their own fixed clock.
For a live run use its actual admitted deadlines instead of copying test
offsets or renewing a historical budget.

1. Interrupt after a persisted submit receipt but before queue acknowledgment;
   replay the same scoped key and run repair. Independently read back **one**
   receipt, work generation, queue send and provider turn count. A changed
   payload must conflict, not create another paid inquiry.
2. Interrupt after provider acceptance but before turn acknowledgment and
   restart the worker; observe the **same** attempt through FO01's qualified
   read-only interface. Advance the controlled clock past a session expiry and
   retry observation under reauthorized original delegation. Record no second
   native submit, bounded attempts/polls and a concrete `outcome_unknown` if
   neither a turn nor proof of no effect can be observed. Revocation must block.
3. Interrupt after a CE page save; reconcile the original collection and
   read back every page/cursor/digest without mixing generations or exceeding
   the admitted page-request cap. Include complete zero, signed credit, missing,
   partial and stale/unknown `Estimated` cases; independently compare native
   answer artifact, exact signed amounts and separate CE state. An unknown
   native state stays unknown even if CE reports explicit zero.
4. Interrupt after Context/Insights consumer acceptance but before its receipt;
   reconcile the same publication id, inspect the actual consumer projection
   and retain the result revision. Read through installed UI, CLI, MCP and
   qualified periodic consumers with the same scope and exact answer artifact.
   Never treat a pending receipt or an API 202 as a published result.

Record each boundary's before/after SQL, queue, provider, CE and client
readbacks separately, with UTC time, attempt counts, private hashes, actual
cost state (`measured`, `estimated` or `unknown`), outcome and owner/action.
SCN-03 passes only if all real observations satisfy EC-04 (one native identity),
EC-05 (truthful billing) and EC-06 (scoped exact client results). Unsupported
injection/observation is `blocked`, not a synthetic success. For the offline
selector, run
`python -m pytest -q -s modules/L5-services/tests/finops/test_fo07.py::test_fault_acceptance`
with L5 and gateway test dependencies from the repository root. It uses fixed
fixture clocks and disables AWS credential lookup; it does not perform live
fault injection or prove a provider turn, installed UI or consumer receipt.

## Publication, reuse and owned retirement

Before retiring either fixture, freeze the private release and scope manifest,
the immutable result and publication revisions, the current binding generation,
operation-owned setup receipts and the allowed cost ceiling. A successful result
read is not a consumer acknowledgment: inspect both Context and Insights
publication ids, distinct destination receipts, projection/readback state and
the cited original answer and independent billing-page references. After a lost
consumer acknowledgment, reconcile the **same** publication id under the current
delegation; do not create another result, consumer contribution or native turn.

1. Retrieve each result through the project-scoped reader and both entitled
   consumer paths. Record the returned scope, run/result id and revision,
   `Estimated`, coverage, freshness, signed amount and citation. A later,
   **distinct** currently entitled task must retrieve the same revision and
   demonstrate its actual use of that citation; a listed publication alone is
   insufficient. Attempt the same retrieval after source/delegation revocation
   and from a foreign task/project/tenant; require denial from the installed
   reader and consumers without leaking private data. Retain the original
   immutable result and authorized reads even if setup resources are retired.
2. Stop new admissions and fixture triggers before retirement. Enumerate active
   and unknown runs, consumer work and shared-resource references. Keep bounded
   observation and repair on the existing queue; reconcile uncertain native
   effects before attempting any owned cleanup. Freeze each setup receipt's
   operation id, ownership (`owned` or `adopted`), native reference and verified
   state; do not treat a requested deletion as verified absence.
3. Retire only resources with **operation-owned** receipts. Verify provider
   context/agent absence first, then exclusively owned data roles and billing
   views after checking other bindings/configurations and their references.
   Preserve adopted resources, still-referenced views/roles, the retained agent
   scope guard while the native agent exists, results and publication receipts.
   Read back provider list/agent, role and view absence where supported. A
   context `Deleted` marker is not physical erasure; use the existing retention
   and legal/purge policy, not a new FinOps expiry.
4. A revoked cleanup grant, missing provider readback or uncertain delete is
   `blocked`/`unknown`, not `cleaned`. Preserve a durable cleanup/repair intent,
   operation id and attempt count and report the selected owner/action. Retry
   only bounded, authorized read/cleanup under the **same selected connection**;
   never recreate setup, adopt somebody else's resource, broaden IAM or switch
   accounts to make a readback pass. Recheck retained results, adopted resources
   and denied reuse after the attempted cleanup.

In the private ledger record per fixture the tested release/source/migration/
client digests, scoped setup and publication receipts, before/after references,
provider absence/readback, later-task use and denial, attempts and outcome.
Report provider/request charges separately from the inquiry's CE comparison:
mark charges `measured` only with actual independent billing evidence, otherwise
`estimated` with its basis or `unknown` (never zero by default). Record each
owned/adopted/retained/unknown resource's disposition; map EC-02 to ownership
and denial, EC-05 to missing/partial/retained result truth, EC-07 to consumer
receipts and later use, and EC-08 to both unchanged project scopes, artifact
identities, cleanup and cost proof. A local publication or ownership fixture
cannot establish live consumer use or resource absence.

The pre-review installed-package selector above also exercises the same wheel's
FO06 source revocation and publication paths, retained result reads after
unlink, owned/adopted reference checks and the gateway's denied source reuse.
It verifies that the binding, consumer and result-reader modules load from that
one wheel; those local assertions are not a provider absence readback or a
deployed consumer acknowledgment.

### SCN-04 private reuse, cleanup and proof ledger

Open a separate private ledger for **each** authorized fixture and the same
pinned release used in SCN-01–03; retain account/role selection, source/schema/
wheel/worker/client digests, billing-scope digest, original run/result and setup
operation ids, ownership receipts and current delegation. For each numbered
step record UTC time, actor/task, before/after state, independent readback and
immutable private proof reference, attempts, charge state/evidence and disposition.
Never place those values or raw consumer/billing artifacts in this repository.

1. For Context and Insights separately, inspect the actual publication id,
   accepted contribution hash, receipt, projection revision and cited result.
   Simulate lost acknowledgment only in an authorized fault fixture and repair
   the **same** contribution. From a *later distinct entitled task* retrieve
   the same immutable result revision and record its actual use of the original
   answer and CE citation. A pending receipt or a listing is not later use.
2. Revoke the controlled entitlement and attempt result and consumer reuse
   through current installed routes, also attempting a foreign project/tenant.
   Require denied reads and no private data leakage; verify the stored result
   and previously valid citation remain unchanged for authorized readers.
3. Stop new inquiries before cleanup. Reconcile active/unknown work, inventory
   every operation-owned and adopted agent/context/role/view receipt and check
   other bindings. Retire only exclusive owned resources in dependency order;
   inspect independent provider list/agent, role and view absence, then re-read
   adopted resources and retained results. A deletion response alone, a
   `Deleted` marker, revoked cleanup authority or a shared reference is not
   absence; retain a bounded cleanup intent with owner/action instead.
4. For each provider/CE/cleanup attempt, record **operation charges** separately
   from the inquiry's CE answer: `measured` requires independent charge proof,
   `estimated` requires a stated basis, and `unknown` has no invented numeric
   amount. A zero inquiry result does not establish zero provider charges.
   Capture retained/adopted/owned/unknown disposition and any failed cleanup.

For every EC-01–EC-08, child AC-01–AC-04 and SCN-01–SCN-04, create a ledger
row with tested revision, fixture, attempts, status (`passed`, `failed`,
`blocked` or `not_run`), private evidence digests, charges/disposition and next
owner/action. Use the [epic mapping](backlog.md#story-to-design-and-ec-to-ac-consistency) to
link each EC to its applicable checks, not just to a task name. In particular:

| EC | Independent SCN-04 evidence before marking passed |
| --- | --- |
| EC-02 | Controlled real view/project/tenant denial plus owned/adopted resource disposition and unchanged delegated scope. |
| EC-05 | Immutable retained result read with explicit missing/partial/zero/credit and no false attribution. |
| EC-07 | Both consumer acknowledgments, later distinct entitled use with citation and current denied reuse. |
| EC-08 | Same installed digests and two effective scopes, owned absence, adopted/result retention and measured/estimated/unknown cost report. |

Run `python -m pytest -q -s modules/L5-services/tests/finops/test_fo07.py::test_cleanup_reuse`
from the repository root with L5 and gateway test dependencies for the offline
selector. It exercises publication/revocation, exact cost states, retained
results, ownership/reference rejection and scoped denial with fictional
fixtures and no AWS credential lookup. Its passing result is **not** live
consumer use, provider cleanup or a measured operation charge.

## Proof and disposition

At the tested revisions, retain private migration, gateway, worker, queue,
schedule, provider, billing, client and consumer readbacks with the immutable
release identity. Run [FO07's scenario procedures](backlog.md#fo07-acceptance-and-scenarios)
and map each EC, AC and scenario to independent proof, attempts and measured,
estimated or unknown costs. The two non-ADP project fixtures must have distinct
effective billing scopes; a CI pass or mock answer cannot substitute for native
results, controlled denial, actual cleanup or the consumer's later entitled use.
Record every scenario as passed, failed, blocked or not run and name its next
owner/action. Do not claim live acceptance until the live readbacks exist.
