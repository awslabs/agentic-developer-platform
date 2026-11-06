#!/usr/bin/env bash
# Operator/installer foundation bootstrap. Runtime publication uses deploy.sh.
set -euo pipefail
umask 077
unset PYTHONPATH PYTHONHOME
exec python3 "$(dirname "$0")/bootstrap.py" "$@"
