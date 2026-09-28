# Terraform-managed worker security rollout

Worker security configuration belongs to the reusable Terraform modules. Every
environment uses the same resources; account, region, cluster, queue, table and
API identifiers come from its provider/configuration. Runtime compatibility and
live acceptance in #5195 remain release requirements.

Preparation defaults on. **Preparation does not activate restricted worker
launches.** It creates the bounded role, service account, registry entry, signing
material, TokenReview/RBAC, gateway dispatch/task-source IAM and webhook admission
IAM while preserving legacy authentication and launch selection. This replaces
the dev-specific preparation overrides in #5176.

## Ownership and deployment

| Configuration | Terraform owner |
| --- | --- |
| Worker identity, signing, registry and producer IAM | `modules/agent-factory/webhook-ingress/infra` |
| Gateway authority environment and rollout | Webhook ConfigMap `adp-worker-authority-config` and `terraform_data.worker_gateway_rollout` |
| Tick authority IAM and runtime configuration | `modules/gateway/infra`, consuming webhook-published resource identifiers |
| Legacy IAM attachment retirement and customer-source boundary | Webhook Terraform |
| Platform-owned EKS administrator entries | `platform/infra`, for each owning cluster state |

The gateway consumes the Terraform ConfigMap after its base ConfigMap. Terraform
restarts it when authority configuration changes and checks readiness before
updating worker launches. Activation refuses a gateway template that cannot read
this map and all four signing/service secret references, or whose referenced keys
are absent or empty. Signing secrets remain in the gateway namespace.

`deploy-all.sh` follows gateway-before-webhook ordering, then performs a second
Terraform pass for the gateway-owned tick authority policy once webhook resource
discovery is available, only when gateway is also in the resolved deployment
scope. It refreshes credentials and retains gateway upgrade context. This
bootstrap pass targets that policy; full gateway
plans retain its ownership. Preparation does not enable tick dispatch or its
legacy command bridge.

CLI deploy, CI plan, CI apply and imports share `scripts/terraform-webhook.sh`.
It loads module defaults followed by optional overlays:
`environments/<environment>/modules/webhook-ingress.tfvars` and `.tfvars.json`.
The wrapper requires an explicit environment and region. The auto-loaded module
file contains only portable defaults; dev settings (including concurrency and
adversarial infrastructure) live in the dev overlay. A fresh non-dev environment
keeps reserved concurrency and does not require the dev adversarial secret.
Upgrade precedence is module defaults, selected environment overlays, observed
live context, then explicitly requested inputs such as a release image.
Changes to the dev overlay obey the same deployment hold as infrastructure.
Other environment overlays do not trigger a default dev deployment. The backend
key is `<environment>/modules/webhook-ingress/terraform.tfstate`;
`ADP_STATE_REGION` supports centralized state storage. The default cluster is
`adp-<environment>-eks-cluster`; `eks_cluster_name` supports custom names. A new
environment never inherits a fallback dev cluster or state key.

Existing deployments must also retain the configuration discovered by
`platform/scripts/upgrade-state.py prepare` and the gateway ALB inputs used by
the canonical update path. A plan made only with portable defaults can propose
removing existing optional integrations, administrator entries or immutable ECR
encryption settings. Review the saved plan with the actual retained inputs.
The snapshot also retains a configured tick's queue, command-bridge table, KMS
key and existing tenant-secret scope. It matches selectors to webhook-owned
state and refuses missing, ambiguous or foreign-target evidence; an unwired tick
does not gain these integrations or authority activation through discovery.
Acknowledgement permissions are retained independently of queue/table wiring.
A policy without that optional grant leaves it disabled. An existing single
tenant-secret grant is copied exactly, including a narrower tenant/path pattern;
conditions, multiple scopes and other shapes that the Terraform input cannot
represent are refused instead of being dropped or broadened.
The legacy GitLab placeholder secret-version address uses a `removed` block with
`destroy=false`: setup/rotation owns its values, and migration must not remove an
existing version or alter its stages. The secret itself remains managed. This
requires Terraform 1.7 or later.

The saved-plan gate recognizes that exact GitLab version `forget` only when
the same target-account secret remains managed and unchanged in the plan.
Credential updates, deletion, replacement, other forgotten credentials, or a
missing/changed secret remain blocked. The ScaledJob carrier must use
create-before-delete in the same cluster and namespace; delete-first replacement
remains blocked. These checks recognize the migration shape, not release approval
or live preservation evidence.

## Run services (#5195 / #5513)

Protected workers can receive messages, change visibility, delete messages and
read attributes on their environment's agent input queue. These are fixed IAM
permissions shared by workers using the role, not permissions changed per task.
Other queues and queue sending/administration remain denied. This grants access
to the shared input queue; IAM does not restrict a consumer to an assigned message.
The protected runtime still uses gateway task delivery for its run binding and
durable acknowledgement records; these IAM grants do not change that protocol.
All direct S3 operations, including access to the run archive bucket, remain denied. The gateway receives
receive/change-visibility/delete on the task queue and PutObject on the archive
bucket's `runs/*` prefix. It selects the run/attempt path after authentication.
KEDA uses its own operator identity to poll queue depth. Shared Beads/Dolt S3
synchronization is unavailable to protected workers; they use GitHub task tracking.

The gateway's namespace Role also permits `get` on `batch/jobs` so bootstrap can
verify the controller Job's absolute lifetime for pause budgeting (#5205). Workers
receive no Kubernetes Job permission. A compatible gateway and worker must retain
this bound across bootstrap and credential renewal; missing lifecycle evidence
leaves pause unavailable. Applying RBAC alone does not establish live control
acceptance.

`ADP_RUN_TASKS_ENABLED` follows the authority activation flag; preparation leaves
it false and omits `ADP_RUN_TASK_QUEUE_URL`. Both archive bucket settings select
this environment's run-log bucket. `agent_door_service_url` selects a gateway-only
Door origin; its default is the existing in-cluster service. The Door key reuses
the gateway's existing `bedrockgateway-secrets/internal-api-key` reference.

Activation reads **AWSCURRENT of the existing webhook marker secret** and projects
it to `agent-run-services/marker-signing-key` in the gateway namespace. It never
generates a replacement key. Empty, short and public placeholder values refuse
the plan; preparation neither reads nor projects that key. The value is sensitive
Terraform state, so the protected backend remains part of the trusted platform.
Marker version changes trigger gateway rollout. The rollout helper verifies all
four gateway signing/service secret references and the names of their existing,
nonempty keys before restarting an active configuration. Kubernetes formats each
Secret response as nonempty key names only; no secret values reach the shell or
logs. Preparation does not check or require these keys. Worker pods receive none
of these shared signing/service keys.

The gateway, worker and Door source from #5513 must be deployed and verified
before asserting runtime readiness. A source merge or mocked-provider plan does
not satisfy these canaries or release the existing hold. The rollout still needs
the full IAM/Kubernetes inventory, including other clusters and legacy mappings.

## Stages

Use reviewed environment configuration and Terraform plans. Follow the
[canonical deployment guide](../adp-platform-deployment/deploy-with-agent.md)
for target confirmation and verification. No direct IAM attachment, SSM flag or
`kubectl set env` command is required.

| Stage | Webhook settings | Effect |
| --- | --- | --- |
| Prepare | `agent_authority_prepared=true`, `agent_authority_enabled=false` | Retain the protected infrastructure and existing launch selection/grants. |
| Quiesce | `agent_worker_admission_paused=true` | Stop new KEDA Jobs and preserve running Jobs. Reconcile legacy queued work and finish active work before changing authentication. |
| Configure | `agent_authority_enabled=true`, `agent_authority_runtime_ready=true`, `agent_authority_legacy_workers_drained=true`; keep admissions paused | Configure protected workers, gateway and producer. `agent_image` must pin a real digest in `agent_authority_worker_image_digests`. |
| Isolate source | Apply platform retirement after drain; verify all platform Kubernetes grants are removed | Preserve the customer-trusted role ARN without platform Kubernetes access. |
| Retire administrator authority | `agent_task_source_isolation_confirmed=true`, `agent_legacy_worker_admin_retired=true` | Detach managed policies including out-of-band AdministratorAccess; install customer-STS-only inline permissions/boundary and enable gateway source trust. |
| Admit | Complete compatible gateway/tick/worker/provider acceptance, then `agent_worker_admission_paused=false` | New Jobs use the restricted service account. Release refuses missing source isolation or legacy IAM retirement. |

The matching gateway setting is `orchestration_agent_authority_enabled=true` in
its environment Terraform configuration. Separate
`orchestration_agent_authority_prepared=true` prepares IAM without dispatch.
Keep admissions paused while updating and verifying these deployment units.

Each producer derives `AGENT_AUTHORITY_ENABLED` and `ADP_WORK_CLAIMS_ENABLED`
from its same authority rollout input. The tick, gateway and webhook therefore
enable ownership tracking together with protected authority; there is no separate
work-claim switch to remember during setup. Keep the gateway environment's
`orchestration_agent_authority_enabled` aligned with webhook
`agent_authority_enabled`. The standard gateway release verification reads a
running gateway pod and the Lambda configuration and refuses completion if these
controls disagree within either runtime or across them. It does not activate
authority or resume flows to resolve a mismatch.

For platform-owned EKS entries, set `agent_legacy_worker_admin_retired=true` and
`agent_authority_legacy_workers_drained=true` in each owning platform state. The
protected worker is always excluded from the administrator set, including when
listed as deployer or extra administrator. Retirement also excludes the legacy
worker. This manages that state's entries; it does not discover/delete another
state's grants, arbitrary aws-auth mappings or implicit-creator access. Inventory
those paths and import unmanaged grants into their owning Terraform configuration
before reviewed retirement. Fresh environments have no historical administrator
attachment to adopt.

The retired source keeps its ARN, preserving customer trusts. Its boundary allows
only customer-account STS assumption, tags/source identity and caller identity;
platform-account chaining and other AWS actions are denied. Customer ExternalIds
remain caller-controlled. Gateway session policies still select accepted targets.

Readiness, drain and isolation settings are **release assertions**, not automated
proof. They must not bypass missing marker/Door mediation, supervisor isolation,
compatible images, queue reconciliation or live credential/logging canaries.
Terraform rejects incomplete configuration; IAM cannot make unsupported runtime
features work. Keep preparation enabled through rollback, pause admissions, and
reconcile in-flight work without restoring administrator access or downgrading
protected identities to shared-secret authentication.

The #5197 webhook deployment hold remains pending #5195 release evidence. This
change does not apply infrastructure, remove the hold or certify a live migration.
Other infrastructure workflows have separate deployment triggers; review them
before merging a rollout change.

## Verification

Native Terraform tests cover preparation, region/environment scoping, incomplete
activation, mutable-image refusal, protected identity selection and legacy IAM
retirement. Provider-free expression tests cover dev/staging/prod tick wiring and
EKS principal exclusion. Shell tests cover environment overlays, backend isolation
and gateway rollout refusal using command doubles. Live acceptance is separate.
