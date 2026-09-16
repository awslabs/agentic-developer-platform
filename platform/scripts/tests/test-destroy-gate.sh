#!/usr/bin/env bash
# Run behavioral regression tests against the shared saved-plan gate.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 -m unittest discover -s "$SCRIPT_DIR" -p test_update_safety.py -v
