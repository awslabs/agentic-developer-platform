#!/usr/bin/env bash
set -euo pipefail

module_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_dir="$(cd "$module_dir/../.." && pwd)"
output_dir="${1:-$module_dir/dist}"
mkdir -p "$output_dir"
output_dir="$(cd "$output_dir" && pwd)"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-315532800}"

cp "$module_dir/pyproject.toml" "$stage/"
cp -a "$module_dir/src" "$stage/"
mkdir -p "$stage/src/l5_services/contracts/v1"
test -f "$repo_dir/docs/l5-services/contracts/v1/schemas.json"
test -f "$repo_dir/docs/l5-services/contracts/v1/openapi.json"
for contract in "$repo_dir"/docs/l5-services/contracts/v1/*; do
    case "$contract" in
        *.json|*.tsv|*.md) cp "$contract" "$stage/src/l5_services/contracts/v1/" ;;
    esac
done

uv build --wheel --out-dir "$output_dir" "$stage"
wheel="$output_dir/adp_l5_services-$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$module_dir/pyproject.toml")-py3-none-any.whl"
test -f "$wheel"
(cd "$output_dir" && sha256sum "${wheel##*/}" > "${wheel##*/}.sha256")
cp "$wheel.sha256" "$output_dir/wheel.sha256"
printf '%s\n' "$wheel"
