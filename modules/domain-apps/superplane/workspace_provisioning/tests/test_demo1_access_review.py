"""Acceptance adapter compatibility with the real bootstrap ownership compiler."""

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from harness_jobs.identity import payload_digest

from superplane_acceptance.demo1_retirement import validate_access_review

from . import test_managed_retirement_request as producer_fixtures

composed = producer_fixtures.composed


def test_acceptance_reads_compiled_managed_preparation_without_claiming_cleanup(
    composed,
):
    inventory, plan, _, source, deployment, preparation = composed
    now = datetime.now(UTC)
    selected = SimpleNamespace(
        org_id=inventory.org_id,
        account=inventory.cluster_arn.split(":")[4],
        region=inventory.cluster_arn.split(":")[3],
        deadline=now + timedelta(days=1),
    )
    original = SimpleNamespace(
        workspace_id=inventory.workspace_id,
        retirement_request_id=plan.retirement_request_id,
    )
    envelope = SimpleNamespace(
        max_runtime_seconds=deployment["operation_max_runtime_seconds"]
    )
    review = json.loads(
        json.dumps(
            {
                "retirement_request_id": plan.retirement_request_id,
                "request_id": preparation.idempotency_key,
                "workspace_id": inventory.workspace_id,
                "phase": "prepare-retirement-access",
                "revision": payload_digest(preparation),
                "source_operation_id": source.operation_id,
                "allocation_id": plan.allocation_id,
                "original_allocation_id": plan.original_allocation_id,
                "inventory_sha256": plan.inventory_sha256,
                "access_plan": asdict(plan),
                "max_resource_units": 0,
                "max_cost_micros": 0,
                "admission_available": True,
                "approval_request": {
                    "workspace_id": inventory.workspace_id,
                    "action": preparation.action,
                    "idempotency_key": preparation.idempotency_key,
                    "parameters": dict(preparation.parameters),
                },
            }
        )
    )
    result = validate_access_review(
        review, selected, envelope, original, source.operation_id, now
    )
    assert result["review_status"] == "OBSERVED"
    assert result["status"] == "BLOCKED" and not result["admission_submitted"]
    assert "resource coverage, deletion and cost unproved" in result["reason"]
