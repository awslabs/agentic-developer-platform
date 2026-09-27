"""Task preferences use existing CRUD without authorizing legacy dispatch."""

import pytest

from src.admin.persona_models import catalogue_service as catalogue
from src.admin.persona_models import service
from src.admin.persona_models.catalogue import persona_harness_contract_revision
from src.tasks.personas import TASK_PERSONAS

from .conftest import DEFAULT_MODEL_ID, DEST_ACCOUNT, DEST_REGION, make_evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", tuple(TASK_PERSONAS))
async def test_task_selection_requires_its_exact_transport_evidence(db_session, persona):
    profile = TASK_PERSONAS[persona]
    kwargs = dict(
        org_id="tenant",
        principal_kind="human",
        canonical_principal_id="user",
        persona_key=persona,
        model=DEFAULT_MODEL_ID,
        account_id=DEST_ACCOUNT,
        region=DEST_REGION,
    )
    assert (await catalogue.validate_selection(db_session, **kwargs)).reason == "probing_disabled"
    # A genuine legacy SDK evidence row cannot certify the Task transport.
    db_session.add(make_evidence())
    await db_session.flush()
    assert (await catalogue.validate_selection(db_session, **kwargs)).reason == "probing_disabled"
    db_session.add(
        make_evidence(
            compatibility_class=profile.compatibility_class,
            harness_contract_revision=profile.harness_contract_revision,
            request_shape_sha256=profile.request_shape_sha256,
        )
    )
    await db_session.flush()
    chosen = await catalogue.validate_selection(db_session, **kwargs)
    assert chosen.canonical_model_id == DEFAULT_MODEL_ID
    assert chosen.compatibility_class == profile.compatibility_class
    assert chosen.harness_contract_revision == profile.harness_contract_revision
    assert persona in service.CONFIGURABLE_PERSONAS
    assert persona_harness_contract_revision(persona) == profile.harness_contract_revision
    models = await catalogue.build_model_catalogue(db_session, persona_key=persona, account_id=DEST_ACCOUNT, region=DEST_REGION)
    assert next(row for row in models if row.canonical_model_id == DEFAULT_MODEL_ID).selectable


@pytest.mark.asyncio
async def test_codex_task_profile_never_relabels_an_openai_model(db_session):
    result = await catalogue.validate_selection(
        db_session,
        org_id="tenant",
        principal_kind="human",
        canonical_principal_id="user",
        persona_key="agent-task-codex-developer",
        model="openai.gpt-6-sol",
        require_evidence=False,
    )
    assert result.reason == "harness_incompatible"


def test_probe_body_cannot_mutate_registry():
    profile = TASK_PERSONAS["agent-task-investigator"]
    digest = profile.request_shape_sha256
    profile.probe_body["max_tokens"] = 999999
    assert profile.probe_body["max_tokens"] == 16
    assert profile.request_shape_sha256 == digest


@pytest.mark.asyncio
async def test_task_selection_without_provider_receipt_is_unproven(db_session):
    persona = "agent-task-investigator"
    profile = TASK_PERSONAS[persona]
    db_session.add(
        make_evidence(
            compatibility_class=profile.compatibility_class,
            harness_contract_revision=profile.harness_contract_revision,
            request_shape_sha256=profile.request_shape_sha256,
            provider_request_id="",
        )
    )
    await db_session.flush()
    result = await catalogue.validate_selection(
        db_session,
        org_id="tenant",
        principal_kind="human",
        canonical_principal_id="user",
        persona_key=persona,
        model=DEFAULT_MODEL_ID,
        account_id=DEST_ACCOUNT,
        region=DEST_REGION,
    )
    assert result.reason == "probing_disabled"


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", tuple(TASK_PERSONAS))
async def test_task_preference_actual_http_crud_with_real_evidence(session, persona, monkeypatch):
    from unittest.mock import AsyncMock

    from httpx import ASGITransport, AsyncClient

    from src.admin.persona_models.self_routes import router
    from src.shared.models.organization import User

    from .conftest import MEMBER_ID, MEMBER_SUB, ORG_ID, build_app, member_context

    session.add(User(id=MEMBER_ID, org_id=ORG_ID, team_id="team-5420-ml", name="member", email="member@example.com", cognito_sub=MEMBER_SUB))
    profile = TASK_PERSONAS[persona]
    session.add(
        make_evidence(
            compatibility_class=profile.compatibility_class,
            harness_contract_revision=profile.harness_contract_revision,
            request_shape_sha256=profile.request_shape_sha256,
        )
    )
    await session.commit()
    monkeypatch.setattr(
        "src.admin.persona_models.catalogue_routes.resolve_effective_destination", AsyncMock(return_value=(DEST_ACCOUNT, DEST_REGION))
    )
    app = build_app(session, member_context())
    app.include_router(router)
    path = f"/me/persona-models/{persona}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.put(path, json={"model": DEFAULT_MODEL_ID})
        assert created.status_code == 200, created.text
        assert created.json()["effective_model_id"] == DEFAULT_MODEL_ID
        assert created.json()["compatibility_class"] == profile.compatibility_class
        assert created.json()["harness_contract_revision"] == profile.harness_contract_revision
        listed = await client.get("/me/persona-models")
        row = next(row for row in listed.json()["entries"] if row["persona_key"] == persona)
        assert row["saved_model_id"] == DEFAULT_MODEL_ID
        updated = await client.put(path, json={"model": DEFAULT_MODEL_ID, "expected_revision": 1})
        assert updated.status_code == 200, updated.text
        stale = await client.put(path, json={"model": DEFAULT_MODEL_ID, "expected_revision": 1})
        assert stale.status_code == 409
        explained = await client.get(f"/me/persona-models/explain/{persona}")
        assert explained.status_code == 200
        assert explained.json()["effective_model_id"] == DEFAULT_MODEL_ID
        reset = await client.request("DELETE", path, json={"expected_revision": 2})
        assert reset.status_code == 200, reset.text
        listed = await client.get("/me/persona-models")
        row = next(row for row in listed.json()["entries"] if row["persona_key"] == persona)
        assert row["saved_model_id"] is None
        # A Task persona never falls back to the legacy Claude SDK class default.
        assert row["effective_model_id"] is None
