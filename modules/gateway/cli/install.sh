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
# Requirements: curl, jq. Deliberately NOT the aws CLI — ordinary gateway users
# hold no AWS credentials, and the point of the gateway-routed refresh (#4846) is
# that they need none. The previous version of this script installed the
# deprecated bg-auth.sh and hard-required `aws`.

set -eu

VERSION="2.0.0"
DEFAULT_INSTALL_DIR="${HOME}/.adp/bin"

# The three files that must land side by side.
ADP_SCRIPT="adp"
CORE_SCRIPT="bg-cognito-auth.sh"
PROXY_SCRIPT="bg-gateway-proxy.py"

CONFIG_DIR="${HOME}/.bedrock-gateway"
CONFIG_FILE="${CONFIG_DIR}/config.json"

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

# Resolve the gateway URL: flag/env, else the one a previous install persisted.
# Never guessed — a wrong origin would silently install a CLI pointed at another
# deployment.
resolve_gateway_url() {
    if [ -z "${GATEWAY_URL}" ] && [ -f "${CONFIG_FILE}" ]; then
        GATEWAY_URL=$(jq -r '.gateway_url // empty' "${CONFIG_FILE}" 2>/dev/null || true)
    fi
    GATEWAY_URL="${GATEWAY_URL%/}"
}

require_gateway_url() {
    if [ -n "${GATEWAY_URL}" ]; then return 0; fi
    log_error "No gateway URL supplied and none stored."
    log_info "Re-run with your gateway's URL — the /setup page shows this line with it filled in:"
    printf '\n    curl -fsSL https://<gateway>/api/cli/install.sh | sh -s -- --gateway-url https://<gateway>\n\n' >&2
    exit 1
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

# Install one file, from the repo checkout when we are running inside one, else
# from the gateway's download route.
install_file() {
    name="$1"
    target="${INSTALL_DIR}/${name}"
    src_dir=$(script_dir)

    if [ "${KEEP_PREVIOUS}" = "1" ] && [ -f "${target}" ]; then
        cp -f "${target}" "${target}.prev"
    fi

    tmp=$(mktemp "${target}.XXXXXX")

    if [ -n "${src_dir}" ] && [ -f "${src_dir}/${name}" ]; then
        cp -f "${src_dir}/${name}" "${tmp}"
    else
        require_gateway_url
        if ! curl -fsSL "${GATEWAY_URL}/cli/${name}" -o "${tmp}"; then
            rm -f "${tmp}"
            log_error "Could not download ${name} from ${GATEWAY_URL}/cli/${name}"
            exit 1
        fi
    fi

    # Only the entrypoints need +x; the proxy is run as `python3 <file>`.
    case "${name}" in
        "${ADP_SCRIPT}"|"${CORE_SCRIPT}") chmod 755 "${tmp}" ;;
        *) chmod 644 "${tmp}" ;;
    esac
    mv -f "${tmp}" "${target}"
    log_success "Installed ${target}"
}

do_install() {
    resolve_gateway_url
    require_gateway_url

    mkdir -p "${INSTALL_DIR}"

    install_file "${ADP_SCRIPT}"
    install_file "${CORE_SCRIPT}"
    install_file "${PROXY_SCRIPT}"

    persist_gateway_url
    log_success "Gateway URL saved: ${GATEWAY_URL}"
}

do_uninstall() {
    removed=0
    for name in "${ADP_SCRIPT}" "${CORE_SCRIPT}" "${PROXY_SCRIPT}"; do
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

    adp login          # approve once in the browser
    adp status         # confirm you are signed in
    adp codex setup    # or: adp claude setup
    codex              # or: claude

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
