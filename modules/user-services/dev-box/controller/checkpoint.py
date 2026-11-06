"""Capture the Crew home once more before the machine stops (#8379).

Both stop paths fence host authority at admission (the API for an owner stop, the scheduler for
an idle stop); the controller then observes and stops the instance in ``state.reconcile_state``.
Between the fence and ``StopInstances`` the machine is still up, so the controller asks the host
to run ``checkpoint_crew_host.py --mode final`` (quiesced capture, direct-to-S3 upload, gateway
verification) and waits a bounded time for its one-line JSON verdict. The outcome is recorded as
a ``DevBoxEvent`` on the stop operation:

* ``pre_stop_checkpoint_verified`` / ``_unchanged`` — the owner's latest verified checkpoint now
  covers the home as it was at the stop (or already did);
* ``pre_stop_checkpoint_failed`` — the host refused, timed out or could not be reached;
* ``pre_stop_checkpoint_skipped`` — a Crew box that never reached ``crew_ready`` (nothing to save).

The stop proceeds in every case (fail-open): the retained workspace disk still holds the home,
the periodic timer's last checkpoint remains the recovery point, and a stop that could never
complete would leave the owner paying for a machine they asked to stop. Non-Crew boxes are not
touched and record nothing.
"""

import asyncio
import json
import logging

from botocore.exceptions import BotoCoreError, ClientError
from src.shared.models.base import new_uuid, utcnow
from src.shared.models.dev_box import DevBoxEvent

logger = logging.getLogger("dev-box-controller")

FINAL_CAPTURE_COMMAND = "python3 -I /opt/adp/crew/checkpoint_crew_host.py --mode final"
PRE_STOP_CHECKPOINT_TIMEOUT_SECONDS = 150
PRE_STOP_CHECKPOINT_POLL_SECONDS = 5
OUTCOMES = ("verified", "unchanged", "failed", "skipped")


def pre_stop_outcome(result):
    """Map an SSM invocation result to an outcome; anything but a clean verdict is ``failed``."""
    if result is None or result.get("Status") != "Success":
        return "failed"
    lines = [line for line in (result.get("StandardOutputContent") or "").splitlines() if line.strip()]
    try:
        verdict = json.loads(lines[-1]) if lines else {}
    except ValueError:
        return "failed"
    status = verdict.get("status") if isinstance(verdict, dict) else None
    return status if status in ("verified", "unchanged") else "failed"


async def capture_before_stop(session_factory, ssm, *, box, instance, operation, lease=None):
    """Run the host's final capture and record the outcome; returns the outcome name."""
    if box.profile != "crew" or ssm is None:
        return None   # not a Crew home, or a controller without SSM: the feature is inert, nothing recorded
    if instance.readiness_class != "crew_ready" or not instance.provider_instance_id:
        outcome = "skipped"
    else:
        outcome = await _run_final_capture(ssm, instance.provider_instance_id, lease)
    async with session_factory() as db:
        db.add(DevBoxEvent(
            id=new_uuid(), box_id=box.id, user_id=box.user_id, org_id=box.org_id, deployment_id=box.deployment_id,
            machine_generation=operation.machine_generation, operation_id=operation.id,
            event_type=f"pre_stop_checkpoint_{outcome}", created_at=utcnow(),
        ))
        await db.commit()
    return outcome


async def _run_final_capture(ssm, instance_id, lease):
    try:
        command = await asyncio.to_thread(
            ssm.send_command, InstanceIds=[instance_id], DocumentName="AWS-RunShellScript",
            Parameters={"commands": [FINAL_CAPTURE_COMMAND], "executionTimeout": [str(PRE_STOP_CHECKPOINT_TIMEOUT_SECONDS + 20)]},
            CloudWatchOutputConfig={"CloudWatchOutputEnabled": False},
        )
    except (OSError, BotoCoreError, ClientError) as error:
        logger.warning("dev-box pre-stop checkpoint could not be requested: instance=%s error=%s", instance_id, type(error).__name__)
        return "failed"
    command_id = command["Command"]["CommandId"]
    result = None
    try:
        async with asyncio.timeout(PRE_STOP_CHECKPOINT_TIMEOUT_SECONDS):
            while True:
                if lease is not None:
                    await lease()
                try:
                    result = await asyncio.to_thread(ssm.get_command_invocation, CommandId=command_id, InstanceId=instance_id)
                except ClientError as error:
                    if error.response.get("Error", {}).get("Code") != "InvocationDoesNotExist":
                        raise
                else:
                    if result.get("Status") not in {"Pending", "InProgress", "Delayed"}:
                        break
                await asyncio.sleep(PRE_STOP_CHECKPOINT_POLL_SECONDS)
    except TimeoutError:
        logger.warning("dev-box pre-stop checkpoint timed out: instance=%s command=%s", instance_id, command_id)
        return "failed"
    except (OSError, BotoCoreError, ClientError) as error:
        logger.warning("dev-box pre-stop checkpoint could not be observed: instance=%s error=%s", instance_id, type(error).__name__)
        return "failed"
    outcome = pre_stop_outcome(result)
    if outcome == "failed":
        # Secret-free, bounded: the helper names its refusal class on stderr (#8223 follow-up).
        logger.warning(
            "dev-box pre-stop checkpoint failed: instance=%s status=%s stdout=%r stderr=%r", instance_id, result.get("Status"),
            (result.get("StandardOutputContent") or "")[-200:], (result.get("StandardErrorContent") or "")[-400:],
        )
    return outcome
