# Shared ECR publication and SkyPilot adoption — #6120

Status: source repair; live mutability convergence and Terraform adoption remain open.

Read-only inventory on2026-09-25 in account000000000101/us-east-1 is retained in
`runs/2026-09-21/evidence/6120-ecr-readonly-20260925.json` relative to this directory.
The four workload repositories (`adp-gateway`, `adp-agent-runtime`, `adp-chat-agent`,
`adp-agent-gateway`) are MUTABLE/AES256. `adp-skill-registry` is MUTABLE/**KMS**,
using key `f380852a-edef-4037-ab60-c009f68ee3a7`; the original PR's AES256 override
for that repository was incorrect and has been removed. `adp-superplane-skypilot`
is already IMMUTABLE/AES256. This is a repository-attribute observation, not a
Terraform state inventory or an apply receipt.

The shared publisher now requires a full source SHA, refuses mutable aliases and
mismatched IMAGE_TAG values, and reuses a registry-resolved existing artifact on
retry. A missing tag permits a build; access errors and missing repositories stop
the build. Gateway/worker selfchecks still gate publication. No post-build phase
can push after a failed build. The publisher verifies the resulting OCI digest.
Repositories must already exist through their Terraform owner.

The webhook deploy path uses an archived source commit, resolves the selected
worker artifact by digest before Lambda upload/state import/apply, and passes that
digest explicitly to Terraform. Terraform rejects empty/latest/placeholder image
values. The chat deploy path similarly resolves an explicit SHA or digest before
cluster mutation and uses that digest for both ScaledJob and prepull DaemonSet.
Gateway workflow consumers still select full SHA tags; live IMMUTABLE convergence
is required before treating those tags as an immutable deployment contract.

Validation: executable publisher/resolver tests cover all four build contexts,
immutable retries, registry failures, failed build/selfcheck/push, invalid or missing
digests, and mutable/mismatched selector rejection. No live image publication or
workload rollout was performed by this source repair.

## Live convergence gates

Inventory the actual backend/workspace and repository state addresses before a
serialized owner apply. The four AES256 overrides retain observed encryption;
verify the managed KMS key for the skill registry remains the observed key.
Review a saved Terraform plan's JSON actions for every repository: **no delete or
replacement**. Encryption migration remains#5003. Do not assume a source change
or repository attribute inventory proves applied Terraform state.

Confirm all active publishers use the repaired entry point before changing live
mutability. Inventory manually invoked/legacy publishers as well; a mutable-tag
producer cannot be declared compatible simply because it is outside a workflow.
A failed build leaves the existing consumers unchanged. Rollback selects a
previous verified digest; enabling PUBLISH_LATEST is not a valid rollback under
IMMUTABLE.

The local gateway, agent-gateway and chat-agent paths in `deploy-all.sh`, and the
standalone `modules/agent-factory/scripts/deploy-gateway.sh`, use
`platform/scripts/publish-local-image.sh`. Local Docker builds retain cache use
but take their inputs from `git archive` of the full selected commit; commit any
intended source edits before publishing. The current shared publisher stages the
gateway contracts and runs the same self-checks as CodeBuild. An existing source
tag reuses its verified digest; a registry error, failed build or failed
self-check stops publication. These entry points resolve the full-SHA tag to an
ECR digest before promoting a workload. Chat and agent-gateway use separate
repositories and the same full source SHA, without a `-chat` suffix.

For a standalone agent-gateway rollback, use `--skip-image-build` with an explicit
`AGENT_IMAGE` digest (or a full-SHA `AGENT_IMAGE_TAG`). The script verifies that
image before Terraform or Kubernetes writes. Shared repositories must already
exist under their Terraform owner; local publishers do not create replacements.

## SkyPilot adoption

Preserve the published image digest:
`sha256:f8d893cfabadff1946f7ccb8e2c0ba002322d1bfb0ee416cf6ce039f45de55ec`.

The declared address is
`aws_ecr_repository.superplane["adp-superplane-skypilot"]` in the Superplane
control-plane module. Verify its absence/presence in the correct state first;
do not import a resource already managed there or in another state. Resolve paths
from the repository root, not three parents above the control-plane directory:

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT/modules/domain-apps/superplane/infra/control-plane"
terraform init -input=false \
  -backend-config="$REPO_ROOT/environments/dev/modules/superplane-backend.tfvars" \
  -backend-config=bucket=adp-terraform-state-000000000101
terraform state list
# Only after serialized ownership verification and a state backup:
terraform import \
  -var-file="$REPO_ROOT/environments/dev/modules/superplane.tfvars" \
  'aws_ecr_repository.superplane["adp-superplane-skypilot"]' adp-superplane-skypilot
terraform plan -input=false \
  -var-file="$REPO_ROOT/environments/dev/modules/superplane.tfvars" -out=skypilot-adoption.tfplan
terraform show -json skypilot-adoption.tfplan > skypilot-adoption.plan.json
```

Check command exit status and inspect `resource_changes[].change.actions` in the
JSON, including exact repository and lifecycle-policy addresses. Human-plan grep
output is not an acceptance test. Reject any repository delete/replacement or
unrelated change. SkyPilot source retention now expires only untagged images; its tagged S03
release is excluded from count-based expiration. Verify the live lifecycle
policy matches this source before adoption acceptance. Other repositories retain
their existing count30 policy.
Re-read the preserved digest from ECR after adoption. These commands are a runbook,
not evidence that import, retention protection or apply has happened.

No gbrain repository change is included;#6102 owns that release path. Pod manifest
security contexts remain with#6105. Both live ownership and publication compatibility
must be demonstrated before closing#6120.
