#!/usr/bin/env bash
# Stage the canonical shared receiver library into the Gateway build context.
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
target="${1:-$repo_root/modules/gateway/common-wheel}"
mkdir -p "$target"
rm -f "$target"/adp_common-*.whl "$target/wheel.sha256"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-315532800}"
uv build --wheel --out-dir "$target" "$repo_root/libs/python/adp-common"
(cd "$target" && sha256sum adp_common-*.whl > wheel.sha256 && sha256sum -c wheel.sha256)
