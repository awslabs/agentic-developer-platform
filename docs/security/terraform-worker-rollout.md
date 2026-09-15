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
this map. Signing secrets remain in the gateway namespace.

`deploy-all.sh` follows gateway-before-webhook ordering, then performs a second
Terraform pass for the gateway-owned tick authority policy once webhook resource
discovery is available. This bootstrap pass targets that policy; full gateway
plans retain its ownership. Preparation does not enable tick dispatch or its
legacy command bridge.

CLI deploy, CI plan, CI apply and imports share `scripts/terraform-webhook.sh`.
It loads module defaults followed by optional overlays:
`environments/<environment>/modules/webhook-ingress.tfvars` and `.tfvars.json`.
Selected environment and region override the legacy default file. The backend
key is `<environment>/modules/webhook-ingress/terraform.tfstate`;
`ADP_STATE_REGION` supports centralized state storage. The default cluster is
`adp-<environment>-eks-cluster`; `eks_cluster_name` supports custom names. A new
environment never inherits a fallback dev cluster or state key.

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
