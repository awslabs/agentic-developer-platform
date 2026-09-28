#!/usr/bin/env bash
set -euo pipefail
# Repository root is the only build context. Publishing is an explicit operation.
repo_uri=${1:?Usage: build-image.sh ECR_REPOSITORY_URI [--push] [--browser]}
shift
mode=
browser=false
for argument in "$@"; do
  case "$argument" in
    --push) [[ -z "$mode" ]] || exit 2; mode=--push ;;
    --browser) [[ "$browser" == false ]] || exit 2; browser=true ;;
    *) exit 2 ;;
  esac
done
[[ "$repo_uri" =~ ^([0-9]{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/([a-z0-9_./-]+)$ ]] || { echo 'Expected an ECR repository URI without a tag' >&2; exit 2; }
build_account=${BASH_REMATCH[1]}
build_region=${BASH_REMATCH[2]}
build_repository=${BASH_REMATCH[3]}
repo_root=$(cd "$(dirname "$0")/../../../.." && pwd)
source_sha=${ADP_SOURCE_SHA:-$(git -C "$repo_root" rev-parse HEAD)}
[[ "$source_sha" =~ ^[a-f0-9]{40}$ ]] || { echo "Build source must be a reviewed full SHA" >&2; exit 2; }
if [[ -d "$repo_root/.git" ]]; then
  [[ "$(git -C "$repo_root" rev-parse HEAD)" == "$source_sha" ]] || { echo "Build source differs from checkout" >&2; exit 2; }
fi
image_tag="source-${source_sha}"
dockerfile=Dockerfile
if [[ "$browser" == true ]]; then image_tag="${image_tag}-browser"; dockerfile=Dockerfile.browser; fi
image_ref="${repo_uri}:${image_tag}"
if [[ "$mode" == --push ]]; then
  if [[ -d "$repo_root/.git" ]]; then
    [[ -z "$(git -C "$repo_root" status --porcelain)" ]] || { echo 'Publish requires a clean committed checkout' >&2; exit 2; }
  else
    [[ -n "${ADP_SOURCE_SHA:-}" ]] || { echo 'CodeBuild requires source-archive SHA' >&2; exit 2; }
  fi
  actual_account=$(aws sts get-caller-identity --query Account --output text)
  [[ "$actual_account" == "$build_account" ]] || { echo 'ECR account differs from active AWS identity' >&2; exit 2; }
  aws ecr get-login-password --region "$build_region" | docker login --username AWS --password-stdin "${repo_uri%%/*}"
fi
docker build --platform linux/amd64 --provenance=false --label "org.opencontainers.image.revision=$source_sha" \
  -f "$repo_root/modules/tools/agentcore/$dockerfile" -t "$image_ref" "$repo_root"
if [[ "$mode" == --push ]]; then
  docker push "$image_ref"
  image_digest=$(aws ecr describe-images --region "$build_region" --repository-name "$build_repository" \
    --image-ids "imageTag=$image_tag" --query 'imageDetails[0].imageDigest' --output text)
  [[ "$image_digest" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo 'ECR did not confirm image digest' >&2; exit 1; }
  printf '%s@%s\n' "$repo_uri" "$image_digest"
fi
