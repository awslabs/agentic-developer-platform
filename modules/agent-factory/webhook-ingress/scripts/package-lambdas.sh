#!/usr/bin/env bash
# Package each channel Lambda separately with common/ (and any in-process
# modules) included.
# Output: dist/<lambda-name>.zip for each STANDALONE handler directory.
#
# Module layout (issue #2203):
#   - common/      — shared library, bundled into EVERY zip; never standalone.
#   - <channel>/   — a standalone Lambda function (e.g. github/) → its own zip
#                    deployed as adp-<env>-<channel>-webhook.
#   - in-process modules — handler code invoked IN-PROCESS by another Lambda
#                    (not its own AWS function). These must be BUNDLED into the
#                    owning Lambda's zip and must NOT be emitted as a standalone
#                    zip, or the deploy pipeline's update-code step will try to
#                    update a function (adp-<env>-<name>-webhook) that doesn't
#                    exist → ResourceNotFoundException.
#                    `eventbridge/` is such a module: github/handler.py routes
#                    EventBridge events to it via `from eventbridge.handler
#                    import handle_eventbridge` (issue #2154); there is no
#                    separate eventbridge Lambda — it reuses github-webhook.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_DIR="$MODULE_DIR/dist"
LAMBDA_DIR="$MODULE_DIR/lambda"
COMMON_DIR="$LAMBDA_DIR/common"

# In-process modules: bundled into EVERY standalone Lambda zip (like common/),
# never emitted as their own zip. Add a dir here when its handler is invoked
# in-process by another Lambda rather than deployed as its own function.
IN_PROCESS_MODULES=("eventbridge")

# Returns 0 if "$1" is an in-process module (should be bundled, not standalone).
_is_in_process_module() {
  local name="$1"
  local m
  for m in "${IN_PROCESS_MODULES[@]}"; do
    [ "$name" = "$m" ] && return 0
  done
  return 1
}

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

  # Skip in-process modules — they're bundled into standalone zips below,
  # never deployed as their own Lambda function (issue #2203).
  if _is_in_process_module "$handler_name"; then
    echo "Skipping standalone zip for in-process module: $handler_name (bundled into owning Lambda)"
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

  # Add in-process modules (e.g. eventbridge/) so handlers that import them
  # in-process (github/handler.py → eventbridge.handler) resolve at runtime.
  for module in "${IN_PROCESS_MODULES[@]}"; do
    if [ -d "$LAMBDA_DIR/$module" ]; then
      (cd "$LAMBDA_DIR" && zip -r "$ZIP_FILE" "$module/" \
        -x '__pycache__/*' '*.pyc' "$module/tests/*" '*/tests/*')
    fi
  done

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
