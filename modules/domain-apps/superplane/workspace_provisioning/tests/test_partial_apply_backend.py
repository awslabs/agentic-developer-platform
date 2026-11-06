# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""A partial-state observation must fail closed on live drift and ownership loss."""

import hashlib
import io
import json

import pytest

from workspace_provisioning.partial_apply_backend import read_snapshot
from workspace_provisioning.runtime_config import LifecycleRefused
from .test_partial_apply_plan import evidence  # noqa: F401


class Backend:
    def __init__(self, state, target):
        state["resources"][1]["instances"][0]["attributes"]["public_ip"] = "192.0.2.1"
        self.raw = json.dumps(state).encode()
        self.version = self.head_version = "version-1"
        self.versioning = "Enabled"
        self.checksum = hashlib.md5(self.raw, usedforsecurity=False).hexdigest()
        self.locks = [False, False]
        self.calls = []
        self.body = None
        self.addresses = [
            {
                "AllocationId": "eipalloc-0123456789abcdef0",
                "Domain": "vpc",
                "PublicIp": "192.0.2.1",
                "Tags": [
                    {"Key": key, "Value": target[field]}
                    for key, field in [
                        ("OrgId", "org_id"),
                        ("WorkspaceId", "workspace_id"),
                    ]
                ],
            }
        ]

    def client(self, name, *, region_name):
        self.calls.append((name, region_name))
        return self

    def get_bucket_versioning(self, **kwargs):
        assert kwargs["ExpectedBucketOwner"] == "123456789012"
        return {"Status": self.versioning}

    def get_item(self, **kwargs):
        assert kwargs["ConsistentRead"] is True
        if kwargs["Key"]["LockID"]["S"].endswith("-md5"):
            return {"Item": {"Digest": {"S": self.checksum}}}
        return {"Item": {"Info": {"S": "locked"}}} if self.locks.pop(0) else {}

    def get_object(self, **kwargs):
        assert kwargs["ExpectedBucketOwner"] == "123456789012"
        self.body = io.BytesIO(self.raw)
        return {"VersionId": self.version, "Body": self.body}

    def head_object(self, **kwargs):
        assert kwargs["ExpectedBucketOwner"] == "123456789012"
        return {"VersionId": self.head_version}

    def describe_addresses(self, **kwargs):
        assert kwargs == {"AllocationIds": ["eipalloc-0123456789abcdef0"]}
        return {"Addresses": self.addresses}


CONFIG = {"bucket": "state", "region": "us-east-1", "lock_table": "locks"}


def test_versioned_read_checks_backend_and_eip_in_their_respective_regions(evidence):
    state, target, inventory, _ = evidence
    backend = Backend(state, target)
    result = read_snapshot(backend, CONFIG, target)
    assert result == {
        "bucket": "state",
        "region": "us-east-1",
        "lock_table": "locks",
        "version_id": "version-1",
        "key": f"test/modules/superplane-workspaces/v2/{target['org_id']}/{target['workspace_id']}/terraform.tfstate",
        "sha256": hashlib.sha256(backend.raw).hexdigest(),
        **inventory,
    }
    assert backend.calls == [
        ("s3", "us-east-1"),
        ("dynamodb", "us-east-1"),
        ("ec2", "us-west-2"),
    ]
    assert backend.body.closed and not backend.locks
    backend.locks = [False, False]
    assert read_snapshot(backend, CONFIG, target, expected=result) == result


@pytest.mark.parametrize(
    "bad",
    [
        "versioning",
        "null-version",
        "missing-version",
        "checksum",
        "pre-lock",
        "post-lock",
        "version-drift",
        "empty",
        "oversize",
        "foreign-org",
        "foreign-workspace",
        "foreign-eip",
        "wrong-domain",
        "public-ip",
        "ambiguous",
        "AssociationId",
        "NetworkInterfaceId",
        "InstanceId",
        "PrivateIpAddress",
    ],
)
def test_live_snapshot_refuses_drift_or_an_address_in_use(evidence, bad):
    state, target, _, _ = evidence
    backend = Backend(state, target)
    address = backend.addresses[0]
    if bad == "versioning":
        backend.versioning = "Suspended"
    elif bad == "null-version":
        backend.version = "null"
    elif bad == "missing-version":
        backend.version = None
    elif bad == "checksum":
        backend.checksum = "0" * 32
    elif bad == "pre-lock":
        backend.locks[0] = True
    elif bad == "post-lock":
        backend.locks[1] = True
    elif bad == "version-drift":
        backend.head_version = "version-2"
    elif bad == "empty":
        backend.raw = b""
    elif bad == "oversize":
        backend.raw = b"x" * ((64 << 20) + 1)
    elif bad == "foreign-org":
        address["Tags"][0]["Value"] = "foreign"
    elif bad == "foreign-workspace":
        address["Tags"][1]["Value"] = "foreign"
    elif bad == "foreign-eip":
        address["AllocationId"] = "eipalloc-00000000000000000"
    elif bad == "wrong-domain":
        address["Domain"] = "standard"
    elif bad == "public-ip":
        address["PublicIp"] = "192.0.2.2"
    elif bad == "ambiguous":
        backend.addresses *= 2
    else:
        address[bad] = "in-use"
    with pytest.raises(LifecycleRefused):
        read_snapshot(backend, CONFIG, target)
    if backend.body is not None:
        assert backend.body.closed


def test_even_equivalent_state_at_a_new_version_requires_a_new_owner_review(evidence):
    state, target, _, _ = evidence
    backend = Backend(state, target)
    expected = read_snapshot(backend, CONFIG, target)
    backend.locks = [False, False]
    backend.version = backend.head_version = "version-2"
    with pytest.raises(LifecycleRefused, match="changed since owner"):
        read_snapshot(backend, CONFIG, target, expected=expected)
