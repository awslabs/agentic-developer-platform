"""Conditional authority provisioning, replay, replacement and collision tests."""

import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

spec = importlib.util.spec_from_file_location(
    "domain_registration", Path(__file__).parents[1] / "register-domain-operation.py"
)
registration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registration)


@pytest.fixture
def scenario(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        iam, ddb = boto3.client("iam"), boto3.client("dynamodb")
        ddb.create_table(
            TableName="test-domain-registry",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "agent_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "agent_id", "AttributeType": "S"},
                {"AttributeName": "role_arn", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by-role-arn",
                    "KeySchema": [{"AttributeName": "role_arn", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
        document = {
            "version": 1,
            "account_id": "123456789012",
            "region": "us-east-1",
            "environment": "dev",
            "operator_role_arn": "arn:aws:iam::123456789012:role/Installer",
            "registry_table": "test-domain-registry",
            "domain_org_id": "00000000-0000-4000-8000-000000000001",
            "adp_org_id": "test-organization",
        }
        for kind, suffix in (
            ("producer", "api-producer"),
            ("worker", "domain-worker"),
        ):
            role = iam.create_role(
                RoleName=f"adp-dev-superplane-{suffix}",
                AssumeRolePolicyDocument='{"Version":"2012-10-17","Statement":[]}',
            )["Role"]
            document[kind] = {
                "agent_id": registration.registry_id(role["Arn"]),
                "role_arn": role["Arn"],
                "role_id": role["RoleId"],
            }
        caller = {
            "Account": "123456789012",
            "Arn": "arn:aws:sts::123456789012:assumed-role/Installer/test",
        }
        clients = {
            "iam": iam,
            "dynamodb": ddb,
            "sts": SimpleNamespace(get_caller_identity=lambda: caller),
        }
        session = SimpleNamespace(client=lambda service, **_: clients[service])
        yield document, session, ddb, iam, caller, clients


def test_atomic_pair_and_identical_resume_have_only_fixed_scopes(scenario):
    document, session, ddb, *_ = scenario
    assert registration.register(document, session, check_only=True)["state"] == "absent"
    assert ddb.scan(TableName=document["registry_table"])["Count"] == 0
    first = registration.register(document, session)
    assert registration.register(document, session) == first
    rows = ddb.scan(TableName=document["registry_table"])["Items"]
    assert len(rows) == 2
    assert sorted(scope for row in rows for scope in row["credential_scopes"]["SS"]) == [
        "domain:operation-executor",
        "domain:operation-producer",
        "domain:operation-recovery",
    ]
    assert all("allowed_models" not in row and row["scope"] == {"S": "internal"} for row in rows)


def test_lost_write_reply_recovers_original_atomic_pair(scenario):
    document, session, ddb, _, _, clients = scenario

    class LostReply:
        def __getattr__(self, name):
            return getattr(ddb, name)

        def transact_write_items(self, **kwargs):
            ddb.transact_write_items(**kwargs)
            raise TimeoutError("transport lost reply")

    clients["dynamodb"] = LostReply()
    with pytest.raises(TimeoutError):
        registration.register(document, session)
    clients["dynamodb"] = ddb
    assert registration.register(document, session)["state"] == "verified"
    assert ddb.scan(TableName=document["registry_table"])["Count"] == 2


@pytest.mark.parametrize("change", ["foreign-owner", "revoked", "role-scopes", "extra-field"])
def test_existing_authority_is_never_overwritten_or_restored(scenario, change):
    document, session, ddb, *_ = scenario
    registration.register(document, session)
    rows = ddb.scan(TableName=document["registry_table"])["Items"]
    changed = rows[0]
    if change == "foreign-owner":
        changed["owner"] = {"S": "another-owner"}
    if change == "revoked":
        changed["status"] = {"S": "revoked"}
    if change == "role-scopes":
        changed["credential_scopes"]["SS"].append("credential:raw-read")
    if change == "extra-field":
        changed["unreviewed"] = {"S": "value"}
    ddb.put_item(TableName=document["registry_table"], Item=changed)
    before = ddb.scan(TableName=document["registry_table"])["Items"]
    with pytest.raises(registration.Refused, match="different owner"):
        registration.register(document, session)
    assert ddb.scan(TableName=document["registry_table"])["Items"] == before


def test_other_registry_id_for_same_role_is_refused(scenario):
    document, session, ddb, *_ = scenario
    foreign = registration.records(document)[0]
    foreign["agent_id"] = {"S": "foreign-id"}
    ddb.put_item(TableName=document["registry_table"], Item=foreign)
    with pytest.raises(registration.Refused, match="another registry mapping"):
        registration.register(document, session)
    assert ddb.scan(TableName=document["registry_table"])["Count"] == 1


def test_replaced_role_refused_even_when_arn_is_identical(scenario):
    document, session, _, iam, *_ = scenario
    name = document["producer"]["role_arn"].rsplit("/", 1)[1]
    iam.delete_role(RoleName=name)
    iam.create_role(
        RoleName=name, AssumeRolePolicyDocument='{"Version":"2012-10-17","Statement":[]}'
    )
    with pytest.raises(registration.Refused, match="replaced"):
        registration.register(document, session)


def test_same_account_wrong_operator_and_foreign_account_refused(scenario):
    document, session, ddb, _, caller, _ = scenario
    for arn in (
        "arn:aws:sts::123456789012:assumed-role/Other/test",
        "arn:aws:sts::999999999999:assumed-role/Installer/test",
    ):
        caller["Arn"] = arn
        with pytest.raises(registration.Refused):
            registration.register(document, session)
    assert ddb.scan(TableName=document["registry_table"])["Count"] == 0


@pytest.mark.parametrize(
    "field,value",
    [("credential_scopes", ["*"]), ("worker", {}), ("domain_org_id", "not-an-organization")],
)
def test_closed_recipe_rejects_unreviewed_inputs_before_any_calls(scenario, field, value):
    document, *_ = scenario
    altered = copy.deepcopy(document)
    altered[field] = value
    session = SimpleNamespace(client=lambda *a, **k: pytest.fail("invalid input reached AWS"))
    with pytest.raises(ValueError):
        registration.register(altered, session)


def test_set_order_does_not_break_consistent_readback(scenario):
    document, session, ddb, *_ = scenario
    registration.register(document, session)
    worker = registration.records(document)[1]
    worker["credential_scopes"]["SS"].reverse()
    ddb.put_item(TableName=document["registry_table"], Item=worker)
    assert registration.register(document, session)["state"] == "verified"


def test_changed_registry_ids_refused_even_with_stale_role_index(scenario):
    document, session, ddb, _, _, clients = scenario
    registration.register(document, session)

    class StaleIndex:
        def __getattr__(self, name):
            return getattr(ddb, name)

        def query(self, **kwargs):
            return {"Items": []}

    clients["dynamodb"] = StaleIndex()
    changed = copy.deepcopy(document)
    changed["producer"]["agent_id"] = "00000000-0000-4000-8000-000000000004"
    changed["worker"]["agent_id"] = "00000000-0000-4000-8000-000000000005"
    with pytest.raises(registration.Refused, match="derived from the exact role"):
        registration.register(changed, session)
    assert ddb.scan(TableName=document["registry_table"])["Count"] == 2
    assert registration.register(document, session)["state"] == "verified"


def test_competing_org_pair_is_atomic_despite_both_preflight_reads_being_absent(scenario):
    document, session, ddb, _, _, clients = scenario
    competing = copy.deepcopy(document)
    competing["domain_org_id"] = "00000000-0000-4000-8000-000000000006"

    class ConcurrentWinner:
        def __getattr__(self, name):
            return getattr(ddb, name)

        def query(self, **kwargs):
            return {"Items": []}

        def transact_write_items(self, **kwargs):
            # Interleave the second full writer after the first writer's reads.
            # Both observe absent primary keys and an empty GSI before writing.
            clients["dynamodb"] = ddb
            assert registration.register(competing, session)["state"] == "verified"
            return ddb.transact_write_items(**kwargs)

    clients["dynamodb"] = ConcurrentWinner()
    with pytest.raises(ClientError, match="TransactionCanceledException"):
        registration.register(document, session)
    rows = ddb.scan(TableName=document["registry_table"])["Items"]
    assert len(rows) == 2
    assert all(row["domain_org_id"]["S"] == competing["domain_org_id"] for row in rows)
    with pytest.raises(registration.Refused, match="different owner"):
        registration.register(document, session)
    assert registration.register(competing, session)["state"] == "verified"
