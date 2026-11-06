"""Private control invocations use durable work identity, not Task credentials."""

import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.session import get_session

from l5_services.adapters.transform.authority import TransformWorkAuthority
from l5_services.adapters.transform.control_client import TransformControlClient
from l5_services.adapters.transform.execution import TransformAdapter
from l5_services.adapters.transform.provider_job import finite_provider_job
from l5_services.workers.execution import AuthorityRevoked
from test_provider_job import admitted


NOW = datetime(2026, 10, 29, tzinfo=UTC)
FUNCTION_ARN = "arn:aws:lambda:us-east-1:123456789012:function:example-transform-control"


def fixtures():
    run, manifest, attempt = admitted()
    intent = TransformAdapter(object())._intent(run, attempt)
    intent["provider_job"] = finite_provider_job(intent, manifest, run.admission["transform_qualification"],
                                                 run.admission["limits"])
    current = [NOW]
    authority = TransformWorkAuthority(
        scope=run.admission["scope"], capture_limits={}, identity=None,
        source_request={"delegation_id": "grant-a", "delegation_revision": 2,
                        "operation_id": run.run_id, "attempt_id": "work-a",
                        "lease_owner": "worker-a", "lease_fence": 3},
        credentials={"access_key_id": "fixture-access", "secret_access_key": "fixture-secret",
                     "session_token": "fixture-token"},
        expires_at=NOW + timedelta(minutes=5), clock=lambda: current[0])
    return intent, manifest, authority, current


class PrivateFunction:
    def __init__(self):
        self.meta = SimpleNamespace(config=SimpleNamespace(
            retries={"total_max_attempts": 1}, connect_timeout=2, read_timeout=5))
        self.calls = []
        self.modify = None
        self.result = None
        self.function_error = None

    def invoke(self, *, FunctionName, InvocationType, Payload):
        self.calls.append((FunctionName, InvocationType, Payload))
        event = json.loads(Payload)
        result = {"schema_version": "1.0", "request_id": event["request_id"],
                  "operation": event["operation"], "run_id": event["intent"]["run_id"],
                  "executor_name": event["intent"]["executor_name"],
                  "attempt_token": event["intent"]["attempt_token"],
                  "work_id": event["work"]["attempt_id"],
                  "lease_fence": event["work"]["lease_fence"], "result": self.result}
        if self.modify is not None:
            self.modify(result)
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(result).encode()),
                **({"FunctionError": self.function_error} if self.function_error else {})}


@pytest.mark.asyncio
async def test_four_verbs_use_private_invocation_with_no_provider_credentials():
    intent, manifest, authority, _ = fixtures()
    private = PrivateFunction()
    control = TransformControlClient(private, function_arn=FUNCTION_ARN)
    private.result = {"executor_name": intent["executor_name"], "readback": None}
    assert (await control.launch_provider(intent=intent, manifest=manifest, authority=authority)) == private.result
    assert (await control.read_provider(intent=intent, authority=authority)) == private.result
    native = {"executor_name": intent["executor_name"], "executor_uid": "pod-uid-a"}
    assert (await control.request_stop(intent=intent, native_job_ref=native, authority=authority)) == private.result
    assert (await control.collect_provider(intent=intent, authority=authority)) == private.result
    assert [json.loads(call[2])["operation"] for call in private.calls] == ["launch", "read", "stop", "collect"]
    assert all(call[0] == FUNCTION_ARN and call[1] == "RequestResponse" for call in private.calls)
    launch = json.loads(private.calls[0][2])
    assert launch["manifest_sha256"] == intent["input_hash"]
    assert launch["snapshot_ids"] == manifest["source_snapshot_ids"]
    assert json.loads(private.calls[2][2])["native_job_ref"] == native
    assert all(secret not in call[2].decode() for call in private.calls
               for secret in ("fixture-access", "fixture-secret", "fixture-token"))
    assert all("task_id" not in json.loads(call[2]) for call in private.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["foreign_product", "wrong_work", "expired", "stale_fence",
                                        "missing_job", "foreign_source", "wrong_native", "oversize",
                                        "credential_in_intent", "credential_in_work"])
async def test_unscoped_or_expired_request_never_reaches_private_host(change):
    intent, manifest, authority, current = fixtures()
    if change == "foreign_product":
        authority.scope = {**authority.scope, "product_id": "foreign-product"}
    elif change == "wrong_work":
        authority.source_request["operation_id"] = "other-run"
    elif change == "expired":
        current[0] = NOW + timedelta(minutes=5)
    elif change == "stale_fence":
        authority.source_request["lease_fence"] = 0
    elif change == "missing_job":
        intent.pop("provider_job")
    elif change == "foreign_source":
        manifest = {**manifest, "run_id": "foreign-run"}
    elif change == "wrong_native":
        intent["executor_name"] = "tf-" + "a" * 40
    elif change == "credential_in_intent":
        intent["provider_job"]["aws_secret_access_key"] = "fixture-secret"
    elif change == "credential_in_work":
        authority.source_request["session_token"] = "fixture-token"
    else:
        intent["extra"] = "x" * 33000
    private = PrivateFunction()
    control = TransformControlClient(private, function_arn=FUNCTION_ARN)
    with pytest.raises((AuthorityRevoked, ValueError)):
        if change == "wrong_native":
            await control.request_stop(intent=intent, native_job_ref={"executor_name": "tf-other",
                                                                      "executor_uid": "pod-uid-a"}, authority=authority)
        else:
            await control.launch_provider(intent=intent, manifest=manifest, authority=authority)
    assert private.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["request_id", "run_id", "attempt_token", "work_id",
                                        "lease_fence", "missing_result", "malformed_result", "function_error",
                                        "credential_result", "credential_envelope"])
async def test_forged_or_failed_response_keeps_effect_outcome_unknown(change):
    intent, _, authority, _ = fixtures()
    private = PrivateFunction()
    if change == "function_error":
        private.function_error = "Unhandled"
    else:
        def corrupt(result):
            if change == "missing_result":
                result.pop("result")
            elif change == "malformed_result":
                result["result"] = "claimed-success"
            elif change == "credential_result":
                result["result"] = {"aws_access_key_id": "fixture-access"}
            elif change == "credential_envelope":
                result["session_token"] = "fixture-token"
            else:
                result[change] = "other"
        private.modify = corrupt
    with pytest.raises(ConnectionError):
        await TransformControlClient(private, function_arn=FUNCTION_ARN).read_provider(
            intent=intent, authority=authority)
    assert len(private.calls) == 1


@pytest.mark.asyncio
async def test_lost_response_does_not_repeat_paid_invocation():
    intent, manifest, authority, current = fixtures()

    class LostResponse(PrivateFunction):
        def invoke(self, **kwargs):
            self.calls.append(kwargs)
            raise OSError("lost response")

    private = LostResponse()
    with pytest.raises(ConnectionError):
        await TransformControlClient(private, function_arn=FUNCTION_ARN).launch_provider(
            intent=intent, manifest=manifest, authority=authority)
    assert len(private.calls) == 1
    private = PrivateFunction()
    private.modify = lambda result: current.__setitem__(0, NOW + timedelta(minutes=6))
    with pytest.raises(ConnectionError):
        await TransformControlClient(private, function_arn=FUNCTION_ARN).launch_provider(
            intent=intent, manifest=manifest, authority=authority)
    assert len(private.calls) == 1


@pytest.mark.asyncio
async def test_transport_timeout_is_uncertain_and_never_retried():
    intent, manifest, authority, _ = fixtures()

    class Timeout(PrivateFunction):
        def invoke(self, **kwargs):
            self.calls.append(kwargs)
            raise TimeoutError("response unavailable")

    private = Timeout()
    with pytest.raises(ConnectionError, match="outcome unknown"):
        await TransformControlClient(private, function_arn=FUNCTION_ARN).launch_provider(
            intent=intent, manifest=manifest, authority=authority)
    assert len(private.calls) == 1


@pytest.mark.asyncio
async def test_denied_private_invocation_and_invalid_function_are_not_retried():
    intent, _, authority, _ = fixtures()

    class Denied(PrivateFunction):
        def invoke(self, **kwargs):
            self.calls.append(kwargs)
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "Invoke")

    denied = Denied()
    with pytest.raises(AuthorityRevoked):
        await TransformControlClient(denied, function_arn=FUNCTION_ARN).read_provider(
            intent=intent, authority=authority)
    assert len(denied.calls) == 1
    with pytest.raises(ValueError):
        TransformControlClient(denied, function_arn="unscoped-function")


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [
    {"retries": {"total_max_attempts": 2}},
    {"retries": {"max_attempts": 1}},
    {"read_timeout": 10},
    {"connect_timeout": 10},
    {"read_timeout": 0},
])
async def test_retry_enabled_or_unbounded_sdk_client_cannot_submit(override):
    intent, manifest, authority, _ = fixtures()
    private = PrivateFunction()
    for name, value in override.items():
        setattr(private.meta.config, name, value)
    with pytest.raises(ValueError, match="SDK invocation retries"):
        TransformControlClient(private, function_arn=FUNCTION_ARN)
    assert private.calls == []


def test_real_sdk_client_requires_one_attempt_and_short_socket_timeouts():
    sdk = get_session()
    settings = {"service_name": "lambda", "region_name": "us-east-1",
                "aws_access_key_id": "fixture", "aws_secret_access_key": "fixture"}
    default = sdk.create_client(**settings)
    with pytest.raises(ValueError, match="SDK invocation retries"):
        TransformControlClient(default, function_arn=FUNCTION_ARN)
    pinned = sdk.create_client(**settings, config=Config(
        retries={"total_max_attempts": 1, "mode": "standard"},
        connect_timeout=2, read_timeout=5))
    assert TransformControlClient(pinned, function_arn=FUNCTION_ARN).client is pinned
