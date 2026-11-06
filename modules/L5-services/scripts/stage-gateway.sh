#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
bash "$repo_root/modules/L5-services/scripts/build-package.sh" "$staging"
target="${1:-$repo_root/modules/gateway/l5-wheel}"
mkdir -p "$target"
rm -f "$target"/adp_l5_services-*.whl "$target/wheel.sha256"
cp "$staging"/adp_l5_services-*.whl "$staging/wheel.sha256" "$target/"
(cd "$target" && sha256sum -c wheel.sha256)
release="$(dirname "$target")/.l5-release"
mkdir -p "$release"
rm -f "$release"/*.whl "$release"/*.whl.sha256
cp "$staging"/adp_l5_services-*.whl "$staging"/adp_l5_services-*.whl.sha256 "$release/"
(cd "$release" && sha256sum -c *.whl.sha256)
bash "$repo_root/modules/gateway/scripts/stage-common-wheel.sh" "$(dirname "$target")/common-wheel"
