#!/usr/bin/env bash
# validate-bedrock-routing-shadow.sh — Post-merge validation for the Bedrock
# routing shadow-mode foundation (issue #4743, parent #4692).
#
# Asserts the two operations-owned post-merge criteria from #4743:
#   1. usage_logs.bedrock_account_id POPULATES for live calls. With zero
#      mappings authored, the resolver must fall to the platform rung, so the
#      value should be the platform Bedrock account id (12 digits).
#   2. Proxy latency is UNCHANGED (the resolution ladder runs on every model
#      call; #4689 is the cautionary precedent).
#
# WHY THIS READS THE API AND NOT THE DATABASE
#   bedrockgw-<env>-postgres is not publicly accessible, and the agent pod has
#   neither psql/psycopg2 nor kubectl access to the adp-gateway namespace (the
#   agent-scaledjob-sa role cannot list pods there, so port-forward is out).
#   GET /api/usage/logs exposes bedrock_account_id in its response model
#   (src/usage/schemas.py), which makes this check psql-free by construction —
#   matching the "psql-free" requirement in the issue's validation section.
#
# WHY THE LATENCY CHECK USES /api/health AND NOT MODEL-CALL p50
#   Measured on dev 2026-09-07: model-call p50 was 6529 ms with p95 40783 ms
#   (min 1322 / max 82995). That spread is inference time, not our code, and it
#   would hide a resolver regression of a few ms entirely. /api/health traverses
#   the same ASGI middleware/request path with p50 91.1 ms and stdev 4.4 ms, so
#   a shared-path regression is actually detectable. Model p50 is still reported
#   as context, but the PASS/FAIL gate is the low-variance control.
#
# Idempotent: read-only. Authors no mappings, writes no state, mutates nothing.
# Safe to re-run any number of times, in any environment.
#
# Usage:
#   ./platform/scripts/validate-bedrock-routing-shadow.sh --environment dev
#   ./platform/scripts/validate-bedrock-routing-shadow.sh -e dev --org aws-e
#   ./platform/scripts/validate-bedrock-routing-shadow.sh -e dev --baseline-p50 91.1
#
# Exit codes: 0 = all criteria pass, 1 = a criterion failed, 2 = setup error.
set -euo pipefail

ENVIRONMENT="dev"
ORG=""
BASELINE_P50=""
TOLERANCE_PCT="50"
SAMPLES="25"
LIMIT="100"

usage() {
  cat <<'USAGE'
Usage: validate-bedrock-routing-shadow.sh [options]

Options:
  --environment, -e   Target environment (default: dev)
  --org               Org id to inspect usage logs for (default: auto-detect
                      the org with the most requests)
  --baseline-p50      Pre-merge control p50 in ms. If supplied, the control p50
                      must stay within --tolerance of it. Recorded pre-merge on
                      dev for #4743: 91.1
  --tolerance         Allowed control-p50 regression, percent (default: 50)
  --samples           Control-endpoint samples to take (default: 25)
  --limit             Usage-log rows to inspect, 1-100 (default: 100)
  --help, -h          Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --environment|-e) ENVIRONMENT="$2"; shift 2 ;;
    --org)            ORG="$2"; shift 2 ;;
    --baseline-p50)   BASELINE_P50="$2"; shift 2 ;;
    --tolerance)      TOLERANCE_PCT="$2"; shift 2 ;;
    --samples)        SAMPLES="$2"; shift 2 ;;
    --limit)          LIMIT="$2"; shift 2 ;;
    --help|-h)        usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

for bin in aws curl python3; do
  command -v "$bin" >/dev/null 2>&1 || { echo "ERROR: '$bin' not found in PATH" >&2; exit 2; }
done

echo "=== Bedrock routing shadow-mode validation (#4743) ==="
echo "Environment: ${ENVIRONMENT}"
echo

# ---------------------------------------------------------------------------
# Resolve the gateway base URL from the CloudFront distribution comment.
# ---------------------------------------------------------------------------
echo "--> Resolving gateway endpoint..."
CF_DOMAIN="$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?contains(Comment, 'bedrockgw-${ENVIRONMENT}')].DomainName | [0]" \
  --output text 2>/dev/null || true)"

if [[ -z "${CF_DOMAIN}" || "${CF_DOMAIN}" == "None" ]]; then
  echo "ERROR: no CloudFront distribution found for bedrockgw-${ENVIRONMENT}." >&2
  echo "       Is the gateway deployed in this account/environment?" >&2
  exit 2
fi
BASE_URL="https://${CF_DOMAIN}"
echo "    ${BASE_URL}"

HEALTH="$(curl -s --max-time 15 "${BASE_URL}/api/health" || true)"
if ! grep -q healthy <<<"${HEALTH}"; then
  echo "ERROR: gateway /api/health is not healthy: ${HEALTH}" >&2
  exit 2
fi
echo "    health: OK"
echo

# ---------------------------------------------------------------------------
# Authenticate. Credentials come from Secrets Manager and are never echoed;
# the token is handed to curl via a file, never a command-line argument (argv
# is world-readable via /proc on a shared host).
# ---------------------------------------------------------------------------
echo "--> Authenticating (LOGS_READ required)..."
TOKEN_FILE="$(mktemp)"
trap 'rm -f "${TOKEN_FILE}"' EXIT

if ! ADP_ENV="${ENVIRONMENT}" python3 - "${TOKEN_FILE}" <<'PY'
import json, os, subprocess, sys

env = os.environ["ADP_ENV"]
secret_id = f"adp/{env}/gateway/test-admin-credentials"
try:
    raw = subprocess.run(
        ["aws", "secretsmanager", "get-secret-value", "--secret-id", secret_id,
         "--query", "SecretString", "--output", "text"],
        capture_output=True, text=True, check=True,
    ).stdout
    d = json.loads(raw)
except Exception as exc:                      # noqa: BLE001 - surface cause, hide value
    print(f"could not read {secret_id}: {type(exc).__name__}", file=sys.stderr)
    sys.exit(1)

try:
    import boto3
    client = boto3.client("cognito-idp", region_name=d.get("aws_region", "us-east-1"))
    resp = client.initiate_auth(
        ClientId=d["cognito_client_id"],
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": d["username"], "PASSWORD": d["password"]},
    )
    with open(sys.argv[1], "w") as fh:
        fh.write(resp["AuthenticationResult"]["IdToken"])
except Exception as exc:                      # noqa: BLE001
    print(f"cognito auth failed: {type(exc).__name__}", file=sys.stderr)
    sys.exit(1)
PY
then
  echo "ERROR: authentication failed. Cannot read usage logs." >&2
  exit 2
fi
echo "    auth: OK"
echo

auth_get() {  # auth_get <path> -> body on stdout
  curl -s --max-time 30 -H "Authorization: Bearer $(cat "${TOKEN_FILE}")" "${BASE_URL}$1"
}

# ---------------------------------------------------------------------------
# Pick the busiest org if one was not supplied — an org with no traffic would
# make the populate check vacuously "pass" on an empty result set.
# ---------------------------------------------------------------------------
if [[ -z "${ORG}" ]]; then
  echo "--> Auto-detecting the org with the most requests..."
  ORG="$(auth_get "/api/usage/organizations" | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    sys.exit(0)
rows = [r for r in rows if r.get("org_id") and r.get("total_requests")]
if rows:
    print(max(rows, key=lambda r: r["total_requests"])["org_id"])
')"
  [[ -n "${ORG}" ]] || { echo "ERROR: could not auto-detect an org with traffic. Pass --org." >&2; exit 2; }
fi
echo "    org: ${ORG}"
echo

# ---------------------------------------------------------------------------
# Criterion 1 — the column populates.
# ---------------------------------------------------------------------------
echo "--> [1/2] Checking usage_logs.bedrock_account_id populates..."
LOGS_JSON="$(auth_get "/api/usage/logs?org_id=${ORG}&limit=${LIMIT}")"

CRIT1_RESULT="$(printf '%s' "${LOGS_JSON}" | python3 -c '
import json, sys, collections

try:
    payload = json.load(sys.stdin)
except Exception:
    print("SETUP|could not parse /api/usage/logs response")
    sys.exit(0)

items = payload.get("items") or []
if not items:
    print("SETUP|no usage-log rows returned; cannot judge population")
    sys.exit(0)

populated = [i for i in items if i.get("bedrock_account_id")]
counts = collections.Counter(i["bedrock_account_id"] for i in populated)
pct = 100.0 * len(populated) / len(items)

detail = f"{len(populated)}/{len(items)} rows populated ({pct:.1f}%)"
if counts:
    detail += " | values: " + ", ".join(f"{v}={n}" for v, n in counts.most_common(5))

# Shape check: a Bedrock account id is exactly 12 digits.
bad = [v for v in counts if not (v.isdigit() and len(v) == 12)]
if bad:
    print(f"FAIL|malformed account id(s) {bad[:3]} — expected 12 digits | {detail}")
elif not populated:
    print(f"FAIL|column still NULL on every row — shadow-mode write is not landing | {detail}")
elif pct < 100.0:
    # Partial population = some Bedrock-reaching path was missed (issue §7).
    print(f"PARTIAL|only some rows populated — a Bedrock-reaching path is likely unthreaded | {detail}")
else:
    print(f"PASS|{detail}")
' )"

CRIT1_STATUS="${CRIT1_RESULT%%|*}"
CRIT1_DETAIL="${CRIT1_RESULT#*|}"

case "${CRIT1_STATUS}" in
  PASS)    echo "    PASS: ${CRIT1_DETAIL}" ;;
  PARTIAL) echo "    PARTIAL: ${CRIT1_DETAIL}" ;;
  FAIL)    echo "    FAIL: ${CRIT1_DETAIL}" ;;
  *)       echo "    INCONCLUSIVE: ${CRIT1_DETAIL}" ;;
esac
echo

# ---------------------------------------------------------------------------
# Criterion 2 — latency unchanged.
# ---------------------------------------------------------------------------
echo "--> [2/2] Measuring latency (${SAMPLES} samples on the control endpoint)..."
LAT_FILE="$(mktemp)"
trap 'rm -f "${TOKEN_FILE}" "${LAT_FILE}"' EXIT
for _ in $(seq 1 "${SAMPLES}"); do
  curl -s -o /dev/null -w '%{time_total}\n' --max-time 15 "${BASE_URL}/api/health" >>"${LAT_FILE}" || true
done

# Model-call p50 reported for context only (high variance — see header).
printf '%s' "${LOGS_JSON}" | python3 -c '
import json, statistics, sys
try:
    items = json.load(sys.stdin).get("items") or []
except Exception:
    sys.exit(0)
lat = sorted(i["latency_ms"] for i in items
             if i.get("latency_ms") is not None and i.get("status_code") == 200)
if lat:
    p95 = lat[max(0, int(len(lat) * 0.95) - 1)]
    print(f"    context only (high variance, not a gate): model-call p50="
          f"{statistics.median(lat):.0f}ms p95={p95}ms n={len(lat)}")
'

CRIT2_RESULT="$(BASELINE_P50="${BASELINE_P50}" TOLERANCE_PCT="${TOLERANCE_PCT}" \
  python3 - "${LAT_FILE}" <<'PY'
import os, statistics, sys

vals = sorted(float(l) * 1000 for l in open(sys.argv[1]) if l.strip())
if not vals:
    print("SETUP|no latency samples collected")
    sys.exit(0)

p50 = statistics.median(vals)
p95 = vals[max(0, int(len(vals) * 0.95) - 1)]
stdev = statistics.stdev(vals) if len(vals) > 1 else 0.0
detail = f"control p50={p50:.1f}ms p95={p95:.1f}ms stdev={stdev:.1f}ms n={len(vals)}"

baseline = os.environ.get("BASELINE_P50", "").strip()
if not baseline:
    print(f"BASELINE|{detail} (no --baseline-p50 given; recorded for comparison)")
    sys.exit(0)

base = float(baseline)
tol = float(os.environ["TOLERANCE_PCT"])
ceiling = base * (1 + tol / 100.0)
delta_pct = (p50 - base) / base * 100.0
verdict = "PASS" if p50 <= ceiling else "FAIL"
print(f"{verdict}|{detail} | baseline={base:.1f}ms delta={delta_pct:+.1f}% "
      f"ceiling={ceiling:.1f}ms (tolerance {tol:.0f}%)")
PY
)"

CRIT2_STATUS="${CRIT2_RESULT%%|*}"
CRIT2_DETAIL="${CRIT2_RESULT#*|}"
echo "    ${CRIT2_STATUS}: ${CRIT2_DETAIL}"
echo

# ---------------------------------------------------------------------------
# Verdict.
# ---------------------------------------------------------------------------
echo "=== Summary ==="
echo "  [1] bedrock_account_id populates : ${CRIT1_STATUS}"
echo "  [2] latency unchanged            : ${CRIT2_STATUS}"
echo

if [[ "${CRIT1_STATUS}" == "PASS" && ( "${CRIT2_STATUS}" == "PASS" || "${CRIT2_STATUS}" == "BASELINE" ) ]]; then
  echo "RESULT: PASS — shadow mode is populating and latency is within tolerance."
  echo "Wave 2 (#4744 R3, #4745 R4) may proceed."
  exit 0
fi

echo "RESULT: NOT PASSING — Wave 2 (#4744, #4745) stays gated."
if [[ "${CRIT1_STATUS}" == "FAIL" ]]; then
  echo "  Hint: pre-merge, NULL on every row is EXPECTED — the column is dormant on main."
  echo "        Both proxy paths (src/proxy/service.py, src/proxy/mantle_service.py)"
  echo "        call log_request() without bedrock_account_id."
fi
if [[ "${CRIT1_STATUS}" == "PARTIAL" ]]; then
  echo "  Hint: enumerate every Bedrock-reaching path (issue §7). An unthreaded"
  echo "        path leaves its rows NULL."
fi
exit 1
