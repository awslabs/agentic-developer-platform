"""Nested reference and financial bounds in the Project configuration contract."""

import pytest
from pydantic import ValidationError

from src.products.configuration import ProjectConfiguration

REFERENCE = {
    "tenant_id": "fixture-tenant",
    "project_id": "fixture-project",
    "source_connection_id": "fixture-source",
    "record_type": "document",
    "record_id": "fixture-record",
    "record_revision": "1",
}
ALLOCATION = {
    "id": "fixture-allocation",
    "name": "Test budget",
    "target_ref": REFERENCE,
    "amount": {"amount": "2500.00", "currency": "USD"},
    "category": "ai",
    "period_start": "2026-01-01",
    "period_end": "2026-12-31",
    "approval_status": "proposed",
    "owner_ref": {"kind": "user", "id": "fixture-user"},
}


@pytest.mark.parametrize(
    "reference",
    [
        {**REFERENCE, "record_revision": "latest"},
        {**REFERENCE, "record_revision": "null"},
        {**REFERENCE, "record_revision": 1},
        {**REFERENCE, "record_revision": "1" * 513},
        {**REFERENCE, "project_id": " padded-id "},
        {**REFERENCE, "record_id": ""},
        {**REFERENCE, "record_id": "x" * 513},
        {**REFERENCE, "tenant_id": "x" * 256},
        {**REFERENCE, "record_type": " "},
        {**REFERENCE, "unexpected": True},
    ],
)
def test_invalid_configuration_reference(reference):
    with pytest.raises(ValidationError):
        ProjectConfiguration.model_validate({"basic": {"repositories": [reference]}})


@pytest.mark.parametrize("references", [[REFERENCE, REFERENCE], [{**REFERENCE, "record_id": str(index)} for index in range(101)]])
def test_reference_lists_are_unique_and_bounded(references):
    with pytest.raises(ValidationError):
        ProjectConfiguration.model_validate({"advanced": {"environments": references}})


@pytest.mark.parametrize(
    "patch",
    [
        {"amount": {"amount": 2500, "currency": "USD"}},
        {"amount": {"amount": "-1", "currency": "USD"}},
        {"amount": {"amount": "1.1234567", "currency": "USD"}},
        {"amount": {"amount": "2500", "currency": "usd"}},
        {"category": "budget"},
        {"approval_status": "pending"},
        {"owner_ref": {"kind": "owner", "id": "fixture-user"}},
        {"owner_ref": {"kind": "user", "id": "fixture-user", "extra": True}},
        {"period_start": "2026-02-30"},
        {"period_end": "2026-12-31T00:00:00Z"},
        {"period_end": None},
        {"unexpected": True},
    ],
)
def test_invalid_financial_allocation(patch):
    with pytest.raises(ValidationError):
        ProjectConfiguration.model_validate({"financials": {"allocations": [{**ALLOCATION, **patch}]}})
