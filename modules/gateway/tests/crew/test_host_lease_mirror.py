"""clear_host_lease_mirror: a lifecycle fence retracts the owner's published host lease.

#8223 cause 39. stop/replace/release revoked the run and terminated the access session in the
control plane but left the DynamoDB lease mirror (``crew#<tenant>#<user>`` / ``executor``)
carrying the pre-fence lease with its original expiry, so ``publish_host_lease`` refused every
same-generation restart as a conflicting live lease. The helper proven here is what the fence
calls; the end-to-end stop → start → publish sequence lives in
``tests/cloud_desktop/test_host_enrollment.py``.
"""

import time

import boto3
import pytest
from moto import mock_aws

from src.crew.host_lease import clear_host_lease_mirror
from src.crew.targets import CrewOwner
from tests.crew.lease_mirror_table import GatewayRoleTable

KEY = {"PK": "crew#one#alice", "SK": "executor"}
OWNER = CrewOwner("one", "alice")


@pytest.fixture
def table():
    with mock_aws():
        # Wrapped so the helper is proven against the gateway role's real grant (PutItem +
        # DeleteItem on crew#* keys) and DynamoDB's expression validation, not moto's leniency.
        yield GatewayRoleTable(
            boto3.resource("dynamodb", region_name="us-east-1").create_table(
                TableName="lease-mirror",
                BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
            )
        )


def _mirror(table, *, state="ready", target_kind="ec2-instance", target_ref="box-a", tenant="one", user="alice"):
    item = {
        **KEY,
        "tenantId": tenant,
        "ownerUserId": user,
        "crewLeaseState": state,
        "crewLease": {
            "tenant_id": tenant,
            "user_id": user,
            "target_kind": target_kind,
            "target_ref": target_ref,
            "generation": 25,
            "expires_at": int(time.time()) + 600,
            "endpoint": "https://10.0.10.129:8443",
        },
    }
    table.put_item(Item=item)
    return item


def test_fence_marks_the_boxs_lease_fenced_and_drops_it(table):
    _mirror(table)
    assert clear_host_lease_mirror(table, OWNER, "box-a") is True
    item = table.get_item(Key=KEY)["Item"]
    assert item["crewLeaseState"] == "fenced", "fenced is recoverable; revoked is terminal for the owner"
    assert "crewLease" not in item
    assert (item["tenantId"], item["ownerUserId"]) == ("one", "alice")
    # Idempotent: nothing left to clear.
    assert clear_host_lease_mirror(table, OWNER, "box-a") is False
    assert table.get_item(Key=KEY)["Item"] == item


@pytest.mark.parametrize(
    "mirror",
    [
        pytest.param({"target_kind": "eks-pod", "target_ref": "pod-uid"}, id="pod_lease"),
        pytest.param({"target_ref": "box-b"}, id="another_box"),
        pytest.param({"state": "revoked"}, id="revoked_owner"),
    ],
)
def test_fence_leaves_leases_it_does_not_own_alone(table, mirror):
    before = _mirror(table, **mirror)
    assert clear_host_lease_mirror(table, OWNER, "box-a") is False
    assert table.get_item(Key=KEY)["Item"] == before


def test_fence_without_a_mirror_or_a_table_is_a_no_op(table):
    assert clear_host_lease_mirror(table, OWNER, "box-a") is False
    assert "Item" not in table.get_item(Key=KEY)
    assert clear_host_lease_mirror(None, OWNER, "box-a") is False


def test_fence_never_touches_another_owners_row(table):
    before = _mirror(table, tenant="two", user="alice")
    assert clear_host_lease_mirror(table, OWNER, "box-a") is False
    assert table.get_item(Key=KEY)["Item"] == before


def test_fence_uses_only_the_grants_the_gateway_role_holds(table):
    """#8223 cause 43: the clear ran as UpdateItem, which the role may not call on crew#* keys, so
    dev's stop left the stale lease behind. The wrapped table refuses UpdateItem like IAM does."""
    _mirror(table)
    assert clear_host_lease_mirror(table, OWNER, "box-a") is True
    assert "crewLease" not in table.get_item(Key=KEY)["Item"]
