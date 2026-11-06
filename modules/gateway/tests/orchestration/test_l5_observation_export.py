"""Same-run workflow export separates current authority from historical facts."""

import copy
import io
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from src.agentauth.human_control import HumanControlSession
from src.orchestration import l5_observation_export as exports
from src.orchestration import l5_observation_export_routes as routes
from src.orchestration import l5_observations as observations
from src.orchestration.l5_observation_contract import EndorseObservation, RecordObservation
from src.orchestration.l5_observation_scope import ObservationError
from src.orchestration.models import OrchestrationDecision
from src.orchestration.operations_release_job import NativeReleaseJob, service
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import TeamMembership, User
from tests.orchestration.test_l5_observation_scope import product_case, scope_case  # noqa: F401
from tests.orchestration.test_policy_admission import APPROVER, ORG_A, TEAM_A


@pytest.fixture
async def export_case(scope_case, db_session_factory, monkeypatch, request):  # noqa: F811
    c = scope_case
    owner = APPROVER
    if getattr(request, "param", None) == "cross_owner":
        owner = "separate-owner"
        c.db.add(User(id=owner, org_id=ORG_A, team_id=TEAM_A, email="owner@example.test", user_kind="human"))
        await c.db.flush()
        c.db.add_all(
            [
                TenantMembership(user_id=owner, tenant_id=ORG_A, role="member"),
                TeamMembership(org_id=ORG_A, user_id=owner, team_id=TEAM_A, role="member"),
            ]
        )
        c.product.owner_user_id = c.config.owner_id = owner
        c.request = c.request.model_copy(update={"observer_id": owner})
        await c.db.commit()
    human = HumanControlSession(user_id=APPROVER, tenant_id=ORG_A, expires_at=c.now + timedelta(hours=1))
    c.provider_run["created_at"] = (c.now - timedelta(minutes=40)).isoformat()
    # Collection happened in this exact still-running job, but its human window
    # has now expired. Historical export must not authenticate that old session.
    created = c.now - timedelta(minutes=35)
    c.challenge = await observations.prepare(c.db, flow_id=c.flow.id, human=human, request=c.request, now=created)
    if owner != APPROVER:
        human = HumanControlSession(user_id=owner, tenant_id=ORG_A, expires_at=c.now + timedelta(hours=1))
        await observations.endorse(
            c.db,
            flow_id=c.flow.id,
            challenge_id=c.challenge["challenge_id"],
            human=human,
            body=EndorseObservation(snapshot_sha256=c.challenge["snapshot_sha256"]),
            now=created,
        )
    c.recorded = await observations.record(
        c.db,
        flow_id=c.flow.id,
        challenge_id=c.challenge["challenge_id"],
        human=human,
        body=RecordObservation(snapshot_sha256=c.challenge["snapshot_sha256"], observation="displayed", displayed_configuration_revision=1),
        now=created + timedelta(seconds=1),
    )
    await c.db.commit()
    selected = c.challenge["snapshot"]["selection"]
    c.body = exports.ExportObservation.model_validate(
        {
            "job": {
                "correlation": c.action.detail["correlation"],
                "source_revision": "d" * 40,
                "dispatch_revision": "e" * 40,
                "workflow": "l5-services-release.yml",
                "role_arn": "arn:aws:iam::123456789012:role/release",
                "mutation_role_arn": "arn:aws:iam::123456789012:role/mutation",
                "contract_digest": c.action.detail["contract_digest"],
            },
            "challenge_id": c.challenge["challenge_id"],
            "snapshot_sha256": c.challenge["snapshot_sha256"],
            "selection": {
                **{
                    key: selected[key]
                    for key in (
                        "product_id",
                        "project_id",
                        "service_id",
                        "binding_id",
                        "configuration_id",
                        "configuration_revision",
                    )
                },
                "check_id": c.request.check_id,
                "subject": c.request.subject,
                "result_id": None,
                "result_revision": None,
                "result_run_id": None,
            },
        }
    )
    c.guard = NativeReleaseJob(db_session_factory, provider=c.provider)
    c.claims = {"run_id": "72", "run_attempt": "1", "actor_id": "789"}

    async def job_snapshot(guard, db, body, claims, **kwargs):
        assert body == c.body.job and claims == c.claims
        assert not kwargs or kwargs["expected_action"] == c.action.id and kwargs["started_at"] is not None
        # Generic job authorization has its own real lease/authority suite. This
        # boundary test retains its double-read contract and real provider read.
        return await db.get(type(c.action), c.action.id), c.report

    c.authority = AsyncMock(side_effect=job_snapshot)
    monkeypatch.setattr(exports, "job_snapshot", c.authority)
    return c


async def test_export_reads_expired_human_window_only_for_current_same_job(export_case):
    c = export_case
    result = await exports.export_observation(c.guard, c.body, c.claims)
    assert c.authority.await_count == 2
    assert result["contract"] == exports.EXPORT_CONTRACT
    assert result["observation"]["observed"]["displayed_configuration_revision"] == 1
    assert result["observation"]["actor_id"] == APPROVER
    assert result["release_check_completed"] is result["native_qualified"] is False
    assert result["observation"]["release_check_completed"] is result["observation"]["native_qualified"] is False
    assert await exports.export_observation(c.guard, c.body, c.claims) == result
    assert len(list(await c.db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == exports.EXPORTED)))) == 1


@pytest.mark.parametrize("export_case", ["cross_owner"], indirect=True)
async def test_expired_endorsement_remains_valid_for_its_prior_human_observation(export_case):
    c = export_case
    result = await exports.export_observation(c.guard, c.body, c.claims)
    assert result["observation"]["actor_id"] == "separate-owner"
    assert len(result["observation"]["decision_references"]) == 3
    await c.db.execute(delete(TeamMembership).where(TeamMembership.user_id == "separate-owner"))
    await c.db.commit()
    with pytest.raises(ObservationError, match="not_found"):
        await exports.export_observation(c.guard, c.body, c.claims)


async def test_real_gateway_export_is_consumed_by_release_journal_adapter(export_case, monkeypatch):
    c = export_case
    result = await exports.export_observation(c.guard, c.body, c.claims)
    scripts = Path(__file__).resolve().parents[3] / "L5-services" / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    import qualification_human_observations as consumer

    environment = {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": c.report.repo,
        "GITHUB_REPOSITORY_ID": "123",
        "GITHUB_WORKFLOW_REF": c.report.repo + "/.github/workflows/l5-services-release.yml@refs/heads/main",
        "GITHUB_RUN_ID": "72",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_WORKFLOW_SHA": "e" * 40,
        "GITHUB_SHA": "e" * 40,
        "L5_CHECKOUT_REVISION": "d" * 40,
        "L5_SERVICE": "devops",
        "L5_ENVIRONMENT": "dev",
        "ADP_SOURCE_REVISION": "d" * 40,
        "ADP_DEFINITION_REVISION": "e" * 40,
        "ADP_CONTRACT_DIGEST": c.body.job.contract_digest,
        "ADP_CORRELATION": c.body.job.correlation,
        "L5_RELEASE_ROLE_ARN": c.body.job.role_arn,
        "L5_MUTATION_ROLE_ARN": c.body.job.mutation_role_arn,
        "ADP_GATEWAY_BASE_URL": "https://gateway.example.invalid",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://pipeline.actions.githubusercontent.com/token",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "test-token",
    }
    sent = []

    def open_request(request, **kwargs):
        sent.append(request)
        response = io.BytesIO(json.dumps({"value": "current-oidc"} if len(sent) == 1 else result).encode())
        response.status = 200
        return response

    records = []
    journal = SimpleNamespace(binding={"context": consumer.release_identity(environment)}, append=lambda kind, value: records.append((kind, value)))
    values = consumer.collect_into(journal, [c.body.model_dump(exclude={"job"})], environment=environment, opener=SimpleNamespace(open=open_request))
    assert values[0].facts == result and records[0][0] == "human-observation-export"
    assert result["observation"]["release"]["source_revision"] == "d" * 40
    assert result["observation"]["release"]["definition_revision"] == "b" * 40
    assert result["observation"]["release"]["dispatch_revision"] == "e" * 40
    assert journal.binding["context"]["definition_revision"] == "e" * 40
    assert records[0][1]["response"]["release_check_completed"] is False


@pytest.mark.parametrize("change", ["run", "source", "selection", "snapshot", "owner", "membership", "claim_expired", "paused"])
async def test_export_refuses_other_scope_or_tampered_history(export_case, change):
    c = export_case
    if change in {"run", "source"}:
        detail = copy.deepcopy(c.action.detail)
        if change == "run":
            detail["release_job"]["run_id"] = "73"
        else:
            detail["source_revision"] = "0" * 40
        c.action.detail = detail
    elif change == "selection":
        c.body.selection.product_id = "another-product"
    elif change == "snapshot":
        c.body.snapshot_sha256 = "0" * 64
    elif change == "owner":
        c.product.owner_user_id = "another-owner"
    elif change == "membership":
        await c.db.execute(delete(TeamMembership).where(TeamMembership.user_id == APPROVER))
    elif change == "claim_expired":
        c.claim.lease_expires_at = c.now - timedelta(seconds=1)
    elif change == "paused":
        c.flow.execution_paused = True
    await c.db.commit()
    with pytest.raises((ObservationError, HTTPException)):
        await exports.export_observation(c.guard, c.body, c.claims)
    assert not list(await c.db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == exports.EXPORTED)))


@pytest.mark.parametrize("change", ["observed_fields", "receipt_time", "pass"])
async def test_historical_reader_independently_rejects_corrupted_receipt(export_case, change):
    c = export_case
    row = await c.db.get(OrchestrationDecision, c.recorded["observation_receipt"]["receipt_id"])
    data = json.loads(row.reason)
    if change == "observed_fields":
        data["observed"]["displayed_configuration_revision"] = 2
    elif change == "receipt_time":
        data["observed_at"] = c.now.isoformat()
    else:
        data["release_check_completed"] = True
    # The real store disallows rewriting a decision. Simulate a corrupt read
    # without disabling that protection or mutating the persisted evidence.
    bad = SimpleNamespace(**{column.key: getattr(row, column.key) for column in row.__table__.columns})
    bad.reason = json.dumps(data)

    async def get(model, identity, **kwargs):
        return bad if model is OrchestrationDecision and identity == row.id else await c.db.get(model, identity, **kwargs)

    db = SimpleNamespace(get=get, scalar=c.db.scalar)
    parent = await c.db.get(OrchestrationDecision, c.body.challenge_id)
    with pytest.raises(ObservationError):
        await exports.historical_observation(db, parent=parent, expected=c.body, dispatch=c.action, now=c.now)


async def test_second_current_authority_read_is_required_before_export(export_case):
    c = export_case
    c.authority.side_effect = [(c.action, c.report), HTTPException(403, "revoked")]
    with pytest.raises(HTTPException):
        await exports.export_observation(c.guard, c.body, c.claims)
    assert not list(await c.db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == exports.EXPORTED)))


async def test_changed_current_configuration_does_not_rewrite_valid_historical_display(export_case):
    c = export_case
    c.config.current_revision = 2
    await c.db.commit()
    result = await exports.export_observation(c.guard, c.body, c.claims)
    assert result["observation"]["selection"]["configuration_revision"] == 1


async def test_mounted_export_uses_workflow_oidc_and_rejects_human_token(app, test_client, export_case, monkeypatch):
    c = export_case
    app.dependency_overrides[service] = lambda: c.guard
    authenticated = AsyncMock(return_value=c.claims)
    monkeypatch.setattr(routes, "github_claims", authenticated)
    path = "/orchestration/operations-release-job/l5-observations/export"
    response = await test_client.post(path, json=c.body.model_dump(), headers={"Authorization": "Bearer current-oidc"})
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    authenticated.assert_awaited_once_with("current-oidc", audience="adp-operations-release-job-v1")
    authenticated.side_effect = HTTPException(403, "invalid_workflow_identity")
    assert (await test_client.post(path, json=c.body.model_dump(), headers={"Authorization": "Bearer human-session"})).status_code == 403


async def test_export_requires_existing_job_pin_before_provider_call(export_case):
    c = export_case
    c.action.detail = {key: value for key, value in c.action.detail.items() if key != "release_job"}
    await c.db.commit()
    c.provider.request.reset_mock()
    with pytest.raises(ObservationError, match="workflow_unverified"):
        await exports.export_observation(c.guard, c.body, c.claims)
    c.provider.request.assert_not_awaited()
