"""Exercise installed two-Product access with HTTP and native SDK boundaries mocked."""

import json
import subprocess
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from botocore.session import Session
from botocore.validate import validate_parameters

from context_selector import HTTPSScopedTarget, authorize_channels
from installed_portability import read_provider_access, validate_installed_access
from provider_access import read_access
import test_portability_selector as portability_fixtures
import test_selector as selector_fixtures

portable_selection = portability_fixtures.portable_selection
selector_case = selector_fixtures.selector_case


@pytest.fixture
def installed_access(portable_selection, monkeypatch):
    manifest, path, directory, prior = portable_selection
    manifest["authorization"]["expires_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    manifest["portability"]["readback_channels"].pop("access")
    entries, bindings, identities = [], {}, {}
    for index, project in enumerate(manifest["projects"], start=1):
        account = "123456789012" if index == 1 else "234567890123"
        role = f"arn:aws:iam::{account}:role/example-{index}"
        project.update(account_ref=account, role_ref=role)
        token_env = f"DEVOPS_PORTABLE_PRODUCT_{index}"
        monkeypatch.setenv(token_env, f"independent-portable-reader-{index}")
        provider = {"account_id": account, "role_arn": role, "region": "us-east-1",
                    "connection_label": f"example-{index}", "source_connection_id": f"connection-{index}"}
        entry = {"scope": project["scope"], "binding_id": f"binding-{index}", "binding_revision": 3,
                 "agent_space_id": f"space-{index}", "product_token_env": token_env, "provider": provider}
        entries.append(entry)
        bindings[entry["binding_id"]] = {
            "binding_id": entry["binding_id"], "revision": 3, "generation": 2, "state": "ready", "gaps": [],
            "scope": {**project["scope"], "owner_id": f"owner-{index}", "visibility": "product_team"},
            "provider_identity": {key: provider[key] for key in ("account_id", "role_arn", "region")},
            "native_resource_ref": {"record_type": "agent_space", "record_id": entry["agent_space_id"],
                "record_revision": "1", "tenant_id": project["scope"]["tenant_id"],
                "project_id": project["scope"]["project_id"], "source_connection_id": provider["source_connection_id"]}}
        identities[account] = {"Account": account, "Arn": f"arn:aws:sts::{account}:assumed-role/example-{index}/test"}
    manifest["portability"]["installed_access"] = {"projects": entries}
    calls = []
    state = {"status": 200, "after_native": None, "space": None, "body": None}
    model = Session().get_service_model("devops-agent")

    def transport(request):
        assert request.method == "GET" and request.url.host == "gateway.example.org"
        entry = next(entry for entry in entries if request.url.path.endswith("/" + entry["binding_id"]))
        scope = entry["scope"]
        assert request.url.path == (f"/api/v1/products/{scope['product_id']}/projects/{scope['project_id']}"
                                    f"/services/devops/bindings/{entry['binding_id']}")
        assert request.headers["Authorization"] == f"Bearer independent-portable-reader-{entries.index(entry) + 1}"
        calls.append(("binding", entry["binding_id"]))
        return httpx.Response(state["status"], content=state["body"]) if state["body"] is not None else httpx.Response(
            state["status"], json=deepcopy(bindings[entry["binding_id"]]))

    def native(provider, space_id, *, deadline):
        assert deadline > time.monotonic()

        class Provider:
            def get_caller_identity(self):
                calls.append(("identity", provider["account_id"]))
                return deepcopy(identities[provider["account_id"]])

            def get_agent_space(self, **kwargs):
                validate_parameters(kwargs, model.operation_model("GetAgentSpace").input_shape)
                assert kwargs == {"agentSpaceId": space_id}
                calls.append(("space", space_id))
                if state["after_native"]:
                    state["after_native"]()
                return {"agentSpace": {"agentSpaceId": state["space"] or space_id}}

        def client_factory(service, **kwargs):
            assert service in {"sts", "devops-agent"} and kwargs["region_name"] == provider["region"]
            assert kwargs["config"].retries["total_max_attempts"] == 1
            return Provider()

        return read_access(provider["account_id"], provider["role_arn"], provider["region"], space_id,
                           client_factory=client_factory)

    original = httpx.Client
    monkeypatch.setattr("httpx.Client", lambda **kwargs: original(transport=httpx.MockTransport(transport), **kwargs))
    monkeypatch.setattr("installed_portability.read_provider_access", native)
    path.write_text(json.dumps(manifest))
    return SimpleNamespace(manifest=manifest, path=path, directory=directory, entries=entries,
                           bindings=bindings, identities=identities, state=state, calls=calls)


def target_for(case, **kwargs):
    return HTTPSScopedTarget(case.manifest, case.directory, "portability",
                             deadline=kwargs.get("deadline", time.monotonic() + 60),
                             max_requests=kwargs.get("max_requests", 60))


def test_two_product_access_uses_installed_binding_and_native_role_without_custom_channel(installed_access):
    case = installed_access
    authorize_channels(case.manifest, case.manifest["release"]["source_sha"], case.directory, "portability")
    target = target_for(case)
    try:
        for entry in case.entries:
            observed = target.read_access(entry["scope"])
            assert observed["scope"] == entry["scope"] and observed["authorized"] is True
            assert observed["account_ref"] == entry["provider"]["account_id"]
            assert observed["role_ref"] == entry["provider"]["role_arn"]
            assert observed["readback"]["binding"] == case.bindings[entry["binding_id"]]
        assert target.calls == 8
        assert [kind for kind, _ in case.calls] == ["binding", "identity", "space", "binding"] * 2
    finally:
        target.close()


@pytest.mark.parametrize("change", [
    lambda binding: binding["scope"].update(tenant_id="foreign"),
    lambda binding: binding["scope"].update(product_id="foreign"),
    lambda binding: binding["scope"].update(project_id="foreign"),
    lambda binding: binding.update(binding_id="foreign"),
    lambda binding: binding.update(revision=4),
    lambda binding: binding.update(state="revoked"),
    lambda binding: binding.update(gaps=["delegation_revoked"]),
    lambda binding: binding["provider_identity"].update(account_id="999999999999"),
    lambda binding: binding["provider_identity"].update(role_arn="arn:aws:iam::123456789012:role/foreign"),
    lambda binding: binding["native_resource_ref"].update(source_connection_id="foreign"),
    lambda binding: binding["native_resource_ref"].update(record_id="foreign"),
])
def test_foreign_or_stale_product_binding_stops_before_native_requests(installed_access, change):
    case = installed_access
    change(case.bindings["binding-1"])
    target = target_for(case)
    try:
        with pytest.raises(ValueError, match="binding"):
            target.read_access(case.entries[0]["scope"])
        assert case.calls == [("binding", "binding-1")]
    finally:
        target.close()


@pytest.mark.parametrize("status", [302, 401, 403, 404, 503])
def test_denied_or_redirected_product_access_stops_before_native_requests(installed_access, status):
    case = installed_access
    case.state["status"] = status
    target = target_for(case)
    try:
        with pytest.raises(ValueError, match="authority unavailable"):
            target.read_access(case.entries[0]["scope"])
        assert case.calls == [("binding", "binding-1")]
    finally:
        target.close()


@pytest.mark.parametrize("field,value", [("Account", "999999999999"),
    ("Arn", "arn:aws:sts::123456789012:assumed-role/foreign/test")])
def test_wrong_selected_native_identity_never_contacts_devops(installed_access, field, value):
    case = installed_access
    case.identities["123456789012"][field] = value
    target = target_for(case)
    try:
        with pytest.raises(ValueError, match="identity"):
            target.read_access(case.entries[0]["scope"])
        assert [kind for kind, _ in case.calls] == ["binding", "identity"]
    finally:
        target.close()


@pytest.mark.parametrize("change", ["revision", "revocation", "native_space"])
def test_native_or_product_changes_during_readback_are_not_accepted(installed_access, change):
    case = installed_access
    if change == "revision":
        case.state["after_native"] = lambda: case.bindings["binding-1"].update(revision=4)
    elif change == "revocation":
        case.state["after_native"] = lambda: case.state.update(status=404)
    else:
        case.state["space"] = "foreign"
    target = target_for(case)
    try:
        with pytest.raises(ValueError):
            target.read_access(case.entries[0]["scope"])
    finally:
        target.close()


@pytest.mark.parametrize("case_name", ["foreign", "expired", "requests", "deadline"])
def test_invalid_scope_or_bounds_have_no_external_effect(installed_access, case_name):
    case = installed_access
    if case_name == "expired":
        case.manifest["authorization"]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    target = target_for(case, max_requests=7 if case_name == "requests" else 60,
                        deadline=time.monotonic() - 1 if case_name == "deadline" else time.monotonic() + 60)
    try:
        scope = {**case.entries[0]["scope"], "product_id": "foreign"} if case_name == "foreign" else case.entries[0]["scope"]
        with pytest.raises(ValueError):
            target.read_access(scope)
        assert case.calls == []
    finally:
        target.close()


def test_oversized_binding_is_rejected_before_provider_access(installed_access):
    case = installed_access
    case.state["body"] = b" " * 65537
    target = target_for(case)
    try:
        with pytest.raises(ValueError, match="oversized"):
            target.read_access(case.entries[0]["scope"])
        assert case.calls == [("binding", "binding-1")]
    finally:
        target.close()


def test_product_read_credentials_cannot_be_reused_between_products_or_actions(installed_access, monkeypatch):
    case = installed_access
    for value in ("independent-portable-reader-2", "private-portability-action-configure-token", "private-product-token"):
        monkeypatch.setenv(case.entries[0]["product_token_env"], value)
        with pytest.raises(ValueError, match="independent Product credentials"):
            validate_installed_access(case.manifest)
    assert case.calls == []


def test_native_readback_uses_only_the_selected_broker_connection(installed_access, monkeypatch):
    entry = installed_access.entries[0]
    calls = []
    def execute(command, **kwargs):
        calls.append(command)
        assert command[:7] == ["adp-cred", "assume", "--service", "aws", "--label", "example-1", "--exec"]
        assert command[-4:] == ["123456789012", "arn:aws:iam::123456789012:role/example-1", "us-east-1", "space-1"]
        assert kwargs["timeout"] <= 20 and kwargs["capture_output"] is True
        return SimpleNamespace(returncode=0, stdout=b'{"requests":2}')
    monkeypatch.setattr("installed_portability.subprocess.run", execute)
    assert read_provider_access(entry["provider"], "space-1", deadline=time.monotonic() + 10) == {"requests": 2}
    assert len(calls) == 1


@pytest.mark.parametrize("result", [SimpleNamespace(returncode=1, stdout=b"private"),
    SimpleNamespace(returncode=0, stdout=b"private"), SimpleNamespace(returncode=0, stdout=b" " * 65537)])
def test_broker_failure_never_discloses_its_output(installed_access, monkeypatch, result):
    monkeypatch.setattr("installed_portability.subprocess.run", lambda *args, **kwargs: result)
    with pytest.raises(ValueError, match="^selected native access readback unavailable$"):
        read_provider_access(installed_access.entries[0]["provider"], "space-1", deadline=time.monotonic() + 10)


def test_broker_timeout_is_bounded_and_not_retried(installed_access, monkeypatch):
    calls = []
    def timeout(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr("installed_portability.subprocess.run", timeout)
    with pytest.raises(ValueError, match="unavailable"):
        read_provider_access(installed_access.entries[0]["provider"], "space-1", deadline=time.monotonic() + 10)
    assert len(calls) == 1
