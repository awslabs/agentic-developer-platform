#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/../../../.." && pwd)
source_tmp=$(mktemp -d)
trap 'rm -rf "$source_tmp"' EXIT

"$root/modules/connectors/devlake/scripts/build.sh" \
  --customization "$(git -C "$root" rev-parse HEAD)" \
  --destination "$source_tmp/source"
# Use production staging so an adapter omitted from the image cannot pass only
# because a test runner silently installed it.
python3 - "$root" "$source_tmp/source" <<'PYTHON'
import importlib.util
from pathlib import Path
import subprocess
import sys
root, source = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location("devlake_build", root / "modules/connectors/devlake/scripts/build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)
revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
build.stage(build.read_lock(build.ROOT / "source-lock.json"), source, revision, "api")
PYTHON
cp "$root/modules/connectors/devlake/providers/"*_test.go "$source_tmp/source/backend/adp/providers/"
cp "$root/modules/connectors/devlake/cmd/connector-api/"*_test.go "$source_tmp/source/backend/cmd/adp-connector-api/"
(cd "$source_tmp/source/backend" && GOMAXPROCS="${GOMAXPROCS:-2}" CGO_ENABLED=0 go test -mod=readonly -p 2 ./adp/providers ./cmd/adp-connector-api -count=1)

# Optional Gateway integration uses this same prepared production source. Supply
# the pytest executable from the Gateway test environment; no live services run.
if [[ -n "${DEVLAKE_PROFILE_PYTEST:-}" ]]; then
  (cd "$source_tmp/source/backend" && GOMAXPROCS="${GOMAXPROCS:-2}" CGO_ENABLED=0 go build -mod=readonly -p 2 -o "$source_tmp/connector-api" ./cmd/adp-connector-api)
  (cd "$root/modules/gateway" && DEVLAKE_CONNECTOR_BINARY="$source_tmp/connector-api" \
    PYTHONPATH="$root/modules/L5-services/src:$root/libs/python/adp-common/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$DEVLAKE_PROFILE_PYTEST" tests/products/test_jenkins_profile.py -q)
fi
