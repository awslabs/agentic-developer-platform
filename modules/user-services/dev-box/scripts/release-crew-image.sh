#!/usr/bin/env bash
# =============================================================================
# release-crew-image.sh — one command from pinned inputs to a pinned Crew AMI
# release (#8054): bundle → Crew image → read-back → release pin → stage.
# =============================================================================
#   [1/4] bundle     scripts/build-crew-bundle.sh (CodeBuild by default) publishes
#                    crew.tar.gz + profile.json under s3://…/dev-box/image-inputs/<sha>/
#   [2/4] image      scripts/build-dev-box-image.sh --crew-bundle --crew-manifest
#                    --profile-revision builds base + Crew AMIs (image/buildspec.yml)
#                    and publishes /adp/<env>/dev-box/{image-id,image-revision,
#                    image-source-revision,base-image-id,profile-revision}
#   [3/4] read-back  the published image-id must be tagged adp:profile-revision =
#                    the requested profile, adp:image-revision = image-revision and
#                    adp:source-revision = this source (release admission rejects
#                    an AMI whose profile tag differs)
#   [4/4] pin        scripts/crew_release_pin.py prints the release pin — SSM
#                    image-id / image-revision / profile-revision / crew-model-id
#                    and the environments/<env>/modules/dev-box.tfvars diff
#                    (crew_model_id). Plan only unless --pin or --apply.
#                    --pin writes them and journals the release; --apply also runs
#                    scripts/deploy-dev-box.sh --skip-image --skip-verify so the
#                    foundation, release manifest, controller and seed pick up the
#                    new image (then launch a box: POST /cloud-desktops/ensure).
#
# Usage:
#   release-crew-image.sh --env dev --crew-model-id <approved gateway model>
#                         [--region us-east-1] [--profile-revision <rev>] [--image-revision <rev>]
#                         [--bundle-mode codebuild|local]  (default codebuild)
#                         [--skip-bundle] [--bundle-dir <dir>]   reuse <dir>/bundle.env from an earlier run
#                         [--skip-image]                         re-pin the AMI already in SSM image-id
#                         [--account-id <12 digits>]             refuse any other caller account
#                         [--tfvars <file>] [--journal <file>] [--plan-output <file>]
#                         [--pin] [--apply] [--dry-run]
#
# --dry-run passes through to both build scripts (no CodeBuild run) and plans the
# pin from whatever SSM currently holds. The self-managed rollout on dev is:
#   release-crew-image.sh --env dev --crew-model-id <id> --apply
# The ADP-managed workflow (.github/workflows/dev-box-crew-image.yml) runs the same
# script with --pin under the connection identity and uploads the plan.
#
# Exit codes: 0 done; 1 a step failed or the read-back refused the image; 2 usage.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${MODULE_ROOT}/../../.." && pwd)"

ENVIRONMENT="${ADP_ENV:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
CREW_MODEL_ID=""
PROFILE_REVISION=""
IMAGE_REVISION=""
BUNDLE_MODE="codebuild"
SKIP_BUNDLE=false
BUNDLE_DIR=""
SKIP_IMAGE=false
EXPECTED_ACCOUNT=""
TFVARS=""
JOURNAL=""
PLAN_OUTPUT=""
PIN=false
APPLY=false
DRY_RUN=false

usage_error() { echo "release-crew-image.sh: $*" >&2; exit 2; }
fail() { echo "release-crew-image.sh: $*" >&2; exit 1; }
step() { echo; echo "━━━ Crew image release $1 ━━━"; echo; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)              ENVIRONMENT="${2:?--env requires a value}"; shift 2 ;;
    --region)           AWS_REGION="${2:?--region requires a value}"; shift 2 ;;
    --crew-model-id)    CREW_MODEL_ID="${2:?--crew-model-id requires a model id}"; shift 2 ;;
    --profile-revision) PROFILE_REVISION="${2:?--profile-revision requires a value}"; shift 2 ;;
    --image-revision)   IMAGE_REVISION="${2:?--image-revision requires a value}"; shift 2 ;;
    --bundle-mode)      BUNDLE_MODE="${2:?--bundle-mode requires codebuild or local}"; shift 2 ;;
    --skip-bundle)      SKIP_BUNDLE=true; shift ;;
    --bundle-dir)       BUNDLE_DIR="${2:?--bundle-dir requires a path}"; shift 2 ;;
    --skip-image)       SKIP_IMAGE=true; shift ;;
    --account-id)       EXPECTED_ACCOUNT="${2:?--account-id requires a value}"; shift 2 ;;
    --tfvars)           TFVARS="${2:?--tfvars requires a path}"; shift 2 ;;
    --journal)          JOURNAL="${2:?--journal requires a path}"; shift 2 ;;
    --plan-output)      PLAN_OUTPUT="${2:?--plan-output requires a path}"; shift 2 ;;
    --pin)              PIN=true; shift ;;
    --apply)            PIN=true; APPLY=true; shift ;;
    --dry-run)          DRY_RUN=true; shift ;;
    -h|--help)          sed -n '2,42p' "$0"; exit 0 ;;
    *) usage_error "unknown argument $1" ;;
  esac
done

[[ "$ENVIRONMENT" =~ ^[a-z][a-z0-9-]{0,31}$ ]] || usage_error "--env must be a short lowercase name"
[ -n "$CREW_MODEL_ID" ] || usage_error "--crew-model-id is required (the approved gateway model for the Crew host supervisor)"
[[ "$CREW_MODEL_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$ ]] || usage_error "--crew-model-id is not a model id"
[ -z "$PROFILE_REVISION" ] || [[ "$PROFILE_REVISION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || usage_error "--profile-revision is not a valid revision"
[ -z "$IMAGE_REVISION" ] || [[ "$IMAGE_REVISION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || usage_error "--image-revision is not a valid revision"
[ "$BUNDLE_MODE" = codebuild ] || [ "$BUNDLE_MODE" = local ] || usage_error "--bundle-mode must be codebuild or local"
[ -z "$EXPECTED_ACCOUNT" ] || [[ "$EXPECTED_ACCOUNT" =~ ^[0-9]{12}$ ]] || usage_error "--account-id must be 12 digits"
[ "$SKIP_IMAGE" = false ] || SKIP_BUNDLE=true
for tool in aws python3 git; do command -v "$tool" >/dev/null || usage_error "$tool is required"; done
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION"

SSM_PREFIX="/adp/${ENVIRONMENT}/dev-box"
WORK_DIR="$REPO_ROOT/.adp-dev-box/$ENVIRONMENT"
[ -n "$BUNDLE_DIR" ] || BUNDLE_DIR="$WORK_DIR/crew-bundle"
[ -n "$TFVARS" ] || TFVARS="$REPO_ROOT/environments/$ENVIRONMENT/modules/dev-box.tfvars"
[ -n "$JOURNAL" ] || JOURNAL="$WORK_DIR/release/crew-image-journal.jsonl"
SOURCE_SHA=$(git -C "$REPO_ROOT" rev-parse HEAD)
[ -n "$PROFILE_REVISION" ] || PROFILE_REVISION="crew-$(date -u +%Y%m%d)-${SOURCE_SHA:0:12}"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || usage_error "AWS credentials for the platform account are required"
[ -z "$EXPECTED_ACCOUNT" ] || [ "$ACCOUNT_ID" = "$EXPECTED_ACCOUNT" ] || usage_error "the caller resolves to account $ACCOUNT_ID, not --account-id $EXPECTED_ACCOUNT"
if [ -f "$TFVARS" ]; then
  TFVARS_ACCOUNT=$(sed -nE 's/^[[:space:]]*account_id[[:space:]]*=[[:space:]]*"?([^"#]*)"?.*$/\1/p' "$TFVARS" | head -1 | tr -d '[:space:]')
  [ -z "$TFVARS_ACCOUNT" ] || [ "$TFVARS_ACCOUNT" = "$ACCOUNT_ID" ] \
    || usage_error "$TFVARS account_id=$TFVARS_ACCOUNT but the caller resolves to $ACCOUNT_ID; switch AWS_PROFILE or fix the tfvars"
fi
get_ssm() { aws ssm get-parameter --region "$AWS_REGION" --name "$1" --query Parameter.Value --output text 2>/dev/null || true; }
ssm_or_empty() { local v; v=$(get_ssm "$1"); [ "$v" != "None" ] && [ "$v" != " " ] && printf '%s' "$v" || printf ''; }

echo "release-crew-image.sh: env=$ENVIRONMENT account=$ACCOUNT_ID region=$AWS_REGION source=${SOURCE_SHA:0:12} profile=$PROFILE_REVISION model=$CREW_MODEL_ID mode=$([ "$APPLY" = true ] && echo apply || { [ "$PIN" = true ] && echo pin || echo plan; })$([ "$DRY_RUN" = true ] && echo ' (dry-run)')"

# ---------------------------------------------------------------------------
step "[1/4] Bundle"
BUNDLE_ENV="$BUNDLE_DIR/bundle.env"
if [ "$SKIP_BUNDLE" = true ]; then
  if [ -f "$BUNDLE_ENV" ]; then
    echo "Reusing $BUNDLE_ENV"
  else
    [ "$SKIP_IMAGE" = true ] || usage_error "--skip-bundle needs $BUNDLE_ENV from an earlier run (or --bundle-dir)"
    echo "Skipping the bundle (--skip-image re-pins the published image)"
  fi
else
  BUNDLE_ARGS=(--env "$ENVIRONMENT" --region "$AWS_REGION" --output-dir "$BUNDLE_DIR" --revision "$PROFILE_REVISION" --source-revision "$SOURCE_SHA")
  if [ "$BUNDLE_MODE" = local ]; then BUNDLE_ARGS+=(--local); else BUNDLE_ARGS+=(--codebuild); fi
  [ "$DRY_RUN" = false ] || BUNDLE_ARGS+=(--dry-run)
  bash "$SCRIPT_DIR/build-crew-bundle.sh" "${BUNDLE_ARGS[@]}" || fail "the Crew bundle build failed (see above)"
fi
CREW_BUNDLE=""; CREW_MANIFEST=""; CREW_BUNDLE_SHA256=""
if [ -f "$BUNDLE_ENV" ]; then
  # shellcheck disable=SC1090
  source "$BUNDLE_ENV"
  [ "${CREW_PROFILE_REVISION:-}" = "$PROFILE_REVISION" ] || fail "$BUNDLE_ENV describes profile ${CREW_PROFILE_REVISION:-?}, not $PROFILE_REVISION (pass --profile-revision ${CREW_PROFILE_REVISION:-} or rebuild)"
  [ "${CREW_SOURCE_REVISION:-}" = "$SOURCE_SHA" ] || echo "release-crew-image.sh: warning: the bundle was built from ${CREW_SOURCE_REVISION:-?}, this checkout is $SOURCE_SHA" >&2
  echo "Bundle: $CREW_BUNDLE (sha256 ${CREW_BUNDLE_SHA256:-?})"
elif [ "$DRY_RUN" = true ] && [ "$SKIP_IMAGE" = false ]; then
  echo "dry-run: no bundle.env yet; the image step is planned with placeholder inputs"
  CREW_BUNDLE="s3://adp-terraform-state-${ACCOUNT_ID}/dev-box/image-inputs/<bundle-sha>/crew.tar.gz"
  CREW_MANIFEST="s3://adp-terraform-state-${ACCOUNT_ID}/dev-box/image-inputs/<bundle-sha>/profile.json"
fi

# ---------------------------------------------------------------------------
step "[2/4] Crew image (base + derived AMI in CodeBuild)"
if [ "$SKIP_IMAGE" = true ]; then
  echo "Skipping the image build (--skip-image); pinning the AMI already published at $SSM_PREFIX/image-id"
else
  [ -n "$CREW_BUNDLE" ] && [ -n "$CREW_MANIFEST" ] || fail "no bundle inputs to build the image from"
  IMAGE_ARGS=(--env "$ENVIRONMENT" --region "$AWS_REGION" --crew-bundle "$CREW_BUNDLE" --crew-manifest "$CREW_MANIFEST" --profile-revision "$PROFILE_REVISION")
  [ -z "$IMAGE_REVISION" ] || IMAGE_ARGS+=(--image-revision "$IMAGE_REVISION")
  if [ "$DRY_RUN" = true ]; then
    echo "dry-run: bash $SCRIPT_DIR/build-dev-box-image.sh ${IMAGE_ARGS[*]} --dry-run"
    [ -f "$BUNDLE_ENV" ] && bash "$SCRIPT_DIR/build-dev-box-image.sh" "${IMAGE_ARGS[@]}" --dry-run || true
  else
    bash "$SCRIPT_DIR/build-dev-box-image.sh" "${IMAGE_ARGS[@]}" || fail "the Crew image build failed (see the CodeBuild log above)"
  fi
fi

# ---------------------------------------------------------------------------
step "[3/4] Read-back of the published image"
IMAGE_ID=$(ssm_or_empty "$SSM_PREFIX/image-id")
PUBLISHED_REVISION=$(ssm_or_empty "$SSM_PREFIX/image-revision")
PUBLISHED_PROFILE=$(ssm_or_empty "$SSM_PREFIX/profile-revision")
PUBLISHED_SOURCE=$(ssm_or_empty "$SSM_PREFIX/image-source-revision")
if [ "$DRY_RUN" = true ] && ! [[ "$IMAGE_ID" =~ ^ami-[0-9a-f]{8,17}$ ]]; then
  echo "dry-run: $SSM_PREFIX/image-id holds no AMI yet; the pin below uses placeholders"
  IMAGE_ID="ami-00000000"; PUBLISHED_REVISION="${IMAGE_REVISION:-base-00000000-000000000000}-crew-${PROFILE_REVISION}"; PUBLISHED_PROFILE="$PROFILE_REVISION"; PUBLISHED_SOURCE="$SOURCE_SHA"
else
  [[ "$IMAGE_ID" =~ ^ami-[0-9a-f]{8,17}$ ]] || fail "$SSM_PREFIX/image-id holds no AMI id"
  [ "$PUBLISHED_PROFILE" = "$PROFILE_REVISION" ] || fail "$SSM_PREFIX/profile-revision is '${PUBLISHED_PROFILE:-<absent>}', not $PROFILE_REVISION: the published image is not the requested Crew image"
  [[ "$PUBLISHED_REVISION" == *"-crew-${PROFILE_REVISION}" ]] || fail "$SSM_PREFIX/image-revision '$PUBLISHED_REVISION' was not derived with profile $PROFILE_REVISION"
  if [ "$SKIP_IMAGE" = false ]; then
    [ "$PUBLISHED_SOURCE" = "$SOURCE_SHA" ] || fail "$SSM_PREFIX/image-source-revision $PUBLISHED_SOURCE is not this checkout's $SOURCE_SHA"
  fi
  TAGS_JSON=$(aws ec2 describe-images --region "$AWS_REGION" --image-ids "$IMAGE_ID" --query 'Images[0].Tags' --output json 2>/dev/null) \
    || fail "cannot describe $IMAGE_ID in $AWS_REGION"
  python3 - "$TAGS_JSON" "$PROFILE_REVISION" "$PUBLISHED_REVISION" "$PUBLISHED_SOURCE" <<'PY' || fail "AMI tags differ from the published pin; refusing to release $IMAGE_ID"
import json, sys
tags = {tag["Key"]: tag["Value"] for tag in (json.loads(sys.argv[1]) or [])}
expected = {"adp:profile-revision": sys.argv[2], "adp:image-revision": sys.argv[3], "adp:source-revision": sys.argv[4], "adp:component": "dev-box"}
mismatch = {key: (tags.get(key), value) for key, value in expected.items() if tags.get(key) != value}
if mismatch:
    print("tag mismatch (actual, expected): " + json.dumps(mismatch, sort_keys=True), file=sys.stderr)
    sys.exit(1)
PY
  echo "Image $IMAGE_ID: revision $PUBLISHED_REVISION, profile $PUBLISHED_PROFILE, source ${PUBLISHED_SOURCE:0:12} (tags verified)"
fi

# ---------------------------------------------------------------------------
step "[4/4] Release pin"
PIN_ARGS=(--env "$ENVIRONMENT" --region "$AWS_REGION" --image-id "$IMAGE_ID" --image-revision "$PUBLISHED_REVISION"
  --profile-revision "$PROFILE_REVISION" --crew-model-id "$CREW_MODEL_ID" --source-revision "$PUBLISHED_SOURCE"
  --tfvars "$TFVARS" --journal "$JOURNAL")
[ -z "${CREW_BUNDLE_SHA256:-}" ] || PIN_ARGS+=(--bundle-sha256 "$CREW_BUNDLE_SHA256")
[ -z "$PLAN_OUTPUT" ] || PIN_ARGS+=(--plan-output "$PLAN_OUTPUT")
if [ "$PIN" = true ] && [ "$DRY_RUN" = false ]; then
  python3 "$SCRIPT_DIR/crew_release_pin.py" "${PIN_ARGS[@]}" --apply || fail "writing the release pin failed"
else
  python3 "$SCRIPT_DIR/crew_release_pin.py" "${PIN_ARGS[@]}" || fail "planning the release pin failed"
  [ "$PIN" = false ] || echo "dry-run: the pin above was not written"
fi

if [ "$APPLY" = true ]; then
  STAGE_CMD=(bash "$SCRIPT_DIR/deploy-dev-box.sh" --env "$ENVIRONMENT" --region "$AWS_REGION" --skip-image --skip-verify)
  if [ "$DRY_RUN" = true ]; then
    echo "dry-run: ${STAGE_CMD[*]}"
  else
    "${STAGE_CMD[@]}" || fail "the Dev Box stage did not apply the pinned image; fix the step it names and re-run with --skip-image --apply"
    echo "release-crew-image.sh: Crew image $IMAGE_ID ($PUBLISHED_REVISION, profile $PROFILE_REVISION, model $CREW_MODEL_ID) is live on $ENVIRONMENT"
    echo "  next: launch a box (POST /cloud-desktops/ensure, profile crew) and confirm crew_ready=true; python3 $SCRIPT_DIR/verify-dev-box.py --env $ENVIRONMENT --require-crew"
  fi
elif [ "$PIN" = true ] && [ "$DRY_RUN" = false ]; then
  echo "release-crew-image.sh: pinned; run $SCRIPT_DIR/deploy-dev-box.sh --env $ENVIRONMENT --skip-image --skip-verify (or re-run with --apply) to roll it out"
else
  echo "release-crew-image.sh: plan only; re-run with --pin to write the pin or --apply to also run the stage"
fi
