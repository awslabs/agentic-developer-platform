# Release verification evidence readers

Release verification uses the ordinary scoped L5 run API and worker. It does not
create a deployment. A server-configured deployment reader must identify the
selected project's deployment and source revision independently of the request;
the caller's references are selectors, not proof. Configure and authorize the
reader for the selected Product/project. The gateway's authenticated request
backend reads `l5_devops_verification_reader` from trusted application state
and passes it as `verification_reader` to shared admission. Without a reader, admission returns
`deployment_evidence_adapter_unavailable` and creates no run.

The [concrete queue producer](queue-verification-producer.md) implements signed
deployment evidence, a bounded independent Lambda oracle and exact owned-data
cleanup. Its Gateway-bound factory is not installed in the runtime yet. The
document lists the actual fixture preparation, permissions, source selection and
native asset qualification still required before live completion.

The deployment reader supplies `read_deployment(deployment_ref, target_ref)` and
`read_scenarios(artifact_refs)`. The former returns matching scoped source,
deployment, test-target and secret references, a deployed SHA, a fresh check time,
an expiry covering the run deadline, isolation and non-admin observations, network
scope, and the supported adapter identifier. The latter resolves pinned scenario
artifacts into bounded request counts and timeouts, positive and negative expected
outcomes, network scope, and fixture-data cleanup references. It must check the
actual configured deployment and fixture; a health URL, ADP workflow name or EKS
readback does not establish those facts. The worker repeats these checks before
native submission and records a scoped denial if the selected target changes.
Admission also serializes the provider description, including the generated run,
attempt and idempotency identifiers. Requests exceeding the provider's
10,000-character limit return `verification_description_too_large` before any
durable admission or execution work is created; the 20-scenario count is an upper
bound, not a guarantee that every set of references fits.

The worker needs two **distinct evidence sources** at collection time:

- A configured native-results reader supplies `read_native_results(space_id, task_id, execution_id)`. Its receipt must tie the native task/execution, selected deployment and source references to individual scenario statuses and evidence references.
- The deployment reader supplies `read_results(deployment_ref, execution_id)` with independent positive and negative observations, their evidence references and the deployed SHA. It also supplies `read_fixture_data(ref)` and `delete_fixture_data(ref, owner_run_id=..., idempotency_key=...)` for scoped deletion and absence readback.

The installed DevOps Agent SDK supports the `RELEASE_TESTING` backlog task but
its task response does **not** contain individual scenario results. The native
reader therefore needs a qualified provider evidence source in the selected
worker factory; this repository
must not infer outcomes from task completion or create an empty success list.
Without that reader, successful-task collection remains incomplete and retries do not submit
another native task. Native reader availability and independence from the
deployment/behavior reader require live qualification by DA19.

Cleanup deletes only data the reader confirms belongs to the run and native
execution. A deterministic deletion token makes collection replay safe; each
absence receipt and unchanged deployment identity are retained in the result.
Confirmed `FAILED` or `TIMED_OUT` tasks also retain durable collection work. The
worker performs owned-data cleanup before reading any available scenario evidence
and keeps both the run and native outcome failed. Failure readers may return
`None` when no receipt or observations exist, or return the available subset of
scenarios; supplied evidence still undergoes identity and outcome validation.
Incomplete evidence is reported with partial or unknown coverage, never invented
scenario passes. Empty outcome lists are valid only for a failed verification
with incomplete coverage, not a successful verification.
If reporting fails after deletion, collection resumes without repeating the
native tests or deleting other projects' data. DA19's private live manifest
must record the actual adapter, scoped fixture, source/runtime revision, native
task and execution IDs, individual observations and cleanup readbacks. Do not
publish live account, environment or credential details in this document.
