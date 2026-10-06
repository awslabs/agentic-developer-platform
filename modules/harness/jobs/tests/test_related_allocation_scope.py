"""Related cleanup authority remains bound to the current immutable approval."""

from types import SimpleNamespace

import pytest

from harness_jobs.identity import ContractViolation, OperationRequest
from harness_jobs.inventory import InventoryAuthority


def authority(allocation):
    return InventoryAuthority(
        connect=lambda: None,
        authenticate=lambda _: None,
        related_allocation_id=allocation,
    )


def record(action="teardown", related='["control"]'):
    request = OperationRequest(
        action=action,
        idempotency_key="scope-test",
        parameters={"allocation_id": "original", "cleanup_allocation_ids": related},
    )
    return SimpleNamespace(admitted_request=lambda: request)


def test_default_allocation_scope_is_unchanged():
    assert authority(None).approved_allocation(record()) == "original"


def test_exact_related_allocation_is_explicitly_approved():
    assert authority("control").approved_allocation(record()) == "control"


@pytest.mark.parametrize(
    "related",
    [
        "[]",
        '["different"]',
        '["original","control"]',
        '["control","control"]',
        "null",
        "{}",
        "bad-json",
    ],
)
def test_missing_or_ambiguous_related_scope_is_refused(related):
    with pytest.raises(ContractViolation):
        authority("control").approved_allocation(record(related=related))


def test_related_authority_cannot_create():
    with pytest.raises(ContractViolation):
        authority("control").approved_allocation(record(action="provision"))
