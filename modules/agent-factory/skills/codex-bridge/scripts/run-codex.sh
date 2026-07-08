#!/usr/bin/env bash
# run-codex.sh — thin, human-gated wrapper around the Codex CLI for the
# codex-bridge skill (issue #2705, EPIC #2702).
#
# The supervising Claude agent calls this ONLY when the triggering issue/comment
# explicitly asked for Codex (the gate lives in SKILL.md). This script just runs
# `codex exec` safely and hands the output back for the agent to review.
#
# Usage:
#   run-codex.sh write       "<instruction>"  # Codex authors/edits code in $PWD
#   run-codex.sh review      "<file path>"     # Codex reviews a file (read-only intent)
#   run-codex.sh review-diff [<base-ref>]      # Codex reviews git diff <base>...
#                                              #   (read-only; base defaults to origin/main)
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
#
# Observability (issue #2753):
#   * Codex runs with `--json` (structured JSONL event stream, CLI 0.142.5).
#   * The raw JSONL is teed to /tmp/codex-runs/<ts>.jsonl (pod-local, ephemeral).
#   * Events are piped through render-codex-events.py, which prints one compact
#     line per step, Codex's final message verbatim, then a trailer with the
#     session id + token usage + JSONL path. Codex's OWN exit code is preserved
#     via PIPESTATUS — the renderer is display-only and never changes rc.

set -euo pipefail

# --- Hard timeout (seconds). Overridable for tests; defaults to 10 minutes. ---
CODEX_TIMEOUT="${CODEX_TIMEOUT:-600}"

# --- Codex binary (overridable for tests via CODEX_BIN). ---
CODEX_BIN="${CODEX_BIN:-codex}"

# --- Renderer + raw-log location (issue #2753) ------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RENDER_SCRIPT="${SCRIPT_DIR}/render-codex-events.py"
CODEX_RUNS_DIR="${CODEX_RUNS_DIR:-/tmp/codex-runs}"

# --- Live-stream events file (issue #2884) ----------------------------------
# In addition to the per-run archival tee above, append the SAME JSONL event
# stream to one stable, well-known path so the agent-worker's codexEventWatcher
# has a single file to tail. The watcher forwards compact per-step summaries to
# the live run page WHILE Codex is running (Phase 1 only rendered them after the
# fact). This file is truncated at the start of each delegation (tee without
# -a), so the watcher — which owns the stable path for the whole worker
# lifecycle — sees each delegation as a fresh write sequence.
CODEX_EVENTS_FILE="${CODEX_EVENTS_FILE:-/tmp/codex-events/current.jsonl}"

usage() {
    echo "Usage: run-codex.sh {write|review|review-diff} <instruction|file-path|[base-ref]>" >&2
    echo "  write       \"<instruction>\"   Codex authors/edits code in the current dir" >&2
    echo "  review      \"<file path>\"      Codex reviews the given file, findings only" >&2
    echo "  review-diff [<base-ref>]        Codex reviews git diff <base>... (default origin/main), findings only" >&2
}

# --- Distilled persona pack resolution (issue #2891, #2945) -----------------
# Resolved up-front so BOTH the AGENTS.md render (further below) and the review /
# review-diff prompt calibration (this issue) share the exact same file. Missing
# file → every consumer is a byte-identical no-op vs. prior behavior.
CODEX_DISTILLED_DIR="${CODEX_DISTILLED_DIR:-$PWD/.adp-rules/personas/codex-distilled}"
AGENT_TYPE="${AGENT_TYPE:-developer}"
DISTILLED_FILE="${CODEX_DISTILLED_DIR}/${AGENT_TYPE}.md"

# Prepend the distilled persona pack (conventions + quality bar) to a review
# instruction so review findings are persona-calibrated (issue #2945 §8c). When
# the distilled file is absent, the base instruction is emitted byte-identically
# — the same no-op guarantee as the AGENTS.md branch. This is additive
# calibration of the TASK; the AGENTS.md render still applies to review modes too.
_prepend_distilled() {
    local base="$1"
    if [ -f "${DISTILLED_FILE}" ]; then
        printf 'You are reviewing with the following conventions and quality bar:\n%s\n\n%s' \
            "$(cat "${DISTILLED_FILE}")" "${base}"
    else
        printf '%s' "${base}"
    fi
}

MODE="${1:-}"

case "$MODE" in
    write)
        [ "$#" -eq 2 ] || { usage; exit 2; }
        INSTRUCTION="$2"
        ;;
    review)
        [ "$#" -eq 2 ] || { usage; exit 2; }
        ARG="$2"
        if [ ! -f "$ARG" ]; then
            echo "run-codex.sh: review target not found: $ARG" >&2
            exit 2
        fi
        # Build a read-only review prompt. The path is embedded into a single
        # argv string handed to Codex — never re-interpreted by the shell. The
        # distilled pack (if present) is prepended as task calibration; the
        # read-only sentence + target path are preserved verbatim (contract-
        # pinned in test_run_codex_contract.py::TestReviewMode).
        INSTRUCTION="$(_prepend_distilled "Review the file '${ARG}' for correctness, bugs, and clear improvements. Report your findings as a concise list. Do not modify any files.")"
        ;;
    review-diff)
        # PR-level review: feed `git diff <base>...` (three-dot / merge-base
        # semantics) to Codex, read-only. Base defaults to origin/main. The diff
        # is captured to a temp file, size-capped, and embedded as a SINGLE
        # literal argv (never shell-evaluated — same one-argv contract as write).
        [ "$#" -le 2 ] || { usage; exit 2; }
        BASE_REF="${2:-origin/main}"
        if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
            echo "run-codex.sh: review-diff must run inside a git repository" >&2
            exit 2
        fi
        # --- Branch resolution fix (issue #3269) --------------------------------
        # When the PR branch under review isn't checked out locally (remote-only),
        # `rev-parse --verify` fails and the entire review-diff mode aborts. The
        # supervisor then silently falls back to Claude. Fix: attempt to fetch the
        # ref from origin before verifying. If the ref is already local this is a
        # no-op; if remote-only, it brings the commit local so the three-dot diff
        # resolves. The fetch is best-effort (non-fatal) — if it fails (no remote,
        # offline) we still fall through to the existing error path.
        if ! git rev-parse --verify --quiet "${BASE_REF}^{commit}" >/dev/null 2>&1; then
            git fetch origin "${BASE_REF}" >/dev/null 2>&1 || true
            # After fetch, try resolving via FETCH_HEAD as a fallback when the
            # ref name itself still doesn't resolve (e.g. bare branch name that
            # exists on remote but not as a local tracking ref).
            if ! git rev-parse --verify --quiet "${BASE_REF}^{commit}" >/dev/null 2>&1; then
                if git rev-parse --verify --quiet "FETCH_HEAD^{commit}" >/dev/null 2>&1; then
                    BASE_REF="FETCH_HEAD"
                else
                    echo "run-codex.sh: review-diff base ref not found: ${BASE_REF}" >&2
                    exit 2
                fi
            fi
        fi
        CODEX_DIFF_MAX_BYTES="${CODEX_DIFF_MAX_BYTES:-262144}"
        DIFF_FILE="$(mktemp "${TMPDIR:-/tmp}/codex-review-diff.XXXXXX")"
        if ! git diff "${BASE_REF}..." >"${DIFF_FILE}" 2>/dev/null; then
            echo "run-codex.sh: git diff failed for base ${BASE_REF}" >&2
            rm -f "${DIFF_FILE}"
            exit 2
        fi
        # Empty diff → nothing to review; exit 0 WITHOUT invoking Codex (no spend).
        if [ ! -s "${DIFF_FILE}" ]; then
            echo "run-codex.sh: no changes vs ${BASE_REF}; nothing to review" >&2
            rm -f "${DIFF_FILE}"
            exit 0
        fi
        # Cap the diff bytes so an oversized PR can't blow Codex's context; when
        # truncated, append an explicit marker and tell Codex in the prompt.
        TRUNC_NOTE=""
        if [ "$(wc -c <"${DIFF_FILE}")" -gt "${CODEX_DIFF_MAX_BYTES}" ]; then
            head -c "${CODEX_DIFF_MAX_BYTES}" "${DIFF_FILE}" >"${DIFF_FILE}.capped"
            printf '\n[diff truncated at %s bytes]\n' "${CODEX_DIFF_MAX_BYTES}" >>"${DIFF_FILE}.capped"
            mv -f "${DIFF_FILE}.capped" "${DIFF_FILE}"
            TRUNC_NOTE=" Note: the diff below was truncated at ${CODEX_DIFF_MAX_BYTES} bytes; review only what is present."
        fi
        DIFF_CONTENT="$(cat "${DIFF_FILE}")"
        rm -f "${DIFF_FILE}"
        INSTRUCTION="$(_prepend_distilled "Review the following unified diff for correctness, bugs, and clear improvements. Report findings as a concise list referencing file+line. Do not modify any files.${TRUNC_NOTE}"$'\n\n'"${DIFF_CONTENT}")"
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

# --- Persona prompt pack: render distilled AGENTS.md into the cwd -----------
# (issue #2891, spike #2839 §3–§4). Codex 0.142.5 reads an `AGENTS.md` from its
# working root and obeys it (empirically verified). We render the adp-owned
# distilled persona file (mindset + conventions only — never identity/outer-loop/
# mention content, which is why it is a hand-authored fileset, NOT extracted from
# the full persona at runtime) so Codex output is persona-calibrated.
#
# Content is adp-image-baked (.adp-rules/personas/codex-distilled/), NEVER sourced
# from repo-controlled paths (injection surface, spike §3.4).
#
# Safety:
#   * Missing distilled file        → no-op (byte-identical to prior behavior).
#   * No pre-existing $PWD/AGENTS.md → write distilled; trap-delete on EXIT
#                                      (covers success, failure, timeout).
#   * Pre-existing $PWD/AGENTS.md    → save original bytes, write distilled-first
#                                      then original; restore exact bytes on EXIT.
# The distilled block goes FIRST so adp-owned conventions are not silently
# overridden by repo text (spike §3.3.2). project_doc_max_bytes is passed as a
# per-run `-c` override (NOT a config-file write) so an oversized file can never
# blow the context budget.
# (CODEX_DISTILLED_DIR / AGENT_TYPE / DISTILLED_FILE are resolved up-front, above
# the mode switch, so the review / review-diff prompt calibration shares them.)

# Hard cap on the project-doc bytes Codex will ingest (issue #2891 impact row 1).
CODEX_PROJECT_DOC_MAX_BYTES="${CODEX_PROJECT_DOC_MAX_BYTES:-32768}"

# Extra `-c` overrides appended only when we actually render AGENTS.md. Empty by
# default so the missing-file path is byte-identical to prior behavior.
CODEX_DOC_ARGS=()

# State for the EXIT trap. AGENTS_RENDERED marks that we own the file; when an
# original existed we stash its bytes to restore verbatim.
AGENTS_RENDERED=0
AGENTS_PATH="$PWD/AGENTS.md"
AGENTS_BACKUP=""

_restore_agents_md() {
    [ "${AGENTS_RENDERED}" -eq 1 ] || return 0
    if [ -n "${AGENTS_BACKUP}" ] && [ -f "${AGENTS_BACKUP}" ]; then
        # A repo AGENTS.md pre-existed: restore its exact original bytes.
        mv -f "${AGENTS_BACKUP}" "${AGENTS_PATH}"
    else
        # We created AGENTS.md ourselves: remove it.
        rm -f "${AGENTS_PATH}"
    fi
}

if [ -f "${DISTILLED_FILE}" ]; then
    if [ -f "${AGENTS_PATH}" ]; then
        # Repo ships its own AGENTS.md. Preserve original bytes, then write the
        # distilled block FIRST followed by the original content.
        AGENTS_BACKUP="$(mktemp)"
        cp -f "${AGENTS_PATH}" "${AGENTS_BACKUP}"
        {
            cat "${DISTILLED_FILE}"
            printf '\n'
            cat "${AGENTS_BACKUP}"
        } > "${AGENTS_PATH}"
    else
        # No repo AGENTS.md: render the distilled file as AGENTS.md.
        cp -f "${DISTILLED_FILE}" "${AGENTS_PATH}"
    fi
    AGENTS_RENDERED=1
    trap _restore_agents_md EXIT
    CODEX_DOC_ARGS=(-c "project_doc_max_bytes=${CODEX_PROJECT_DOC_MAX_BYTES}")
fi

# --- Invoke Codex -----------------------------------------------------------
# Flags per spike #2703 headless transcript:
#   --dangerously-bypass-approvals-and-sandbox : no interactive approval (pod is
#                                                externally sandboxed + ephemeral)
#   --skip-git-repo-check                       : run outside a git repo if needed
#   --json                                      : structured JSONL events (#2753)
# Model/provider/region come from the baked $HOME/.codex/config.toml.
# stdin closed so Codex can never block on a prompt; timeout bounds the run.
#
# The raw JSONL is teed to a pod-local file and piped through the renderer so
# the agent (and the live run page) see a compact per-step summary. Codex's own
# exit code is recovered from PIPESTATUS — the tee/renderer never mask it.
mkdir -p "${CODEX_RUNS_DIR}"
JSONL_PATH="${CODEX_RUNS_DIR}/$(date +%Y%m%dT%H%M%S)-$$.jsonl"

# Ensure the stable live-stream file's directory exists. `tee` (no -a) truncates
# it, so each delegation starts the watcher's view fresh (issue #2884).
mkdir -p "$(dirname "${CODEX_EVENTS_FILE}")"

# Tee the raw JSONL to BOTH sinks in a single pass: the per-run archival log
# (unchanged, #2753) and the stable live-stream file the worker tails (#2884).
# PIPESTATUS[0] still refers to `timeout ... codex` (first pipe element), so
# Codex's own exit code is preserved exactly as before — tee/renderer never
# mask it.
set +e
timeout "${CODEX_TIMEOUT}" "${CODEX_BIN}" exec \
    --dangerously-bypass-approvals-and-sandbox \
    --skip-git-repo-check \
    --json \
    "${CODEX_DOC_ARGS[@]}" \
    "${INSTRUCTION}" </dev/null \
    | tee "${JSONL_PATH}" "${CODEX_EVENTS_FILE}" \
    | python3 "${RENDER_SCRIPT}" --jsonl-path "${JSONL_PATH}"
rc="${PIPESTATUS[0]}"
set -e

if [ "$rc" -eq 124 ]; then
    echo "run-codex.sh: Codex timed out after ${CODEX_TIMEOUT}s" >&2
elif [ "$rc" -ne 0 ]; then
    echo "run-codex.sh: Codex exited non-zero (${rc})" >&2
fi

exit "$rc"
