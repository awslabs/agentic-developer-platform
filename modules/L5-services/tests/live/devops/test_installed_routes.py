"""Installed route recovery with only external AWS calls substituted."""

import json
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import boto3
import httpx
import pytest
from botocore.stub import Stubber

import installed_routes
import recovery_selector
from deployed_routes import route_definitions
from installed_routes import cleanup_reserve, validate_installed_routes
from provider_routes import recovery_routes
from recovery import DENIALS, FAULTS, verify_recovery
from recovery_selector import HTTPRecoveryTarget, authorize_recovery

pytest_plugins = ("test_recovery_selector",)


def export_document(routes):
    paths = {}
    for route in routes.values():
        method, path = route["RouteKey"].split(" ", 1)
        paths.setdefault(path, {})[method.lower()] = {
            "responses": {"default": {"description": "default"}},
            "x-amazon-apigateway-integration": {"type": "aws_proxy", "uri": route["Target"]}}
    return json.dumps({"openapi": "3.0.1", "info": {"title": "Example", "version": "1"}, "paths": paths}).encode()


@pytest.fixture
def routes_case(recovery_selection, monkeypatch):
    manifest, _, directory, _ = recovery_selection
    manifest["authorization"]["expires_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    recovery = manifest["recovery"]
    recovery["owned_routes"] = ["example:owned"]
    recovery["retained_routes"] = ["example:retained"]
    reader = {"connection_label": "route-reader", "account_id": "123456789012",
              "role_arn": "arn:aws:iam::123456789012:role/RouteReader", "region": "us-east-1"}
    actor = {**reader, "connection_label": "route-actor", "role_arn": "arn:aws:iam::123456789012:role/RouteActor"}
    recovery["installed_routes"] = {"api_id": "example", "reader": reader, "actor": actor}
    recovery["action_channels"].pop("cleanup_routes")
    for name in ("routes", "route_owners", "route_inventory"):
        recovery["readback_channels"].pop(name)
    tags = {**{f"adp:{field}": value for field, value in recovery["scope"].items()},
            "adp:qualification-run": manifest["authorization"]["run_id"]}
    api = {"ApiId": "example", "ProtocolType": "HTTP", "Tags": tags}
    native_routes = {"owned": {"RouteId": "owned", "RouteKey": "GET /obsolete", "Target": "integrations/owned"},
                     "retained": {"RouteId": "retained", "RouteKey": "GET /retained", "Target": "integrations/retained"},
                     "other": {"RouteId": "other", "RouteKey": "GET /other", "Target": "integrations/other"}}
    state = {"api": api, "routes": native_routes, "wrong_role": False, "repeat_cursor": False,
             "changed_before_delete": False, "lost_delete_ack": False, "keep_owned": False,
             "auto_deploy": True, "deployment": "before", "deployment_status": "DEPLOYED",
             "hold_deployment": False, "deployment_cutoff": None, "deletions": 0,
             "deployed_routes": deepcopy(native_routes), "stage_names": ["$default"],
             "stage_overrides": {}, "reader_override": None, "propagation_polls": None}
    calls = []

    def client_factory(role):
        def record(name, **parameters):
            calls.append((role, name, parameters))
            if name == "get_caller_identity":
                return {"Account": reader["account_id"], "Arn": "arn:aws:sts::123456789012:assumed-role/"
                        + ("Wrong" if state["wrong_role"] else role) + "/session"}
            assert parameters["ApiId"] == "example"
            if name == "get_api":
                return deepcopy(state["api"])
            if name == "get_routes":
                return {"Items": deepcopy(list(state["routes"].values())),
                        **({"NextToken": "repeat"} if state["repeat_cursor"] else {})}
            if name == "get_route":
                route = deepcopy(state["routes"][parameters["RouteId"]])
                if state["changed_before_delete"]:
                    route["Target"] = "integrations/replaced"
                return {**route, "ResponseMetadata": {"RequestId": "example"}}
            if name == "get_stages":
                if state["deletions"] >= 2 and state["propagation_polls"] is not None:
                    if state["propagation_polls"] == 0:
                        state["deployed_routes"] = deepcopy(state["routes"])
                        state["deployment"] = "complete"
                    else:
                        state["propagation_polls"] -= 1
                return {"Items": [{"StageName": name, "AutoDeploy": state["auto_deploy"],
                                   "DeploymentId": state["deployment"]} for name in state["stage_names"]]}
            if name == "get_deployment":
                return {"DeploymentId": parameters["DeploymentId"], "DeploymentStatus": state["deployment_status"]}
            if name == "export_api":
                assert parameters["StageName"] in state["stage_names"]
                assert parameters == {"ApiId": "example", "StageName": parameters["StageName"], "Specification": "OAS30",
                                      "OutputType": "JSON", "IncludeExtensions": True}
                observed = state["stage_overrides"].get(parameters["StageName"], state["deployed_routes"])
                if role == "RouteReader" and state["deletions"] and state["reader_override"] is not None:
                    observed = state["reader_override"]
                return {"body": export_document(observed)}
            if name == "get_stage":
                return {"StageName": parameters["StageName"], "AutoDeploy": state["auto_deploy"], "DeploymentId": state["deployment"]}
            assert name == "delete_route" and role == "RouteActor"
            assert "example:" + parameters["RouteId"] in recovery["owned_routes"]
            if not state["keep_owned"]:
                del state["routes"][parameters["RouteId"]]
                state["deletions"] += 1
                if not state["hold_deployment"] and (state["deployment_cutoff"] is None
                                                     or state["deletions"] <= state["deployment_cutoff"]):
                    state["deployment"] = "after" + str(state["deletions"])
                    state["deployed_routes"] = deepcopy(state["routes"])
            if state["lost_delete_ack"]:
                state["lost_delete_ack"] = False
                raise TimeoutError("lost deletion acknowledgement")
            return {}

        def factory(service, **kwargs):
            names = ("get_caller_identity",) if service == "sts" else (
                "get_api", "get_routes", "get_route", "delete_route", "get_stages", "get_deployment", "export_api", "get_stage")
            assert service in {"sts", "apigatewayv2"}
            return SimpleNamespace(**{name: lambda name=name, **parameters: record(name, **parameters) for name in names})

        return factory

    invocations = []

    def invoke(authority, selection, *, deadline):
        invocations.append((deepcopy(authority), deepcopy(selection)))
        assert deadline > time.monotonic()
        return recovery_routes(authority["account_id"], authority["role_arn"], authority["region"], selection,
                               client_factory=client_factory(authority["role_arn"].split("/")[-1]))

    monkeypatch.setattr(installed_routes, "invoke_routes", invoke)
    target = HTTPRecoveryTarget(manifest, directory, deadline=time.monotonic() + 60, max_requests=100)
    yield SimpleNamespace(manifest=manifest, directory=directory, target=target, state=state,
                          scope=recovery["scope"], calls=calls, invocations=invocations,
                          factory=client_factory)
    target.close()


def test_installed_routes_replace_four_custom_channels(routes_case):
    case = routes_case
    assert authorize_recovery(case.manifest, case.manifest["release"]["source_sha"], str(case.directory)) == case.directory
    assert case.target.cleanup_reserve == cleanup_reserve(case.manifest) + 1
    assert "cleanup_routes" not in case.target.actions
    assert not {"routes", "route_owners", "route_inventory"} & set(case.target.readbacks)
    assert case.target.read_route_owners(case.scope, ["example:owned"]) == {"example:owned": case.scope}
    baseline = case.target.read_route_inventory(case.scope)
    case.target.cleanup_routes(case.scope, ["example:owned"])
    assert case.target.read_routes(case.scope, ["example:owned"]) == {}
    assert case.target.read_route_inventory(case.scope) == {key: value for key, value in baseline.items() if key != "example:owned"}
    assert [name for role, name, _ in case.calls if role == "RouteActor"].count("delete_route") == 1
    assert not any(name == "delete_route" for role, name, _ in case.calls if role == "RouteReader")
    assert case.invocations[-1][0]["connection_label"] == "route-reader"


@pytest.mark.parametrize("stage_names", [["$default"], ["$default", "live"]])
def test_complete_recovery_with_installed_routes_fits_request_budget(routes_case, monkeypatch, stage_names):
    case = routes_case
    case.manifest["recovery"]["bounds"]["max_requests"] = 100
    case.state["stage_names"] = stage_names
    sessions, jobs, faults, space_reads = [], {}, [], []
    external_requests = []

    def browser(command, **kwargs):
        external_requests.append("browser")
        assert command[0] == "node"
        request = json.loads(kwargs["input"])
        if request["kind"] == "allowed":
            user = next(user for user in case.manifest["recovery"]["users"]
                        if case.manifest["recovery"]["browser"]["storage_states"][user["id"]]
                        == request["storage_state"])
            sessions.append({"user_id": user["id"], "scope": case.scope,
                             "provider_identity": "native-" + user["id"]})
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "status": 200 if request["kind"] == "allowed" else DENIALS[request["kind"]]}).encode())

    def transport(request):
        external_requests.append(str(request.url))
        if request.url.path == "/api/auth/logout":
            return httpx.Response(200, json={"message": "Logout successful"})
        action = request.method == "POST"
        channels = case.target.actions if action else case.target.readbacks
        channel = next(name for name, selection in channels.items()
                       if selection["url"] == str(request.url.copy_with(query=None)))
        body = json.loads(request.content if action else request.url.params["selection"])
        if action:
            if channel == "inject_fault":
                faults.append(body["fault"])
            if channel == "submit":
                receipt = request.headers["Idempotency-Key"]
                jobs[receipt] = {"scope": case.scope, "receipt": receipt,
                                         "status": "completed", "work_id": "work-" + body["fault"],
                                         "native_job_id": "job-" + body["fault"], "native_submit_count": 1}
                raise httpx.ReadTimeout("submission acknowledgement lost", request=request)
            return httpx.Response(204)
        if channel == "spaces":
            space_reads.append(case.target.calls)
        observation = {"sessions": sessions, "provider_calls": [], "spaces": {"retained": "space"},
                       "work": jobs.get(body.get("receipt"))}[channel]
        return httpx.Response(200, json={"target_ref": case.target.target,
                                        "qualification_run_id": case.target.run_id, "observation": observation})

    monkeypatch.setattr(recovery_selector.subprocess, "run", browser)
    case.target.client.close()
    case.target.client = httpx.Client(transport=httpx.MockTransport(transport))
    proof = verify_recovery(case.manifest, case.target, case.target)
    assert faults == list(FAULTS)
    assert set(proof["recovery"]) == set(FAULTS)
    assert proof["cleanup"] == {"owned_absent": True, "retained_unchanged": True}
    assert len(space_reads) == 2 and space_reads[-1] == case.target.calls
    assert case.target.calls <= 100
    assert len(case.calls) + len(external_requests) == case.target.calls
    assert "owned" not in case.state["routes"]
    assert len(case.invocations) == 4


def test_final_route_read_failure_preserves_retained_space_allocation(routes_case):
    case = routes_case
    case.target.read_route_snapshot(case.scope, ownership=True)
    case.target.cleanup_routes(case.scope, ["example:owned"])
    case.state["wrong_role"] = True
    with pytest.raises(ValueError, match="identity"):
        case.target.read_route_snapshot(case.scope)
    assert case.target.max_requests - case.target.calls == 1


def test_logout_does_not_consume_route_and_space_cleanup_reserve(routes_case, monkeypatch):
    case = routes_case
    case.target.read_route_snapshot(case.scope, ownership=True)
    case.target.calls = case.target.max_requests - case.target.cleanup_reserve - 1

    def forbidden_request(*args, **kwargs):
        pytest.fail("logout must not consume cleanup requests")

    monkeypatch.setattr(case.target.client, "stream", forbidden_request)
    with pytest.raises(ValueError, match="reserved cleanup"):
        case.target.logout(case.manifest["recovery"]["users"][0]["credential_ref"])


def test_independent_cleanup_failure_preserves_final_observation_allocation(routes_case, monkeypatch):
    case = routes_case
    case.target.read_route_snapshot(case.scope, ownership=True)
    invoke = installed_routes.invoke_routes

    def fail_reader(authority, selection, *, deadline):
        if selection["operation"] == "read":
            raise TimeoutError("independent route read unavailable")
        return invoke(authority, selection, deadline=deadline)

    monkeypatch.setattr(installed_routes, "invoke_routes", fail_reader)
    with pytest.raises(TimeoutError):
        case.target.cleanup_routes(case.scope, ["example:owned"])
    assert case.target.max_requests - case.target.calls == case.target.installed_routes.read_requests + 1


@pytest.mark.parametrize("change", [
    lambda selected: selected["actor"].update(connection_label="route-reader"),
    lambda selected: selected["actor"].update(role_arn=selected["reader"]["role_arn"]),
    lambda selected: selected["actor"].update(region="eu-west-1"),
    lambda selected: selected["reader"].update(account_id="210987654321"),
    lambda selected: selected.update(api_id="another"),
])
def test_route_selection_rejects_wrong_or_nonindependent_authority(routes_case, change):
    change(routes_case.manifest["recovery"]["installed_routes"])
    with pytest.raises(ValueError):
        validate_installed_routes(routes_case.manifest)
    assert routes_case.calls == []


@pytest.mark.parametrize("method,args", [
    ("read_route_inventory", ()), ("read_routes", (["example:owned"],)),
    ("read_route_owners", (["example:owned"],)), ("cleanup_routes", (["example:owned"],)),
])
def test_foreign_product_makes_no_provider_calls(routes_case, method, args):
    with pytest.raises(ValueError, match="foreign scope"):
        getattr(routes_case.target, method)({**routes_case.scope, "product_id": "foreign"}, *args)
    assert routes_case.calls == []


@pytest.mark.parametrize("change", [
    lambda state: state.update(wrong_role=True),
    lambda state: state["api"]["Tags"].update({"adp:product_id": "foreign"}),
    lambda state: state["api"]["Tags"].update({"adp:qualification-run": "another-run"}),
    lambda state: state["api"].update(ProtocolType="WEBSOCKET"),
    lambda state: state["api"].update(ApiId="other"),
    lambda state: state["api"].update(ApiGatewayManaged=True),
])
def test_unverified_native_scope_cannot_be_read_or_deleted(routes_case, change):
    change(routes_case.state)
    with pytest.raises(ValueError):
        routes_case.target.read_route_inventory(routes_case.scope)
    assert not any(name in {"get_routes", "delete_route"} for _, name, _ in routes_case.calls)


@pytest.mark.parametrize("change", [
    lambda state: state["routes"]["owned"].update(Target="integrations/replaced"),
    lambda state: state["routes"]["owned"].update(ApiGatewayManaged=True),
    lambda state: state.update(changed_before_delete=True),
    lambda state: state["api"]["Tags"].update({"adp:tenant_id": "foreign"}),
])
def test_cleanup_revalidates_ownership_and_unchanged_route(routes_case, change):
    case = routes_case
    case.target.read_route_inventory(case.scope)
    change(case.state)
    with pytest.raises(ValueError):
        case.target.cleanup_routes(case.scope, ["example:owned"])
    assert not any(name == "delete_route" for _, name, _ in case.calls)


def test_cleanup_refuses_retained_or_unobserved_routes(routes_case):
    case = routes_case
    with pytest.raises(ValueError, match="baseline"):
        case.target.cleanup_routes(case.scope, ["example:owned"])
    case.target.read_route_inventory(case.scope)
    with pytest.raises(ValueError, match="baseline"):
        case.target.cleanup_routes(case.scope, ["example:retained"])
    assert not any(name == "delete_route" for _, name, _ in case.calls)


def test_unpersisted_baseline_never_permits_cleanup(routes_case, monkeypatch):
    case = routes_case

    def unavailable_journal(*args, **kwargs):
        raise OSError("private journal unavailable")

    monkeypatch.setattr(case.target, "_journal", unavailable_journal)
    with pytest.raises(OSError, match="journal"):
        case.target.read_route_inventory(case.scope)
    with pytest.raises(ValueError, match="baseline"):
        case.target.cleanup_routes(case.scope, ["example:owned"])
    assert not any(name == "delete_route" for _, name, _ in case.calls)


def test_cleanup_acknowledgement_without_absence_is_not_success(routes_case):
    case = routes_case
    case.target.read_route_inventory(case.scope)
    case.state["keep_owned"] = True
    with pytest.raises(ValueError, match="preserve"):
        case.target.cleanup_routes(case.scope, ["example:owned"])
    journal = next(case.directory.glob("*-recovery.jsonl")).read_text()
    assert '"phase": "uncertain"' in journal and '"phase": "acknowledged"' not in journal


def test_lost_delete_ack_is_reconciled_without_a_second_effect(routes_case):
    case = routes_case
    case.target.read_route_inventory(case.scope)
    snapshots = {"owned": deepcopy(case.state["routes"]["owned"])}
    case.state["lost_delete_ack"] = True
    with pytest.raises(TimeoutError):
        case.target.cleanup_routes(case.scope, ["example:owned"])
    journal = [json.loads(line) for line in next(case.directory.glob("*-recovery.jsonl")).read_text().splitlines()]
    assert journal[0]["operation"] == "route_baseline"
    assert journal[0]["evidence"]["routes"]["example:owned"] == snapshots["owned"]
    assert journal[-1]["phase"] == "uncertain"
    selection = case.invocations[-1][1]
    assert selection["expected_routes"] == snapshots
    result = recovery_routes("123456789012", "arn:aws:iam::123456789012:role/RouteActor", "us-east-1",
                             selection, client_factory=case.factory("RouteActor"))
    assert "owned" not in result["routes"]
    assert sum(name == "delete_route" for _, name, _ in case.calls) == 1


@pytest.mark.parametrize("bound", ["time", "requests", "authorization"])
def test_exhausted_bound_stops_before_provider_calls(routes_case, bound):
    case = routes_case
    if bound == "time":
        case.target.deadline = time.monotonic() - 1
    elif bound == "requests":
        case.target.calls = case.target.max_requests - case.target.cleanup_reserve
    else:
        case.manifest["authorization"]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="bounds"):
        case.target.read_route_inventory(case.scope)
    assert case.calls == []


def test_pagination_is_bounded_and_never_returns_partial_inventory(routes_case):
    routes_case.state["repeat_cursor"] = True
    with pytest.raises(ValueError, match="ambiguous|cursor"):
        routes_case.target.read_route_inventory(routes_case.scope)
    assert len(routes_case.calls) == 4


@pytest.mark.parametrize("change", [
    lambda state: state.update(auto_deploy=False),
    lambda state: state.update(deployment_status="PENDING"),
])
def test_manual_or_unready_deployments_cannot_be_cleaned(routes_case, change):
    case = routes_case
    case.target.read_route_inventory(case.scope)
    change(case.state)
    with pytest.raises(ValueError, match="deploy"):
        case.target.cleanup_routes(case.scope, ["example:owned"])
    assert not any(name == "delete_route" for _, name, _ in case.calls)


@pytest.mark.parametrize("deployment", ["before", "unrelated"])
def test_old_serving_deployment_is_not_reported_clean(routes_case, monkeypatch, deployment):
    case = routes_case
    case.target.read_route_inventory(case.scope)
    case.state["deployment"] = deployment
    case.state["hold_deployment"] = True
    monkeypatch.setattr("provider_routes.time.sleep", lambda duration: None)
    with pytest.raises(ValueError, match="allocation"):
        case.target.cleanup_routes(case.scope, ["example:owned"])
    assert case.target.calls < case.target.max_requests
    assert case.target.max_requests - case.target.calls >= 2 * case.target.installed_routes.read_requests + 1


@pytest.mark.parametrize("eventually_propagates", [False, True])
def test_two_route_intermediate_deployment_is_not_acknowledged(routes_case, monkeypatch, eventually_propagates):
    case = routes_case
    case.manifest["recovery"]["owned_routes"].append("example:second")
    case.state["routes"]["second"] = {"RouteId": "second", "RouteKey": "POST /second", "Target": "integrations/second"}
    case.state["deployed_routes"] = deepcopy(case.state["routes"])
    case.target.read_route_inventory(case.scope)
    case.state["deployment_cutoff"] = 1
    case.state["propagation_polls"] = 1 if eventually_propagates else None
    monkeypatch.setattr("provider_routes.time.sleep", lambda duration: None)
    if eventually_propagates:
        case.target.cleanup_routes(case.scope, case.manifest["recovery"]["owned_routes"])
    else:
        with pytest.raises(ValueError, match="allocation"):
            case.target.cleanup_routes(case.scope, case.manifest["recovery"]["owned_routes"])
    assert "second" not in case.state["routes"]
    journal = [json.loads(line) for line in next(case.directory.glob("*-recovery.jsonl")).read_text().splitlines()]
    if eventually_propagates:
        assert "second" not in case.state["deployed_routes"]
        assert journal[-1]["phase"] == "acknowledged"
        assert case.target.calls <= case.target.max_requests
    else:
        assert "second" in case.state["deployed_routes"]
        assert journal[-1]["phase"] == "uncertain"
        assert not any(entry["phase"] == "acknowledged" for entry in journal)


def test_every_serving_stage_must_remove_owned_routes(routes_case, monkeypatch):
    case = routes_case
    case.state["stage_names"].append("secondary")
    case.target.read_route_inventory(case.scope)
    case.state["stage_overrides"]["secondary"] = deepcopy(case.state["routes"])
    monkeypatch.setattr("provider_routes.time.sleep", lambda duration: None)
    with pytest.raises(ValueError, match="allocation"):
        case.target.cleanup_routes(case.scope, ["example:owned"])
    assert any(name == "export_api" and arguments["StageName"] == "secondary" for _, name, arguments in case.calls)


@pytest.mark.parametrize("change", ["owned_present", "retained_changed", "retained_missing"])
def test_independent_stage_export_must_confirm_actual_cleanup(routes_case, change):
    case = routes_case
    case.target.read_route_inventory(case.scope)
    independent = deepcopy(case.state["routes"])
    if change != "owned_present":
        del independent["owned"]
    if change == "retained_changed":
        independent["retained"]["Target"] = "integrations/other"
    if change == "retained_missing":
        del independent["retained"]
    case.state["reader_override"] = independent
    with pytest.raises(ValueError, match="serving routes"):
        case.target.cleanup_routes(case.scope, ["example:owned"])
    assert "owned" not in case.state["deployed_routes"]
    journal = [json.loads(line) for line in next(case.directory.glob("*-recovery.jsonl")).read_text().splitlines()]
    assert journal[-1]["phase"] == "uncertain"


def test_provider_parameters_match_installed_aws_sdk(routes_case):
    case = routes_case
    credentials = {"region_name": "us-east-1", "aws_access_key_id": "testing", "aws_secret_access_key": "testing"}
    sts = boto3.client("sts", **credentials)
    api = boto3.client("apigatewayv2", **credentials)
    selection = {"api_id": "example", "scope": case.scope, "qualification_run_id": case.manifest["authorization"]["run_id"],
                 "operation": "cleanup", "routes": ["owned"], "max_requests": 20,
                 "expected_routes": {"owned": case.state["routes"]["owned"]},
                 "previous_deployments": {"$default": "before"},
                 "expected_stage_routes": {"$default": route_definitions(export_document({
                     key: value for key, value in case.state["routes"].items() if key != "owned"}))}}
    api_response = {**case.state["api"], "Name": "fictional-qualification", "RouteSelectionExpression": "$request.method $request.path"}
    with Stubber(sts) as identity_stub, Stubber(api) as api_stub:
        identity_stub.add_response("get_caller_identity", {"Account": "123456789012", "UserId": "example:session",
            "Arn": "arn:aws:sts::123456789012:assumed-role/RouteActor/session"}, {})
        api_stub.add_response("get_api", api_response, {"ApiId": "example"})
        api_stub.add_response("get_routes", {"Items": list(case.state["routes"].values())}, {"ApiId": "example", "MaxResults": "100"})
        api_stub.add_response("get_stages", {"Items": [{"StageName": "$default", "AutoDeploy": True, "DeploymentId": "before"}]},
                              {"ApiId": "example", "MaxResults": "100"})
        api_stub.add_response("get_deployment", {"DeploymentId": "before", "DeploymentStatus": "DEPLOYED"},
                              {"ApiId": "example", "DeploymentId": "before"})
        api_stub.add_response("export_api", {"body": export_document(case.state["routes"])},
                              {"ApiId": "example", "StageName": "$default", "Specification": "OAS30",
                               "OutputType": "JSON", "IncludeExtensions": True})
        api_stub.add_response("get_stage", {"StageName": "$default", "AutoDeploy": True, "DeploymentId": "before"},
                              {"ApiId": "example", "StageName": "$default"})
        api_stub.add_response("get_api", api_response, {"ApiId": "example"})
        api_stub.add_response("get_route", case.state["routes"]["owned"], {"ApiId": "example", "RouteId": "owned"})
        api_stub.add_response("delete_route", {}, {"ApiId": "example", "RouteId": "owned"})
        api_stub.add_response("get_routes", {"Items": [value for key, value in case.state["routes"].items() if key != "owned"]},
                              {"ApiId": "example", "MaxResults": "100"})
        api_stub.add_response("get_stages", {"Items": [{"StageName": "$default", "AutoDeploy": True, "DeploymentId": "after"}]},
                              {"ApiId": "example", "MaxResults": "100"})
        api_stub.add_response("get_deployment", {"DeploymentId": "after", "DeploymentStatus": "DEPLOYED"},
                              {"ApiId": "example", "DeploymentId": "after"})
        api_stub.add_response("export_api", {"body": export_document({
            key: value for key, value in case.state["routes"].items() if key != "owned"})},
            {"ApiId": "example", "StageName": "$default", "Specification": "OAS30",
             "OutputType": "JSON", "IncludeExtensions": True})
        api_stub.add_response("get_stage", {"StageName": "$default", "AutoDeploy": True, "DeploymentId": "after"},
                              {"ApiId": "example", "StageName": "$default"})
        result = recovery_routes("123456789012", "arn:aws:iam::123456789012:role/RouteActor", "us-east-1", selection,
                                 client_factory=lambda service, **kwargs: sts if service == "sts" else api)
        assert result["requests"] == 15 and "owned" not in result["routes"]
        identity_stub.assert_no_pending_responses()
        api_stub.assert_no_pending_responses()
