"""Reject incomplete DA19 live evidence against the accepted 63-pair contract."""

import hashlib
import json

from qualification import ROOT, REVISION, digest, timestamp, validate_manifest, verify_release

PLAN = ROOT / "docs/l5-services/devopsagnent/delivery-plan.json"


def verify_acceptance(manifest, evidence, reader, *, now):
    """Only separate readbacks can attest a scenario; receipt text is not proof."""
    validate_manifest(manifest)
    run_id = manifest.get("authorization", {}).get("run_id")
    verify_release(manifest, reader, now=now, qualification_run_id=run_id)
    plan = json.loads(PLAN.read_text())
    required = {(story["alias"], acceptance, scenario)
                for story in plan["stories"]
                for acceptance, scenario in zip(story["acceptance_ids"], story["scenario_ids"], strict=True)}
    epic = {criterion for story in plan["stories"] for criterion in story["epic_criteria"]}
    if len(required) != 63 or len(epic) != 10 or plan["requirements_revision"] != REVISION:
        raise ValueError("accepted R3 story and epic contract changed; review new requirements first")
    if (not isinstance(evidence, dict) or evidence.get("requirements_revision") != REVISION
            or evidence.get("release") != manifest["release"]
            or (run_id and evidence.get("authorization_run_id") != run_id)
            or set(evidence.get("epic_criteria", [])) != epic
            or evidence.get("cleanup") != {"status": "confirmed", "owned_absent": True, "retained_unchanged": True}
            or not isinstance(evidence.get("attempts"), list) or not evidence["attempts"]
            or any(not isinstance(attempt, dict) or attempt.get("status") not in {"completed", "reconciled"}
                   or not attempt.get("receipt") for attempt in evidence["attempts"])):
        raise ValueError("incomplete release, attempt, epic or cleanup evidence")
    epic_readback = reader.read_epic_criteria(manifest["target_ref"])
    if (not isinstance(epic_readback, dict) or set(epic_readback) != epic
            or any(not isinstance(item, dict) or item.get("status") != "passed"
                   or item.get("source_sha") != manifest["release"]["source_sha"]
                   or item.get("deployment_id") != manifest["release"]["deployment_id"]
                   or (run_id and item.get("qualification_run_id") != run_id)
                   or not timestamp(manifest["release"]["deployed_at"]) <= timestamp(
                       item.get("observed_at")) <= now for item in epic_readback.values())):
        raise ValueError("ten epic criteria require independent current observations")
    checks = evidence.get("live_checks", {})
    if (not isinstance(checks, dict) or not {
            "sso-gateway-only-ingress", "sso-denial-before-federation"} <= set(checks)
            or any(not isinstance(check, dict) or check.get("status") != "passed"
                   or not check.get("readback_ref") for check in checks.values())):
        raise ValueError("R3 ingress and denial checks require independent passing readback")
    for name, check in checks.items():
        observed_check = reader.read_check(manifest["target_ref"], check["readback_ref"])
        if (not isinstance(observed_check, dict) or observed_check.get("name") != name
                or observed_check.get("status") != "passed"
                or observed_check.get("source_sha") != manifest["release"]["source_sha"]
                or observed_check.get("deployment_id") != manifest["release"]["deployment_id"]
                or (run_id and observed_check.get("qualification_run_id") != run_id)
                or not timestamp(manifest["release"]["deployed_at"]) <= timestamp(
                    observed_check.get("observed_at")) <= now):
            raise ValueError("independent live check differs from the selected release")
        if (name == "sso-denial-before-federation" and observed_check.get("provider_session_calls") != 0):
            raise ValueError("denied launches reached provider sessions")
        if (name == "sso-gateway-only-ingress" and (
                observed_check.get("browser_launch_route") != "gateway"
                or observed_check.get("function_urls") != []
                or observed_check.get("public_launchers") != [])):
            raise ValueError("live ingress still exposes another launcher")
    cases = evidence.get("cases", [])
    if not isinstance(cases, list) or len(cases) != len(required):
        raise ValueError("all 63 AC/SCN pairs need native evidence")
    found = set()
    refs = set()
    for case in cases:
        key = (case.get("story"), case.get("acceptance"), case.get("scenario"))
        if (key not in required or key in found or case.get("status") != "passed"
                or case.get("source_sha") != manifest["release"]["source_sha"]
                or case.get("deployment_id") != manifest["release"]["deployment_id"]
                or (run_id and case.get("qualification_run_id") != run_id)
                or not digest(case.get("readback_sha256"), 64) or not case.get("readback_ref")
                or not case.get("native_id") or not case.get("scope")
                or case["readback_ref"] in refs):
            raise ValueError("missing, stale, wrong-source or duplicate story evidence")
        found.add(key)
        refs.add(case["readback_ref"])
        observation = reader.read_case(manifest["target_ref"], case["readback_ref"])
        expected_hash = hashlib.sha256(json.dumps(observation, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if (case["readback_sha256"] != expected_hash or observation.get("status") != "passed"
                or not observation.get("native_outcome")
                or (run_id and observation.get("qualification_run_id") != run_id)
                or any(observation.get(field) != case[field] for field in (
                    "story", "acceptance", "scenario", "source_sha", "deployment_id", "native_id", "scope"))
                or not timestamp(manifest["release"]["deployed_at"]) <= timestamp(observation.get("observed_at")) <= now):
            raise ValueError("independent observation does not match current native case")
    if found != required:
        raise ValueError("story acceptance has missing AC/SCN pairs")
    if reader.read_cleanup(manifest["target_ref"]) != evidence["cleanup"]:
        raise ValueError("owned cleanup was not independently confirmed")
    return {"requirements_revision": REVISION, "verified_pairs": len(required), "verified_epic": len(epic)}
