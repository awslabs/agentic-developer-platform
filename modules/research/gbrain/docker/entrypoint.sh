#!/usr/bin/env sh
# Fargate filesystems are ephemeral: initialize the local brain configuration
# from injected database settings before serving. Never log the connection URL.
set -e

if [ -z "${GBRAIN_DATABASE_URL:-}" ]; then
  : "${GBRAIN_DB_HOST:?GBRAIN_DB_HOST is required}"
  : "${GBRAIN_DB_USER:?GBRAIN_DB_USER is required}"
  : "${GBRAIN_DB_PASSWORD:?GBRAIN_DB_PASSWORD is required}"
  GBRAIN_DATABASE_URL=$(node -e '
    const e = process.env;
    const url = new URL("postgresql://localhost");
    url.hostname = e.GBRAIN_DB_HOST;
    url.port = e.GBRAIN_DB_PORT || "5432";
    url.username = e.GBRAIN_DB_USER;
    url.password = e.GBRAIN_DB_PASSWORD;
    url.pathname = "/" + (e.GBRAIN_DB_NAME || "gbrain");
    url.searchParams.set("sslmode", e.GBRAIN_DB_SSLMODE || "require");
    process.stdout.write(url.toString());
  ')
  export GBRAIN_DATABASE_URL
fi

echo "gbrain entrypoint: initializing brain..."
# Current gbrain accepts --url; the former --config flag is unsupported.
# Failed initialization must stop startup instead of being masked by serve.
# Canonical content belongs in Postgres; task-local files disappear on restart.
# Upstream refuses --db-only when an existing canonical filesystem root is set.
gbrain init --non-interactive --db-only --url "$GBRAIN_DATABASE_URL"

echo "gbrain entrypoint: starting serve..."
# The upstream CLI defaults to loopback. Container peers need an explicit bind.
exec gbrain serve --http --port "${PORT:-3000}" --bind "${GBRAIN_BIND_ADDRESS:-0.0.0.0}"
