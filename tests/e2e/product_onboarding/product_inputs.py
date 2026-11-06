"""Assemble B01 inputs from pinned private runtime reports, never from fixtures."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .product_evidence import SCENARIOS
from .product_publish import read_input


def installed_cli_surface(cli, release, dispatch, digest):
    identities = release.get("identities") or {}
    required_cases = {"PR03-C01", "PR03-C02", "PR03-C03"}
    rows = cli.get("cases")
    attempts = cli.get("attempts")
    artifact = cli.get("artifact_ref")
    if (
        cli.get("schema_version") != 1
        or cli.get("surface") != "installed-cli"
        or cli.get("status") != "passed"
        or cli.get("failures") != []
        or cli.get("cleanup") != "complete"
        or cli.get("cleanup_outstanding") != []
        or cli.get("workflow_run_id") != dispatch.get("run_id")
        or not isinstance(cli.get("evaluation_id"), str)
        or not cli["evaluation_id"]
        or not isinstance(cli.get("attempt_id"), str)
        or not cli["attempt_id"]
        or not isinstance(artifact, str)
        or not re.fullmatch(r"private:[A-Za-z0-9/_:.-]{1,256}", artifact)
        or f"/{dispatch.get('run_id')}/{cli['evaluation_id']}/" not in artifact
        or cli.get("source_revision") != dispatch.get("expected_revision")
        or cli.get("workflow_revision") != dispatch.get("workflow_revision")
        or cli.get("api_image_digest") != digest
        or cli.get("cli_manifest_sha256") != identities.get("cli_checksum")
        or not isinstance(rows, list)
        or len(rows) != len(required_cases)
        or {
            row.get("case_id")
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("case_id"), str)
        }
        != required_cases
        or any(
            not isinstance(row, dict) or row.get("status") != "passed" for row in rows
        )
        or not isinstance(attempts, list)
    ):
        return None
    latest = {
        attempt["case_id"]: (index, attempt)
        for index, attempt in enumerate(attempts)
        if isinstance(attempt, dict)
        and isinstance(attempt.get("case_id"), str)
        and attempt["case_id"] in required_cases
    }
    if set(latest) != required_cases or any(
        proof.get("status") != "passed" for _, proof in latest.values()
    ):
        return None
    attempt_index, attempt = latest["PR03-C01"]
    events = attempt.get("cli_events")
    if not isinstance(events, list):
        return None
    for event_index, event in enumerate(events):
        if (
            isinstance(event, dict)
            and isinstance(event.get("argv"), list)
            and event["argv"][:3] == ["adp", "product", "show"]
            and event.get("exit_code") == 0
        ):
            reference = f"{artifact}#/attempts/{attempt_index}/cli_events/{event_index}"
            return {
                "status": "pass",
                "artifact_ref": artifact,
                "expected": "installed Product CLI agrees with the selected release",
                "actual": "verified Product command and readback in the private V03 report",
                "source_revision": cli["source_revision"],
                "command": "adp product show --json",
                "stdout_ref": reference + "/output",
                "stderr_ref": reference + "/stderr_diagnostic",
                "exit_code": 0,
                "cli_checksum": cli["cli_manifest_sha256"],
            }
    return None


def assemble(dispatch_path, api_path, release_path, cli_path, output_path, packet_path):
    issues = []
    dispatch = read_input(dispatch_path, "selected API dispatch", issues)
    api = read_input(api_path, "selected API evidence", issues)
    release = read_input(release_path, "independent deployment receipt", issues)
    cli = read_input(cli_path, "installed CLI report", issues)
    source = dispatch.get("expected_revision")
    workflow = dispatch.get("workflow_revision")
    run_id = dispatch.get("run_id")
    inputs = {
        "run_id": run_id,
        "report_artifact": str(packet_path),
        "identities": {"source_revision": source, "workflow_revision": workflow},
        "deployment": {},
        "cases": {},
        "surfaces": {},
        "cleanup": {},
    }
    inventory = api.get("inventory") if isinstance(api.get("inventory"), dict) else {}
    native = api.get("native") if isinstance(api.get("native"), dict) else {}
    pods = inventory.get("pods") if isinstance(inventory.get("pods"), list) else []
    if (
        dispatch.get("status") == "pass"
        and dispatch.get("cleanup_verified") is True
        and dispatch.get("observed_revision") == source
        and api.get("run_id") == run_id
        and api.get("source_revision") == source
        and api.get("workflow_revision") == workflow
        and api.get("observation_ref") == dispatch.get("observation_ref")
        and isinstance(run_id, str)
        and run_id
        and isinstance(source, str)
        and source
        and isinstance(workflow, str)
        and workflow
        and isinstance(dispatch.get("observation_ref"), str)
        and dispatch["observation_ref"]
        and inventory.get("source_revision") == source
        and isinstance(inventory.get("digest"), str)
        and inventory["digest"]
        and pods
        and all(
            isinstance(pod, dict)
            and pod.get("uid")
            and pod.get("digest") == inventory["digest"]
            for pod in pods
        )
        and native.get("source_revision") == source
        and native.get("tick_verified") is True
        and native.get("tick_digest") == inventory["digest"]
        and native.get("pod_uids") == [pod["uid"] for pod in pods]
        and native.get("observation_ref") == dispatch["observation_ref"]
        and isinstance(native.get("database_heads"), list)
        and native["database_heads"]
        and native.get("database_heads") == native.get("image_heads")
        and len(native["database_heads"]) == len(pods)
        and all(isinstance(head, str) and head for head in native["database_heads"])
        and len(set(native["database_heads"])) == 1
    ):
        product = api.get("product") if isinstance(api.get("product"), dict) else {}
        results = (
            product.get("results") if isinstance(product.get("results"), dict) else {}
        )
        cleanup = (
            product.get("cleanup") if isinstance(product.get("cleanup"), dict) else {}
        )
        selected = dispatch.get("selected_cases")
        if (
            product.get("run_id") == run_id
            and product.get("source_revision") == source
            and cleanup.get("fixture_removed") is True
            and isinstance(selected, list)
            and selected
            and set(selected) == set(results)
            and len(selected) == len(results)
        ):
            recovery = results.get("api_recovery_cleanup")
            shared_verified = (
                isinstance(recovery, dict) and recovery.get("cleanup_verified") is True
            )
            for name in selected:
                if (
                    name not in SCENARIOS
                    or not isinstance(results[name], dict)
                    or not results[name].get("product_id")
                ):
                    continue
                result = results[name]
                evidence_ref = f"{api_path}#/product/results/{name}"
                cleanup_ref = f"{api_path}#/product/cleanup"
                attempts = []
                if name == "api_recovery_cleanup":
                    for index, attempt in enumerate(result.get("attempts") or []):
                        if (
                            isinstance(attempt, dict)
                            and attempt.get("state") == "response_unknown"
                        ):
                            attempts.append(
                                {
                                    "attempt_id": f"{run_id}/{name}/{index}",
                                    "result": "interrupted",
                                    "expected": "original write",
                                    "actual": "response unknown; resumed from durable state",
                                    "evidence_ref": evidence_ref + f"/attempts/{index}",
                                }
                            )
                attempts.append(
                    {
                        "attempt_id": f"{run_id}/{name}/verified",
                        "result": "pass",
                        "expected": "selected Product API case",
                        "actual": "API runner verified the result",
                        "evidence_ref": evidence_ref,
                    }
                )
                inputs["cases"][name] = {
                    "ac_id": SCENARIOS[name][0],
                    "scenario_id": SCENARIOS[name][1],
                    "run_id": run_id,
                    "test_ref": f"product_recovery.run::{name}",
                    "expected": "selected Product behavior",
                    "actual": "verified API result",
                    "status": "pass",
                    "evidence_ref": evidence_ref,
                    "readback_ref": evidence_ref,
                    "cleanup_ref": cleanup_ref,
                    "attempts": attempts,
                }
                inputs["cleanup"][name] = {
                    "status": "verified" if shared_verified else "incomplete",
                    "receipt_ref": cleanup_ref,
                    "owned_absent": True,
                    "shared_unchanged": shared_verified,
                }
            if set(inputs["cases"]) == set(SCENARIOS):
                inputs["surfaces"]["api"] = {
                    "status": "pass",
                    "source_revision": source,
                    "artifact_ref": str(api_path),
                    "readback_ref": str(api_path),
                    "expected": "selected Product API behavior",
                    "actual": "runner verified all selected cases and fixture retirement",
                }
        else:
            issues.append("selected API results or fixture retirement are unavailable")
        identities = (
            release.get("identities")
            if isinstance(release.get("identities"), dict)
            else {}
        )
        deployment = (
            release.get("deployment")
            if isinstance(release.get("deployment"), dict)
            else {}
        )
        observed = (
            deployment.get("observed")
            if isinstance(deployment.get("observed"), dict)
            else {}
        )
        observed_pods = observed.get("serving_pods")
        if (
            identities.get("source_revision") == source
            and identities.get("workflow_revision") == workflow
            and identities.get("api_image_digest") == inventory["digest"]
            and identities.get("schema_head") == native["database_heads"][0]
            and observed.get("source_revision") == source
            and observed.get("workflow_revision") == workflow
            and observed.get("schema_head") == native["database_heads"][0]
            and observed.get("api_image_digest") == inventory["digest"]
            and deployment.get("observation_ref") == dispatch["observation_ref"]
            and deployment.get("receipt_ref")
            and deployment["receipt_ref"] != deployment["observation_ref"]
            and isinstance(observed_pods, list)
            and [pod.get("pod_ref") for pod in observed_pods if isinstance(pod, dict)]
            == [pod["uid"] for pod in pods]
        ):
            inputs["plan_revision"] = release.get("plan_revision")
            inputs["identities"].update(
                {
                    key: identities.get(key)
                    for key in (
                        "requirements_revision",
                        "source_revision",
                        "workflow_revision",
                        "schema_head",
                        "api_image_digest",
                        "cli_checksum",
                    )
                }
            )
            inputs["deployment"] = {
                "receipt_ref": deployment["receipt_ref"],
                "observation_ref": deployment["observation_ref"],
                "observed": {
                    **{
                        key: observed.get(key)
                        for key in (
                            "source_revision",
                            "workflow_revision",
                            "schema_head",
                            "api_image_digest",
                            "cli_checksum",
                        )
                    },
                    "serving_pods": [
                        {
                            key: pod.get(key)
                            for key in (
                                "pod_ref",
                                "source_revision",
                                "api_image_digest",
                                "product_profile_enabled",
                                "product_onboarding_enabled",
                            )
                        }
                        for pod in observed_pods
                        if isinstance(pod, dict)
                    ],
                },
            }
            surface = installed_cli_surface(cli, release, dispatch, inventory["digest"])
            if surface is not None:
                inputs["surfaces"]["cli"] = surface
            else:
                issues.append(
                    "installed CLI report does not match the selected run and release"
                )
        else:
            issues.append(
                "independent deployment receipt does not match observed API release"
            )
    else:
        issues.append("selected API evidence or native observation is incomplete")
    inputs["producer_issues"] = issues
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(inputs, sort_keys=True, indent=2) + "\n")
    output.chmod(0o600)
    return inputs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for option in (
        "dispatch-report",
        "api-evidence",
        "release-report",
        "cli-report",
        "output",
        "packet-path",
    ):
        parser.add_argument("--" + option, required=True)
    args = parser.parse_args(argv)
    assemble(
        args.dispatch_report,
        args.api_evidence,
        args.release_report,
        args.cli_report,
        args.output,
        args.packet_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
