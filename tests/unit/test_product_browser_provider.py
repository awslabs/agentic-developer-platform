"""Controlled integration of the real browser fixtures, state and report hooks."""

from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from botocore.credentials import Credentials
from botocore.exceptions import ClientError
import pytest

from tests.e2e.new_ui.conftest import product_case_evidence, product_provider, product_run_fixture
from tests.e2e.new_ui.product_fixture_ports import ProtectedRetirement
from tests.e2e.new_ui.product_fixture_provider import BrowserFixtureProvider, pairing_differences
from tests.e2e.new_ui.product_fixture_state import BrowserRunState
from tests.e2e.new_ui.product_recovery import dispatch_owned_product
from tests.e2e.new_ui.test_product_profile import assert_revoked_read, validate_conflict_fixture
from tests.e2e.product_onboarding.product_v02_evidence import CASES
from tests.e2e.product_onboarding.product_v02_record import BrowserEvidence
from tests.e2e.product_onboarding.test_product_v02_record import phase

SOURCE = "a" * 40
ORIGIN = "https://workspace.example"
BINDING = {
    "run_id": "product-42-1", "source_revision": SOURCE,
    "tenant_id": "owned-workspace", "actor_id": "owner", "owner_subject": "owner-sub",
    "nonmember_id": "nonmember", "nonmember_subject": "nonmember-sub", "nonmember_tenant_id": "foreign-workspace",
}


def client_error(code):
    return ClientError({"Error": {"Code": code}}, "controlled")


class S3:
    def __init__(self):
        self.objects, self.writes = {}, []
        self.fail_write = False

    def get_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise client_error("NoSuchKey")
        data, version = self.objects[Key]
        return {"Body": io.BytesIO(data), "ETag": version}

    def put_object(self, **kwargs):
        if self.fail_write:
            raise client_error("AccessDenied")
        key = kwargs["Key"]
        prior = self.objects.get(key)
        if kwargs.get("IfNoneMatch") == "*" and prior or kwargs.get("IfMatch") and (not prior or prior[1] != kwargs["IfMatch"]):
            raise client_error("PreconditionFailed")
        assert kwargs["ServerSideEncryption"] == "aws:kms"
        assert kwargs["SSEKMSKeyId"] == "test-key"
        version = hashlib.sha256(kwargs["Body"]).hexdigest()
        self.objects[key] = kwargs["Body"], version
        self.writes.append(deepcopy(kwargs))
        return {"ETag": version}


class Response:
    def __init__(self, status, value):
        self.status, self.value = status, deepcopy(value)

    def json(self):
        return deepcopy(self.value)

    def body(self):
        return json.dumps(self.value).encode()

    def text(self):
        return self.body().decode()


class API:
    def __init__(self):
        self.status = "active"
        self.products, self.intents, self.events = {}, {}, []
        self.foreign_owner = False
        self.lost_create = False
        self.lost_revoke = False
        self.fail_retire = False
        self.external_change = False

    def member(self):
        return {"id": "owner", "org_id": "owned-workspace", "kind": "member", "revision": "b" * 64,
                "resource": {"id": "owner", "org_id": "owned-workspace", "role": "member",
                             "team_id": "changed" if self.external_change else "", "teams": [], "membership_status": self.status}}

    def get(self, url, **kwargs):
        if url.endswith("/auth/cli/admin-session"):
            return Response(200, {"verified": True, "role": "platform_admin"})
        if "/api/admin/identity/organizations/" in url:
            assert "/api/api/admin/identity/organizations/" in url
        if "/api/api/admin/identity/organizations/" in url and url.endswith("/users"):
            owner = "/owned-workspace/" in url
            return Response(200, {"users": [{"id": "owner" if owner else "nonmember",
                                            "org_id": "owned-workspace" if owner else "foreign-workspace",
                                            "cognito_sub": "owner-sub" if owner else "nonmember-sub",
                                            "cognito_username": "owner" if owner else "nonmember", "role": "member"}]})
        if "/api/api/admin/identity/organizations/" in url:
            return Response(200, {"id": "owned-workspace", "name": "eval-regression-controlled"})
        if "/hierarchy/member/" in url:
            return Response(200, self.member())
        if url.endswith("/auth/me"):
            owner = kwargs["headers"]["Authorization"] != "Bearer nonmember"
            if owner and self.status == "revoked":
                return Response(403, {})
            return Response(200, {"account_type": "human", "is_admin": False,
                                  "user_id": "wrong" if self.foreign_owner else "owner-sub" if owner else "nonmember-sub",
                                  "org_id": "owned-workspace" if owner else "foreign-workspace"})
        if "/api/v1/products" in url and self.status == "revoked" and kwargs["headers"]["Authorization"] == "Bearer owner":
            return Response(403, {"detail": {"error": "tenant_membership_revoked"}})
        if url.endswith("/api/v1/products"):
            return Response(200, {"items": [{"id": key} for key, value in self.products.items() if not value["retired"]], "next_cursor": None})
        if "/api/v1/products/" in url:
            product_id = url.split("/api/v1/products/", 1)[1].split("/")[0]
            product = self.products.get(product_id)
            return Response(200, product["payload"]) if product and not product["retired"] else Response(404, {})
        raise AssertionError(url)

    def post(self, url, **kwargs):
        if url.endswith("/internal/v1/product-evaluation/retire"):
            assert kwargs["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256 ")
            return Response(200, self.retire(json.loads(kwargs["data"])))
        if url.endswith("/members"):
            assert kwargs["data"] == {"user_id": "owner", "role": "member"}
            self.status = "active"
            self.events.append("restored")
            return Response(201, {"id": "owner", "org_id": "owned-workspace"})
        assert url.endswith("/api/v1/products")
        key = kwargs["headers"]["Idempotency-Key"]
        self.events.append(("create", key))
        if key not in self.intents:
            product_id = "product-" + str(len(self.products) + 1)
            project_id = "project-" + str(len(self.products) + 1)
            body = kwargs["data"]
            payload = {"tenant_id": "owned-workspace", "product_id": product_id, "project_id": project_id,
                       "product": {**body, "owner_ref": {"kind": "user", "id": "owner"}},
                       "project": {"id": project_id, "tenant_id": "owned-workspace", "owner_user_id": "owner", "name": body["name"]}}
            self.products[product_id] = {"payload": payload, "retired": False}
            self.intents[key] = product_id
        if self.lost_create:
            self.lost_create = False
            raise TimeoutError("controlled lost create reply")
        return Response(201, self.products[self.intents[key]]["payload"])

    def delete(self, url, **kwargs):
        assert "/hierarchy/member/owner?expected_revision=" in url
        self.status = "revoked"
        self.events.append("revoked")
        if self.lost_revoke:
            raise TimeoutError("controlled lost revocation reply")
        return Response(200, self.member())

    def retire(self, manifest):
        if self.fail_retire:
            raise RuntimeError("controlled retirement refusal")
        assert self.status == "active"
        assert self.intents[manifest["idempotency_key"]] == manifest["product_id"]
        self.products[manifest["product_id"]]["retired"] = True
        self.events.append("retired")
        return {**{key: manifest[key] for key in ("run_id", "product_id", "project_id")}, "fixture_removed": True, "cleanup_revision": 3}


def state(s3, binding=None):
    return BrowserRunState(s3, "private-test-state", "test-key", binding or BINDING)


def provider(api, s3, *, recover=False, namespace=None):
    credentials = {name: {"username": name, "password": "synthetic", "cognito_user_pool_id": "pool",
                          "cognito_client_id": "client"} for name in ("owner", "nonmember")}
    return BrowserFixtureProvider(
        api, ORIGIN, {"access_token": "control"},
        state=BrowserRunState(s3, "private-test-state", "test-key", BINDING, namespace=namespace), retire=api.retire,
        frontend={"verified": True}, evidence_context={"plan_revision": SOURCE}, credentials=credentials,
        recover=recover, authenticate=lambda value: {"access_token": value.username},
    )


def collector(tmp_path):
    return BrowserEvidence(tmp_path, {"GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": SOURCE,
                                     "PRODUCT_EXPECTED_REVISION": SOURCE, "PRODUCT_BROWSER_WORKFLOW_REVISION": SOURCE})


def create_intent(selected, case="create"):
    body = {"name": "eval-product-product-42-1", "purpose": "Verify saved Product workspace information",
            "intended_users": ["Maintainers"], "starting_point": "idea", "project": {"mode": "create"}}
    run = selected.run(case, {"request": body})
    run.record_intent(body, "original-key-123456789")
    return run


@pytest.mark.parametrize("case,test_name", [("create", "test_product_create"), ("conflict", "test_product_conflict_denial"),
                                           ("evidence", "test_product_evidence")])
def test_actual_fixture_to_collector_lifecycle(tmp_path, case, test_name):
    api, s3 = API(), S3()
    selected, evidence = provider(api, s3), collector(tmp_path)
    name = {"create": "live_browser_create", "conflict": "live_browser_conflict_denial", "evidence": "live_browser_evidence"}[case]
    request = SimpleNamespace(config=SimpleNamespace(product_evidence=evidence), node=SimpleNamespace(name=test_name, nodeid=CASES[name][2]))
    lifecycle = product_run_fixture.__wrapped__(request, selected)
    fixture = next(lifecycle)
    record = product_case_evidence.__wrapped__(request, fixture)
    assert record is evidence.records[name]
    if case == "create":
        run = create_intent(selected)
        run.reconcile(record.request(api), ORIGIN)
    else:
        validate_conflict_fixture(fixture, fixture["owner"], SOURCE)
    if case == "conflict":
        fixture["revoke"]()
        assert selected.state.document["restore_required"] is True
        assert_revoked_read(record.request(api).get(ORIGIN + "/api/v1/products", headers=selected.headers(fixture["revoked"])),
                            ("eval-product-product-42-1", "Verify saved Product workspace information"))
    with pytest.raises(StopIteration):
        next(lifecycle)
    for when in ("setup", "call", "teardown"):
        phase(evidence, name, when, "passed")
    inputs = evidence.write()
    assert inputs["cases"][name]["status"] == "pass"
    assert record.cleanup[-1]["owned_absent"] is True
    assert record.cleanup[-1]["shared_unchanged"] is True
    assert record.reads
    assert api.status == "active"
    assert all(item["retired"] for item in api.products.values())
    if case == "conflict":
        assert api.events.index("revoked") < api.events.index("restored") < api.events.index("retired")
    selected.recover()
    assert selected.state.read(selected.state.lock_key)[0] == {"run_id": None}


def test_connected_single_project_cleanup_persists_identity_before_dispatch(tmp_path):
    api, s3 = API(), S3()
    selected = provider(api, s3)
    # Identity admission is exercised by the production-builder tests; select
    # the connected cleanup path without rewriting the store's ownership key.
    selected.binding = {**selected.binding, "scope_fixture_mode": "connected_scope", "editor_id": "dedicated-editor"}
    captured = []
    def retire(manifest):
        saved = selected.state.document["cases"]["v11_release"]["manifest"]
        assert saved == manifest
        assert manifest["fixture_editor_id"] == "dedicated-editor"
        assert manifest["scope_fixture_mode"] == "connected_scope"
        assert "scope" not in manifest
        captured.append(deepcopy(manifest))
        return api.retire(manifest)
    selected.retire = retire
    record = collector(tmp_path).records["live_browser_conflict_denial"]
    with selected.fixture("v11_release", record) as fixture:
        validate_conflict_fixture(fixture, fixture["owner"], SOURCE, name_suffix="-v11_release")
        with pytest.raises(ValueError, match="create name differs"):
            validate_conflict_fixture(fixture, fixture["owner"], SOURCE)
    assert len(captured) == 1 and record.cleanup[-1]["owned_absent"] is True
    # Recovery reuses the same persisted identity, even after a lost response.
    selected.cleanup("v11_release", api)
    assert captured == [captured[0], captured[0]]


@pytest.mark.parametrize("lost_revoke", [False, True])
def test_assertion_or_lost_revocation_response_restores_and_cleans(tmp_path, lost_revoke):
    api, s3 = API(), S3()
    selected, evidence = provider(api, s3), collector(tmp_path)
    record = evidence.records["live_browser_conflict_denial"]
    api.lost_revoke = lost_revoke
    with pytest.raises((AssertionError, TimeoutError)):
        with selected.fixture("conflict", record) as fixture:
            fixture["revoke"]()
            raise AssertionError("controlled browser assertion")
    assert api.status == "active"
    assert record.cleanup[-1]["fixture_removed"] is True


def test_lost_create_reply_reconciles_only_original_key(tmp_path):
    api, s3 = API(), S3()
    selected = provider(api, s3)
    api.lost_create = True
    with pytest.raises(TimeoutError):
        with selected.fixture("evidence", collector(tmp_path).records["live_browser_evidence"]):
            pytest.fail("lost response must not reach the browser")
    assert len(api.products) == 1
    assert len({event[1] for event in api.events if isinstance(event, tuple)}) == 1
    assert next(iter(api.products.values()))["retired"]


def test_new_process_recovers_persisted_intent_and_membership(tmp_path):
    api, s3 = API(), S3()
    selected = provider(api, s3)
    selected.state.save({**selected.state.document, "cases": {"create": {"before": {}, "document": None, "manifest": None}}})
    run = create_intent(selected)
    run.reconcile(api, ORIGIN)
    selected.membership.revoke(selected.state)
    recovered = provider(api, s3, recover=True)
    receipt = recovered.recover()
    assert receipt["cases"]["create"]["owned_absent"] is True
    assert api.status == "active"
    assert len(api.products) == 1
    assert recovered.state.read(recovered.state.lock_key)[0] == {"run_id": None}


def test_suite_cleanup_retains_real_retirement_membership_and_released_lock(tmp_path):
    from tests.e2e.new_ui.product_suite_cleanup import retain_cleanup

    api, s3 = API(), S3()
    selected = provider(api, s3, namespace="pass-1-issue-8252")
    with selected.fixture("conflict", collector(tmp_path / "records").records["live_browser_conflict_denial"]):
        selected.membership.revoke(selected.state)
    receipt = selected.recover()
    environment = {"GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1",
                   "PRODUCT_EXPECTED_REVISION": SOURCE, "PRODUCT_SUITE_ID": "pass-1-issue-8252"}
    path = Path(retain_cleanup(receipt, tmp_path, environment))
    retained = json.loads(path.read_text())
    assert retained["cases"]["conflict"]["owned_absent"] is True
    assert retained["memberships"]["owner"]["observed"] == api.member()
    assert retained["ownership_lock"]["version"] == selected.state.read(selected.state.lock_key)[1]
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        retain_cleanup(receipt, tmp_path, environment)
    for changes in ({"PRODUCT_SUITE_ID": "pass-2-issue-8252"}, {"GITHUB_RUN_ATTEMPT": "2"}):
        with pytest.raises(ValueError, match="original workflow"):
            retain_cleanup(receipt, tmp_path, {**environment, **changes})


def test_suite_cleanup_refuses_external_membership_changes_even_without_revoke():
    api, s3 = API(), S3()
    selected = provider(api, s3)
    api.external_change = True
    with pytest.raises(ValueError, match="membership differs"):
        selected.recover()
    assert selected.state.read(selected.state.lock_key)[0]["run_id"] == BINDING["run_id"]


def test_suite_cleanup_refuses_an_unobserved_lock_release(monkeypatch):
    selected = state(S3())
    selected.claim()
    read = selected.read

    def stale_lock(key):
        value, version = read(key)
        if key == selected.lock_key and value == {"run_id": None}:
            return selected.lease, "old-version"
        return value, version

    monkeypatch.setattr(selected, "read", stale_lock)
    with pytest.raises(RuntimeError, match="lock release was not independently"):
        selected.release()


def test_failed_persistence_aborts_browser_dispatch(tmp_path):
    api, s3 = API(), S3()
    selected = provider(api, s3)
    with selected.fixture("create", collector(tmp_path).records["live_browser_create"]):
        s3.fail_write = True
        run = selected.run("create", {"request": {"name": "eval-product-product-42-1", "purpose": "purpose",
                                                 "intended_users": ["Maintainers"], "starting_point": "idea", "project": {"mode": "create"}}})
        events = []
        route = SimpleNamespace(request=SimpleNamespace(method="POST", post_data_json=run.request_body,
                                                       headers={"idempotency-key": "original-key-123456789"}),
                                abort=lambda **kwargs: events.append("aborted"), continue_=lambda: events.append("sent"))
        assert dispatch_owned_product(route, run) is False
        assert events == ["aborted"]
        s3.fail_write = False
    assert api.products == {}


@pytest.mark.parametrize("namespace", [None, "issue-8252"])
def test_refused_retirement_keeps_owned_state_for_recovery(tmp_path, namespace):
    api, s3 = API(), S3()
    selected = provider(api, s3, namespace=namespace)
    api.fail_retire = True
    record = collector(tmp_path).records["live_browser_evidence"]
    with pytest.raises(RuntimeError, match="retirement refusal"):
        with selected.fixture("evidence", record):
            pass
    assert record.cleanup == []
    assert selected.state.read(selected.state.lock_key)[0]["run_id"] == BINDING["run_id"]
    with pytest.raises(RuntimeError, match="retirement refusal"):
        provider(api, s3, recover=True, namespace=namespace).recover()
    assert selected.state.read(selected.state.lock_key)[0]["run_id"] == BINDING["run_id"]
    assert not selected.state.read(selected.state.key)[0]["cases"]["evidence"].get("cleanup")
    api.fail_retire = False
    receipt = provider(api, s3, recover=True, namespace=namespace).recover()
    assert receipt["suite_id"] == namespace
    assert receipt["cases"]["evidence"]["owned_absent"]


def test_wrong_peer_cannot_claim_or_mutate():
    api, s3 = API(), S3()
    api.foreign_owner = True
    with pytest.raises(ValueError, match="authenticated peer"):
        provider(api, s3)
    assert s3.objects == {}
    assert api.events == []


def test_external_membership_change_is_not_overwritten(tmp_path):
    api, s3 = API(), S3()
    selected = provider(api, s3)
    with pytest.raises(ValueError, match="externally changed"):
        with selected.fixture("conflict", collector(tmp_path).records["live_browser_conflict_denial"]) as fixture:
            fixture["revoke"]()
            api.external_change = True
    assert api.status == "revoked"
    assert "restored" not in api.events
    assert selected.state.document["restore_required"] is True


def test_durable_binding_lease_and_conditional_write_guards():
    s3 = S3()
    first = state(s3)
    first.claim()
    with pytest.raises(ValueError, match="binding differs"):
        state(s3, {**BINDING, "source_revision": "c" * 40})
    with pytest.raises(ValueError, match="active browser run"):
        state(s3, {**BINDING, "run_id": "product-43-1"}).claim()
    recovering = state(s3)
    recovering.claim(recover=True)
    with pytest.raises(ValueError, match="ownership changed"):
        first.save(first.document)
    old = deepcopy(recovering.document)
    recovering.save(old)
    stale = state(s3)
    stale.lock_version = recovering.lock_version
    stale.lease = recovering.lease
    recovering.save({**old, "updated": True})
    with pytest.raises(ClientError, match="PreconditionFailed"):
        stale.save(old)


def test_access_denied_is_not_mistaken_for_missing_state():
    def denied(**kwargs):
        raise client_error("AccessDenied")

    s3 = SimpleNamespace(get_object=denied, list_objects_v2=denied)
    with pytest.raises(RuntimeError, match="could not be read"):
        state(s3)


def test_prefix_scoped_browser_state_proves_absence_before_claiming():
    class PrefixScopedS3(S3):
        def get_object(self, *, Bucket, Key):
            if Key not in self.objects:
                raise client_error("AccessDenied")
            return super().get_object(Bucket=Bucket, Key=Key)

        def list_objects_v2(self, *, Bucket, Prefix, MaxKeys):
            assert Bucket == "private-test-state" and MaxKeys == 1
            assert Prefix.startswith("cli-uplift-eval/state/product-browser/")
            assert Prefix.endswith(("/product-42-1.json", "/owner.json"))
            assert not any(key.startswith(Prefix) for key in self.objects)
            return {"IsTruncated": False, "KeyCount": 0}

    s3 = PrefixScopedS3()
    selected = state(s3)
    assert not s3.writes
    selected.claim()
    assert state(s3).document == selected.document
    assert len(s3.writes) == 2
    assert all(write["IfNoneMatch"] == "*" for write in s3.writes)


@pytest.mark.parametrize("listing", [
    "existing", {}, {"IsTruncated": True}, {"IsTruncated": False, "Contents": "invalid"},
    {"IsTruncated": False, "Contents": [{}]},
])
def test_state_read_refusal_cannot_be_bypassed_by_an_unproven_listing(listing):
    def list_objects_v2(**kwargs):
        if listing == "existing":
            return {"IsTruncated": False, "Contents": [{"Key": kwargs["Prefix"]}]}
        return listing

    s3 = SimpleNamespace(
        get_object=lambda **kwargs: (_ for _ in ()).throw(client_error("AccessDenied")),
        list_objects_v2=list_objects_v2,
    )
    with pytest.raises(RuntimeError, match="could not be read"):
        state(s3)


@pytest.mark.parametrize("identity", ["wrong-role", "wrong-account", "valid"])
def test_v01_retirement_uses_exact_role_and_signed_internal_route(identity):
    account, role = "123456789012", "product-evaluator-retirement"
    origin = "https://example.execute-api.us-east-1.amazonaws.com/internal"
    arn = f"arn:aws:sts::{account}:assumed-role/{role}/fixture"
    observed = {"Account": account, "Arn": arn}
    if identity == "wrong-role":
        observed["Arn"] = arn.replace(role, "checks")
    if identity == "wrong-account":
        observed["Account"] = "111111111111"
    session = SimpleNamespace(client=lambda service: SimpleNamespace(get_caller_identity=lambda: observed),
                              get_credentials=lambda: Credentials("synthetic-key", "synthetic-secret", "synthetic-token"))
    sent = []
    request = SimpleNamespace(post=lambda url, **kwargs: sent.append((url, kwargs)) or Response(200, {"fixture_removed": True}))
    arguments = {"account": account, "region": "us-east-1", "role_arn": f"arn:aws:iam::{account}:role/{role}",
                 "checks_role": f"arn:aws:iam::{account}:role/checks", "origin": origin}
    if identity != "valid":
        with pytest.raises(ValueError, match="selected evaluator role"):
            ProtectedRetirement(request, session, **arguments)
        assert sent == []
        return
    retire = ProtectedRetirement(request, session, **arguments)
    manifest = {"run_id": BINDING["run_id"], "idempotency_key": "original-key-123456789"}
    assert retire(manifest)["fixture_removed"]
    assert sent[0][0] == origin + "/internal/v1/product-evaluation/retire"
    assert json.loads(sent[0][1]["data"]) == manifest
    assert sent[0][1]["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256 ")


@pytest.fixture
def builder(tmp_path, monkeypatch):
    import boto3
    from modules.gateway.deployment_evidence.store import EvidenceStore
    from src.orchestration.deployment_runtime_reader import PilotRuntimeReader
    from tests.e2e.new_ui import product_fixture_provider as module

    monkeypatch.chdir(tmp_path)
    api, s3, calls = API(), S3(), []
    role = "arn:aws:iam::123456789012:role/product-evaluator-retirement"
    environment = {
        "ADP_CHECKS_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-trusted-checks",
        "PRODUCT_EXPECTED_REVISION": SOURCE, "PRODUCT_RELEASE_WORKFLOW_REVISION": "b" * 40,
        "GITHUB_REPOSITORY": "aws-e/adp", "GITHUB_REPOSITORY_ID": "5", "PRODUCT_RELEASE_RUN_ID": "10",
        "PRODUCT_RELEASE_ATTEMPT": "1", "AWS_REGION": "us-east-1", "ENVIRONMENT": "dev",
        "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1",
    }
    config = {**BINDING, "base_url": "https://example.cloudfront.net", "plan_revision": SOURCE, "requirements_revision": SOURCE,
              "backend_release": {"run_id": 11, "attempt": 1, "workflow_revision": "c" * 40},
              "state_bucket": "private-test-state", "state_kms_key_id": "test-key", "retirement_role": role,
              "internal_invoke_url": "https://example.execute-api.us-east-1.amazonaws.com/internal",
              "credentials": {name: {"username": name, "password": "synthetic", "cognito_user_pool_id": "pool",
                                     "cognito_client_id": "client"} for name in ("owner", "nonmember")}}
    frontend = {"schema_version": 1, "repository_id": 5, "run_id": 10, "run_attempt": 1,
                "source_revision": SOURCE, "workflow_revision": "b" * 40, "workflow_path": ".github/workflows/gateway-frontend-deploy.yml",
                "account_id": "123456789012", "region": "us-east-1", "component": "gateway-frontend", "assets": {"index.html": "d" * 64}}
    backend = {**frontend, "run_id": 11, "workflow_revision": "c" * 40, "workflow_path": ".github/workflows/gateway-deploy.yml",
               "component": "gateway-backend", "resource_id": "test-cluster/test-namespace", "assets": {},
               "image_digest": "sha256:" + "e" * 64, "product_profile_enabled": True, "product_onboarding_enabled": False,
               "product_profile_schema_version": "1.0", "produced_at": "2026-10-20T00:00:00Z"}

    def read(store, **kwargs):
        calls.append(("signed", kwargs))
        return json.dumps(frontend if kwargs["name"] == "gateway-frontend" else backend).encode(), "d" * 64, "signed-" + kwargs["name"]

    def assume(**kwargs):
        assert kwargs["RoleArn"] == role
        calls.append(("assume", kwargs))
        return {"Credentials": {"AccessKeyId": "synthetic", "SecretAccessKey": "synthetic", "SessionToken": "synthetic"}}

    def client(service, *, retirement=False):
        if service == "sts":
            return SimpleNamespace(get_caller_identity=lambda: {
                "Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/" +
                ("product-evaluator-retirement" if retirement else "adp-dev-trusted-checks") + "/fixture",
            }, assume_role=assume)
        if service == "ssm":
            return SimpleNamespace(get_parameter=lambda **kwargs: {"Parameter": {"Value": "D123"}})
        if service == "cloudfront":
            return SimpleNamespace(get_distribution=lambda **kwargs: {"Distribution": {
                "ARN": "arn:aws:cloudfront::123456789012:distribution/D123", "Status": "Deployed",
                "DistributionConfig": {"Enabled": True}, "DomainName": "example.cloudfront.net",
            }})
        if service == "secretsmanager":
            return SimpleNamespace(get_secret_value=lambda **kwargs: {"SecretString": json.dumps(config)})
        if service == "eks":
            return SimpleNamespace(describe_cluster=lambda **kwargs: {"cluster": {"name": kwargs["name"]}})
        assert service == "s3"
        return s3

    def inspect(reader, session, description, namespace, **kwargs):
        calls.append(("runtime", kwargs))
        assert description["name"] == "test-cluster" and namespace == "test-namespace"
        artifact = kwargs["artifact"]
        values = {"migration_head": "profile-head", "image_digest": artifact.image_digest,
                  "cli_manifest_sha256": "f" * 64, "product_profile_enabled": True,
                  "product_onboarding_enabled": artifact.product_onboarding_enabled}
        return SimpleNamespace(**values, model_dump=lambda **kwargs: values)

    monkeypatch.setattr(boto3, "Session", lambda **kwargs: SimpleNamespace(
        client=lambda service: client(service, retirement="aws_access_key_id" in kwargs), region_name="us-east-1",
        get_credentials=lambda: Credentials("synthetic-key", "synthetic-secret", "synthetic-token"),
    ))
    monkeypatch.setattr(EvidenceStore, "read", read)
    monkeypatch.setattr(PilotRuntimeReader, "inspect", inspect)
    monkeypatch.setattr(module, "check_served_assets", lambda *args: calls.append(("assets", args)))
    monkeypatch.setattr(module, "get_cognito_tokens", lambda value: {"access_token": value.username})
    return module, environment, config, frontend, backend, api, s3, calls


def test_production_builder_binds_verified_releases_and_authenticated_fixture(builder):
    module, environment, config, frontend, backend, api, s3, calls = builder
    selected = module.build_provider(environment, config["base_url"], api, {"access_token": "control"})
    assert selected.owner["actor_id"] == BINDING["actor_id"]
    assert selected.nonmember["tenant_id"] != selected.owner["tenant_id"]
    assert selected.evidence_context["identities"]["api_image_digest"] == backend["image_digest"]
    assert selected.evidence_context["deployment"]["observed"]["product_profile_enabled"] is True
    assert [event[1]["name"] for event in calls if event[0] == "signed"] == ["gateway-frontend", "gateway-backend"]
    assert any(event[0] == "runtime" for event in calls)
    assert any(event[0] == "assets" for event in calls)
    assert selected.state.document["binding"]["retirement_role"] == config["retirement_role"]
    selected.recover()


@pytest.mark.parametrize("enabled", [True, False])
def test_product_only_builder_accepts_shared_release_admission(builder, enabled):
    module, environment, config, _, backend, api, _, _ = builder
    backend["product_onboarding_enabled"] = enabled
    selected = module.build_provider(environment, config["base_url"], api, {"access_token": "control"})
    assert selected.evidence_context["deployment"]["observed"]["product_onboarding_enabled"] is enabled
    selected.recover()


@pytest.mark.parametrize("required", [True, False])
def test_explicit_admission_requirement_rejects_other_release_before_claim(builder, required):
    module, environment, config, _, backend, api, s3, _ = builder
    backend["product_onboarding_enabled"] = not required
    with pytest.raises(ValueError, match="product_onboarding_enabled"):
        module.build_provider(environment, config["base_url"], api, {"access_token": "control"},
                              onboarding_enabled=required)
    assert s3.writes == [] and api.events == []


def test_product_only_builder_still_rejects_runtime_admission_drift(builder, monkeypatch):
    from src.orchestration.deployment_runtime_reader import PilotRuntimeReader

    module, environment, config, _, backend, api, s3, _ = builder
    original = PilotRuntimeReader.inspect

    def inspect(*args, **kwargs):
        result = original(*args, **kwargs)
        result.product_onboarding_enabled = not backend["product_onboarding_enabled"]
        return result

    monkeypatch.setattr(PilotRuntimeReader, "inspect", inspect)
    with pytest.raises(ValueError, match="Observed onboarding admission differs"):
        module.build_provider(environment, config["base_url"], api, {"access_token": "control"})
    assert s3.writes == [] and api.events == []


@pytest.mark.parametrize("failure", [None, "install", "suite"])
def test_resume_suite_installs_verified_cli_and_restores_owned_state(monkeypatch, failure):
    from tests.e2e.new_ui import conftest, product_fixture_provider
    from tests.e2e.product_onboarding import product_v04_cli

    events, scratch = [], []
    provider = SimpleNamespace(evidence_context={"identities": {"cli_checksum": "a" * 64}},
                               recover=lambda: events.append("recovered"))
    context = SimpleNamespace(request=object(), close=lambda: events.append("closed"))
    browser = SimpleNamespace(new_context=lambda **kwargs: context)
    monkeypatch.setattr(product_fixture_provider, "build_provider", lambda *args, **kwargs: provider)
    monkeypatch.setenv("PRODUCT_RESUME_CLI_PATH", "previous-cli")
    monkeypatch.delenv("PRODUCT_RESUME_CLI_HASHES", raising=False)

    def install(selected, evidence, directory):
        assert selected is provider and evidence.context is provider.evidence_context
        scratch.append(directory)
        if failure == "install":
            raise RuntimeError("served CLI mismatch")
        binary = directory / "bin" / "adp"
        binary.parent.mkdir()
        binary.write_text("fixture CLI")
        events.append("installed")
        return binary, {"adp": "b" * 64}

    monkeypatch.setattr(product_v04_cli, "install_served_cli", install)
    fixture = conftest.product_resume_provider.__wrapped__(browser, {}, "https://workspace.example")
    if failure == "install":
        with pytest.raises(RuntimeError, match="served CLI mismatch"):
            next(fixture)
    else:
        assert next(fixture) is provider
        assert Path(os.environ["PRODUCT_RESUME_CLI_PATH"]).is_file()
        assert json.loads(os.environ["PRODUCT_RESUME_CLI_HASHES"]) == {"adp": "b" * 64}
        if failure == "suite":
            with pytest.raises(RuntimeError, match="live assertion failed"):
                fixture.throw(RuntimeError("live assertion failed"))
        else:
            with pytest.raises(StopIteration):
                next(fixture)
    assert events[-2:] == ["recovered", "closed"]
    assert os.environ["PRODUCT_RESUME_CLI_PATH"] == "previous-cli"
    assert "PRODUCT_RESUME_CLI_HASHES" not in os.environ
    assert scratch and not scratch[0].exists()


@pytest.mark.parametrize("inputs", [None, [], [{"selection": "untrusted"}]])
def test_scope_builder_rejects_missing_or_untrusted_protected_inputs_before_mutation(builder, inputs):
    module, environment, config, _, _, api, s3, calls = builder
    config["scope_inputs"] = inputs
    config["scope_editor"] = {"actor_id": "other-editor", "subject": "other-subject", "team_id": "team"}
    config["credentials"]["editor"] = dict(config["credentials"]["owner"], username="other-editor")
    with pytest.raises(ValueError, match="two scope|Scope inputs"):
        module.build_provider(environment, config["base_url"], api, {"access_token": "control"},
                              scope_editor_enabled=True)
    assert not api.products and not s3.writes


@pytest.mark.parametrize("ambient_origin", [None, "https://unselected.execute-api.us-east-1.amazonaws.com/dev"])
def test_browser_runtime_probe_uses_protected_fixture_origin(builder, monkeypatch, ambient_origin):
    from datetime import datetime, timezone
    import httpx
    from src.orchestration.deployment_runtime_reader import KubernetesRuntime, PilotRuntimeReader

    module, environment, config, frontend, backend, api, s3, calls = builder
    if ambient_origin is None:
        monkeypatch.delenv("ADP_RUNTIME_PROOF_INVOKE_URL", raising=False)
    else:
        monkeypatch.setenv("ADP_RUNTIME_PROOF_INVOKE_URL", ambient_origin)
    observed = []

    def respond(request):
        assert str(request.url) == config["internal_invoke_url"] + "/internal/v1/runtime-proof/schema"
        assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        payload = json.loads(request.content)
        assert payload["namespace"] == "test-namespace" and payload["pod_uid"] == "serving-pod"
        observed.append(str(request.url))
        return httpx.Response(200, json={**payload, "observed_at": datetime.now(timezone.utc).isoformat()})

    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))

    def initialize(runtime, scoped, description, namespace, *, proof_invoke_url=None):
        runtime.scoped, runtime.namespace, runtime.proof_invoke_url = scoped, namespace, proof_invoke_url

    monkeypatch.setattr(KubernetesRuntime, "__init__", initialize)
    original_inspect = PilotRuntimeReader.inspect

    def inspect(reader, session, description, namespace, **kwargs):
        runtime = reader.kubernetes(session, description, namespace)
        runtime.protected_schema({"metadata": {"uid": "serving-pod"}})
        return original_inspect(reader, session, description, namespace, **kwargs)

    monkeypatch.setattr(PilotRuntimeReader, "inspect", inspect)
    selected = module.build_provider(environment, config["base_url"], api, {"access_token": "control"})
    assert observed == [config["internal_invoke_url"] + "/internal/v1/runtime-proof/schema"]
    selected.recover()


@pytest.mark.parametrize("wrong", ["frontend", "backend", "target", "retirement-role"])
def test_production_builder_refuses_mismatch_before_claim_or_product_mutation(builder, wrong):
    module, environment, config, frontend, backend, api, s3, calls = builder
    if wrong == "frontend":
        frontend["source_revision"] = "f" * 40
    elif wrong == "backend":
        backend["source_revision"] = "f" * 40
    elif wrong == "target":
        config["base_url"] = "https://wrong.example"
    else:
        config["retirement_role"] = environment["ADP_CHECKS_ROLE_ARN"]
    with pytest.raises(ValueError):
        module.build_provider(environment, "https://example.cloudfront.net", api, {"access_token": "control"})
    assert s3.writes == []
    assert api.events == []


def test_production_builder_uses_the_same_suite_namespace_on_recovery(builder):
    module, environment, config, frontend, backend, api, s3, calls = builder
    environment["PRODUCT_SUITE_ID"] = "issue-8252"
    selected = module.build_provider(environment, config["base_url"], api,
                                     {"access_token": "control"})
    assert selected.state.key.endswith("/product-42-1-issue-8252.json")
    with pytest.raises(ValueError, match="original suite namespace"):
        module.build_provider({key: value for key, value in environment.items()
                               if key != "PRODUCT_SUITE_ID"}, config["base_url"], api,
                              {"access_token": "control"}, recovery_run_id="product-42-1")
    recovered = module.build_provider(environment, config["base_url"], api,
                                      {"access_token": "control"}, recovery_run_id="product-42-1")
    recovered.recover()


def test_production_builder_recovers_legacy_suite_from_explicit_empty_namespace(builder):
    module, environment, config, frontend, backend, api, s3, calls = builder
    environment["PRODUCT_SUITE_ID"] = ""
    selected = module.build_provider(environment, config["base_url"], api, {"access_token": "control"})
    assert selected.state.namespace is None
    assert selected.recover()["suite_id"] is None
    resumed = module.build_provider(environment, config["base_url"], api,
                                    {"access_token": "control"}, recovery_run_id="product-42-1")
    assert resumed.state.namespace is None
    assert resumed.recover()["suite_id"] is None


def test_package_fixture_runs_all_three_cases_through_signed_cleanup_and_collector(builder, tmp_path, monkeypatch):
    module, environment, config, frontend, backend, api, s3, calls = builder
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    closed = []
    context = SimpleNamespace(request=api, close=lambda: closed.append(True))
    browser = SimpleNamespace(new_context=lambda **kwargs: context)
    package = product_provider.__wrapped__(browser, {"access_token": "control"}, config["base_url"],
                                           SimpleNamespace(config=SimpleNamespace()))
    selected = next(package)
    evidence = collector(tmp_path / "test-results")
    for case, test_name, name in (
        ("create", "test_product_create", "live_browser_create"),
        ("conflict", "test_product_conflict_denial", "live_browser_conflict_denial"),
        ("evidence", "test_product_evidence", "live_browser_evidence"),
    ):
        request = SimpleNamespace(config=SimpleNamespace(product_evidence=evidence), node=SimpleNamespace(name=test_name, nodeid=CASES[name][2]))
        lifecycle = product_run_fixture.__wrapped__(request, selected)
        fixture = next(lifecycle)
        record = product_case_evidence.__wrapped__(request, fixture)
        if case == "create":
            create_intent(selected).reconcile(record.request(api), config["base_url"])
        else:
            validate_conflict_fixture(fixture, fixture["owner"], SOURCE)
        if case == "conflict":
            fixture["revoke"]()
            assert_revoked_read(record.request(api).get(config["base_url"] + "/api/v1/products", headers=selected.headers(fixture["revoked"])),
                                ("eval-product-product-42-1", "Verify saved Product workspace information"))
        with pytest.raises(StopIteration):
            next(lifecycle)
        for when in ("setup", "call", "teardown"):
            phase(evidence, name, when, "passed")
    with pytest.raises(StopIteration):
        next(package)
    inputs = evidence.write()
    assert all(case["status"] == "pass" for case in inputs["cases"].values())
    assert inputs["cleanup"]["status"] == "verified"
    assert inputs["surfaces"]["api"]["status"] == "pass"
    assert inputs["deployment"]["observed"]["api_image_digest"] == backend["image_digest"]
    assert len(api.products) == 3 and all(product["retired"] for product in api.products.values())
    assert closed == [True]
    assert selected.state.read(selected.state.lock_key)[0] == {"run_id": None}


# Issue #8246: the documented protected configuration had never been published
# (the entry existed with no version), so a missing or partial configuration
# surfaced as a raw client error or a KeyError in the middle of fixture
# construction. These cover the read-only preflight that now states the
# required contents, keeps release pairing dynamic, and is taken by a fresh B02
# run only - recovery must still construct the provider in order to restore a
# temporarily revoked owner.


@pytest.fixture
def bootstrap():
    from tests.e2e.new_ui import product_fixture_bootstrap as module

    return module


def test_fixture_preflight_accepts_the_documented_configuration_without_changing_it(builder, bootstrap):
    module, environment, config, _, _, api, s3, calls = builder
    original = deepcopy(config)
    proof = bootstrap.preflight(config, base_url=config["base_url"], environment=environment)
    assert proof["configuration"] == "complete"
    assert proof["tenant_id"] == BINDING["tenant_id"] and proof["nonmember_tenant_id"] == BINDING["nonmember_tenant_id"]
    assert proof["backend_release"] == config["backend_release"]
    assert config == original and s3.writes == [] and api.events == [] and not api.products


@pytest.mark.parametrize("missing", ["credentials", "backend_release", "state_kms_key_id", "nonmember_subject"])
def test_fixture_preflight_names_each_missing_documented_key(builder, bootstrap, missing):
    module, environment, config, *_ = builder
    config.pop(missing)
    with pytest.raises(ValueError, match="missing documented keys: " + missing):
        bootstrap.preflight(config, base_url=config["base_url"], environment=environment)


@pytest.mark.parametrize("partial", [
    {},
    {"username": "nonmember"},
    {"username": "nonmember", "password": "", "cognito_user_pool_id": "pool", "cognito_client_id": "client"},
])
def test_fixture_preflight_rejects_unusable_peer_credentials(builder, bootstrap, partial):
    module, environment, config, *_ = builder
    config["credentials"] = {**config["credentials"], "nonmember": partial}
    with pytest.raises(ValueError, match="credentials are unusable for peer: nonmember"):
        bootstrap.preflight(config, base_url=config["base_url"], environment=environment)


@pytest.mark.parametrize("shared", ["tenant_id", "actor_id", "owner_subject"])
def test_fixture_preflight_rejects_peers_that_are_not_distinct_people(builder, bootstrap, shared):
    module, environment, config, *_ = builder
    counterpart = {"tenant_id": "nonmember_tenant_id", "actor_id": "nonmember_id", "owner_subject": "nonmember_subject"}
    config[counterpart[shared]] = config[shared]
    with pytest.raises(ValueError, match="distinct ordinary identities"):
        bootstrap.preflight(config, base_url=config["base_url"], environment=environment)


@pytest.mark.parametrize("blank", ["retirement_role", "internal_invoke_url", "tenant_id"])
def test_fixture_preflight_rejects_an_empty_documented_value(builder, bootstrap, blank):
    module, environment, config, *_ = builder
    config[blank] = ""
    with pytest.raises(ValueError, match="value is empty: " + blank):
        bootstrap.preflight(config, base_url=config["base_url"], environment=environment)


def test_fixture_preflight_rejects_another_origin(builder, bootstrap):
    module, environment, config, *_ = builder
    with pytest.raises(ValueError, match="another origin or run"):
        bootstrap.preflight(config, base_url="https://unselected.example", environment=environment)


@pytest.mark.parametrize("code", ["ResourceNotFoundException", "AccessDeniedException"])
def test_unpublished_fixture_configuration_states_the_required_contents(bootstrap, code):
    def get_secret_value(**kwargs):
        assert kwargs["SecretId"] == "adp/dev/gateway/product-browser-fixtures"
        raise client_error(code)

    with pytest.raises(ValueError) as error:
        bootstrap.read_configuration(SimpleNamespace(get_secret_value=get_secret_value), "dev")
    reported = str(error.value)
    assert code in reported and "adp/dev/gateway/product-browser-fixtures" in reported
    assert all(key in reported for key in bootstrap.DOCUMENTED_KEYS)


def test_unpublished_b03_configuration_names_extra_fixture_inputs(bootstrap):
    def unavailable(**kwargs):
        raise client_error("ResourceNotFoundException")

    with pytest.raises(ValueError) as error:
        bootstrap.read_configuration(SimpleNamespace(get_secret_value=unavailable), "dev", scope_editor_enabled=True)
    for field in ("scope_editor", "scope_inputs", "credentials.editor"):
        assert field in str(error.value)


def test_unrelated_configuration_read_failures_are_not_reinterpreted(bootstrap):
    def get_secret_value(**kwargs):
        raise client_error("ThrottlingException")

    with pytest.raises(ClientError):
        bootstrap.read_configuration(SimpleNamespace(get_secret_value=get_secret_value), "dev")


@pytest.mark.parametrize("body", ["", None, json.dumps([BINDING]), "x" * 65537])
def test_fixture_configuration_must_be_a_bounded_json_object(bootstrap, body):
    client = SimpleNamespace(get_secret_value=lambda **kwargs: {"SecretString": body})
    with pytest.raises(ValueError):
        bootstrap.read_configuration(client, "dev")


@pytest.mark.parametrize("selected,accepted", [
    ({}, True),
    ({"run_id": "11", "attempt": "1", "workflow_revision": "c" * 40}, True),
    ({"run_id": "12", "attempt": "1", "workflow_revision": "c" * 40}, False),
    ({"run_id": "11", "attempt": "1", "workflow_revision": "d" * 40}, False),
])
def test_fixture_preflight_requires_the_release_pairing_selected_for_this_run(builder, bootstrap, selected, accepted):
    module, environment, config, _, backend, *_ = builder
    for key, value in selected.items():
        environment["PRODUCT_BACKEND_RELEASE_" + key.upper()] = value
    if accepted:
        proof = bootstrap.preflight(config, base_url=config["base_url"], environment=environment)
        assert str(proof["backend_release"]["run_id"]) == str(backend["run_id"])
    else:
        with pytest.raises(ValueError, match="not the pairing selected for this run"):
            bootstrap.preflight(config, base_url=config["base_url"], environment=environment)


@pytest.mark.parametrize("selected", [
    {"run_id": "11"},
    {"run_id": "11", "attempt": "1", "workflow_revision": "not-a-commit"},
    {"run_id": "0", "attempt": "1", "workflow_revision": "c" * 40},
])
def test_a_partial_release_selection_cannot_stand_in_for_a_pairing(builder, bootstrap, selected):
    module, environment, config, *_ = builder
    for key, value in selected.items():
        environment["PRODUCT_BACKEND_RELEASE_" + key.upper()] = value
    with pytest.raises(ValueError, match="incomplete or malformed"):
        bootstrap.preflight(config, base_url=config["base_url"], environment=environment)


@pytest.mark.parametrize("release", [
    {"run_id": 11, "attempt": 1},
    {"run_id": 11, "attempt": 1, "workflow_revision": "c" * 40, "extra": 1},
    {"run_id": "", "attempt": 1, "workflow_revision": "c" * 40},
    {"run_id": 11, "attempt": True, "workflow_revision": "c" * 40},
    {"run_id": 11, "attempt": 1, "workflow_revision": "C" * 40},
])
def test_a_malformed_protected_release_identity_is_refused(builder, bootstrap, release):
    module, environment, config, *_ = builder
    config["backend_release"] = release
    with pytest.raises(ValueError, match="paired API release"):
        bootstrap.preflight(config, base_url=config["base_url"], environment=environment)


def test_configuration_preflight_runs_before_any_claim_or_product_mutation(builder):
    module, environment, config, _, _, api, s3, calls = builder
    config["credentials"]["nonmember"].pop("password")
    with pytest.raises(ValueError, match="credentials are unusable for peer: nonmember"):
        module.build_provider(environment, config["base_url"], api, {"access_token": "control"})
    assert s3.writes == [] and api.events == [] and not api.products
    assert not [event for event in calls if event[0] == "assume"]


@pytest.mark.parametrize("mode,taken", [
    ({}, True),
    ({"recovery_run_id": "product-41-1"}, False),
    ({"scope_editor_enabled": True}, False),
    ({"onboarding_enabled": True}, False),
])
def test_only_a_fresh_b02_run_takes_the_configuration_preflight(builder, monkeypatch, mode, taken):
    from contextlib import suppress

    module, environment, config, _, _, api, s3, calls = builder
    seen = []
    monkeypatch.setattr(module, "preflight", lambda config, **kwargs: seen.append(kwargs))
    # Recovery exists to restore a temporarily revoked owner, and the B03/V11
    # inventories keep their own fixture contracts, so only the preflight
    # selection is asserted here - not those contracts' own outcomes.
    with suppress(Exception):
        module.build_provider(environment, config["base_url"], api, {"access_token": "control"}, **mode)
    assert bool(seen) is taken
    if taken:
        assert seen[0]["base_url"] == config["base_url"]


def test_b03_preflight_names_all_missing_fields_before_any_claim(builder):
    module, environment, config, _, _, api, s3, calls = builder
    with pytest.raises(ValueError) as error:
        module.build_provider(environment, config["base_url"], api,
                              {"access_token": "control"}, scope_editor_enabled=True)
    for field in ("scope_editor", "scope_inputs", "credentials.editor"):
        assert field in str(error.value)
    assert s3.writes == [] and api.events == [] and not api.products
    assert not [event for event in calls if event[0] == "assume"]


def test_b03_preflight_rejects_partial_editor_credentials_before_claim(builder):
    module, environment, config, _, _, api, s3, calls = builder
    config["scope_editor"] = {"actor_id": "other-editor", "subject": "other-subject", "team_id": "team"}
    config["scope_inputs"] = [
        {"selection": {"kind": "repository", "provider": "github",
                       "connection_ref": {"kind": "github_installation", "id": "11"},
                       "namespace": "example-org", "native_id": str(index + 10)},
         "record_revision": "b" * 40, "release_target_date": f"202{index + 7}-12-01"}
        for index in range(2)
    ]
    config["credentials"]["editor"] = {"username": "other-editor"}
    with pytest.raises(ValueError, match="credentials.editor.password"):
        module.build_provider(environment, config["base_url"], api,
                              {"access_token": "control"}, scope_editor_enabled=True)
    assert s3.writes == [] and api.events == [] and not api.products
    assert not [event for event in calls if event[0] == "assume"]


def test_b03_preflight_accepts_two_repositories_on_one_installation(builder, bootstrap):
    module, environment, config, _, _, api, s3, calls = builder
    config["scope_editor"] = {"actor_id": "other-editor", "subject": "other-subject", "team_id": "team"}
    config["credentials"]["editor"] = dict(config["credentials"]["owner"], username="other-editor")
    config["scope_inputs"] = [
        {"selection": {"kind": "repository", "provider": "github",
                       "connection_ref": {"kind": "github_installation", "id": "11"},
                       "namespace": "example-org", "native_id": str(index + 10)},
         "record_revision": "b" * 40, "release_target_date": f"202{index + 7}-12-01"}
        for index in range(2)
    ]
    original = deepcopy(config)
    assert bootstrap.preflight_b03(config, base_url=config["base_url"], environment=environment)["configuration"] == "complete"
    assert config == original and s3.writes == [] and api.events == [] and not api.products


def test_b03_recovery_uses_pinned_editor_without_fresh_source_inputs(builder, monkeypatch):
    module, environment, config, _, _, api, s3, calls = builder
    config["scope_editor"] = {"actor_id": "editor", "subject": "editor-sub", "team_id": "editor-team"}
    config["credentials"]["editor"] = dict(config["credentials"]["owner"], username="editor")
    original = deepcopy(config)
    monkeypatch.setattr(module, "BrowserFixtureProvider", lambda *args, **kwargs: SimpleNamespace(
        binding=kwargs["state"].binding, recover=kwargs["recover"],
    ))
    resumed = module.build_provider(environment, config["base_url"], api, {"access_token": "control"},
                                    scope_editor_enabled=True, recovery_run_id="product-42-1")
    assert resumed.recover and resumed.scope_inputs is None
    assert resumed.binding["editor_id"] == "editor" and config == original
    assert not s3.writes and not api.events and not api.products


@pytest.mark.parametrize("recovery", [False, True])
def test_shared_onboarding_inherits_connected_fixture_membership(builder, monkeypatch, recovery):
    module, environment, config, _, backend, api, s3, _ = builder
    backend["product_onboarding_enabled"] = True
    config.update(scope_fixture_mode="connected_scope", scope_editor={
        "actor_id": "editor", "subject": "editor-sub", "team_id": "owned-team"})
    config["credentials"]["owner"]["username"] = "eval-regression-owner"
    config["credentials"]["editor"] = dict(config["credentials"]["owner"], username="eval-regression-editor")
    if not recovery:
        config["scope_inputs"] = [
            {"selection": {"kind": "repository", "provider": "github",
                           "connection_ref": {"kind": "github_installation", "id": "11"},
                           "namespace": "example-org", "native_id": str(index + 10)},
             "record_revision": "b" * 40, "release_target_date": f"202{index + 7}-12-01"}
            for index in range(2)
        ]
    original = deepcopy(config)
    monkeypatch.setattr(module, "BrowserFixtureProvider", lambda *args, **kwargs: SimpleNamespace(
        binding=kwargs["state"].binding, recovering=kwargs["recover"],
    ))
    selected = module.build_provider(
        environment, config["base_url"], api, {"access_token": "control"},
        onboarding_enabled=True, recovery_run_id="product-42-1" if recovery else None,
    )
    assert selected.binding["scope_fixture_mode"] == "connected_scope"
    assert selected.binding["editor_id"] == "editor"
    assert selected.binding["editor_team_id"] == "owned-team"
    assert selected.recovering is recovery
    assert selected.scope_inputs == (None if recovery else config["scope_inputs"])
    assert config == original and not s3.writes and not api.products


def test_shared_connected_fixture_rejects_ordinary_login_before_claim(builder):
    module, environment, config, _, backend, api, s3, calls = builder
    backend["product_onboarding_enabled"] = True
    config.update(scope_fixture_mode="connected_scope", scope_editor={
        "actor_id": "editor", "subject": "editor-sub", "team_id": "owned-team"})
    config["credentials"]["editor"] = dict(config["credentials"]["owner"], username="eval-regression-editor")
    config["scope_inputs"] = [
        {"selection": {"kind": "repository", "provider": "github",
                       "connection_ref": {"kind": "github_installation", "id": "11"},
                       "namespace": "example-org", "native_id": str(index + 10)},
         "record_revision": "b" * 40, "release_target_date": f"202{index + 7}-12-01"}
        for index in range(2)
    ]
    with pytest.raises(ValueError, match="dedicated ordinary fixture logins"):
        module.build_provider(environment, config["base_url"], api, {"access_token": "control"},
                              onboarding_enabled=True)
    assert not s3.writes and not api.events and not api.products
    assert not [event for event in calls if event[0] == "assume"]


def test_a_configuration_missing_its_origin_is_refused_as_configuration(builder):
    # The origin comparison reads the documented key defensively: a published
    # configuration that omits base_url entirely is an unusable fixture, and it
    # must be reported as one before any claim or mutation, not surface as a
    # KeyError from the binding step.
    module, environment, config, _, _, api, s3, calls = builder
    base_url = config.pop("base_url")
    with pytest.raises(ValueError, match="another origin or run"):
        module.build_provider(environment, base_url, api, {"access_token": "control"})
    assert s3.writes == [] and api.events == [] and not api.products
    assert not [event for event in calls if event[0] == "assume"]


def test_a_pairing_mismatch_names_the_differing_field_and_both_revisions():
    # Issue #8246: a maintained backend_release naming an older API release raised one opaque
    # message per browser case, so the actual mismatch could only be established by reading
    # protected secret metadata out of band. The pairing stays exact; the refusal now names the
    # field and both revisions, which is what the fixture owner has to maintain.
    expected = {"source_revision": "a" * 40, "component": "gateway-backend", "run_id": 11,
                "run_attempt": 1, "product_profile_enabled": True}
    artifact = SimpleNamespace(source_revision="b" * 40, component="gateway-backend", run_id=11,
                               run_attempt=1, product_profile_enabled=True)
    differences = pairing_differences(artifact, expected)
    assert len(differences) == 1
    assert "source_revision" in differences[0]
    assert "b" * 40 in differences[0] and "a" * 40 in differences[0]


def test_a_matching_pairing_reports_no_difference():
    expected = {"source_revision": "a" * 40, "run_id": 11, "run_attempt": 1,
                "product_profile_enabled": True, "product_onboarding_enabled": False}
    artifact = SimpleNamespace(**expected)
    assert pairing_differences(artifact, expected) == []


def test_an_unselected_release_flag_is_named_without_echoing_private_identifiers():
    expected = {"product_profile_enabled": True, "account_id": "111122223333", "repository_id": 7}
    artifact = SimpleNamespace(product_profile_enabled=False, account_id="444455556666", repository_id=8)
    reported = "; ".join(pairing_differences(artifact, expected))
    assert "product_profile_enabled=False" in reported and "requires True" in reported
    assert "account_id differs" in reported and "repository_id differs" in reported
    assert "111122223333" not in reported and "444455556666" not in reported


@pytest.mark.parametrize("fault", [None, "owner_login", "editor_login", "unsupported_mode"])
def test_b03_connected_scope_preflight_rejects_bad_mode_or_nonfixture_logins(builder, bootstrap, fault):
    _, environment, config, _, _, api, s3, _ = builder
    config["scope_fixture_mode"] = "connected_scope"
    config["scope_editor"] = {"actor_id": "other-editor", "subject": "other-subject", "team_id": "team"}
    config["credentials"]["editor"] = dict(config["credentials"]["owner"], username="eval-regression-editor")
    config["credentials"]["owner"]["username"] = "eval-regression-owner"
    config["scope_inputs"] = [
        {"selection": {"kind": "repository", "provider": "github",
                       "connection_ref": {"kind": "github_installation", "id": "11"},
                       "namespace": "example-org", "native_id": str(index + 10)},
         "record_revision": "b" * 40, "release_target_date": f"202{index + 7}-12-01"}
        for index in range(2)
    ]
    if fault == "owner_login":
        config["credentials"]["owner"]["username"] = "shared-owner"
    elif fault == "editor_login":
        config["credentials"]["editor"]["username"] = "shared-editor"
    elif fault == "unsupported_mode":
        config["scope_fixture_mode"] = "unknown"
    if fault is None:
        assert bootstrap.preflight_b03(config, base_url=config["base_url"], environment=environment)["configuration"] == "complete"
    else:
        with pytest.raises(ValueError, match="supported scope fixture mode|dedicated ordinary fixture logins"):
            bootstrap.preflight_b03(config, base_url=config["base_url"], environment=environment)
    assert s3.writes == [] and api.events == [] and not api.products


def test_scope_producer_isolation_uses_real_progress_rules(tmp_path):
    # Issue #8250: two distinct repositories and plan dates legitimately complete
    # the same share of their selected checks, so the scope producer's isolation
    # proof has to rest on a real applicability difference computed by the
    # deployed profile rules, not on a synthetic progress payload that carries
    # the Project ID.
    from src.product_onboarding.progress import profile_progress
    from src.products.models import Product, Project
    from tests.e2e.new_ui.product_scope_fixture import prepare_scope_fixture
    from tests.e2e.product_onboarding.test_product_scope_producer import ProductAPI, Response as ScopeResponse, setup

    class RealProgressAPI(ProductAPI):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            if response.status != 200:
                return response
            value = response.json()
            product = Product(tenant_id=value["tenant_id"], name="Owned Product", purpose="Verify scope isolation",
                              intended_users=["Maintainers"], owner_user_id="example-owner", starting_point="idea")
            project = Project(tenant_id=value["tenant_id"], id=value["project_id"],
                              configuration=value["configuration"], provenance=[])
            value["progress"] = profile_progress(product, project)
            return ScopeResponse(value)

    state, _, owner, product, inputs, path = setup.__wrapped__(tmp_path)
    api = RealProgressAPI()
    fixture = prepare_scope_fixture(state, api, "https://example.invalid", owner, product, inputs, path)
    scopes = fixture["scopes"]
    checks = [{check["id"]: check["status"] for check in scope["progress"]["checks"]} for scope in scopes]
    assert [check["financials"] for check in checks] == ["missing", "later"]
    assert [scope["progress"]["percentage"] for scope in scopes] == [50, 50]
    assert scopes[0]["progress"] != scopes[1]["progress"]
    assert "applicability" not in scopes[0]["configuration"]
    assert scopes[1]["configuration"]["applicability"] == [
        {"check_id": "financials", "state": "later", "reason": "Review this scope's financial plan later."}
    ]
    assert prepare_scope_fixture(state, api, "https://example.invalid", owner, product, inputs, path) == fixture
    assert api.mutations == 5


def test_suite_namespaces_preserve_attempt_identity_and_serialize_shared_membership():
    s3 = S3()
    scope = BrowserRunState(s3, "private-test-state", "test-key", BINDING,
                            namespace="issue-8250")
    resume = BrowserRunState(s3, "private-test-state", "test-key", BINDING,
                             namespace="issue-8252")
    legacy = state(s3)
    assert scope.key != resume.key != legacy.key
    assert scope.lock_key == resume.lock_key == legacy.lock_key
    assert scope.binding == resume.binding == BINDING

    scope.claim()
    with pytest.raises(ValueError, match="recover the active browser run"):
        resume.claim()
    with pytest.raises(ValueError, match="original suite namespace"):
        resume.claim(recover=True)
    with pytest.raises(ValueError, match="original suite namespace"):
        legacy.claim(recover=True)
    scope.release()
    resume.claim()
    assert scope.key in s3.objects and resume.key in s3.objects
    resume.release()

    historical = BrowserRunState(s3, "private-test-state", "test-key", BINDING,
                                 namespace="issue-8250")
    assert historical.document == scope.document
    with pytest.raises(ValueError, match="existing browser run"):
        historical.claim()
    historical.claim(recover=True)
    historical.release()


def test_suite_recovery_refuses_a_wrong_namespace_after_lock_before_document():
    s3 = S3()
    owned = BrowserRunState(s3, "private-test-state", "test-key", BINDING,
                            namespace="issue-8250")
    s3.put_object(Bucket=owned.bucket, Key=owned.lock_key,
                  Body=json.dumps({"run_id": BINDING["run_id"],
                                   "namespace": "issue-8250", "holder": "aborted"}).encode(),
                  ServerSideEncryption="aws:kms", SSEKMSKeyId="test-key")
    wrong = BrowserRunState(s3, "private-test-state", "test-key", BINDING,
                            namespace="issue-8252")
    with pytest.raises(ValueError, match="original suite namespace"):
        wrong.claim(recover=True)
    assert wrong.key not in s3.objects
    owned.claim(recover=True)
    owned.release()


@pytest.mark.parametrize("namespace", ["", "../foreign", "ISSUE-8250", "0issue", "x" * 41, True])
def test_suite_namespace_rejects_unowned_or_unbounded_state_keys(namespace):
    s3 = S3()
    with pytest.raises(ValueError, match="bounded suite identifier"):
        BrowserRunState(s3, "private-test-state", "test-key", BINDING,
                        namespace=namespace)
    assert not s3.objects


@pytest.mark.parametrize("namespace", [None, "issue-8252"])
def test_interrupted_suite_recovery_retains_verified_cleanup_and_original_namespace(namespace):
    api, s3 = API(), S3()
    selected = provider(api, s3, namespace=namespace)
    selected.state.save({**selected.state.document, "cases": {
        "create": {"before": {}, "document": None, "manifest": None}}})
    create_intent(selected).reconcile(api, ORIGIN)
    selected.membership.revoke(selected.state)
    if namespace is not None:
        with pytest.raises(ValueError, match="original suite namespace"):
            provider(api, s3, recover=True).recover()
        assert api.status == "revoked"
    recovered = provider(api, s3, recover=True, namespace=namespace)
    receipt = recovered.recover()
    assert receipt["run_id"] == BINDING["run_id"]
    assert receipt["suite_id"] == namespace
    assert receipt["cases"]["create"]["owned_absent"] is True
    assert receipt["cases"]["create"]["shared_unchanged"] is True
    assert recovered.state.document["cases"]["create"]["cleanup"] == receipt["cases"]["create"]
    assert recovered.state.read(recovered.state.lock_key)[0] == {"run_id": None}
    assert api.status == "active"
    assert next(iter(api.products.values()))["retired"] is True


@pytest.mark.parametrize("namespace,receipt_namespace,accepted", [
    ("issue-8252", "issue-8252", True), ("", None, True),
    ("issue-8252", "issue-8250", False), ("issue-8252", None, False),
    ("", "issue-8252", False), ("", "missing", False),
])
def test_recovery_workflow_checks_original_suite_and_receipt(tmp_path, namespace, receipt_namespace, accepted):
    import yaml

    workflow = yaml.safe_load((Path(__file__).resolve().parents[2] /
                               ".github/workflows/product-browser-recovery.yml").read_text())
    inputs = workflow.get("on", workflow.get(True))["workflow_dispatch"]["inputs"]
    assert inputs["suite_id"]["default"] == "legacy"
    assert set(inputs["suite_id"]["options"]) == {
        "legacy", "issue-8250", "issue-8252", "issue-8258", "issue-8268"}
    job = workflow["jobs"]["recover"]
    assert job["env"]["PRODUCT_SUITE_ID"] == "${{ inputs.suite_id != 'legacy' && inputs.suite_id || '' }}"
    assert workflow["concurrency"]["group"] == "cli-live-eval-dev"
    script = next(step["run"] for step in job["steps"]
                  if step.get("name") == "Reconcile original ownership and retain cleanup receipt")
    receipt_check = script.split("python - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
    run_id = BINDING["run_id"]
    (tmp_path / "test-results").mkdir()
    receipt = {"run_id": run_id, "cases": {"create": {"owned_absent": True}}}
    if receipt_namespace != "missing":
        receipt["suite_id"] = receipt_namespace
    (tmp_path / "test-results" / f"product-recovery-{run_id}.json").write_text(json.dumps(receipt))
    result = subprocess.run([sys.executable, "-c", receipt_check], cwd=tmp_path,
                            env={**os.environ, "PRODUCT_RECOVERY_RUN_ID": run_id, "PRODUCT_SUITE_ID": namespace},
                            capture_output=True, text=True)
    assert (result.returncode == 0) is accepted, result.stderr


def test_independent_suites_cannot_concurrently_claim_shared_membership():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    class RacingS3(S3):
        def __init__(self):
            super().__init__()
            self.barrier = Barrier(2)
            self.lock_key = None

        def get_object(self, *, Bucket, Key):
            if Key == self.lock_key:
                self.barrier.wait(timeout=5)
                self.lock_key = None
            return super().get_object(Bucket=Bucket, Key=Key)

    s3 = RacingS3()
    first = BrowserRunState(s3, "private-test-state", "test-key", BINDING, namespace="issue-8250")
    second = BrowserRunState(s3, "private-test-state", "test-key",
                             {**BINDING, "run_id": "product-43-1"}, namespace="issue-8252")
    s3.lock_key = first.lock_key
    assert first.lock_key == second.lock_key

    def attempt_claim(selected):
        try:
            selected.claim()
            return True
        except (ClientError, ValueError):
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt_claim, (first, second)))
    assert outcomes.count(True) == 1
    winner, loser = (first, second) if outcomes[0] else (second, first)
    assert json.loads(s3.objects[winner.lock_key][0])["run_id"] == winner.binding["run_id"]
    assert loser.key not in s3.objects
    winner.release()
    loser.claim()
    loser.release()
