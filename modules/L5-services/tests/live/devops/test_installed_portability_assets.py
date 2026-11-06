"""Exercise scoped installed asset cleanup at external AWS boundaries only."""

import json
import os
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import httpx

import installed_portability_assets
from context_selector import HTTPSScopedTarget, authorize_channels
from installed_portability_assets import validate_installed_assets
from portability import verify_portability
from provider_routes import recovery_routes
from test_installed_routes import export_document
from test_portability import FakePlatform, FakeReader
import test_portability_selector as portability_fixtures
import test_selector as selector_fixtures

portable_selection = portability_fixtures.portable_selection
selector_case = selector_fixtures.selector_case


@pytest.fixture
def assets_case(portable_selection, monkeypatch):
    manifest, _, directory, _ = portable_selection
    manifest["authorization"]["expires_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    manifest["portability"]["limits"]["max_requests"] = 100
    manifest["portability"]["action_channels"].pop("cleanup_owned")
    manifest["portability"]["readback_channels"].pop("assets")
    entries, states, calls, invocations = [], {}, [], []
    for index, project in enumerate(manifest["projects"], start=1):
        api_id, account = f"example{index}", f"00000000000{index}"
        project.update(account_ref=account, owned_assets=[api_id + ":owned"], retained_assets=[api_id + ":retained"])
        reader = {"account_id": account, "role_arn": f"arn:aws:iam::{account}:role/Reader{index}",
                  "region": "us-east-1", "connection_label": f"reader-{index}"}
        actor = {**reader, "role_arn": f"arn:aws:iam::{account}:role/Actor{index}", "connection_label": f"actor-{index}"}
        entries.append({"scope": project["scope"], "api_id": api_id, "reader": reader, "actor": actor})
        routes = {name: {"RouteId": name, "RouteKey": f"GET /{name}", "Target": "integrations/" + name}
                  for name in ("owned", "retained", "other")}
        states[api_id] = {"routes": routes, "stages": [], "deployment": "before", "deployed": deepcopy(routes),
                          "api": {"ApiId": api_id, "ProtocolType": "HTTP", "Tags": {
                              **{f"adp:{key}": value for key, value in project["scope"].items()},
                              "adp:qualification-run": manifest["authorization"]["run_id"]}},
                          "lost_ack": False, "wrong_role": False, "changed_before_delete": False,
                          "tamper_reader": False, "deletes": 0}
    manifest["portability"]["installed_assets"] = {"projects": entries}

    def invoke(authority, selection, *, deadline):
        assert deadline > time.monotonic()
        invocations.append((deepcopy(authority), deepcopy(selection)))
        state = states[selection["api_id"]]
        role = authority["role_arn"].split("/")[-1]

        def record(name, **arguments):
            calls.append((role, name, arguments))
            if name == "get_caller_identity":
                assumed = "Wrong" if state["wrong_role"] else role
                return {"Account": authority["account_id"],
                        "Arn": f"arn:aws:sts::{authority['account_id']}:assumed-role/{assumed}/test"}
            assert arguments["ApiId"] == selection["api_id"]
            if name == "get_api":
                return deepcopy(state["api"])
            if name == "get_routes":
                routes = deepcopy(state["routes"])
                if state["tamper_reader"] and role.startswith("Reader") and state["deletes"]:
                    routes["retained"]["Target"] = "integrations/tampered"
                return {"Items": list(routes.values())}
            if name in {"get_stages", "get_stage"}:
                stages = [{"StageName": stage, "DeploymentId": state["deployment"], "AutoDeploy": True}
                          for stage in state["stages"]]
                return {"Items": stages} if name == "get_stages" else next(
                    stage for stage in stages if stage["StageName"] == arguments["StageName"])
            if name == "get_deployment":
                return {"DeploymentId": state["deployment"], "DeploymentStatus": "DEPLOYED"}
            if name == "export_api":
                return {"body": export_document(state["deployed"])}
            if name == "get_route":
                route = deepcopy(state["routes"][arguments["RouteId"]])
                if state["changed_before_delete"]:
                    route["Target"] = "integrations/changed"
                return route
            assert name == "delete_route" and role.startswith("Actor") and arguments["RouteId"] == "owned"
            state["routes"].pop("owned")
            state["deletes"] += 1
            state["deployment"] = "after"
            state["deployed"] = deepcopy(state["routes"])
            if state["lost_ack"]:
                state["lost_ack"] = False
                raise TimeoutError("lost deletion response")
            return {}

        def factory(service, **kwargs):
            assert kwargs["config"].retries["total_max_attempts"] == 1
            assert kwargs["region_name"] == authority["region"]
            names = (("get_caller_identity",) if service == "sts" else
                     ("get_api", "get_routes", "get_stages", "get_stage", "get_deployment", "export_api",
                      "get_route", "delete_route"))
            return SimpleNamespace(**{name: lambda name=name, **arguments: record(name, **arguments) for name in names})

        return recovery_routes(authority["account_id"], authority["role_arn"], authority["region"], selection,
                               client_factory=factory)

    monkeypatch.setattr(installed_portability_assets, "invoke_routes", invoke)
    target = HTTPSScopedTarget(manifest, directory, "portability", deadline=time.monotonic() + 60, max_requests=100)
    yield SimpleNamespace(manifest=manifest, directory=directory, entries=entries, states=states,
                          calls=calls, invocations=invocations, target=target)
    target.close()


def test_installed_assets_remove_only_the_two_replaced_channels(assets_case):
    case = assets_case
    assert authorize_channels(case.manifest, case.manifest["release"]["source_sha"],
                              str(case.directory), "portability") == case.directory
    assert "assets" not in case.manifest["portability"]["readback_channels"]
    assert "cleanup_owned" not in case.manifest["portability"]["action_channels"]
    case.manifest["portability"]["readback_channels"].pop("native")
    with pytest.raises(ValueError, match="bounded actions"):
        authorize_channels(case.manifest, case.manifest["release"]["source_sha"], str(case.directory), "portability")
    assert not case.calls


def test_two_product_assets_and_cleanup_preserve_other_routes_within_bound(assets_case):
    case = assets_case
    retained = []
    for project in case.manifest["projects"]:
        case.target.cleanup_phase = False
        scope = project["scope"]
        owned = case.target.read_assets(scope, project["owned_assets"])
        assert all(value["owner_scope"] == scope for value in owned.values())
        baseline = case.target.read_assets(scope, project["retained_assets"])
        retained.append((scope, project["retained_assets"], baseline))
        case.target.cleanup_owned(scope, project["owned_assets"])
        assert case.target.read_assets(scope, project["owned_assets"]) == {}
        assert case.target.read_assets(scope, project["retained_assets"]) == baseline
    for scope, refs, baseline in retained:
        assert case.target.read_assets(scope, refs) == baseline
    assert case.target.calls == len(case.calls) == 66
    assert case.target.cleanup_reserve == 4
    assert all(state["deletes"] == 1 and "other" in state["routes"] for state in case.states.values())
    assert not any(name == "delete_route" for role, name, _ in case.calls if role.startswith("Reader"))
    journal = case.directory / f"devops-{case.target.run_id}-portability.jsonl"
    rows = [json.loads(row) for row in journal.read_text().splitlines()]
    assert [row["phase"] for row in rows if row["operation"] == "cleanup_owned"] == [
        "prepared", "acknowledged", "prepared", "acknowledged"]
    assert os.stat(journal).st_mode & 0o777 == 0o600


@pytest.mark.parametrize("stages", [([], []), (["$default"], []), ([], ["$default"])])
def test_complete_cleanup_sequence_fits_at_non_cleanup_budget_boundary(assets_case, stages):
    case = assets_case
    retained = []
    for project, stage_names, entry in zip(case.manifest["projects"], stages, case.entries, strict=True):
        case.states[entry["api_id"]]["stages"] = stage_names
        baseline = case.target.read_assets(project["scope"], project["retained_assets"])
        retained.append((project["scope"], project["retained_assets"], baseline))
    boundary = case.target.max_requests - case.target.cleanup_reserve
    assert case.target.calls <= boundary
    case.target.calls = boundary
    for project, (_, _, baseline) in zip(case.manifest["projects"], retained, strict=True):
        case.target.cleanup_owned(project["scope"], project["owned_assets"])
        assert case.target.read_assets(project["scope"], project["owned_assets"]) == {}
        assert case.target.read_assets(project["scope"], project["retained_assets"]) == baseline
    case.target.client.close()
    case.target.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        "target_ref": case.manifest["target_ref"], "qualification_run_id": case.target.run_id,
        "observation": case.manifest["release"]})))
    assert case.target.read_platform(case.manifest["target_ref"]) == case.manifest["release"]
    for scope, refs, baseline in retained:
        assert case.target.read_assets(scope, refs) == baseline
    assert case.target.calls == 97 < case.target.max_requests == 100
    assert case.target.cleanup_reserve == 3
    assert all(state["deletes"] == 1 for state in case.states.values())


def test_cleanup_reserve_releases_only_the_completed_caller_step(assets_case):
    case = assets_case
    first, second = case.manifest["projects"]
    for project in (first, second):
        case.target.read_assets(project["scope"], project["retained_assets"])
    case.target.calls = case.target.max_requests - case.target.cleanup_reserve
    case.target.cleanup_owned(first["scope"], first["owned_assets"])
    assert case.target.cleanup_reserve == 41
    before = len(case.calls)
    with pytest.raises(ValueError, match="bounds"):
        case.target.read_assets(first["scope"], first["retained_assets"])
    assert len(case.calls) == before
    assert case.target.cleanup_reserve == 41
    case.target.read_assets(first["scope"], first["owned_assets"])
    assert case.target.cleanup_reserve == 37
    case.target.read_assets(first["scope"], first["retained_assets"])
    assert case.target.cleanup_reserve == 33
    before = len(case.calls)
    with pytest.raises(ValueError, match="bounds"):
        case.target.read_assets(first["scope"], first["retained_assets"])
    assert len(case.calls) == before
    assert case.target.cleanup_reserve == 33
    case.target.cleanup_owned(second["scope"], second["owned_assets"])
    assert case.target.cleanup_reserve == 20
    case.target.read_assets(second["scope"], second["owned_assets"])
    assert case.target.cleanup_reserve == 16
    case.target.read_assets(second["scope"], second["retained_assets"])
    assert case.target.cleanup_reserve == 12
    case.target.read_assets(first["scope"], first["retained_assets"])
    assert case.target.cleanup_reserve == 8
    case.target.read_assets(second["scope"], second["retained_assets"])
    assert case.target.cleanup_reserve == 4


def test_failed_caller_read_does_not_release_its_reservation(assets_case, monkeypatch):
    case = assets_case
    project = case.manifest["projects"][0]
    case.target.read_assets(project["scope"], project["retained_assets"])
    case.target.calls = case.target.max_requests - case.target.cleanup_reserve
    case.target.cleanup_owned(project["scope"], project["owned_assets"])
    pending = deepcopy(case.target.asset_readers[0].pending_reads)
    reserve = case.target.cleanup_reserve

    def unavailable(*args, **kwargs):
        raise ValueError("readback unavailable")

    monkeypatch.setattr(installed_portability_assets, "invoke_routes", unavailable)
    with pytest.raises(ValueError, match="unavailable"):
        case.target.read_assets(project["scope"], project["owned_assets"])
    assert case.target.asset_readers[0].pending_reads == pending
    assert case.target.cleanup_reserve == reserve


def test_selector_executes_installed_asset_paths_with_other_channels_mocked(assets_case):
    case = assets_case
    platform = FakePlatform(case.manifest)
    reader = FakeReader(platform)
    http_calls = []

    def transport(request):
        channel = request.url.host.split(".")[0]
        selected = json.loads(request.content if request.method == "POST" else request.url.params["selection"])
        http_calls.append(channel)
        scope = selected.get("scope")
        actions = {
            "action-configure": lambda: (platform.configure(selected["project"]) or {"scope": selected["project"]["scope"]}),
            "action-run-operation": lambda: platform.run_operation(scope, selected["capability"], selected["limits"]),
            "action-run-context": lambda: platform.run_context(scope, selected["result_ref"], selected["limits"]),
            "action-probe-foreign": lambda: platform.probe_foreign(scope, selected["foreign_scope"]),
            "readback-platform": lambda: reader.read_platform(selected["target_ref"]),
            "readback-product": lambda: reader.read_product(scope),
            "readback-configuration": lambda: reader.read_configuration(scope),
            "readback-access": lambda: reader.read_access(scope),
            "readback-native": lambda: reader.read_native(scope, selected["run_id"]),
            "readback-context": lambda: reader.read_context(scope, selected["run_id"]),
            "readback-foreign-denial": lambda: reader.read_foreign_denial(scope, selected["foreign_scope"]),
            "readback-usage": lambda: reader.read_usage(scope),
        }
        return httpx.Response(404 if channel == "action-probe-foreign" else 200, json={
            "target_ref": case.manifest["target_ref"], "qualification_run_id": case.target.run_id,
            "observation": actions[channel]()})

    case.target.client.close()
    case.target.client = httpx.Client(transport=httpx.MockTransport(transport))
    assert len(verify_portability(case.manifest, case.target, case.target)) == 2
    assert case.target.calls == len(case.calls) + len(http_calls) == 92
    assert all(state["deletes"] == 1 for state in case.states.values())
    assert not {"action-cleanup-owned", "readback-assets"} & set(http_calls)


@pytest.mark.parametrize("change", [
    lambda observed: observed.update(scope={"product_id": "foreign"}),
    lambda observed: observed.update(qualification_run_id="foreign"),
    lambda observed: observed.update(api_id="foreign"),
    lambda observed: observed.update(requests=0),
    lambda observed: observed.update(routes={"owned": {"RouteId": "foreign", "RouteKey": "GET /owned"}}),
    lambda observed: observed.update(deployments={"$default": ""}),
    lambda observed: observed.update(stage_routes={"$default": {}}),
])
def test_invalid_provider_envelope_cannot_become_ownership_evidence(assets_case, monkeypatch, change):
    case = assets_case
    invoke = installed_portability_assets.invoke_routes

    def tampered(*args, **kwargs):
        observed = invoke(*args, **kwargs)
        change(observed)
        return observed

    monkeypatch.setattr(installed_portability_assets, "invoke_routes", tampered)
    project = case.manifest["projects"][0]
    with pytest.raises(ValueError, match="foreign, incomplete"):
        case.target.read_assets(project["scope"], project["owned_assets"])
    assert case.target.asset_readers[0].baseline is None
    assert case.target.calls == case.target.max_requests - case.target.cleanup_reserve
    assert not any(name == "delete_route" for _, name, _ in case.calls)


@pytest.mark.parametrize("stages", [["$default"], ["$default", "live"]])
def test_every_serving_stage_is_independently_verified(assets_case, stages):
    case = assets_case
    case.states["example1"]["stages"] = stages
    project = case.manifest["projects"][0]
    scope = project["scope"]
    case.target.read_assets(scope, project["owned_assets"])
    case.target.cleanup_owned(scope, project["owned_assets"])
    observed = case.target.asset_readers[0]
    assert observed.cleaned
    assert case.invocations[-1][0] == case.entries[0]["reader"]
    for stage in stages:
        assert any(role == "Reader1" and name == "export_api" and args["StageName"] == stage
                   for role, name, args in case.calls)


@pytest.mark.parametrize("change", [
    lambda case: case.entries[0].update(scope=case.entries[1]["scope"]),
    lambda case: case.entries[0]["actor"].update(role_arn=case.entries[0]["reader"]["role_arn"]),
    lambda case: case.entries[0]["reader"].update(account_id="000000000009"),
    lambda case: case.manifest["projects"][0].update(owned_assets=["foreign:owned"]),
    lambda case: case.manifest["projects"][1].update(cleanup_receipt=case.manifest["projects"][0]["cleanup_receipt"]),
    lambda case: case.entries[1]["actor"].update(connection_label=case.entries[0]["actor"]["connection_label"]),
    lambda case: case.entries[0].update(api_id="https://example.org"),
    lambda case: case.manifest["portability"]["limits"].update(max_requests=40),
])
def test_invalid_selection_is_rejected_without_provider_calls(assets_case, change):
    change(assets_case)
    with pytest.raises(ValueError):
        validate_installed_assets(assets_case.manifest)
    assert not assets_case.calls


@pytest.mark.parametrize("fault", ["wrong_role", "foreign_owner", "foreign_run", "managed", "missing_asset"])
def test_provider_authority_and_inventory_fail_closed(assets_case, fault):
    case = assets_case
    state = case.states["example1"]
    if fault == "wrong_role":
        state["wrong_role"] = True
    elif fault in {"foreign_owner", "foreign_run"}:
        field = "product_id" if fault == "foreign_owner" else "qualification-run"
        state["api"]["Tags"]["adp:" + field] = "foreign"
    elif fault == "managed":
        state["api"]["ApiGatewayManaged"] = True
    else:
        state["routes"].pop("retained")
    project = case.manifest["projects"][0]
    with pytest.raises(ValueError):
        case.target.read_assets(project["scope"], project["owned_assets"])
    assert not any(name == "delete_route" for _, name, _ in case.calls)


@pytest.mark.parametrize("fault", ["changed_before_delete", "revoked_owner", "lost_ack", "tamper_reader"])
def test_cleanup_retains_uncertainty_and_never_deletes_unowned_routes(assets_case, fault):
    case = assets_case
    project, state = case.manifest["projects"][0], case.states["example1"]
    case.target.read_assets(project["scope"], project["owned_assets"])
    if fault == "revoked_owner":
        state["api"]["Tags"]["adp:product_id"] = "foreign"
    else:
        state[fault] = True
    with pytest.raises((ValueError, TimeoutError)):
        case.target.cleanup_owned(project["scope"], project["owned_assets"])
    assert not case.target.asset_readers[0].cleaned
    assert {"retained", "other"} <= set(state["routes"])
    journal = case.directory / f"devops-{case.target.run_id}-portability.jsonl"
    rows = [json.loads(row) for row in journal.read_text().splitlines()]
    assert rows[-1]["phase"] == "uncertain"
    assert state["deletes"] == (1 if fault in {"lost_ack", "tamper_reader"} else 0)


def test_foreign_scope_and_unobserved_cleanup_have_zero_effects(assets_case):
    case = assets_case
    project = case.manifest["projects"][0]
    with pytest.raises(ValueError, match="foreign"):
        case.target.read_assets({**project["scope"], "product_id": "foreign"}, project["owned_assets"])
    with pytest.raises(ValueError, match="baseline"):
        case.target.cleanup_owned(project["scope"], project["owned_assets"])
    assert not case.calls


@pytest.mark.parametrize("fault", ["deadline", "expired", "requests"])
def test_read_bounds_preserve_cleanup_capacity_without_provider_calls(assets_case, fault):
    case = assets_case
    if fault == "deadline":
        case.target.deadline = time.monotonic() - 1
    elif fault == "expired":
        case.manifest["authorization"]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    else:
        case.target.calls = case.target.max_requests - case.target.cleanup_reserve
    project = case.manifest["projects"][0]
    with pytest.raises(ValueError, match="bounds"):
        case.target.read_assets(project["scope"], project["owned_assets"])
    assert not case.calls


def test_another_products_cleanup_phase_does_not_spend_reserved_capacity(assets_case):
    case = assets_case
    case.target.cleanup_phase = True
    case.target.calls = case.target.max_requests - case.target.cleanup_reserve
    project = case.manifest["projects"][1]
    with pytest.raises(ValueError, match="bounds"):
        case.target.read_assets(project["scope"], project["owned_assets"])
    assert not case.calls


def test_cleanup_grace_does_not_extend_authorization_expiry(assets_case):
    case = assets_case
    project = case.manifest["projects"][0]
    case.target.read_assets(project["scope"], project["owned_assets"])
    before = len(case.calls)
    case.target.deadline = time.monotonic() - 1
    case.manifest["authorization"]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="bounds"):
        case.target.cleanup_owned(project["scope"], project["owned_assets"])
    assert len(case.calls) == before


def test_cleanup_after_deadline_uses_only_the_existing_grace(assets_case):
    case = assets_case
    project = case.manifest["projects"][0]
    case.target.read_assets(project["scope"], project["owned_assets"])
    case.target.deadline = time.monotonic() - 1
    case.target.cleanup_owned(project["scope"], project["owned_assets"])
    assert case.target.asset_readers[0].cleaned
