"""Provider absence, incomplete ownership and retained dependent resource evidence."""

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest
from harness_jobs.effects import CallEffect, call_effect
from harness_jobs.inventory import AllocationResource, ResourcePresence

from workspace_provisioning.retirement_observation import (
    RetirementObservations,
    reference,
)

from .test_retirement_plan import component, inventory


class ProviderError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


@pytest.fixture
def observer():
    record = inventory(cluster_ownership="adopted", remove_namespace=False)
    target = SimpleNamespace(
        cluster_arn=record.cluster_arn, account_id="879318057152", region="us-east-1"
    )
    clients = {
        "sts": SimpleNamespace(
            get_caller_identity=lambda: {"Account": target.account_id}
        )
    }
    kube = SimpleNamespace(target=target, _get=lambda spec: None)
    reader = RetirementObservations(
        session=SimpleNamespace(client=lambda name, **_: clients[name]),
        kubernetes=kube,
        eks=None,
    )
    return reader, record, clients


def resource(reader, kind, **identity):
    ref = reference(kind, **identity)
    reader.descriptors[ref] = {"kind": kind, **identity}
    return AllocationResource(ref, "aws", ref, kind, frozenset())


@pytest.mark.parametrize(
    "code,presence",
    [
        ("InvalidVolume.NotFound", ResourcePresence.ABSENT),
        ("UnauthorizedOperation", ResourcePresence.UNKNOWN),
        ("RequestLimitExceeded", ResourcePresence.UNKNOWN),
    ],
)
def test_only_exact_provider_notfound_proves_retained_volume_absence(
    observer, code, presence
):
    reader, record, clients = observer

    def read(**kwargs):
        assert kwargs == {"VolumeIds": ["vol-owned"]}
        raise ProviderError(code)

    clients["ec2"] = SimpleNamespace(describe_volumes=read)
    retained = AllocationResource(
        "vol-owned", "aws", "vol-owned", "volume", frozenset({"original-create"})
    )
    observation = reader.observe(record, retained)
    assert observation.presence is presence
    assert observation.provider_state is None
    assert observation.detail


def test_pending_kms_deletion_remains_present(observer):
    reader, record, clients = observer
    clients["kms"] = SimpleNamespace(
        describe_key=lambda **_: {"KeyMetadata": {"KeyState": "PendingDeletion"}}
    )
    item = resource(
        reader,
        "terraform-resource",
        resource_type="aws_kms_key",
        identity={"id": "key-1"},
    )
    observation = reader.observe(record, item)
    assert observation.presence is ResourcePresence.PRESENT
    assert observation.provider_state


def test_iam_replacement_never_becomes_absence(observer):
    reader, record, clients = observer
    clients["iam"] = SimpleNamespace(
        get_role=lambda **_: {"Role": {"Arn": "arn:role", "RoleId": "replacement"}}
    )
    item = resource(
        reader,
        "terraform-resource",
        resource_type="aws_iam_role",
        identity={"name": "role", "arn": "arn:role", "unique_id": "original"},
    )
    observation = reader.observe(record, item)
    assert observation.presence is ResourcePresence.UNKNOWN
    assert observation.provider_state is None
    assert observation.detail


def test_shared_and_adopted_objects_are_excluded_but_missing_ownership_is_retained(
    observer,
):
    reader, record, _ = observer
    record = replace(
        record,
        components=(
            component("shared", kind="ClusterRole", namespace=""),
            component("adopted", owned=False),
        ),
        components_complete=False,
    )
    catalog = reader.catalog(record, None, {}, {})
    assert len(catalog) == 1
    item = next(iter(catalog.values()))
    assert item.kind == "unresolved-bootstrap-ownership"
    observation = reader.observe(record, item)
    assert observation.presence is ResourcePresence.UNKNOWN
    assert observation.provider_state is None
    assert observation.detail


def test_descriptor_reference_is_bounded_and_recoverable_from_original_journal(
    observer,
):
    reader, record, _ = observer
    item = component("owned")
    item.desired["spec"] = {"payload": "x" * 3000}
    record = replace(record, components=(item,))
    catalog = reader.catalog(record, None, {}, {})
    saved = next(iter(catalog.values()))
    assert len(saved.provider_reference) <= 255
    reader.descriptors.clear()
    assert reader.catalog(record, None, {}, catalog) == catalog
    assert reader.observe(record, saved).presence is ResourcePresence.ABSENT


def test_terminated_instances_are_absent_but_stopped_instances_still_cost(observer):
    reader, record, clients = observer
    state = "stopped"
    clients["ec2"] = SimpleNamespace(
        describe_instances=lambda **_: {
            "Reservations": [
                {"Instances": [{"InstanceId": "i-owned", "State": {"Name": state}}]}
            ]
        }
    )
    item = resource(reader, "instance", id="i-owned")
    assert reader.observe(record, item).presence is ResourcePresence.PRESENT
    state = "terminated"
    assert reader.observe(record, item).presence is ResourcePresence.ABSENT


def test_read_only_verification_is_exactly_classified():
    assert (
        call_effect("verify-resource-inventory", provider="superplane-aws")
        is CallEffect.OBSERVES
    )
    assert (
        call_effect("verify-resource-inventory", provider="another-provider")
        is CallEffect.UNRECOGNIZED
    )


def test_fresh_discovery_retains_detached_volume_after_it_leaves_provider_listings(
    observer,
):
    reader, adopted, clients = observer
    record = replace(adopted, cluster_ownership="adp-created")
    volumes = [{"VolumeId": "vol-leaked"}]

    class Pages:
        def __init__(self, method):
            self.method = method

        def paginate(self, **kwargs):
            result = {
                "describe_instances": {"Reservations": []},
                "describe_network_interfaces": {"NetworkInterfaces": []},
                "describe_volumes": {"Volumes": volumes},
                "get_resources": {"ResourceTagMappingList": []},
            }
            return [result[self.method]]

    def volume(**kwargs):
        assert kwargs == {"VolumeIds": ["vol-leaked"]}
        if not volumes:
            raise ProviderError("InvalidVolume.NotFound")
        return {"Volumes": volumes}

    clients["ec2"] = SimpleNamespace(
        get_paginator=Pages,
        describe_addresses=lambda **_: {"Addresses": []},
        describe_volumes=volume,
    )
    clients["resourcegroupstaggingapi"] = SimpleNamespace(get_paginator=Pages)
    document = {
        "resource_changes": [
            {
                "type": "aws_vpc",
                "change": {"before": {"id": "vpc-owned", "arn": "arn:vpc-owned"}},
            }
        ]
    }
    artifact = SimpleNamespace(
        read=lambda *_: (b"saved", json.dumps(document).encode(), b"authorization")
    )
    known = reader.catalog(record, artifact, {}, {}, frozenset({"original-create"}))
    assert known["vol-leaked"].operation_keys == frozenset({"original-create"})
    assert (
        reader.observe(record, known["vol-leaked"]).presence is ResourcePresence.PRESENT
    )
    volumes = []
    subsequent = reader.catalog(record, artifact, {}, known)
    assert "vol-leaked" in subsequent
    assert (
        reader.observe(record, subsequent["vol-leaked"]).presence
        is ResourcePresence.ABSENT
    )
