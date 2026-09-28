"""Access inventory must distinguish principals and recreated EKS entries."""

import pytest

from superplane_bootstrap.adapters import AwsPrerequisiteAccess
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.prerequisites import ACCESS_ENTRY
from .conftest import (
    CLUSTER_ARN,
    CLUSTER_NAME,
    PRINCIPAL_ARN,
    REGION,
    FakePrerequisiteAccess,
)
from .test_adapters import _Scripted
from .test_prerequisites import _verify, target as _target

ENTRY_ARN = (
    CLUSTER_ARN.replace(":cluster/", ":access-entry/")
    + "/role/000000000000/SyntheticTestRole/entry-one"
)


@pytest.fixture
def target(binding, provider_identity, observed_cluster, expected_target):
    return _target.__wrapped__(
        binding, provider_identity, observed_cluster, expected_target
    )


def observation(**changes):
    result = dict(FakePrerequisiteAccess().access_entry(CLUSTER_ARN, PRINCIPAL_ARN))
    result.update(
        cluster_arn=CLUSTER_ARN, principal_arn=PRINCIPAL_ARN, access_entry_arn=ENTRY_ARN
    )
    result.update(changes)
    return result


@pytest.mark.parametrize(
    "changes",
    [
        {"principal_arn": "arn:aws:iam::123456789012:role/Other"},
        {"principal_arn": ""},
        {"cluster_arn": CLUSTER_ARN + "-other"},
        {"access_entry_arn": ""},
        {"access_entry_arn": ENTRY_ARN.replace(":access-entry/", ":cluster/")},
        {
            "access_entry_arn": ENTRY_ARN.replace(
                CLUSTER_NAME + "/", CLUSTER_NAME + "-other/"
            )
        },
    ],
)
def test_unverified_access_identity_cannot_enter_inventory(target, changes):
    access = FakePrerequisiteAccess()
    access.access_entry = lambda *_: observation(**changes)
    with pytest.raises(BootstrapRefused, match="access entry identity"):
        _verify(target, access)


def test_recreated_access_entry_has_a_distinct_durable_identity(target):
    identifiers = []
    for suffix in ("entry-one", "entry-two"):
        access = FakePrerequisiteAccess()
        arn = ENTRY_ARN.rsplit("/", 1)[0] + "/" + suffix
        access.access_entry = lambda *_, arn=arn: observation(access_entry_arn=arn)
        inventory, _ = _verify(target, access)
        entry = next(
            item for item in inventory.prerequisites if item.kind == ACCESS_ENTRY
        )
        assert entry.identifier.startswith(arn + "#")
        assert not entry.removable
        identifiers.append(entry.identifier)
    assert identifiers[0] != identifiers[1]


@pytest.mark.parametrize(
    "changes",
    [
        {"principalArn": "arn:aws:iam::123456789012:role/Other"},
        {"clusterName": CLUSTER_NAME + "-other"},
        {"type": "EC2_LINUX"},
        {"accessEntryArn": ""},
    ],
)
def test_production_adapter_refuses_mismatched_describe_response(changes):
    entry = dict(
        principalArn=PRINCIPAL_ARN,
        clusterName=CLUSTER_NAME,
        type="STANDARD",
        accessEntryArn=ENTRY_ARN,
    )
    entry.update(changes)
    runner = _Scripted(
        {
            "describe-access-entry": {"accessEntry": entry},
            "list-associated-access-policies": {
                "associatedAccessPolicies": [
                    {
                        "policyArn": "arn:aws:eks::aws:cluster-access-policy/AmazonEKSAdminPolicy",
                        "accessScope": {
                            "type": "namespace",
                            "namespaces": ["workspace"],
                        },
                    }
                ]
            },
        }
    )
    with pytest.raises(BootstrapRefused, match="access entry identity"):
        AwsPrerequisiteAccess(runner=runner, region=REGION).access_entry(
            CLUSTER_ARN, PRINCIPAL_ARN
        )
