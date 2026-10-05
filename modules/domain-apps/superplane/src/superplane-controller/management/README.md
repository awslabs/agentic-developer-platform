# Controller management

`superplane-controller --management-only` runs an authenticated registration loop
without invoking the legacy workspace/provider controllers. It requires:

- `CONTROL_PLANE_API_URL`: explicit HTTPS origin, or an isolated in-cluster service DNS URL.
- `SUPERPLANE_ORG_ID`: the bootstrapped domain organization UUID.
- `SUPERPLANE_REGISTRY_CREDENTIAL_FILE`: a mounted observation credential whose
  server-side grant contains exactly `controller_management/<org UUID>`.

The API must run with `SUPERPLANE_MANAGEMENT_ONLY=true`, enforced ADP domain
authorization, and its migrated, isolated PostgreSQL schema. This API mode permits
organization administration and registration reads, and blocks workspace execution
and legacy background reconcilers. The full installation capability gate remains
in force outside this explicitly selected mode.

Each registration read acquires/renews a 45-second PostgreSQL observation lease.
The controller checks every 10 seconds. A replacement process waits for the old
lease to expire. No Kubernetes credentials are needed at zero registrations, and
no default/in-cluster workspace configuration is loaded. `/healthz` is process
liveness; `/readyz` requires a successful authenticated registry read and an
unexpired lease. `/statusz` additionally requires the registry credential and
reports per-target observations and `governed_provisioning: false`.

For read-only workspace inspections, supply a directory in
`SUPERPLANE_WORKSPACE_CREDENTIALS_DIR` and the management API server exclusion in
`SUPERPLANE_MANAGEMENT_API_SERVER`. Each file is named `<workspace UUID>.kubeconfig`
and re-read every cycle. Its single context must name the registered EKS ARN and
namespace; its TLS endpoint must match the durable registration. Ambient paths,
exec plugins, authentication providers, proxies, impersonation and insecure TLS
are refused. Dedicated registrations retain their historical namespace/fleet reads
and management-endpoint exclusion.

Shared targets instead require fresh database-selected `membership_credential`
metadata matching the kubeconfig's `superplane.aws-e/membership` extension exactly:
organization/workspace/cluster, generation, namespace UID, ServiceAccount UID,
reader scope, revision and unexpired lifetime. The management endpoint is permitted
only for an explicitly platform-eligible shared cluster in that same registry
binding. A file extension or caller-selected endpoint grants no exception.

Shared inspection asks the Kubernetes API for a SelfSubjectReview and verifies the
actual authenticated ServiceAccount UID and generation/revision-specific username.
The issuer journal links that UID to the observed namespace UID. Live namespaced
Pod, Job and SuperplaneNode reads then prove access. This does not trust decoded
JWT claims, request namespace/fleet privileges or treat omitted observations as
success. Shared heartbeat collection uses namespaced SuperplaneNodes; cluster
readiness and native fleet inventory remain cluster-owned observations.

Before registration, only the original live bootstrap claim may expose a projected
reader revision. Its exact metadata is echoed in the fenced manager observation
and checked again by the API. It cannot receive execution assignments or use the
normal workload observation endpoint. Normal discovery selects only active reader
revisions; lost bindings and revoked/expired credentials fail closed.
Authenticated `/workload-observation` additionally supports bounded original
Job/Deployment status and Pod log windows; see
[the permission and observation contract](../../../executor/WORKLOAD-OBSERVATIONS.md).

This delivers management and target observation, **not governed provisioning**.
Missing or retired registrations remain explicit; no direct SkyPilot/SDK path is
activated. Governed execution uses the separate trusted execution composition. Management
observations do not themselves establish successful authenticated workload use.

Verification: `go test ./...` and `go test -race ./management`. The API's
`test_controller_management.py` and `test_controller_management_postgres.py`
exercise authorization, management-only mutation denial, registration changes,
credential revocation, and concurrent first ownership/restart on real PostgreSQL.
