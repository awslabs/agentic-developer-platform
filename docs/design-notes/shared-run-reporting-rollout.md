# Shared-role engine reporting rollout

The engine can acknowledge developer and reviewer reports while protected IAM
execution authority remains disabled. It reuses the current worker IAM role and
GitHub App. Reporting authenticates a server-created SQL assignment; it does not
create separate developer/reviewer accounts or new worker roles.

Reporting activation is explicit. `shared_run_reporting_enabled` and
`shared_worker_continuation_enabled` in webhook-ingress Terraform both default to
`false`. The worker gateway ConfigMap and the existing non-secret worker-runtime
SSM wiring carry these values. Gateway Terraform consumes that wiring on its next
apply to configure the tick. Never use key presence as the activation switch.

Deploy migration 063 then 064 and compatible gateway and worker images first.
Verify the deployed worker image digest matches the reviewed release and contains
the reporting client and model proxy header support, and that `AGENT_RUN_LOGS_BUCKET` is configured. The existing
`agent-authority-signing/run-credential-key` is mirrored into an encrypted SSM
parameter owned by webhook-ingress; no secret value appears in outputs. The tick
receives only its parameter name and permission to read that exact parameter,
using the existing encryption key. Gateway pods retain their existing Secret
reference. Verify both readers can load the same key before enabling reporting.

After reviewing the infrastructure plan and runtime cohort, enable both reporting
and continuation in the gateway and tick before accepting the first governed
flow. A continuation-enabled runtime still requires that flow's attributed
acceptance; runtime flags do not supply one. Use the normal engine scheduler for
the first flow canary. Verify its real worker start, acknowledged PR binding and
terminal receipt before accepting a second flow. This avoids creating a special
dispatcher or giving the gateway new scheduler permissions.

Restart gateway pods after ConfigMap changes: updating an `envFrom` source does
not update a running process. Reconcile the tick's two resources after updating
SSM wiring, preserving its existing CI-supplied queue, event-table and App-secret
inputs and its approved release digest. `AGENT_WORKER_ROLE_ARN` always names the
existing shared worker role. These steps do not enable protected authority.

The dispatcher stores no raw capability in decisions or SQL metadata. It can
reconstruct the identical queue envelope after a publication failure, keeping the
same invocation, node attempt and SQS deduplication key. The worker uses the
existing logs bucket for an untrusted pending-report spool and records its start
before invoking the model. A redelivery with a candidate retries only reporting;
an interrupted start with no recoverable candidate requires an explicit recovery
decision. It never silently launches development again.

Legacy result reconciliation uses a valid current-attempt SQL terminal receipt
when the dispatch has a report assignment. Missing receipts remain unfinished,
even if advisory DynamoDB status says complete. Delivered code still requires
verified binding, merge, CI and accepted review evidence.

Disabling the reporting flag stops new report assignments and outbox publication;
it does not erase existing receipts or make them advisory. Keep the matching
signing key and artifact bucket available for in-flight acknowledgements. Key
rotation cannot reconstruct envelopes signed with the old key; drain or explicitly
recover outstanding assignments before rotating it.

When the webhook infrastructure hold is still active, targeting
`null_resource.keda_scaledjob` can pull unrelated resources and destructive
changes into its dependency graph. Do not apply that expanded plan. A scoped
prerequisite plan can instead target `aws_ssm_parameter.agent_run_reporting_key`,
`aws_ssm_parameter.worker_runtime_wiring`, and
`kubernetes_config_map.worker_gateway[0]`; inspect every resulting action.
Gateway restart and tick configuration still require their separate verified
steps above.

For the worker template, `scripts/plan-shared-worker-rollout.py` under
`modules/agent-factory/webhook-ingress` prepares an optimistic-concurrency JSON
patch from a live ScaledJob snapshot, the verified ECR digest, and the existing
trusted gateway and GitLab SSM URLs. It changes only the worker image and those
two environment entries. It preserves the service account, admission pause,
existing Jobs, and all other configuration. It does not apply the patch. Review
the generated patch, then use `kubectl patch --type=json --patch-file=...` on
that ScaledJob; a changed resource version requires a fresh snapshot and review.

The same command updates a supplied Terraform JSON overlay with `agent_image`
and `gitlab_webhook_enabled`. Retain it with the durable rollout record for the
verified AWS account, and explicitly pass that exact overlay after the shared
variable files on every subsequent reconciliation of this runtime. The shared
`environments/dev` inputs also serve fresh and customer accounts: do not put a
platform-account image or optional GitLab activation in those shared files.
URLs remain deployment-owned SSM inputs, and reporting/continuation flags remain
explicit separate rollout decisions. This scoped patch does not remove the
deployment hold or establish whole-module Terraform convergence. Reconciliation
without the account-specific overlay can undo the patch and must not proceed.

## Platform account maintenance and first-flow canary

For the reviewed September 2026 rollout in account `879318057152`, environment
`dev`, use the manual-only `Shared Runtime Maintenance` workflow. Its helper is
`platform/scripts/maintain-shared-runtime.py`. This is a narrow runtime rollout,
not a new deployment engine, a whole-module apply, or removal of the existing
webhook infrastructure hold. It uses the current ARC runner credentials and the
existing worker role. It does not accept policies, dispatch workers, cancel jobs,
pause the shared dispatcher, import resources, or edit orchestration rows.

Supply the confirmed account, full reviewed gateway and worker source SHAs, and
the immutable worker digest. Gateway and tick must already run the selected
gateway release. Every stage rechecks AWS identity, ECR source/digest, gateway
pod cohort, migration 064, existing signing material, and shared worker service
account. The workflow shares `gateway-release-dev` concurrency with normal
gateway deployment and never cancels another release.

| Stage | Only permitted changes |
| --- | --- |
| `prerequisites` | Three webhook prerequisite targets listed above, flags false; then the tick IAM policy and Lambda configuration with reporting/continuation false. |
| `worker` | The reviewed optimistic-concurrency ScaledJob JSON patch: image and trusted control/GitLab endpoints. Existing Jobs remain untouched. |
| `gateway-enable` | Worker gateway ConfigMap reporting/continuation true, then restart and verify the gateway. SSM and tick flags remain false during this stage. |
| `tick-enable` | Worker-runtime SSM wiring reporting/continuation true, then only the tick IAM policy and Lambda configuration. |
| `verify` | Read-only verification of actual gateway/tick flags, roles, worker image/endpoints, and signing-key equality. No Terraform inputs are generated. |

Every Terraform operation saves a private plan and validates it before applying
that exact binary. The guard rejects destruction, replacement, import/move
(including no-op state moves), writes outside that stage's address allowlist,
signing-key rotation, unrelated ConfigMap/SSM changes, existing IAM permission
changes, and unrelated Lambda environment or image changes. The only added tick
IAM statement is `ssm:GetParameter` on the exact reporting-key parameter. The
existing KMS scope and dispatch grants must survive. No raw plan, state, secret,
or reporting capability is uploaded; retained artifacts contain sanitized
evidence and non-secret account-specific inputs only.

`execute=false` prepares the first plan or patch for the selected stage without
applying it. The tick plan in `prerequisites`/`tick-enable` is generated only after
the preceding SSM change exists; execution validates this fresh plan separately
before its apply. A partial failure does not undo completed prerequisites or
start a flow. Resolve the reported stage failure and repeat the stage with fresh
verification. Successful `tick-enable` retains the account's final true flags;
earlier stages retain only the worker image/GitLab input overlay, so a temporary
gateway/tick flag difference is never presented as a converged configuration.

If the known webhook backend orphan lock remains, only `prerequisites` with
explicit execution and `unlock_known_orphan=true` may remove lock
`387a6df1-7b5c-f833-9334-305429bfdac4`. The script first verifies the live lock's
ID, owner, operation, timestamp and backend, GitHub job `106168875014` is still
the matching cancelled job, its log records orphan Terraform termination, and
its exact runner pod is absent. Any changed evidence stops the unlock. No other
lock can be removed by this workflow.

Before either enabling stage, the helper reads all organizations' current
accepted continuations, unfinished SQL report assignments (including JSON null),
executions, unresolved actions and queued authoring. It also checks non-target
ready/running stories, SQS visible/inflight/delayed counts and active worker image
compatibility. Unreviewed work or old active workers stop activation; let existing
workers finish and review that work instead of cancelling them. These checks are
observations, not a global dispatcher pause. Normal legacy scheduling remains
active, and future legacy dispatches will also acquire the shared reporting
contract after global activation.

After `verify`, obtain a fresh continuation preview and accept **only CLI**
(`0737183c-99c4-4e1f-bdb7-e4432b46ca20`, tenant `aws-e`) through the existing API,
with the reviewed concurrency-one policy. Let the normal tick choose its ready
code story, currently #5621 or #5637. The external eval prerequisites #5329,
#5331 and #5564 are not developer canary targets. Verify one real assignment,
worker start, model accounting, implementation-PR binding and successful terminal
receipt, followed by ordinary review/repair scheduling. Then obtain a fresh
preview and accept Security (`a555da26-2724-4f38-b960-8738e10fa88c`). Superplane
and other existing flows are not accepted or resumed by this maintenance step.

## Read worker acknowledgments with the existing ADP login

`GET /orchestration/flows/{flow_id}/run-reports` uses the same authenticated
`USAGE_READ` permission and tenant boundary as the graph/execution views. It
returns at most 200 assignments per page (`limit`/`offset`, with `total`), including
the run, persona, attempt, assignment time, binding at acknowledgment, and typed
worker-start, binding, terminal and review acknowledgments. Empty results mean no
report assignments were observed. A historical attempt is marked explicitly;
`is_current_attempt` does not claim that every run in that attempt is still active.

Use the graph's current PR binding and delivery progress alongside these reports.
A terminal acknowledgment records the worker's outcome; it does not establish
merge readiness or approval. Execution action references such as `dispatch:<run>`
can acknowledge successful queue publication before a worker has started, so they
are not substitutes for the worker-start/terminal fields. Binding and review
receipts that have no stored timestamp return `recorded_at: null` rather than an
invented time. No capability, ownership nonce, credential hash, dispatch envelope,
work claim, candidate body, or acceptance identity is exposed. The internal
`GET /internal/v1/agent/report` remains scoped to the worker's reporting capability.
