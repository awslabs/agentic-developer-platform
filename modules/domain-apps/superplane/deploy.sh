#!/usr/bin/env bash
set -euo pipefail
SUPERPLANE_MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${SUPERPLANE_MODULE_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m installation "$@"
