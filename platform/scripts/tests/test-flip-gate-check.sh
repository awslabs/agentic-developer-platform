#!/usr/bin/env bash
# Preserve the existing shell entry point for the gate regression suite.
set -euo pipefail
exec python3 -m unittest discover -s "$(dirname "${BASH_SOURCE[0]}")" -p test_credential_binding_readiness.py -v
