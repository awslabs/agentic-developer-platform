"""Consumers keep the canonical scope and truthful pending-state vocabulary."""

import json
from pathlib import Path

from src.shared.models.dev_box import (
    DevBox,
    DevBoxAccessSession,
    DevBoxDevice,
    DevBoxInstance,
    DevBoxOperation,
    DevBoxResource,
    DevBoxUsage,
)


def test_versioned_consumer_fixture_has_canonical_scope_and_unknown_state():
    fixture = json.loads((Path(__file__).resolve().parents[1] / "contract-v1.json").read_text())
    assert fixture["contract_version"] == 1
    assert fixture["desktop"]["observed_state"] == "unknown"
    assert fixture["operation"]["status"] == "pending"
    assert fixture["operation"]["retention_choice"] is None
    assert fixture["instance"]["device_identity_id"] is None
    assert fixture["resource"]["deletion_state"] == "unknown"
    assert fixture["usage"]["currency"] == "USD"
    for model in (DevBoxInstance, DevBoxOperation, DevBoxResource, DevBoxUsage, DevBoxDevice, DevBoxAccessSession):
        assert {"user_id", "org_id", "deployment_id", "machine_generation", "box_id"} <= set(model.__table__.columns.keys())
    assert {"user_id", "org_id", "deployment_id", "machine_generation", "template_revision"} <= set(DevBox.__table__.columns.keys())
    assert {"actor_user_id", "provider_token", "idempotency_key", "request_digest", "retention_choice"} <= set(DevBoxOperation.__table__.columns.keys())
