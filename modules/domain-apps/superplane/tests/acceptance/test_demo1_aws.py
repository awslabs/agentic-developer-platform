"""Offline AWS CLI doubles: authorization, absence, survivor and cost boundaries."""

import json
import subprocess
from datetime import UTC, datetime

import pytest
from test_demo1_cli import fixture_documents, identifier

from superplane_acceptance.demo1_aws import AwsProviderReader
from superplane_acceptance.demo1_evidence import DemoInput, EvidenceError
from superplane_acceptance.demo1_provider import observe_provider


def arn(service, kind, name):
    return f"arn:aws:{service}:us-east-1:123456789012:{kind}/{name}"


OWNED = (
    arn("eks", "cluster", "example-owned"),
    arn("ec2", "volume", "vol-12345678"),
    arn("ec2", "vpc", "vpc-12345678"),
)
SURVIVORS = (arn("eks", "cluster", "example-peer"),)


class AwsCli:
    def __init__(self):
        self.calls = []
        self.role = "ExampleObserver"
        self.account = "123456789012"
        self.absent = set(OWNED)
        self.denied = set()

    def __call__(self, command, **options):
        assert options == {
            "capture_output": True,
            "text": True,
            "check": False,
            "timeout": 30,
        }
        self.calls.append(command)
        assert command[:8] == [
            "adp-cred",
            "assume",
            "--service",
            "aws",
            "--label",
            "example-connection",
            "--exec",
            "aws",
        ]
        service, operation = command[8:10]
        if service == "sts":
            assert operation == "get-caller-identity"
            output = {
                "Account": self.account,
                "Arn": f"arn:aws:sts:{self.account}:assumed-role/{self.role}/example-session",
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(output), "")
        assert command[-4:-2] == ["--region", "us-east-1"]
        kind = {
            "describe-cluster": "cluster",
            "describe-volumes": "volume",
            "describe-vpcs": "vpc",
        }[operation]
        name = command[-5]
        resource = arn("eks" if kind == "cluster" else "ec2", kind, name)
        if resource in self.denied:
            return subprocess.CompletedProcess(
                command, 255, "", "An error occurred (AccessDeniedException)"
            )
        if resource in self.absent:
            error = {
                "cluster": "ResourceNotFoundException",
                "volume": "InvalidVolume.NotFound",
                "vpc": "InvalidVpcID.NotFound",
            }[kind]
            return subprocess.CompletedProcess(
                command, 255, "", f"An error occurred ({error})"
            )
        output = {
            "cluster": {"cluster": {"arn": resource}},
            "volume": {"Volumes": [{"VolumeId": name}]},
            "vpc": {"Vpcs": [{"VpcId": name}]},
        }[kind]
        return subprocess.CompletedProcess(command, 0, json.dumps(output), "")


@pytest.fixture
def selection():
    value, _ = fixture_documents()
    value["survivors"] = list(SURVIVORS)
    return DemoInput.parse(value)


def reader(selection, executor):
    return AwsProviderReader(
        connection_id=selection.connection_id,
        broker_label="example-connection",
        account=selection.account,
        role_name=selection.role,
        region=selection.region,
        runner=executor,
        clock=lambda: datetime(2026, 10, 5, 11, 30, tzinfo=UTC),
    )


def query(selection, executor, owned=OWNED):
    return observe_provider(
        selection,
        identifier(6),
        owned,
        reader(selection, executor),
        origin="provider-unverified",
    )


def test_no_resource_observation_without_matching_sts_identity(selection):
    executor = AwsCli()
    executor.account = "000000000000"
    outcome = query(selection, executor)
    assert outcome.cleanup == outcome.survivors == outcome.cost == "BLOCKED"
    assert all(command[8] == "sts" for command in executor.calls)
    executor = AwsCli()
    executor.role = "ExampleOtherRole"
    assert query(selection, executor).cleanup == "BLOCKED"
    assert all(command[8] == "sts" for command in executor.calls)


def test_verified_reads_preserve_surviving_peer_but_never_assert_zero_cost(selection):
    executor = AwsCli()
    executor.absent.discard(SURVIVORS[0])
    outcome = query(selection, executor)
    assert (outcome.cleanup, outcome.survivors, outcome.cost) == (
        "BLOCKED",
        "BLOCKED",
        "BLOCKED",
    )
    assert outcome.origin == "provider-unverified"
    assert len([command for command in executor.calls if command[8] == "sts"]) == len(
        OWNED
    ) + len(SURVIVORS)
    assert len(executor.calls) == 2 * (len(OWNED) + len(SURVIVORS))


@pytest.mark.parametrize("change", ["partial", "survivor", "denied"])
def test_partial_cleanup_missing_peer_or_denied_inventory_never_passes(
    selection, change
):
    executor = AwsCli()
    if change == "partial":
        executor.absent.discard(OWNED[1])
        executor.absent.discard(SURVIVORS[0])
    if change == "survivor":
        executor.absent.add(SURVIVORS[0])
    if change == "denied":
        executor.denied.add(OWNED[1])
    outcome = query(selection, executor)
    assert outcome.cleanup == ("FAIL" if change == "partial" else "BLOCKED")
    assert outcome.survivors == ("FAIL" if change == "survivor" else "BLOCKED")
    assert outcome.cost == "BLOCKED"


def test_missing_resource_class_is_incomplete_even_if_every_read_returns_absent(
    selection,
):
    executor = AwsCli()
    outcome = query(selection, executor, OWNED[:1])
    assert outcome.cleanup == outcome.survivors == outcome.cost == "BLOCKED"


@pytest.mark.parametrize(
    "mutation", ["account", "role", "connection", "foreign-resource", "duplicate"]
)
def test_swapped_selection_or_invented_ownership_refused_before_network(
    selection, mutation
):
    executor = AwsCli()
    provider = reader(selection, executor)
    from superplane_acceptance.demo1_provider import InventoryQuery

    fields = {
        "connection_id": selection.connection_id,
        "role": selection.role,
        "account": selection.account,
        "region": selection.region,
        "workspace_id": identifier(6),
        "expected_owned": OWNED,
        "expected_survivors": SURVIVORS,
    }
    if mutation == "account":
        fields["account"] = "000000000000"
    elif mutation == "role":
        fields["role"] = "ExampleOtherRole"
    elif mutation == "connection":
        fields["connection_id"] = identifier(77)
    elif mutation == "foreign-resource":
        fields["expected_owned"] = (OWNED[0].replace("123456789012", "000000000000"),)
    else:
        fields["expected_owned"] = (OWNED[0], OWNED[0])
    with pytest.raises(EvidenceError):
        provider.read_inventory(InventoryQuery(**fields))
    assert executor.calls == []
