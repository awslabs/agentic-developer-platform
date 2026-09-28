"""Run in an AWS worker against the isolated adaptive_fixture broker."""

import argparse
import json
import os
import time
import urllib.request

import boto3
import live_evaluation as live
from adaptive_fixture import HOST
from benchmark import get_bytes, put_json, require_aws_runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-prefix", required=True)
    args = parser.parse_args()
    require_aws_runtime()
    prefix = args.output_prefix.rstrip("/")
    s3 = boto3.client("s3")
    for attempt in range(45):
        try:
            with urllib.request.urlopen(
                os.environ["URL_ANALYSIS_BROWSER_BROKER"] + "/healthz", timeout=3
            ) as response:
                if response.status == 200:
                    break
        except OSError:
            time.sleep(2)
    else:
        raise RuntimeError("Isolated acceptance broker did not become healthy")
    manifest = {
        "schema_version": "cyber-live-evaluation/1",
        "cases": [
            {
                "id": "fixture-" + variant,
                "url": f"https://{HOST}/{variant}/seed",
                "scope": "host",
                "objective": "Investigate this controlled fictional website's account verification flow: what information is requested, who claims to operate it, and what evidence supports or contradicts its claimed affiliation? Examine relevant leads and counterevidence. Do not submit forms or enter credentials.",
            }
            for variant in ("a", "b")
        ],
    }
    put_json(s3, prefix + "/seeds.json", manifest)
    try:
        live.main(
            [
                "--manifest",
                prefix + "/seeds.json",
                "--output-prefix",
                prefix,
                "--max-cases",
                "2",
            ]
        )
    except Exception as error:
        put_json(
            s3,
            prefix + "/acceptance-error.json",
            {"type": type(error).__name__, "message": str(error)[:2000]},
        )
        raise
    summary = json.loads(get_bytes(s3, prefix + "/summary.json"))
    acceptance = []
    for result in summary["cases"]:
        case_prefix = prefix + "/cases/" + result["id"]
        case = json.loads(get_bytes(s3, case_prefix + "/case.json"))
        transcript = json.loads(get_bytes(s3, case_prefix + "/model-decisions.json"))
        observations = case["observations"]
        actions = [
            r for r in transcript["turns"] if r.get("ok") and r.get("tool") == "advance"
        ]
        checks = {
            "real_model_finished": result["model_completed"],
            "multiple_model_selected_actions": len(actions) >= 2,
            "received_new_evidence_after_each_action": all(
                r["before_observation"] != r["after_observation"]
                and r["browser_open_before"]
                and r["browser_open_after"]
                for r in actions
            ),
            "state_dependent_form_reached": any(o["forms"] for o in observations)
            and not any(
                "Missing session context" in o["visible_text"] for o in observations
            ),
            "operator_disclosure_reached": any(
                o["page_title"] == "Operator disclosure" for o in observations
            ),
            "context_preserved": result["adaptive"]["distinct_contexts"] == 1
            and result["adaptive"]["multiple_observations_in_one_context"],
            "evidence_reviewed": result["adaptive"]["reviews"] >= len(observations),
            # Fixture A supplies counterevidence; B can support the earlier concern.
            # A revision is not mandatory when the new evidence confirms it.
            "counterevidence_revised_when_present": result["id"] != "fixture-a"
            or result["adaptive"]["hypothesis_revisions"] > 0,
            "stopped_and_closed": result["adaptive"]["has_stop_reason"]
            and result["adaptive"]["cleanup_confirmed"],
        }
        record = {
            "id": result["id"],
            "checks": checks,
            "structural_pass": all(checks.values()),
            "reviews": case["reviews"],
            "assessment": case["assessment"],
            "stop_reason": case["stop_reason"],
            "limitation": "Structural checks require separate review of the model's claims against the synthetic evidence; no accuracy or ingress claim.",
        }
        acceptance.append(record)
        # This entry point is synthetic-only. Real-site runs never log findings.
        print(json.dumps({"event": "synthetic_acceptance", **record}), flush=True)
    put_json(s3, prefix + "/acceptance.json", {"cases": acceptance})
    if not all(row["structural_pass"] for row in acceptance):
        raise RuntimeError("Adaptive acceptance did not satisfy the structural checks")


if __name__ == "__main__":
    main()
