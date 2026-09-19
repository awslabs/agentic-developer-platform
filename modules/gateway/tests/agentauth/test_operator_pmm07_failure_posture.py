"""A failed proposal must retain the separately established committed posture."""

import pytest
from sqlalchemy import update

from src.agentauth.model_policy import bootstrap_model_policy_live
from src.shared.models.persona_models import PersonaModelPolicySetting
from tests.agentauth.test_model_policy import (
    _grant,
    _live_policy_record,
    live_snapshot,
    policy_store,  # noqa: F401
)
from tests.agentauth.test_operator_pmm07_posture_cache import posture_sessions  # noqa: F401
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401


@pytest.mark.integration
@pytest.mark.parametrize("posture", ["disabled", "report_only", "enforcing"])
@pytest.mark.parametrize("failure", ["direct_override_unresolved", "snapshot_missing"])
async def test_failed_proposal_retains_committed_live_posture(posture_sessions, policy_store, posture, failure):
    async with posture_sessions() as administrator:
        await administrator.execute(update(PersonaModelPolicySetting).values(enforcement_posture=posture, posture_revision=11))
        await administrator.commit()
    policy = live_snapshot()
    record = _live_policy_record(policy_store, policy)
    key = {"pk": {"S": "TENANT#tenant-a"}, "sk": {"S": "EXEC#run-live-developer"}}
    if failure == "direct_override_unresolved":
        policy_store.client.update_item(
            TableName=policy_store.table, Key=key,
            UpdateExpression="SET direct_model_requested = :requested",
            ExpressionAttributeValues={":requested": {"S": "not-a-canonical-model"}},
        )
    else:
        policy_store.client.update_item(
            TableName=policy_store.table, Key=key,
            UpdateExpression="REMOVE model_policy_snapshot, model_policy_snapshot_digest",
        )
    async with posture_sessions() as reader:
        result = await bootstrap_model_policy_live(reader, store=policy_store, record=record, grant=_grant("github_event"), env={})
    assert result["status"] == "unavailable"
    assert result["reason"] == failure
    assert result["posture"] == posture
    assert result["posture_verified"] is True
