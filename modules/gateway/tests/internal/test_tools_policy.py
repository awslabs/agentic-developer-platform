from decimal import Decimal
from unittest.mock import Mock

import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from fastapi import HTTPException

from src.internal.tools_policy import ToolsPolicyStore

CATALOG = [{"name": "browser___start", "description": "Start browser", "inputSchema": {"type": "object"}}]
TENANT = "tenant-one"
OWNER = "00000000-0000-4000-8000-000000000001"


def item(**updates):
    record = {
        "record_type": "TOOLS_POLICY",
        "schema_version": "1.0",
        "tenant_id": TENANT,
        "owner_id": OWNER,
        "status": "active",
        "allowed_tools": ["websearch.search"],
    }
    record.update(updates)
    return {key: TypeSerializer().serialize(value) for key, value in record.items()}


def test_tools_policy_uses_separate_owned_namespace_and_consistent_read():
    client = Mock()
    client.get_item.return_value = {"Item": item()}
    result = ToolsPolicyStore(table_name="authority", client=client).get(tenant_id=TENANT, owner_id=OWNER)
    assert result["allowed_tools"] == ["websearch.search"]
    client.get_item.assert_called_once_with(
        TableName="authority",
        ConsistentRead=True,
        Key={"pk": {"S": "TENANT#tenant-one"}, "sk": {"S": f"TOOLS_POLICY#{OWNER}"}},
    )


@pytest.mark.parametrize(
    "corruption", [{"tenant_id": "another-tenant"}, {"owner_id": "other"}, {"record_type": "TASK_SERVICE_POLICY"}, {"schema_version": "2.0"}]
)
def test_wrong_owner_or_schema_never_grants_tools(corruption):
    client = Mock()
    client.get_item.return_value = {"Item": item(**corruption)}
    with pytest.raises(HTTPException) as refused:
        ToolsPolicyStore(table_name="authority", client=client).get(tenant_id=TENANT, owner_id=OWNER)
    assert refused.value.status_code == 503


def test_missing_policy_does_not_grant_tools():
    client = Mock()
    client.get_item.return_value = {}
    assert ToolsPolicyStore(table_name="authority", client=client).get(tenant_id=TENANT, owner_id=OWNER) is None


def test_consent_update_is_conditional_on_owner_and_entitlements():
    client = Mock()
    ToolsPolicyStore(table_name="authority", client=client).put_consent(
        tenant_id=TENANT, owner_id=OWNER, allowed_tools=["websearch.search"], consented_tools=[], previous_consent=[], expected_revision=0
    )
    params = client.update_item.call_args.kwargs
    assert params["Key"] == {"pk": {"S": f"TENANT#{TENANT}"}, "sk": {"S": f"TOOLS_POLICY#{OWNER}"}}
    assert params["ExpressionAttributeValues"][":consent"] == {"L": []}
    assert params["ExpressionAttributeValues"][":allowed"] == {"L": [{"S": "websearch.search"}]}
    assert "owner_id = :owner" in params["ConditionExpression"]
    assert "revision = :next" in params["UpdateExpression"]
    assert "consented_tools = :previous" in params["ConditionExpression"]
    assert params["ExpressionAttributeValues"][":expected"] == {"N": "0"}
    assert params["ExpressionAttributeValues"][":next"] == {"N": "1"}


def test_stale_entitlements_refuse_consent_write():
    client = Mock()
    client.update_item.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
    with pytest.raises(HTTPException) as refused:
        ToolsPolicyStore(table_name="authority", client=client).put_consent(
            tenant_id=TENANT, owner_id=OWNER, allowed_tools=["websearch.search"], consented_tools=[], previous_consent=[], expected_revision=0
        )
    assert refused.value.status_code == 409


def test_new_user_policy_is_conditional_and_never_creates_consent():
    client = Mock()
    client.get_item.return_value = {}
    result = ToolsPolicyStore(table_name="authority", client=client).put_allowed(
        tenant_id=TENANT,
        owner_id=OWNER,
        allowed_tools=["browser.start"],
        tool_max_cost_usd={"browser.start": Decimal("0.20")},
        catalog=CATALOG,
        expected_revision=0,
    )
    request = client.put_item.call_args.kwargs
    assert request["ConditionExpression"] == "attribute_not_exists(pk)"
    assert request["Item"]["consented_tools"] == {"L": []}
    assert request["Item"]["catalog"]["L"][0]["M"]["name"] == {"S": "browser___start"}
    assert request["Item"]["tool_max_cost_usd"] == {"M": {"browser.start": {"N": "0.20"}}}
    assert result["revision"] == 1
    assert result["consented_tools"] == []
    client.update_item.assert_not_called()


def test_policy_reconciliation_retains_only_existing_consent_and_checks_revision():
    client = Mock()
    client.get_item.return_value = {
        "Item": item(
            allowed_tools=["browser.start", "websearch.search"],
            consented_tools=["websearch.search", "browser.start"],
            revision=3,
        )
    }
    store = ToolsPolicyStore(table_name="authority", client=client)
    result = store.put_allowed(
        tenant_id=TENANT,
        owner_id=OWNER,
        allowed_tools=["browser.start", "code_interpreter.start"],
        tool_max_cost_usd={"browser.start": Decimal("0.20"), "code_interpreter.start": Decimal("0.30")},
        catalog=CATALOG,
        expected_revision=3,
    )
    assert result["consented_tools"] == ["browser.start"]
    assert type(result["revision"]) is int
    assert "code_interpreter.start" not in result["consented_tools"]
    values = client.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert values[":previous_consent"] == {"L": [{"S": "websearch.search"}, {"S": "browser.start"}]}
    assert values[":consent"] == {"L": [{"S": "browser.start"}]}
    assert values[":revision"] == {"N": "3"}
    assert values[":next"] == {"N": "4"}
    assert values[":catalog"]["L"][0]["M"]["name"] == {"S": "browser___start"}
    with pytest.raises(HTTPException) as refused:
        store.put_allowed(tenant_id=TENANT, owner_id=OWNER, allowed_tools=[], tool_max_cost_usd={}, catalog=[], expected_revision=2)
    assert refused.value.status_code == 409
    assert client.update_item.call_count == 1


def test_concurrent_policy_write_returns_explicit_conflict():
    client = Mock()
    client.get_item.return_value = {"Item": item(consented_tools=[], revision=1)}
    client.update_item.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
    with pytest.raises(HTTPException) as refused:
        ToolsPolicyStore(table_name="authority", client=client).put_allowed(
            tenant_id=TENANT,
            owner_id=OWNER,
            allowed_tools=[],
            tool_max_cost_usd={},
            catalog=[],
            expected_revision=1,
        )
    assert refused.value.status_code == 409


def test_consent_and_admin_changes_reject_stale_snapshots_without_losing_consent(monkeypatch):
    import boto3
    from moto import mock_aws

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="authority",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        )
        store = ToolsPolicyStore(table_name="authority", client=client)
        store.put_allowed(
            tenant_id=TENANT,
            owner_id=OWNER,
            allowed_tools=["browser.start"],
            tool_max_cost_usd={"browser.start": Decimal("0.20")},
            catalog=CATALOG,
            expected_revision=0,
        )
        old = store.get(tenant_id=TENANT, owner_id=OWNER)
        store.put_consent(
            tenant_id=TENANT,
            owner_id=OWNER,
            allowed_tools=old["allowed_tools"],
            consented_tools=["browser.start"],
            previous_consent=[],
            expected_revision=old["revision"],
        )
        with pytest.raises(HTTPException) as refused:
            store.put_consent(
                tenant_id=TENANT,
                owner_id=OWNER,
                allowed_tools=old["allowed_tools"],
                consented_tools=[],
                previous_consent=[],
                expected_revision=old["revision"],
            )
        assert refused.value.status_code == 409
        with pytest.raises(HTTPException) as refused:
            store.put_allowed(tenant_id=TENANT, owner_id=OWNER, allowed_tools=[], tool_max_cost_usd={}, catalog=[], expected_revision=old["revision"])
        assert refused.value.status_code == 409
        assert store.get(tenant_id=TENANT, owner_id=OWNER)["consented_tools"] == ["browser.start"]
        assert store.get(tenant_id=TENANT, owner_id=OWNER)["revision"] == 2
        store.put_allowed(tenant_id=TENANT, owner_id=OWNER, allowed_tools=[], tool_max_cost_usd={}, catalog=[], expected_revision=2)
        final = store.get(tenant_id=TENANT, owner_id=OWNER)
        assert final["consented_tools"] == [] and final["revision"] == 3

        with monkeypatch.context() as fault:
            fault.setattr(
                client,
                "update_item",
                Mock(
                    side_effect=ClientError(
                        {"Error": {"Code": "InternalServerError"}},
                        "UpdateItem",
                    )
                ),
            )
            with pytest.raises(HTTPException) as refused:
                store.put_consent(tenant_id=TENANT, owner_id=OWNER, allowed_tools=[], consented_tools=[], previous_consent=[], expected_revision=3)
        assert refused.value.status_code == 503
        assert store.get(tenant_id=TENANT, owner_id=OWNER) == final


def test_first_party_consent_survives_admin_policy_writes_and_can_precede_them(monkeypatch):
    """#8113: consent to an implicitly allowed platform tool rides the tenant entitlement, not `allowed_tools`."""
    import boto3
    from moto import mock_aws

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="authority",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        )
        store = ToolsPolicyStore(table_name="authority", client=client)
        assert store.get(tenant_id=TENANT, owner_id=OWNER) is None
        store.ensure_policy(tenant_id=TENANT, owner_id=OWNER)
        store.ensure_policy(tenant_id=TENANT, owner_id=OWNER)  # a lost race is not an error
        created = store.get(tenant_id=TENANT, owner_id=OWNER)
        assert created["status"] == "active" and created["allowed_tools"] == [] and created["consented_tools"] == [] and created["revision"] == 0
        store.put_consent(
            tenant_id=TENANT, owner_id=OWNER, allowed_tools=[], consented_tools=["platform.my_work_items"], previous_consent=[], expected_revision=0
        )
        assert store.get(tenant_id=TENANT, owner_id=OWNER)["consented_tools"] == ["platform.my_work_items"]
        # An admin later grants a third-party tool: the platform consent is kept, a stale third-party consent is not.
        store.put_allowed(
            tenant_id=TENANT,
            owner_id=OWNER,
            allowed_tools=["browser.start"],
            tool_max_cost_usd={"browser.start": Decimal("0.20")},
            catalog=CATALOG,
            expected_revision=1,
        )
        store.put_consent(
            tenant_id=TENANT,
            owner_id=OWNER,
            allowed_tools=["browser.start"],
            consented_tools=["platform.my_work_items", "browser.start"],
            previous_consent=["platform.my_work_items"],
            expected_revision=2,
        )
        store.put_allowed(tenant_id=TENANT, owner_id=OWNER, allowed_tools=[], tool_max_cost_usd={}, catalog=[], expected_revision=3)
        assert store.get(tenant_id=TENANT, owner_id=OWNER)["consented_tools"] == ["platform.my_work_items"]
