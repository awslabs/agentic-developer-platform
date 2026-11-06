"""Offline contract fixtures; passing here does not establish deployed acceptance."""

from contextlib import contextmanager
from copy import deepcopy

import pytest

from tests.e2e.product_onboarding.product_profile_api import (
    api_product_lifecycle as driver_lifecycle,
)
from tests.e2e.product_onboarding.product_recovery import InterruptedWrite


class Store:
    def __init__(self):
        self.objects = {}
        self.fail_save = False

    def load(self, run_id):
        return deepcopy(self.objects.get(run_id, (None, None)))

    def save(self, run_id, document, manifest=None):
        if self.fail_save:
            raise RuntimeError("protected state unavailable")
        self.objects[run_id] = deepcopy((document, manifest))


def run_lifecycle(client_factory, *, state_store=None, **kwargs):
    return driver_lifecycle(
        client_factory,
        state_store=state_store or Store(),
        actor_id="owner-id",
        source_revision="a" * 40,
        **kwargs,
    )


class Response:
    def __init__(self, status, profile):
        self.status_code = status
        self.headers = {"Profile-Revision": str(profile["revision"])}
        self.profile = deepcopy(profile)

    def json(self):
        return self.profile


class FixtureAPI:
    def __init__(self, tenant_id="owned-tenant", product_id="owned-product"):
        self.tenant_id = tenant_id
        self.product_id = product_id
        self.clients = self.posts = self.edits = self.reads = 0
        self.profile = None
        self.response_lost_once = False

    @contextmanager
    def client(self, token):
        assert token == "owner-token"
        self.clients += 1
        yield self

    def post(self, path, *, json, headers):
        assert path == "/v1/products" and json["project"] == {"mode": "create"}
        assert json["starting_point"] == "idea" and json["intended_users"] == [
            "Maintainers"
        ]
        self.posts += 1
        if self.profile is None:
            self.create_key = headers["Idempotency-Key"]
            self.profile = {
                "schema_version": "1.0",
                "tenant_id": self.tenant_id,
                "product_id": self.product_id,
                "project_id": "owned-project",
                "revision": 1,
                "product": {
                    "name": json["name"],
                    "purpose": json["purpose"],
                    "intended_users": json["intended_users"],
                    "starting_point": "idea",
                },
                "project": {
                    "id": "owned-project",
                    "tenant_id": self.tenant_id,
                    "name": json["name"],
                },
                "configuration": {},
                "provenance": [],
            }
        else:
            assert headers["Idempotency-Key"] == self.create_key
        if self.response_lost_once:
            self.response_lost_once = False
            raise ConnectionError("create committed but response was lost")
        return Response(201, self.profile)

    def patch(self, path, *, json, headers):
        assert path == f"/v1/products/{self.product_id}/projects/owned-project"
        assert headers["If-Profile-Revision"] == "1"
        assert headers["Idempotency-Key"].startswith("product-edit-")
        self.edits += 1
        self.profile["revision"] = 2
        self.profile["product"].update(json)
        return Response(200, self.profile)

    def get(self, path):
        assert path == f"/v1/products/{self.product_id}/projects/owned-project"
        assert self.clients >= 2
        self.reads += 1
        return Response(200, self.profile)


def test_api_product_lifecycle_fixture():
    fixture = FixtureAPI()
    result = run_lifecycle(
        fixture.client,
        tenant_id="owned-tenant",
        other_tenant_id="other-tenant",
        owner_token="owner-token",
        run_id="test-run-0001",
    )
    assert {key: result[key] for key in ("product_id", "project_id", "revision")} == {
        "product_id": "owned-product",
        "project_id": "owned-project",
        "revision": 2,
    }
    assert result["edit_key"].startswith("product-edit-")
    assert (fixture.clients, fixture.posts, fixture.edits, fixture.reads) == (
        2,
        2,
        1,
        1,
    )


@pytest.mark.parametrize(
    "tenant_id,other_tenant_id",
    [
        ("owned-tenant", "owned-tenant"),
        ("", "other-tenant"),
    ],
)
def test_api_product_lifecycle_rejects_invalid_fixtures(tenant_id, other_tenant_id):
    fixture = FixtureAPI()
    with pytest.raises(AssertionError):
        run_lifecycle(
            fixture.client,
            tenant_id=tenant_id,
            other_tenant_id=other_tenant_id,
            owner_token="owner-token",
            run_id="test-run-0001",
        )
    assert fixture.clients == 0


@pytest.mark.parametrize(
    "tenant_id,expected_product_id",
    [
        ("other-tenant", None),
        ("owned-tenant", "deliberately-wrong-product"),
    ],
)
def test_api_product_lifecycle_refuses_wrong_readback(tenant_id, expected_product_id):
    fixture = FixtureAPI(tenant_id=tenant_id)
    with pytest.raises(AssertionError):
        run_lifecycle(
            fixture.client,
            tenant_id="owned-tenant",
            other_tenant_id="other-tenant",
            owner_token="owner-token",
            run_id="test-run-0001",
            expected_product_id=expected_product_id,
        )


def test_lifecycle_persists_create_intent_before_lost_response_and_reuses_key():
    store = Store()
    fixture = FixtureAPI()
    fixture.response_lost_once = True
    arguments = {
        "tenant_id": "owned-tenant",
        "other_tenant_id": "other-tenant",
        "owner_token": "owner-token",
        "run_id": "test-run-0001",
    }
    with pytest.raises(InterruptedWrite):
        run_lifecycle(fixture.client, state_store=store, **arguments)
    document, manifest = store.load("lifecycle-test-run-0001")
    assert document["receipt"] is None
    assert [attempt["state"] for attempt in document["attempts"]] == [
        "create_started",
        "response_unknown",
    ]
    assert manifest["idempotency_key"] == fixture.create_key
    assert (
        manifest["actor_id"] == "owner-id" and manifest["source_revision"] == "a" * 40
    )
    result = run_lifecycle(fixture.client, state_store=store, **arguments)
    assert result["create_key"] == fixture.create_key
    assert fixture.posts == 3 and fixture.edits == 1
    document, manifest = store.load("lifecycle-test-run-0001")
    assert manifest["product_id"] == result["product_id"]
    assert document["attempts"][-1] == {
        "state": "fresh_readback_verified",
        "revision": 2,
    }
    with pytest.raises(ValueError, match="already has an owned create receipt"):
        run_lifecycle(fixture.client, state_store=store, **arguments)
    assert fixture.posts == 3


def test_lifecycle_refuses_unavailable_state_before_any_write():
    fixture, store = FixtureAPI(), Store()
    store.fail_save = True
    with pytest.raises(RuntimeError, match="protected state unavailable"):
        run_lifecycle(
            fixture.client,
            state_store=store,
            tenant_id="owned-tenant",
            other_tenant_id="other-tenant",
            owner_token="owner-token",
            run_id="test-run-0001",
        )
    assert fixture.clients == fixture.posts == 0


def test_lifecycle_refuses_mismatched_existing_intent_without_new_write():
    fixture, store = FixtureAPI(), Store()
    fixture.response_lost_once = True
    arguments = {
        "tenant_id": "owned-tenant",
        "other_tenant_id": "other-tenant",
        "owner_token": "owner-token",
        "run_id": "test-run-0001",
    }
    with pytest.raises(InterruptedWrite):
        run_lifecycle(fixture.client, state_store=store, **arguments)
    document, manifest = store.load("lifecycle-test-run-0001")
    manifest["actor_id"] = "foreign-actor"
    store.save("lifecycle-test-run-0001", document, manifest=manifest)
    with pytest.raises(ValueError, match="intent differs"):
        run_lifecycle(fixture.client, state_store=store, **arguments)
    assert fixture.posts == 1


def test_explicit_product_intent_refuses_key_change_after_lost_response():
    store = Store()
    fixture = FixtureAPI()
    fixture.response_lost_once = True
    arguments = {
        "tenant_id": "owned-tenant",
        "other_tenant_id": "other-tenant",
        "owner_token": "owner-token",
        "run_id": "test-run-0001",
    }
    original = "product-create-" + "b" * 64
    with pytest.raises(InterruptedWrite):
        run_lifecycle(
            fixture.client, state_store=store, idempotency_key=original, **arguments
        )
    assert store.load("lifecycle-test-run-0001")[1]["idempotency_key"] == original
    with pytest.raises(ValueError, match="intent differs"):
        run_lifecycle(
            fixture.client,
            state_store=store,
            idempotency_key="product-create-" + "f" * 64,
            **arguments,
        )
    assert fixture.posts == 1
    result = run_lifecycle(
        fixture.client, state_store=store, idempotency_key=original, **arguments
    )
    assert result["create_key"] == original
    assert fixture.posts == 3
