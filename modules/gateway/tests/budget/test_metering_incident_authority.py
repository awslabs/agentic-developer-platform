"""Protected incident grants cannot be constructed from reconciliation input."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from src.budget.metering_incident_authority import verify_metering_incident_grant


@pytest.fixture
def grant(monkeypatch):
    record = {
        "incident_id": "incident-fixture",
        "actor_id": "human-operator",
        "tenant_id": "tenant-1",
        "request_id": "request-1",
        "action": "apply_trusted_receipt",
        "expected_revision": 4,
        "evidence_ref": "metering/tenant-1/request-1/final-usage.json",
        "evidence_version": "fixture-version",
        "evidence_sha256": "a" * 64,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    }
    monkeypatch.setenv("BG_METERING_INCIDENT_GRANTS", json.dumps([record]))
    return record


def verify(record):
    return verify_metering_incident_grant(
        actor_id=record["actor_id"],
        tenant_id=record["tenant_id"],
        request_id=record["request_id"],
        action=record["action"],
        expected_revision=record["expected_revision"],
        evidence_ref=record["evidence_ref"],
        evidence_version=record["evidence_version"],
        evidence_sha256=record["evidence_sha256"],
    )


def test_exact_unexpired_grant_is_read_from_server_configuration(grant):
    assert verify(grant).incident_id == "incident-fixture"


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("actor_id", "service-principal"),
        ("tenant_id", "other-tenant"),
        ("request_id", "other-attempt"),
        ("action", "confirm_not_dispatched"),
        ("expected_revision", 5),
        ("evidence_ref", "https://outside.invalid/receipt"),
        ("evidence_version", "older-version"),
        ("evidence_sha256", "b" * 64),
    ],
)
def test_mismatched_grant_cannot_authorize_action(grant, field, changed):
    with pytest.raises(HTTPException) as error:
        verify(grant | {field: changed})
    assert error.value.status_code == 403


@pytest.mark.parametrize("change", [{"actor_id": "*"}, {"evidence_ref": "https://outside.invalid/receipt"}, {"evidence_sha256": "wrong"}])
def test_malformed_operator_configuration_fails_closed(grant, monkeypatch, change):
    monkeypatch.setenv("BG_METERING_INCIDENT_GRANTS", json.dumps([grant | change]))
    with pytest.raises(HTTPException) as error:
        verify(grant)
    assert error.value.status_code == 503


def test_expired_or_missing_grant_denies(grant, monkeypatch):
    expired = grant | {"expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}
    monkeypatch.setenv("BG_METERING_INCIDENT_GRANTS", json.dumps([expired]))
    with pytest.raises(HTTPException) as error:
        verify(grant)
    assert error.value.status_code == 403
    monkeypatch.delenv("BG_METERING_INCIDENT_GRANTS")
    with pytest.raises(HTTPException) as error:
        verify(grant)
    assert error.value.status_code == 403
