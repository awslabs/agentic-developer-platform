#!/usr/bin/env bash
set -euo pipefail
# Compatibility entrypoint; Python uses argv arrays and never prints passwords.
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bootstrap-admin.py" "$@"
