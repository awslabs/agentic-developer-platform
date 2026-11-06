"""Bind preliminary Product checks and a held recovery to one automatic release."""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path

from tests.e2e.cli_uplift import ports, statestore

from . import live, product_d01_cases, product_d01_release, product_inputs
from .product_publish import read_input


def identity_key(run_id):
    statestore.product_run_key(run_id)
    return f"{statestore.PREFIX}/product-runs/{run_id}/accepted-release.json"


def preliminary_key(run_id):
    statestore.product_run_key(run_id)
    return f"{statestore.PREFIX}/product-runs/{run_id}/preliminary-grades.json"


def selected_run(run_id, workflow_revision, source_revision):
    selected = live.workflow_run_id()
    if (
        os.environ.get("GITHUB_REF") != "refs/heads/main"
        or run_id != selected
        or workflow_revision != os.environ.get("GITHUB_WORKFLOW_SHA")
        or not re.fullmatch(r"[a-f0-9]{40}", str(source_revision))
    ):
        raise ports.PortError("Automatic Product checks require the same reviewed deployment attempt")


def checked_identity(identity, run_id, source_revision):
    if not isinstance(identity, dict) or set(identity) != {
        "run_id", "run_attempt", "source_revision", "workflow_revision",
        "account_id", "region", "resource_id", "repository_id",
        "image_digest", "release_ref", "release_hash",
    }:
        raise ports.PortError("Automatic Product identity is incomplete")
    selected_run(run_id, identity["workflow_revision"], source_revision)
    if (
        identity["run_id"] != int(os.environ["GITHUB_RUN_ID"])
        or identity["run_attempt"] != int(os.environ["GITHUB_RUN_ATTEMPT"])
        or identity["source_revision"] != source_revision
        or not re.fullmatch(r"[0-9]{12}", str(identity["account_id"]))
        or not isinstance(identity["region"], str) or not identity["region"]
        or not isinstance(identity["resource_id"], str) or not identity["resource_id"]
        or type(identity["repository_id"]) is not int or identity["repository_id"] < 1
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(identity["image_digest"]))
        or not re.fullmatch(r"[a-f0-9]{64}", str(identity["release_hash"]))
        or not isinstance(identity["release_ref"], str)
        or not identity["release_ref"].startswith("s3://")
        or "versionId=" not in identity["release_ref"]
    ):
        raise ports.PortError("Automatic Product identity differs from the selected release")
    return identity


def candidate_identity(native, run_id, source_revision, workflow_revision):
    release = native.get("release_artifact") if isinstance(native, dict) else None
    target = native.get("target") if isinstance(native, dict) else None
    if not isinstance(release, dict) or not isinstance(target, dict):
        raise ports.PortError("Automatic Product checks have no signed release and target")
    selected_run(run_id, workflow_revision, source_revision)
    release_identity = {
        "run_id": release.get("run_id"),
        "run_attempt": release.get("run_attempt"),
        "source_revision": release.get("source_revision"),
        "workflow_revision": release.get("workflow_revision"),
        "account_id": release.get("account_id"),
        "region": release.get("region"),
        "resource_id": release.get("resource_id"),
        "repository_id": release.get("repository_id"),
        "image_digest": release.get("image_digest"),
        "release_ref": native.get("release_ref"),
        "release_hash": native.get("release_hash"),
    }
    if (
        release.get("component") != "gateway-backend"
        or release.get("workflow_path") != ".github/workflows/gateway-deploy.yml"
        or release_identity["run_id"] != int(os.environ["GITHUB_RUN_ID"])
        or release_identity["run_attempt"] != int(os.environ["GITHUB_RUN_ATTEMPT"])
        or release_identity["source_revision"] != source_revision
        or release_identity["workflow_revision"] != workflow_revision
        or not isinstance(release_identity["repository_id"], int)
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(release_identity["image_digest"]))
        or not re.fullmatch(r"[a-f0-9]{64}", str(release_identity["release_hash"]))
        or not isinstance(release_identity["release_ref"], str)
        or not release_identity["release_ref"].startswith("s3://")
        or "versionId=" not in release_identity["release_ref"]
        or any(not value or target.get(field) != value for field, value in release_identity.items()
               if field in ("account_id", "region", "resource_id", "repository_id"))
    ):
        raise ports.PortError("Automatic Product release identity differs from the deployment")
    return checked_identity(release_identity, run_id, source_revision)


def stage(store, inputs_path, dispatch_path, packet_path, release_path, api_path, cli_path, grades_path):
    issues = []
    inputs, dispatch, packet, release, api, cli, grades = (
        read_input(path, label, issues)
        for path, label in (
            (inputs_path, "Product inputs"), (dispatch_path, "Product dispatch"),
            (packet_path, "Product packet"), (release_path, "signed release"),
            (api_path, "Product observation"), (cli_path, "installed CLI"),
            (grades_path, "Product grades"),
        )
    )
    if issues:
        raise ports.PortError("Automatic Product checks lack complete private evidence")
    expected = product_d01_cases.evaluate(inputs, dispatch, packet, release)
    with tempfile.TemporaryDirectory(prefix="product-accepted-release-") as root:
        observed_release = product_d01_release.materialize(dispatch_path, api_path, Path(root) / "release.json")
    run_id = dispatch.get("run_id")
    source = dispatch.get("expected_revision")
    workflow = dispatch.get("workflow_revision")
    native = api.get("native")
    identity = candidate_identity(native, run_id, source, workflow)
    if (
        grades != {**expected, "input_issues": []}
        or expected["cases"]["PR01-C01"]["status"] != "PASSED"
        or expected["cases"]["PR01-C02"]["status"] != "PASSED"
        or any(expected["cases"][case]["status"] != "NOT_RUN" for case in ("PR01-C03", "PR01-C04"))
        or release != observed_release
        or packet.get("deployment") != release.get("deployment")
        or packet.get("identities") != release.get("identities")
        or packet.get("run_id") != run_id
        or inputs.get("run_id") != run_id
        or dispatch.get("selected_cases") != list(product_d01_cases.SCENARIOS)
        or cli.get("workflow_run_id") != run_id
        or product_inputs.installed_cli_surface(cli, release, dispatch, identity["image_digest"]) is None
        or cli.get("workflow_revision") != workflow
        or cli.get("source_revision") != source
    ):
        raise ports.PortError("Automatic Product checks did not qualify the same signed release")
    key = identity_key(run_id)
    record = {"identity": identity, "cleanup_verified": False}
    existing = store._get(key)
    if existing is not None and existing != record:
        raise ports.PortError("Automatic Product evidence belongs to another attempt")
    if existing is None:
        store._put(key, record)
    if store._get(key) != record:
        raise ports.PortError("Automatic Product evidence was not retained")
    grades_key = preliminary_key(run_id)
    preliminary = {"identity": identity, "report": grades}
    previous = store._get(grades_key)
    if previous is not None and previous != preliminary:
        raise ports.PortError("Preliminary Product grades belong to another attempt")
    store._put(grades_key, preliminary)
    if store._get(grades_key) != preliminary:
        raise ports.PortError("Preliminary Product grades were not retained")
    return identity


def complete(store, run_id, source_revision):
    key = identity_key(run_id)
    existing = store._get(key)
    if not isinstance(existing, dict) or not isinstance(existing.get("identity"), dict):
        raise ports.PortError("Automatic Product cleanup has no selected release")
    identity = checked_identity(existing["identity"], run_id, source_revision)
    verified = {"identity": identity, "cleanup_verified": True}
    if existing != verified:
        if existing != {"identity": identity, "cleanup_verified": False}:
            raise ports.PortError("Automatic Product cleanup has a different receipt")
        store._put(key, verified)
    if store._get(key) != verified:
        raise ports.PortError("Automatic Product cleanup was not retained")
    return identity


def require_qualified(store, run_id, source_revision, native=None):
    record = store._get(identity_key(run_id))
    if not isinstance(record, dict) or record.get("cleanup_verified") is not True or not isinstance(record.get("identity"), dict):
        raise ports.PortError("Held Product needs completed original release checks and cleanup")
    checked_identity(record["identity"], run_id, source_revision)
    if native is not None and record["identity"] != candidate_identity(native, run_id, source_revision, record["identity"]["workflow_revision"]):
        raise ports.PortError("Held Product differs from the accepted release or target")
    return record["identity"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("stage", "complete", "verify"))
    parser.add_argument("--expected-revision", required=True)
    for name in ("inputs", "dispatch", "packet", "release", "api", "cli", "grades"):
        parser.add_argument("--" + name)
    args = parser.parse_args(argv)
    if not os.environ.get("CLI_UPLIFT_EVAL_STATE_BUCKET"):
        raise ports.PortError("Automatic Product checks require private durable state")
    aws = ports.AwsPort(os.environ.get("AWS_REGION", "us-east-1"))
    store = statestore.S3StateStore(aws, os.environ["CLI_UPLIFT_EVAL_STATE_BUCKET"],
                                   kms_key_id=os.environ.get("CLI_UPLIFT_EVAL_STATE_KMS_KEY_ID") or None)
    if args.mode == "stage":
        if not all(getattr(args, name) for name in ("inputs", "dispatch", "packet", "release", "api", "cli", "grades")):
            parser.error("Staging Product checks requires all independent receipts")
        return stage(store, args.inputs, args.dispatch, args.packet, args.release, args.api, args.cli, args.grades)
    run_id = live.workflow_run_id()
    if args.mode == "complete":
        return complete(store, run_id, args.expected_revision)
    return require_qualified(store, run_id, args.expected_revision)


if __name__ == "__main__":
    main()
