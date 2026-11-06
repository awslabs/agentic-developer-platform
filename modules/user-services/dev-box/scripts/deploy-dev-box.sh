#!/usr/bin/env bash
# =============================================================================
# deploy-dev-box.sh — self-managed Dev Box stage for the platform account (#7998)
# =============================================================================
# The module's own entrypoint. deploy-all.sh calls it as "Step 12: Dev Box" when
# the stage is on (--dev-box on, ADP_DEV_BOX=on, or stage_enabled = true in
# environments/<env>/modules/dev-box.tfvars) and from --dev-box-only; it also
# runs standalone against an environment whose gateway is already deployed.
# Dev Box is an OPTIONAL module: nothing here is required by the core platform,
# and an environment without the tfvars never runs it.
#
#   [1/7] platform prerequisites  terraform infra/platform (workspace KMS key, SSM
#                                 interface endpoints + group, AMI CodeBuild project)
#   [2/7] image                   reuse /adp/<env>/dev-box/image-id or build it
#                                 (scripts/build-dev-box-image.sh) when absent or --build-image
#         crew bootstrap (#8055)  --crew-bootstrap image (default: the Crew profile is baked
#                                 into the AMI) or user-data (base AMI; the guest installs the
#                                 pinned bundle at first boot from s3://<state-bucket>/dev-box/
#                                 image-inputs/<--crew-bundle-sha>/; the stage uploads the
#                                 root-only helpers next to it). The mode persists in SSM
#                                 crew-bootstrap, so a re-run without flags keeps it; the
#                                 digest and revision default to the bundle build's
#                                 crew-bundle-sha256 / crew-bundle-revision (#8054).
#   [3/7] foundation              terraform infra (guest/controller IAM, launch template,
#                                 controller runtime IRSA role, SSH grant role trusted by
#                                 the live gateway IRSA role (#8049), SSM /adp/<env>/dev-box/*)
#   [4/7] release                 manifest (scripts/self_managed_release.py) + runtime.py
#                                 read-back of image, network and IAM; receipt kept locally
#   [5/7] gateway + controller    BG_DEV_BOX_* in the live bedrockgateway-config (same
#                                 values both renderers read from SSM), controller
#                                 Deployment from k8s/controller.yaml (gateway image)
#   [6/7] seed                    python -m src.cloud_desktop.seed in a gateway pod:
#                                 policy row + approved template + ec2-instance target
#   [7/7] verify                  scripts/verify-dev-box.py when an operator token is
#                                 available (ADP_TOKEN or `adp token`), else printed
#   --controller-only             skip every stage and only bring the running controller
#                                 up to this checkout's controller/*.py on the gateway image
#                                 the environment already runs (scripts/refresh-controller.sh;
#                                 the same path gateway-deploy.yml and
#                                 dev-box-controller-deploy.yml use). Needs kubectl only.
#
# Usage:
#   deploy-dev-box.sh [--env dev] [--region us-east-1] [--update] [--confirm-destructive]
#                     [--build-image] [--skip-platform] [--skip-image] [--skip-foundation]
#                     [--skip-controller] [--skip-seed] [--skip-verify] [--verify-live]
#                     [--controller-only]
#                     [--crew-bootstrap image|user-data] [--crew-bundle-sha <sha256>]
#                     [--profile-revision <rev>] [--crew-model-id <id>]
#                     [--dry-run]
#
# Exit codes: 0 stage complete; 1 a step failed (message names the fix);
# 2 inputs missing (tfvars, image, gateway not deployed, wrong account).
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${MODULE_ROOT}/../../.." && pwd)"

ENVIRONMENT="${ADP_ENV:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
UPDATE_MODE=false
CONFIRM_DESTRUCTIVE=false
BUILD_IMAGE=false
SKIP_PLATFORM=false
SKIP_IMAGE=false
SKIP_FOUNDATION=false
SKIP_CONTROLLER=false
SKIP_SEED=false
SKIP_VERIFY=false
CONTROLLER_ONLY=false
VERIFY_LIVE="${ADP_DEV_BOX_VERIFY_LIVE:-auto}"
CREW_BOOTSTRAP=""
CREW_BUNDLE_SHA=""
CREW_PROFILE_REVISION=""
CREW_MODEL_ID=""
DRY_RUN=false
CONTROLLER_NAMESPACE_DEFAULT="adp-gateway"
GATEWAY_NAMESPACE="adp-gateway"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)                 ENVIRONMENT="${2:?--env requires a value}"; shift 2 ;;
    --region)              AWS_REGION="${2:?--region requires a value}"; shift 2 ;;
    --update)              UPDATE_MODE=true; shift ;;
    --confirm-destructive) CONFIRM_DESTRUCTIVE=true; shift ;;
    --build-image)         BUILD_IMAGE=true; shift ;;
    --skip-platform)       SKIP_PLATFORM=true; shift ;;
    --skip-image)          SKIP_IMAGE=true; shift ;;
    --skip-foundation)     SKIP_FOUNDATION=true; shift ;;
    --skip-controller)     SKIP_CONTROLLER=true; shift ;;
    --skip-seed)           SKIP_SEED=true; shift ;;
    --skip-verify)         SKIP_VERIFY=true; shift ;;
    --controller-only)     CONTROLLER_ONLY=true; shift ;;
    --verify-live)         VERIFY_LIVE=true; shift ;;
    --crew-bootstrap)      CREW_BOOTSTRAP="${2:?--crew-bootstrap requires image or user-data}"; shift 2 ;;
    --crew-bundle-sha)     CREW_BUNDLE_SHA="${2:?--crew-bundle-sha requires the bundle sha256}"; shift 2 ;;
    --profile-revision)    CREW_PROFILE_REVISION="${2:?--profile-revision requires a value}"; shift 2 ;;
    --crew-model-id)       CREW_MODEL_ID="${2:?--crew-model-id requires a gateway model id}"; shift 2 ;;
    --dry-run)             DRY_RUN=true; shift ;;
    -h|--help)             sed -n '2,44p' "$0"; exit 0 ;;
    *) echo "deploy-dev-box.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
step() { echo -e "\n${BLUE}━━━ Dev Box $1 ━━━${NC}\n"; }
ok()   { echo -e "${GREEN}✓ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
fail() { echo -e "${RED}✗ $1${NC}" >&2; exit 1; }
inputs_missing() { echo -e "${RED}✗ Dev Box inputs missing: $1${NC}" >&2; exit 2; }

[[ "$ENVIRONMENT" =~ ^[a-z][a-z0-9-]{0,31}$ ]] || inputs_missing "--env must be a short lowercase name"

if [ "$CONTROLLER_ONLY" = true ]; then
  # Operator fast path: no Terraform, image, seed or verify — just converge the live
  # controller with this checkout. Refuses to combine with stage flags so the two
  # modes cannot be confused in a transcript.
  for flag in "$UPDATE_MODE" "$BUILD_IMAGE" "$SKIP_PLATFORM" "$SKIP_IMAGE" "$SKIP_FOUNDATION" "$SKIP_CONTROLLER" "$SKIP_SEED" "$SKIP_VERIFY"; do
    [ "$flag" = false ] || inputs_missing "--controller-only takes no stage flags"
  done
  step "controller refresh (--controller-only)"
  command -v kubectl >/dev/null || inputs_missing "kubectl is required on the operator box"
  REFRESH_ARGS=(--namespace "$CONTROLLER_NAMESPACE_DEFAULT")
  [ "$DRY_RUN" = false ] || REFRESH_ARGS+=(--dry-run)
  bash "$SCRIPT_DIR/refresh-controller.sh" "${REFRESH_ARGS[@]}" || fail "controller refresh failed; see the controller log printed above"
  ok "Controller refreshed from $(git -C "$REPO_ROOT" rev-parse --short HEAD)"
  exit 0
fi
[ -z "$CREW_BOOTSTRAP" ] || [[ "$CREW_BOOTSTRAP" =~ ^(image|user-data)$ ]] || inputs_missing "--crew-bootstrap must be image or user-data"
[ -z "$CREW_BUNDLE_SHA" ] || [[ "$CREW_BUNDLE_SHA" =~ ^[0-9a-f]{64}$ ]] || inputs_missing "--crew-bundle-sha must be the lowercase hex sha256 of crew.tar.gz"
[ -z "$CREW_PROFILE_REVISION" ] || [[ "$CREW_PROFILE_REVISION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || inputs_missing "--profile-revision is not a valid revision"
[ -z "$CREW_MODEL_ID" ] || [[ "$CREW_MODEL_ID" =~ ^[A-Za-z0-9_.:-][A-Za-z0-9_.:-]{0,255}$ ]] || inputs_missing "--crew-model-id is not a valid gateway model id"
ENV_DIR="$REPO_ROOT/environments/$ENVIRONMENT/modules"
FOUNDATION_TFVARS="$ENV_DIR/dev-box.tfvars"
PLATFORM_TFVARS="$ENV_DIR/dev-box-platform.tfvars"
FOUNDATION_BACKEND="$ENV_DIR/dev-box-backend.tfvars"
PLATFORM_BACKEND="$ENV_DIR/dev-box-platform-backend.tfvars"
SSM_PREFIX="/adp/${ENVIRONMENT}/dev-box"
WORK_DIR="$REPO_ROOT/.adp-dev-box/$ENVIRONMENT"
RELEASE_DIR="$WORK_DIR/release"

[ -f "$FOUNDATION_TFVARS" ] || inputs_missing "$FOUNDATION_TFVARS not found. Copy environments/dev/modules/dev-box.tfvars, set the platform-account values (vpc_id, private_subnet_id, gateway_security_group_id, run_table_arn, gateway_execute_arn, policy_org_id) and re-run"
[ -f "$PLATFORM_TFVARS" ] || inputs_missing "$PLATFORM_TFVARS not found. Copy environments/dev/modules/dev-box-platform.tfvars (vpc_id, vpc_cidr_block, endpoint_subnet_ids, build_subnet_id)"
for tool in aws terraform kubectl python3 tar sha256sum; do
  command -v "$tool" >/dev/null || inputs_missing "$tool is required on the operator box"
done

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || inputs_missing "AWS credentials for the platform account are required"
tfvar() { # tfvar <file> <name> -> scalar value without quotes, or empty
  sed -nE "s/^[[:space:]]*$2[[:space:]]*=[[:space:]]*\"?([^\"#]*)\"?[[:space:]]*(#.*)?$/\1/p" "$1" | head -1 | sed -E 's/[[:space:]]+$//'
}
TFVARS_ACCOUNT=$(tfvar "$FOUNDATION_TFVARS" account_id)
[ "$TFVARS_ACCOUNT" = "$ACCOUNT_ID" ] || inputs_missing "dev-box.tfvars account_id=$TFVARS_ACCOUNT but the caller resolves to $ACCOUNT_ID; switch AWS_PROFILE or fix the tfvars"
TFVARS_REGION=$(tfvar "$FOUNDATION_TFVARS" region)
[ -z "$TFVARS_REGION" ] || [ "$TFVARS_REGION" = "$AWS_REGION" ] || inputs_missing "dev-box.tfvars region=$TFVARS_REGION differs from --region $AWS_REGION"
DEPLOYMENT_ID=$(tfvar "$FOUNDATION_TFVARS" deployment_id)
[ -n "$DEPLOYMENT_ID" ] || inputs_missing "deployment_id is not set in $FOUNDATION_TFVARS"
POLICY_ORG_ID=$(tfvar "$FOUNDATION_TFVARS" policy_org_id)
[ -n "$POLICY_ORG_ID" ] || inputs_missing "policy_org_id is not set in $FOUNDATION_TFVARS (the organizations.id that receives the Dev Box policy, e.g. aws-e)"
CONTROLLER_NAMESPACE=$(tfvar "$FOUNDATION_TFVARS" controller_namespace); CONTROLLER_NAMESPACE="${CONTROLLER_NAMESPACE:-$CONTROLLER_NAMESPACE_DEFAULT}"
CONTROLLER_SA=$(tfvar "$FOUNDATION_TFVARS" controller_service_account); CONTROLLER_SA="${CONTROLLER_SA:-dev-box-controller}"

STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"
LOCK_TABLE="adp-terraform-locks"
SOURCE_SHA=$(git -C "$REPO_ROOT" rev-parse HEAD)
mkdir -p "$RELEASE_DIR"

get_ssm() { aws ssm get-parameter --name "$1" --query Parameter.Value --output text --region "$AWS_REGION" 2>/dev/null || echo "${2:-}"; }
ssm_or_empty() { local v; v=$(get_ssm "$1" ""); [ "$v" != "None" ] && [ "$v" != " " ] && printf '%s' "$v" || printf ''; }

write_backend() { # write_backend <file> <state-key>
  [ -f "$1" ] || cat > "$1" <<EOT
bucket         = "${STATE_BUCKET}"
key            = "${2}"
region         = "${AWS_REGION}"
encrypt        = true
dynamodb_table = "${LOCK_TABLE}"
EOT
  python3 "$REPO_ROOT/platform/scripts/prepare-backends.py" "$REPO_ROOT/environments/$ENVIRONMENT" "$ACCOUNT_ID"
}

# terraform apply with the update-mode destroy gate: in --update a plan that
# destroys anything is refused unless --confirm-destructive (same contract as
# deploy-all.sh's terraform_update_apply).
tf_apply() { # tf_apply <label> <chdir> <backend> <args...>
  local label="$1" dir="$2" backend="$3"; shift 3
  terraform -chdir="$dir" init -backend-config="$backend" -input=false -reconfigure >/dev/null
  if [ "$DRY_RUN" = true ]; then
    echo "dry-run: terraform -chdir=$dir plan $*"
    return 0
  fi
  if [ "$UPDATE_MODE" = true ]; then
    local plan="$WORK_DIR/$label.tfplan"
    terraform -chdir="$dir" plan -input=false -out="$plan" "$@"
    local destroys
    destroys=$(terraform -chdir="$dir" show -json "$plan" | python3 -c '
import json, sys
plan = json.load(sys.stdin)
print(sum(1 for change in plan.get("resource_changes", []) if "delete" in change.get("change", {}).get("actions", [])))')
    if [ "$destroys" != "0" ] && [ "$CONFIRM_DESTRUCTIVE" = false ]; then
      fail "$label: the plan destroys $destroys resource(s); re-run with --confirm-destructive after reviewing the plan"
    fi
    terraform -chdir="$dir" apply -input=false -auto-approve "$plan"
  else
    terraform -chdir="$dir" apply -input=false -auto-approve "$@"
  fi
}

# ---------------------------------------------------------------------------
step "[1/7] Platform prerequisites (KMS key, SSM endpoints, image builder)"
PLATFORM_DIR="$MODULE_ROOT/infra/platform"
if [ "$SKIP_PLATFORM" = true ]; then
  warn "Skipping the platform prerequisites (--skip-platform)"
else
  write_backend "$PLATFORM_BACKEND" "${ENVIRONMENT}/modules/dev-box-platform/terraform.tfstate"
  tf_apply dev-box-platform "$PLATFORM_DIR" "$PLATFORM_BACKEND" -var-file="$PLATFORM_TFVARS" \
    -var "account_id=$ACCOUNT_ID" -var "region=$AWS_REGION" -var "environment=$ENVIRONMENT" -var "state_bucket=$STATE_BUCKET"
  ok "Platform prerequisites applied"
fi
KMS_KEY_ARN=$(ssm_or_empty "$SSM_PREFIX/kms-key-arn")
SSM_ENDPOINT_SG=$(ssm_or_empty "$SSM_PREFIX/ssm-endpoint-security-group-id")
if [ "$DRY_RUN" = false ]; then
  [ -n "$KMS_KEY_ARN" ] && [ -n "$SSM_ENDPOINT_SG" ] \
    || inputs_missing "$SSM_PREFIX/kms-key-arn or ssm-endpoint-security-group-id is absent; apply modules/user-services/dev-box/infra/platform (re-run without --skip-platform)"
fi

# ---------------------------------------------------------------------------
step "[2/7] Image"
IMAGE_ID=$(ssm_or_empty "$SSM_PREFIX/image-id")
if [ "$SKIP_IMAGE" = true ]; then
  warn "Skipping the image step (--skip-image)"
elif [ "$BUILD_IMAGE" = true ] || [ -z "$IMAGE_ID" ]; then
  [ -z "$IMAGE_ID" ] && echo "No AMI published at $SSM_PREFIX/image-id; building the base image (Crew image: re-run build-dev-box-image.sh with --crew-bundle/--crew-manifest/--profile-revision)"
  if [ "$DRY_RUN" = true ]; then
    echo "dry-run: bash $SCRIPT_DIR/build-dev-box-image.sh --env $ENVIRONMENT --region $AWS_REGION"
  else
    bash "$SCRIPT_DIR/build-dev-box-image.sh" --env "$ENVIRONMENT" --region "$AWS_REGION" \
      || fail "Image build failed; see the CodeBuild log above, then re-run (the stage resumes at the image)"
    IMAGE_ID=$(ssm_or_empty "$SSM_PREFIX/image-id")
  fi
fi
IMAGE_REVISION=$(ssm_or_empty "$SSM_PREFIX/image-revision")
IMAGE_SOURCE_REVISION=$(ssm_or_empty "$SSM_PREFIX/image-source-revision")
PROFILE_REVISION=$(ssm_or_empty "$SSM_PREFIX/profile-revision")
if [ "$DRY_RUN" = false ]; then
  [[ "$IMAGE_ID" =~ ^ami-[0-9a-f]{8,17}$ ]] || inputs_missing "$SSM_PREFIX/image-id holds no AMI; run modules/user-services/dev-box/scripts/build-dev-box-image.sh --env $ENVIRONMENT"
  [ -n "$IMAGE_REVISION" ] && [[ "$IMAGE_SOURCE_REVISION" =~ ^[0-9a-f]{40}$ ]] \
    || inputs_missing "$SSM_PREFIX/image-revision or image-source-revision is absent; rebuild the image with build-dev-box-image.sh (it publishes all of them)"
  ok "Image $IMAGE_ID (revision $IMAGE_REVISION, source ${IMAGE_SOURCE_REVISION:0:12}, Crew profile ${PROFILE_REVISION:-none})"
fi

# Crew bootstrap mode (#8055): the flag, else what this environment last applied
# (SSM crew-bootstrap, written by the foundation), else image. In user-data mode the
# profile comes from the pinned bundle, not from the AMI: the bundle digest names the
# S3 prefix build-crew-bundle.sh / build-dev-box-image.sh upload to, and without
# --crew-bundle-sha / --profile-revision the stage takes the bundle the build last
# published (SSM crew-bundle-sha256 / crew-bundle-revision, image/bundle-buildspec.yml).
# The manifest there supplies the source revision Terraform pins, and the root-only
# helpers user data installs are uploaded next to it from this checkout (Terraform
# pins their digests from the same files, infra/crew_bootstrap.tf).
[ -n "$CREW_BOOTSTRAP" ] || CREW_BOOTSTRAP=$(ssm_or_empty "$SSM_PREFIX/crew-bootstrap")
CREW_BOOTSTRAP="${CREW_BOOTSTRAP:-image}"
CREW_BUNDLE_URI=""; CREW_MANIFEST_URI=""; CREW_SOURCE_REVISION=""
CREW_HELPERS=(image/install_crew.py image/write_enrollment.py image/write_host_certificate.py image/verify_crew_ready.py
  image/restore_crew_host.py image/checkpoint_crew_host.py
  ../../agent-factory/agent/scripts/crew_checkpoint.py)
if [ "$CREW_BOOTSTRAP" = user-data ]; then
  [ -n "$CREW_BUNDLE_SHA" ] || CREW_BUNDLE_SHA=$(ssm_or_empty "$SSM_PREFIX/crew-bundle-sha256")
  [ -n "$CREW_PROFILE_REVISION" ] || CREW_PROFILE_REVISION=$(ssm_or_empty "$SSM_PREFIX/crew-bundle-revision")
  [[ "$CREW_BUNDLE_SHA" =~ ^[0-9a-f]{64}$ ]] \
    || inputs_missing "--crew-bootstrap user-data needs --crew-bundle-sha <sha256> (or a bundle published to $SSM_PREFIX/crew-bundle-sha256 by build-crew-bundle.sh): s3://$STATE_BUCKET/dev-box/image-inputs/<sha256>/crew.tar.gz"
  [[ "$CREW_PROFILE_REVISION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] \
    || inputs_missing "--crew-bootstrap user-data needs --profile-revision <rev> (the profile.json revision of that bundle; build-crew-bundle.sh publishes it as $SSM_PREFIX/crew-bundle-revision)"
  CREW_INPUT_PREFIX="s3://${STATE_BUCKET}/dev-box/image-inputs/${CREW_BUNDLE_SHA}"
  CREW_BUNDLE_URI="$CREW_INPUT_PREFIX/crew.tar.gz"
  CREW_MANIFEST_URI="$CREW_INPUT_PREFIX/profile.json"
  if [ "$DRY_RUN" = true ]; then
    CREW_SOURCE_REVISION="0000000000000000000000000000000000000000"
    echo "dry-run: would read $CREW_MANIFEST_URI and upload ${#CREW_HELPERS[@]} helper scripts to $CREW_INPUT_PREFIX/adp/"
  else
    aws s3api head-object --region "$AWS_REGION" --bucket "$STATE_BUCKET" --key "dev-box/image-inputs/${CREW_BUNDLE_SHA}/crew.tar.gz" >/dev/null 2>&1 \
      || inputs_missing "$CREW_BUNDLE_URI does not exist; upload the Crew bundle and profile.json first (#8054 build-crew-bundle.sh, or build-dev-box-image.sh --crew-bundle --crew-manifest --profile-revision)"
    aws s3 cp --region "$AWS_REGION" --only-show-errors "$CREW_MANIFEST_URI" "$WORK_DIR/crew-profile.json" \
      || inputs_missing "$CREW_MANIFEST_URI does not exist next to the bundle"
    CREW_SOURCE_REVISION=$(python3 - "$WORK_DIR/crew-profile.json" "$CREW_PROFILE_REVISION" "$CREW_BUNDLE_SHA" <<'PYMANIFEST'
import json, re, sys
manifest = json.load(open(sys.argv[1]))
assert manifest.get("revision") == sys.argv[2], f"profile.json revision {manifest.get('revision')!r} differs from --profile-revision {sys.argv[2]}"
assert manifest.get("bundle_sha256") == sys.argv[3], "profile.json bundle_sha256 differs from --crew-bundle-sha"
assert re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("source_revision"))), "profile.json source_revision is not a full commit"
print(manifest["source_revision"])
PYMANIFEST
) || inputs_missing "the Crew profile manifest at $CREW_MANIFEST_URI does not describe --crew-bundle-sha / --profile-revision"
    for helper in "${CREW_HELPERS[@]}"; do
      aws s3 cp --region "$AWS_REGION" --only-show-errors "$MODULE_ROOT/$helper" "$CREW_INPUT_PREFIX/adp/$(basename "$helper")" \
        || fail "could not upload $(basename "$helper") to $CREW_INPUT_PREFIX/adp/ (the guest fetches it at first boot)"
    done
    ok "Crew helpers uploaded to $CREW_INPUT_PREFIX/adp/ (${#CREW_HELPERS[@]} files; digests pinned by Terraform)"
  fi
  [ -z "$PROFILE_REVISION" ] || [ "$PROFILE_REVISION" = "$CREW_PROFILE_REVISION" ] \
    || warn "image $IMAGE_ID already carries Crew profile $PROFILE_REVISION; user-data mode installs $CREW_PROFILE_REVISION on top of it (a base AMI is the intended source)"
  PROFILE_REVISION="$CREW_PROFILE_REVISION"
  ok "Crew bootstrap: user-data from $CREW_BUNDLE_URI (profile $PROFILE_REVISION, source ${CREW_SOURCE_REVISION:0:12})"
else
  [ -z "$CREW_BUNDLE_SHA" ] && [ -z "$CREW_PROFILE_REVISION" ] \
    || inputs_missing "--crew-bundle-sha and --profile-revision apply only with --crew-bootstrap user-data (image mode takes the profile from the AMI's profile-revision)"
  ok "Crew bootstrap: image (profile ${PROFILE_REVISION:-none} from the AMI)"
fi

# ---------------------------------------------------------------------------
step "[3/7] Foundation (guest/controller IAM, launch template, controller runtime, SSM)"
FOUNDATION_DIR="$MODULE_ROOT/infra"
# The release pins the commit the AMI was built from: runtime.py compares the
# manifest's source_revision with the image's adp:source-revision tag. The
# controller code deployed in step 5 comes from HEAD and is recorded on the
# Deployment (adp.aws-e.io/source-revision).
RELEASE_SOURCE_REVISION="${IMAGE_SOURCE_REVISION:-$SOURCE_SHA}"
# The run registry table may be encrypted with a customer KMS key; the controller then needs
# that key to mint/renew/revoke host runs (seen live 2026-10-18: AccessDeniedException on
# TransactWriteItems kept host enrollment pending forever). Resolve it from the table's SSE
# settings so neither deploy path has to know the key by hand; empty means an AWS-owned key.
RUN_TABLE_ARN=$(tfvar "$FOUNDATION_TFVARS" run_table_arn)
RUN_TABLE_KMS_KEY_ARN=""
if [ -n "$RUN_TABLE_ARN" ]; then
  RUN_TABLE_KMS_KEY_ARN=$(aws dynamodb describe-table --region "$AWS_REGION" --table-name "${RUN_TABLE_ARN##*:table/}" \
    --query 'Table.SSEDescription.KMSMasterKeyArn' --output text 2>/dev/null || true)
  [ "$RUN_TABLE_KMS_KEY_ARN" != "None" ] || RUN_TABLE_KMS_KEY_ARN=""
  [ -z "$RUN_TABLE_KMS_KEY_ARN" ] || [[ "$RUN_TABLE_KMS_KEY_ARN" =~ ^arn:aws:kms:[a-z]{2}-[a-z]+-[0-9]:[0-9]{12}:key/[0-9a-f-]{36}$ ]] \
    || inputs_missing "The run registry table reports an unexpected KMS key ARN: $RUN_TABLE_KMS_KEY_ARN"
  ok "Run registry ${RUN_TABLE_ARN##*:table/}: ${RUN_TABLE_KMS_KEY_ARN:+customer KMS key granted to the controller}${RUN_TABLE_KMS_KEY_ARN:-AWS-owned key}"
fi
# Crew host-lease mirror (#9620): the controller pod fences a stopped box by rewriting
# the owner's lease item in the gateway's chat-context table, which is SSE-KMS with a
# customer key. Resolve both from the table (named by the gateway as
# adp-<environment>-chat-context) so the runtime role is granted exactly that table and
# key; an environment without the table grants nothing.
CHAT_CONTEXT_TABLE_ARN=""
CHAT_CONTEXT_KMS_KEY_ARN=""
CHAT_CONTEXT_TABLE_JSON=$(aws dynamodb describe-table --region "$AWS_REGION" --table-name "adp-${ENVIRONMENT}-chat-context" \
  --query '[Table.TableArn, Table.SSEDescription.KMSMasterKeyArn]' --output json 2>/dev/null || echo '[]')
CHAT_CONTEXT_TABLE_ARN=$(python3 -c 'import json,sys; v=json.loads(sys.argv[1]); print(v[0] if len(v) > 0 and v[0] else "")' "$CHAT_CONTEXT_TABLE_JSON")
CHAT_CONTEXT_KMS_KEY_ARN=$(python3 -c 'import json,sys; v=json.loads(sys.argv[1]); print(v[1] if len(v) > 1 and v[1] else "")' "$CHAT_CONTEXT_TABLE_JSON")
if [ -n "$CHAT_CONTEXT_TABLE_ARN" ]; then
  [[ "$CHAT_CONTEXT_TABLE_ARN" =~ ^arn:aws:dynamodb:[a-z]{2}-[a-z]+-[0-9]:[0-9]{12}:table/[A-Za-z0-9_.-]+$ ]] \
    || inputs_missing "The chat-context table reports an unexpected ARN: $CHAT_CONTEXT_TABLE_ARN"
  [ -z "$CHAT_CONTEXT_KMS_KEY_ARN" ] || [[ "$CHAT_CONTEXT_KMS_KEY_ARN" =~ ^arn:aws:kms:[a-z]{2}-[a-z]+-[0-9]:[0-9]{12}:key/[0-9a-f-]{36}$ ]] \
    || inputs_missing "The chat-context table reports an unexpected KMS key ARN: $CHAT_CONTEXT_KMS_KEY_ARN"
  ok "Chat-context table ${CHAT_CONTEXT_TABLE_ARN##*:table/}: lease-mirror fence granted to the controller runtime (${CHAT_CONTEXT_KMS_KEY_ARN:+customer KMS key}${CHAT_CONTEXT_KMS_KEY_ARN:-AWS-owned key})"
else
  warn "No adp-${ENVIRONMENT}-chat-context table visible; the controller runtime gets no lease-mirror grant (fence logs 'host lease mirror not cleared')"
fi
# Regional STS may resolve privately; its endpoint must accept this guest group.
STS_ENDPOINT_GROUPS=$(aws ec2 describe-vpc-endpoints --region "$AWS_REGION" \
  --filters "Name=vpc-id,Values=$(tfvar "$FOUNDATION_TFVARS" vpc_id)" "Name=service-name,Values=com.amazonaws.$AWS_REGION.sts" \
  --query 'VpcEndpoints[?PrivateDnsEnabled && State == `available`].Groups[].GroupId' --output json)
FOUNDATION_ARGS=(-var-file="$FOUNDATION_TFVARS" -var "account_id=$ACCOUNT_ID" -var "region=$AWS_REGION" -var "environment=$ENVIRONMENT"
  -var "image_id=${IMAGE_ID:-ami-00000000}" -var "image_revision=$IMAGE_REVISION" -var "source_revision=$RELEASE_SOURCE_REVISION"
  -var "sts_endpoint_security_group_ids=$STS_ENDPOINT_GROUPS" -var "crew_profile_revision=$PROFILE_REVISION" -var "kms_key_arn=$KMS_KEY_ARN" -var "ssm_endpoint_security_group_id=$SSM_ENDPOINT_SG"
  -var "crew_bootstrap=$CREW_BOOTSTRAP" -var "crew_bundle_s3_uri=$CREW_BUNDLE_URI" -var "crew_manifest_s3_uri=$CREW_MANIFEST_URI"
  -var "crew_bundle_sha256=$CREW_BUNDLE_SHA" -var "crew_source_revision=$CREW_SOURCE_REVISION"
  -var "chat_context_table_arn=$CHAT_CONTEXT_TABLE_ARN" -var "chat_context_kms_key_arn=$CHAT_CONTEXT_KMS_KEY_ARN")
# Crew model pin: the flag, else what this environment last applied (SSM crew-model-id,
# written by the foundation). The foundation only wires gateway_execute_arn (the host role
# that lets the controller consume enrollments) while the model is pinned, so a re-run
# without --crew-model-id must keep the pin rather than silently drop it (dev, 2026-10-18).
[ -n "$CREW_MODEL_ID" ] || CREW_MODEL_ID=$(ssm_or_empty "$SSM_PREFIX/crew-model-id")
[ -z "$CREW_MODEL_ID" ] || [[ "$CREW_MODEL_ID" =~ ^[A-Za-z0-9_.:-][A-Za-z0-9_.:-]{0,255}$ ]] || inputs_missing "$SSM_PREFIX/crew-model-id is not a valid gateway model id: $CREW_MODEL_ID"
[ -z "$CREW_MODEL_ID" ] || FOUNDATION_ARGS+=(-var "crew_model_id=$CREW_MODEL_ID")
FOUNDATION_ARGS+=(-var "run_table_kms_key_arn=$RUN_TABLE_KMS_KEY_ARN")
if [ -z "$(tfvar "$FOUNDATION_TFVARS" controller_principal_arn)" ] && [ -z "$(tfvar "$FOUNDATION_TFVARS" eks_oidc_provider_arn)" ]; then
  OIDC_ISSUER=$(aws eks describe-cluster --name "adp-${ENVIRONMENT}-eks-cluster" --query 'cluster.identity.oidc.issuer' --output text 2>/dev/null || true)
  [ -n "$OIDC_ISSUER" ] && [ "$OIDC_ISSUER" != "None" ] || inputs_missing "cannot resolve the EKS OIDC issuer for adp-${ENVIRONMENT}-eks-cluster; set eks_oidc_provider_arn in $FOUNDATION_TFVARS"
  FOUNDATION_ARGS+=(-var "eks_oidc_provider_arn=arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${OIDC_ISSUER#https://}")
fi
# SSH grant (#8049): the grant role trusts exactly the gateway's IRSA role, read
# from the live ServiceAccount so the stage never guesses a role name. The gateway
# learns the grant ARN through SSM ssh-grant-role-arn (step 5 and both renderers).
GATEWAY_ROLE_ARN=$(kubectl -n "$GATEWAY_NAMESPACE" get sa gateway-service -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}' 2>/dev/null || true)
[[ "$GATEWAY_ROLE_ARN" =~ ^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$ ]] \
  || inputs_missing "cannot resolve the gateway IRSA role: serviceaccount/gateway-service in $GATEWAY_NAMESPACE has no eks.amazonaws.com/role-arn annotation (got '${GATEWAY_ROLE_ARN:-<empty>}'); deploy the gateway first (deploy-all.sh renders modules/gateway/k8s/serviceaccount.yaml), then re-run with --dev-box-only"
FOUNDATION_ARGS+=(-var "gateway_principal_arn=$GATEWAY_ROLE_ARN")
if [ -z "$(tfvar "$FOUNDATION_TFVARS" rds_resource_id)" ]; then
  RDS_RESOURCE_ID=$(aws rds describe-db-instances --db-instance-identifier "bedrockgw-${ENVIRONMENT}-postgres" --query 'DBInstances[0].DbiResourceId' --output text 2>/dev/null || true)
  if [ -n "$RDS_RESOURCE_ID" ] && [ "$RDS_RESOURCE_ID" != "None" ]; then
    FOUNDATION_ARGS+=(-var "rds_resource_id=$RDS_RESOURCE_ID")
  else
    warn "gateway RDS instance bedrockgw-${ENVIRONMENT}-postgres not found; the controller runtime gets no rds-db:connect grant (set rds_resource_id in the tfvars)"
  fi
fi
if [ "$SKIP_FOUNDATION" = true ]; then
  warn "Skipping the foundation apply (--skip-foundation)"
  terraform -chdir="$FOUNDATION_DIR" init -backend-config="$FOUNDATION_BACKEND" -input=false -reconfigure >/dev/null
else
  write_backend "$FOUNDATION_BACKEND" "${ENVIRONMENT}/modules/dev-box/terraform.tfstate"
  tf_apply dev-box "$FOUNDATION_DIR" "$FOUNDATION_BACKEND" "${FOUNDATION_ARGS[@]}"
  ok "Foundation applied"
fi
if [ "$DRY_RUN" = true ]; then
  echo "dry-run: stopping before the release manifest, controller, seed and verification"
  exit 0
fi

# ---------------------------------------------------------------------------
step "[4/7] Release manifest and runtime read-back"
OUTPUTS_JSON="$RELEASE_DIR/outputs.json"
terraform -chdir="$FOUNDATION_DIR" output -json > "$OUTPUTS_JSON"
MODULE_TAR="$RELEASE_DIR/dev-box-module.tar.gz"
tar -czf "$MODULE_TAR" --exclude=".terraform*" --exclude="*.tfstate*" --exclude="__pycache__" --exclude="tests" \
  -C "$MODULE_ROOT" image infra controller k8s scripts release.py runtime.py engine_evidence.py deploy.sh
INFRA_SHA=$(sha256sum "$FOUNDATION_DIR/main.tf" | cut -d' ' -f1)
MODULE_SHA=$(sha256sum "$MODULE_TAR" | cut -d' ' -f1)
MANIFEST_JSON="$RELEASE_DIR/release.json"
RELEASE_SHA=$(python3 "$SCRIPT_DIR/self_managed_release.py" --outputs "$OUTPUTS_JSON" --source-revision "$RELEASE_SOURCE_REVISION" \
  --infra-sha256 "$INFRA_SHA" --module-sha256 "$MODULE_SHA" --output "$MANIFEST_JSON") \
  || fail "Could not build the release manifest from the Terraform outputs"
python3 -c "import boto3" 2>/dev/null || inputs_missing "python3 needs boto3 for runtime.py (python3 -m pip install boto3)"
python3 "$MODULE_ROOT/runtime.py" --manifest "$MANIFEST_JSON" --outputs "$OUTPUTS_JSON" --receipt "$RELEASE_DIR/receipt.json" \
  || fail "runtime.py refused the deployed release (image tags, network or IAM differ from the manifest)"
aws ssm put-parameter --region "$AWS_REGION" --name "$SSM_PREFIX/release-manifest-sha256" --type String --overwrite --value "$RELEASE_SHA" >/dev/null
ok "Release $RELEASE_SHA verified; manifest, outputs and receipt in $RELEASE_DIR"

# ---------------------------------------------------------------------------
step "[5/7] Gateway configuration and controller"
kubectl get configmap bedrockgateway-config -n "$GATEWAY_NAMESPACE" >/dev/null 2>&1 \
  || inputs_missing "bedrockgateway-config not found in $GATEWAY_NAMESPACE; deploy the gateway first (deploy-all.sh), then re-run with --dev-box-only"
GATEWAY_IMAGE=$(kubectl get deploy bedrockgateway -n "$GATEWAY_NAMESPACE" -o jsonpath='{.spec.template.spec.containers[0].image}')
[ -n "$GATEWAY_IMAGE" ] || inputs_missing "deploy/bedrockgateway has no image; the gateway must be deployed before Dev Box"
# Same keys and parameters both renderers read (gateway-deploy.yml, deploy-all.sh
# Step 4); patching now means the gateway accepts admissions in this run, and the
# next full render converges to the same values.
DESIRED_CM=$(python3 - "$ENVIRONMENT" "$AWS_REGION" <<'PY'
import json, subprocess, sys
environment, region = sys.argv[1:3]
keys = {
    "BG_DEV_BOX_DEPLOYMENT_ID": ("deployment-id", ""),
    "BG_DEV_BOX_MAX_DEPLOYMENT_LIVE_BOXES": ("max-deployment-live-boxes", "0"),
    "BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_BOXES": ("max-deployment-running-boxes", "0"),
    "BG_DEV_BOX_MAX_DEPLOYMENT_STORAGE_GB": ("max-deployment-storage-gb", "0"),
    "BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_HOURS": ("max-deployment-running-hours", "0"),
    "BG_DEV_BOX_MAX_DEPLOYMENT_BUDGET_USD": ("max-deployment-budget-usd", "0"),
    "BG_DEV_BOX_SSH_GRANT_ROLE_ARN": ("ssh-grant-role-arn", ""),
    "BG_DEV_BOX_CREW_BOOTSTRAP": ("crew-bootstrap", "image"),
}
desired = {}
for key, (name, default) in keys.items():
    result = subprocess.run(["aws", "ssm", "get-parameter", "--region", region, "--name", f"/adp/{environment}/dev-box/{name}",
                             "--query", "Parameter.Value", "--output", "text"], capture_output=True, text=True)
    value = result.stdout.strip() if result.returncode == 0 else default
    # " " is Terraform's "unset" sentinel (self_managed.tf); "None" is the CLI's absent value.
    desired[key] = default if value in ("", " ", "None") else value
print(json.dumps(desired))
PY
)
LIVE_CM=$(kubectl get configmap bedrockgateway-config -n "$GATEWAY_NAMESPACE" -o json | python3 -c '
import json, sys
data = json.load(sys.stdin).get("data", {})
desired = json.loads(sys.argv[1])
print("same" if all(data.get(k) == v for k, v in desired.items()) else "differs")' "$DESIRED_CM")
if [ "$LIVE_CM" = same ]; then
  ok "bedrockgateway-config already carries the Dev Box values"
else
  kubectl patch configmap bedrockgateway-config -n "$GATEWAY_NAMESPACE" --type merge --patch "{\"data\": $DESIRED_CM}" >/dev/null
  kubectl rollout restart deploy/bedrockgateway -n "$GATEWAY_NAMESPACE" >/dev/null
  kubectl rollout status deploy/bedrockgateway -n "$GATEWAY_NAMESPACE" --timeout=600s \
    || fail "Gateway rollout did not complete after the Dev Box ConfigMap patch (kubectl -n $GATEWAY_NAMESPACE rollout status deploy/bedrockgateway)"
  ok "bedrockgateway-config updated (BG_DEV_BOX_DEPLOYMENT_ID=$DEPLOYMENT_ID, BG_DEV_BOX_SSH_GRANT_ROLE_ARN from SSM ssh-grant-role-arn) and the gateway restarted"
fi

if [ "$SKIP_CONTROLLER" = true ]; then
  warn "Skipping the controller (--skip-controller)"
else
  RUNTIME_ROLE_ARN=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["controller_runtime_role_arn"]["value"])' "$OUTPUTS_JSON")
  [ -n "$RUNTIME_ROLE_ARN" ] || inputs_missing "controller_runtime_role_arn is empty: the tfvars supply controller_principal_arn, so run that principal's controller yourself or clear controller_principal_arn"
  kubectl get namespace "$CONTROLLER_NAMESPACE" >/dev/null 2>&1 || inputs_missing "namespace $CONTROLLER_NAMESPACE does not exist"
  kubectl create configmap dev-box-controller-code -n "$CONTROLLER_NAMESPACE" --from-file="$MODULE_ROOT/controller" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl create configmap dev-box-release -n "$CONTROLLER_NAMESPACE" --from-file=release.json="$MANIFEST_JSON" --from-file=outputs.json="$OUTPUTS_JSON" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  sed -e "s|__NAMESPACE__|${CONTROLLER_NAMESPACE}|g" \
      -e "s|__SERVICE_ACCOUNT__|${CONTROLLER_SA}|g" \
      -e "s|__CONTROLLER_RUNTIME_ROLE_ARN__|${RUNTIME_ROLE_ARN}|g" \
      -e "s|__GATEWAY_IMAGE__|${GATEWAY_IMAGE}|g" \
      -e "s|__DEPLOYMENT_ID__|${DEPLOYMENT_ID}|g" \
      -e "s|__RELEASE_SHA256__|${RELEASE_SHA}|g" \
      -e "s|__SOURCE_REVISION__|${SOURCE_SHA}|g" \
      -e "s|__CODE_CONFIGMAP__|dev-box-controller-code|g" \
      -e "s|__RELEASE_CONFIGMAP__|dev-box-release|g" \
      "$MODULE_ROOT/k8s/controller.yaml" | kubectl apply -f - >/dev/null
  if ! kubectl rollout status deploy/dev-box-controller -n "$CONTROLLER_NAMESPACE" --timeout=300s; then
    kubectl logs deploy/dev-box-controller -n "$CONTROLLER_NAMESPACE" --tail=50 || true
    fail "dev-box-controller did not become ready; the log above names the refused release check or missing grant"
  fi
  ok "Controller running in $CONTROLLER_NAMESPACE as $CONTROLLER_SA (image $GATEWAY_IMAGE)"
fi

# ---------------------------------------------------------------------------
step "[6/7] Seed admission policy and template through the gateway"
if [ "$SKIP_SEED" = true ]; then
  warn "Skipping the seed (--skip-seed)"
else
  POLICY_SPEC=$(terraform -chdir="$FOUNDATION_DIR" output -json policy_spec)
  SEED_RESULT=$(kubectl exec -n "$GATEWAY_NAMESPACE" deploy/bedrockgateway -- \
    env "BG_DEV_BOX_DEPLOYMENT_ID=$DEPLOYMENT_ID" python -m src.cloud_desktop.seed --spec-json "$POLICY_SPEC" --allow-ec2-crew-target) \
    || fail "Seeding dev_box_policies / dev_box_template_versions failed (see the gateway message above; an unknown organization means policy_org_id must name an existing tenant)"
  echo "$SEED_RESULT" | python3 -c '
import json, sys
result = json.loads(sys.stdin.read().strip().splitlines()[-1])
assert result.get("policy_enabled") is True and result.get("template_approved") is True, result
print("policy {} ({}), template {} ({}), crew target {}".format(result["policy"], result["policy_id"], result["template"], result["template_id"], result.get("crew_target")))' \
    || fail "The seed returned without an enabled policy"
  ok "Admission rows seeded for org $POLICY_ORG_ID / deployment $DEPLOYMENT_ID"
fi

# ---------------------------------------------------------------------------
step "[7/7] Live verification"
VERIFY_CMD="python3 $SCRIPT_DIR/verify-dev-box.py --env $ENVIRONMENT --region $AWS_REGION"
if [ "$SKIP_VERIFY" = true ]; then
  warn "Skipping live verification (--skip-verify). Run it yourself: $VERIFY_CMD"
else
  HAVE_TOKEN=false
  if [ -n "${ADP_TOKEN:-}" ]; then HAVE_TOKEN=true
  elif command -v adp >/dev/null 2>&1 && adp token >/dev/null 2>&1; then HAVE_TOKEN=true
  elif [ -x "$HOME/.adp/bin/adp" ] && "$HOME/.adp/bin/adp" token >/dev/null 2>&1; then HAVE_TOKEN=true
  fi
  if [ "$HAVE_TOKEN" = true ] && [ "$VERIFY_LIVE" = true ]; then
    EVIDENCE="$RELEASE_DIR/owner-paths-$(date -u +%Y%m%dT%H%M%SZ)-$$.json"
    python3 "$SCRIPT_DIR/verify-owner-paths.py" --env "$ENVIRONMENT" --region "$AWS_REGION" --evidence "$EVIDENCE" \
      --expected-revision "$PROFILE_REVISION" \
      || fail "Owner-path live acceptance failed; inspect $EVIDENCE before asking the owner to open Crew"
    ok "Owner paths verified; evidence: $EVIDENCE"
  elif [ "$HAVE_TOKEN" = true ] && [ "$VERIFY_LIVE" != false ]; then
    if python3 "$SCRIPT_DIR/verify-dev-box.py" --env "$ENVIRONMENT" --region "$AWS_REGION"; then
      ok "A Dev Box was created, reached running, answered SSM and was released"
    else
      code=$?
      [ "$code" -eq 3 ] && warn "Box ready and released but Crew did not report ready (expected until a Crew image with a pinned profile is deployed)" \
        || fail "verify-dev-box.py exited $code (see its exit code table); the stage is applied but not accepted"
    fi
  elif [ "$VERIFY_LIVE" = true ]; then
    fail "--verify-live needs an operator token: set ADP_TOKEN or `adp login`, then re-run"
  else
    warn "No operator token available (ADP_TOKEN / adp login); run the acceptance yourself: $VERIFY_CMD"
  fi
fi

echo ""
ok "Dev Box stage complete for $ENVIRONMENT (deployment $DEPLOYMENT_ID, image $IMAGE_ID, Crew bootstrap $CREW_BOOTSTRAP, release $RELEASE_SHA)"
echo "  UI: Settings → Runtime → 'Kiro Crew on my EC2 box', then chat → 'Open my EC2 Crew'"
echo "  Controller: kubectl -n $CONTROLLER_NAMESPACE logs deploy/dev-box-controller"
echo "  Acceptance: $VERIFY_CMD"
