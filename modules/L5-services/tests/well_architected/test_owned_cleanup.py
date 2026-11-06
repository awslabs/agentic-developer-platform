"""Cleanup-only native boundary uses durable ownership, never source authority."""

from hashlib import sha256
import json
from unittest.mock import Mock

from botocore.exceptions import ClientError
import pytest

from l5_services.adapters.well_architected.binding import (
    ProfileAdapter, ProfileCleanupProvider, ProfileIdentityError, ProfileIdentitySelection,
)


def cleanup_fixture():
    account, region = "000000000000", "us-east-1"
    role = f"arn:aws:iam::{account}:role/broker-example"
    selection = ProfileIdentitySelection("tenant-example", "product-example", "project-example", account,
                                         region, role, f"arn:aws:iam::{account}:role/source-example", ("SECURITY",))
    fingerprint = selection.scope_fingerprint()
    arn = f"arn:aws:wellarchitected:{region}:{account}:agent-profile/owned-example"
    native = {"arn": arn, "name": ProfileAdapter._created_name(fingerprint, "create-example"),
              "tags": [{"key": "adp:scope", "value": fingerprint}], "aggregationConfiguration": [],
              "executionRoleArn": selection.execution_role_arn, "pillars": ["SECURITY"],
              "eligibleForArchitectureGeneration": True, "eligibleForScheduledGeneration": False,
              "fieldErrors": [], "deletionProtection": False}
    evidence = {"arn": arn, "aggregation": [], "role": selection.execution_role_arn, "pillars": ["SECURITY"],
                "contexts": [], "eligible": True, "scheduled": False, "field_errors": []}
    connection = {"record_id": "connection-example"}
    reference = {"record_type": "agent_profile", "record_id": arn, "source_connection_id": connection["record_id"],
                 "record_revision": sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()}
    identity = {"account_id": account, "region": region, "role_arn": role, "execution_role_arn": selection.execution_role_arn}
    receipt = {"operation_id": "create-example", "native_resource_ref": reference, "provider_identity": identity, "intent_hash": "a" * 64}
    owned = {"native_resource_ref": reference, "provider_identity": identity, "creation_receipt": receipt}
    verified = {**identity, "connection_ref": connection, "authorization": {"control_action": "cleanup"}, "owned_cleanup": owned}
    deleted = []
    client = Mock()

    def get(**kwargs):
        assert kwargs == {"profileArn": arn}
        if deleted:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetAgentProfile")
        return native

    client.get_agent_profile.side_effect = get
    client.list_agent_profiles.side_effect = lambda **_: {"items": [] if deleted else [{"arn": arn, "name": native["name"]}]}
    client.list_agent_contexts.return_value = {"items": []}
    client.delete_agent_profile.side_effect = lambda **kwargs: deleted.append(kwargs)
    provider = ProfileCleanupProvider(client, selection, connection, owned)
    return provider, client, native, reference, receipt, verified, deleted


@pytest.mark.asyncio
async def test_owned_cleanup_requires_no_source_qualification_and_verifies_absence():
    provider, client, _, reference, receipt, verified, deleted = cleanup_fixture()
    assert await provider.cleanup(reference, verified, receipt) is True
    assert deleted == [{"profileArn": reference["record_id"]}]
    assert await provider.cleanup(reference, verified, receipt) is True
    assert len(deleted) == 1
    assert not hasattr(provider, "create") and not hasattr(provider, "read") and not hasattr(provider, "reconcile")
    assert {call[0] for call in client.mock_calls} <= {
        "get_agent_profile", "list_agent_profiles", "list_agent_contexts", "delete_agent_profile",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["name", "tag", "contexts", "snapshot", "protected", "absent", "foreign_receipt", "wrong_action"])
async def test_cleanup_denies_native_drift_unproven_absence_or_other_authority(change):
    provider, client, native, reference, receipt, verified, deleted = cleanup_fixture()
    if change == "name":
        native["name"] = "customer-owned"
    elif change == "tag":
        native["tags"] = []
    elif change == "contexts":
        client.list_agent_contexts.return_value = {"items": [{"profileArn": reference["record_id"], "id": "unrequested"}]}
    elif change == "snapshot":
        native["eligibleForScheduledGeneration"] = True
    elif change == "protected":
        native["deletionProtection"] = True
    elif change == "absent":
        client.delete_agent_profile.side_effect = None
        assert await provider.cleanup(reference, verified, receipt) is False
        return
    elif change == "foreign_receipt":
        receipt = {**receipt, "operation_id": "different-create"}
    else:
        verified = {**verified, "authorization": {"control_action": "resume"}}
    with pytest.raises(ProfileIdentityError):
        await provider.cleanup(reference, verified, receipt)
    assert deleted == []


def test_identity_only_selection_cannot_authorize_profile_creation():
    provider, client, _, _, _, _, _ = cleanup_fixture()
    with pytest.raises(AttributeError):
        provider.adapter.create(provider.selection, "new-operation")
    client.create_agent_profile.assert_not_called()
