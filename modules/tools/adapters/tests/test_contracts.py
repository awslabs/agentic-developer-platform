import copy

import pytest
from adp_tools.adapter_contracts import Admission, AuthorizeRequest, DispatchReceipt, ObserveRequest, contract_metadata
from pydantic import ValidationError


@pytest.fixture
def admission(binding):
    return {
        "schema_version": "1.0",
        "admission_id": "admission-1",
        "binding": binding,
        "purpose": "tool_call",
        "connector": "atlassian",
        "operation": "atlassian___search",
        "identity": {"workload": "workload-1", "provider": "provider-1", "canonical_user_id": "user-1"},
        "context": {
            "tenant_id": "tenant-1",
            "canonical_user_id": "user-1",
            "connection_id": "connection-1",
            "binding_revision": "revision-1",
            "site_id": "site-1",
            "scopes": ["read:jira-work"],
        },
        "issued_at_ms": 100_000,
        "expires_at_ms": 119_000,
        "deadline_at_ms": 120_000,
    }


def test_round_trip_retains_all_owner_release_and_deadline_bindings(admission):
    value = Admission.model_validate(admission)
    assert Admission.model_validate_json(value.model_dump_json()) == value
    assert value.binding.release.group_generation == 1
    assert value.identity.canonical_user_id == value.context.canonical_user_id == "user-1"


@pytest.mark.parametrize(
    "change",
    [
        {"expires_at_ms": 120_001},
        {"deadline_at_ms": 118_000},
        {"expires_at_ms": 100_000},
        {"issued_at_ms": True},
        {"expires_at_ms": 119_000.0},
    ],
)
def test_expired_extended_or_coerced_authority_is_invalid(admission, change):
    with pytest.raises(ValidationError):
        Admission.model_validate({**admission, **change})


def test_cross_owner_or_unbound_tool_admission_refuses(admission):
    changed = copy.deepcopy(admission)
    changed["identity"]["canonical_user_id"] = "another-user"
    with pytest.raises(ValidationError):
        Admission.model_validate(changed)
    for name in ("connection_id", "binding_revision"):
        changed = copy.deepcopy(admission)
        changed["context"].pop(name)
        with pytest.raises(ValidationError):
            Admission.model_validate(changed)


def test_observation_does_not_require_connection_that_it_creates(admission):
    admission["purpose"] = "connection_observe"
    admission["context"].pop("connection_id")
    admission["context"].pop("binding_revision")
    assert Admission.model_validate(admission).context.connection_id is None


@pytest.mark.parametrize("name", ["access_token", "refresh_token", "client_secret", "token"])
def test_token_fields_cannot_enter_wire_state(admission, name):
    for location in (admission, admission["identity"], admission["context"], admission["binding"]):
        location[name] = "never-a-wire-field"
        with pytest.raises(ValidationError):
            Admission.model_validate(admission)
        location.pop(name)


def test_observation_ticket_cannot_be_retyped_as_tool_invocation(binding):
    request = {
        "schema_version": "1.0",
        "purpose": "connection_observe",
        "binding": binding,
        "connector": "atlassian",
        "attempt_id": "attempt-1",
        "config_revision": "d" * 64,
        "signed_assertion": "signed-control-proof",
        "deadline_at_ms": 120_000,
    }
    assert ObserveRequest.model_validate(request).purpose == "connection_observe"
    with pytest.raises(ValidationError):
        ObserveRequest.model_validate({**request, "purpose": "tool_call"})
    with pytest.raises(ValidationError):
        AuthorizeRequest.model_validate(request)


def test_consumption_after_deadline_is_not_dispatch_permission(binding):
    with pytest.raises(ValidationError):
        DispatchReceipt.model_validate(
            {
                "schema_version": "1.0",
                "admission_id": "a-1",
                "binding": binding,
                "binding_revision": "r-1",
                "consumed_at_ms": 120_000,
                "deadline_at_ms": 120_000,
            }
        )


def test_contract_metadata_is_stable_and_has_no_deployment_values():
    assert contract_metadata() == contract_metadata()
    assert set(contract_metadata()) == {"contract_version", "schema_sha256"}
    assert len(contract_metadata()["schema_sha256"]) == 64


def test_legacy_admission_requires_exact_provider_metadata(admission):
    payload = copy.deepcopy(admission)
    payload.update(connector="github", operation="get_user")
    with pytest.raises(ValidationError):
        Admission.model_validate(payload)
    payload["context"]["legacy"] = {
        "provider_account_id": "17",
        "observed_scopes": ["read:user"],
        "resources_sha256": None,
        "app_id": None,
        "site_scopes": [],
    }
    value = Admission.model_validate(payload)
    assert value.context.legacy.provider_account_id == "17"
    assert Admission.model_validate_json(value.model_dump_json()) == value
    payload["context"]["legacy"]["client_secret"] = "unapproved"
    with pytest.raises(ValidationError):
        Admission.model_validate(payload)
    payload["context"]["legacy"].pop("client_secret")
    payload["connector"] = "atlassian"
    with pytest.raises(ValidationError):
        Admission.model_validate(payload)
