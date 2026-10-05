#!/usr/bin/env bash
set -euo pipefail
# Repository root is the only build context. Publishing is an explicit operation.
repo_uri=${1:?Usage: build-image.sh ECR_REPOSITORY_URI [--push]}
mode=${2:-}
[[ $# -le 2 && ( -z "$mode" || "$mode" == --push ) ]] || exit 2
[[ "$repo_uri" =~ ^([0-9]{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/([a-z0-9_./-]+)$ ]] || { echo 'Expected an ECR repository URI without a tag' >&2; exit 2; }
build_account=${BASH_REMATCH[1]}
build_region=${BASH_REMATCH[2]}
build_repository=${BASH_REMATCH[3]}
repo_root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
source_sha=$(git -C "$repo_root" rev-parse HEAD)
image_tag="source-${source_sha}"
image_ref="${repo_uri}:${image_tag}"
if [[ "$mode" == --push ]]; then
  [[ -z "$(git -C "$repo_root" status --porcelain)" ]] || { echo 'Publish requires a clean committed checkout' >&2; exit 2; }
  actual_account=$(aws sts get-caller-identity --query Account --output text)
  [[ "$actual_account" == "$build_account" ]] || { echo 'ECR account differs from active AWS identity' >&2; exit 2; }
  aws ecr get-login-password --region "$build_region" | docker login --username AWS --password-stdin "${repo_uri%%/*}"
fi
docker build --platform linux/amd64 --provenance=false --label "org.opencontainers.image.revision=$source_sha" \
  -f "$repo_root/modules/domain-apps/cyber/tools/Dockerfile" -t "$image_ref" "$repo_root"
if [[ "$mode" == --push ]]; then
  docker push "$image_ref"
  image_digest=$(aws ecr describe-images --region "$build_region" --repository-name "$build_repository" \
    --image-ids "imageTag=$image_tag" --query 'imageDetails[0].imageDigest' --output text)
  [[ "$image_digest" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo 'ECR did not confirm image digest' >&2; exit 1; }
  printf '%s@%s\n' "$repo_uri" "$image_digest"
fi
