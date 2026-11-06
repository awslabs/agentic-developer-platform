"""Mounted Security configuration scenarios with isolated Product and L5 state."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import func, select

from l5_services.models.base import Base as L5Base
from l5_services.models.configuration import Configuration, ConfigurationReceipt, ConfigurationVersion
from src.auth.dependencies import get_current_user
from src.products.models import ProductChange, Project
from src.shared.models.organization import Organization, User
from tests.products.membership import add_team_member
from tests.products.test_i05 import REQUEST


@pytest.mark.asyncio
@pytest.mark.parametrize("product_change", ["classification", "repository", "account"])
async def test_sibling_service_handoffs_preserve_prior_profile_inputs(configuration_scenario, product_change):
    from src.auth.aws_connection_authority import connection_binding
    from src.l5_readiness import current_profile_revision
    from src.shared.models.vault import UserCredential

    scenario = configuration_scenario
    actor = scenario["actors"][0]
    product = await scenario["prepare"](actor, "shared-services", "internal",
                                        ["code_review", "finops", "well_architected", "devops", "refactor"])
    client = scenario["client"]
    connection_id = f"{actor.org_id}-connection-one"
    scenario["typed_connections"].update({"enabled": True, "provider": "aws", "project_id": product["project_id"]})
    scenario["permissions"][(actor.org_id, product["project_id"])].add((connection_id, connection_id))
    connection = {"tenant_id": actor.org_id, "project_id": product["project_id"], "source_connection_id": connection_id,
                  "record_type": "connection", "record_id": connection_id, "record_revision": "revision-1"}
    async with scenario["sessions"]() as session:
        credential = UserCredential(
            id=connection_id, org_id=actor.org_id, user_id=actor.user_id, service="aws", credential_type="aws_role",
            label="Fixture AWS role", secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:fixture",
            strict=False, aws_external_id="example-external-id", aws_verified_at=scenario["fixed_time"],
            aws_verification_attempt="example-attempt", aws_verified_version_id="revision-1",
            scopes={"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Example", "status": "verified"},
        )
        credential.aws_verified_binding = connection_binding(credential)
        session.add(credential)
        await session.commit()
    choices = {
        "security": {"capabilities": ["code_review"]},
        "finops": {"service_id": "finops", "capabilities": ["cost_inquiry"], "finops": {}},
        "well_architected": {"service_id": "well_architected", "capabilities": ["architecture_review"],
                             "well_architected": {"review_mode": "source_only", "pillars": ["SECURITY"]}},
        "devops": {"service_id": "devops", "capabilities": ["investigation"], "enabled": False,
                   "triggers": [], "devops": {"space_mode": "create", "accounts": [], "integrations": []}},
        "transform": {"service_id": "transform", "capabilities": ["refactor"], "enabled": False,
                      "source_refs": [product["references"][0]], "overrides": [
                          {"field": "source_refs", "value": [product["references"][0]], "reason": "Select repository"}],
                      "transform": {"connection_ref": connection, "region": "us-east-1",
                                    "executor_binding_ref": {**connection, "record_type": "executor_binding", "record_id": "example-executor"},
                                    "test_policy_ref": {**connection, "record_type": "test_policy", "record_id": "example-policy"},
                                    "read_paths": ["src"], "write_paths": ["src/main.py"], "excluded_paths": [".git"],
                                    "goal_defaults": {"kind": "refactor", "summary": "Simplify code"}}},
    }
    saved = {}
    for service_id, body in choices.items():
        path = product["path"].replace("/security/", f"/{service_id}/")
        response = await client.put(path, json=body, headers={
            "Idempotency-Key": f"shared_services_create_{service_id}", "If-None-Match": "*",
        })
        assert response.status_code == 201, response.text
        assert response.json()["handoff_state"] == "linked"
        saved[service_id] = response.json()
        for prior in saved:
            if prior in {"devops", "transform"}:
                continue
            checked = await client.post(product["path"].replace("/security/", f"/{prior}/") + "/check")
            assert checked.status_code == 200, checked.text
            assert "profile_revision_stale" not in checked.json()["gaps"], (service_id, prior, checked.json())
        profile = (await client.get(product["profile_path"])).json()
        async with scenario["sessions"]() as session:
            for original in saved.values():
                configuration = await session.get(Configuration, (actor.org_id, original["configuration_id"]))
                version = await session.get(ConfigurationVersion, (actor.org_id, original["configuration_id"], 1))
                assert await current_profile_revision(session, configuration, version, profile)
    if product_change == "classification":
        await scenario["revise_profile"](product, "restricted")
    else:
        profile = (await client.get(product["profile_path"])).json()
        configuration = profile["configuration"]
        if product_change == "repository":
            configuration["basic"]["repositories"][0]["record_revision"] = "commit-2"
        else:
            configuration["advanced"] = {**(configuration.get("advanced") or {}), "environments": [
                {**connection, "record_type": "environment", "record_id": "another-account-environment"}]}
        changed = await client.patch(product["profile_path"] + "/configuration", json={"configuration": configuration}, headers={
            "Idempotency-Key": f"shared_services_change_{product_change}", "If-Profile-Revision": str(profile["revision"]),
        })
        assert changed.status_code == 200, changed.text
    profile = (await client.get(product["profile_path"])).json()
    for service_id, original in saved.items():
        path = product["path"].replace("/security/", f"/{service_id}/")
        historical = await client.get(path + "/versions/1")
        assert historical.json()["settings"] == original["settings"]
        if service_id not in {"devops", "transform"}:
            checked = await client.post(path + "/check")
            assert "profile_revision_stale" in checked.json()["gaps"]
        async with scenario["sessions"]() as session:
            configuration = await session.get(Configuration, (actor.org_id, original["configuration_id"]))
            version = await session.get(ConfigurationVersion, (actor.org_id, original["configuration_id"], 1))
            assert not await current_profile_revision(session, configuration, version, profile)
    assert scenario["provider_attempts"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("service_id", "capability", "selection"), [
    ("security", "code_review", "code_review"),
    ("finops", "cost_inquiry", "finops"),
    ("well_architected", "architecture_review", "well_architected"),
])
async def test_own_configuration_handoff_does_not_stale_profile(configuration_scenario, service_id, capability, selection):
    scenario = configuration_scenario
    product = await scenario["prepare"](scenario["actors"][0], service_id, "internal", [selection])
    client = scenario["client"]
    path = product["path"].replace("/security/", f"/{service_id}/")
    choices = {"capabilities": [capability]}
    if service_id != "security":
        choices.update(service_id=service_id, **{service_id: {}})
    if service_id == "well_architected":
        choices[service_id] = {"review_mode": "source_only", "pillars": ["SECURITY"]}
    created = await client.put(path, json=choices, headers={
        "Idempotency-Key": "own_handoff_create_00001", "If-None-Match": "*",
    })
    assert created.status_code == 201, created.text
    original = created.json()
    assert original["handoff_state"] == "linked"
    assert "profile_revision_stale" not in (await client.post(path + "/check")).json()["gaps"]
    changed = await scenario["revise_profile"](product, "restricted")
    assert "profile_revision_stale" in (await client.post(path + "/check")).json()["gaps"]
    saved = created
    for attempt in range(2):
        profile = (await client.get(product["profile_path"])).json()
        selected_ref = {**saved.json()["settings"]["profile_ref"], "record_revision": str(profile["revision"])}
        saved = await client.post(path + "/refresh", json={"profile_ref": selected_ref}, headers={
            "Idempotency-Key": f"own_handoff_refresh_0000{attempt}", "If-Match": saved.headers["etag"],
        })
        assert saved.status_code == 200, saved.text
        current = (await client.get(product["profile_path"])).json()
        assert current["revision"] == profile["revision"] + 1
        assert saved.json()["settings"]["profile_ref"] == selected_ref
        checked = await client.post(path + "/check")
        assert checked.status_code == 200, checked.text
        assert "profile_revision_stale" not in checked.json()["gaps"]
        assert checked.json()["provider_contacted"] is False
    historical = await client.get(path + "/versions/1")
    assert historical.status_code == 200 and historical.json()["settings"] == original["settings"]
    assert changed["configuration"]["governance"]["data_classification"] == "restricted"
    assert scenario["provider_attempts"] == []


@pytest.fixture
async def configuration_scenario(app, test_client, test_engine, db_session, db_session_factory, valid_token_context, monkeypatch):
    from src import l5_integration
    from src.products import source_routes
    from src.shared.models import base as models_base

    monkeypatch.setenv("BG_PRODUCT_PROFILE_ENABLED", "true")
    fixed_time = datetime.now(UTC).replace(microsecond=0)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_time if tz is None else fixed_time.astimezone(tz)

    monkeypatch.setattr(models_base, "datetime", FrozenDateTime)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: fixed_time)
    monkeypatch.setattr("src.product_onboarding.handoffs.utcnow", lambda: fixed_time)
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)

    other_actor = valid_token_context.model_copy(update={
        "org_id": "scenario-other-tenant", "user_id": "scenario-other-user", "team_id": "scenario-other-team",
    })
    db_session.add_all([
        Organization(id=valid_token_context.org_id, name="Scenario first workspace"),
        User(id=valid_token_context.user_id, org_id=valid_token_context.org_id, team_id=valid_token_context.team_id,
             email="scenario-first@example.test", user_kind="human"),
        Organization(id=other_actor.org_id, name="Scenario second workspace"),
        User(id=other_actor.user_id, org_id=other_actor.org_id, team_id=other_actor.team_id,
             email="scenario-second@example.test", user_kind="human"),
    ])
    await add_team_member(db_session, valid_token_context)
    await add_team_member(db_session, other_actor)
    await db_session.commit()
    current_actor = {"value": valid_token_context}
    app.dependency_overrides[get_current_user] = lambda: current_actor["value"]
    permissions = {}
    connection_reads = []
    typed_connections = {"enabled": False}
    source_reads = []
    provider_attempts = []
    product_attempts = []
    faults = {"profile": None, "sources": None, "handoff": None}

    async def forbidden_outbound_request(transport, request):
        provider_attempts.append(request.url.scheme)
        raise AssertionError("configuration must not call an external HTTP provider")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_outbound_request)

    async def authorized_connections(db, actor):
        connection_reads.append(actor.org_id)
        return [({"connection_ref": {"kind": "credential", "id": f"{actor.org_id}-connection-{suffix}"},
                  **({"provider": typed_connections["provider"]} if typed_connections.get("provider") else {}),
                  **({"record_ref": {"tenant_id": actor.org_id, "project_id": typed_connections.get("project_ids", {}).get(actor.org_id, typed_connections.get("project_id")),
                                     "source_connection_id": f"{actor.org_id}-connection-{suffix}",
                                     "record_type": "connection", "record_id": f"{actor.org_id}-connection-{suffix}",
                                     "record_revision": "revision-1"}} if typed_connections["enabled"] else {})}, None)
                for suffix in ("one", "two")]

    async def permitted_source_refs(db, actor, project_id):
        source_reads.append((actor.org_id, project_id))
        if faults["sources"] is not None:
            raise faults["sources"]
        return permissions.get((actor.org_id, project_id), set()).copy()

    product_get_profile = l5_integration.product_routes.get_profile
    product_link = l5_integration.link_l5_configuration

    async def get_profile(*args, **kwargs):
        if faults["profile"] is not None:
            raise faults["profile"]
        return await product_get_profile(*args, **kwargs)

    async def link_profile(*args, **kwargs):
        product_attempts.append("conditional-link")
        if faults["handoff"] is not None:
            raise faults["handoff"]
        return await product_link(*args, **kwargs)

    monkeypatch.setattr(l5_integration.product_routes, "get_profile", get_profile)
    monkeypatch.setattr(l5_integration, "link_l5_configuration", link_profile)
    monkeypatch.setattr(l5_integration, "authorized_connections", authorized_connections)
    monkeypatch.setattr(l5_integration, "permitted_source_refs", permitted_source_refs)
    monkeypatch.setattr(source_routes, "permitted_source_refs", permitted_source_refs)

    def switch(actor):
        current_actor["value"] = actor

    async def prepare(actor, label, classification, capabilities):
        switch(actor)
        product = await test_client.post("/v1/products", json={**REQUEST, "name": f"Security {label}"},
                                         headers={"Idempotency-Key": f"scenario_product_{label}_create_0001"})
        assert product.status_code == 201, product.text
        product_id, project_id = product.json()["product_id"], product.json()["project_id"]
        profile_path = f"/v1/products/{product_id}/projects/{project_id}"
        references = [
            {"tenant_id": actor.org_id, "project_id": project_id,
             "source_connection_id": f"{actor.org_id}-connection-{suffix}",
             "record_type": record_type, "record_id": f"{label}-{record_type}", "record_revision": revision}
            for suffix, record_type, revision in (("one", "repository_commit", "commit-1"),
                                                  ("two", "document", "document-1"))
        ]
        permissions[(actor.org_id, project_id)] = {
            (reference["source_connection_id"], reference["record_id"]) for reference in references
        }
        selected = [{"capability": capability, "ownership_mode": "managed"} for capability in capabilities]
        selected.append({"capability": "Delivery", "ownership_mode": "integrated"})
        profile = await test_client.patch(f"{profile_path}/configuration", json={"configuration": {
            "basic": {"repositories": [references[0]], "documents": [references[1]]},
            "governance": {"data_classification": classification},
            "selected_capabilities": selected,
        }}, headers={"Idempotency-Key": f"scenario_product_{label}_profile_0001", "If-Profile-Revision": "1"})
        assert profile.status_code == 200, profile.text
        return {"actor": actor, "product_id": product_id, "project_id": project_id,
                "path": f"{profile_path}/services/security/configuration", "profile_path": profile_path,
                "profile_revision": profile.json()["revision"], "references": references,
                "classification": classification, "capabilities": capabilities}

    async def revise_profile(product, classification):
        switch(product["actor"])
        current = await test_client.get(product["profile_path"])
        assert current.status_code == 200, current.text
        configuration = current.json()["configuration"]
        revision = current.json()["revision"]
        updated = await test_client.patch(f'{product["profile_path"]}/configuration', json={"configuration": {
            **configuration,
            "governance": {**(configuration.get("governance") or {}), "data_classification": classification},
        }}, headers={"Idempotency-Key": f'scenario_profile_refresh_{product["product_id"]}',
                    "If-Profile-Revision": str(revision)})
        assert updated.status_code == 200, updated.text
        assert updated.json()["revision"] == revision + 1
        return updated.json()

    try:
        yield {"client": test_client, "sessions": db_session_factory, "fixed_time": fixed_time,
               "actors": (valid_token_context, other_actor), "switch": switch, "prepare": prepare, "revise_profile": revise_profile,
               "source_reads": source_reads, "connection_reads": connection_reads,
               "provider_attempts": provider_attempts, "product_attempts": product_attempts,
               "permissions": permissions, "faults": faults, "typed_connections": typed_connections}
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        permissions.clear()
        faults.clear()


@pytest.fixture
async def transform_binding_scenario(configuration_scenario, app, monkeypatch):
    from datetime import timedelta

    from src.auth.aws_connection_authority import connection_binding
    from src.l5_service_identity import worker_id
    from src.shared.models.vault import CredentialServiceDelegation, UserCredential

    async def unqualified(tenant_id, product_id, project_id, reference):
        return {"status": "unqualified", "executor_binding_ref": reference}

    monkeypatch.setattr(app.state, "l5_transform_executor_qualification_reader", unqualified)
    scenario = configuration_scenario
    actor = scenario["actors"][0]
    product = await scenario["prepare"](actor, "transform-binding", "internal", ["refactor"])
    product["path"] = product["path"].replace("/services/security/", "/services/transform/")
    scenario["typed_connections"].update({"enabled": True, "provider": "aws", "project_id": product["project_id"]})
    connection_id = f"{actor.org_id}-connection-one"
    scenario["permissions"][(actor.org_id, product["project_id"])].add((connection_id, connection_id))
    fixed_time = scenario["fixed_time"]
    async with scenario["sessions"]() as session:
        credential = UserCredential(
            id=connection_id, org_id=actor.org_id, user_id=actor.user_id,
            service="aws", credential_type="aws_role", label="Fixture AWS role",
            secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:fixture",
            strict=False, aws_external_id="example-external-id", aws_verified_at=fixed_time,
            aws_verification_attempt="example-attempt", aws_verified_version_id="revision-1",
            expires_at=fixed_time + timedelta(days=1),
            scopes={"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Example",
                    "status": "verified"},
        )
        credential.aws_verified_binding = connection_binding(credential)
        session.add(credential)
        session.add(CredentialServiceDelegation(
            tenant_id=actor.org_id, credential_id=connection_id, service_id=worker_id("transform"),
            product_id=product["product_id"], project_id=product["project_id"], created_by=actor.user_id,
            operations=["product:read", "credential:session"],
            sources=[{"tenant_id": actor.org_id, "project_id": product["project_id"],
                      "source_connection_id": connection_id, "record_type": "credential",
                      "record_id": connection_id, "record_revision": "revision-1"}],
            capabilities=[], visibility="restricted", expires_at=fixed_time + timedelta(hours=2),
        ))
        await session.commit()
    repository = product["references"][0]
    connection = {"tenant_id": actor.org_id, "project_id": product["project_id"],
                  "source_connection_id": connection_id, "record_type": "connection",
                  "record_id": connection_id, "record_revision": "revision-1"}
    selected = {"service_id": "transform", "source_refs": [repository], "capabilities": ["refactor"],
                "enabled": False, "overrides": [
                    {"field": "source_refs", "value": [repository], "reason": "Pin selected repository"}],
                "transform": {
                    "connection_ref": connection,
                    "executor_binding_ref": {**connection, "record_type": "executor_binding", "record_id": "example-executor"},
                    "test_policy_ref": {**connection, "record_type": "test_policy", "record_id": "example-policy"},
                    "region": "example-region", "read_paths": ["src"], "write_paths": ["src/main.py"],
                    "excluded_paths": [".git"], "goal_defaults": {"kind": "refactor", "summary": "Simplify code"}}}
    saved = await scenario["client"].put(product["path"], json=selected, headers={
        "Idempotency-Key": "transform_binding_config_0001", "If-None-Match": "*"})
    assert saved.status_code == 201, saved.text
    return scenario, product, selected, saved


@pytest.mark.asyncio
async def test_transform_gateway_capability_readiness(transform_binding_scenario, app):
    scenario, product, selected, saved = transform_binding_scenario
    client, path = scenario["client"], product["path"]
    schemas = json.loads((Path(__file__).resolve().parents[4] / "docs/l5-services/contracts/v1/schemas.json").read_text())

    def validate(name, response):
        assert response.status_code == 200, response.text
        Draft202012Validator({**schemas, "$ref": "#/$defs/" + name}).validate(response.json())
        return response.json()

    initial = validate("Check", await client.post(path + "/check"))
    assert not initial["ready"] and not initial["provider_contacted"]
    assert [item["capability"] for item in initial["readiness"]] == ["refactor"]
    assert initial["readiness"][0]["status"] == "unqualified"
    assert initial["gaps"] == ["executor_unqualified"]
    options = validate("Options", await client.get(path + "/options"))
    assert [item["capability"] for item in options["readiness"]] == ["refactor"]
    assert "test_policy_ref_missing" not in options["unavailable_reasons"]
    assert "executor_unqualified" in options["unavailable_reasons"]
    assert "region_options_unavailable" not in options["unavailable_reasons"]

    missing_policy = {field: value for field, value in selected["transform"].items()
                      if field != "test_policy_ref"}
    edited = await client.put(path, json={"transform": missing_policy, "enabled": True}, headers={
        "Idempotency-Key": "missing_transform_policy_01", "If-Match": saved.headers["etag"]})
    assert edited.status_code == 200, edited.text
    missing = validate("Check", await client.post(path + "/check"))
    assert missing["readiness"][0]["status"] == "missing_input"
    assert {gap["code"] for gap in missing["readiness"][0]["gaps"]} == {
        "test_policy_ref_missing", "executor_unqualified"}
    assert missing["readiness"][0]["gaps"][0] == {
        "code": "test_policy_ref_missing", "field": "transform.test_policy_ref",
        "owner": "product", "affected_step": "test"}
    assert "test_policy_ref_missing" in validate("Options", await client.get(path + "/options"))["unavailable_reasons"]

    observed = []

    async def qualified(tenant_id, product_id, project_id, reference):
        observed.append((tenant_id, product_id, project_id, reference))
        return {"status": "qualified", "executor_binding_ref": {
            **reference, "record_revision": "revision-2"}}

    app.state.l5_transform_executor_qualification_reader = qualified
    complete = await client.put(path, json={"transform": selected["transform"]}, headers={
        "Idempotency-Key": "restored_transform_policy_01", "If-Match": edited.headers["etag"]})
    assert complete.status_code == 200, complete.text
    stale = validate("Check", await client.post(path + "/check"))
    assert not stale["ready"] and not stale["provider_contacted"]
    assert stale["gaps"] == ["executor_qualification_stale"]
    assert stale["readiness"] == [{"capability": "refactor", "status": "stale", "gaps": [{
        "code": "executor_qualification_stale", "field": "transform.executor_binding_ref",
        "owner": "executor", "affected_step": "execute"}]}]
    assert observed and all(item == (
        product["actor"].org_id, product["product_id"], product["project_id"],
        selected["transform"]["executor_binding_ref"]) for item in observed)
    options = validate("Options", await client.get(path + "/options"))
    assert "executor_qualification_stale" in options["unavailable_reasons"]
    assert "test_policy_ref_missing" not in options["unavailable_reasons"]
    assert options["readiness"] == stale["readiness"]

    async def foreign(tenant_id, product_id, project_id, reference):
        return {"status": "qualified", "executor_binding_ref": {
            **reference, "tenant_id": "foreign-tenant", "record_revision": "revision-2"}}

    async def current(tenant_id, product_id, project_id, reference):
        return {"status": "qualified", "executor_binding_ref": reference}

    app.state.l5_transform_executor_qualification_reader = current
    still_blocked = validate("Check", await client.post(path + "/check"))
    assert not still_blocked["ready"] and still_blocked["gaps"] == ["submission_unavailable"]
    assert still_blocked["readiness"][0]["status"] == "unqualified"
    app.state.l5_transform_executor_qualification_reader = foreign
    ignored = validate("Check", await client.post(path + "/check"))
    assert ignored["gaps"] == ["executor_unqualified"]
    assert ignored["readiness"][0]["status"] == "unqualified"
    submission = await client.post(path.removesuffix("/configuration") + "/runs", json={
        "service_id": "transform", "configuration_revision": complete.json()["revision"],
        "capability": "refactor"}, headers={"Idempotency-Key": "no_unqualified_submit_01"})
    assert submission.status_code == 503 and submission.json()["detail"]["code"] == "capability_unavailable"
    assert scenario["provider_attempts"] == []


@pytest.mark.asyncio
async def test_transform_gateway_owned_setup_cleanup(transform_binding_scenario, monkeypatch):
    from datetime import timedelta

    from l5_services.adapters.transform.bindings import TransformDefinitionCleanup
    from l5_services.models.binding import Binding
    from l5_services.models.setup import BindingOperation, SetupIntent, SetupWork
    from l5_services.workers.bindings import execute_setup_work
    from src import l5_integration

    scenario, product, selected, saved = transform_binding_scenario
    client, path = scenario["client"], product["path"].removesuffix("/configuration")
    actor, sessions = product["actor"], scenario["sessions"]
    identity = {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Example",
                "region": "example-region"}
    unrelated = {**selected["transform"]["connection_ref"], "record_type": "transform_definition",
                 "record_id": "unrelated-definition"}
    adopted_ref = selected["transform"]["executor_binding_ref"]
    inventory = {"unrelated-definition": {"native_resource_ref": unrelated, "provider_identity": identity},
                 adopted_ref["record_id"]: {"native_resource_ref": adopted_ref, "provider_identity": identity}}
    creates, deletes = [], []
    absence_available = False

    class Native:
        async def read(self, resource, verified):
            found = inventory.get(resource["record_id"])
            if found is not None:
                return found
            result = {"native_resource_ref": resource, "absent": True}
            if absence_available:
                result["absence_evidence_ref"] = {**resource, "record_type": "transform_absence",
                                                  "record_revision": "absence-1"}
            return result

        async def delete(self, resource, verified):
            deletes.append(resource["record_id"])
            inventory.pop(resource["record_id"], None)

    class Provider:
        async def read(self, resource, verified):
            assert resource == adopted_ref
            return {"native_resource_ref": resource, "provider_identity": identity,
                    "state": "blocked", "gaps": ["executor_unqualified"]}

        async def create(self, intent, verified):
            creates.append(intent.operation_id)
            reference = {**selected["transform"]["connection_ref"], "record_type": "transform_definition",
                         "record_id": intent.operation_id, "record_revision": "native-1"}
            receipt = {"operation_id": intent.operation_id, "intent_hash": intent.intent_hash,
                       "native_resource_ref": reference, "provider_identity": identity}
            inventory[intent.operation_id] = {"native_resource_ref": reference, "provider_identity": identity,
                                               "creation_receipt": receipt, "state": "blocked", "gaps": []}
            raise TimeoutError("create acknowledgement lost")

        async def reconcile(self, intent, verified, candidate_id):
            return inventory.get(intent.operation_id)

        async def cleanup(self, resource, verified, receipt, intent):
            return await TransformDefinitionCleanup(Native()).cleanup(resource, verified, receipt, intent)

    provider = Provider()
    create_body = {"action": "create", "configuration_revision": saved.json()["revision"]}
    unqualified = await client.post(path + "/binding-operations", json=create_body, headers={
        "Idempotency-Key": "unqualified_create_0001"})
    assert unqualified.status_code == 503 and unqualified.json()["detail"]["code"] == "provider_unqualified"
    async with sessions() as reader:
        assert await reader.scalar(select(func.count()).select_from(SetupIntent)) == 0

    original_authority = l5_integration.GatewayConfigurationBackend.binding_authority

    async def qualified_authority(backend, product_id, project_id, service_id, *, setup_action=None):
        store, verified = await original_authority(backend, product_id, project_id, service_id, setup_action=setup_action)
        verified["transform_definition_create_qualified"] = True
        return store, verified

    monkeypatch.setattr(l5_integration.GatewayConfigurationBackend, "binding_authority", qualified_authority)

    async def authorize(intent, claim):
        assert (intent.tenant_id, intent.product_id, intent.project_id) == (
            actor.org_id, product["product_id"], product["project_id"])
        assert claim.operation_id == intent.operation_id and claim.tenant_id == intent.tenant_id
        async with sessions() as session:
            backend = l5_integration.GatewayConfigurationBackend(session, actor)
            _, verified = await backend.binding_authority(product["product_id"], product["project_id"], "transform")
        assert verified["scope"] == intent.scope
        return verified, provider

    async def perform(operation_id):
        async with sessions() as session:
            work = await session.scalar(select(SetupWork).where(
                SetupWork.tenant_id == actor.org_id, SetupWork.operation_id == operation_id,
                SetupWork.completed_at.is_(None)).order_by(SetupWork.generation.desc()))
            assert work is not None
            envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation}
        assert await execute_setup_work(sessions, envelope, authorize, now=datetime.now(UTC))
        response = await client.get(path + "/binding-operations/" + operation_id)
        assert response.status_code == 200, response.text
        return response.json()

    created = await client.post(path + "/binding-operations", json=create_body, headers={
        "Idempotency-Key": "owned_create_00000001"})
    adopted = await client.post(path + "/binding-operations", json={
        "action": "link", "configuration_revision": 1, "native_resource_ref": adopted_ref}, headers={
        "Idempotency-Key": "adopt_executor_000001"})
    assert created.status_code == adopted.status_code == 202
    replay = await client.post(path + "/binding-operations", json=create_body, headers={
        "Idempotency-Key": "owned_create_00000001"})
    assert replay.json() == created.json()
    linked = await perform(adopted.json()["operation_id"])
    assert linked["state"] == "succeeded"
    unknown = await perform(created.json()["operation_id"])
    assert unknown["state"] == "outcome_unknown" and creates == [created.json()["operation_id"]]
    async with sessions() as session:
        work = await session.scalar(select(SetupWork).where(
            SetupWork.operation_id == created.json()["operation_id"]))
        work.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    reconcile = await client.post(path + "/binding-operations/" + created.json()["operation_id"] + "/reconcile",
                                  json={"reason": "Create acknowledgement lost"}, headers={
                                      "Idempotency-Key": "reconcile_owned_00001"})
    assert reconcile.status_code == 202 and reconcile.json()["state"] == "waiting", reconcile.text
    owned_result = await perform(created.json()["operation_id"])
    assert owned_result["state"] == "succeeded" and creates == [created.json()["operation_id"]]
    listing = await client.get(path + "/bindings")
    assert listing.status_code == 200
    assert {item["ownership"] for item in listing.json()["items"]} == {"customer_linked", "adp_created"}
    adopted_id, owned_id = linked["binding_id"], owned_result["binding_id"]
    adopted_read = await client.get(path + "/bindings/" + adopted_id)
    owned_read = await client.get(path + "/bindings/" + owned_id)
    assert adopted_read.json()["native_resource_ref"] == adopted_ref
    cleanup_body = {"action": "cleanup", "configuration_revision": 1,
                    "binding_id": adopted_id, "owned_cleanup_confirmed": True}
    denied = await client.post(path + "/binding-operations", json=cleanup_body, headers={
        "Idempotency-Key": "adopted_cleanup_00001", "If-Match": adopted_read.headers["etag"]})
    assert denied.status_code == 403 and inventory[adopted_ref["record_id"]]
    cleanup_body["binding_id"] = owned_id
    cleanup = await client.post(path + "/binding-operations", json=cleanup_body, headers={
        "Idempotency-Key": "owned_cleanup_0000001", "If-Match": owned_read.headers["etag"]})
    assert cleanup.status_code == 202, cleanup.text
    first = await perform(cleanup.json()["operation_id"])
    assert first["state"] == "failed" and "cleanup_receipt" not in first
    assert deletes == [created.json()["operation_id"]]
    assert set(inventory) == {"unrelated-definition", adopted_ref["record_id"]}
    absence_available = True
    failed_binding = await client.get(path + "/bindings/" + owned_id)
    assert failed_binding.json()["state"] == "cleanup_failed"
    retry = await client.post(path + "/binding-operations", json=cleanup_body, headers={
        "Idempotency-Key": "owned_cleanup_retry_01", "If-Match": failed_binding.headers["etag"]})
    assert retry.status_code == 202, retry.text
    final = await perform(retry.json()["operation_id"])
    assert final["state"] == "succeeded" and final["cleanup_receipt"]["absent"] is True
    assert deletes == [created.json()["operation_id"]]
    assert set(inventory) == {"unrelated-definition", adopted_ref["record_id"]}
    async with sessions() as reader:
        owned = await reader.get(Binding, (actor.org_id, owned_id))
        adopted_binding = await reader.get(Binding, (actor.org_id, adopted_id))
        intent = await reader.get(SetupIntent, (actor.org_id, retry.json()["operation_id"]))
        receipt = await reader.get(BindingOperation, (actor.org_id, retry.json()["operation_id"]))
        assert owned.state == "cleaned" and adopted_binding.ownership == "customer_linked"
        assert owned.creation_receipt == intent.owned_resource_receipt == receipt.cleanup_receipt["creation_receipt"]
        assert receipt.cleanup_receipt["absence_evidence_ref"]["record_id"] == created.json()["operation_id"]
    assert scenario["provider_attempts"] == []


@pytest.mark.asyncio
async def test_transform_gateway_binding_scope_denial(transform_binding_scenario):
    from src.auth.aws_connection_authority import connection_binding
    from src.shared.models.vault import CredentialServiceDelegation, UserCredential
    from l5_services.models.setup import BindingOperation, SetupIntent, SetupWork
    from l5_services.models.binding import Binding

    scenario, product, selected, saved = transform_binding_scenario
    client, path = scenario["client"], product["path"].removesuffix("/configuration")
    body = {"action": "link", "configuration_revision": saved.json()["revision"],
            "native_resource_ref": selected["transform"]["executor_binding_ref"]}
    headers = {"Idempotency-Key": "transform_binding_link_0001"}
    started = await client.post(path + "/binding-operations", json=body, headers=headers)
    assert started.status_code == 202, started.text
    operation_id = started.json()["operation_id"]
    assert (await client.get(path + "/binding-operations/" + operation_id)).status_code == 200
    assert (await client.get(path + "/bindings/fictional-binding")).status_code == 404

    scenario["switch"](scenario["actors"][1])
    for response in (await client.get(path + "/binding-operations/" + operation_id),
                     await client.get(path + "/bindings"),
                     await client.get(path + "/bindings/fictional-binding"),
                     await client.post(path + "/binding-operations", json=body, headers=headers)):
        assert response.status_code == 404, response.text
        assert operation_id not in response.text
    scenario["switch"](product["actor"])
    foreign_project = path.replace("/projects/" + product["project_id"] + "/", "/projects/foreign-project/")
    assert (await client.get(foreign_project + "/binding-operations/" + operation_id)).status_code == 404
    assert (await client.post(foreign_project + "/binding-operations", json=body, headers=headers)).status_code == 404
    for field, value in (("tenant_id", "foreign-tenant"), ("project_id", "foreign-project"),
                         ("record_type", "repository_commit"), ("record_id", "foreign-executor")):
        forged = await client.post(path + "/binding-operations", json={**body, "native_resource_ref": {
            **body["native_resource_ref"], field: value}}, headers={
                "Idempotency-Key": "transform_binding_forged_01"})
        assert forged.status_code == 404, forged.text

    async with scenario["sessions"]() as session:
        credential = await session.get(UserCredential, selected["transform"]["connection_ref"]["record_id"])
        credential.scopes = {**credential.scopes, "role_arn": "arn:aws:iam::210987654321:role/Wrong"}
        credential.aws_verified_binding = connection_binding(credential)
        await session.commit()
    for response in (await client.get(path + "/binding-operations/" + operation_id),
                     await client.post(path + "/binding-operations", json=body, headers=headers)):
        assert response.status_code == 503, response.text
        assert operation_id not in response.text

    async with scenario["sessions"]() as session:
        credential = await session.get(UserCredential, selected["transform"]["connection_ref"]["record_id"])
        credential.scopes = {**credential.scopes, "role_arn": "arn:aws:iam::123456789012:role/Example"}
        credential.aws_verified_binding = connection_binding(credential)
        await session.commit()
    repository_key = (selected["source_refs"][0]["source_connection_id"], selected["source_refs"][0]["record_id"])
    rights = scenario["permissions"][(product["actor"].org_id, product["project_id"])]
    rights.remove(repository_key)
    for response in (await client.get(path + "/binding-operations/" + operation_id),
                     await client.get(path + "/bindings"),
                     await client.get(path + "/bindings/fictional-binding"),
                     await client.post(path + "/binding-operations", json=body, headers=headers)):
        assert response.status_code == 404, response.text
        assert operation_id not in response.text
    rights.add(repository_key)
    async with scenario["sessions"]() as session:
        grant = await session.scalar(select(CredentialServiceDelegation).where(
            CredentialServiceDelegation.product_id == product["product_id"]))
        grant.revoked_at = datetime.now(UTC)
        grant.revision += 1
        await session.commit()
    assert (await client.get(product["path"])).status_code == 200
    # Revoking execution consent leaves authorized setup metadata visible.
    operation = await client.get(path + "/binding-operations/" + operation_id)
    assert operation.status_code == 200 and operation.json()["operation_id"] == operation_id
    bindings = await client.get(path + "/bindings")
    assert bindings.status_code == 200 and bindings.json() == {"items": []}
    assert (await client.get(path + "/bindings/fictional-binding")).status_code == 404
    for response in (await client.post(path + "/binding-operations", json=body, headers=headers),
                     await client.post(path + "/binding-operations/" + operation_id + "/reconcile",
                                       json={"reason": "Replay"}, headers={"Idempotency-Key": "transform_reconcile_replay_01"})):
        assert response.status_code == 403, response.text
        assert operation_id not in response.text
    async with scenario["sessions"]() as reader:
        assert await reader.scalar(select(func.count()).select_from(SetupIntent)) == 1
        assert await reader.scalar(select(func.count()).select_from(BindingOperation)) == 1
        assert await reader.scalar(select(func.count()).select_from(SetupWork)) == 1
        assert await reader.scalar(select(func.count()).select_from(Binding)) == 0
    assert scenario["provider_attempts"] == []


@pytest.mark.asyncio
async def test_transform_gateway_configuration_roundtrip(configuration_scenario):
    from datetime import timedelta

    from src.auth.aws_connection_authority import connection_binding
    from src.shared.models.vault import UserCredential

    scenario = configuration_scenario
    client = scenario["client"]
    first_actor, second_actor = scenario["actors"]
    products = [
        await scenario["prepare"](first_actor, "transform-first", "internal", ["refactor"]),
        await scenario["prepare"](second_actor, "transform-second", "confidential", ["dependency_upgrade"]),
    ]
    scenario["typed_connections"].update({"enabled": True, "provider": "aws", "project_ids": {
        product["actor"].org_id: product["project_id"] for product in products}})
    fixed_time = scenario["fixed_time"]
    async with scenario["sessions"]() as session:
        for product in products:
            actor = product["actor"]
            connection_id = f"{actor.org_id}-connection-one"
            scenario["permissions"][(actor.org_id, product["project_id"])].add((connection_id, connection_id))
            credential = UserCredential(
                id=connection_id, org_id=actor.org_id, user_id=actor.user_id,
                service="aws", credential_type="aws_role", label="Fixture AWS role",
                secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:fixture",
                strict=False, aws_external_id="example-external-id",
                aws_verified_at=fixed_time, aws_verification_attempt="example-attempt",
                aws_verified_version_id="revision-1", expires_at=fixed_time + timedelta(days=1),
                scopes={"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Example",
                        "status": "verified"},
            )
            credential.aws_verified_binding = connection_binding(credential)
            session.add(credential)
        await session.commit()

    for product in products:
        scenario["switch"](product["actor"])
        product["path"] = product["path"].replace("/services/security/", "/services/transform/")
        options = await client.get(product["path"] + "/options")
        assert options.status_code == 200, options.text
        assert options.json()["sources"] == [product["references"][0]]
        assert len(options.json()["connections"]) == 2
        repository = options.json()["sources"][0]
        connection = next(ref for ref in options.json()["connections"] if ref["source_connection_id"] == repository["source_connection_id"])
        choice = {"service_id": "transform", "capabilities": product["capabilities"], "enabled": False,
                  "source_refs": [repository], "overrides": [
                      {"field": "source_refs", "value": [repository], "reason": "Pin selected repository"}],
                  "transform": {
                      "connection_ref": connection,
                      "executor_binding_ref": {**connection, "record_type": "executor_binding", "record_id": "example-executor"},
                      "test_policy_ref": {**connection, "record_type": "test_policy", "record_id": "example-test-policy"},
                      "region": "example-region", "read_paths": ["src"], "write_paths": ["src/module.py"],
                      "excluded_paths": [".git"],
                      "goal_defaults": {"kind": product["capabilities"][0], "summary": "Improve source"}},
                  }
        if product["capabilities"] == ["dependency_upgrade"]:
            choice["transform"]["goal_defaults"]["target"] = {
                "kind": "dependency", "name": "sample-lib", "version": "2.0.0"}
        product["choice"] = choice
        created = await client.put(product["path"], json=choice, headers={
            "Idempotency-Key": "transform_roundtrip_create_0001", "If-None-Match": "*"})
        assert created.status_code == 201, created.text
        assert created.json()["handoff_state"] == "linked"
        assert created.json()["settings"]["source_refs"] == [repository]
        assert created.json()["settings"]["transform"]["test_policy_ref"] == choice["transform"]["test_policy_ref"]
        assert (await client.get(product["path"])).json() == created.json()
        changed = await client.put(product["path"], json={"transform": {
            **choice["transform"], "write_paths": ["src/updated.py"]}}, headers={
            "Idempotency-Key": "transform_roundtrip_edit_00001", "If-Match": created.headers["etag"]})
        assert changed.status_code == 200, changed.text
        assert changed.headers["etag"] != created.headers["etag"]
        revised = await scenario["revise_profile"](product, "restricted")
        new_ref = {**created.json()["settings"]["profile_ref"], "record_revision": str(revised["revision"])}
        refreshed = await client.post(product["path"] + "/refresh", json={"profile_ref": new_ref},
                                      headers={"Idempotency-Key": "transform_roundtrip_refresh_01",
                                               "If-Match": changed.headers["etag"]})
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["revision"] == 3
        assert refreshed.headers["etag"] != changed.headers["etag"]
        assert refreshed.json()["settings"]["classification"] == "restricted"
        assert refreshed.json()["settings"]["transform"]["write_paths"] == ["src/updated.py"]
        assert refreshed.json()["settings"]["overrides"] == choice["overrides"]
        assert refreshed.json()["settings"]["source_refs"] == [repository]
        current_product = await client.get(product["profile_path"])
        assert current_product.status_code == 200, current_product.text
        selection = next(entry for entry in current_product.json()["configuration"]["selected_capabilities"]
                         if entry["capability"] == product["capabilities"][0])
        assert selection["configuration_ref"]["record_id"] == refreshed.json()["configuration_id"]
        assert selection["configuration_ref"]["record_revision"] == "3"
        assert (await client.get(product["path"] + "/versions/1")).json()["settings"] == created.json()["settings"]
        assert (await client.get(product["path"] + "/versions/2")).json()["settings"] == changed.json()["settings"]
        product["saved"] = refreshed.json()

    scenario["switch"](first_actor)
    assert (await client.get(products[1]["path"])).status_code == 404
    async with scenario["sessions"]() as reader:
        assert await reader.scalar(select(func.count()).select_from(Configuration)) == 2
        assert await reader.scalar(select(func.count()).select_from(ConfigurationVersion)) == 6
        assert await reader.scalar(select(func.count()).select_from(ConfigurationReceipt)) == 2
        for product in products:
            configuration = await reader.get(Configuration, (product["actor"].org_id, product["saved"]["configuration_id"]))
            assert configuration.current_revision == 3
            for revision in (1, 2, 3):
                version = await reader.get(ConfigurationVersion, (product["actor"].org_id, configuration.configuration_id, revision))
                assert version is not None and version.settings["source_refs"] == product["choice"]["source_refs"]
                assert version.hash
                if revision == 3:
                    assert version.settings == product["saved"]["settings"]
                    assert version.hash == product["saved"]["hash"]
    assert scenario["provider_attempts"] == []


@pytest.mark.asyncio
async def test_profile_population(configuration_scenario):
    scenario = configuration_scenario
    client = scenario["client"]
    first_actor, second_actor = scenario["actors"]
    first_product = await scenario["prepare"](first_actor, "first", "confidential", ["threat_model", "code_review"])
    second_product = await scenario["prepare"](second_actor, "second", "internal", ["threat_model"])
    responses = []
    for product in (first_product, second_product):
        scenario["switch"](product["actor"])
        result = await client.put(product["path"], json={"capabilities": product["capabilities"]},
                                  headers={"Idempotency-Key": "scenario_config_create_00001", "If-None-Match": "*"})
        assert result.status_code == 201, result.text
        saved = result.json()
        assert saved["revision"] == 1 and saved["scope"]["tenant_id"] == product["actor"].org_id
        assert saved["state"] == "disabled" and saved["handoff_state"] == "linked"
        assert saved["settings"]["purpose"] == REQUEST["purpose"]
        assert saved["settings"]["classification"] == product["classification"]
        assert saved["settings"]["source_refs"] == product["references"]
        assert saved["inherited_fields"] == ["purpose", "classification", "source_refs"]
        assert saved["settings"]["profile_ref"] == {
            "tenant_id": product["actor"].org_id, "project_id": product["project_id"],
            "source_connection_id": "adp.profile", "record_type": "product",
            "record_id": product["product_id"], "record_revision": str(product["profile_revision"]),
        }
        assert "connection_ref" not in saved["settings"]
        assert {"connection_ref", "region", "limits", "execution_delegation"} <= set(saved["gaps"])
        assert result.headers["etag"] == f'"{saved["hash"]}"'
        assert (await client.get(product["path"])).json() == saved
        check = await client.post(f"{product['path']}/check")
        assert check.status_code == 200
        projected = check.json()
        assert projected["ready"] is False and projected["provider_contacted"] is False
        assert {"configuration_disabled", "binding_or_delegation_unavailable", "execution_delegation"} <= set(projected["gaps"])
        profile = await client.get(product["profile_path"])
        assert profile.status_code == 200 and profile.json()["revision"] == product["profile_revision"] + 1
        selections = profile.json()["configuration"]["selected_capabilities"]
        for selection in selections[:-1]:
            assert selection["configuration_ref"]["record_id"] == saved["configuration_id"]
            assert selection["configuration_ref"]["record_revision"] == "1"
        assert selections[-1] == {"capability": "Delivery", "ownership_mode": "integrated"}
        responses.append(saved)

    assert responses[0]["configuration_id"] != responses[1]["configuration_id"]
    scenario["switch"](first_actor)
    assert (await client.get(second_product["path"])).status_code == 404
    assert len(scenario["connection_reads"]) >= 2 and len(scenario["source_reads"]) >= 2
    assert scenario["provider_attempts"] == []

    async with scenario["sessions"]() as reader:
        assert await reader.scalar(select(func.count()).select_from(Configuration)) == 2
        assert await reader.scalar(select(func.count()).select_from(ConfigurationVersion)) == 2
        assert await reader.scalar(select(func.count()).select_from(ConfigurationReceipt)) == 2
        for product, saved in zip((first_product, second_product), responses, strict=True):
            configuration = await reader.get(Configuration, (product["actor"].org_id, saved["configuration_id"]))
            version = await reader.get(ConfigurationVersion, (product["actor"].org_id, saved["configuration_id"], 1))
            project = await reader.get(Project, (product["actor"].org_id, product["project_id"]))
            changes = (await reader.scalars(select(ProductChange).where(
                ProductChange.tenant_id == product["actor"].org_id,
                ProductChange.product_id == product["product_id"],
            ))).all()
            assert configuration.current_revision == version.revision == 1
            assert configuration.handoff_state == "linked"
            assert version.settings == saved["settings"] and version.hash == saved["hash"]
            assert project.configuration["selected_capabilities"][-1] == {
                "capability": "Delivery", "ownership_mode": "integrated",
            }
            assert len(changes) == 3
            assert all(change.recorded_at.replace(tzinfo=UTC) == scenario["fixed_time"] for change in changes)

@pytest.mark.asyncio
async def test_refresh_and_revision_race(configuration_scenario):
    import asyncio

    scenario = configuration_scenario
    client = scenario["client"]
    first_actor, second_actor = scenario["actors"]
    first_product = await scenario["prepare"](first_actor, "refresh-first", "confidential", ["threat_model"])
    second_product = await scenario["prepare"](second_actor, "refresh-second", "internal", ["threat_model"])
    created = {}
    for product in (first_product, second_product):
        scenario["switch"](product["actor"])
        result = await client.put(product["path"], json={"capabilities": product["capabilities"],
                                                        "exclusions": ["generated/"]},
                                  headers={"Idempotency-Key": "scenario_refresh_create_0001", "If-None-Match": "*"})
        assert result.status_code == 201, result.text
        created[product["product_id"]] = result

    first = created[first_product["product_id"]]
    other = created[second_product["product_id"]]
    updated_profile = await scenario["revise_profile"](first_product, "restricted")
    scenario["switch"](first_actor)
    unchanged = await client.get(first_product["path"])
    assert unchanged.status_code == 200
    assert unchanged.json()["settings"]["classification"] == "confidential"
    assert unchanged.json()["settings"]["exclusions"] == ["generated/"]
    next_ref = {**first.json()["settings"]["profile_ref"], "record_revision": str(updated_profile["revision"])}
    refreshed = await client.post(f'{first_product["path"]}/refresh', json={"profile_ref": next_ref},
                                  headers={"Idempotency-Key": "scenario_refresh_profile_0001",
                                           "If-Match": first.headers["etag"]})
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["revision"] == 2 and refreshed.json()["handoff_state"] == "linked"
    assert refreshed.json()["settings"]["classification"] == "restricted"
    assert refreshed.json()["settings"]["source_refs"] == first.json()["settings"]["source_refs"]
    assert refreshed.json()["settings"]["exclusions"] == ["generated/"]
    assert refreshed.json()["settings"]["profile_ref"] == next_ref
    assert refreshed.headers["etag"] != first.headers["etag"]
    assert (await client.get(f'{first_product["path"]}/versions/1')).json()["settings"] == first.json()["settings"]

    current_read = await client.get(first_product["path"])
    assert current_read.status_code == 200, current_read.text
    pinned_read = await client.get(f'{first_product["profile_path"]}/revisions/{next_ref["record_revision"]}')
    assert pinned_read.status_code == 200, pinned_read.text
    assert pinned_read.json()["configuration"]["governance"]["data_classification"] == "restricted"

    both_read = asyncio.Event()
    start_edits = asyncio.Event()
    first_done = asyncio.Event()
    observed_etags = []

    async def edit(exclusions, key, wait_for_first):
        observed = await client.get(first_product["path"])
        observed_etags.append(observed.headers.get("etag"))
        if len(observed_etags) == 2:
            both_read.set()
        await start_edits.wait()
        if wait_for_first:
            await first_done.wait()
        try:
            return await client.put(first_product["path"], json={"exclusions": exclusions}, headers={
                "Idempotency-Key": key, "If-Match": refreshed.headers["etag"],
            })
        finally:
            if not wait_for_first:
                first_done.set()

    writers = (
        asyncio.create_task(edit(["winner/"], "scenario_racing_writer_one_0001", False)),
        asyncio.create_task(edit(["loser/"], "scenario_racing_writer_two_0001", True)),
    )
    await asyncio.wait_for(both_read.wait(), timeout=30)
    assert observed_etags == [refreshed.headers["etag"]] * 2
    start_edits.set()
    results = await asyncio.gather(*writers)
    assert sorted(result.status_code for result in results) == [200, 412], [result.text for result in results]
    accepted = next(result for result in results if result.status_code == 200)
    rejected = next(result for result in results if result.status_code == 412)
    assert rejected.json()["detail"]["code"] == "stale_revision"
    assert accepted.json()["revision"] == 3
    assert accepted.json()["settings"]["exclusions"] in (["winner/"], ["loser/"])
    assert accepted.json()["settings"]["classification"] == "restricted"
    assert accepted.json()["settings"]["profile_ref"] == next_ref
    assert (await client.get(first_product["path"])).json() == accepted.json()
    assert (await client.get(f'{first_product["path"]}/versions/2')).json()["settings"] == refreshed.json()["settings"]

    scenario["switch"](second_actor)
    assert (await client.get(second_product["path"])).json() == other.json()
    assert scenario["provider_attempts"] == []
    async with scenario["sessions"]() as reader:
        identifier = first.json()["configuration_id"]
        config = await reader.get(Configuration, (first_actor.org_id, identifier))
        other_config = await reader.get(Configuration, (second_actor.org_id, other.json()["configuration_id"]))
        assert config.current_revision == 3 and other_config.current_revision == 1
        old, proposed, current = [await reader.get(ConfigurationVersion, (first_actor.org_id, identifier, revision))
                                  for revision in (1, 2, 3)]
        assert old.settings == first.json()["settings"] and old.hash == first.json()["hash"]
        assert proposed.settings == refreshed.json()["settings"] and proposed.hash == refreshed.json()["hash"]
        assert current.settings == accepted.json()["settings"] and current.hash == accepted.json()["hash"]
        assert old.settings["exclusions"] == proposed.settings["exclusions"] == ["generated/"]
        assert old.settings["classification"] == "confidential"
        assert proposed.settings["classification"] == current.settings["classification"] == "restricted"
        assert await reader.scalar(select(func.count()).select_from(ConfigurationVersion)) == 4
        receipts = list(await reader.scalars(select(ConfigurationReceipt)))
        assert len(receipts) == 3
        edits = [receipt for receipt in receipts if receipt.operation == "edit"]
        assert len(edits) == 1
        assert edits[0].idempotency_key == "scenario_racing_writer_one_0001"
        assert edits[0].configuration_id == identifier and edits[0].revision == 3
        changes = (await reader.scalars(select(ProductChange).where(
            ProductChange.tenant_id == first_actor.org_id, ProductChange.product_id == first_product["product_id"],
        ).order_by(ProductChange.revision))).all()
        assert len(changes) == 6
        assert all(change.recorded_at.replace(tzinfo=UTC) == scenario["fixed_time"] for change in changes)

@pytest.mark.asyncio
async def test_scope_replay_outage(configuration_scenario):
    import httpx

    scenario = configuration_scenario
    client = scenario["client"]
    first_actor, second_actor = scenario["actors"]
    first_product = await scenario["prepare"](first_actor, "recovery-first", "confidential", ["threat_model"])
    second_product = await scenario["prepare"](second_actor, "recovery-second", "internal", ["threat_model"])
    create_key = "scenario_recovery_create_0001"
    create_headers = {"Idempotency-Key": create_key, "If-None-Match": "*"}
    body = {"capabilities": ["threat_model"]}

    scenario["switch"](first_actor)
    scenario["faults"]["handoff"] = httpx.ConnectError("product-transport-down")
    saved = await client.put(first_product["path"], json=body, headers=create_headers)
    scenario["faults"]["handoff"] = None
    assert saved.status_code == 201, saved.text
    assert saved.json()["handoff_state"] == "unavailable"
    assert saved.json()["revision"] == 1
    async with scenario["sessions"]() as reader:
        original = await reader.get(ConfigurationVersion, (first_actor.org_id, saved.json()["configuration_id"], 1))
        assert original.settings == saved.json()["settings"]
        assert (await reader.get(Configuration, (first_actor.org_id, saved.json()["configuration_id"]))).handoff_state == "unavailable"
        assert await reader.scalar(select(func.count()).select_from(ConfigurationVersion)) == 1
        assert await reader.scalar(select(func.count()).select_from(ConfigurationReceipt)) == 1
        first_changes = (await reader.scalars(select(ProductChange).where(
            ProductChange.tenant_id == first_actor.org_id, ProductChange.product_id == first_product["product_id"],
        ))).all()
        assert len(first_changes) == 2

    scenario["switch"](second_actor)
    other = await client.put(second_product["path"], json=body, headers=create_headers)
    assert other.status_code == 201, other.text
    assert other.json()["configuration_id"] != saved.json()["configuration_id"]
    assert other.json()["scope"]["tenant_id"] == second_actor.org_id
    assert other.json()["handoff_state"] == "linked"
    assert (await client.get(second_product["path"])).json() == other.json()
    attempts_before_replay = len(scenario["product_attempts"])
    scenario["switch"](first_actor)
    replay = await client.put(first_product["path"], json=body, headers=create_headers)
    assert replay.status_code == 201 and replay.json() == saved.json()
    assert len(scenario["product_attempts"]) == attempts_before_replay
    changed = await client.put(first_product["path"], json={**body, "exclusions": ["not-saved/"]},
                               headers=create_headers)
    assert changed.status_code == 409 and changed.json()["detail"]["code"] == "idempotency_conflict"
    new_key = await client.put(first_product["path"], json=body, headers={
        **create_headers, "Idempotency-Key": "scenario_recovery_other_0001",
    })
    assert new_key.status_code == 412

    scenario["switch"](second_actor)
    for method, path, kwargs in (
        (client.get, first_product["path"], {}),
        (client.get, f'{first_product["path"]}/versions/1', {}),
        (client.post, f'{first_product["path"]}/check', {}),
        (client.put, first_product["path"], {"json": body, "headers": create_headers}),
        (client.put, first_product["path"], {"json": {"exclusions": ["foreign/"]}, "headers": {
            "Idempotency-Key": "scenario_foreign_edit_00001", "If-Match": saved.headers["etag"],
        }}),
        (client.post, f'{first_product["path"]}/refresh', {
            "json": {"profile_ref": saved.json()["settings"]["profile_ref"]},
            "headers": {"Idempotency-Key": "scenario_foreign_refresh_0001", "If-Match": saved.headers["etag"]},
        }),
    ):
        denied = await method(path, **kwargs)
        assert denied.status_code == 404, denied.text
        assert saved.json()["configuration_id"] not in denied.text
    assert (await client.get(second_product["path"])).json() == other.json()

    scenario["switch"](first_actor)
    scenario["faults"]["profile"] = httpx.ConnectError("product-transport-down")
    for method, path, kwargs in (
        (client.get, first_product["path"], {}),
        (client.post, f'{first_product["path"]}/check', {}),
        (client.put, first_product["path"], {"json": body, "headers": create_headers}),
        (client.put, first_product["path"], {"json": {"exclusions": ["outage/"]}, "headers": {
            "Idempotency-Key": "scenario_outage_edit_00001", "If-Match": saved.headers["etag"],
        }}),
        (client.post, f'{first_product["path"]}/refresh', {
            "json": {"profile_ref": saved.json()["settings"]["profile_ref"]},
            "headers": {"Idempotency-Key": "scenario_outage_refresh_0001", "If-Match": saved.headers["etag"]},
        }),
    ):
        unavailable = await method(path, **kwargs)
        assert unavailable.status_code == 503, unavailable.text
        assert unavailable.json()["detail"] == {"code": "dependency_unavailable", "retryable": True}
        assert "product-transport-down" not in unavailable.text
    scenario["faults"]["profile"] = None
    scenario["faults"]["sources"] = httpx.ConnectError("product-sources-down")
    unavailable = await client.get(first_product["path"])
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "dependency_unavailable"
    scenario["faults"]["sources"] = None

    assert len(scenario["product_attempts"]) == attempts_before_replay
    assert scenario["provider_attempts"] == []
    async with scenario["sessions"]() as reader:
        original = await reader.get(ConfigurationVersion, (first_actor.org_id, saved.json()["configuration_id"], 1))
        assert original.settings == saved.json()["settings"] and original.hash == saved.json()["hash"]
        assert await reader.scalar(select(func.count()).select_from(ConfigurationVersion)) == 2
        assert await reader.scalar(select(func.count()).select_from(ConfigurationReceipt)) == 2

    recovered = await client.post(f'{first_product["path"]}/handoff', headers={
        "Idempotency-Key": "scenario_recovery_handoff_0001", "If-Match": saved.headers["etag"],
    })
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["handoff_state"] == "linked"
    assert recovered.json()["settings"] == saved.json()["settings"]
    assert recovered.json()["revision"] == 1
    assert (await client.get(first_product["path"])).json() == recovered.json()
    check = await client.post(f'{first_product["path"]}/check')
    assert check.status_code == 200 and check.json()["ready"] is False
    assert check.json()["provider_contacted"] is False
    assert len(scenario["product_attempts"]) == attempts_before_replay + 1
    assert scenario["provider_attempts"] == []
    async with scenario["sessions"]() as reader:
        configuration = await reader.get(Configuration, (first_actor.org_id, saved.json()["configuration_id"]))
        version = await reader.get(ConfigurationVersion, (first_actor.org_id, saved.json()["configuration_id"], 1))
        assert configuration.current_revision == 1 and configuration.handoff_state == "linked"
        assert version.settings == saved.json()["settings"]
        assert await reader.scalar(select(func.count()).select_from(ConfigurationVersion)) == 2
        assert await reader.scalar(select(func.count()).select_from(ConfigurationReceipt)) == 2
        changes = (await reader.scalars(select(ProductChange).where(
            ProductChange.tenant_id == first_actor.org_id, ProductChange.product_id == first_product["product_id"],
        ))).all()
        assert len(changes) == 3
        assert all(change.recorded_at.replace(tzinfo=UTC) == scenario["fixed_time"] for change in changes)


@pytest.mark.asyncio
async def test_configuration_options(configuration_scenario, monkeypatch):
    from src import l5_integration

    scenario = configuration_scenario
    client = scenario["client"]
    first_actor, second_actor = scenario["actors"]
    first = await scenario["prepare"](first_actor, "options-first", "internal", ["threat_model", "code_review"])
    second = await scenario["prepare"](second_actor, "options-second", "confidential", ["threat_model"])
    scenario["switch"](first_actor)
    path = f'{first["path"]}/options'
    missing = await client.get(path)
    assert missing.status_code == 200, missing.text
    assert missing.headers["cache-control"] == "no-store"
    assert missing.json()["connections"] == []
    assert missing.json()["sources"] == first["references"]
    assert "connection_revision_unavailable" in missing.json()["unavailable_reasons"]
    assert missing.json()["regions"] == []
    assert missing.json()["capabilities"] == first["capabilities"]
    original_inputs = l5_integration.GatewayConfigurationBackend.profile_inputs

    async def incomplete_inputs(backend, profile, product_id, project_id, service_id):
        relationship, profile_ref, available = await original_inputs(backend, profile, product_id, project_id, service_id)
        return relationship, profile_ref, {**available, "next_cursor": "more"}

    with monkeypatch.context() as partial:
        partial.setattr(l5_integration.GatewayConfigurationBackend, "profile_inputs", incomplete_inputs)
        incomplete = await client.get(path)
    assert incomplete.status_code == 200
    assert incomplete.json()["sources"] == incomplete.json()["connections"] == []
    assert "source_coverage_incomplete" in incomplete.json()["unavailable_reasons"]
    invented = {"tenant_id": first_actor.org_id, "project_id": first["project_id"],
                "source_connection_id": f"{first_actor.org_id}-connection-one", "record_type": "connection",
                "record_id": f"{first_actor.org_id}-connection-one", "record_revision": "invented"}
    body = {"capabilities": first["capabilities"], "connection_ref": invented,
            "overrides": [{"field": "connection_ref", "value": invented, "reason": "Choose provider"}]}
    headers = {"Idempotency-Key": "options_create_configuration_001", "If-None-Match": "*"}
    rejected = await client.put(first["path"], json=body, headers=headers)
    assert rejected.status_code == 422, rejected.text
    scenario["typed_connections"].update(enabled=True, project_id=first["project_id"])
    options = await client.get(path)
    assert options.status_code == 200, options.text
    refs = options.json()["connections"]
    assert len(refs) == 2 and refs[0] != refs[1]
    assert {ref["record_id"] for ref in refs} == {
        f"{first_actor.org_id}-connection-one", f"{first_actor.org_id}-connection-two"}
    assert "connection_revision_unavailable" not in options.json()["unavailable_reasons"]
    assert options.json()["sources"] == first["references"]
    body["connection_ref"] = refs[0]
    body["overrides"][0]["value"] = refs[0]
    created = await client.put(first["path"], json=body, headers=headers)
    assert created.status_code == 201, created.text
    assert created.json()["settings"]["connection_ref"] == refs[0]
    assert (await client.get(first["path"])).json()["settings"]["connection_ref"] == refs[0]
    scenario["typed_connections"].update(enabled=False)
    assert (await client.get(first["path"])).status_code == 404
    scenario["switch"](second_actor)
    assert (await client.get(path)).status_code == 404
    second_options = await client.get(f'{second["path"]}/options')
    assert second_options.status_code == 200
    assert second_options.json()["connections"] == []
    scenario["faults"]["sources"] = httpx.ConnectError("product-options-down")
    outage = await client.get(f'{second["path"]}/options')
    assert outage.status_code == 503 and outage.json()["detail"]["retryable"] is True
    scenario["faults"]["sources"] = None
    assert scenario["provider_attempts"] == []
    async with scenario["sessions"]() as reader:
        assert await reader.scalar(select(func.count()).select_from(ConfigurationVersion)) == 1


@pytest.mark.asyncio
async def test_finops_configuration_uses_authorized_connection_without_repository(configuration_scenario):
    scenario = configuration_scenario
    client = scenario["client"]
    actor = scenario["actors"][0]
    product = await scenario["prepare"](actor, "finops", "internal", ["finops"])
    path = product["path"].replace("/security/", "/finops/")
    scenario["typed_connections"].update(enabled=True, provider="aws", project_id=product["project_id"])
    scenario["permissions"][(actor.org_id, product["project_id"])].clear()
    original_profile = (await client.get(product["profile_path"])).json()
    updated = await client.patch(product["profile_path"] + "/configuration", json={"configuration": {
        **original_profile["configuration"], "basic": {},
    }}, headers={"Idempotency-Key": "finops_profile_remove_sources_0001",
                "If-Profile-Revision": str(original_profile["revision"])})
    assert updated.status_code == 200, updated.text
    options = await client.get(path + "/options")
    assert options.status_code == 200, options.text
    assert options.json()["sources"] == []
    connection = options.json()["connections"][0]
    selected = {"service_id": "finops", "capabilities": ["cost_inquiry"], "enabled": False,
                "connection_ref": connection, "region": "us-east-1",
                "limits": {"deadline_seconds": 3600},
                "overrides": [{"field": "connection_ref", "value": connection, "reason": "Selected"},
                              {"field": "region", "value": "us-east-1", "reason": "Selected"}],
                "finops": {"billing_scope": {"account_ids": ["123456789012"], "view_ref": "view-example"},
                           "agent_mode": "adopt", "native_agent_ref": "agent-example", "data_role_ref": "role-example"}}
    for headers, status, code in (
        ({}, 428, "precondition_required"),
        ({"If-None-Match": "*"}, 428, "precondition_required"),
        ({"Idempotency-Key": "finops_configuration_missing_etag_0001"}, 428, "precondition_required"),
        ({"Idempotency-Key": "finops_configuration_conflicting_0001", "If-None-Match": "*",
          "If-Match": '"stale"'}, 422, "conflicting_preconditions"),
        ({"Idempotency-Key": "short", "If-None-Match": "*"}, 422, "validation_error"),
    ):
        rejected = await client.put(path, json=selected, headers=headers)
        assert rejected.status_code == status and rejected.json() == {"detail": {"code": code}}
    assert (await client.get(path)).status_code == 404
    created = await client.put(path, json=selected, headers={
        "Idempotency-Key": "finops_configuration_create_0001", "If-None-Match": "*"})
    assert created.status_code == 201, created.text
    config = created.json()
    assert config["settings"]["source_refs"] == []
    assert config["settings"]["profile_ref"]["record_revision"] == str(updated.json()["revision"])
    assert config["settings"]["finops"] == selected["finops"]
    assert "source_refs" not in config["gaps"]
    assert "billing_scope_unverified" in config["gaps"]
    assert "native_unqualified" in config["gaps"]
    assert (await client.get(path)).json() == config
    replay_headers = {"Idempotency-Key": "finops_configuration_create_0001", "If-None-Match": "*"}
    replay = await client.put(path, json=selected, headers=replay_headers)
    assert replay.status_code == 201 and replay.json() == config
    assert replay.headers["ETag"] == created.headers["ETag"]
    conflict = await client.put(path, json={**selected, "enabled": True}, headers=replay_headers)
    assert conflict.status_code == 409 and conflict.json() == {"detail": {"code": "idempotency_conflict"}}
    assert (await client.get(path)).json() == config
    async with scenario["sessions"]() as reader:
        assert await reader.scalar(select(func.count()).select_from(ConfigurationVersion).where(
            ConfigurationVersion.tenant_id == actor.org_id,
            ConfigurationVersion.configuration_id == config["configuration_id"])) == 1
        assert await reader.scalar(select(func.count()).select_from(ConfigurationReceipt)) == 1
    check = await client.post(path + "/check")
    assert check.status_code == 200 and check.json()["provider_contacted"] is False
    assert check.json()["ready"] is False
    assert (await client.put(path, json={"region": "us-west-2"}, headers={
        "Idempotency-Key": "finops_configuration_update_0001", "If-Match": '"stale"'})).status_code == 412
    assert (await client.get(path)).json()["settings"]["finops"] == selected["finops"]
    revised = await scenario["revise_profile"](product, "restricted")
    revised_ref = {**config["settings"]["profile_ref"], "record_revision": str(revised["revision"])}
    refreshed = await client.post(path + "/refresh", json={"profile_ref": revised_ref}, headers={
        "Idempotency-Key": "finops_configuration_refresh_0001", "If-Match": created.headers["ETag"]})
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["settings"]["profile_ref"] == revised_ref
    assert refreshed.json()["settings"]["finops"] == selected["finops"]
    assert refreshed.json()["settings"]["connection_ref"] == connection
    assert refreshed.json()["settings"]["source_refs"] == []
    stale = await client.post(path + "/refresh", json={"profile_ref": revised_ref}, headers={
        "Idempotency-Key": "finops_configuration_refresh_0002", "If-Match": created.headers["ETag"]})
    assert stale.status_code == 412
    assert scenario["provider_attempts"] == []
    async with scenario["sessions"]() as reader:
        version = await reader.get(ConfigurationVersion, (actor.org_id, config["configuration_id"], 1))
        assert version.settings["finops"] == selected["finops"]


@pytest.mark.asyncio
async def test_finops_configuration_isolated_by_project_and_tenant(configuration_scenario):
    scenario = configuration_scenario
    client = scenario["client"]
    first_actor, second_actor = scenario["actors"]
    products = [
        await scenario["prepare"](actor, label, "internal", ["finops"])
        for actor, label in ((first_actor, "finops-project-one"),
                             (first_actor, "finops-project-two"),
                             (second_actor, "finops-other-tenant"))
    ]
    stored = []
    for index, product in enumerate(products):
        scenario["switch"](product["actor"])
        scenario["typed_connections"].update(enabled=True, provider="aws", project_id=product["project_id"])
        scenario["permissions"][(product["actor"].org_id, product["project_id"])].clear()
        path = product["path"].replace("/security/", "/finops/")
        options = await client.get(path + "/options")
        assert options.status_code == 200, options.text
        connection = options.json()["connections"][0]
        scope = {"account_ids": [f"{index + 1:012d}"], "view_ref": f"view-{index + 1}"}
        created = await client.put(path, json={
            "service_id": "finops", "capabilities": ["cost_inquiry"], "enabled": False,
            "connection_ref": connection, "region": "us-east-1", "finops": {"billing_scope": scope},
            "overrides": [{"field": "connection_ref", "value": connection, "reason": "Selected"},
                          {"field": "region", "value": "us-east-1", "reason": "Selected"}],
        }, headers={"Idempotency-Key": "finops_isolated_config_0001", "If-None-Match": "*"})
        assert created.status_code == 201, created.text
        assert created.json()["settings"]["finops"]["billing_scope"] == scope
        stored.append((path, scope))
    scenario["switch"](first_actor)
    for product, (path, scope) in zip(products[:2], stored[:2]):
        scenario["typed_connections"]["project_id"] = product["project_id"]
        view = await client.get(path)
        assert view.status_code == 200, view.text
        assert view.json()["settings"]["finops"]["billing_scope"] == scope
        check = await client.post(path + "/check")
        assert check.status_code == 200 and check.json()["ready"] is False
    assert (await client.get(stored[2][0])).status_code == 404
    scenario["switch"](second_actor)
    for path, _ in stored[:2]:
        assert (await client.get(path)).status_code == 404
        assert (await client.post(path + "/check")).status_code == 404
    assert scenario["provider_attempts"] == []


@pytest.mark.asyncio
async def test_mcp_configuration_edit_preserves_http_etags(configuration_scenario, monkeypatch, request):
    from src.internal import tools_l5

    contract = Path(__file__).resolve().parents[4] / "docs/l5-services"
    monkeypatch.setattr(tools_l5, "files", lambda package: contract)
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    request.addfinalizer(tools_l5.l5_tool_catalogue.cache_clear)
    request.addfinalizer(tools_l5.l5_operations.cache_clear)
    scenario = configuration_scenario
    first_actor, second_actor = scenario["actors"]
    products = [await scenario["prepare"](first_actor, "mcp-first", "confidential", ["threat_model"]),
                await scenario["prepare"](second_actor, "mcp-second", "internal", ["threat_model"])]
    client = scenario["client"]
    operations = {item["name"]: item for item in tools_l5.available_l5_catalogue()["tools"]}

    async def invoke(tool, arguments):
        response = await client.post("/v1/l5/mcp/tools/call", json={"tool": tool, "arguments": arguments})
        assert response.status_code == 200, response.text
        result = response.json()
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
        return result

    for product in products:
        scenario["switch"](product["actor"])
        scope = {"product_id": product["product_id"], "project_id": product["project_id"],
                 "service_id": "security"}
        created = await invoke("l5___save_configuration", {
            **scope, "input": {"capabilities": product["capabilities"]},
            "idempotency_key": "mcp-create-configuration-0001", "create_only": True,
        })
        assert created["structuredContent"]["http_status"] == 201
        assert created["structuredContent"]["etag"].startswith('"')
        Draft202012Validator(operations["l5.save_configuration"]["outputSchema"]).validate(created["structuredContent"])
        read = await invoke("l5___get_configuration", scope)
        etag = read["structuredContent"]["etag"]
        direct = await client.get(product["path"])
        assert etag == direct.headers["etag"] and read["structuredContent"]["data"] == direct.json()
        edited = await invoke("l5___save_configuration", {
            **scope, "input": {"exclusions": ["mcp-edited/"]},
            "idempotency_key": "mcp-edit-configuration-00001", "if_match": etag,
        })
        assert edited["structuredContent"]["http_status"] == 200
        assert edited["structuredContent"]["etag"] != etag
        assert edited["structuredContent"]["data"]["revision"] == 2
        Draft202012Validator(operations["l5.save_configuration"]["outputSchema"]).validate(edited["structuredContent"])
        stale = await invoke("l5___save_configuration", {
            **scope, "input": {"exclusions": ["stale/"]},
            "idempotency_key": "mcp-stale-configuration-0001", "if_match": etag,
        })
        assert stale["isError"] is True
        assert stale["structuredContent"]["http_status"] == 412
        assert stale["structuredContent"]["error"]["code"] == "stale_revision"
        async with scenario["sessions"]() as independent:
            configuration = (await independent.scalars(select(Configuration).where(
                Configuration.tenant_id == product["actor"].org_id,
                Configuration.product_id == product["product_id"],
            ))).one()
            versions = (await independent.scalars(select(ConfigurationVersion).where(
                ConfigurationVersion.tenant_id == product["actor"].org_id,
                ConfigurationVersion.configuration_id == configuration.configuration_id,
            ).order_by(ConfigurationVersion.revision))).all()
            assert [version.revision for version in versions] == [1, 2]
            assert edited["structuredContent"]["etag"] == f'"{versions[-1].hash}"'
    assert scenario["provider_attempts"] == []
