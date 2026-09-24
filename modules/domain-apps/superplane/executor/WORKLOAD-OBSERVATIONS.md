# Workload status and log windows

`GET /workspaces/{workspace_id}/batch-jobs/{job_id}/observation` and
`GET /workspaces/{workspace_id}/deployments/{deployment_id}/observation`
require the caller's current workspace READ grant. They return the original Job
or Deployment status and a bounded owned Pod list. Select a returned `pod_uid`
and add `logs=true` to read the workload container's latest 100 lines, at most
16 KiB before redaction. The UI offers these reads from each workload row.

The API derives the canonical target, image, plan digest, original paid operation
and captured resource UID from durable records. The caller cannot supply a
provider endpoint, namespace, resource name or workload UID. The manager uses
its active observation lease and the exact workspace's projected read credential;
GET responses are checked again against current grants, target and lease before
being returned. Replacement resources, missing original UIDs, stale leases,
changed credentials or ambiguous inventories produce unavailable responses.

Job Pods must belong to the original Job. Serving Pods must belong to a ReplicaSet
controlled by the original Deployment. Matching labels alone are insufficient.
The image must match the approved immutable template and Pod. Lists are capped at
32 entries and refuse pagination; four concurrent management reads and a ten-second
manager deadline bound the work. Pod UID is rechecked after reading logs, and the
original workload is rechecked before returning the projection.

## Read credential contract

Provision the existing workspace manager identity independently with the reads it
needs. The installer validates this identity; it does not create or broaden these
permissions. Alongside existing namespace/node/Superplane reads, status needs
`get` on `batch/jobs` and `apps/deployments`, and `list` on `apps/replicasets` and
core `pods`. Log windows additionally need `get` on core `pods` and `pods/log`.
Scope namespaced rules to the registered workspace namespace. The manager requires
a complete SelfSubjectRulesReview and rejects Secret, exec/proxy, wildcard or
mutation authority. Do not reuse the executor or bootstrap supervisor credential.
Missing read rights report unavailable and never trigger a credential fallback.

## Meaning and limits

This is a fresh observation, not a durable result artifact or cleanup/cost
settlement. Job completion does not release model quota. Approved teardown and
verified owned absence remain separate. Serving availability here is Kubernetes
availability, not authenticated endpoint acceptance. API metadata, container
environment, provider errors and arbitrary links are not returned. Log text is
plain text, never HTML; common token, password, key and control patterns are
redacted. Pattern redaction is best effort, so workloads must avoid printing
credentials. No log content is saved in browser receipts or local storage.

Validation runs only in remote code CI: linked PostgreSQL admission/UID/lease
checks, Go fake/TLS Kubernetes transport checks, frontend race/refusal tests and
isolated Chromium interactions. Fixture evidence does not establish live workload
acceptance. No additional infrastructure is provisioned by these read routes.
