#!/usr/bin/env bash
# =============================================================================
# build-dev-box-image.sh — build the Dev Box AMI through the module's CodeBuild
# project (#7998). No Packer or Docker on the operator box.
# =============================================================================
# Runs image/buildspec.yml in CodeBuild project adp-<env>-dev-box-image (created
# by infra/platform, applied by scripts/deploy-dev-box.sh): a clean private base
# image from the latest Amazon Linux 2023 AMI and, when an approved Crew bundle +
# profile are supplied, the derived Crew image (image/crew.pkr.hcl). The build
# publishes the AMI to SSM /adp/<env>/dev-box/image-id (+ image-revision,
# image-source-revision, base-image-id, profile-revision); the foundation stack
# reads image-id from there.
#
# Usage:
#   build-dev-box-image.sh [--env dev] [--region us-east-1]
#                          [--image-revision <rev>]          default base-<date>-<sha12>
#                          [--source-ami ami-...]            default: latest AL2023 (resolved in the build)
#                          [--crew-bundle <crew.tar.gz|s3://…/crew.tar.gz> --crew-manifest <profile.json|s3://…/profile.json>
#                           --profile-revision <rev>]
#                          [--dry-run]
#
# The Crew bundle and profile are approved files (see README "Crew image
# pipeline"), produced by scripts/build-crew-bundle.sh and never committed. Local
# paths are uploaded to s3://adp-terraform-state-<account>/dev-box/image-inputs/
# <sha256>/ for the build; s3:// URIs under that prefix (what the CodeBuild bundle
# build publishes) are verified in place and used without a second upload.
#
# Exit codes: 0 image published; 1 build failed; 2 usage / prerequisite missing.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"

ENVIRONMENT="${ADP_ENV:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
IMAGE_REVISION=""
SOURCE_AMI=""
CREW_BUNDLE=""
CREW_MANIFEST=""
PROFILE_REVISION=""
REMOTE_INPUTS=false
BUNDLE_BYTES=""
DRY_RUN=false

usage_error() { echo "build-dev-box-image.sh: $*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)              ENVIRONMENT="${2:?--env requires a value}"; shift 2 ;;
    --region)           AWS_REGION="${2:?--region requires a value}"; shift 2 ;;
    --image-revision)   IMAGE_REVISION="${2:?--image-revision requires a value}"; shift 2 ;;
    --source-ami)       SOURCE_AMI="${2:?--source-ami requires an AMI id}"; shift 2 ;;
    --crew-bundle)      CREW_BUNDLE="${2:?--crew-bundle requires a path}"; shift 2 ;;
    --crew-manifest)    CREW_MANIFEST="${2:?--crew-manifest requires a path}"; shift 2 ;;
    --profile-revision) PROFILE_REVISION="${2:?--profile-revision requires a value}"; shift 2 ;;
    --dry-run)          DRY_RUN=true; shift ;;
    -h|--help)          sed -n '2,27p' "$0"; exit 0 ;;
    *) usage_error "unknown argument $1" ;;
  esac
done

[[ "$ENVIRONMENT" =~ ^[a-z][a-z0-9-]{0,31}$ ]] || usage_error "--env must be a short lowercase name"
[ -z "$IMAGE_REVISION" ] || [[ "$IMAGE_REVISION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || usage_error "--image-revision is not a valid revision"
[ -z "$SOURCE_AMI" ] || [[ "$SOURCE_AMI" =~ ^ami-[0-9a-f]{8,17}$ ]] || usage_error "--source-ami is not an AMI id"
if [ -n "$CREW_BUNDLE" ] || [ -n "$CREW_MANIFEST" ] || [ -n "$PROFILE_REVISION" ]; then
  [ -n "$CREW_BUNDLE" ] && [ -n "$CREW_MANIFEST" ] && [ -n "$PROFILE_REVISION" ] \
    || usage_error "--crew-bundle, --crew-manifest and --profile-revision must be given together"
  [[ "$PROFILE_REVISION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || usage_error "--profile-revision is not a valid revision"
  if [[ "$CREW_BUNDLE" == s3://* ]] || [[ "$CREW_MANIFEST" == s3://* ]]; then
    [[ "$CREW_BUNDLE" == s3://*/crew.tar.gz ]] && [[ "$CREW_MANIFEST" == s3://*/profile.json ]] \
      || usage_error "--crew-bundle and --crew-manifest must both be s3:// URIs (…/crew.tar.gz and …/profile.json) or both local files"
    REMOTE_INPUTS=true
  else
    REMOTE_INPUTS=false
    [ -f "$CREW_BUNDLE" ] || usage_error "Crew bundle not found: $CREW_BUNDLE"
    [ -f "$CREW_MANIFEST" ] || usage_error "Crew profile manifest not found: $CREW_MANIFEST"
    python3 - "$CREW_MANIFEST" "$PROFILE_REVISION" "$CREW_BUNDLE" <<'PY' || usage_error "the Crew profile manifest does not describe the bundle"
import hashlib, json, sys
manifest = json.load(open(sys.argv[1]))
assert manifest.get("schema_version") == 1, "schema_version must be 1"
assert manifest.get("revision") == sys.argv[2], "manifest revision differs from --profile-revision"
digest = hashlib.sha256(open(sys.argv[3], "rb").read()).hexdigest()
assert manifest.get("bundle_sha256") == digest, "bundle_sha256 does not match the bundle"
PY
  fi
fi
command -v aws >/dev/null || usage_error "aws CLI is required"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$AWS_REGION" 2>/dev/null) \
  || usage_error "AWS credentials for the platform account are required"
STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"
SSM_PREFIX="/adp/${ENVIRONMENT}/dev-box"
PROJECT=$(aws ssm get-parameter --region "$AWS_REGION" --name "${SSM_PREFIX}/image-builder-project" --query Parameter.Value --output text 2>/dev/null || true)
[ -n "$PROJECT" ] && [ "$PROJECT" != "None" ] && [ "$PROJECT" != " " ] \
  || usage_error "no image builder project in ${SSM_PREFIX}/image-builder-project; apply modules/user-services/dev-box/infra/platform first (scripts/deploy-dev-box.sh --env ${ENVIRONMENT} does this)"

SOURCE_SHA=$(git -C "$REPO_ROOT" rev-parse HEAD)
OVERRIDES=("name=IMAGE_REVISION,value=${IMAGE_REVISION},type=PLAINTEXT" "name=SOURCE_AMI,value=${SOURCE_AMI},type=PLAINTEXT")
if [ -n "$PROFILE_REVISION" ] && [ "$REMOTE_INPUTS" = true ]; then
  # Inputs already in the state bucket (scripts/build-crew-bundle.sh --codebuild):
  # the manifest must name this profile and the URIs must sit under the digest
  # prefix the Crew buildspec re-verifies against the downloaded bundle.
  BUNDLE_URI="$CREW_BUNDLE"
  MANIFEST_URI="$CREW_MANIFEST"
  REMOTE_MANIFEST=$(mktemp "${TMPDIR:-/tmp}/crew-profile.XXXXXX")
  trap 'rm -f "$REMOTE_MANIFEST"' EXIT
  aws s3 cp --region "$AWS_REGION" "$MANIFEST_URI" "$REMOTE_MANIFEST" >/dev/null 2>&1 || usage_error "cannot read $MANIFEST_URI"
  python3 - "$REMOTE_MANIFEST" "$PROFILE_REVISION" "$BUNDLE_URI" "$MANIFEST_URI" "$STATE_BUCKET" <<'PY' || usage_error "the remote Crew profile manifest does not describe the bundle URI"
import json, re, sys
manifest = json.load(open(sys.argv[1]))
assert manifest.get("schema_version") == 1, "schema_version must be 1"
assert manifest.get("revision") == sys.argv[2], "manifest revision differs from --profile-revision"
digest = manifest.get("bundle_sha256", "")
assert re.fullmatch(r"[0-9a-f]{64}", digest), "manifest bundle_sha256 is not a digest"
prefix = f"s3://{sys.argv[5]}/dev-box/image-inputs/{digest}/"
assert sys.argv[3] == prefix + "crew.tar.gz" and sys.argv[4] == prefix + "profile.json", "the input URIs are not under the digest prefix of this account's state bucket"
PY
  BUNDLE_BYTES=$(aws s3api head-object --region "$AWS_REGION" --bucket "$STATE_BUCKET" --key "${BUNDLE_URI#s3://${STATE_BUCKET}/}" --query ContentLength --output text 2>/dev/null) \
    || usage_error "Crew bundle not found: $BUNDLE_URI"
  OVERRIDES+=("name=CREW_BUNDLE_S3_URI,value=${BUNDLE_URI},type=PLAINTEXT" "name=CREW_MANIFEST_S3_URI,value=${MANIFEST_URI},type=PLAINTEXT"
              "name=PROFILE_REVISION,value=${PROFILE_REVISION},type=PLAINTEXT")
elif [ -n "$PROFILE_REVISION" ]; then
  INPUT_PREFIX="dev-box/image-inputs/$(sha256sum "$CREW_BUNDLE" | cut -d' ' -f1)"
  BUNDLE_BYTES=$(stat -c %s "$CREW_BUNDLE")
  BUNDLE_URI="s3://${STATE_BUCKET}/${INPUT_PREFIX}/crew.tar.gz"
  MANIFEST_URI="s3://${STATE_BUCKET}/${INPUT_PREFIX}/profile.json"
  if [ "$DRY_RUN" = false ]; then
    aws s3 cp --region "$AWS_REGION" "$CREW_BUNDLE" "$BUNDLE_URI" >/dev/null
    aws s3 cp --region "$AWS_REGION" "$CREW_MANIFEST" "$MANIFEST_URI" >/dev/null
  fi
  OVERRIDES+=("name=CREW_BUNDLE_S3_URI,value=${BUNDLE_URI},type=PLAINTEXT" "name=CREW_MANIFEST_S3_URI,value=${MANIFEST_URI},type=PLAINTEXT"
              "name=PROFILE_REVISION,value=${PROFILE_REVISION},type=PLAINTEXT")
fi

echo "build-dev-box-image.sh: project=${PROJECT} source=${SOURCE_SHA} revision=${IMAGE_REVISION:-<default>} crew_profile=${PROFILE_REVISION:-none}"
[ -z "$PROFILE_REVISION" ] || echo "  crew bundle: ${BUNDLE_URI} ($(( ${BUNDLE_BYTES:-0} / 1048576 )) MiB; the Packer build instance fetches it from S3)"
if [ "$DRY_RUN" = true ]; then
  printf '  override %s\n' "${OVERRIDES[@]}"
  echo "dry-run: not starting the build"
  exit 0
fi
STATE_BUCKET="$STATE_BUCKET" AWS_REGION="$AWS_REGION" SOURCE_SHA="$SOURCE_SHA" \
  bash "$REPO_ROOT/platform/scripts/codebuild-run.sh" "$PROJECT" "${OVERRIDES[@]}" || exit 1

IMAGE_ID=$(aws ssm get-parameter --region "$AWS_REGION" --name "${SSM_PREFIX}/image-id" --query Parameter.Value --output text 2>/dev/null || true)
[[ "$IMAGE_ID" =~ ^ami-[0-9a-f]{8,17}$ ]] || { echo "build-dev-box-image.sh: the build finished but ${SSM_PREFIX}/image-id holds no AMI id" >&2; exit 1; }
PUBLISHED_SOURCE=$(aws ssm get-parameter --region "$AWS_REGION" --name "${SSM_PREFIX}/image-source-revision" --query Parameter.Value --output text 2>/dev/null || true)
[ "$PUBLISHED_SOURCE" = "$SOURCE_SHA" ] || { echo "build-dev-box-image.sh: published image-source-revision ${PUBLISHED_SOURCE} is not this build's ${SOURCE_SHA}" >&2; exit 1; }
echo "build-dev-box-image.sh: ${SSM_PREFIX}/image-id=${IMAGE_ID}"
