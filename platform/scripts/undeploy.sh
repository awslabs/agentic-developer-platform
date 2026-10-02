#!/bin/bash
# Portable launcher: no Bash 4 arrays or flock dependency.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Help must work without credentials/configuration.
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  exec python3 "$SCRIPT_DIR/teardown.py" "$@"
fi
source "$SCRIPT_DIR/load-deploy-config.sh"
exec python3 "$SCRIPT_DIR/teardown.py" "$@"
