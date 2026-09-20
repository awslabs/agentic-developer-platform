#!/usr/bin/env bash
# Prepare account access for the actual Claude runtime defaults, discover other
# ACTIVE Anthropic models, and accept missing Marketplace agreements.
# ADP_BEDROCK_USE_CASE_FILE supplies real first-use registration details when needed.
# --verify checks access and invokes each default once (at most 8 output tokens).
# --check performs read-only access checks; --dry-run makes no AWS calls.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/bedrock-model-access.py" "$@"
