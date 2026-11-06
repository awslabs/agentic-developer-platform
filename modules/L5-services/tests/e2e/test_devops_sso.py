"""Offline browser handoff through the L5 contract, guard and federation adapter."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from l5_services.adapters.devops.federation import mint_browser_launch
from l5_services.adapters.devops.spaces import SpaceSelection
from l5_services.api.router import create_router
from l5_services.devops.access import authorize_browser_launch

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def offline_launch():
    selection = SpaceSelection("tenant", "product", "project", "binding", "123456789012", "us-east-1")
    actor = SimpleNamespace(user_id="member-one", org_id="tenant", account_type="human",
                            auth_source="jwt", expires_at=NOW + timedelta(minutes=20))
    member = SimpleNamespace(id="member-one", org_id="tenant", user_kind="human", is_shadow=False)
    member_sessions = {
        "Bearer offline-access": (actor, member),
        "Bearer offline-other": (
            SimpleNamespace(user_id="member-two", org_id="tenant", account_type="human",
                            auth_source="jwt", expires_at=NOW + timedelta(minutes=20)),
            SimpleNamespace(id="member-two", org_id="tenant", user_kind="human", is_shadow=False),
        ),
    }
    configuration = SimpleNamespace(tenant_id="tenant", product_id="product", project_id="project",
                                    service_id="devops", relationship_revision="3", current_revision=2,
                                    handoff_state="linked")
    version = SimpleNamespace(revision=2, state="ready", settings={"devops": {
        "browser_launch_policy": "scoped_session", "authentication_mode": "scoped_iam_federation",
        "permitted_native_actions": ["launchNativeBrowser"],
    }})
    receipt = SimpleNamespace(tenant_id="tenant", configuration_revision=2, state="ready",
                              native_space_id="space-one", scope_fingerprint=selection.fingerprint(),
                              selection=vars(selection).copy())

    def assume_role(**arguments):
        role_arn = arguments["RoleArn"]
        account_id = role_arn.split(":")[4]
        return {"AssumedRoleUser": {"Arn": (
            f"arn:aws:sts::{account_id}:assumed-role/{role_arn.rsplit('/', 1)[-1]}/" + arguments["RoleSessionName"]
        )}, "Credentials": {"AccessKeyId": "offline-id", "SecretAccessKey": "offline-key",
                          "SessionToken": "offline-session", "Expiration": NOW + timedelta(minutes=15)}}

    sts_client = Mock(assume_role=Mock(side_effect=assume_role))
    operator_client = Mock(
        get_agent_space=Mock(return_value={"agentSpace": {"agentSpaceId": "space-one"}}),
        get_operator_app=Mock(return_value={"iam": {"operatorAppRoleArn": "arn:aws:iam::123456789012:role/DevopsOperator"},
                                            "operatorAppUrl": "https://space-one.aidevops.global.app.aws/"}),
    )
    provider_requests = []

    def respond(request):
        provider_requests.append(request)
        return httpx.Response(200, json={"SigninToken": "offline-ticket"})

    provider_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))

    class Backend:
        def __init__(self):
            self.calls = []

        async def launch_browser(self, product_id, project_id, binding_id, configuration_revision, request):
            session = member_sessions.get(request.headers.get("Authorization"))
            if session is None:
                raise HTTPException(401, detail={"code": "session_required"})
            request_actor, request_member = session
            verified = authorize_browser_launch(
                request_actor, request_member, configuration, version, receipt, product_id=product_id,
                project_id=project_id, binding_id=binding_id, configuration_revision=configuration_revision,
                relationship_revision=3, action_allowed=True, now=NOW,
            )
            self.calls.append(verified.fingerprint())
            return await mint_browser_launch(
                verified, native_space_id=receipt.native_space_id,
                launcher_role_arn="arn:aws:iam::123456789012:role/DevopsLauncher",
                operator_role_arn="arn:aws:iam::123456789012:role/DevopsOperator",
                user_id=request_actor.user_id, issuer="https://adp.example.invalid", sts_client=sts_client,
                federation_client=provider_client, operator_client=operator_client, now=NOW,
            )

    backend = Backend()
    app = FastAPI()
    app.include_router(create_router(lambda: backend))
    async with provider_client, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline.invalid",
    ) as client:
        yield SimpleNamespace(client=client, backend=backend, sts=sts_client, provider_client=provider_client,
                              provider_requests=provider_requests,
                              selection=selection,
                              foreign_bookmark="/v1/products/product/projects/project/services/devops/bindings/foreign/browser-launch")


@pytest.mark.asyncio
async def test_session_launch(offline_launch):
    fixture = offline_launch
    path = "/v1/products/product/projects/project/services/devops/bindings/binding/browser-launch"
    headers = {"Authorization": "Bearer offline-access"}
    accepted = await fixture.client.post(path, headers=headers, json={"configuration_revision": 2})

    assert accepted.status_code == 200
    assert accepted.headers["Cache-Control"] == "no-store"
    assert accepted.headers["Referrer-Policy"] == "no-referrer"
    assert accepted.json()["binding_id"] == "binding"
    assert accepted.json()["expires_at"] == "2026-01-01T00:15:00Z"
    assert fixture.backend.calls == [fixture.selection.fingerprint()]
    assumed = fixture.sts.assume_role.call_args.kwargs
    assert assumed["RoleArn"] == "arn:aws:iam::123456789012:role/DevopsLauncher"
    assert assumed["RoleSessionName"] == assumed["SourceIdentity"]
    assert assumed["DurationSeconds"] == 900
    assert len(fixture.provider_requests) == 1
    assert fixture.provider_requests[0].url.host == "signin.aws.amazon.com"
    destination = parse_qs(urlsplit(accepted.json()["launch_url"]).query)["Destination"][0]
    assert "/agent-spaces/space-one/launch" in destination
    assert "offline-key" not in accepted.text and "offline-session" not in accepted.text

    missing = await fixture.client.post(path, json={"configuration_revision": 2})
    forged = await fixture.client.post(path, headers=headers,
                                       json={"configuration_revision": 2, "redirect": "https://foreign.invalid"})
    foreign = await fixture.client.post(path.replace("/projects/project/", "/projects/foreign/"),
                                        headers=headers, json={"configuration_revision": 2})
    assert (missing.status_code, forged.status_code, foreign.status_code) == (401, 422, 404)
    assert fixture.sts.assume_role.call_count == len(fixture.provider_requests) == 1


@pytest.mark.asyncio
async def test_member_isolation(offline_launch):
    fixture = offline_launch
    path = "/v1/products/product/projects/project/services/devops/bindings/binding/browser-launch"
    first_headers = {"Authorization": "Bearer offline-access"}
    second_headers = {"Authorization": "Bearer offline-other"}
    for headers in (first_headers, second_headers, first_headers):
        response = await fixture.client.post(path, headers=headers, json={"configuration_revision": 2})
        assert response.status_code == 200
        assert response.json()["binding_id"] == "binding"
        assert response.headers["Cache-Control"] == "no-store"
        assert "offline-session" not in response.text

    assumed = fixture.sts.assume_role.call_args_list
    identities = [invocation.kwargs["SourceIdentity"] for invocation in assumed]
    assert len(identities) == 3
    assert identities[0] == identities[2] != identities[1]
    assert all(identity.startswith("adp-") for identity in identities)
    assert all(invocation.kwargs["RoleSessionName"] == invocation.kwargs["SourceIdentity"]
               for invocation in assumed)
    assert len(fixture.provider_requests) == 3

    foreign = await fixture.client.post(fixture.foreign_bookmark, headers=second_headers,
                                        json={"configuration_revision": 2})
    tampered = await fixture.client.post(path, headers=first_headers,
                                         json={"configuration_revision": 2, "user_id": "member-two"})
    unknown = await fixture.client.post(path, headers={"Authorization": "Bearer foreign"},
                                        json={"configuration_revision": 2})
    assert (foreign.status_code, tampered.status_code, unknown.status_code) == (404, 422, 401)
    assert fixture.backend.calls == [fixture.selection.fingerprint()] * 3
    assert fixture.sts.assume_role.call_count == len(fixture.provider_requests) == 3


@pytest.fixture
def two_project_configurations():
    entries = (
        (SpaceSelection("tenant", "alpha-product", "alpha-project", "alpha-binding",
                        "123456789012", "us-east-1"), "member-one", "space-alpha", "Alpha"),
        (SpaceSelection("tenant", "beta-product", "beta-project", "beta-binding",
                        "210987654321", "us-west-2"), "member-two", "space-beta", "Beta"),
    )
    cases = []
    for selection, user_id, native_space_id, role_suffix in entries:
        configuration = SimpleNamespace(tenant_id="tenant", product_id=selection.product_id,
                                        project_id=selection.project_id, service_id="devops",
                                        relationship_revision="3", current_revision=2, handoff_state="linked")
        version = SimpleNamespace(revision=2, state="ready", settings={"devops": {
            "browser_launch_policy": "scoped_session", "authentication_mode": "scoped_iam_federation",
            "permitted_native_actions": ["launchNativeBrowser"],
        }})
        receipt = SimpleNamespace(tenant_id="tenant", configuration_revision=2, state="ready",
                                  native_space_id=native_space_id, scope_fingerprint=selection.fingerprint(),
                                  selection=vars(selection).copy())
        values = {"actor": SimpleNamespace(user_id=user_id, org_id="tenant", account_type="human",
                                           auth_source="jwt", expires_at=NOW + timedelta(minutes=20)),
                  "member": SimpleNamespace(id=user_id, org_id="tenant", user_kind="human", is_shadow=False),
                  "configuration": configuration, "version": version, "receipt": receipt,
                  "product_id": selection.product_id, "project_id": selection.project_id,
                  "binding_id": selection.binding_id, "configuration_revision": 2, "relationship_revision": 3,
                  "now": NOW}
        cases.append(SimpleNamespace(selection=selection, values=values, native_space_id=native_space_id,
                                     launcher_role_arn=f"arn:aws:iam::{selection.account_id}:role/DevopsLauncher{role_suffix}",
                                     operator_role_arn=f"arn:aws:iam::{selection.account_id}:role/DevopsOperator{role_suffix}"))
    actions = {(case.selection.product_id, case.selection.project_id, case.values["actor"].user_id):
               {"launchNativeBrowser"} for case in cases}
    return SimpleNamespace(cases=cases, actions=actions)


@pytest.mark.asyncio
async def test_project_session_policy(offline_launch, two_project_configurations):
    fixture = offline_launch
    projects = two_project_configurations
    for case in projects.cases:
        scope = (case.selection.product_id, case.selection.project_id, case.values["actor"].user_id)
        allowed = "launchNativeBrowser" in projects.actions.get(scope, set())
        verified = authorize_browser_launch(**case.values, action_allowed=allowed)
        assert verified == case.selection
        handoff = await mint_browser_launch(
            verified, native_space_id=case.native_space_id, launcher_role_arn=case.launcher_role_arn,
            operator_role_arn=case.operator_role_arn, user_id=case.values["actor"].user_id,
            issuer="https://adp.example.invalid", sts_client=fixture.sts,
            federation_client=fixture.provider_client, now=NOW,
            operator_client=Mock(
                get_agent_space=Mock(return_value={"agentSpace": {"agentSpaceId": case.native_space_id}}),
                get_operator_app=Mock(return_value={"iam": {"operatorAppRoleArn": case.operator_role_arn},
                                                   "operatorAppUrl": f"https://{case.native_space_id}.aidevops.global.app.aws/"}),
            ),
        )
        assert handoff["binding_id"] == case.selection.binding_id
        destination = parse_qs(urlsplit(handoff["launch_url"]).query)["Destination"][0]
        assert f"/agent-spaces/{case.native_space_id}/launch" in destination
        assert fixture.sts.assume_role.call_args.kwargs["RoleArn"] == case.launcher_role_arn

    assert len(fixture.provider_requests) == 2
    assert fixture.sts.assume_role.call_count == 2
    assert (fixture.sts.assume_role.call_args_list[0].kwargs["SourceIdentity"] !=
            fixture.sts.assume_role.call_args_list[1].kwargs["SourceIdentity"])
    for case, other in ((projects.cases[0], projects.cases[1]),
                        (projects.cases[1], projects.cases[0])):
        with pytest.raises(HTTPException) as foreign:
            authorize_browser_launch(**{**case.values, "project_id": other.selection.project_id}, action_allowed=True)
        assert foreign.value.status_code == 404
        with pytest.raises(HTTPException) as denied:
            authorize_browser_launch(**case.values, action_allowed=False)
        assert denied.value.status_code == 404
    assert fixture.sts.assume_role.call_count == len(fixture.provider_requests) == 2
