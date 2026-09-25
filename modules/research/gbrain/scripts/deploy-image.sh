#!/usr/bin/env bash
# Provision only build prerequisites before publication; activate ECS by digest.
# Rollback: GBRAIN_IMAGE_DIGEST=sha256:<previous digest> bash scripts/deploy-image.sh
set -euo pipefail
GBRAIN_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GBRAIN_REPO_ROOT="$(cd "$GBRAIN_SCRIPT_DIR/../../../.." && pwd)"
AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"
GBRAIN_DIGEST="${GBRAIN_IMAGE_DIGEST:-}"
# Runtime profiles are private operator inputs, not source archive content.
# Require the reviewed profile for every update/retry/rollback of existing state.
GBRAIN_RUNTIME_ARGS=()
if [ -n "${GBRAIN_RUNTIME_TFVARS:-}" ]; then
  [[ -f "$GBRAIN_RUNTIME_TFVARS" && -r "$GBRAIN_RUNTIME_TFVARS" ]] || { echo 'Runtime profile is not a readable file' >&2; exit 1; }
  GBRAIN_RUNTIME_TFVARS=$(realpath "$GBRAIN_RUNTIME_TFVARS")
  GBRAIN_RUNTIME_HASH=$(sha256sum "$GBRAIN_RUNTIME_TFVARS" | cut -d ' ' -f 1)
  [[ "$GBRAIN_RUNTIME_HASH" == "${GBRAIN_RUNTIME_TFVARS_SHA256:-}" ]] || { echo 'Runtime profile SHA-256 does not match the reviewed hash' >&2; exit 1; }
  python3 - "$GBRAIN_RUNTIME_TFVARS" <<'PROFILE'
import json, sys
with open(sys.argv[1]) as stream:
    profile = json.load(stream)
for key in ("container_command", "container_entrypoint", "container_environment", "service_subnet_ids"):
    if key not in profile:
        raise SystemExit("Runtime profile is missing " + key)
for key in ("container_command", "container_entrypoint"):
    value = profile[key]
    if value is not None and not (isinstance(value, list) and all(isinstance(x, str) for x in value)):
        raise SystemExit("Invalid runtime profile " + key)
env = profile["container_environment"]
if not isinstance(env, list) or not all(isinstance(x, dict) and set(x) == {"name", "value"} and all(isinstance(v, str) for v in x.values()) for x in env):
    raise SystemExit("Invalid runtime profile environment")
if len({x["name"] for x in env}) != len(env):
    raise SystemExit("Duplicate runtime environment names")
subnets = profile["service_subnet_ids"]
if not isinstance(subnets, list) or not subnets or not all(isinstance(x, str) and x for x in subnets):
    raise SystemExit("Invalid runtime profile subnets")
PROFILE
  GBRAIN_RUNTIME_ARGS=("-var-file=$GBRAIN_RUNTIME_TFVARS")
  echo "Runtime profile SHA-256: $GBRAIN_RUNTIME_HASH"
else
  GBRAIN_STATE_ERROR=$(mktemp)
  trap 'rm -f "$GBRAIN_STATE_ERROR"' EXIT
  if aws s3api head-object --bucket "$STATE_BUCKET" --key research/gbrain/terraform.tfstate --region "$AWS_REGION" > /dev/null 2>"$GBRAIN_STATE_ERROR"; then
    echo 'Existing Gbrain state requires GBRAIN_RUNTIME_TFVARS and its reviewed SHA-256 for updates and rollback' >&2
    exit 1
  elif ! grep -Eq '404|NoSuchKey|Not Found' "$GBRAIN_STATE_ERROR"; then
    echo 'Unable to establish whether Gbrain state exists; refusing deployment' >&2
    exit 1
  fi
  GBRAIN_EXISTING_SERVICE=$(aws ecs describe-services --region "$AWS_REGION" --cluster adp-research-gbrain --services adp-research-gbrain-mcp --query 'services[?status==`ACTIVE`].serviceName' --output text 2>"$GBRAIN_STATE_ERROR") || {
    grep -q 'ClusterNotFoundException' "$GBRAIN_STATE_ERROR" || { echo 'Unable to establish whether Gbrain service exists; refusing deployment' >&2; exit 1; }
  }
  [[ -z "$GBRAIN_EXISTING_SERVICE" || "$GBRAIN_EXISTING_SERVICE" == "None" ]] || { echo 'Existing Gbrain service requires a reviewed runtime profile' >&2; exit 1; }
  rm -f "$GBRAIN_STATE_ERROR"
  trap - EXIT
fi
if [ -n "$GBRAIN_DIGEST" ]; then
  [[ "$GBRAIN_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Invalid rollback digest' >&2; exit 1; }
else
  SOURCE_SHA="${SOURCE_SHA:-$(git -C "$GBRAIN_REPO_ROOT" rev-parse HEAD)}"
  [[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo 'Expected a full source SHA' >&2; exit 1; }
fi
cd "$GBRAIN_SCRIPT_DIR/../terraform"
terraform init -backend-config="bucket=$STATE_BUCKET" \
  -backend-config=key=research/gbrain/terraform.tfstate \
  -backend-config="region=$AWS_REGION" -backend-config=encrypt=true \
  -backend-config=dynamodb_table=adp-terraform-locks -input=false -reconfigure
if [ -z "$GBRAIN_DIGEST" ]; then
  # Deliberate bootstrap exception: these targets include no ECS task/service.
  # The full apply below is allowed only after publication and registry verification.
  terraform apply -var-file=environments/dev.tfvars -var="state_bucket=$STATE_BUCKET" "${GBRAIN_RUNTIME_ARGS[@]}" \
    -target=module.storage -target=module.build -input=false -auto-approve
  PROJECT=$(terraform output -raw build_project_name)
  ADP_RELEASE_BUILD=true STATE_BUCKET="$STATE_BUCKET" AWS_REGION="$AWS_REGION" SOURCE_SHA="$SOURCE_SHA" \
    bash "$GBRAIN_REPO_ROOT/platform/scripts/codebuild-run.sh" "$PROJECT"
  GBRAIN_DIGEST=$(aws ecr describe-images --region "$AWS_REGION" \
    --repository-name adp-research-gbrain --image-ids "imageTag=$SOURCE_SHA" \
    --query 'imageDetails[0].imageDigest' --output text)
  [[ "$GBRAIN_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Build did not publish a valid digest' >&2; exit 1; }
fi
# Resolve by digest even on rollback. Missing/unreadable artifacts stop before activation.
GBRAIN_VERIFIED=$(aws ecr describe-images --region "$AWS_REGION" \
  --repository-name adp-research-gbrain --image-ids "imageDigest=$GBRAIN_DIGEST" \
  --query 'imageDetails[0].imageDigest' --output text)
[[ "$GBRAIN_VERIFIED" == "$GBRAIN_DIGEST" ]] || { echo 'Digest verification failed' >&2; exit 1; }
terraform apply -var-file=environments/dev.tfvars -var="state_bucket=$STATE_BUCKET" "${GBRAIN_RUNTIME_ARGS[@]}" \
  -var="container_image_digest=$GBRAIN_DIGEST" -input=false -auto-approve
