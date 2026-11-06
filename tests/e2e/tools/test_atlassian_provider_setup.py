"""Generated AgentCore control-plane fixtures; no real registration or consent."""

import json

import pytest

from tests.e2e.tools.atlassian_oauth_probe import IdentityConfig, IdentityLifecycle, ProbeBlocked, CONSENT_SCOPES, RESOURCE
from tests.e2e.tools.atlassian_provider_setup import OWNERSHIP_TAG, ProviderPlan, ProviderSetup
from tests.e2e.tools.test_atlassian_oauth_probe import IdentityFixture, TEST_ISSUER


CALLBACK = "https://bedrock-agentcore.us-east-1.amazonaws.com/identities/oauth2/callback/example"
SECRET = "arn:aws:secretsmanager:us-east-1:123456789012:secret:atlassian-proof-app"
PROTECTED = {"resource": RESOURCE, "authorization_servers": [TEST_ISSUER], "scopes_supported": list(CONSENT_SCOPES)}
AUTH = {
    "issuer": TEST_ISSUER,
    "authorization_endpoint": "https://auth.atlassian.com/oauth/authorize",
    "token_endpoint": "https://auth.atlassian.com/oauth/token",
    "code_challenge_methods_supported": ["S256"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "token_endpoint_auth_methods_supported": ["client_secret_basic"],
}


class MissingResource(Exception):
    response = {"Error": {"Code": "ResourceNotFoundException"}}


class ControlFixture:
    def __init__(self, plan):
        self.plan = plan
        self.workload = None
        self.provider = None
        self.calls = []
        self.lose_workload_reply = False
        self.lose_provider_reply = False
        self.lose_provider_delete_reply = False
        self.lose_workload_delete_reply = False
        self.fail_provider_delete_without_effect = False
        self.tags = {}
        account = plan.secret_id.split(":")[4]
        prefix = f"arn:aws:bedrock-agentcore:{plan.region}:{account}:"
        self.workload_arn = prefix + "workload-identity/atlassian-proof-workload"
        self.provider_arn = prefix + "oauth2-provider/atlassian-proof-mcp"

    def get_workload_identity(self, name):
        if self.workload is None:
            raise MissingResource()
        return self.workload

    def get_oauth2_credential_provider(self, name):
        if self.provider is None:
            raise MissingResource()
        return self.provider

    def create_workload_identity(self, **kwargs):
        self.calls.append(("create_workload", kwargs))
        self.workload = {"name": kwargs["name"], "workloadIdentityArn": self.workload_arn,
                         "allowedResourceOauth2ReturnUrls": kwargs["allowedResourceOauth2ReturnUrls"]}
        self.tags[self.workload_arn] = dict(kwargs["tags"])
        if self.lose_workload_reply:
            raise TimeoutError("response lost")

    def create_oauth2_credential_provider(self, **kwargs):
        config = kwargs["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]
        if "tokenEndpointAuthMethods" in config["oauthDiscovery"]["authorizationServerMetadata"]:
            raise ValueError("AWS rejects deprecated metadata alongside clientAuthenticationMethod")
        self.calls.append(("create_provider", kwargs))
        self.provider = {
            "name": kwargs["name"], "credentialProviderArn": self.provider_arn,
            "credentialProviderVendor": "CustomOauth2", "status": "READY",
            "callbackUrl": CALLBACK, "clientSecretSource": "EXTERNAL", "clientSecretArn": {"secretArn": self.plan.secret_id},
            "clientSecretJsonKey": "client_secret",
            "oauth2ProviderConfigOutput": {"customOauth2ProviderConfig": {
                "oauthDiscovery": config["oauthDiscovery"],
                "clientId": self.plan.client_id, "clientAuthenticationMethod": "CLIENT_SECRET_BASIC",
            }},
        }
        self.tags[self.provider_arn] = dict(kwargs["tags"])
        if self.lose_provider_reply:
            raise TimeoutError("response lost")

    def list_tags_for_resource(self, resourceArn):
        self.calls.append(("list_tags", resourceArn))
        return {"tags": dict(self.tags.get(resourceArn, {}))}

    def delete_oauth2_credential_provider(self, name):
        self.calls.append(("delete_provider", name))
        assert name == self.plan.identity.provider_name
        if self.fail_provider_delete_without_effect:
            self.fail_provider_delete_without_effect = False
            raise TimeoutError("response lost before provider deletion")
        self.provider = None
        if self.lose_provider_delete_reply:
            raise TimeoutError("response lost after provider deletion")

    def delete_workload_identity(self, name):
        self.calls.append(("delete_workload", name))
        assert name == self.plan.identity.workload_name
        self.workload = None
        if self.lose_workload_delete_reply:
            raise TimeoutError("response lost after workload deletion")


def setup_for(tmp_path, protected=None, auth=None, secret_id=SECRET):
    protected = PROTECTED if protected is None else protected
    auth = AUTH if auth is None else auth
    identity = IdentityConfig.from_metadata("atlassian-proof-workload", "atlassian-proof-mcp", "https://adp.example.test/return", protected, auth)
    plan = ProviderPlan(identity, protected, auth, "approved-app-client", secret_id, "us-east-1")
    control = ControlFixture(plan)
    lifecycle = IdentityLifecycle(identity, IdentityFixture("core"), IdentityFixture("adapter"))
    return ProviderSetup(plan, control, lifecycle, tmp_path / "provider-setup.json"), control


def test_prepare_isolated_custom_provider_and_reuse_after_restart(tmp_path):
    setup, control = setup_for(tmp_path)
    first = setup.prepare()
    assert first == {"status": "ready", "mode": "retained-deployment-client", "provider": "atlassian-proof-mcp", "callback_url": CALLBACK}
    assert [call[0] for call in control.calls if call[0].startswith("create_")] == ["create_workload", "create_provider"]
    args = next(value for operation, value in control.calls if operation == "create_provider")
    assert args["credentialProviderVendor"] == "CustomOauth2"
    assert args["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]["clientSecretConfig"] == {"secretId": SECRET, "jsonKey": "client_secret"}
    assert setup.prepare() == first
    assert len([call for call in control.calls if call[0].startswith("create_")]) == 2
    ledger = json.loads(setup.ledger.path.read_text())
    assert set(ledger) == {"config_digest", "setup_digest", "status", "phase", "uncertain", "ownership_nonce", "workload_arn", "provider_arn"}
    assert control.tags[control.workload_arn][OWNERSHIP_TAG] == ledger["ownership_nonce"]
    assert control.tags[control.provider_arn][OWNERSHIP_TAG] == ledger["ownership_nonce"]
    assert "approved-app-client" not in setup.ledger.path.read_text()
    assert SECRET not in setup.ledger.path.read_text()


def test_existing_probe_metadata_and_ledger_remain_recoverable(tmp_path):
    setup, control = setup_for(tmp_path)
    first = setup.prepare()
    original = setup.ledger.path.read_bytes()
    config = control.provider["oauth2ProviderConfigOutput"]["customOauth2ProviderConfig"]
    config["oauthDiscovery"]["authorizationServerMetadata"]["tokenEndpointAuthMethods"] = ["client_secret_basic"]
    assert setup.prepare() == first
    assert setup.ledger.path.read_bytes() == original
    assert setup.cleanup()["status"] == "cleaned"


@pytest.mark.parametrize("field,value", [("clientSecretArn", SECRET), ("clientSecretJsonKey", "bootstrap_secret")])
def test_probe_rejects_wrong_secret_reference_shape_or_key(tmp_path, field, value):
    setup, control = setup_for(tmp_path)
    setup.prepare()
    control.provider[field] = value
    with pytest.raises(ProbeBlocked, match="app secret differs"):
        setup.prepare()


@pytest.mark.parametrize("which", ["lose_workload_reply", "lose_provider_reply"])
def test_lost_create_reply_reads_back_without_duplicate_registration(tmp_path, which):
    setup, control = setup_for(tmp_path)
    setattr(control, which, True)
    with pytest.raises(ProbeBlocked):
        setup.prepare()
    assert setup.prepare()["status"] == "ready"
    assert [call[0] for call in control.calls if call[0].startswith("create_")] == ["create_workload", "create_provider"]


def test_uncertain_missing_provider_never_recreates_without_authoritative_readback(tmp_path):
    setup, control = setup_for(tmp_path)
    control.lose_provider_reply = True
    with pytest.raises(ProbeBlocked):
        setup.prepare()
    control.provider = None
    with pytest.raises(ProbeBlocked, match="lost"):
        setup.prepare()
    assert len([call for call in control.calls if call[0].startswith("create_")]) == 2


def test_existing_unowned_or_drifted_provider_is_not_adopted(tmp_path):
    setup, control = setup_for(tmp_path)
    control.workload = {"name": setup.plan.identity.workload_name, "allowedResourceOauth2ReturnUrls": [setup.plan.identity.return_url]}
    with pytest.raises(ProbeBlocked, match="unowned"):
        setup.prepare()
    control.workload = None
    setup.prepare()
    control.provider["credentialProviderVendor"] = "AtlassianOauth2"
    with pytest.raises(ProbeBlocked, match="differs"):
        setup.prepare()


def test_legacy_or_unsupported_mode_and_secret_configuration_blocks(tmp_path):
    for metadata in [
        {**AUTH, "issuer": "https://auth.atlassian.com/legacy"},
        {**AUTH, "token_endpoint_auth_methods_supported": ["none"]},
        {**AUTH, "authorization_endpoint": "http://auth.atlassian.com/oauth/authorize"},
    ]:
        with pytest.raises((ProbeBlocked, ValueError)):
            setup_for(tmp_path, auth=metadata)
    with pytest.raises(ProbeBlocked):
        setup_for(tmp_path, secret_id="raw-client-secret-not-an-arn")


def test_ready_setup_never_recreates_deleted_workload(tmp_path):
    setup, control = setup_for(tmp_path)
    setup.prepare()
    control.workload = None
    with pytest.raises(ProbeBlocked, match="disappeared"):
        setup.prepare()
    assert [call[0] for call in control.calls if call[0].startswith("create_")] == ["create_workload", "create_provider"]



def test_owned_cleanup_is_provider_first_and_read_back_twice(tmp_path, record_property):
    setup, control = setup_for(tmp_path)
    setup.prepare()
    result = setup.cleanup()
    assert result == {"status": "cleaned", "controlled_cleanup": "verified", "provider_grant_revocation": "unproven"}
    assert [name for name, _ in control.calls if name.startswith("delete_")] == ["delete_provider", "delete_workload"]
    assert control.workload is control.provider is None
    assert setup.cleanup() == result
    assert [name for name, _ in control.calls if name.startswith("delete_")] == ["delete_provider", "delete_workload"]
    record = json.loads(setup.ledger.path.read_text())
    observation = {
        "phase": record["cleanup_phase"],
        "uncertain": record["uncertain"],
        "remaining_resources": [name for name, value in (
            ("provider", control.provider), ("workload", control.workload)
        ) if value is not None],
    }
    assert observation == {"phase": "verified", "uncertain": False, "remaining_resources": []}
    record_property("adp_generated_cleanup", json.dumps(observation, sort_keys=True))
    with pytest.raises(ProbeBlocked, match="cannot restart"):
        setup.prepare()


def test_refuses_unowned_setup_and_foreign_tag_or_replaced_arn(tmp_path):
    setup, control = setup_for(tmp_path)
    with pytest.raises(ProbeBlocked, match="No tagged setup"):
        setup.cleanup()
    setup.prepare()
    control.tags[control.provider_arn][OWNERSHIP_TAG] = "another-attempt"
    with pytest.raises(ProbeBlocked, match="ownership"):
        setup.cleanup()
    assert not any(name.startswith("delete_") for name, _ in control.calls)
    control.tags[control.provider_arn][OWNERSHIP_TAG] = json.loads(setup.ledger.path.read_text())["ownership_nonce"]
    control.provider["credentialProviderArn"] += "-replaced"
    control.tags[control.provider["credentialProviderArn"]] = dict(control.tags[control.provider_arn])
    with pytest.raises(ProbeBlocked, match="does not match"):
        setup.cleanup()
    assert not any(name.startswith("delete_") for name, _ in control.calls)


def test_lost_delete_reply_reconciles_only_after_readback(tmp_path):
    setup, control = setup_for(tmp_path)
    setup.prepare()
    control.lose_provider_delete_reply = True
    with pytest.raises(ProbeBlocked, match="ambiguous"):
        setup.cleanup()
    assert json.loads(setup.ledger.path.read_text())["cleanup_phase"] == "provider_deleting"
    assert setup.cleanup()["status"] == "cleaned"
    assert [name for name, _ in control.calls if name.startswith("delete_")] == ["delete_provider", "delete_workload"]


def test_foreign_workload_blocks_final_deletion_after_owned_provider_removal(tmp_path):
    setup, control = setup_for(tmp_path)
    setup.prepare()
    control.tags[control.workload_arn][OWNERSHIP_TAG] = "foreign-owner"
    with pytest.raises(ProbeBlocked, match="ownership"):
        setup.cleanup()
    assert [name for name, _ in control.calls if name.startswith("delete_")] == ["delete_provider"]
    assert control.workload is not None
    assert json.loads(setup.ledger.path.read_text())["status"] == "cleaning"


def test_lost_delete_without_effect_requires_matching_live_owner_before_retry(tmp_path):
    setup, control = setup_for(tmp_path)
    setup.prepare()
    control.fail_provider_delete_without_effect = True
    with pytest.raises(ProbeBlocked, match="ambiguous"):
        setup.cleanup()
    control.tags[control.provider_arn][OWNERSHIP_TAG] = "foreign-owner"
    with pytest.raises(ProbeBlocked, match="ownership"):
        setup.cleanup()
    assert [name for name, _ in control.calls if name == "delete_provider"] == ["delete_provider"]
    control.tags[control.provider_arn][OWNERSHIP_TAG] = json.loads(setup.ledger.path.read_text())["ownership_nonce"]
    assert setup.cleanup()["controlled_cleanup"] == "verified"
    assert [name for name, _ in control.calls if name == "delete_provider"] == ["delete_provider", "delete_provider"]


def test_partial_setup_cleanup_checks_owned_tag_and_unresolved_create(tmp_path):
    setup, control = setup_for(tmp_path)
    control.lose_provider_reply = True
    with pytest.raises(ProbeBlocked, match="Provider create unconfirmed"):
        setup.prepare()
    assert setup.cleanup()["status"] == "cleaned"


def test_ambiguous_create_without_readback_never_claims_cleanup(tmp_path):
    setup, control = setup_for(tmp_path)
    control.lose_provider_reply = True
    with pytest.raises(ProbeBlocked, match="unconfirmed"):
        setup.prepare()
    provider = control.provider
    control.provider = None
    with pytest.raises(ProbeBlocked, match="unresolved"):
        setup.cleanup()
    assert not any(name.startswith("delete_") for name, _ in control.calls)
    control.provider = provider
    assert setup.cleanup()["controlled_cleanup"] == "verified"


def test_ready_resource_disappearance_and_reappearance_never_trigger_unowned_delete(tmp_path):
    setup, control = setup_for(tmp_path)
    setup.prepare()
    previous = control.provider
    control.provider = None
    with pytest.raises(ProbeBlocked, match="unexpected removal"):
        setup.cleanup()
    assert not any(name.startswith("delete_") for name, _ in control.calls)
    control.provider = previous
    setup.cleanup()
    control.provider = previous
    with pytest.raises(ProbeBlocked, match="reappeared"):
        setup.cleanup()
    assert [name for name, _ in control.calls if name == "delete_provider"] == ["delete_provider"]


def test_unmarked_old_setup_record_cannot_be_adopted_or_cleaned(tmp_path):
    setup, control = setup_for(tmp_path)
    setup.prepare()
    record = json.loads(setup.ledger.path.read_text())
    record.pop("ownership_nonce")
    setup.ledger._save(record)
    with pytest.raises(ProbeBlocked, match="older setup"):
        setup.prepare()
    with pytest.raises(ProbeBlocked, match="No tagged setup"):
        setup.cleanup()
    assert not any(name.startswith("delete_") for name, _ in control.calls)


@pytest.mark.parametrize("status", ["CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"])
def test_owned_failed_provider_can_be_cleaned_without_ready_configuration(tmp_path, status):
    setup, control = setup_for(tmp_path)
    setup.prepare()
    control.provider = {"name": setup.plan.identity.provider_name, "credentialProviderArn": control.provider_arn,
                        "status": status}
    assert setup.cleanup() == {"status": "cleaned", "controlled_cleanup": "verified",
                               "provider_grant_revocation": "unproven"}
    assert [name for name, _ in control.calls if name.startswith("delete_")] == ["delete_provider", "delete_workload"]


def test_failed_partial_create_with_lost_reply_uses_recorded_attempt_tag(tmp_path):
    setup, control = setup_for(tmp_path)
    control.lose_provider_reply = True
    with pytest.raises(ProbeBlocked, match="unconfirmed"):
        setup.prepare()
    record = json.loads(setup.ledger.path.read_text())
    assert record["uncertain"] is True and "provider_arn" not in record
    control.provider = {"name": setup.plan.identity.provider_name, "credentialProviderArn": control.provider_arn,
                        "status": "CREATE_FAILED"}
    assert setup.cleanup()["controlled_cleanup"] == "verified"
    assert [name for name, _ in control.calls if name.startswith("delete_")] == ["delete_provider", "delete_workload"]


@pytest.mark.parametrize("status,failed", [("CREATING", "CREATE_FAILED"), ("UPDATING", "UPDATE_FAILED")])
def test_in_progress_provider_waits_for_failure_before_cleanup(tmp_path, status, failed):
    setup, control = setup_for(tmp_path)
    setup.prepare()
    control.provider["status"] = status
    with pytest.raises(ProbeBlocked, match="still in progress"):
        setup.cleanup()
    assert not any(name.startswith("delete_") for name, _ in control.calls)
    control.provider["status"] = failed
    assert setup.cleanup()["controlled_cleanup"] == "verified"


def test_deleting_provider_is_polled_not_deleted_again(tmp_path):
    setup, control = setup_for(tmp_path)
    setup.prepare()
    control.provider["status"] = "DELETING"
    with pytest.raises(ProbeBlocked, match="still in progress"):
        setup.cleanup()
    assert json.loads(setup.ledger.path.read_text())["cleanup_phase"] == "provider_deleting"
    assert not any(name.startswith("delete_") for name, _ in control.calls)
    control.provider = None
    assert setup.cleanup()["controlled_cleanup"] == "verified"
    assert [name for name, _ in control.calls if name.startswith("delete_")] == ["delete_workload"]


def test_deleting_provider_rechecks_tag_and_arn_before_failed_retry(tmp_path):
    setup, control = setup_for(tmp_path)
    setup.prepare()
    control.provider["status"] = "DELETING"
    with pytest.raises(ProbeBlocked, match="still in progress"):
        setup.cleanup()
    control.provider["status"] = "DELETE_FAILED"
    control.tags[control.provider_arn][OWNERSHIP_TAG] = "foreign-owner"
    with pytest.raises(ProbeBlocked, match="ownership"):
        setup.cleanup()
    assert not any(name.startswith("delete_") for name, _ in control.calls)
    control.tags[control.provider_arn][OWNERSHIP_TAG] = json.loads(setup.ledger.path.read_text())["ownership_nonce"]
    assert setup.cleanup()["controlled_cleanup"] == "verified"


@pytest.mark.parametrize("status", [None, "UNRECOGNIZED"])
def test_missing_or_unrecognized_provider_status_does_not_authorize_deletion(tmp_path, status):
    setup, control = setup_for(tmp_path)
    setup.prepare()
    control.provider["status"] = status
    with pytest.raises(ProbeBlocked, match="status does not authorize"):
        setup.cleanup()
    assert not any(name.startswith("delete_") for name, _ in control.calls)


def test_installed_control_sdk_accepts_tagged_creation_and_scoped_deletion(tmp_path):
    from botocore.session import get_session
    from botocore.validate import ParamValidator

    setup, control = setup_for(tmp_path)
    setup.prepare()
    setup.cleanup()
    service = get_session().get_service_model("bedrock-agentcore-control")
    validator = ParamValidator()
    assert {"CREATE_FAILED", "UPDATE_FAILED", "DELETING", "DELETE_FAILED"} <= set(
        service.operation_model("GetOauth2CredentialProvider").output_shape.members["status"].enum
    )
    operations = {
        "create_workload": "CreateWorkloadIdentity",
        "create_provider": "CreateOauth2CredentialProvider",
        "list_tags": "ListTagsForResource",
        "delete_provider": "DeleteOauth2CredentialProvider",
        "delete_workload": "DeleteWorkloadIdentity",
    }
    for operation, arguments in control.calls:
        if operation not in operations:
            continue
        shape = service.operation_model(operations[operation]).input_shape
        params = arguments if operation.startswith("create_") else {
            "resourceArn" if operation == "list_tags" else "name": arguments
        }
        assert not validator.validate(params, shape).has_errors()
        if operation.startswith("create_"):
            assert OWNERSHIP_TAG in params["tags"]


def test_lost_workload_delete_reply_reconciles_without_provider_replay(tmp_path):
    setup, control = setup_for(tmp_path)
    setup.prepare()
    control.lose_workload_delete_reply = True
    with pytest.raises(ProbeBlocked, match="ambiguous"):
        setup.cleanup()
    assert json.loads(setup.ledger.path.read_text())["cleanup_phase"] == "workload_deleting"
    assert setup.cleanup()["controlled_cleanup"] == "verified"
    assert [name for name, _ in control.calls if name.startswith("delete_")] == ["delete_provider", "delete_workload"]


def test_ownership_tag_read_failure_blocks_deletion(tmp_path):
    setup, control = setup_for(tmp_path)
    setup.prepare()

    def unavailable(**_kwargs):
        raise TimeoutError("protected-tag-read-unavailable")

    control.list_tags_for_resource = unavailable
    with pytest.raises(ProbeBlocked, match="could not be read"):
        setup.cleanup()
    assert not any(name.startswith("delete_") for name, _ in control.calls)
