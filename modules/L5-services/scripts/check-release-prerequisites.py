#!/usr/bin/env python3
"""Check protected release inputs without credentials, publication or provider work.

This is an early configuration check, not fixture authorization or qualification.
The selected service's existing private materializer and runtime preflight remain
responsible for its complete schema, authority, sessions and installed readbacks.
"""

import argparse
import json
import os
import re


SERVICES = {"security", "devops", "finops", "transform", "well_architected"}
MAX_INPUT_BYTES = 262144


def blocker(code, name, action):
    # All messages come from reviewed constants, never private input values or
    # parser/provider exception text. They are safe for the workflow's public log.
    return {"code": code, "input": name, "next_action": action}


def object_input(environment, key, label, failures):
    raw = environment.get(key, "")
    if not raw or (isinstance(raw, str) and not raw.strip()):
        failures.append(blocker("missing_protected_input", label,
            "Configure this secret in the selected protected deployment environment before retrying."))
        return None
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_INPUT_BYTES:
        failures.append(blocker("invalid_protected_input", label,
            "Supply a private JSON object within the supported size bound."))
        return None
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        value = None
    if not isinstance(value, dict) or not value:
        failures.append(blocker("invalid_protected_input", label,
            "Supply a non-empty private JSON object; check its syntax without printing secret contents."))
        return None
    return value


def source_matches(document, source):
    if "bundle_version" in document:
        if (type(document["bundle_version"]) is not int or document["bundle_version"] != 1
                or document.get("source_sha") != source or not isinstance(document.get("fixture"), dict)):
            return False
        document = document["fixture"]
    pins = [document.get("source_sha"), document.get("release_sha")]
    for section, keys in (
        ("authorization", ("source_sha",)),
        ("release", ("source_sha", "worker")),
        ("installed_release", ("source_revision",)),
    ):
        nested = document.get(section)
        if isinstance(nested, dict):
            pins.extend(nested.get(key) for key in keys)
    selected = [pin for pin in pins if pin is not None]
    return bool(selected) and all(pin == source for pin in selected)


def check(service, source_sha, environment, *, operation="release"):
    service = service.replace("-", "_")
    if service not in SERVICES:
        raise ValueError("Select a supported L5 service")
    if operation not in {"release", "install", "publish"}:
        raise ValueError("Select release, standalone installation or artifact publication")
    failures = []
    if operation != "release" and any(environment.get(key) for key in (
            "ADP_CORRELATION", "ADP_SOURCE_REVISION", "ADP_DEFINITION_REVISION", "ADP_CONTRACT_DIGEST")):
        failures.append(blocker("installation_cannot_complete_delivery" if operation == "install" else "publication_cannot_complete_delivery", "operation",
            "Use full release qualification for engine delivery; standalone installation or publication cannot emit delivery evidence."))
    source_valid = isinstance(source_sha, str) and re.fullmatch(r"[a-f0-9]{40}", source_sha)
    if not source_valid:
        failures.append(blocker("invalid_source_revision", "SOURCE_SHA",
            "Select the reviewed full source commit; do not repin an existing authorization automatically."))
    if operation == "release":
        fixture_name = f"ADP_L5_{service.upper()}_LIVE_CONFIG_JSON"
        fixture = object_input(environment, "PRIVATE_FIXTURE_JSON", fixture_name, failures)
        if fixture is not None and source_valid and not source_matches(fixture, source_sha):
            failures.append(blocker("fixture_source_mismatch", fixture_name,
                "Review and authorize the fixture for the selected source revision, including every supplied source pin."))
    if service == "devops" and operation == "release" and fixture is not None and source_valid and source_matches(fixture, source_sha):
        selected = fixture.get("fixture", fixture) if "bundle_version" in fixture else fixture
        if environment.get("L5_BROWSER_AUTOMATION", "disabled") != "allowed":
            manual = selected.get("manual_ui", {})
            if not isinstance(manual, dict) or manual.get("mode") != "authenticated-human":
                failures.append(blocker("devops_human_evidence_required", fixture_name,
                    "Select the original two human observers and UI receipt in manual_ui before release. "
                    "Keep browser and AWS Console automation disabled; runtime preflight verifies the complete scope."))
    infrastructure = (object_input(environment, "ADP_L5_INFRA_CONFIG_JSON", "ADP_L5_INFRA_CONFIG_JSON", failures)
                      if operation != "publish" else None)
    if infrastructure is not None:
        services = infrastructure.get("services")
        if not isinstance(services, dict) or not isinstance(services.get(service), dict) or not services[service]:
            failures.append(blocker("missing_service_infrastructure", "ADP_L5_INFRA_CONFIG_JSON",
                "Add the selected service's private infrastructure inputs using the modular deployment contract."))
    if service == "security" and operation == "release":
        credentials = object_input(environment, "ADP_L5_SECURITY_LIVE_CREDENTIALS_JSON",
            "ADP_L5_SECURITY_LIVE_CREDENTIALS_JSON", failures)
        if credentials is not None and any(
                not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
                or not isinstance(value, str) or not value for name, value in credentials.items()):
            failures.append(blocker("invalid_private_credentials", "ADP_L5_SECURITY_LIVE_CREDENTIALS_JSON",
                "Supply the dedicated test sessions as a non-empty environment variable map; runtime preflight verifies authority."))
    if service == "transform":
        object_input(environment, "ADP_L5_TRANSFORM_IMAGE_CONFIG_JSON", "ADP_L5_TRANSFORM_IMAGE_CONFIG_JSON", failures)
    return {"schema_version": 1, "service": service, "phase": "protected_input_preflight",
            "outcome": "blocked" if failures else "configured", "blockers": failures,
            "live_acceptance": "not_evaluated"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=sorted(SERVICES | {"well-architected"}))
    parser.add_argument("source_sha")
    parser.add_argument("--operation", choices=("release", "install", "publish"), default="release")
    options = parser.parse_args()
    report = check(options.service, options.source_sha, os.environ, operation=options.operation)
    for issue in report["blockers"]:
        print(f"{issue['code']}: {issue['input']}: {issue['next_action']}")
    if report["blockers"]:
        print("L5 release blocked before artifact publication or infrastructure installation; repair the listed inputs before retrying.")
        return 1
    print("Protected L5 inputs are configured; service authorization and live qualification remain to be verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
