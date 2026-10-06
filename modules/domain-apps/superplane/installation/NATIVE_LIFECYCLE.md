# Native lifecycle installation

The native integration depends on the native runtime (#7047), separate domain and
Harness store composition, the protected binding owner (#7065), maintained runtime
preparation (#7059), and current ADP identity composition (#7051). Source validation
and installer receipts do not establish that a live demo has passed.

Select `paid_worker.mode: native-lifecycle`. The existing `operation_schema` names
the shared Harness schema and must differ from `database.schema` (the domain).
`database_secret` projects the distinct `domain-dsn` and `execution-dsn` plus `ca.pem`.
The worker checks the exact release's domain migration head and checks the Harness
version only on the execution pool.

Native lifecycle mounts only the combined domain/execution database Secret, the
reviewed policy, workload identity token and persistent state. It does not mount
workload credentials or a SkyPilot provider token and does not run the controller
sidecar. `workspace_credentials_secret` and `provider_secret` are rejected in this
mode; native-controller retains both requirements. Gateway prepared/executable
binding proof also refuses legacy credential mounts or a controller sidecar in a
native lifecycle template. No empty placeholder Secret is needed.

Native mode additionally requires exact existing `lifecycle_policy_configmap`,
`lifecycle_state_claim`, and `lifecycle_policy_sha256` (SHA-256 of the UTF-8 bytes of
`data.lifecycle.json`). The state PVC must be Bound. The installer reads policy
content and only Secret UID/resourceVersion metadata. It never records DSNs.

`api_adapters.dispatcher.operation_database_secret_ref` names the separate API
shared-service database Secret and key `dsn` (prepared name:
`superplane-operation-api-db`). It must not reuse the worker Secret. The installer
projects `SUPERPLANE_OPERATION_DATABASE_URL`, `SUPERPLANE_OPERATION_DB_SCHEMA`, and
`CURRENT_IDENTITY_ENFORCED=true`. It renders a deployment-owned expected worker
binding file using the canonical role-ARN registry UUID derivation.

For an organization without a selected existing workspace credential connection,
use `api_adapters.verification: {mode: organization-bootstrap}`. This private
control requires a current human ADP membership and organization administrator
grant, rechecks both after probing adapters, and explicitly reports that credential
metadata has not been verified. It grants no workspace authority. Real creation
continues through its normal credential and approval checks.

The maintained installer installs a paused zero-capacity ScaledJob and deny-all
worker network policy. Live protected Gateway proofs have three explicit states:

- `quiescent`: reads shared execution state, without claiming an installed worker;
- `prepared`: checks the paused installed worker and shared quiescence;
- `executable`: checks the enabled installed worker.

The installer pins actual Kubernetes identities, policy bytes, Secret metadata,
queue, image, roles, registry IDs and schemas. It repeats prepared verification,
enables only owned worker resources, then verifies executable proof before enabling
API admission. The shared binding digest must remain unchanged across this
transition. An activation failure restores disabled API admission and pauses the
worker; a failed restore remains a failure, never a successful receipt.

The API producer's exact IAM recipe adds only `binding-proof`, `current-identity`, and
`current-identity/readiness` to its existing three POST routes. A saved-plan upgrade may replace that known old
inline recipe while preserving the role ARN, immutable RoleId, trust and all other
authority. Arbitrary policy changes and role replacement remain refused.

Controller-only mode retains its inert projection and unavailable activation gate.

For upgrades, shared quiescence cannot depend on the old API image (and API absence
is not evidence of an empty shared store). After the reviewed producer IAM plan is
applied and verified, but before any worker object changes, the installer runs a
bounded verification Job from the new immutable API image. It uses the exact
producer service account, has no database or provider Secret mounts, and calls only
the protected non-consuming quiescence route. Its network policy admits only the
reviewed Gateway and STS endpoints plus cluster DNS. The response must be fresh,
and the producer identity is checked again afterward. Failure prevents worker
foundations or rollout. This Job and its minimal Namespace/service-account/network
prerequisites use the same owner apply, wait and receipt paths as installation Jobs.

## Owned policy and durable state preparation

The API and native worker mount the same exact lifecycle ConfigMap. API creation
uses `SUPERPLANE_LIFECYCLE_CONFIG_FILE`; the deployment template pins the reviewed
raw policy digest so a changed reviewed configuration causes a rollout.

To create dependencies through the installer, add `lifecycle_foundations` to the
reviewed environment, with exactly:

- `policy_json`: the complete UTF-8 JSON string, at most 65536 bytes, with
  `version: 1` and one `tenants` entry keyed by the unchanged domain `org_id`.
  The entry must satisfy `LifecyclePolicy`, preserve `adp_org_id`, management
  account and cluster, and match `paid_worker.lifecycle_policy_sha256` byte for
  byte. The operation runtime must exceed 900 seconds and fit within the worker
  deadline; 3600 seconds leaves room for EKS provisioning and renewable sessions.
- `state: {storage_class: <reviewed-existing-retained-EBS-class>, capacity: <1..999Gi>}`.
  The class must already use `Retain` and either the standard EBS CSI driver or
  EKS Auto Mode EBS driver. `Immediate` and `WaitForFirstConsumer` are supported.
  This path uses `ReadWriteOnce` and requires `paid_worker.max_replica_count: 1`.

Offline output includes the immutable policy ConfigMap and PVC for review. During
execution, after protected shared-store quiescence and namespace foundations, the
installer checks every existing dependency before its first write. It uses
create-only operations and refuses foreign ownership, different policy bytes,
changed storage requests, clone sources, missing recorded resources or replaced
UIDs. It never updates or adopts an existing policy/PVC.

A bounded non-root Job from the reviewed paid-worker image mounts only the state
claim, has no service-account token or credential mounts, and uses the worker's
node selector. It writes, fsyncs and removes one random probe file. This triggers
`WaitForFirstConsumer` binding and tests the actual worker UID/GID. The owner then
checks a Bound PVC, the exact PV claim UID/namespace/name, retention and live
object identities. Prepared/executable verification repeats those checks. The
policy and state are retained outside workload-cleanup inventory; normal cleanup
cannot delete them. A changed policy needs a separately reviewed replacement
procedure rather than silently rewriting the fixed immutable ConfigMap.

The installer does not invent a lifecycle policy, organization ID, workspace
Terraform backend, actor roles, bootstrap credential reference, management network
inputs or image digests. It does not create a StorageClass or authorize a provider
connection. Those reviewed owner inputs must exist before execution. Omitting
`lifecycle_foundations` preserves the existing externally provisioned dependency
path and does not assert that such dependencies exist.

### Explicit public HTTPS transport

Native lifecycle may opt into `paid_worker.egress` with exactly:

```yaml
mode: public-https
database:
  cidr: <observed-private-RDS-address>/32
  port: 5432
```

This grants public IPv4 TCP443, including AWS APIs and Terraform provider download
hosts. It does not claim isolation by destination domain. RFC1918, shared-address,
loopback, link-local, metadata, multicast and reserved address ranges are excluded;
no IPv6 egress is added. The existing verified Gateway Service namespace/pod
selectors and service/target ports, exact private database /32 on TCP5432, and
cluster DNS selectors are the only additional peers. The controller mode cannot
select this recipe. Existing fixed-peer mode remains unchanged.

The exact activation egress rules are rendered in the paused NetworkPolicy's
`adp.aws-e.io/activation-egress` annotation, covered by the saved environment and
source review. Preparation and activation both run an authority-free, bounded
network Job using the paid worker image, node selector and exact rule set. The Job
mounts no Secret, AWS identity or Kubernetes token. It verifies public service TLS,
actual RDS DNS correspondence to the reviewed private peer, Gateway reachability,
and database TCP reachability. A changed RDS address or a private DNS override of
a public service refuses; it requires an updated reviewed recipe. This transport
probe does not replace the existing live network-policy enforcement, worker
identity, database, current human grant or protected binding checks.
