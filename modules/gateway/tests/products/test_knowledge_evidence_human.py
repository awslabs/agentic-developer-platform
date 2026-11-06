"""Knowledge evidence consumes the existing verified human launch receipt chain."""

import pytest

from src import knowledge_evidence
from src.orchestration import l5_observations
from src.orchestration.l5_observation_contract import RecordObservation
from tests.orchestration.test_l5_observation_launch import events, invoke
from tests.orchestration.test_l5_observation_launch import launch_case as launch_case
from tests.orchestration.test_l5_observation_scope import product_case as product_case
from tests.orchestration.test_l5_observation_scope import scope_case as scope_case

__all__ = ["launch_case", "product_case", "scope_case"]


@pytest.mark.parametrize("observed", [True, False])
async def test_existing_human_launch_chain_is_required_without_new_identity(launch_case, monkeypatch, observed):
    from src.orchestration.l5_observation_scope import ObservationError

    case = launch_case

    async def external_provider(*args):
        await args[-1].before_provider()
        return {"url": "https://fixture-space.aidevops.global.app.aws"}

    monkeypatch.setattr(case.backend, "_launch_browser", external_provider)
    await invoke(case)
    receipts = await events(case)
    if observed:
        await l5_observations.record(
            case.db,
            flow_id=case.flow_id,
            challenge_id=case.challenge["challenge_id"],
            human=case.human,
            body=RecordObservation(
                snapshot_sha256=case.challenge["snapshot_sha256"],
                observation="displayed",
                displayed_native_hostname="fixture-space.aidevops.global.app.aws",
                launch_receipt_id=receipts["issued"][0],
            ),
        )
        await case.db.commit()
        selected = await knowledge_evidence.native_observation(case.backend, case.human, case.flow_id, case.challenge["challenge_id"])
        assert selected["observation_receipt"]["actor_id"] == case.human.user_id
        assert selected["launch_attempt"][-1]["phase"] == "issued"
        assert selected["release_check_completed"] is False and selected["native_qualified"] is False
    else:
        with pytest.raises(ObservationError, match="knowledge_evidence_native_observation_required"):
            await knowledge_evidence.native_observation(case.backend, case.human, case.flow_id, case.challenge["challenge_id"])
