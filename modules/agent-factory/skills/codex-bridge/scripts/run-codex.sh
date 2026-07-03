#!/usr/bin/env bash
# run-codex.sh — thin, human-gated wrapper around the Codex CLI for the
# codex-bridge skill (issue #2705, EPIC #2702).
#
# The supervising Claude agent calls this ONLY when the triggering issue/comment
# explicitly asked for Codex (the gate lives in SKILL.md). This script just runs
# `codex exec` safely and hands the output back for the agent to review.
#
# Usage:
#   run-codex.sh write  "<instruction>"    # Codex authors/edits code in $PWD
#   run-codex.sh review "<file path>"      # Codex reviews a file (read-only intent)
#
# Safety properties (see #2705 impact table):
#   * set -euo pipefail                 — fail fast, no silent errors
#   * hard timeout (CODEX_TIMEOUT, 600s)— a TTY-less pod can't be un-hung by a
#                                         human; the startup chatgpt.com 401
#                                         (#2703) must never stall the turn
#   * stdin closed (</dev/null)         — Codex never blocks waiting on a prompt
#   * instruction passed as ONE argv    — issue text is data, never shell-eval'd
#   * AWS_* reset to pod IRSA defaults  — Codex signs with the pod role, never
#                                         any assumed customer creds in the env
#   * non-zero exit surfaces stderr     — the agent sees why Codex failed

set -euo pipefail

# --- Hard timeout (seconds). Overridable for tests; defaults to 10 minutes. ---
CODEX_TIMEOUT="${CODEX_TIMEOUT:-600}"

# --- Codex binary (overridable for tests via CODEX_BIN). ---
CODEX_BIN="${CODEX_BIN:-codex}"

usage() {
    echo "Usage: run-codex.sh {write|review} <instruction-or-file-path>" >&2
    echo "  write  \"<instruction>\"   Codex authors/edits code in the current dir" >&2
    echo "  review \"<file path>\"      Codex reviews the given file, findings only" >&2
}

if [ "$#" -ne 2 ]; then
    usage
    exit 2
fi

MODE="$1"
ARG="$2"

case "$MODE" in
    write)
        INSTRUCTION="$ARG"
        ;;
    review)
        if [ ! -f "$ARG" ]; then
            echo "run-codex.sh: review target not found: $ARG" >&2
            exit 2
        fi
        # Build a read-only review prompt. The path is embedded into a single
        # argv string handed to Codex — never re-interpreted by the shell.
        INSTRUCTION="Review the file '${ARG}' for correctness, bugs, and clear improvements. Report your findings as a concise list. Do not modify any files."
        ;;
    *)
        usage
        exit 2
        ;;
esac

# --- Credential isolation ---------------------------------------------------
# The run environment may carry static/assumed customer AWS credentials (e.g.
# from `adp-cred assume`). Codex must sign Bedrock requests with the POD's own
# IRSA identity, so clear the static-credential and profile vars and let the SDK
# fall back to the pod's web-identity chain (AWS_ROLE_ARN +
# AWS_WEB_IDENTITY_TOKEN_FILE, injected by IRSA). Region is pinned to the mantle
# region (#2703).
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN AWS_PROFILE AWS_DEFAULT_PROFILE || true
export AWS_REGION="${AWS_REGION:-us-east-1}"

# --- Invoke Codex -----------------------------------------------------------
# Flags per spike #2703 headless transcript:
#   --dangerously-bypass-approvals-and-sandbox : no interactive approval (pod is
#                                                externally sandboxed + ephemeral)
#   --skip-git-repo-check                       : run outside a git repo if needed
# Model/provider/region come from the baked $HOME/.codex/config.toml.
# stdin closed so Codex can never block on a prompt; timeout bounds the run.
set +e
timeout "${CODEX_TIMEOUT}" "${CODEX_BIN}" exec \
    --dangerously-bypass-approvals-and-sandbox \
    --skip-git-repo-check \
    "${INSTRUCTION}" </dev/null
rc=$?
set -e

if [ "$rc" -eq 124 ]; then
    echo "run-codex.sh: Codex timed out after ${CODEX_TIMEOUT}s" >&2
elif [ "$rc" -ne 0 ]; then
    echo "run-codex.sh: Codex exited non-zero (${rc})" >&2
fi

exit "$rc"
