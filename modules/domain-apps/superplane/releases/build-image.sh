#!/usr/bin/env bash
# Builds only the selected Superplane component from maintained ADP source.
#
# U22 (#5326) transferred the three components into this repository, so the build context is
# an in-repository directory instead of source staged from a repository ADP cannot read.
# The former `releases/source/` staging area and its `.superplane-revision` marker are gone:
# there is nothing to stage when the source is already in the checkout, and a marker file
# asserting a revision was only ever a stand-in for source we could not obtain.
#
# What replaces that marker is a stronger check, because it verifies the thing rather than a
# note about it: the build context must be the maintained directory the LOCK names, it must
# contain that component's Dockerfile, and it must not be the read-only reference snapshot.
#
# ORIGIN_REVISION is provenance, not a fetch target — it labels the image with the upstream
# revision the source was transferred from. IMAGE_TAG is passed separately by the lane,
# because after the transfer the ADP commit is what identifies a rebuild: two ADP commits
# can now produce two different images from the same origin revision, so tagging by origin
# would make the second build silently overwrite the first.
set -euo pipefail
component="${1:?component required}"
case "$component" in
  superplane-api|superplane-controller|superplane-platform-monitor|superplane-executor) ;;
  *) echo "Unknown Superplane component" >&2; exit 1 ;;
esac
[[ "${ORIGIN_REVISION:-}" =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid origin revision" >&2; exit 1; }
expected_source="src/$component"
[[ "$component" != "superplane-executor" ]] || expected_source="executor"
[[ "${SOURCE_PATH:-}" == "$expected_source" ]] || { echo "Invalid source scope" >&2; exit 1; }
[[ "${ECR_REPO:-}" == "adp-$component" ]] || { echo "Invalid ECR scope" >&2; exit 1; }
[[ "${ACCOUNT_ID:-}" =~ ^[0-9]{12}$ ]] || { echo "Invalid target account" >&2; exit 1; }
[[ "${AWS_REGION:-}" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]+$ ]] || { echo "Invalid region" >&2; exit 1; }
[[ "${IMAGE_TAG:-}" =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid image tag" >&2; exit 1; }
expected_registry="$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
[[ "${REGISTRY:-}" == "$expected_registry" ]] || { echo "Registry/account mismatch" >&2; exit 1; }

# The maintained build context. Recomputed here from the module root rather than taken from
# the environment, so a caller cannot redirect this script at an arbitrary directory by
# exporting a different SUPERPLANE_SOURCE_DIR; if the lane passes one, it must agree.
maintained_root="modules/domain-apps/superplane"
context="$maintained_root/$SOURCE_PATH"
[[ "${SUPERPLANE_SOURCE_DIR:-$context}" == "$context" ]] || { echo "Build context does not match the maintained source path" >&2; exit 1; }
[[ -d "$context" && ! -L "$context" ]] || { echo "Maintained source directory missing" >&2; exit 1; }
[[ -f "$context/Dockerfile" && ! -L "$context/Dockerfile" ]] || { echo "Component Dockerfile missing" >&2; exit 1; }
# The reference snapshot is read-only evidence and must never become a build context.
[[ "$context" != *"ai-super-plane"* ]] || { echo "Refusing to build from the reference snapshot" >&2; exit 1; }

build_context="$context"
build_options=()
if [[ "$component" == "superplane-executor" ]]; then
  [[ "${PYTHON_IMAGE:-}" =~ ^[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}$ ]] || { echo "Executor requires an explicitly reviewed digest-pinned Python 3.12 image" >&2; exit 1; }
  # The trusted service consumes two maintained shared packages. Its Dockerfile
  # copies only these three directories from the repository-root context.
  build_context="."
  # This release component runs the installer's long-lived execution service.
  # The Dockerfile also has a paid-worker stage with a different entrypoint.
  build_options=(--file "$context/Dockerfile" --target controller-service --build-arg "PYTHON_IMAGE=$PYTHON_IMAGE")
fi

# Sibling packages are generated build inputs and absent from a clean checkout.
# Stage before touching AWS or Docker so missing maintained source fails locally.
if [[ "$component" == "superplane-api" ]]; then
  bash "$context/scripts/stage-domain-auth.sh"
fi

aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$AWS_REGION" >/dev/null
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
tag="$REGISTRY/$ECR_REPO:$IMAGE_TAG"
# Two labels, kept distinct on purpose: `image.revision` is the ADP commit that produced
# this image (what a rebuild changes), and `source.origin.revision` is the upstream revision
# the source was transferred from (what never changes unless someone re-transfers). Collapse
# them and a reader can no longer tell an ADP change from a re-tag of the same code.
docker build \
  "${build_options[@]}" \
  --label "org.opencontainers.image.revision=$IMAGE_TAG" \
  --label "org.opencontainers.image.source=$maintained_root/$SOURCE_PATH" \
  --label "com.adp.superplane.origin.repository=${ORIGIN_REPOSITORY:-unset}" \
  --label "com.adp.superplane.origin.revision=$ORIGIN_REVISION" \
  -t "$tag" "$build_context"
docker push "$tag"
aws ecr describe-images --repository-name "$ECR_REPO" --image-ids "imageTag=$IMAGE_TAG" --region "$AWS_REGION" --query 'imageDetails[0].imageDigest' --output text
