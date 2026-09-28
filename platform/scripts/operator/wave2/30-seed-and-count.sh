#!/usr/bin/env bash
# Wave 2 fixture step 3 — seed synthetic runs through the REAL writer and count
# them through the REAL reader (W2-06 row, W2-07 counters).
#
# Issue #3968 / epic #3959.
#
# What makes this evidence rather than a restatement of itself:
#
#   * Rows are created with `common.webhook_events.WebhookEventLogger.log_event`
#     and transitioned with `lib.invocation_status.update_status` -- the exact two
#     functions production uses. A raw put_item would let this script write a
#     status the real writer refuses, and W2-08's whole subject is that refusal.
#   * Counts are read from the gateway's `/admin/agent-run-stats` response, i.e.
#     through StatsService, not recomputed here. A locally recomputed count would
#     agree with itself no matter what the deployed reader does, which is the one
#     thing W2-07 exists to detect.
#   * Every count is a DELTA between two snapshots of a DEDICATED synthetic
#     tenant. The table is shared and carries ~391k rows; an absolute assertion
#     against live tenant totals would be both false and flaky (§7).
#
# Three isolated synthetic tenants, because the three claims need different
# populations and mixing them would make each assertion untrue:
#
#   delta tenant  -- starts empty; receives ONLY aborted rows. Gives
#                    today_before/today_after, daily_deltas, persona_deltas.
#   four tenant   -- exactly one complete + one failed + one in_progress + one
#                    aborted, so `total == completed+failed+active+aborted` is a
#                    true statement about it. It does not hold in general.
#   mixed tenant  -- the same four PLUS blocked/skipped/budget_stopped, which
#                    count toward `total` and no bucket. Measured before and
#                    after the aborted row to show nothing was reclassified.
#
# Usage:
#   ./30-seed-and-count.sh --run-id w2-... --ledger <ledger.json> \
#       --evidence-dir <dir> --gateway-url <url> --admin-token-env VAR \
#       [--owner-user-id <id> --owner-tenant-id <id>] [--check-only]
#
#   --admin-token-env  NAME of the env var holding the admin bearer token. The
#                      value is read from the environment and never logged; the
#                      name is what appears in evidence.
#   --owner-user-id /  seed the W2-06 read-back row as a real owner identity.
#   --owner-tenant-id  OMITTED BY DEFAULT: this is the only write that lands in a
#                      non-synthetic tenant, so it is opt-in, and it is recorded
#                      in the ledger like every other row.
#   --check-only       validate imports, credentials, token, reader reachability
#                      and tenant emptiness. Writes NOTHING.

set -euo pipefail

readonly EXPECT_ACCOUNT="879318057152"
readonly CRED_LABEL="adp-embark1"
readonly TABLE="adp-dev-webhook-events"
readonly REGION="us-east-1"
readonly FIXTURE_PERSONA="w2-fixture-persona"

RUN_ID=""; LEDGER=""; EVIDENCE_DIR=""; GATEWAY_URL=""; ADMIN_TOKEN_ENV=""
OWNER_USER_ID=""; OWNER_TENANT_ID=""; CHECK_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --run-id)          RUN_ID="${2:?}"; shift 2 ;;
    --ledger)          LEDGER="${2:?}"; shift 2 ;;
    --evidence-dir)    EVIDENCE_DIR="${2:?}"; shift 2 ;;
    --gateway-url)     GATEWAY_URL="${2:?}"; shift 2 ;;
    --admin-token-env) ADMIN_TOKEN_ENV="${2:?}"; shift 2 ;;
    --owner-user-id)   OWNER_USER_ID="${2:?}"; shift 2 ;;
    --owner-tenant-id) OWNER_TENANT_ID="${2:?}"; shift 2 ;;
    --check-only)      CHECK_ONLY=1; shift ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
ok()   { printf 'ok   %s\n' "$*"; }
note() { printf '     %s\n' "$*"; }

[ -n "$RUN_ID" ]          || fail "--run-id is required"
[ -n "$LEDGER" ]          || fail "--ledger is required; every seeded row is recorded before it is written"
[ -n "$EVIDENCE_DIR" ]    || fail "--evidence-dir is required"
[ -n "$GATEWAY_URL" ]     || fail "--gateway-url is required (the fixture gateway from 10-create-fixture.sh)"
[ -n "$ADMIN_TOKEN_ENV" ] || fail "--admin-token-env is required (the NAME of the env var, not the token)"
case "$RUN_ID" in w2-*) : ;; *) fail "--run-id must start with 'w2-'" ;; esac

# The token is resolved by NAME so no credential is ever an argument (arguments
# are visible in ps output and in this script's own echoed command line).
ADMIN_TOKEN="$(printenv "$ADMIN_TOKEN_ENV" || true)"
[ -n "$ADMIN_TOKEN" ] || fail "\$$ADMIN_TOKEN_ENV is unset or empty. Root supplies the admin bearer
       token at execution time; this script must not contain one."
export ADP_W2_ADMIN_TOKEN="$ADMIN_TOKEN"
unset ADMIN_TOKEN

if [ -n "$OWNER_USER_ID" ] || [ -n "$OWNER_TENANT_ID" ]; then
  [ -n "$OWNER_USER_ID" ] && [ -n "$OWNER_TENANT_ID" ] \
    || fail "--owner-user-id and --owner-tenant-id must be given together: a row with one
       and not the other is not readable by the owner identity W2-06 uses"
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib/session.sh"
w2_require_account
run_seed() {
  if [ "$(w2_cred_mode)" = vault ]; then
    adp-cred assume --service aws --label "$W2_CRED_LABEL" \
      --purpose "issue-3968 wave2 synthetic seed + counter snapshots" --exec "$@"
  else
    "$@"
  fi
}
REPO_ROOT="$(cd "$HERE/../../../.." && pwd)"
LAMBDA_DIR="$REPO_ROOT/modules/agent-factory/webhook-ingress/lambda"
WORKER_DIR="$REPO_ROOT/modules/agent-factory/agent-worker-image"
GW_DIR="$REPO_ROOT/modules/gateway"
ART="$EVIDENCE_DIR/artifacts"
mkdir -p "$ART"

[ -f "$LAMBDA_DIR/common/webhook_events.py" ] || fail "cannot find the real row writer at $LAMBDA_DIR"
[ -f "$WORKER_DIR/lib/invocation_status.py" ] || fail "cannot find the real status writer at $WORKER_DIR"
[ -f "$LEDGER" ] || fail "ledger $LEDGER not found -- run 10-create-fixture.sh first so cleanup is already armed"

# The reader's cache TTL, read FROM the deployed reader's source. Hardcoding 60
# here would silently stop working the day someone tunes it, and the symptom
# would be every delta reading as zero -- which looks exactly like broken
# counters and would send someone to debug code that is fine.
CACHE_TTL="$(python3 - "$GW_DIR" <<'PY'
import re, sys
from pathlib import Path
src = (Path(sys.argv[1]) / "src/activity/stats_service.py").read_text()
m = re.search(r"^_CACHE_TTL_SECONDS\s*=\s*(\d+)", src, re.M)
print(m.group(1) if m else "")
PY
)"
[ -n "$CACHE_TTL" ] || fail "could not read _CACHE_TTL_SECONDS from stats_service.py. Refusing to guess:
       a wrong wait makes the 'after' snapshot a cached copy of 'before', and every
       delta would read 0 -- indistinguishable from counters that do not work."
ok "reader cache TTL is ${CACHE_TTL}s (read from stats_service.py); snapshots will be spaced past it"

printf '\n== seeding plan ==\n'
note "delta tenant : w2-delta-${RUN_ID#w2-}   (aborted rows only)"
note "four tenant  : w2-four-${RUN_ID#w2-}    (exactly four outcomes)"
note "mixed tenant : w2-mixed-${RUN_ID#w2-}   (four + blocked/skipped/budget_stopped)"
if [ -n "$OWNER_TENANT_ID" ]; then
  note "W2-06 row    : ONE aborted row in the real tenant $OWNER_TENANT_ID (opt-in, ledgered)"
else
  note "W2-06 row    : NOT seeded (no --owner-tenant-id). W2-06 needs aborted_run_id;"
  note "               without it that check stays not_run rather than passing on a"
  note "               row the owner identity cannot actually read."
fi

if [ "$CHECK_ONLY" = 1 ]; then
  printf '\n== CHECK-ONLY: validating without writing anything ==\n'
fi

# `set -e` suspended around the seeder: a non-zero exit here can mean rows were
# already written, and the operator MUST see the cleanup command below. Dying on
# the exit status would drop that message and leave ledgered rows behind with no
# on-screen instruction to remove them.
set +e
run_seed env \
    W2_RUN_ID="$RUN_ID" \
    W2_LEDGER="$LEDGER" \
    W2_ART="$ART" \
    W2_TABLE="$TABLE" \
    W2_REGION="$REGION" \
    W2_GATEWAY_URL="$GATEWAY_URL" \
    W2_TOKEN_ENV="$ADMIN_TOKEN_ENV" \
    W2_PERSONA="$FIXTURE_PERSONA" \
    W2_OWNER_USER_ID="$OWNER_USER_ID" \
    W2_OWNER_TENANT_ID="$OWNER_TENANT_ID" \
    W2_CHECK_ONLY="$CHECK_ONLY" \
    W2_CACHE_TTL="$CACHE_TTL" \
    W2_EXPECT_ACCOUNT="$EXPECT_ACCOUNT" \
    W2_LAMBDA_DIR="$LAMBDA_DIR" \
    W2_WORKER_DIR="$WORKER_DIR" \
    PYTHONPATH="$LAMBDA_DIR:$WORKER_DIR" \
    python3 "$HERE/31-seed-and-count.py"
rc=$?
set -e

printf '\n'
if [ "$rc" -eq 0 ]; then
  if [ "$CHECK_ONLY" = 1 ]; then
    ok "CHECK-ONLY complete: nothing was written"
  else
    ok "aborted_counters.json written to $ART"
    note "Every seeded row is in $LEDGER with BOTH event_id and arrived_at."
    note "ALWAYS finish with 90-cleanup-ledger.sh, including on failure."
  fi
else
  note "seed/count reported rc=$rc. Any row already created is in the ledger:"
  note "  ./90-cleanup-ledger.sh $LEDGER $EVIDENCE_DIR"
fi
exit "$rc"
