#!/usr/bin/env python3
"""Verify issue #6999 selectors against the private frozen SARIF without publishing it."""

import argparse
import hashlib
import json
from pathlib import Path

from diff_security_findings import _has_accepted_suppression, _sarif_rule_index, resolve_sarif_severity

ORIGINAL_SHA256 = "4b77d2237eb6864e2228eb433a6ec314a94aad858417f7ee094248c9ad6227bd"
SSRF = "nodejs_scan.javascript-ssrf-rule-node_ssrf"
EVAL = "nodejs_scan.javascript-eval-rule-eval_nodejs"
ASSIGNED = (
    (SSRF, "demos/domain-mri/public/app.js", 23),
    (EVAL, "modules/agent-factory/agent/src/run-heartbeat.ts", 205),
    (SSRF, "modules/gateway/frontend/src/pages/DomainMRI.tsx", 24),
    (SSRF, "modules/gateway/frontend/src/services/activity.ts", 207),
    (SSRF, "modules/gateway/frontend/src/services/agentExplanations.ts", 19),
    (SSRF, "modules/gateway/frontend/src/services/api.ts", 92),
    (SSRF, "modules/gateway/frontend/src/services/auth.ts", 285),
    (SSRF, "modules/gateway/frontend/src/services/auth.ts", 355),
    (SSRF, "modules/gateway/frontend/src/services/taskActivity.ts", 25),
    (EVAL, "modules/tools/task-sdk/codex-runner.mjs", 95),
    (SSRF, "modules/tools/task-sdk/test/codex.test.mjs", 72),
)


def reconcile(report: dict) -> list[dict]:
    """Require eleven unique run-zero selectors and native 9 critical / 2 high ratings."""
    matches = {key: [] for key in ASSIGNED}
    for run_index, run in enumerate(report.get("runs", [])):
        if run_index != 0:
            continue
        rules = _sarif_rule_index(run)
        for result_index, result in enumerate(run.get("results", [])):
            for location in result.get("locations", []):
                physical = location.get("physicalLocation", {})
                uri = physical.get("artifactLocation", {}).get("uri", "")
                line = physical.get("region", {}).get("startLine")
                rule_id = result.get("ruleId", "")
                for key in ASSIGNED:
                    rule, path, expected_line = key
                    if (line == expected_line and (uri == path or uri.endswith("/" + path))
                            and (rule_id == rule or rule_id.endswith("." + rule))):
                        rating, source = resolve_sarif_severity(result, rules)
                        matches[key].append({"result_index": result_index, "rating": rating,
                                             "rating_source": source,
                                             "accepted_suppression": _has_accepted_suppression(result)})
    missing = [f"{path}:{line}={len(found)}" for (_, path, line), found in matches.items() if len(found) != 1]
    if missing:
        raise ValueError("Assigned records must each occur exactly once in run 0: " + ", ".join(missing))
    records = [dict(rule=rule, path=path, line=line, **matches[(rule, path, line)][0])
               for rule, path, line in ASSIGNED]
    ratings = [record["rating"] for record in records]
    if ratings.count("critical") != 9 or ratings.count("high") != 2 or any(
            record["rating_source"] != "native" for record in records):
        raise ValueError("Assigned records must have nine native critical and two native high ratings")
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sarif", type=Path, help="Private original report; do not commit or print it")
    args = parser.parse_args()
    source = args.sarif.read_bytes()
    if hashlib.sha256(source).hexdigest() != ORIGINAL_SHA256:
        parser.error("Original SARIF digest does not match issue #6999")
    print(json.dumps(reconcile(json.loads(source)), indent=2))


if __name__ == "__main__":
    main()
