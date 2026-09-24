# Governed batch API

The maintained controller now accepts an explicitly installed batch profile through
the workspace API. It creates a Kubernetes Job on the profile's workspace EKS
capacity using the existing paid outbox, worker identity, RPC, UID inventory and
allocation finalizer. Batch and serving share the workspace GPU quota; their read
and stop routes cannot exchange resource identities.

This first producer supports GPU Jobs with a fixed immutable image and invocation.
Source and input data must be included in that image, or otherwise accessible to
the installed invocation without introducing an ambient credential. There is no
arbitrary source checkout, data mount, environment injection, or caller-chosen
namespace. Workload logs, result delivery, progress observations, cancellation
before the original operation settles, and a batch browser form remain separate
work. These routes alone do not complete the researcher lifecycle.

## Policy

Use the existing version 1 controller policy document and installation command.
For a batch profile, keep the same explicit target, credential reference, capacity,
runtime and cost fields as a serving profile. Set `workload.kind` to `batch`,
`workload.port`, `workload.auth_secret` and `serving_auth_contract` to `null`, and
`model_options` to `{}`. The image must include its `@sha256:` digest. Supply a
nonempty `command`, `args`, `gpu_count` (1–8), `cpu` and `memory`. Runtime is bounded
to 1–86,400 seconds and cost must have a positive finite ceiling. The pinned image
preflight validates the same producer used by the API. Configuration acceptance
does not establish live readiness.

Migration `032_batch_workload_kind` preserves existing records as serving and
adds the closed workload discriminator. Downgrade refuses while any batch record
exists, including retained or deleted records: an older image cannot safely
interpret those identities. Preserve recovery records when planning rollback.

## Requests

All paths below are relative to the authenticated ADP `/api/superplane/v1` proxy.
Every request is scoped to the workspace's current server-held grants. Discovery
uses READ; mutations also require SPEND at the route and PROVISION at admission.

1. `GET /workspaces/{workspace_id}/batch-profiles` returns authorized profiles and
   current submission/teardown-review availability. Keep the exact `batch_options`
   from the selected profile. The serving catalog excludes batch profiles.
2. `POST /workspaces/{workspace_id}/batch-jobs/preview` with an original UUID
   `operation_id`, `profile_id`, `name` and those `batch_options` returns the exact
   approval request, revision, job identity and allocation. No intent or paid work
   is created by a preview.
3. Submit the returned `approval_request` through `/operation-approvals` and obtain
   the required human decision. POST the original batch body to
   `/workspaces/{workspace_id}/batch-jobs`, adding `approval_id` and `plan_revision`.
   The server recomputes and checks the exact plan before reserving quota.
4. Keep the original operation UUID for retries. A lost response can be recovered
   through `/operations/by-idempotency/{operation_id}`; accepted work survives
   removal of its current profile. Never allocate a second UUID to retry it.
5. `GET /workspaces/{workspace_id}/batch-jobs` lists the latest 100 recorded Jobs,
   including cleanup tombstones, with an explicit truncation flag. Use
   `/batch-jobs/{job_id}` for a saved identity outside that window.
6. After the original operation settles, POST an original stop UUID as
   `operation_id` to `/batch-jobs/{job_id}/teardown-preview`. Obtain approval for
   that returned request, then DELETE `/batch-jobs/{job_id}` with the stop UUID,
   approval and revision. Teardown retains the original allocation and targets the
   original Job UID; it reserves zero additional resource units and cost.

`operation_state` describes the admitted workflow. `execution_outcome` remains
unknown in this projection until workload observation is composed. Observed cost
is `null`, never zero. Cleanup remains unconfirmed until the trusted finalizer
verifies complete absence of the original owned workload and provider resources.
A remaining volume keeps the quota reservation. Requesting stop or receiving a
successful operation response cannot assert cleanup.

Remote Domain CI runs `test_batch_deployment_postgres.py`, the maintained plan,
installer and full PostgreSQL migration checks. The linked batch test runs actual
API admission, outbox, task registry, worker RPC and finalizer with only
provider/network transports simulated. Live batch acceptance remains with the
separately authorized workload evaluation.
