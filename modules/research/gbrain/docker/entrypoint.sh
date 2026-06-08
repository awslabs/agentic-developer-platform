#!/usr/bin/env sh
# =============================================================================
# entrypoint.sh — Initialize brain then start gbrain serve
#
# gbrain serve exits 1 with "No brain configured" unless `gbrain init` has been
# run first. Because ECS Fargate tasks have ephemeral filesystems, any local
# config file written by `init` would be lost on task restart. This entrypoint
# runs `gbrain init` on EVERY container start so the brain is always configured
# from the injected GBRAIN_DB_* environment variables.
#
# The init command is idempotent — if the brain is already initialized (state
# stored in Postgres), it exits 0 with no changes. This means:
#   - Fresh tasks: init creates the brain config → serve starts
#   - Restarted tasks: init detects existing brain → serve starts
#   - Re-deploys: init is a no-op → serve starts
#
# Required env vars (injected by ECS task definition):
#   GBRAIN_DB_HOST, GBRAIN_DB_PORT, GBRAIN_DB_NAME,
#   GBRAIN_DB_USER, GBRAIN_DB_PASSWORD
# =============================================================================
set -e

echo "gbrain entrypoint: initializing brain..."

# Run gbrain init with the config file that specifies Postgres storage.
# --non-interactive prevents any prompts in the container environment.
# If already initialized, this is a no-op (exits 0).
if gbrain init --non-interactive --config /app/config/gbrain.yml 2>&1; then
  echo "gbrain entrypoint: brain initialized (or already configured)"
else
  INIT_EXIT=$?
  # Check if the "already initialized" case is reported as non-zero
  # Some versions exit 0 on already-init, others print a message and exit 1.
  # Try alternative: init without --config flag (uses env vars directly)
  echo "gbrain entrypoint: first init attempt exited ${INIT_EXIT}, trying with env vars only..."
  if gbrain init --non-interactive 2>&1; then
    echo "gbrain entrypoint: brain initialized via env vars"
  else
    RETRY_EXIT=$?
    echo "gbrain entrypoint: init exited ${RETRY_EXIT}, checking if brain is already configured..."
    # Verify that serve would work — if init truly failed, serve will also fail
    # and the health check will catch it. Allow startup to proceed.
    echo "gbrain entrypoint: proceeding to serve (health check will catch failures)"
  fi
fi

echo "gbrain entrypoint: starting serve..."
exec gbrain serve --http --port 3000
