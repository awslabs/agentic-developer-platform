# Recover an initial request that never dispatched

An initial workspace admission can remain `unknown` after the durable outbox
exhausts delivery attempts. Fix the underlying delivery failure before recovery.
The original workspace and provisioning operation remain the recovery target.

The workspace details UI offers **Review original request for recovery**, then a
fresh human decision and **Resume original provisioning**. The authenticated API
provides the same sequence under `/workspaces/{workspace_id}/dispatch-recovery`:

1. `POST /review` with the observed operation `expected_version` and a stable UUID
   `idempotency_key`. Review the original plan and spend limits in the response.
2. The original selected human approver calls `POST /decision` with
   `reauthorization_id` and `result: allowed-once`. Existing strict or streamlined
   approval policy still applies. An expired review requires a new review key.
3. The original requester calls `POST /resume` with that `reauthorization_id`.
   Observe the original operation and workspace to confirm progress.

The protected Gateway checks current producer, requester, approver, original
budget reservation and installed lifecycle authority. Recovery locks the outbox,
operation and approval consumption, and refuses cancellation, cleanup, claims,
delivery, execution leases, provider intents/reports, allocation resources or
execution audit records. While holding the same operation lock as dispatch, it
consistently reads every possible initial protected dispatch mapping. Any mapping
or inconclusive read refuses recovery. Database absence alone is insufficient.

One recovery cycle is permitted per operation. An immutable receipt retains the
old delivery cycle and transport proof. The outbox claim generation increases,
so a stale acknowledgement cannot settle the new cycle. Repeating the same resume
returns the receipt without another reset. The original approval, expiry,
request bytes, operation/job/attempt identities and reservation remain intact.
A separately persisted fresh decision authorizes execution of that exact plan.

## Coordinated deployment

These endpoints require Harness schema 11 and Superplane domain migration
`046_dispatch_reauthorization`, plus compatible Gateway, tick, API and paid-worker
images. The dedicated API producer role must grant `execute-api:Invoke` on the
exact `POST /internal/v1/controller-execution/recover-undispatched` ARN. Upgrade
the prior closed seven-route policy through the app-owned Terraform saved-plan
review; preserve the role identity, trust and all prior routes. Pause admission dispatch and worker delivery using installation maintenance,
verify no active work, migrate through the schema owners, and roll out qualified
images together. Update the registered worker digest and schema head, renew the
installation provider binding if its digest changed, verify readiness and release
the maintenance lock before resuming the original operation. Do not run a generic
SQL reset or change an expired approval. Downgrades refuse to erase recovery
history once it exists.

## Readiness observations

The UI polls authenticated control-plane health separately from workspace cluster
heartbeats. Workspace list and detail APIs both resolve cluster observations and
cloud account metadata within the authenticated organization. Requested region
is distinct from the registered cluster region.

Installation-managed AWS connections are resolved from the workspace's registered
account, then verified by Gateway against current authority and its persisted
validation report. The UI can refresh the real AWS validation and restores the
reading on re-entry. Neither a healthy control plane nor a provider report claims
that an unregistered or stale workspace is ready.
