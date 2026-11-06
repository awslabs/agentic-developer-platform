#!/usr/bin/env python3
"""Publish bounded qualification metadata and the Gateway's delivery/v2 contract.

Service readers own scenario evaluation. This adapter never invents a criterion
or upgrades its outcome. Raw observations, credentials and browser state stay in
the private evidence directory; only an explicit metadata allowlist is staged.
"""

import base64
from datetime import UTC, datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re

from release_context import authorize_engine, release_identity, verify_principal


WORKFLOW = ".github/workflows/l5-services-release.yml"
SECTIONS = ("epic_criteria", "story_criteria", "scenarios", "live_checks")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,255}\Z")
SHA = re.compile(r"[a-f0-9]{40}\Z")
OUTCOMES = {"passed", "failed", "skipped", "incomplete", "blocked", "not_evaluated"}
READBACK_ACTIONS = {
    "acceptance_catalog_not_evaluated":
        "Extend the service driver to evaluate the accepted criteria, scenarios and live checks; retain their original IDs.",
    "service_journey_not_evaluated":
        "Complete authorized setup, client retrieval, recovery, cleanup and downstream reuse checks; selected-run readback alone does not complete delivery.",
    "provider_source_coverage_unverified":
        "Retain the selected source verification and explicitly resolve the provider ingestion evidence required by the accepted design.",
    "release_attempt_identity_missing":
        "Bind the service emitter to the validated workflow identity and fixture digest before treating its observations as evidence for this attempt.",
    "transform_bootstrap_not_exercised":
        "Run the bounded Product setup and execution journey and retain its original run/result identities; this driver only reads existing runs.",
}


def private_json(path):
    path = Path(path)
    if (not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents))
            or not path.is_file() or path.stat().st_mode & 0o077
            or path.stat().st_size > 4 * 1024 * 1024
            or path.resolve().is_relative_to(Path(__file__).resolve().parents[3])):
        raise ValueError("Qualification input must be bounded, private and outside the checkout")
    raw = path.read_bytes()
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("Qualification input must be an object")
    return document, hashlib.sha256(raw).hexdigest()


def label(value):
    if not isinstance(value, str) or not LABEL.fullmatch(value):
        raise ValueError("Qualification identifiers must be bounded labels")
    return value


def incomplete_readback(identity, digest, *, identity_verified):
    """Publish a diagnostic, never upgrade the limited service readback format.

    Transform's original export has no workflow identity. Only its same-source,
    explicitly non-accepted format may produce an unverified diagnostic. No run
    contents or purported passed checks are copied into the release report.
    """
    reasons = ["acceptance_catalog_not_evaluated", "service_journey_not_evaluated"]
    if identity["service"] == "transform":
        reasons += ["release_attempt_identity_missing", "transform_bootstrap_not_exercised"]
    else:
        reasons.append("provider_source_coverage_unverified")
    return {
        "schema_version": 1,
        **{key: identity[key] for key in ("source_revision", "definition_revision", "run_id", "run_attempt")},
        "workflow_path": WORKFLOW, "correlation_id": identity["correlation"],
        "contract_digest": identity["contract_digest"], "input_sha256": digest,
        "qualification_scope": "selected_product_native_readback", "live_acceptance": "not_evaluated",
        "reported_attempt_identity_verified": identity_verified, "outcome": "incomplete",
        "unresolved": reasons, "cleanup": {"outcome": "incomplete"},
        **{section: [] for section in SECTIONS},
        "diagnostics": [{"code": reason, "next_action": READBACK_ACTIONS[reason]} for reason in reasons],
    }


def normalized_report(raw, identity, digest):
    """Retain evaluator fields, replacing private references with content hashes."""
    if identity["service"] == "transform" and raw.get("schema") == "transform-release-qualification/v1":
        if (set(raw) != {"schema", "source_revision", "status", "live_accepted", "runs"}
                or raw.get("source_revision") != identity["source_revision"]
                or raw.get("live_accepted") is not False
                or raw.get("status") not in {"evaluated", "unresolved"}
                or not isinstance(raw.get("runs"), list) or not 1 <= len(raw["runs"]) <= 10):
            raise ValueError("Transform readback diagnostic is incompatible with this release")
        return incomplete_readback(identity, digest, identity_verified=False)
    expected = {key: identity[key] for key in ("source_revision", "definition_revision", "run_id", "run_attempt")}
    if (raw.get("schema_version") != 1 or raw.get("workflow_path") != WORKFLOW
            or any(str(raw.get(key)) != str(value) for key, value in expected.items())
            or raw.get("correlation_id", raw.get("correlation")) != identity["correlation"]
            or raw.get("contract_digest") != identity["contract_digest"]):
        raise ValueError("Qualification belongs to another release attempt")
    if (identity["service"] == "well_architected"
            and raw.get("qualification_scope") == "selected_product_native_readback"
            and raw.get("live_acceptance") == "not_evaluated"
            and raw.get("limitations") == ["provider_source_coverage_unverified", "full_live_acceptance_not_evaluated"]):
        return incomplete_readback(identity, digest, identity_verified=True)
    if identity["service"] == "well_architected":
        from qualification_well_architected_acceptance import validate_catalog

        validate_catalog(raw)
    if identity["service"] == "transform":
        from qualification_transform_acceptance import validate_catalog

        if raw.get("release_context") != identity:
            raise ValueError("Transform collector belongs to another validated release identity")
        validate_catalog(raw)
    result = {"schema_version": 1, **expected, "workflow_path": WORKFLOW,
              "correlation_id": identity["correlation"], "contract_digest": identity["contract_digest"],
              "input_sha256": digest}
    if "requirements_revision" in raw:
        result["requirements_revision"] = label(raw["requirements_revision"])
    unresolved = raw.get("unresolved")
    if not isinstance(unresolved, list) or len(unresolved) > 4096:
        raise ValueError("Qualification must retain unresolved observations")
    # Free-form diagnostics can contain native responses. Publish only hashes.
    result["unresolved"] = ["diagnostic-sha256:" + hashlib.sha256(
        json.dumps(item, sort_keys=True).encode()).hexdigest() for item in unresolved]
    if identity["service"] == "well_architected":
        from qualification_well_architected_acceptance import NEXT

        # Only source-controlled explanations may be public. Ignore arbitrary
        # next_action/native text in the private observation document.
        incomplete = {item["id"] for item in raw["live_checks"] if item.get("outcome") != "passed"}
        result["diagnostics"] = [{"code": identifier + ":not-observed", "next_action": NEXT[identifier]}
                                 for identifier in sorted(incomplete) if identifier in NEXT]
        result["unresolved"].extend(item["code"] for item in result["diagnostics"])
    if identity["service"] == "transform":
        from qualification_transform_acceptance import NEXT

        result["diagnostics"] = [{"code": name + ":not-observed", "next_action": NEXT[name]}
                                 for name in sorted(NEXT) if name not in raw["observed_criteria"]]
        from qualification_transform_retained import COLLECTION_ACTIONS

        progress = raw.get("progress", [])
        if not isinstance(progress, list) or len(progress) > 128 or any(not isinstance(item, dict) for item in progress):
            raise ValueError("Transform collection progress exceeds its bound")
        failures = sorted({item.get("kind") for item in progress if item.get("outcome") == "incomplete"
                           and isinstance(item.get("kind"), str) and item["kind"] in COLLECTION_ACTIONS})
        collection_diagnostics = [{"code": "transform-collection:" + kind, "next_action": COLLECTION_ACTIONS[kind]}
                                  for kind in failures]
        result["diagnostics"].extend(collection_diagnostics)
        result["unresolved"].extend(item["code"] for item in collection_diagnostics)

    def records(values, section):
        if not isinstance(values, list) or len(values) > 1024:
            raise ValueError("Qualification records must be bounded lists")
        output = []
        seen = set()
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                raise ValueError("Qualification record must be an object")
            identifier = label(item.get("id"))
            if identifier in seen or item.get("outcome") not in OUTCOMES:
                raise ValueError("Qualification records require unique IDs and explicit outcomes")
            seen.add(identifier)
            for key, value in expected.items():
                if key in item and str(item[key]) != str(value):
                    raise ValueError("Qualification record belongs to another release")
            record = {"id": identifier, "outcome": item["outcome"],
                      "evidence_verified": item.get("evidence_verified") is True,
                      "evidence_ref": f"sha256:{digest}#/{section}/{index}"}
            if "observation" in item:
                record["observation"] = label(item["observation"])
            if "verification_layer" in item:
                record["verification_layer"] = label(item["verification_layer"])
            output.append(record)
        return output

    for section in SECTIONS:
        result[section] = records(raw.get(section, []), section)
        if not result[section]:
            result["unresolved"].append(section + ":not-observed")
    if identity["service"] == "devops":
        portability = raw.get("portability") or {}
        result["portability"] = {"assertions": records(portability.get("assertions", []), "portability/assertions")}
        if not result["portability"]["assertions"]:
            result["unresolved"].append("portability:not-observed")
    if identity["service"] != "security" and not result.get("requirements_revision"):
        result["unresolved"].append("requirements-revision:not-observed")
    cleanup = raw.get("cleanup") or {}
    outcome = cleanup.get("outcome")
    result["cleanup"] = {"outcome": outcome if outcome in OUTCOMES | {"not_required"} else "incomplete"}
    return result


def complete(report):
    rows = [row for section in SECTIONS for row in report[section]]
    rows += report.get("portability", {}).get("assertions", [])
    return (not report["unresolved"] and report["cleanup"]["outcome"] == "passed"
            and all(row["outcome"] == "passed" and row["evidence_verified"] for row in rows))


def load_lifecycle():
    spec = importlib.util.spec_from_file_location("l5_publication_lifecycle", Path(__file__).with_name("worker-lifecycle.py"))
    lifecycle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lifecycle)
    return lifecycle


def installed_revision(environment, identity, target):
    """Prove manifest-to-installed-code provenance through fresh AWS readbacks."""
    lifecycle = load_lifecycle()
    verify_principal(environment, lifecycle.aws("sts", "get-caller-identity"))
    role = environment.get("L5_MUTATION_ROLE_ARN") or environment["L5_RELEASE_ROLE_ARN"]
    if (target["account_id"] != role.split(":")[4]
            or target["region"] != environment["AWS_REGION"]):
        raise ValueError("Installed runtime differs from the authorized release target")
    _, _, code_hash = lifecycle.manifest_for(environment["RELEASE_BUCKET"], identity["source_revision"],
        environment["WHEEL_DIGEST"], environment["MANIFEST_VERSION"])
    if code_hash != base64.b64encode(bytes.fromhex(environment["ARCHIVE_DIGEST"])).decode():
        raise ValueError("Published archive differs from the verified release manifest")
    worker, repair = lifecycle.functions(identity["environment"], identity["service"])
    mapping = lifecycle.mapping(worker, identity["environment"], target["account_id"], identity["service"])
    observed = lifecycle.state(identity["environment"], worker, repair, mapping)
    lifecycle.scope_readback(identity["environment"], target["account_id"], worker, repair, observed, identity["service"])
    names = lifecycle.auxiliary_names(identity["environment"], identity["service"])
    auxiliary = lifecycle.auxiliary_state(names, observed, target["account_id"])
    functions = [observed["worker"], observed["repair"], *(value["function"] for value in auxiliary.values())]
    if any(fn.get("CodeSha256") != code_hash or fn.get("State") != "Active"
           or fn.get("LastUpdateStatus") != "Successful" for fn in functions):
        raise ValueError("Installed service does not match the verified source archive")
    lifecycle.verify_runtime(worker, repair, identity["service"], names)
    # Source is established by the immutable manifest and matching installed bytes.
    return identity["source_revision"]


def delivery_evidence(report, identity, target, actual_revision, produced_at):
    from src.orchestration.delivery_contract import WorkflowDeliveryEvidence

    if not complete(report) or not SHA.fullmatch(actual_revision) or actual_revision != identity["source_revision"]:
        raise ValueError("Incomplete qualification or unverified installed revision")
    if any(row.get("observation") != "independent-live" for row in report["live_checks"]):
        raise ValueError("Delivery requires independent native observations")
    document = {"schema_version": 2, "contract_digest": identity["contract_digest"],
        "correlation": identity["correlation"],
        **{key: identity[key] for key in ("repository_id", "run_id", "run_attempt", "source_revision")},
        "workflow_path": WORKFLOW, "workflow_revision": identity["definition_revision"],
        "actual_revision": actual_revision,
        **{key: target[key] for key in ("account_id", "region", "resource_kind", "resource_id")},
        "component": "l5-" + identity["service"].replace("_", "-"),
        "checks": [{key: row[key] for key in ("id", "outcome", "evidence_ref")} for row in report["live_checks"]],
        "cleanup": report["cleanup"]["outcome"], "produced_at": produced_at}
    return WorkflowDeliveryEvidence.model_validate(document).model_dump(mode="json")


def write_new(path, document):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(p.is_symlink() for p in (path, *path.parents)) or path.parent.stat().st_mode & 0o077:
        raise ValueError("Publication staging must remain private")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as destination:
        json.dump(document, destination, sort_keys=True, indent=2)
        destination.write("\n")


def main(environment=None):
    environment = os.environ if environment is None else environment
    identity = release_identity(environment)
    root = Path(environment["RUNNER_TEMP"])
    if (not root.is_absolute() or any(p.is_symlink() for p in (root, *root.parents))
            or root.resolve().is_relative_to(Path(__file__).resolve().parents[3])):
        raise ValueError("Publication staging must be outside the checkout")
    service = identity["service"].replace("_", "-")
    source = (root / "l5-security-qualification/qualification.json" if service == "security"
              else Path(environment["PRIVATE_EVIDENCE_DIR"]) / "qualification.json")
    if service == "devops":
        fixture, _ = private_json(environment["PRIVATE_FIXTURE"])
        run_id = fixture["authorization"]["run_id"]
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id):
            raise ValueError("DevOps qualification run must be a single path component")
        source = Path(environment["PRIVATE_EVIDENCE_DIR"]) / run_id / "l5-devops-qualification/qualification.json"
    raw, digest = private_json(source)
    report = normalized_report(raw, identity, digest)
    if service == "transform" and raw.get("qualification_scope") == "transform_release_acceptance":
        from qualification_transform_acceptance import validate_journal

        validate_journal(raw, source.parent, digest)
    stage = root / "l5-published-reports"
    write_new(stage / f"l5-{service}-qualification/qualification.json", report)
    output_flag(environment, "service", service)
    output_flag(environment, "qualification")
    if not complete(report):
        for issue in report.get("diagnostics", []):
            print(f"{issue['code']}: {issue['next_action']}")
        raise ValueError("Native qualification remains incomplete; diagnostic metadata staged")
    if identity["correlation"] is not None:
        target = authorize_engine(environment, identity, target=True)
        actual = installed_revision(environment, identity, target)
        delivery = delivery_evidence(report, identity, target, actual, datetime.now(UTC))
        write_new(stage / "delivery-evidence/delivery-evidence.json", delivery)
        output_flag(environment, "delivery")
    print("Bounded qualification metadata staged; original observations remain private")


def output_flag(environment, name, value="staged"):
    if environment.get("GITHUB_OUTPUT"):
        with open(environment["GITHUB_OUTPUT"], "a") as output:
            output.write(name + "=" + value + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("L5 evidence publication incomplete; inspect the private service report") from None
