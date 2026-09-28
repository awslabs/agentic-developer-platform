#!/bin/sh
# Runs in a fresh ephemeral Fargate HOME. Never print the database URL.
set -e
: "${GBRAIN_DB_HOST:?GBRAIN_DB_HOST is required}"
: "${GBRAIN_DB_USER:?GBRAIN_DB_USER is required}"
: "${GBRAIN_DB_PASSWORD:?GBRAIN_DB_PASSWORD is required}"
GBRAIN_DATABASE_URL=$(node -e '
const e=process.env;
const url=new URL("postgresql://localhost");
url.hostname=e.GBRAIN_DB_HOST;
url.port=e.GBRAIN_DB_PORT||"5432";
url.username=e.GBRAIN_DB_USER;
url.password=e.GBRAIN_DB_PASSWORD;
url.pathname="/"+(e.GBRAIN_DB_NAME||"gbrain");
url.searchParams.set("sslmode",e.GBRAIN_DB_SSLMODE||"require");
process.stdout.write(url.toString());
')
export GBRAIN_DATABASE_URL
gbrain init --non-interactive --force --db-only --url "$GBRAIN_DATABASE_URL" \
  --embedding-model "${GBRAIN_EMBEDDING_MODEL:-litellm:bedrock/amazon.titan-embed-text-v2:0}" \
  --embedding-dimensions "${GBRAIN_EMBEDDING_DIMENSIONS:-1024}"
# dream is a finite batch command. --non-interactive is an init flag, not a
# documented dream option. Respect the configured phases and model safeguards.
exec gbrain dream
