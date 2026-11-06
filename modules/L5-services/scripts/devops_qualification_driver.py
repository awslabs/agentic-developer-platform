"""Run DevOps selectors and emit evidence for the shared release dispatcher."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/live/devops"))

from context_selector import authorize_channels, qualify_acceptance, qualify_context, qualify_portability
from journey_driver import private_path, validate_driver
from installed_journeys import validate_runs
from installed_setup import prepare_binding, validate_setup
from qualification import load_manifest
from recovery_selector import authorize_recovery, qualify_recovery
from selector import authorize, qualify_journeys

import qualification_devops
from release_context import release_identity

SELECTORS = {"journeys": qualify_journeys, "recovery": qualify_recovery, "context": qualify_context,
             "portability": qualify_portability, "acceptance": qualify_acceptance}


def emitter_environment(identity):
    return {"SOURCE_SHA": identity["source_revision"], "DEFINITION_SHA": identity["definition_revision"],
            "TARGET_ENVIRONMENT": identity["environment"], "CORRELATION_ID": identity["correlation"] or "",
            "CONTRACT_DIGEST": identity["contract_digest"] or ""}


def preflight(source_sha, manifest_path, evidence_dir, *, observations=None):
    identity = release_identity(os.environ)
    manifest = load_manifest(manifest_path)
    if (identity["service"] != "devops" or identity["source_revision"] != source_sha
            or manifest["release"]["source_sha"] != source_sha
            or manifest.get("authorization", {}).get("source_sha") != source_sha):
        raise ValueError("DevOps qualification differs from the selected release")
    from devops_private_fixture import materialize_clients

    manifest = materialize_clients(manifest, manifest_path)
    for name in SELECTORS:
        authorize(manifest, source_sha, evidence_dir, scenario=name)
    from manual_ui import validate_manual
    manual = validate_manual(manifest)
    if os.environ.get("L5_BROWSER_AUTOMATION", "disabled") != "allowed" and manual is None:
        raise ValueError("devops_human_evidence_required: select original human observers and receipt; do not enable browser automation")
    if manual is not None and (not identity["correlation"] or not identity["contract_digest"]):
        raise ValueError("DevOps manual evidence requires the original engine-owned release")
    validate_driver(manifest)
    validate_setup(manifest)
    authorize_recovery(manifest, source_sha, evidence_dir)
    for name in ("context", "portability", "acceptance"):
        authorize_channels(manifest, source_sha, evidence_dir, name)
    if observations is not None:
        observations = private_path(observations)
    if any(os.environ.get(key, expected) != expected for key, expected in emitter_environment(identity).items()):
        raise ValueError("DevOps evidence identity differs from the selected release")
    return identity, manifest


def run_all(source_sha, manifest_path, evidence_dir, *, observations=None):
    identity, manifest = preflight(source_sha, manifest_path, evidence_dir, observations=observations)
    os.environ.update(emitter_environment(identity))
    run_id = manifest["authorization"]["run_id"]
    directory = Path(evidence_dir) / run_id
    directory.mkdir(mode=0o700)
    write_private = qualification_devops.shared_rollup().write_private
    if "installed_journeys" in manifest:
        manifest["installed_journeys"].pop("runs", None)
    snapshot = directory / "manifest.json"
    write_private(snapshot, manifest)
    release = directory / "release.json"
    write_private(release, manifest["release"])
    failures = []
    if "installed_setup" in manifest:
        try:
            manifest = prepare_binding(manifest, directory)
            snapshot = directory / "manifest-setup.json"
            write_private(snapshot, manifest)
        except Exception as error:
            failures.append({"selector": "setup", "error_type": type(error).__name__})
            write_private(directory / "driver.json", {"release_identity": identity, "run_id": run_id,
                                                     "attempted_selectors": [], "failures": failures})
            emit_evidence(directory, observations=observations)
            return 1
    for name, selector in SELECTORS.items():
        try:
            if selector(source_sha, str(snapshot), str(directory)) != run_id:
                raise ValueError("selector returned another qualification run")
            if name == "journeys" and "installed_journeys" in manifest:
                result = json.loads((directory / f"devops-{run_id}-journeys.json").read_text())
                if result.get("authorization_run_id") != run_id:
                    raise ValueError("installed journey evidence belongs to another attempt")
                manifest["installed_journeys"]["runs"] = validate_runs({
                    entry: value["run_id"] for entry, value in result["driven_journeys"].items()})
                snapshot = directory / "manifest-observed.json"
                write_private(snapshot, manifest)
        except Exception as error:
            failure = {"selector": name, "error_type": type(error).__name__}
            from manual_ui import ManualEvidencePending
            if isinstance(error, ManualEvidencePending):
                failure.update(code=error.code, next_action=error.next_action)
            failures.append(failure)
            write_private(directory / f"{name}.json", {"status": "failed", "run_id": run_id})
    write_private(directory / "driver.json", {"release_identity": identity, "run_id": run_id,
                                             "attempted_selectors": list(SELECTORS), "failures": failures})
    status = emit_evidence(directory, observations=observations)
    return 1 if failures else status


def emit_evidence(directory, *, observations=None):
    output_dir = directory / qualification_devops.EVIDENCE_NAME
    output_dir.mkdir(mode=0o700, exist_ok=True)
    if output_dir.is_symlink() or output_dir.stat().st_mode & 0o077:
        raise ValueError("DevOps evidence output must remain private")
    arguments = ["--release", str(directory / "release.json"), "--evidence", str(directory),
                 "--output", str(output_dir / "qualification.json"),
                 "--delivery-evidence", str(directory / "delivery-evidence.json")]
    if observations is not None:
        arguments.extend(["--observations", str(observations)])
    return qualification_devops.main(arguments)


def emit_saved(source_sha, manifest_path, evidence_dir, *, observations=None):
    identity, manifest = preflight(source_sha, manifest_path, evidence_dir, observations=observations)
    directory = Path(evidence_dir) / manifest["authorization"]["run_id"]
    if directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o077:
        raise ValueError("DevOps attempt evidence must remain private")
    driver = json.loads(private_path(str(directory / "driver.json")).read_text())
    release = json.loads(private_path(str(directory / "release.json")).read_text())
    original = json.loads(private_path(str(directory / "manifest.json")).read_text())
    if "installed_journeys" in manifest:
        manifest["installed_journeys"].pop("runs", None)
    if (driver.get("release_identity") != identity or driver.get("run_id") != manifest["authorization"]["run_id"]
            or driver.get("attempted_selectors") != list(SELECTORS) or driver.get("failures") != []
            or release != manifest["release"] or original != manifest):
        raise ValueError("DevOps driver evidence does not belong to the successful selected attempt")
    os.environ.update(emitter_environment(identity))
    return emit_evidence(directory, observations=observations)
