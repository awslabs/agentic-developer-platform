"""Select one published Product release without confusing manual runs with engine delivery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path

from tests.e2e.product_onboarding.product_completion_inventory import CASES, DEFERRED_ISSUES
from tests.e2e.product_onboarding.product_completion_release import (
    COMPONENTS,
    CORRELATION,
    DIGEST,
    SHA,
)
from tests.e2e.product_onboarding.product_completion_runner import ISSUES

RELEASE_CHECKS = (
    "release-manifest",
    "gateway-runtime",
    "frontend-runtime",
    "installed-cli",
    "release-health",
)
REGRESSION_CHECKS = tuple(
    f"issue-{issue}-{criterion.lower()}"
    for issue in ISSUES
    if issue not in DEFERRED_ISSUES
    for criterion in CASES[issue]
) + ("baseline-regression", "repeatability", "cleanup", "evidence-index")


def required_checks():
    if (
        set(CASES) != set(ISSUES)
        or any(
            set(CASES[issue])
            != {f"AC-{index:02}" for index in range(1, 5 if issue == 8276 else 4)}
            for issue in ISSUES
        )
        or len(REGRESSION_CHECKS) != len(set(REGRESSION_CHECKS))
    ):
        raise ValueError(
            "Product regression check inventory differs from the original stories"
        )
    return REGRESSION_CHECKS


def release_artifact(release_id):
    match = re.fullmatch(r"product-([1-9][0-9]{0,18})-([1-9][0-9]{0,5})", release_id)
    if match is None:
        raise ValueError(
            "Product release identifier is not an artifact run and attempt"
        )
    return match.group(1), f"product-profile-release-{match.group(1)}-{match.group(2)}"


def _private(path):
    file = Path(path)
    if file.is_symlink() or not file.is_file():
        raise ValueError("Regression evidence is missing or symlinked")
    details = file.stat()
    if (
        details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
        or not 0 < details.st_size <= 1024 * 1024
    ):
        raise ValueError("Regression evidence must be private and bounded")
    return file


def _identity(environment, workflow):
    if (
        environment.get("GITHUB_REPOSITORY") != "aws-e/adp"
        or environment.get("GITHUB_REF") != "refs/heads/main"
        or environment.get("GITHUB_WORKFLOW_REF")
        != f"aws-e/adp/.github/workflows/{workflow}.yml@refs/heads/main"
    ):
        raise ValueError("Regression must run from its reviewed main workflow")
    for key in ("GITHUB_REPOSITORY_ID", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        value = environment.get(key, "")
        if not value.isdecimal() or int(value) < 1:
            raise ValueError("Regression run identity is incomplete")


def engine_workflow(environment):
    reference = environment.get("GITHUB_WORKFLOW_REF", "")
    for workflow in ("product-profile-release", "product-profile-regression"):
        if reference == f"aws-e/adp/.github/workflows/{workflow}.yml@refs/heads/main":
            return workflow
    raise ValueError("Product delivery must run from its reviewed main workflow")


def engine_identity(environment):
    _identity(environment, engine_workflow(environment))
    if environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise ValueError("Engine regression needs its dispatched run")
    source = environment.get("ADP_SOURCE_REVISION", "")
    definition = environment.get("ADP_DEFINITION_REVISION", "")
    correlation = environment.get("ADP_CORRELATION", "")
    digest = environment.get("ADP_CONTRACT_DIGEST", "")
    if (
        not SHA.fullmatch(source)
        or source != environment.get("ADP_CHECKOUT_REVISION", environment.get("GITHUB_SHA"))
        or not SHA.fullmatch(definition)
        or definition != environment.get("GITHUB_WORKFLOW_SHA")
        or not CORRELATION.fullmatch(correlation)
        or not DIGEST.fullmatch(digest)
        or environment.get("PRODUCT_ENVIRONMENT") != "dev"
        or environment.get("PRODUCT_SUITE") != "product-profile"
        or environment.get("PRODUCT_MAX_PARALLEL") != "8"
    ):
        raise ValueError("Engine regression differs from its pinned dispatch")
    return {
        "run_id": int(environment["GITHUB_RUN_ID"]),
        "run_attempt": int(environment["GITHUB_RUN_ATTEMPT"]),
        "source_revision": source,
        "workflow_revision": definition,
        "contract_digest": digest,
        "correlation": correlation,
        "authorized": True,
    }


def guard_engine(environment, receipt_path):
    expected = engine_identity(environment)
    receipt = json.loads(_private(receipt_path).read_text())
    if receipt != expected:
        raise ValueError("Engine ownership was not verified for this attempt")


def guard_standalone(environment):
    _identity(environment, "product-profile-regression-standalone")
    if environment.get("GITHUB_EVENT_NAME") not in ("workflow_dispatch", "schedule"):
        raise ValueError("Standalone regression needs a named manual or scheduled run")
    if any(
        environment.get(key)
        for key in (
            "ADP_CORRELATION",
            "ADP_SOURCE_REVISION",
            "ADP_DEFINITION_REVISION",
            "ADP_CONTRACT_DIGEST",
        )
    ):
        raise ValueError("Standalone regression cannot claim engine transport")


def select_manifest(path, *, release_id, manifest_sha256, environment):
    file = _private(path)
    if (
        not DIGEST.fullmatch(manifest_sha256)
        or hashlib.sha256(file.read_bytes()).hexdigest() != manifest_sha256
    ):
        raise ValueError(
            "Named release manifest digest differs from the published artifact"
        )
    manifest = json.loads(file.read_text())
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("release_id") != release_id
        or manifest.get("release_id")
        != f"product-{manifest.get('run_id')}-{manifest.get('run_attempt')}"
        or manifest.get("repository") != environment.get("GITHUB_REPOSITORY")
        or manifest.get("repository_id") != int(environment["GITHUB_REPOSITORY_ID"])
        or manifest.get("environment") != "dev"
        or not isinstance(manifest.get("source_revision"), str)
        or not SHA.fullmatch(manifest["source_revision"])
        or not isinstance(manifest.get("components"), dict)
        or set(manifest["components"]) != set(COMPONENTS)
        or any(
            not isinstance(item, dict) or item.get("verified") is not True
            for item in manifest["components"].values()
        )
    ):
        raise ValueError("Named release does not cover the verified Product components")
    return manifest


def main():
    parser = argparse.ArgumentParser(
        description="Validate a named Product regression attempt"
    )
    parser.add_argument("mode", choices=("engine", "standalone"))
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "engine":
        if args.authorization is None:
            parser.error("engine regression needs an authenticated ownership receipt")
        guard_engine(os.environ, args.authorization)
    else:
        if args.authorization is not None:
            parser.error("standalone regression cannot use an engine ownership receipt")
        guard_standalone(os.environ)
    manifest = select_manifest(
        args.manifest,
        release_id=args.release_id,
        manifest_sha256=args.manifest_sha256,
        environment=os.environ,
    )
    output = {
        "release_id": manifest["release_id"],
        "manifest_sha256": args.manifest_sha256,
        "mode": args.mode,
        "required_checks": required_checks(),
    }
    descriptor = os.open(
        args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as file:
        json.dump(output, file, sort_keys=True)


if __name__ == "__main__":
    main()
