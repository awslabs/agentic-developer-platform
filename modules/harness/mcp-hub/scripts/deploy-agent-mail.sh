#!/usr/bin/env bash
# Digest-only Agent Mail deployment. Manifests are operator supplied; none are
# bundled in this repository. Dry-run renders locally and invokes no cloud tools.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)
exec python3 "$ROOT/modules/harness/mcp-hub/scripts/deploy_agent_mail.py" "$@"
