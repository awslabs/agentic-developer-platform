# Concrete queue verification producer

`src.l5_devops_queue_fixture.build_queue_fixture` is a runnable Gateway-bound
producer and independent oracle for one synthetic invoice queue worker. It is
not yet installed in the service runtime or exposed as a public action. It uses
real owner, Product source, configuration, binding, grant and persisted run
readers; its provider boundaries use GitHub, Lambda, SQS, EC2, IAM, S3 and KMS.
It does not start a native task or infer a native pass.

The source artifact is the exact `index.mjs` blob read from the selected GitHub
commit through the existing Product installation. The deterministic one-file ZIP
must match Lambda's `CodeSha256` for a numeric published version. Labels and tags
are not source provenance. Mutable aliases and `$LATEST` are rejected.

## Prepare only the dedicated queue fixture

The source fixture in `fixtures/queue/index.mjs` has no provider/network side
effects. The independent test invokes the Lambda version directly with one
valid batch and one mixed batch containing invalid JSON, an invalid amount and
an extra field. This establishes deployed handler behavior and partial-batch
responses; it does not establish SQS delivery, billing/payment behavior, or
native agent execution.

Build from the fixture repository's selected immutable commit, with the Gateway
environment active:

```sh
python -m src.l5_devops_queue_prepare --repository /path/to/fixture-repository --commit FULL_SOURCE_SHA --output /path/to/new-worker.zip
```

The script only builds by default. The optional `--publish-version
--aws-profile PROFILE --function-arn EXACT_UNQUALIFIED_FUNCTION_ARN` publishes
an already deployed matching function using both `CodeSha256` and `RevisionId`
preconditions. It does not deploy code, create infrastructure, grant access,
invoke a function or remove resources. Publishing has no provider idempotency
token: after an unknown response, inspect the exact function's versions before
retrying. Keep the returned numeric version ARN.

Before qualification, separately prepare these concrete resources in the
dedicated fixture account:

- Lambda runtime `nodejs22.x`, handler `index.handler`, timeout 30 seconds,
  memory 128 MiB, reserved concurrency 2, no layers, environment variables or
  failure destination. Deploy the deterministic ZIP and publish a version.
- One selected VPC subnet and security group. The function must use only that
  subnet/group, with no IPv6 allowance. The group must have no ingress or egress
  rules. Remove Lambda's transient VPC-network setup permissions from its
  execution role before qualification; retain the already provisioned network.
- An execution role trusted only by `lambda.amazonaws.com`, with exactly the
  `scoped-queue-read` inline policy returned by
  `l5_devops_queue_observer.worker_policy(queue_arn)`, no managed policies or
  permissions boundary. Its explicit denies bound actions and resources even
  if another resource policy is added. No IAM administration permission is used.
- An encrypted SQS queue with visibility 180 seconds and no queue resource
  policy. The selected event-source mapping must point to the exact published
  version, be enabled, use batch size 5 and `ReportBatchItemFailures`.
- A private versioned evidence bucket and an asymmetric KMS signing key
  supporting `RSASSA_PSS_SHA_256`. Install the producer in that account with
  only the exact selected resource permissions. Observer calls are
  `GetFunction`, `GetFunctionConcurrency`, `GetEventSourceMapping`,
  `GetQueueAttributes`, `DescribeSecurityGroups`, `GetRole`, `ListRolePolicies`,
  `ListAttachedRolePolicies`, `GetRolePolicy` and `GetCallerIdentity`.
  Oracle authority adds `InvokeFunction` on only the numeric version.
  Evidence authority adds S3 Get/Put on its exact fixture prefix, DeleteObjectVersion
  only for its run payload objects, and KMS Sign/Verify on its exact key.

The producer cannot fix missing infrastructure or permissions. These are
operator preparation steps, outside native execution and never automatic.
The [fixture preparation tool](fixture-deployment-preparation.md) builds the
private queue deployment bundle and exact archive source bundle without
deploying either. Queue bootstrap, sealing and activation remain separate
reviewed stages, and producer/owner authority is not installed by that tool.

## Bind to actual owner admission and execution

`L5_DEVOPS_QUEUE_FIXTURE` is a server-installed JSON object with these exact
fields: `account_id`, `region`, `function_version_arn`, `queue_url`, `queue_arn`,
`mapping_id`, `execution_role_arn`, `vpc_id`, `subnet_id`, `security_group_id`,
`bucket`, `prefix`, `signing_key_arn`, `producer`, `source_ref`, `deployment_ref`,
`target_ref`, `credential_ref`. The prefix is
`l5/devops/queue-fixture/<64 hex characters>/`. The deployment, target and secret
reference IDs must be the numeric Lambda version ARN, security group ID and
execution role ARN respectively. Scope/source selection must match the current
owner's Product configuration. Replacing the installed selection invalidates
existing reader instances.

Within the existing authenticated Gateway request/session:

```python
producer = build_queue_fixture(backend, product_id, project_id, session=scoped_session)
# backend.admission_operation and admission_key are the exact submit/retry intent.
# producer.options carries the fixed scenario artifact and installed refs.
evidence_ref = await producer.publish_admission()
```

The return value is the exact version/hash/key reference accepted by a
request-bound `VerificationReader(session=scoped_session, reference=evidence_ref)`.
There is no process-global reference or ambient credential fallback.
The envelope binds owner scope, current
configuration, admission key and observation freshness. Preserve this private
reference; do not publish it in a task or document. The regular admission
validator still checks the actual run deadline against its two-hour expiry.

After a real native submission has persisted its execution ID:

```python
reader = build_queue_fixture(backend, product_id, project_id, session=scoped_oracle_session, run_id=actual_run_id)
independent_results = await reader.run_oracle()
```

`run_oracle` loads the actual SQL Run and its immutable admission/manifest;
caller-supplied run labels cannot establish execution identity. It rechecks
current owner, source, configuration, binding generation and retained owner
grant. It invokes at most twice, records request IDs, executed version, function
errors and raw returned payloads in signed versioned evidence, and never
accepts an independent result as a native result. Current cancellation and
deadline prevent new invocation.

The same reader implements the existing release adapter methods
`read_deployment`, `read_scenarios`, `read_results`, `read_fixture_data` and
`delete_fixture_data`. An immutable receipt owns the exact run/execution payload
VersionId. Cleanup deletes only that version and independently rereads absence;
it never deletes the function, stack, queue, unrelated data or retained audit
evidence. A lost delete acknowledgement recovers without a second delete.
Replay reuses results. If invocation was started but no response was durably
recorded, `queue_oracle_outcome_unknown` prevents reinvocation of that run.

The separate unmounted `QueueVerificationReader` admission factory resolves
the selected owner's current child work grant and parent consent before issuing
observation and evidence sessions through the existing broker assume-role
contract. It checks exact configuration/binding/source/credential scope before
and after issuance and at evidence boundaries. These sessions grant neither
invocation nor deletion; they cannot be reused for the oracle or cleanup.
Submit and retry bind a new reader to the authoritative admission, and receipt
replay returns without regenerating evidence.

## Native results remain an explicit remaining qualification

The installed `devops-agent` API model (2026-01-01) supports this read path:

1. `GetBacklogTask(agentSpaceId, taskId)` returns the task's `executionId` and
   task status. `ListExecutions(agentSpaceId, taskId)` scopes the execution list
   to that task.
2. `ListAssets(agentSpaceId, ...)` returns asset IDs, type, metadata and version.
   It has no modeled task/execution filter. A safe task-to-asset association
   therefore still needs qualification from the actual native journal/content;
   the producer does not guess metadata semantics or pick the latest asset.
   `ListJournalRecords(agentSpaceId, executionId)` is execution-scoped, but
   its `content` and asset `metadata` are unmodeled documents. Neither supplies
   a modeled task-to-asset relationship or scenario result contract.
3. Read only a qualified asset ID/version via `GetAsset`, `GetAssetFile(path)`
   or `GetAssetContent`. File content and metadata are modeled, but no individual
   release-scenario result schema is specified by the API model.

No selected completed native fixture task is available yet. Inspecting an
actual task's journal and immutable asset contents must establish that linkage
and result schema before installing `native_results_reader`. Native COMPLETED
alone is insufficient, and the release adapter remains fail-closed.

This implements deployment/code/privilege qualification, the independent
positive/negative oracle, replay and exact fixture-data cleanup. Full DA12
native scenario acceptance and DA19 live portability remain incomplete until
the prepared fixture, current source selection, owner grant and actual native
results are exercised together. The archive fixture has no executable target
and is outside this producer's scope.
