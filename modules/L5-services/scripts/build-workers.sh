#!/usr/bin/env bash
set -euo pipefail

module_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
wheel="${1:?Usage: build-workers.sh WHEEL [OUTPUT_DIR_OR_ZIP]}"
output="${2:-$module_dir/dist}"
if [[ "$output" != *.zip ]]; then
    output="$output/l5-security-workers.zip"
fi
wheel="$(realpath "$wheel")"
test -f "$wheel.sha256"
(cd "$(dirname "$wheel")" && sha256sum --check "$(basename "$wheel").sha256")
mkdir -p "$(dirname "$output")"
output="$(realpath -m "$output")"
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
uv pip install --target "$staging/site" --python-version 3.12 --python-platform x86_64-manylinux_2_28 --only-binary :all: --require-hashes -r "$module_dir/requirements-security.lock"
uv pip install --target "$staging/site" --no-deps "$wheel"
for required in \
    l5_services/workers/devops_events.py \
    l5_services/workers/devops_readback.py \
    l5_services/adapters/devops/aws_access.py \
    boto3/__init__.py botocore/__init__.py \
    sqlalchemy/__init__.py asyncpg/__init__.py httpx/__init__.py; do
    if [[ ! -f "$staging/site/$required" ]]; then
        printf 'worker archive is missing %s\n' "$required" >&2
        exit 1
    fi
done
if ! compgen -G "$staging/site/botocore/data/devops-agent/*/service-2.json.gz" > /dev/null; then
    printf 'worker archive is missing the pinned DevOps provider API model\n' >&2
    exit 1
fi
WHEEL="$wheel" STAGING="$staging" OUTPUT="$output" python3 - <<'PY'
import hashlib
import csv
import io
import json
import os
import zipfile
from pathlib import Path, PurePosixPath

wheel = Path(os.environ["WHEEL"])
staging = Path(os.environ["STAGING"])
output = Path(os.environ["OUTPUT"])
manifest = {"wheel_filename": wheel.name, "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}


def runtime_file(name):
    path = PurePosixPath(name)
    # Installation records contain checkout paths and uv cache timestamps.
    # Lambda uses handlers, so runner-specific console script shebangs are
    # not runtime inputs either. Keep package RECORDs accurate below.
    return not (
        name == ".lock" or path.parts[0] == "bin"
        or "__pycache__" in path.parts or path.suffix == ".pyc"
        or path.parent.name.endswith(".dist-info")
        and path.name in {"direct_url.json", "uv_cache.json"}
    )


def entry(archive, name, payload, *, executable=False):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = (0o100755 if executable else 0o100644) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, payload)


with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted((staging / "site").rglob("*")):
        if path.is_file():
            name = path.relative_to(staging / "site").as_posix()
            if not runtime_file(name):
                continue
            payload = path.read_bytes()
            if path.name == "RECORD" and path.parent.name.endswith(".dist-info"):
                record = io.StringIO(newline="")
                csv.writer(record, lineterminator="\n").writerows(
                    row for row in csv.reader(io.StringIO(payload.decode()))
                    if runtime_file(row[0])
                )
                payload = record.getvalue().encode()
            entry(archive, name, payload, executable=bool(path.stat().st_mode & 0o111))
    entry(archive, "release/wheel.json", json.dumps(manifest, sort_keys=True).encode())
    entry(archive, f"release/{wheel.name}", wheel.read_bytes())
PY
(cd "$(dirname "$output")" && sha256sum "$(basename "$output")" > "$(basename "$output").sha256")
