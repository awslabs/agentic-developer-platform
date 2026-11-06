"""Independent progress expectations; synthetic records are not live evidence."""

from copy import deepcopy

import pytest

from tests.e2e.new_ui.test_product_resume import assert_progress


@pytest.fixture
def progress_records():
    def build(stage):
        values = {"initial": (2, 2, 100, "not_started", None),
                  "selected": (2, 3, 66, "in_progress", "missing"),
                  "later": (2, 3, 66, "later", "later"),
                  "not_applicable": (2, 2, 100, "not_applicable", "not_applicable")}
        completed, applicable, percentage, step_status, check_status = values[stage]
        checks = [{"id": "information", "status": "complete", "required": True},
                  {"id": "project_scope", "status": "complete", "required": True}]
        if check_status is not None:
            financial = {"id": "financials", "status": check_status, "required": False}
            if stage == "not_applicable":
                financial["reason"] = "No financial plan yet"
            checks.append(financial)
        step = {"status": step_status}
        if stage == "not_applicable":
            step["reason"] = "No financial plan yet"
        session = {"progress": {"rule_version": "profile-1", "completed": completed,
                                "applicable": applicable, "percentage": percentage, "checks": checks},
                   "steps": {"information": {"status": "complete"},
                             "basic": {"status": "not_started"}, "advanced": {"status": "not_started"},
                             "financials": step}, "operation_ids": []}
        return session, {"progress": deepcopy(session["progress"]), "configuration": {}}

    return build


@pytest.mark.parametrize("stage", ("initial", "selected", "later", "not_applicable"))
def test_idea_progress_expected_arithmetic_and_untouched_sections(progress_records, stage):
    session, profile = progress_records(stage)
    assert_progress(session, profile, stage)


@pytest.mark.parametrize("invalid", (
    "rounded_up", "excluded_later", "included_not_applicable", "required_optional", "untouched_advanced",
    "funding_grant", "execution_grant", "unreasoned_exclusion", "mismatched_readback", "pending_operation",
))
def test_progress_refuses_false_completion_or_grants(progress_records, invalid):
    stage = "not_applicable" if invalid in ("included_not_applicable", "unreasoned_exclusion") else "later"
    session, profile = progress_records(stage)
    if invalid == "rounded_up":
        session["progress"]["percentage"] = profile["progress"]["percentage"] = 67
    elif invalid == "excluded_later":
        session["progress"]["applicable"] = profile["progress"]["applicable"] = 2
    elif invalid == "included_not_applicable":
        session["progress"]["applicable"] = profile["progress"]["applicable"] = 3
    elif invalid == "required_optional":
        session["progress"]["checks"][-1]["required"] = profile["progress"]["checks"][-1]["required"] = True
    elif invalid == "untouched_advanced":
        session["steps"]["advanced"]["status"] = "complete"
    elif invalid == "funding_grant":
        profile["configuration"]["financials"] = {"allocations": [{"approval_status": "approved"}]}
    elif invalid == "execution_grant":
        profile["configuration"]["governance"] = {"execution_policy_ref": {"record_id": "grant"}}
    elif invalid == "unreasoned_exclusion":
        session["steps"]["financials"].pop("reason")
    elif invalid == "mismatched_readback":
        profile["progress"]["percentage"] = 100
    elif invalid == "pending_operation":
        session["operation_ids"] = ["unexpected-operation"]
    with pytest.raises(AssertionError):
        assert_progress(session, profile, stage)
