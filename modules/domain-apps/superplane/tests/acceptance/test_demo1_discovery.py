"""Read-only AWS census fixtures; listings never establish complete cleanup."""

import copy
import json
import subprocess

import pytest
import test_demo1_ownership as ownership_fixtures
from test_demo1_aws import arn
from test_demo1_current import CurrentProducer, records_for, run_observation

from superplane_acceptance import demo1_discovery
from superplane_acceptance.demo1_report import reference

producer_imports = ownership_fixtures.producer_imports


class CensusProducer(CurrentProducer):
    def __init__(self, records):
        super().__init__(records)
        self.discovery_calls = []
        self.change = lambda operation, scope, result, token: result
        self.tags = [
            {"Key": "OrgId", "Value": records.scope["org_id"]},
            {"Key": "WorkspaceId", "Value": records.scope["workspace_id"]},
        ]
        self.rows = {
            "instances": (
                "Reservations",
                {
                    "OwnerId": records.scope["account"],
                    "Instances": [
                        {
                            "InstanceId": "i-11111111",
                            "VpcId": "vpc-12345678",
                            "Tags": self.tags,
                            "State": {"Name": "running"},
                            "BlockDeviceMappings": [
                                {"Ebs": {"VolumeId": "vol-11111111"}}
                            ],
                        }
                    ],
                },
            ),
            "volumes": ("Volumes", {"VolumeId": "vol-22222222", "Tags": self.tags}),
            "snapshots": (
                "Snapshots",
                {
                    "SnapshotId": "snap-11111111",
                    "Tags": self.tags,
                    "OwnerId": records.scope["account"],
                },
            ),
            "network-interfaces": (
                "NetworkInterfaces",
                {
                    "NetworkInterfaceId": "eni-11111111",
                    "VpcId": "vpc-12345678",
                    "Tags": self.tags,
                    "Association": {"AllocationId": "eipalloc-11111111"},
                },
            ),
            "addresses": (
                "Addresses",
                {"AllocationId": "eipalloc-22222222", "Tags": self.tags},
            ),
            "nat-gateways": (
                "NatGateways",
                {
                    "NatGatewayId": "nat-11111111",
                    "VpcId": "vpc-12345678",
                    "Tags": self.tags,
                },
            ),
            "route-tables": (
                "RouteTables",
                {
                    "RouteTableId": "rtb-11111111",
                    "VpcId": "vpc-12345678",
                    "Tags": self.tags,
                },
            ),
            "internet-gateways": (
                "InternetGateways",
                {
                    "InternetGatewayId": "igw-11111111",
                    "Attachments": [{"VpcId": "vpc-12345678"}],
                    "Tags": self.tags,
                },
            ),
            "vpc-endpoints": (
                "VpcEndpoints",
                {
                    "VpcEndpointId": "vpce-11111111",
                    "VpcId": "vpc-12345678",
                    "Tags": self.tags,
                },
            ),
            "launch-templates": (
                "LaunchTemplates",
                {"LaunchTemplateId": "lt-11111111", "Tags": self.tags},
            ),
            "subnets": (
                "Subnets",
                {
                    "SubnetId": "subnet-11111111",
                    "VpcId": "vpc-12345678",
                    "Tags": self.tags,
                },
            ),
            "security-groups": (
                "SecurityGroups",
                {"GroupId": "sg-11111111", "VpcId": "vpc-12345678", "Tags": self.tags},
            ),
            "vpcs": ("Vpcs", {"VpcId": "vpc-12345678", "Tags": self.tags}),
        }
        self.tagged = [
            {
                "ResourceARN": arn("kms", "key", "example-unresolved-key"),
                "Tags": self.tags,
            }
        ]

    def __call__(self, command, **options):
        if (
            command[5] != self.records.authority["broker_label"]
            or "--no-paginate" not in command
        ):
            return super().__call__(command, **options)
        assert command[:8] == [
            "adp-cred",
            "assume",
            "--service",
            "aws",
            "--label",
            self.records.authority["broker_label"],
            "--exec",
            "aws",
        ]
        assert self.calls[-1][8:10] == ["sts", "get-caller-identity"]
        assert 0 < options["timeout"] <= 30
        self.calls.append(command)
        operation = command[9].removeprefix("describe-")
        token = next(
            (
                command[command.index(key) + 1]
                for key in ("--next-token", "--pagination-token")
                if key in command
            ),
            None,
        )
        if operation == "get-resources":
            assert json.loads(command[command.index("--tag-filters") + 1]) == [
                {"Key": "OrgId", "Values": [self.records.scope["org_id"]]},
                {"Key": "WorkspaceId", "Values": [self.records.scope["workspace_id"]]},
            ]
            scope, result = (
                "tags",
                {"ResourceTagMappingList": copy.deepcopy(self.tagged)},
            )
        elif "--volume-ids" in command or "--allocation-ids" in command:
            option, collection, field = (
                ("--volume-ids", "Volumes", "VolumeId")
                if operation == "volumes"
                else ("--allocation-ids", "Addresses", "AllocationId")
            )
            scope, result = (
                "attachment",
                {collection: [{field: command[command.index(option) + 1]}]},
            )
        else:
            option = "--filter" if operation == "nat-gateways" else "--filters"
            filters = json.loads(command[command.index(option) + 1])
            if filters[0]["Name"].endswith("vpc-id"):
                assert filters == [
                    {
                        "Name": "attachment.vpc-id"
                        if operation == "internet-gateways"
                        else "vpc-id",
                        "Values": ["vpc-12345678"],
                    }
                ]
                scope = "vpc"
            else:
                assert filters == [
                    {"Name": "tag:OrgId", "Values": [self.records.scope["org_id"]]},
                    {
                        "Name": "tag:WorkspaceId",
                        "Values": [self.records.scope["workspace_id"]],
                    },
                ]
                scope = "tags"
            collection, row = self.rows[operation]
            result = {collection: [copy.deepcopy(row)]}
        self.discovery_calls.append((operation, scope, token))
        result = self.change(operation, scope, result, token)
        if result == "denied":
            return subprocess.CompletedProcess(
                command, 254, "", "private operator credential diagnostic"
            )
        if result == "timeout":
            raise subprocess.TimeoutExpired(command, options["timeout"])
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")


def test_census_covers_compute_storage_network_and_retains_unresolved_obligations():
    records = records_for()
    producer = CensusProducer(records)
    report = run_observation(records, producer)
    census = report["provider"]["discovery"]
    assert census["status"] == "OBSERVED" and census["listing_complete"] is True
    assert census["inventory_complete"] is False and census["owned_vpc_scanned"] is True
    entries = {item["resource_ref"]: item for item in census["resources"]}
    assert len(entries) == 15
    assert entries[reference(arn("ec2", "instance", "i-11111111"))]["basis"] == [
        "owned-vpc",
        "workspace-tags",
    ]
    assert entries[reference(arn("ec2", "volume", "vol-11111111"))]["basis"] == [
        "provider-attachment"
    ]
    assert entries[reference(arn("ec2", "elastic-ip", "eipalloc-11111111"))][
        "basis"
    ] == ["provider-attachment"]
    assert census["unresolved_resource_refs"] == [
        reference(producer.tagged[0]["ResourceARN"])
    ]
    assert report["provider"]["cost_usd"] is None
    assert all(
        report["checks"][key]["status"] == "BLOCKED"
        for key in ("cleanup", "cost", "current_inventory")
    )
    text = json.dumps(report)
    assert (
        records.scope["account"] not in text
        and records.scope["workspace_id"] not in text
    )
    assert "example-unresolved-key" not in text and "i-11111111" not in text


def test_list_operations_and_arguments_match_installed_aws_service_models():
    from botocore.session import get_session

    producer = CensusProducer(records_for())
    run_observation(producer.records, producer)
    session = get_session()
    for command in producer.calls:
        if "--no-paginate" not in command:
            continue
        service, operation = command[8:10]
        name = "".join(part.title() for part in operation.split("-"))
        members = (
            session.get_service_model(service).operation_model(name).input_shape.members
        )
        expected = {"--" + re_name(key) for key in members}
        options = {part for part in command[10:] if part.startswith("--")}
        assert options - {"--output", "--region", "--no-paginate"} <= expected


def re_name(value):
    import re

    return re.sub(r"(?<!^)(?=[A-Z][a-z]|[A-Z]$)", "-", value).lower()


@pytest.mark.parametrize("operation", ["instances", "get-resources"])
def test_pagination_reads_all_pages_under_selected_identity(operation):
    producer = CensusProducer(records_for())

    def pages(current, scope, result, token):
        if current != operation or scope != "tags":
            return result
        key = "PaginationToken" if current == "get-resources" else "NextToken"
        if token:
            if current == "instances":
                result["Reservations"][0]["Instances"][0]["InstanceId"] = "i-22222222"
            else:
                result["ResourceTagMappingList"][0]["ResourceARN"] = arn(
                    "kms", "key", "second-unresolved-key"
                )
        else:
            result[key] = "next-page"
        return result

    producer.change = pages
    census = run_observation(producer.records, producer)["provider"]["discovery"]
    assert census["status"] == "OBSERVED"
    assert (operation, "tags", "next-page") in producer.discovery_calls


@pytest.mark.parametrize(
    "failure",
    [
        "denied",
        "timeout",
        "missing-list",
        "foreign-vpc",
        "foreign-tag",
        "foreign-owner",
        "wrong-id",
        "duplicate-id",
        "repeated-token",
        "invalid-token",
        "unexpected-token",
        "missing-attachment",
        "ambiguous-attachment",
        "foreign-arn",
        "malformed-arn",
        "missing-tags",
        "survivor",
        "observed-absent",
        "changed-state",
        "resource-limit",
        "page-limit",
        "wrong-role",
    ],
)
def test_incomplete_or_foreign_census_never_reports_complete_inventory(
    failure, monkeypatch
):
    producer = CensusProducer(records_for())
    if failure == "page-limit":
        monkeypatch.setattr(demo1_discovery, "MAX_PAGES", 1)

    def corrupt(operation, scope, result, token):
        if operation == "instances" and scope == "tags":
            if failure in ("denied", "timeout"):
                return failure
            if failure == "missing-list":
                return {}
            instance = result["Reservations"][0]["Instances"][0]
            if failure == "foreign-tag":
                instance["Tags"][0]["Value"] = "foreign-organization"
            if failure == "foreign-owner":
                result["Reservations"][0]["OwnerId"] = "000000000000"
            if failure == "wrong-id":
                instance["InstanceId"] = "i-invalid"
            if failure == "duplicate-id":
                result["Reservations"][0]["Instances"].append(copy.deepcopy(instance))
            if failure == "repeated-token":
                result["NextToken"] = "repeated"
            if failure == "invalid-token":
                result["NextToken"] = 42
            if failure == "unexpected-token":
                result["PaginationToken"] = "foreign-paginator"
            if failure == "resource-limit":
                result["Reservations"][0]["Instances"] *= 201
            if failure == "wrong-role":
                producer.wrong_role = True
        if operation == "instances" and scope == "vpc":
            if failure == "foreign-vpc":
                result["Reservations"][0]["Instances"][0]["VpcId"] = "vpc-99999999"
            if failure == "changed-state":
                result["Reservations"][0]["Instances"][0]["State"]["Name"] = (
                    "terminated"
                )
        if operation == "volumes" and scope == "attachment":
            if failure == "missing-attachment":
                result["Volumes"] = []
            if failure == "ambiguous-attachment":
                result["Volumes"] *= 2
        if operation == "get-resources":
            if failure == "malformed-arn":
                result["ResourceTagMappingList"][0]["ResourceARN"] += "\n"
            if failure == "foreign-arn":
                result["ResourceTagMappingList"][0]["ResourceARN"] = arn(
                    "kms", "key", "foreign"
                ).replace("123456789012", "000000000000")
            if failure == "missing-tags":
                result["ResourceTagMappingList"][0]["Tags"] = []
            if failure == "survivor":
                result["ResourceTagMappingList"][0]["ResourceARN"] = (
                    producer.records.selected["survivors"][0]
                )
            if failure == "observed-absent":
                result["ResourceTagMappingList"][0]["ResourceARN"] = (
                    producer.runtime.observation["owned_resources"][0]
                )
        return result

    if failure == "observed-absent":
        producer.absent.add(producer.runtime.observation["owned_resources"][0])
    producer.change = corrupt
    report = run_observation(producer.records, producer)
    assert report["provider"]["discovery"] == {
        "status": "BLOCKED",
        "listing_complete": False,
        "inventory_complete": False,
    }
    assert "owned_absent_refs" not in report["provider"]
    assert report["checks"]["cleanup"]["status"] == "BLOCKED"
    assert "private operator" not in json.dumps(report)


def test_supplied_network_never_triggers_broad_vpc_census():
    records = records_for()
    metadata = json.loads(records.rows[records.applied]["artifact_metadata_json"])
    metadata["outputs"]["network_ownership"]["value"] = "supplied"
    records.rewrite(
        records.applied,
        "artifact_metadata_json",
        json.dumps(metadata, sort_keys=True, separators=(",", ":")),
    )
    producer = CensusProducer(records)

    def omit_preserved_vpc(operation, scope, result, token):
        return {"Vpcs": []} if operation == "vpcs" else result

    producer.change = omit_preserved_vpc
    census = run_observation(records, producer)["provider"]["discovery"]
    assert census["status"] == "OBSERVED" and census["owned_vpc_scanned"] is False
    assert not any(scope == "vpc" for _, scope, _ in producer.discovery_calls)


def test_empty_census_never_turns_partial_absence_into_cleanup():
    records = records_for()
    producer = CurrentProducer(records)
    producer.absent.update(producer.runtime.observation["owned_resources"])
    report = run_observation(records, producer)
    census = report["provider"]["discovery"]
    assert census["resources"] == [] and census["listing_complete"] is True
    assert census["inventory_complete"] is False
    assert report["checks"]["cleanup"]["status"] == "BLOCKED"


def test_census_exhaustion_stops_reads_within_the_shared_runtime_budget():
    producer = CensusProducer(records_for())
    elapsed = [0]

    def exhaust_budget(operation, scope, result, token):
        elapsed[0] = 901
        return result

    producer.change = exhaust_budget
    report = run_observation(producer.records, producer, monotonic=lambda: elapsed[0])
    assert producer.discovery_calls == [("instances", "tags", None)]
    assert report["provider"]["discovery"] == {
        "status": "BLOCKED",
        "listing_complete": False,
        "inventory_complete": False,
    }
    assert "owned_absent_refs" not in report["provider"]
    assert report["checks"]["cleanup"]["status"] == "BLOCKED"
