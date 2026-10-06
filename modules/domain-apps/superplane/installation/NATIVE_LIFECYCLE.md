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
