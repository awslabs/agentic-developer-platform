"""Real selected-role census and exact-ID adapters over synthetic AWS transport."""

import copy
import json
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from test_demo1_cli import fixture_documents, identifier

from superplane_acceptance.demo1_browser import CreationCheckpoint
from superplane_acceptance.demo1_evidence import DemoInput, EvidenceError
from superplane_acceptance.demo1_live import LiveEnvelope
from superplane_acceptance.demo1_removal_provider import (
    SCOPE,
    capture_provider_baseline,
    validate_provider_baseline,
    verify_provider_removal,
)
from superplane_acceptance.demo1_report import reference
from workspace_provisioning.artifacts import digest

# Provider-shaped replies independently cover each census and exact lookup kind.
OPERATIONS = {
    "instances": (
        "Reservations",
        "InstanceId",
        "instance",
        "i-11111111",
        "InvalidInstanceID.NotFound",
    ),
    "volumes": (
        "Volumes",
        "VolumeId",
        "volume",
        "vol-11111111",
        "InvalidVolume.NotFound",
    ),
    "snapshots": (
        "Snapshots",
        "SnapshotId",
        "snapshot",
        "snap-11111111",
        "InvalidSnapshot.NotFound",
    ),
    "network-interfaces": (
        "NetworkInterfaces",
        "NetworkInterfaceId",
        "network-interface",
        "eni-11111111",
        "InvalidNetworkInterfaceID.NotFound",
    ),
    "addresses": (
        "Addresses",
        "AllocationId",
        "elastic-ip",
        "eipalloc-11111111",
        "InvalidAllocationID.NotFound",
    ),
    "nat-gateways": (
        "NatGateways",
        "NatGatewayId",
        "natgateway",
        "nat-11111111",
        "NatGatewayNotFound",
    ),
    "route-tables": (
        "RouteTables",
        "RouteTableId",
        "route-table",
        "rtb-11111111",
        "InvalidRouteTableID.NotFound",
    ),
    "internet-gateways": (
        "InternetGateways",
        "InternetGatewayId",
        "internet-gateway",
        "igw-11111111",
        "InvalidInternetGatewayID.NotFound",
    ),
    "vpc-endpoints": (
        "VpcEndpoints",
        "VpcEndpointId",
        "vpc-endpoint",
        "vpce-11111111",
        "InvalidVpcEndpointId.NotFound",
    ),
    "launch-templates": (
        "LaunchTemplates",
        "LaunchTemplateId",
        "launch-template",
        "lt-11111111",
        "InvalidLaunchTemplateId.NotFound",
    ),
    "subnets": (
        "Subnets",
        "SubnetId",
        "subnet",
        "subnet-11111111",
        "InvalidSubnetID.NotFound",
    ),
    "security-groups": (
        "SecurityGroups",
        "GroupId",
        "security-group",
        "sg-11111111",
        "InvalidGroup.NotFound",
    ),
    "vpcs": ("Vpcs", "VpcId", "vpc", "vpc-11111111", "InvalidVpcID.NotFound"),
}
KEY = "arn:aws:kms:us-east-1:123456789012:key/11111111-2222-3333-4444-555555555555"
NOW = datetime.fromisoformat("2026-10-05T11:30:00+00:00")


def arn(kind, name):
    service = "eks" if kind == "cluster" else "ec2"
    return f"arn:aws:{service}:us-east-1:123456789012:{kind}/{name}"


class Provider:
    def __init__(self, selected, envelope, checkpoint):
        self.selected, self.envelope, self.checkpoint = selected, envelope, checkpoint
        self.calls, self.absent, self.terminal, self.hidden = [], set(), set(), set()
        self.fault = None
        self.change = lambda operation, listing, result: result
        self.tags = [
            {"Key": "OrgId", "Value": selected.org_id},
            {"Key": "WorkspaceId", "Value": checkpoint.workspace_id},
        ]
        self.tagged = []
        self.plan = {}
        for suffix in ("-cluster-role", "-node-role", "-vpc-cni-role", "-admin"):
            key = "arn:aws:iam::123456789012:role/example-owned" + suffix
            self.plan[key] = {
                "Arn": key,
                "RoleName": "example-owned" + suffix,
                "Tags": self.tags,
            }
        self.plan[KEY] = {
            "Arn": KEY,
            "AWSAccountId": selected.account,
            "Enabled": True,
            "KeyState": "Enabled",
        }
        self.oidc = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
        self.plan[self.oidc] = {
            "Url": "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE",
            "Tags": self.tags,
        }
        self.log = "arn:aws:logs:us-east-1:123456789012:log-group:/aws/eks/example-owned/cluster"
        self.plan[self.log] = {
            "logGroupName": "/aws/eks/example-owned/cluster",
            "arn": self.log + ":*",
            "kmsKeyId": KEY,
        }
        self.group = "arn:aws:autoscaling:us-east-1:123456789012:autoScalingGroup:11111111-2222-3333-4444-555555555555:autoScalingGroupName/eks-example-owned-default-EXAMPLE"
        self.plan[self.group] = {
            "AutoScalingGroupARN": self.group,
            "AutoScalingGroupName": "eks-example-owned-default-EXAMPLE",
        }
        self.node = "arn:aws:eks:us-east-1:123456789012:nodegroup/example-owned/example-owned-default/EXAMPLE"
        self.plan[self.node] = {
            "nodegroupArn": self.node,
            "nodeRole": "arn:aws:iam::123456789012:role/example-owned-node-role",
            "launchTemplate": {"id": "lt-11111111"},
            "resources": {
                "autoScalingGroups": [{"name": "eks-example-owned-default-EXAMPLE"}]
            },
        }
        self.addon = (
            "arn:aws:eks:us-east-1:123456789012:addon/example-owned/vpc-cni/EXAMPLE"
        )
        self.plan[self.addon] = {
            "addonArn": self.addon,
            "serviceAccountRoleArn": "arn:aws:iam::123456789012:role/example-owned-vpc-cni-role",
        }
        self.profile = "arn:aws:iam::123456789012:instance-profile/eks-example-owned"
        self.plan[self.profile] = {
            "Arn": self.profile,
            "Roles": [
                {"Arn": "arn:aws:iam::123456789012:role/example-owned-node-role"}
            ],
        }
        self.rows = {}
        for operation, (_, field, _, name, _) in OPERATIONS.items():
            row = {field: name, "Tags": self.tags, "OwnerId": selected.account}
            if operation in {
                "instances",
                "network-interfaces",
                "nat-gateways",
                "route-tables",
                "vpc-endpoints",
                "subnets",
                "security-groups",
            }:
                row["VpcId"] = "vpc-11111111"
            if operation == "internet-gateways":
                row["Attachments"] = [{"VpcId": "vpc-11111111"}]
            if operation == "instances":
                row["State"] = {"Name": "running"}
                row["BlockDeviceMappings"] = [{"Ebs": {"VolumeId": "vol-11111111"}}]
            if operation == "network-interfaces":
                row["Association"] = {"AllocationId": "eipalloc-11111111"}
            if operation == "nat-gateways":
                row["State"] = "available"
            self.rows[operation] = row

    def __call__(self, command, **options):
        assert command[:8] == [
            "adp-cred",
            "assume",
            "--service",
            "aws",
            "--label",
            self.envelope.broker_label,
            "--exec",
            "aws",
        ]
        assert 0 < options["timeout"] <= 30
        self.calls.append(command)
        service, operation = command[8:10]
        if self.fault == "timeout":
            raise subprocess.TimeoutExpired(command, options["timeout"])
        if self.fault == "denied":
            return subprocess.CompletedProcess(
                command, 254, "", "private operator credential diagnostic"
            )
        if service == "sts":
            role = "ForeignRole" if self.fault == "wrong-role" else self.selected.role
            result = {
                "Account": self.selected.account,
                "Arn": f"arn:aws:sts::{self.selected.account}:assumed-role/{role}/session",
            }
            if self.fault == "wrong-account":
                result["Account"] = "000000000002"
        else:
            assert self.calls[-2][8:10] == ["sts", "get-caller-identity"]
            assert "--no-paginate" in command and command[-2:] == ["--output", "json"]
            listing = (
                "--filters" in command
                or "--filter" in command
                or service == "resourcegroupstaggingapi"
            )
            if operation == "get-resources":
                result = {"ResourceTagMappingList": copy.deepcopy(self.tagged)}
            elif operation == "describe-cluster":
                name = command[11]
                key = arn("cluster", name)
                if key in self.absent:
                    return self.missing(
                        command, "ResourceNotFoundException", "DescribeCluster", name
                    )
                result = {
                    "cluster": {
                        "arn": key,
                        "roleArn": "arn:aws:iam::123456789012:role/"
                        + name
                        + "-cluster-role",
                        "tags": {tag["Key"]: tag["Value"] for tag in self.tags},
                        "encryptionConfig": [{"provider": {"keyArn": KEY}}],
                        "identity": {
                            "oidc": {
                                "issuer": "https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
                            }
                        },
                    }
                }
            elif service in {"iam", "kms", "logs", "autoscaling", "eks"}:
                return self.plan_response(command)
            else:
                operation = operation.removeprefix("describe-")
                collection, field, kind, name, error = OPERATIONS[operation]
                if not listing:
                    name = command[11]
                key = arn(kind, name)
                if not listing and key in self.absent:
                    api = "Describe" + "".join(
                        part.title() for part in operation.split("-")
                    )
                    return self.missing(command, error, api, name)
                rows = []
                if not listing or key not in self.absent | self.hidden:
                    row = copy.deepcopy(self.rows[operation])
                    row[field] = name
                    if key in self.terminal:
                        if kind == "instance":
                            row["State"] = {"Name": "terminated"}
                            row["BlockDeviceMappings"] = []
                        elif kind == "natgateway":
                            row["State"] = "deleted"
                    rows = [row]
                result = {collection: rows}
                if operation == "instances":
                    result = {
                        collection: [
                            {"OwnerId": self.selected.account, "Instances": rows}
                        ]
                        if rows
                        else []
                    }
            result = self.change(operation, listing, result)
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

    def plan_response(self, command):
        service, operation = command[8:10]
        field = None
        if operation == "list-nodegroups":
            result = {"nodegroups": ["example-owned-default"]}
        elif operation == "list-addons":
            result = {"addons": ["vpc-cni"]}
        elif operation == "list-instance-profiles-for-role":
            result = {
                "InstanceProfiles": [self.plan[self.profile]],
                "IsTruncated": False,
            }
        elif operation == "describe-log-groups":
            result = {
                "logGroups": [] if self.log in self.absent else [self.plan[self.log]]
            }
        elif operation == "describe-auto-scaling-groups":
            result = {
                "AutoScalingGroups": []
                if self.group in self.absent
                else [self.plan[self.group]]
            }
        else:
            if operation == "get-role":
                key, field = "arn:aws:iam::123456789012:role/" + command[11], "Role"
            elif operation == "describe-key":
                key, field = command[11], "KeyMetadata"
            elif operation == "get-open-id-connect-provider":
                key = command[11]
            elif operation == "get-instance-profile":
                key, field = self.profile, "InstanceProfile"
            elif operation == "describe-nodegroup":
                key, field = self.node, "nodegroup"
            elif operation == "describe-addon":
                key, field = self.addon, "addon"
            else:
                raise AssertionError(operation)
            if key in self.absent:
                error = (
                    "NoSuchEntity"
                    if service == "iam"
                    else "NotFoundException"
                    if service == "kms"
                    else "ResourceNotFoundException"
                )
                api = (
                    "GetOpenIDConnectProvider"
                    if operation == "get-open-id-connect-provider"
                    else "".join(part.title() for part in operation.split("-"))
                )
                return self.missing(command, error, api, command[11])
            row = self.plan[key]
            result = {field: row} if field else row
        result = self.change(
            operation, operation.startswith("list-"), copy.deepcopy(result)
        )
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

    def missing(self, command, error, api, name):
        if self.fault == "wrong-missing-id":
            name += "0"
        if self.fault == "wrong-missing-api":
            api = "DeleteSomething"
        if self.fault == "wrong-missing-type":
            error = "AccessDeniedException"
        message = f"An error occurred ({error}) when calling the {api} operation: Resource '{name}' was not found"
        return subprocess.CompletedProcess(command, 254, "", message)


@pytest.fixture
def setup():
    document, _ = fixture_documents()
    document["survivors"] = [arn("cluster", "example-peer"), KEY]
    selected = DemoInput.parse(document)
    envelope = LiveEnvelope(
        "https://example.invalid",
        "example-connection",
        identifier(20),
        900,
        selected.deadline + timedelta(hours=1),
    )
    checkpoint = CreationCheckpoint(
        selected.request_id,
        identifier(6),
        selected.plan_revision,
        identifier(8),
        identifier(9),
        True,
    )
    ownership = {
        "version": 1,
        "status": "OBSERVED",
        "org_id": selected.org_id,
        "workspace_id": checkpoint.workspace_id,
        "request_id": selected.request_id,
        "plan_revision": selected.plan_revision,
        "region": selected.region,
        "account_id": selected.account,
        "artifact_id": "d" * 64,
        "current_operation_id": identifier(11),
        "recorded_at": NOW.isoformat(),
        "inventory_complete": False,
        "owned_resources": [
            arn("cluster", "example-owned"),
            arn("vpc", "vpc-11111111"),
        ],
        "preserved_resources": [],
    }
    provider = Provider(selected, envelope, checkpoint)
    return selected, envelope, checkpoint, ownership, provider


def capture(setup):
    selected, envelope, checkpoint, ownership, provider = setup
    return capture_provider_baseline(
        selected,
        envelope,
        checkpoint,
        ownership,
        900,
        runner=provider,
        clock=lambda: NOW,
    )


def verify(setup, baseline, **options):
    selected, envelope, checkpoint, _, provider = setup
    return verify_provider_removal(
        selected,
        envelope,
        checkpoint,
        baseline,
        900,
        runner=provider,
        clock=lambda: NOW,
        **options,
    )


def removed(setup, baseline):
    provider = setup[-1]
    provider.absent.update(baseline["expected_removed"])
    provider.calls.clear()


def test_saved_baseline_compares_every_kind_by_exact_id_and_surviving_peer(setup):
    baseline = json.loads(json.dumps(capture(setup)))
    assert len(baseline["expected_removed"]) == 24
    assert baseline["census"]["inventory_complete"] is False
    removed(setup, baseline)
    report = verify(setup, baseline)
    assert report["status"] == "OBSERVED" and report["inventory_complete"] is True
    assert report["checks"] == {
        "owned_absence": {"status": "PASS"},
        "survivors": {"status": "PASS"},
    }
    assert (
        report["inventory_scope"] == SCOPE
        and report["full_inventory_complete"] is False
    )
    assert report["cost_usd"] is None
    assert len(report["absent_refs"]) == 24
    serialized = json.dumps(report)
    for private in (
        setup[0].account,
        setup[2].workspace_id,
        "example-owned",
        "vpc-11111111",
        "example-peer",
    ):
        assert private not in serialized
    assert all(
        command[9].startswith(("describe-", "get-", "list-"))
        or command[9] in {"get-caller-identity", "get-resources"}
        for command in setup[-1].calls
    )


@pytest.mark.parametrize(
    "kind,name", [("instance", "i-11111111"), ("natgateway", "nat-11111111")]
)
def test_terminal_records_are_reported_separately_from_absence(setup, kind, name):
    baseline = capture(setup)
    removed(setup, baseline)
    key = arn(kind, name)
    provider = setup[-1]
    provider.absent.remove(key)
    provider.terminal.add(key)
    report = verify(setup, baseline)
    assert report["checks"]["owned_absence"]["status"] == "PASS"
    assert report["terminal_refs"] == [reference(key)]
    assert reference(key) not in report["absent_refs"] and report["cost_usd"] is None


@pytest.mark.parametrize(
    "kind,name",
    [
        ("volume", "vol-11111111"),
        ("elastic-ip", "eipalloc-11111111"),
        ("snapshot", "snap-11111111"),
    ],
)
def test_lingering_resource_cannot_hide_by_losing_tags_or_attachments(
    setup, kind, name
):
    baseline = capture(setup)
    removed(setup, baseline)
    key = arn(kind, name)
    setup[-1].absent.remove(key)
    setup[-1].hidden.add(key)
    report = verify(setup, baseline)
    assert report["status"] == "OBSERVED"
    assert report["checks"]["owned_absence"]["status"] == "FAIL"
    assert report["remaining_refs"] == [reference(key)]
    assert report["checks"]["survivors"]["status"] == "PASS"


def test_missing_peer_fails_without_claiming_it_was_removed_by_this_operation(setup):
    baseline = capture(setup)
    removed(setup, baseline)
    setup[-1].absent.add(setup[0].survivors[0])
    report = verify(setup, baseline)
    assert report["checks"]["survivors"]["status"] == "FAIL"
    assert report["missing_survivor_refs"] == [reference(setup[0].survivors[0])]


@pytest.mark.parametrize(
    "fault",
    [
        "timeout",
        "denied",
        "wrong-role",
        "wrong-account",
        "wrong-missing-id",
        "wrong-missing-api",
        "wrong-missing-type",
    ],
)
def test_unknown_provider_outcomes_never_prove_absence(setup, fault):
    baseline = capture(setup)
    removed(setup, baseline)
    setup[-1].fault = fault
    report = verify(setup, baseline)
    assert report["status"] == "BLOCKED" and report["inventory_complete"] is False
    assert all(value["status"] == "BLOCKED" for value in report["checks"].values())
    assert "private operator" not in json.dumps(report)


@pytest.mark.parametrize(
    "failure",
    [
        "vpc",
        "compute",
        "storage",
        "peer",
        "unsupported",
        "cycle",
        "owner",
        "overlap",
        "foreign",
        "empty-success",
        "paginated-exact",
    ],
)
def test_incomplete_baseline_refused_before_any_approval(setup, failure):
    ownership, provider = setup[-2:]
    if failure == "vpc":
        ownership["owned_resources"].pop()
    elif failure in {"compute", "storage"}:
        operation = "instances" if failure == "compute" else "volumes"
        provider.change = (
            lambda op, listing, result: {OPERATIONS[op][0]: []}
            if op == operation
            else result
        )
    elif failure == "peer":
        provider.absent.add(setup[0].survivors[0])
    elif failure == "unsupported":
        provider.tagged = [
            {
                "ResourceARN": "arn:aws:kms:us-east-1:123456789012:key/example",
                "Tags": provider.tags,
            }
        ]
    elif failure == "cycle":
        provider.change = (
            lambda op, listing, result: {**result, "NextToken": "same"}
            if op == "snapshots" and listing
            else result
        )
    elif failure == "owner":
        provider.rows["volumes"]["OwnerId"] = "000000000002"
    elif failure == "overlap":
        ownership["owned_resources"].append(setup[0].survivors[0])
    elif failure == "foreign":
        ownership["owned_resources"][0] = ownership["owned_resources"][0].replace(
            "123456789012", "000000000002"
        )
    elif failure == "empty-success":
        provider.change = (
            lambda op, listing, result: {"cluster": {}}
            if op == "describe-cluster"
            else result
        )
    else:
        provider.change = (
            lambda op, listing, result: {**result, "NextToken": "truncated"}
            if not listing
            else result
        )
    with pytest.raises(EvidenceError):
        capture(setup)


def test_new_resource_after_baseline_blocks_and_does_not_refresh_baseline(setup):
    baseline = capture(setup)
    before = json.dumps(baseline, sort_keys=True)
    removed(setup, baseline)
    provider = setup[-1]
    provider.change = (
        lambda op, listing, result: {
            "Volumes": [{"VolumeId": "vol-22222222", "Tags": provider.tags}]
        }
        if op == "volumes" and listing
        else result
    )
    report = verify(setup, baseline)
    assert report["status"] == "BLOCKED"
    assert json.dumps(baseline, sort_keys=True) == before


@pytest.mark.parametrize(
    "failure",
    [
        "hash",
        "scope",
        "retirement",
        "window",
        "shape",
        "census-kind",
        "census-basis",
        "census-resources",
        "global-complete",
        "survivors",
        "states",
        "compute",
    ],
)
def test_changed_or_malformed_saved_baseline_blocks_before_provider_reads(
    setup, failure
):
    baseline = capture(setup)
    if failure == "hash":
        baseline["baseline_sha256"] = "f" * 64
    elif failure == "scope":
        baseline["scope"]["connection_id"] = identifier(99)
    elif failure == "retirement":
        baseline["scope"]["retirement_request_id"] = identifier(99)
    elif failure == "window":
        baseline["observed_at"] = (NOW + timedelta(minutes=1)).isoformat()
    elif failure == "shape":
        baseline["unexpected"] = "private detail"
    elif failure == "census-kind":
        baseline["census"]["resources"][arn("volume", "vol-11111111")]["kind"] = (
            "snapshot"
        )
    elif failure == "census-basis":
        baseline["census"]["resources"][arn("volume", "vol-11111111")]["basis"] = [
            "invented"
        ]
    elif failure == "census-resources":
        baseline["census"]["resources"] = []
    elif failure == "global-complete":
        baseline["full_inventory_complete"] = True
    elif failure == "survivors":
        baseline["survivors"] = []
    elif failure == "states":
        baseline["resource_states"] = []
    else:
        baseline["census"]["resources"].pop(arn("instance", "i-11111111"))
    if failure != "hash":
        baseline["baseline_sha256"] = digest(
            {k: v for k, v in baseline.items() if k != "baseline_sha256"}
        )
    setup[-1].calls.clear()
    assert verify(setup, baseline)["status"] == "BLOCKED"
    with pytest.raises(EvidenceError):
        validate_provider_baseline(*setup[:3], baseline, clock=lambda: NOW)
    assert setup[-1].calls == []


def test_authorization_and_monotonic_runtime_limits_stop_reads(setup):
    baseline = capture(setup)
    setup[-1].calls.clear()
    selected = replace(setup[0], deadline=NOW)
    assert verify((selected, *setup[1:]), baseline)["status"] == "BLOCKED"
    assert setup[-1].calls == []
    ticks = iter((0, 899, 901))
    report = verify(setup, baseline, monotonic=lambda: next(ticks))
    assert report["status"] == "BLOCKED" and setup[-1].calls == []


def test_exact_lookup_arguments_match_installed_aws_models(setup):
    from botocore.session import get_session

    capture(setup)
    session = get_session()
    for command in setup[-1].calls:
        service, operation = command[8:10]
        api = (
            "GetOpenIDConnectProvider"
            if operation == "get-open-id-connect-provider"
            else "".join(part.title() for part in operation.split("-"))
        )
        members = (
            session.get_service_model(service).operation_model(api).input_shape.members
        )

        def cli_name(value):
            from botocore import xform_name

            return "--" + xform_name(value, "-")

        expected = {cli_name(key) for key in members}
        options = {value for value in command[10:] if value.startswith("--")}
        assert options - {"--output", "--region", "--no-paginate"} <= expected


@pytest.mark.parametrize(
    "failure", ["empty", "duplicate", "owner", "identity", "pagination", "wrong-shape"]
)
def test_malformed_exact_read_of_recorded_extra_resource_blocks_removal(setup, failure):
    baseline = capture(setup)
    removed(setup, baseline)
    provider = setup[-1]
    provider.absent.remove(arn("snapshot", "snap-11111111"))
    provider.hidden.add(arn("snapshot", "snap-11111111"))

    def changed(operation, listing, result):
        if operation != "snapshots" or listing:
            return result
        if failure == "empty":
            result["Snapshots"] = []
        elif failure == "duplicate":
            result["Snapshots"] *= 2
        elif failure == "owner":
            result["Snapshots"][0]["OwnerId"] = "000000000002"
        elif failure == "identity":
            result["Snapshots"][0]["SnapshotId"] = "snap-22222222"
        elif failure == "pagination":
            result["NextToken"] = "unfinished"
        else:
            result["Snapshots"] = {}
        return result

    provider.change = changed
    assert verify(setup, baseline)["status"] == "BLOCKED"


def test_provider_census_follows_pages_before_recording_baseline(setup):
    pages = []

    def changed(operation, listing, result):
        if operation == "snapshots" and listing:
            pages.append(operation)
            if len(pages) == 1:
                return {"Snapshots": [], "NextToken": "next-page"}
        return result

    setup[-1].change = changed
    baseline = capture(setup)
    assert len(pages) == 2
    assert arn("snapshot", "snap-11111111") in baseline["expected_removed"]
    assert any(
        "--next-token" in command and "next-page" in command
        for command in setup[-1].calls
    )


@pytest.mark.parametrize(
    "resource", ["role", "oidc", "log", "node", "addon", "group", "profile"]
)
def test_maintained_plan_leftovers_fail_even_when_no_tag_census_finds_them(
    setup, resource
):
    baseline = capture(setup)
    removed(setup, baseline)
    provider = setup[-1]
    key = (
        "arn:aws:iam::123456789012:role/example-owned-node-role"
        if resource == "role"
        else getattr(provider, resource)
    )
    provider.absent.remove(key)
    report = verify(setup, baseline)
    assert report["status"] == "OBSERVED"
    assert report["checks"]["owned_absence"]["status"] == "FAIL"
    assert report["remaining_refs"] == [reference(key)]


@pytest.mark.parametrize("state", ["missing", "disabled", "pending-deletion"])
def test_supplied_key_must_survive_and_remain_enabled(setup, state):
    baseline = capture(setup)
    removed(setup, baseline)
    provider = setup[-1]
    if state == "missing":
        provider.absent.add(KEY)
    else:
        provider.plan[KEY].update(
            Enabled=False,
            KeyState="Disabled" if state == "disabled" else "PendingDeletion",
        )
    report = verify(setup, baseline)
    assert report["checks"]["owned_absence"]["status"] == "PASS"
    assert report["checks"]["survivors"]["status"] == "FAIL"
    assert report["missing_survivor_refs"] == [reference(KEY)]
    assert KEY not in json.dumps(report)


def test_tag_census_reconciles_supported_plan_resources_without_discarding_them(setup):
    provider = setup[-1]
    provider.tagged = [
        {"ResourceARN": key, "Tags": provider.tags}
        for key in (provider.log + ":*", provider.node, provider.addon, KEY)
    ]
    baseline = capture(setup)
    assert len(baseline["census"]["unresolved_resources"]) == 4
    removed(setup, baseline)
    # The provider can temporarily retain stale tag mappings, but exact-ID checks
    # still independently establish absence and the required retained survivor.
    report = verify(setup, baseline)
    assert report["checks"]["owned_absence"]["status"] == "PASS"
    assert report["checks"]["survivors"]["status"] == "PASS"


@pytest.mark.parametrize(
    "operation", ["nat-gateways", "vpc-endpoints", "launch-templates"]
)
def test_complete_exact_id_empty_lists_are_absent_for_supported_list_apis(
    setup, operation
):
    baseline = capture(setup)
    removed(setup, baseline)
    provider = setup[-1]
    collection, _, kind, name, _ = OPERATIONS[operation]
    key = arn(kind, name)
    provider.absent.remove(key)
    provider.hidden.add(key)
    provider.change = (
        lambda op, listing, result: {collection: []}
        if op == operation and not listing
        else result
    )
    report = verify(setup, baseline)
    assert report["checks"]["owned_absence"]["status"] == "PASS"
    assert reference(key) in report["absent_refs"]


@pytest.mark.parametrize(
    "failure",
    [
        "key-not-selected",
        "key-disabled",
        "cluster-key",
        "log-key",
        "role-tags",
        "node-role",
        "addon-role",
        "oidc-url",
        "no-default",
        "no-cni",
        "profile-role",
        "malformed-cluster",
    ],
)
def test_actual_plan_relationships_are_required_before_removal_approval(setup, failure):
    provider = setup[-1]
    if failure == "key-not-selected":
        setup = (replace(setup[0], survivors=(setup[0].survivors[0],)), *setup[1:])
    elif failure == "key-disabled":
        provider.plan[KEY].update(Enabled=False, KeyState="Disabled")
    elif failure == "cluster-key":

        def change(op, listing, result):
            if op == "describe-cluster":
                result["cluster"]["encryptionConfig"] = [
                    {"provider": {"keyArn": KEY + "different"}}
                ]
            return result

        provider.change = change
    elif failure == "log-key":
        provider.plan[provider.log]["kmsKeyId"] = KEY + "different"
    elif failure == "role-tags":
        provider.plan["arn:aws:iam::123456789012:role/example-owned-node-role"][
            "Tags"
        ] = []
    elif failure == "node-role":
        provider.plan[provider.node]["nodeRole"] += "foreign"
    elif failure == "addon-role":
        provider.plan[provider.addon]["serviceAccountRoleArn"] += "foreign"
    elif failure == "oidc-url":
        provider.plan[provider.oidc]["Url"] += "foreign"
    elif failure == "no-default":
        provider.change = (
            lambda op, listing, result: {"nodegroups": []}
            if op == "list-nodegroups"
            else result
        )
    elif failure == "no-cni":
        provider.change = (
            lambda op, listing, result: {"addons": []}
            if op == "list-addons"
            else result
        )
    elif failure == "profile-role":
        provider.plan[provider.profile]["Roles"] = []
    else:

        def change(op, listing, result):
            if op == "describe-cluster":
                result["cluster"]["identity"] = None
            return result

        provider.change = change
    with pytest.raises(EvidenceError):
        capture(setup)


def test_optional_unconfigured_admin_is_saved_as_absent_and_cannot_appear_later(setup):
    key = "arn:aws:iam::123456789012:role/example-owned-admin"
    setup[-1].absent.add(key)
    baseline = capture(setup)
    assert baseline["resource_states"][key] == "absent"
    removed(setup, baseline)
    assert verify(setup, baseline)["checks"]["owned_absence"]["status"] == "PASS"
    setup[-1].absent.remove(key)
    assert verify(setup, baseline)["checks"]["owned_absence"]["status"] == "FAIL"


def test_documented_provider_scope_covers_current_terraform_resource_types():
    import re
    from pathlib import Path

    terraform = Path(__file__).resolve().parents[2] / "infra" / "workspaces"
    declared = {
        kind
        for path in terraform.glob("*.tf")
        for kind in re.findall(r'^resource "([^"]+)"', path.read_text(), re.MULTILINE)
    }
    assert declared == {
        "terraform_data",
        "aws_iam_role",
        "aws_iam_role_policy_attachment",
        "aws_iam_role_policy",
        "aws_iam_openid_connect_provider",
        "aws_kms_key",
        "aws_kms_alias",
        "aws_cloudwatch_log_group",
        "aws_security_group",
        "aws_vpc_security_group_egress_rule",
        "aws_vpc_security_group_ingress_rule",
        "aws_eks_cluster",
        "aws_eks_node_group",
        "aws_eks_addon",
        "aws_launch_template",
        "aws_vpc",
        "aws_subnet",
        "aws_eip",
        "aws_nat_gateway",
        "aws_internet_gateway",
        "aws_route_table",
        "aws_route_table_association",
        "aws_default_security_group",
        "aws_vpc_endpoint",
    }
    source = (terraform / "iam.tf").read_text()
    for suffix in ("-cluster-role", "-node-role", "-vpc-cni-role", "-admin"):
        assert "${local.name_prefix}" + suffix in source
    assert "cluster_name = local.name_prefix" in (terraform / "main.tf").read_text()


@pytest.mark.parametrize(
    "failure",
    [
        "logs-wrong-token",
        "eks-wrong-token",
        "iam-no-marker",
        "iam-cycle",
        "logs-no-arn",
    ],
)
def test_plan_provider_malformed_or_unfinished_pages_refuse_baseline(setup, failure):
    def change(operation, listing, result):
        if failure == "logs-wrong-token" and operation == "describe-log-groups":
            result["NextToken"] = "wrong-casing"
        elif failure == "eks-wrong-token" and operation == "list-nodegroups":
            result["PaginationToken"] = "wrong-api"
        elif (
            failure.startswith("iam-")
            and operation == "list-instance-profiles-for-role"
        ):
            result["IsTruncated"] = True
            if failure == "iam-cycle":
                result["Marker"] = "same-page"
        elif failure == "logs-no-arn" and operation == "describe-log-groups":
            result["logGroups"][0]["arn"] = None
        return result

    setup[-1].change = change
    with pytest.raises(EvidenceError):
        capture(setup)


@pytest.mark.parametrize(
    "operation",
    [
        "get-role",
        "get-open-id-connect-provider",
        "describe-log-groups",
        "describe-auto-scaling-groups",
        "describe-key",
        "describe-nodegroup",
    ],
)
def test_denied_plan_exact_reads_never_establish_removal_or_survival(setup, operation):
    baseline = capture(setup)
    removed(setup, baseline)
    provider = setup[-1]

    def denied(command, **options):
        if command[9] == operation:
            return subprocess.CompletedProcess(
                command, 254, "", "private credential diagnostic"
            )
        return provider(command, **options)

    report = verify_provider_removal(
        *setup[:3], baseline, 900, runner=denied, clock=lambda: NOW
    )
    assert report["status"] == "BLOCKED"
    assert all(check["status"] == "BLOCKED" for check in report["checks"].values())
    assert "private credential" not in json.dumps(report)


def test_saved_baseline_cannot_drop_required_plan_resources_even_with_new_checksum(
    setup,
):
    baseline = capture(setup)
    baseline["maintained_plan"]["resources"].pop(
        "arn:aws:iam::123456789012:role/example-owned-node-role"
    )
    baseline["baseline_sha256"] = digest(
        {k: v for k, v in baseline.items() if k != "baseline_sha256"}
    )
    setup[-1].calls.clear()
    assert verify(setup, baseline)["status"] == "BLOCKED"
    assert not setup[-1].calls
