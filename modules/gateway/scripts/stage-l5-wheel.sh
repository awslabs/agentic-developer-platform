#!/usr/bin/env bash
set -euo pipefail

gateway_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
module_dir="$(cd "$gateway_dir/../L5-services" && pwd)"
stage="$gateway_dir/.l5-release"
if [[ "${1:-}" == "--clean" ]]; then
    rm -r "$stage"
    exit 0
fi
if ! command -v uv >/dev/null; then
    python3 -m pip install --user uv==0.13.5
    export PATH="$(python3 -m site --user-base)/bin:$PATH"
fi
mkdir -p "$stage"
rm -f "$stage"/*.whl "$stage"/*.whl.sha256
if [[ -n "${1:-}" ]]; then
    wheel="$(realpath "$1")"
    (cd "$(dirname "$wheel")" && sha256sum -c "$(basename "$wheel").sha256")
    cp "$wheel" "$wheel.sha256" "$stage/"
else
    wheel="$("$module_dir/scripts/build-package.sh" "$stage" | tail -1)"
fi
(cd "$stage" && sha256sum -c "$(basename "$wheel").sha256")
target="$gateway_dir/l5-wheel"
mkdir -p "$target"
rm -f "$target"/*.whl "$target/wheel.sha256"
cp "$stage"/*.whl "$target/"
cp "$stage/$(basename "$wheel").sha256" "$target/wheel.sha256"
(cd "$target" && sha256sum -c wheel.sha256)
bash "$gateway_dir/scripts/stage-common-wheel.sh"
