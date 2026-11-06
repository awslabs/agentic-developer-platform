"""Human facts stay attributable, immutable, scoped and separate from acceptance."""

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select

from src.orchestration import l5_observations as observations
from src.orchestration.l5_observation_contract import CONTRACT, LAUNCH, OBSERVED, PrepareObservation, RecordObservation
from src.orchestration.l5_observation_receipts import launch_id
from src.orchestration.l5_observation_scope import ObservationError
from src.orchestration.models import OrchestrationDecision, OrchestrationFlow, OrchestrationNode


@pytest.fixture
async def observation_case(db_session, monkeypatch):
    db = db_session
    flow = OrchestrationFlow(org_id="tenant", slug="l5-observation", title="L5 release")
    db.add(flow)
    await db.flush()
    node = OrchestrationNode(
        org_id="tenant",
        flow_id=flow.id,
        epic_ref="epic",
        wave_ref="wave",
        node_ref="release",
        kind="story",
        title="Release",
        attempts=2,
        state="running",
    )
    db.add(node)
    await db.commit()
    now = datetime.now(UTC)
    owner = SimpleNamespace(tenant_id="tenant", user_id="owner", expires_at=now + timedelta(hours=1))
    observer = SimpleNamespace(tenant_id="tenant", user_id="observer", expires_at=now + timedelta(hours=1))
    request = PrepareObservation(
        request_key="observation_fixture_0001",
        node_id=node.id,
        expected_plan_version=3,
        expected_plan_hash="a" * 64,
        expected_attempt=2,
        workflow_action_id="action",
        product_id="product",
        project_id="project",
        service_id="devops",
        binding_id="binding",
        check_id="ui-cli-mcp-parity",
        observer_id=observer.user_id,
        subject="configuration",
        reason="Observe the installed UI configuration",
    )
    snapshot = {
        "release": {"source_revision": "b" * 40, "run_id": 72, "run_attempt": 1},
        "selection": {
            "owner_id": "owner",
            "configuration_revision": 8,
            "binding_id": "binding",
            "binding_generation": 2,
            "native_hostname": "selected-space.aidevops.global.app.aws",
        },
        "local_package": {"l5_wheel_sha256": "c" * 64},
    }
    read_scope = AsyncMock(side_effect=lambda *args, **kwargs: (node, copy.deepcopy(snapshot), now + timedelta(hours=1)))
    monkeypatch.setattr(observations, "scope", read_scope)
    return SimpleNamespace(
        db=db, flow=flow, node=node, now=now, owner=owner, observer=observer, request=request, snapshot=snapshot, read_scope=read_scope
    )


async def prepared(case, **updates):
    request = case.request.model_copy(update=updates)
    result = await observations.prepare(case.db, flow_id=case.flow.id, human=case.owner, request=request, now=case.now)
    await case.db.commit()
    return result


async def test_observation_is_human_attributed_and_never_completes_release(observation_case):
    case = observation_case
    result = await prepared(case)
    body = RecordObservation(snapshot_sha256=result["snapshot_sha256"], observation="displayed", displayed_configuration_revision=8)
    result = await observations.record(
        case.db, flow_id=case.flow.id, challenge_id=result["challenge_id"], human=case.observer, body=body, now=case.now
    )
    await case.db.commit()
    receipt = result["observation_receipt"]
    assert receipt["actor_id"] == "observer" and receipt["provenance"] == "authenticated-human-observation"
    assert result["qualification_scope"] == "human-observation-only"
    assert result["release_check_completed"] is receipt["release_check_completed"] is False
    assert result["native_qualified"] is receipt["native_qualified"] is False
    assert case.node.state == "running" and case.node.attempts == 2
    row = await case.db.get(OrchestrationDecision, receipt["receipt_id"])
    assert row.kind == OBSERVED and row.actor_kind == "human" and row.from_state is row.to_state is None
    replay = await observations.record(
        case.db, flow_id=case.flow.id, challenge_id=result["challenge_id"], human=case.observer, body=body, now=case.now
    )
    assert replay == result
    with pytest.raises(ObservationError, match="already_recorded"):
        await observations.record(
            case.db,
            flow_id=case.flow.id,
            challenge_id=result["challenge_id"],
            human=case.observer,
            body=body.model_copy(update={"observation": "mismatch"}),
            now=case.now,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("actor_id", "owner"),
        ("source_revision", "e" * 40),
        ("passed", True),
        ("evidence_verified", True),
        ("evidence_url", "https://untrusted.invalid/receipt"),
    ],
)
def test_no_body_supplied_identity_authority_or_pass(field, value):
    with pytest.raises(ValidationError):
        RecordObservation.model_validate({"snapshot_sha256": "a" * 64, "observation": "displayed", field: value})


@pytest.mark.parametrize("who", ["owner", "other", "foreign"])
async def test_only_exact_selected_observer_can_record(observation_case, who):
    case = observation_case
    result = await prepared(case)
    actor = (
        case.owner
        if who == "owner"
        else SimpleNamespace(user_id=who, tenant_id="elsewhere" if who == "foreign" else "tenant", expires_at=case.observer.expires_at)
    )
    with pytest.raises(ObservationError):
        await observations.record(
            case.db,
            flow_id=case.flow.id,
            challenge_id=result["challenge_id"],
            human=actor,
            body=RecordObservation(snapshot_sha256=result["snapshot_sha256"], observation="displayed", displayed_configuration_revision=8),
            now=case.now,
        )
    assert not list(await case.db.scalars(select(OrchestrationDecision.id).where(OrchestrationDecision.kind == OBSERVED)))


@pytest.mark.parametrize("field", ["release", "selection", "local_package"])
async def test_rebuild_detects_source_runtime_and_configuration_changes(observation_case, field):
    case = observation_case
    result = await prepared(case)
    case.snapshot[field]["changed"] = True
    with pytest.raises(ObservationError, match="snapshot_changed"):
        await observations.read(case.db, flow_id=case.flow.id, challenge_id=result["challenge_id"], human=case.observer, now=case.now)


async def test_expiry_and_display_mismatch_do_not_create_success(observation_case):
    case = observation_case
    result = await prepared(case)
    with pytest.raises(ObservationError, match="fields_mismatch"):
        await observations.record(
            case.db,
            flow_id=case.flow.id,
            challenge_id=result["challenge_id"],
            human=case.observer,
            body=RecordObservation(snapshot_sha256=result["snapshot_sha256"], observation="displayed", displayed_configuration_revision=9),
            now=case.now,
        )
    with pytest.raises(ObservationError, match="expired"):
        await observations.read(
            case.db, flow_id=case.flow.id, challenge_id=result["challenge_id"], human=case.observer, now=case.now + timedelta(minutes=31)
        )


async def launch_chain(case, result, *, phase="denied", started=False):
    parent = await case.db.get(OrchestrationDecision, result["challenge_id"])
    for step in ["prepared", *(["provider_started"] if started else []), phase]:
        attempted = started and step != "prepared"
        row = observations.append(
            case.db,
            identity=parent,
            kind=LAUNCH,
            actor_id="system:l5-native-launch",
            actor_kind="service",
            row_id=launch_id(parent.id, step),
            data={
                "contract": CONTRACT,
                "challenge_id": parent.id,
                "snapshot_sha256": result["snapshot_sha256"],
                "observer_id": "observer",
                "phase": step,
                "observed_at": case.now.isoformat(),
                "http_status": 200 if step == "issued" else 403 if step == "denied" else None,
                "provider_session_calls": None if attempted else 0,
                "provider_invocation_started": attempted,
            },
        )
    await case.db.flush()
    return row


@pytest.mark.parametrize("started", [True, False])
async def test_negative_observer_gets_no_foreign_snapshot_and_needs_server_denial(observation_case, started):
    case = observation_case
    result = await prepared(case, subject="native_denial", check_id="sso-denial-before-federation")
    view = await observations.read(case.db, flow_id=case.flow.id, challenge_id=result["challenge_id"], human=case.observer, now=case.now)
    assert view["snapshot"] is None
    body = RecordObservation(snapshot_sha256=result["snapshot_sha256"], observation="denied")
    with pytest.raises(ObservationError, match="launch_unverified"):
        await observations.record(case.db, flow_id=case.flow.id, challenge_id=result["challenge_id"], human=case.observer, body=body, now=case.now)
    launch = await launch_chain(case, result, started=started)
    selected = body.model_copy(update={"launch_receipt_id": launch.id})
    if started:
        with pytest.raises(ObservationError, match="denial_after_provider"):
            await observations.record(
                case.db, flow_id=case.flow.id, challenge_id=result["challenge_id"], human=case.observer, body=selected, now=case.now
            )
    else:
        saved = await observations.record(
            case.db, flow_id=case.flow.id, challenge_id=result["challenge_id"], human=case.observer, body=selected, now=case.now
        )
        assert saved["observation_receipt"]["observed"]["observation"] == "denied"
        assert saved["release_check_completed"] is False


async def test_receipt_cannot_be_reused_by_another_challenge(observation_case):
    case = observation_case
    first = await prepared(case, subject="native_ui")
    launch = await launch_chain(case, first, phase="issued", started=True)
    await case.db.commit()
    second = await prepared(
        case, subject="native_ui", request_key="observation_fixture_0002", reason="A different bounded observation of this release"
    )
    with pytest.raises(ObservationError, match="launch_unverified"):
        await observations.record(
            case.db,
            flow_id=case.flow.id,
            challenge_id=second["challenge_id"],
            human=case.observer,
            body=RecordObservation(
                snapshot_sha256=second["snapshot_sha256"],
                observation="displayed",
                displayed_native_hostname="selected-space.aidevops.global.app.aws",
                launch_receipt_id=launch.id,
            ),
            now=case.now,
        )


async def test_read_reverifies_native_receipt_chain(observation_case):
    case = observation_case
    challenge = await prepared(case, subject="native_ui")
    launch = await launch_chain(case, challenge, phase="issued", started=True)
    body = RecordObservation(
        snapshot_sha256=challenge["snapshot_sha256"],
        observation="displayed",
        displayed_native_hostname="selected-space.aidevops.global.app.aws",
        launch_receipt_id=launch.id,
    )
    saved = await observations.record(
        case.db, flow_id=case.flow.id, challenge_id=challenge["challenge_id"], human=case.observer, body=body, now=case.now
    )
    assert saved["observation_receipt"]["observed"] == body.model_dump(mode="json")
    # Model an unavailable/corrupt receipt store, bypassing normal append-only
    # writes. A previously saved human assertion cannot substitute for the chain.
    await case.db.execute(delete(OrchestrationDecision).where(OrchestrationDecision.id == launch_id(challenge["challenge_id"], "provider_started")))
    await case.db.commit()
    with pytest.raises(ObservationError, match="launch_unverified"):
        await observations.read(case.db, flow_id=case.flow.id, challenge_id=challenge["challenge_id"], human=case.observer, now=case.now)


async def test_request_key_replays_exact_preparation_and_new_key_recovers_expiry(observation_case):
    case = observation_case
    challenge = await prepared(case)
    same = await observations.prepare(case.db, flow_id=case.flow.id, human=case.owner, request=case.request, now=case.now)
    assert same["challenge_id"] == challenge["challenge_id"]
    with pytest.raises(ObservationError, match="request_key_reused"):
        await observations.prepare(
            case.db,
            flow_id=case.flow.id,
            human=case.owner,
            request=case.request.model_copy(update={"reason": "Different request cannot silently reuse this key"}),
            now=case.now,
        )
    later = case.now + timedelta(minutes=31)
    with pytest.raises(ObservationError, match="expired"):
        await observations.prepare(case.db, flow_id=case.flow.id, human=case.owner, request=case.request, now=later)
    fresh = await observations.prepare(
        case.db,
        flow_id=case.flow.id,
        human=case.owner,
        request=case.request.model_copy(update={"request_key": "observation_fixture_0002"}),
        now=later,
    )
    assert fresh["challenge_id"] != challenge["challenge_id"]
    assert datetime.fromisoformat(fresh["expires_at"]) > later
