"""Durable effects survive errors and cannot be upgraded to human UI evidence."""

import io
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Response
from l5_services.api.router import create_router
from l5_services.models.configuration import ConfigurationVersion
from sqlalchemy import select
from starlette.requests import Request

from src import l5_observation_effects as effects
from src.agentauth.human_control import HumanControlSession
from src.l5_integration import GatewayConfigurationBackend
from src.orchestration import l5_observation_export as exports
from src.orchestration import l5_observations as observations
from src.orchestration.l5_effect_export import ExportEffect, export_effect, historical_effect
from src.orchestration.l5_effect_read import read_effect
from src.orchestration.models import OrchestrationDecision
from tests.internal.test_l5_security_tools import compiled_dispatch_catalogue  # noqa: F401
from tests.orchestration.test_l5_observation_scope import product_case, scope_case  # noqa: F401
from tests.orchestration.test_policy_admission import APPROVER, ORG_A


@pytest.fixture
async def effect_case(scope_case, db_session_factory, monkeypatch):  # noqa: F811
    c = scope_case
    # The scope fixture inserts minimal ORM rows directly. Select Investigation
    # and its persisted visibility before hashing/preparing the challenge so the
    # canonical check exercises Investigation's real worker-readiness gate.
    c.version.settings = {**c.version.settings, "visibility": "only_me", "capabilities": ["investigation"]}
    c.version.hash = effects.digest(c.version.settings)
    await c.db.commit()
    c.human = HumanControlSession(user_id=APPROVER, tenant_id=ORG_A, expires_at=c.now + timedelta(hours=1))
    c.challenge = await observations.prepare(c.db, flow_id=c.flow.id, human=c.human, request=c.request)
    await c.db.commit()
    c.response = Response()
    c.http_request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/configuration/check",
            "headers": [(effects.HEADER.lower().encode(), c.challenge["challenge_id"].encode())],
        }
    )
    monkeypatch.setattr(effects, "human_session", AsyncMock(return_value=c.human))
    c.backend = GatewayConfigurationBackend(c.db, SimpleNamespace(org_id=ORG_A, user_id=APPROVER, account_type="human"))
    c.backend.sessions = db_session_factory
    # Keep the real readiness implementation; only Product access is supplied by
    # this fixture, while challenge scope uses its real ORM authorization.
    c.backend.current = AsyncMock(return_value=(c.config, c.version))
    c.wrapper = effects.EffectBackend(c.backend, c.http_request, c.response)
    selected = c.challenge["snapshot"]["selection"]
    c.expected = ExportEffect.model_validate(
        {
            "job": {
                "correlation": c.action.detail["correlation"],
                "source_revision": "d" * 40,
                "dispatch_revision": "e" * 40,
                "workflow": "l5-services-release.yml",
                "contract_digest": c.action.detail["contract_digest"],
                "role_arn": "arn:aws:iam::123456789012:role/release",
                "mutation_role_arn": "arn:aws:iam::123456789012:role/mutation",
            },
            "challenge_id": c.challenge["challenge_id"],
            "snapshot_sha256": c.challenge["snapshot_sha256"],
            "selection": {
                **{
                    key: selected[key]
                    for key in ("product_id", "project_id", "service_id", "binding_id", "configuration_id", "configuration_revision")
                },
                "check_id": "ui-cli-mcp-parity",
                "subject": "configuration",
            },
            "operation": "check",
            "request_sha256": effects.digest({}),
        }
    )
    return c


async def test_real_canonical_check_records_before_after_and_historical_reader(effect_case):
    c = effect_case
    app = FastAPI()
    app.include_router(create_router(lambda: c.wrapper))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/v1/products/{c.product.id}/projects/{c.project.id}/services/devops/configuration/check")
    # A successful canonical check records its result even when runtime is not
    # ready: this effect fixture does not install a worker publisher.
    assert response.status_code == 200 and response.json()["ready"] is False
    assert response.json()["gaps"] == ["work_queue_unavailable"]
    assert response.json()["capability"] == "investigation"
    assert response.json()["configuration_hash"] == c.version.hash == c.challenge["snapshot"]["selection"]["configuration_sha256"]
    facts = await read_effect(c.db, flow_id=c.flow.id, challenge_id=c.challenge["challenge_id"], human=c.human)
    assert [row["phase"] for row in facts["events"]] == ["before", "after"]
    assert facts["events"][1]["response_sha256"] == effects.digest(response.json())
    assert facts["events"][1]["readiness"] == {"ready": False, "provider_contacted": False}
    assert facts["outcome"] == "returned" and facts["provenance"] == "authenticated-canonical-request-effect"
    assert facts["release_check_completed"] is facts["native_qualified"] is False
    # The same challenge cannot be reused, even after a successful response.
    with pytest.raises(HTTPException) as duplicate:
        await c.wrapper.check(c.product.id, c.project.id, "devops")
    assert duplicate.value.detail["code"] == "l5_effect_already_attempted"


async def test_before_commit_survives_lost_response_without_automatic_retry(effect_case):
    c = effect_case
    operation = await effects.start(
        c.backend, c.http_request, operation="check", product_id=c.product.id, project_id=c.project.id, service_id="devops", payload={}
    )
    await c.db.rollback()
    facts = await read_effect(c.db, flow_id=c.flow.id, challenge_id=c.challenge["challenge_id"], human=c.human)
    assert len(facts["events"]) == 1 and facts["outcome"] == "unknown"
    assert operation.prepared["request_sha256"] == effects.digest({})
    with pytest.raises(HTTPException):
        await c.wrapper.check(c.product.id, c.project.id, "devops")


async def test_failed_save_retains_committed_after_state_without_raw_settings(effect_case):
    c = effect_case
    payload = {"settings": {"private": "never-export"}, "idempotency_key": "selected-key-0001", "if_none_match": None, "if_match": '"a"'}

    async def committed_then_lost(*args):
        c.config.current_revision = 2
        c.db.add(
            ConfigurationVersion(
                tenant_id=ORG_A,
                configuration_id=c.config.configuration_id,
                revision=2,
                settings={"private": "never-export"},
                inherited_fields=[],
                gaps=[],
                state="ready",
                hash="b" * 64,
            )
        )
        await c.db.commit()
        raise HTTPException(503, detail={"code": "handoff_unavailable", "private": "never-export"})

    c.backend.save = committed_then_lost
    with pytest.raises(HTTPException) as lost:
        await c.wrapper.save(c.product.id, c.project.id, "devops", **payload)
    assert lost.value.headers[effects.RECEIPT_HEADER] == effects.receipt_id(c.challenge["challenge_id"], "before")
    await c.db.rollback()
    facts = await read_effect(c.db, flow_id=c.flow.id, challenge_id=c.challenge["challenge_id"], human=c.human)
    assert facts["events"][-1]["after"]["revision"] == 2
    assert facts["outcome"] == "returned_error"
    assert "never-export" not in json.dumps(facts)
    # Human display still requires a fresh challenge for revision 2; no relaxed
    # snapshot equality is introduced by forensic effect reads.
    with pytest.raises(ValueError, match="snapshot_changed"):
        await observations.read(c.db, flow_id=c.flow.id, challenge_id=c.challenge["challenge_id"], human=c.human)


async def test_stale_etag_is_preserved_in_effect_receipt(effect_case):
    c = effect_case
    c.backend.save = AsyncMock(side_effect=HTTPException(412, detail={"code": "stale_revision"}))
    with pytest.raises(HTTPException) as error:
        await c.wrapper.save(c.product.id, c.project.id, "devops", {}, "selected-key-0001", None, '"stale"')
    assert error.value.status_code == 412
    facts = await read_effect(c.db, flow_id=c.flow.id, challenge_id=c.challenge["challenge_id"], human=c.human)
    before, after = facts["events"]
    assert after["http_status"] == 412 and after["error_code"] == "stale_revision"
    assert before["before"] == after["after"]


async def test_expired_effect_window_preserves_history_but_not_revoked_product_authority(effect_case):
    c = effect_case
    await c.wrapper.check(c.product.id, c.project.id, "devops")
    parent = await c.db.get(OrchestrationDecision, c.challenge["challenge_id"])
    after_window = c.now + timedelta(hours=2)
    facts = await historical_effect(c.db, parent=parent, expected=c.expected, dispatch=c.action, now=after_window)
    assert facts["outcome"] == "returned"
    assert facts["release_check_completed"] is facts["native_qualified"] is False
    c.product.owner_user_id = "replacement-owner"
    await c.db.commit()
    with pytest.raises(ValueError, match="not_found"):
        await historical_effect(c.db, parent=parent, expected=c.expected, dispatch=c.action, now=after_window)


@pytest.mark.parametrize("change", ["outside_window", "actor", "qualification", "configuration_hash", "missing_after_field"])
async def test_effect_history_rejects_tampered_canonical_facts(effect_case, change):
    c = effect_case
    await c.wrapper.check(c.product.id, c.project.id, "devops")
    phase = "before" if change in {"outside_window", "actor"} else "after"
    row = await c.db.get(OrchestrationDecision, effects.receipt_id(c.challenge["challenge_id"], phase))
    value = json.loads(row.reason)
    if change == "outside_window":
        value["recorded_at"] = c.challenge["expires_at"]
    elif change == "actor":
        value["actor_id"] = "other-observer"
    elif change == "qualification":
        value["native_qualified"] = True
    elif change == "configuration_hash":
        value["after"]["sha256"] = "0" * 64
    else:
        del value["readiness"]
    # The persisted decision is append-only. Simulate a corrupt read without
    # disabling that boundary or changing the retained original evidence.
    bad = SimpleNamespace(**{column.key: getattr(row, column.key) for column in row.__table__.columns})
    bad.reason = json.dumps(value)

    async def get(model, identity, **kwargs):
        return bad if model is OrchestrationDecision and identity == row.id else await c.db.get(model, identity, **kwargs)

    db = SimpleNamespace(get=get, scalar=c.db.scalar)
    parent = await c.db.get(OrchestrationDecision, c.challenge["challenge_id"])
    with pytest.raises(ValueError, match="l5_effect_unverifiable"):
        await historical_effect(db, parent=parent, expected=c.expected, dispatch=c.action, now=c.now + timedelta(hours=2))


async def test_effect_export_is_consumed_by_current_release_journal(effect_case, monkeypatch, db_session_factory):
    c = effect_case
    await c.wrapper.check(c.product.id, c.project.id, "devops")
    from src.orchestration.operations_release_job import NativeReleaseJob

    monkeypatch.setattr(exports, "job_snapshot", AsyncMock(return_value=(c.action, c.report)))
    guard = NativeReleaseJob(db_session_factory, provider=c.provider)
    document = await export_effect(guard, c.expected, {"run_id": "72", "run_attempt": "1", "actor_id": "789"})
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "L5-services" / "scripts"))
    import qualification_request_effects as collector

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
        "ADP_CONTRACT_DIGEST": c.expected.job.contract_digest,
        "ADP_CORRELATION": c.expected.job.correlation,
        "L5_RELEASE_ROLE_ARN": c.expected.job.role_arn,
        "L5_MUTATION_ROLE_ARN": c.expected.job.mutation_role_arn,
        "ADP_GATEWAY_BASE_URL": "https://gateway.example.invalid",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://pipeline.actions.githubusercontent.com/token",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "fictional-request-token",
    }
    sent = []

    def open_request(request, **kwargs):
        sent.append(request)
        response = io.BytesIO(json.dumps({"value": "current-oidc"} if len(sent) == 1 else document).encode())
        response.status = 200
        return response

    records = []
    journal = SimpleNamespace(binding={"context": collector.release_identity(environment)}, append=lambda kind, value: records.append((kind, value)))
    values = collector.collect_into(
        journal, [c.expected.model_dump(exclude={"job"})], environment=environment, opener=SimpleNamespace(open=open_request)
    )
    assert values[0].facts == document and records[0][0] == "request-effect-export"
    assert document["effect"]["release"]["definition_revision"] == "b" * 40
    assert document["effect"]["release"]["dispatch_revision"] == "e" * 40


async def test_existing_replay_records_exact_run_and_never_dispatches(effect_case):
    from l5_services.models.evidence import AssessmentResult
    from l5_services.models.receipt import RunReceipt
    from l5_services.models.run import Run

    c = effect_case
    inputs = {"service_id": "devops", "configuration_revision": 1, "binding_id": c.binding.binding_id, "capability": "release_readiness"}
    run = Run(
        tenant_id=ORG_A,
        run_id="existing-run",
        product_id=c.product.id,
        project_id=c.project.id,
        service_id="devops",
        configuration_id=c.config.configuration_id,
        configuration_revision=1,
        binding_id=c.binding.binding_id,
        binding_generation=1,
        trigger_id="manual",
        input=inputs,
        admission={"exclusions": [], "scope": {"tenant_id": ORG_A, "product_id": c.product.id, "project_id": c.project.id}},
        deadline_at=c.now + timedelta(hours=1),
    )
    c.db.add(run)
    await c.db.flush()
    c.db.add_all(
        [
            AssessmentResult(tenant_id=ORG_A, result_id="existing-result", run_id=run.run_id, version=1, payload={}),
            RunReceipt(
                tenant_id=ORG_A,
                product_id=c.product.id,
                project_id=c.project.id,
                service_id="devops",
                actor_id=APPROVER,
                operation="submit",
                idempotency_key="selected-key-0001",
                run_id=run.run_id,
                request_hash=effects.digest({"input": inputs, "event_id": None, "exclusions": []}),
                expires_at=c.now + timedelta(days=1),
            ),
        ]
    )
    await c.db.commit()
    challenge = await observations.prepare(
        c.db,
        flow_id=c.flow.id,
        human=c.human,
        request=c.request.model_copy(update={"subject": "result", "result_id": "existing-result", "request_key": "replay-fixture-0001"}),
    )
    await c.db.commit()
    request = Request(
        {"type": "http", "method": "POST", "path": "/runs", "headers": [(effects.HEADER.lower().encode(), challenge["challenge_id"].encode())]}
    )
    c.backend.run_source_rights = AsyncMock(return_value=[run])
    c.backend.publish = AsyncMock()
    wrapper = effects.EffectBackend(c.backend, request, Response())
    returned = await wrapper.submit(c.product.id, c.project.id, "devops", inputs, "selected-key-0001")
    assert returned.run_id == run.run_id
    c.backend.publish.assert_not_awaited()
    facts = await read_effect(c.db, flow_id=c.flow.id, challenge_id=challenge["challenge_id"], human=c.human)
    assert facts["events"][0]["replay"]["run_id"] == facts["events"][1]["returned_run_id"] == run.run_id
    assert facts["outcome"] == "returned"


@pytest.mark.parametrize("boundary", ["wrong_product", "unaccepted_subject", "machine_session", "expired_window"])
async def test_effect_refuses_changed_scope_before_backend_call(effect_case, monkeypatch, boundary):
    c = effect_case
    if boundary == "machine_session":
        monkeypatch.setattr(effects, "human_session", AsyncMock(side_effect=HTTPException(403)))
    elif boundary == "expired_window":
        c.human = HumanControlSession(user_id=APPROVER, tenant_id=ORG_A, expires_at=c.now - timedelta(seconds=1))
        monkeypatch.setattr(effects, "human_session", AsyncMock(return_value=c.human))
    call = c.wrapper.submit if boundary == "unaccepted_subject" else c.wrapper.check
    arguments = (
        (c.product.id, c.project.id, "devops", {}, "new-key-00000001")
        if boundary == "unaccepted_subject"
        else ("other-product" if boundary == "wrong_product" else c.product.id, c.project.id, "devops")
    )
    with pytest.raises(HTTPException):
        await call(*arguments)
    assert not list(await c.db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == effects.KIND)))


async def test_discovery_waits_for_same_run_effect_without_future_fixture_ids(effect_case, monkeypatch, db_session_factory):
    from src.orchestration.l5_observation_discovery import DiscoverObservation, discover
    from src.orchestration.operations_release_job import NativeReleaseJob

    c = effect_case
    monkeypatch.setattr(exports, "job_snapshot", AsyncMock(return_value=(c.action, c.report)))
    guard = NativeReleaseJob(db_session_factory, provider=c.provider)
    claims = {"run_id": "72", "run_attempt": "1", "actor_id": "789"}
    body = DiscoverObservation(
        job=c.expected.job,
        selection=c.expected.selection,
        preparer_id=APPROVER,
        observer_id=APPROVER,
        evidence="effect",
        operation="check",
        request_sha256=effects.digest({}),
    )
    pending = await discover(guard, body, claims)
    assert pending["status"] == "pending" and pending["selector"] is None
    await c.wrapper.check(c.product.id, c.project.id, "devops")
    ready = await discover(guard, body, claims)
    assert ready["status"] == "ready"
    assert ready["selector"] == c.expected.model_dump(exclude={"job"})
    exported = await export_effect(guard, c.expected, claims)
    # Both source-owned calls happen after the workflow starts; fixture owner
    # and exact readback selection contain no future challenge identifiers.
    assert "challenge_id" not in body.model_dump() and "snapshot_sha256" not in body.model_dump()
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "L5-services" / "scripts"))
    import qualification_observation_wait as waiter

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
        "ADP_CONTRACT_DIGEST": c.expected.job.contract_digest,
        "ADP_CORRELATION": c.expected.job.correlation,
        "L5_RELEASE_ROLE_ARN": c.expected.job.role_arn,
        "L5_MUTATION_ROLE_ARN": c.expected.job.mutation_role_arn,
        "ADP_GATEWAY_BASE_URL": "https://gateway.example.invalid",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://pipeline.actions.githubusercontent.com/token",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "fictional-request-token",
    }
    documents = iter([{"value": "oidc"}, pending, {"value": "oidc"}, ready, {"value": "oidc"}, exported])

    def open_request(request, **kwargs):
        response = io.BytesIO(json.dumps(next(documents)).encode())
        response.status = 200
        return response

    records, waits = [], []
    journal = SimpleNamespace(binding={"context": waiter.release_identity(environment)}, append=lambda kind, value: records.append((kind, value)))
    selected = body.model_dump(exclude={"job"})
    values = waiter.collect_by_scope(journal, [selected], environment=environment, opener=SimpleNamespace(open=open_request), sleep=waits.append)
    assert values[0].facts == exported and waits == [5]
    assert [kind for kind, _ in records] == ["observation-awaiting-human", "request-effect-export"]


async def test_discovery_refuses_ambiguous_completed_effects(effect_case, monkeypatch, db_session_factory):
    from src.orchestration.l5_observation_discovery import DiscoverObservation, discover
    from src.orchestration.operations_release_job import NativeReleaseJob

    c = effect_case
    await c.wrapper.check(c.product.id, c.project.id, "devops")
    second = await observations.prepare(
        c.db, flow_id=c.flow.id, human=c.human, request=c.request.model_copy(update={"request_key": "second-observation-0001"})
    )
    await c.db.commit()
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/configuration/check",
            "headers": [(effects.HEADER.lower().encode(), second["challenge_id"].encode())],
        }
    )
    await effects.EffectBackend(c.backend, request, Response()).check(c.product.id, c.project.id, "devops")
    monkeypatch.setattr(exports, "job_snapshot", AsyncMock(return_value=(c.action, c.report)))
    guard = NativeReleaseJob(db_session_factory, provider=c.provider)
    body = DiscoverObservation(
        job=c.expected.job,
        selection=c.expected.selection,
        preparer_id=APPROVER,
        observer_id=APPROVER,
        evidence="effect",
        operation="check",
        request_sha256=effects.digest({}),
    )
    with pytest.raises(ValueError, match="discovery_ambiguous"):
        await discover(guard, body, {"run_id": "72", "run_attempt": "1", "actor_id": "789"})


async def test_installed_mcp_check_records_same_canonical_effect(effect_case, compiled_dispatch_catalogue):  # noqa: F811
    from src.internal.tools_l5 import dispatch_l5

    c = effect_case
    response = await dispatch_l5(
        "l5___check_configuration",
        {
            "product_id": c.product.id,
            "project_id": c.project.id,
            "service_id": "devops",
        },
        c.wrapper,
    )
    assert response["structuredContent"]["http_status"] == 200
    facts = await read_effect(c.db, flow_id=c.flow.id, challenge_id=c.challenge["challenge_id"], human=c.human)
    assert facts["events"][-1]["response_sha256"] == effects.digest(response["structuredContent"]["data"])
    assert facts["provenance"] == "authenticated-canonical-request-effect"


async def test_real_save_then_fresh_human_observation_exports_returned_revision(effect_case, monkeypatch, db_session_factory):
    from l5_services.api import configuration as configuration_api
    from l5_services.api.configuration import view

    from src import l5_integration
    from src.orchestration.l5_observation_contract import RecordObservation
    from src.orchestration.operations_release_job import NativeReleaseJob

    c = effect_case
    # Fixture Product/profile values are provided at their boundary. The actual
    # Gateway save, ConfigurationStore edit/append, SQL commit, canonical route,
    # independent effect readback and human observation/export are exercised.
    baseline = ConfigurationVersion(
        tenant_id=ORG_A,
        configuration_id=c.config.configuration_id,
        revision=2,
        settings={"profile_ref": {"record_revision": "1"}, "visibility": "only_me", "enabled": False},
        inherited_fields=[],
        gaps=[],
        state="ready",
        hash="2" * 64,
    )
    c.db.add(baseline)
    c.config.current_revision = 2
    await c.db.commit()
    initial = await observations.prepare(
        c.db, flow_id=c.flow.id, human=c.human, request=c.request.model_copy(update={"request_key": "before-save-000001"})
    )
    await c.db.commit()

    async def current(product_id, project_id, service_id, **kwargs):
        return await c.backend.store.current(product_id, project_id, service_id)

    async def handoff(configuration, version, *args):
        configuration.handoff_state = "linked"
        await c.db.commit()
        return view(configuration, version)

    c.backend.current = current
    c.backend.handoff = handoff
    c.backend.authorize_settings = AsyncMock(return_value=[])
    c.backend.profile_inputs = AsyncMock(return_value=(c.relationship, baseline.settings["profile_ref"], {}))
    c.backend.store._readiness = AsyncMock(return_value=("ready", [], {}))
    monkeypatch.setattr(l5_integration.product_routes, "get_revision", AsyncMock(return_value={"revision": 1}))
    monkeypatch.setattr(configuration_api, "populate_settings", lambda profile, ref, sources, choices, service: ({**choices, "profile_ref": ref}, []))
    http_request = Request(
        {"type": "http", "method": "PUT", "path": "/configuration", "headers": [(effects.HEADER.lower().encode(), initial["challenge_id"].encode())]}
    )
    wrapper = effects.EffectBackend(c.backend, http_request, Response())
    app = FastAPI()
    app.include_router(create_router(lambda: wrapper))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put(
            f"/v1/products/{c.product.id}/projects/{c.project.id}/services/devops/configuration",
            json={"purpose": "Updated governed review"},
            headers={"Idempotency-Key": "selected-save-key-01", "If-Match": '"' + baseline.hash + '"'},
        )
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["revision"] == 3
    before_effect = await read_effect(c.db, flow_id=c.flow.id, challenge_id=initial["challenge_id"], human=c.human)
    returned = before_effect["events"][-1]["returned_configuration"]
    assert returned["revision"] == saved["revision"] and returned["sha256"] == saved["hash"]
    with pytest.raises(ValueError, match="snapshot_changed"):
        await observations.record(
            c.db,
            flow_id=c.flow.id,
            challenge_id=initial["challenge_id"],
            human=c.human,
            body=RecordObservation(
                snapshot_sha256=initial["snapshot_sha256"], observation="displayed", displayed_configuration_revision=saved["revision"]
            ),
        )
    await c.db.commit()
    # The actual signed-in human observes a newly prepared snapshot of revision3.
    # No effect receipt claims a display, and no first-challenge field is changed.
    fresh = await observations.prepare(
        c.db, flow_id=c.flow.id, human=c.human, request=c.request.model_copy(update={"request_key": "after-save-000001"})
    )
    assert fresh["snapshot"]["selection"]["configuration_revision"] == returned["revision"]
    assert fresh["snapshot"]["selection"]["configuration_sha256"] == returned["sha256"]
    await observations.record(
        c.db,
        flow_id=c.flow.id,
        challenge_id=fresh["challenge_id"],
        human=c.human,
        body=RecordObservation(
            snapshot_sha256=fresh["snapshot_sha256"], observation="displayed", displayed_configuration_revision=returned["revision"]
        ),
    )
    await c.db.commit()
    monkeypatch.setattr(exports, "job_snapshot", AsyncMock(return_value=(c.action, c.report)))
    guard = NativeReleaseJob(db_session_factory, provider=c.provider)
    expected = exports.ExportObservation(
        job=c.expected.job,
        challenge_id=fresh["challenge_id"],
        snapshot_sha256=fresh["snapshot_sha256"],
        selection=c.expected.selection.model_copy(update={"configuration_revision": returned["revision"]}),
    )
    exported = await exports.export_observation(guard, expected, {"run_id": "72", "run_attempt": "1", "actor_id": "789"})
    human = exported["observation"]
    assert human["selection"]["configuration_id"] == returned["configuration_id"]
    assert human["observed"]["displayed_configuration_revision"] == returned["revision"]
    assert human["release"] == before_effect["release"]
    assert human["challenge_id"] != before_effect["challenge_id"]
    assert exported["release_check_completed"] is exported["native_qualified"] is False


@pytest.fixture
async def submission_case(effect_case):
    from l5_services.models.run import Run

    from src.orchestration.l5_observation_contract import PrepareObservation

    c = effect_case
    request = PrepareObservation.model_validate({**c.request.model_dump(), "subject": "run_submission", "request_key": "devops-ui-submit-0001"})
    c.challenge = await observations.prepare(c.db, flow_id=c.flow.id, human=c.human, request=request)
    await c.db.commit()
    c.inputs = {
        "service_id": "devops",
        "configuration_revision": 1,
        "binding_id": c.binding.binding_id,
        "capability": "investigation",
        "devops_input": {"kind": "investigation", "question": "synthetic"},
    }
    c.receipt = "devops-ui-request-0001"
    c.http_request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/runs",
            "headers": [(effects.HEADER.lower().encode(), c.challenge["challenge_id"].encode()), (b"x-adp-observation-operation", b"submit")],
        }
    )

    async def submit(product, project, service, inputs, key, *, replay_only):
        assert not replay_only and key == c.receipt
        run = Run(
            tenant_id=ORG_A,
            run_id="human-ui-run",
            product_id=product,
            project_id=project,
            service_id=service,
            configuration_id=c.config.configuration_id,
            configuration_revision=1,
            binding_id=c.binding.binding_id,
            binding_generation=1,
            trigger_id="manual",
            input=inputs,
            admission={"scope": {"tenant_id": ORG_A, "product_id": product, "project_id": project}},
            deadline_at=c.now + timedelta(hours=1),
        )
        c.db.add(run)
        await c.db.flush()
        from l5_services.models.receipt import RunReceipt

        c.db.add(
            RunReceipt(
                tenant_id=ORG_A,
                product_id=product,
                project_id=project,
                service_id=service,
                actor_id=APPROVER,
                operation="submit",
                idempotency_key=key,
                run_id=run.run_id,
                request_hash=effects.digest({"input": inputs, "event_id": None, "exclusions": []}),
                expires_at=run.deadline_at,
            )
        )
        await c.db.commit()
        return run

    c.backend.submit = AsyncMock(side_effect=submit)
    c.wrapper = effects.EffectBackend(c.backend, c.http_request, Response())
    return c


async def test_fresh_ui_submit_needs_its_own_human_assertion_and_exact_canonical_effect(submission_case):
    from src.orchestration.l5_observation_contract import RecordObservation

    c = submission_case
    run = await c.wrapper.submit(c.product.id, c.project.id, "devops", c.inputs, c.receipt)
    body = RecordObservation(snapshot_sha256=c.challenge["snapshot_sha256"], observation="submitted", submitted_run_id=run.run_id)
    recorded = await observations.record(c.db, flow_id=c.flow.id, challenge_id=c.challenge["challenge_id"], human=c.human, body=body)
    assert recorded["observation_receipt"]["observed"]["submitted_run_id"] == run.run_id
    assert recorded["native_qualified"] is recorded["release_check_completed"] is False
    facts = await read_effect(c.db, flow_id=c.flow.id, challenge_id=c.challenge["challenge_id"], human=c.human)
    assert facts["operation"] == "submit" and facts["events"][-1]["returned_run_id"] == run.run_id
    assert facts["request_sha256"] == effects.digest({"input": c.inputs, "idempotency_key": c.receipt})
    with pytest.raises(HTTPException) as duplicate:
        await c.wrapper.submit(c.product.id, c.project.id, "devops", c.inputs, c.receipt)
    assert duplicate.value.detail["code"] == "l5_effect_already_attempted"
    c.backend.submit.assert_awaited_once()


@pytest.mark.parametrize("boundary", ["missing_effect", "wrong_run", "wrong_binding", "wrong_configuration"])
async def test_ui_assertion_cannot_invent_or_rebind_submission(submission_case, boundary):
    from l5_services.models.run import Run

    from src.orchestration.l5_observation_contract import RecordObservation
    from src.orchestration.l5_observation_scope import ObservationError

    c = submission_case
    if boundary != "missing_effect":
        await c.wrapper.submit(c.product.id, c.project.id, "devops", c.inputs, c.receipt)
    if boundary in {"wrong_binding", "wrong_configuration"}:
        run = await c.db.get(Run, (ORG_A, "human-ui-run"))
        if boundary == "wrong_binding":
            run.binding_id = "another-binding"
        else:
            run.input = {**run.input, "configuration_revision": 2}
        await c.db.commit()
    body = RecordObservation(
        snapshot_sha256=c.challenge["snapshot_sha256"],
        observation="submitted",
        submitted_run_id="foreign-run" if boundary == "wrong_run" else "human-ui-run",
    )
    with pytest.raises(ObservationError, match="unverifiable"):
        await observations.record(c.db, flow_id=c.flow.id, challenge_id=c.challenge["challenge_id"], human=c.human, body=body)


async def test_submit_is_rejected_before_effect_for_changed_original_binding(submission_case):
    c = submission_case
    with pytest.raises(HTTPException) as rejected:
        await c.wrapper.submit(c.product.id, c.project.id, "devops", {**c.inputs, "binding_id": "foreign"}, c.receipt)
    assert rejected.value.detail["code"] == "l5_effect_selection_mismatch"
    c.backend.submit.assert_not_awaited()


async def test_uncertain_ui_submit_keeps_original_effect_and_never_retries(submission_case):
    c = submission_case
    submit = c.backend.submit.side_effect

    async def committed_then_lost(*args, **kwargs):
        await submit(*args, **kwargs)
        raise RuntimeError("Response lost")

    c.backend.submit.side_effect = committed_then_lost
    with pytest.raises(RuntimeError):
        await c.wrapper.submit(c.product.id, c.project.id, "devops", c.inputs, c.receipt)
    facts = await read_effect(c.db, flow_id=c.flow.id, challenge_id=c.challenge["challenge_id"], human=c.human)
    assert facts["outcome"] == "unknown"
    assert len(facts["events"]) == 2
    with pytest.raises(HTTPException) as duplicate:
        await c.wrapper.submit(c.product.id, c.project.id, "devops", c.inputs, c.receipt)
    assert duplicate.value.detail["code"] == "l5_effect_already_attempted"
    c.backend.submit.assert_awaited_once()


@pytest.mark.parametrize(
    "boundary", ["invalid_json", "list_payload", "wrong_contract", "naive_timestamp", "after_observation", "missing_submission", "receipt_mismatch"]
)
async def test_submission_assertion_rejects_malformed_or_late_effect(submission_case, boundary):
    from src.orchestration.l5_observation_contract import RecordObservation
    from src.orchestration.l5_observation_scope import ObservationError

    c = submission_case
    await c.wrapper.submit(c.product.id, c.project.id, "devops", c.inputs, c.receipt)
    parent = await c.db.get(OrchestrationDecision, c.challenge["challenge_id"])
    row = await c.db.get(OrchestrationDecision, effects.receipt_id(parent.id, "after"))
    value = json.loads(row.reason)
    observed_at = effects.datetime.now(effects.UTC)
    if boundary == "wrong_contract":
        value["contract"] = "other/v1"
    elif boundary == "naive_timestamp":
        value["recorded_at"] = observed_at.replace(tzinfo=None).isoformat()
    elif boundary == "after_observation":
        value["recorded_at"] = (observed_at + timedelta(seconds=1)).isoformat()
    elif boundary == "missing_submission":
        del value["submission"]
    elif boundary == "receipt_mismatch":
        value["submission"]["idempotency_key_sha256"] = "0" * 64
    bad = SimpleNamespace(**{column.key: getattr(row, column.key) for column in row.__table__.columns})
    bad.reason = "{bad" if boundary == "invalid_json" else "[]" if boundary == "list_payload" else json.dumps(value)

    async def get(model, identity, **kwargs):
        return bad if model is OrchestrationDecision and identity == row.id else await c.db.get(model, identity, **kwargs)

    db = SimpleNamespace(get=get, scalars=c.db.scalars)
    body = RecordObservation(snapshot_sha256=c.challenge["snapshot_sha256"], observation="submitted", submitted_run_id="human-ui-run")
    with pytest.raises(ObservationError, match="l5_effect_unverifiable"):
        await effects.verify_submission(db, parent=parent, data=json.loads(parent.reason), body=body, now=observed_at)
