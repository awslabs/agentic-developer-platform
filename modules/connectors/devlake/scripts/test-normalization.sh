#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/../../../.." && pwd)
pg_bin=${DEVLAKE_TEST_PG_BIN:-}
if [[ -z "$pg_bin" ]] && command -v pg_config >/dev/null; then
  pg_bin=$(pg_config --bindir)
fi
for binary in initdb pg_ctl postgres; do
  if [[ ! -x "$pg_bin/$binary" ]]; then
    echo "Set DEVLAKE_TEST_PG_BIN to a PostgreSQL bin directory (initdb, pg_ctl, postgres)." >&2
    exit 1
  fi
done
if [[ $(id -u) == 0 ]]; then
  echo "Run this disposable PostgreSQL test as a non-root user." >&2
  exit 1
fi

# Never use an existing database or a TCP listener. Trust authentication is only
# for this private, disposable Unix-socket cluster; this is not an IAM test.
test_tmp=$(mktemp -d /tmp/devlake-normalization.XXXXXX)
cleanup() {
  if [[ -s "$test_tmp/data/postmaster.pid" ]]; then
    "$pg_bin/pg_ctl" -D "$test_tmp/data" -m immediate -w -t 15 stop >/dev/null
  fi
  rm -rf "$test_tmp"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -m 700 "$test_tmp/socket"
"$pg_bin/initdb" -D "$test_tmp/data" -U devlake_test_admin --auth-local=trust --auth-host=reject --no-locale --encoding=UTF8 >"$test_tmp/initdb.log"
"$pg_bin/pg_ctl" -D "$test_tmp/data" -l "$test_tmp/postgres.log" \
  -o "-k $test_tmp/socket -c listen_addresses='' -c shared_buffers=32MB -c max_connections=20" \
  -w -t 15 start >/dev/null

"$root/modules/connectors/devlake/scripts/build.sh" \
  --customization "$(git -C "$root" rev-parse HEAD)" \
  --destination "$test_tmp/source"
python3 - "$root" "$test_tmp/source" <<'PYTHON'
import importlib.util
from pathlib import Path
import subprocess
import sys
root, source = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location("devlake_build", root / "modules/connectors/devlake/scripts/build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)
revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
build.stage(build.read_lock(build.ROOT / "source-lock.json"), source, revision, "worker")
PYTHON
cp "$root/modules/connectors/devlake/normalization/"*_test.go "$test_tmp/source/backend/adp/normalization/"
cp -R "$root/modules/connectors/devlake/normalization/testdata" "$test_tmp/source/backend/adp/normalization/"
(cd "$test_tmp/source/backend" && \
  DEVLAKE_TEST_PG_SOCKET="$test_tmp/socket" GOMAXPROCS="${GOMAXPROCS:-2}" CGO_ENABLED=0 \
  go test -mod=readonly -p 2 -timeout 2m ./adp/normalization -count=1 -v)
