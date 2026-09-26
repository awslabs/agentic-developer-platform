#!/bin/sh
# Run inside the final image with network disabled, read-only root and private /tmp.
set -eu
export HOME=/tmp/npm-fixture-home
export npm_config_cache=/tmp/npm-fixture-cache
export npm_config_userconfig=/tmp/npm-userconfig
export npm_config_globalconfig=/tmp/npm-globalconfig
mkdir -p "$HOME" /tmp/npm-fixture/source /tmp/npm-fixture/consumer
: > "$npm_config_userconfig"
: > "$npm_config_globalconfig"
cd /tmp/npm-fixture/source
cat > package.json <<'JSON'
{"name":"adp-offline-npm-fixture","version":"1.0.0","main":"index.js","bin":{"adp-offline-npm-fixture":"cli.js"}}
JSON
printf 'module.exports = "package-loaded";\n' > index.js
printf '#!/usr/bin/env node\nconsole.log("fixture-executed");\n' > cli.js
chmod +x cli.js
npm pack --offline --ignore-scripts
cd /tmp/npm-fixture/consumer
printf '{"name":"fixture-consumer","version":"1.0.0"}\n' > package.json
npm install --offline --ignore-scripts ../source/adp-offline-npm-fixture-1.0.0.tgz
node -e 'if(require("adp-offline-npm-fixture") !== "package-loaded") process.exit(1)'
npm exec --offline -- adp-offline-npm-fixture
npm ci --offline --ignore-scripts
node -e 'if(require("adp-offline-npm-fixture") !== "package-loaded") process.exit(1)'
node --version
npm --version
cd /app
node dist/health-server.js > /tmp/health-server.log 2>&1 &
health_pid=$!
trap 'kill "$health_pid" 2>/dev/null || true' EXIT INT TERM
for attempt in 1 2 3 4 5; do
    if wget -qO /tmp/health.json http://127.0.0.1:8765/health; then
        cat /tmp/health.json
        exit 0
    fi
    sleep 1
done
cat /tmp/health-server.log
exit 1
