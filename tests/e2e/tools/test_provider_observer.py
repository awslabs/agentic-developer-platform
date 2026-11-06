"""Offline adversarial contracts: no AWS, model, tool or provider dispatch."""

import copy
from contextlib import ExitStack
import hashlib
import json
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from botocore.stub import Stubber
import pytest

from tests.e2e.tools import provider_observer as observer


ACCOUNT, REGION = "123456789012", "us-east-1"
EXECUTION = "11111111-1111-4111-8111-111111111111"
START = "22222222-2222-4222-8222-222222222222"
REQUEST = "33333333-3333-4333-8333-333333333333"
RESULT = "44444444-4444-4444-8444-444444444444"
NOW = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
DEADLINE = int(NOW.timestamp()) + 290
SHARED = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/fixture-shared-operations"
BROWSER = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/fixture-browser-operations"
INTERPRETER = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:code-interpreter-custom/fixture_interpreter"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/fixture-observer"
PRINCIPAL = {"tenant_id": "fixture-tenant", "user_id": "fixture-user", "team_id": "fixture-team"}
# Golden keys/handles from the separately maintained provider storage contract.
SEARCH_KEY = "GATEWAY_SEARCH#950b0ec0b81f21e18d508ff82d0b654b0cce23f69b2589bac3fcb3f7669dde3d"
CODE_KEY = "GATEWAY_CODE#677135b0abac7f8add1aa6061b84467dfe6355e31f9e6a77d34f5d70b61cee19"
BROWSER_KEY = "GATEWAY_BROWSER#677135b0abac7f8add1aa6061b84467dfe6355e31f9e6a77d34f5d70b61cee19"
CODE_HANDLE = "058300dd237625e66ec9f107f3715017bdc5501af8e24568395b0042109bb87b"
BROWSER_HANDLE = "7e213ff326265ee1b79e7023296cf68c7a1bd4a0d827286d9687f1092965cf6d"


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def browser_digest(action, arguments):
    return sha(encoded([action, arguments, PRINCIPAL, None, None]))


class Clock:
    def __init__(self):
        self.elapsed = 0
        self.sleeps = []

    def monotonic(self):
        return self.elapsed

    def now(self):
        return NOW + timedelta(seconds=self.elapsed)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds


class Fixture:
    def __init__(self, operation="websearch___search"):
        family, action = operation.split("___")
        self.clock, self.calls, self.rows = Clock(), [], {}
        self.identity = {"Account": ACCOUNT, "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/fixture-observer/fixture-session"}
        request = START if action == "start" or family == "websearch" or operation == "code_interpreter___close" else REQUEST
        receipt = "fixture-start-receipt" if request == START else "fixture-operation-receipt"
        reference = RESULT if action == "result" else REQUEST if operation == "code_interpreter___close" else request
        arguments = {"execution_id": EXECUTION, "request_id": request}
        root_arguments = {"execution_id": EXECUTION, "request_id": START}
        if family == "websearch": arguments["query"] = "fixture unicode café"
        if family == "browser":
            root_arguments["url"] = "https://example.com"
            if action == "start": arguments["url"] = "https://example.com"
        if action not in {"start", "search"}:
            arguments["session_id"] = CODE_HANDLE if family == "code_interpreter" else BROWSER_HANDLE
        execute_arguments = {"execution_id": EXECUTION, "request_id": REQUEST,
                             "session_id": CODE_HANDLE, "code": "print('fixture result')", "language": "python"}
        if action == "execute": arguments = dict(execute_arguments)
        if action == "result": arguments["provider_id"] = "fixture-task"
        if action == "file": arguments["path"] = "/tmp/fixture.txt"
        if family == "browser" and action in {"step", "inspect"}: arguments["view_id"] = "view-1"
        if action == "step": arguments.update(action="click", candidate_id="candidate-1")
        if operation == "code_interpreter___close": arguments.pop("request_id")
        binding = {"client": "cli", "operation": operation, "operation_ref": reference,
                   "execution_id": EXECUTION, "provider_request_id": request, "provider_receipt_id": receipt,
                   "arguments": arguments, "root": None, "parent": None, "artifact": None, "cleanup": None}
        if family != "websearch":
            binding["root"] = {"request_id": START, "receipt_id": "fixture-start-receipt", "arguments": root_arguments,
                               "deadline_at": DEADLINE, "ttl_seconds": 300}
            binding["cleanup"] = {"trigger": "public-close", "requested_at": (NOW - timedelta(seconds=1)).isoformat(), "max_seconds": 60}
        if action == "result":
            binding["parent"] = {"operation": "code_interpreter___execute", "request_id": REQUEST,
                                 "receipt_id": receipt, "arguments": execute_arguments}
        self.binding = binding
        self.manifest = {"version": 1, "deployment": {"account": ACCOUNT, "region": REGION, "observer_role_arn": ROLE,
            "shared_table_arn": SHARED, "browser_table_arn": BROWSER, "interpreter_arn": INTERPRETER,
            "browser_identifier": "aws.browser.v1", "provider_source_revision": "a" * 40, "release_receipt_sha256": "b" * 64},
            "authority": {"principal": dict(PRINCIPAL), "run_id": None, "workload_id": None},
            "limits": {"max_reads": 16, "max_polls": 1, "poll_seconds": 1, "timeout_seconds": 30, "max_item_bytes": 200000},
            "operations": [binding]}
        self.provider = {"sessionId": "fixture-provider-session", "status": "TERMINATED", "createdAt": NOW - timedelta(seconds=10), "sessionTimeoutSeconds": 300}
        if family == "code_interpreter": self.provider["name"] = "adp-" + CODE_HANDLE
        self.provider["browserIdentifier" if family == "browser" else "codeInterpreterIdentifier"] = "aws.browser.v1" if family == "browser" else "fixture_interpreter"
        identity = {"execution_id": EXECUTION, "request_id": request, "receipt_id": receipt, "state": "confirmed"}
        self.content = None
        if family == "websearch":
            self.content = encoded({"status": "completed", "results": [{"title": "fixture", "url": "https://example.com"}]})
            self.add(SHARED, SEARCH_KEY, "OP", {**identity, "request_digest": sha(encoded({"query": arguments["query"]})), "result_json": self.content.decode()})
        elif family == "code_interpreter":
            self.add(SHARED, CODE_KEY, "CODE_SESSION", {"execution_id": EXECUTION, "request_id": START,
                "receipt_id": "fixture-start-receipt", "state": "confirmed", "session_handle": CODE_HANDLE,
                "provider_identifier": "fixture_interpreter", "provider_id": "fixture-provider-session",
                "deadline_at": DEADLINE, "cleanup_state": "closed"})
            if action not in {"start", "close"}:
                row = {**identity, "session_handle": CODE_HANDLE}
                if action == "file":
                    self.content = b"fixture file bytes"
                    import base64
                    data = {"path": arguments["path"], "data_base64": base64.b64encode(self.content).decode(),
                            "content_sha256": sha(self.content), "byte_length": len(self.content)}
                    row.update(operation="file", request_digest=sha(encoded({"session_id": CODE_HANDLE, "path": arguments["path"]})),
                               result_json=encoded(data).decode(), artifact_sha256=sha(self.content), artifact_length=len(self.content))
                else:
                    self.content = encoded({"status": "completed", "stdout": "fixture result\n"})
                    row.update(request_digest=sha(encoded({k: execute_arguments[k] for k in ("session_id", "code", "language")})),
                               provider_task_id="fixture-task", result_json=self.content.decode())
                self.add(SHARED, CODE_KEY, "CODE_OP#" + request, row)
        else:
            self.add(BROWSER, BROWSER_KEY, "SESSION", {"execution_id": EXECUTION, "start_id": START,
                "generation": 2 if action == "step" else 1, "state": "closed", "deadline_at": DEADLINE,
                "provider_id": "fixture-provider-session", "provider_token": "secret-provider-token",
                "session_id": BROWSER_HANDLE, "view_id": "view-1"})
            start = {"execution_id": EXECUTION, "request_id": START, "receipt_id": "fixture-start-receipt",
                "generation": 1, "state": "confirmed", "operation": "start", "digest": browser_digest("start", root_arguments),
                "execution_deadline_at": DEADLINE, "provider_deadline_at": DEADLINE, "provider_dispatch": "sent",
                "provider_id": "fixture-provider-session", "provider_token": "secret-provider-token"}
            self.add(BROWSER, BROWSER_KEY, "OP#" + START, start)
            row = start if action == "start" else {**identity, "operation": action, "generation": 1, "digest": browser_digest(action, arguments)}
            result = {"status": "confirmed", "session_id": BROWSER_HANDLE}
            if action == "close": result.update(termination_confirmed=True, cleanup_status="stopped")
            else:
                view = "view-2" if action == "step" else "view-1"
                self.content = encoded({"view_id": view, "manifest": {"session_id": "fixture-provider-session"}, "capture": "private fixture data"})
                result.update(view_id=view, artifact={"artifact_id": request, "content_sha256": sha(self.content),
                                                     "byte_length": len(self.content), "content_type": "application/json"})
                self.add(BROWSER, BROWSER_KEY, "ARTIFACT#" + request, {"content": self.content, "sha256": sha(self.content), "byte_length": len(self.content)})
            row["result"] = result
            self.add(BROWSER, BROWSER_KEY, "OP#" + request, row)

    def add(self, table, key, sort, row):
        self.rows[table, key, sort] = {"event_id": key, "arrived_at": sort, **row}

    def read(self, service, operation, **arguments):
        self.calls.append((service, operation, copy.deepcopy(arguments)))
        assert (service, operation) in observer.READS, "Mutation or unbounded inventory attempted"
        if service == "sts": result = self.identity
        elif service == "bedrock-agentcore": result = self.provider
        else:
            assert arguments["ConsistentRead"] is True
            fields = set(arguments["ExpressionAttributeNames"].values())
            assert not {"provider_token", "adp_assertion", "signature", "credentials"} & fields
            assert set(arguments) == {"TableName", "Key", "ConsistentRead", "ProjectionExpression", "ExpressionAttributeNames"}
            key = arguments["TableName"], arguments["Key"]["event_id"]["S"], arguments["Key"]["arrived_at"]["S"]
            row = self.rows.get(key)
            result = {"Item": {k: TypeSerializer().serialize(v) for k, v in row.items() if k in fields}} if row else {}
        return {**copy.deepcopy(result), "ResponseMetadata": {"RequestId": "fixture-request-id"}}

    def make(self, read=None):
        return observer.Observer(self.manifest, account=ACCOUNT, region=REGION, read=read or self.read,
                                 monotonic=self.clock.monotonic, now=self.clock.now, sleep=self.clock.sleep)

    def run(self, **options):
        return self.make(**options).observe(**{k: self.binding[k] for k in ("operation_ref", "execution_id", "operation", "client")}, timeout=30)


@pytest.mark.parametrize("operation", sorted(observer.OPERATIONS))
def test_supported_provider_rows_produce_only_facts_not_acceptance(operation):
    fixture = Fixture(operation)
    result = fixture.run()
    facts = result["facts"]
    assert facts["correlation"]["status"] == facts["durable_outcome"]["status"] == "verified"
    assert result["acceptance"] == "not-evaluated"
    assert result["transport_kind"] == "injected-reader" and result["offline_only"] is True
    for unsupported in observer.UNSUPPORTED:
        assert facts[unsupported]["status"] == "unsupported"
    assert not {"provider_request_count", "cost_usd", "passed"} & result.keys()
    if operation != "websearch___search":
        assert facts["termination"]["provider_status"] == "TERMINATED"
        assert facts["termination"]["status"] == facts["session_ttl"]["status"] == "verified"
        assert facts["session_ttl"]["ttl_seconds"] == 300
    if fixture.content is not None:
        assert facts["provider_artifact"]["sha256"] == sha(fixture.content)
    assert all((service, action) in observer.READS for service, action, _ in fixture.calls)
    serialized = json.dumps(result)
    for private in ("secret-provider-token", "private fixture data", "fixture unicode café", "print('fixture result')"):
        assert private not in serialized


def test_exact_golden_owner_keys_and_consistent_secret_free_projection():
    fixture = Fixture("browser___inspect")
    fixture.run()
    rows = [args for service, _, args in fixture.calls if service == "dynamodb"]
    assert all(row["TableName"] == BROWSER and row["Key"]["event_id"]["S"] == BROWSER_KEY for row in rows)
    assert {row["Key"]["arrived_at"]["S"] for row in rows} == {"SESSION", "OP#" + START, "OP#" + REQUEST, "ARTIFACT#" + REQUEST}


@pytest.mark.parametrize("mutation", ["wrong-account", "wrong-role", "iam-user"])
def test_identity_refusal_precedes_all_provider_reads(mutation):
    fixture = Fixture()
    if mutation == "wrong-account": fixture.identity["Account"] = "000000000001"
    if mutation == "wrong-role": fixture.identity["Arn"] = f"arn:aws:sts::{ACCOUNT}:assumed-role/administrator/session"
    if mutation == "iam-user": fixture.identity["Arn"] = f"arn:aws:iam::{ACCOUNT}:user/administrator"
    result = fixture.run()
    assert result["facts"]["correlation"]["status"] == "refused"
    assert len(fixture.calls) == 1 and fixture.calls[0][:2] == ("sts", "get_caller_identity")


@pytest.mark.parametrize("field,value", [("execution_id", RESULT), ("request_id", RESULT), ("receipt_id", "foreign"),
                                         ("request_digest", "0" * 64), ("event_id", "foreign-key"), ("state", "invented")])
def test_forged_search_row_cannot_correlate(field, value):
    fixture = Fixture()
    fixture.rows[SHARED, SEARCH_KEY, "OP"][field] = value
    assert fixture.run()["facts"]["correlation"]["status"] == "refused"


@pytest.mark.parametrize("field", ["tenant_id", "user_id", "team_id"])
def test_other_owner_does_not_read_fixture_rows(field):
    fixture = Fixture()
    fixture.manifest["authority"]["principal"][field] = "different-owner"
    result = fixture.run()
    assert result["facts"]["correlation"]["status"] == "pending"
    assert fixture.calls[1][2]["Key"]["event_id"]["S"] != SEARCH_KEY


@pytest.mark.parametrize("state", ["pending", "unknown"])
def test_uncertain_ledger_never_confirms_outcome(state):
    fixture = Fixture()
    fixture.rows[SHARED, SEARCH_KEY, "OP"]["state"] = state
    result = fixture.run()
    assert result["facts"]["correlation"]["status"] == "verified"
    assert result["facts"]["durable_outcome"]["status"] == "pending"


def test_malformed_provider_field_is_refused_without_response_content():
    fixture = Fixture()
    fixture.rows[SHARED, SEARCH_KEY, "OP"]["result_json"] = {"secret": "not-a-json-string"}
    result = fixture.run()
    assert result["facts"]["correlation"]["status"] == "refused"
    assert "not-a-json-string" not in json.dumps(result)


@pytest.mark.parametrize("operation", ["code_interpreter___start", "browser___start"])
@pytest.mark.parametrize("change", ["ready", "wrong-session", "wrong-resource", "too-long", "stale-created", "late-expiry"])
def test_session_proof_cannot_substitute_wrong_or_nonterminal_session(operation, change):
    fixture = Fixture(operation)
    if change == "ready": fixture.provider["status"] = "READY"
    if change == "wrong-session": fixture.provider["sessionId"] = "foreign-session"
    if change == "wrong-resource":
        fixture.provider["browserIdentifier" if operation.startswith("browser") else "codeInterpreterIdentifier"] = "foreign-resource"
    if change == "too-long": fixture.provider["sessionTimeoutSeconds"] = 301
    if change == "stale-created": fixture.provider["createdAt"] = NOW - timedelta(days=1)
    if change == "late-expiry": fixture.provider["createdAt"] = NOW
    status = fixture.run()["facts"]["termination"]["status"]
    assert status == ("pending" if change == "ready" else "refused")


@pytest.mark.parametrize("code,status", [("ResourceNotFoundException", "pending"), ("AccessDeniedException", "refused")])
def test_provider_not_found_or_denied_never_means_terminated(code, status):
    fixture = Fixture("code_interpreter___close")
    def read(service, operation, **arguments):
        if service == "bedrock-agentcore":
            raise ClientError({"Error": {"Code": code, "Message": "secret-provider-error"}}, operation)
        return fixture.read(service, operation, **arguments)
    result = fixture.run(read=read)
    assert result["facts"]["termination"]["status"] == status
    assert "secret-provider-error" not in json.dumps(result)


def test_parent_execute_identity_and_payload_are_required_for_result():
    fixture = Fixture("code_interpreter___result")
    assert fixture.binding["operation_ref"] != fixture.binding["provider_request_id"]
    assert fixture.run()["facts"]["correlation"]["provider_request_id"] == REQUEST
    fixture.binding["parent"]["arguments"]["code"] = "different-code"
    assert fixture.run()["facts"]["correlation"]["status"] == "refused"


@pytest.mark.parametrize("missing", ["result_json", "provider_task_id"])
def test_confirmed_interpreter_state_requires_actual_task_and_result(missing):
    fixture = Fixture("code_interpreter___execute")
    fixture.rows[SHARED, CODE_KEY, "CODE_OP#" + REQUEST].pop(missing)
    assert fixture.run()["facts"]["durable_outcome"]["status"] == "refused"


def test_close_uses_bound_start_receipt_without_inventing_a_new_operation_row():
    fixture = Fixture("code_interpreter___close")
    result = fixture.run()
    assert result["operation_ref"] == REQUEST
    assert result["facts"]["correlation"]["provider_request_id"] == START
    assert result["facts"]["correlation"]["provider_receipt_id"] == "fixture-start-receipt"
    assert {args["Key"]["arrived_at"]["S"] for service, _, args in fixture.calls if service == "dynamodb"} == {"CODE_SESSION"}


@pytest.mark.parametrize("change", ["start-id", "deadline", "handle", "artifact-bytes", "view"])
def test_browser_lineage_and_artifacts_are_not_trusted_from_metadata_alone(change):
    fixture = Fixture("browser___inspect")
    if change == "start-id": fixture.rows[BROWSER, BROWSER_KEY, "SESSION"]["provider_id"] = "different-provider-session"
    if change == "deadline": fixture.rows[BROWSER, BROWSER_KEY, "OP#" + START]["execution_deadline_at"] += 1
    if change == "handle": fixture.rows[BROWSER, BROWSER_KEY, "SESSION"]["session_id"] = "different-handle"
    if change == "artifact-bytes": fixture.rows[BROWSER, BROWSER_KEY, "ARTIFACT#" + REQUEST]["content"] += b"x"
    if change == "view": fixture.rows[BROWSER, BROWSER_KEY, "OP#" + REQUEST]["result"]["view_id"] = "foreign-view"
    assert fixture.run()["facts"]["correlation"]["status"] == "refused"


def test_cleanup_start_owned_identity_can_be_observed_without_repairing_session():
    fixture = Fixture("browser___close")
    fixture.rows[BROWSER, BROWSER_KEY, "SESSION"].pop("provider_id")
    result = fixture.run()
    assert result["facts"]["termination"]["status"] == "verified"
    assert result["facts"]["termination"]["identity_source"] == "start-ledger"
    fixture.rows[BROWSER, BROWSER_KEY, "OP#" + START]["provider_deadline_at"] += 1
    assert fixture.run()["facts"]["termination"]["status"] == "refused"


def test_interpreter_aws_session_name_must_bind_the_owned_session_handle():
    fixture = Fixture("code_interpreter___start")
    fixture.provider["name"] = "adp-foreign-handle"
    assert fixture.run()["facts"]["termination"]["status"] == "refused"


def test_file_bytes_are_decoded_and_hashed_with_bound_chunk_scope():
    fixture = Fixture("code_interpreter___file")
    fixture.binding["artifact"] = {"scope": "provider-payload", "sha256": sha(fixture.content),
                                   "byte_length": len(fixture.content), "offset": 2, "length": 7}
    artifact = fixture.run()["facts"]["provider_artifact"]
    assert artifact["chunk_sha256"] == sha(fixture.content[2:9]) and artifact["length"] == 7
    row = fixture.rows[SHARED, CODE_KEY, "CODE_OP#" + REQUEST]
    row["artifact_sha256"] = "0" * 64
    assert fixture.run()["facts"]["provider_artifact"]["status"] == "refused"


def test_outer_facade_artifact_scope_cannot_be_passed_as_provider_payload():
    fixture = Fixture()
    fixture.binding["artifact"] = {"scope": "facade-envelope", "sha256": sha(fixture.content),
                                   "byte_length": len(fixture.content), "offset": 0, "length": 8192}
    with pytest.raises(ValueError): fixture.make()
    assert not fixture.calls


@pytest.mark.parametrize("change", ["read-limit", "poll-limit", "time-limit", "item-limit"])
def test_observation_respects_independent_hard_limits(change):
    fixture = Fixture("code_interpreter___start")
    fixture.provider["status"] = "READY"
    if change == "read-limit": fixture.manifest["limits"].update(max_reads=1, max_polls=10)
    if change == "poll-limit": fixture.manifest["limits"].update(max_polls=2)
    if change == "item-limit": fixture.manifest["limits"]["max_item_bytes"] = 10
    def slow_read(service, operation, **arguments):
        value = fixture.read(service, operation, **arguments)
        fixture.clock.elapsed += 31
        return value
    result = fixture.run(read=slow_read if change == "time-limit" else None)
    assert result["facts"]["termination"]["status"] == ("refused" if change == "item-limit" else "pending")
    assert len(fixture.calls) <= fixture.manifest["limits"]["max_reads"]
    assert len(fixture.clock.sleeps) < fixture.manifest["limits"]["max_polls"]
    if change in {"read-limit", "time-limit"}: assert len(fixture.calls) == 1


def test_cleanup_observation_outside_bound_is_refused_and_future_trigger_not_invented():
    fixture = Fixture("browser___close")
    fixture.binding["cleanup"]["requested_at"] = (NOW - timedelta(seconds=61)).isoformat()
    assert fixture.run()["facts"]["termination"]["status"] == "refused"
    fixture.binding["cleanup"].update(trigger="deadline", requested_at=datetime.fromtimestamp(DEADLINE, timezone.utc).isoformat())
    assert fixture.run()["facts"]["termination"]["status"] == "pending"


@pytest.mark.parametrize("change", ["account", "region", "foreign-table", "index-selector", "table-collision", "unexpected-field", "duplicate-cell", "ttl", "unsupported"])
def test_invalid_manifest_is_rejected_before_any_reads(change):
    fixture = Fixture("browser___start")
    deployment = fixture.manifest["deployment"]
    if change == "account": deployment["account"] = "000000000001"
    if change == "region": deployment["region"] = "us-west-2"
    if change == "foreign-table": deployment["shared_table_arn"] = SHARED.replace(ACCOUNT, "000000000001")
    if change == "index-selector": deployment["shared_table_arn"] += "/index/cleanup-due"
    if change == "table-collision": deployment["browser_table_arn"] = SHARED
    if change == "unexpected-field": deployment["endpoint_url"] = "https://foreign.invalid"
    if change == "duplicate-cell": fixture.manifest["operations"].append(copy.deepcopy(fixture.binding))
    if change == "ttl": fixture.binding["root"]["ttl_seconds"] = 301
    if change == "unsupported": fixture.binding["operation"] = "slack___post_message"
    with pytest.raises(ValueError): fixture.make()
    assert fixture.calls == []


def test_private_manifest_pin_permissions_and_factory_have_no_ambient_fallback(tmp_path):
    fixture = Fixture()
    path = tmp_path / "private.json"
    path.write_bytes(encoded(fixture.manifest))
    path.chmod(0o600)
    pin = sha(path.read_bytes())
    instance = observer.from_manifest(path, expected_sha256=pin, account=ACCOUNT, region=REGION, read=fixture.read)
    assert instance.capabilities("websearch___search")["supported"]
    assert not instance.capabilities("knowledge___remember")["supported"]
    with pytest.raises(ValueError, match="supply-one-explicit"):
        observer.from_manifest(path, expected_sha256=pin, account=ACCOUNT, region=REGION)
    with pytest.raises(ValueError, match="pin-mismatch"):
        observer.private_manifest(path, "0" * 64)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        observer.private_manifest(path, pin)
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError): observer.private_manifest(link, pin)
    assert not fixture.calls


def test_caller_mutation_cannot_change_validated_private_binding():
    fixture = Fixture()
    instance = fixture.make()
    fixture.binding["arguments"]["query"] = "changed-after-validation"
    fixture.manifest["deployment"]["shared_table_arn"] = BROWSER
    result = instance.observe(operation_ref=START, execution_id=EXECUTION, operation="websearch___search", client="cli", timeout=30)
    assert result["facts"]["correlation"]["status"] == "verified"
    with pytest.raises(ValueError, match="not-in-private"):
        instance.observe(operation_ref=RESULT, execution_id=EXECUTION, operation="websearch___search", client="cli", timeout=30)


def test_sdk_reader_frozen_credentials_real_endpoint_resolution_and_read_allowlist(monkeypatch):
    # Construct clients with literal offline credentials; Stubber prevents network.
    source = boto3.Session(aws_access_key_id="fixture-key", aws_secret_access_key="fixture-secret", aws_session_token="fixture-token", region_name=REGION)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://foreign.invalid")
    monkeypatch.setenv("AWS_ENDPOINT_URL_STS", "https://foreign-sts.invalid")
    reader = observer.SDKReader(source, REGION)
    sts = reader.session.client("sts", config=reader.config)
    reader.clients["sts"] = sts
    assert "foreign" not in sts.meta.endpoint_url
    assert reader.config.retries["total_max_attempts"] == 1
    assert reader.config.connect_timeout == 1 and reader.config.read_timeout == 2
    frozen = reader.session.get_credentials().get_frozen_credentials()
    assert (frozen.access_key, frozen.secret_key, frozen.token) == ("fixture-key", "fixture-secret", "fixture-token")
    with Stubber(sts) as stubber:
        stubber.add_response("get_caller_identity", {"Account": ACCOUNT, "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/fixture-observer/session", "UserId": "fixture-id"}, {})
        assert reader("sts", "get_caller_identity")["Account"] == ACCOUNT
    for service, method in (("dynamodb", "scan"), ("dynamodb", "put_item"), ("bedrock-agentcore", "stop_browser_session"), ("lambda", "invoke")):
        with pytest.raises(ValueError, match="not-read-only"): reader(service, method)


def test_sdk_transport_provenance_is_separate_from_acceptance_with_stubbed_aws_models():
    fixture = Fixture()
    fixture.run()
    expected = copy.deepcopy(fixture.calls)
    source = boto3.Session(aws_access_key_id="fixture-key", aws_secret_access_key="fixture-secret", region_name=REGION)
    reader = observer.SDKReader(source, REGION)
    with ExitStack() as stack:
        for service, method, arguments in expected:
            client = reader.session.client(service, config=reader.config)
            reader.clients[service] = client
            stubber = stack.enter_context(Stubber(client))
            stubber.add_response(method, fixture.read(service, method, **arguments), arguments)
        result = fixture.run(read=reader)
    assert result["transport_kind"] == "sdk-reader" and result["offline_only"] is False
    assert result["acceptance"] == "not-evaluated"
    assert result["facts"]["correlation"]["status"] == "verified"
    assert result["facts"]["cost_usd"]["status"] == "unsupported"
