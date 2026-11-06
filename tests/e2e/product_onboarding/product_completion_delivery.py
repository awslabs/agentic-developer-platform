"""Publish a Product delivery artifact from one verified live inventory attempt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from tests.e2e.product_onboarding.product_completion_regression import required_checks
from tests.e2e.product_onboarding.product_completion_runner import (
    required_cases,
)


def _create(path, content):
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as destination:
        destination.write(json.dumps(content, sort_keys=True, separators=(",", ":")))


def publish_inventory(evidence_root, *, context, contract, component):
    """Execute the real runner, then emit only complete, owned attempt evidence."""
    from src.orchestration.delivery_contract import (
        WorkflowDeliveryEvidence,
        verify_delivery_evidence,
    )

    root = Path(evidence_root)
    cases = required_cases()
    workflow_identity = {
        "GITHUB_REPOSITORY_ID": str(context.repository_id),
        "GITHUB_RUN_ID": str(context.run_id),
        "GITHUB_RUN_ATTEMPT": str(context.run_attempt),
        "PRODUCT_BROWSER_WORKFLOW_REVISION": context.workflow_revision,
        "PRODUCT_CORRELATION": context.correlation,
        "ADP_SOURCE_REVISION": context.source_revision,
        "ADP_DEFINITION_REVISION": context.workflow_revision,
        "ADP_CORRELATION": context.correlation,
        "GITHUB_WORKFLOW_REF": (
            f"{os.environ.get('GITHUB_REPOSITORY')}/{context.workflow_path}"
            "@refs/heads/main"
        ),
    }
    if any(os.environ.get(key) != value for key, value in workflow_identity.items()):
        raise ValueError("Product delivery context differs from the running workflow")
    if not context.correlation:
        raise ValueError("Product delivery needs a correlated workflow attempt")
    if (
        not root.is_absolute()
        or root.is_symlink()
        or not root.is_dir()
        or root.stat().st_uid != os.getuid()
        or stat.S_IMODE(root.stat().st_mode) != 0o700
    ):
        raise ValueError("Product delivery needs a private attempt directory")
    # The live fixtures expect the immutable named release, whose source revision
    # legitimately precedes this engine attempt's reviewed source. Bind
    # PRODUCT_EXPECTED_REVISION to that digest-verified release and prove the
    # release is contained in this source's lineage, rather than demanding that
    # the fixture and engine revisions are identical.
    from tests.e2e.product_onboarding.product_completion_attempt import load
    from tests.e2e.product_onboarding.product_completion_regression import (
        select_manifest,
    )
    from tests.e2e.product_onboarding.product_shared_release import (
        verify_shared_source_lineage,
    )

    selected = load(root, root / "required-checks.json")
    manifest = select_manifest(
        root / "release-manifest.json",
        release_id=selected["release_id"],
        manifest_sha256=selected["manifest_sha256"],
        environment=os.environ,
    )
    if (
        os.environ.get("PRODUCT_EXPECTED_REVISION") != manifest["source_revision"]
        or not verify_shared_source_lineage(manifest, os.environ)
    ):
        raise ValueError("Product fixtures are not bound to the selected release")
    if (
        set(contract.required_checks) != set(required_checks())
        or set(cases) != set(required_checks()) - {"baseline-regression", "repeatability", "cleanup", "evidence-index"}
        or not contract.cleanup_required
        or context.inputs.get("adp_contract_digest") != contract.digest
        or (context.resource_kind, context.resource_id)
        != (contract.resource_kind, contract.resource_id)
    ):
        raise ValueError("Product delivery contract differs from the pinned attempt")
    from tests.e2e.product_onboarding.product_completion_context import (
        verify_contract_pins,
    )

    verify_contract_pins(
        contract,
        os.environ,
        authorization=root / "engine-authorization.json",
    )
    destination = root / "delivery.json"
    if destination.exists() or destination.is_symlink():
        raise ValueError("Delivery evidence already exists for this attempt")

    from tests.e2e.product_onboarding.product_completion_attempt import execute_attempt

    checks = execute_attempt(root, os.environ)
    if {check["id"] for check in checks} != set(required_checks()):
        raise ValueError("Product delivery has missing or unexpected checks")

    artifact = WorkflowDeliveryEvidence(
        **context.model_dump(exclude={"schema_version", "inputs"}),
        schema_version=2,
        contract_digest=contract.digest,
        actual_revision=context.source_revision,
        component=component,
        checks=checks,
        cleanup="passed",
        produced_at=datetime.now(UTC),
    )
    verify_delivery_evidence(
        artifact,
        contract=contract,
        context=context,
        component=component,
        digest=hashlib.sha256(artifact.model_dump_json().encode()).hexdigest(),
        reference="evidence/delivery.json",
    )
    _create(destination, artifact.model_dump(mode="json"))
    return destination


def main():
    from src.orchestration.delivery_contract import DeliveryContract
    from src.orchestration.deployment_workflow_provider import WorkflowContext

    parser = argparse.ArgumentParser(
        description="Run and publish pinned Product regression evidence"
    )
    parser.add_argument("attempt", type=Path)
    parser.add_argument("context", type=Path)
    parser.add_argument("contract", type=Path)
    parser.add_argument("component")
    args = parser.parse_args()
    context = WorkflowContext.model_validate_json(args.context.read_text())
    contract = DeliveryContract.model_validate_json(args.contract.read_text())
    print(
        publish_inventory(
            args.attempt, context=context, contract=contract, component=args.component
        )
    )


if __name__ == "__main__":
    main()
