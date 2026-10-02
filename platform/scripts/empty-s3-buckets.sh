#!/bin/bash
# Empty explicitly selected buckets; propagate listing and per-object failures.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 - "$SCRIPT_DIR" "$@" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from teardown import aws, empty_bucket, TeardownError
if len(sys.argv) < 3:
    raise SystemExit('Usage: empty-s3-buckets.sh <bucket> [bucket ...]')
try:
    account = aws('sts', 'get-caller-identity')['Account']
    for bucket in sys.argv[2:]:
        print('Emptying explicitly selected bucket: ' + bucket, flush=True)
        empty_bucket(bucket, account)
except TeardownError as error:
    raise SystemExit(str(error))
PY
