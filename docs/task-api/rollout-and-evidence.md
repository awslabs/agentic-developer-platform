# Task API rollout and bounded evidence

This is a task-specific checklist supplement to the canonical
[agent deployment guide](../adp-platform-deployment/deploy-with-agent.md).
Follow that guide and [operator prerequisites](operator-prerequisites.md) for
account confirmation and exact deployment commands. These tools never enable
traffic, resume AI-DLC, submit tasks, or mark unexecuted live criteria PASS.
The engine can remain paused while authorized operators qualify the Task API.

## Readiness inventory

Capture the actual account/region, queue ARN, gateway/backend source and image
versions, ingress/recovery Lambda published versions and configuration hashes,
worker resolved image digest, all four canonical Task API flags, canonical
principal/persona active versioned policy and token expiry. Capture commands and exit codes alongside
outputs. Bind every file by SHA-256. Do not capture credentials or unredacted
Lambda environment values. Read-only inventory commands include:

```sh
aws sts get-caller-identity
kubectl -n adp-agents get pods,jobs,deployments,scaledjobs -o json
aws lambda list-event-source-mappings --event-source-arn "$TASK_QUEUE_ARN"
aws lambda get-function --function-name "$TASK_INGRESS_FUNCTION" --query '{Arn:Configuration.FunctionArn,Version:Configuration.Version,CodeSha256:Configuration.CodeSha256,Image:Code.ResolvedImageUri}'
aws sqs get-queue-attributes --queue-url "$TASK_QUEUE_URL" --attribute-names QueueArn ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
```

Inventory running pods AND retained Job templates/backoff/deadlines. Updating
ScaledJob alone does not replace an already-created Job: it may restart an old
consumer. Before admission, drain old jobs or prove their bound active workers
cannot acquire again and their controllers cannot restart an old image. Include
standing Deployment consumers, all future ScaledJob templates and Lambda queue
mappings. Never infer absence of consumers solely from zero queue depth. Bind
capability evidence to the exact resolved digest, including embedded investigator,
chunk transport, task envelope routing and the task-only projected workload token.
Use the existing approved `agent-scaledjob-sa`, audience `adp-agent-bootstrap`,
and `ADP_WORKLOAD_TOKEN_FILE=/var/run/adp-workload/token`.
`ADP_AGENT_AUTHORITY_ENABLED` stays false/absent; the task-only worker flag
authorizes this workload verifier path.

Copy `readiness-inventory.example.json` into an owned evidence directory and
replace every unset value with observed facts and hashed capture paths. The
checker requires complete consumer enumeration, compatible image identities,
old-job disposition, projected token binding and immutable component versions.
An older bound worker is excluded from new consumers only with hashed evidence
of completed initial acquisition, a single acquisition code path, and disabled
restart; terminal Jobs require terminal-state evidence. Enumerate empty Deployment
consumer sets explicitly. Require every desired gateway replica ready on the
verified image before operator review.
It emits BLOCKED for missing evidence; a successful inventory check means only
READY FOR OPERATOR REVIEW, never live qualification or automatic admission.

```sh
python3 scripts/task-api/check-readiness.py evidence/inventory.json --output evidence/readiness.json
```

## Ordered rollout

1. Keep submit off. Establish tables/index/KMS/artifact access, OAuth scopes,
   workload verifier, canonical principal policy and actual budgets. Preserve
   read access to previously accepted tasks.
2. Build and qualify the exact worker image and deploy task-capable queue consumers
   first. Inspect running pods and all restartable old Job templates as above.
   Apply the task-only projected token and narrow verifier image/SA binding.
   Set `agent_authority_prepared=true` to provision gateway TokenReview/pod-read
   RBAC while keeping `agent_authority_enabled=false`. Terraform blocks task
   workers without this prepared (or explicitly enabled) verifier prerequisite.
3. Deploy gateway/ingress/recovery versions; verify their source/image identities,
   configuration and required access. Keep `ADP_RUN_TASKS_ENABLED=false`; Task API
   uses `ADP_TASK_API_READ_ENABLED`, `ADP_TASK_API_ADMISSION_ENABLED`,
   `ADP_TASK_API_WORKER_ENABLED`, `ADP_TASK_API_RECOVERY_ENABLED` independently.
4. Verify capability with read-only inventory plus built-image/component evidence.
   Run legacy regression baseline using component-owned V1–V3 fixtures. Store
   source/fixture/image hashes and measured results. Admission stays off until
   all consumers and authority are ready and the operator authorizes bounded work.
5. Enable reads, worker and recovery for qualified components, then submit last.
   Use an active versioned policy for the owned principal, expiring OAuth tokens,
   enforced per-task deadlines and the protected qualification budget. TaskServicePolicy
   has no policy expiry field; do not invent one in readiness evidence. This example evidence
   slice caps traffic at three tasks and total spend atUSD3 (stricter than the
   accepted USD25 qualification ceiling). Stop on exhaustion or unknown provider
   outcomes; never increase limits to obtain PASS.
6. Run the external client against actual HTTPS endpoints. Capture submission
   and identical replay task IDs, live progress timestamps, reconnect cursor,
   follow-up receipt/consumption, cancel intent/stop evidence, terminal snapshot,
   artifact digest and real usage attribution. Use separate scenarios when the
   terminal outcome makes operations incompatible. Cite exact V4/V5 criterion
   IDs from the existing evaluation manifest; omitted criteria remain NOT RUN.

## Rollback and cleanup

Stop new submission first; preserve reads/events/artifacts. Keep compatible workers
and recovery available to drain already accepted tasks. For unwanted owned tasks,
submit ordinary cancel commands and wait for confirmed stop. Quarantine uncertain
work through existing recovery/authority controls; preserve unknown model holds
and records. Do not purge the shared queue, replace tables, delete task artifacts,
flip the global legacy guard or redeploy incompatible consumers while task work
remains deliverable. Shared-queue legacy regression must remain healthy.

After accepted work has terminal/stop evidence and no old task envelopes can be
consumed, worker/recovery can be disabled or reverted deliberately. Record queue
counts, owned task states, retained jobs and policy disposition. Revoke the
qualification principal policy and remove only explicitly owned temporary
pods/policies; normal task30-day/tombstone90-day retention handles records.
Unclaimed uploads expire24 hours. Preserve failure evidence and resource IDs.

## Evidence packaging

`package-evidence.py` validates versioned manifests and hashes owned files. It
cannot establish truth from labels: an independent evaluator still checks actual
commands/logs and all required criteria. PASS requires an actual live lane,
nonzero executed count, command, zero exit status and evidence artifacts. Mocked
client tests are component evidence only. Missing live execution is NOT RUN or
BLOCKED. Do not copy fixture/example values into a live result.

```sh
python3 scripts/task-api/package-evidence.py evidence/run.json --output evidence/hashed-run.json
python3 -m pytest -q tests/task-api/test_external_client.py
```

The manifest records schema version1.0, environment/account, immutable source/image,
evaluator, timestamps, owned resources, bounds and cleanup. Each `results` item
has criterion ID, lane, outcome, exact command, exit status, executed count and
artifact-relative paths. Files must remain inside the evidence directory and
under16MiB each. Package failed runs too; retain defect links and rerun changed
criteria under a new source/fixture revision. V4/V5 acceptance remains open until
all their required real-service checks pass independently.

Start from `live-evidence.example.json`, which enumerates every V4/V5 criterion
as NOT RUN. Use `examples/task-api/observe.py` to capture timestamped SSE and
snapshot for already accepted owned tasks. Bind component-owned fixtures from
`evaluation-manifest.json`; use actual legacy issue/comment fixture controls for
coexistence only under the existing operator authorization. Neither observer nor
packager creates legacy work or changes the engine pause state.
