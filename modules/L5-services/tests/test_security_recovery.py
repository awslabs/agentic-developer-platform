"""An explicit recovery window is bounded and cannot authorize other work."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from l5_services.security_recovery import recovery_deadline, request_recovery


@pytest.mark.parametrize("fault", [None, "elapsed", "future", "unbounded", "naive", "native", "submit", "cancel", "cancelled", "foreign_service"])
def test_recovery_window_is_read_only_and_pinned(fault):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    run = SimpleNamespace(service_id="security", state="expired", native_job_ref={"record_id": "original"},
                          monitoring=None, deadline_at=now - timedelta(hours=2))
    run.monitoring = request_recovery(run, now)
    window = run.monitoring["security_recovery"]
    kind = "poll"
    if fault == "elapsed":
        now += timedelta(minutes=15)
    elif fault == "future":
        window["requested_at"] = (now + timedelta(seconds=1)).isoformat()
    elif fault == "unbounded":
        window["expires_at"] = (now + timedelta(minutes=16)).isoformat()
    elif fault == "naive":
        window["expires_at"] = "2026-01-01T00:15:00"
    elif fault == "native":
        run.native_job_ref = {"record_id": "other"}
    elif fault in {"submit", "cancel"}:
        kind = fault
    elif fault == "cancelled":
        run.state = "cancelled"
    elif fault == "foreign_service":
        run.service_id = "devops"
    assert (recovery_deadline(run, kind, now) is not None) == (fault is None)
