"""New-work controls use fixed offline time and selected event identity."""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from l5_services.devops.trigger_policy import require_new_work

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
ENVIRONMENT = {"tenant_id": "tenant-example", "project_id": "project-example",
               "source_connection_id": "connection-example", "record_type": "environment",
               "record_id": "environment-example", "record_revision": "revision-1"}
EVENT = {"severity": "high", "environment_ref": ENVIRONMENT}
TRIGGER = {"severities": ["high"], "environment_refs": [ENVIRONMENT], "maintenance_windows": []}


def test_selected_event_is_allowed():
    require_new_work({"devops": {}}, TRIGGER, event=EVENT, now=NOW)


@pytest.mark.parametrize("settings,trigger,event,code", [
    ({"devops": {"paused": True}}, TRIGGER, EVENT, "trigger_paused"),
    ({"devops": {}}, {**TRIGGER, "paused": True}, EVENT, "trigger_paused"),
    ({"devops": {}}, TRIGGER, {**EVENT, "severity": "low"}, "incident_filtered"),
    ({"devops": {}}, TRIGGER, {**EVENT, "environment_ref": {**ENVIRONMENT, "project_id": "other"}}, "incident_filtered"),
    ({"devops": {}}, {**TRIGGER, "maintenance_windows": [{
        "starts_at": "2026-10-27T08:00:00Z", "ends_at": "2026-10-27T10:00:00Z",
        "environment_refs": [ENVIRONMENT]}]}, EVENT, "maintenance_exclusion"),
    ({"devops": {}}, {**TRIGGER, "maintenance_windows": [{
        "starts_at": "2026-10-27T10:00:00Z", "ends_at": "2026-10-27T08:00:00Z",
        "environment_refs": [ENVIRONMENT]}]}, EVENT, "maintenance_policy_unavailable"),
    ({"devops": {}}, {"environment_refs": [ENVIRONMENT]}, EVENT, "incident_filtered"),
])
def test_ineligible_new_work_is_denied(settings, trigger, event, code):
    with pytest.raises(HTTPException) as refused:
        require_new_work(settings, trigger, event=event, now=NOW)
    assert refused.value.status_code == 409
    assert refused.value.detail == {"code": code}


def test_prior_work_is_not_mutated_by_pause():
    require_new_work({"devops": {}}, {"paused": False}, now=NOW)
