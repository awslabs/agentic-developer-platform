"""Publish the provider-bound workflow context from the accepted Product contract."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import subprocess
from pathlib import Path

from src.orchestration.delivery_contract import DeliveryContract
from src.orchestration.deployment_workflow_provider import WorkflowContext

from tests.e2e.product_onboarding.product_completion_regression import (
    engine_workflow,
    guard_engine,
)
from tests.e2e.product_onboarding.product_completion_release import _write

SOURCE_ROOT = Path(__file__).resolve().parents[3]


def verify_contract_pins(contract, environment, *, authorization=None):
    """A baseline may precede the merge; the reviewed workflow bytes must match.

    An authenticated engine receipt already approved the effective source and
    definition pins of this very run: ``engine_identity`` binds them to
    ``GITHUB_SHA``/``GITHUB_WORKFLOW_SHA`` and the gateway job check accepted
    them. An accepted repair merged after the contract was recorded therefore
    advances the definition legitimately, and re-comparing the running
    workflow bytes against the stale baseline would reject it. Without a
    receipt the supplied pins are unauthenticated, so the reviewed workflow
    bytes must still match the accepted definition exactly.
    """
    if authorization is not None:
        guard_engine(environment, authorization)
        if environment["ADP_CONTRACT_DIGEST"] != contract.digest:
            raise ValueError("Product authorization belongs to another contract")
    source = environment["ADP_SOURCE_REVISION"]
    definition = environment["ADP_DEFINITION_REVISION"]
    if source != contract.baseline_revision:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", contract.baseline_revision, source],
            cwd=SOURCE_ROOT,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError("Product source does not contain the accepted baseline")
    if authorization is not None:
        return
    if definition != contract.definition_revision:
        path = ".github/workflows/" + engine_workflow(environment) + ".yml"
        expected = subprocess.check_output(
            ["git", "show", contract.definition_revision + ":" + path],
            cwd=SOURCE_ROOT,
            timeout=30,
        )
        observed = subprocess.check_output(
            ["git", "show", definition + ":" + path],
            cwd=SOURCE_ROOT,
            timeout=30,
        )
        if expected != observed:
            accepted_side = []
            for line in difflib.unified_diff(
                expected.decode("utf-8", "replace").splitlines(),
                observed.decode("utf-8", "replace").splitlines(),
                n=0,
                lineterm="",
            ):
                if line.startswith("@@"):
                    accepted_side.append(line)
                elif line.startswith("-") and not line.startswith("---"):
                    body = line[1:]
                    indent = len(body) - len(body.lstrip(" "))
                    accepted_side.append("[%d]%s" % (indent, body.strip()))
            raise ValueError(
                "Product workflow differs from the accepted definition"
                f" (accepted {contract.definition_revision}"
                f" sha {hashlib.sha256(expected).hexdigest()[:12]};"
                f" observed {definition}"
                f" sha {hashlib.sha256(observed).hexdigest()[:12]};"
                " accepted-side lines only, [n]=n leading spaces, ||=newline) "
                + " || ".join(accepted_side)[:3000]
            )


def publish_context(environment, root):
    root = Path(root)
    guard_engine(environment, root / "engine-authorization.json")
    serialized = environment["PRODUCT_ACCEPTED_CONTRACT_JSON"]
    if len(serialized.encode()) > 65536:
        raise ValueError("Product contract exceeds the bounded context input")
    contract = DeliveryContract.model_validate_json(serialized)
    expected_inputs = {
        "environment": "dev",
        "suite": "product-profile",
        "max_parallel": "8",
    }
    region = environment["PRODUCT_TARGET_REGION"]
    if (
        contract.digest != environment["ADP_CONTRACT_DIGEST"]
        or contract.job_mutation_role_arn != environment["PRODUCT_MUTATION_ROLE_ARN"]
        or contract.workflow_inputs != expected_inputs
        or not re.fullmatch(r"[a-z0-9-]{3,64}", region)
    ):
        raise ValueError("Product context differs from the accepted delivery contract")
    verify_contract_pins(
        contract, environment, authorization=root / "engine-authorization.json"
    )
    context = WorkflowContext(
        schema_version=1,
        repository_id=int(environment["GITHUB_REPOSITORY_ID"]),
        run_id=int(environment["GITHUB_RUN_ID"]),
        run_attempt=int(environment["GITHUB_RUN_ATTEMPT"]),
        workflow_path=".github/workflows/" + engine_workflow(environment) + ".yml",
        workflow_revision=environment["ADP_DEFINITION_REVISION"],
        source_revision=environment["ADP_SOURCE_REVISION"],
        account_id=environment["PRODUCT_DELIVERY_ROLE_ARN"].split(":")[4],
        region=region,
        resource_kind=contract.resource_kind,
        resource_id=contract.resource_id,
        inputs={**expected_inputs, "adp_contract_digest": contract.digest},
        correlation=environment["ADP_CORRELATION"],
    )
    _write(contract.model_dump(mode="json"), root / "delivery-contract.json")
    _write(context.model_dump(mode="json"), root / "workflow-context.json")
    _write(context.model_dump(mode="json"), root / "deployment-context.json")
    return context


if __name__ == "__main__":
    publish_context(
        os.environ, Path(os.environ["RUNNER_TEMP"]) / engine_workflow(os.environ)
    )
