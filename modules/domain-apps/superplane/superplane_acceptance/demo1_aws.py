"""Selected-connection AWS reads; no write command or ambient credential fallback."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime

from .demo1_evidence import EvidenceError, identifier, text
from .demo1_provider import InventoryQuery

RESOURCE_KINDS = {
    "instance": (
        "ec2",
        "describe-instances",
        "--instance-ids",
        "InstanceId",
        "Reservations",
        "Instances",
    ),
    "volume": ("ec2", "describe-volumes", "--volume-ids", "VolumeId", "Volumes", None),
    "vpc": ("ec2", "describe-vpcs", "--vpc-ids", "VpcId", "Vpcs", None),
    "subnet": ("ec2", "describe-subnets", "--subnet-ids", "SubnetId", "Subnets", None),
    "network-interface": (
        "ec2",
        "describe-network-interfaces",
        "--network-interface-ids",
        "NetworkInterfaceId",
        "NetworkInterfaces",
        None,
    ),
    "security-group": (
        "ec2",
        "describe-security-groups",
        "--group-ids",
        "GroupId",
        "SecurityGroups",
        None,
    ),
}
NOT_FOUND = {
    "ResourceNotFoundException",
    "InvalidInstanceID.NotFound",
    "InvalidVolume.NotFound",
    "InvalidVpcID.NotFound",
    "InvalidSubnetID.NotFound",
    "InvalidNetworkInterfaceID.NotFound",
    "InvalidGroup.NotFound",
}
AWS_ARN = re.compile(
    r"arn:(?:aws|aws-us-gov|aws-cn):(?P<service>eks|ec2):(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):(?P<kind>[a-z-]+)/(?P<name>[A-Za-z0-9_-]{1,100})\Z"
)
ROLE_NAME = re.compile(r"[A-Za-z0-9+=,.@_-]{1,64}\Z")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _resource(resource: str, query: InventoryQuery) -> tuple[str, str]:
    matched = AWS_ARN.fullmatch(text(resource, "resource ARN"))
    if (
        not matched
        or matched["account"] != query.account
        or matched["region"] != query.region
    ):
        raise EvidenceError("inventory: foreign or malformed provider resource")
    kind, name = matched["kind"], matched["name"]
    if matched["service"] == "eks" and kind == "cluster":
        return "cluster", name
    if matched["service"] != "ec2" or kind not in RESOURCE_KINDS:
        raise EvidenceError("inventory: unsupported resource type")
    prefix = {
        "instance": "i",
        "volume": "vol",
        "network-interface": "eni",
        "security-group": "sg",
        "subnet": "subnet",
        "vpc": "vpc",
    }[kind]
    if not re.fullmatch(rf"{prefix}-[0-9a-f]{{8}}(?:[0-9a-f]{{9}})?", name):
        raise EvidenceError("inventory: malformed resource identity")
    return kind, name


class AwsProviderReader:
    """Query immutable resource IDs with a selected broker label and strict STS role."""

    def __init__(
        self,
        *,
        connection_id: str,
        broker_label: str,
        account: str,
        role_name: str,
        region: str,
        runner: Callable = subprocess.run,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.connection_id = identifier(connection_id, "connection_id")
        self.broker_label = text(broker_label, "broker label")
        if not LABEL.fullmatch(self.broker_label):
            raise EvidenceError("inventory: invalid broker label")
        self.account = text(account, "account")
        if not re.fullmatch(r"[0-9]{12}", self.account):
            raise EvidenceError("inventory: invalid account")
        self.role_name = text(role_name, "role")
        if not ROLE_NAME.fullmatch(self.role_name):
            raise EvidenceError("inventory: exact assumed role name required")
        self.region = text(region, "region")
        if not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-[0-9]", self.region):
            raise EvidenceError("inventory: invalid region")
        self.runner = runner
        self.clock = clock

    def _execute(self, *arguments: str) -> tuple[int, object, str]:
        command = [
            "adp-cred",
            "assume",
            "--service",
            "aws",
            "--label",
            self.broker_label,
            "--exec",
            "aws",
            *arguments,
            "--output",
            "json",
        ]
        try:
            completed = self.runner(
                command, capture_output=True, text=True, check=False, timeout=30
            )
        except (OSError, subprocess.SubprocessError):
            return -1, None, ""
        if completed.returncode:
            return completed.returncode, None, completed.stderr or ""
        try:
            return 0, json.loads(completed.stdout), ""
        except (ValueError, TypeError):
            return -1, None, ""

    def _identity(self) -> bool:
        code, result, _ = self._execute("sts", "get-caller-identity")
        if (
            code
            or not isinstance(result, dict)
            or result.get("Account") != self.account
        ):
            return False
        arn = result.get("Arn")
        return (
            isinstance(arn, str)
            and re.fullmatch(
                rf"arn:(?:aws|aws-us-gov|aws-cn):sts:{self.account}:assumed-role/{re.escape(self.role_name)}/[^/]+",
                arn,
            )
            is not None
        )

    def _lookup(self, kind: str, name: str, arn: str) -> str:
        if not self._identity():
            return "denied"
        if kind == "cluster":
            code, result, error = self._execute(
                "eks", "describe-cluster", "--name", name, "--region", self.region
            )
            observed = result.get("cluster") if isinstance(result, dict) else None
            matches = isinstance(observed, dict) and observed.get("arn") == arn
        else:
            service, operation, option, id_field, collection, nested = RESOURCE_KINDS[
                kind
            ]
            code, result, error = self._execute(
                service, operation, option, name, "--region", self.region
            )
            entries = result.get(collection) if isinstance(result, dict) else None
            if nested and isinstance(entries, list):
                entries = [
                    item
                    for group in entries
                    if isinstance(group, dict)
                    for item in group.get(nested, [])
                ]
            matches = (
                isinstance(entries, list)
                and len(entries) == 1
                and isinstance(entries[0], dict)
                and entries[0].get(id_field) == name
            )
        if code:
            found = re.search(r"\(([A-Za-z0-9.]+)\)", error)
            return "absent" if found and found[1] in NOT_FOUND else "denied"
        return "present" if matches else "incomplete"

    def read_inventory(self, query: InventoryQuery) -> dict:
        if (
            query.connection_id != self.connection_id
            or query.role != self.role_name
            or query.account != self.account
            or query.region != self.region
        ):
            raise EvidenceError(
                "inventory: broker connection or selected target mismatch"
            )
        identifier(query.workspace_id, "workspace_id")
        if (
            not query.expected_owned
            or not query.expected_survivors
            or set(query.expected_owned) & set(query.expected_survivors)
        ):
            raise EvidenceError(
                "inventory: independent ownership and survivor baselines required"
            )
        resources = query.expected_owned + query.expected_survivors
        if len(set(resources)) != len(resources):
            raise EvidenceError("inventory: duplicate ownership identity")
        parsed = [(resource, *_resource(resource, query)) for resource in resources]
        present_owned: list[str] = []
        present_survivors: list[str] = []
        statuses = []
        for index, (resource, kind, name) in enumerate(parsed):
            observed = self._lookup(kind, name, resource)
            statuses.append(observed)
            if observed == "present":
                (
                    present_owned
                    if index < len(query.expected_owned)
                    else present_survivors
                ).append(resource)
            if observed == "denied":
                break
        owned_kinds = {kind for _, kind, _ in parsed[: len(query.expected_owned)]}
        survivor_kinds = {kind for _, kind, _ in parsed[len(query.expected_owned) :]}
        complete_baseline = (
            bool(owned_kinds & {"cluster", "instance"})
            and "volume" in owned_kinds
            and bool(
                owned_kinds & {"vpc", "subnet", "network-interface", "security-group"}
            )
            and bool(survivor_kinds & {"cluster", "instance"})
        )
        status = (
            "denied"
            if "denied" in statuses
            else "complete"
            if complete_baseline
            and len(statuses) == len(resources)
            and all(item in ("present", "absent") for item in statuses)
            else "incomplete"
        )
        return {
            "connection_id": query.connection_id,
            "role": query.role,
            "account": query.account,
            "region": query.region,
            "workspace_id": query.workspace_id,
            "status": status,
            "owned_present": present_owned,
            "survivors_present": present_survivors,
            "cost_usd": None,
            "observed_at": self.clock().isoformat(),
        }
