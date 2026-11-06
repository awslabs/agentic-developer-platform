"""Periodic evidence uses actual scoped rows and cannot manufacture delivery facts."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from l5_services.models.configuration import ConfigurationVersion
from l5_services.models.event import EventReceipt
from l5_services.models.schedule import EventObservation, ScheduleState
from l5_services.models.wa_periodic import WAPeriodicCheckpoint
from sqlalchemy import func, select

from tests.products.test_l5_wa_qualification import consent_fixture as consent_fixture
from tests.products.test_l5_wa_qualification import l5_owner as l5_owner
from tests.products.test_l5_wa_qualification import selected as selected


@pytest.fixture
async def periodic(selected, db_session, monkeypatch):  # noqa: F811 - imported pytest fixture
    state = selected
    version = state["configurations"]["well_architected"][1]
    version.settings = {
        **version.settings,
        "triggers": [
            {"trigger_id": "selected-tick", "kind": "schedule", "enabled": False, "schedule": "* * * * *", "binding_id": state["binding"].binding_id}
        ],
    }
    now = datetime.now(UTC)
    db_session.add(
        ScheduleState(
            tenant_id=state["binding"].tenant_id,
            configuration_id=version.configuration_id,
            configuration_revision=version.revision,
            service_id="well_architected",
            initialized_at=now,
            next_due_at=None,
            triggers={"selected-tick": {"next_due_at": None, "reason": "trigger_disabled"}},
        )
    )
    db_session.add(
        EventObservation(
            observation_id="actual-event-arrival",
            tenant_id=state["binding"].tenant_id,
            idempotency_key=state["event"].idempotency_key,
            observed_at=now,
            kind="received",
            state="rejected",
            reason="trigger_disabled",
        )
    )
    await db_session.commit()
    native = {
        "source": "independent-native-read",
        "profile_arn": state["binding"].native_resource_ref["record_id"],
        "architecture_eligible": True,
        "aggregation_configuration": [],
        "contexts": [],
        "generations": [],
    }
    reader = AsyncMock(return_value=native)
    monkeypatch.setattr("src.l5_wa_periodic_observations.read_native", reader)
    return {**state, "periodic_selection": {"binding_id": state["binding"].binding_id, "trigger_id": "selected-tick"}, "native_reader": reader}


async def checkpoint(client, state, key="periodic_checkpoint_fixture_01"):
    version = state["configurations"]["well_architected"][1]
    return await client.post(
        state["path"] + "/periodic-checkpoints", json=state["periodic_selection"], headers={"If-Match": f'"{version.hash}"', "Idempotency-Key": key}
    )


async def test_checkpoint_exports_cursor_event_history_without_creating_run_or_effect(periodic, test_client, db_session):
    response = await checkpoint(test_client, periodic)
    assert response.status_code == 200, response.text
    facts = response.json()
    runtime = facts["snapshot"]["runtime"]
    assert runtime["cursor"]["triggers"]["selected-tick"]["reason"] == "trigger_disabled"
    assert runtime["events"][0]["run_id"] is None
    assert [item["observation_id"] for item in runtime["event_history"]] == ["actual-event-arrival"]
    assert runtime["transport"] == [] and runtime["work"] == []
    assert facts["execution_authorized"] is facts["native_qualified"] is False
    replay = await checkpoint(test_client, periodic)
    assert replay.json() == facts
    assert periodic["native_reader"].await_count == 1
    assert await db_session.scalar(select(func.count()).select_from(WAPeriodicCheckpoint)) == 1
    path = periodic["path"] + "/periodic-checkpoints/" + facts["checkpoint_id"]
    assert (await test_client.get(path)).json() == facts
    periodic["provider"].assert_not_called()


async def test_no_run_no_event_disabled_checkpoint_does_not_require_run_identity(periodic, test_client, db_session):
    await db_session.delete(periodic["run"])
    await db_session.delete(periodic["event"])
    await db_session.commit()
    response = await checkpoint(test_client, periodic)
    assert response.status_code == 200, response.text
    runtime = response.json()["snapshot"]["runtime"]
    assert runtime["events"] == runtime["runs"] == runtime["event_history"] == runtime["transport"] == []


async def test_legacy_cross_service_journal_key_collision_never_exposes_other_history(periodic, test_client, db_session):
    original = periodic["event"]
    db_session.add(
        EventReceipt(
            tenant_id=original.tenant_id,
            product_id=original.product_id,
            project_id=original.project_id,
            service_id="transform",
            trigger_id=original.trigger_id,
            event_id=original.event_id,
            idempotency_key=original.idempotency_key,
            event_kind="schedule",
            configuration_id="foreign-configuration",
            configuration_revision=1,
            state="rejected",
            reason="private-unrelated-reason",
            received_at=datetime.now(UTC),
            recovery_attempts=0,
        )
    )
    await db_session.commit()
    result = await checkpoint(test_client, periodic)
    assert result.status_code == 409 and result.json()["detail"]["code"] == "periodic_history_ambiguous"
    assert "private-unrelated-reason" not in result.text
    periodic["native_reader"].assert_not_awaited()


@pytest.mark.parametrize("changed", ["new-revision", "owned-cleanup"])
async def test_owner_can_read_retained_history_after_toggle_or_owned_cleanup(periodic, test_client, db_session, changed):
    result = await checkpoint(test_client, periodic)
    assert result.status_code == 200, result.text
    if changed == "owned-cleanup":
        periodic["binding"].state = "cleaned"
        periodic["binding"].generation += 1
    else:
        configuration, version = periodic["configurations"]["well_architected"]
        settings = deepcopy(version.settings)
        settings["triggers"][0]["enabled"] = True
        configuration.current_revision = 2
        db_session.add(
            ConfigurationVersion(
                tenant_id=version.tenant_id,
                configuration_id=version.configuration_id,
                revision=2,
                settings=settings,
                hash="e" * 64,
                inherited_fields=[],
                gaps=[],
                state=version.state,
            )
        )
    await db_session.commit()
    observed = await test_client.get(periodic["path"] + "/periodic-checkpoints/" + result.json()["checkpoint_id"])
    assert observed.status_code == 200 and observed.json() == result.json()


@pytest.mark.parametrize("change", ["owner", "source", "foreign-profile", "configuration", "tenant"])
async def test_retained_checkpoint_rechecks_current_owner_source_and_native_scope(periodic, test_client, db_session, monkeypatch, change):
    from fastapi import HTTPException

    response = await checkpoint(test_client, periodic)
    assert response.status_code == 200, response.text
    if change == "owner":
        periodic["configurations"]["well_architected"][0].owner_id = "another-owner"
    elif change == "source":
        periodic["source_authority"].side_effect = HTTPException(404)
    elif change == "foreign-profile":
        periodic["binding"].native_resource_ref = {**periodic["binding"].native_resource_ref, "record_id": "foreign-profile"}
    elif change == "configuration":
        periodic["configurations"]["well_architected"][1].hash = "f" * 64
    else:
        row = await db_session.scalar(select(WAPeriodicCheckpoint))
        row.tenant_id = "foreign-tenant"
    await db_session.commit()
    result = await test_client.get(periodic["path"] + "/periodic-checkpoints/" + response.json()["checkpoint_id"])
    assert result.status_code in {404, 409}, result.text
    assert "snapshot" not in result.json()


async def test_checkpoint_rechecks_after_native_io_and_rejects_retrospective_or_authored_data(periodic, test_client, db_session):
    original = deepcopy(periodic["periodic_selection"])
    periodic["periodic_selection"]["provider_count"] = 0
    assert (await checkpoint(test_client, periodic)).status_code == 422
    periodic["periodic_selection"] = original

    async def changed(*args, **kwargs):
        periodic["configurations"]["well_architected"][0].owner_id = "revoked-during-read"
        await db_session.commit()
        return {"source": "independent-native-read"}

    periodic["native_reader"].side_effect = changed
    result = await checkpoint(test_client, periodic)
    assert result.status_code == 404
    assert await db_session.scalar(select(func.count()).select_from(WAPeriodicCheckpoint)) == 0


async def test_bounded_interval_reads_only_selected_receipts_and_rechecks_after_audit(periodic, test_client, db_session):
    first = await checkpoint(test_client, periodic)
    second = await checkpoint(test_client, periodic, "periodic_checkpoint_fixture_02")
    assert first.status_code == second.status_code == 200
    body = {"before_id": first.json()["checkpoint_id"], "after_id": second.json()["checkpoint_id"]}
    result = await test_client.post(periodic["path"] + "/periodic-intervals/read", json=body)
    assert result.status_code == 200, result.text
    assert result.json()["native_audit"]["code"] == "periodic_native_audit_not_selected"
    assert periodic["native_reader"].await_count == 2
    reverse = await test_client.post(
        periodic["path"] + "/periodic-intervals/read", json={"before_id": body["after_id"], "after_id": body["before_id"]}
    )
    assert reverse.status_code == 409
    stale = await db_session.get(WAPeriodicCheckpoint, (periodic["binding"].tenant_id, body["before_id"]))
    stale.observed_at = datetime.now(UTC) - timedelta(days=2)
    await db_session.commit()
    assert (await test_client.post(periodic["path"] + "/periodic-intervals/read", json=body)).status_code == 409
