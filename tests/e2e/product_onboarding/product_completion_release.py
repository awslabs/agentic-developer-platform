"""Bind observed Product components to one immutable shared release manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path

COMPONENTS = (
    "gateway-backend",
    "gateway-frontend",
    "installed-cli",
    "schema",
)
# Explicit release scope, retained in the evidence rather than reported as passes.
DEFERRED_INTEGRATIONS = {
    "context-l5": {"issue": 8266, "status": "deferred", "live_acceptance": False},
    "engineering-insights": {"issue": 8276, "status": "deferred", "live_acceptance": False},
}
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
CORRELATION = re.compile(
    r"(?:[0-9a-f]{64}|orch:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\Z"
)


def _evidence_ref(value):
    return (
        isinstance(value, str)
        and len(value) <= 512
        and re.fullmatch(r"evidence/[A-Za-z0-9._/-]+", value)
        and all(segment not in ("", ".", "..") for segment in value.split("/"))
    )


def _private(path):
    file = Path(path)
    if file.is_symlink() or not file.is_file():
        raise ValueError("Release receipt is missing or symlinked")
    details = file.stat()
    if (
        details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
        or not 0 < details.st_size <= 1024 * 1024
    ):
        raise ValueError("Release receipt is not private and bounded")
    return json.loads(
        file.read_text(),
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON")),
    )


def release_identity(environment):
    if (
        environment.get("PRODUCT_ENVIRONMENT") != "dev"
        or environment.get("PRODUCT_SUITE") != "product-profile"
        or environment.get("PRODUCT_MAX_PARALLEL") != "8"
    ):
        raise ValueError("Product release business inputs must be fixed")
    source = environment.get("ADP_SOURCE_REVISION", "")
    workflow = environment.get("ADP_DEFINITION_REVISION", "")
    correlation = environment.get("ADP_CORRELATION", "")
    contract = environment.get("ADP_CONTRACT_DIGEST", "")
    if (
        not SHA.fullmatch(source)
        or not SHA.fullmatch(workflow)
        or not DIGEST.fullmatch(contract)
        or not CORRELATION.fullmatch(correlation)
        or workflow != environment.get("GITHUB_WORKFLOW_SHA")
        or source != environment.get("ADP_CHECKOUT_REVISION", environment.get("GITHUB_SHA"))
    ):
        raise ValueError("Product release transport differs from the pinned workflow")
    repository = environment.get("GITHUB_REPOSITORY")
    if repository != "aws-e/adp" or environment.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("Product release requires the reviewed main repository")
    identity = {
        "repository": repository,
        "source_revision": source,
        "workflow_revision": workflow,
        "contract_digest": contract,
        "correlation": correlation,
        "environment": "dev",
        "suite": "product-profile",
        "max_parallel": 8,
    }
    for key, variable in (
        ("repository_id", "GITHUB_REPOSITORY_ID"),
        ("run_id", "GITHUB_RUN_ID"),
        ("run_attempt", "GITHUB_RUN_ATTEMPT"),
    ):
        value = environment.get(variable, "")
        if not value.isdecimal() or int(value) < 1:
            raise ValueError("Product release requires a positive workflow identity")
        identity[key] = int(value)
    identity["release_id"] = f"product-{identity['run_id']}-{identity['run_attempt']}"
    identity["workflow_path"] = ".github/workflows/product-profile-release.yml"
    return identity


def plan_actions(desired, current, environment):
    source = release_identity(environment)["source_revision"]
    if (
        not isinstance(desired, dict)
        or set(desired) != set(COMPONENTS)
        or not isinstance(current, dict)
        or not set(current) <= set(COMPONENTS)
    ):
        raise ValueError("Product release candidates are incomplete")
    actions = {}
    for name in COMPONENTS:
        candidate = desired[name]
        observed = current.get(name)
        if (
            not isinstance(candidate, dict)
            or set(candidate) != {"sha256", "fingerprint", "source_revision"}
            or candidate["source_revision"] != source
            or not all(
                isinstance(candidate[key], str) and DIGEST.fullmatch(candidate[key])
                for key in ("sha256", "fingerprint")
            )
        ):
            raise ValueError(f"{name} has no pinned candidate artifact")
        if observed is not None and (
            not isinstance(observed, dict)
            or set(observed)
            != {"sha256", "fingerprint", "source_revision", "verified", "evidence_ref"}
            or not all(
                isinstance(observed[key], str) and DIGEST.fullmatch(observed[key])
                for key in ("sha256", "fingerprint")
            )
            or not isinstance(observed["source_revision"], str)
            or not SHA.fullmatch(observed["source_revision"])
            or observed["verified"] is not True
            or not _evidence_ref(observed["evidence_ref"])
        ):
            raise ValueError(f"{name} has unverified deployed provenance")
        actions[name] = (
            "reused"
            if observed is not None
            and candidate["sha256"] == observed["sha256"]
            and candidate["fingerprint"] == observed["fingerprint"]
            else "deployed"
        )
    return actions


def build_manifest(receipts, environment, desired, current):
    identity = release_identity(environment)
    actions = plan_actions(desired, current, environment)
    if not isinstance(receipts, dict) or set(receipts) != set(COMPONENTS):
        raise ValueError("Product release requires every component observation")
    components = {}
    for name in COMPONENTS:
        receipt = receipts[name]
        if not isinstance(receipt, dict) or set(receipt) != {
            "desired_sha256",
            "observed_sha256",
            "source_fingerprint",
            "observed_fingerprint",
            "observed_source_revision",
            "evidence_ref",
            "verified",
            "action",
        }:
            raise ValueError(f"{name} has incomplete release provenance")
        if (
            not all(
                isinstance(receipt[key], str) and DIGEST.fullmatch(receipt[key])
                for key in (
                    "desired_sha256",
                    "observed_sha256",
                    "source_fingerprint",
                    "observed_fingerprint",
                )
            )
            or not isinstance(receipt["observed_source_revision"], str)
            or not SHA.fullmatch(receipt["observed_source_revision"])
            or not _evidence_ref(receipt["evidence_ref"])
            or receipt["verified"] is not True
            or receipt["action"] != actions[name]
            or receipt["desired_sha256"] != receipt["observed_sha256"]
            or receipt["desired_sha256"] != desired[name]["sha256"]
            or receipt["source_fingerprint"] != receipt["observed_fingerprint"]
            or receipt["source_fingerprint"] != desired[name]["fingerprint"]
            or (
                receipt["action"] == "reused"
                and (
                    receipt["observed_source_revision"]
                    != current[name]["source_revision"]
                    or receipt["evidence_ref"] != current[name]["evidence_ref"]
                )
            )
            or (
                receipt["action"] == "deployed"
                and receipt["observed_source_revision"] != identity["source_revision"]
            )
        ):
            raise ValueError(f"{name} is not a verified matching artifact")
        components[name] = receipt
    return {"schema_version": 1, **identity, "components": components,
            "deferred_integrations": DEFERRED_INTEGRATIONS}


def _write(document, destination):
    destination = Path(destination)
    if (
        not destination.is_absolute()
        or destination.parent.is_symlink()
        or not destination.parent.is_dir()
    ):
        raise ValueError("Release manifest needs an existing private directory")
    if (
        destination.parent.stat().st_uid != os.getuid()
        or stat.S_IMODE(destination.parent.stat().st_mode) != 0o700
    ):
        raise ValueError("Release manifest directory is not private")
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    descriptor = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as file:
        file.write(payload)
    return hashlib.sha256(payload).hexdigest()


def write_plan(desired_path, current_path, plan_path, *, environment=os.environ):
    identity = release_identity(environment)
    actions = plan_actions(_private(desired_path), _private(current_path), environment)
    return _write({"identity": identity, "actions": actions}, plan_path)


def publish(
    receipt_path,
    manifest_path,
    *,
    desired_path,
    current_path,
    plan_path,
    environment=os.environ,
):
    desired, current = _private(desired_path), _private(current_path)
    expected_plan = {
        "identity": release_identity(environment),
        "actions": plan_actions(desired, current, environment),
    }
    if _private(plan_path) != expected_plan:
        raise ValueError("Release plan differs from the observed attempt")
    manifest = build_manifest(_private(receipt_path), environment, desired, current)
    return _write(manifest, manifest_path)


def main():
    parser = argparse.ArgumentParser(
        description="Publish the observed Product release manifest"
    )
    parser.add_argument("--desired", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--receipts", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.receipts is None:
        if arguments.output is not None:
            parser.error("--output requires --receipts")
        write_plan(arguments.desired, arguments.current, arguments.plan)
        return
    if arguments.output is None:
        parser.error("--receipts requires --output")
    digest = publish(
        arguments.receipts,
        arguments.output,
        desired_path=arguments.desired,
        current_path=arguments.current,
        plan_path=arguments.plan,
    )
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"manifest_sha256={digest}\n")
        output.write(f"release_id={release_identity(os.environ)['release_id']}\n")


if __name__ == "__main__":
    main()
