"""Installed binding and native knowledge observations, with external boundaries mocked."""

import hashlib
import json
import subprocess
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest
import test_context_selector as context_fixtures
import test_selector as selector_fixtures
from botocore.session import Session
from botocore.validate import validate_parameters

from context_selector import HTTPSScopedTarget, authorize_channels, qualify_context
from installed_asset import read_provider_asset
from provider_asset import read_asset
from test_context_selector import FakeReleaseReader
from test_selector import offline_policy


selector_case = selector_fixtures.selector_case
live_context = context_fixtures.live_context
CONTENT = b"installed native knowledge example"


def archive(content=CONTENT, path="SKILL.md", *, extra=False):
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as packed:
        packed.writestr(path, content)
        if extra:
            packed.writestr("unexpected.txt", b"unexpected")
    return output.getvalue()


@pytest.fixture
def installed_asset(live_context, monkeypatch):
    manifest, path, directory, prior_reader, state, driver, reader = live_context
    manifest["authorization"]["expires_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    context = manifest["context"]
    context["scope"].update({key: manifest["journey_driver"]["scope"][key] for key in ("product_id", "project_id")})
    source = context["sources"][0]
    source.update(tenant_id=context["scope"]["tenant_id"], project_id=context["scope"]["project_id"],
                  source_connection_id="documents-example", record_type="document",
                  sha256=hashlib.sha256(CONTENT).hexdigest())
    selected = {"token_env": "DEVOPS_ASSET_READ_TOKEN", "agent_space_id": "space-example",
                "binding_revision": 1, "kind": "skill", "publication_scope": "agent", "visibility": "product_team",
                "native_metadata": {"name": "source-runbook", "description": "Selected source guidance",
                                    "agent_types": ["CHAT"]}}
    context["installed_asset"] = selected
    context["readback_channels"].pop("asset")
    monkeypatch.delenv("DEVOPS_CONTEXT_ASSET_TOKEN")
    monkeypatch.setenv("DEVOPS_ASSET_READ_TOKEN", "independent-product-asset-read")
    provider = manifest["journey_driver"]["provider_readback"]
    source_ref = {key: value for key, value in source.items() if key != "sha256"}
    binding = {"binding_id": "example", "scope": {**context["scope"], "relationship_revision": "1",
               "owner_id": "owner-example", "visibility": "product_team"}, "state": "ready", "revision": 1,
               "provider_identity": {key: provider[key] for key in ("account_id", "role_arn", "region")},
               "native_resource_ref": {"tenant_id": context["scope"]["tenant_id"],
                   "project_id": context["scope"]["project_id"], "record_type": "agent_space",
                   "source_connection_id": provider["source_connection_id"], "record_id": "space-example",
                   "record_revision": "1"}}
    asset = {"assetId": context["asset_ref"], "assetType": "skill", "version": 3,
             "createdAt": datetime.now(UTC), "updatedAt": datetime.now(UTC),
             "metadata": {"adp_source_ref": source_ref, "adp_source_hash": source["sha256"],
                          "adp_mapping_version": 2, "adp_scope": "agent", "adp_visibility": "product_team",
                          "adp_active": True, **selected["native_metadata"], "status": "ACTIVE", "skill_type": "USER"}}
    payload = {"content": {"zipFile": archive()}, "version": 3}
    calls = []
    identity = {"Account": provider["account_id"],
                "Arn": f"arn:aws:sts::{provider['account_id']}:assumed-role/example-readonly/example"}
    model = Session().get_service_model("devops-agent")

    class Provider:
        def get_caller_identity(self):
            calls.append("identity")
            return identity

        def get_asset(self, **kwargs):
            validate_parameters(kwargs, model.operation_model("GetAsset").input_shape)
            assert kwargs == {"agentSpaceId": "space-example", "assetId": context["asset_ref"]}
            calls.append("asset")
            return {"asset": deepcopy(asset)}

        def get_asset_content(self, **kwargs):
            validate_parameters(kwargs, model.operation_model("GetAssetContent").input_shape)
            assert kwargs == {"agentSpaceId": "space-example", "assetId": context["asset_ref"], "assetVersion": 3}
            calls.append("content")
            return deepcopy(payload)

    def client_factory(service, **kwargs):
        assert service in {"sts", "devops-agent"} and kwargs["region_name"] == provider["region"]
        assert kwargs["config"].retries["total_max_attempts"] == 1
        return Provider()

    def native(selection, space_id, asset_id, *, deadline):
        assert selection == provider and deadline > time.monotonic()
        return read_asset(selection["account_id"], selection["role_arn"], selection["region"], space_id, asset_id,
                          client_factory=client_factory)

    def transport(request):
        calls.append("binding")
        assert request.method == "GET" and request.url.host == "gateway.example.org"
        assert request.url.path == "/api/v1/products/example/projects/example/services/devops/bindings/example"
        assert request.headers["Authorization"] == "Bearer independent-product-asset-read"
        return httpx.Response(200, json=binding)

    original_client = httpx.Client
    monkeypatch.setattr("httpx.Client", lambda **kwargs: original_client(transport=httpx.MockTransport(transport), **kwargs))
    monkeypatch.setattr("installed_asset.read_provider_asset", native)
    path.write_text(json.dumps(manifest))
    return SimpleNamespace(manifest=manifest, path=path, directory=directory, prior=prior_reader,
                           state=state, driver=driver, reader=reader, asset=asset, binding=binding,
                           payload=payload, calls=calls, identity=identity, client_factory=client_factory)


def read_selected(case, **kwargs):
    target = HTTPSScopedTarget(case.manifest, case.directory, "context",
                               deadline=kwargs.get("deadline", time.monotonic() + 60),
                               max_requests=kwargs.get("max_requests", 25))
    try:
        result = target.read_asset(kwargs.get("scope", case.manifest["context"]["scope"]),
                                   kwargs.get("asset_ref", case.manifest["context"]["asset_ref"]))
        assert target.calls == 4
        return result
    finally:
        target.close()


def test_installed_asset_reads_current_binding_and_exact_native_content(installed_asset):
    case = installed_asset
    authorize_channels(case.manifest, case.manifest["release"]["source_sha"], case.directory, "context")
    observed = read_selected(case)
    assert case.calls == ["binding", "identity", "asset", "content"]
    assert observed["source_refs"] == case.manifest["context"]["sources"]
    assert observed["readback"]["asset"] == {key: case.asset[key] for key in ("assetId", "assetType", "version", "metadata")}
    assert observed["readback"]["binding"] == case.binding
    assert observed["status"] == "active" and observed["version"] == 3
    model = Session().get_service_model("devops-agent")
    validate_parameters({"asset": case.asset}, model.operation_model("GetAsset").output_shape)
    validate_parameters(case.payload, model.operation_model("GetAssetContent").output_shape)


@pytest.mark.parametrize("change", [
    {"visibility": "only_me"}, {"kind": "context"}, {"native_metadata": {}},
])
def test_installed_asset_rejects_unqualified_native_selection_before_calls(installed_asset, change):
    installed_asset.manifest["context"]["installed_asset"].update(change)
    with pytest.raises(ValueError):
        read_selected(installed_asset)
    assert installed_asset.calls == []


@pytest.mark.parametrize("mutate", [
    lambda case: case.binding["scope"].update(tenant_id="foreign"),
    lambda case: case.binding["scope"].update(product_id="foreign"),
    lambda case: case.binding["scope"].update(project_id="foreign"),
    lambda case: case.binding["scope"].update(visibility="only_me"),
    lambda case: case.binding.update(binding_id="foreign"),
    lambda case: case.binding.update(revision=2),
    lambda case: case.binding.update(state="revoked"),
    lambda case: case.binding["provider_identity"].update(role_arn="foreign"),
    lambda case: case.binding["native_resource_ref"].update(record_id="foreign"),
    lambda case: case.binding["native_resource_ref"].update(source_connection_id="foreign"),
    lambda case: case.binding["native_resource_ref"].update(tenant_id="foreign"),
])
def test_installed_asset_denies_invalid_binding_before_native_calls(installed_asset, mutate):
    mutate(installed_asset)
    with pytest.raises(ValueError, match="binding"):
        read_selected(installed_asset)
    assert installed_asset.calls == ["binding"]


@pytest.mark.parametrize("mutate", [
    lambda case: case.asset.update(assetId="foreign"),
    lambda case: case.asset.update(version=True),
    lambda case: case.asset.update(assetType="context"),
    lambda case: case.asset["metadata"]["adp_source_ref"].update(project_id="foreign"),
    lambda case: case.asset["metadata"]["adp_source_ref"].update(record_revision="old"),
    lambda case: case.asset["metadata"].update(adp_source_hash="0" * 64),
    lambda case: case.asset["metadata"].update(adp_active=False),
    lambda case: case.asset["metadata"].update(status="INACTIVE"),
    lambda case: case.asset["metadata"].update(agent_types=["GENERIC"]),
    lambda case: case.asset["metadata"].update(adp_scope="global"),
    lambda case: case.asset["metadata"].update(adp_visibility="only_me"),
    lambda case: case.asset["metadata"].update(adp_mapping_version=1),
    lambda case: case.payload.update(version=2),
    lambda case: case.payload["content"].update(zipFile=b"not-a-zip"),
    lambda case: case.payload["content"].update(zipFile=archive(b"changed")),
    lambda case: case.payload["content"].update(zipFile=archive(path="../SOURCE.md")),
    lambda case: case.payload["content"].update(zipFile=archive(extra=True)),
    lambda case: case.payload["content"].update(zipFile=archive(b"a" * (1024 * 1024 + 1))),
])
def test_installed_asset_rejects_wrong_native_scope_version_and_content(installed_asset, mutate):
    mutate(installed_asset)
    with pytest.raises(ValueError, match="native"):
        read_selected(installed_asset)


@pytest.mark.parametrize("field,value", [("Account", "000000000000"), ("Arn", "arn:aws:sts::123456789012:assumed-role/wrong/example")])
def test_installed_asset_rejects_wrong_selected_aws_identity(installed_asset, field, value):
    installed_asset.identity[field] = value
    with pytest.raises(ValueError, match="identity"):
        read_selected(installed_asset)
    assert installed_asset.calls == ["binding", "identity"]


@pytest.mark.parametrize("kwargs", [
    {"max_requests": 3}, {"deadline": 0}, {"asset_ref": "foreign"},
    {"scope": {"tenant_id": "foreign", "product_id": "example", "project_id": "example"}},
])
def test_installed_asset_bounds_and_scope_precede_external_calls(installed_asset, kwargs):
    with pytest.raises(ValueError):
        read_selected(installed_asset, **kwargs)
    assert installed_asset.calls == []


@pytest.mark.parametrize("token", ["DEVOPS_PRODUCT_TOKEN", "DEVOPS_MCP_TOKEN", "DEVOPS_CONTEXT_RUN_TOKEN",
                                  "DEVOPS_CONTEXT_SOURCE_TOKEN", "DEVOPS_RELEASE_TOKEN"])
def test_installed_asset_rejects_shared_credentials(installed_asset, monkeypatch, token):
    installed_asset.manifest["context"]["installed_asset"]["token_env"] = token
    with pytest.raises(ValueError, match="independent"):
        authorize_channels(installed_asset.manifest, installed_asset.manifest["release"]["source_sha"],
                           installed_asset.directory, "context")
    assert installed_asset.calls == []


def test_installed_asset_cannot_fall_back_to_a_custom_channel(installed_asset):
    case = installed_asset
    case.manifest["context"]["readback_channels"]["asset"] = {
        "source": "asset", "url": "https://custom.example.org", "token_env": "DEVOPS_ASSET_READ_TOKEN"}
    with pytest.raises(ValueError, match="bounded actions"):
        authorize_channels(case.manifest, case.manifest["release"]["source_sha"], case.directory, "context")


def test_provider_asset_transport_selects_broker_and_retains_no_credentials(installed_asset, monkeypatch):
    selected = installed_asset.manifest["journey_driver"]["provider_readback"]
    calls = []

    def process(command, **kwargs):
        calls.append(command)
        assert kwargs["capture_output"] and 0 < kwargs["timeout"] <= 30
        return SimpleNamespace(returncode=0, stdout=b'{"asset": {}}')

    monkeypatch.setattr("installed_asset.subprocess.run", process)
    assert read_provider_asset(selected, "space-example", "asset-example", deadline=time.monotonic() + 60) == {"asset": {}}
    assert calls[0][:8] == ["adp-cred", "assume", "--service", "aws", "--label", "example", "--exec", calls[0][7]]
    assert calls[0][-5:] == [selected["account_id"], selected["role_arn"], selected["region"], "space-example", "asset-example"]


@pytest.mark.parametrize("failure", ["timeout", "exit", "oversize", "invalid-json"])
def test_provider_asset_transport_fails_closed(installed_asset, monkeypatch, failure):
    def process(command, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 1)
        return SimpleNamespace(returncode=1 if failure == "exit" else 0,
                               stdout=b"a" * 65537 if failure == "oversize" else b"invalid")

    monkeypatch.setattr("installed_asset.subprocess.run", process)
    with pytest.raises(ValueError, match="unavailable"):
        read_provider_asset(installed_asset.manifest["journey_driver"]["provider_readback"],
                            "space-example", "asset-example", deadline=time.monotonic() + 60)


def test_context_selector_records_installed_asset_without_asset_server(installed_asset):
    case = installed_asset
    context = case.manifest["context"]
    for observed in case.state.values():
        if "scope" in observed:
            observed["scope"] = context["scope"]
        if "source_refs" in observed:
            observed["source_refs"] = context["sources"]
    case.state["source"]["content"] = CONTENT
    case.state["later"]["citations"][0]["source_refs"] = context["sources"]

    class Target(HTTPSScopedTarget):
        def _request(self, channel, selection, **kwargs):
            if channel == "run_later":
                return case.driver.run_later(context["scope"], context["intent"], context["publication_ref"], context["limits"])
            if channel == "source":
                import base64
                return {**case.state["source"], "content_b64": base64.b64encode(CONTENT).decode()}
            if channel == "denial":
                return case.state["denials"][selection["case"]]
            return case.state[{"native_job": "native"}.get(channel, channel)]

    run_id = qualify_context(case.manifest["release"]["source_sha"], case.path, case.directory,
                            target_factory=Target, policy_reader=offline_policy,
                            release_reader_factory=lambda manifest, **kwargs: FakeReleaseReader(manifest, case.prior, **kwargs))
    evidence = json.loads((case.directory / f"devops-{run_id}-context.json").read_text())
    assert evidence["citation"]["source_refs"] == context["sources"]
    assert case.calls == ["binding", "identity", "asset", "content"]
    assert (case.directory / "context.json").exists()
