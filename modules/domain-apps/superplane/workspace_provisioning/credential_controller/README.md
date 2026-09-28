# Installed membership credential controller

This is a separate privileged credential reconciler, not a new workspace or paid
operations engine. Initial bootstrap continues to use its original live operation,
claim, ComponentJournal and member credential journal. This controller processes
only active or removed memberships; it never renews from a completed bootstrap
lease and never touches another operation's reserved membership.

`python -m workspace_provisioning.credential_controller` loads immutable installed
authority documents from its mounted configuration and compares them with enabled
`cluster_credential_authorities` rows. Each document pins the domain org/cluster,
controller IAM RoleId, issuer/projector IAM roles and RoleIds, immutable EKS access
entries, target/control-plane ARN/endpoint/CA, admission policy/binding UIDs, API
audience, and distinct reader/mutator Secret names and namespace/Secret UIDs.

The controller requires the installed IRSA identity. It privately assumes the two
actor roles, verifies actual RoleId/STS principal and EKS access-entry identity,
and signs EKS bearer tokens through the SDK. No ambient kubeconfig, local AWS
profile, static cluster-admin token or tenant-provided callback is accepted by the
production entrypoint. Bootstrap may reuse `compose_transports` with separately
delivered source/management sessions and its own live operation verifier.

Every provider request checks the current installed lease/fence and current
membership/generation/namespace UID. Issuance and publication additionally verify
the installed admission policy. A 60-second PostgreSQL lease fences competing
controllers; takeover invalidates old attempts. Each member is independently
reconciled and paginated so a failing member does not indefinitely starve peers.

Renewal starts five minutes before a 15-minute token expires. It uses the existing
`membership_credentials` reserve/delegated/issued/projection-intent/projected/active
states. `membership_credential_components` records only typed SA/Role/RoleBinding
intents and provider UIDs. A replacement SA is journalled before TokenRequest;
token bytes remain in memory. Projection intent retains the public content digest
before the Secret CAS request. A lost response is recovered from the exact
projected bytes. A lost, unpublished token is revoked before another revision.

The shared projected-credential verifier reads the real Secret, checks the exact
binding/CA and authenticated ServiceAccount identity, exercises namespaced reads
and privilege denials, and acknowledges the revision before activation. The
manager/executor then independently compare their projected file against the
new active journal identity. This source path does not prove kubelet projection
latency, live workload success or cloud networking without an authorized live
demonstration.

Activation fences the prior revision. Cleanup verifies its exact Secret receipt
or a different explicitly journalled revision before preserving another key,
revokes its ServiceAccount by UID, and removes only its owned RBAC. Unresolved
revocation blocks further rotation for that scope. Namespace/cluster deletion and
cluster-wide authority removal remain separately authorized lifecycle work.

## Installation prerequisites and database separation

The installer has optional `credential_controller` configuration with `role_arn`,
`database_secret`, `registration_database_secret`, and `authorities` (each an
`authority_id` plus the document validated by `registry.Authority`). Version 1
supports renewal; version 2 adds the pinned shared-bootstrap dependencies described
in [shared-runtime-wiring.md](../shared-runtime-wiring.md).
See `installation/credential_controller.py` for the closed schema. The two Secret
projections must contain `domain-dsn` and `ca-pem`; they must be distinct.

Before registration, an authorized platform installation must have created the
exact IAM roles/trust, EKS access entries and cluster-owned RBAC, admission policy
and binding, and empty or existing reader/mutator projection Secrets. Registration
checks their real identities; it does not manufacture role ARNs or assert that
cloud prerequisites exist. Issuer RBAC owns namespace delegation and TokenRequest;
projector RBAC grants access only to the configured Secrets. Observer/executor
ServiceAccounts receive none of those privileges. Actor AWS permissions include
STS assume, exact self IAM GetRole, exact EKS DescribeCluster/DescribeAccessEntry.

The installer renders a dedicated ServiceAccount, immutable ConfigMap, restricted
Deployment and egress policy. A separate one-shot `--register` Job runs after
schema/bootstrap and before controller rollout using only the registration DSN.
It verifies live configured prerequisites and inserts an immutable enabled registry
row; it refuses replacement or re-enabling an existing disabled installation.
Its command/document is included in the reviewable installer manifests. This code
has not itself been installed or used against a live cloud during implementation.

The renewal DB role needs SELECT on cluster/workspace/membership/authority rows,
UPDATE only `holder,fence_token,lease_expires_at` on the authority registry, and
SELECT/INSERT/UPDATE on credential and credential-component journals. Grant only
`UPDATE(updated_at)` on cluster_memberships to permit PostgreSQL `FOR UPDATE`
locking; no identity/lifecycle column updates are needed. It must not own these
tables or inherit broader rights. Runtime explicitly refuses registry insert/delete
or immutable-column update privileges and broad cluster/workspace/membership DML.
The installer registration DSN has registry insert rights and is never mounted in
the long-lived Deployment. Database role creation/Secret provisioning is an
explicit deployment prerequisite, not a hidden runtime privilege escalation.

Local validation for this implementation is static only. Provider doubles and
PostgreSQL takeover/revocation tests run in remote CI. Live installation, rotation,
connectivity and teardown verification remain separately authorized work.
