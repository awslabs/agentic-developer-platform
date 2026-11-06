import json
import logging

from src.internal.domain_failure_diagnostics import record


def test_preflight_diagnostics_include_code_locations_without_secrets(caplog):
    secret = "must-never-appear-in-a-log"
    try:
        raise ValueError(secret)
    except ValueError as error:
        with caplog.at_level(logging.WARNING):
            record(error)
    assert secret not in caplog.text
    event = json.loads(caplog.records[-1].getMessage())
    assert event["exception_type"] == "ValueError"
    assert event["code_frames"][-1]["function"] == "test_preflight_diagnostics_include_code_locations_without_secrets"
    assert set(event["code_frames"][-1]) == {"file", "line", "function"}
