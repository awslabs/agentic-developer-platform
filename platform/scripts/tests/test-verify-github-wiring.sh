#!/bin/bash
# =============================================================================
# test-verify-github-wiring.sh — Unit tests for verify-github-wiring.sh predicates
# =============================================================================
# Issue #4032. Tests the pure predicate functions that decide PASS vs FAIL.
#
# Each case below corresponds to an input that the ORIGINAL Phase 9 check either
# false-passed or false-failed. If these predicates regress, the Phase 9 gate
# goes back to being green on a broken deployment — which is the whole bug.
#
# No AWS calls: the predicates are sourced out of the script in isolation.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$SCRIPT_DIR/verify-github-wiring.sh"
FAILURES=0
PASSES=0

# Source only the predicate functions, so the script's arg parsing and AWS calls
# never execute (same approach as test-force-delete-secrets-protect.sh).
for fn in is_valid_app_id is_valid_private_key tenant_secret_is_wellformed count_agent_replies; do
  eval "$(sed -n "/^${fn}()/,/^}/p" "$TARGET")"
done

pass() { echo "  PASS: $*"; PASSES=$((PASSES + 1)); }
bad()  { echo "  FAIL: $*"; FAILURES=$((FAILURES + 1)); }

assert_true() {  # <description> <command...>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then pass "$desc"; else bad "$desc (expected true, got false)"; fi
}

assert_false() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then bad "$desc (expected false, got true)"; else pass "$desc"; fi
}

assert_eq() {  # <description> <expected> <actual>
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    pass "$desc (= $actual)"
  else
    bad "$desc (expected $expected, got $actual)"
  fi
}

# Feed stdin to a predicate IN THIS SHELL. Do not reach for `bash -c` here: the
# predicates are eval-sourced into this shell only, so a subshell would hit
# "command not found" (exit 127) and every negative assertion would pass
# vacuously — the tests would look green while asserting nothing.
secret_wellformed() { printf '%s' "$1" | tenant_secret_is_wellformed; }

PEM='-----BEGIN RSA PRIVATE KEY-----
MIIBOgIBAAJBAKj34GkxFhD9paFm
-----END RSA PRIVATE KEY-----'

echo "=== is_valid_app_id ==="
assert_true  "numeric App ID accepted"          is_valid_app_id "123456"
assert_false "empty App ID rejected"            is_valid_app_id ""
assert_false "AWS 'None' sentinel rejected"     is_valid_app_id "None"
assert_false "placeholder text rejected"        is_valid_app_id "REPLACE_ME"
assert_false "non-numeric rejected"             is_valid_app_id "app-12345"
assert_false "whitespace-only rejected"         is_valid_app_id "   "
echo ""

echo "=== is_valid_private_key ==="
assert_true  "PEM-armoured key accepted"        is_valid_private_key "$PEM"
assert_false "empty key rejected"               is_valid_private_key ""
assert_false "placeholder key rejected"         is_valid_private_key "placeholder"
assert_false "'None' sentinel rejected"         is_valid_private_key "None"
echo ""

echo "=== tenant_secret_is_wellformed ==="
# The healthy case. The original doc command failed HERE, on good input, because
# it omitted --output text and jq got a JSON-quoted string instead of an object.
assert_true "healthy secret accepted" \
  secret_wellformed "$(jq -nc --arg k "$PEM" '{app_id:"123456",private_key:$k}')"

# The original `jq -e '.app_id and .private_key'` exits 0 on these. A truncated
# or placeholder write is exactly the bug class this gate must catch.
assert_false "empty-string values rejected" \
  secret_wellformed '{"app_id":"","private_key":""}'
assert_false "empty app_id with good key rejected" \
  secret_wellformed "$(jq -nc --arg k "$PEM" '{app_id:"",private_key:$k}')"
assert_false "non-PEM private_key rejected" \
  secret_wellformed '{"app_id":"123456","private_key":"placeholder"}'
assert_false "missing private_key rejected" \
  secret_wellformed '{"app_id":"123456"}'
assert_false "missing app_id rejected" \
  secret_wellformed "$(jq -nc --arg k "$PEM" '{private_key:$k}')"
assert_false "empty input rejected" \
  secret_wellformed ''
assert_false "non-JSON input rejected" \
  secret_wellformed 'not json'
# Guards the --output text mistake itself: a JSON-quoted string must not pass.
assert_false "JSON-quoted string (missing --output text) rejected" \
  secret_wellformed "$(jq -nc --arg k "$PEM" '{app_id:"1",private_key:$k}' | jq -Rc .)"
# Integer app_id: both writers emit strings today, but tostring must tolerate it.
assert_true "integer app_id tolerated" \
  secret_wellformed "$(jq -nc --arg k "$PEM" '{app_id:123456,private_key:$k}')"
echo ""

echo "=== count_agent_replies ==="
STARTED_ONLY='[{"user":{"type":"Bot"},"body":"<!-- adp-run:abc -->\n🤖 **Agent `developer` started** working on this issue."}]'
HUMAN_ONLY='[{"user":{"type":"User"},"body":"@agent-developer say hello"}]'
REAL_REPLY='[{"user":{"type":"Bot"},"body":"## Task Complete\nHello!"}]'

assert_eq "no comments → 0"     0 "$(printf '%s' '[]' | count_agent_replies)"
assert_eq "human trigger only → 0" 0 "$(printf '%s' "$HUMAN_ONLY" | count_agent_replies)"

# THE critical case. The worker posts its "started" comment at bootstrap step 9;
# counting it as success false-passes every failure downstream of step 9 — e.g.
# the Phase 8 Bedrock gotcha, where the started comment posts and nothing else
# ever does. Phase 8 sits immediately before Phase 9, so this is not academic.
assert_eq "started-comment only → 0 (must not count as a reply)" \
  0 "$(printf '%s' "$STARTED_ONLY" | count_agent_replies)"

assert_eq "genuine bot reply → 1" 1 "$(printf '%s' "$REAL_REPLY" | count_agent_replies)"

# The worker's failure paths post a Bot comment marked `<!-- adp-failed:`. It has
# no `adp-run:` marker, so the started-comment exclusion does not catch it: left
# uncounted-for, a FAILED run reads as a successful reply and step 5 reports
# healthy dispatch. Note the payload below is the real zero-token diagnostic —
# the Bedrock case the started-comment rule above is itself guarding against, so
# both exclusions are needed to close that one hole.
FAILED_ONLY='[{"user":{"type":"Bot"},"body":"<!-- adp-failed:abc -->\nAgent `developer` failed: the model call never succeeded (0 tokens burned)."}]'
assert_eq "failure-comment only → 0 (a failed run is not a reply)" \
  0 "$(printf '%s' "$FAILED_ONLY" | count_agent_replies)"

# The full shape of a broken run: human trigger, worker starts, worker fails.
# Nothing here is a reply, so any count > 0 is a false pass.
STARTED_THEN_FAILED='[{"user":{"type":"User"},"body":"@agent-developer say hello"},
        {"user":{"type":"Bot"},"body":"<!-- adp-run:abc -->\nstarted"},
        {"user":{"type":"Bot"},"body":"<!-- adp-failed:abc -->\nAgent `developer` failed with exit code 1."}]'
assert_eq "human + started + failed → 0 (whole broken-run sequence)" \
  0 "$(printf '%s' "$STARTED_THEN_FAILED" | count_agent_replies)"

MIXED='[{"user":{"type":"User"},"body":"@agent-developer say hello"},
        {"user":{"type":"Bot"},"body":"<!-- adp-run:abc -->\nstarted"},
        {"user":{"type":"Bot"},"body":"## Task Complete"}]'
assert_eq "human + started + real reply → 1" 1 "$(printf '%s' "$MIXED" | count_agent_replies)"

# A real reply must still count when a failure comment from an EARLIER run is
# present — otherwise a retry on a previously-failed issue could never pass.
FAILED_THEN_SUCCEEDED='[{"user":{"type":"Bot"},"body":"<!-- adp-failed:old -->\nAgent `developer` failed with exit code 1."},
        {"user":{"type":"Bot"},"body":"<!-- adp-run:new -->\nstarted"},
        {"user":{"type":"Bot"},"body":"## Task Complete\nHello!"}]'
assert_eq "prior failure + fresh real reply → 1 (retry can still pass)" \
  1 "$(printf '%s' "$FAILED_THEN_SUCCEEDED" | count_agent_replies)"

NULL_BODY='[{"user":{"type":"Bot"},"body":null}]'
assert_eq "null body does not crash → 1" 1 "$(printf '%s' "$NULL_BODY" | count_agent_replies)"

assert_eq "malformed JSON → 0 (no crash)" 0 "$(printf 'not json' | count_agent_replies)"
echo ""

echo "=== Results: ${PASSES} passed, ${FAILURES} failed ==="
[ "$FAILURES" -eq 0 ] || exit 1
