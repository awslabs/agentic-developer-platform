"""AWS synthetic analyst and hosted-SDK acceptance. Artifacts remain in S3."""

import argparse
import json
import os
import time
import urllib.request

import boto3
import hosted_evaluation
import live_evaluation
from adaptive_fixture import HOST
from benchmark import get_bytes, put_json, require_aws_runtime


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-prefix", required=True)
    args = p.parse_args()
    require_aws_runtime()
    s3 = boto3.client("s3")
    prefix = args.output_prefix.rstrip("/")
    for attempt in range(45):
        try:
            with urllib.request.urlopen(
                os.environ["URL_ANALYSIS_BROWSER_BROKER"] + "/healthz", timeout=3
            ):
                break
        except OSError:
            time.sleep(2)
    else:
        raise RuntimeError("Synthetic broker unavailable")
    cases = [
        {
            "id": "fixture-" + variant,
            "url": f"https://{HOST}/{variant}/seed",
            "scope": "host",
            "objective": "Investigate this controlled fictional account-verification flow. Follow relevant leads to identify the requested information and claimed operator; examine counterevidence. Do not submit forms. This is a training scenario, not a real brand.",
            "incident_context": [
                {
                    "source": "Synthetic researcher briefing",
                    "reported_at": "2026-09-24T00:00:00Z",
                    "summary": "This fictional account-verification flow was supplied for training. Its provider relationship requires examination.",
                }
            ],
        }
        for variant in ("a", "b")
    ]
    put_json(
        s3,
        prefix + "/seeds.json",
        {"schema_version": "cyber-live-evaluation/1", "cases": cases},
    )
    live_evaluation.main(
        [
            "--manifest",
            prefix + "/seeds.json",
            "--output-prefix",
            prefix + "/adapter",
            "--max-cases",
            "2",
        ]
    )
    put_json(
        s3,
        prefix + "/hosted-seed.json",
        {"schema_version": "cyber-live-evaluation/1", "cases": cases[:1]},
    )
    hosted_evaluation.main(
        [
            "--manifest",
            prefix + "/hosted-seed.json",
            "--output-prefix",
            prefix + "/hosted",
            "--max-cases",
            "1",
        ]
    )
    results = []
    for mode in ("adapter", "hosted"):
        summary = json.loads(get_bytes(s3, prefix + "/" + mode + "/summary.json"))
        for result in summary["cases"]:
            case = json.loads(
                get_bytes(
                    s3, prefix + "/" + mode + "/cases/" + result["id"] + "/case.json"
                )
            )
            checks = {
                "model_completed": result["model_completed"],
                "stateful_form_reached": any(o["forms"] for o in case["observations"]),
                "operator_disclosure_reached": any(
                    o["page_title"] == "Operator disclosure"
                    for o in case["observations"]
                ),
                "incident_context_retained": bool(case.get("incident_context")),
                "cleanup_confirmed": all(
                    s["cleanup_status"] == "stopped" for s in case["sessions"]
                ),
            }
            results.append(
                {
                    "mode": mode,
                    "id": result["id"],
                    "checks": checks,
                    "pass": all(checks.values()),
                }
            )
    put_json(
        s3,
        prefix + "/acceptance.json",
        {
            "results": results,
            "limitation": "Structural checks require separate evidence review; no public-site accuracy or ingress claim.",
        },
    )
    print(json.dumps({"acceptance": results}), flush=True)
    if not all(r["pass"] for r in results):
        raise RuntimeError("Analyst acceptance did not satisfy structural checks")


if __name__ == "__main__":
    main()
