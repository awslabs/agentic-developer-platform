"""Offline interrupted-write fixtures; no deployed cleanup is claimed."""

from __future__ import annotations

import io
import json
from contextlib import contextmanager
from copy import deepcopy

import pytest

from tests.e2e.cli_uplift.ports import PortError
from tests.e2e.product_onboarding import live
from tests.e2e.product_onboarding.product_recovery import (
    InterruptedWrite,
    ProductStateStore,
)
from tests.e2e.product_onboarding.product_recovery import (
    api_recovery_cleanup as run_recovery,
)

SOURCE = "a" * 40
RUN_ID = "test-run-0001"


class Store:
    def __init__(self):
        self.objects = {}
        self.fail_save = False

    def load(self, run_id):
        return deepcopy(self.objects.get(run_id, (None, None)))

    def save(self, run_id, document, manifest=None):
        if self.fail_save:
            raise RuntimeError("durable store unavailable")
        self.objects[run_id] = deepcopy((document, manifest))


class Reply:
    def __init__(self, status, data):
        self.status_code = status
        self.data = deepcopy(data)
        self.headers = {"Profile-Revision": str(data["revision"])} if "revision" in data else {}

    def json(self):
        return self.data


class API:
    def __init__(self):
        self.shared = {
            "schema_version": "1.0", "tenant_id": "tenant-shared",
            "product_id": "product-shared", "project_id": "project-shared", "revision": 1,
            "product": {"name": "shared-product"},
        }
        self.products = {}
        self.keys = {}
        self.requests = []
        self.response_lost_once = False
        self.duplicate_effect = False
        self.response_lost_before_commit = False

    @contextmanager
    def client(self, token):
        assert token in {"owner-token", "shared-token"}
        yield Session(self, token)

    def cleanup(self, receipt):
        assert receipt["run_id"] == RUN_ID and receipt["tenant_id"] == "tenant-owned"
        for product_id in receipt["owned_product_ids"]:
            self.products.pop(product_id, None)


class Session:
    def __init__(self, api, token):
        self.api = api
        self.token = token

    def get(self, path):
        if self.token == "shared-token":
            assert path == "/v1/products/product-shared/projects/project-shared"
            return Reply(200, self.api.shared)
        if path == "/v1/products":
            return Reply(200, {"items": [
                {"id": product_id, "name": product["product"]["name"]}
                for product_id, product in self.api.products.items()
            ]})
        if path.endswith("/changes"):
            return Reply(200, {"items": [{"event_id": "change-1", "revision": 1,
                                           "product_id": path.split("/")[3]}]})
        product_id = path.split("/")[3]
        product = self.api.products.get(product_id)
        return Reply(200, product) if product else Reply(404, {"detail": {"code": "not_found"}})

    def post(self, path, *, json, headers):
        assert path == "/v1/products" and self.token == "owner-token"
        key = headers["Idempotency-Key"]
        if self.api.response_lost_before_commit:
            self.api.response_lost_before_commit = False
            raise ConnectionError("response interrupted before commit")
        self.api.requests.append(key)
        if key not in self.api.keys or self.api.duplicate_effect:
            product_id = f"owned-{len(self.api.products) + 1}"
            profile = {
                "schema_version": "1.0", "tenant_id": "tenant-owned", "product_id": product_id,
                "project_id": f"project-{product_id}", "revision": 1,
                "product": {"name": json["name"]},
            }
            self.api.products[product_id] = profile
            self.api.keys[key] = product_id
        if self.api.response_lost_once:
            self.api.response_lost_once = False
            raise ConnectionError("response interrupted after commit")
        return Reply(201, self.api.products[self.api.keys[key]])


def run(api, store, cleanup=None, **overrides):
    options = {
        "tenant_id": "tenant-owned", "other_tenant_id": "tenant-shared",
        "owner_token": "owner-token", "foreign_token": "shared-token",
        "shared_product_id": "product-shared", "shared_project_id": "project-shared",
        "run_id": RUN_ID, "source_revision": SOURCE,
    }
    options.update(overrides)
    return run_recovery(api.client, store, api.cleanup if cleanup is None else cleanup, **options)


def test_interrupted_write_reuses_persisted_key_and_removes_only_owned_product():
    api, store = API(), Store()
    shared_baseline = deepcopy(api.shared)
    api.response_lost_once = True
    with pytest.raises(InterruptedWrite, match="original run ID"):
        run(api, store)
    document, manifest = store.load(RUN_ID)
    assert document["receipt"] is None and manifest["product_id"] is None
    assert [attempt["state"] for attempt in document["attempts"]] == ["write_started", "response_unknown"]
    assert "owner-token" not in json.dumps(document) and "shared-token" not in json.dumps(document)
    assert len(api.products) == 1

    result = run(api, store)
    assert api.requests == [document["idempotency_key"], document["idempotency_key"]]
    assert len(api.products) == 0 and api.shared == shared_baseline
    assert result["revision"] == 1 and result["cleanup_verified"] is True
    assert result["attempts"][-1]["state"] == "cleanup_verified"
    assert store.load(RUN_ID)[1]["product_id"] == result["product_id"]
    with pytest.raises(ValueError, match="already cleaned"):
        run(api, store)


def test_response_lost_before_commit_resumes_with_original_key():
    api, store = API(), Store()
    api.response_lost_before_commit = True
    with pytest.raises(InterruptedWrite):
        run(api, store)
    document, _ = store.load(RUN_ID)
    assert api.products == {}
    assert document["attempts"][-1]["state"] == "response_unknown"
    result = run(api, store)
    assert api.requests == [document["idempotency_key"]]
    assert result["cleanup_verified"] is True and api.products == {}


def test_existing_encrypted_store_recovers_on_a_new_runner():
    class StubAws:
        def __init__(self):
            self.objects = {}

        def call(self, service, operation, **kwargs):
            assert service == "s3" and kwargs["Bucket"] == "private-test-bucket"
            if operation == "get_object":
                if kwargs["Key"] not in self.objects:
                    raise PortError("s3.get_object failed: NoSuchKey")
                return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}
            assert operation == "put_object"
            assert kwargs["ServerSideEncryption"] == "aws:kms"
            assert kwargs["ACL"] == "bucket-owner-full-control"
            self.objects[kwargs["Key"]] = kwargs["Body"]
            return {}

    aws, api = StubAws(), API()
    api.response_lost_once = True
    with pytest.raises(InterruptedWrite):
        run(api, ProductStateStore(aws, "private-test-bucket", kms_key_id="fake-key"))
    assert len(aws.objects) == 2
    assert all(key.startswith("cli-uplift-eval/state/product-v01-test-run-0001/") for key in aws.objects)
    resumed = run(api, ProductStateStore(aws, "private-test-bucket", kms_key_id="fake-key"))
    assert resumed["cleanup_verified"] is True
    assert api.products == {}
    assert "owner-token" not in str(aws.objects)


@pytest.mark.parametrize("failure", ["AccessDenied", "garbled", "empty"])
def test_unavailable_recovery_state_refuses_write(failure):
    class StubAws:
        def call(self, service, operation, **kwargs):
            assert service == "s3" and operation == "get_object"
            if failure == "AccessDenied":
                raise PortError("s3.get_object failed: AccessDenied")
            return {"Body": b"{invalid" if failure == "garbled" else b""}

    api = API()
    store = ProductStateStore(StubAws(), "private-test-bucket")
    with pytest.raises(RuntimeError, match="state read is unavailable|state is invalid|state is empty"):
        run(api, store)
    assert api.requests == []


def test_missing_cleanup_or_store_refuses_to_start_write():
    api, store = API(), Store()
    with pytest.raises(TypeError, match="cleanup"):
        run_recovery(api.client, store, None, tenant_id="tenant-owned",
                             other_tenant_id="tenant-shared", owner_token="owner-token",
                             foreign_token="shared-token", shared_product_id="product-shared",
                             shared_project_id="project-shared", run_id=RUN_ID, source_revision=SOURCE)
    store.fail_save = True
    with pytest.raises(RuntimeError, match="store unavailable"):
        run(api, store)
    assert api.requests == []


def test_stale_recovery_state_refuses_to_replay():
    api, store = API(), Store()
    api.response_lost_once = True
    with pytest.raises(InterruptedWrite):
        run(api, store)
    with pytest.raises(ValueError, match="differs"):
        run(api, store, source_revision="b" * 40)
    assert len(api.requests) == 1


def test_missing_ownership_manifest_refuses_replay():
    api, store = API(), Store()
    api.response_lost_once = True
    with pytest.raises(InterruptedWrite):
        run(api, store)
    document, _ = store.load(RUN_ID)
    store.objects[RUN_ID] = (document, None)
    with pytest.raises(ValueError, match="manifest is missing"):
        run(api, store)
    assert len(api.requests) == 1


@pytest.mark.parametrize("failure", ["no_delete", "shared_changed", "delete_error"])
def test_cleanup_must_be_independently_verified(failure):
    api, store = API(), Store()

    def cleanup(receipt):
        if failure == "delete_error":
            raise PermissionError("owned cleanup denied")
        if failure == "shared_changed":
            api.cleanup(receipt)
            api.shared["product"]["name"] = "unapproved change"

    with pytest.raises((AssertionError, PermissionError)):
        run(api, store, cleanup=cleanup)
    document, _ = store.load(RUN_ID)
    assert document["attempts"][-1]["state"] == "cleanup_failed"
    assert document.get("cleanup_verified") is not True


def test_duplicate_effect_is_not_mistaken_for_one_product():
    api, store = API(), Store()
    api.response_lost_once = api.duplicate_effect = True
    with pytest.raises(InterruptedWrite):
        run(api, store)
    with pytest.raises(AssertionError):
        run(api, store)
    assert len(api.products) == 1
    assert "owned-1" in api.products
    assert store.load(RUN_ID)[0]["attempts"][-1]["state"] == "cleanup_failed"
    document, _ = store.load(RUN_ID)
    assert document.get("cleanup_verified") is not True
    assert any(attempt["state"] == "mutation_count_mismatch" for attempt in document["attempts"])
    assert next(attempt["owned_product_ids"] for attempt in document["attempts"]
                if attempt["state"] == "cleanup_started") == ["owned-2"]


def test_selected_recovery_resumes_from_durable_manifest_after_lost_response():
    api, store = API(), Store()
    api.response_lost_once = True
    fixtures = {
        "tenant_id": "tenant-owned", "other_tenant_id": "tenant-shared",
        "owner_token": "owner-token", "foreign_token": "shared-token",
        "shared_product_id": "product-shared", "shared_project_id": "project-shared",
        "run_id": RUN_ID,
    }
    snapshots = []

    def observe():
        snapshots.append(SOURCE)
        return SOURCE

    with pytest.raises(InterruptedWrite):
        live.run("api_recovery_cleanup", SOURCE, fixtures, observe, api.client, api.cleanup,
                 state_store=store)
    document, manifest = store.load(RUN_ID)
    assert document["attempts"][-1]["state"] == "response_unknown"
    assert manifest["run_id"] == RUN_ID
    assert len(api.products) == 1
    result = live.run("api_recovery_cleanup", SOURCE, fixtures, observe, api.client, api.cleanup,
                      state_store=store)["api_recovery_cleanup"]
    assert api.requests == [document["idempotency_key"], document["idempotency_key"]]
    assert result["cleanup_verified"] is True and api.products == {}
    assert len(snapshots) == 3
    assert store.load(RUN_ID)[1]["product_id"] == result["product_id"]
