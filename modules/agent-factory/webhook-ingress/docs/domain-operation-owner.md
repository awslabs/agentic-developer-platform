# Native Superplane operation authority

The webhook Terraform module owns protected registry registration, the Gateway's
domain binding, its exact queue/secret permissions, and read-only Kubernetes
proof permissions. `domain_operation_bindings` defaults to `{}`: ordinary
deployments receive no domain binding or additional authority.

This is one bounded recipe for a native Superplane installation. It does not
create the application queue or roles, prepare databases, install workers, or
assert that a worker is ready. Use the canonical
[agent deployment guide](../../../../docs/adp-platform-deployment/deploy-with-agent.md)
for deployment authorization and account selection.

## Prepare the reviewed inputs

Complete app-owned runtime preparation and separate shared operation-database
preparation first. Collect the selected account, region, environment and operator
role; the app-owned standard queue URL; both exact IAM role ARNs and immutable
`RoleId` values; and two new, stable registry UUIDs. The maintained role names are
`adp-<environment>-superplane-api-producer` and
`adp-<environment>-superplane-domain-worker`.

Keep a private Terraform variable file containing the typed
`domain_operation_bindings.superplane` object defined in
[`domain-operation-bindings.tf`](../infra/domain-operation-bindings.tf). Supply
secret **ARNs**, never secret values. Its `binding` has three separate purposes:

| Fields | Purpose |
| --- | --- |
| `database_secret_id`, `database_schema` | Gateway access to the shared Harness operation store; for example, schema `superplane_operations` |
| `domain_database_secret_id`, `domain_database_schema` | Separate domain-port connection; for example, schema `superplane` |
| `observation_credential_secret_id` | Credential for the reviewed observation endpoint |

All three secret ARNs must differ. Shared and domain schemas must differ. The
worker's database credential is a separate installer input, never a Gateway
binding credential. If these Secrets Manager secrets use customer-managed keys,
provide their exact `secret_kms_key_arns`; decrypt permission remains constrained
to those secret encryption contexts.

Pin the actual paid-worker image digest, namespace, organization IDs, repository
and observation endpoint. The only accepted worker service account and ScaledJob
are `superplane-paid-worker`, with container `paid-worker`. Placeholder zero
digests are refused. The deployed Gateway must support the separate domain-port
fields and `worker_scaled_job` before this binding is enabled (#5535).

## Plan and apply through the existing owner

Use the active authorized AWS profile and the **existing webhook backend**. Do
not introduce a second state owner. Python 3 with `boto3` must be available to the
operator process. `scripts/terraform-webhook.sh init` resolves the maintained
backend from the selected environment and state bucket; its `plan` action retains
the existing base and environment variable files. Add the absolute private
domain variable-file path and an absolute saved-plan path to that plan command:

```bash
scripts/terraform-webhook.sh plan -var-file="$DOMAIN_BINDING_FILE" -out="$DOMAIN_BINDING_PLAN"
```

Run this from the webhook-ingress directory, with the same reviewed deployment
environment inputs used by the existing owner. Review the saved plan for only
the intended owner changes. Apply that exact saved plan from `infra` using
`terraform apply "$DOMAIN_BINDING_PLAN"` (the wrapper's `apply` action adds variable
arguments, so it is not the saved-plan entry point). Respect any active deployment
hold and the canonical deployment guide. This document authorizes no live apply.

Apply checks the selected operator and current immutable IAM role identities,
then runs the maintained conditional registration helper. It atomically creates
exactly two protected registry records: producer scope
`domain:operation-producer`, and worker scopes `domain:operation-executor` and
`domain:operation-recovery`. Worker requests still require delegated run identity
and independent pod proof. No model grants are added.

An existing exact pair is a verified no-op. Changed ownership, revoked status,
scope drift, extra fields, partial pairs, another role mapping or a recreated IAM
role is refused. A lost response is reconciled by retrying with the **same**
document and registry IDs. Never generate replacement IDs to bypass a refusal.
The helper's `--check` mode reads identities and records without writing; its
input is the same `ADP_DOMAIN_REGISTRATION_DOCUMENT` Terraform builds.

The existing Terraform-owned `adp-worker-authority-config` publishes
`ADP_DOMAIN_OPERATION_BINDINGS` and triggers the maintained Gateway rollout only
after registration, IAM and Kubernetes proof permissions are applied. Do not use
ad-hoc registry writes or `kubectl set env` as an alternative owner.

## Verify and retain authority history

`domain_operation_binding_revisions` identifies configured content only. Obtain
the separate prepared-binding proof, install the paused worker, and then obtain
the executable proof through the maintained activation path. Verify a real
operation end to end before claiming demo readiness.

Removing the binding removes its Gateway grants and configuration but does not
delete registry records. The Terraform registration resource intentionally has
no destroy-side action. Decommissioning protected authority requires separate
owner reconciliation; automatic recreation, adoption or silent reactivation is
not supported.
