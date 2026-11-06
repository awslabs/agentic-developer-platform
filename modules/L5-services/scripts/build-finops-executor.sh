#!/usr/bin/env bash
set -euo pipefail

module_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
wheel="${1:?Usage: build-finops-executor.sh WHEEL IMAGE_TAG}"
tag="${2:?Usage: build-finops-executor.sh WHEEL IMAGE_TAG}"
test -f "$wheel.sha256"
(cd "$(dirname "$wheel")" && sha256sum --check "$(basename "$wheel").sha256")
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
mkdir "$staging/wheel"
cp "$wheel" "$staging/wheel/"
cp "$module_dir/infra/finops/Dockerfile.executor" "$staging/Dockerfile"
docker build --platform linux/amd64 --provenance=false -t "$tag" "$staging"
