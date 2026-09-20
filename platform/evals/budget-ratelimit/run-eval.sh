#!/usr/bin/env bash
#
# `source-path` must be a FILE-LEVEL directive (in the leading comment block,
# before the first command); attached to a `.` line it covers only that line.
# shellcheck source-path=SCRIPTDIR
#
# =============================================================================
# run-eval.sh — clean-room evaluation of budgets and rate limits
# =============================================================================
# Issue #4163. Seeds a throwaway org structure in dev, defines budget and
# rate-limit configs at every entity level THROUGH THE REAL ADMIN APIs, then
# drives traffic from a clean-room "laptop" pod through BOTH wire paths and
# asserts enforcement, error shapes, accounting and tenant isolation.
#
# WHY A CLEAN ROOM (inherited from #4157/#4171, see platform/evals/lib/pod.sh)
# The harness (this process, on the ARC runner) holds the credentials. The
# traffic is driven from a pod created from a stock image with no
# service-account token and no ambient env, reached only by `kubectl exec`. For
# THIS eval the clean room matters for a specific reason: the agent-worker image
# bakes in a sigv4-proxy and ANTHROPIC_* env, and a request that goes through
# that path authenticates as an AGENT, not as the seeded human — so it is
# enforced against a different budget entity entirely. A contaminated run would
# report false green on every cascading-cap case.
#
# THE SEEDED WORLD (all throwaway, all name-tagged eval-bgt-<run_id>)
#   Org A ─ department D1 ─ team T1 ─ u1, u2
#         │                └ team T2 ─ u3
#         └ org-admin a1                      (does the admin-API writes)
#   Org B ─ x1                                (isolation control)
#
# WHAT THE CASES ASSERT. TPM and observational agent coverage remain findings;
# org ledger/RPM regressions are now assertions against the implemented contract.
#
#   1  user cap        402, details.entity_type == "user"
#   2  team cap        402, details.entity_type == "team"
#   3  dept cap        402, details.entity_type == "department"
#   4  org cap         402, details.entity_type == "org"; other tenant unaffected
#   5  precedence      most-specific exceeded level wins
#   6  no-config       200: an unconfigured level must not deny
#   7  accounting      spend lands on the right entities in budget_usage
#   8  user RPM        429 with the documented shape
#   9  user TPM        FINDING: unreachable — consume_rate_limit is only ever
#                      called with tokens=1, so the TPM bucket is debited one
#                      token per request regardless of real token usage
#  10  concurrent=1    429 limit_type "concurrent"
#  11  org RPM         429 with the documented shape (the org key is fixed)
#  12  defaults        the default limits apply with no config present
#
#   H  the headline question: does human-triggered agent spend land under the
#      triggering human's budget? Answered OBSERVATIONALLY from existing
#      lineage — no agent is dispatched and no attribution is fabricated.
#
# USAGE
#   run-eval.sh                                     # full run against dev
#   run-eval.sh --phases 1,2,3                      # a subset of cases
#   run-eval.sh --dry-run                           # stubbed, no AWS, no cluster
#   run-eval.sh --dry-run --fail-phase 2            # proves cleanup-on-failure
#   run-eval.sh --inject-failure wrong-entity       # deliberately-broken run
#   run-eval.sh --cleanup-only                      # idempotent teardown
#   run-eval.sh --assert-clean-room                 # the contamination gate
#
# ENVIRONMENT
#   ENVIRONMENT       target env (default: dev)
#   AWS_REGION        default: us-east-1
#   EVAL_RUN_ID       unique suffix for throwaway resources (default: local-$$)
#   EVAL_WORKDIR      scratch dir (default: mktemp -d)
#   EVAL_POD_NAMESPACE  namespace for the laptop pod (default: adp-gateway)
#   EVAL_POD_IMAGE    laptop pod image (default: node:20-bookworm)
# =============================================================================

set -euo pipefail

# Absolute path to this script, resolved before anything can cd. The harness
# copies it into the clean-room pod so `--assert-clean-room` runs there verbatim.
EVAL_SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

# The shared harness. Resolved RELATIVE to this script, which is what lets the
# in-pod copy work: laptop_put_harness mirrors the repo layout into the pod as
# harness/<eval>/run-eval.sh + harness/lib/*.sh, so `../lib` resolves the same
# way on the runner and in the clean room — with no env var, because telling the
# clean room where its lib is would mean adding environment to the thing whose
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
# Read by lib/clean-room.sh, which asserts nothing is already listening on the
# port a CLI proxy would use. This eval never starts a proxy; the gate still
# checks the port, because a listener there means the pod is not a clean room.
PROXY_PORT="${EVAL_PROXY_PORT:-9191}"

# The sweep convention: every entity this eval creates carries this prefix, and
# anything matching it is a leaked throwaway from a crashed run.
EVAL_USER_PREFIX="eval-bgt"
EVAL_TAG="${EVAL_USER_PREFIX}-${EVAL_RUN_ID}"
EVAL_SEED_NAME="$EVAL_TAG"
EVAL_SUMMARY_TITLE="Budget + rate-limit eval"

# The seeded world has real organizations, canonical users and memberships.
# Budget writes resolve user entities through those rows; Cognito claims alone
# are insufficient, even though ledger/config org_id columns have no FK.
ORG_A="${EVAL_TAG}-orga"
ORG_B="${EVAL_TAG}-orgb"
DEPT_1="${EVAL_TAG}-d1"
TEAM_1="${EVAL_TAG}-t1"
TEAM_2="${EVAL_TAG}-t2"

U1="${EVAL_TAG}-u1@example.com"
U2="${EVAL_TAG}-u2@example.com"
U3="${EVAL_TAG}-u3@example.com"
A1="${EVAL_TAG}-a1@example.com"
X1="${EVAL_TAG}-x1@example.com"

# The inference model. Both are in enable-bedrock-models.sh's REQUIRED_MODELS,
# so a deploy that passed cannot lack access to them.
EVAL_MODEL="${EVAL_MODEL:-global.anthropic.claude-sonnet-4-6}"
EVAL_CODEX_MODEL="${EVAL_CODEX_MODEL:-openai.gpt-5.6-sol}"

# -----------------------------------------------------------------------------
# The numbers that make this eval deterministic and nearly free
# -----------------------------------------------------------------------------
# BUDGET: enforcement adds a FLAT estimate before comparing —
#   projected = current_spend + _DEFAULT_ESTIMATE_USD($0.05)   [enforcement_middleware.py:38]
#   deny when projected > cap                                  [enforcement_service.py:409]
# so a cap of $0.01 denies on the FIRST request, at zero spend, with no token
# burn and no waiting on the async S3→Lambda ledger. That is what makes cases
# 1-5 deterministic. TRIP_CAP must stay < $0.05 for this to hold.
TRIP_CAP="0.01"
# The "not this level" cap: high enough that no level under test is ever the
# incidental cause of a denial.
OPEN_CAP="1000.00"

# RATE LIMIT: the trip point is BURST CAPACITY, not the nominal limit —
#   max_tokens = int(rpm * burst_multiplier(1.5) / 60 * refill_buffer(10))
#              = max(1, ...)                                   [ratelimit/service.py:216]
# At the default 60 rpm that is 15 tokens, so "the 61st request 429s" is simply
# false. rpm=1 gives capacity 1 (refill 0.0167/s ≈ one token per minute), which
# is the only setting that makes a 429 reachable in a bounded number of requests.
TRIP_RPM=1
TRIP_CONCURRENT=1

# Dev runs the IN-MEMORY limiter backend (RATELIMIT_BACKEND_TYPE unset → "memory";
# BG_REDIS_URL is a different env prefix and never reaches RateLimitConfig), and
# the gateway runs replicas:2 × --workers 4 = 8 independent token buckets behind
# an ALB. So the eval asserts "at least one 429 within N requests", never "request
# number K is the one that 429s". N must exceed 8 × capacity with margin.
RL_BURST_REQUESTS="${EVAL_RL_BURST_REQUESTS:-40}"

# The limiter reloads configs from the DB at most every 60s
# (_DB_RELOAD_INTERVAL, ratelimit/service.py:29). A rate-limit config written
# through the admin API is invisible to enforcement until that elapses, so the
# eval waits it out rather than racing it.
RL_RELOAD_WAIT="${EVAL_RL_RELOAD_WAIT:-70}"

MODE="full"
DRY_RUN=false
FAIL_PHASE=""
INJECT_FAILURE=""
PHASES="1,2,3,4,5,6,7,8,9,10,11,12,H"

# -----------------------------------------------------------------------------
# The laptop pod — the clean room
# -----------------------------------------------------------------------------
POD_NAMESPACE="${EVAL_POD_NAMESPACE:-adp-gateway}"
POD_IMAGE="${EVAL_POD_IMAGE:-node:20-bookworm}"
POD_LABEL_APP="$EVAL_USER_PREFIX"
POD_RUN_LABEL="$(printf '%s' "$EVAL_RUN_ID" | tr -c 'A-Za-z0-9._-' '-' | cut -c1-63)"
# A trailing '-' is an invalid RFC 1123 name, and both tr and cut can leave one.
LAPTOP_POD="${EVAL_USER_PREFIX}-$(printf '%s' "$EVAL_RUN_ID" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | cut -c1-24 | sed 's/-*$//')"

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
    -h|--help)           sed -n '2,75p' "$0"; exit 0 ;;
    *)                   die "Unknown option: $1" ;;
  esac
done

case "$INJECT_FAILURE" in
  ""|wrong-entity) ;;
  *) die "Unknown --inject-failure kind: $INJECT_FAILURE (supported: wrong-entity)" ;;
esac

WORKDIR="${EVAL_WORKDIR:-$(mktemp -d)}"
mkdir -p "$WORKDIR"
chmod 700 "$WORKDIR"
STATE_FILE="$WORKDIR/state.env"
TRACE_FILE="$WORKDIR/trace.log"
RESULTS_FILE="$WORKDIR/results.tsv"
FINDINGS_FILE="$WORKDIR/findings.txt"
# Where burst_until_429() leaves the first 429 body it captured, for the shape
# assertion to read. Defined here, at top level, because that function is always
# called in a command substitution — see the comment on it.
BURST_BODY="$WORKDIR/burst.json"
: > "$TRACE_FILE"
: > "$RESULTS_FILE"
: > "$FINDINGS_FILE"

# Where the emulated developer's HOME lives INSIDE the pod. Under --dry-run the
# kubectl stub runs exec'd commands locally, so these stay inside the run's own
# scratch directory and cannot escape it.
if [ "$DRY_RUN" = true ]; then
  POD_WORKDIR="$WORKDIR/pod"
  POD_HOME="$WORKDIR/pod/home"
  mkdir -p "$POD_WORKDIR" "$POD_HOME"
else
  POD_WORKDIR="/tmp/eval"
  POD_HOME="/tmp/eval/home"
fi
POD_PATH="${POD_HOME}/bin:${POD_HOME}/.npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
POD_EVAL_SCRIPT="${POD_WORKDIR}/harness/budget-ratelimit/run-eval.sh"

# EMPTY in a real run: the clean-room gate must inherit nothing. The dry-run
# tests populate it to prove the gate actually detects contamination.
POD_ASSERT_ENV=()

phase_enabled() { case ",$PHASES," in *,"$1",*) return 0 ;; *) return 1 ;; esac; }

# =============================================================================
# Dry-run stubs
# =============================================================================
# The stubs keep their own little world under $EVAL_STUB_STATE_DIR: the budget
# and rate-limit configs the curl stub was told to create, and the request
# counts per identity. The curl stub then DERIVES each verdict from that state
# by re-implementing the real cascade and the real bucket arithmetic, instead of
# being told per-case what to answer.
#
# That distinction is the whole value of the dry run: a stub handed the expected
# answer cannot fail, so `--dry-run --inject-failure wrong-entity` would report a
# deliberately-broken run as green and the acceptance check would be worthless.
# Here the injection changes only what the eval EXPECTS, the stub still computes
# what the platform WOULD do, and the two genuinely disagree.
setup_dry_run_stubs() {
  local bin="$WORKDIR/stub-bin"
  mkdir -p "$bin"

  export EVAL_STUB_STATE_DIR="$WORKDIR/stub-state"
  mkdir -p "$EVAL_STUB_STATE_DIR/budgets" "$EVAL_STUB_STATE_DIR/ratelimits" "$EVAL_STUB_STATE_DIR/counts"

  # The stub world needs to know which identity maps to which tenant claims, so
  # the curl stub can walk the same user→team→department→org hierarchy the real
  # enforcement service walks. Written here rather than discovered so the stub
  # stays independent of Cognito.
  cat > "$EVAL_STUB_STATE_DIR/hierarchy" <<EOF
U1 ${ORG_A} ${TEAM_1} ${DEPT_1}
U2 ${ORG_A} ${TEAM_1} ${DEPT_1}
U3 ${ORG_A} ${TEAM_2} ${DEPT_1}
X1 ${ORG_B} - -
EOF

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
  # The sub must be DETERMINISTIC PER USERNAME: the eval keys budget configs by
  # sub, and a random sub per call would make the config it wrote unfindable by
  # the request that should be denied by it.
  *"cognito-idp admin-get-user"*)
    u=""
    for a in "$@"; do case "$prev" in --username) u="$a" ;; esac; prev="$a"; done
    # Real Cognito subs are UUIDs, not strings containing our username/run tag.
    python3 -c 'import sys,uuid; print(uuid.uuid5(uuid.NAMESPACE_DNS, sys.argv[1]))' "$u" ;;
  *"cognito-idp initiate-auth"*)              echo '{"AuthenticationResult":{"AccessToken":"stub.access.token","RefreshToken":"stub-refresh-token"}}' ;;
  # Phase H: one human-rooted lineage item, so H1-H5 exercise the real join.
  *"dynamodb scan"*)
    echo '{"Items":[{"event_id":{"S":"stub-event-0001"},"root_human_id":{"S":"stub-root-human"}}]}' ;;
  *) echo "{}" ;;
esac
exit 0
STUB

  cat > "$bin/kubectl" <<'STUB'
#!/usr/bin/env bash
[ -n "${EVAL_STUB_KUBECTL_LOG:-}" ] && printf '%s\n' "$*" >> "$EVAL_STUB_KUBECTL_LOG"

# `exec` deliberately is NOT a no-op: it strips the kubectl wrapper and runs the
# command LOCALLY, so the dry run still exercises the real in-pod logic (file
# modes, curl config construction, the burst loop) against a real filesystem.
# Safe because $POD_WORKDIR points inside the run's own scratch dir under
# --dry-run.
case "${1:-}" in
  exec)
    shift
    while [ $# -gt 0 ]; do
      case "$1" in
        --)             shift; break ;;
        -n|--namespace) shift 2 ;;
        -c|--container) shift 2 ;;
        *)              shift ;;
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
exit 0
STUB

  # The psql stub records the budget_usage rows a billable request would produce,
  # so case 7 exercises its real polling and period-fan-out assertions.
  cat > "$bin/psql" <<'STUB'
#!/usr/bin/env bash
sql=""
while [ $# -gt 0 ]; do
  [ "$1" = "-c" ] && sql="$2"
  shift
done
sd="${EVAL_STUB_STATE_DIR:-/tmp}"
case "$sql" in
  *"INSERT INTO"*|*"DELETE FROM"*) : ;;
  *"count(DISTINCT period_type)"*)
    # The tracker writes daily+weekly+monthly per entity.
    if [ -f "$sd/billed" ]; then echo "3"; else echo "0"; fi ;;
  *"FROM budget_usage"*"entity_type='user'"*)
    # Org B is the isolation probe and must always read zero.
    case "$sql" in
      *orgb*) echo "0" ;;
      *) if [ -f "$sd/billed" ]; then echo "1"; else echo "0"; fi ;;
    esac ;;
  *"FROM budget_usage"*"entity_type='org'"*)
    if [ -f "$sd/billed" ]; then echo "1"; else echo "0"; fi ;;
  *"FROM budget_usage"*) echo "0" ;;
  *"FROM usage_logs"*)
    # Phase H2/H3: the run is billed to the AGENT's name, never the root human —
    # which is the actual behaviour the phase exists to observe.
    echo "agent-developer" ;;
  *) : ;;
esac
exit 0
STUB

  # The curl stub is where the real logic lives. It re-implements:
  #   * the budget cascade: user→team→department→org, first denial wins,
  #     projected = spend + 0.05 > cap  (so a 0.01 cap denies at zero spend)
  #   * the rate-limit bucket: capacity = max(1, int(rpm*1.5/60*10)), consumed
  #     per request, rpm checked before concurrent
  # from the recorded config state, so the verdicts are computed and not dictated.
  cat > "$bin/curl" <<'STUB'
#!/usr/bin/env bash
out=""; url=""; cfg=""; body_file=""; method="GET"
prev=""
for a in "$@"; do
  case "$prev" in
    -o) out="$a" ;;
    -K) cfg="$a" ;;
    -X) method="$a" ;;
    --data-binary) body_file="${a#@}" ;;
  esac
  case "$a" in http*) url="$a" ;; esac
  prev="$a"
done

sd="${EVAL_STUB_STATE_DIR:-/tmp}"
status=200
body='{"ok":true}'

emit() { [ -n "$out" ] && printf '%s' "$body" > "$out"; printf '%s' "$status"; exit 0; }

# ── Admin writes: record the config the eval asked for ───────────────────────
case "$url" in
  */admin/organizations/*/budgets)
    if [ "$method" = "POST" ] && [ -f "$body_file" ]; then
      et="$(jq -r '.entity_type' "$body_file")"
      ei="$(jq -r '.entity_id' "$body_file")"
      amt="$(jq -r '.budget_amount_usd' "$body_file")"
      printf '%s' "$amt" > "$sd/budgets/${et}:${ei}"
      status=201; body='{"created":true}'
    fi
    emit ;;
  */admin/organizations/*/budget/*)
    if [ "$method" = "DELETE" ]; then
      # .../budget/<et>/<ei>/<period>
      rest="${url#*/budget/}"; et="${rest%%/*}"; rest="${rest#*/}"; ei="${rest%%/*}"
      rm -f "$sd/budgets/${et}:${ei}"
      status=204; body=''
    fi
    emit ;;
  */admin/organizations/*/ratelimits)
    if [ "$method" = "POST" ] && [ -f "$body_file" ]; then
      et="$(jq -r '.entity_type' "$body_file")"
      ei="$(jq -r '.entity_id' "$body_file")"
      # `-` for absent, never an empty field: a config with rpm unset and
      # concurrent set (case 10) would otherwise be written " 1", and `read rpm
      # conc` would collapse the leading blank and land the 1 in rpm — turning
      # case 10's concurrent limit into an rpm limit and failing it for a reason
      # that exists only in the stub.
      rpm="$(jq -r '.rpm // "-"' "$body_file")"
      conc="$(jq -r '.concurrent_requests // "-"' "$body_file")"
      printf '%s %s' "${rpm:--}" "${conc:--}" > "$sd/ratelimits/${et}:${ei}"
      status=201; body='{"created":true}'
    fi
    emit ;;
  */admin/organizations/*/ratelimit/*)
    if [ "$method" = "DELETE" ]; then
      rest="${url#*/ratelimit/}"; et="${rest%%/*}"; rest="${rest#*/}"; ei="${rest%%/*}"
      rm -f "$sd/ratelimits/${et}:${ei}"
      status=204; body=''
    fi
    emit ;;
esac

# ── Inference paths: compute the verdict ─────────────────────────────────────
case "$url" in
  *v1/messages*|*openai/v1/responses*)
    # Which identity is calling is carried ONLY by the curl config file (-K),
    # which is where the token lives — the same property the real run relies on.
    who="$(basename "$cfg" .curlrc)"
    read -r _ org team dept <<< "$(grep "^${who} " "$sd/hierarchy" 2>/dev/null || echo "$who - - -")"

    # The budget cascade, most-specific first. The flat $0.05 estimate is what
    # makes a $0.01 cap deny at zero spend.
    for lvl in "user:sub-${who}" "team:${team}" "department:${dept}" "org:${org}"; do
      et="${lvl%%:*}"; ei="${lvl#*:}"
      [ "$ei" = "-" ] && continue
      # The real user-level entity_id is the Cognito sub. The aws stub derives
      # subs from the username, so reconstruct the same shape here.
      if [ "$et" = "user" ]; then
        ei="$(cat "$sd/subs/${who}" 2>/dev/null || echo "$ei")"
      fi
      f="$sd/budgets/${et}:${ei}"
      [ -f "$f" ] || continue
      cap="$(cat "$f")"
      # projected = 0 spend + 0.05 estimate
      if awk -v c="$cap" 'BEGIN{exit !(0.05 > c)}'; then
        status=402
        body="{\"error\":\"budget_exceeded\",\"message\":\"Budget exceeded for ${et} ${ei}\",\"details\":{\"entity_type\":\"${et}\",\"entity_id\":\"${ei}\",\"budget_usd\":${cap},\"spent_usd\":0.0,\"enforcement_mode\":\"hard\"}}"
        emit
      fi
    done

    # The rate-limit bucket, same order the limiter uses (rpm then concurrent),
    # checking the user config or the organization config used by case 11.
    sub="$(cat "$sd/subs/${who}" 2>/dev/null || echo "sub-${who}")"
    rl="$sd/ratelimits/user:${sub}"
    [ -f "$rl" ] || rl="$sd/ratelimits/org:${org}"
    if [ -f "$rl" ]; then
      read -r rpm conc < "$rl"
      cnt_f="$sd/counts/${who}"
      cnt="$(cat "$cnt_f" 2>/dev/null || echo 0)"
      cnt=$((cnt + 1)); printf '%s' "$cnt" > "$cnt_f"
      if [ "${rpm:--}" != "-" ]; then
        cap_tokens=$(awk -v r="$rpm" 'BEGIN{c=int(r*1.5/60*10); if(c<1)c=1; print c}')
        if [ "$cnt" -gt "$cap_tokens" ]; then
          status=429
          body="{\"error\":\"rate_limited\",\"message\":\"Rate limit exceeded (rpm limit)\",\"details\":{\"limit_type\":\"rpm\",\"limit\":${rpm},\"remaining\":0,\"reset_seconds\":60}}"
          emit
        fi
      fi
      if [ "${conc:--}" != "-" ] && [ "$cnt" -gt "$conc" ]; then
        status=429
        body="{\"error\":\"rate_limited\",\"message\":\"Rate limit exceeded (concurrent limit)\",\"details\":{\"limit_type\":\"concurrent\",\"limit\":${conc},\"remaining\":0,\"reset_seconds\":5}}"
        emit
      fi
    fi

    # Allowed: mark that a billable request happened, so case 7's ledger poll
    # has something to find.
    touch "$sd/billed"
    body='{"id":"msg_stub","content":[{"type":"text","text":"OK"}]}'
    emit ;;
esac

emit
STUB

  # laptop_provision() apt-installs the laptop's baseline tooling. Under
  # --dry-run the kubectl stub runs exec'd commands locally, so this must not
  # reach the real apt — that needs root and would mutate the machine running the
  # tests.
  cat > "$bin/apt-get" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB

  chmod 755 "$bin/aws" "$bin/kubectl" "$bin/psql" "$bin/curl" "$bin/apt-get"
  PATH="$bin:$PATH"
  export PATH

  # Putting the stubs on the HARNESS PATH is not enough on its own: laptop()
  # execs `env ... PATH="$POD_PATH" "$@"`, which REPLACES the PATH the stub
  # directory was prepended to. Under --dry-run the kubectl stub runs exec'd
  # commands locally, so without this line every pod-side command — apt-get in
  # laptop_provision(), curl in the laptop cases — resolves to the machine's real
  # binary and the dry run tries to apt-install as a non-root user.
  POD_PATH="$bin:$POD_PATH"

  # The subs directory lets the curl stub resolve who→sub the same way the
  # harness does, without re-running the aws stub.
  mkdir -p "$EVAL_STUB_STATE_DIR/subs"

  # A dry run must not actually wait out the limiter's 60s reload window.
  RL_RELOAD_WAIT=0
  # Nor burst 40 times per case when the stub's bucket is deterministic.
  RL_BURST_REQUESTS=6
}

maybe_fail_phase() {
  if [ -n "$FAIL_PHASE" ] && [ "$1" = "$FAIL_PHASE" ]; then
    die "--fail-phase $1: simulated failure"
  fi
}

# =============================================================================
# The tag guard
# =============================================================================
# Organizational IDs carry the run tag. User IDs are Cognito UUIDs and must
# match the recorded sub AND username of a user seeded by this run (below).
# Every admin write checks ownership immediately before it runs, and dies
# rather than records: a write that escaped the tag would be a mutation to a real
# tenant's budget or rate limit in a shared dev account, so there is no
# "continue and report it" option. The guard is cheap and unconditional.
assert_tagged() {
  local what="$1" value="$2"
  case "$value" in
    *"${EVAL_TAG}"*) return 0 ;;
    *) die "REFUSING to write ${what}='${value}' — it does not carry the run tag ${EVAL_TAG}. This guard exists so the eval can never mutate a real tenant's config." ;;
  esac
}

# Cognito owns the UUID format; accepting arbitrary UUIDs would remove the
# mutation boundary. Match both pieces of the seeding receipt to this run.
assert_owned_entity() {
  local what="$1" entity_type="$2" value="$3" who username
  if [ "$entity_type" != user ]; then
    assert_tagged "$what" "$value"
    return
  fi
  for who in U1 U2 U3 X1 A1; do
    username="$(state_get "${who}_USERNAME")"
    if [ -n "$value" ] && [ "$value" = "$(state_get "${who}_SUB")" ] \
       && [ "$username" = "${!who}" ]; then
      assert_tagged "$what seeded username" "$username"
      return 0
    fi
  done
  die "REFUSING to write ${what}='${value}' — not a Cognito user seeded by this run"
}

# =============================================================================
# Config resolution
# =============================================================================
resolve_config() {
  # Named distinctly from the seeding phase below: the two are the read-only half
  # and the first-mutation half of setup, and the trace file (which the dry-run
  # tests assert on) cannot tell two phases named "setup" apart.
  phase config "resolving gateway configuration from SSM"

  USER_POOL_ID="$(eval_ssm "/adp/${ENVIRONMENT}/gateway/cognito-user-pool-id")"
  CLIENT_ID="$(eval_ssm "/adp/${ENVIRONMENT}/gateway/cognito-client-id")"
  CF_DOMAIN="$(eval_ssm "/adp/${ENVIRONMENT}/gateway/cloudfront-domain")"
  RDS_HOST="$(eval_ssm "/adp/${ENVIRONMENT}/gateway/rds-host")"
  RDS_DB="$(eval_ssm "/adp/${ENVIRONMENT}/gateway/rds-database-name")"

  local v
  for v in USER_POOL_ID CLIENT_ID CF_DOMAIN RDS_HOST RDS_DB; do
    if [ -z "${!v}" ] || [ "${!v}" = "None" ]; then
      die "Could not resolve $v from SSM for environment '$ENVIRONMENT'"
    fi
  done

  # The public base. The SPA calls /api/*, and a CloudFront Function strips that
  # segment before the ALB origin (infra/modules/cloudfront/main.tf:110), so the
  # app path /admin/... is reached as /api/admin/... from outside.
  BASE_URL="https://${CF_DOMAIN}"
  API="${BASE_URL}/api"

  resolve_db_creds
  log "gateway: ${BASE_URL}   pool: ${USER_POOL_ID}"
}

# =============================================================================
# Seeding
# =============================================================================
# The five identities. The tenant claims are the whole point: BOTH the budget
# hierarchy (_get_entity_hierarchy) and the rate-limit hierarchy
# (_get_hierarchy_entities) are built PURELY from custom:org_id / custom:team_id
# / custom:department_id, which the pre-token-generation Lambda copies into the
# ACCESS token (infra/modules/cognito/lambda/pre_token_generation.py:113-125).
# A user seeded without them has no team/department/org level to enforce at, so a
# cascading-cap case would silently assert nothing.
#
# Note `users` has no department_id column at all — department is claim-only, so
# the claim is the ONLY way to exercise case 3.
seed_world() {
  phase seed "seeding the throwaway org structure"

  seed_user "$U1" "U1" "" \
    "Name=custom:org_id,Value=${ORG_A}" \
    "Name=custom:team_id,Value=${TEAM_1}" \
    "Name=custom:department_id,Value=${DEPT_1}"
  seed_user "$U2" "U2" "" \
    "Name=custom:org_id,Value=${ORG_A}" \
    "Name=custom:team_id,Value=${TEAM_1}" \
    "Name=custom:department_id,Value=${DEPT_1}"
  seed_user "$U3" "U3" "" \
    "Name=custom:org_id,Value=${ORG_A}" \
    "Name=custom:team_id,Value=${TEAM_2}" \
    "Name=custom:department_id,Value=${DEPT_1}"
  seed_user "$X1" "X1" "" \
    "Name=custom:org_id,Value=${ORG_B}"

  # a1 carries org_admin as a claim for completeness, but the claim is NOT what
  # grants it authority — see seed_admin_identity().
  seed_user "$A1" "A1" "org_admin" \
    "Name=custom:org_id,Value=${ORG_A}"

  # Under --dry-run the curl stub must resolve who→sub exactly as the harness
  # does, or a budget written against U1's sub would not be found by U1's
  # request and every cascade case would silently pass by allowing everything.
  if [ "$DRY_RUN" = true ]; then
    local who
    for who in U1 U2 U3 X1; do
      printf '%s' "$(state_get "${who}_SUB")" > "${EVAL_STUB_STATE_DIR}/subs/${who}"
    done
  fi

  seed_admin_identity
  seed_member_identities
}

# The admin API's authority model (#3987): the TOKEN establishes identity, the
# DATABASE establishes authority. AccessControl resolves role by
# users.cognito_sub → users.id → tenant_memberships.role
# (admin/access_control.py:117-145), and a caller with no membership row now
# defaults to MEMBER (least privilege), which holds neither budget:update nor
# ratelimit:update. So a Cognito user with custom:role=org_admin is NOT an admin.
#
# Hence three rows: an organizations row (tenant_memberships.tenant_id FKs to it),
# a users row, and an ACTIVE tenant_memberships row with role='org_admin'.
seed_admin_identity() {
  local sub
  sub="$(state_get A1_SUB)"
  [ -n "$sub" ] || die "no Cognito sub recorded for the admin identity"

  assert_tagged "admin org_id" "$ORG_A"

  # Recorded BEFORE the writes: if one half-succeeds, cleanup must still know to
  # remove them.
  state_set ADMIN_ORG "$ORG_A"
  state_set ADMIN_SUB "$sub"

  h_psql -c "INSERT INTO organizations (id, name, aws_accounts, role_mappings, settings,
                                        github_installation_ids, cognito_client_ids, created_via, created_at)
             VALUES ('${ORG_A}', '${EVAL_TAG}', '[]', '{}', '{}', '[]', '[]', 'operator', now())
             ON CONFLICT (id) DO NOTHING;" >/dev/null \
    || die "could not insert the throwaway organizations row"

  # team_id is NOT NULL on users; org_id comes from TenantMixin.
  h_psql -c "INSERT INTO users (id, org_id, team_id, email, name, cognito_sub, created_at)
             VALUES ('${sub}', '${ORG_A}', '${TEAM_1}', '${A1}', '${EVAL_TAG}', '${sub}', now())
             ON CONFLICT (id) DO NOTHING;" >/dev/null \
    || die "could not insert the throwaway users row for the admin identity"

  h_psql -c "INSERT INTO tenant_memberships (id, user_id, tenant_id, role, is_active, created_at)
             VALUES ('${sub}', '${sub}', '${ORG_A}', 'org_admin', true, now())
             ON CONFLICT (user_id, tenant_id) DO UPDATE SET role='org_admin', is_active=true;" >/dev/null \
    || die "could not insert the throwaway tenant_memberships row"

  pass "admin identity has an active org_admin tenant_memberships row (authority is DB-resolved, not a token claim)"
}

# The budget API resolves a user entity through canonical users in its tenant
# (#4511). A Cognito identity alone can infer, but cannot receive a budget config.
seed_member_identities() {
  assert_tagged "isolation org_id" "$ORG_B"
  h_psql -c "INSERT INTO organizations (id, name, aws_accounts, role_mappings, settings,
                                        github_installation_ids, cognito_client_ids, created_via, created_at)
             VALUES ('${ORG_B}', '${ORG_B}', '[]', '{}', '{}', '[]', '[]', 'operator', now())
             ON CONFLICT (id) DO NOTHING;" >/dev/null \
    || die "could not insert the isolation organizations row"

  local who sub org team username
  for who in U1 U2 U3 X1; do
    sub="$(state_get "${who}_SUB")"
    username="$(state_get "${who}_USERNAME")"
    assert_owned_entity "seeded member" user "$sub"
    org="$ORG_A"; team="$TEAM_1"
    [ "$who" != U3 ] || team="$TEAM_2"
    if [ "$who" = X1 ]; then org="$ORG_B"; team=""; fi
    h_psql -c "INSERT INTO users (id, org_id, team_id, email, name, cognito_sub, created_at)
               VALUES ('${sub}', '${org}', '${team}', '${username}', '${EVAL_TAG}', '${sub}', now())
               ON CONFLICT (id) DO NOTHING;" >/dev/null \
      || die "could not insert canonical user for $who"
    h_psql -c "INSERT INTO tenant_memberships (id, user_id, tenant_id, role, is_active, created_at)
               VALUES ('${sub}', '${sub}', '${org}', 'member', true, now())
               ON CONFLICT (user_id, tenant_id) DO UPDATE SET role='member', is_active=true;" >/dev/null \
      || die "could not insert tenant membership for $who"
  done
  pass "all four budget actors have canonical users and active tenant memberships"
}

# =============================================================================
# Admin-API config writers
# =============================================================================
# Budget configs go through POST /api/admin/organizations/{org}/budgets, which is
# the only surface that can create one: the mutating routes on the /budgets
# router were deliberately removed (#3988, budget/routes.py:40-56).
#
# NOTE on entity_type spelling: the budget admin API accepts
# Literal["org","department","team","user"] (admin/schemas.py:298) and the budget
# enforcement path reads EntityType.ORGANIZATION == "org", so for BUDGETS the
# admin spelling and the enforcement spelling agree, as they do for rate limits.
set_budget() {
  local entity_type="$1" entity_id="$2" amount="$3" period="${4:-daily}" org="${5:-$ORG_A}"
  local body="$WORKDIR/req.json" out="$WORKDIR/resp.json" status

  assert_owned_entity "budget entity_id" "$entity_type" "$entity_id"
  assert_tagged "budget org_id" "$org"

  jq -n --arg t "$entity_type" --arg i "$entity_id" --arg p "$period" --arg a "$amount" \
    '{entity_type:$t, entity_id:$i, period_type:$p, budget_amount_usd:($a|tonumber), enforcement_mode:"hard"}' \
    > "$body"

  # Recorded BEFORE the call: a config that was created but whose response was
  # lost must still be swept.
  state_append CREATED_BUDGETS "${org}|${entity_type}|${entity_id}|${period}"

  status="$(http_post_json "$WORKDIR/A1.curlrc" "${API}/admin/organizations/${org}/budgets" "$body" "$out")"
  if [ "$status" != "201" ] && [ "$status" != "200" ]; then
    fail "could not create ${entity_type} budget for ${entity_id} (HTTP ${status}): $(head -c 300 "$out")"
    return 1
  fi
  log "budget set: ${entity_type}=${entity_id} ${period} \$${amount}"
}

# Rate-limit configs go through POST /api/admin/organizations/{org}/ratelimits.
#
# The current limiter and admin API both use entity_type="org". Case 11 must
# fail if that configured cap no longer enforces; the old spelling bug is fixed.
set_ratelimit() {
  local entity_type="$1" entity_id="$2" rpm="$3" tpm="${4:-}" concurrent="${5:-}" org="${6:-$ORG_A}"
  local body="$WORKDIR/req.json" out="$WORKDIR/resp.json" status

  assert_owned_entity "ratelimit entity_id" "$entity_type" "$entity_id"
  assert_tagged "ratelimit org_id" "$org"

  jq -n --arg t "$entity_type" --arg i "$entity_id" \
        --arg r "$rpm" --arg p "$tpm" --arg c "$concurrent" \
    '{entity_type:$t, entity_id:$i}
     + (if $r == "" then {} else {rpm:($r|tonumber)} end)
     + (if $p == "" then {} else {tpm:($p|tonumber)} end)
     + (if $c == "" then {} else {concurrent_requests:($c|tonumber)} end)' \
    > "$body"

  state_append CREATED_RATELIMITS "${org}|${entity_type}|${entity_id}"

  status="$(http_post_json "$WORKDIR/A1.curlrc" "${API}/admin/organizations/${org}/ratelimits" "$body" "$out")"
  if [ "$status" != "201" ] && [ "$status" != "200" ]; then
    fail "could not create ${entity_type} rate limit for ${entity_id} (HTTP ${status}): $(head -c 300 "$out")"
    return 1
  fi
  log "ratelimit set: ${entity_type}=${entity_id} rpm=${rpm:-–} tpm=${tpm:-–} concurrent=${concurrent:-–}"
}

# =============================================================================
# Driving traffic from the clean room, on both wire paths
# =============================================================================
# Both paths are ENFORCED_PATHS (shared/enforced_paths.py:24-34) and both are
# matched by prefix, so the middleware treats them identically. Running every
# enforcement case through both is what proves enforcement is wire-format
# agnostic — a denial implemented in one router and not the other would show up
# here as a per-path divergence.
#
# Claude Code path: /v1/messages, Anthropic body shape.
# Codex path:       /openai/v1/responses, Responses body shape.
laptop_call() {
  local wire="$1" who="$2" out_local="$3"
  # Case 10 issues concurrent requests for one identity. Sharing its body/output
  # paths lets one request's 200 body overwrite another request's 429 evidence.
  local request_dir request_id
  request_dir="$(mktemp -d "$WORKDIR/request.XXXXXX")" || return 1
  request_id="$(basename "$request_dir")"
  local pod_body="${POD_WORKDIR}/${request_id}/body.json"
  local pod_out="${POD_WORKDIR}/${request_id}/out.json"
  local pod_cfg="${POD_WORKDIR}/${who}.curlrc"
  local body_local="$request_dir/body.json" url status

  case "$wire" in
    claude) anthropic_body "$body_local" "reply with OK"; url="${API}/v1/messages" ;;
    codex)  responses_body "$body_local" "reply with OK"; url="${API}/openai/v1/responses" ;;
    *) die "unknown wire path: $wire" ;;
  esac

  laptop_put_file "$body_local" "$pod_body" 600
  status="$(laptop_http_post_json "$pod_cfg" "$url" "$pod_body" "$pod_out")"
  laptop_get_file "$pod_out" "$out_local" 2>/dev/null || : > "$out_local"
  printf '%s' "$status"
}

# The token reaches the pod ONCE per identity, on stdin, and the pod builds its
# own 0600 curl config from it. The token never appears in an exec'd command line
# (visible in the exec API and the runner's process table) and never comes back.
provision_identity() {
  local who="$1"
  laptop_put_file "$WORKDIR/${who}.access" "${POD_WORKDIR}/${who}.access" 600
  laptop_write_curl_auth_config "${POD_WORKDIR}/${who}.access" "${POD_WORKDIR}/${who}.curlrc"
}

# -----------------------------------------------------------------------------
# Assertions
# -----------------------------------------------------------------------------
# The 402 shape is built in budget/enforcement_middleware.py:160-209:
#   {"error":"budget_exceeded","message":...,
#    "details":{"entity_type","entity_id","budget_usd","spent_usd","enforcement_mode"}}
# `expected_entity` is the level that SHOULD have tripped, which is the whole
# point of the cascade cases: a 402 from the wrong level is a precedence bug and
# must not pass.
assert_budget_denied() {
  local label="$1" wire="$2" status="$3" body="$4" expected_entity="$5"
  local err entity

  if [ "$status" != "402" ]; then
    # A 503 budget_check_unavailable is a DIFFERENT failure (#4075 fail-closed)
    # and must be reported as itself rather than as "no denial".
    err="$(jq -r '(.error // .detail.error // "")' "$body" 2>/dev/null || true)"
    if [ "$status" = "503" ] && [ "$err" = "budget_check_unavailable" ]; then
      fail "${label} [${wire}]: budget check was UNAVAILABLE (fail-closed 503), so enforcement was never evaluated"
      return 1
    fi
    fail "${label} [${wire}]: expected HTTP 402, got ${status}: $(head -c 200 "$body")"
    return 1
  fi

  err="$(jq -r '.error // ""' "$body" 2>/dev/null || true)"
  if [ "$err" != "budget_exceeded" ]; then
    fail "${label} [${wire}]: 402 body has error='${err}', expected 'budget_exceeded'"
    return 1
  fi

  entity="$(jq -r '.details.entity_type // ""' "$body" 2>/dev/null || true)"
  if [ "$entity" != "$expected_entity" ]; then
    fail "${label} [${wire}]: denied at entity_type='${entity}', expected '${expected_entity}' — the cascade tripped at the wrong level"
    return 1
  fi

  # The remaining documented fields must be present, not merely the two above:
  # a details object that omits them is a contract regression even when the
  # entity_type is right.
  local f
  for f in entity_id budget_usd enforcement_mode; do
    if [ "$(jq -r "(.details.${f} // \"\") | tostring" "$body")" = "" ]; then
      fail "${label} [${wire}]: 402 details is missing '${f}'"
      return 1
    fi
  done

  pass "${label} [${wire}]: HTTP 402 budget_exceeded at entity_type='${entity}'"
}

assert_allowed() {
  local label="$1" wire="$2" status="$3" body="$4"
  case "$status" in
    200)
      pass "${label} [${wire}]: allowed (HTTP 200)" ;;
    402)
      fail "${label} [${wire}]: DENIED with 402 but nothing should have tripped: $(head -c 200 "$body")" ;;
    429)
      fail "${label} [${wire}]: rate limited (429) during a budget case — a limiter bucket leaked in from an earlier case" ;;
    *)
      fail "${label} [${wire}]: expected HTTP 200, got ${status}: $(head -c 200 "$body")" ;;
  esac
}

# The 429 shape is built in ratelimit/enforcement_middleware.py:106-146:
#   {"error":"rate_limited","message":...,
#    "details":{"limit_type","limit","remaining","reset_seconds"}}
# Note `reset_seconds`, NOT retry_after_seconds — the dead
# ratelimit/middleware.py uses the other spelling and is never mounted.
assert_rate_limited_shape() {
  local label="$1" body="$2" expected_limit_type="$3"
  local err lt

  err="$(jq -r '.error // ""' "$body" 2>/dev/null || true)"
  if [ "$err" != "rate_limited" ]; then
    fail "${label}: 429 body has error='${err}', expected 'rate_limited'"
    return 1
  fi

  lt="$(jq -r '.details.limit_type // ""' "$body" 2>/dev/null || true)"
  if [ -n "$expected_limit_type" ] && [ "$lt" != "$expected_limit_type" ]; then
    fail "${label}: 429 limit_type='${lt}', expected '${expected_limit_type}'"
    return 1
  fi

  # All four documented keys must be present. `remaining` is legitimately 0 and
  # `reset_seconds` is legitimately any integer, so presence is checked with
  # `has`, not truthiness — a `remaining: 0` must not read as missing.
  local f
  for f in limit_type limit remaining reset_seconds; do
    if [ "$(jq -r "has(\"details\") and (.details|has(\"${f}\"))" "$body")" != "true" ]; then
      fail "${label}: 429 details is missing '${f}'"
      return 1
    fi
  done

  pass "${label}: HTTP 429 rate_limited, limit_type='${lt}', full documented shape present"
}

# Burst until a 429 appears, or give up after RL_BURST_REQUESTS.
#
# Deliberately asserts "at least one 429 within N", never "request K 429s": with
# 8 in-memory buckets behind an ALB the ordinal is not a property of the system.
# Echoes the ordinal of the first 429; the body it captured is at $BURST_BODY.
#
# BURST_BODY is a top-level constant rather than something this function sets,
# because every caller reads it as `n="$(burst_until_429 ...)"` — a command
# substitution, i.e. a subshell. An assignment made in here would be discarded on
# return and the caller would then dereference an unset variable under `set -u`.
# The captured file itself survives (it is on disk); only the variable would not.
burst_until_429() {
  local who="$1" wire="$2" n="$3" i status
  for i in $(seq 1 "$n"); do
    status="$(laptop_call "$wire" "$who" "$BURST_BODY")"
    if [ "$status" = "429" ]; then
      printf '%s' "$i"
      return 0
    fi
  done
  printf '0'
  return 1
}

# =============================================================================
# Budget cases 1–7
# =============================================================================
# Each case (a) writes the caps, (b) drives BOTH wire paths, (c) asserts, then
# (d) removes its caps so the next case starts from a known state. Teardown per
# case matters: budget_configs is unique on (org, entity_type, entity_id,
# period) so a leftover $0.01 user cap would deny every later case at the user
# level and every one of them would "pass" for the wrong reason.
clear_budgets() {
  local entry org et ei period out="$WORKDIR/del.json"
  for entry in $(state_get CREATED_BUDGETS | tr ',' ' '); do
    IFS='|' read -r org et ei period <<< "$entry"
    [ -n "$ei" ] || continue
    http_delete "$WORKDIR/A1.curlrc" \
      "${API}/admin/organizations/${org}/budget/${et}/${ei}/${period}" "$out" >/dev/null || true
  done
  state_set CREATED_BUDGETS ""
}

case_01() {
  phase 1 "user cap → 402 at entity_type=user"
  maybe_fail_phase 1

  # The injected failure asserts the WRONG level, proving a real cascade
  # regression would be caught. It must change what the eval EXPECTS, never what
  # the platform is configured to do — an injection that also moved the cap would
  # test nothing.
  local expect="user"
  [ "$INJECT_FAILURE" = "wrong-entity" ] && expect="team"

  set_budget user "$(state_get U1_SUB)" "$TRIP_CAP" daily || return 1

  local wire status
  for wire in claude codex; do
    status="$(laptop_call "$wire" U1 "$WORKDIR/r.json")"
    assert_budget_denied "case 1 user cap" "$wire" "$status" "$WORKDIR/r.json" "$expect"
  done
  clear_budgets
}

case_02() {
  phase 2 "team cap → 402 at entity_type=team"
  maybe_fail_phase 2

  # The user level is given an OPEN cap rather than left unset: with no user
  # config at all the cascade would skip the user level and reach team anyway, so
  # the assertion would hold even if precedence were broken. An open user cap
  # makes the case prove that team is reached *past* a satisfied user level.
  set_budget user "$(state_get U1_SUB)" "$OPEN_CAP" daily || return 1
  set_budget team "$TEAM_1" "$TRIP_CAP" daily || return 1

  local wire status
  for wire in claude codex; do
    status="$(laptop_call "$wire" U1 "$WORKDIR/r.json")"
    assert_budget_denied "case 2 team cap" "$wire" "$status" "$WORKDIR/r.json" "team"
  done
  clear_budgets
}

case_03() {
  phase 3 "department cap → 402 at entity_type=department"
  maybe_fail_phase 3

  set_budget user "$(state_get U1_SUB)" "$OPEN_CAP" daily || return 1
  set_budget team "$TEAM_1" "$OPEN_CAP" daily || return 1
  set_budget department "$DEPT_1" "$TRIP_CAP" daily || return 1

  local wire status
  for wire in claude codex; do
    status="$(laptop_call "$wire" U1 "$WORKDIR/r.json")"
    assert_budget_denied "case 3 dept cap" "$wire" "$status" "$WORKDIR/r.json" "department"
  done
  clear_budgets
}

case_04() {
  phase 4 "org cap → 402 at entity_type=org, and Org-B is unaffected"
  maybe_fail_phase 4

  set_budget user "$(state_get U1_SUB)" "$OPEN_CAP" daily || return 1
  set_budget team "$TEAM_1" "$OPEN_CAP" daily || return 1
  set_budget department "$DEPT_1" "$OPEN_CAP" daily || return 1
  set_budget org "$ORG_A" "$TRIP_CAP" daily || return 1

  local wire status
  for wire in claude codex; do
    status="$(laptop_call "$wire" U1 "$WORKDIR/r.json")"
    assert_budget_denied "case 4 org cap" "$wire" "$status" "$WORKDIR/r.json" "org"
  done

  # TENANT ISOLATION: x1 is in Org B and must be entirely unaffected by Org A's
  # exhausted cap. This is the case that would catch an enforcement query that
  # forgot its org_id predicate.
  for wire in claude codex; do
    status="$(laptop_call "$wire" X1 "$WORKDIR/r.json")"
    assert_allowed "case 4 Org-B isolation" "$wire" "$status" "$WORKDIR/r.json"
  done

  # This case trips the pre-request estimate; case 7 separately checks the
  # settled organization ledger. It does not spend through a real dollar cap.
  clear_budgets
}

case_05() {
  phase 5 "precedence — the most-specific exceeded level is the one reported"
  maybe_fail_phase 5

  # Every level over its cap simultaneously. _get_entity_hierarchy builds
  # user→team→department→org (enforcement_service.py:249-282) and the loop
  # returns the FIRST denial (enforcement_service.py:337), so "user" is the only
  # correct answer. If this reports team/department/org, the cascade is walking
  # the hierarchy in the wrong direction.
  set_budget user "$(state_get U1_SUB)" "$TRIP_CAP" daily || return 1
  set_budget team "$TEAM_1" "$TRIP_CAP" daily || return 1
  set_budget department "$DEPT_1" "$TRIP_CAP" daily || return 1
  set_budget org "$ORG_A" "$TRIP_CAP" daily || return 1

  local wire status
  for wire in claude codex; do
    status="$(laptop_call "$wire" U1 "$WORKDIR/r.json")"
    assert_budget_denied "case 5 precedence" "$wire" "$status" "$WORKDIR/r.json" "user"
  done
  clear_budgets
}

case_06() {
  phase 6 "no-config baseline — an unconfigured hierarchy must not deny"
  maybe_fail_phase 6

  # u2 gets no budget config at any level. _check_entity_budget returns
  # allowed=True when no config row exists (enforcement_service.py:389-391), so
  # the correct behaviour is a clean 200. This is the case that catches a
  # fail-closed regression where absence of config starts denying.
  local wire status
  for wire in claude codex; do
    status="$(laptop_call "$wire" U2 "$WORKDIR/r.json")"
    assert_allowed "case 6 no-config baseline" "$wire" "$status" "$WORKDIR/r.json"
  done
}

case_07() {
  phase 7 "accounting integrity — spend lands on the right entities"
  maybe_fail_phase 7

  # Cases 1-5 deliberately avoid the ledger by tripping on the estimate. This
  # case is the one that exercises it, so it needs a request that actually
  # SUCCEEDS and produces usage. u3 has no cap, so its call is billed normally.
  local wire status
  for wire in claude codex; do
    status="$(laptop_call "$wire" U3 "$WORKDIR/r.json")"
    assert_allowed "case 7 billable request" "$wire" "$status" "$WORKDIR/r.json"
  done

  # The ledger is asynchronous: the gateway writes a chat log to S3, an
  # ObjectCreated notification fires the tracker Lambda
  # (infra/modules/budget-lambda/main.tf:204-213), and only then do budget_usage
  # rows appear. Polling with a bounded deadline is the only correct way to
  # observe it; a fixed sleep either flakes or wastes time.
  local sub deadline rows=0
  sub="$(state_get U3_SUB)"
  deadline=$(( $(date +%s) + 300 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    rows="$(h_psql -t -A -c "SELECT count(*) FROM budget_usage
              WHERE org_id='${ORG_A}' AND entity_type='user' AND entity_id='${sub}';" 2>/dev/null | tr -d '[:space:]')"
    [ "${rows:-0}" -gt 0 ] && break
    sleep 15
  done

  if [ "${rows:-0}" -gt 0 ]; then
    pass "case 7: budget_usage has ${rows} row(s) keyed ('user','${sub:0:8}…') after a billable request"
  else
    fail "case 7: no budget_usage row appeared for the billing user within 300s — the S3→Lambda accounting path did not land"
    return 1
  fi

  # The tracker writes one row per (entity, period) for daily/weekly/monthly
  # (handler.py:411-420), so all three periods must be present. A single-period
  # result means the period fan-out regressed.
  local periods
  periods="$(h_psql -t -A -c "SELECT count(DISTINCT period_type) FROM budget_usage
               WHERE org_id='${ORG_A}' AND entity_type='user' AND entity_id='${sub}';" 2>/dev/null | tr -d '[:space:]')"
  if [ "${periods:-0}" -eq 3 ]; then
    pass "case 7: usage recorded for all three period types (daily, weekly, monthly)"
  else
    fail "case 7: expected 3 period_types in budget_usage, found ${periods:-0}"
  fi

  local org_rows=0
  while [ "$(date +%s)" -lt "$deadline" ]; do
    org_rows="$(h_psql -t -A -c "SELECT count(*) FROM budget_usage
                 WHERE org_id='${ORG_A}' AND entity_type='org' AND entity_id='${ORG_A}';" | tr -d '[:space:]')"
    [ "${org_rows:-0}" -gt 0 ] && break
    sleep 5
  done
  if [ "${org_rows:-0}" -gt 0 ]; then
    pass "case 7: settled organization usage uses the enforceable 'org' ledger key"
  else
    fail "case 7: no settled organization usage under the 'org' ledger key"
  fi

  # Isolation on the ledger side, not just the enforcement side: Org A's spend
  # must not appear under Org B.
  local leaked
  leaked="$(h_psql -t -A -c "SELECT count(*) FROM budget_usage
              WHERE org_id='${ORG_B}' AND entity_id='${sub}';" 2>/dev/null | tr -d '[:space:]')"
  if [ "${leaked:-0}" -eq 0 ]; then
    pass "case 7: Org-A user spend did not leak into Org-B's ledger"
  else
    fail "case 7: ${leaked} budget_usage row(s) for an Org-A user are attributed to Org-B"
  fi
}

# =============================================================================
# Rate-limit cases 8–12
# =============================================================================
clear_ratelimits() {
  local entry org et ei out="$WORKDIR/del.json"
  for entry in $(state_get CREATED_RATELIMITS | tr ',' ' '); do
    IFS='|' read -r org et ei <<< "$entry"
    [ -n "$ei" ] || continue
    http_delete "$WORKDIR/A1.curlrc" \
      "${API}/admin/organizations/${org}/ratelimit/${et}/${ei}" "$out" >/dev/null || true
  done
  state_set CREATED_RATELIMITS ""
}

# The limiter caches DB configs for up to 60s (_DB_RELOAD_INTERVAL,
# ratelimit/service.py:29), and each of the 8 workers has its own clock. Waiting
# the full interval plus a margin is the only way to make a freshly-written
# config observable; polling cannot help because there is no endpoint that
# reports the ENFORCING instance's view (the /ratelimits status route reads a
# different singleton — see the README).
wait_for_ratelimit_reload() {
  log "waiting ${RL_RELOAD_WAIT}s for the limiter's DB config reload (interval is 60s, per worker)"
  sleep "$RL_RELOAD_WAIT"
}

case_08() {
  phase 8 "user RPM → 429 with the documented shape"
  maybe_fail_phase 8

  local expect="rpm"
  [ "$INJECT_FAILURE" = "wrong-entity" ] && expect="concurrent"

  set_ratelimit user "$(state_get U1_SUB)" "$TRIP_RPM" || return 1
  wait_for_ratelimit_reload

  local n
  if n="$(burst_until_429 U1 claude "$RL_BURST_REQUESTS")"; then
    pass "case 8: a 429 appeared after ${n} request(s) at rpm=${TRIP_RPM}"
    assert_rate_limited_shape "case 8 user RPM" "$BURST_BODY" "$expect"
  else
    fail "case 8: no 429 within ${RL_BURST_REQUESTS} requests at rpm=${TRIP_RPM} — with 8 in-memory buckets this should be reachable, so enforcement is not applying the config"
  fi
  clear_ratelimits
}

case_09() {
  phase 9 "user TPM"
  maybe_fail_phase 9

  # TPM cannot be exercised end to end, and saying so is more useful than a
  # green row that means nothing. consume_rate_limit's `tokens` parameter
  # defaults to 1 and the ONLY caller in src/ is
  # ratelimit/enforcement_middleware.py:88, which never passes it — so the TPM
  # bucket is debited exactly one token per request no matter how many tokens the
  # request actually consumes. Tripping a TPM limit would therefore require as
  # many REQUESTS as the token limit, and the RPM limit would deny long first.
  #
  # Setting tpm=1 to "prove" a 429 would be dishonest: it would 429 because one
  # request debits one token, not because token accounting works.
  skip "case 9: user TPM enforcement is not reachable end-to-end — see the finding"
  finding "TPM rate limiting is effectively unenforceable: consume_rate_limit(context, tokens=1) is the only call site (ratelimit/enforcement_middleware.py:88) and never passes a real token count, so the TPM bucket is debited 1 token per REQUEST rather than per token. A tpm limit therefore behaves as a second, much larger rpm limit."
  clear_ratelimits
}

case_10() {
  phase 10 "concurrent=1 → 429 limit_type=concurrent"
  maybe_fail_phase 10

  # rpm is left unset so the concurrent limit is the only thing that can trip;
  # with both set, an rpm denial would mask it (rpm is checked first in
  # _check_entity_limits, ratelimit/service.py:212-242).
  set_ratelimit user "$(state_get U2_SUB)" "" "" "$TRIP_CONCURRENT" || return 1
  wait_for_ratelimit_reload

  # Concurrency needs genuinely overlapping in-flight requests. Sequential calls
  # each release their slot in the middleware's `finally`
  # (enforcement_middleware.py:99-101), so they can never collide. These are
  # backgrounded so they overlap.
  local pids=() i status_file
  for i in 1 2 3 4 5 6; do
    status_file="$WORKDIR/conc-${i}.status"
    ( laptop_call claude U2 "$WORKDIR/conc-${i}.json" > "$status_file" 2>/dev/null ) &
    pids+=("$!")
  done
  for i in "${pids[@]}"; do wait "$i" || true; done

  local found=""
  for i in 1 2 3 4 5 6; do
    if [ "$(cat "$WORKDIR/conc-${i}.status" 2>/dev/null)" = "429" ]; then
      found="$WORKDIR/conc-${i}.json"
      break
    fi
  done

  if [ -n "$found" ]; then
    assert_rate_limited_shape "case 10 concurrent" "$found" "concurrent"
  else
    # Not a hard failure: 6 overlapping requests against 8 buckets can genuinely
    # miss, and a flaky red is worse than an honest inconclusive.
    skip "case 10: no concurrent denial observed — 6 overlapping requests can miss when spread across 8 per-worker buckets"
    finding "case 10 (concurrent=1) could not be observed from outside the cluster: the in-memory limiter gives each of the 8 gateway workers its own concurrency counter, so overlapping requests must all land on the SAME worker to collide."
  fi
  clear_ratelimits
}

case_11() {
  phase 11 "org RPM shared bucket"
  maybe_fail_phase 11

  # Written through the real admin API, exactly as an operator would. The point
  # of the case is what happens next.
  set_ratelimit org "$ORG_A" "$TRIP_RPM" || return 1
  wait_for_ratelimit_reload

  local n
  if n="$(burst_until_429 U1 claude "$RL_BURST_REQUESTS")"; then
    pass "case 11: org-level rpm=${TRIP_RPM} enforced (429 after ${n} requests)"
    assert_rate_limited_shape "case 11 org RPM" "$BURST_BODY" "rpm"
  else
    fail "case 11: no org RPM denial within ${RL_BURST_REQUESTS} requests at rpm=${TRIP_RPM}"
  fi
  clear_ratelimits
}

case_12() {
  phase 12 "defaults apply with no rate-limit config present"
  maybe_fail_phase 12

  # No config for u3 at any level. _get_limits_for_entity falls back to
  # default_rpm=60 / default_tpm=100000 / default_concurrent=10
  # (ratelimit/config.py:15-17), applied per level. So a handful of requests must
  # be allowed: capacity at 60 rpm is int(60*1.5/60*10)=15 tokens per bucket.
  #
  # The assertion is that defaults do NOT deny normal traffic. Proving the
  # default eventually 429s would need ~8×15=120+ requests of real inference,
  # which is a load test — explicitly a non-goal of this issue.
  local wire status ok=true
  for wire in claude codex; do
    status="$(laptop_call "$wire" U3 "$WORKDIR/r.json")"
    if [ "$status" = "429" ]; then
      fail "case 12 [${wire}]: a single request was rate limited with NO config present — the default limits are misconfigured"
      ok=false
    elif [ "$status" != "200" ]; then
      fail "case 12 [${wire}]: expected 200 under default limits, got ${status}: $(head -c 200 "$WORKDIR/r.json")"
      ok=false
    fi
  done
  if [ "$ok" = true ]; then
    pass "case 12: default limits (60 rpm / 100000 tpm / 10 concurrent) admit normal traffic on both wire paths"
  fi
}

# =============================================================================
# Phase H — does human-triggered agent spend land under the human's budget?
# =============================================================================
# Read-only observation of existing lineage. Direct usage and human-rooted
# agent usage have DIFFERENT ledgers ('user' and 'root_user'). An agent's direct
# billing identity cannot establish whether its root human was also charged.
# This phase never dispatches a new agent or tests exhaustion of a root-user cap.
phase_h() {
  phase H "does human-triggered agent spend land under the triggering human's budget?"
  maybe_fail_phase H

  local table="adp-${ENVIRONMENT}-webhook-events"

  # H1 — find a human-rooted agent run. root_human_id lives on the DDB item and
  # has a GSI precisely so this lookup is cheap.
  local scan="$WORKDIR/h-scan.json" event_id="" root_human=""
  if ! h_aws dynamodb scan --table-name "$table" \
        --filter-expression "attribute_exists(root_human_id)" \
        --projection-expression "event_id,root_human_id" \
        --max-items 25 --output json > "$scan" 2>/dev/null; then
    skip "phase H: could not read ${table} — H1 needs the webhook-events table"
    finding "phase H could not be observed: the webhook-events DynamoDB table was unreadable from the harness, so no human→agent lineage was available to join."
    return 0
  fi

  event_id="$(jq -r '[.Items[]? | select(.root_human_id.S != null and .root_human_id.S != "")][0].event_id.S // ""' "$scan")"
  root_human="$(jq -r '[.Items[]? | select(.root_human_id.S != null and .root_human_id.S != "")][0].root_human_id.S // ""' "$scan")"

  if [ -z "$event_id" ] || [ -z "$root_human" ]; then
    # Not a failure: a freshly deployed dev may simply never have run an agent.
    skip "phase H: no human-rooted agent run exists in ${table} to observe"
    finding "phase H: no existing human-rooted lineage was available; agent-triggered budget enforcement was not tested"
    return 0
  fi
  pass "phase H1: found a human-rooted agent run (event ${event_id:0:12}…, root human ${root_human:0:8}…)"

  # H2 — join the run to the ledger. event_id == ADP_MESSAGE_ID ==
  # x-agent-runid == usage_logs.agent_run_id is the documented join key.
  local billed
  billed="$(h_psql -t -A -F'|' -c "SELECT DISTINCT user_id FROM usage_logs
              WHERE agent_run_id='${event_id}';" 2>/dev/null | tr -d '\r')"
  if [ -z "$billed" ]; then
    skip "phase H2: no usage_logs rows carry agent_run_id='${event_id}' — the run produced no billable inference"
    return 0
  fi
  pass "phase H2: the run joins to usage_logs via agent_run_id"

  finding "phase H3: this existing run's direct billing identity is '${billed}'. Direct billing alone does not establish root-human budget attribution."

  local root_rows
  if ! root_rows="$(h_psql -t -A -c "SELECT count(*) FROM budget_usage
                 WHERE entity_type='root_user' AND entity_id='${root_human}';" | tr -d '[:space:]')"; then
    fail "phase H4: could not read the root_user ledger"
    return 1
  fi
  finding "phase H4: observed ${root_rows:-0} root_user ledger row(s) for this existing root principal. This aggregate is not correlated proof for the selected event."
  finding "phase H coverage: agent-triggered budget exhaustion is NOT TESTED. A new bounded agent run, its correlated root_user accrual, and the resulting denial are still required; no conclusion about that capability follows from this historical sample."

}

# =============================================================================
# Cleanup
# =============================================================================
# Runs from an EXIT trap AND standalone via --cleanup-only, so a crashed run and
# a deliberate teardown take the same path. Everything here is idempotent and
# nothing aborts on error: a cleanup that gave up halfway would leak throwaway
# config into a shared dev account.
run_cleanup() {
  local rc=$?
  trap - EXIT
  CURRENT_PHASE="cleanup"
  trace "phase:cleanup"
  echo ""
  log "═══ Cleanup ═══"

  # Config first, identities second: the DELETEs authenticate as a1, so removing
  # that identity first would strand every config it created.
  if [ -f "$WORKDIR/A1.curlrc" ]; then
    clear_budgets
    clear_ratelimits
    log "removed budget and rate-limit configs created by this run"
  fi

  local u
  for u in "$U1" "$U2" "$U3" "$A1" "$X1"; do
    delete_seeded_user "$u"
  done

  # Canonical users and memberships belong to the tagged test tenants. Delete
  # children first, including a partial seed, before deleting either organization.
  h_psql -c "DELETE FROM tenant_memberships WHERE tenant_id LIKE '${EVAL_USER_PREFIX}-%';" >/dev/null 2>&1 || fail "cleanup could not delete seeded tenant memberships"
  h_psql -c "DELETE FROM users WHERE org_id LIKE '${EVAL_USER_PREFIX}-%';" >/dev/null 2>&1 || fail "cleanup could not delete seeded user rows"

  # Tag-scoped, so this cannot touch a real tenant even if state was lost. The
  # budget_usage rows are the only trace a billable case leaves behind.
  h_psql -c "DELETE FROM budget_usage    WHERE org_id LIKE '${EVAL_USER_PREFIX}-%';" >/dev/null 2>&1 || fail "cleanup could not delete tagged budget usage"
  h_psql -c "DELETE FROM budget_configs  WHERE org_id LIKE '${EVAL_USER_PREFIX}-%';" >/dev/null 2>&1 || fail "cleanup could not delete tagged budget configs"
  h_psql -c "DELETE FROM rate_limit_configs WHERE org_id LIKE '${EVAL_USER_PREFIX}-%';" >/dev/null 2>&1 || fail "cleanup could not delete tagged rate-limit configs"
  h_psql -c "DELETE FROM organizations   WHERE id LIKE '${EVAL_USER_PREFIX}-%';" >/dev/null 2>&1 || fail "cleanup could not delete tagged organizations"
  log "swept tag-scoped rows for ${EVAL_USER_PREFIX}-*"

  if laptop_pod_delete; then
    log "deleted the clean-room pod"
  else
    fail "cleanup could not delete the clean-room pod"
  fi

  # A fatal setup error can exit before an assertion records a failure.
  # Preserve it in the summary as well as in the process exit status.
  if [ "$rc" -ne 0 ] && [ "$FAILURES" -eq 0 ]; then
    fail "eval aborted before completing its scenarios (exit $rc); see the preceding error"
  fi
  write_summary
  if [ "$FAILURES" -gt 0 ]; then rc=1; fi
  exit "$rc"
}

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
      # The sweep deletes rows via psql, so a standalone teardown needs DB creds
      # too or it reports spurious failures.
      resolve_db_creds
      run_cleanup
      ;;
  esac

  if [ "$DRY_RUN" = true ]; then setup_dry_run_stubs; fi

  log "workdir: $WORKDIR   env: $ENVIRONMENT   phases: $PHASES   dry-run: $DRY_RUN"
  log "run tag: $EVAL_TAG (every entity carries it; every admin write asserts it)"

  # Announced on stdout as well as in the job summary, so someone reading the log
  # of a deliberately-broken acceptance run knows why it is red without having to
  # find the summary. The injection changes only what the eval EXPECTS (cases 1
  # and 8 assert the WRONG entity/limit type); the platform's behaviour is
  # untouched, which is what makes a red result meaningful.
  if [ -n "$INJECT_FAILURE" ]; then
    log "--inject-failure ${INJECT_FAILURE}: cases 1 and 8 assert the wrong entity_type/limit_type and are expected to FAIL"
  fi

  # Registered before the first mutation so any later failure still restores.
  trap run_cleanup EXIT

  resolve_config

  # The clean room is created AFTER resolve_config (which only reads) and BEFORE
  # seed_world (the first mutation), so a contaminated clean room fails the run
  # in seconds having changed nothing in dev.
  laptop_pod_create
  laptop_provision

  seed_world

  # Each identity's token reaches the pod once. a1's stays on the HARNESS side:
  # the admin writes are harness actions, not laptop actions, and putting an
  # org-admin token in the clean room would defeat the point of the split.
  local who
  for who in U1 U2 U3 X1; do
    provision_identity "$who"
  done

  # Each case is called DIRECTLY (not through a dispatcher taking a function
  # name) so both a reader and shellcheck can see that these functions are
  # invoked; `|| rc=$?` keeps an early return from aborting the matrix.
  local rc before c
  for c in 1 2 3 4 5 6 7 8 9 10 11 12; do
    phase_enabled "$c" || continue
    rc=0; before="$FAILURES"
    case "$c" in
      1)  case_01 || rc=$? ;;
      2)  case_02 || rc=$? ;;
      3)  case_03 || rc=$? ;;
      4)  case_04 || rc=$? ;;
      5)  case_05 || rc=$? ;;
      6)  case_06 || rc=$? ;;
      7)  case_07 || rc=$? ;;
      8)  case_08 || rc=$? ;;
      9)  case_09 || rc=$? ;;
      10) case_10 || rc=$? ;;
      11) case_11 || rc=$? ;;
      12) case_12 || rc=$? ;;
    esac
    after_phase "$c" "$rc" "$before"
  done

  if phase_enabled H; then
    rc=0; before="$FAILURES"; phase_h || rc=$?; after_phase H "$rc" "$before"
  fi

  # run_cleanup fires from the EXIT trap, writes the summary and preserves this
  # exit status.
  if [ "$FAILURES" -gt 0 ]; then
    exit 1
  fi
  exit 0
}

main
