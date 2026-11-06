#!/usr/bin/env bash
set -euo pipefail

[[ "$#" == 4 ]] || { echo 'Usage: publish-workers.sh WHEEL ARCHIVE BUCKET REVIEWED_SHA' >&2; exit 2; }
wheel="$(realpath "$1")"
archive="$(realpath "$2")"
bucket="$3"
reviewed_sha="$4"
[[ "$reviewed_sha" =~ ^[0-9a-f]{40}$ ]] || { echo 'A reviewed source SHA is required' >&2; exit 2; }
[[ "$bucket" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || { echo 'A private artifact bucket is required' >&2; exit 2; }
(cd "$(dirname "$wheel")" && sha256sum -c "$(basename "$wheel").sha256" >/dev/null)
(cd "$(dirname "$archive")" && sha256sum -c "$(basename "$archive").sha256" >/dev/null)
wheel_hash="$(sha256sum "$wheel" | cut -d' ' -f1)"
archive_hash="$(sha256sum "$archive" | cut -d' ' -f1)"
prefix="l5/security/${reviewed_sha}/${wheel_hash}"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

publish_object() {
    local source="$1" key="$2" version
    version="$(aws s3api put-object --bucket "$bucket" --key "$key" --body "$source" --if-none-match '*' --query VersionId --output text 2>"$stage/put-error")" || version=''
    if [[ -z "$version" || "$version" == None || "$version" == null ]]; then
        grep -qE 'PreconditionFailed|ConditionalRequestConflict|pre-conditions you specified did not hold' "$stage/put-error" \
            || { cat "$stage/put-error" >&2; echo 'Versioned artifact storage is required' >&2; return 1; }
        version="$(aws s3api head-object --bucket "$bucket" --key "$key" --query VersionId --output text)"
    fi
    [[ -n "$version" && "$version" != None && "$version" != null ]] || { echo 'Versioned artifact storage is required' >&2; return 1; }
    aws s3api get-object --bucket "$bucket" --key "$key" --version-id "$version" "$stage/readback" >/dev/null
    cmp --silent "$source" "$stage/readback" || { echo 'Published artifact differs from local bytes' >&2; return 1; }
    printf '%s' "$version"
}

wheel_version="$(publish_object "$wheel" "$prefix/wheel.whl")"
archive_version="$(publish_object "$archive" "$prefix/workers.zip")"
jq -n --arg source_sha "$reviewed_sha" --arg wheel_sha256 "$wheel_hash" --arg archive_sha256 "$archive_hash" \
    --arg wheel_version "$wheel_version" --arg archive_version "$archive_version" \
    '{source_sha:$source_sha,wheel_sha256:$wheel_sha256,archive_sha256:$archive_sha256,wheel_version:$wheel_version,archive_version:$archive_version}' > "$stage/manifest.json"
manifest_version="$(publish_object "$stage/manifest.json" "$prefix/manifest.json")"
printf 'release %s wheel=%s archive=%s manifest=%s\n' "$prefix" "$wheel_version" "$archive_version" "$manifest_version"
echo 'Pinned Security wheel and worker archive published together; Lambda rollout remains a separate explicit action.'
