#!/usr/bin/env bash
# Read-only preconditions for #3186. The workflow alone owns any actual flip.
set -euo pipefail
exec python3 "$(dirname "${BASH_SOURCE[0]}")/credential-binding-readiness.py" "$@"
