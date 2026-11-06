"""Grade real D01 release evidence; incomplete recovery and handoff stay NOT_RUN."""

from __future__ import annotations

import argparse
import hashlib
import json
from copy import deepcopy
from pathlib import Path

from .product_evidence import SCENARIOS, build_packet
from .product_publish import read_input


CASE_IDS = ("PR01-C01", "PR01-C02", "PR01-C03", "PR01-C04")


def reject_false_green(packet):
    mutations = {
        "missing_case": lambda value: value["cases"].pop("api_product_lifecycle"),
        "stale_source": lambda value: value["deployment"]["observed"].update(
            source_revision="0" * 40
        ),
        "stale_schema": lambda value: value["deployment"]["observed"].update(
            schema_head="stale-schema"
        ),
        "mismatched_image": lambda value: value["deployment"]["observed"].update(
            api_image_digest="sha256:" + "0" * 64
        ),
        "mismatched_cli": lambda value: value["surfaces"]["cli"].update(
            cli_checksum="0" * 64
        ),
        "health_only": lambda value: value["deployment"]["observed"].update(
            serving_pods=[]
        ),
    }
    results = {}
    for name, mutate in mutations.items():
        candidate = deepcopy(packet)
        mutate(candidate)
        results[name] = build_packet(candidate)["status"] == "incomplete"
    return results


def evaluate(inputs, dispatch, packet, release):
    identities = packet.get("identities") if isinstance(packet.get("identities"), dict) else {}
    deployment = packet.get("deployment") if isinstance(packet.get("deployment"), dict) else {}
    claimed_dispatch = packet.get("dispatch") if isinstance(packet.get("dispatch"), dict) else {}
    checks = {
        "packet_complete": packet.get("status") == "pass"
        and packet.get("issues") == []
        and build_packet(packet)["status"] == "pass",
        "packet_unchanged": packet.get("packet_revision") == "sha256:" + hashlib.sha256(
            json.dumps({key: value for key, value in packet.items() if key != "packet_revision"},
                       sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "producers_complete": inputs.get("producer_issues") == []
        and dispatch.get("status") == "pass"
        and dispatch.get("cleanup_verified") is True,
        "selection_complete": dispatch.get("selected_cases") == list(SCENARIOS)
        and claimed_dispatch.get("selected_cases") == list(SCENARIOS)
        and set(packet.get("cases") or {}) == set(SCENARIOS),
        "run_matched": isinstance(packet.get("run_id"), str)
        and packet["run_id"] == dispatch.get("run_id") == inputs.get("run_id")
        and claimed_dispatch.get("run_id") == packet["run_id"],
        "source_matched": identities.get("source_revision") == dispatch.get("expected_revision")
        == dispatch.get("observed_revision") == claimed_dispatch.get("expected_revision")
        == inputs.get("identities", {}).get("source_revision"),
        "workflow_matched": identities.get("workflow_revision") == dispatch.get("workflow_revision")
        == claimed_dispatch.get("workflow_revision") == inputs.get("identities", {}).get("workflow_revision"),
        "release_matched": identities == release.get("identities")
        and deployment == release.get("deployment")
        and deployment.get("observation_ref") == dispatch.get("observation_ref")
        == claimed_dispatch.get("observation_ref"),
        "api_cli_matched": packet.get("surfaces") == inputs.get("surfaces")
        and packet.get("cases") == inputs.get("cases")
        and packet.get("cleanup") == inputs.get("cleanup"),
    }
    report = {
        "schema_version": 1,
        "run_id": dispatch.get("run_id"),
        "source_revision": dispatch.get("expected_revision"),
        "workflow_revision": dispatch.get("workflow_revision"),
        "packet_revision": packet.get("packet_revision"),
        "checks": checks,
        "cases": {case_id: {"status": "NOT_RUN"} for case_id in CASE_IDS},
    }
    if not all(checks.values()):
        report["cases"]["PR01-C01"] = {"status": "FAILED", "failed_checks": [
            name for name, passed in checks.items() if not passed
        ]}
    else:
        report["cases"]["PR01-C01"] = {"status": "PASSED", "evidence_ref": packet["report_artifact"]}
        negative = reject_false_green(packet)
        report["cases"]["PR01-C02"] = {
            "status": "PASSED" if all(negative.values()) else "FAILED",
            "negative_checks": negative,
            "evidence_ref": packet["report_artifact"],
        }
    report["status"] = "pass" if all(
        case["status"] == "PASSED" for case in report["cases"].values()
    ) else "incomplete"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preliminary", action="store_true")
    for name in ("inputs", "dispatch", "packet", "release", "output"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    issues = []
    report = evaluate(
        read_input(args.inputs, "B01 inputs", issues),
        read_input(args.dispatch, "selected dispatch", issues),
        read_input(args.packet, "B01 packet", issues),
        read_input(args.release, "signed release report", issues),
    )
    report["input_issues"] = issues
    if issues:
        report["status"] = "incomplete"
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    preliminary_ok = args.preliminary and not issues and all(
        report["cases"][case]["status"] == "PASSED" for case in CASE_IDS[:2]
    )
    return 0 if report["status"] == "pass" or preliminary_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
