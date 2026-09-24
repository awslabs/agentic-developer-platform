"""Build the per-operation run handoff these tests need to reach execution at all.

Kept beside the tests rather than in the package: a helper that manufactures grants
belongs to test setup only. Production reads the document #5535 produces.
"""

from datetime import UTC, datetime, timedelta

from superplane_executor.handoff import RunHandoff


def granted(verified, *, seconds=300):
    """The grant map a trusted service would hold for one verified operation."""
    lease = verified.grant.lease
    return {
        lease.operation_id: RunHandoff(
            lease.operation_id,
            lease.attempt_id,
            verified.job_id,
            datetime.now(UTC) + timedelta(seconds=seconds),
        )
    }


def handoff_file(registry, path):
    """Use the production file reader so tests cover on-disk revocation races."""
    import json
    from dataclasses import asdict
    from superplane_executor.handoff import read_handoff

    path.write_text(
        json.dumps(
            {
                "version": 1,
                "grants": [
                    {**asdict(grant), "not_after": grant.not_after.isoformat()}
                    for grant in registry.handoffs.values()
                ],
            }
        )
    )
    registry.handoff_reader = lambda: read_handoff(path)
    return path
