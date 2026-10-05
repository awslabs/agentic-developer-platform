#!/bin/sh
# Run inside the built image as UID1001, network=none, with writable /work.
set -eu
test "$(id -u)" = 1001
test "$(pnpm --version)" = "${EXPECTED_PNPM_VERSION:-12.6.0}"
test "$(yarn --version)" = "${EXPECTED_YARN_VERSION:-4.18.1}"
fixture=$(mktemp -d /work/corepack-fixture.XXXXXX)
trap 'rm -rf "$fixture"' EXIT
mkdir -p "$fixture/dependency" "$fixture/pnpm" "$fixture/yarn"
printf '%s\n' '{"name":"local-boundary-fixture","version":"1.0.0","main":"index.js"}' > "$fixture/dependency/package.json"
printf '%s\n' 'module.exports = 42;' > "$fixture/dependency/index.js"
for manager in pnpm yarn; do
  cd "$fixture/$manager"
  printf '%s\n' '{"name":"offline-consumer","version":"1.0.0","private":true,"dependencies":{"local-boundary-fixture":"file:../dependency"},"scripts":{"check":"node check.js"}}' > package.json
  printf '%s\n' 'if (require("local-boundary-fixture") !== 42) process.exit(1);' > check.js
  if [ "$manager" = pnpm ]; then
    pnpm install --offline --ignore-scripts --store-dir "$fixture/pnpm-store"
    pnpm run check
    pnpm install --offline --frozen-lockfile --ignore-scripts --store-dir "$fixture/pnpm-store"
  else
    export YARN_ENABLE_NETWORK=0 YARN_ENABLE_GLOBAL_CACHE=0
    export YARN_GLOBAL_FOLDER="$fixture/yarn-global"
    yarn install
    yarn run check
    yarn install --immutable
  fi
  node -e 'const fs=require("fs"); const p=JSON.parse(fs.readFileSync("package.json")); p.dependencies["another-local-fixture"]="file:../dependency"; fs.writeFileSync("package.json",JSON.stringify(p));'
  if [ "$manager" = pnpm ]; then
    if pnpm install --offline --frozen-lockfile --ignore-scripts --store-dir "$fixture/pnpm-store" > refusal.log 2>&1; then
      echo 'pnpm accepted a changed manifest with a frozen lockfile' >&2
      exit 1
    fi
    grep -q ERR_PNPM_OUTDATED_LOCKFILE refusal.log
  else
    if yarn install --immutable > refusal.log 2>&1; then
      echo 'Yarn accepted a changed manifest with an immutable lockfile' >&2
      exit 1
    fi
    grep -q YN0028 refusal.log
  fi
done
echo 'PASS: both baked package managers installed and loaded a local dependency offline'
