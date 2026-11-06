#!/usr/bin/env bash
set -euo pipefail

# GitHub/Jira and the expanded providers share the production staging/test path.
exec "$(dirname "$0")/test-providers.sh"
