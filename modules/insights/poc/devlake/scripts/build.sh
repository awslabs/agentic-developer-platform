#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo 'usage: build.sh <full ADP customization commit> <new OCI output directory>' >&2
    exit 2
fi

revision="$1"
exec "$(dirname "$0")/../../../../connectors/devlake/scripts/build.sh" \
    --customization "$revision" --component worker --output "$2"
