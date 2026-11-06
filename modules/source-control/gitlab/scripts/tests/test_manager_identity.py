"""A retained GitLab manager record is not a second live Kubernetes manager."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from manager_identity import verify_current_manager


def records():
    now = datetime.now(timezone.utc)
    registered = now - timedelta(seconds=10)
    current = {"system_id": "r_" + "a" * 12, "status": "online", "version": "19.5.2",
               "platform": "linux", "architecture": "amd64", "contacted_at": now.isoformat()}
    previous = {**current, "system_id": "r_" + "b" * 12,
                "contacted_at": (registered - timedelta(seconds=1)).isoformat()}
    return now, registered, current, previous


def test_retained_online_status_before_current_registration_is_historical():
    now, registered, current, previous = records()
    result = verify_current_manager([previous, current], {"system_id": current["system_id"]},
                                    "19.5.2", registered, now)
    assert result == {"historical_manager_records": 1, "historical_online_records": 1}


@pytest.mark.parametrize("change", ["recent_other", "duplicate_current", "missing_current", "offline",
                                   "wrong_version", "stale_current", "future_current"])
def test_current_identity_and_no_other_fresh_contact_are_required(change):
    now, registered, current, previous = records()
    managers = [current, previous]
    if change == "recent_other": previous["contacted_at"] = registered.isoformat()
    elif change == "duplicate_current": managers.append(dict(current))
    elif change == "missing_current": managers = [previous]
    elif change == "offline": current["status"] = "offline"
    elif change == "wrong_version": current["version"] = "19.4.0"
    elif change == "stale_current": current["contacted_at"] = (now - timedelta(minutes=5)).isoformat()
    else: current["contacted_at"] = (now + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError):
        verify_current_manager(managers, {"system_id": "r_" + "a" * 12}, "19.5.2", registered, now)
