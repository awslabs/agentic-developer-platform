"""Select recovery only by its recorded, closed receipt contract.

The legacy dispatch field is named developer_recovery_decision_id. The selected
contract identifies the actual historical worker; it never creates a developer
identity for a reviewer.
"""

from .models import OrchestrationDecision
from .protected_developer_recovery import reservation_id  # noqa: F401
from .protected_review_recovery import require


async def protocol(session, decision_id):
    from . import protected_developer_recovery, released_reviewer_recovery, retained_work_recovery

    row = await session.get(OrchestrationDecision, decision_id)
    require(row is not None, "recovery_authority_changed")
    kinds = {
        protected_developer_recovery.KIND: protected_developer_recovery,
        retained_work_recovery.KIND: retained_work_recovery,
        released_reviewer_recovery.KIND: released_reviewer_recovery,
    }
    require(row.kind in kinds, "recovery_authority_changed")
    return kinds[row.kind]


async def verified_decision(session, *, decision_id, **kwargs):
    selected = await protocol(session, decision_id)
    return await selected.verified_decision(session, decision_id=decision_id, **kwargs)


async def verify_transfer(session, *, decision_id, services=None, **kwargs):
    selected = await protocol(session, decision_id)
    from . import released_reviewer_recovery, retained_work_recovery

    if selected.KIND in {retained_work_recovery.KIND, released_reviewer_recovery.KIND}:
        kwargs["services"] = services
    return await selected.verify_transfer(session, decision_id=decision_id, **kwargs)


async def recovery_snapshot(session, context, node, binding, services, dispatches):
    from . import protected_developer_recovery, released_reviewer_recovery, retained_work_recovery

    for selected in (released_reviewer_recovery, retained_work_recovery, protected_developer_recovery):
        snapshot = await selected.recovery_snapshot(session, context, node, binding, services, dispatches)
        if snapshot is not None:
            return snapshot
    return None
