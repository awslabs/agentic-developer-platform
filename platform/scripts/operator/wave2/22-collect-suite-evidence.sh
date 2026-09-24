#!/usr/bin/env bash
# Wave 2 fixture step 2b — collect the suite/schema evidence (W2-02, W2-08, W2-09).
#
# Issue #3968 / epic #3959.
#
# Three artifacts, all produced by running real suites or exporting from real
# source -- never hand-written:
#
#   neutral_contract.json  (W2-02) the neutral contract suite run against BOTH the
#                          Claude adapter and the independently shaped echo
#                          adapter, with each one's real test count.
#   vocabulary_parity.json (W2-08) the writer/reader/renderer suites plus the
#                          deployed writer and gateway digests.
#   stats_schema_keys.json (W2-09) the stats response field names exported FROM
#                          the backend models, so the live response is compared
#                          against the schema rather than against a copy of itself.
#
# Needs NO AWS credential for the suites. The two deployed-digest booleans in
# vocabulary_parity DO need a cluster read, so that part runs inside an
# adp-cred session and is skipped (recorded false) with --no-cloud.
#
# Usage:
#   ./22-collect-suite-evidence.sh --evidence-dir <dir> [--no-cloud]

set -euo pipefail

EVIDENCE_DIR=""
NO_CLOUD=0
LEDGER=""
IDENTITY=""
GATEWAY_IMAGE=""
WORKER_IMAGE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --evidence-dir) EVIDENCE_DIR="${2:?}"; shift 2 ;;
    --ledger) LEDGER="${2:?}"; shift 2 ;;
    --expected-identity) IDENTITY="${2:?}"; shift 2 ;;
    --gateway-image) GATEWAY_IMAGE="${2:?}"; shift 2 ;;
    --worker-image) WORKER_IMAGE="${2:?}"; shift 2 ;;
    --no-cloud)     NO_CLOUD=1; shift ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
ok()   { printf 'ok   %s\n' "$*"; }
note() { printf '     %s\n' "$*"; }

[ -n "$EVIDENCE_DIR" ] || fail "--evidence-dir is required"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../../.." && pwd)"
AGENT_DIR="$REPO_ROOT/modules/agent-factory/agent"
GW_DIR="$REPO_ROOT/modules/gateway"
WORKER_DIR="$REPO_ROOT/modules/agent-factory/agent-worker-image"
ART="$EVIDENCE_DIR/artifacts"
mkdir -p "$ART"

. "$HERE/lib/session.sh"
if [ "$NO_CLOUD" -eq 0 ]; then
  [ -n "$LEDGER" ] && [ -n "$IDENTITY" ] && [ -n "$GATEWAY_IMAGE" ] && [ -n "$WORKER_IMAGE" ] || fail "live deployment evidence requires --ledger --expected-identity --gateway-image --worker-image"
fi

# ---------------------------------------------------------------------------
# 1. neutral_contract (W2-02)
# ---------------------------------------------------------------------------
printf '\n== W2-02: neutral contract suite, both adapters ==\n'
cd "$AGENT_DIR"
[ -d node_modules ] || { note "installing node_modules (no AWS credential needed)"; npm ci >/dev/null 2>&1 || fail "npm ci failed"; }

# Per-adapter test counts come from jest's own JSON report, so "passed" is
# accompanied by a real number. A suite that ran zero tests also exits 0, which
# the harness explicitly rejects.
JEST_JSON="$ART/raw-jest-neutral-contract.json"
set +e
npx --no-install jest --runInBand --json --outputFile="$JEST_JSON" \
  src/control-runtime.test.ts \
  src/harnesses/claude-control.test.ts \
  src/utils/resilientQuery.test.ts >/dev/null 2>&1
JEST_RC=$?
set -e
[ -f "$JEST_JSON" ] || fail "jest produced no JSON report (rc=$JEST_RC)"
[ "$JEST_RC" -eq 0 ] || note "jest exited $JEST_RC -- the real result is recorded, not masked"

python3 - "$JEST_JSON" "$ART/neutral_contract.json" "$AGENT_DIR" <<'PY'
import json, re, sys
from pathlib import Path

jest_path, out_path, agent_dir = sys.argv[1:4]
report = json.loads(Path(jest_path).read_text())
agent = Path(agent_dir)

# Attribute each test to an adapter by the file it lives in. The echo adapter's
# tests live in the shared contract suite (it is exercised THROUGH the neutral
# contract, which is the point), so we count by which adapter a test names.
claude_tests = echo_tests = 0
claude_failed = echo_failed = 0
for suite in report.get("testResults", []):
    for case in suite.get("assertionResults", []):
        title = f"{' '.join(case.get('ancestorTitles') or [])} {case.get('title','')}".lower()
        failed = case.get("status") == "failed"
        if "echo" in title:
            echo_tests += 1
            echo_failed += failed
        else:
            claude_tests += 1
            claude_failed += failed

# The second adapter must not import the provider SDK, and must declare a
# missing capability. Both are read FROM ITS SOURCE, not asserted.
echo_src_raw = (agent / "src/harnesses/__fixtures__/echo-control.ts").read_text()
# Comments stripped for the same reason as the shared contract below.
echo_src = re.sub(r"//.*$", "", re.sub(r"/\*.*?\*/", "", echo_src_raw, flags=re.S), flags=re.M)
imports_sdk = bool(re.search(r"""from\s+['"]@anthropic-ai/""", echo_src))
# `steer`/`abort` declared false is the capability gap.
declares_gap = bool(re.search(r"(steer|abort)\s*:\s*(false|\{[^}]*supported:\s*false)", echo_src))

# The shared neutral contract must not import any provider SDK or expose a
# provider type. Comments are stripped first: this file DOCUMENTS the ban ("No
# `Query`, no `SDKUserMessage`"), so scanning raw text would flag the very
# comment that states the rule and report a neutral file as contaminated.
shared_src = (agent / "src/control-runtime.ts").read_text()
shared_code = re.sub(r"/\*.*?\*/", "", shared_src, flags=re.S)
shared_code = re.sub(r"//.*$", "", shared_code, flags=re.M)
shared_clean = not re.search(r"""from\s+['"]@anthropic-ai/""", shared_code) and not re.search(
    r"\b(Query|SDKUserMessage)\b", shared_code)

# Installed SDK vs lockfile.
lock = json.loads((agent / "package-lock.json").read_text())
pinned = (lock.get("packages", {}).get("node_modules/@anthropic-ai/claude-agent-sdk") or {}).get("version")
try:
    installed = json.loads((agent / "node_modules/@anthropic-ai/claude-agent-sdk/package.json").read_text())["version"]
except Exception:
    installed = None

# Properties proven by the suite as a whole: True only when the suite that
# exercises them actually passed. A failing suite must not yield True here.
suite_green = claude_failed == 0 and echo_failed == 0 and claude_tests > 0 and echo_tests > 0

payload = {
    "protocol_version": 1,
    "adapter_id": "claude",
    "sdk_version": installed,
    "sdk_matches_lockfile": (installed is not None and installed == pinned),
    "adapters": {
        "claude": {"passed": claude_failed == 0 and claude_tests > 0, "test_count": claude_tests},
        "echo": {"passed": echo_failed == 0 and echo_tests > 0, "test_count": echo_tests},
    },
    "second_adapter": {
        "name": "echo",
        "declares_missing_capability": declares_gap,
        "imports_provider_sdk": imports_sdk,
    },
    "no_provider_types_in_shared_contract": shared_clean,
    # Each of these is proven by a named test group in the suite above. They are
    # reported as the suite's verdict, not as an independent claim.
    "capability_intersection_proven": suite_green,
    "normalized_input_kinds_proven": suite_green,
    "authorization_at_handoff": suite_green,
    "unknown_outcome_supported": suite_green,
    "opaque_attempt_replacement": suite_green,
    "stale_events_rejected": suite_green,
    "disposed_once": suite_green,
    "fresh_private_input_per_attempt": suite_green,
    "session_and_no_option_behavior_preserved": suite_green,
    "cancel_prevents_new_query": suite_green,
    "forced_retry_exercised": suite_green,
    # S3 ships no verbs; S2 adds pause/resume. Read from the runtime constant so
    # the artifact cannot disagree with the build.
    "implemented_verbs": sorted(re.findall(r"'(pause|resume|steer|abort)'", re.search(
        r"IMPLEMENTED_CONTROL_VERBS[^\]]*\]", shared_src).group(0))) if re.search(
        r"IMPLEMENTED_CONTROL_VERBS[^\]]*\]", shared_src) else [],
    "_provenance": {
        "jest_report": jest_path,
        "claude_failed": claude_failed,
        "echo_failed": echo_failed,
        "note": "test counts and pass/fail are taken from jest's own JSON report",
    },
}
Path(out_path).write_text(json.dumps(payload, indent=2) + "\n")
print(f"ok   wrote {out_path} (claude={claude_tests} tests, echo={echo_tests} tests)")
if not suite_green:
    print("     NOTE: suite not fully green -- property booleans recorded False accordingly")
PY

# ---------------------------------------------------------------------------
# 2. vocabulary_parity (W2-08)
# ---------------------------------------------------------------------------
printf '\n== W2-08: writer/reader/renderer parity suites ==\n'
declare -A SUITE_RESULT

run_suite() { # label  dir  command...
  local label="$1" dir="$2" rc=0
  shift 2
  if [ ! -d "$dir" ]; then SUITE_RESULT["$label"]="not_run:missing_dir"; note "$label not run (missing $dir)"; return 0; fi
  # `set -e` is suspended around the run on purpose: a failing suite is a RESULT
  # to record, not a reason to abandon collection. Without this the first red
  # suite would kill the script and the remaining three would be silently absent
  # from the artifact -- which reads as "not evaluated" instead of "failed".
  set +e
  ( cd "$dir" && "$@" >/dev/null 2>&1 )
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then SUITE_RESULT["$label"]="passed"; ok "$label passed"
  else SUITE_RESULT["$label"]="failed"; note "$label FAILED (rc=$rc, recorded as failed)"; fi
}

run_suite "tests/activity/test_status_aborted.py" "$GW_DIR" python3 -m pytest tests/activity/test_status_aborted.py -q
run_suite "tests/test_status_vocabulary.py" "$WORKER_DIR" python3 -m pytest tests/test_status_vocabulary.py -q
run_suite "src/__tests__/utils/status.test.ts" "$GW_DIR/frontend" npx vitest run src/__tests__/utils/status.test.ts
run_suite "src/__tests__/components/InvocationChain.test.tsx" "$GW_DIR/frontend" npx vitest run src/__tests__/components/InvocationChain.test.tsx

# Deployed digests + the live allowlists. Requires a cluster read.
DEPLOYED_JSON="$ART/raw-deployed-vocabulary.json"
if [ "$NO_CLOUD" = 1 ]; then
  note "--no-cloud: deployed digests NOT verified (recorded false, not assumed true)"
  printf '{"writer_digest_deployed": false, "gateway_digest_deployed": false, "_reason": "--no-cloud: not verified"}\n' > "$DEPLOYED_JSON"
else
  w2_require_account
  KUBECONFIG="$(w2_kubeconfig)"
  export KUBECONFIG
  export W2_VOCAB_COLLECTOR="$HERE/lib/vocabulary_deployment.py"
  export W2_VOCAB_LEDGER="$LEDGER" W2_VOCAB_IDENTITY="$IDENTITY"
  export W2_VOCAB_GATEWAY_IMAGE="$GATEWAY_IMAGE" W2_VOCAB_WORKER_IMAGE="$WORKER_IMAGE"
  export W2_VOCAB_OUTPUT="$DEPLOYED_JSON"
  w2_session 'python3 "$W2_VOCAB_COLLECTOR" --ledger "$W2_VOCAB_LEDGER" --identity "$W2_VOCAB_IDENTITY" --gateway-image "$W2_VOCAB_GATEWAY_IMAGE" --worker-image "$W2_VOCAB_WORKER_IMAGE" --out "$W2_VOCAB_OUTPUT"' || fail "fixture deployment observation failed"
  ok "verified approved images on ledger-bound fixture pods"
fi

python3 - "$ART/vocabulary_parity.json" "$DEPLOYED_JSON" "$WORKER_DIR" "$GW_DIR" \
  "${SUITE_RESULT["tests/activity/test_status_aborted.py"]}" \
  "${SUITE_RESULT["tests/test_status_vocabulary.py"]}" \
  "${SUITE_RESULT["src/__tests__/utils/status.test.ts"]}" \
  "${SUITE_RESULT["src/__tests__/components/InvocationChain.test.tsx"]}" <<'PY'
import ast, json, re, sys
from pathlib import Path

out, deployed_path, worker_dir, gw_dir, s1, s2, s3, s4 = sys.argv[1:9]
deployed = json.loads(Path(deployed_path).read_text())

# The allowlists are IMPORTED from the reviewed source, never regex-scraped: a
# pattern that silently fails to match would yield an empty list, and an empty
# list is indistinguishable from "aborted is missing" -- a false failure that
# would send someone hunting a nonexistent defect. Import fails loudly instead.
def load_const(module_path: Path, module_name: str, const: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return sorted(getattr(module, const))

# liveness.py is import-safe (constants + pure functions, no AWS clients).
terminal = load_const(Path(gw_dir) / "src/activity/liveness.py", "_w2_liveness",
                      "OBSERVED_TERMINAL_STATUSES")

# invocation_status.py imports boto3 lazily but may pull module-level deps; parse
# its literal set with ast instead of executing it, which is exact (no regex).
writer_src = (Path(worker_dir) / "lib/invocation_status.py").read_text()
writer_statuses = []
for node in ast.walk(ast.parse(writer_src)):
    if isinstance(node, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == "ALLOWED_WRITE_STATUSES" for t in node.targets
    ):
        # frozenset({...}) -> the set literal is the single call argument.
        call = node.value
        literal = call.args[0] if isinstance(call, ast.Call) and call.args else call
        writer_statuses = sorted(ast.literal_eval(literal))
if not writer_statuses:
    print("FAIL: could not read ALLOWED_WRITE_STATUSES from the writer source",
          file=sys.stderr)
    raise SystemExit(1)

payload = {
    # Only the live cluster read can establish these. Absent that, False.
    "writer_digest_deployed": deployed.get("writer_digest_deployed") is True,
    "gateway_digest_deployed": deployed.get("gateway_digest_deployed") is True,
    "writer_allowed_statuses": writer_statuses,
    "gateway_terminal_statuses": terminal,
    # The reject path must be OBSERVED firing. test_status_vocabulary.py covers
    # it; we report the suite's verdict and require the operator to keep it green.
    "unknown_status_rejected": s2 == "passed",
    "unknown_status_reached_table": False if s2 == "passed" else None,
    "suites": {
        "tests/activity/test_status_aborted.py": s1,
        "tests/test_status_vocabulary.py": s2,
        "src/__tests__/utils/status.test.ts": s3,
        "src/__tests__/components/InvocationChain.test.tsx": s4,
    },
    "_provenance": {"deployed_read": deployed},
}
Path(out).write_text(json.dumps(payload, indent=2) + "\n")
print(f"ok   wrote {out}")
for name, status in payload["suites"].items():
    if status != "passed":
        print(f"     SUITE NOT PASSED: {name} = {status}")
PY

# ---------------------------------------------------------------------------
# 3. stats_schema_keys (W2-09)
# ---------------------------------------------------------------------------
printf '\n== W2-09: stats schema keys exported from the backend models ==\n'
python3 - "$ART/stats_schema_keys.json" "$GW_DIR" <<'PY'
import json, re, sys
from pathlib import Path

out, gw_dir = sys.argv[1:3]
src = (Path(gw_dir) / "src/activity/stats_schemas.py").read_text()

# Parse the Pydantic models' declared field names. Exported from the backend
# models on purpose: a fixed list in the harness would not notice a field ADDED
# to the schema that the deployment omits.
def fields(model: str) -> list[str]:
    m = re.search(rf"class {model}\(BaseModel\):(.*?)(?=\nclass |\Z)", src, re.S)
    if not m:
        return []
    return [n for n in re.findall(r"^\s{4}([a-z_][a-z0-9_]*)\s*:", m.group(1), re.M)]

levels = {
    "response": fields("StatsResponse"),
    "today": fields("TodayCounts"),
    "daily": fields("DailyEntry"),
    "by_persona": fields("PersonaStats"),
    "active_runs": fields("ActiveRun"),
    "recent_failures": fields("RecentFailure"),
    "top_repos": fields("TopRepo"),
    "spend": fields("Spend"),
}
missing = [k for k, v in levels.items() if not v]
payload = {"levels": levels, "_provenance": {
    "source": "modules/gateway/src/activity/stats_schemas.py",
    "note": "field names parsed from the Pydantic models of the reviewed revision",
}}
Path(out).write_text(json.dumps(payload, indent=2) + "\n")
print(f"ok   wrote {out}")
for level, keys in levels.items():
    print(f"     {level}: {len(keys)} keys")
if missing:
    print(f"FAIL: could not parse model(s) for: {missing}", file=sys.stderr)
    raise SystemExit(1)
PY

printf '\nok   suite/schema evidence collected into %s\n' "$ART"
