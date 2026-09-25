#!/bin/sh
#
# install.sh — install the `adp` CLI (Issue #4852, Phase 1).
#
# Installs `adp` plus the two files it wraps — `bg-cognito-auth.sh` (the auth
# core) and `bg-gateway-proxy.py` (the Codex auth proxy) — SIDE BY SIDE into
# ~/.adp/bin, because `adp` resolves both as siblings of itself rather than via
# PATH. ~/.adp/bin is what goes on PATH. ~/bin is deliberately never touched, so
# an existing hand-installed bg-cognito-auth.sh keeps working untouched.
#
# Primary path (the /setup page renders this with its own origin filled in):
#
#   curl -fsSL https://<gateway>/api/cli/install.sh | sh -s -- --gateway-url https://<gateway>
#
# The download route serves a STATIC file, so this script cannot be templated
# per-request — the gateway URL is passed in and persisted to
# ~/.bedrock-gateway/config.json (key `gateway_url`), the same key
# bg-cognito-auth.sh already reads. That is what lets `adp login` take no flags
# on first run and `adp update` know where to re-pull from.
#
# POSIX sh, not bash: the documented install line pipes into `sh`, which ignores
# this shebang, and on Debian-family systems /bin/sh is dash. So: no arrays, no
# [[ ]], no BASH_SOURCE, no `echo -e`.
#
# Usage:
#   ./install.sh --gateway-url https://gw.example.com   # from a repo checkout
#   curl -fsSL <gw>/api/cli/install.sh | sh -s -- --gateway-url <gw>
#   ./install.sh --prefix /path/to/bin                  # custom install dir
#   ./install.sh --uninstall
#
# Requirements: curl, jq, python3. Deliberately NOT the aws CLI — ordinary gateway users
# hold no AWS credentials, and the point of the gateway-routed refresh (#4846) is
# that they need none. The previous version of this script installed the
# deprecated bg-auth.sh and hard-required `aws`.

set -eu

VERSION="2.0.0"
DEFAULT_INSTALL_DIR="${HOME}/.adp/bin"

# The files that must land side by side.
ADP_SCRIPT="adp"
CORE_SCRIPT="bg-cognito-auth.sh"
PROXY_SCRIPT="bg-gateway-proxy.py"
CLI_FILES="adp bg-cognito-auth.sh bg-gateway-proxy.py adp_common.py adp_deployments.py adp-admin.py adp-bedrock.py adp-aws.py adp-github.py adp-github-admin.py adp-superplane.py adp-superplane-onboarding.py adp-models.py adp-flow.py adp-doctor.py command-manifest.json adp-usage.py adp-agent.py adp-task.py adp_task_client.py"

# The auth store this install writes its gateway URL into.
#
# BG_CONFIG_DIR is honoured because `adp update` runs this script as a child and
# exports the SELECTED deployment's pin (Issue #5413). Without that, an
# `adp --deployment prod update` rewrote the LEGACY store's gateway_url to prod's
# URL while leaving the legacy refresh token in place beside it — so the next
# refresh sent one deployment's credential to another deployment's gateway. A
# standalone `curl | sh` install has no pin and keeps the original default.
CONFIG_DIR="${BG_CONFIG_DIR:-${HOME}/.bedrock-gateway}"
CONFIG_FILE="${CONFIG_DIR}/config.json"

# Issue #5039: the version this install is PINNED to. When set, the CLI version
# that actually lands must equal it or nothing is installed — an install that
# silently delivered a different version than the one asked for is the failure
# mode being closed here. Recorded in the manifest so `adp update --to <v>` and
# `--rollback --to <v>` can be version-explicit rather than "one generation back".
VERSION_PIN="${ADP_VERSION_PIN:-}"
MANIFEST_FILE_NAME=".adp-manifest.json"

INSTALL_DIR="${DEFAULT_INSTALL_DIR}"
GATEWAY_URL="${ADP_GATEWAY_URL:-}"
UNINSTALL=false
NO_PATH_EDIT=false
# Set by `adp update`: keep the outgoing copies as *.prev so `adp update
# --rollback` has something to restore.
KEEP_PREVIOUS="${ADP_KEEP_PREVIOUS:-0}"

log_info() { printf '[INFO] %s\n' "$*" >&2; }
log_success() { printf '[OK] %s\n' "$*" >&2; }
log_warn() { printf '[WARN] %s\n' "$*" >&2; }
log_error() { printf '[ERROR] %s\n' "$*" >&2; }

usage() {
    cat << EOF
install.sh v${VERSION} — install the adp CLI

Usage: install.sh [OPTIONS]

Options:
  --gateway-url URL   Your gateway's base URL (or set ADP_GATEWAY_URL).
                      Persisted so 'adp login' and 'adp update' need no flags.
  --prefix DIR        Install directory (default: ${DEFAULT_INSTALL_DIR})
  --uninstall         Remove the installed files
  --no-path-edit      Do not touch your shell rc; just print the PATH line
  -h, --help          Show this message
  -v, --version       Show the installer version

Examples:
  curl -fsSL https://gw.example.com/api/cli/install.sh | sh -s -- \\
      --gateway-url https://gw.example.com

  # Prefer to read it first? Download, inspect, then run:
  curl -fsSL https://gw.example.com/api/cli/install.sh -o install.sh
  less install.sh
  sh install.sh --gateway-url https://gw.example.com
EOF
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --gateway-url)
                if [ -z "${2:-}" ]; then log_error "--gateway-url requires a URL"; exit 1; fi
                GATEWAY_URL="$2"; shift 2 ;;
            --prefix)
                if [ -z "${2:-}" ]; then log_error "--prefix requires a directory"; exit 1; fi
                INSTALL_DIR="$2"; shift 2 ;;
            --version-pin)
                if [ -z "${2:-}" ]; then log_error "--version-pin requires a version"; exit 1; fi
                VERSION_PIN="$2"; shift 2 ;;
            --uninstall) UNINSTALL=true; shift ;;
            --no-path-edit) NO_PATH_EDIT=true; shift ;;
            -h|--help) usage; exit 0 ;;
            -v|--version) echo "install.sh v${VERSION}"; exit 0 ;;
            *) log_error "Unknown option: $1"; usage; exit 1 ;;
        esac
    done
}

check_dependencies() {
    missing=""
    command -v curl >/dev/null 2>&1 || missing="curl"
    command -v jq >/dev/null 2>&1 || missing="${missing:+${missing} }jq"
    command -v python3 >/dev/null 2>&1 || missing="${missing:+${missing} }python3"
    if [ -n "${missing}" ]; then
        log_error "Missing required dependencies: ${missing}"
        log_info "Install them with your package manager (e.g. 'brew install ${missing}' or 'sudo apt install ${missing}')."
        exit 1
    fi
}

# Directory this script lives in, or empty when it has no on-disk source — which
# is the `curl | sh` case, where the script arrives on stdin and the files must
# be fetched from the gateway instead of copied from a checkout.
script_dir() {
    case "$0" in
        ""|sh|bash|dash|-*|/dev/fd/*|/proc/self/fd/*) return 0 ;;
    esac
    [ -f "$0" ] || return 0
    (cd -P "$(dirname "$0")" && pwd)
}

# The gateway serves everything the CLI needs under an /api prefix
# (/api/cli/* here, /api/auth/cli/* for login + refresh). bg-cognito-auth.sh
# reads the stored gateway_url and appends /auth/... to it directly, so the
# canonical form MUST end in /api. Accept the URL with OR without it — a bare
# https://<gateway> is exactly what the /setup page and this file's own examples
# show — and normalize to the /api form. Without this a bare URL breaks two
# things: the download below 403s (or, worse, the SPA fallback returns index.html
# with a 200 and we would install HTML as `adp`), and the bare URL then gets
# persisted, so `adp login` afterwards also hits the wrong path.
normalize_gateway_url() {
    [ -z "${GATEWAY_URL}" ] && return 0
    GATEWAY_URL="${GATEWAY_URL%/}"
    case "${GATEWAY_URL}" in
        */api) : ;;
        *) GATEWAY_URL="${GATEWAY_URL}/api" ;;
    esac
}

# Resolve the gateway URL: flag/env, else the one a previous install persisted.
# Never guessed — a wrong origin would silently install a CLI pointed at another
# deployment.
resolve_gateway_url() {
    if [ -z "${GATEWAY_URL}" ] && [ -f "${CONFIG_FILE}" ]; then
        GATEWAY_URL=$(jq -r '.gateway_url // empty' "${CONFIG_FILE}" 2>/dev/null || true)
    fi
    normalize_gateway_url
}

require_gateway_url() {
    if [ -n "${GATEWAY_URL}" ]; then return 0; fi
    log_error "No gateway URL supplied and none stored."
    log_info "Re-run with your gateway's URL — the /setup page shows this line with it filled in:"
    printf '\n    curl -fsSL https://<gateway>/api/cli/install.sh | sh -s -- --gateway-url https://<gateway>\n\n' >&2
    exit 1
}

# Check before staging binaries: a reinstall may refresh an existing binding,
# but must never place an old deployment's credentials beside a new endpoint.
check_existing_binding() {
    [ -e "${CONFIG_FILE}" ] || [ -e "${CONFIG_DIR}/tokens.json" ] || return 0
    if ! python3 - "${CONFIG_FILE}" "${GATEWAY_URL}" <<'PY'
import json, sys
from urllib.parse import urlsplit

def canonical(value):
    url = urlsplit(value)
    if not url.hostname or url.scheme not in ("https", "http") or url.username or url.password or url.query or url.fragment:
        raise ValueError("invalid binding")
    port = url.port or (443 if url.scheme == "https" else 80)
    path = url.path.rstrip("/")
    if not path.endswith("/api"):
        path += "/api"
    return url.scheme.lower(), url.hostname.lower(), port, path

try:
    with open(sys.argv[1]) as config:
        existing = json.load(config)["gateway_url"]
    if canonical(existing) != canonical(sys.argv[2]):
        raise ValueError("different binding")
except (OSError, ValueError, KeyError, TypeError):
    sys.exit(1)
PY
    then
        log_error "Existing deployment state cannot be rebound by the installer. Nothing was installed."
        log_info "Use 'adp deployment add <name> --url <gateway>' for a different deployment."
        exit 1
    fi
}

# Persist the URL under the key bg-cognito-auth.sh already reads, so a first
# `adp login` needs no --gateway-url. MERGES rather than overwrites: an existing
# config carries a live session's client_id / refresh_via, and clobbering those
# would break refresh for a user who is merely updating.
persist_gateway_url() {
    mkdir -p "${CONFIG_DIR}"
    chmod 700 "${CONFIG_DIR}"

    existing='{}'
    if [ -f "${CONFIG_FILE}" ]; then
        existing=$(jq '.' "${CONFIG_FILE}" 2>/dev/null || echo '{}')
    fi

    tmp=$(mktemp "${CONFIG_FILE}.XXXXXX")
    printf '%s' "${existing}" | jq --arg url "${GATEWAY_URL}" '.gateway_url = $url' > "${tmp}"
    chmod 600 "${tmp}"
    mv -f "${tmp}" "${CONFIG_FILE}"
}

# Space-separated list of temp files staged this run. cleanup_staged removes any
# that survive an early exit, so a failed install leaves nothing behind — never a
# usable `adp` without the core script it depends on.
STAGED_TMPS=""

cleanup_staged() {
    for f in ${STAGED_TMPS}; do
        rm -f "${f}"
    done
}

# Reject a "download" that isn't actually one of our artifacts. A misrouted request
# (e.g. the gateway URL missing its /api prefix) can come back as the SPA's
# index.html with a 200, which curl -f happily accepts — installing that as `adp`
# is the partial/broken install we are guarding against. Scripts require a
# shebang; the checked command manifest requires its versioned JSON shape.
validate_staged() {
    name="$1"; tmp="$2"
    if [ ! -s "${tmp}" ]; then
        log_error "Downloaded ${name} is empty — refusing to install a broken copy."
        exit 1
    fi
    if [ "${name}" = "command-manifest.json" ]; then
        if ! jq -e '
            type == "object"
            and ((.schema_version | type) == "string")
            and ((.schema_version | length) > 0)
            and ((.commands | type) == "array")
            and ((.commands | length) > 0)
        ' "${tmp}" >/dev/null 2>&1; then
            log_error "Downloaded ${name} is not a valid command manifest. Nothing was installed."
            exit 1
        fi
        return 0
    fi
    first_line=$(head -n 1 "${tmp}" 2>/dev/null || true)
    case "${first_line}" in
        "#!"*) : ;;
        *)
            log_error "Downloaded ${name} is not a script — the gateway URL is likely wrong or missing its /api path. Nothing was installed."
            exit 1 ;;
    esac
}

# Fetch one file into a temp (from the repo checkout when we are running inside
# one, else from the gateway's download route) and validate it. Registers the
# temp so cleanup_staged can reclaim it if a later file fails.
stage_file() {
    name="$1"
    tmp="${INSTALL_DIR}/.${name}.tmp.$$"
    STAGED_TMPS="${STAGED_TMPS} ${tmp}"
    src_dir=$(script_dir)

    if [ -n "${src_dir}" ] && [ -f "${src_dir}/${name}" ]; then
        cp -f "${src_dir}/${name}" "${tmp}"
    else
        require_gateway_url
        url="${GATEWAY_URL}/cli/${name}"
        if ! curl -fsSL "${url}" -o "${tmp}"; then
            log_error "Could not download ${name} from ${url}"
            exit 1
        fi
        validate_staged "${name}" "${tmp}"
    fi

    # Only the entrypoints need +x; the proxy is run as `python3 <file>`.
    case "${name}" in
        "${ADP_SCRIPT}"|"${CORE_SCRIPT}") chmod 755 "${tmp}" ;;
        *) chmod 644 "${tmp}" ;;
    esac
}

# Move a previously staged temp into its final place. Only run once every file
# has staged successfully, so the install lands all-or-nothing.
commit_file() {
    name="$1"
    target="${INSTALL_DIR}/${name}"
    tmp="${INSTALL_DIR}/.${name}.tmp.$$"

    if [ "${KEEP_PREVIOUS}" = "1" ] && [ -f "${target}" ]; then
        cp -f "${target}" "${target}.prev"
    fi
    mv -f "${tmp}" "${target}"
    log_success "Installed ${target}"
}

# The ADP_VERSION the STAGED `adp` declares — read from the staged temp, not the
# installed copy, so the pin is checked against what would land.
staged_adp_version() {
    sed -n 's/^readonly ADP_VERSION="\([^"]*\)".*/\1/p' "${INSTALL_DIR}/.${ADP_SCRIPT}.tmp.$$" 2>/dev/null | head -n 1
}

# Record what this install actually landed, so a later `adp update --to <v>` /
# `--rollback --to <v>` can verify a version rather than trusting a filename.
# Not a secret and not a session: plain metadata beside the binaries.
write_manifest() {
    installed_version="$1"
    previous_version=""
    if [ "${KEEP_PREVIOUS}" = "1" ] && [ -f "${INSTALL_DIR}/${MANIFEST_FILE_NAME}" ]; then
        previous_version=$(jq -r '.version // empty' "${INSTALL_DIR}/${MANIFEST_FILE_NAME}" 2>/dev/null || true)
    fi
    tmp=$(mktemp "${INSTALL_DIR}/${MANIFEST_FILE_NAME}.XXXXXX")
    jq -n --arg v "${installed_version}" --arg p "${previous_version}" --arg url "${GATEWAY_URL}" \
        '{version: $v, previous_version: $p, gateway_url: $url}' > "${tmp}"
    chmod 644 "${tmp}"
    mv -f "${tmp}" "${INSTALL_DIR}/${MANIFEST_FILE_NAME}"
}

do_install() {
    resolve_gateway_url
    require_gateway_url
    check_existing_binding

    mkdir -p "${INSTALL_DIR}"

    # Stage-then-commit: fetch and validate all three files first, and only move
    # them into place once every one has succeeded. A mid-way failure trips the
    # trap, cleanup_staged wipes the temps, and the previous install (if any) is
    # left untouched — no half-finished state where `adp` exists but its core
    # script does not.
    trap cleanup_staged EXIT INT TERM

    for name in ${CLI_FILES}; do stage_file "${name}"; done

    # Issue #5039: verify the pin BEFORE anything is committed, so a mismatch
    # leaves the working CLI untouched rather than installing a version the
    # caller did not ask for and then reporting it.
    staged_version=$(staged_adp_version)
    if [ -n "${VERSION_PIN}" ] && [ "${staged_version}" != "${VERSION_PIN}" ]; then
        log_error "Version pin mismatch: asked for ${VERSION_PIN}, but ${GATEWAY_URL} serves ${staged_version:-an unreadable version}."
        log_info "Nothing was installed. Your gateway serves one CLI version; pin that one, or omit --version-pin."
        exit 1
    fi

    for name in ${CLI_FILES}; do commit_file "${name}"; done

    write_manifest "${staged_version}"

    trap - EXIT INT TERM
    STAGED_TMPS=""

    persist_gateway_url
    log_success "Gateway URL saved: ${GATEWAY_URL}"
}

do_uninstall() {
    removed=0
    for name in ${CLI_FILES}; do
        if [ -f "${INSTALL_DIR}/${name}" ]; then
            rm -f "${INSTALL_DIR}/${name}" "${INSTALL_DIR}/${name}.prev"
            removed=1
        fi
    done
    if [ "${removed}" -eq 0 ]; then
        log_warn "Nothing to uninstall in ${INSTALL_DIR}"
        return 0
    fi
    log_success "Removed the adp CLI from ${INSTALL_DIR}"
    # Deliberately left alone: ~/.bedrock-gateway (your session) and the shell rc
    # PATH line. Deleting a live session on an uninstall would be a surprise.
    log_info "Your session in ${CONFIG_DIR} was left in place — 'rm -rf ${CONFIG_DIR}' to clear it."
}

# The rc file for the user's login shell. $SHELL is what the terminal launched,
# which is what matters here — this script itself always runs under sh/bash
# regardless of the user's interactive shell.
shell_rc_file() {
    case "${SHELL:-}" in
        */zsh) echo "${HOME}/.zshrc" ;;
        */bash)
            if [ -f "${HOME}/.bash_profile" ]; then echo "${HOME}/.bash_profile"; else echo "${HOME}/.bashrc"; fi ;;
        *) echo "" ;;
    esac
}

ensure_on_path() {
    path_line="export PATH=\"\$PATH:${INSTALL_DIR}\""

    case ":${PATH}:" in
        *":${INSTALL_DIR}:"*) log_success "${INSTALL_DIR} is already on your PATH"; return 0 ;;
    esac

    rc=$(shell_rc_file)

    if [ "${NO_PATH_EDIT}" = "true" ] || [ -z "${rc}" ]; then
        log_warn "${INSTALL_DIR} is not on your PATH. Add this line to your shell config:"
        printf '\n    %s\n\n' "${path_line}" >&2
        return 0
    fi

    # Idempotent: re-running the installer must not append the line twice.
    if [ -f "${rc}" ] && grep -Fq "${INSTALL_DIR}" "${rc}"; then
        log_success "${INSTALL_DIR} is already in ${rc}"
    else
        {
            echo ""
            echo "# Added by the adp CLI installer"
            echo "${path_line}"
        } >> "${rc}"
        log_success "Added ${INSTALL_DIR} to your PATH in ${rc}"
    fi

    log_info "Reload your shell to pick it up:  source ${rc}"
}

print_next_steps() {
    cat << EOF

  adp is installed. Next:

    "${INSTALL_DIR}/adp" login    # works immediately, before reloading PATH
    adp status         # confirm you are signed in
    adp codex setup    # or: adp claude setup
    codex              # or: claude

  First-time platform administrator (before GitHub is configured):
    "${INSTALL_DIR}/adp" admin setup

  One login is shared by every tool — adding a second tool is just its setup verb.

EOF
}

main() {
    parse_args "$@"
    check_dependencies

    if [ "${UNINSTALL}" = "true" ]; then
        do_uninstall
        exit 0
    fi

    do_install
    ensure_on_path
    print_next_steps
}

main "$@"
