"""Publish selected Product evidence in the maintained private Gateway result artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .product_evidence import SHA, build_packet, present
from .product_profile_api import CASES


def read_input(path, label, issues):
    try:
        result = json.loads(Path(path).read_text())
    except (OSError, ValueError, TypeError):
        issues.append(f"{label} is missing or unreadable")
        return {}
    if not isinstance(result, dict):
        issues.append(f"{label} is not an evidence mapping")
        return {}
    return result


def publish(inputs_path, dispatch_path, output_path, *, selection, source_revision, workflow_revision):
    """Retain incomplete attempts; refuse green when any required producer is absent."""
    issues = []
    inputs = read_input(inputs_path, "combined B01 inputs", issues)
    dispatch = read_input(dispatch_path, "selected API dispatch", issues)
    packet = build_packet(inputs)
    cases = selection.split(",")
    if len(cases) != len(CASES) or set(cases) != set(CASES):
        issues.append("selected API cases do not cover the complete B01 inventory")
    if not all(isinstance(revision, str) and SHA.fullmatch(revision)
               for revision in (source_revision, workflow_revision)):
        issues.append("source/workflow dispatch revision is missing or invalid")
    identities = packet.get("identities") if isinstance(packet.get("identities"), dict) else {}
    if identities.get("source_revision") != source_revision or identities.get("workflow_revision") != workflow_revision:
        issues.append("dispatch does not match the packet source/workflow revision")
    if (dispatch.get("selected_cases") != cases or dispatch.get("expected_revision") != source_revision
            or dispatch.get("observed_revision") != source_revision
            or dispatch.get("workflow_revision") != workflow_revision
            or dispatch.get("run_id") != packet.get("run_id")):
        issues.append("API dispatch did not observe the selected release and run")
    deployment = packet.get("deployment") if isinstance(packet.get("deployment"), dict) else {}
    if (not present(dispatch.get("observation_ref"))
            or dispatch.get("observation_ref") != deployment.get("observation_ref")):
        issues.append("independent API dispatch observation is unavailable or mismatched")
    if dispatch.get("status") != "pass" or dispatch.get("cleanup_verified") is not True:
        issues.append("API driver failed, did not run, or did not verify cleanup")
    if packet.get("report_artifact") != str(output_path):
        issues.append("packet artifact path differs from the selected output")
    packet["dispatch"] = {
        "selected_cases": cases, "expected_revision": source_revision,
        "workflow_revision": workflow_revision,
        "report_path": str(output_path),
        "observed_revision": dispatch.get("observed_revision"),
        "observation_ref": dispatch.get("observation_ref"),
        "run_id": dispatch.get("run_id"),
    }
    packet["issues"].extend(issues)
    packet["status"] = "pass" if not packet["issues"] else "incomplete"
    packet.pop("packet_revision")
    packet["packet_revision"] = "sha256:" + hashlib.sha256(
        json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(packet, sort_keys=True, indent=2) + "\n")
    return packet


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--dispatch-report", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--workflow-revision", required=True)
    args = parser.parse_args(argv)
    packet = publish(args.inputs, args.dispatch_report, args.output,
                     selection=args.cases, source_revision=args.expected_revision,
                     workflow_revision=args.workflow_revision)
    return 0 if packet["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
