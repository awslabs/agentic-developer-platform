"""Batch failures are diagnosable without exposing worker inputs or changing retries."""

import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.exc import ProgrammingError

from l5_services.setup_authority import setup_authorization
from l5_services.workers import execution


WORK_ID = "861aa70f-ec97-4b55-acec-be726f682860"
MESSAGE_ID = "f5dc3ed6-482b-4f05-ae84-191ec859ed47"
REQUEST_ID = "2f9e6a1c-7aa5-4713-ae3a-e5513b3ad83d"
SECRET = "credential-canary-do-not-log"


def record(work_id=WORK_ID, message_id=MESSAGE_ID):
    return {"messageId": message_id, "body": json.dumps({
        "schema_version": "1.0", "work_id": work_id, "generation": 1, "secret": SECRET,
    }), "messageAttributes": {"credential": SECRET}}


def diagnostic(caplog):
    records = [entry for entry in caplog.records if entry.name == execution.LOGGER.name]
    assert len(records) == 1
    entry = records[0]
    assert entry.exc_info is None and entry.stack_info is None
    assert SECRET not in entry.getMessage()
    assert entry.getMessage().startswith("L5 work item failed: ")
    return json.loads(entry.getMessage().removeprefix("L5 work item failed: "))


@pytest.mark.parametrize("service_id", ["security", "devops", "well_architected", "transform", "finops"])
async def test_partial_batch_keeps_successes_and_validated_correlations(caplog, service_id):
    calls = []

    class Service:
        async def execute(self, envelope):
            calls.append(envelope["work_id"])
            if envelope["work_id"] == WORK_ID:
                raise TimeoutError(SECRET)
            return False  # Already claimed/committed work remains acknowledged.

    service = Service()
    service.service_id = service_id
    result = await execution.process_batch({"Records": [record("before"), record(), record("after")]},
                                           service, request_id=REQUEST_ID)
    assert result == {"batchItemFailures": [{"itemIdentifier": MESSAGE_ID}]}
    assert calls == ["before", WORK_ID, "after"]
    data = diagnostic(caplog)
    assert {key: data[key] for key in ("event", "disposition", "service_id", "request_id", "message_id", "work_id")} == {
        "event": "l5_worker_record_failed", "disposition": "partial_batch_failure",
        "service_id": service_id, "request_id": REQUEST_ID, "message_id": MESSAGE_ID, "work_id": WORK_ID,
    }
    assert data["error_code"] == "request_timeout"
    assert data["frames"] == [{"file": "l5_services/workers/execution.py", "function": "process_batch",
                                "line": data["frames"][0]["line"]}]
    assert 0 < data["frames"][0]["line"] <= 1_000_000
    assert "operator_action" not in data


async def test_malformed_json_does_not_log_body_or_previous_envelope(caplog):
    calls = []

    class Service:
        async def execute(self, envelope):
            calls.append(envelope)

    malformed = {"messageId": MESSAGE_ID, "body": '{"password": "' + SECRET}
    result = await execution.process_batch({"Records": [record(), malformed]}, Service())
    assert result == {"batchItemFailures": [{"itemIdentifier": MESSAGE_ID}]}
    assert len(calls) == 1
    data = diagnostic(caplog)
    assert data["error_code"] == "malformed_json"
    assert "work_id" not in data


@pytest.mark.parametrize("invalid", [SECRET, "a" * 10000, WORK_ID.upper(), WORK_ID.replace("-", ""), None, 12, [], {}])
async def test_invalid_correlations_and_service_are_omitted(caplog, invalid):
    class Service:
        service_id = invalid

        async def execute(self, envelope):
            raise RuntimeError(SECRET)

    # The original identifier is still returned verbatim; diagnostics do not
    # introduce validation or change acknowledgement semantics for SQS inputs.
    result = await execution.process_batch({"Records": [record(invalid, invalid)]}, Service(), request_id=invalid)
    assert result == {"batchItemFailures": [{"itemIdentifier": invalid}]}
    data = diagnostic(caplog)
    assert not {"request_id", "message_id", "work_id", "service_id"} & data.keys()
    assert data["error_code"] == "unclassified"


@pytest.mark.parametrize("attribute", ["sqlstate", "pgcode"])
async def test_database_permission_error_is_actionable_without_sql_or_secrets(caplog, attribute):
    class DriverError(Exception):
        def __str__(self):
            raise AssertionError("diagnostics must never format the driver exception")

    original = DriverError(SECRET)
    setattr(original, attribute, "42501")
    error = ProgrammingError(f"SELECT {SECRET} FROM private_table_canary", {"token": SECRET}, original)

    class Session:
        async def scalars(self, statement):
            raise error

    class Service:
        service_id = "devops"

        async def execute(self, envelope):
            await setup_authorization(Session(), SimpleNamespace(
                service_id="devops", authorization={"purpose": "devops_setup", "token": SECRET},
                tenant_id=SECRET, operation_id=WORK_ID,
            ))

    result = await execution.process_batch({"Records": [record()]}, Service())
    assert result == {"batchItemFailures": [{"itemIdentifier": MESSAGE_ID}]}
    data = diagnostic(caplog)
    assert data["error_code"] == "database_permission_denied"
    assert data["operator_action"] == (
        "Run modules/L5-services/scripts/bootstrap-worker-db.sql as the database owner after L5 migrations. "
        "Follow docs/l5-services/deployment-authority.md and verify worker database privileges."
    )
    assert "private_table_canary" not in caplog.text
    assert [(frame["file"], frame["function"]) for frame in data["frames"]] == [
        ("l5_services/workers/execution.py", "process_batch"),
        ("l5_services/setup_authority.py", "setup_authorization"),
        ("l5_services/setup_authority.py", "continuation_rows"),
    ]


@pytest.mark.parametrize("sqlstate", ["42501" + SECRET, "23505", None, 42501, ["42501"]])
async def test_other_database_failures_do_not_invent_permission_action(caplog, sqlstate):
    original = RuntimeError(SECRET)
    original.sqlstate = sqlstate

    class Service:
        async def execute(self, envelope):
            raise ProgrammingError(SECRET, {"password": SECRET}, original)

    await execution.process_batch({"Records": [record()]}, Service())
    data = diagnostic(caplog)
    assert data["error_code"] == "database_error"
    assert "operator_action" not in data
    assert "sqlstate" not in data


@pytest.mark.parametrize("error,expected", [
    (HTTPException(403, {"code": "setup_authority_denied", "message": SECRET}),
     {"error_code": "http_error", "http_status": 403, "cause_code": "setup_authority_denied"}),
    (HTTPException(500, {"code": SECRET, "message": SECRET}), {"error_code": "http_error", "http_status": 500}),
    (HTTPException(SECRET, {"code": [SECRET]}), {"error_code": "http_error"}),
    (httpx.ConnectError(SECRET, request=httpx.Request("GET", f"https://example.invalid/{SECRET}")),
     {"error_code": "transport_error"}),
    (httpx.ReadTimeout(SECRET, request=httpx.Request("GET", f"https://example.invalid/{SECRET}")),
     {"error_code": "request_timeout"}),
    (httpx.HTTPStatusError(SECRET, request=httpx.Request("GET", f"https://example.invalid/{SECRET}"),
                           response=httpx.Response(503, text=SECRET)),
     {"error_code": "http_error", "http_status": 503}),
    (type(SECRET, (Exception,), {})(SECRET), {"error_code": "unclassified"}),
])
async def test_http_and_unknown_errors_never_log_messages_details_urls_or_causes(caplog, error, expected):
    class Service:
        async def execute(self, envelope):
            raise error from RuntimeError(SECRET)

    await execution.process_batch({"Records": [record()]}, Service())
    data = diagnostic(caplog)
    assert {key: value for key, value in data.items() if key in {"error_code", "http_status", "cause_code"}} == expected
    assert "operator_action" not in data


@pytest.mark.parametrize("filename,function", [
    (f"/private/{SECRET}/l5_services/workers/execution.py", "execute"),
    (execution.__file__, "secret_function_canary"),
    (execution.__file__, "execute"),
])
async def test_source_frames_are_bounded_and_allowlisted(caplog, filename, function):
    namespace = {"failure": RuntimeError(SECRET)}
    exec(compile(f"def {function}(depth):\n    if depth: return {function}(depth - 1)\n    raise failure\n",
                 filename, "exec"), namespace)

    class Service:
        async def execute(self, envelope):
            namespace[function](20)

    await execution.process_batch({"Records": [record()]}, Service())
    data = diagnostic(caplog)
    assert "secret_function_canary" not in caplog.text
    assert 1 <= len(data["frames"]) <= 5
    assert all(frame["file"] == "l5_services/workers/execution.py" for frame in data["frames"])
    if filename == execution.__file__ and function == "execute":
        assert len(data["frames"]) == 5
        assert all(frame["function"] == "execute" for frame in data["frames"])
    else:
        assert [frame["function"] for frame in data["frames"]] == ["process_batch"]


async def test_logging_handler_failure_preserves_retries_and_successful_neighbor():
    calls = []

    class BrokenHandler(logging.Handler):
        def emit(self, record):
            raise RuntimeError("logging unavailable")

    class Service:
        async def execute(self, envelope):
            calls.append(envelope["work_id"])
            if envelope["work_id"] == WORK_ID:
                raise RuntimeError(SECRET)

    handler = BrokenHandler()
    execution.LOGGER.addHandler(handler)
    try:
        result = await execution.process_batch({"Records": [record(), record("ok")]}, Service())
    finally:
        execution.LOGGER.removeHandler(handler)
    assert result == {"batchItemFailures": [{"itemIdentifier": MESSAGE_ID}]}
    assert calls == [WORK_ID, "ok"]


async def test_metadata_extraction_failure_preserves_retries_and_successful_neighbor():
    calls = []

    class Service:
        @property
        def service_id(self):
            raise RuntimeError(SECRET)

        async def execute(self, envelope):
            calls.append(envelope["work_id"])
            if envelope["work_id"] == WORK_ID:
                raise RuntimeError(SECRET)

    result = await execution.process_batch({"Records": [record(), record("ok")]}, Service())
    assert result == {"batchItemFailures": [{"itemIdentifier": MESSAGE_ID}]}
    assert calls == [WORK_ID, "ok"]


def test_lambda_propagates_request_correlation(caplog):
    class Service:
        async def execute(self, envelope):
            raise RuntimeError(SECRET)

    result = execution.lambda_handler({"Records": [record()]}, SimpleNamespace(aws_request_id=REQUEST_ID), service=Service())
    assert result == {"batchItemFailures": [{"itemIdentifier": MESSAGE_ID}]}
    assert diagnostic(caplog)["request_id"] == REQUEST_ID


@pytest.mark.parametrize("connectivity", [False, True])
def test_runtime_probes_remain_outside_batch_diagnostics(caplog, connectivity):
    class Context:
        @property
        def aws_request_id(self):
            raise AssertionError("runtime probe must not enter batch diagnostics")

    service = SimpleNamespace(service_id="transform", runtime_connectivity_probe=lambda: {"state": "ready"})
    event = {"l5_runtime_probe": 1}
    expected = {"service_id": "transform"}
    if connectivity:
        event["connectivity"] = "transform"
        expected["connectivity"] = {"state": "ready"}
    assert execution.lambda_handler(event, Context(), service=service) == expected
    assert caplog.records == []


@pytest.mark.parametrize("code,operation,expected", [
    ("AccessDenied", "GetObject", {"cause_code": "AccessDenied", "provider_operation": "GetObject"}),
    ("ValidationException", "UpdateThreatModel", {"cause_code": "ValidationException", "provider_operation": "UpdateThreatModel"}),
    (SECRET, SECRET, {}),
])
async def test_aws_failure_metadata_is_allowlisted(caplog, code, operation, expected):
    from botocore.exceptions import ClientError
    class Service:
        async def execute(self, envelope):
            raise ClientError({"Error": {"Code": code, "Message": SECRET}}, operation)
    await execution.process_batch({"Records": [record()]}, Service())
    data = diagnostic(caplog)
    assert data["error_code"] == "provider_error"
    assert {key: data[key] for key in ("cause_code", "provider_operation") if key in data} == expected
