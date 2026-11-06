"""Revoke current host authority before admitting a lifecycle transition."""

import logging
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from src.agentauth.chat_crew_run import current_crew_store
from src.crew.host_lease import clear_host_lease_mirror
from src.crew.targets import CrewOwner
from src.shared.models.dev_box import DevBoxAccessSession, DevBoxDevice

logger = logging.getLogger(__name__)


async def fence_host(db, box, run_store=None, *, keep_devices=False):
    """Revoke the generation's live run and access session; revoke its devices unless kept.

    ``keep_devices=True`` is the owner-requested (or scheduled) **stop**: the machine comes back
    on the same generation, so the device record that proved "the owner's browser approved this
    host" stays valid and the controller re-binds a run on start without a second browser
    enrollment (#8223 simplification). Replace and release revoke the devices as before: the
    machine they approved is gone.
    """
    sessions = (
        await db.scalars(
            select(DevBoxAccessSession).where(
                DevBoxAccessSession.box_id == box.id,
                DevBoxAccessSession.user_id == box.user_id,
                DevBoxAccessSession.org_id == box.org_id,
                DevBoxAccessSession.deployment_id == box.deployment_id,
                DevBoxAccessSession.machine_generation == box.machine_generation,
                DevBoxAccessSession.terminated_at.is_(None),
            )
        )
    ).all()
    if box.auth_readiness == "ready" and len(sessions) != 1:
        raise HTTPException(503, "Host authority cannot be fenced")
    for session in sessions:
        if not session.provider_session_ref:
            raise HTTPException(503, "Host authority cannot be fenced")
        try:
            store = run_store or current_crew_store()
            row = await run_in_threadpool(store.read_run, session.provider_session_ref)
            if row is not None:
                if (
                    row.get("kind") != "crew"
                    or row.get("target_kind") != "ec2-instance"
                    or row.get("tenant_id") != box.org_id
                    or row.get("user_id") != box.user_id
                    or row.get("session_id") != box.id
                    or row.get("lease_generation") != box.machine_generation
                    or row.get("deployment_id") != box.deployment_id
                ):
                    raise HTTPException(503, "Host authority cannot be fenced")
            revoked = await run_in_threadpool(
                store.revoke_for_lease,
                tenant_id=box.org_id,
                session_id=box.id,
                lease_generation=box.machine_generation,
                expected_run_id=session.provider_session_ref,
            )
            if row is not None and not revoked:
                raise HTTPException(503, "Host authority cannot be fenced")
            if await run_in_threadpool(store.read_run, session.provider_session_ref) is not None:
                raise HTTPException(503, "Host authority cannot be fenced")
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(503, "Host authority cannot be fenced") from error
        session.terminated_at = datetime.now(UTC)
        if not keep_devices:
            device = await db.get(DevBoxDevice, session.device_id)
            if device is not None:
                device.revoked_at = session.terminated_at
    pending = (
        await db.scalars(
            select(DevBoxDevice).where(
                DevBoxDevice.box_id == box.id,
                DevBoxDevice.machine_generation == box.machine_generation,
                DevBoxDevice.enrollment_consumed_at.is_(None),
                DevBoxDevice.revoked_at.is_(None),
            )
        )
    ).all()
    for device in pending:
        device.revoked_at = datetime.now(UTC)
        device.sealed_enrollment = None
    # A ``pending`` box has an enrollment mid-flight (consumed device, possibly an expired run);
    # fencing it must retract that authority too, else a stopped/replaced box keeps ``pending`` and
    # the controller can neither complete its stop (require_access_fenced) nor admit a replace.
    if sessions or box.auth_readiness in ("ready", "pending"):
        box.auth_readiness = "fenced"
    # The owner's DynamoDB lease mirror is a projection of the authority revoked above. Left as it
    # was, it still named this box's pre-fence lease with its original expiry, and every restart on
    # the same generation was refused as a conflicting live lease (#8223, cause 39). The control DB
    # stays the authority and publication tolerates a stale mirror, so a failure here is logged,
    # never a half-done fence.
    from src.orchestration import intake_wiring

    try:
        await run_in_threadpool(clear_host_lease_mirror, intake_wiring._get_context_table(), CrewOwner(box.org_id, box.user_id), box.id)
    except Exception:  # noqa: BLE001 - best effort; publish_host_lease tolerates the stale mirror
        logger.warning("host lease mirror not cleared for box %s", box.id, exc_info=True)
