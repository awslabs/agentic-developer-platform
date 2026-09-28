#!/usr/bin/env bash
# Wave 2 step 90 — bounded, ownership-verified cleanup and absence proof.
#
# Issue #3968, W2-10. Run this ALWAYS, including after a failed run: the failure
# path is exactly when a fixture is most likely to be left with a live control
# listener, which is the DP-INV-1 state this whole step exists to close.
#
# WHAT CHANGED AND WHY
# --------------------
# The previous revision of this script reported a clean teardown it had not
# verified, four different ways:
#
#   * ANY `get-queue-url` error counted as "queue already absent" -- so an
#     AccessDenied, an expired token or a throttle left a live fixture queue
#     recorded as cleaned up.
#   * Kubernetes absence was inferred from empty stdout, which an unreachable API
#     server also produces.
#   * `delete-queue` returning 0 was recorded as absence, though SQS documents up
#     to 60 seconds before the queue is really gone.
#   * `q.get("run_bound", True)` DEFAULTED authority to delete, and the only
#     thing protecting the pre-existing probe queue was a hardcoded date string.
#
# The logic now lives in lib/cleanup.py, where every one of those paths has a
# behavioural test (tests/test_cleanup.py). Absence is reported only when a
# specific not-found signal was observed; anything else is UNKNOWN and fails the
# cleanup. Ownership is proven from the ledger's recorded metadata.uid (k8s) or
# owner-nonce tag (SQS), re-verified against the live resource immediately before
# the delete, so a same-name replacement is SKIPPED rather than destroyed.
#
# Scope discipline, by construction rather than by care:
#   * Synthetic rows are deleted by BOTH keys (event_id AND arrived_at).
#   * Only resources named in the ledger are touched: no scan, query, prefix or
#     wildcard, so an item not listed is unreachable from here.
#   * NEVER purges a queue. NEVER deletes an object this run did not create.
#
# Exit codes: 0 verified clean | 1 refused before acting | 5 cleanup unverified.
#
# Usage:
#   ./90-cleanup-ledger.sh <ledger.json> <evidence-dir> [--dry-run]
#
# Credentials: vault, AWS_PROFILE or ambient env (see lib/session.sh). All three
# are subject to the same target-account assertion.
#
# Writes <evidence-dir>/cleanup-ledger-result.json. Preserve raw evidence FIRST:
# run this after the harness has written result.json.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/session.sh
. "$HERE/lib/session.sh"

readonly TABLE="${W2_EVENTS_TABLE:-adp-dev-webhook-events}"

LEDGER="${1:?usage: 90-cleanup-ledger.sh <ledger.json> <evidence-dir> [--dry-run]}"
EVIDENCE_DIR="${2:?usage: 90-cleanup-ledger.sh <ledger.json> <evidence-dir> [--dry-run]}"
DRY_RUN_FLAG="${3:-}"

[ -f "$LEDGER" ] || w2_fail "ledger $LEDGER not found"
case "$DRY_RUN_FLAG" in
  ""|--dry-run) ;;
  *) w2_fail "third argument must be --dry-run or absent, got '$DRY_RUN_FLAG'" ;;
esac
mkdir -p "$EVIDENCE_DIR"

# The run id is the ledger's own; the account is asserted from the live session.
# Both are passed to the cleanup so it can REFUSE a foreign ledger rather than
# tearing down resources this run never created.
RUN_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("run_id",""))' "$LEDGER")"
[ -n "$RUN_ID" ] || w2_fail "ledger $LEDGER has no run_id; refusing to act on an unidentified ledger"

w2_report_mode
w2_require_account   # sets W2_ACCOUNT / W2_ARN; exits here, not in a subshell
ACCOUNT="$W2_ACCOUNT"
w2_ok "account $ACCOUNT"
w2_note "identity: $W2_ARN"
w2_note "ledger run: $RUN_ID"

# Stamped by the caller, not inside the cleanup, so the record is reproducible.
GENERATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# kubeconfig is needed for the k8s absence probes; w2_kubeconfig fails loudly
# rather than continuing with a broken context that would read as "absent".
KUBECONFIG_PATH="$(w2_kubeconfig)"
export KUBECONFIG="$KUBECONFIG_PATH"

# The deletions must run under the SAME credential the account assertion above
# verified. The previous revision asserted identity through w2_aws (which in vault
# mode assumes adp-embark1) and then invoked cleanup.py DIRECTLY -- so its runner
# called ambient `aws`/`kubectl`. On an ADP worker, where several identities are
# reachable at once, the actual deletes could run as a different account while
# --account-id recorded the asserted one. The record would name embark1 and the
# deletes would happen somewhere else.
#
# w2_session runs the whole cleanup inside the resolved credential, so the
# identity that was asserted is the identity that mutates. Exported rather than
# interpolated: the body is `bash -c` and a path containing a quote would
# otherwise break it (see lib/session.sh).
export W2_CLEANUP_LEDGER="$LEDGER"
export W2_CLEANUP_EVIDENCE_DIR="$EVIDENCE_DIR"
export W2_CLEANUP_TABLE="$TABLE"
export W2_CLEANUP_RUN_ID="$RUN_ID"
export W2_CLEANUP_ACCOUNT="$ACCOUNT"
export W2_CLEANUP_GENERATED_AT="$GENERATED_AT"
export W2_CLEANUP_DRY_RUN="${DRY_RUN_FLAG:-}"
export W2_CLEANUP_SCRIPT="$HERE/lib/cleanup.py"
export W2_CLEANUP_EXPECT_ACCOUNT="$W2_EXPECT_ACCOUNT"

set +e
w2_session '
  set -u
  # Re-assert the identity IMMEDIATELY BEFORE mutating, from inside the session
  # that will do the deleting. The outer assertion proved the outer shell'"'"'s
  # credential; this proves the one the deletes actually use. A credential that
  # changed in between, or a session that resolves differently, stops here.
  live="$(aws sts get-caller-identity --query Account --output text 2>&1)" || {
    printf "FAIL: could not read caller identity inside the cleanup session.\n" >&2
    printf "%s\n" "$live" >&2
    exit 1
  }
  if [ "$live" != "$W2_CLEANUP_EXPECT_ACCOUNT" ]; then
    printf "FAIL: the cleanup session resolves to account %s, expected %s.\n" \
      "$live" "$W2_CLEANUP_EXPECT_ACCOUNT" >&2
    printf "  The verified session and the deleting session are NOT the same identity.\n" >&2
    printf "  Refusing to delete: the account recorded in the evidence would be a lie.\n" >&2
    exit 1
  fi
  if [ "$live" != "$W2_CLEANUP_ACCOUNT" ]; then
    printf "FAIL: cleanup session account %s differs from the asserted %s.\n" \
      "$live" "$W2_CLEANUP_ACCOUNT" >&2
    exit 1
  fi
  exec python3 "$W2_CLEANUP_SCRIPT" \
    --ledger "$W2_CLEANUP_LEDGER" \
    --evidence-dir "$W2_CLEANUP_EVIDENCE_DIR" \
    --table "$W2_CLEANUP_TABLE" \
    --run-id "$W2_CLEANUP_RUN_ID" \
    --account-id "$W2_CLEANUP_ACCOUNT" \
    --generated-at "$W2_CLEANUP_GENERATED_AT" \
    ${W2_CLEANUP_DRY_RUN:+--dry-run}
'
rc=$?
set -e

# The success message is DERIVED FROM THE RECORD, not from the exit code. The
# previous revision printed "every ledger item confirmed absent" whenever rc was
# 0 -- including on --dry-run, where cleanup_ok is null and nothing was deleted or
# even checked. Reading cleanup_ok back means the message cannot contradict the
# artifact it is describing.
RESULT_JSON="$EVIDENCE_DIR/cleanup-ledger-result.json"
CLEANUP_OK="absent-file"
if [ -f "$RESULT_JSON" ]; then
  CLEANUP_OK="$(python3 -c '
import json,sys
try:
    d=json.load(open(sys.argv[1]))
except Exception:
    print("unreadable"); raise SystemExit(0)
v=d.get("cleanup_ok")
print("null" if v is None else ("true" if v is True else "false"))
' "$RESULT_JSON")"
fi

if [ -n "$DRY_RUN_FLAG" ]; then
  if [ "$rc" -eq 0 ]; then
    w2_ok "dry run complete: this is a PLAN, not a cleanup proof"
    w2_note "cleanup_ok is $CLEANUP_OK -- nothing was deleted and no absence was verified."
    w2_note "Re-run without --dry-run to actually tear the fixture down."
  else
    printf 'FAIL: dry run refused before planning (rc=%s)\n' "$rc" >&2
  fi
  exit "$rc"
fi

case "$rc" in
  0)
    if [ "$CLEANUP_OK" = "true" ]; then
      w2_ok "cleanup verified: every ledger item confirmed absent"
    else
      # Belt and braces: never announce a verified cleanup over a record that
      # does not say so.
      printf 'FAIL: cleanup exited 0 but cleanup_ok is %s in %s.\n' \
        "$CLEANUP_OK" "$RESULT_JSON" >&2
      printf '  Refusing to report a verified teardown the record does not support.\n' >&2
      rc=5
    fi
    ;;
  5) printf 'FAIL: cleanup did not verify; see %s\n' "$RESULT_JSON" >&2 ;;
  *) printf 'FAIL: cleanup refused before acting (rc=%s)\n' "$rc" >&2 ;;
esac
exit "$rc"
