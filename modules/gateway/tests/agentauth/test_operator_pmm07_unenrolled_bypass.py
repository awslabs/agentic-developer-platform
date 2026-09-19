import asyncio
"""Missing snapshot material is never permission to skip live enforcement."""

import httpx
import pytest
from sqlalchemy import delete, update

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.workload import WORKLOAD_HEADER
from src.shared.database import get_db
from src.shared.models.persona_models import PersonaModelPolicySetting
from tests.agentauth.test_bootstrap_routes import http_client, kubernetes, provision, store  # noqa: F401
from tests.agentauth.test_operator_pmm07_posture_cache import posture_sessions  # noqa: F401
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401


@pytest.mark.integration
@pytest.mark.parametrize("posture", ["report_only", "enforcing", None])
@pytest.mark.parametrize("snapshot_shape", ["absent", "missing_binding"])
async def test_real_bootstrap_missing_snapshot_obeys_committed_class_posture(posture_sessions, store, kubernetes, monkeypatch, posture, snapshot_shape):
    async with posture_sessions() as operator:
        if posture is None:
            await operator.execute(delete(PersonaModelPolicySetting))
        else:
            await operator.execute(update(PersonaModelPolicySetting).values(enforcement_posture=posture, posture_revision=11))
        await operator.commit()
    envelope, _ = provision(store)  # Actual protected developer execution, no snapshot attached.
    if snapshot_shape == "missing_binding":
        store.client.update_item(
            TableName=store.table,
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-a"}},
            UpdateExpression="SET model_policy_snapshot = :snapshot, model_policy_snapshot_digest = :digest",
            ExpressionAttributeValues={":snapshot": {"S": "{}"}, ":digest": {"S": "a" * 64}},
        )
    existing_client, _ = await asyncio.to_thread(http_client, store, kubernetes, monkeypatch)

    async def database():
        async with posture_sessions() as reader:
            yield reader

    existing_client.app.dependency_overrides[get_db] = database
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=existing_client.app), base_url="http://test") as client:
        response = await client.post(
            "/internal/v1/agent/bootstrap",
            json={"invocation_id": "run-a", "envelope_digest": envelope_digest(envelope)},
            headers={"X-Caller-Identity": "registered-worker-transport", WORKLOAD_HEADER: "pod-token"},
        )
    if posture == "report_only":
        assert response.status_code == 200, response.text
        assert response.json()["model_policy"]["posture"] == "report_only"
        assert response.json()["model_policy"]["posture_verified"] is True
    else:
        assert response.status_code >= 400, response.json()
        assert "credential" not in response.json()
