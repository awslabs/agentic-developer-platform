#!/usr/bin/env bash
# =============================================================================
# build-crew-bundle.sh — produce the Kiro Crew bundle (crew.tar.gz) and its
# profile manifest (profile.json) from pinned inputs (#8054).
# =============================================================================
# The bundle is what image/install_crew.py installs under /opt/adp/crew/<rev>
# and what image/crew.pkr.hcl bakes into the Crew AMI. Every input is pinned in
# image/crew-bundle.pins and verified by SHA-256 before use:
#   venv/                  kirocrew wheel + awscli in a venv built against the
#   python/                relocatable CPython 3.12 carried in the bundle (the
#                          AL2023 guest has 3.9 and no package egress)
#   default-agent.json     kiro_crew.config defaults from the installed wheel
#   clients/node_modules   agent/kirocrew-clients (npm ci from the lock file);
#                          .bin/node -> ../../../host/node for the adapter's shebang
#   host/node              bin/node from the pinned Node 22 LTS tarball
#   host/crew-host-*.js    npm run build:host-model / build:host-dashboard /
#                          build:host-bootstrap in modules/agent-factory/agent
#   host/gh, host/git-credential-adp, host/adp-github-token
#                          `gh`/`git` as the owner through the loopback adapter (#8555)
#   host/gh-cli/gh         the pinned GitHub CLI release binary the wrapper execs
#   host/adp/adp + *.py    the owner-bound ADP CLI and MCP shim, run by venv/bin/python3
#   host/adp/adp-skills.tar.gz + skills/  verified shared skills archive and installed source
#   models/*.gguf          the pinned embedding model
# The archive is written deterministically by scripts/crew_bundle.py (sorted
# members, root ownership, commit-time mtimes, gzip without a timestamp) and
# validated with install_crew.py's own checks. profile.json carries exactly
# {schema_version, revision, source_revision, crew_version, bundle_sha256}.
#
# Modes:
#   --codebuild (default)  run image/bundle-buildspec.yml in the module's image
#                          CodeBuild project (the dev box needs neither Node nor
#                          2 GB of downloads); the build uploads the result to
#                          s3://adp-terraform-state-<acct>/dev-box/image-inputs/<sha>/
#                          and publishes crew-bundle-sha256 / crew-bundle-revision
#                          under /adp/<env>/dev-box. profile.json and bundle.env
#                          (URIs, revision, digest) land in --output-dir.
#   --local                assemble here (Linux x86_64 only; used inside the
#                          CodeBuild build and for a developer with the tools).
#   --offline-stage <dir>  TEST ONLY: pack an already assembled staging tree;
#                          nothing is downloaded or built.
#
# Usage:
#   build-crew-bundle.sh [--codebuild|--local] [--env dev] [--region us-east-1]
#                        [--output-dir <dir>]        default .adp-dev-box/<env>/crew-bundle
#                        [--work-dir <dir>]          default <output-dir>/.work
#                        [--revision <rev>]          default crew-<yyyymmdd>-<sha12>
#                        [--source-revision <sha>]   default git HEAD (required without git)
#                        [--source-date-epoch <n>]   default the commit time of the source
#                        [--dry-run]
#
# Exit codes: 0 bundle written/published; 1 a build step failed; 2 usage / prerequisite.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${MODULE_ROOT}/../../.." && pwd)"
PINS_FILE="$MODULE_ROOT/image/crew-bundle.pins"
HELPER="$SCRIPT_DIR/crew_bundle.py"

ENVIRONMENT="${ADP_ENV:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
MODE="codebuild"
OUTPUT_DIR=""
WORK_DIR=""
REVISION=""
SOURCE_REVISION=""
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-}"
OFFLINE_STAGE=""
DRY_RUN=false

usage_error() { echo "build-crew-bundle.sh: $*" >&2; exit 2; }
fail() { echo "build-crew-bundle.sh: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --codebuild)         MODE="codebuild"; shift ;;
    --local)             MODE="local"; shift ;;
    --offline-stage)     MODE="offline"; OFFLINE_STAGE="${2:?--offline-stage requires a directory}"; shift 2 ;;
    --env)               ENVIRONMENT="${2:?--env requires a value}"; shift 2 ;;
    --region)            AWS_REGION="${2:?--region requires a value}"; shift 2 ;;
    --output-dir)        OUTPUT_DIR="${2:?--output-dir requires a path}"; shift 2 ;;
    --work-dir)          WORK_DIR="${2:?--work-dir requires a path}"; shift 2 ;;
    --revision)          REVISION="${2:?--revision requires a value}"; shift 2 ;;
    --source-revision)   SOURCE_REVISION="${2:?--source-revision requires a commit}"; shift 2 ;;
    --source-date-epoch) SOURCE_DATE_EPOCH="${2:?--source-date-epoch requires a number}"; shift 2 ;;
    --dry-run)           DRY_RUN=true; shift ;;
    -h|--help)           sed -n '2,45p' "$0"; exit 0 ;;
    *) usage_error "unknown argument $1" ;;
  esac
done

[[ "$ENVIRONMENT" =~ ^[a-z][a-z0-9-]{0,31}$ ]] || usage_error "--env must be a short lowercase name"
[ -f "$PINS_FILE" ] || usage_error "pins file missing: $PINS_FILE"
# shellcheck disable=SC1090
source "$PINS_FILE"
for pin in CREW_VERSION KIROCREW_WHEEL_URL KIROCREW_WHEEL_SHA256 AWSCLI_VERSION EMBEDDING_MODEL_URL EMBEDDING_MODEL_SHA256 \
           EMBEDDING_MODEL_FILE NODE_VERSION NODE_TARBALL_URL NODE_TARBALL_SHA256 PYTHON_VERSION PYTHON_TARBALL_URL \
           PYTHON_TARBALL_SHA256 GH_VERSION GH_TARBALL_URL GH_TARBALL_SHA256 CLIENTS_DIR AGENT_DIR INSTALL_PREFIX; do
  [ -n "${!pin:-}" ] || usage_error "$pin is not pinned in $PINS_FILE"
done

if [ -z "$SOURCE_REVISION" ]; then
  SOURCE_REVISION=$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)
  [ -n "$SOURCE_REVISION" ] || usage_error "--source-revision is required outside a git checkout"
  if [ -n "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    echo "build-crew-bundle.sh: warning: the checkout has uncommitted changes; the manifest binds ${SOURCE_REVISION:0:12}" >&2
  fi
fi
[[ "$SOURCE_REVISION" =~ ^[0-9a-f]{40}$ ]] || usage_error "--source-revision must be a full commit"
[ -n "$REVISION" ] || REVISION="crew-$(date -u +%Y%m%d)-${SOURCE_REVISION:0:12}"
[[ "$REVISION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || usage_error "--revision is not a valid profile revision"
if [ -z "$SOURCE_DATE_EPOCH" ]; then
  SOURCE_DATE_EPOCH=$(git -C "$REPO_ROOT" show -s --format=%ct "$SOURCE_REVISION" 2>/dev/null || echo 0)
fi
[[ "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]] || usage_error "--source-date-epoch must be a number"
[ -n "$OUTPUT_DIR" ] || OUTPUT_DIR="$REPO_ROOT/.adp-dev-box/$ENVIRONMENT/crew-bundle"
[ -n "$WORK_DIR" ] || WORK_DIR="$OUTPUT_DIR/.work"
mkdir -p "$OUTPUT_DIR"
PYTHON="${ADP_CREW_PYTHON:-python3}"

write_bundle_env() { # write_bundle_env <bundle-ref> <manifest-ref> <sha>
  cat > "$OUTPUT_DIR/bundle.env" <<EOT
CREW_BUNDLE=$1
CREW_MANIFEST=$2
CREW_PROFILE_REVISION=$REVISION
CREW_BUNDLE_SHA256=$3
CREW_SOURCE_REVISION=$SOURCE_REVISION
CREW_VERSION=$CREW_VERSION
EOT
}

pack_stage() { # pack_stage <stage-dir>
  "$PYTHON" "$HELPER" pack --stage "$1" --output-dir "$OUTPUT_DIR" --revision "$REVISION" \
    --source-revision "$SOURCE_REVISION" --crew-version "$CREW_VERSION" --source-date-epoch "$SOURCE_DATE_EPOCH" \
    > "$OUTPUT_DIR/bundle-summary.json" || fail "packing or validating the bundle failed"
  local sha summary
  sha=$(sha256sum "$OUTPUT_DIR/crew.tar.gz" | cut -d' ' -f1)
  write_bundle_env "$OUTPUT_DIR/crew.tar.gz" "$OUTPUT_DIR/profile.json" "$sha"
  summary=$("$PYTHON" - "$OUTPUT_DIR/bundle-summary.json" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
print(f"{s['size_bytes'] / 2**20:.0f} MiB compressed, {s['uncompressed_bytes'] / 2**20:.0f} MiB unpacked, {s['members']} members, pack peak RSS {s['peak_rss_mb']} MiB")
PY
)
  echo "build-crew-bundle.sh: bundle $OUTPUT_DIR/crew.tar.gz sha256=$sha revision=$REVISION source=${SOURCE_REVISION:0:12} ($summary)"
}

# ---------------------------------------------------------------------------
# TEST ONLY: pack a prepared staging tree; no downloads, no npm, no pip.
if [ "$MODE" = offline ]; then
  [ -d "$OFFLINE_STAGE" ] || usage_error "offline stage not found: $OFFLINE_STAGE"
  echo "build-crew-bundle.sh: offline stage mode (test only) — packing $OFFLINE_STAGE without network access"
  pack_stage "$OFFLINE_STAGE"
  exit 0
fi

# ---------------------------------------------------------------------------
if [ "$MODE" = codebuild ]; then
  command -v aws >/dev/null || usage_error "aws CLI is required"
  ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$AWS_REGION" 2>/dev/null) \
    || usage_error "AWS credentials for the platform account are required"
  STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"
  SSM_PREFIX="/adp/${ENVIRONMENT}/dev-box"
  PROJECT=$(aws ssm get-parameter --region "$AWS_REGION" --name "${SSM_PREFIX}/image-builder-project" --query Parameter.Value --output text 2>/dev/null || true)
  [ -n "$PROJECT" ] && [ "$PROJECT" != "None" ] && [ "$PROJECT" != " " ] \
    || usage_error "no image builder project in ${SSM_PREFIX}/image-builder-project; apply modules/user-services/dev-box/infra/platform first"
  OVERRIDES=("name=BUNDLE_REVISION,value=${REVISION},type=PLAINTEXT" "name=SOURCE_DATE_EPOCH,value=${SOURCE_DATE_EPOCH},type=PLAINTEXT")
  # The project is sized for Packer (BUILD_GENERAL1_SMALL, 3 GB); two npm ci runs
  # plus pip need more headroom, so this build alone asks for MEDIUM (7 GB).
  COMPUTE_TYPE="${ADP_CREW_BUNDLE_COMPUTE_TYPE:-BUILD_GENERAL1_MEDIUM}"
  echo "build-crew-bundle.sh: project=${PROJECT} buildspec=image/bundle-buildspec.yml compute=${COMPUTE_TYPE} source=${SOURCE_REVISION} revision=${REVISION}"
  if [ "$DRY_RUN" = true ]; then
    printf '  override %s\n' "${OVERRIDES[@]}"
    echo "dry-run: not starting the bundle build"
    exit 0
  fi
  CODEBUILD_BUILDSPEC_OVERRIDE="modules/user-services/dev-box/image/bundle-buildspec.yml" CODEBUILD_COMPUTE_TYPE_OVERRIDE="$COMPUTE_TYPE" \
    STATE_BUCKET="$STATE_BUCKET" AWS_REGION="$AWS_REGION" SOURCE_SHA="$SOURCE_REVISION" \
    bash "$REPO_ROOT/platform/scripts/codebuild-run.sh" "$PROJECT" "${OVERRIDES[@]}" || exit 1
  PUBLISHED_REVISION=$(aws ssm get-parameter --region "$AWS_REGION" --name "${SSM_PREFIX}/crew-bundle-revision" --query Parameter.Value --output text 2>/dev/null || true)
  [ "$PUBLISHED_REVISION" = "$REVISION" ] || fail "the build finished but ${SSM_PREFIX}/crew-bundle-revision holds '${PUBLISHED_REVISION}', not ${REVISION}"
  BUNDLE_SHA=$(aws ssm get-parameter --region "$AWS_REGION" --name "${SSM_PREFIX}/crew-bundle-sha256" --query Parameter.Value --output text 2>/dev/null || true)
  [[ "$BUNDLE_SHA" =~ ^[0-9a-f]{64}$ ]] || fail "${SSM_PREFIX}/crew-bundle-sha256 holds no digest"
  BUNDLE_URI="s3://${STATE_BUCKET}/dev-box/image-inputs/${BUNDLE_SHA}/crew.tar.gz"
  MANIFEST_URI="s3://${STATE_BUCKET}/dev-box/image-inputs/${BUNDLE_SHA}/profile.json"
  aws s3 cp --region "$AWS_REGION" "$MANIFEST_URI" "$OUTPUT_DIR/profile.json" >/dev/null || fail "cannot download $MANIFEST_URI"
  "$PYTHON" - "$OUTPUT_DIR/profile.json" "$REVISION" "$BUNDLE_SHA" "$SOURCE_REVISION" "$CREW_VERSION" <<'PY' || fail "the published manifest does not describe this build"
import json, sys
manifest = json.load(open(sys.argv[1]))
assert set(manifest) == {"schema_version", "revision", "source_revision", "crew_version", "bundle_sha256"}, "unexpected manifest fields"
assert manifest["schema_version"] == 1 and manifest["revision"] == sys.argv[2], "revision differs"
assert manifest["bundle_sha256"] == sys.argv[3], "bundle digest differs from crew-bundle-sha256"
assert manifest["source_revision"] == sys.argv[4], "source revision differs from this checkout"
assert manifest["crew_version"] == sys.argv[5], "crew_version differs from the pin"
PY
  aws s3api head-object --region "$AWS_REGION" --bucket "$STATE_BUCKET" --key "dev-box/image-inputs/${BUNDLE_SHA}/crew.tar.gz" >/dev/null \
    || fail "$BUNDLE_URI is not in the state bucket"
  write_bundle_env "$BUNDLE_URI" "$MANIFEST_URI" "$BUNDLE_SHA"
  echo "build-crew-bundle.sh: bundle $BUNDLE_URI sha256=$BUNDLE_SHA revision=$REVISION source=${SOURCE_REVISION:0:12}"
  exit 0
fi

# ---------------------------------------------------------------------------
# Local assembly (also what image/bundle-buildspec.yml runs inside CodeBuild).
[ "$(uname -s)" = Linux ] && [ "$(uname -m)" = x86_64 ] || usage_error "--local builds only on Linux x86_64 (the guest's platform); use --codebuild"
for tool in curl tar xz sha256sum python3; do
  command -v "$tool" >/dev/null || usage_error "$tool is required for --local"
done
if [ "$DRY_RUN" = true ]; then
  echo "dry-run: would assemble the Crew bundle in $WORK_DIR from $PINS_FILE and pack it to $OUTPUT_DIR (revision $REVISION)"
  exit 0
fi
STAGE="$WORK_DIR/stage"
DOWNLOADS="$WORK_DIR/downloads"
rm -rf "$STAGE" "$WORK_DIR/node" "$WORK_DIR/agent"
mkdir -p "$STAGE/host" "$STAGE/models" "$STAGE/clients" "$DOWNLOADS" "$WORK_DIR/node"

fetch() { # fetch <url> <sha256> <destination>
  if [ -f "$3" ] && echo "$2  $3" | sha256sum -c --status -; then return 0; fi
  curl --proto '=https' --tlsv1.2 --fail --location --retry 3 --max-time 1800 --silent --show-error --output "$3.part" "$1" \
    || fail "download failed: $1"
  echo "$2  $3.part" | sha256sum -c --status - || fail "checksum mismatch for $1"
  mv -f "$3.part" "$3"
}

echo "build-crew-bundle.sh: [1/6] relocatable CPython ${PYTHON_VERSION}"
fetch "$PYTHON_TARBALL_URL" "$PYTHON_TARBALL_SHA256" "$DOWNLOADS/cpython-${PYTHON_VERSION}.tar.gz"
tar -xzf "$DOWNLOADS/cpython-${PYTHON_VERSION}.tar.gz" -C "$STAGE"   # extracts python/
[ -x "$STAGE/python/bin/python3.12" ] || fail "the CPython tarball does not contain python/bin/python3.12"
PYTHON="$STAGE/python/bin/python3.12"
"$PYTHON" --version

echo "build-crew-bundle.sh: [2/6] Node ${NODE_VERSION} (host/node and the build-time npm)"
fetch "$NODE_TARBALL_URL" "$NODE_TARBALL_SHA256" "$DOWNLOADS/node-v${NODE_VERSION}-linux-x64.tar.xz"
tar -xJf "$DOWNLOADS/node-v${NODE_VERSION}-linux-x64.tar.xz" -C "$WORK_DIR/node" --strip-components=1
NODE_BIN="$WORK_DIR/node/bin"
[ -x "$NODE_BIN/node" ] && [ -e "$NODE_BIN/npm" ] || fail "the Node tarball does not contain bin/node and bin/npm"
install -m 0755 "$NODE_BIN/node" "$STAGE/host/node"
"$STAGE/host/node" --version
export PATH="$NODE_BIN:$PATH"

echo "build-crew-bundle.sh: [3/6] kirocrew ${CREW_VERSION} + awscli ${AWSCLI_VERSION} venv"
fetch "$KIROCREW_WHEEL_URL" "$KIROCREW_WHEEL_SHA256" "$DOWNLOADS/kirocrew-${CREW_VERSION}-py3-none-any.whl"
"$PYTHON" -m venv "$STAGE/venv"
PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1 "$STAGE/venv/bin/python" -m pip install --quiet --no-cache-dir --no-compile \
  "$DOWNLOADS/kirocrew-${CREW_VERSION}-py3-none-any.whl" "awscli==${AWSCLI_VERSION}" || fail "pip install of the pinned wheel failed"
"$STAGE/venv/bin/kirocrew" --version
"$STAGE/venv/bin/python" -c 'import kiro_crew.cli; from importlib.resources import files; from shutil import copyfile; import sys; copyfile(files("kiro_crew.config").joinpath("defaults.json"), sys.argv[1])' "$STAGE/default-agent.json"
"$PYTHON" "$HELPER" normalize-venv --venv "$STAGE/venv" --stage "$STAGE" --install-prefix "$INSTALL_PREFIX" >/dev/null \
  || fail "relocating the venv to $INSTALL_PREFIX failed"

echo "build-crew-bundle.sh: [4/6] Claude clients from ${CLIENTS_DIR} (npm ci)"
cp -f "$REPO_ROOT/$CLIENTS_DIR/package.json" "$REPO_ROOT/$CLIENTS_DIR/package-lock.json" "$STAGE/clients/"
(cd "$STAGE/clients" && npm ci --omit=dev --no-audit --no-fund --loglevel=error) || fail "npm ci of the Crew clients failed"
"$STAGE/clients/node_modules/.bin/claude" --version
"$STAGE/clients/node_modules/.bin/claude-agent-acp" --version
"$STAGE/clients/node_modules/.bin/codex" --version
# The adapter is a "#!/usr/bin/env node" script and the guest has no system Node: expose the
# bundle's own interpreter next to it so any PATH that reaches this bin dir resolves `node`
# (dev 2026-10-20, #8223 cause 31: every Crew turn died with "env: 'node': No such file").
ln -s ../../../host/node "$STAGE/clients/node_modules/.bin/node"
"$STAGE/clients/node_modules/.bin/node" --version >/dev/null || fail "clients/node_modules/.bin/node does not resolve to host/node"
rm -f "$STAGE/clients/package.json" "$STAGE/clients/package-lock.json"

echo "build-crew-bundle.sh: [5/6] host supervisor, dashboard and bootstrap from ${AGENT_DIR}"
mkdir -p "$WORK_DIR/agent"
cp -f "$REPO_ROOT/$AGENT_DIR/package.json" "$REPO_ROOT/$AGENT_DIR/package-lock.json" "$WORK_DIR/agent/"
[ -f "$REPO_ROOT/$AGENT_DIR/tsconfig.json" ] && cp -f "$REPO_ROOT/$AGENT_DIR/tsconfig.json" "$WORK_DIR/agent/"
cp -R "$REPO_ROOT/$AGENT_DIR/src" "$WORK_DIR/agent/src"
(cd "$WORK_DIR/agent" && npm ci --no-audit --no-fund --loglevel=error \
  && npm run --silent build:host-model && npm run --silent build:host-dashboard && npm run --silent build:host-bootstrap) \
  || fail "building the host JavaScript failed"
for artifact in crew-host-model.js crew-host-dashboard.js crew-host-bootstrap.js; do
  install -m 0644 "$WORK_DIR/agent/dist/$artifact" "$STAGE/host/$artifact"
done
# The ADP tools MCP shim (#8380): stdlib-only Python from the gateway CLI, run by the bundle's
# own interpreter (venv/bin/python3) with the siblings it imports. Kiro Crew and Claude Code on
# the box reach ADP tools through it and the loopback model adapter; no `adp login` on the box.
mkdir -p "$STAGE/host/adp"
for helper in adp-mcp.py adp_common.py adp_deployments.py adp_native_test.py adp_native_credential_handoff.py \
  adp-flow.py adp-product.py adp-github.py adp_skills.py; do
  install -m 0644 "$REPO_ROOT/modules/gateway/cli/$helper" "$STAGE/host/adp/$helper"
done
install -m 0755 "$REPO_ROOT/modules/gateway/cli/adp" "$STAGE/host/adp/adp"
bash "$REPO_ROOT/modules/gateway/scripts/build-skills-archive.sh" --build \
  "$REPO_ROOT/modules/agent-factory/skills" "$WORK_DIR" "$REPO_ROOT/modules/gateway/cli/adp"
install -m 0644 "$WORK_DIR/adp-skills.tar.gz" "$STAGE/host/adp/adp-skills.tar.gz"
mkdir -p "$STAGE/host/adp/skills"
tar -xzf "$STAGE/host/adp/adp-skills.tar.gz" -C "$STAGE/host/adp/skills" --transform='s,^skills/,,'
# #8555: `gh` and `git` as the owner. host/ is first on PATH for Kiro Crew, Claude Code and the
# owner shell, so the wrapper shadows /usr/bin/gh; the credential helper is wired system-wide by
# the image (prepare_shell.sh). Both fetch the token from the loopback adapter per call.
install -m 0755 "$REPO_ROOT/modules/agent-factory/agent-worker-image/adp_cred/devbox.py" "$STAGE/host/adp-cred"
for helper in adp-github-token gh git-credential-adp; do
  install -m 0755 "$REPO_ROOT/$AGENT_DIR/host/$helper" "$STAGE/host/$helper"
done
# The real GitHub CLI the wrapper execs (host/gh-cli/gh): the build instance cannot reach
# cli.github.com's RPM repository, so the pinned release binary ships in the bundle.
fetch "$GH_TARBALL_URL" "$GH_TARBALL_SHA256" "$DOWNLOADS/gh_${GH_VERSION}_linux_amd64.tar.gz"
mkdir -p "$STAGE/host/gh-cli"
tar -xzf "$DOWNLOADS/gh_${GH_VERSION}_linux_amd64.tar.gz" -C "$STAGE/host/gh-cli" --strip-components=2 "gh_${GH_VERSION}_linux_amd64/bin/gh"
[ -x "$STAGE/host/gh-cli/gh" ] || fail "the gh tarball does not contain bin/gh"
chmod 0755 "$STAGE/host/gh-cli/gh"
rm -f "$DOWNLOADS/gh_${GH_VERSION}_linux_amd64.tar.gz"
# The agent's node_modules (~0.5 GB) and the Node distribution are build-time
# only; free the scratch before the model lands so nothing is kept twice.
rm -rf "$WORK_DIR/agent" "$WORK_DIR/node"

echo "build-crew-bundle.sh: [6/6] embedding model"
# Fetched straight into the stage: no second 600 MB copy under downloads/.
fetch "$EMBEDDING_MODEL_URL" "$EMBEDDING_MODEL_SHA256" "$STAGE/models/$EMBEDDING_MODEL_FILE"
chmod 0644 "$STAGE/models/$EMBEDDING_MODEL_FILE"
rm -f "$DOWNLOADS/cpython-${PYTHON_VERSION}.tar.gz" "$DOWNLOADS/node-v${NODE_VERSION}-linux-x64.tar.xz"

pack_stage "$STAGE"
