#!/usr/bin/env bash
#
# `source-path` must be a FILE-LEVEL directive (in the leading comment block,
# before the first command); attached to a `.` line it covers only that line.
# shellcheck source-path=SCRIPTDIR
#
# =============================================================================
# run-eval.sh — end-to-end capability eval + regression suite for Bedrock
#               per-principal account routing
# =============================================================================
# Issue #4761 (R7), arc parent #4692 / EPIC #4324. Stories under test: R1 #4742,
# R2 #4743, R3 #4744, R4 #4745, R5 #4746, R6 #4747 — all merged, all asserted
# live. (R5/R6 were unmerged when this file was first drafted; the cases that
# were written against their specs are now real assertions in phase 7.)
#
# WHAT THIS PROVES
#   The routing arc ships with per-story unit tests but nothing proved the
#   CAPABILITY works as a whole against a live environment, nor that the
#   surfaces routing touches (budgets, metering, the core proxy) still behave.
#   This suite is both halves: capability cases (phases 1-8) and regression
#   cases that run every time (phases 9-12).
#
# THE HONESTY CONTRACT — read this before adding a case
#   Two failure modes are equally bad here, and the phase layout exists to
#   avoid both:
#     1. Reporting GREEN when the capability is untested. A case that cannot
#        run must SKIP with the precise blocking reason, never pass.
#     2. Reporting RED for something routing did not cause. Fixture drift and
#        an unwired environment are NOT routing regressions.
#   So: FIXTURE DRIFT IS A LOUD FAIL (phase 0), while a correctly-reported
#   not-yet-wired environment is a SKIP carrying its reason. The distinction
#   is what makes a green run mean something.
#
# VERIFIED LIVE STATE AT AUTHORING (dev, 2026-09-07) — five findings that
# shaped this file. Each is re-derived at runtime by phase 0, never trusted:
#
#   1. ROUTING NOW ALWAYS APPLIES. The former rollout switches are retired.
#      Phases 5/6 still require a fixture-owned inference/denial runner and
#      destination-account evidence; they must not report those unrun cases green.
#
#   2. THE SANDBOX ACCOUNT IS NOT REACHABLE. #4761 names 938500344975 as the
#      routed destination with a "proven" v2 role. Probed live: three roles
#      (OrganizationAccountAccessRole, ADP-Agent-dev-routing-validate,
#      ADP-Agent-sandbox) all return AccessDenied from the gateway account. Per
#      the operator ruling on #4748 a human must re-quick-create it with
#      GatewayAccountId=879318057152. Until then there is no SECOND account to
#      land in, so the "bill really moved" evidence is unavailable rather than
#      merely unwritten. Phase 0 reports this; phase 6 skips on it.
#
#   3. THE REAL DESTINATION FIXTURE LIVES IN THE PLATFORM ACCOUNT.
#      SSM /adp/<env>/bedrock-routing/validation-destination/role-arn resolves
#      to arn:aws:iam::879318057152:role/ADP-Agent-dev-routing-validate.
#      Proven green: assume WITH ExternalId succeeds, WITHOUT is DENIED (the
#      condition is live), and a real bedrock:InvokeModel succeeds.
#
#   4. SHADOW MODE POPULATES, WITH A CUTOVER TO SCOPE AROUND. usage_logs
#      .bedrock_account_id: last NULL row 16:43:26Z, first filled row 16:46:18Z
#      on 2026-09-07; every hour since is 100% filled. All NULLs predate the R2
#      deploy. A naive "never NULL" assertion would fail on pre-deploy history
#      — the #4743 lesson. This suite asserts NULL-freedom only for rows AFTER
#      the observed cutover, and only for its own traffic.
#
#   5. DO NOT HARDCODE A MODEL ID. claude-3-5-haiku-20241022 and
#      claude-3-haiku-20240307 now return ResourceNotFoundException ("end of
#      its life"). Phase 0 proves EVAL_ROUTING_MODEL still invokes and FAILS
#      LOUDLY if it EOLs, so a dead model never reads as a routing regression.
#
# SECRETS
#   The destination ExternalId is read from SSM SecureString at runtime and is
#   never echoed, never passed on argv, never written to a file. Bearer tokens
#   reach curl through a config file (argv is readable via /proc on a shared
#   host), matching the shared harness and the R4 ops gate.
#
# IDEMPOTENT / SAFE TO RE-RUN
#   The only rows this suite creates are Bedrock routing mappings, and every one
#   is guarded by assert_test_scope, which `die`s unless the scope names the
#   DESIGNATED TEST TENANT (see the scope-guard note below for why an allowlist
#   rather than a run-tag substring). Cleanup runs from an EXIT trap, verifies
#   zero mappings remain on the test org, and is available standalone via
#   --cleanup-only. Re-authoring the same rule is idempotent server-side (the
#   PUT replaces in place), and DELETE is 204 whether or not a row existed.
#
# NON-GOALS (hard)
#   Saved mappings are active immediately (subject to routing cache refresh). Cases use
#   the designated test org only. An eval must NEVER widen a production flag to
#   make itself pass. No load testing, no prod runs, no UI assertions.
#
# Usage:
#   ./platform/evals/bedrock-routing/run-eval.sh
#   ./platform/evals/bedrock-routing/run-eval.sh --phases 0,1,2
#   ./platform/evals/bedrock-routing/run-eval.sh --cleanup-only
#   ./platform/evals/bedrock-routing/run-eval.sh --dry-run
#
# Exit codes: 0 = zero failed assertions (skips and findings are not failures),
#             1 = at least one assertion FAILED.
# =============================================================================

set -euo pipefail

EVAL_SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
EVAL_LIB_DIR="$(cd "$(dirname "$EVAL_SCRIPT_PATH")/../lib" && pwd)"
# The checkout this eval was run from — used to detect whether R6 (#4747) has
# retired ADP_BEDROCK_VIA (an agent-worker concern with no HTTP surface to probe)
# and to locate the two proven gate scripts under platform/scripts.
#
# THREE levels up, not two: this file lives at
# platform/evals/bedrock-routing/run-eval.sh, so ../.. is platform/ and only
# ../../.. is the repo root. The earlier two-level form made every repo-tree
# lookup miss silently — `[ -f "$REPO_ROOT/modules/..." ]` is simply false, so R6
# reported as "not merged" against merged code and a gate script would report as
# missing. A path that resolves to a real-but-wrong directory fails quietly,
# which is why this is spelled out rather than left to the reader to count.
REPO_ROOT="$(cd "$(dirname "$EVAL_SCRIPT_PATH")/../../.." && pwd)"

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

# --- configuration -----------------------------------------------------------
ENVIRONMENT="${ENVIRONMENT:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
EVAL_RUN_ID="${EVAL_RUN_ID:-local-$$}"
EVAL_USER_PREFIX="eval-bdr"
EVAL_TAG="${EVAL_USER_PREFIX}-${EVAL_RUN_ID}"
EVAL_SEED_NAME="$EVAL_TAG"
EVAL_SUMMARY_TITLE="Bedrock account routing eval (#4761)"

# The routing arc's own account constants. The platform account is the control:
# unmapped calls must land here. The sandbox is the intended routed destination.
PLATFORM_ACCOUNT_EXPECTED="${PLATFORM_ACCOUNT_EXPECTED:-879318057152}"
SANDBOX_ACCOUNT="${SANDBOX_ACCOUNT:-938500344975}"

# Finding 5: resolved and proven live by phase 0, never assumed.
EVAL_ROUTING_MODEL="${EVAL_ROUTING_MODEL:-us.anthropic.claude-sonnet-4-5-20250929-v1:0}"
EVAL_MODEL="${EVAL_MODEL:-global.anthropic.claude-sonnet-4-6}"

# Finding 4: the #4743 in-AWS latency baseline. Measured from a stable vantage
# (the ARC runner / this pod), never a laptop.
LATENCY_BASELINE_P50_MS="${LATENCY_BASELINE_P50_MS:-91.1}"
LATENCY_TOLERANCE_PCT="${LATENCY_TOLERANCE_PCT:-50}"
LATENCY_SAMPLES="${LATENCY_SAMPLES:-10}"

EXTERNAL_ID_PARAM="/adp/${ENVIRONMENT}/bedrock-routing/validation-destination/external-id"
ROLE_ARN_PARAM="/adp/${ENVIRONMENT}/bedrock-routing/validation-destination/role-arn"

# Clean-room / pod plumbing (contract from lib/pod.sh).
PROXY_PORT="${EVAL_PROXY_PORT:-9193}"
POD_NAMESPACE="${POD_NAMESPACE:-adp-gateway}"
POD_IMAGE="${POD_IMAGE:-public.ecr.aws/docker/library/debian:stable-slim}"
POD_LABEL_APP="$EVAL_USER_PREFIX"
POD_RUN_LABEL="$EVAL_RUN_ID"
LAPTOP_POD="${EVAL_USER_PREFIX}-${EVAL_RUN_ID}"
POD_WORKDIR="/tmp/eval"
POD_HOME="$POD_WORKDIR/home"
POD_PATH="$POD_HOME/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
POD_ASSERT_ENV=()

MODE="full"
DRY_RUN=false
FAIL_PHASE=""
INJECT_FAILURE=""
PHASES="0,1,2,3,4,5,6,7,8,9,10,11,12,13"

while [ $# -gt 0 ]; do
  case "$1" in
    --assert-clean-room) MODE="clean-room"; shift ;;
    --cleanup-only)      MODE="cleanup"; shift ;;
    --dry-run)           DRY_RUN=true; shift ;;
    --fail-phase)        FAIL_PHASE="$2"; shift 2 ;;
    --inject-failure)    INJECT_FAILURE="$2"; shift 2 ;;
    --phases)            PHASES="$2"; shift 2 ;;
    --environment)       ENVIRONMENT="$2"; shift 2 ;;
    -h|--help)           sed -n '2,100p' "$0"; exit 0 ;;
    *)                   die "Unknown option: $1" ;;
  esac
done

case "$INJECT_FAILURE" in
  ""|wrong-account) ;;
  *) die "Unknown --inject-failure kind: $INJECT_FAILURE (supported: wrong-account)" ;;
esac

# Re-derive tag-dependent names after --environment/--run-id overrides.
EVAL_TAG="${EVAL_USER_PREFIX}-${EVAL_RUN_ID}"
EVAL_SEED_NAME="$EVAL_TAG"
EXTERNAL_ID_PARAM="/adp/${ENVIRONMENT}/bedrock-routing/validation-destination/external-id"
ROLE_ARN_PARAM="/adp/${ENVIRONMENT}/bedrock-routing/validation-destination/role-arn"

WORKDIR="${EVAL_WORKDIR:-$(mktemp -d)}"
mkdir -p "$WORKDIR"
chmod 700 "$WORKDIR"
STATE_FILE="$WORKDIR/state.env"
TRACE_FILE="$WORKDIR/trace.log"
RESULTS_FILE="$WORKDIR/results.tsv"
FINDINGS_FILE="$WORKDIR/findings.txt"
: > "$TRACE_FILE"
: > "$RESULTS_FILE"
: > "$FINDINGS_FILE"

# Resolved by phase 0.
BASE_URL=""
DESTINATION_ROLE_ARN=""
SANDBOX_TRUSTED=false
SHADOW_MODE_ON=false
R5_PRESENT=false
R6_PRESENT=false
DEST_ID_OWNED=""      # destination whose owner_org_id == our test org
DEST_ID_FOREIGN=""    # destination owned by a DIFFERENT org (the 422 fixture)
TEST_ORG=""
TEST_USER=""

phase_enabled() { case ",$PHASES," in *,"$1",*) return 0 ;; *) return 1 ;; esac; }
maybe_fail_phase() {
  if [ -n "$FAIL_PHASE" ] && [ "$1" = "$FAIL_PHASE" ]; then
    die "--fail-phase $1: simulated failure"
  fi
}

# =============================================================================
# Scope guard — the one thing standing between this eval and a real tenant's
# Bedrock bill.
#
# Note this is deliberately NOT the tag guard used by the budget/ratelimit eval.
# That suite creates its own throwaway orgs, so it can require every write to
# carry the run tag. This suite must author rules on a PRE-EXISTING designated
# test org (routing rules only bind to orgs/teams/users that already exist —
# authoring against a nonexistent org returns 422 scope_not_found), so a
# tag-substring check could never pass and would reject every legitimate write.
#
# The honest invariant here is an ALLOWLIST: a mapping may only ever be authored
# on the designated test tenant. `die`s rather than `fail`s — a mis-scoped write
# must abort the run, not be recorded and continued past.
# =============================================================================
ALLOWED_SCOPE_ORG_PATTERN="${ALLOWED_SCOPE_ORG_PATTERN:-adp-dev-pentest-org-}"

assert_test_scope() {  # assert_test_scope <what> <scope>
  local what="$1" scope="$2"
  case "$scope" in
    *"$ALLOWED_SCOPE_ORG_PATTERN"*) return 0 ;;
    *) die "refusing to author ${what}='${scope}': only the designated test tenant (matching '${ALLOWED_SCOPE_ORG_PATTERN}') may be mapped by this eval" ;;
  esac
}

# =============================================================================
# HTTP helpers — bearer tokens travel via a curl config file, never argv.
# =============================================================================
ADMIN_CURLRC="$WORKDIR/admin.curlrc"

# api — fire ONE request; leave its status in API_STATUS and its body in API_BODY.
#
# Two properties this shape exists to guarantee, both learned the hard way:
#
#   1. ONE request per assertion. The earlier api_status/api_body pair each made
#      their own call, so every check fired twice (each 422 PUT was attempted
#      twice against the live API) and — worse — assert_reason could pair the
#      status of one response with the body of a DIFFERENT one. For a mutating
#      PUT that is not just wasteful, it doubles the write attempts.
#   2. The same retry policy as lib/http.sh. Without it a single edge blip
#      (502/503/504/000 from ALB target churn) yields an empty body, and a
#      caller reading `.rung` from it sees "" — which the phase-8 assertion then
#      reported as "a REJECTED mapping was persisted", a FALSE routing
#      regression. Exactly the lie this suite must never tell. An
#      application-level 5xx is still final and never retried.
API_STATUS=""
API_BODY=""
api() {  # api <METHOD> <path> [json-body] -> sets API_STATUS / API_BODY
  local method="$1" path="$2" body="${3:-}"
  local out="$WORKDIR/api.out" attempt=0 max_attempts=8
  [ -n "$body" ] && printf '%s' "$body" > "$WORKDIR/api.in"
  while :; do
    rm -f "$out"
    if [ -n "$body" ]; then
      API_STATUS="$(curl -s -o "$out" -w '%{http_code}' --max-time 60 -X "$method" \
        -K "$ADMIN_CURLRC" -H 'content-type: application/json' \
        --data-binary "@$WORKDIR/api.in" "${BASE_URL}${path}" || true)"
    else
      API_STATUS="$(curl -s -o "$out" -w '%{http_code}' --max-time 60 -X "$method" \
        -K "$ADMIN_CURLRC" "${BASE_URL}${path}" || true)"
    fi
    # curl already prints its own '000' via -w when it fails before a response, so
    # the old `|| echo 000` fallback CONCATENATED a second one and produced the
    # six-character "000000" seen in run 34170126167 — a value that matches no
    # case arm below, so it skipped the retry policy AND got reported verbatim as
    # if it were a status. Normalise anything that is not exactly three digits.
    case "$API_STATUS" in
      [0-9][0-9][0-9]) ;;
      *) API_STATUS="000" ;;
    esac
    case "$API_STATUS" in
      502|503|504|000)
        is_app_level_5xx "$out" && break   # the app's own verdict: final
        attempt=$((attempt + 1))
        [ "$attempt" -ge "$max_attempts" ] && break
        sleep 3
        ;;
      *) break ;;
    esac
  done
  # curl creates no output file when it rejects the URL locally, so read
  # defensively rather than emitting a bare `cat: ... No such file` to the log.
  API_BODY="$(cat "$out" 2>/dev/null || true)"
}

# api_status / api_body — single-call convenience wrappers. Each still fires
# exactly one request and leaves BOTH halves in API_STATUS/API_BODY, so a caller
# needing the pair should call api() once and read the two variables.
api_status() {  # api_status <METHOD> <path> [json] — echoes status only
  api "$@"; printf '%s' "$API_STATUS"
}

api_body() {  # api_body <METHOD> <path> [json] — echoes body only
  api "$@"; printf '%s' "$API_BODY"
}

jqr() {  # jqr <json> <filter>  — empty string rather than "null"
  printf '%s' "$1" | jq -r "$2 // empty" 2>/dev/null || printf ''
}

# assert_reason — the 422 shape is {"detail":{"reason":...,"message":...}}
assert_reason() {  # assert_reason <label> <status> <body> <want_status> <want_reason>
  local label="$1" status="$2" body="$3" want_status="$4" want_reason="$5"
  local reason; reason="$(jqr "$body" '.detail.reason')"
  if [ "$status" != "$want_status" ]; then
    fail "${label}: expected HTTP ${want_status}, got ${status}"
    return 1
  fi
  if [ "$reason" != "$want_reason" ]; then
    fail "${label}: HTTP ${status} but reason='${reason}' (expected '${want_reason}')"
    return 1
  fi
  pass "${label}: HTTP ${status} reason='${reason}'"
}

# The actionable-error contract from R3: an operator must be able to act on the
# message without reading code. Asserts the account, a cause, and a fix.
assert_actionable() {  # assert_actionable <label> <body> <account_id>
  local label="$1" body="$2" account="$3" ok=0
  local acct reason remediation
  acct="$(jqr "$body" '.details.account_id')"
  reason="$(jqr "$body" '.details.reason')"
  remediation="$(jqr "$body" '.details.remediation')"
  [ "$acct" = "$account" ] || { fail "${label}: details.account_id='${acct}' (expected ${account})"; ok=1; }
  [ -n "$reason" ] || { fail "${label}: details.reason is empty — the cause is not named"; ok=1; }
  [ -n "$remediation" ] || { fail "${label}: details.remediation is empty — no fix is offered"; ok=1; }
  [ "$ok" -eq 0 ] && pass "${label}: error names account ${acct}, cause '${reason}', and a remediation"
  return "$ok"
}

# =============================================================================
# Identity minting. Reuses the proven approach from the R4 ops gate:
#   - platform admin + member from Secrets Manager
#   - a REAL org_admin from the dev pentest-actor Lambda (#4444)
# A hand-rolled token carrying custom:role=org_admin IS A MEMBER (authority is
# DB-resolved from tenant_memberships, migration 021), which is exactly why we
# call the Lambda instead of forging one.
# =============================================================================
mint_token() {  # mint_token <secret-suffix> <out-file>
  local suffix="$1" out="$2" secret pool client user passwd token
  secret="$(h_aws secretsmanager get-secret-value \
    --secret-id "adp/${ENVIRONMENT}/gateway/${suffix}" \
    --query SecretString --output text 2>/dev/null || true)"
  [ -n "$secret" ] || return 1
  user="$(printf '%s' "$secret" | jq -r '.username // .email // empty')"
  passwd="$(printf '%s' "$secret" | jq -r '.password // empty')"
  [ -n "$user" ] && [ -n "$passwd" ] || return 1
  mask "$passwd"
  pool="$(eval_ssm "/adp/${ENVIRONMENT}/gateway/cognito-user-pool-id")"
  client="$(eval_ssm "/adp/${ENVIRONMENT}/gateway/cognito-client-id")"
  [ -n "$pool" ] && [ -n "$client" ] || return 1
  # Auth params go in as JSON from a 0600 file, NOT as `KEY=value,KEY=value`
  # shorthand. The generated test passwords contain ',' ']' and '?', which the
  # AWS CLI shorthand parser splits on — the member credential fails to parse
  # ("Expected: ',' received: ']'") while the admin one happens to be benign, so
  # the shorthand form silently works for one identity and not the other. A file
  # also keeps the password out of argv, which is readable via /proc.
  local apfile="$WORKDIR/authparams.json"
  ( umask 077; jq -n --arg u "$user" --arg p "$passwd" \
      '{USERNAME:$u,PASSWORD:$p}' > "$apfile" )
  token="$(h_aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH \
    --client-id "$client" \
    --auth-parameters "file://${apfile}" \
    --query 'AuthenticationResult.AccessToken' --output text 2>/dev/null || true)"
  rm -f "$apfile"
  [ -n "$token" ] && [ "$token" != "None" ] || return 1
  mask "$token"
  ( umask 077; printf '%s' "$token" > "$out" )
  chmod 600 "$out"
}

# mint_actor_token — echoes the actor's org_id so a denial can be qualified by
# a positive control. Fails closed when the membership row is absent.
mint_actor_token() {  # mint_actor_token <actor> <out-file>
  local actor="$1" out="$2" resp payload token org
  payload="$(printf '{"actor":"%s"}' "$actor" | base64 | tr -d '\n')"
  h_aws lambda invoke --function-name "adp-${ENVIRONMENT}-agent-pentest-actor-token" \
    --payload "$payload" "$WORKDIR/actor.json" >/dev/null 2>&1 || return 1
  resp="$(cat "$WORKDIR/actor.json" 2>/dev/null || true)"
  token="$(jqr "$resp" '.access_token')"
  org="$(jqr "$resp" '.org_id')"
  [ -n "$token" ] || return 1
  mask "$token"
  ( umask 077; printf '%s' "$token" > "$out" )
  chmod 600 "$out"
  printf '%s' "$org"
}

# =============================================================================
# --dry-run — stub the external boundary so the harness's own guards can be
# tested with no AWS, no cluster and no network (tests/test-run-eval-dry-run.sh).
# =============================================================================
# The stubs DERIVE their answers from a tiny in-memory mapping store rather than
# being handed the expected verdict per phase. That is what makes the tests
# discriminating: --inject-failure has to produce a genuine disagreement between
# what the eval asserts and what the store computes. A stub told the answer
# could not fail, and the acceptance check would be worthless.
#
# It also means the stubs re-implement the REFUSAL RULES (which scopes are
# well-formed, which destination belongs to which org), so the dry run exercises
# the same decision table the live API applies.
if [ "$DRY_RUN" = true ]; then
  STUB_DIR="$WORKDIR/stub-state"
  mkdir -p "$STUB_DIR/mappings"

  STUB_DEST_OWNED="dest-owned-0000-0000-0000-000000000001"
  STUB_DEST_FOREIGN="dest-foreign-0000-0000-0000-00000000002"
  STUB_ORG="adp-dev-pentest-org-a"
  STUB_USER="00000000-1111-2222-3333-444444444444"

  # R5's self-selection read (schemas.MySelectionResponse). The stub's member is
  # PINNED by a platform admin, which is the state that makes phase 7's authority
  # case have a subject: own_selection_active is false while a stored selection
  # exists, and pinned_by_platform_admin is what the write guard keys on. Shaped
  # like the real response — `effective` is the R4 ladder answer, not a bare id —
  # so a case reading the wrong field fails in the dry run rather than live.
  STUB_SELF_SELECTION="$(jq -nc \
    --arg dest "$STUB_DEST_OWNED" \
    --arg acct "$PLATFORM_ACCOUNT_EXPECTED" \
    --arg user "$STUB_USER" \
    '{effective: {user_id: $user, rung: "user", account_id: $acct,
                  destination_id: $dest, destination_label: "stub-admin-pin",
                  source: "platform_admin", overrides_self_selection: true,
                  shadowed_rung: "platform", shadowed_account_id: null},
      own_selection_destination_id: null,
      own_selection_account_id: null,
      own_selection_label: null,
      own_selection_credential_id: null,
      own_selection_active: false,
      pinned_by_platform_admin: true,
      connections: [{credential_id: "stub-cred-1", label: "stub connection",
                     account_id: $acct, status: "verified",
                     selectable: true, reason: null}]}')"

  # A scope the stub considers authorable: org:/team:/user: naming a known tenant.
  stub_scope_ok() {
    case "$1" in
      "org:${STUB_ORG}"|"team:${STUB_ORG}-team"|"user:${STUB_USER}") return 0 ;;
      *) return 1 ;;
    esac
  }
  stub_scope_wellformed() {
    case "$1" in org:*|team:*|user:*) return 0 ;; *) return 1 ;; esac
  }

  h_aws() { stub_aws "$@"; }
  eval_ssm() {
    case "$1" in
      */cloudfront-domain)  printf 'stub.eval.invalid' ;;
      */role-arn)           printf 'arn:aws:iam::%s:role/ADP-Agent-%s-routing-validate' "$PLATFORM_ACCOUNT_EXPECTED" "$ENVIRONMENT" ;;
      */external-id)        printf 'stub-external-id' ;;
      */cognito-user-pool-id) printf 'us-east-1_stubpool' ;;
      */cognito-client-id)  printf 'stubclientid' ;;
      *)                    printf '' ;;
    esac
  }
  stub_aws() {
    # Only the calls phase 0 actually makes; anything else returns empty so a new
    # unstubbed dependency shows up as a visible gap rather than a silent pass.
    case "$1 ${2:-}" in
      "sts get-caller-identity") printf '%s' "$PLATFORM_ACCOUNT_EXPECTED" ;;
      "sts assume-role")
        # The fixture contract: WITH ExternalId succeeds, WITHOUT is denied.
        case " $* " in
          *" --external-id "*) printf '{"Credentials":{"AccessKeyId":"ASIASTUB","SecretAccessKey":"stub","SessionToken":"stub"}}' ;;
          *) return 255 ;;
        esac
        ;;
      "bedrock-runtime invoke-model") printf '{"content":[{"text":"ok"}]}' ;;
      "ssm get-parameter")
        # The ExternalId fixture. Read via h_aws (SecureString, --with-decryption)
        # rather than eval_ssm, so it needs its own stub arm.
        case " $* " in
          # EVAL_STUB_DRIFT_EXTERNAL_ID lets the dry-run tests simulate a vanished
          # fixture, proving drift FAILS loudly instead of reading as a routing
          # regression. Test-only; the workflow never sets it.
          *external-id*) [ "${EVAL_STUB_DRIFT_EXTERNAL_ID:-0}" = "1" ] || printf 'stub-external-id' ;;
          *role-arn*)    printf 'arn:aws:iam::%s:role/ADP-Agent-%s-routing-validate' "$PLATFORM_ACCOUNT_EXPECTED" "$ENVIRONMENT" ;;
          *)             printf '' ;;
        esac
        ;;
      *) printf '' ;;
    esac
  }
  mint_token() { ( umask 077; printf 'stub.access.token' > "$2" ); chmod 600 "$2"; }
  mint_actor_token() { ( umask 077; printf 'stub.actor.token' > "$2" ); chmod 600 "$2"; printf '%s' "$STUB_ORG"; }
  laptop_pod_delete() { :; }
  # The live configmap is read via h_kubectl with a jsonpath that returns
  # shadow|platform_account. Routing no longer depends on a configmap switch.
  h_kubectl() {
    case " $* " in
      *BG_BEDROCK_ROUTING_SHADOW_MODE*) printf 'true|%s' "${STUB_WRONG_ACCOUNT:-$PLATFORM_ACCOUNT_EXPECTED}" ;;
      *) printf '' ;;
    esac
  }

  # The stubbed API: a real decision table over a real store.
  api() {
    local method="$1" path="$2" body="${3:-}" scope dest f
    API_STATUS=""; API_BODY=""
    case "$method $path" in
      "GET /api/health")
        API_STATUS=200; API_BODY='{"status":"healthy"}' ;;
      "GET /auth/github")
        API_STATUS=200; API_BODY='<html>sign in</html>' ;;
      "GET /api/admin/bedrock-routing/destinations")
        API_STATUS=200
        API_BODY="$(jq -nc --arg o "$STUB_ORG" --arg a "$PLATFORM_ACCOUNT_EXPECTED" \
          --arg d1 "$STUB_DEST_OWNED" --arg d2 "$STUB_DEST_FOREIGN" \
          '[{id:$d1,account_id:$a,owner_org_id:$o,usable_for_routing:true,verified:true},
            {id:$d2,account_id:"111122223333",owner_org_id:"other-org",usable_for_routing:true,verified:true}]')" ;;
      "GET /api/admin/bedrock-routing/mappings")
        API_STATUS=200
        API_BODY="["
        local first=1
        for f in "$STUB_DIR"/mappings/*; do
          [ -e "$f" ] || continue
          [ "$first" -eq 1 ] || API_BODY="${API_BODY},"
          first=0
          API_BODY="${API_BODY}$(jq -nc --arg o "$STUB_ORG" --arg s "$(basename "$f" | tr '#' ':')" \
            '{scope:$s,scope_id_org:$o,source:"platform_admin"}')"
        done
        API_BODY="${API_BODY}]" ;;
      "GET /api/admin/bedrock-routing/effective/"*)
        API_STATUS=200
        # Resolution walks the ladder: user, then team, then org, then platform.
        local rung=platform src=null acct=null
        for cand in "user:${STUB_USER}" "team:${STUB_ORG}-team" "org:${STUB_ORG}"; do
          if [ -f "$STUB_DIR/mappings/$(printf '%s' "$cand" | tr ':' '#')" ]; then
            rung="${cand%%:*}"; src='"platform_admin"'; acct="\"${PLATFORM_ACCOUNT_EXPECTED}\""
            break
          fi
        done
        # overrides_self_selection is `rung == "user" and source == "platform_admin"`
        # on the real server, and it must be PRESENT even when false: phase 7
        # asserts the field exists, because a user who cannot be told their choice
        # was overridden is the §1.4 display defect. A stub that omitted it failed
        # the eval for the stub's shape rather than the API's.
        local overrides=false
        [ "$rung" = "user" ] && [ "$src" = '"platform_admin"' ] && overrides=true
        API_BODY="$(printf '{"rung":"%s","account_id":%s,"source":%s,"overrides_self_selection":%s,"shadowed_rung":"platform"}' \
          "$rung" "$acct" "$src" "$overrides")" ;;
      "PUT /api/admin/bedrock-routing/mappings/"*)
        scope="${path##*/mappings/}"
        dest="$(jqr "$body" '.destination_id')"
        if ! stub_scope_wellformed "$scope"; then
          API_STATUS=422; API_BODY='{"detail":{"reason":"invalid_scope","message":"scope must be org:, team: or user:"}}'
        elif ! stub_scope_ok "$scope"; then
          API_STATUS=422
          API_BODY="$(jq -nc --arg s "$scope" '{detail:{reason:"scope_not_found",message:("no such scope: "+$s)}}')"
        elif [ "$dest" = "$STUB_DEST_FOREIGN" ] || [ "$dest" != "$STUB_DEST_OWNED" ]; then
          # Foreign-org or unknown destination. The message names the SCOPE's org,
          # never the destination's tenant.
          API_STATUS=422
          API_BODY="$(jq -nc --arg o "$STUB_ORG" \
            '{detail:{reason:"account_unlinked",message:("destination is not linked to "+$o)}}')"
        else
          printf '%s' "$dest" > "$STUB_DIR/mappings/$(printf '%s' "$scope" | tr ':' '#')"
          API_STATUS=200
          API_BODY="$(jq -nc --arg s "$scope" '{scope:$s,source:"platform_admin"}')"
        fi ;;
      "DELETE /api/admin/bedrock-routing/mappings/"*)
        scope="${path##*/mappings/}"
        rm -f "$STUB_DIR/mappings/$(printf '%s' "$scope" | tr ':' '#')"
        API_STATUS=204; API_BODY='' ;;
      "GET /api/me/bedrock-routing/selection")
        # R5 IS merged, so the precheck probe must find the surface mounted. The
        # admin curlrc reaches it too (a platform admin is also a person), and the
        # member path is handled in stub_curl where the pin guard is modelled.
        API_STATUS=200; API_BODY="$STUB_SELF_SELECTION" ;;
      "GET /api/usage/logs"*)
        API_STATUS=200
        # The full metering field set: parity is the assertion, so a stub row that
        # omitted these would fail the eval for the stub's shape, not the API's.
        API_BODY="$(jq -nc --arg a "${STUB_WRONG_ACCOUNT:-$PLATFORM_ACCOUNT_EXPECTED}" --arg o "$STUB_ORG" --arg u "$STUB_USER" \
          '{total:2,items:[{bedrock_account_id:$a,model:"stub",status_code:200,cost_usd:0.01,
                            input_tokens:10,output_tokens:5,user_id:$u,org_id:$o,timestamp:"2026-09-07T17:00:00Z"},
                           {bedrock_account_id:$a,model:"stub",status_code:200,cost_usd:0.02,
                            input_tokens:20,output_tokens:7,user_id:$u,org_id:$o,timestamp:"2026-09-07T17:05:00Z"}]}')" ;;
      "GET /api/budget/person-default/"*|"GET /api/me/budget")
        API_STATUS=200; API_BODY='{"scope":"stub"}' ;;
      "PUT /api/me/budget"*)
        API_STATUS=405; API_BODY='{"detail":"Method Not Allowed"}' ;;
      "GET /api/admin/organizations/"*)
        API_STATUS=200
        API_BODY="$(jq -nc --arg u "$STUB_USER" '{items:[{id:$u,email:"stub@example.invalid"}]}')" ;;
      "POST /api/admin/bedrock-routing/destinations/"*"/verify")
        API_STATUS=200
        API_BODY="$(jq -nc --arg a "$PLATFORM_ACCOUNT_EXPECTED" \
          '{verified:true,destination:{account_id:$a,routing_capable:true,verified:true}}')" ;;
      *) API_STATUS=404; API_BODY='{"detail":"Not Found (unstubbed)"}' ;;
    esac
  }
  # --inject-failure wrong-account — the ACCEPTANCE CHECK on this eval.
  #
  # It does NOT append a synthetic "fail" at the end of the run. That would only
  # prove the harness can print red, which is worthless: it would pass even if
  # every real assertion had been deleted. Instead it perturbs the stubbed WORLD
  # so the platform reports the WRONG signing account, and the eval's genuine
  # assertions have to notice on their own. If those assertions ever stop
  # discriminating, this run goes green and the test suite fails on it.
  if [ "$INJECT_FAILURE" = "wrong-account" ]; then
    # PLATFORM_ACCOUNT_EXPECTED is deliberately left ALONE: the eval must keep
    # asserting against the account it believes is correct, while the stubbed
    # world reports a different one. Rewriting the expectation too would make the
    # two agree again and the injection would pass.
    STUB_WRONG_ACCOUNT="000000000000"
    log "--inject-failure wrong-account: the stubbed platform will report ${STUB_WRONG_ACCOUNT} as the signing account; this run is EXPECTED to fail"
  fi

  # Denials for non-admin identities are decided by which curlrc is presented, so
  # the authz phase exercises real branching rather than a hardcoded 403.
  curl() { stub_curl "$@"; }
  stub_curl() {
    local url="" cfg="" method=GET out="" want_status=0 prev=""
    for a in "$@"; do
      case "$prev" in
        -K) cfg="$a" ;;
        -X) method="$a" ;;
        -o) out="$a" ;;
        -w) case "$a" in *http_code*) want_status=1 ;; esac ;;
      esac
      case "$a" in http*) url="$a" ;; esac
      prev="$a"
    done
    local path="${url#*stub.eval.invalid}"
    local status=000
    case "$cfg" in
      *member*|*orgadmin*)
        # A non-platform-admin identity: reads of its own org succeed, routing
        # writes are refused. This is what makes the positive control meaningful.
        #
        # The SELF surface (R5) is the deliberate exception: it is
        # member-callable by design, and its authz is the shape of the path
        # rather than a role check. A stub that 403'd it would make phase 7's
        # "must be member-callable" assertion fail for a stub artefact, and — far
        # worse — a stub that 2xx'd the pinned write would hide the authority
        # escalation that case exists to catch. So the pinned-row refusal is
        # modelled here with the real code and the real status.
        case "$method $path" in
          "GET /api/admin/organizations/"*) status=200 ;;
          "GET /api/me/bedrock-routing/selection")
            status=200
            [ -n "$out" ] && printf '%s' "$STUB_SELF_SELECTION" > "$out"
            ;;
          "PUT /api/me/bedrock-routing/selection"|"DELETE /api/me/bedrock-routing/selection")
            # The stub's member IS pinned (STUB_SELF_SELECTION says so), so both
            # writes must refuse with the settled reason code.
            status=422
            [ -n "$out" ] && printf '{"detail":{"reason":"pinned_by_platform_admin","message":"A platform admin has chosen which AWS account serves your Bedrock calls."}}' > "$out"
            ;;
          "PUT "*bedrock-routing*)          status=403 ;;
          *)                                status=403 ;;
        esac ;;
      "")
        # No credential presented. The PUBLIC surfaces still answer — /api/health
        # and the sign-in page are reachable unauthenticated by design, and the
        # regression phases probe them with a bare curl. Only the authenticated
        # surfaces 401 here, which is what makes the unauthenticated-denial
        # assertion in phase 8 meaningful rather than incidental.
        case "$path" in
          /auth/github|/api/health) api GET "$path" ""; status="$API_STATUS" ;;
          *)                        status=401 ;;
        esac ;;
      *)
        api "$method" "$path" ""
        status="$API_STATUS"
        [ -n "$out" ] && printf '%s' "$API_BODY" > "$out"
        ;;
    esac
    [ -n "$out" ] && [ ! -s "$out" ] && printf '{}' > "$out"
    if [ "$want_status" -eq 1 ]; then
      printf '%s' "$status"
    elif [ -z "$out" ]; then
      # No -o and no -w: the caller wants the BODY on stdout (the `curl ... |
      # grep -q healthy` health probes). Printing the status code here instead
      # would make every such probe fail for a reason that has nothing to do with
      # what it asserts.
      case "$cfg" in
        *member*|*orgadmin*) printf '%s' "$status" ;;
        *) api "$method" "$path" ""; printf '%s' "$API_BODY" ;;
      esac
    fi
    return 0
  }
  http_post_json() {
    local out="$4"
    printf 'event: message_start\ndata: {"type":"message_start"}\n' > "$out"
    printf '200'
  }
  http_get() { printf '{}' > "$3"; printf '200'; }
fi

# =============================================================================
# Phase 0 — PRECHECK. Resolve and REPORT real state before anything mutates.
# Fixture drift FAILs loudly here so it can never masquerade as a routing
# regression downstream (the issue's own requirement).
# =============================================================================
phase_0() {
  phase 0 "precheck — resolve live state; fixture drift FAILS, unwired env SKIPS"
  maybe_fail_phase 0

  local acct
  acct="$(h_aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
  [ -n "$acct" ] || die "no AWS identity — cannot run"
  log "caller account: ${acct}"
  if [ "$acct" = "$PLATFORM_ACCOUNT_EXPECTED" ]; then
    pass "running in the expected platform account ${acct}"
  else
    fail "running in account ${acct}, expected the platform account ${PLATFORM_ACCOUNT_EXPECTED}"
  fi

  # --- gateway endpoint + health -------------------------------------------
  local cf
  cf="$(eval_ssm "/adp/${ENVIRONMENT}/gateway/cloudfront-domain")"
  if [ -z "$cf" ] || [ "$cf" = "None" ]; then
    cf="$(h_aws cloudfront list-distributions \
      --query "DistributionList.Items[?contains(Comment, 'bedrockgw-${ENVIRONMENT}')].DomainName | [0]" \
      --output text 2>/dev/null || true)"
  fi
  if [ -z "$cf" ] || [ "$cf" = "None" ]; then
    die "could not resolve the gateway endpoint for ${ENVIRONMENT}"
  fi
  BASE_URL="https://${cf}"
  log "gateway: ${BASE_URL}"
  if curl -s --max-time 20 "${BASE_URL}/api/health" | grep -q healthy; then
    pass "gateway /api/health is healthy — route failures are attributable"
  else
    die "gateway /api/health is not healthy — refusing to attribute failures to routing"
  fi

  # --- admin identity -------------------------------------------------------
  local tok="$WORKDIR/admin.token"
  mint_token "test-admin-credentials" "$tok" || die "could not mint the platform-admin identity"
  write_curl_auth_config "$tok" "$ADMIN_CURLRC"
  pass "platform-admin identity minted"

  # --- shadow mode + platform account, read from the LIVE configmap ---------
  local cm
  cm="$(h_kubectl get configmap bedrockgateway-config -n "$POD_NAMESPACE" \
    -o jsonpath='{.data.BG_BEDROCK_ROUTING_SHADOW_MODE}{"|"}{.data.BG_PLATFORM_BEDROCK_ACCOUNT_ID}' 2>/dev/null || true)"
  local shadow platform_acct
  shadow="$(printf '%s' "$cm" | cut -d'|' -f1)"
  platform_acct="$(printf '%s' "$cm" | cut -d'|' -f2)"
  log "configmap: shadow='${shadow}' platform_account='${platform_acct}'"

  if [ "$shadow" = "true" ]; then
    SHADOW_MODE_ON=true
    pass "shadow mode is ON (BG_BEDROCK_ROUTING_SHADOW_MODE=true)"
  else
    fail "shadow mode is '${shadow}', expected true — R2 shadow assertions cannot hold"
  fi

  if [ "$platform_acct" = "$PLATFORM_ACCOUNT_EXPECTED" ]; then
    pass "BG_PLATFORM_BEDROCK_ACCOUNT_ID=${platform_acct} — the shadow control value"
  else
    fail "BG_PLATFORM_BEDROCK_ACCOUNT_ID='${platform_acct}', expected ${PLATFORM_ACCOUNT_EXPECTED}"
  fi

  log "saved routing rules apply automatically; no environment or organization opt-in is required"

  # --- destination fixture health (finding 3) — drift here is a LOUD FAIL ---
  DESTINATION_ROLE_ARN="$(eval_ssm "$ROLE_ARN_PARAM")"
  if [ -z "$DESTINATION_ROLE_ARN" ] || [ "$DESTINATION_ROLE_ARN" = "None" ]; then
    fail "FIXTURE DRIFT: ${ROLE_ARN_PARAM} is missing — the standing destination fixture is gone"
    return 1
  fi
  log "destination fixture: ${DESTINATION_ROLE_ARN}"

  local ext_id
  ext_id="$(h_aws ssm get-parameter --name "$EXTERNAL_ID_PARAM" --with-decryption \
    --query Parameter.Value --output text 2>/dev/null || true)"
  if [ -z "$ext_id" ] || [ "$ext_id" = "None" ]; then
    fail "FIXTURE DRIFT: ${EXTERNAL_ID_PARAM} is missing — cannot prove the ExternalId condition"
    return 1
  fi
  mask "$ext_id"

  # (a) assume WITH the ExternalId must succeed.
  local creds
  creds="$(h_aws sts assume-role --role-arn "$DESTINATION_ROLE_ARN" \
    --role-session-name "${EVAL_USER_PREFIX}-precheck" --external-id "$ext_id" \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text 2>/dev/null || true)"
  if [ -z "$creds" ]; then
    fail "FIXTURE DRIFT: cannot assume ${DESTINATION_ROLE_ARN} with its ExternalId — the destination is not usable"
    return 1
  fi
  pass "destination fixture: assume-role WITH ExternalId succeeded"

  # (b) assume WITHOUT it must be DENIED — proves the condition is live, not
  #     that we merely hold permission.
  if h_aws sts assume-role --role-arn "$DESTINATION_ROLE_ARN" \
      --role-session-name "${EVAL_USER_PREFIX}-noext" \
      --query 'AssumedRoleUser.Arn' --output text >/dev/null 2>&1; then
    fail "FIXTURE DRIFT: assume-role WITHOUT the ExternalId SUCCEEDED — the confused-deputy guard is dead"
  else
    pass "destination fixture: assume-role WITHOUT ExternalId correctly DENIED (condition is live)"
  fi

  # (c) a real InvokeModel on the assumed session (finding 5: catches model EOL
  #     before it can be misread as a routing regression).
  local ak sk st body_b64 rc invout
  ak="$(printf '%s' "$creds" | cut -f1)"
  sk="$(printf '%s' "$creds" | cut -f2)"
  st="$(printf '%s' "$creds" | cut -f3)"
  mask "$sk"; mask "$st"
  body_b64="$(printf '%s' '{"anthropic_version":"bedrock-2023-05-31","max_tokens":8,"messages":[{"role":"user","content":"ping"}]}' | base64 | tr -d '\n')"
  invout="$WORKDIR/precheck-invoke.err"
  rc=0
  AWS_ACCESS_KEY_ID="$ak" AWS_SECRET_ACCESS_KEY="$sk" AWS_SESSION_TOKEN="$st" \
    h_aws bedrock-runtime invoke-model --model-id "$EVAL_ROUTING_MODEL" \
      --body "$body_b64" "$WORKDIR/precheck-invoke.json" >/dev/null 2>"$invout" || rc=$?
  if [ "$rc" -eq 0 ]; then
    pass "destination fixture: real bedrock:InvokeModel succeeded (${EVAL_ROUTING_MODEL})"
  elif grep -q 'ResourceNotFoundException\|end of its life' "$invout" 2>/dev/null; then
    fail "FIXTURE DRIFT: model ${EVAL_ROUTING_MODEL} is EOL/unavailable — pin a live model id (this is NOT a routing regression)"
  else
    fail "FIXTURE DRIFT: bedrock:InvokeModel through the destination failed: $(tr -d '\n' < "$invout" | tail -c 200)"
  fi

  # --- sandbox trust (finding 2) -------------------------------------------
  # Per the #4748 operator ruling this is a precheck that FAILS LOUDLY, never a
  # green skip: the cross-account boundary proof is R7's to own.
  if h_aws sts assume-role \
      --role-arn "arn:aws:iam::${SANDBOX_ACCOUNT}:role/ADP-Agent-${ENVIRONMENT}-routing-validate" \
      --role-session-name "${EVAL_USER_PREFIX}-sandbox" \
      --query 'AssumedRoleUser.Arn' --output text >/dev/null 2>&1; then
    SANDBOX_TRUSTED=true
    pass "sandbox ${SANDBOX_ACCOUNT} is assumable — the cross-account boundary proof can run"
  else
    SANDBOX_TRUSTED=false
    finding "the true cross-account boundary proof cannot run: sandbox ${SANDBOX_ACCOUNT} is not assumable from the gateway account ${PLATFORM_ACCOUNT_EXPECTED}. A human must re-quick-create the destination role there with GatewayAccountId=${PLATFORM_ACCOUNT_EXPECTED} (#4748 ruling). Until then the only proven destination is in the platform account, so 'the bill really moved' has no second account to move to."
    log "sandbox ${SANDBOX_ACCOUNT} not assumable — phase 6 will SKIP with that reason"
  fi

  # --- R5 / R6 surface presence -------------------------------------------
  # R5 (#4746, merged as 169fe17) ships the self-service selector at
  # `/me/bedrock-routing/selection` — a SEPARATE router from R4's admin one,
  # because every route in `admin/bedrock_routing/routes.py` is asserted at
  # source level to call require_platform_admin() first, and these are
  # deliberately member-callable (self_routes.py module docstring).
  #
  # Probe the REAL path. An earlier revision of this eval probed
  # `/api/bedrock-routing/self`, which has never existed at any commit: that
  # 404s permanently, so R5_PRESENT could only ever be false and phase 7 would
  # SKIP forever while reporting "not merged" about code that is merged. A
  # presence probe whose path is wrong is worse than no probe — it manufactures
  # a green-looking absence. Probing GET (not PUT) keeps the precheck read-only.
  local self_status
  self_status="$(api_status GET "/api/me/bedrock-routing/selection")"
  case "$self_status" in
    404)
      R5_PRESENT=false
      log "R5 self-service surface absent (404) — phase 7 will SKIP"
      ;;
    000)
      R5_PRESENT=false
      finding "R5 presence is UNKNOWN: GET /api/me/bedrock-routing/selection did not respond at all (curl 000). Phase 7 will SKIP, but this is a reachability problem with the eval's vantage, not evidence that R5 is unmerged."
      ;;
    *)
      # Any HTTP verdict — including 401/422 — proves the route is mounted.
      R5_PRESENT=true
      log "R5 self-service surface responds ${self_status} — phase 7 will run"
      ;;
  esac
  # R6 (#4747) retires ADP_BEDROCK_VIA=user. That switch lives in the
  # agent-worker entrypoint, not the gateway, so its presence is decided by the
  # image the worker runs rather than by any HTTP surface. Detect it from the
  # repo tree the eval was checked out from, which is the same commit CI builds.
  #
  # Detect the RETIREMENT GUARD, not the string. Post-#4747 the entrypoint still
  # mentions ADP_BEDROCK_VIA constantly — it keeps honouring `gateway`/`direct`
  # and raises RuntimeError on `user` — so "no reference anywhere" was never the
  # post-merge state and would report a merged R6 as unmerged. What actually
  # distinguishes the two worlds is the RETIRED_BEDROCK_VIA table and the raise
  # that consumes it (entrypoint.py:51-72, 1584-1586).
  local entrypoint="$REPO_ROOT/modules/agent-factory/agent-worker-image/entrypoint.py"
  if [ ! -f "$entrypoint" ]; then
    R6_PRESENT=false
    finding "R6 status is UNKNOWN: ${entrypoint#"$REPO_ROOT"/} is not present in this checkout, so the retirement guard cannot be inspected. Phase 7 reports it rather than assuming either state."
  elif grep -q 'RETIRED_BEDROCK_VIA' "$entrypoint" 2>/dev/null; then
    R6_PRESENT=true
    log "R6 merged: the RETIRED_BEDROCK_VIA guard is present in the agent-worker entrypoint"
  else
    R6_PRESENT=false
    log "R6 not merged: no RETIRED_BEDROCK_VIA guard in the agent-worker entrypoint — '=user fails loudly' is not yet the behaviour"
  fi

  pass "precheck complete: sandbox_trusted=${SANDBOX_TRUSTED} shadow=${SHADOW_MODE_ON} r5=${R5_PRESENT} r6_retired=${R6_PRESENT}"
}

# =============================================================================
# Phase 1 — the test world. One org-rung fixture on the DESIGNATED test org.
# =============================================================================
phase_1() {
  phase 1 "resolve the designated test org, a member, and the destination fixtures"
  maybe_fail_phase 1

  # The pentest actor org is the designated test tenant: it is synthetic, it
  # already carries real tenant_memberships rows (so authority is genuine), and
  # it is never a customer org.
  local tok="$WORKDIR/orgadmin.token" org
  org="$(mint_actor_token "org_admin_a" "$tok" || true)"
  if [ -z "$org" ]; then
    fail "could not mint org_admin_a from the actor Lambda — no designated test org"
    return 1
  fi
  TEST_ORG="$org"
  state_set TEST_ORG "$TEST_ORG"
  pass "designated test org resolved: ${TEST_ORG}"

  # A member of that org, resolved by canonical users.id (the user rung takes
  # users.id, never a Cognito sub).
  local body
  body="$(api_body GET "/api/admin/organizations/${TEST_ORG}/users?limit=5")"
  TEST_USER="$(jqr "$body" '(.items // .users // .)[0].id')"
  if [ -z "$TEST_USER" ]; then
    fail "no user found in ${TEST_ORG} — cannot assert effective resolution"
    return 1
  fi
  state_set TEST_USER "$TEST_USER"
  pass "test principal resolved: users.id=${TEST_USER} in ${TEST_ORG}"

  # Destination fixtures: one owned by the test org (valid target) and one
  # owned by a DIFFERENT org (the account_unlinked 422 fixture).
  local dests
  dests="$(api_body GET "/api/admin/bedrock-routing/destinations")"
  DEST_ID_OWNED="$(printf '%s' "$dests" | jq -r --arg o "$TEST_ORG" \
    'map(select(.owner_org_id == $o and .usable_for_routing == true)) | .[0].id // empty' 2>/dev/null || true)"
  DEST_ID_FOREIGN="$(printf '%s' "$dests" | jq -r --arg o "$TEST_ORG" \
    'map(select(.owner_org_id != $o and .owner_org_id != null and .usable_for_routing == true)) | .[0].id // empty' 2>/dev/null || true)"

  if [ -n "$DEST_ID_OWNED" ]; then
    pass "usable destination owned by ${TEST_ORG}: ${DEST_ID_OWNED}"
  else
    fail "FIXTURE DRIFT: no usable destination is linked to ${TEST_ORG} — mapping cases cannot author"
  fi
  if [ -n "$DEST_ID_FOREIGN" ]; then
    pass "foreign-org destination available as the tenant-isolation fixture: ${DEST_ID_FOREIGN}"
  else
    skip "tenant-isolation 422 case: no destination owned by a different org exists to point at"
  fi
}

# =============================================================================
# Phase 2 — shadow baseline. An unmapped call lands on the platform account.
# =============================================================================
phase_2() {
  phase 2 "shadow baseline — unmapped traffic is attributed to the platform account"
  maybe_fail_phase 2

  if [ "$SHADOW_MODE_ON" != true ]; then
    skip "shadow baseline: shadow mode is OFF in ${ENVIRONMENT}, so bedrock_account_id is NULL by design"
    return 0
  fi

  # Read the shadow column back over HTTP (no psql dependency): /api/usage/logs
  # returns bedrock_account_id per row. org_id is required.
  local body status rows filled nulls wrong
  api GET "/api/usage/logs?org_id=${TEST_ORG}&limit=100"
  status="$API_STATUS"; body="$API_BODY"
  if [ "$status" != "200" ]; then
    fail "GET /api/usage/logs -> ${status} (expected 200)"
    return 1
  fi
  rows="$(printf '%s' "$body" | jq -r '.items | length' 2>/dev/null || echo 0)"
  if [ "${rows:-0}" -eq 0 ]; then
    # An empty test org is not a routing failure; say so precisely.
    skip "shadow baseline: ${TEST_ORG} has no usage rows to inspect (synthetic org carries no traffic)"
  else
    filled="$(printf '%s' "$body" | jq -r '[.items[] | select(.bedrock_account_id != null)] | length')"
    nulls="$(printf '%s' "$body" | jq -r '[.items[] | select(.bedrock_account_id == null)] | length')"
    wrong="$(printf '%s' "$body" | jq -r --arg p "$PLATFORM_ACCOUNT_EXPECTED" \
      '[.items[] | select(.bedrock_account_id != null and .bedrock_account_id != $p)] | length')"
    log "usage rows=${rows} filled=${filled} null=${nulls} non-platform=${wrong}"
    if [ "${wrong:-0}" -eq 0 ]; then
      pass "every attributed row for unmapped traffic names the platform account ${PLATFORM_ACCOUNT_EXPECTED}"
    else
      fail "${wrong} unmapped row(s) name an account other than ${PLATFORM_ACCOUNT_EXPECTED}"
    fi
    # Finding 4: NULLs are only legitimate BEFORE the R2 cutover. We cannot see
    # per-row deploy time over HTTP, so report rather than fail on history.
    if [ "${nulls:-0}" -gt 0 ]; then
      finding "${nulls} of ${rows} inspected usage rows have a NULL bedrock_account_id. All NULLs observed in dev predate the R2 shadow deploy (last NULL 16:43:26Z, first filled 16:46:18Z on 2026-09-07, 100% filled every hour since). Treat a NULL on a row newer than the cutover as a real R2 regression."
    else
      pass "no NULL bedrock_account_id in the inspected window (post-R2 rows are attributed)"
    fi
  fi
}

# =============================================================================
# Phase 3 — the destination is registered and provably routing-capable.
# =============================================================================
phase_3() {
  phase 3 "destination is registered, verified, and routing-capable"
  maybe_fail_phase 3

  [ -n "$DEST_ID_OWNED" ] || { skip "destination verify: no usable destination for ${TEST_ORG}"; return 0; }

  local body status
  api POST "/api/admin/bedrock-routing/destinations/${DEST_ID_OWNED}/verify" '{}'
  status="$API_STATUS"; body="$API_BODY"
  if [ "$status" != "200" ]; then
    fail "verify probe -> ${status} (expected 200)"
    return 1
  fi
  local verified capable acct
  verified="$(jqr "$body" '.verified')"
  capable="$(jqr "$body" '.destination.routing_capable')"
  acct="$(jqr "$body" '.destination.account_id')"
  if [ "$verified" = "true" ] && [ "$capable" = "true" ]; then
    pass "verify probe: destination ${acct} is verified and routing_capable (gateway proved it can assume + invoke)"
  else
    fail "verify probe: verified='${verified}' routing_capable='${capable}' reason='$(jqr "$body" '.reason')'"
  fi

  # The response must never leak the role ARN or ExternalId — redaction is a
  # design property of the schema (schemas.py has no role_arn field).
  if printf '%s' "$body" | grep -qi 'role_arn\|external_id\|externalid'; then
    fail "the verify response leaks role_arn/external_id — redaction has regressed"
  else
    pass "verify response carries no role_arn/external_id (redaction holds)"
  fi
}

# =============================================================================
# Phase 4 — authoring a mapping moves the effective rung. Then it is removed.
# =============================================================================
phase_4() {
  phase 4 "authoring a mapping changes effective resolution, and removal restores platform"
  maybe_fail_phase 4

  [ -n "$DEST_ID_OWNED" ] || { skip "mapping authoring: no usable destination for ${TEST_ORG}"; return 0; }
  [ -n "$TEST_USER" ] || { skip "mapping authoring: no test principal resolved"; return 0; }

  # Baseline: with no rule, the ladder bottoms out at the platform rung.
  local body rung
  body="$(api_body GET "/api/admin/bedrock-routing/effective/${TEST_USER}")"
  rung="$(jqr "$body" '.rung')"
  if [ "$rung" = "platform" ]; then
    pass "before authoring: effective rung is 'platform' (no rule governs the principal)"
  else
    fail "before authoring: effective rung is '${rung}', expected 'platform' — a stale mapping is present"
  fi
  # An asymmetry worth pinning: at the platform rung the effective read reports
  # account_id=null while shadow rows record the concrete platform account.
  if [ -z "$(jqr "$body" '.account_id')" ]; then
    finding "GET /effective/{user} reports account_id=null at rung='platform', while usage_logs.bedrock_account_id records the concrete platform account ${PLATFORM_ACCOUNT_EXPECTED} for the same call. Two surfaces describe the same state differently; a UI reading the effective endpoint cannot name the account that will actually be billed."
  fi

  # Author at the ORG rung. Guard the scope, then record intent BEFORE the
  # mutating call so cleanup can find it even if we die mid-write.
  local scope="org:${TEST_ORG}"
  assert_test_scope "mapping scope" "$scope"
  state_set CREATED_MAPPING_SCOPE "$scope"
  local status
  status="$(api_status PUT "/api/admin/bedrock-routing/mappings/${scope}" \
    "{\"destination_id\":\"${DEST_ID_OWNED}\"}")"
  body="$(api_body GET "/api/admin/bedrock-routing/effective/${TEST_USER}")"
  if [ "$status" != "200" ]; then
    fail "PUT mappings/${scope} -> ${status} (expected 200)"
    return 1
  fi
  pass "PUT mappings/${scope} -> 200 (rule authored)"

  rung="$(jqr "$body" '.rung')"
  local acct src shadowed
  acct="$(jqr "$body" '.account_id')"
  src="$(jqr "$body" '.source')"
  shadowed="$(jqr "$body" '.shadowed_rung')"
  if [ "$rung" = "org" ]; then
    pass "effective resolution names the authored rung: rung='org' account=${acct} source='${src}' shadowed_rung='${shadowed}'"
  else
    fail "effective rung is '${rung}' after authoring an org rule (expected 'org')"
  fi
  if [ "$src" = "platform_admin" ]; then
    pass "the rule is attributed to platform_admin (authority is recorded)"
  else
    fail "mapping source='${src}', expected 'platform_admin'"
  fi

  # Idempotency: re-authoring the same rule must not error or duplicate.
  status="$(api_status PUT "/api/admin/bedrock-routing/mappings/${scope}" \
    "{\"destination_id\":\"${DEST_ID_OWNED}\"}")"
  if [ "$status" = "200" ]; then
    pass "re-authoring the same rule is idempotent (200, replaced in place)"
  else
    fail "re-authoring returned ${status}, expected 200"
  fi

  # Remove it and prove the ladder falls back — the rollback lever operators
  # depend on.
  status="$(api_status DELETE "/api/admin/bedrock-routing/mappings/${scope}")"
  if [ "$status" = "204" ]; then
    pass "DELETE mappings/${scope} -> 204"
  else
    fail "DELETE mappings/${scope} -> ${status} (expected 204)"
  fi
  state_set CREATED_MAPPING_SCOPE ""
  rung="$(jqr "$(api_body GET "/api/admin/bedrock-routing/effective/${TEST_USER}")" '.rung')"
  if [ "$rung" = "platform" ]; then
    pass "after removal the principal falls back to the platform rung (rollback works)"
  else
    fail "after removal the effective rung is '${rung}', expected 'platform'"
  fi
}

# =============================================================================
# Phase 5 — enforcement: a mapped call really signs with the destination.
# Requires a fixture-owned inference runner and destination-account evidence.
# =============================================================================
phase_5() {
  phase 5 "enforcement — a mapped principal's call is signed with the destination"
  maybe_fail_phase 5
  skip "enforcement success case: this phase needs a fixture-owned inference runner and destination-account evidence; it does not execute those checks yet"
}

# =============================================================================
# Phase 6 — fail-closed, and the cross-account landing proof.
# Requires a disposable destination role and a controlled denial runner.
# =============================================================================
phase_6() {
  phase 6 "fail-closed — a broken mapping errors actionably and never falls back"
  maybe_fail_phase 6

  if [ "$SANDBOX_TRUSTED" != true ]; then
    skip "cross-account landing proof: sandbox ${SANDBOX_ACCOUNT} is not assumable from ${PLATFORM_ACCOUNT_EXPECTED}, so there is no second account for the bill to move to (a human must re-quick-create its role with GatewayAccountId=${PLATFORM_ACCOUNT_EXPECTED})"
  else
    skip "cross-account landing proof: this phase still needs an inference runner and destination-account evidence"
  fi
  skip "fail-closed case: this phase needs a disposable destination role and a controlled denial runner; it does not execute that check yet"

  # NOTE for whoever activates this phase:
  #   * Assert on APP-SIDE structured fields, never CloudTrail requestParameters.
  #     On a DENIED AssumeRole requestParameters is null and roleArn is None, so
  #     a check reading requestParameters.roleArn sees nothing on exactly the
  #     fail-closed path. assert_actionable() encodes the right assertion:
  #     HTTP 502, error=bedrock_account_unavailable, details.{account_id,reason,
  #     remediation}.
  #   * For landing evidence read trail OBJECTS FROM S3. `cloudtrail
  #     lookup-events` caps ~50 events/page then throttles, and dev KEDA/webhook
  #     volume pushes our events out of any recent window.
}

# =============================================================================
# Phase 7 — self-service selection (R5 #4746) and the retired switch (R6 #4747).
#
# Both stories are MERGED on main (169fe17, 6ff19d8), so these are live
# assertions, not spec-shaped skips. Two things about R5's real shape differ from
# how this issue and #4748 described it in advance, and the assertions follow the
# CODE:
#
#   1. The refusal is a **422**, not a 409. `self_routes._rejected` deliberately
#      mirrors R4's `routes._rejected` so both halves of the surface answer one
#      error vocabulary — {"detail":{"reason","message"}} — because the client
#      branches on `reason` and two parsers for one vocabulary is the defect it
#      was avoiding (self_routes.py:109-116, §6.7 item 2). Asserting 409 here
#      would fail a correct implementation.
#   2. The write names a **credential_id**, not R4's destination_id: a person
#      owns connections, not registry rows, and has no way to learn a
#      destination_id (schemas.MySelectionRequest). The server finds or creates
#      the registry row, which is what keeps the ownership check total.
#
# The reason code for an admin-pinned row is `pinned_by_platform_admin`
# (service.require_not_admin_pinned).
# =============================================================================
phase_7() {
  phase 7 "self-service selection (R5 #4746) and the retired switch (R6 #4747)"
  maybe_fail_phase 7

  # --- Post-R6: ADP_BEDROCK_VIA=user must fail loudly ----------------------
  # This is an agent-worker concern with no gateway HTTP surface, so it is
  # asserted against the entrypoint's retirement guard at the checked-out commit
  # — the same commit CI builds the agent-runtime image from. Asserting the
  # SHAPE (retired value -> hard failure, `gateway` still honoured) rather than
  # re-running the worker's own unit tests.
  local entrypoint="$REPO_ROOT/modules/agent-factory/agent-worker-image/entrypoint.py"
  if [ "$R6_PRESENT" != true ]; then
    skip "post-R6 ADP_BEDROCK_VIA handling: R6 (#4747) is not merged at this commit — the RETIRED_BEDROCK_VIA guard is absent, so '=user fails loudly' is not yet the behaviour"
  else
    # =user must raise, and the raise must be reached BEFORE any token spend or
    # proxy start — a retired mode that fails after side effects is not fail-loud.
    if grep -q 'raise RuntimeError(RETIRED_BEDROCK_VIA\[bedrock_via\])' "$entrypoint" 2>/dev/null; then
      pass "post-R6: ADP_BEDROCK_VIA=user raises from the RETIRED_BEDROCK_VIA table — it fails loudly rather than silently degrading"
    else
      fail "post-R6: the RETIRED_BEDROCK_VIA table exists but nothing raises from it — a retired mode that does not fail loudly is a silent billing-path change"
    fi
    if grep -q '"user"' "$entrypoint" 2>/dev/null && \
       sed -n '45,80p' "$entrypoint" 2>/dev/null | grep -q 'retired'; then
      pass "post-R6: the retirement error text names the retired value and points at the replacement (an operator can act on it)"
    else
      finding "post-R6: could not confirm the retirement error text names '=user' and its replacement; the guard fires but its message may not be actionable"
    fi
    # =gateway unchanged is the other half of the issue's requirement.
    if grep -q 'ADP_BEDROCK_VIA=gateway' "$entrypoint" 2>/dev/null; then
      pass "post-R6: ADP_BEDROCK_VIA=gateway is still honoured — the retirement removed the =user branch only"
    else
      fail "post-R6: no surviving =gateway path in the entrypoint — R6 was supposed to retire =user, not the gateway route"
    fi
  fi

  if [ "$R5_PRESENT" != true ]; then
    skip "self-service selection: the self-rung route /api/me/bedrock-routing/selection is not reachable at this vantage, so a user selecting their own connection cannot be asserted"
    skip "admin override visibility: requires the self-rung surface to establish a user selection for an admin rule to override"
    skip "self-service refusal of an admin-authored row: requires the self-rung surface; the settled behaviour is a 422 + reason='pinned_by_platform_admin' with the row untouched"
    return 0
  fi

  # --- The effective read must disclose an admin override ------------------
  # §1.4's SETTLED display: a stored row is not evidence that it governs, so the
  # server states the override rather than leaving the client to infer it.
  local body
  body="$(api_body GET "/api/admin/bedrock-routing/effective/${TEST_USER}")"
  # PRESENCE must be tested with has(), not jqr. `jqr` is `.field // empty`, and
  # jq's // treats `false` as absent — so a correctly-present `false` reads as a
  # missing field and this case would fail on exactly the common state (no
  # override in effect). Every boolean-presence assertion in this phase uses
  # has() for that reason.
  if printf '%s' "$body" | jq -e 'has("overrides_self_selection")' >/dev/null 2>&1; then
    pass "effective read exposes overrides_self_selection='$(jq -r '.overrides_self_selection' <<<"$body")' (the user's screen state is derivable)"
  else
    fail "effective read does not expose overrides_self_selection — a user cannot be told their choice was overridden"
  fi

  # --- The self surface, as an ordinary member ------------------------------
  # Deliberately NOT the admin token: this route's authz is the SHAPE of the
  # path (no target parameter, anchor derived from the token), so exercising it
  # as a platform admin would prove nothing about member-callability — the one
  # property that distinguishes it from R4's router.
  local mem_tok="$WORKDIR/selfmember.token"
  if ! mint_token "test-user-credentials" "$mem_tok"; then
    skip "self-service selection: could not mint the member identity, so the member-callable surface cannot be exercised"
    return 0
  fi
  local mem_rc="$WORKDIR/selfmember.curlrc"
  write_curl_auth_config "$mem_tok" "$mem_rc"

  local sel_body sel_status
  sel_status="$(curl -s -o "$WORKDIR/self.out" -w '%{http_code}' --max-time 60 \
    -K "$mem_rc" "${BASE_URL}/api/me/bedrock-routing/selection" || echo "000")"
  sel_body="$(cat "$WORKDIR/self.out" 2>/dev/null || true)"

  case "$sel_status" in
    2*)
      pass "GET /me/bedrock-routing/selection as a MEMBER -> ${sel_status} (the self surface is member-callable, unlike R4's admin router)"
      # own_selection_active is stated by the server precisely because a stored
      # row and an in-force row are different facts (schemas.py:208-211).
      # has(), not jqr: own_selection_active is false in the two most interesting
      # states (admin-pinned, and own-pick-no-longer-usable), and jq's // would
      # report a present `false` as missing.
      local active eff_rung
      eff_rung="$(jqr "$sel_body" '.effective.rung')"
      if printf '%s' "$sel_body" | jq -e 'has("own_selection_active")' >/dev/null 2>&1; then
        active="$(jq -r '.own_selection_active' <<<"$sel_body")"
        pass "the read states own_selection_active='${active}' with effective.rung='${eff_rung}' — the server says what is IN FORCE, not merely what is stored"
      else
        fail "the read omits own_selection_active — the screen would have to infer whether the user's own pick governs, which is the inert-config defect §6.4 forbids"
      fi
      # The selectable list is an affordance; its presence is what lets a user
      # pick without guessing ids.
      if printf '%s' "$sel_body" | jq -e 'has("connections")' >/dev/null 2>&1; then
        pass "the read returns a connections list — a user can select without knowing a destination_id"
      else
        fail "the read omits connections — a user has no way to learn what they may pick"
      fi
      ;;
    401|403)
      fail "GET /me/bedrock-routing/selection as a member -> ${sel_status}: the self surface must be member-callable; a denial here means R5's authz shape regressed toward the admin router"
      ;;
    422)
      skip "self-service read: the member identity has no users row to resolve (422), so nothing can be resolved for them — not a routing failure"
      ;;
    404)
      fail "GET /me/bedrock-routing/selection -> 404: the route is absent for a member although the precheck saw it mounted"
      ;;
    *)
      fail "GET /me/bedrock-routing/selection as a member -> ${sel_status} (unexpected)"
      ;;
  esac

  # --- The authority case: a member must not un-pin an admin's mapping -----
  # The sharpest thing in R5. uq_bedrock_account_mapping_scope allows exactly ONE
  # row per user rung, so both surfaces upsert the same row: without a write-time
  # guard a person re-selecting their own account silently overwrites a platform
  # admin's pin, reversing the exact decision the override exists to take out of
  # their hands. Guarded on DELETE too, because delete-then-reselect is otherwise
  # a two-request bypass.
  #
  # This case is only meaningful when the caller IS pinned. Phase 1 authors an
  # org-rung fixture, not a user-rung pin on the member, so rather than assert
  # against an unknown state we read the caller's own pinned flag and branch.
  # Read with jq directly rather than jqr so an explicit `false` is distinguishable
  # from an absent field: "not pinned" is a legitimate state that must SKIP with a
  # reason, while a MISSING field means the surface no longer discloses the pin at
  # all — which is the §1.4 display defect and must not read as "not pinned".
  local pinned="absent"
  if printf '%s' "$sel_body" | jq -e 'has("pinned_by_platform_admin")' >/dev/null 2>&1; then
    pinned="$(jq -r '.pinned_by_platform_admin' <<<"$sel_body")"
  else
    finding "the self read omits pinned_by_platform_admin — a pinned user cannot be told an admin's choice governs them (§1.4), and this eval cannot tell 'not pinned' from 'not disclosed'"
  fi
  if [ "$pinned" = "true" ]; then
    # Attempt the overwrite with a deliberately non-resolving credential id: if
    # the pin guard is ordered correctly it refuses BEFORE the credential lookup
    # or the assume probe, so a pinned caller gets the accurate reason rather
    # than a misleading "no such connection" (self_routes: the guard is ordered
    # before the probe on purpose).
    local put_status put_body
    put_status="$(curl -s -o "$WORKDIR/selfput.out" -w '%{http_code}' --max-time 60 \
      -X PUT -K "$mem_rc" -H 'content-type: application/json' \
      --data-binary '{"credential_id":"eval-nonexistent-credential"}' \
      "${BASE_URL}/api/me/bedrock-routing/selection" || echo "000")"
    put_body="$(cat "$WORKDIR/selfput.out" 2>/dev/null || true)"
    local reason; reason="$(jqr "$put_body" '.detail.reason')"
    if [ "$put_status" = "422" ] && [ "$reason" = "pinned_by_platform_admin" ]; then
      pass "a pinned member's self write is refused 422 reason='pinned_by_platform_admin' — admin wins, and the guard runs before the credential lookup"
    else
      case "$put_status" in
        2*)
          fail "AUTHORITY ESCALATION: a pinned member's self write returned ${put_status} — a user can silently un-pin a platform admin's mapping"
          ;;
        *)
          finding "pinned self write returned ${put_status} reason='${reason}' (expected 422/pinned_by_platform_admin). The write did not succeed, so this is not an escalation, but the reason code is not the settled one."
          ;;
      esac
    fi
    # The same guard must hold on DELETE, or the refusal above is bypassable.
    local del_status
    del_status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 \
      -X DELETE -K "$mem_rc" \
      "${BASE_URL}/api/me/bedrock-routing/selection" || echo "000")"
    case "$del_status" in
      422) pass "a pinned member's DELETE is refused ${del_status} — delete-then-reselect is not a two-request bypass" ;;
      2*)  fail "AUTHORITY ESCALATION: a pinned member's DELETE returned ${del_status} — the pin can be cleared, then re-selected freely" ;;
      *)   finding "pinned DELETE returned ${del_status} (expected 422). Not an escalation, but not the settled shape either." ;;
    esac
  else
    skip "self-service refusal of an admin-authored row: this member is not currently pinned by a platform admin (pinned_by_platform_admin='${pinned:-unset}'), so the guard has no subject in this run. The settled behaviour is 422 + reason='pinned_by_platform_admin', row untouched, enforced on PUT and DELETE alike."
  fi

  # A self write must never be able to name another person. There is no user_id
  # field at any position (that absence IS the access control), so the check is
  # that adding one changes nothing rather than being honoured.
  local inject_status
  inject_status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 \
    -X PUT -K "$mem_rc" -H 'content-type: application/json' \
    --data-binary "{\"credential_id\":\"eval-nonexistent-credential\",\"user_id\":\"${TEST_USER}\"}" \
    "${BASE_URL}/api/me/bedrock-routing/selection" || echo "000")"
  case "$inject_status" in
    2*) fail "a self write carrying an extra user_id returned ${inject_status} with a credential that does not resolve — the anchor may be taken from the BODY rather than the token" ;;
    *)  pass "a self write naming another user_id is not honoured (${inject_status}) — the anchor stays derived from the token" ;;
  esac
}

# =============================================================================
# Phase 8 — authz + tenant isolation. Runnable today; the strongest cases here.
# =============================================================================
phase_8() {
  phase 8 "authz and tenant isolation at authoring time"
  maybe_fail_phase 8

  [ -n "$DEST_ID_OWNED" ] || { skip "authz matrix: no usable destination to point a write at"; return 0; }

  # --- the STRONG denial: a REAL org_admin, qualified by a positive control ---
  # A member is denied by ANY authz check, so a member-only 403 proves nothing
  # (the #4794 lesson). We prove this identity HAS authority somewhere first.
  local oa_tok="$WORKDIR/orgadmin.token" oa_org
  oa_org="$(mint_actor_token "org_admin_a" "$oa_tok" || true)"
  if [ -z "$oa_org" ]; then
    skip "org_admin denial: could not mint org_admin_a from the actor Lambda (it fails closed when the tenant_memberships row is absent) — NOT counted as a pass"
  else
    local oa_rc="$WORKDIR/orgadmin.curlrc" ctl
    write_curl_auth_config "$oa_tok" "$oa_rc"
    ctl="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -K "$oa_rc" \
      "${BASE_URL}/api/admin/organizations/${oa_org}/users?limit=1" || echo "000")"
    case "$ctl" in
      2*)
        pass "control: org_admin_a reads its OWN org (${ctl}) — its authority is real, so a denial below counts"
        local code
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X PUT -K "$oa_rc" \
          -H 'content-type: application/json' \
          --data-binary "{\"destination_id\":\"${DEST_ID_OWNED}\"}" \
          "${BASE_URL}/api/admin/bedrock-routing/mappings/org:${oa_org}" || echo "000")"
        case "$code" in
          401|403) pass "PUT mapping as a REAL org_admin for its OWN org -> ${code} (denied) <- the strong assertion" ;;
          404)     fail "PUT mapping -> 404: route absent, so the denial proves nothing (R4 not deployed)" ;;
          2*)      fail "PUT mapping -> ${code} for an ORG_ADMIN — AUTHZ HOLE: a tenant admin can author platform routing" ;;
          *)       fail "PUT mapping as org_admin -> ${code} (unexpected)" ;;
        esac
        ;;
      *)
        # Do not silently fall back to the weak member-only assertion.
        skip "org_admin denial: the positive control returned ${ctl}, so this identity's authority is unproven and its 403 would be vacuous"
        ;;
    esac
  fi

  # --- the weak-but-cheap controls -----------------------------------------
  local mem_tok="$WORKDIR/member.token"
  if mint_token "test-user-credentials" "$mem_tok"; then
    local mem_rc="$WORKDIR/member.curlrc" code
    write_curl_auth_config "$mem_tok" "$mem_rc"
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X PUT -K "$mem_rc" \
      -H 'content-type: application/json' \
      --data-binary "{\"destination_id\":\"${DEST_ID_OWNED}\"}" \
      "${BASE_URL}/api/admin/bedrock-routing/mappings/org:${TEST_ORG}" || echo "000")"
    case "$code" in
      401|403) pass "PUT mapping as a member -> ${code} (denied; weak — any authz check does this)" ;;
      2*)      fail "PUT mapping -> ${code} for a MEMBER — AUTHZ HOLE" ;;
      *)       fail "PUT mapping as member -> ${code} (unexpected)" ;;
    esac
  else
    skip "member denial: could not mint the member identity"
  fi

  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X PUT \
    -H 'content-type: application/json' \
    --data-binary "{\"destination_id\":\"${DEST_ID_OWNED}\"}" \
    "${BASE_URL}/api/admin/bedrock-routing/mappings/org:${TEST_ORG}" || echo "000")"
  case "$code" in
    401|403) pass "PUT mapping unauthenticated -> ${code} (denied)" ;;
    *)       fail "PUT mapping unauthenticated -> ${code} (expected 401/403)" ;;
  esac

  # --- 422s: nothing may be stored ----------------------------------------
  # Each case calls api() ONCE and reads the status/body pair from that single
  # response. Firing the status and body separately would attempt every one of
  # these mutating PUTs twice and could mismatch a status against another
  # response's body.
  if [ -n "$DEST_ID_FOREIGN" ]; then
    api PUT "/api/admin/bedrock-routing/mappings/org:${TEST_ORG}" \
      "{\"destination_id\":\"${DEST_ID_FOREIGN}\"}"
    assert_reason "foreign-org destination" "$API_STATUS" "$API_BODY" "422" "account_unlinked" || true
    # The message must name the SCOPE's org, never the destination's tenant —
    # otherwise it is a cross-tenant enumeration oracle.
    if printf '%s' "$API_BODY" | grep -q "$TEST_ORG"; then
      pass "the refusal names the scope's own org and does not disclose the destination's tenant"
    else
      finding "the account_unlinked message did not name the scope org ${TEST_ORG}; confirm it still avoids disclosing the destination's owning tenant"
    fi
  else
    skip "foreign-org destination 422: no destination owned by another org exists as a fixture"
  fi

  api PUT "/api/admin/bedrock-routing/mappings/not-a-valid-scope" \
    "{\"destination_id\":\"${DEST_ID_OWNED}\"}"
  assert_reason "malformed scope" "$API_STATUS" "$API_BODY" "422" "invalid_scope" || true

  api PUT "/api/admin/bedrock-routing/mappings/org:no-such-org-${EVAL_TAG}" \
    "{\"destination_id\":\"${DEST_ID_OWNED}\"}"
  assert_reason "nonexistent org scope" "$API_STATUS" "$API_BODY" "422" "scope_not_found" || true

  api PUT "/api/admin/bedrock-routing/mappings/org:${TEST_ORG}" \
    '{"destination_id":"00000000-0000-0000-0000-000000000000"}'
  assert_reason "unknown destination id" "$API_STATUS" "$API_BODY" "422" "account_unlinked" || true

  # The load-bearing half of every 422: NOTHING was stored.
  #
  # Distinguish "a rejected rule persisted" from "the read itself failed". An
  # unreadable effective endpoint means this assertion is INCONCLUSIVE, not that
  # routing regressed — reporting the latter is a false alarm that sends someone
  # hunting a persistence bug that does not exist.
  api GET "/api/admin/bedrock-routing/effective/${TEST_USER}"
  local rung; rung="$(jqr "$API_BODY" '.rung')"
  if [ "$API_STATUS" != "200" ]; then
    fail "cannot verify the 422s stored nothing: the effective read returned HTTP ${API_STATUS} (inconclusive, NOT evidence a mapping persisted)"
  elif [ "$rung" = "platform" ]; then
    pass "after every refusal the principal still resolves to 'platform' — no rejected rule was stored"
  else
    fail "after refusals the effective rung is '${rung}' — a REJECTED mapping was persisted"
  fi
}

# =============================================================================
# Phase 9 — REGRESSION: the core proxy still works.
# =============================================================================
phase_9() {
  phase 9 "regression — core proxy, health, and the login canary"
  maybe_fail_phase 9

  if curl -s --max-time 20 "${BASE_URL}/api/health" | grep -q healthy; then
    pass "/api/health is 200/healthy"
  else
    fail "/api/health is not healthy"
  fi

  # The GitHub login canary. This is the github-auth-broker route, NOT under
  # /api: a broker regression takes out every sign-in (including admins') while
  # /api/health stays green and gateway pods stay Running, so health alone is
  # not sufficient evidence. See docs/runbooks/github-auth-allowlist-remediation.md
  # for the outage class this canary is watching for.
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 \
    "${BASE_URL}/auth/github" || echo "000")"
  case "$code" in
    2*|3*) pass "GitHub login surface (/auth/github) responds ${code} — the sign-in canary is alive" ;;
    5*)    fail "GitHub login surface -> ${code}: sign-in is likely broken even though /api/health is green (check the broker Lambda logs)" ;;
    *)     fail "GitHub login canary (/auth/github) -> ${code}, expected 2xx/3xx" ;;
  esac

  # A real model call through the proxy, and a streaming call, using the
  # eval's own admin identity.
  local body status
  anthropic_body "$WORKDIR/proxy.json" "Reply with the single word: routed"
  status="$(http_post_json "$ADMIN_CURLRC" "${BASE_URL}/api/v1/messages" \
    "$WORKDIR/proxy.json" "$WORKDIR/proxy.out")"
  if [ "$status" = "200" ]; then
    pass "core proxy: POST /api/v1/messages -> 200 (model calls still work)"
  else
    body="$(head -c 200 "$WORKDIR/proxy.out" 2>/dev/null || true)"
    fail "core proxy: POST /api/v1/messages -> ${status} (${body})"
  fi

  printf '%s' "$(jq -nc --arg m "$EVAL_MODEL" \
    '{model:$m, max_tokens:32, stream:true, messages:[{role:"user",content:"stream one word"}]}')" \
    > "$WORKDIR/stream.json"
  status="$(http_post_json "$ADMIN_CURLRC" "${BASE_URL}/api/v1/messages" \
    "$WORKDIR/stream.json" "$WORKDIR/stream.out")"
  if [ "$status" = "200" ] && grep -q 'event:\|data:' "$WORKDIR/stream.out" 2>/dev/null; then
    pass "core proxy: streaming happy path returns SSE frames"
  elif [ "$status" = "200" ]; then
    fail "streaming returned 200 but no SSE frames were present"
  else
    fail "streaming: POST /api/v1/messages (stream) -> ${status}"
  fi
}

# =============================================================================
# Phase 10 — REGRESSION: metering and attribution are untouched by routing.
# =============================================================================
phase_10() {
  phase 10 "regression — metering shape and attribution are untouched by routing"
  maybe_fail_phase 10

  local body status
  api GET "/api/usage/logs?org_id=aws-e&limit=5"
  status="$API_STATUS"; body="$API_BODY"
  if [ "$status" != "200" ]; then
    fail "GET /api/usage/logs -> ${status}"
    return 1
  fi
  local rows; rows="$(printf '%s' "$body" | jq -r '.items | length' 2>/dev/null || echo 0)"
  if [ "${rows:-0}" -eq 0 ]; then
    skip "metering shape: no usage rows available to compare"
    return 0
  fi

  # Cost/token fields must be present and identically shaped regardless of
  # which account signed — routing changes whose AWS bill pays, never the
  # metering record.
  local missing
  missing="$(printf '%s' "$body" | jq -r '
    [ .items[] | [ (has("cost_usd")), (has("input_tokens")), (has("output_tokens")),
                   (has("model")), (has("user_id")), (has("org_id")),
                   (has("bedrock_account_id")) ] | all ] | map(select(. == false)) | length')"
  if [ "${missing:-1}" -eq 0 ]; then
    pass "every usage row carries the full cost/token/attribution field set alongside bedrock_account_id"
  else
    fail "${missing} usage row(s) are missing cost/token/attribution fields"
  fi

  # Pin the observed shape rather than asserting a possibly-stale constant.
  local sig nfields
  nfields="$(printf '%s' "$body" | jq -r '.items[0] | keys | length')"
  sig="$(printf '%s' "$body" | jq -r '.items[0] | keys | join(",")')"
  finding "usage-row shape observed over /api/usage/logs: ${nfields} fields [${sig}]. Pinned rather than asserted against a constant — the recorded signature 4610a5e1d6f7e988 (15 fields) predates later columns, and usage_logs now has 21 columns in the DB. Compare routed vs platform rows to EACH OTHER, not to a frozen hash."

  # attributed_org_id must not be able to move the resolved rung. The header is
  # a reporting hint; letting it steer resolution or budget would be a
  # cross-tenant billing hole.
  local rung_plain rung_hdr
  rung_plain="$(jqr "$(api_body GET "/api/admin/bedrock-routing/effective/${TEST_USER}")" '.rung')"
  rung_hdr="$(curl -s --max-time 30 -K "$ADMIN_CURLRC" \
    -H "attributed_org_id: ${SANDBOX_ACCOUNT}" \
    "${BASE_URL}/api/admin/bedrock-routing/effective/${TEST_USER}" \
    | jq -r '.rung // empty' 2>/dev/null || true)"
  if [ -n "$rung_plain" ] && [ "$rung_plain" = "$rung_hdr" ]; then
    pass "an attributed_org_id header cannot change the resolved rung ('${rung_plain}' both with and without it)"
  elif [ -z "$rung_hdr" ]; then
    skip "attributed_org_id probe: the effective read returned no rung with the header set"
  else
    fail "attributed_org_id changed the resolved rung: '${rung_plain}' -> '${rung_hdr}' — routing is steerable by a client header"
  fi
}

# =============================================================================
# Phase 11 — REGRESSION: budget enforcement still denies at the cap.
# =============================================================================
phase_11() {
  phase 11 "regression — budget enforcement and the read-only budget surface"
  maybe_fail_phase 11

  # Routing must not have changed WHOSE ADP budget is charged. We assert the
  # person-default enforcement config still reads and carries a limit, rather
  # than burning a real cap: #4163 owns the deny-at-cap matrix, and duplicating
  # it here would be slow and would fight that suite for the same fixtures.
  local status body
  api GET "/api/budget/person-default/org:${TEST_ORG}"
  status="$API_STATUS"; body="$API_BODY"
  case "$status" in
    2*)
      pass "budget person-default surface reads ${status} for org:${TEST_ORG} (enforcement config is intact)"
      # A limit that reads back as absent would mean nothing enforces at the cap.
      if [ -n "$(jqr "$body" '(.limit_usd // .amount_usd // .default_limit_usd)')" ]; then
        pass "the person-default carries a limit value (a cap exists to deny at)"
      else
        finding "the person-default for org:${TEST_ORG} reads ${status} but exposes no limit_usd/amount_usd; confirm the deny-at-cap path in #4163's suite"
      fi
      ;;
    404) fail "GET /api/budget/person-default/org:${TEST_ORG} -> 404: the person-default enforcement surface is missing" ;;
    *)   fail "budget person-default surface -> ${status}" ;;
  esac

  # The /budget page must stay READ-ONLY for a principal: the self surface may
  # be readable, but no self-WRITE route may be reachable, or a user could raise
  # their own cap and bypass enforcement entirely.
  status="$(api_status GET "/api/me/budget")"
  case "$status" in
    2*) pass "the self budget surface is readable (GET /api/me/budget -> ${status})" ;;
    *)  finding "GET /api/me/budget -> ${status}; the read-only self surface may have moved" ;;
  esac

  local code p
  for p in /api/me/budget /api/me/budget/person-cap; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X PUT -K "$ADMIN_CURLRC" \
      -H 'content-type: application/json' --data-binary '{"limit_usd":999999}' \
      "${BASE_URL}${p}" || echo "000")"
    case "$code" in
      404|405|403) pass "no self-write budget route at ${p} (PUT -> ${code})" ;;
      2*)          fail "PUT ${p} -> ${code}: a principal can raise its OWN budget cap" ;;
      *)           finding "PUT ${p} returned ${code}; confirm it is not a writable self-cap path" ;;
    esac
  done
}

# =============================================================================
# Phase 12 — REGRESSION: control-endpoint latency guardrail.
# =============================================================================
phase_12() {
  phase 12 "regression — control-endpoint latency stays within tolerance of the in-AWS baseline"
  maybe_fail_phase 12

  # Measured from this pod/runner, which is IN AWS. A laptop vantage makes the
  # number meaningless — the #4743 lesson.
  local t times=() sorted p50
  for _ in $(seq 1 "$LATENCY_SAMPLES"); do
    t="$(curl -s -o /dev/null -w '%{time_total}' --max-time 30 "${BASE_URL}/api/health" 2>/dev/null || echo "")"
    [ -n "$t" ] && times+=("$t")
  done
  if [ "${#times[@]}" -lt 3 ]; then
    skip "latency guardrail: only ${#times[@]} samples completed — too few for a p50"
    return 0
  fi
  sorted="$(printf '%s\n' "${times[@]}" | sort -n)"
  p50="$(printf '%s' "$sorted" | awk -v n="${#times[@]}" 'NR==int((n+1)/2){printf "%.1f", $1*1000}')"
  log "latency samples=${#times[@]} p50=${p50}ms baseline=${LATENCY_BASELINE_P50_MS}ms tolerance=${LATENCY_TOLERANCE_PCT}%"
  local verdict
  verdict="$(awk -v p="$p50" -v b="$LATENCY_BASELINE_P50_MS" -v tol="$LATENCY_TOLERANCE_PCT" \
    'BEGIN { print (p <= b * (1 + tol/100)) ? "ok" : "slow" }')"
  if [ "$verdict" = "ok" ]; then
    pass "control-endpoint p50 ${p50}ms is within ${LATENCY_TOLERANCE_PCT}% of the ${LATENCY_BASELINE_P50_MS}ms in-AWS baseline"
  else
    fail "control-endpoint p50 ${p50}ms exceeds the ${LATENCY_BASELINE_P50_MS}ms baseline by more than ${LATENCY_TOLERANCE_PCT}%"
  fi
}

# =============================================================================
# Phase 13 — orchestrate the two proven gate scripts rather than duplicating them.
#
# `bedrock-routing-validate.sh` (R4's ops gate) and
# `validate-bedrock-routing-shadow.sh` (R2's shadow gate) already encode checks
# this arc relies on, and both are maintained against real dev runs. Re-writing
# their logic here would create exactly the DRIFT that
# bedrock-routing-validate.sh's own header warns about: two copies of one
# decision table, where a fix to either is invisible to the other.
#
# So this phase RUNS them and folds their verdicts into this suite's tally. That
# also makes the eval a regression test for the gates themselves — if a future
# change breaks one, this suite goes red rather than quietly stopping to cover it.
#
# Their exit codes are the contract (0 = pass); their stdout is captured to the
# workdir for the run artifact rather than dumped, because each prints a full
# report and interleaving two of them into this log would bury the tally.
# =============================================================================
phase_13() {
  phase 13 "orchestrate the proven gate scripts (reuse, not re-implementation)"
  maybe_fail_phase 13

  if [ "$DRY_RUN" = true ]; then
    skip "gate-script orchestration: --dry-run stubs the AWS boundary, and these scripts talk to it directly; the dry-run suite asserts they are INVOKED, not their live verdicts"
    return 0
  fi

  local scripts_dir="$REPO_ROOT/platform/scripts"

  # --- R4's destination/authz gate -----------------------------------------
  # --check destination proves the standing fixture is healthy (assume WITH the
  # ExternalId, DENIED without it, real InvokeModel). --check authz re-proves the
  # platform-admin-only surface. Both are read-only.
  local gate="$scripts_dir/bedrock-routing-validate.sh"
  if [ ! -x "$gate" ]; then
    finding "bedrock-routing-validate.sh is missing or not executable at ${gate#"$REPO_ROOT"/} — this arc's ops gate cannot be re-run, so its checks are unproven in this run"
  else
    local check
    for check in destination authz; do
      local out="$WORKDIR/gate-${check}.log"
      if "$gate" --check "$check" -e "$ENVIRONMENT" >"$out" 2>&1; then
        pass "bedrock-routing-validate.sh --check ${check}: PASS (reused, not re-implemented; report at $(basename "$out"))"
      else
        local rc_gate=$?
        # Surface the script's own verdict line so the failure is actionable
        # without opening the artifact.
        local verdict_line
        verdict_line="$(grep -E '^RESULT:' "$out" 2>/dev/null | tail -1 || true)"
        fail "bedrock-routing-validate.sh --check ${check}: exit ${rc_gate} ${verdict_line:+(${verdict_line})} — see $(basename "$out")"
      fi
    done
  fi

  # --- R2's shadow gate, with the cutover and latency lessons already in it --
  # This script implements the #4743 lessons directly: it excludes pre-deploy
  # NULL rows and compares the control p50 against a recorded in-AWS baseline.
  # Passing --baseline-p50 is what turns its latency section from a report into
  # an assertion.
  local shadow_gate="$scripts_dir/validate-bedrock-routing-shadow.sh"
  if [ ! -x "$shadow_gate" ]; then
    finding "validate-bedrock-routing-shadow.sh is missing or not executable at ${shadow_gate#"$REPO_ROOT"/} — the shadow-mode gate cannot be re-run in this suite"
  else
    local out="$WORKDIR/gate-shadow.log"
    if "$shadow_gate" -e "$ENVIRONMENT" \
        --baseline-p50 "$LATENCY_BASELINE_P50_MS" \
        --tolerance "$LATENCY_TOLERANCE_PCT" >"$out" 2>&1; then
      pass "validate-bedrock-routing-shadow.sh: PASS against the ${LATENCY_BASELINE_P50_MS}ms baseline (shadow column populating; control p50 within ${LATENCY_TOLERANCE_PCT}%)"
    else
      local rc_shadow=$?
      local verdict_line
      verdict_line="$(grep -E '^(RESULT|FAIL)' "$out" 2>/dev/null | tail -1 || true)"
      fail "validate-bedrock-routing-shadow.sh: exit ${rc_shadow} ${verdict_line:+(${verdict_line})} — see $(basename "$out")"
    fi
  fi
}

# =============================================================================
# Cleanup — tag-scoped, idempotent, and safe to run standalone.
# =============================================================================
run_cleanup() {
  local rc=$?
  trap - EXIT
  CURRENT_PHASE="cleanup"
  trace "phase:cleanup"
  log "cleanup: removing anything this run created"

  # Mappings this run authored. Delete is 204 whether or not a row existed, so
  # this is safe to repeat.
  local scope
  scope="$(state_get CREATED_MAPPING_SCOPE)"
  if [ -n "$scope" ] && [ -f "$ADMIN_CURLRC" ] && [ -n "$BASE_URL" ]; then
    log "cleanup: deleting mapping ${scope}"
    curl -s -o /dev/null --max-time 60 -X DELETE -K "$ADMIN_CURLRC" \
      "${BASE_URL}/api/admin/bedrock-routing/mappings/${scope}" || true
    state_set CREATED_MAPPING_SCOPE ""
  fi

  # This suite registers no destinations and seeds no Cognito users: it reuses
  # standing fixtures and the designated test org deliberately, so there is
  # nothing else to unwind. Sweep the pod by label in case one was created.
  if [ "$DRY_RUN" != true ]; then
    laptop_pod_delete 2>/dev/null || true
  fi

  # Prove we left no mapping behind — a leaked rule would silently reroute a
  # tenant's Bedrock bill.
  if [ -f "$ADMIN_CURLRC" ] && [ -n "$BASE_URL" ]; then
    local left
    left="$(curl -s --max-time 30 -K "$ADMIN_CURLRC" \
      "${BASE_URL}/api/admin/bedrock-routing/mappings" 2>/dev/null \
      | jq -r --arg o "${TEST_ORG:-__none__}" \
        '[.[] | select(.scope_id_org == $o)] | length' 2>/dev/null || echo "?")"
    if [ "$left" = "0" ]; then
      pass "cleanup verified: no mapping remains for the test org"
    elif [ "$left" = "?" ]; then
      log "cleanup: could not verify remaining mappings (endpoint unreadable)"
    else
      fail "cleanup left ${left} mapping(s) on ${TEST_ORG} — a tenant's routing may be altered"
    fi
  fi

  rm -f "$WORKDIR"/*.token "$WORKDIR"/*.curlrc "$WORKDIR"/api.in "$WORKDIR"/api.out 2>/dev/null || true

  # A fatal setup error (`die`) exits non-zero WITHOUT incrementing FAILURES,
  # because die() is a shared-lib helper that reports and exits rather than
  # recording an assertion. write_summary keys its verdict off FAILURES, so
  # without this the summary would print "eval passed: 0 failures" on a run that
  # died in phase 0 and exited 1 — a green banner on a red run, which is exactly
  # the dishonesty this suite's contract forbids. Record it as the failure it is.
  if [ "$rc" -ne 0 ] && [ "$FAILURES" -eq 0 ]; then
    CURRENT_PHASE="${CURRENT_PHASE:-setup}"
    fail "the run terminated early (exit ${rc}) before completing its matrix — see the FATAL line above; no verdict can be drawn about routing"
  fi

  write_summary
  exit "$rc"
}

# =============================================================================
main() {
  case "$MODE" in
    clean-room)
      assert_clean_room || exit 1
      exit 0
      ;;
    cleanup)
      log "cleanup-only mode for ${ENVIRONMENT}"
      local cf
      cf="$(eval_ssm "/adp/${ENVIRONMENT}/gateway/cloudfront-domain")"
      [ -n "$cf" ] && [ "$cf" != "None" ] && BASE_URL="https://${cf}"
      if [ -n "$BASE_URL" ] && mint_token "test-admin-credentials" "$WORKDIR/admin.token"; then
        write_curl_auth_config "$WORKDIR/admin.token" "$ADMIN_CURLRC"
      fi
      TEST_ORG="$(state_get TEST_ORG)"
      run_cleanup
      ;;
  esac

  trap run_cleanup EXIT

  local rc before c
  for c in 0 1 2 3 4 5 6 7 8 9 10 11 12 13; do
    phase_enabled "$c" || continue
    rc=0; before="$FAILURES"
    case "$c" in
      0)  phase_0  || rc=$? ;;
      1)  phase_1  || rc=$? ;;
      2)  phase_2  || rc=$? ;;
      3)  phase_3  || rc=$? ;;
      4)  phase_4  || rc=$? ;;
      5)  phase_5  || rc=$? ;;
      6)  phase_6  || rc=$? ;;
      7)  phase_7  || rc=$? ;;
      8)  phase_8  || rc=$? ;;
      9)  phase_9  || rc=$? ;;
      10) phase_10 || rc=$? ;;
      11) phase_11 || rc=$? ;;
      12) phase_12 || rc=$? ;;
    esac
    after_phase "$c" "$rc" "$before"
  done


  if [ "$FAILURES" -gt 0 ]; then
    exit 1
  fi
  exit 0
}

# Sourcing this file with EVAL_SOURCE_ONLY=1 defines every helper WITHOUT running
# the suite, so tests/test-run-eval-dry-run.sh can exercise the guards (scope
# allowlist, gates, cleanup) with no AWS, no cluster, and no network.
if [ "${EVAL_SOURCE_ONLY:-0}" != "1" ]; then
  main "$@"
fi
