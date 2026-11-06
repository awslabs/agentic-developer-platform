"""Workspace-disk recovery after a controller restart (#8100): the disk on the operation's own new instance."""

import logging
import sys
from pathlib import Path

import pytest
from sqlalchemy import select
from src.shared.models.dev_box import (
    DevBox,
    DevBoxEvent,
    DevBoxInstance,
    DevBoxOperation,
    DevBoxPolicy,
    DevBoxResource,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_controller_launch as seeds  # noqa: E402

from controller import launch as LAUNCH  # noqa: E402
from controller import storage as STORAGE  # noqa: E402

controller_database = seeds.controller_database  # fixture: owner, policy, template, generation-1 box and its setup operation

REPLACE_TOKEN = "b" * 64
LAUNCH_TEMPLATE = LAUNCH.LaunchTemplate("lt-example", "4", "us-east-1", account_id="000000000000", disk_gb=40, kms_key_arn="kms-example")
OWNERSHIP = {"adp:component": "dev-box", "adp:deployment-id": "example-deployment", "adp:owner-id": "example-owner", "adp:box-id": "box"}


def tags(generation, **extra):
    return [{"Key": key, "Value": value} for key, value in {**OWNERSHIP, "adp:generation": str(generation), **extra}.items()]


class WorkspaceEC2:
    """Live shape of #8100: previous machine stopped, the replacement this operation launched, one workspace volume."""

    def __init__(self, *, attached_to=None, attach_state="attached", replacement_visible=True):
        self.calls = []
        self.lose_attachment = False
        self.lose_launch = False
        self.instances = {"i-previous": {
            "InstanceId": "i-previous", "State": {"Name": "stopped"}, "Tags": tags(1), "Placement": {"AvailabilityZone": "us-east-1a"},
        }}
        if replacement_visible:
            self.instances["i-replacement"] = self.replacement()
        self.volume = {
            "VolumeId": "vol-owned", "Size": 40, "Encrypted": True, "KmsKeyId": "kms-example", "AvailabilityZone": "us-east-1a",
            "Tags": tags(1, **{"adp:workspace": "persistent"}),
            "State": "in-use" if attached_to else "available",
            "Attachments": [{"InstanceId": attached_to, "State": attach_state, "Device": "/dev/xvdf"}] if attached_to else [],
        }

    @staticmethod
    def replacement():
        return {"InstanceId": "i-replacement", "ClientToken": REPLACE_TOKEN, "State": {"Name": "running"}, "Tags": tags(2),
                "PrivateIpAddress": "10.4.0.9", "Placement": {"AvailabilityZone": "us-east-1a"}}

    def run_instances(self, **kwargs):
        self.calls.append(("run_instances", kwargs["ClientToken"]))
        self.instances["i-replacement"] = self.replacement()
        if self.lose_launch:
            raise ConnectionError("the launch response was lost")
        return {"Instances": [self.instances["i-replacement"]]}

    def describe_instances(self, **kwargs):
        if "InstanceIds" in kwargs:
            found = [self.instances[item] for item in kwargs["InstanceIds"] if item in self.instances]
        else:
            token = next(item["Values"][0] for item in kwargs["Filters"] if item["Name"] == "client-token")
            found = [item for item in self.instances.values() if item.get("ClientToken") == token]
        return {"Reservations": [{"Instances": found}]} if found else {"Reservations": []}

    def describe_volumes(self, **kwargs):
        assert kwargs.get("VolumeIds") == ["vol-owned"], kwargs
        return {"Volumes": [self.volume]}

    def attach_volume(self, **kwargs):
        self.calls.append(("attach", kwargs["InstanceId"]))
        self.volume["State"] = "in-use"
        self.volume["Attachments"] = [{"InstanceId": kwargs["InstanceId"], "State": "attached", "Device": kwargs["Device"]}]
        if self.lose_attachment:
            raise ConnectionError("the attach response was lost")

    def detach_volume(self, **kwargs):
        self.calls.append(("detach", kwargs["InstanceId"]))
        self.volume["State"] = "available"
        self.volume["Attachments"] = []

    def modify_volume(self, **kwargs):
        self.calls.append(("modify", kwargs["VolumeId"], kwargs["Size"], kwargs["VolumeType"]))
        if getattr(self, "defer_growth", None):
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": self.defer_growth, "Message": "synthetic"}}, "ModifyVolume")
        self.volume["Size"] = kwargs["Size"]
        self.volume["VolumeType"] = kwargs["VolumeType"]


async def admit_replace(factory, *, progress, retries=0, error_class=None, target_recorded=True):
    """Generation 2 replace admitted; `target_recorded` None omits the new generation row, False leaves its provider id unset."""
    async with factory() as db:
        setup = await db.get(DevBoxOperation, "operation")
        setup.status, setup.progress = "succeeded", "instance_observed"
        box = await db.get(DevBox, "box")
        box.machine_generation, box.status, box.observed_state = 2, "requested", "unknown"
        scope = dict(box_id="box", user_id="example-owner", org_id="example-org", deployment_id="example-deployment")
        db.add(DevBoxInstance(id="previous", **scope, machine_generation=1, template_revision="r1", image_ref="ami-example",
                              instance_size="t3.small", region="us-east-1", availability_zone="us-east-1a",
                              provider_instance_id="i-previous", observed_state="stopped"))
        if target_recorded is not None:
            db.add(DevBoxInstance(id="replacement", **scope, machine_generation=2, template_revision="r1", image_ref="ami-example",
                                  instance_size="t3.small", region="us-east-1", availability_zone="us-east-1a",
                                  provider_instance_id="i-replacement" if target_recorded else None, observed_state="unknown"))
        db.add(DevBoxResource(id="disk", **scope, machine_generation=1, resource_kind="workspace_disk", provider_resource_id="vol-owned",
                              size_gb=40, encrypted=True, retention_state="retained", deletion_state="present"))
        db.add(DevBoxOperation(id="replace", **scope, machine_generation=2, action="replace", status="unknown", idempotency_key="replace-key",
                               request_digest="digest-2", actor_user_id="example-owner", provider_token=REPLACE_TOKEN,
                               progress=progress, retries=retries, error_class=error_class))
        await db.commit()


async def read(factory):
    async with factory() as db:
        operation = await db.get(DevBoxOperation, "replace")
        box = await db.get(DevBox, "box")
        replacement = await db.scalar(select(DevBoxInstance).where(DevBoxInstance.machine_generation == 2))
        events = list(await db.scalars(select(DevBoxEvent.event_type).where(DevBoxEvent.operation_id == "replace").order_by(DevBoxEvent.created_at)))
        return operation, box, replacement, events


async def reconcile(factory, ec2, worker):
    return await LAUNCH.reconcile_setup(factory, ec2, "replace", worker, LAUNCH_TEMPLATE)


async def admit_template_disk(factory, disk_gb):
    """The seeded template grew (operator raised workspace_disk_gb); the launch template carries the same size."""
    from dataclasses import replace
    from src.shared.models.dev_box import DevBoxTemplate
    async with factory() as db:
        template = await db.get(DevBoxTemplate, "template")
        template.disk_gb = disk_gb  # within the seeded policy (max_disk_gb 50, max_storage_gb 100)
        await db.commit()
    return replace(LAUNCH_TEMPLATE, disk_gb=disk_gb)


async def recorded_disk_size(factory):
    async with factory() as db:
        return await db.scalar(select(DevBoxResource.size_gb).where(DevBoxResource.id == "disk"))


@pytest.mark.asyncio
async def test_replace_grows_the_retained_disk_in_place_to_the_larger_template(controller_database):
    """#8835 disk growth: 100 GB was too small for a developer. The template grew to 48; the owner's 40 GB
    disk is enlarged by ModifyVolume (gp3) before the attach, its record follows, nothing is re-created."""
    ec2 = WorkspaceEC2()
    template = await admit_template_disk(controller_database, 48)
    await admit_replace(controller_database, progress="instance_observed")
    assert await LAUNCH.reconcile_setup(controller_database, ec2, "replace", "worker", template)
    operation, box, _, _ = await read(controller_database)
    assert operation.status == "succeeded" and operation.error_class is None and box.status == "running"
    assert ec2.calls == [("modify", "vol-owned", 48, "gp3"), ("attach", "i-replacement")]
    assert ec2.volume["Size"] == 48 and ec2.volume["VolumeType"] == "gp3"
    assert await recorded_disk_size(controller_database) == 48
    assert not await LAUNCH.reconcile_setup(controller_database, ec2, "replace", "worker", template)


@pytest.mark.asyncio
async def test_growth_already_applied_by_a_lost_modify_is_recorded_without_a_second_modify(controller_database):
    ec2 = WorkspaceEC2()
    ec2.volume["Size"] = 48  # EBS accepted the previous attempt's ModifyVolume; the record still says 40
    template = await admit_template_disk(controller_database, 48)
    await admit_replace(controller_database, progress="instance_observed")
    assert await LAUNCH.reconcile_setup(controller_database, ec2, "replace", "worker", template)
    operation, _, _, _ = await read(controller_database)
    assert operation.status == "succeeded" and ec2.calls == [("attach", "i-replacement")]
    assert await recorded_disk_size(controller_database) == 48


@pytest.mark.asyncio
async def test_deferred_growth_starts_the_box_on_its_current_size_and_keeps_the_intent(controller_database):
    """EBS allows one modification per six hours: a refusal is not this operation's failure."""
    ec2 = WorkspaceEC2()
    ec2.defer_growth = "VolumeModificationRateExceeded"
    template = await admit_template_disk(controller_database, 48)
    await admit_replace(controller_database, progress="instance_observed")
    assert await LAUNCH.reconcile_setup(controller_database, ec2, "replace", "worker", template)
    operation, box, _, _ = await read(controller_database)
    assert operation.status == "succeeded" and box.status == "running"
    assert ec2.calls == [("modify", "vol-owned", 48, "gp3"), ("attach", "i-replacement")]
    assert ec2.volume["Size"] == 40 and await recorded_disk_size(controller_database) == 40, "the next operation grows it"


@pytest.mark.asyncio
async def test_a_template_smaller_than_the_disk_is_still_a_policy_mismatch(controller_database):
    ec2 = WorkspaceEC2()
    template = await admit_template_disk(controller_database, 20)
    await admit_replace(controller_database, progress="instance_observed")
    assert await LAUNCH.reconcile_setup(controller_database, ec2, "replace", "worker", template)
    operation, _, _, _ = await read(controller_database)
    assert operation.error_class == "workspace_policy_mismatch" and operation.status != "succeeded"
    assert ec2.calls == [] and ec2.volume["Size"] == 40


@pytest.mark.parametrize("target_recorded", [True, False], ids=["row-has-provider-id", "provider-id-only-in-inventory"])
@pytest.mark.parametrize("progress", ["attachment_intent", "attachment_requested"])
@pytest.mark.asyncio
async def test_recovery_with_the_disk_already_on_the_new_instance_completes_the_replace(controller_database, progress, target_recorded):
    """AC-01: live 2026-10-17 shape — new generation launched, disk attached to it, controller restarted mid-operation."""
    ec2 = WorkspaceEC2(attached_to="i-replacement")
    await admit_replace(controller_database, progress=progress, retries=3, error_class="workspace_attached_elsewhere",
                        target_recorded=target_recorded)
    assert await reconcile(controller_database, ec2, "restarted-worker")
    operation, box, replacement, events = await read(controller_database)
    assert operation.status == "succeeded" and operation.progress == "instance_observed" and operation.error_class is None
    assert operation.lease_owner is None and operation.provider_request_ref == "i-replacement"
    assert box.status == "running" and box.observed_state == "running"
    assert replacement.provider_instance_id == "i-replacement" and replacement.observed_state == "running"
    assert ec2.calls == [], "the detach step and the attach step both recognise the disk on the operation's own instance"
    assert ec2.volume["Attachments"][0]["InstanceId"] == "i-replacement"
    assert events == []
    assert not await reconcile(controller_database, ec2, "later-worker")


@pytest.mark.asyncio
async def test_recovery_waits_while_the_disk_is_still_attaching_to_the_new_instance(controller_database):
    ec2 = WorkspaceEC2(attached_to="i-replacement", attach_state="attaching")
    await admit_replace(controller_database, progress="attachment_requested")
    assert await reconcile(controller_database, ec2, "restarted-worker")
    operation, box, _, events = await read(controller_database)
    assert operation.status == "unknown" and operation.error_class == "attachment_not_observed"
    assert box.status == "requested" and ec2.calls == [] and events == []
    ec2.volume["Attachments"][0]["State"] = "attached"
    assert await reconcile(controller_database, ec2, "restarted-worker")
    operation, _, _, _ = await read(controller_database)
    assert operation.status == "succeeded" and ec2.calls == []


@pytest.mark.asyncio
async def test_disk_on_a_foreign_instance_fails_the_replace_after_the_bound(controller_database, monkeypatch, caplog):
    """AC-02: only an attachment to neither the previous nor the target instance is `workspace_attached_elsewhere`,
    and it is bounded instead of looping every tick for ever."""
    monkeypatch.setattr(LAUNCH, "DEV_BOX_LAUNCH_RETRY_BOUND", 2)
    ec2 = WorkspaceEC2(attached_to="i-stranger")
    await admit_replace(controller_database, progress="attachment_intent")
    for worker in ("tick-one", "tick-two"):
        assert await reconcile(controller_database, ec2, worker)
        operation, _, _, events = await read(controller_database)
        assert operation.status == "unknown" and operation.error_class == "workspace_attached_elsewhere"
        assert operation.lease_owner is None and operation.lease_expires_at is None, "the lease is released between ticks"
        assert events == []
    with caplog.at_level(logging.INFO, logger=LAUNCH.__name__):
        assert await reconcile(controller_database, ec2, "tick-three")
    operation, box, _, events = await read(controller_database)
    assert operation.status == "failed" and operation.error_class == "workspace_attached_elsewhere" and operation.retries == 3
    assert operation.lease_owner is None and events == ["recovery_exhausted"]
    assert box.status == "requested" and box.desired_state == "running", "the box is left for the owner to release or replace"
    assert ec2.calls == [], "a foreign attachment is never detached or overwritten"
    line = next(record for record in caplog.records if "operation_id=replace" in record.getMessage() and "status=failed" in record.getMessage())
    assert line.levelno == logging.WARNING
    assert "vol-owned is attached to i-stranger (attached), which is neither the previous instance i-previous nor this operation's instance i-replacement" in line.getMessage()
    assert "unresolved after 3 reconciles" in line.getMessage()
    assert not await reconcile(controller_database, ec2, "tick-four"), "a failed operation is not claimed again"


@pytest.mark.asyncio
async def test_one_late_transient_observation_does_not_fail_a_slow_but_healthy_operation(controller_database, monkeypatch):
    monkeypatch.setattr(LAUNCH, "DEV_BOX_LAUNCH_RETRY_BOUND", 1)
    ec2 = WorkspaceEC2(attached_to="i-stranger")
    # The previous tick was clean (no error class) even though the claim count is already past the bound.
    await admit_replace(controller_database, progress="attachment_intent", retries=4, error_class=None)
    assert await reconcile(controller_database, ec2, "tick-five")
    operation, _, _, events = await read(controller_database)
    assert operation.status == "unknown" and operation.error_class == "workspace_attached_elsewhere" and events == []
    assert await reconcile(controller_database, ec2, "tick-six")
    operation, _, _, events = await read(controller_database)
    assert operation.status == "failed" and events == ["recovery_exhausted"]


@pytest.mark.asyncio
async def test_attachment_requested_is_persisted_before_the_attach_call_and_a_lost_response_recovers(controller_database):
    ec2 = WorkspaceEC2()
    ec2.lose_attachment = True
    await admit_replace(controller_database, progress="launch_intent", target_recorded=False)
    assert await reconcile(controller_database, ec2, "worker-one")
    operation, _, replacement, _ = await read(controller_database)
    assert operation.status == "unknown" and operation.error_class == "workspace_provider_unknown"
    assert operation.progress == "attachment_requested", "the marker is durable even though the attach response was lost"
    assert replacement.provider_instance_id == "i-replacement"
    assert ec2.calls == [("attach", "i-replacement")]
    ec2.lose_attachment = False
    assert await reconcile(controller_database, ec2, "restarted-worker")
    operation, box, _, _ = await read(controller_database)
    assert operation.status == "succeeded" and box.status == "running"
    assert ec2.calls == [("attach", "i-replacement")], "the disk already on the target instance is not attached twice"


@pytest.mark.asyncio
async def test_an_attach_that_never_took_effect_is_re_sent_when_the_disk_still_reads_available(controller_database):
    ec2 = WorkspaceEC2()
    await admit_replace(controller_database, progress="attachment_requested")
    assert await reconcile(controller_database, ec2, "restarted-worker")
    operation, _, _, _ = await read(controller_database)
    assert operation.status == "succeeded"
    assert ec2.calls == [("attach", "i-replacement")]


@pytest.mark.asyncio
async def test_detach_step_does_not_reset_progress_and_the_launch_retry_keeps_its_token(controller_database):
    """A replace whose launch response was lost re-sends RunInstances with the same ClientToken (#8037); the
    detach-from-previous re-check must not rewind progress to `admitted` on the way."""
    ec2 = WorkspaceEC2(replacement_visible=False)
    ec2.lose_launch = True
    await admit_replace(controller_database, progress="launch_intent", retries=1, error_class="provider_unknown", target_recorded=False)
    assert await reconcile(controller_database, ec2, "worker-two")
    operation, _, _, _ = await read(controller_database)
    assert operation.status == "unknown" and operation.progress == "launch_intent" and operation.error_class == "provider_unknown"
    assert ec2.calls == [("run_instances", REPLACE_TOKEN)]
    ec2.lose_launch = False
    assert await reconcile(controller_database, ec2, "worker-three")
    operation, box, _, _ = await read(controller_database)
    assert operation.status == "succeeded" and box.status == "running"
    assert ec2.calls == [("run_instances", REPLACE_TOKEN), ("attach", "i-replacement")]


@pytest.mark.asyncio
async def test_disk_still_detaching_from_the_previous_instance_is_not_foreign(controller_database):
    ec2 = WorkspaceEC2(attached_to="i-previous", attach_state="detaching")
    await admit_replace(controller_database, progress="detach_intent", target_recorded=None)
    assert await reconcile(controller_database, ec2, "worker-two")
    operation, _, _, _ = await read(controller_database)
    assert operation.status == "unknown" and operation.error_class == "detach_not_observed"
    assert ec2.calls == [], "a detach that is in flight is observed, not re-sent"


def test_attachment_classifies_the_holder_against_target_and_previous():
    volume = {"State": "in-use", "Attachments": [{"InstanceId": "i-new", "State": "attached"}]}
    assert STORAGE.attachment(volume, "i-new", "i-old") == ("target", "attached")
    assert STORAGE.attachment(volume, None, "i-old") == ("foreign", "attached"), "an unknown target never makes a holder its own"
    volume["Attachments"][0]["InstanceId"] = "i-old"
    assert STORAGE.attachment(volume, "i-new", "i-old") == ("previous", "attached")
    assert STORAGE.attachment({"State": "available", "Attachments": []}, "i-new", "i-old") == ("none", "available")
    error = STORAGE.attached_elsewhere({"VolumeId": "vol-1", "Attachments": [{"InstanceId": "i-x", "State": "attached"}]}, "i-new", "i-old")
    assert error == "workspace_attached_elsewhere" and isinstance(error, str) and type(str(error)) is str
    assert "i-x (attached)" in error.detail and "i-old" in error.detail and "i-new" in error.detail


# --- Capacity fallback on a replace (#8423): the retained disk pins the zone ----------------------

ZONED_TEMPLATE = LAUNCH.LaunchTemplate(
    "lt-example", "4", "us-east-1", account_id="000000000000", disk_gb=40, kms_key_arn="kms-example", instance_size="t3.small",
    fallback_sizes=("t3.small", "t3a.small"), subnets=(("subnet-a", "us-east-1a"), ("subnet-b", "us-east-1b")), security_group_id="sg-guest",
)


class RefusingWorkspaceEC2(WorkspaceEC2):
    """Every RunInstances is refused for lack of capacity; the calls record which (type, subnet) was asked."""

    def run_instances(self, **kwargs):
        from botocore.exceptions import ClientError

        interfaces = kwargs.get("NetworkInterfaces")
        self.calls.append(("run_instances", kwargs.get("InstanceType"), interfaces[0]["SubnetId"] if interfaces else None))
        raise ClientError({"Error": {"Code": "InsufficientInstanceCapacity", "Message": "no capacity"}}, "RunInstances")


@pytest.mark.asyncio
async def test_replace_falls_back_through_sizes_but_never_leaves_the_workspace_disk_zone(controller_database):
    await admit_replace(controller_database, progress="admitted", target_recorded=None)
    ec2 = RefusingWorkspaceEC2(attached_to=None, replacement_visible=False)
    assert await LAUNCH.reconcile_setup(controller_database, ec2, "replace", "worker", ZONED_TEMPLATE)
    launches = [call for call in ec2.calls if call[0] == "run_instances"]
    assert launches == [("run_instances", None, None), ("run_instances", "t3a.small", None)], launches
    operation, box, _, events = await read(controller_database)
    assert operation.status == "failed" and operation.error_class == "provider_refused:InsufficientInstanceCapacity"
    # #8223 causes 49/52/53: nothing launched, so the box retreats to the stopped machine it still has and the
    # API may admit the next Replace without anyone editing rows; the failed operation stays as the record.
    assert (box.status, box.observed_state, box.desired_state, box.machine_generation) == ("stopped", "stopped", "stopped", 1)
    assert events == ["launch_capacity_fallback", "launch_refused", "replace_retreated"]


@pytest.mark.asyncio
async def test_replace_with_no_approved_subnet_in_the_disk_zone_fails_without_a_provider_call(controller_database):
    await admit_replace(controller_database, progress="admitted", target_recorded=None)
    ec2 = RefusingWorkspaceEC2(attached_to=None, replacement_visible=False)
    elsewhere = LAUNCH.LaunchTemplate(
        "lt-example", "4", "us-east-1", account_id="000000000000", disk_gb=40, kms_key_arn="kms-example", instance_size="t3.small",
        fallback_sizes=("t3.small",), subnets=(("subnet-b", "us-east-1b"),), security_group_id="sg-guest",
    )
    assert await LAUNCH.reconcile_setup(controller_database, ec2, "replace", "worker", elsewhere)
    assert not [call for call in ec2.calls if call[0] == "run_instances"]
    operation, box, replacement, events = await read(controller_database)
    assert operation.status == "failed" and operation.error_class == "workspace_zone_unavailable"
    assert events == ["launch_refused", "replace_retreated"]
    assert box.status == "stopped" and box.machine_generation == 1 and replacement is None


@pytest.mark.asyncio
async def test_replace_finds_the_stopped_machine_across_a_generation_consumed_by_a_failed_launch(controller_database, monkeypatch):
    """#8223 cause 49 (dev 2026-10-21): a Replace whose launch never produced a machine still consumed an
    owner generation (28 → failed 29), so the next Replace (30) looked for a machine at exactly 29, found
    nothing and refused forever with replacement_not_stopped_or_missing_workspace. The machine being
    replaced is the latest recorded one below the new generation."""
    ec2 = WorkspaceEC2(replacement_visible=False)  # disk already detached: the failed attempt got that far
    original = WorkspaceEC2.replacement
    monkeypatch.setattr(WorkspaceEC2, "replacement", staticmethod(lambda: {**original(), "Tags": tags(3)}))
    await admit_replace(controller_database, progress="admitted", target_recorded=None)
    async with controller_database() as db:
        box = await db.get(DevBox, "box")
        box.machine_generation = 3  # generation 2 was consumed by the launch that never ran
        operation = await db.get(DevBoxOperation, "replace")
        operation.machine_generation = 3
        await db.commit()
    assert await reconcile(controller_database, ec2, "worker")
    async with controller_database() as db:
        operation = await db.get(DevBoxOperation, "replace")
    assert operation.error_class != "replacement_not_stopped_or_missing_workspace"
    assert "i-replacement" in ec2.instances, "the replace launched the new machine"
    for _ in range(3):
        await reconcile(controller_database, ec2, "worker")
    async with controller_database() as db:
        operation = await db.get(DevBoxOperation, "replace")
        box = await db.get(DevBox, "box")
    assert operation.status == "succeeded" and box.status == "running"
    assert ec2.volume["Attachments"][0]["InstanceId"] == "i-replacement"


# --- #8223 causes 52/53: a full subnet is a definite refusal, a failed launch retreats, superseded machines retire --

def _machine_from_launch(kwargs, instance_id="i-replacement"):
    tags = kwargs["TagSpecifications"][0]["Tags"]
    interfaces = kwargs.get("NetworkInterfaces")
    return {"InstanceId": instance_id, "ClientToken": kwargs["ClientToken"], "State": {"Name": "running"}, "Tags": tags,
            "PrivateIpAddress": "10.4.0.9", "Placement": {"AvailabilityZone": "us-east-1a"},
            "SubnetId": interfaces[0]["SubnetId"] if interfaces else "subnet-a"}


class ExhaustedSubnetEC2(WorkspaceEC2):
    """RunInstances answers InsufficientFreeAddressesInSubnet `refusals` times, then launches; terminations are recorded."""

    def __init__(self, refusals, **kwargs):
        super().__init__(**kwargs)
        self.refusals = refusals
        self.terminated = []

    def run_instances(self, **kwargs):
        from botocore.exceptions import ClientError

        interfaces = kwargs.get("NetworkInterfaces")
        self.calls.append(("run_instances", kwargs.get("InstanceType"), interfaces[0]["SubnetId"] if interfaces else None))
        if self.refusals > 0:
            self.refusals -= 1
            message = "There are not enough free addresses in subnet 'subnet-a' to satisfy the requested number of instances."
            raise ClientError({"Error": {"Code": "InsufficientFreeAddressesInSubnet", "Message": message}}, "RunInstances")
        self.instances["i-replacement"] = _machine_from_launch(kwargs)
        return {"Instances": [self.instances["i-replacement"]]}

    def terminate_instances(self, **kwargs):
        self.terminated.extend(kwargs["InstanceIds"])
        for instance_id in kwargs["InstanceIds"]:
            self.instances[instance_id]["State"] = {"Name": "terminated"}
        return {}


async def _admit_next_replace(factory, generation):
    """What the API does when it admits a Replace on a stopped box: new owner generation, box requested."""
    async with factory() as db:
        box = await db.get(DevBox, "box")
        assert (box.status, box.observed_state, box.desired_state) == ("stopped", "stopped", "stopped"), "retreat left the box admissible"
        box.machine_generation, box.status, box.observed_state, box.desired_state = generation, "requested", "unknown", "running"
        scope = dict(box_id="box", user_id="example-owner", org_id="example-org", deployment_id="example-deployment")
        db.add(DevBoxOperation(id=f"replace-{generation}", **scope, machine_generation=generation, action="replace", status="pending",
                               idempotency_key=f"replace-key-{generation}", request_digest=f"digest-{generation}", actor_user_id="example-owner",
                               provider_token="c" * 64, progress="admitted", retries=0))
        await db.commit()


@pytest.mark.asyncio
async def test_full_subnet_is_a_definite_refusal_the_box_retreats_from_and_the_next_replace_succeeds(controller_database, caplog):
    """#8223 causes 52/53 (dev 2026-10-22): the launch subnet had no free address. EC2 refused every send with
    InsufficientFreeAddressesInSubnet, the controller read it as `provider_unknown` (code and message dropped),
    ended `launch_unconfirmed` after the retry bound and left the box `requested` with no machine, which the API
    then refused to replace again. Now: the code is a capacity refusal (fallback within the disk's zone, then a
    fast failure that names it), the box retreats to its stopped machine, and the next Replace is admissible and
    launches when the subnet has an address again."""
    import logging

    await admit_replace(controller_database, progress="admitted", target_recorded=None)
    ec2 = ExhaustedSubnetEC2(refusals=5, attached_to=None, replacement_visible=False)
    with caplog.at_level(logging.INFO, logger=LAUNCH.__name__):
        assert await LAUNCH.reconcile_setup(controller_database, ec2, "replace", "worker", ZONED_TEMPLATE)
    launches = [call for call in ec2.calls if call[0] == "run_instances"]
    assert launches == [("run_instances", None, None), ("run_instances", "t3a.small", None)], "both sizes in the disk's zone, never subnet-b"
    operation, box, replacement, events = await read(controller_database)
    assert operation.status == "failed" and operation.error_class == "provider_refused:InsufficientFreeAddressesInSubnet"
    assert events == ["launch_capacity_fallback", "launch_refused", "replace_retreated"]
    assert (box.status, box.observed_state, box.desired_state, box.machine_generation) == ("stopped", "stopped", "stopped", 1)
    assert box.auth_readiness in ("unknown", "fenced"), "the API admits a Replace only in these readiness states"
    assert replacement is None, "the generation that never launched leaves no machine row"
    lines = [record.getMessage() for record in caplog.records if "operation_id=replace " in record.getMessage()]
    line = next(text for text in lines if "status=failed" in text)
    assert "provider_code=InsufficientFreeAddressesInSubnet" in line and "not enough free addresses" in line
    assert ec2.refusals == 3

    # The owner generation counter moved on (the API advances it per attempt); the previous machine is at 1.
    await _admit_next_replace(controller_database, 3)
    ec2.refusals = 0
    for _ in range(4):
        await LAUNCH.reconcile_setup(controller_database, ec2, "replace-3", "worker", ZONED_TEMPLATE)
    async with controller_database() as db:
        operation = await db.get(DevBoxOperation, "replace-3")
        box = await db.get(DevBox, "box")
        machines = {row.machine_generation: row for row in await db.scalars(select(DevBoxInstance))}
    assert operation.status == "succeeded" and box.status == "running" and box.machine_generation == 3
    assert machines[3].provider_instance_id == "i-replacement" and machines[1].observed_state == "stopped"
    assert ec2.volume["Attachments"][0]["InstanceId"] == "i-replacement"


@pytest.mark.asyncio
async def test_superseded_machine_is_retired_once_the_new_generation_is_verified_ready(controller_database):
    """#8223 cause 53: the machine a Replace detached the disk from stayed `stopped` forever, holding a subnet
    address and billing; six of them exhausted the box's subnet. Retirement runs only after readiness, only for
    generations below the box's, with the release path's ownership checks, and exactly once."""
    from controller.cleanup import retire_superseded_instances

    await admit_replace(controller_database, progress="attachment_requested", target_recorded=True)
    ec2 = ExhaustedSubnetEC2(refusals=0, attached_to="i-replacement")
    assert await reconcile(controller_database, ec2, "worker")
    operation, box, _, _ = await read(controller_database)
    assert operation.status == "succeeded" and box.status == "running"

    # Not before the host is verified: readiness is still pending.
    assert await retire_superseded_instances(controller_database, ec2, "example-deployment") == 0
    assert ec2.terminated == []

    async with controller_database.begin() as db:
        (await db.get(DevBox, "box")).auth_readiness = "ready"
    assert await retire_superseded_instances(controller_database, ec2, "example-deployment") == 1
    assert ec2.terminated == ["i-previous"], "only the superseded machine, never the current one"
    async with controller_database() as db:
        previous = await db.scalar(select(DevBoxInstance).where(DevBoxInstance.machine_generation == 1))
        current = await db.scalar(select(DevBoxInstance).where(DevBoxInstance.machine_generation == 2))
        events = list(await db.scalars(select(DevBoxEvent.event_type).order_by(DevBoxEvent.created_at)))
    assert previous.observed_state == "terminated" and current.observed_state == "running"
    assert "superseded_machine_retired" in events
    assert ec2.volume["Attachments"][0]["InstanceId"] == "i-replacement", "the workspace disk is untouched"
    # Idempotent: a terminated row is no longer a candidate.
    assert await retire_superseded_instances(controller_database, ec2, "example-deployment") == 0
    assert ec2.terminated == ["i-previous"]


@pytest.mark.asyncio
async def test_retirement_refuses_a_machine_whose_tags_no_longer_match_the_box(controller_database):
    await admit_replace(controller_database, progress="attachment_requested", target_recorded=True)
    ec2 = ExhaustedSubnetEC2(refusals=0, attached_to="i-replacement")
    assert await reconcile(controller_database, ec2, "worker")
    async with controller_database.begin() as db:
        (await db.get(DevBox, "box")).auth_readiness = "ready"
    ec2.instances["i-previous"]["Tags"] = tags(1, **{"adp:box-id": "another-box"})
    from controller.cleanup import retire_superseded_instances

    assert await retire_superseded_instances(controller_database, ec2, "example-deployment") == 0
    assert ec2.terminated == []
    async with controller_database() as db:
        previous = await db.scalar(select(DevBoxInstance).where(DevBoxInstance.machine_generation == 1))
    assert previous.observed_state == "stopped"

class RestoreEC2:
    def __init__(self):
        self.snapshot = {
            "SnapshotId": "snap-owned", "VolumeId": "vol-owned", "OwnerId": "000000000000",
            "VolumeSize": 40, "Encrypted": True, "KmsKeyId": "kms-example", "State": "completed",
            "Tags": tags(1, **{"adp:workspace": "persistent"}),
        }
        self.previous_volume = {"VolumeId": "vol-owned", "Attachments": [{"InstanceId": "i-previous", "State": "attached"}]}
        self.restored_volume = None
        self.creations = []

    def describe_snapshots(self, **request):
        assert request == {"SnapshotIds": ["snap-owned"]}
        return {"Snapshots": [self.snapshot]}

    def describe_volumes(self, **request):
        if "VolumeIds" in request:
            return {"Volumes": [self.restored_volume] if self.restored_volume else []}
        assert all(item["Name"].startswith("tag:") for item in request["Filters"])
        return {"Volumes": [self.restored_volume] if self.restored_volume else []}

    def create_volume(self, **request):
        self.creations.append(request)
        self.restored_volume = {
            "VolumeId": "vol-restored", "SnapshotId": request["SnapshotId"], "Size": request["Size"],
            "Encrypted": request["Encrypted"], "KmsKeyId": request["KmsKeyId"],
            "AvailabilityZone": request["AvailabilityZone"], "Tags": request["TagSpecifications"][0]["Tags"],
            "Attachments": [], "State": "available",
        }


def restore_records():
    from types import SimpleNamespace

    box = SimpleNamespace(id="box", user_id="example-owner", org_id="example-org", deployment_id="example-deployment")
    scope = dict(box_id="box", user_id="example-owner", org_id="example-org", deployment_id="example-deployment")
    source = SimpleNamespace(**scope, machine_generation=1, resource_kind="workspace_disk", provider_resource_id="vol-owned", size_gb=40, encrypted=True)
    snapshot = SimpleNamespace(**scope, machine_generation=1, resource_kind="snapshot", provider_resource_id="snap-owned", size_gb=40, encrypted=True, deletion_state="present")
    target = SimpleNamespace(**scope, machine_generation=2, resource_kind="workspace_disk", provider_resource_id="intent:restore", size_gb=40, encrypted=True)
    return box, snapshot, source, target


def test_snapshot_restore_creates_target_zone_volume_once_without_touching_previous_disk():
    ec2 = RestoreEC2()
    records = restore_records()
    for _ in range(2):
        volume = STORAGE.restore_volume_from_snapshot(ec2, *records, "us-east-1b", "kms-example", "000000000000")
        assert volume["VolumeId"] == "vol-restored"
    assert len(ec2.creations) == 1
    request = ec2.creations[0]
    assert request["AvailabilityZone"] == "us-east-1b" and request["SnapshotId"] == "snap-owned"
    assert request["Encrypted"] and request["KmsKeyId"] == "kms-example" and request["VolumeType"] == "gp3"
    assert {tag["Key"]: tag["Value"] for tag in request["TagSpecifications"][0]["Tags"]}["adp:generation"] == "2"
    assert ec2.previous_volume["Attachments"] == [{"InstanceId": "i-previous", "State": "attached"}]
    assert volume["Attachments"] == []


def test_snapshot_restore_fills_a_larger_template_disk_and_refuses_a_smaller_one():
    """#8835 disk growth: the template grew after the snapshot; the guest's xfs_growfs fills the extra space."""
    ec2 = RestoreEC2()
    records = list(restore_records())
    records[3].size_gb = 120
    volume = STORAGE.restore_volume_from_snapshot(ec2, *records, "us-east-1b", "kms-example", "000000000000")
    assert volume["Size"] == 120 and ec2.creations[0]["Size"] == 120
    ec2 = RestoreEC2()
    records = list(restore_records())
    records[3].size_gb = 20
    with pytest.raises(ValueError, match="unverified"):
        STORAGE.restore_volume_from_snapshot(ec2, *records, "us-east-1b", "kms-example", "000000000000")
    assert ec2.creations == []


@pytest.mark.parametrize("unsafe_change", [
    ("record", "user_id", "another-owner"),
    ("record", "box_id", "another-box"),
    ("record", "machine_generation", 4),
    ("provider", "OwnerId", "111111111111"),
    ("provider", "VolumeId", "vol-other"),
    ("provider", "KmsKeyId", "kms-other"),
    ("provider", "Encrypted", False),
    ("provider", "State", "pending"),
])
def test_snapshot_restore_rejects_unverified_source_before_creating_volume(unsafe_change):
    ec2 = RestoreEC2()
    records = list(restore_records())
    origin, field, value = unsafe_change
    if origin == "record":
        setattr(records[1], field, value)
    else:
        ec2.snapshot[field] = value
    with pytest.raises(ValueError, match="unverified"):
        STORAGE.restore_volume_from_snapshot(ec2, *records, "us-east-1b", "kms-example", "000000000000")
    assert ec2.creations == []
    assert ec2.previous_volume["Attachments"][0]["InstanceId"] == "i-previous"

class SnapshotReplaceEC2(WorkspaceEC2):
    SNAPSHOT_ID = "snap-0123456789abcdef0"

    def __init__(self, *, invalid_owner=False):
        super().__init__(attached_to="i-previous")
        self.restored_volume = None
        self.invalid_owner = invalid_owner
        self.snapshot_creations = []

    @staticmethod
    def replacement():
        replacement = WorkspaceEC2.replacement()
        replacement["Placement"] = {"AvailabilityZone": "us-east-1b"}
        return replacement

    def describe_snapshots(self, **request):
        assert request == {"SnapshotIds": [self.SNAPSHOT_ID]}
        return {"Snapshots": [{
            "SnapshotId": self.SNAPSHOT_ID, "VolumeId": "vol-owned", "OwnerId": "111111111111" if self.invalid_owner else "000000000000",
            "VolumeSize": 40, "Encrypted": True, "KmsKeyId": "kms-example", "State": "completed",
            "Tags": tags(1, **{"adp:workspace": "persistent"}),
        }]}

    def describe_volumes(self, **request):
        if request.get("VolumeIds") == ["vol-owned"]:
            return {"Volumes": [self.volume]}
        if request.get("VolumeIds") == ["vol-restored"]:
            return {"Volumes": [self.restored_volume] if self.restored_volume else []}
        assert "Filters" in request
        return {"Volumes": [self.restored_volume] if self.restored_volume else []}

    def create_volume(self, **request):
        self.snapshot_creations.append(request)
        self.restored_volume = {
            "VolumeId": "vol-restored", "SnapshotId": request["SnapshotId"], "Size": request["Size"],
            "Encrypted": request["Encrypted"], "KmsKeyId": request["KmsKeyId"], "AvailabilityZone": request["AvailabilityZone"],
            "Tags": request["TagSpecifications"][0]["Tags"], "State": "available", "Attachments": [],
        }

    def attach_volume(self, **request):
        assert request["VolumeId"] == "vol-restored"
        self.calls.append(("attach", request["InstanceId"]))
        self.restored_volume["State"] = "in-use"
        self.restored_volume["Attachments"] = [{"InstanceId": request["InstanceId"], "State": "attached", "Device": request["Device"]}]
        if self.lose_attachment:
            raise ConnectionError("the attach response was lost")

    def terminate_instances(self, **request):
        assert request == {"InstanceIds": ["i-replacement"]}
        self.calls.append(("terminate", "i-replacement"))
        self.instances["i-replacement"]["State"]["Name"] = "terminated"
        if self.restored_volume and self.restored_volume["Attachments"] == [{
            "InstanceId": "i-replacement", "State": "attached", "Device": "/dev/xvdf",
        }]:
            self.restored_volume["Attachments"] = []
            self.restored_volume["State"] = "available"

    def delete_volume(self, **request):
        assert request == {"VolumeId": "vol-restored"} and self.restored_volume["State"] == "available"
        self.calls.append(("delete", "vol-restored"))
        self.restored_volume = None


async def selected_replace(factory):
    await admit_replace(factory, progress="admitted")
    async with factory() as db:
        db.add(DevBoxResource(id="snapshot-selected", box_id="box", user_id="example-owner", org_id="example-org",
                              deployment_id="example-deployment", machine_generation=1, resource_kind="snapshot",
                              provider_resource_id=SnapshotReplaceEC2.SNAPSHOT_ID, size_gb=40, encrypted=True,
                              retention_state="retained", deletion_state="present", last_observed_state="present"))
        db.add(DevBoxEvent(id="selection", box_id="box", user_id="example-owner", org_id="example-org",
                           deployment_id="example-deployment", machine_generation=2, operation_id="replace",
                           event_type=f"restore:{SnapshotReplaceEC2.SNAPSHOT_ID}"))
        await db.commit()


@pytest.mark.asyncio
async def test_replace_restores_selected_snapshot_in_new_zone_without_detaching_old_disk(controller_database):
    ec2 = SnapshotReplaceEC2()
    await selected_replace(controller_database)
    assert await reconcile(controller_database, ec2, "first-worker")
    operation, box, replacement, events = await read(controller_database)
    assert operation.status == "succeeded" and box.status == "running" and replacement.availability_zone == "us-east-1b"
    assert len(ec2.snapshot_creations) == 1
    assert ec2.snapshot_creations[0]["AvailabilityZone"] == "us-east-1b"
    assert ec2.restored_volume["Attachments"][0]["InstanceId"] == "i-replacement"
    assert ec2.volume["Attachments"][0]["InstanceId"] == "i-previous"
    assert ("detach", "i-previous") not in ec2.calls and events == [f"restore:{ec2.SNAPSHOT_ID}"]
    async with controller_database() as db:
        disks = list((await db.scalars(select(DevBoxResource).where(DevBoxResource.resource_kind == "workspace_disk").order_by(DevBoxResource.machine_generation))).all())
    assert [(disk.machine_generation, disk.provider_resource_id) for disk in disks] == [(1, "vol-owned"), (2, "vol-restored")]


@pytest.mark.asyncio
async def test_snapshot_restore_retries_lost_attach_without_recreating_or_detaching_old_disk(controller_database):
    ec2 = SnapshotReplaceEC2()
    ec2.lose_attachment = True
    await selected_replace(controller_database)
    assert await reconcile(controller_database, ec2, "first-worker")
    operation, _, _, _ = await read(controller_database)
    assert operation.status == "unknown" and operation.error_class == "workspace_provider_unknown"
    ec2.lose_attachment = False
    assert await reconcile(controller_database, ec2, "restarted-worker")
    operation, box, _, _ = await read(controller_database)
    assert operation.status == "succeeded" and box.status == "running"
    assert len(ec2.snapshot_creations) == 1 and ec2.volume["Attachments"][0]["InstanceId"] == "i-previous"


@pytest.mark.asyncio
async def test_replace_rejects_foreign_snapshot_without_attaching_or_detaching_old_disk(controller_database):
    ec2 = SnapshotReplaceEC2(invalid_owner=True)
    await selected_replace(controller_database)
    assert await reconcile(controller_database, ec2, "first-worker")
    operation, box, _, _ = await read(controller_database)
    assert operation.status == "unknown" and operation.progress == "restore_cleanup" and box.status == "requested"
    assert await reconcile(controller_database, ec2, "second-worker")
    operation, box, _, _ = await read(controller_database)
    assert operation.status == "unknown", (operation.status, operation.progress, operation.error_class, box.status)
    assert await reconcile(controller_database, ec2, "third-worker")
    operation, box, _, _ = await read(controller_database)
    assert operation.status == "failed" and operation.error_class == "snapshot_ownership_mismatch"
    assert box.status == "stopped" and box.machine_generation == 1 and ec2.snapshot_creations == []
    assert ec2.instances["i-replacement"]["State"]["Name"] == "terminated"
    assert ec2.volume["Attachments"][0]["InstanceId"] == "i-previous"
    assert ("detach", "i-previous") not in ec2.calls

@pytest.mark.asyncio
async def test_failed_restore_cleanup_finishes_after_policy_revocation_without_touching_previous_disk(controller_database):
    ec2 = SnapshotReplaceEC2(invalid_owner=True)
    await selected_replace(controller_database)
    assert await reconcile(controller_database, ec2, "first-worker")
    async with controller_database() as db:
        policy = await db.get(DevBoxPolicy, "policy")
        policy.enabled = False
        await db.commit()
    assert await reconcile(controller_database, ec2, "second-worker")
    assert await reconcile(controller_database, ec2, "third-worker")
    operation, box, replacement, _ = await read(controller_database)
    assert operation.status == "failed" and operation.error_class == "snapshot_ownership_mismatch"
    assert box.machine_generation == 1 and box.status == "stopped"
    assert replacement.observed_state == "terminated"
    assert ec2.volume["Attachments"][0]["InstanceId"] == "i-previous"
    assert ("detach", "i-previous") not in ec2.calls

@pytest.mark.asyncio
async def test_restore_does_not_attach_existing_snapshot_volume_on_foreign_instance(controller_database, monkeypatch):
    monkeypatch.setattr(LAUNCH, "DEV_BOX_LAUNCH_RETRY_BOUND", 1)
    ec2 = SnapshotReplaceEC2()
    ec2.restored_volume = {
        "VolumeId": "vol-restored", "SnapshotId": ec2.SNAPSHOT_ID, "Size": 40,
        "Encrypted": True, "KmsKeyId": "kms-example", "AvailabilityZone": "us-east-1b",
        "Tags": tags(2, **{"adp:workspace": "persistent"}), "State": "in-use",
        "Attachments": [{"InstanceId": "i-stranger", "State": "attached", "Device": "/dev/xvdf"}],
    }
    await selected_replace(controller_database)
    assert await reconcile(controller_database, ec2, "first-worker")
    assert await reconcile(controller_database, ec2, "second-worker")
    operation, box, _, _ = await read(controller_database)
    assert operation.status == "unknown" and operation.progress == "restore_cleanup"
    assert box.status == "requested" and ec2.snapshot_creations == []
    assert ec2.restored_volume["Attachments"][0]["InstanceId"] == "i-stranger"
    assert ec2.volume["Attachments"][0]["InstanceId"] == "i-previous"
    assert await reconcile(controller_database, ec2, "third-worker")
    operation, box, _, _ = await read(controller_database)
    assert operation.status == "unknown", (operation.status, operation.progress, operation.error_class, box.status)
    assert await reconcile(controller_database, ec2, "fourth-worker")
    operation, box, _, _ = await read(controller_database)
    assert operation.status == "unknown" and box.status == "requested"
    assert ec2.restored_volume["Attachments"][0]["InstanceId"] == "i-stranger"
    ec2.restored_volume["Attachments"] = []
    ec2.restored_volume["State"] = "available"
    assert await reconcile(controller_database, ec2, "fifth-worker")
    assert await reconcile(controller_database, ec2, "sixth-worker")
    operation, box, _, _ = await read(controller_database)
    assert operation.status == "failed" and operation.error_class == "workspace_attached_elsewhere"
    assert box.status == "stopped" and box.machine_generation == 1
    assert ec2.restored_volume is None and ec2.volume["Attachments"][0]["InstanceId"] == "i-previous"
    assert all(action[0] not in ("attach", "detach") for action in ec2.calls)
