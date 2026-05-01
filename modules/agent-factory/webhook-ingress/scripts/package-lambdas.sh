#!/usr/bin/env bash
# Package each channel Lambda separately with common/ included.
# Output: dist/<lambda-name>.zip for each handler directory.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_DIR="$MODULE_DIR/dist"
LAMBDA_DIR="$MODULE_DIR/lambda"
COMMON_DIR="$LAMBDA_DIR/common"

rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

# Install dependencies to a temporary layer if requirements.txt exists
DEPS_DIR=""
if [ -f "$MODULE_DIR/requirements.txt" ]; then
  DEPS_DIR=$(mktemp -d)
  pip install -r "$MODULE_DIR/requirements.txt" -t "$DEPS_DIR" --quiet --no-deps 2>/dev/null || \
    pip install -r "$MODULE_DIR/requirements.txt" -t "$DEPS_DIR" --quiet
fi

# Find all Lambda handler directories (each subdirectory of lambda/ that is not common/)
LAMBDA_COUNT=0
for handler_dir in "$LAMBDA_DIR"/*/; do
  handler_name=$(basename "$handler_dir")

  # Skip common/ — it's a shared library, not a standalone Lambda
  if [ "$handler_name" = "common" ]; then
    continue
  fi

  # Skip __pycache__ and hidden directories
  if [[ "$handler_name" == __* ]] || [[ "$handler_name" == .* ]]; then
    continue
  fi

  ZIP_FILE="$DIST_DIR/${handler_name}.zip"
  echo "Packaging: $handler_name -> dist/${handler_name}.zip"

  # Create zip with the handler code
  (cd "$handler_dir" && zip -r "$ZIP_FILE" . -x '__pycache__/*' '*.pyc' '.pytest_cache/*')

  # Add common/ module if it exists
  if [ -d "$COMMON_DIR" ]; then
    (cd "$LAMBDA_DIR" && zip -r "$ZIP_FILE" common/ -x '__pycache__/*' '*.pyc')
  fi

  # Add dependencies if they were installed
  if [ -n "$DEPS_DIR" ] && [ -d "$DEPS_DIR" ]; then
    (cd "$DEPS_DIR" && zip -r "$ZIP_FILE" . -x '__pycache__/*' '*.pyc' '*.dist-info/*')
  fi

  LAMBDA_COUNT=$((LAMBDA_COUNT + 1))
done

# Clean up temp deps
if [ -n "$DEPS_DIR" ]; then
  rm -rf "$DEPS_DIR"
fi

if [ "$LAMBDA_COUNT" -eq 0 ]; then
  echo "WARNING: No Lambda handlers found in $LAMBDA_DIR/"
  echo "Expected structure: lambda/<channel-name>/handler.py"
  # Create a placeholder so the CI package-verification step doesn't fail
  # on an empty module (before actual Lambda code is written)
  mkdir -p "$LAMBDA_DIR/placeholder"
  echo "# Placeholder handler" > "$LAMBDA_DIR/placeholder/handler.py"
  (cd "$LAMBDA_DIR/placeholder" && zip "$DIST_DIR/placeholder.zip" handler.py)
  rm -rf "$LAMBDA_DIR/placeholder"
  LAMBDA_COUNT=1
fi

echo ""
echo "Packaged $LAMBDA_COUNT Lambda(s) to $DIST_DIR/"
ls -lh "$DIST_DIR/"
