# Cyber Task tools service

This package owns the malware-analysis tools for `agent-task-cyber`. It is built
as a separate Lambda image and served by the same API Gateway at
`POST /tools/cyber`. The request's `operation` selects the tool. The generic
`POST /v1/tasks` submission shape is unchanged.

```text
Client OAuth credentials -> generic Task submission -> Task owner/grant
Task worker host -> API Gateway AWS_IAM /tools/cyber -> cyber Lambda
                                                      |-> generic Task authorization
                                                      |-> existing cyber backends
                                                      |-> generic artifact publication
```

`cyber_tools/operations.py` owns sample validation, effect deduplication, job
ownership and cleanup. `backends.py` owns CAPE, triage/static queues, browser and
VirusTotal connections. `handler.py` validates the IAM caller and binds every
request to live platform Task authority. Shared transport/contracts/storage
helpers live in [`modules/tools`](../../../tools/README.md).

The former `/internal/v1/agent/task/cyber` implementation is removed from the
model gateway. The service uses its own DynamoDB table; it cannot mutate platform
Task records. Only a small identity/state/version mirror and domain operation
records live in that table. Full Task input comes from verified authorization.

## Worker request

The trusted Python host signs the request with SigV4 and attaches
`X-Adp-Workload-Token` and `X-Adp-Run-Credential`. Neither header is visible to the
model-facing SDK. Configure the host's `ADP_CYBER_TOOLS_ENDPOINT` to the exact
HTTPS URL ending in `/tools/cyber` (including the API stage). There is no fallback
to the previous gateway route.

The existing bounded IPC body remains:

```json
{
  "schema_version": "1.0",
  "attempt": {
    "run": {
      "task_id": "tsk_<uuid4>",
      "invocation_id": "<uuid4>",
      "generation": 1
    },
    "runtime_attempt_id": "<uuid4>"
  },
  "operation_id": "<uuid4>",
  "operation": "triage",
  "payload": {"sample_s3_uri": "s3://<bucket>/<owned-versioned-sample-key>"}
}
```

Supported operations are `triage`, `static`, `dynamic`, `result`, `url_analysis`,
`enrich` and host-only `cancel_jobs`. Service permissions are the corresponding
`cyber.<operation>` names. For analysis jobs, grant `cyber.result` as well as the
submission operation. Cleanup does not require a standing start permission.

An administrator might give one principal `cyber.triage`, `cyber.static`,
`cyber.result` and `cyber.enrich`, while denying `cyber.dynamic`. Model selection
of an unauthorized tool is refused even when the persona exposes that MCP tool.
Configure the same supported names in gateway `ADP_TASK_PERSONA_TOOLS`:

```json
{
  "agent-task-cyber": [
    "cyber.triage", "cyber.static", "cyber.dynamic", "cyber.result",
    "cyber.url_analysis", "cyber.enrich"
  ]
}
```

Task-level grants are frozen at admission and intersected with live policy on
every authorization. Caller-supplied tenant/principal IDs are never authority.

## Lifecycle and evidence

The service records an operation claim before a potentially irreversible send.
Reusing the same operation identity returns its receipt; ambiguous sends are not
repeated under a fresh model tool-call ID. Sample content is version/digest-pinned.
Backend findings are published through the generic Task artifact API and returned
with verified Task/content-bound artifact IDs.

Cleanup closes the local attempt fence and observes all jobs for the Task,
including previous attempts. Confirmed stopped jobs are persisted so cleanup can
make progress across short Lambda invocations. Remaining-time checks bound each
cleanup pass; least-recently-checked jobs are observed first to avoid starvation.
Unknown jobs remain pending. CAPE stop is not claimed where its API cannot prove
cessation. A Lambda timeout or lost provider response may require operator
reconciliation; it never justifies replaying the submission.

The independent operation table has no automatic active-record TTL. Preserve
records while work or settlement is unresolved; deletion/retention automation is
not introduced by this change. Do not destroy the service/table to disable tool
starts: set `capability_enabled=false`, preserving cleanup access and evidence.

## Build, deploy and verify

See [infrastructure and release instructions](infra/README.md). Provisioning is
default-off. Configure only the required backend resources and grant the Lambda
role the matching access. The platform gateway's role no longer needs these new
Task-cyber backend permissions. Existing backend queue resource policies must
explicitly trust the service role where applicable.

The service's environment uses `CYBER_TOOLS_TABLE`, `CYBER_TOOLS_WORKER_ROLES`,
`ADP_TASK_AUTHORITY_ENDPOINT`, `ADP_TASK_CYBER_ENABLED`, and enabled backend
configuration. The authority endpoint ends in `/internal/v1/agent/task`; its only
client actions are `/tool-authorize` and `/artifact`.

Run standalone domain tests from the repository root:

```bash
PYTHONPATH=modules/tools:modules/domain-apps/cyber/tools \
  python3 -m pytest modules/domain-apps/cyber/tools/tests
```

These tests need pytest/moto and the runtime dependencies listed in the Dockerfile.
The tests and image have no gateway source dependency. Infrastructure tests use
Terraform mock providers. Live IAM registration, API stage publication, backend
connectivity and end-to-end Task execution still require deployment qualification.
