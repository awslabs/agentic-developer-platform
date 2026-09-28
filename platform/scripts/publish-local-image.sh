#!/usr/bin/env bash
# Local Docker builds use the same immutable publisher as CodeBuild, from an
# archived commit so a source-SHA tag never describes uncommitted build inputs.
set -euo pipefail
[[ "$#" == 1 ]] || { echo 'Usage: publish-local-image.sh REPOSITORY' >&2; exit 1; }
LOCAL_REPO="${1:?Usage: publish-local-image.sh REPOSITORY}"
case "$LOCAL_REPO" in
  adp-gateway|adp-agent-runtime|adp-chat-agent|adp-agent-gateway) ;;
  *) echo 'Unsupported shared repository' >&2; exit 1 ;;
esac
LOCAL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOCAL_SHA="${SOURCE_SHA:-$(git -C "$LOCAL_ROOT" rev-parse HEAD)}"
[[ "$LOCAL_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo 'Expected a full source SHA' >&2; exit 1; }
[[ "${IMAGE_TAG:-$LOCAL_SHA}" == "$LOCAL_SHA" ]] || { echo 'IMAGE_TAG must match source SHA' >&2; exit 1; }
[[ "${PUBLISH_LATEST:-false}" == false ]] || { echo 'Mutable latest publication is unsupported' >&2; exit 1; }
git -C "$LOCAL_ROOT" cat-file -e "$LOCAL_SHA^{commit}"
LOCAL_ARCHIVE=$(mktemp -d "${TMPDIR:-/tmp}/adp-local-image.XXXXXX")
trap 'rm -rf "$LOCAL_ARCHIVE"' EXIT
git -C "$LOCAL_ROOT" archive "$LOCAL_SHA" | tar -x -C "$LOCAL_ARCHIVE"
echo "Building archived source $LOCAL_SHA with local Docker"
ADP_SOURCE_SHA="$LOCAL_SHA" IMAGE_TAG="$LOCAL_SHA" PUBLISH_LOCAL_BUILD=true PUBLISH_SOURCE_ROOT="$LOCAL_ARCHIVE" \
  bash "$LOCAL_ROOT/platform/scripts/publish-shared-image.sh" "$LOCAL_REPO"
