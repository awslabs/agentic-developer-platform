"""A deployment selector can narrow a native task to one recovery assignment.

This is not authority. The ordinary workload bootstrap and recovery scope still
authenticate every call. In particular, a queue message cannot select execution
in an observer Job, even before a run credential has been issued.
"""

import json
import os

from harness_jobs.identity import OperationRefused

VARIABLE = "SUPERPLANE_OBSERVATION_ONLY"
FIELDS = frozenset({"operation_id", "org_id", "workspace_id", "job_id", "attempt_id"})


def selector():
    raw = os.environ.get(VARIABLE)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
        if (
            len(raw) > 4096
            or not isinstance(value, dict)
            or set(value) != FIELDS
            or any(
                not isinstance(v, str) or not 1 <= len(v) <= 255 for v in value.values()
            )
        ):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise OperationRefused("invalid recovery-only deployment selector") from None


def require_selected(original):
    expected = selector()
    if expected is not None and (
        not isinstance(original, dict)
        or original.get("mode") != "recovery"
        or any(original.get(k) != v for k, v in expected.items())
    ):
        raise OperationRefused(
            "observer refuses execution or a different recovery task"
        )
