# Superplane API and ADP CLI design

Date: 2026-10-15

Status: Proposed API/CLI extensions, with existing interfaces identified below.
The new training and factory commands are design examples, not commands that can
be run against today's implementation.

The [architecture overview](README.md) describes the user's journey: describe a
training job, compare capacity-aware options, approve a plan and follow its
execution through ADP. The [control-plane design](control-plane-and-reconciliation.md)
defines reconciliation and execution authority. The [database design](database-design.md)
defines the records referenced here.

ADP UI, CLI, GitHub-triggered agents and other clients use the same Superplane
contract. They do not need direct Argo, SkyPilot, Kubeflow or MLflow access.
Agents may recommend a plan; the server enforces selection, approval, constraints
and spending authority before resources are provisioned.

Examples use symbolic IDs, provider/location placeholders and reserved domains.
They contain no real deployment targets. IDs such as `PLAN_ID` represent opaque
server-returned values; implementations may validate their underlying UUID type.

## 1. Public boundary and compatibility

The public gateway prefix is **`/api/superplane/v1`**. Paths in this document's
tables are relative to that prefix. Native domain routers omit it. The existing
CLI uses `API_BASE = "/superplane/v1"` because its shared gateway helper already
adds `/api`; adding `/api` to both would produce the wrong URL.

Use the current ADP login, selected deployment and organization context. Resolve
the authenticated principal and organization through the gateway/domain guard;
do not trust a body-supplied `org_id`, requester or approval decision-maker.
Check workspace grants for existing workspaces and organization provisioning
permissions for requests that may create one. Cross-tenant IDs return a
non-disclosing not-found response. Opaque IDs are not access tokens.

| Existing interface | Reuse or extension |
| --- | --- |
| `GET /capabilities` | Add versioned training, planning, factory and transport capabilities. |
| `GET /workspaces/{workspace_id}/deployment-profiles` | Retain workspace-specific discovery; add training/runtime compatibility metadata. |
| `POST /operation-approvals`, `GET /operation-approvals/{approval_id}`, `POST /operation-approvals/{approval_id}/decision` | Retain one approval owner; extend its request subject/action model for immutable training plans. |
| `GET /operations/{operation_id}`, `GET /operations/by-idempotency/{idempotency_key}` | Retain execution-operation observation/recovery. Operation IDs are distinct from training-run IDs. |
| Workspace lifecycle proposal/preview/continue and retirement preview/submit routes | Retain governed changes, recovery and removal instead of adding an unrestricted factory-delete route. |
| `adp superplane onboarding ...`, `cluster ...`, `provider-connection ...` | Preserve existing commands and their approval, credential and sharing rules. |

The current approval request accepts `workspace_id`, an action of `provision` or
`teardown`, an idempotency key and string parameters. It rejects extra fields.
The training approval body below therefore requires a typed subject/action
extension across the API, approval service and execution contract. It is not
supported just because the approval URL exists.

The gateway uses an explicit method/path allowlist and the domain keeps an
endpoint inventory. Every new route must be added to both, with authorization
and request/response contracts. The current generic proxy buffers bodies and
does not transparently forward arbitrary headers or streaming responses.

For the first implementation, put `idempotency_key` and `expected_version` in
JSON bodies, and return polling paths/delays in JSON. Use bounded cursor polling
for events and logs. `Idempotency-Key`, `If-Match`, `ETag`, `Location`,
`Retry-After`, SSE and resumable streaming require explicit gateway/transport
changes before clients can depend on them.

## 2. Common request and response rules

All new schemas have a version. A v1 endpoint may add optional response fields;
clients ignore unknown response fields and display unknown states safely.
Reject unknown mutation fields and unsupported requested schema versions. Required
semantic changes need a new schema/capability version, with compatibility during
rolling upgrades. Publish OpenAPI schemas and CLI command-manifest entries as
part of implementation.

| Concern | Proposed contract |
| --- | --- |
| Idempotency | Persist a caller-supplied `idempotency_key` for every new durable command; existing command bodies retain their compatibility contract. Scope it to organization, principal and action; the normalized body hash excludes the key. Same key/body returns the original resource; changed body returns `409 idempotency_conflict`. Reauthorize every replay. |
| Concurrency | Responses expose `version` for mutable resources. Request revision changes and run continuations require `expected_version`; mismatches return `409 version_conflict`. Immutable plans cannot be patched. |
| Async acceptance | `202` means the durable command was accepted, not that infrastructure exists or training succeeded. Return resource/operation handles, `status_path` and `poll_after_seconds`. |
| Creation | `201` creates a request, plan or other synchronous domain record. Planning records alone cannot authorize cloud work. |
| Pagination | Opaque `cursor`, bounded `limit` and deterministic ordering; response has `items` and `next_cursor`. Cursors are tenant/query-bound, not authorization. Recheck access on every page. |
| Time and money | RFC 3339 UTC timestamps, integer seconds, decimal-string currency amounts. Execution budget ceilings map to existing integer cost micros through the server. |
| References | Resolve code/image/data/profile/credential references through authorized registries. Do not accept arbitrary privileged service accounts, vault paths or provider credentials in training specs. |
| Errors | Structured code, safe message, request ID, retryability and next action. Never echo credentials, internal endpoints or raw provider exception bodies. |

Idempotency keys remain recoverable while effects or retained exposure may exist.
A lost response must be resolved by lookup or retrying the same command/key.
Generating a new key to overcome a timeout can create a second intent. Independent
keys still cannot consume the same plan for two runs.

## 3. Discovery, workspaces and the four factories

The user normally selects approved profiles and requirements. Factory resources
are internal orchestration objects with user-visible status, not an API for
arbitrary cloud mutations.

| Method and path | Result | ADP CLI mapping |
| --- | --- | --- |
| `GET /capabilities` — existing, extend | Enabled features/schema versions, provider combinations, runtime compatibility, evidence sources, planning/approval support and polling limits. | Existing `adp superplane onboarding capabilities` |
| `GET /provisioning-profiles` — proposed | Organization-authorized, versioned environment/network/cluster/platform compositions that can be used before a workspace exists. | Proposed `adp superplane factory profiles` |
| `GET /provider-environments` — proposed view | Authorized environment summaries from the existing `cloud_accounts` inventory, with create/onboard capabilities and baseline status. | Proposed `adp superplane factory environments` |
| `GET /workspaces/{workspace_id}/status` — proposed | Readiness by account, networking, cluster and workload layers; current desired/observed revisions, freshness, blockers and active operations. | Proposed `adp superplane workspace status --workspace WORKSPACE_ID` |
| `GET /factory-resources?workspace_id=WORKSPACE_ID` — proposed | Factory resources accessible through that workspace's bindings. An organization-authorized query may omit the workspace filter. | Proposed `adp superplane factory list --workspace WORKSPACE_ID` |
| `GET /factory-resources/{resource_id}` — proposed | Desired revision, ownership/retention, dependency gates, Argo state, provider observations and drift as separate fields. | Proposed `adp superplane factory show --resource-id RESOURCE_ID` |
| `GET /workspaces/{workspace_id}/applications` — proposed | Maintained application versions, Argo sync/health, target readiness and operator compatibility. | Proposed `adp superplane workspace applications --workspace WORKSPACE_ID` |

Creating or changing factory resources follows an approved training plan or the
existing workspace lifecycle preview/approval/continue path. Repair outside an
approved reconciliation policy returns an actionable approval requirement.
There is no public "force sync to bypass approval" operation. Workspace retirement
uses its existing preview and durable retirement flow; account closure remains
separate. Shared resources and retained data are visible in previews.

The existing lifecycle URLs are `GET /workspaces/{workspace_id}/lifecycle`,
`GET /workspaces/{workspace_id}/lifecycle-proposals`, and
`POST /workspaces/{workspace_id}/lifecycle-proposals/{artifact_id}/preview` or
`/continue`. Their CLI equivalents are `adp superplane onboarding lifecycle
list`, `plan`, `request-approval` and `continue`, with `--workspace`, an artifact
ID and the relevant plan revision/approval. The proposed factory action types
must be added to that artifact/authority contract before it can execute them.
Retirement uses `POST /workspaces/{workspace_id}/retirement/preview` and
`POST /workspaces/{workspace_id}/retirement`. Standalone organization account
closure and shared-network redesign require separately defined lifecycle
subjects/permissions; the initial training API does not expose those mutations.

For a new workspace, the request names a provisioning profile. Its organization
service authority may create/onboard an AWS account, Azure subscription, GCP
project or neocloud environment and establish the selected baseline. It does not
grant the caller direct use of those credentials. Existing workspace-bound
provider connections remain bound to exactly one workspace. Unavailable
new-environment quota evidence is exposed as a conditional check.

## 4. Planning and selection

All routes and training commands in this section are proposed.

| Method and path | Behavior | CLI after `adp superplane` |
| --- | --- | --- |
| `POST /training/requests/validate` | Validate schema, references and request permissions without saving intent or dispatching work. | `training request create --file FILE --dry-run` |
| `POST /training/requests` | Save revision 1 of user intent; return `201`. | `training request create --file FILE --key KEY` |
| `GET /training/requests/{request_id}` | Read current intent and revision history references. | `training request show --request-id ID` |
| `POST /training/requests/{request_id}/revisions` | Append a revision with `expected_version`; preserve earlier evaluations/plans. | `training request revise --request-id ID --file FILE --expected-version N --key KEY` |
| `POST /training/requests/{request_id}/evaluations` | Evaluate a specified immutable revision asynchronously; return `202` with evaluation handle. | `training evaluate --request-id ID --revision N --key KEY` |
| `GET /training/evaluations/{evaluation_id}` | Planning state, completion/expiry, failed checks and polling advice. | `training evaluation show --evaluation-id ID` |
| `GET /training/evaluations/{evaluation_id}/options` | Completed ranked options, rejection reasons and evidence freshness. | `training options --evaluation-id ID` |
| `POST /training/plans` | Select one option and freeze an executable plan; return `201`. No provisioning or approval is implied. | `training plan select --option-id ID --key KEY` |
| `GET /training/plans/{plan_id}` | Full immutable plan, digest, validity, actions, exposure and cleanup terms. | `training plan show --plan-id ID` |

The request expresses requirements, not a raw TrainJob or Terraform document.
Agents can translate an issue into this schema and explain it to the user. Both
the issue identity and the submitting actor remain auditable; issue authorship
or an "approved" comment alone is not a server approval.

An illustrative request body for `POST /training/requests`:

```json
{
  "schema_version": "training.request.v1",
  "idempotency_key": "example-request-1",
  "name": "example-model-training",
  "target": {
    "mode": "create_workspace",
    "provisioning_profile_id": "PROFILE_ID",
    "provisioning_profile_revision": 1
  },
  "spec": {
    "code": {"source_ref": "CODE_SOURCE_REF", "revision": "COMMIT_DIGEST"},
    "image_ref": "APPROVED_IMAGE_DIGEST_REF",
    "runtime_profile_ref": "RUNTIME_PROFILE_VERSION_REF",
    "dataset_refs": ["DATASET_VERSION_REF"],
    "output_ref": "ARTIFACT_DESTINATION_REF",
    "arguments": ["--epochs", "3"],
    "compute": {
      "accelerator_class": "APPROVED_GPU_CLASS",
      "gpus_per_worker": 1,
      "workers": 2,
      "min_gpu_memory_gib": 40,
      "cpu_per_worker": 8,
      "memory_gib_per_worker": 64
    },
    "constraints": {
      "hard": {
        "allowed_providers": ["PROVIDER_KEY"],
        "allowed_regions": ["REGION_CODE"],
        "data_location_policy_ref": "LOCATION_POLICY_VERSION_REF",
        "complete_within_seconds": 10800
      },
      "preferences": {"rank_by": ["lowest_total_cost", "earliest_finish"]}
    },
    "budget": {"currency": "USD", "max_amount": "100.000000"},
    "execution_policy": {
      "max_runtime_seconds": 7200,
      "max_automatic_retries": 1,
      "checkpoint_policy_ref": "CHECKPOINT_POLICY_VERSION_REF"
    },
    "success_criteria": {
      "required_artifact_kinds": ["model", "evaluation_report"]
    },
    "cleanup_policy_ref": "CLEANUP_POLICY_VERSION_REF"
  }
}
```

For reuse, `target` instead contains `mode: "existing_workspace"` and
`workspace_id`. These target forms are mutually exclusive. Resolve relative
deadline requirements to an absolute time at request creation, so replanning
cannot silently extend the deadline. Enforce applicable organization constraints
even when the client omits them.

Evaluation queries central SkyPilot/provider adapters, authorized inventory,
runtime compatibility, quota, pricing and data locality/access. It must not
provision accounts, networks, clusters or machines, start paid probes or buy
capacity reservations. Read-only service rate limits still apply.

An option includes the following fields; this is a shape example with one
conditional candidate, not evidence of available GPUs:

```json
{
  "id": "OPTION_ID",
  "evaluation_id": "EVALUATION_ID",
  "request_revision": 1,
  "rank": 1,
  "feasibility": "conditional",
  "placement": {
    "provider": "PROVIDER_KEY",
    "region": "REGION_CODE",
    "workspace_action": "create",
    "accelerator_class": "APPROVED_GPU_CLASS",
    "total_gpus": 2
  },
  "preparation": [
    {"layer": "account", "action": "onboard"},
    {"layer": "networking", "action": "create"},
    {"layer": "cluster", "action": "create"},
    {"layer": "workload", "action": "install_platform"}
  ],
  "capacity": {
    "evidence_kind": "catalogue",
    "availability": "unknown",
    "observed_at": "2030-01-01T12:00:00Z",
    "valid_until": "2030-01-01T12:05:00Z",
    "reservation": null
  },
  "estimate": {
    "currency": "USD",
    "total_amount_range": ["50.000000", "90.000000"],
    "components": ["setup", "compute", "data_transfer", "storage"],
    "queue_seconds_range": null,
    "finish_time_confidence": "unknown",
    "assumptions": ["No committed capacity or queue-time guarantee"]
  },
  "conditional_checks": ["account_quota", "capacity_at_acquisition"],
  "deadline_feasibility": "unknown"
}
```

Actual estimates include an amount/range or explicit unknown reason for every
component, plus setup, transfer, queue, execution and total completion timing.
Deadline feasibility includes all those stages. Catalogue, provider observation,
existing allocation and reservation evidence must remain distinguishable.
Quotes show currency, source, time, validity and uncertainty. Planning offers
2–3 candidates when available; it never fabricates choices or relaxes hard
constraints to fill that count. Zero/one-option results are normal `200` reads
of a completed evaluation. Explain rejected constraints and possible user edits.

`POST /training/plans` takes `option_id` and `idempotency_key`. The server resolves
the immutable request/evaluation, checks validity and freezes the exact actions,
dependency generations, profile versions, cost/runtime limits, cleanup policy
and fallback bounds. The plan exposes conditional checks and the maximum setup
exposure allowed before quota/capacity is known. The default permits no movement
outside the selected provider/location; any permitted alternatives must be
explicit plan fields before approval.

## 5. Approval, admission and execution

Approval and submission are distinct. A UI button can guide both steps, but an
approval decision cannot implicitly dispatch training.

| Method and path | Behavior | CLI after `adp superplane` |
| --- | --- | --- |
| `POST /operation-approvals` — extend existing | Request approval of the exact training-plan subject; return its approval ID and decision requirements. | Proposed `training plan request-approval --plan-id ID --key KEY` |
| `GET /operation-approvals/{approval_id}` — existing | Decision, expiry/revocation and whether this caller may decide. | Existing `onboarding approval show --approval-id ID` |
| `POST /operation-approvals/{approval_id}/decision` — existing decision shape | Authorized approver allows once or rejects; distinct-approver policy still applies. | Existing `onboarding approval decide --approval-id ID --result allowed-once` |
| `POST /training/plans/{plan_id}/submission-preview` — proposed | Read-only validation of the proposed submission and current known constraints. No approval consumption, execution operation or spending reservation. | Proposed `training submit --plan-id ID --approval-id ID --dry-run` |
| `POST /training/runs` — proposed | Persist run/admission intent and return `202`; governed admission and preparation proceed asynchronously. | Proposed `training submit --plan-id ID --approval-id ID --key KEY` |
| `GET /training/submissions/by-key/{idempotency_key}` — proposed | Recover the submitting principal's authorized run receipt after a lost response. | Proposed `training run recover --key KEY` |
| `POST /training/runs/{run_id}/continuations` — proposed | Accept a newly approved pre-start replacement plan with `expected_version`; preserve the run and its previous resource/exposure history. | Proposed `training run continue --run-id ID --plan-id ID --approval-id ID --expected-version N --key KEY` |

The proposed training variant for the existing approval endpoint is:

```json
{
  "subject": {"kind": "training_plan", "id": "PLAN_ID", "revision": 1},
  "action": "training.execute",
  "idempotency_key": "example-approval-1"
}
```

The server, not the client, supplies the canonical plan digest, requester,
organization, existing or reserved workspace intent, resource/runtime bounds and
integer cost ceiling to the approval owner. Preserve the existing provision and
teardown request variants. The decision body remains
`{"result": "allowed-once"}` or `{"result": "rejected"}`.

Submitting agents need recorded delegation for the approved requester and target
scope. Possession of an approval ID alone does not make it transferable to any
principal. Factory service authority and each underlying mutation gate still
apply to the actions in the approved composition.

Submission takes only references to approved immutable intent:

```json
{
  "plan_id": "PLAN_ID",
  "approval_id": "APPROVAL_ID",
  "idempotency_key": "example-submit-1"
}
```

An illustrative `202` receipt is:

```json
{
  "id": "RUN_ID",
  "version": 1,
  "phase": "admitting",
  "plan_id": "PLAN_ID",
  "admission_id": "ADMISSION_ID",
  "operation_id": null,
  "status_path": "/api/superplane/v1/training/runs/RUN_ID",
  "poll_after_seconds": 5
}
```

An operation ID can be null until the execution owner accepts admission. The
coordinator uses stable keys to revalidate approval/grants, expiry, capacity,
quota, pricing, budgets, dependencies and data access, then resolves budget and
execution receipts. Domain acceptance alone permits no cloud mutation. Partial
admission or ambiguous receipts are recovered through the existing owners before
dispatch; there is no distributed transaction spanning these systems.

The budget owner apportions the approved envelope across preparation and training
operations and counts prior confirmed/retained exposure during continuation.
Starting another factory stage cannot multiply the user's spending ceiling.

Preparation then orchestrates account baseline, networking, cluster and platform
readiness. Argo maintains the approved factory/application declarations; factory
controllers and governed workers perform provider work. Current-generation
readiness and scoped execution authority gate each dependent stage. Superplane
submits the individual TrainJob through the workload owner after readiness and
capacity checks. Kueue admission, Trainer execution and capacity acquisition have
their own observable states.

Revalidate volatile capacity at acquisition as well as at submission. If the
selected option is no longer feasible outside approved fallback bounds, expose
`replan_required`, hold new work and preserve any partially created resources.
Do not silently change provider, region, data placement or spending ceilings.

For a pre-start continuation, evaluate with `run_id` in the evaluation request so
the planner includes current resources, incurred/retained exposure and unresolved
cleanup. Selection includes `continuation_of_run_id` and `expected_run_version`.
The replacement plan explicitly describes reuse/retention/removal, has a new
digest and approval, and can be submitted only while the run is still eligible
for pre-start continuation. Serialize against cancellation and other continuations.

Automatic retries within the approved retry/checkpoint policy keep the same run;
the execution owner manages attempts. An intentional rerun or placement change
after training starts creates a new plan/approval and run. Its request may carry
`resumes_run_id` and a verified, authorized `checkpoint_artifact_id`. Reusing an
approval or clicking submit twice cannot create another experiment.

## 6. Progress, results and cleanup

These proposed reads expose normalized, authorized views. Backend observations
retain source and freshness; they do not become trusted just because an agent
included them in a message.

| Method and path | Result | CLI after `adp superplane` |
| --- | --- | --- |
| `GET /training/runs?workspace_id=ID&phase=running` | Paginated runs authorized in the current org; filters are optional. | `training run list --workspace ID --phase running` |
| `GET /training/runs/{run_id}` | Current phase, admissions/operations, factory/queue/training state, freshness, results, cost and cleanup. | `training run show --run-id ID`; `training run watch --run-id ID` polls this view. |
| `GET /training/runs/{run_id}/events?cursor=CURSOR` | Bounded ordered run events with source timestamps and next cursor. | `training run events --run-id ID --follow` |
| `GET /training/runs/{run_id}/logs?cursor=CURSOR` | Bounded log chunks with attempt, worker/container, timestamps and resume cursor; optional worker/attempt filters. | `training run logs --run-id ID --follow` |
| `GET /training/runs/{run_id}/metrics` | Available metric names and bounded/downsampled series, selected by name/time range. | `training run metrics --run-id ID` |
| `GET /training/runs/{run_id}/artifacts` | Model/checkpoint/report metadata and verification/retention state. | `training run artifacts --run-id ID` |
| `POST /training/runs/{run_id}/artifacts/{artifact_id}/access` | Reauthorize read access and issue a short-lived download grant or bounded proxy handle. This read-access operation creates no durable execution intent. | `training run download --run-id ID --artifact-id ID --output PATH` |
| `POST /training/runs/{run_id}/cancellation` | Persist an authorized monotonic stop request under an idempotency key; `202` until observed. | `training run cancel --run-id ID --key KEY` |
| `POST /training/runs/{run_id}/cleanup` | Request/resume only the cleanup already authorized by the run policy; return a durable receipt. Broader removal requires a lifecycle plan. | `training run cleanup --run-id ID --key KEY` |

A run view must separate the following dimensions:

| Field | Meaning |
| --- | --- |
| `phase` | `admitting`, `preparing`, `queued`, `running`, `replan_required`, `succeeded`, `failed` or `cancelled`. |
| `preparation` | Each layer's desired/observed revision, Ready conditions and dependencies; current bootstrap stage and recovery action. |
| `placement` | Approved and actual placement, with any permitted fallback explicitly recorded; no silent relocation. |
| `applications` | Argo sync/health and operator readiness, distinct from infrastructure drift. |
| `capacity` / `queue` | Acquisition evidence, quota/allocation state and Kueue admission reason. GPU catalogue availability alone cannot mean ready. |
| `execution` | Trainer/job/pod facts, attempt references, progress, times and termination reason. |
| `observations` | Source, observed/received timestamps, age, freshness and disconnected observer reasons. |
| `result_verification` | `pending`, `verified`, `failed` or `not_required`, including required-artifact checks. |
| `cancellation` | `not_requested`, `requested`, `in_progress`, `complete` or `blocked`; request acceptance is not termination evidence. A request racing with completion preserves the observed compute outcome. |
| `cleanup` | `not_started`, `in_progress`, `complete`, `retained` or `blocked`; remaining allocations and their authorized disposition. |
| `cost` | Estimated, reserved and observed amounts with currency, covered time and billing finality. These are distinct values, not additive totals. |
| `blockers` / `next_actions` | Stable reason codes and authorized actions, such as approve a new plan, restore access or inspect cleanup. |

Freshness is per source, not a single misleading global green indicator. Preserve
the last known phase if an observer disconnects and mark it stale. A stale
observation cannot prove successful training, absence of a resource or capacity
release. Terminal compute state remains visible if result verification or cleanup
is still pending. Cost may remain provisional after termination.

Cancellation requires the caller's run-control permission and uses the approved
stop/cleanup policy. It prevents new run dispatch and requests shutdown of owned
work; it does not delete a shared cluster or close an account. During a network
partition the request remains pending until termination is observed. Workspace
execution deadlines/local enforcement continue during a management outage.

Log and artifact access rechecks workspace/data permissions. Redact credentials
and restrict internal topology. Download grants are scoped and short-lived;
never place them in public issue comments or persistent CLI logs. Basic metrics,
status and verified artifacts work without MLflow. Cursor expiry returns a clear
gap/retention result and a safe restart position instead of silently dropping
history. Empty log polling returns no new entries, not a lost-run error.

## 7. CLI experience and complete example

Reuse `adp login` and the existing selected deployment/org context. Do not add a
second Superplane token store or ask users to pass cloud credentials on the
command line. New leaf commands support `--json`; progress and diagnostics go to
stderr, and stdout remains machine-readable.

Existing CLI JSON uses this envelope:

```json
{
  "status": "pending",
  "command": "superplane training submit",
  "detail": {
    "run_id": "RUN_ID",
    "phase": "admitting",
    "status_path": "/api/superplane/v1/training/runs/RUN_ID"
  },
  "next_action": "adp superplane training run watch --run-id RUN_ID"
}
```

Preserve current exit conventions: `0` success, `1` usage/validation, `2`
authentication, `3` authorization, `4` pending/unavailable/retryable and `5`
failure. A successful asynchronous submission returns a usable receipt and
pending exit `4`; automation must not treat this as permission to resubmit with
a new key. Read/list commands return `0` when the requested snapshot is retrieved,
even if it describes an active run. Distinguish remote run failure from transport
failure in structured output.

`training run watch` waits for terminal compute state, then verifies required
results before returning success. `--wait-for-cleanup` additionally waits for
verified cleanup or authorized retention. Failed/cancelled computation or failed
required-artifact verification returns `5`; timeout, stale terminal evidence or
approval/replanning blockers return `4` with the current receipt and next action.
Cleanup may continue after the default watch exits and stays visible in output.
Ctrl-C or a disconnected CLI watcher detaches locally; it never cancels remote
work. `--timeout` bounds local waiting, not job runtime.

Mutating commands support the existing `--yes` prompt convention. `--yes` skips
only a local confirmation; it cannot grant approval or bypass a distinct
approver. Supported `--dry-run` paths validate and preview without creating an
operation, consuming approval, reserving capacity or dispatching. Unsupported
dry-run operations fail clearly rather than executing. Planning deliberately
saves evaluation records but performs no provisioning.

The following walkthrough uses proposed training commands. `training-request.json`
contains the request above without `idempotency_key`; `--key` supplies it. Set
the uppercase ID variables from the previous command's response. Keys shown are
fictional; use a new stable key for each intentional command and keep it for
retries.

```bash
adp login
adp superplane onboarding capabilities --json
adp superplane factory profiles --json

adp superplane training request create \
  --file training-request.json --dry-run --json
adp superplane training request create \
  --file training-request.json --key example-request-1 --json

adp superplane training evaluate \
  --request-id "$REQUEST_ID" --revision 1 --key example-evaluation-1 --json
adp superplane training evaluation show --evaluation-id "$EVALUATION_ID" --json
adp superplane training options --evaluation-id "$EVALUATION_ID" --json

adp superplane training plan select \
  --option-id "$OPTION_ID" --key example-selection-1 --json
adp superplane training plan show --plan-id "$PLAN_ID" --json
adp superplane training plan request-approval \
  --plan-id "$PLAN_ID" --key example-approval-1 --json
```

After reviewing the full plan, the authorized approver uses their own ADP session.
Existing decision semantics apply; a requester cannot act as the approver when
policy requires a different person:

```bash
adp superplane onboarding approval show --approval-id "$APPROVAL_ID" --json
adp superplane onboarding approval decide \
  --approval-id "$APPROVAL_ID" --result allowed-once --yes --json
```

The submitting user or delegated agent can then execute and inspect the run:

```bash
adp superplane training submit \
  --plan-id "$PLAN_ID" --approval-id "$APPROVAL_ID" \
  --key example-submit-1 --yes --json
adp superplane training run recover --key example-submit-1 --json
adp superplane training run watch --run-id "$RUN_ID" --timeout 3600
adp superplane training run logs --run-id "$RUN_ID" --follow
adp superplane training run metrics --run-id "$RUN_ID" --json
adp superplane training run artifacts --run-id "$RUN_ID" --json
adp superplane training run download \
  --run-id "$RUN_ID" --artifact-id "$ARTIFACT_ID" --output ./model-output
```

Cancellation is an explicit alternative when the user wants to stop an active
run; it is not part of successful model download:

```bash
adp superplane training run cancel \
  --run-id "$RUN_ID" --key example-cancel-1 --yes --json
adp superplane training run show --run-id "$RUN_ID" --json
adp superplane training run cleanup \
  --run-id "$RUN_ID" --key example-cleanup-1 --yes --json
```

If capacity requires a pre-start replacement plan, the equivalent continuation
commands are `training evaluate --run-id RUN_ID` in addition to request/revision,
`training plan select --continuation-of-run-id RUN_ID --expected-run-version N`
in addition to option/key, a new approval, then `training run continue` from the
route table. The CLI must show retained resources and additional exposure before
confirmation. There is no generic retry command that silently changes placement.

## 8. Errors, limits and event delivery

The new structured error envelope requires corresponding CLI parser support.
The current shared client recognizes legacy `detail` and string error/reason
forms; normalize both old and new responses during migration, including exit-code
mapping for validation and retryable HTTP errors. Do not change legacy callers'
behavior merely by adding these routes. An illustrative new error envelope is:

```json
{
  "error": {
    "code": "plan_expired",
    "message": "The selected plan requires a new capacity evaluation.",
    "request_id": "REQUEST_TRACE_ID",
    "retryable": false,
    "details": {"plan_id": "PLAN_ID"},
    "next_action": {"type": "evaluate_request", "request_id": "REQUEST_ID"}
  }
}
```

| HTTP status | Typical codes and client behavior |
| --- | --- |
| `400` / `422` | Invalid schema, unsupported fields or incompatible runtime/constraints. Correct the request; do not retry blindly. |
| `401` / `403` | Login required, insufficient grant, approval denied or revoked authority. Preserve the command key while resolving access. |
| `404` | Missing or inaccessible resource, including unknown authorized submission key. A lookup miss is not proof that an in-flight request cannot still commit. |
| `409` | Idempotency/body conflict, version conflict, expired/superseded plan, plan already consumed, approval mismatch or invalid continuation state. Reconcile the referenced resource. |
| `410` | Expired event/log cursor with retention/gap metadata and an available restart cursor. |
| `429` | Per-principal/org planning, polling or submission limit; return `retry_after_seconds` in JSON. |
| `502` / `503` / `504` | Backend unavailable or response uncertain. Read the receipt or retry with the same key and body. |

Failures discovered after `202` appear on the accepted evaluation/run with
stable reason codes and next actions. Capacity scarcity is a planning outcome
or a run blocker, not necessarily an HTTP service failure. A reservation cannot
be called released until its authoritative settlement is known.

Set separate limits for evaluation frequency, concurrent active runs, expensive
provider queries, event/log page size and artifact-access requests. Cap request
payload and log bytes, metric points and pagination size. Return effective limits
through capabilities. Pollers use server advice with backoff/jitter; gateways
must have bounded timeouts for every response.

Persist user-visible events and notification intent independently of an agent
process. GitHub callbacks use configured authorized delivery, deduplication keys
and redacted summaries with ADP links. Duplicate issue/webhook delivery maps to
the same request/run intent. Future SSE can improve latency but must preserve
the same authorization, cursors, retention and reconnect semantics as polling.

## 9. Implementation acceptance

Implement this contract in slices: discovery/read-only planning; exact-plan
approval and durable admission; factory preparation; TrainJob submission and
observation; results/cancellation/cleanup; then optional streaming. Advertise a
capability only when its routes, CLI, owner integrations and provider profile
have passed acceptance.

Required scenarios include:

- An organization-only caller can plan a new workspace without access to another
  workspace's credentials; cross-tenant references and excessive grants fail.
- Zero/one/multiple options, stale/catalogue-only evidence, quota unknown until
  account creation and hard deadline/location constraints remain explicit.
- Planning, option selection, approval and dry-run perform no provisioning or
  billable reservation; submission before valid approval cannot dispatch.
- Duplicate keys, different-body replays, concurrent consumption of one plan,
  lost gateway responses and crashes during admission produce one governed run.
- Price/capacity changes before acquisition require a bounded fallback or a new
  plan, preserving earlier resources, exposure and execution history.
- Gateway allowlisting, domain authorization, JSON bodies, status/cursor fields
  and CLI stdout/exit codes work together; pending receipts survive automation.
- Stale observations, reconnect/relist, log cursor expiry and missing MLflow do
  not invent terminal state or lose access to basic results.
- Cancellation during capacity creation, failed artifacts, shared cluster
  cleanup and management recovery expose the true remaining work and cost.

These are proposed implementation gates. This documentation does not report
passing API, CLI, provider or live training acceptance.

## Source references

- [Gateway Superplane proxy](../../../modules/gateway/src/domain_proxy/superplane.py) and [route allowlist](../../../modules/gateway/src/domain_proxy/superplane_routes.json)
- [Domain endpoint inventory](../../../modules/domain-apps/superplane/src/superplane-api/app/endpoint_inventory.py)
- [Existing approval router](../../../modules/domain-apps/superplane/src/superplane-api/app/routers/operation_approvals.py)
- [ADP Superplane CLI](../../../modules/gateway/cli/adp-superplane.py), [onboarding commands](../../../modules/gateway/cli/adp-superplane-onboarding.py) and [lifecycle commands](../../../modules/gateway/cli/adp-superplane-lifecycle.py)
- [Shared CLI conventions](../../../modules/gateway/cli/adp_common.py) and [command manifest](../../../modules/gateway/cli/command-manifest.json)
- [Database design](database-design.md)
