# S12 credential capability and worker-boundary integration

This integrates #5611/#4702/#4724 with A09 PR #5950, capability PR #6062,
and the existing protected-worker isolation contract. S11 proof-label repair
PR #6081 must land before composite acceptance. No second identity resolver,
credential broker or worker IAM architecture is introduced.

Every user-credential broker operation requires an exact registry capability in
addition to verified IAM transport, canonical run/workload proof, the exact
server-recorded owner, tenant binding and any accepted execution policy. Missing
or wrong capability refuses with 403 before credential effects. Caller headers,
body fields and mutable environment values never grant a capability. Inventory
coverage fails if a new user-credential route omits a capability decision.

## Producer and grant inventory

Only the dedicated protected worker registry seed gains the four new scopes.
Its existing raw-read/materialize grants remain. These are transport capabilities,
not permission to change the run's user, tenant, selected secret or accepted plan.

| Capability | Protected worker producer | Exact operation |
|---|---|---|
| `credential:list` | `adp_cred/client.py:list_credentials` | GET user-credentials metadata |
| `credential:proxy` | `adp_cred/client.py:proxy_http` | POST proxy-request, with existing host restrictions |
| `credential:assume-role` | `adp_cred/assume.py:cmd_assume`, `lib/gateway_credential_client.py` | POST credential-assume-role for the bound user |
| `credential:task-session` | `adp_cred/task_credentials.py:cmd_task_credentials` | POST worker-task-credentials for the AWS SDK credential process |
| `credential:raw-read` | `adp_cred/client.py`, gateway credential client | Existing raw-read operation |
| `credential:materialize` | `adp_cred/client.py:materialize` | Existing file delivery operation |

`adp_cred/client.py:_sigv4_request` carries canonical run/workload proof through
the protected transport. Listing and proxying include the current invocation;
assume and task-session producers do the same. The task-session producer refuses
outside protected mode. Existing tests preserve legitimate list/proxy/file/raw
and customer-role/task-session delivery while rejecting forged scope and ownership.

Legacy scaledjob registry seeds remain exactly raw-read-only. No grants are added
to the deploy runner, shared-secret callers, the older Node chat client or
`platform/scripts/assume-customer-creds.py` merely to preserve former access.
Those producers must satisfy A09 canonical proof and receive an explicit reviewed
grant before using a protected operation. GitHub installation/shared-review token
minting and domain/vault operation delivery retain their separate resource-bound
and `credential:operation-delivery` contracts.

## Remove the old switch

The owner enforcement setting, rendered environment field, deploy-all/workflow
SSM lookup and obsolete flip workflow are removed. Historical SSM values cannot
restore shadow authorization. The tenant-config diagnostic reports
`credential_binding_mode=authenticated_run`; an SSM-only fallback cannot attest
which authorization code the serving gateway runs. Existing parameters are not
modified or deleted by this source change.

## #4724 effective-boundary disposition

The protected `agent_authority_boundary` explicitly denies `secretsmanager:*`
on `*`, denies access to authority records, denies other encryption keys and
direct KMS, and permits only the constrained DynamoDB encryption service path.
The old worker principal, after retirement, carries the task-source policy as
both identity policy and boundary: it denies every non-STS action and denies
assuming any role in the platform account. This denies tenant application keys,
internal control-plane keys, arbitrary secret listing and authority writes
regardless of whether an individual secret uses an AWS-managed key or CMK.

Thus the older suggested CMK reprovisioning architecture is not necessary to close
the actual cross-tenant worker-read path under the selected protected/retired
contract. This is a boundary-based remediation, not a claim that all historical
secrets were re-encrypted. No provisioning, key rotation or legacy-secret migration
is included. If an environment still selects the legacy unretired role, it does
not satisfy this disposition and must complete existing #5195 rollout acceptance.

Progress/control reports use the gateway's caller-scoped endpoints in protected
mode and cannot fall back to direct DynamoDB. The old authority-off client remains
in source for staged legacy compatibility; it is not available as an IAM bypass
under the protected role. Do not describe that code as deleted.

## Controlled order

1. Merge S11 #6081 and A09 #5950 and satisfy their producer/schema prerequisites.
2. Reconcile the dedicated protected-worker registry item with the six inventoried
   scopes; do not widen legacy seeds. Review the exact Terraform plan.
3. Deploy the gateway with mandatory capability enforcement, then validate current
   legitimate protected producers and denied missing/wrong scopes. Existing
   gateway/worker/role retirement rollout receipts remain authoritative.
4. Keep the legacy principal retired and platform Kubernetes-isolated while issued
   task-source sessions exist. Do not roll back by restoring broad worker access.

Source checks do not establish live rollout. S12's required controlled ordering is
documented here; live acceptance must be represented separately and truthfully.
