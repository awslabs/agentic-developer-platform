# Chat runtime access inventory (#6931 / SEC01)

This is a **source audit**, not an observation of a running pod. The checked-in
`chat-agent-worker` source no longer imports the old queue consumer, model
SDK, Bash/file tools or data clients into its entrypoint; it refuses to start
before reading a message. Separate source files still contain old tooling, but
neither the retired entrypoint nor the sandbox bootstrap invokes it. Its Terraform role is queue-depth
only, with explicit model, owner-store and gateway denies. These changes have
not been verified against any installed role, old pod or running image.

| Surface | Source-backed finding | Required qualification |
|---|---|---|
| Durable turn results | `chat_turn_result.py` accepts only same-owner, same-run outcomes through the workload-bound `turn.result` capability. A success references an assistant message already saved by that turn; failure carries no raw exception text. Conditional writes bind the immutable `result_candidate` to the accepted input, current lease, grant and execution, and reject cancellation races. The executable sandbox submits this receipt after recording its reply. The receipt explicitly remains nonterminal: it does not prove sandbox teardown, deliver a final browser response, release processing locks or acknowledge the input queue. | Connect the trusted supervisor to result reconciliation, verified teardown and terminal delivery; then qualify that complete path on the authorized deployed image under #6937. |
| IAM credential provider | `modules/agent-factory/infra/gateway-main.tf` annotates `adp-agent` with the chat role. `modules/agent-factory/infra/chat-worker-iam.tf` permits web-identity assumption by that service account and KEDA role assumption. The ScaledJob uses that account. | Observe mounted identity and effective role, including other attached policies and old pods; source does not prove effective IAM. |
| AWS permissions | The checked-in chat-worker role allows only SQS queue attributes for KEDA. Explicit denies cover direct Bedrock, DynamoDB, S3, Secrets Manager, KMS, gateway invocation, role assumption and queue consumption/response. The separate trusted gateway retains its own owner-store role. Neither this policy source nor a passing static check establishes the effective deployed permissions. | Probe STS and direct SDK/CLI S3, DynamoDB and Secrets Manager access from the sandbox; verify denied platform and other-user data, not just model refusal. |
| Trusted response relay | `modules/agent-factory/infra/gateway-chat-response-access.tf` grants only the trusted gateway service role `sqs:SendMessage` on the exact response FIFO queue. Its URL comes from an explicit `ADP_CHAT_RESPONSE_QUEUE_URL` configuration value; there is no inferred destination, and this response policy grants no input-queue access. The separate pending-input publisher is described below. The sandbox receives neither queue credentials nor this setting. `chat_delivery.py` derives recipients from protected registered-turn metadata and rechecks session ownership, incarnation and active task. The response Lambda repeats these checks without a stale-connection fallback. Provisional text and authenticated owner cancellation are wired; final completion remains unfinished. `chat_cancellation.py` resolves the current registered turn through an atomically stored owner-routing index, including before a context or lease exists. Owner-authorized abort intent fences both pod binding and lease admission against cancellation races and revokes scoped model/data authority. Existing admitted turns without an index retain their lease-bound cancellation path; unindexed queued turns fail closed. Cancellation does not assert sandbox teardown, acknowledge the input queue or clear the turn's processing lock. | Verify the installed attachment and configuration, owner delivery and cancellation, and denied direct queue access from the sandbox. Source tests and browser-hook fixtures are not deployed transport evidence. |
| Projected tokens and files | `modules/agent-factory/agent/k8s/chat-scaledjob.yaml` mounts the `adp-agent-bootstrap` token and does not disable default service-account token mounting. An older deployed worker image may still put its SDK workspace and projected AWS identity in the same execution container. A separate role-free, non-automounting `adp-chat-sandbox` account and bounded scratch template are defined in source, but the checked-in supervisor Job is suspended and no supervisor job is installed. | Inspect actual mounts, environment, writable paths, `/proc`, token audiences and provider-chain resolution in the deployed image. Use synthetic canaries, not real secrets. |
| Model subprocesses | `modules/agent-factory/agent/src/complex-task-chat/run-query.ts` allows Bash, file and web tools in bypass mode. The retired routing helper refuses model calls even if imported directly; the worker entrypoint does not start the Bash/file tools. A separate sandbox image contains a fail-closed bootstrap. Tool shells in an older deployed image may still start background processes. | Test direct reads and provider calls from the tool process, then check process and file absence after a turn and in A2/B1 sandboxes. |
| Model and data path | `bedrock-routing.ts` spawns a proxy using a copy of the worker environment, including workload identity. `chat-scaledjob.yaml` still supplies direct-store settings (`ADP_CHAT_DATA_ENABLED=false`), but checked-in worker startup, store construction and model routing all refuse them. The scoped gateway ports added by #6932 exist; no complete delegated provider and durable response path is installed, so chat must remain disabled. An older deployed image or policy is not characterized by this source change. | Verify owner model calls, summarization and data ports after switching to delegation, while direct Bedrock and broader bootstrap remain denied. |
| Kubernetes RBAC and admission | The ScaledJob specifies `adp-agent`; the repository source here does not establish the cluster's effective RBAC, admission configuration or absence of other bindings. No fixed-template sandbox admission is deployed by this ScaledJob. | Verify effective bindings and reject service-account substitution, arbitrary images, mounts and privilege in the authorized target. |
| Network and metadata | This ScaledJob has no sandbox egress policy. Repository configuration alone does not establish the installed network enforcement or actual routes to the gateway, Kubernetes API, node metadata, cloud endpoints or the Internet. Web tools and subprocesses require a scoped transport or explicit denial. | From the sandbox probe DNS, gateway, node metadata, Kubernetes API and denied routes; verify an enforcing network implementation rather than just a policy object. |
| Terminal reconciliation | `chat_turn_finalization.py` finalizes admitted turns only after trusted exit/removal evidence and lease fencing. It atomically records the accepted outcome, execution revocation and owner-delivery intent. Success preserves saved-message provenance; committed cancellation wins; a missing result becomes interrupted. Failed/interrupted chat outcomes remain distinct while using the existing execution-completed authority state. Accounting is observed, not rewritten: unresolved operations retain their reservations and automatic replay is never permitted. Manual retry requires interrupted state, unchanged history and no recorded model operations. Lost responses recover the same receipt. The supervisor invokes the route after confirmed removal and validates run/session/lease/pod binding. The route now publishes terminal delivery through the response FIFO, but does not release processing locks or acknowledge input notifications. | Complete lifecycle reconciliation and queued cancellation without an admitted sandbox. Validate complete owner turns, worker loss and recovery on the authorized target under #6937. |
| Lifecycle | The legacy ScaledJob could scale if applied independently, but the checked-in deploy script refuses installation and the retired entrypoint cannot receive messages. Installed old images/roles remain unqualified. The source supervisor waits for UID-bound sandbox exit before UID-preconditioned deletion. `chat_teardown.py` requires durable positive exit evidence and a matching Kubernetes pod-not-found response for removal proof. Removal polling is bounded to 60 seconds; accepted or lost deletion responses alone do not prove removal. Transactions bind observations to the dispatch, launch and unchanged execution; retries preserve them after deletion or a lost write response. Cleanup needs no live model grant and grants no model/data capability. The supervisor now validates the gateway completion receipt before acknowledging the current input notification. Trusted recovery restores the original admitted binding on redelivery without creating another sandbox or replaying inference. Unverified exit leaves the existing bounded-lifetime pod available for later observation rather than deleting it without evidence. The Job remains suspended and uninstalled. Recovery before durable admission, queued cancellation and pending-message scheduling remain unfinished. `activeDeadlineSeconds: 900` alone cannot prove process or file cleanup. | Complete the remaining lifecycle paths and verify the integrated supervisor on a running target. Use A1/A2/B1 file/process canaries, kill a worker before result commit, reconcile the lease and reject the stale generation. |

For registered webchat turns, finalization also commits a pending owner-delivery
record through `chat_terminal_delivery.py` in the same transaction as the terminal
outcome. The recipient comes from protected ingress routing, not sandbox input.
The record freezes the saved, scrubbed assistant reply for success, or bounded
trusted messages for failure, cancellation and interruption. Its digest is bound
to the execution; retries validate the original record instead of rebuilding or
overwriting it, including after a lost commit response. Unresolved model accounting
and the prohibition on automatic replay remain intact. After that storage-only
step, the supervisor finalization route invokes `chat_terminal_publication.py` to
send the frozen record through the existing owner-bound response FIFO. It records
queue acceptance separately and retries uncertain handoffs with the same delivery
ID. The response Lambda uses a dedicated terminal path: an owner-, incarnation-
and task-fenced write persists history and its duplicate-detection receipt together.
WebSocket failure leaves delivery retryable without appending history again. A
lost send receipt may repeat a frame, so the browser hooks also deduplicate the
delivery ID, reassemble terminal chunks and distinguish all four outcomes.
The response event-source mapping enables partial-batch failure reporting; the
handler stops after a failed record so FIFO successors remain retryable in order.
Queue acceptance is not browser receipt or full turn completion. The terminal
consumer still bypasses legacy lock/re-enqueue bookkeeping. A separate
supervisor-only completion route now verifies protected final state, confirmed
removal and the owner's sent-delivery receipt. It atomically releases only the
matching processing lock and records durable input-acknowledgement readiness,
fenced by owner, session incarnation, task and sealed execution lease. Archived
delivery digests in the owner session make late duplicate responses harmless
even after a newer task replaces the current receipt; they expire with the session.
`gateway-chat-completion-access.tf` grants only the trusted gateway `UpdateItem`
and `ConditionCheckItem` on the exact owner-session table in source. The condition
check binds buffered cancellation to the unchanged owner/session and retained input
in the same transaction as its cancellation intent. No credential or model permission
is added, and this grant has not been installed. The supervisor now checks the receipt's
run, session incarnation, task, attempt, lease, pod and terminal-delivery digest
before deleting the current SQS receipt. Lost completion replies are retried within
a bounded wait; lost acknowledgements can be retried after redelivery. The trusted
`/internal/v1/agent/chat/data/resume` route checks the registered supervisor,
tenant/persona, protected dispatch digest and original launch before returning its
binding; it issues no model or data capability and is denied on the sandbox listener.
Buffered messages without a matching protected registration keep the processing
lock and input acknowledgement held. For an owner-cancelled turn with no workload
binding or admission, trusted resume now atomically records a queued-phase cancelled
outcome and immutable owner-delivery intent. It verifies the original cancellation
digest, attempt, credential epoch and protected dispatch; concurrent binding or
admission prevents that write. Publication uses the existing owner-fenced response
path, preserves the same delivery ID on retries and never executes a model call.
The outcome explicitly retains `cleanup_required`: absence of admission is not proof
that Kubernetes creation never succeeded. For an unreserved turn only, resume now
separately verifies owner delivery and atomically checks the unchanged cancellation,
dispatch and absence of creation reservation, pod binding, admission and teardown
records. Because the supervisor must reserve creation first, the cancelled execution
and absent reservation establish that no creation permission was issued and prevent
a delayed reservation from succeeding. A distinct queued-completion receipt records
that fence without inventing pod-removal evidence or changing the original outcome.
It releases only the matching lock, or hands off an eligible successor through the
existing durable queue path. The supervisor validates the completion before
acknowledging the current notification, without creating or deleting a pod. Owner
delivery or handoff uncertainty still withholds acknowledgement. Issued reservations
remain excluded from this path, even when no pod can be found.
For an unreserved, registered buffered input whose protected retention deadline has elapsed,
trusted resume records an interrupted, retryable queued outcome instead of trying
to start a sandbox. It verifies the registered input metadata and original dispatch,
atomically excludes creation and admission, and does not renew the input deadline,
execution authority or model allowance. The retained payload need not still exist.
An authenticated cancellation takes precedence over expiry; a racing cancellation
prevents an expiry outcome from committing. The same owner-delivery and
creation-fenced completion path then advances buffered work in order, including
consecutive registered expired inputs, without releasing a newer task's lock.
The supervisor accepts only the matching cancelled/non-retryable or
interrupted/retryable receipt before acknowledging the current notification.
These source checks use storage, queue and supervisor fixtures, not running images.
Owners can also cancel a registered buffered input before it becomes the active
turn. The existing cancellation route checks the authenticated human, current
membership, protected registration and exact retained envelope and user-message
marker. An atomic check of the owner, session incarnation, active-task and complete
buffered-thread snapshot prevents a concurrent handoff or owner change from accepting
stale cancellation. This writes only cancellation intent, without releasing the
active turn's lock. Handoff verifies the original cancellation digest and absence
of creation/admission before scheduling that input for terminal-only reconciliation.
It preserves each cancelled input's owner outcome in order, including consecutive
cancellations, instead of dropping them to reach later work. Existing owner delivery,
completion and current-receipt acknowledgement checks still apply. Lost replies retry
the same intent and handoff; they do not create a sandbox or replay model work.
When the original pod binding was committed but admission was not, trusted resume
can now record a separate pre-admission exit receipt. It requires Kubernetes to
positively identify the terminated container, original UID, full run hash, approved
image and restricted pod template. That write also fences late admission; it cannot
race a session lease into existence. The supervisor deletes only the recorded UID,
then the gateway records removal only after a genuine Kubernetes absence response.
Lost replies reuse these durable observations. Neither receipt creates a lease,
issues model authority, releases the processing lock nor acknowledges the input.
Once removal is confirmed, the gateway atomically records an interrupted,
retryable outcome (or an authenticated cancellation) and its immutable owner-delivery
intent alongside the removal evidence. No admission or model usage is invented.
The existing response transport publishes that outcome with duplicate-safe delivery;
lost storage or queue replies retry the same outcome without replaying the turn.
An expired or replaced owner session cannot receive it. Delivery alone still cannot
release the processing lock, advance buffered work or authorize input acknowledgement.
For a cleaned pre-admission turn, the supervisor-only completion path now verifies
the owner's durable delivery receipt and rechecks the original dispatch, pod binding,
creation reservation, removal evidence and absence of admission in the completion
transaction. It releases only the matching processing lock, or transfers it to an
eligible registered successor through the existing durable queue handoff. The
distinct pre-admission completion receipt creates no lease or model authority;
input acknowledgement becomes ready only after any successor's queue receipt is
durably recorded. The supervisor validates that receipt before acknowledging the
current input notification. Lost completion or acknowledgement replies replay the
same receipt without releasing a later turn's lock or altering its lease.
Before creating a pod, the supervisor now obtains a single-use reservation from the
trusted gateway. The protected reservation fixes the run's pod name and approved
image; binding and admission cannot substitute either. Only the successful initial
reservation response permits a creation request. A retry observes the reservation
instead of obtaining another permission or choosing another name. If Kubernetes
created the pod but its reply was lost before binding, the gateway can discover the
reserved, positively terminated pod and atomically record its original UID and exit
evidence while fencing late admission. The existing UID-bound cleanup then applies.
Neither a missing pod nor a lost reservation reply establishes that creation never
happened. Such reservations remain held, including when submission itself may never
have occurred. Recovery of those never-observed reservations remains unfinished. Running-sandbox
and deployed qualification remain outstanding; no runtime activation is performed
by this source change.

With scoped model policy enabled, trusted ingest now retains each busy-thread
follow-up's authenticated dispatch envelope in the owner session before calling
protected root registration. The retained request includes the original task,
message, attachments, principal and session incarnation. Conditional writes bind
it to the active processing task and append a user-message marker atomically, so
completion cannot silently abandon it. Registration retries reuse the retained
request; duplicate WebSocket deliveries carrying the same transport message ID
retain one buffered turn. Storage or registration failure does not acknowledge
the follow-up or clear the active task. The registered canonical envelope is
retained for scheduling, not sent to the input queue by the buffering path.
After confirmed terminal delivery, completion selects the oldest buffered input,
verifies its exact bytes against protected registration and atomically transfers
the processing lock while recording a durable handoff. Publication retries use
the same task ID and bytes; acknowledgement waits for a saved queue receipt.
Other pending messages remain buffered, and compact scheduling receipts prevent
a duplicate input from being registered again after promotion. A lost registration
response is recoverable when its protected root exists. If registration never
committed, the supervisor-only completion path invokes trusted ingest using the
existing exact-function Lambda permission and `BG_INTAKE_INGEST_FUNCTION` setting.
The invocation supplies only session, owner, incarnation, active-task and pending-ID
fences; ingest reloads the retained request rather than accepting replacement input.
This direct-invocation route is not selectable through WebSocket, HTTP or Slack
payloads. It registers without sending notifications, publishing input or releasing
the processing lock. Completion retries re-read protected registration; Lambda
success alone never authorizes handoff. Delayed source-authenticated registration
past the normal 15-minute arrival window additionally requires exact retained input,
its original arrival marker and a live matching session. It preserves the original
two-hour authority expiry and input-retention deadline; expired input remains refused.
For retained inputs whose registration never committed, completion can instead
record a terminal-only interruption once the original two-hour authority lifetime
has elapsed. It does not infer a historical input-retention deadline from current
configuration or extend authority to retry registration. The gateway verifies the
retained owner, session incarnation, original message marker and arrival time, then
atomically creates immutable dispatch, routing, terminal and owner-delivery records
with the predecessor's completion and processing-lock transfer. Existing execution,
grant, dispatch, routing, delivery, creation or admission records prevent this
transaction from overwriting competing work. A delayed registration transaction
cannot overwrite the terminal records or create a grant afterward. No authority,
grant, lease, executable input or pod-removal evidence is created; a partially
written original authority row is left unchanged. The interrupted outcome remains
retryable by the owner, never automatically replayable, with not-used accounting.
Existing supervisor recovery publishes it and requires owner delivery before
completion, successor advancement and input acknowledgement. Lost transaction or
queue replies reuse the same outcome and handoff. HTTP, transactional storage and
queue fixtures verify this path, not running-image or deployed isolation.
`gateway-chat-pending-access.tf` adds source-only `sqs:SendMessage` permission to
the exact chat FIFO for the trusted gateway, not the supervisor or sandbox.
`ADP_CHAT_INPUT_QUEUE_URL` is explicitly configured from the corresponding queue
parameter; an absent or malformed destination prevents promotion. The existing
deployment guard covers this policy and gateway workflow. No grant is installed
and no runtime is activated by these source changes.

The one-shot supervisor is packaged with a dedicated service account and an
offline Job renderer. Supply reviewed same-account role, FIFO queue, pinned
supervisor and sandbox image digests, region and gateway HTTPS origin through
`SUPERVISOR_ROLE_ARN`, `CHAT_FIFO_URL`, `SUPERVISOR_IMAGE`,
`SANDBOX_IMAGE_DIGEST`, `AWS_REGION` and `GATEWAY_HTTPS_ORIGIN`, then run
`node modules/agent-factory/agent/k8s/render-chat-supervisor-job.mjs` to print
a **suspended** Job manifest. Rendering makes no cluster or AWS changes; do not
unsuspend it until delegated model/result transport, RBAC, admission and
network enforcement have been installed and verified by the authorized owner.

Scoped gateway chat-data bootstrap now verifies only the role-free
`adp-chat-sandbox` service account and a separately pinned
`ADP_CHAT_SANDBOX_IMAGE_DIGESTS` allowlist. The current gateway manifest does
not set that allowlist and the worker still runs under `adp-agent`: absent a
reviewed sandbox image, supervisor launch and delegated model/response paths,
bootstrap refuses the new identity. The checked-in legacy worker refuses startup
and its source role has no direct model or owner-store authority; installed
identities and old images still need independent qualification.
Do not add an AWS role to the sandbox to make a failed bootstrap pass.

The restricted-image build includes Bash, curl, the AWS CLI and the SDK clients
used by `agent/scripts/chat-sandbox-probe.sh`, including STS and direct Bedrock.
Their presence is deliberate: denial must come from the isolation boundary, not
from missing tools. This adds no identity, mounted credential or network grant.
From `modules/agent-factory`, build the image locally with
`docker build -f agent/Dockerfile.sandbox -t chat-sandbox-probe .` in an authorized
Docker runtime. Resolve its immutable local ID using
`docker image inspect --format '{{.Id}}' chat-sandbox-probe`, then pass that ID to
`bash agent/scripts/chat-sandbox-probe-image.test.sh IMAGE_ID` (`IMAGE_ID` is the
resolved ID, not a mutable tag). The smoke check disables networking and host
mounts, checks the non-root user and installed tools, and verifies refusal without
a sandbox token. Missing runtime/image prerequisites exit with status 3, not a
pass. This packaging check does not run the isolation probes or establish IAM,
admission, network-policy enforcement, credential fallback denial or A1/A2/B1
file/process isolation; those require a running restricted sandbox and remain
outstanding separately from deployed qualification.

The live checks belong to #6937's authorized qualification target. Record the
deployed image/source, effective IAM and RBAC, test commands, synthetic fixtures,
actual outcomes, old-pod drain and issued-credential expiry in restricted
evidence. Do not treat this inventory or repository tests as those observations;
do not deploy or expose chat to obtain them without separate authorization.

## Implementation review and qualification boundary

The implementation review of PR #7042 exercised the sandbox image built from
source `4f1c627dfd73c8fa2d92d256f99378d11f75cb1f` locally. With no host credentials or
network, all 17 shipped credential/environment/process/file/SDK/CLI denial probes
passed. A1 was destroyed after writing a canary and starting a background process;
fresh A2 and B1 containers inherited neither its scratch files nor process namespace.
The workload-token mount contained only an inert fixture. These checks establish
local container behavior, not EKS IAM, admission, CNI, real delegated inference or
installed lifecycle enforcement. The review receipt and logs are retained with the PR.

Admitted-worker interruption and stale-lease handling satisfy the original
worker-loss implementation criterion. The additional never-observed creation
reservation edge described above remains fail-closed and requires intervention;
automatic recovery of that case is not claimed. It remains an explicit limitation
for #6937 QUAL03 fault qualification before release. The four deployed task-board
checks stay blocked under #6937. Merging this source does not activate the
suspended supervisor, expose chat, or establish full security/release acceptance.
