"""Reconcile admitted setup operations without replaying an uncertain launch."""

import asyncio
import hashlib
import ipaddress
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import or_, select, update
from src.shared.models.base import new_uuid
from src.shared.models.dev_box import (
    DevBoxEvent,
    DevBoxInstance,
    DevBoxOperation,
    DevBoxTemplate,
    DevBoxResource,
)

from controller.fencing import current as current_operation
from controller.fencing import finish, maintained_lease
from controller.storage import PAST_DETACH_PROGRESS, observed_volume, prepare_replacement, workspace, previous_machine_filter, previous_machine_order

logger = logging.getLogger(__name__)

# #8037: an unconfirmed launch is re-sent with the SAME ClientToken for at most this many claims
# (`DevBoxOperation.retries`) and only while the token is inside EC2's 24 h idempotency window.
# The gateway (src/cloud_desktop/routes.py) reads the same environment variable to decide when a
# parked `unknown` launch no longer blocks release/replace; the controller code ships as a ConfigMap
# on the gateway image, so neither side imports the other's constant.
DEV_BOX_LAUNCH_RETRY_BOUND = int(os.environ.get("DEV_BOX_LAUNCH_RETRY_BOUND", "12"))
DEV_BOX_LAUNCH_TOKEN_WINDOW = timedelta(hours=23)

# #8100: provider observations that may still change on a later tick. They park the operation as `unknown`
# while the retry window is open; once the SAME observation is reported again past the bound the operation
# fails with that error class (event `recovery_exhausted`, StateReason-style detail in the log), so the owner
# can release or replace instead of waiting on a disk or machine the controller will never see move. A clean
# tick clears `error_class`, so one late transient does not fail a slow but healthy launch. `provider_unknown`
# (the launch inventory itself could not be read) is deliberately NOT bounded: #8037 keeps a launch whose
# existence is unconfirmed `unknown` rather than letting a release settle it blind.
UNRESOLVED_OBSERVATIONS = frozenset({
    "workspace_provider_unknown", "workspace_not_observed", "attachment_not_observed",
    "detach_not_observed", "workspace_attached_elsewhere", "replacement_old_instance_unverified",
})

# Synchronous RunInstances error codes that mean EC2 rejected the request and launched nothing.
# Everything else (throttling, RequestLimitExceeded, timeouts, lost responses) may have reached EC2.
PROVIDER_REFUSAL_CODES = frozenset({
    "UnauthorizedOperation", "InstanceLimitExceeded", "VcpuLimitExceeded",
    "InsufficientInstanceCapacity", "MissingParameter",
    # #8223 cause 53: the launch subnet has no free address / no free CIDR block. EC2 answers
    # synchronously and launches nothing; dev 2026-10-22 saw 13 of these in a row read as
    # `provider_unknown` and the operation end `launch_unconfirmed` with the code thrown away.
    "InsufficientFreeAddressesInSubnet", "InsufficientCidrBlocks",
})
PROVIDER_REFUSAL_PREFIXES = ("InvalidParameter", "Unsupported", "InvalidAMIID")
# #8423: synchronous refusals that mean "not this (type, zone) right now", so the next admitted
# size or approved subnet is tried before the operation fails. All are definite: nothing
# launched, so the next attempt may carry a fresh ClientToken without risking a second machine.
# `Unsupported` is EC2's answer when a type is not offered in the requested zone; the subnet
# codes mean this subnet is full, so the next approved subnet in the same zone is tried.
CAPACITY_REFUSAL_CODES = frozenset({
    "InsufficientInstanceCapacity", "Unsupported", "InsufficientFreeAddressesInSubnet", "InsufficientCidrBlocks",
})
# Synchronous answers that mean EC2 did NOT process the request (throttled or a transient service
# fault): the same ClientToken is re-sent after a backoff, under a longer bound than a lost response,
# because nothing is uncertain about them (#8223 cause 52).
PROVIDER_RETRYABLE_CODES = frozenset({
    "RequestLimitExceeded", "InternalError", "InternalFailure", "ServiceUnavailable", "Unavailable", "RequestExpired",
})
PROVIDER_RETRYABLE_PREFIXES = ("Throttling",)
DEV_BOX_LAUNCH_THROTTLE_RETRY_BOUND = int(os.environ.get("DEV_BOX_LAUNCH_THROTTLE_RETRY_BOUND", "40"))
# Seconds the operation's lease is held after a throttled send, so the next claim (and the next
# RunInstances) waits instead of hammering a rate-limited API on the tick cadence.
DEV_BOX_LAUNCH_THROTTLE_BACKOFF_SECONDS = int(os.environ.get("DEV_BOX_LAUNCH_THROTTLE_BACKOFF_SECONDS", "45"))
LAUNCH_REF = re.compile(r"^launch:(?P<token>[0-9a-f]{64})(?:#(?P<attempt>[0-9]+))?$")
# EC2 StateReason code for a stop the owner asked for; any other reason on a machine that never ran is a launch failure.
USER_INITIATED_SHUTDOWN = "Client.UserInitiatedShutdown"


@dataclass(frozen=True)
class LaunchTemplate:
    template_id: str
    version: str
    region: str
    image_id: str | None = None
    revision: str | None = None
    instance_size: str | None = None
    disk_gb: int | None = None
    kms_key_arn: str | None = None
    profile_revision: str | None = None
    source_revision: str | None = None
    gateway_execute_arn: str | None = None
    crew_model_id: str | None = None
    account_id: str | None = None
    # Crew bootstrap (#8055): "image" (profile baked into image_id) or "user-data"
    # (installed at first boot from the pinned bundle; the host must present the
    # bootstrap receipt for crew_bundle_sha256 before any profile check passes).
    crew_bootstrap: str = "image"
    crew_bundle_sha256: str | None = None
    crew_source_revision: str | None = None
    # Capacity fallback (#8423): ordered admitted sizes (preferred first) and approved private
    # subnets as (subnet_id, availability_zone) pairs, the template's own subnet first. A subnet
    # override needs the guest security group because RunInstances replaces the template's
    # network interface wholesale. Rates price the size that actually launched (usage.py).
    fallback_sizes: tuple = ()
    subnets: tuple = ()
    security_group_id: str | None = None
    instance_hour_rates_usd: dict | None = None


@dataclass(frozen=True)
class ReleasePins:
    """What a generation was provisioned from: the Crew profile revision, the commit the
    installed profile declares, the bundle digest (user-data mode) and the launch template
    version. Carries the attribute names ``host_profile_command`` and ``attest_release_host``
    read, so it stands in for a ``LaunchTemplate`` wherever a running box is verified.

    Recorded on the ``DevBoxInstance`` row at launch and read back from the host's first-boot
    receipt for rows that predate the column (#8223 cause 34): a same-generation restart must
    be verified against the release it was provisioned from, not the one current today.
    """
    profile_revision: str | None
    source_revision: str | None
    crew_bundle_sha256: str | None = None
    crew_bootstrap: str = "image"
    image_id: str | None = None
    launch_template_version: str | None = None


def release_pins(launch_template):
    """The pins a launch from ``launch_template`` records on its generation."""
    user_data = launch_template.crew_bootstrap == "user-data"
    return ReleasePins(
        profile_revision=launch_template.profile_revision,
        source_revision=launch_template.crew_source_revision or launch_template.source_revision,
        crew_bundle_sha256=launch_template.crew_bundle_sha256 if user_data else None,
        crew_bootstrap=launch_template.crew_bootstrap,
        image_id=launch_template.image_id,
        launch_template_version=launch_template.version,
    )


def ownership_tags(box, operation):
    return {
        "adp:component": "dev-box",
        "adp:deployment-id": box.deployment_id,
        "adp:owner-id": box.user_id,
        "adp:box-id": box.id,
        "adp:generation": str(operation.machine_generation),
    }


def private_host_address(observed):
    try:
        address = ipaddress.IPv4Address(observed.get("PrivateIpAddress"))
    except (ipaddress.AddressValueError, TypeError):
        return None
    if any(address in network for network in (
        ipaddress.IPv4Network("10.0.0.0/8"),
        ipaddress.IPv4Network("172.16.0.0/12"),
        ipaddress.IPv4Network("192.168.0.0/16"),
    )):
        return str(address)
    return None


def inventory(ec2, operation, tags):
    filters = [{"Name": "client-token", "Values": [operation.provider_token]}]
    filters.extend({"Name": f"tag:{key}", "Values": [value]} for key, value in tags.items())
    response = ec2.describe_instances(Filters=filters)
    instances = [instance for reservation in response.get("Reservations", []) for instance in reservation.get("Instances", [])]
    for instance in instances:
        actual = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
        if any(actual.get(key) != value for key, value in tags.items()) or instance.get("ClientToken") != operation.provider_token:
            raise ValueError("Provider inventory does not match launch intent")
    if len(instances) > 1:
        raise ValueError("Multiple instances match one launch intent")
    return instances[0] if instances else None


def provider_error_detail(error):
    """(code, message) of a provider exception for the operation log: the EC2 error code when there is one,
    else the exception class. Never request parameters or user-data (#8223 cause 52: these were dropped)."""
    if isinstance(error, ClientError):
        payload = error.response.get("Error", {}) if isinstance(error.response, dict) else {}
        return payload.get("Code") or type(error).__name__, str(payload.get("Message") or error)[:400]
    return type(error).__name__, str(error)[:400]


def provider_refusal(error):
    """Return the EC2 error code when RunInstances definitely refused the launch, else None."""
    code = error.response.get("Error", {}).get("Code") if isinstance(error, ClientError) else None
    if code and (code in PROVIDER_REFUSAL_CODES or code.startswith(PROVIDER_REFUSAL_PREFIXES)):
        return code
    return None


def provider_retryable(error):
    """Return the EC2 error code when the request was throttled or the service faulted before processing it."""
    code = error.response.get("Error", {}).get("Code") if isinstance(error, ClientError) else None
    if code and (code in PROVIDER_RETRYABLE_CODES or code.startswith(PROVIDER_RETRYABLE_PREFIXES)):
        return code
    return None


def launch_termination(observed):
    """Return (code, message) when EC2 ended the machine at launch (e.g. Client.InvalidKMSKey.InvalidState), else None.

    #8037: RunInstances can succeed and EC2 still terminate the instance seconds later; the inventory then shows
    `shutting-down`/`terminated` (or `stopped` with a non-owner StateReason) forever, which must not be mistaken for
    "observed, not running yet".
    """
    state = observed.get("State", {}).get("Name")
    reason = observed.get("StateReason") or {}
    code, message = reason.get("Code"), reason.get("Message")
    if state in ("terminated", "shutting-down"):
        return code, message
    if state == "stopped" and code and code != USER_INITIATED_SHUTDOWN:
        return code, message
    return None


def retry_window_open(operation, retries, now):
    """True while the operation may still be retried: within the claim bound and EC2's ClientToken idempotency window.

    For a launch this is exactly when re-sending RunInstances with operation.provider_token is still idempotent; the
    same window bounds how long an unresolved provider observation (#8100) is re-read before the operation fails.
    """
    created = operation.created_at.replace(tzinfo=UTC) if operation.created_at.tzinfo is None else operation.created_at
    # A throttled send never reached the provider, so it is given more claims than a lost response (#8223 cause 52).
    bound = DEV_BOX_LAUNCH_THROTTLE_RETRY_BOUND if operation.error_class == "provider_throttled" else DEV_BOX_LAUNCH_RETRY_BOUND
    return retries <= bound and now - created < DEV_BOX_LAUNCH_TOKEN_WINDOW


def settle_unresolved(db, operation, error, claimed, now):
    """Park an unresolved tick as `unknown`, or fail it once the same observation has outlived the retry window (#8100).

    Returns the StateReason-style detail for the log line, if the error carried one.
    """
    code = str(error)
    detail = getattr(error, "detail", None)
    exhausted = code in UNRESOLVED_OBSERVATIONS and operation.error_class == code and not retry_window_open(operation, claimed, now)
    operation.status = "failed" if exhausted else "unknown"
    operation.error_class = code
    if exhausted:
        record_event(db, operation, "recovery_exhausted")
        return f"{detail or 'the provider observation did not change'}; unresolved after {claimed} reconciles, the owner may release or replace"
    return detail


async def replacement_target(db, ec2, box, operation):
    """The instance this replace launched, if any: the recorded generation row, else inventory by the launch token (#8100).

    The row's provider id is written in the same tick that first observes the machine running, after the attach
    step; a controller that dies in between knows the disk is on its own instance only through the ClientToken.
    """
    recorded = await db.scalar(select(DevBoxInstance.provider_instance_id).where(
        DevBoxInstance.box_id == box.id, DevBoxInstance.machine_generation == operation.machine_generation,
    ))
    if recorded or operation.progress not in PAST_DETACH_PROGRESS:
        return recorded
    try:
        observed = await asyncio.to_thread(inventory, ec2, operation, ownership_tags(box, operation))
    except (ValueError, OSError, BotoCoreError, ClientError):
        return None
    return observed["InstanceId"] if observed else None


def record_event(db, operation, event_type):
    db.add(DevBoxEvent(
        id=new_uuid(), box_id=operation.box_id, user_id=operation.user_id, org_id=operation.org_id,
        deployment_id=operation.deployment_id, machine_generation=operation.machine_generation,
        operation_id=operation.id, event_type=event_type,
    ))


def log_outcome(operation, *, launched=False, provider_code=None, provider_message=None):
    """One structured line per tick that changed an operation, for the controller pod log."""
    logger.log(
        logging.WARNING if operation.status == "failed" else logging.INFO,
        "dev-box operation reconciled operation_id=%s box_id=%s action=%s launched=%s status=%s progress=%s "
        "error_class=%s provider_code=%s provider_message=%r retries=%s",
        operation.id, operation.box_id, operation.action, launched, operation.status, operation.progress,
        operation.error_class, provider_code, (provider_message or "")[:400] or None, operation.retries,
    )


def launch_attempts(launch_template, *, zone=None):
    """Ordered (size, subnet_id, availability_zone) attempts for one launch (#8423).

    Sizes vary first within a subnet, then the next approved subnet is tried, so a box lands in the
    template's zone whenever any admitted size is available there. `zone` restricts the subnets to
    one availability zone: a replace must launch where the retained workspace disk is (EBS volumes
    never leave their zone). A manifest without a plan yields exactly the template's own launch.
    """
    sizes = tuple(launch_template.fallback_sizes) or (launch_template.instance_size,)
    subnets = tuple(launch_template.subnets) or ((None, None),)
    if zone is not None:
        subnets = tuple(item for item in subnets if item[1] is None or item[1] == zone)
    return [(size, subnet, subnet_zone) for subnet, subnet_zone in subnets for size in sizes]


def attempt_token(base_token, attempt):
    """ClientToken of attempt N: the operation's own token first, then a digest of it and the index."""
    if attempt == 0:
        return base_token
    return hashlib.sha256(f"{base_token}:fallback:{attempt}".encode()).hexdigest()


def launch_reference(operation):
    """(base token, attempt index) recorded in provider_request_ref; (provider_token, 0) before any launch."""
    match = LAUNCH_REF.match(operation.provider_request_ref or "")
    if match is None:
        return operation.provider_token, 0
    return match.group("token"), int(match.group("attempt") or 0)


def run_instances_arguments(launch_template, token, tag_list, attempt):
    """RunInstances arguments for one attempt; attempt 0 is the template exactly as approved."""
    size, subnet, _ = attempt
    arguments = {
        "ClientToken": token,
        "LaunchTemplate": {"LaunchTemplateId": launch_template.template_id, "Version": launch_template.version},
        "MinCount": 1, "MaxCount": 1,
        # The controller policy authorizes RunInstances on instance, volume AND
        # network-interface resources only when each carries the ownership
        # request tags, so the primary ENI must be tagged at launch too.
        "TagSpecifications": [{"ResourceType": kind, "Tags": tag_list} for kind in ("instance", "volume", "network-interface")],
    }
    if size is not None and size != launch_template.instance_size:
        arguments["InstanceType"] = size
    first_subnet = launch_template.subnets[0][0] if launch_template.subnets else None
    if subnet is not None and subnet != first_subnet:
        # Overriding the template's interface replaces it wholesale: keep the private placement
        # and the guest group the template carries (IAM: LaunchOwnedInterface, LaunchApprovedInputs).
        arguments["NetworkInterfaces"] = [{
            "DeviceIndex": 0, "SubnetId": subnet, "Groups": [launch_template.security_group_id],
            "AssociatePublicIpAddress": False,
        }]
    return arguments


async def rotate_attempt(session_factory, operation_id, worker_id, claimed, base_token, attempt, size):
    """Persist the next attempt's token and size before it is sent; False when the operation is no longer ours."""
    async with session_factory() as db:
        operation, box, eligibility = await current_operation(db, operation_id, worker_id, claimed)
        if eligibility:
            await db.rollback()
            return False
        operation.provider_token = attempt_token(base_token, attempt)
        operation.provider_request_ref = f"launch:{base_token}#{attempt}"
        instance = await db.scalar(select(DevBoxInstance).where(
            DevBoxInstance.box_id == box.id, DevBoxInstance.machine_generation == operation.machine_generation,
        ))
        if instance is not None:
            instance.instance_size = size
        record_event(db, operation, "launch_capacity_fallback")
        await db.commit()
        return True


def attempts_detail(tried):
    """One line naming every (size, zone) EC2 refused, for the controller log and the operator."""
    codes = sorted({code for *_, code in tried})
    return "EC2 refused every approved launch: " + "; ".join(
        f"{size or 'template size'} in {zone or subnet or 'template subnet'}" for size, subnet, zone, _ in tried
    ) + f" ({', '.join(codes)})"


async def reconcile_setup(session_factory, ec2, operation_id, worker_id, launch_template, *, ssm=None, lease_seconds=60):
    """Return false if another worker owns the lease; never relaunch after an uncertain send."""
    now = datetime.now(UTC)
    async with session_factory() as db:
        claimed = await db.scalar(
            update(DevBoxOperation)
            .where(
                DevBoxOperation.id == operation_id,
                DevBoxOperation.action.in_(("setup", "replace")),
                DevBoxOperation.status.in_(("pending", "unknown")),
                or_(DevBoxOperation.lease_expires_at.is_(None), DevBoxOperation.lease_expires_at <= now),
            )
            .values(lease_owner=worker_id, lease_expires_at=now + timedelta(seconds=lease_seconds), retries=DevBoxOperation.retries + 1)
            .returning(DevBoxOperation.retries)
        )
        if claimed is None:
            await db.rollback()
            return False
        operation, box, eligibility = await current_operation(db, operation_id, worker_id, claimed, cleanup=True)
        if operation is None:
            await db.rollback()
            return False
        if operation.action == "replace" and operation.progress == "restore_cleanup" and not eligibility:
            await db.commit()
            cleaned = await cleanup_failed_restore(session_factory, ec2, operation_id, worker_id, claimed, launch_template)
            async with session_factory() as cleanup_db:
                await cleanup_db.execute(update(DevBoxOperation).where(
                    DevBoxOperation.id == operation_id, DevBoxOperation.lease_owner == worker_id,
                    DevBoxOperation.retries == claimed, DevBoxOperation.status == "unknown",
                ).values(lease_owner=None, lease_expires_at=None))
                await cleanup_db.commit()
            return cleaned
        if eligibility:
            finish(operation, eligibility)
            log_outcome(operation)
            await db.commit()
            return True
        template = await db.get(DevBoxTemplate, box.template_id)
        if (box.machine_generation != operation.machine_generation or box.desired_state != "running" or not template.approved
                or (launch_template.image_id is not None and template.image_ref != launch_template.image_id)
                or (launch_template.revision is not None and box.template_revision != launch_template.revision)
                or (launch_template.instance_size is not None and template.instance_size != launch_template.instance_size)
                or (launch_template.disk_gb is not None and template.disk_gb != launch_template.disk_gb)):
            operation.status = "unknown"
            operation.error_class = "invalid_launch_intent"
            operation.lease_owner = None
            operation.lease_expires_at = None
            log_outcome(operation)
            await db.commit()
            return True
        restore_snapshot_id = None
        if operation.action == "replace":
            restore_event = await db.scalar(select(DevBoxEvent.event_type).where(
                DevBoxEvent.operation_id == operation.id,
                DevBoxEvent.box_id == box.id,
                DevBoxEvent.machine_generation == operation.machine_generation,
                DevBoxEvent.event_type.like("restore:snap-%"),
            ))
            restore_snapshot_id = restore_event.removeprefix("restore:") if restore_event else None
            await db.commit()
            target_id = await replacement_target(db, ec2, box, operation)
            replacement_error = await prepare_replacement(
                session_factory, ec2, operation_id, worker_id, claimed, launch_template, target_instance_id=target_id,
                restore_snapshot_id=restore_snapshot_id,
            )
            db.expire_all()
            operation, box, eligibility = await current_operation(db, operation_id, worker_id, claimed)
            if operation is None:
                await db.rollback()
                return False
            if replacement_error or eligibility:
                detail = settle_unresolved(db, operation, replacement_error or eligibility, claimed, now)
                finish(operation)
                log_outcome(operation, provider_message=detail)
                await db.commit()
                return True
        instance = await db.scalar(
            select(DevBoxInstance).where(DevBoxInstance.box_id == box.id, DevBoxInstance.machine_generation == operation.machine_generation)
        )
        # #8423: a replace must launch in the zone of the retained workspace disk, which is the
        # zone of the machine it replaces; a fresh box may use any approved subnet.
        zone = None
        if operation.action == "replace" and restore_snapshot_id is None:
            zone = await db.scalar(select(DevBoxInstance.availability_zone).where(
                *previous_machine_filter(box.id, operation.machine_generation)
            ).order_by(previous_machine_order()).limit(1))
        attempts = launch_attempts(launch_template, zone=zone)
        base_token, attempt_index = launch_reference(operation)
        attempt_index = min(attempt_index, max(len(attempts) - 1, 0))
        first_attempt = operation.progress == "admitted" and instance is None
        # #8037: a launch that was sent (progress == "launch_intent") but never observed is re-sent with the
        # SAME ClientToken. EC2 de-duplicates RunInstances on that token for 24 h, so an earlier send that did
        # reach the provider yields the same instance instead of a second one; that token reuse is what keeps
        # the "never relaunch after an uncertain send" guarantee. Once the retry bound or the token window
        # has passed the launch is no longer re-sent and, if inventory is still empty, the operation fails.
        retry_launch = operation.progress == "launch_intent" and retry_window_open(operation, claimed, now)
        launch_exhausted = operation.progress == "launch_intent" and not retry_launch
        if instance is None:
            pins = release_pins(launch_template)
            instance = DevBoxInstance(
                id=new_uuid(), box_id=box.id, user_id=box.user_id, org_id=box.org_id, deployment_id=box.deployment_id,
                machine_generation=operation.machine_generation, template_revision=box.template_revision,
                image_ref=template.image_ref, region=launch_template.region,
                instance_size=(attempts[attempt_index][0] if attempts else None) or template.instance_size,
                provider_account_ref=launch_template.account_id,
                observed_state="unknown",
                # The generation's release (#8223 cause 34); a Replace launches a new generation
                # and so records the release current at that moment.
                profile_revision=pins.profile_revision, source_revision=pins.source_revision,
                crew_bundle_sha256=pins.crew_bundle_sha256, launch_template_version=pins.launch_template_version,
            )
            db.add(instance)
        if operation.progress == "admitted":
            operation.progress = "launch_intent"
        if not operation.provider_request_ref:
            operation.provider_request_ref = f"launch:{base_token}"
        await db.commit()

    tags = ownership_tags(box, operation)
    error = None
    observed = None
    launched = False
    refused = None
    plan_exhausted = False
    tried = []
    provider_error = None
    try:
        observed = await asyncio.to_thread(inventory, ec2, operation, tags)
        if observed is None and (first_attempt or retry_launch) and not attempts:
            # #8423: no approved subnet in the zone of the retained workspace disk; nothing to send.
            plan_exhausted = True
        elif observed is None and (first_attempt or retry_launch):
            async with session_factory() as db:
                _, _, eligibility = await current_operation(db, operation_id, worker_id, claimed)
                await db.rollback()
            if eligibility:
                raise ValueError("Launch admission changed before provider call")
            tag_list = [{"Key": key, "Value": value} for key, value in tags.items()]
            launched = True
            while True:
                attempt = attempts[attempt_index]
                try:
                    async with maintained_lease(session_factory, operation_id, worker_id, claimed, lease_seconds):
                        await asyncio.to_thread(
                            ec2.run_instances, **run_instances_arguments(launch_template, operation.provider_token, tag_list, attempt),
                        )
                except ClientError as failure:
                    # Only a synchronous RunInstances error is classified: a refused describe says nothing about the launch.
                    provider_error = provider_error_detail(failure)
                    refused = provider_refusal(failure)
                    if refused is None:
                        raise
                    tried.append((*attempt, refused))
                    if refused in CAPACITY_REFUSAL_CODES and attempt_index + 1 < len(attempts):
                        # #8423: a definite refusal launched nothing, so the next (size, subnet) may go out
                        # under a fresh ClientToken. The rotation is committed before the send so a controller
                        # that dies in between re-sends exactly this attempt with this token (#8037 invariant).
                        attempt_index += 1
                        if not await rotate_attempt(session_factory, operation_id, worker_id, claimed, base_token, attempt_index, attempts[attempt_index][0]):
                            raise ValueError("Launch admission changed before provider call")
                        operation.provider_token = attempt_token(base_token, attempt_index)
                        operation.provider_request_ref = f"launch:{base_token}#{attempt_index}"
                        refused = None
                        continue
                    break
                else:
                    observed = await asyncio.to_thread(inventory, ec2, operation, tags)
                    break
    except ValueError:
        error = "ambiguous_inventory"
    except (OSError, BotoCoreError, ClientError) as failure:
        if provider_error is None:
            provider_error = provider_error_detail(failure)
        # A throttled or faulted send was not processed by EC2: re-send the same token after a backoff.
        # Anything else (lost response, transport error) may have reached the provider and stays `provider_unknown`.
        error = "provider_throttled" if launched and provider_retryable(failure) else "provider_unknown"

    terminated = launch_termination(observed) if observed and not error else None
    detail = None
    if observed and observed["State"]["Name"] == "running" and not error:
        error = await workspace(session_factory, ec2, operation_id, worker_id, claimed, observed, launch_template, restore_snapshot_id=restore_snapshot_id)

    ssm_online = False
    if observed and observed["State"]["Name"] == "running" and ssm is not None:
        try:
            response = await asyncio.to_thread(
                ssm.describe_instance_information, Filters=[{"Key": "InstanceIds", "Values": [observed["InstanceId"]]}]
            )
            ssm_online = any(
                item["InstanceId"] == observed["InstanceId"] and item["PingStatus"] == "Online"
                for item in response.get("InstanceInformationList", [])
            )
        except (OSError, BotoCoreError, ClientError):
            pass

    async with session_factory() as db:
        current, current_box, eligibility = await current_operation(db, operation_id, worker_id, claimed)
        if current is None:
            await db.rollback()
            return False
        if eligibility:
            observed = None
            error = eligibility
        current_instance = await db.scalar(
            select(DevBoxInstance).where(DevBoxInstance.box_id == current.box_id, DevBoxInstance.machine_generation == current.machine_generation)
        )
        if observed and not eligibility:
            current_instance.provider_instance_id = observed["InstanceId"]
            current_instance.provider_account_ref = launch_template.account_id
            current_instance.availability_zone = observed.get("Placement", {}).get("AvailabilityZone")
            current_instance.private_address = private_host_address(observed)
            current_instance.observed_state = observed["State"]["Name"]
            current.provider_request_ref = observed["InstanceId"]
            if not error:
                current.progress = "instance_observed"
                current.error_class = None
            current_box.observed_state = current_instance.observed_state
            if current_instance.observed_state == "running":
                if not error:
                    current.status = "succeeded"
                    current_box.status = "running"
                current_instance.readiness_class = "machine_ssm_online" if ssm_online else "machine_running"
        if terminated and not eligibility:
            # EC2 accepted the launch and then ended the machine itself; the owner may release or replace.
            code, _ = terminated
            current.status = "failed"
            current.error_class = f"provider_refused:{code}"[:64] if code else "instance_terminated_at_launch"
            current_instance.error_class = current.error_class
            current_instance.readiness_class = None
            record_event(db, current, "launch_terminated")
        elif refused and not eligibility:
            # EC2 rejected the request synchronously: nothing launched, so the owner may release or replace.
            # After a capacity fallback (#8423) the log names every (size, subnet) that was refused.
            current.status = "failed"
            current.error_class = f"provider_refused:{refused}"[:64]
            current_box.observed_state = "unknown"
            record_event(db, current, "launch_refused")
            if len(tried) > 1:
                detail = attempts_detail(tried)
        elif plan_exhausted and not eligibility:
            # #8423: the retained workspace disk's zone has no approved subnet; an operator must add one.
            current.status = "failed"
            current.error_class = "workspace_zone_unavailable"
            current_box.observed_state = "unknown"
            record_event(db, current, "launch_refused")
            detail = "no approved private subnet lies in the availability zone of the retained workspace disk"
        elif launch_exhausted and not error and not observed:
            # Retry bound or token window passed and a clean inventory read still shows nothing.
            current.status = "failed"
            current.error_class = "launch_unconfirmed"
            current_box.observed_state = "unknown"
            record_event(db, current, "launch_unconfirmed")
        elif error == "snapshot_ownership_mismatch" and restore_snapshot_id:
            current.status = "failed"
            current.error_class = error
            record_event(db, current, "restore_refused")
        elif error or not observed:
            detail = settle_unresolved(db, current, error or "launch_not_observed", claimed, now)
        current.lease_owner = None
        current.lease_expires_at = None
        if current.status == "unknown" and error == "provider_throttled":
            # Hold the lease so the next claim waits out the provider's rate limit (#8223 cause 52).
            current.lease_expires_at = now + timedelta(seconds=DEV_BOX_LAUNCH_THROTTLE_BACKOFF_SECONDS)
        if (current.status == "failed" and current.action == "replace" and not eligibility
                and current_instance is not None and not current_instance.provider_instance_id):
            await retreat_failed_replace(db, current, current_box, current_instance)
        elif (current.status == "failed" and current.action == "replace" and restore_snapshot_id and not eligibility
                and current_instance is not None and current_instance.provider_instance_id):
            current.status = "unknown"
            current.progress = "restore_cleanup"
        provider_code = refused or (terminated and not eligibility and terminated[0]) or None
        provider_message = terminated[1] if terminated and not eligibility else detail
        if provider_error is not None:
            # The provider's own words survive to the log in every outcome (#8223 cause 52).
            if provider_code is None:
                provider_code = provider_error[0]
            provider_message = f"{provider_message}; provider said: {provider_error[1]}" if provider_message else provider_error[1]
        log_outcome(current, launched=launched, provider_code=provider_code, provider_message=provider_message)
        await db.commit()
    return True


async def retreat_failed_replace(db, operation, box, instance):
    """Return a box whose Replace launched nothing to the stopped machine it still has (#8223 causes 49, 52, 53).

    The launch was refused, the plan was exhausted, or the send was never confirmed: EC2 holds no machine
    for this generation. The owner generation counter has moved on regardless, so the box goes back to
    `stopped` on the latest machine it does have, the never-launched generation row is removed, and the
    API admits the next Replace (which needs `stopped`/`stopped`/fenced and the retained disk) without
    anyone editing rows by hand. The failed operation and its events stay as the record of what happened.
    """
    previous = await db.scalar(select(DevBoxInstance).where(
        *previous_machine_filter(box.id, operation.machine_generation)
    ).order_by(previous_machine_order()).limit(1))
    if previous is None or previous.observed_state != "stopped":
        return False
    if instance.provider_instance_id:
        if instance.observed_state != "terminated":
            return False
    else:
        await db.delete(instance)
    box.machine_generation = previous.machine_generation
    box.status = "stopped"
    box.observed_state = "stopped"
    box.desired_state = "stopped"
    if box.auth_readiness not in ("unknown", "fenced"):
        box.auth_readiness = "fenced"
    record_event(db, operation, "replace_retreated")
    return True


async def cleanup_failed_restore(session_factory, ec2, operation_id, worker_id, claimed, launch_template):
    from controller.state import owned_instance

    async with session_factory() as db:
        operation, box, eligibility = await current_operation(db, operation_id, worker_id, claimed, cleanup=True)
        if eligibility or operation.progress != "restore_cleanup":
            await db.rollback()
            return False
        instance = await db.scalar(select(DevBoxInstance).where(
            DevBoxInstance.box_id == box.id, DevBoxInstance.machine_generation == operation.machine_generation,
        ))
        if not instance or not instance.provider_instance_id or not instance.availability_zone:
            await db.rollback()
            return False
        disk = await db.scalar(select(DevBoxResource).where(
            DevBoxResource.box_id == box.id, DevBoxResource.machine_generation == operation.machine_generation,
            DevBoxResource.resource_kind == "workspace_disk",
        ))
        restore_event = await db.scalar(select(DevBoxEvent.event_type).where(
            DevBoxEvent.operation_id == operation.id, DevBoxEvent.event_type.like("restore:snap-%"),
        ))
        instance_id, zone = instance.provider_instance_id, instance.availability_zone
        disk_id = disk.id if disk else None
        tags = ownership_tags(box, operation)
        await db.commit()

    try:
        observed = await asyncio.to_thread(owned_instance, ec2, instance_id, tags)
        if observed.get("Placement", {}).get("AvailabilityZone") != zone:
            return True
        state = observed["State"]["Name"]
        if state not in ("shutting-down", "terminated"):
            async with maintained_lease(session_factory, operation_id, worker_id, claimed):
                await asyncio.to_thread(ec2.terminate_instances, InstanceIds=[instance_id])
            return True
        if state != "terminated":
            return True
        if disk:
            if not launch_template.kms_key_arn or not restore_event:
                return True
            try:
                volume = await asyncio.to_thread(observed_volume, ec2, box, disk, zone, launch_template.kms_key_arn)
            except ClientError as failure:
                if (failure.response.get("Error", {}).get("Code") != "InvalidVolume.NotFound"
                        or not disk.provider_resource_id.startswith("vol-")):
                    raise
                volume = None
            if volume:
                if (volume.get("SnapshotId") != restore_event.removeprefix("restore:")
                        or volume.get("State") != "available" or volume.get("Attachments")):
                    return True
                async with maintained_lease(session_factory, operation_id, worker_id, claimed):
                    await asyncio.to_thread(ec2.delete_volume, VolumeId=volume["VolumeId"])
                return True
    except (ValueError, KeyError, OSError, BotoCoreError, ClientError):
        logger.warning("Failed snapshot restore cleanup awaits verified provider state")
        return True

    async with session_factory() as db:
        operation, box, eligibility = await current_operation(db, operation_id, worker_id, claimed, cleanup=True)
        if eligibility or operation.progress != "restore_cleanup":
            await db.rollback()
            return False
        instance = await db.scalar(select(DevBoxInstance).where(
            DevBoxInstance.box_id == box.id, DevBoxInstance.machine_generation == operation.machine_generation,
        ))
        if not instance or instance.provider_instance_id != instance_id:
            await db.rollback()
            return False
        if disk_id:
            resource = await db.get(DevBoxResource, disk_id)
            resource.deletion_state = "verified_deleted"
            resource.retention_state = "deleted"
        instance.observed_state = "terminated"
        if not await retreat_failed_replace(db, operation, box, instance):
            await db.rollback()
            return False
        operation.status = "failed"
        operation.lease_owner = None
        operation.lease_expires_at = None
        log_outcome(operation)
        await db.commit()
    return True
