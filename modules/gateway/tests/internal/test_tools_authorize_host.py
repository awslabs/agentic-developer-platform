"""The tools decision plane treats a Dev Box host assertion as the owner, bound to the host run (#8380)."""

from fastapi import HTTPException

from src.crew.host_tools import ASSERTION_HEADER, CrewHostToolsIdentity
from src.internal import tools_authorize as routes
from tests.internal.test_tools_authorize import BODY, HUMAN
from tests.internal.test_tools_authorize import decision_api as decision_api_fixture

decision_api = decision_api_fixture
RUN = "11111111-2222-4333-8444-555555555555"
HOST_HEADERS = {ASSERTION_HEADER: "host-assertion", "x-agent-runid": RUN}


def host_identity(**changes):
    fields = {
        "tenant_id": "tenant-one",
        "user_id": HUMAN,
        "run_id": RUN,
        "instance_id": "i-0123456789abcdef0",
        "box_id": "box-one",
        "generation": 3,
        "expires_at": 4102444800,
    }
    return CrewHostToolsIdentity(**{**fields, **changes})


def test_host_assertion_decides_as_the_owner_with_the_host_run_binding(decision_api, monkeypatch):
    client, _, _, _, _, _ = decision_api
    seen = []

    async def authenticate(request):
        seen.append(request.headers[ASSERTION_HEADER])
        return host_identity()

    monkeypatch.setattr(routes, "authenticate_crew_host", authenticate)
    response = client.post("/internal/v1/tools/authorize", json=BODY, headers=HOST_HEADERS)
    assert response.status_code == 200, response.text
    decision = response.json()
    assert decision["allowed"] is True
    assert decision["principal"] == {"user_id": HUMAN, "root_human_id": HUMAN, "tenant_id": "tenant-one", "team_id": "team-one"}
    assert decision["run_id"] == RUN and decision["workload_id"] == "i-0123456789abcdef0" and decision["run_expires_at"] == 4102444800
    # Identity was read twice: once to decide, once more right before the receipt was released.
    assert seen == ["host-assertion", "host-assertion"]
    recorded = client.app.state.tool_activity_recorder.call_args.kwargs
    assert recorded["user_id"] == HUMAN and recorded["run_id"] == RUN and recorded["tool"] == "websearch___search"


def test_host_listing_is_filtered_by_the_owner_policy(decision_api, monkeypatch):
    client, _, _, policy, _, _ = decision_api
    policy["allowed_tools"] = ["websearch.search", "browser.start"]
    policy["consented_tools"] = ["websearch.search"]
    policy["catalog"] = [
        {"name": name, "description": "Offline fixture", "inputSchema": {"type": "object"}}
        for name in ("websearch___search", "browser___start", "code_interpreter___start")
    ]

    async def authenticate(request):
        return host_identity()

    monkeypatch.setattr(routes, "authenticate_crew_host", authenticate)
    response = client.post("/internal/v1/tools/authorize", json={"schema_version": "1.0", "action": "list"}, headers=HOST_HEADERS)
    assert response.status_code == 200, response.text
    assert [tool["name"] for tool in response.json()["tools"]] == ["websearch___search"]


def test_host_assertion_with_a_bearer_or_crew_proof_is_ambiguous(decision_api, monkeypatch):
    client, _, _, _, _, _ = decision_api
    called = []

    async def authenticate(request):
        called.append(True)
        return host_identity()

    monkeypatch.setattr(routes, "authenticate_crew_host", authenticate)
    for extra in ({"x-adp-user-authorization": "Bearer laptop"}, {"x-adp-chat-capability": "crew"}):
        response = client.post("/internal/v1/tools/authorize", json=BODY, headers={**HOST_HEADERS, **extra})
        assert response.status_code == 403 and response.json()["detail"] == "ambiguous tool identity"
    assert called == []


def test_host_whose_target_moved_between_decision_and_receipt_gets_unknown_run(decision_api, monkeypatch):
    client, _, _, _, _, _ = decision_api
    answers = [host_identity(), host_identity(instance_id="i-22222222222222222", generation=4)]

    async def authenticate(request):
        return answers.pop(0)

    monkeypatch.setattr(routes, "authenticate_crew_host", authenticate)
    response = client.post("/internal/v1/tools/authorize", json=BODY, headers=HOST_HEADERS)
    assert response.status_code == 404 and response.json()["detail"] == "unknown_run"
    assert not client.app.state.tool_activity_recorder.called


def test_host_refusal_from_the_live_target_check_is_not_softened(decision_api, monkeypatch):
    client, _, _, _, _, _ = decision_api

    async def authenticate(request):
        raise HTTPException(404, "unknown_run")

    monkeypatch.setattr(routes, "authenticate_crew_host", authenticate)
    response = client.post("/internal/v1/tools/authorize", json=BODY, headers=HOST_HEADERS)
    assert response.status_code == 404
    assert not client.app.state.tool_activity_recorder.called


def test_host_must_be_an_existing_human_team_member(decision_api, monkeypatch):
    client, _, _, _, _, db = decision_api
    db.scalar.return_value = None

    async def authenticate(request):
        return host_identity()

    monkeypatch.setattr(routes, "authenticate_crew_host", authenticate)
    response = client.post("/internal/v1/tools/authorize", json=BODY, headers=HOST_HEADERS)
    assert response.status_code == 403 and response.json()["detail"] == "tool authority refused"
