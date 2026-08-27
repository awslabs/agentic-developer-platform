#!/usr/bin/env bash
#
# `source-path` must be a FILE-LEVEL directive (in the leading comment block,
# before the first command) — a directive attached to the `.` line itself only
# applies to that one line and shellcheck still resolves ../lib relative to the
# invoking cwd. With this, `shellcheck -x` follows the shared harness and lints
# this file against the real lib, which is why the lint gate passes -x.
# shellcheck source-path=SCRIPTDIR
#
# =============================================================================
# run-eval.sh — clean-room E2E evaluation of the CLI onboarding journey
# =============================================================================
# Issue #4157 (Story 5 of EPIC #4143). Automates the manual live validation of
# 2026-08-26 so the journey shipped by #4144 (approval gate), #4145 (CLI
# seeding + discovery), #4146 (setup-page download route) and #4154 (Codex
# zero-touch proxy) is re-proven on every run instead of once, by hand.
#
# THE CLEAN ROOM IS THE POINT
# ---------------------------
# The laptop journey must NOT run in the agent-worker container, or on the ARC
# runner itself. Those environments bake in ~/.codex/config.toml (pointing at
# the sigv4-proxy on 127.0.0.1:9090), Claude Code settings, ANTHROPIC_* env and
# platform IRSA — any one of which silently substitutes the platform's internal
# auth for the flow under test, so the eval would report green while every
# request rode the agent's own credentials.
#
# So this script runs as a two-part split (issue #4171):
#
#   HARNESS — this process, on the ARC runner. Holds the credentials, resolves
#     SSM, seeds Cognito, flips the flag, reads Postgres, asserts.
#   LAPTOP  — a pod the harness spawns with kubectl (`node:20-bookworm`,
#     `automountServiceAccountToken: false`, no env, nothing pre-installed). It
#     is the emulated developer machine and it is where every Phase-C command
#     runs, via `kubectl exec`.
#
# Three mechanisms enforce the boundary:
#
#   1. The pod is created from a stock public image with no service-account
#      token and no environment, so it HAS no platform credential to borrow.
#   2. `--assert-clean-room` is the FIRST command exec'd in that pod. It also
#      guards against someone later pointing laptop() at a dirty target.
#   3. `laptop()` — every command that emulates the developer's laptop is run
#      through it, and it can only reach the pod. Only the harness helpers
#      (h_aws / h_kubectl / h_psql) ever see credentials.
#
# This is why Tier-1 identity seeding works at all: cognito-idp:InitiateAuth is
# an UNSIGNED API, so `import`/`token`/`refresh` need no credentials whatsoever.
# Anything in the laptop phase that suddenly required credentials would fail —
# which is exactly the signal we want.
#
# Secrets never travel on an exec'd command line: anything in the argv of
# `kubectl exec` is visible in the exec API and in the runner's process table.
# Tokens reach the pod on STDIN (laptop_put_file) and live in 0600 files.
#
# WHAT IT DOES NOT COVER (by design)
# ----------------------------------
# Tier 2 — the real GitHub-OAuth browser leg — is out of scope: it needs a bot
# GitHub account plus its TOTP secret in Secrets Manager. The broker's output is
# just a Cognito user + tokens, so this eval mints the equivalent session
# directly at the Cognito layer and starts from there.
#
# USAGE
#   run-eval.sh --assert-clean-room                 # contamination gate (in-pod)
#   run-eval.sh                                     # full matrix, phases A-D
#   run-eval.sh --phases A,B                        # subset
#   run-eval.sh --dry-run                           # stubbed CLIs, asserts ordering
#   run-eval.sh --dry-run --fail-phase B            # proves cleanup-on-failure
#   run-eval.sh --inject-failure wrong-org          # deliberately-broken run
#   run-eval.sh --cleanup-only                      # idempotent teardown
#
# ENVIRONMENT
#   ENVIRONMENT       target env (default: dev)
#   AWS_REGION        default: us-east-1
#   EVAL_RUN_ID       unique suffix for throwaway resources (default: local-$$)
#   EVAL_WORKDIR      scratch dir (default: mktemp -d)
#   EVAL_ORG_ID       org to approve the throwaway user into (default: discovered)
#   EVAL_POD_NAMESPACE  namespace for the laptop pod (default: adp-gateway)
#   EVAL_POD_IMAGE    laptop pod image (default: node:20-bookworm)
# =============================================================================

set -euo pipefail

# Absolute path to this script, resolved before anything can cd. The harness
# copies it into the clean-room pod so `--assert-clean-room` runs there verbatim,
# from the same file, rather than from a re-implementation that could drift.
EVAL_SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

# The shared harness (#4163 factored it out of this file so the budget/rate-limit
# eval could reuse it verbatim). Resolved RELATIVE to this script, which is what
# lets the in-pod copy work: laptop_put_harness mirrors the repo layout into the
# pod as harness/<eval>/run-eval.sh + harness/lib/*.sh, so `../lib` resolves the
# same way on the runner and in the clean room — with no env var, because telling
# the clean room where its lib is would mean adding environment to the thing whose
# defining property is that it has none.
EVAL_LIB_DIR="$(cd "$(dirname "$EVAL_SCRIPT_PATH")/../lib" && pwd)"
# shellcheck source=../lib/log.sh
. "$EVAL_LIB_DIR/log.sh"
# shellcheck source=../lib/state.sh
. "$EVAL_LIB_DIR/state.sh"
# shellcheck source=../lib/aws.sh
. "$EVAL_LIB_DIR/aws.sh"
# shellcheck source=../lib/clean-room.sh
. "$EVAL_LIB_DIR/clean-room.sh"
# shellcheck source=../lib/http.sh
. "$EVAL_LIB_DIR/http.sh"
# shellcheck source=../lib/pod.sh
. "$EVAL_LIB_DIR/pod.sh"
# shellcheck source=../lib/cognito.sh
. "$EVAL_LIB_DIR/cognito.sh"

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
ENVIRONMENT="${ENVIRONMENT:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
EVAL_RUN_ID="${EVAL_RUN_ID:-local-$$}"
FLAG_NAME="BG_ENFORCE_ORG_ASSIGNMENT"
K8S_NAMESPACE="adp-gateway"
K8S_DEPLOYMENT="deploy/bedrockgateway"
PROXY_PORT="${EVAL_PROXY_PORT:-9191}"

# The eval prefix is the sweep convention: anything matching it in Cognito is a
# leaked throwaway user from a crashed run and is safe to delete.
EVAL_USER_PREFIX="eval-cli-onboarding"
EVAL_USERNAME="${EVAL_USER_PREFIX}-${EVAL_RUN_ID}@example.com"
EVAL_ADMIN_USERNAME="${EVAL_USER_PREFIX}-admin-${EVAL_RUN_ID}@example.com"

# -----------------------------------------------------------------------------
# The laptop pod — the clean room (#4171)
# -----------------------------------------------------------------------------
# The pod lives in the SAME namespace the harness already has RBAC for
# (adp-gateway: pods create/delete + pods/exec create), so this needs no new
# permissions. It is labelled so a leaked pod from a crashed run can be swept by
# selector without knowing its run id — that sweep is what --cleanup-only does.
POD_NAMESPACE="${EVAL_POD_NAMESPACE:-adp-gateway}"
POD_IMAGE="${EVAL_POD_IMAGE:-node:20-bookworm}"
POD_LABEL_APP="$EVAL_USER_PREFIX"
# RFC 1123 for the name, and the label-value charset for the label: the run id
# can carry anything (a local run uses "local-$$"; CI uses "<run_id>-<attempt>").
POD_RUN_LABEL="$(printf '%s' "$EVAL_RUN_ID" | tr -c 'A-Za-z0-9._-' '-' | cut -c1-63)"
# A trailing '-' is an invalid RFC 1123 name, and both tr and cut can leave one.
LAPTOP_POD="${EVAL_USER_PREFIX}-$(printf '%s' "$EVAL_RUN_ID" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | cut -c1-24 | sed 's/-*$//')"

# The inference model. Both are in enable-bedrock-models.sh's REQUIRED_MODELS,
# so a deploy that passed cannot lack access to them.
EVAL_MODEL="${EVAL_MODEL:-global.anthropic.claude-sonnet-4-6}"
EVAL_CODEX_MODEL="${EVAL_CODEX_MODEL:-openai.gpt-5.6-sol}"

# The sentinel the CLIs are asked to echo back. Proves real inference happened
# rather than a 200 with an empty or canned body.
SENTINEL="CLI-ONBOARDING-EVAL-OK"

MODE="full"
DRY_RUN=false
FAIL_PHASE=""
INJECT_FAILURE=""
PHASES="A,B,C"

# Output helpers, the results table and the summary come from lib/log.sh.
# The `name` attribute stamped on seeded Cognito users, which is also the sweep tag.
EVAL_SEED_NAME="$EVAL_USER_PREFIX"
EVAL_SUMMARY_TITLE="CLI onboarding eval"

# -----------------------------------------------------------------------------
# Argument parsing
# -----------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --assert-clean-room) MODE="clean-room"; shift ;;
    --cleanup-only)      MODE="cleanup"; shift ;;
    --dry-run)           DRY_RUN=true; shift ;;
    --fail-phase)        FAIL_PHASE="$2"; shift 2 ;;
    --inject-failure)    INJECT_FAILURE="$2"; shift 2 ;;
    --phases)            PHASES="$2"; shift 2 ;;
    --environment)       ENVIRONMENT="$2"; shift 2 ;;
    -h|--help)           sed -n '2,60p' "$0"; exit 0 ;;
    *)                   die "Unknown option: $1" ;;
  esac
done

case "$INJECT_FAILURE" in
  ""|wrong-org) ;;
  *) die "Unknown --inject-failure kind: $INJECT_FAILURE (supported: wrong-org)" ;;
esac

WORKDIR="${EVAL_WORKDIR:-$(mktemp -d)}"
mkdir -p "$WORKDIR"
chmod 700 "$WORKDIR"
STATE_FILE="$WORKDIR/state.env"
TRACE_FILE="$WORKDIR/trace.log"
RESULTS_FILE="$WORKDIR/results.tsv"
: > "$TRACE_FILE"
: > "$RESULTS_FILE"

# Where the emulated developer's HOME lives INSIDE the pod. Not the pod's own
# $HOME (/root): the clean-room gate asserts that HOME is pristine, and keeping
# the journey in its own tree makes teardown a single rm -rf.
#
# Under --dry-run the kubectl stub runs exec'd commands locally instead of in a
# pod, so these live under $WORKDIR: the dry run then exercises the REAL Phase-C
# code (file modes, JWT shape, the helper's own logic) on a real filesystem
# without a cluster, and cannot escape the test's scratch directory.
if [ "$DRY_RUN" = true ]; then
  POD_WORKDIR="$WORKDIR/pod"
else
  POD_WORKDIR="/tmp/laptop"
fi
POD_HOME="$POD_WORKDIR/home"

phase_enabled() { case ",$PHASES," in *,"$1",*) return 0 ;; *) return 1 ;; esac; }

# The clean-room gate lives in lib/clean-room.sh; the credentialed h_* helpers in
# lib/aws.sh; laptop() and the pod lifecycle in lib/pod.sh.

# The PATH the laptop sees. Explicitly NOT built from the harness's own $PATH:
# the pod's filesystem is not the runner's, so inheriting the runner's PATH would
# name directories that do not exist there. The npm prefix comes first because
# the journey installs both CLIs with `npm install -g`.
POD_PATH="$POD_HOME/.npm-global/bin:$POD_HOME/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Extra env for the in-pod clean-room assertion. EMPTY in a real run — the two
# test-only escape hatches are added here by setup_dry_run_stubs and nowhere
# else, so the workflow (and therefore the live clean room) can never get them.
POD_ASSERT_ENV=()

# -----------------------------------------------------------------------------
# Assertions
# -----------------------------------------------------------------------------
# The 409 must be the approval gate's own body, not merely "a 409". A 409 from
# some other layer, or one with a different error code, is a regression that a
# status-code-only assertion would wave through.
# All assert_* helpers RECORD a failure and return 0 rather than aborting. The
# eval must run the whole matrix: fail-fast would hide every later regression
# behind the first one, and the triage agent needs the complete per-phase table
# to know how many fix-issues to file. $FAILURES carries the verdict.
EXPECTED_409_ERROR="user_not_assigned_to_org"

assert_gate_409() {
  local label="$1" status="$2" body_file="$3"

  if [ "$status" != "409" ]; then
    fail "$label: expected HTTP 409, got $status"
    return 0
  fi

  local err keys msg
  err="$(jq -r '.detail.error // empty' "$body_file" 2>/dev/null || true)"
  keys="$(jq -r '.detail | keys_unsorted | sort | join(",")' "$body_file" 2>/dev/null || true)"
  msg="$(jq -r '.detail.message // empty' "$body_file" 2>/dev/null || true)"

  if [ "$err" != "$EXPECTED_409_ERROR" ]; then
    fail "$label: 409 body has .detail.error='$err', expected '$EXPECTED_409_ERROR'"
    return 0
  fi
  # Shape, not prose: the key set is asserted exactly so an extra/renamed field
  # is caught, while the human-readable message is only required to be present
  # (asserting its wording would break the eval on a harmless copy edit).
  if [ "$keys" != "error,message" ]; then
    fail "$label: 409 .detail keys are '$keys', expected exactly 'error,message'"
    return 0
  fi
  if [ -z "$msg" ]; then
    fail "$label: 409 .detail.message is empty"
    return 0
  fi

  pass "$label: 409 with verbatim {\"detail\":{\"error\":\"$EXPECTED_409_ERROR\",\"message\":...}}"
  return 0
}

# "Not gated" — used where the correct verdict is "anything but the approval
# gate". Upstream may legitimately answer 200, 400 or 429; only the gate's 409
# is a failure.
assert_not_gated() {
  local label="$1" status="$2" body_file="$3"
  if [ "$status" = "409" ] && [ "$(jq -r '.detail.error // empty' "$body_file" 2>/dev/null || true)" = "$EXPECTED_409_ERROR" ]; then
    fail "$label: blocked by the approval gate (409 $EXPECTED_409_ERROR) but should have passed"
    return 0
  fi
  # A transient edge error is NOT evidence of "not gated" — it is a broken
  # request. http_get/http_post_json already retry these, so reaching here with
  # a 5xx means the retries were exhausted; report it rather than pass falsely.
  case "$status" in
    502|503|504|000)
      fail "$label: edge error HTTP $status after retries — could not determine gate behavior"
      return 0
      ;;
  esac
  pass "$label: not gated (HTTP $status)"
  return 0
}

assert_sentinel() {
  local label="$1" text="$2"
  if [ -z "$text" ]; then
    fail "$label: no text returned"
    return 0
  fi
  case "$text" in
    *"$SENTINEL"*) pass "$label: model echoed the sentinel (real inference)" ;;
    *) fail "$label: sentinel '$SENTINEL' absent from the reply" ; return 0 ;;
  esac
}

# The permissions asserted are the ones the file has ON THE LAPTOP, so the stat
# runs in the pod. Statting a copy on the runner would prove nothing: kubectl cp
# does not preserve modes.
assert_mode() {
  local label="$1" path="$2" expected="$3"
  local actual
  actual="$(laptop stat -c '%a' "$path" 2>/dev/null || echo "?")"
  if [ "$actual" = "$expected" ]; then
    pass "$label: $path is $expected"
  else
    fail "$label: $path is $actual, expected $expected"
  fi
}

# =============================================================================
# Dry run
# =============================================================================
# Stubs every external CLI so phase ordering, the flag read-then-restore
# contract and cleanup-on-failure can be tested in CI without touching AWS.
setup_dry_run_stubs() {
  local bin="$WORKDIR/stub-bin"
  mkdir -p "$bin"

  # The stubs keep their own little world in here: the flag value the kubectl
  # stub was last told to set, and the org_id the psql stub last saw inserted.
  # The curl stub then DERIVES the gate's answer from that state using the real
  # exemption order, instead of the harness telling it what to say. That
  # distinction matters: a stub that is handed the expected answer per phase
  # cannot fail, so `--dry-run --inject-failure` would pass a deliberately
  # broken run and the acceptance check would be worthless.
  export EVAL_STUB_STATE_DIR="$WORKDIR/stub-state"
  mkdir -p "$EVAL_STUB_STATE_DIR"
  printf '%s' "${EVAL_STUB_FLAG:-false}" > "$EVAL_STUB_STATE_DIR/flag"

  cat > "$bin/aws" <<'STUB'
#!/usr/bin/env bash
case "$*" in
  *"ssm get-parameter"*cognito-user-pool-id*) echo "us-east-1_STUBPOOL" ;;
  *"ssm get-parameter"*cognito-client-id*)    echo "stubclientid0000000000" ;;
  *"ssm get-parameter"*cloudfront-domain*)    echo "stub.cloudfront.net" ;;
  *"ssm get-parameter"*rds-host*)             echo "stub-rds.example.com" ;;
  *"ssm get-parameter"*rds-database-name*)    echo "bedrockgateway" ;;
  *"secretsmanager list-secrets"*)            echo "arn:aws:secretsmanager:us-east-1:000000000000:secret:rds!db-stub" ;;
  *"secretsmanager get-secret-value"*)        echo '{"username":"stub","password":"stubpassword"}' ;;
  *"rds generate-db-auth-token"*)             echo "stub-iam-auth-token" ;;
  *"cognito-idp admin-create-user"*)          echo '{"User":{"Username":"stub"}}' ;;
  # The two admin-get-user queries must be distinguished: `sub` resolves, while
  # `custom:org_id` must come back EMPTY. Conflating them makes the B3 premise
  # check report a populated claim and fail a run that is actually correct.
  *"cognito-idp admin-get-user"*"custom:org_id"*) echo -n "" ;;
  *"cognito-idp admin-get-user"*)             echo "11111111-2222-3333-4444-555555555555" ;;
  *"cognito-idp initiate-auth"*)              echo '{"AuthenticationResult":{"AccessToken":"stub.access.token","RefreshToken":"stub-refresh-token"}}' ;;
  *) echo "{}" ;;
esac
exit 0
STUB

  # The kubectl stub records every invocation to $EVAL_STUB_KUBECTL_LOG so the
  # tests can assert the read-then-restore contract: what was read at start, and
  # that the restore wrote back exactly that (or removed the var) — and, since
  # #4171, that every laptop command went through `kubectl exec` rather than
  # running on the runner.
  # EVAL_STUB_FLAG controls the simulated starting state:
  #   "true"/"false" — a deployment-level env override exists with that value
  #   "unset"        — no deployment override (the configmap supplies it)
  cat > "$bin/kubectl" <<'STUB'
#!/usr/bin/env bash
[ -n "${EVAL_STUB_KUBECTL_LOG:-}" ] && printf '%s\n' "$*" >> "$EVAL_STUB_KUBECTL_LOG"

# ── The clean-room pod (#4171) ───────────────────────────────────────────────
# `run` / `wait` / `delete pod` are no-ops; the tests assert them from the log.
#
# `exec` deliberately is NOT a no-op: it strips the kubectl wrapper and runs the
# command LOCALLY. That is what keeps the dry run worth having — Phase C still
# exercises the real logic (file modes, the JWT shape, the helper's own
# import/token code) against a real filesystem, with no cluster. Safe because
# $POD_WORKDIR points inside the run's own scratch directory under --dry-run.
case "${1:-}" in
  exec)
    shift
    # Consume kubectl's own flags/pod name up to the `--` separator.
    while [ $# -gt 0 ]; do
      case "$1" in
        --)                   shift; break ;;
        -n|--namespace)       shift 2 ;;
        -c|--container)       shift 2 ;;
        *)                    shift ;;
      esac
    done
    [ $# -eq 0 ] && exit 0
    exec "$@"
    ;;
  run|wait|cp) exit 0 ;;
  delete)
    case "$*" in *pod*) exit 0 ;; esac
    ;;
esac

# Record what the flag was last set to, so the curl stub can honour it.
# NB: join into a plain variable first — ${*##pat} applies the pattern to EACH
# positional parameter and re-joins, which silently yields nonsense here.
all="$*"
case "$all" in
  *"set env"*BG_ENFORCE_ORG_ASSIGNMENT=*)
    v="${all##*BG_ENFORCE_ORG_ASSIGNMENT=}"; v="${v%% *}"
    [ -n "${EVAL_STUB_STATE_DIR:-}" ] && printf '%s' "$v" > "$EVAL_STUB_STATE_DIR/flag" ;;
  *"set env"*BG_ENFORCE_ORG_ASSIGNMENT-*)
    [ -n "${EVAL_STUB_STATE_DIR:-}" ] && printf 'false' > "$EVAL_STUB_STATE_DIR/flag" ;;
esac
case "$*" in
  *"jsonpath"*"spec.template.spec.containers"*ENFORCE*)
    # The deployment-level override. Empty output = no override set.
    case "${EVAL_STUB_FLAG:-false}" in
      unset) echo -n "" ;;
      *)     echo "${EVAL_STUB_FLAG:-false}" ;;
    esac ;;
  *"configmap"*ENFORCE*|*"jsonpath"*data*ENFORCE*)
    echo "false" ;;
  *) echo "stub-kubectl: $*" >&2 ;;
esac
exit 0
STUB

  cat > "$bin/psql" <<'STUB'
#!/usr/bin/env bash
# Positional args after flags are ignored; -c carries the SQL.
sql=""
while [ $# -gt 0 ]; do
  [ "$1" = "-c" ] && sql="$2"
  shift
done
approved="${EVAL_STUB_STATE_DIR:-/tmp}/approved_org"
case "$sql" in
  *"INSERT INTO users"*)
    # Capture the org_id actually inserted — including the EMPTY one that
    # --inject-failure wrong-org uses. This is the row the gate's DB fallback
    # would read, so it decides whether the next request is admitted.
    org="${sql#*VALUES (\'}"; org="${org#*\', \'}"; org="${org%%\'*}"
    printf '%s' "$org" > "$approved" ;;
  *"DELETE FROM users"*)  rm -f "$approved" ;;
  *"FROM organizations"*) echo "stub-org" ;;
  *"FROM teams"*)         echo "stub-team" ;;
  *"FROM users"*)         [ -f "$approved" ] && cat "$approved" || echo -n "" ;;
  *"FROM usage_logs"*)    echo "1" ;;
  *) : ;;
esac
exit 0
STUB

  cat > "$bin/curl" <<'STUB'
#!/usr/bin/env bash
# Writes a plausible body to -o and, when the caller asked for it with -w,
# prints the status. Honours -f/--fail (exit 22 on >=400) because the phase-C
# soft-skip for bg-gateway-proxy.py depends on the exit code, not the body.
out=""; url=""; want_status=false; fail_on_error=false
prev=""
for a in "$@"; do
  [ "$prev" = "-o" ] && out="$a"
  case "$a" in
    http*) url="$a" ;;
    -w|--write-out) want_status=true ;;
    --fail) fail_on_error=true ;;
    -*) case "$a" in *f*) [ "${a#--}" = "$a" ] && fail_on_error=true ;; esac ;;
  esac
  prev="$a"
done
body='{"ok":true}'
status=200
case "$url" in
  *cognito-config*)
    body='{"user_pool_id":"us-east-1_STUBPOOL","client_id":"stubclientid0000000000","region":"us-east-1","identity_pool_id":""}' ;;
  *bg-cognito-auth.sh*)
    # A miniature bg-cognito-auth.sh: enough of import/token/serve for the dry
    # run to exercise the real assertions (file modes, JWT shape, proxy port)
    # without a network or a Cognito pool. Keep in sync with cli/README.md.
    body='#!/usr/bin/env bash
set -euo pipefail
cmd="${1:-}"; shift || true
d="$HOME/.bedrock-gateway"
case "$cmd" in
  import)
    refresh="$(cat)"
    mkdir -p "$d"; chmod 700 "$d"
    umask 077
    printf "{\"refresh_token\":\"%s\"}" "$refresh" > "$d/tokens.json"
    printf "{\"gateway_url\":\"stub\"}" > "$d/config.json"
    chmod 600 "$d/tokens.json" "$d/config.json"
    ;;
  token)
    # Three dot-separated segments, i.e. JWT-shaped, as the real helper prints.
    echo "eyJzdHViIjoxfQ.eyJzdHViIjoyfQ.c3R1YnNpZw"
    ;;
  serve)
    port=9191
    while [ $# -gt 0 ]; do [ "$1" = "--port" ] && port="$2"; shift; done
    # Bind the port so the readiness probe in C10 has something real to find.
    exec python3 -c "import socket,sys,time
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind((\"127.0.0.1\",int(sys.argv[1]))); s.listen(8)
while True: time.sleep(1)" "$port"
    ;;
  *) echo "stub-helper: unknown command $cmd" >&2; exit 2 ;;
esac' ;;
  *bg-gateway-proxy.py*)
    status=404; body='{"detail":{"error":"script_not_found","message":"Unknown CLI script"}}' ;;
  *v1/chat/completions*|*v1/messages*|*openai/v1/responses*)
    # Re-implement the middleware's exemption order (approval_middleware.py):
    #   flag off -> allow; is_admin -> allow; non-empty org in the users row ->
    #   allow; otherwise 409. Derived from the stub state the kubectl and psql
    #   stubs recorded, so a wrong approval genuinely produces a 409 here.
    sd="${EVAL_STUB_STATE_DIR:-/tmp}"
    flag="false"; [ -f "$sd/flag" ] && flag="$(cat "$sd/flag")"
    approved_org=""; [ -f "$sd/approved_org" ] && approved_org="$(cat "$sd/approved_org")"
    # Which identity is calling is carried by the curl config file (-K), the
    # only place the token lives; ADMIN.curlrc means the admin identity.
    is_admin=false
    case "$*" in *ADMIN.curlrc*) is_admin=true ;; esac

    if [ "$flag" != "true" ] || [ "$is_admin" = true ] || [ -n "$approved_org" ]; then
      body='{"content":[{"type":"text","text":"CLI-ONBOARDING-EVAL-OK"}],"choices":[{"message":{"content":"CLI-ONBOARDING-EVAL-OK"}}]}'
    else
      status=409
      body='{"detail":{"error":"user_not_assigned_to_org","message":"Your account is pending approval. Ask a platform admin to approve your access."}}'
    fi ;;
  *auth/me*)      body='{"user_id":"stub","org_id":"","is_admin":false}' ;;
  *access/status*) body='{"status":"registered"}' ;;
esac
if [ "$status" -ge 400 ] && [ "$fail_on_error" = true ]; then
  # curl -f writes nothing to -o and exits 22. C5b's soft-skip relies on this.
  exit 22
fi
[ -n "$out" ] && printf '%s' "$body" > "$out"
[ "$want_status" = true ] && printf '%s' "$status"
exit 0
STUB

  cat > "$bin/npm" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB

  # laptop_provision() apt-installs the laptop's baseline tooling. Under --dry-run
  # the kubectl stub runs exec'd commands locally, so this must not reach the real
  # apt — it would need root and would mutate the machine running the tests.
  cat > "$bin/apt-get" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB

  cat > "$bin/claude" <<'STUB'
#!/usr/bin/env bash
echo "CLI-ONBOARDING-EVAL-OK"
exit 0
STUB

  cat > "$bin/codex" <<'STUB'
#!/usr/bin/env bash
echo "CLI-ONBOARDING-EVAL-OK"
exit 0
STUB

  chmod +x "$bin"/*
  PATH="$bin:$PATH"
  export PATH

  # The kubectl stub runs exec'd commands locally, and laptop() replaces the
  # environment wholesale — so the "pod" PATH has to name the stub directory too,
  # or the laptop steps would find the machine's real curl/npm/claude.
  POD_PATH="$bin:$POD_PATH"

  # The in-pod clean-room gate is a real re-invocation of this script, so the two
  # test-only escape hatches have to be forwarded to it. This is the ONLY place
  # they are ever set for the pod: a live run leaves POD_ASSERT_ENV empty, which
  # is why the "the workflow never sets them" test still means something.
  POD_ASSERT_ENV=()
  if [ -n "${EVAL_SKIP_CLI_PATH_CHECK:-}" ]; then
    POD_ASSERT_ENV+=("EVAL_SKIP_CLI_PATH_CHECK=$EVAL_SKIP_CLI_PATH_CHECK")
  fi
  if [ -n "${EVAL_FORBIDDEN_PORTS:-}" ]; then
    POD_ASSERT_ENV+=("EVAL_FORBIDDEN_PORTS=$EVAL_FORBIDDEN_PORTS")
  fi

  log "dry-run: stubbed aws/kubectl/psql/curl/npm/apt-get/claude/codex on PATH; kubectl exec runs locally under $POD_WORKDIR"
}

# maybe_fail_phase lets the tests prove that a mid-run explosion still restores
# the flag and deletes the throwaway resources.
maybe_fail_phase() {
  if [ -n "$FAIL_PHASE" ] && [ "$FAIL_PHASE" = "$1" ]; then
    die "injected failure in phase $1 (--fail-phase)"
  fi
}

# =============================================================================
# Phase 0 — resolve configuration (harness only)
# =============================================================================
resolve_config() {
  phase "0" "resolve deployment configuration"

  USER_POOL_ID="$(eval_ssm "/adp/${ENVIRONMENT}/gateway/cognito-user-pool-id")"
  CLIENT_ID="$(eval_ssm "/adp/${ENVIRONMENT}/gateway/cognito-client-id")"
  CF_DOMAIN="$(eval_ssm "/adp/${ENVIRONMENT}/gateway/cloudfront-domain")"
  RDS_HOST="$(eval_ssm "/adp/${ENVIRONMENT}/gateway/rds-host")"
  RDS_DB="$(eval_ssm "/adp/${ENVIRONMENT}/gateway/rds-database-name")"

  local v
  for v in USER_POOL_ID CLIENT_ID CF_DOMAIN RDS_HOST RDS_DB; do
    [ -n "${!v}" ] || die "SSM lookup for $v is empty — is ${ENVIRONMENT} deployed?"
  done

  # CloudFront strips the /api prefix before the ALB, so /api IS the gateway
  # root as every client (and cli/README.md) sees it.
  GATEWAY_URL="https://${CF_DOMAIN}/api"
  log "gateway: $GATEWAY_URL   pool: $USER_POOL_ID"

  resolve_db_creds

  # The org to approve the throwaway user into. A real org is used rather than a
  # synthetic id so budget/rate-limit lookups behave as they do for a real user.
  if [ -z "${EVAL_ORG_ID:-}" ]; then
    EVAL_ORG_ID="$(h_psql -c "SELECT id FROM organizations ORDER BY created_at LIMIT 1;" | head -1)"
  fi
  [ -n "$EVAL_ORG_ID" ] || die "No organization found to approve into (set EVAL_ORG_ID)"
  EVAL_TEAM_ID="$(h_psql -c "SELECT id FROM teams WHERE org_id = '${EVAL_ORG_ID}' LIMIT 1;" | head -1)"
  [ -n "$EVAL_TEAM_ID" ] || EVAL_TEAM_ID="${EVAL_USER_PREFIX}-team"
  log "approval target org: $EVAL_ORG_ID (team $EVAL_TEAM_ID)"

  # Read the flag BEFORE touching it and persist it, so restore puts back what
  # was actually there instead of assuming "false". A run that assumed false
  # would silently disable the gate in an env where it had been turned on.
  #
  # Read the SAME place set_flag writes — the deployment's container env — or
  # restore would be asymmetric. `kubectl set env` on the deployment wins over
  # the configmap's envFrom, so reading the configmap while writing the
  # deployment could "restore" a value that the pods never actually saw.
  local flag_at_start
  flag_at_start="$(h_kubectl get "$K8S_DEPLOYMENT" -n "$K8S_NAMESPACE" \
    -o "jsonpath={.spec.template.spec.containers[0].env[?(@.name=='${FLAG_NAME}')].value}" 2>/dev/null || echo "")"

  if [ -n "$flag_at_start" ]; then
    # An explicit deployment env var existed: restore it to this literal value.
    state_set FLAG_AT_START "$flag_at_start"
    state_set FLAG_SOURCE deployment
    pass "flag read before mutation: ${FLAG_NAME}=${flag_at_start} (deployment env; will be restored to this)"
  else
    # No explicit env var. The effective value comes from the configmap (or the
    # app default). Restore must REMOVE the var rather than pin a literal, so the
    # deployment is left exactly as found and keeps tracking the configmap.
    local cm_value
    cm_value="$(h_kubectl get configmap bedrockgateway-config -n "$K8S_NAMESPACE" \
      -o "jsonpath={.data.${FLAG_NAME}}" 2>/dev/null || echo "")"
    state_set FLAG_AT_START "${cm_value:-false}"
    state_set FLAG_SOURCE unset
    pass "flag read before mutation: ${FLAG_NAME}=${cm_value:-unset} (no deployment env override; the override will be removed on restore)"
  fi
}

# -----------------------------------------------------------------------------
# The flag lever — the documented one: env change + pod recycle. The middleware
# reads get_settings() per request, so no image rebuild is involved.
# -----------------------------------------------------------------------------
set_flag() {
  local value="$1"
  log "setting ${FLAG_NAME}=${value} and waiting for rollout"

  # FLAG_MUTATED is recorded BEFORE the call that mutates, not after: if `set env`
  # succeeds but the rollout wait times out, the deployment has still changed and
  # cleanup must restore it. Recording afterwards would leave dev mis-flagged.
  state_set FLAG_MUTATED true

  # Explicit checks, not bare `set -e`: phases run as an `if` condition, which
  # suspends errexit, so an unchecked failure here would let the phase assert a
  # gate state that was never actually applied.
  h_kubectl set env "$K8S_DEPLOYMENT" -n "$K8S_NAMESPACE" "${FLAG_NAME}=${value}" >/dev/null \
    || die "could not set ${FLAG_NAME}=${value} on ${K8S_DEPLOYMENT}"
  h_kubectl rollout status "$K8S_DEPLOYMENT" -n "$K8S_NAMESPACE" --timeout=300s >/dev/null \
    || die "rollout after ${FLAG_NAME}=${value} did not complete in 300s"

  # `kubectl rollout status` returns when K8S reports the new pods Ready, but the
  # ALB target group lags: old targets drain and new ones clear health checks a
  # few seconds later. A request through CloudFront→ALB in that window hits a
  # deregistering target and comes back 502 — which previously made the very
  # first gated assertion after the flip (B1 /v1/messages) flake to 502 while the
  # calls milliseconds behind it saw the real 409. Settle until the edge serves
  # the app again (any non-5xx: 200/401/409 all mean "serving") before asserting.
  # A logic response is never 502/503/504 — those come only from the edge — so
  # this cannot mask a gate bug. Best-effort: needs the user creds to already
  # exist, which they do by the time either phase flips the flag.
  if [ -f "$WORKDIR/USER.curlrc" ] && [ -n "${GATEWAY_URL:-}" ]; then
    local settle_body settle_out settle_status i
    settle_body="$WORKDIR/settle-body.json"
    settle_out="$WORKDIR/settle-out.json"
    printf '{"model":"%s","max_tokens":1,"messages":[{"role":"user","content":"ping"}]}' \
      "$EVAL_MODEL" > "$settle_body"
    for i in $(seq 1 30); do
      settle_status="$(curl -sS -o "$settle_out" -w '%{http_code}' -X POST \
        -K "$WORKDIR/USER.curlrc" -H 'content-type: application/json' \
        --data-binary "@${settle_body}" --max-time 30 \
        "${GATEWAY_URL}/v1/messages" 2>/dev/null || echo "000")"
      case "$settle_status" in
        502|503|504|000) sleep 2 ;;
        *) [ "$i" -gt 1 ] && log "edge settled after ${i} probe(s) (HTTP $settle_status)"; break ;;
      esac
    done
    rm -f "$settle_body" "$settle_out" 2>/dev/null || true
  fi
}

# =============================================================================
# Tier-1 identity seeding — seed_user() lives in lib/cognito.sh
# =============================================================================
# The broker's GitHub-OAuth output is just a Cognito user plus tokens, so an
# equivalent session is minted directly. Deterministic, and needs no bot GitHub
# account (that's Tier 2, out of scope — see the header).
#
# THIS eval calls seed_user with NO extra attributes, so custom:org_id is never
# set. The pre-token-generation Lambda copies user attributes into the access
# token, so leaving it unset is what keeps the org_id claim blank and forces the
# middleware down its DB-fallback path — the one leg the 2026-08-26 manual
# validation skipped. (The budget/rate-limit eval does the opposite and passes the
# tenant claims explicitly, because its hierarchy is built from them.)

# The approval an admin performs: a users row with a non-empty org_id keyed on
# the Cognito sub. The claim stays blank on purpose.
insert_approval_row() {
  local sub="$1" username="$2" org_id="$3"
  local row_id sub_prefix
  sub_prefix="$(printf '%s' "$sub" | cut -c1-8)"
  row_id="${EVAL_USER_PREFIX}-${EVAL_RUN_ID}-${sub_prefix}"

  h_psql -c "INSERT INTO users (id, org_id, team_id, email, name, cognito_sub, created_at)
             VALUES ('${row_id}', '${org_id}', '${EVAL_TEAM_ID}', '${username}',
                     'eval-cli-onboarding', '${sub}', now());" >/dev/null
  local existing
  existing="$(state_get USERS_ROW_IDS)"
  state_set USERS_ROW_IDS "${existing:+${existing},}${row_id}"
  log "inserted users row ${row_id} (org_id=${org_id})"
}

# =============================================================================
# Phase A — gate OFF: an un-approved user is not blocked
# =============================================================================
# Establishes the baseline the gate is measured against. Without it, a Phase-B
# 409 could just as easily mean "the gateway is broken for everyone".
run_phase_a() {
  phase "A" "gate OFF — un-approved inference is not blocked"
  maybe_fail_phase A

  # Explicitly set false rather than trusting the env's current value: the
  # baseline must be deterministic. FLAG_AT_START is what gets restored.
  set_flag false

  local body="$WORKDIR/a-body.json" out="$WORKDIR/a-out.json" status
  anthropic_body "$body" "Reply with exactly: ${SENTINEL}"
  status="$(http_post_json "$WORKDIR/USER.curlrc" "${GATEWAY_URL}/v1/messages" "$body" "$out")"
  assert_not_gated "A1 /v1/messages with gate off" "$status" "$out"
}

# =============================================================================
# Phase B — gate ON: the matrix
# =============================================================================
run_phase_b() {
  phase "B" "gate ON — 409 shape, non-spend paths, DB fallback, admin exemption"
  maybe_fail_phase B

  set_flag true

  local body out status
  body="$WORKDIR/b-body.json"

  # B1 — every enforced inference path returns the gate's own 409. All three are
  # asserted because /openai/v1/responses has slipped enforcement twice before
  # (#2792 / #2809) and a path-registry regression would only show up here.
  anthropic_body "$body" "Reply with exactly: ${SENTINEL}"
  out="$WORKDIR/b1-messages.json"
  status="$(http_post_json "$WORKDIR/USER.curlrc" "${GATEWAY_URL}/v1/messages" "$body" "$out")"
  assert_gate_409 "B1 /v1/messages" "$status" "$out"

  openai_body "$body" "Reply with exactly: ${SENTINEL}"
  out="$WORKDIR/b1-chat.json"
  status="$(http_post_json "$WORKDIR/USER.curlrc" "${GATEWAY_URL}/v1/chat/completions" "$body" "$out")"
  assert_gate_409 "B1 /v1/chat/completions" "$status" "$out"

  responses_body "$body" "Reply with exactly: ${SENTINEL}"
  out="$WORKDIR/b1-responses.json"
  status="$(http_post_json "$WORKDIR/USER.curlrc" "${GATEWAY_URL}/openai/v1/responses" "$body" "$out")"
  assert_gate_409 "B1 /openai/v1/responses" "$status" "$out"

  # B2 — the gate covers spend paths only. If it leaked onto /auth/me or
  # /access/status, a pending user could not even see the "request access"
  # screen: the product would be unusable for exactly the people it gates.
  out="$WORKDIR/b2-me.json"
  status="$(http_get "$WORKDIR/USER.curlrc" "${GATEWAY_URL}/auth/me" "$out")"
  assert_not_gated "B2 /auth/me reachable while gated" "$status" "$out"

  out="$WORKDIR/b2-access.json"
  status="$(http_get "$WORKDIR/USER.curlrc" "${GATEWAY_URL}/access/status" "$out")"
  assert_not_gated "B2 /access/status reachable while gated" "$status" "$out"

  # B3 — the DB-fallback leg. Approve in Postgres only; the token in hand still
  # carries a blank org_id claim, so a pass here can ONLY have come from the
  # Postgres read. This is the leg the manual validation skipped.
  local approve_org="$EVAL_ORG_ID"
  if [ "$INJECT_FAILURE" = "wrong-org" ]; then
    # Deliberately-broken acceptance run: an empty org_id must NOT satisfy the
    # gate. If B3 passes here, the eval is not actually asserting anything.
    approve_org=""
    log "--inject-failure wrong-org: approving with an EMPTY org_id; B3 is expected to FAIL"
  fi
  insert_approval_row "$(state_get USER_SUB)" "$EVAL_USERNAME" "$approve_org"

  # The middleware queries per request with no caching, but the row must be
  # committed and visible to the pod's pool before we re-ask.
  sleep 5

  anthropic_body "$body" "Reply with exactly: ${SENTINEL}"
  out="$WORKDIR/b3-approved.json"
  status="$(http_post_json "$WORKDIR/USER.curlrc" "${GATEWAY_URL}/v1/messages" "$body" "$out")"
  assert_not_gated "B3 approved via DB row (org claim still blank) — DB fallback" "$status" "$out"

  # Prove the premise rather than assume it: if the claim were populated, B3
  # would have passed on the fast path and proven nothing about the fallback.
  local claim_org
  # shellcheck disable=SC2016  # JMESPath backticks, not a shell expansion
  claim_org="$(h_aws cognito-idp admin-get-user --user-pool-id "$USER_POOL_ID" \
    --username "$EVAL_USERNAME" \
    --query 'UserAttributes[?Name==`custom:org_id`].Value' --output text 2>/dev/null || echo "")"
  if [ -z "$claim_org" ] || [ "$claim_org" = "None" ]; then
    pass "B3 premise holds: custom:org_id is unset, so the org_id claim is blank"
  else
    fail "B3 premise broken: custom:org_id='$claim_org' — the fast path may have passed instead of the DB fallback"
  fi

  # B4 — admin exemption with a blank org. The admin who approves everyone must
  # never be the first person locked out (the #3984 self-lockout class).
  seed_user "$EVAL_ADMIN_USERNAME" "ADMIN" "admin"
  anthropic_body "$body" "Reply with exactly: ${SENTINEL}"
  out="$WORKDIR/b4-admin.json"
  status="$(http_post_json "$WORKDIR/ADMIN.curlrc" "${GATEWAY_URL}/v1/messages" "$body" "$out")"
  assert_not_gated "B4 admin with blank org exempt" "$status" "$out"
}

# =============================================================================
# Phase C — the laptop journey
# =============================================================================
# Everything here runs through laptop(), i.e. INSIDE the clean-room pod: a fresh
# HOME on a machine that has no platform credential to borrow. The helper is
# fetched from the LIVE download route, never the checkout — the eval must test
# what is deployed, not what is in the repo.
#
# The harness keeps the assertions (it has jq and psql); the pod does the doing.
# Response bodies are copied back for assertion; TOKENS ARE NOT — they are minted,
# used and destroyed inside the pod.
run_phase_c() {
  phase "C" "the laptop journey in the clean-room pod (no credentials in reach)"
  maybe_fail_phase C

  laptop_provision

  # C5 — download the helper from the real shipped route.
  local helper="$POD_HOME/bin/bg-cognito-auth.sh"
  if laptop curl -fsS -o "$helper" "${GATEWAY_URL}/cli/bg-cognito-auth.sh"; then
    laptop chmod +x "$helper"
    if laptop head -1 "$helper" | grep -q '^#!'; then
      pass "C5 downloaded bg-cognito-auth.sh from the live route ${GATEWAY_URL}/cli/bg-cognito-auth.sh"
    else
      fail "C5 downloaded bg-cognito-auth.sh but it has no shebang — served the SPA HTML fallback?"
      return 1
    fi
  else
    fail "C5 could not download bg-cognito-auth.sh from ${GATEWAY_URL}/cli/bg-cognito-auth.sh"
    return 1
  fi

  # C5b — bg-gateway-proxy.py is a SOFT SKIP until #4156 allowlists it in
  # src/cli_download/routes.py. When that lands this branch flips to a pass with
  # no edit here; until then a 404 is the expected, correct behaviour.
  local proxy_file="$POD_HOME/bin/bg-gateway-proxy.py"
  local proxy_downloaded=false
  if laptop curl -fsS -o "$proxy_file" "${GATEWAY_URL}/cli/bg-gateway-proxy.py" 2>/dev/null \
     && laptop head -1 "$proxy_file" | grep -q 'python\|^#!'; then
    proxy_downloaded=true
    pass "C5b bg-gateway-proxy.py is served by the download route (#4156 has landed)"
  else
    laptop rm -f "$proxy_file" || true
    skip "C5b bg-gateway-proxy.py is not downloadable yet — expected until #4156 allowlists it; Codex leg (C10) will be skipped"
  fi

  # C6 — public discovery. The CLI fetches this before it holds any token, so
  # it must be reachable unauthenticated and agree with SSM. The fetch happens on
  # the laptop; the comparison happens here, where the SSM values live.
  local disco="$WORKDIR/cognito-config.json"
  if laptop curl -fsS -o "$POD_WORKDIR/cognito-config.json" "${GATEWAY_URL}/.well-known/cognito-config" \
     && laptop_get_file "$POD_WORKDIR/cognito-config.json" "$disco"; then
    local d_pool d_client d_region
    d_pool="$(jq -r '.user_pool_id // empty' "$disco")"
    d_client="$(jq -r '.client_id // empty' "$disco")"
    d_region="$(jq -r '.region // empty' "$disco")"
    if [ "$d_pool" = "$USER_POOL_ID" ] && [ "$d_client" = "$CLIENT_ID" ] && [ -n "$d_region" ]; then
      pass "C6 /.well-known/cognito-config returns pool/client/region matching SSM"
    else
      fail "C6 discovery disagrees with SSM (pool='$d_pool' client='$d_client' region='$d_region')"
    fi
  else
    fail "C6 /.well-known/cognito-config unreachable"
  fi

  # C7 — import via STDIN. The refresh token is piped into `kubectl exec -i` and
  # so never reaches argv — neither the helper's, nor kubectl's (an exec'd command
  # line is visible in the exec API and in the runner's process table).
  if laptop bash "$helper" import --gateway-url "$GATEWAY_URL" < "$WORKDIR/USER.refresh" >/dev/null 2>"$WORKDIR/import.err"; then
    pass "C7 import succeeded with the refresh token piped on stdin (never in argv)"
  else
    fail "C7 import failed: $(tail -2 "$WORKDIR/import.err" | tr '\n' ' ')"
    return 1
  fi

  assert_mode "C7 perms" "$POD_HOME/.bedrock-gateway" 700
  assert_mode "C7 perms" "$POD_HOME/.bedrock-gateway/tokens.json" 600
  assert_mode "C7 perms" "$POD_HOME/.bedrock-gateway/config.json" 600

  # C8 — `token` prints a JWT, and that JWT buys a real completion. The JWT is
  # written to a 0600 file INSIDE the pod and never leaves it: only its shape
  # (a dot count) and the completion it bought come back to the harness.
  local pod_tok="$POD_WORKDIR/laptop.token"
  local dots
  # shellcheck disable=SC2016  # $1/$2 are expanded by the pod's shell
  if laptop sh -c 'umask 077; bash "$1" token > "$2"; chmod 600 "$2"' _ "$helper" "$pod_tok" \
       2>"$WORKDIR/token.err"; then
    # shellcheck disable=SC2016  # $1 is expanded by the pod's shell
    dots="$(laptop sh -c 'tr -cd "." < "$1" | wc -c' _ "$pod_tok" | tr -d ' ')"
    if [ "$dots" = "2" ]; then
      pass "C8 token printed a three-segment JWT"
    else
      fail "C8 token output is not a JWT"
      return 1
    fi
  else
    fail "C8 token failed: $(tail -2 "$WORKDIR/token.err" | tr '\n' ' ')"
    return 1
  fi

  local pod_cfg="$POD_WORKDIR/laptop.curlrc" body="$WORKDIR/c8-body.json" out="$WORKDIR/c8-out.json" status
  laptop_write_curl_auth_config "$pod_tok" "$pod_cfg"
  anthropic_body "$body" "Reply with exactly: ${SENTINEL}"
  laptop_put_file "$body" "$POD_WORKDIR/c8-body.json" 644
  status="$(laptop_http_post_json "$pod_cfg" "${GATEWAY_URL}/v1/messages" \
    "$POD_WORKDIR/c8-body.json" "$POD_WORKDIR/c8-out.json")"
  if [ "$status" = "200" ]; then
    laptop_get_file "$POD_WORKDIR/c8-out.json" "$out" || true
    assert_sentinel "C8 direct curl with the helper's JWT" \
      "$(jq -r '[.content[]?.text] | join(" ")' "$out" 2>/dev/null || true)"
  else
    fail "C8 direct curl with the helper's JWT returned HTTP $status"
  fi

  # C9 — Claude Code, configured exactly as the setup page renders it
  # (SetupInstructions.tsx buildAnthropicSettings). Any drift between that
  # component and what actually works is a broken onboarding page.
  log "installing @anthropic-ai/claude-code from npm"
  if laptop npm install -g --silent @anthropic-ai/claude-code >"$WORKDIR/npm-claude.log" 2>&1; then
    # Rendered here (the harness has jq) and streamed into the pod. Nothing in it
    # is secret — the token is fetched at call time by apiKeyHelper, which is the
    # property C9 exists to prove.
    jq -n --arg base "$GATEWAY_URL" --arg helper "bash ${helper} token" --arg model "global.anthropic.claude-opus-4-6-v1" \
      '{env:{ANTHROPIC_BASE_URL:$base}, apiKeyHelper:$helper, apiKeyHelperTtlMs:3300000,
        permissions:{allow:["WebSearch","WebFetch"]}, model:$model}' \
      > "$WORKDIR/claude-settings.json"
    laptop_put_file "$WORKDIR/claude-settings.json" "$POD_HOME/.claude/settings.json" 644

    local answer
    if answer="$(laptop claude -p "Reply with exactly: ${SENTINEL}" 2>"$WORKDIR/claude.err")"; then
      assert_sentinel "C9 Claude Code via apiKeyHelper" "$answer"
    else
      fail "C9 Claude Code run failed: $(tail -3 "$WORKDIR/claude.err" | tr '\n' ' ')"
    fi

    # The answer alone does not prove the request went through the gateway on
    # THIS user's identity — a usage row keyed on the sub does.
    local sub rows
    sub="$(state_get USER_SUB)"
    rows="$(h_psql -c "SELECT count(*) FROM usage_logs WHERE user_id = '${sub}';" | head -1)"
    if [ "${rows:-0}" -gt 0 ]; then
      pass "C9 gateway recorded ${rows} usage row(s) for this user — traffic really transited the gateway"
    else
      fail "C9 no usage_logs row for ${sub:0:8}… — the completion did not go through the gateway on this identity"
    fi
  else
    fail "C9 npm install of @anthropic-ai/claude-code failed: $(tail -3 "$WORKDIR/npm-claude.log" | tr '\n' ' ')"
  fi

  # C10 — Codex through the local auth proxy. Requires the sibling proxy file,
  # so it is skipped for the same reason C5b is, until #4156 lands.
  if [ "$proxy_downloaded" != true ]; then
    skip "C10 Codex zero-touch leg skipped — bg-gateway-proxy.py is not downloadable until #4156"
    return 0
  fi

  laptop chmod +x "$proxy_file" 2>/dev/null || true
  log "installing @openai/codex from npm and starting the auth proxy"
  if ! laptop npm install -g --silent @openai/codex >"$WORKDIR/npm-codex.log" 2>&1; then
    fail "C10 npm install of @openai/codex failed: $(tail -3 "$WORKDIR/npm-codex.log" | tr '\n' ' ')"
    return 0
  fi

  # Started DETACHED inside the pod — not as a backgrounded `kubectl exec` on the
  # runner. Killing a backgrounded kubectl exec does not reliably kill the process
  # it started in the pod, so the pid that matters is the pod-side one, and it is
  # recorded in a pod file for cleanup to read.
  # shellcheck disable=SC2016  # $1/$2/$3 and $! are expanded by the pod's shell
  laptop sh -c 'nohup bash "$1" serve --port "$2" > "$3/proxy.log" 2>&1 & echo $! > "$3/proxy.pid"' \
    _ "$helper" "$PROXY_PORT" "$POD_WORKDIR" </dev/null >/dev/null 2>&1 || true

  local waited=0
  while [ "$waited" -lt 20 ]; do
    laptop_port_is_open "$PROXY_PORT" && break
    sleep 1; waited=$((waited + 1))
  done
  if ! laptop_port_is_open "$PROXY_PORT"; then
    fail "C10 the auth proxy never came up on 127.0.0.1:${PROXY_PORT} in the pod: $(laptop tail -3 "$POD_WORKDIR/proxy.log" 2>/dev/null | tr '\n' ' ')"
    return 0
  fi

  # Config per cli/README.md §"Using Codex". env_key names a var Codex requires
  # to exist but never validates — the proxy discards it and injects the real
  # token, which is the whole point of the zero-touch path.
  cat > "$WORKDIR/codex-config.toml" <<TOML
model = "${EVAL_CODEX_MODEL}"
model_provider = "adp-gateway"

[model_providers.adp-gateway]
name = "ADP Gateway (local auth proxy)"
base_url = "http://127.0.0.1:${PROXY_PORT}/openai/v1"
wire_api = "responses"
env_key = "ADP_GATEWAY_DUMMY"
TOML
  laptop_put_file "$WORKDIR/codex-config.toml" "$POD_HOME/.codex/config.toml" 644

  local codex_out
  if codex_out="$(laptop env ADP_GATEWAY_DUMMY=unused codex exec --skip-git-repo-check \
      "Reply with exactly: ${SENTINEL}" 2>"$WORKDIR/codex.err")"; then
    assert_sentinel "C10 Codex via the local auth proxy" "$codex_out"
  else
    fail "C10 codex exec failed: $(tail -3 "$WORKDIR/codex.err" | tr '\n' ' ')"
  fi
}

# =============================================================================
# Phase D — restore and cleanup
# =============================================================================
# Runs from an EXIT trap AND is reachable standalone via --cleanup-only, so a
# killed job can still be swept. Every step is independently guarded: one
# failure must not skip the rest.
run_cleanup() {
  local rc=$?
  trap - EXIT
  CURRENT_PHASE="D"
  trace "phase:D"
  echo ""
  log "═══ Phase D — restore + cleanup (always runs) ═══"

  # 1. Delete the clean-room pod FIRST. It holds the laptop HOME, the imported
  #    refresh token, the minted JWT and the running auth proxy, so deleting it
  #    destroys all of them in one action — nothing survives the pod.
  #    Swept by LABEL, so a pod leaked by an earlier crashed run goes too, which
  #    is what makes --cleanup-only worth running after a killed job.
  if laptop_pod_delete; then
    log "deleted clean-room pod(s) labelled app=${POD_LABEL_APP} in ${POD_NAMESPACE} (laptop HOME, tokens and the auth proxy went with it)"
  else
    fail "D could not delete the clean-room pod — sweep manually: kubectl delete pod -n ${POD_NAMESPACE} -l app=${POD_LABEL_APP}"
  fi

  # 2. Restore the flag to the value READ AT START — never a hardcoded false.
  #    Leaving dev with the gate off is a silent security regression; leaving it
  #    on when it was off is an inference outage. Both are unacceptable.
  local flag_at_start flag_source restore_arg
  flag_at_start="$(state_get FLAG_AT_START)"
  flag_source="$(state_get FLAG_SOURCE)"
  if [ "$(state_get FLAG_MUTATED)" = "true" ]; then
    # FLAG_SOURCE=unset means there was no deployment-level override before this
    # run, so restoring means DELETING the var (kubectl's `NAME-` syntax), not
    # pinning the value we happened to read from the configmap.
    if [ "$flag_source" = "unset" ]; then
      restore_arg="${FLAG_NAME}-"
    else
      restore_arg="${FLAG_NAME}=${flag_at_start}"
    fi

    if h_kubectl set env "$K8S_DEPLOYMENT" -n "$K8S_NAMESPACE" "$restore_arg" >/dev/null 2>&1 \
       && h_kubectl rollout status "$K8S_DEPLOYMENT" -n "$K8S_NAMESPACE" --timeout=300s >/dev/null 2>&1; then
      pass "D restored the flag as found at start (${restore_arg}; effective value '${flag_at_start}')"
    else
      fail "D COULD NOT RESTORE ${FLAG_NAME} — ${ENVIRONMENT} may be left mis-flagged; fix manually: kubectl set env ${K8S_DEPLOYMENT} -n ${K8S_NAMESPACE} ${restore_arg}"
    fi
  else
    log "flag was never mutated; nothing to restore"
  fi

  # 3. Delete the throwaway users rows.
  local ids id
  ids="$(state_get USERS_ROW_IDS)"
  if [ -n "$ids" ]; then
    IFS=',' read -r -a id_arr <<< "$ids"
    for id in "${id_arr[@]}"; do
      [ -n "$id" ] || continue
      if h_psql -c "DELETE FROM users WHERE id = '${id}';" >/dev/null 2>&1; then
        log "deleted users row $id"
      else
        fail "D could not delete users row $id — delete manually to avoid DB litter"
      fi
    done
  fi

  # 4. Delete the throwaway Cognito users. Idempotent: an already-absent user
  #    (e.g. the always-on "Cleanup sweep" step re-running Phase D after the
  #    matrix step already deleted it) is a clean state, not a failure — mirror
  #    the pod delete's --ignore-not-found semantics so a second sweep stays green.
  local key username
  for key in USER ADMIN; do
    username="$(state_get "${key}_USERNAME")"
    [ -n "$username" ] || continue
    delete_seeded_user "$username"
  done

  # 5. Wipe the harness-side token material. The laptop-side copies went with the
  #    pod in step 1; these are the seeds the harness minted to hand it.
  rm -f "$WORKDIR"/*.access "$WORKDIR"/*.refresh "$WORKDIR"/*.curlrc 2>/dev/null || true

  write_summary
  exit "$rc"
}

# =============================================================================
# Main
# =============================================================================
main() {
  case "$MODE" in
    clean-room)
      assert_clean_room
      exit $?
      ;;
    cleanup)
      if [ "$DRY_RUN" = true ]; then setup_dry_run_stubs; fi
      USER_POOL_ID="$(h_aws ssm get-parameter --name "/adp/${ENVIRONMENT}/gateway/cognito-user-pool-id" \
        --query Parameter.Value --output text 2>/dev/null || echo "")"
      # The sweep deletes users rows via psql, so it needs DB creds too — without
      # this the standalone --cleanup-only run could not connect and reported
      # spurious "could not delete users row" failures.
      resolve_db_creds
      run_cleanup
      ;;
  esac

  # `if`, not `[ ... ] && cmd`: the &&-list form is exempt from `set -e` only
  # because it is a list, which is a subtlety no reader should have to hold in
  # their head while auditing an eval that mutates a live environment.
  if [ "$DRY_RUN" = true ]; then setup_dry_run_stubs; fi

  log "workdir: $WORKDIR   env: $ENVIRONMENT   phases: $PHASES   dry-run: $DRY_RUN"

  # Registered before the first mutation so any later failure still restores.
  trap run_cleanup EXIT

  resolve_config

  # The clean room is created here — AFTER resolve_config (which only reads) and
  # BEFORE seed_user (the first thing that mutates dev). That ordering preserves
  # the property the workflow's old step-1 gate had: a contaminated clean room
  # fails the run in seconds, having changed nothing in the target environment.
  # Only Phase C needs it, and it costs a pod plus a possible node scale-up, so an
  # A,B-only run does not pay for one.
  if phase_enabled C; then
    laptop_pod_create
  fi

  seed_user "$EVAL_USERNAME" "USER" ""

  # Each phase is called DIRECTLY (not through a dispatcher taking a function
  # name) so both a reader and shellcheck can see that these functions are
  # invoked; the `|| rc=$?` form is what keeps an early return from aborting the
  # matrix. See after_phase().
  local rc before
  if phase_enabled A; then
    rc=0; before="$FAILURES"; run_phase_a || rc=$?; after_phase A "$rc" "$before"
  fi
  if phase_enabled B; then
    rc=0; before="$FAILURES"; run_phase_b || rc=$?; after_phase B "$rc" "$before"
  fi
  if phase_enabled C; then
    rc=0; before="$FAILURES"; run_phase_c || rc=$?; after_phase C "$rc" "$before"
  fi

  # run_cleanup fires from the EXIT trap, writes the summary and preserves this
  # exit status.
  if [ "$FAILURES" -gt 0 ]; then
    exit 1
  fi
  exit 0
}

main
