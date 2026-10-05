#!/usr/bin/env bash
# Stage the canonical reviewed source into the factory Docker build context.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FACTORY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SOURCE_DIR="$(cd "$FACTORY_DIR/../gateway/security/stdlib" && pwd)"
if [[ -L "$FACTORY_DIR/security" || -L "$FACTORY_DIR/security/stdlib" ]]; then
  echo 'Refusing a symlinked generated security directory' >&2
  exit 1
fi
rm -rf "$FACTORY_DIR/security/stdlib"
mkdir -p "$FACTORY_DIR/security/stdlib"
for name in PSF-LICENSE.txt README.md apply.py check.py manifest-3.13.16.json manifest.json cpython-3.13.15.patch; do
  cp "$SOURCE_DIR/$name" "$FACTORY_DIR/security/stdlib/$name"
  cmp "$SOURCE_DIR/$name" "$FACTORY_DIR/security/stdlib/$name"
done
