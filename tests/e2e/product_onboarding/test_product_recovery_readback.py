"""The held Product survives a read-only baseline and exact candidate replay."""

import hashlib
import json
from copy import deepcopy

import pytest

from tests.e2e.cli_uplift import ports
from tests.e2e.product_onboarding import product_recovery_readback as readback


def digest(value):
    return (
        "sha256:"
        + hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    )


@pytest.fixture
def held(monkeypatch):
    run_id = "product-42-1"
    original = {
        "schema_version": "1.0",
        "tenant_id": "owned-tenant",
        "product_id": "owned-product",
        "project_id": "owned-project",
        "revision": 1,
        "product": {
            "name": f"eval-product-{run_id}",
            "purpose": "Coordinate a small release",
        },
    }
    edited = deepcopy(original)
    edited["revision"] = 2
    edited["product"] = {
        "name": f"eval-product-{run_id}-edited",
        "purpose": "Ship a focused update",
    }
    history = [
        {
            "revision": revision,
            "event_id": f"event-{revision}",
            "product_id": "owned-product",
            "tenant_id": "owned-tenant",
        }
        for revision in (1, 2)
    ]
    receipt = {
        "product_id": "owned-product",
        "project_id": "owned-project",
        "revision": 1,
    }
    owned = {
        "run_id": run_id,
        "tenant_id": "owned-tenant",
        "actor_id": "owner-id",
        "product_id": "owned-product",
        "project_id": "owned-project",
        "history_ref": digest(history),
        "idempotency_key": "product-create-original",
        "idempotency_receipt": digest(receipt),
    }
    data = {
        "/v1/products": {"items": [{"id": "owned-product"}]},
        "/v1/products/owned-product/projects/owned-project": edited,
        "/v1/products/owned-product/projects/owned-project/revisions/1": original,
        "/v1/products/owned-product/projects/owned-project/revisions/2": edited,
        "/v1/products/owned-product/projects/owned-project/changes": {"items": history},
    }
    calls = []

    def request(
        cfg, path, *, token, body=None, idempotency_key=None, expected_status=200
    ):
        calls.append((path, body, idempotency_key))
        assert token == "private-token"
        # The configured base URL already carries the external /api prefix, so
        # callers must address the mounted Product routes without repeating it.
        assert path.startswith("/v1/products"), path
        if body is not None:
            assert path == "/v1/products"
            assert idempotency_key == owned["idempotency_key"]
            assert body == readback.create_request(run_id)
            assert expected_status == 201
            return 201, deepcopy(edited)
        return 200, deepcopy(data[path])

    monkeypatch.setattr(
        readback.product_sessions,
        "verify",
        lambda cfg, identity, token: identity
        == {"user_id": "owner-id", "org_id": "owned-tenant"}
        and token == "private-token",
    )
    return owned, data, calls, request


def test_baseline_checks_original_read_only_without_product_admissions(held):
    owned, _, calls, request = held
    result = readback.read_owned(
        {}, owned, "private-token", admissions=False, request=request
    )
    assert result["history_ref"] == owned["history_ref"]
    assert result["idempotency_receipt"] == owned["idempotency_receipt"]
    assert result["replay_verified"] is False
    assert all(body is None for _, body, _ in calls)


def test_baseline_reads_only_mounted_product_routes(held):
    owned, _, calls, request = held
    readback.read_owned({}, owned, "private-token", admissions=True, request=request)
    assert calls, "readback made no Product request"
    assert all(
        path.startswith("/v1/products") and not path.startswith("/v1/products/api")
        for path, _, _ in calls
    )


def test_candidate_replays_only_original_key_without_duplicate_history(held):
    owned, _, calls, request = held
    result = readback.read_owned(
        {}, owned, "private-token", admissions=True, request=request
    )
    assert result["replay_verified"] is True
    assert [key for _, body, key in calls if body is not None] == [
        owned["idempotency_key"]
    ]
    assert calls[-1][0].endswith("/changes")


@pytest.mark.parametrize(
    "change",
    ["history", "malformed_history", "receipt", "revision", "edited", "list", "replay"],
)
def test_missing_or_changed_product_evidence_refuses(held, change):
    owned, data, _, request = held
    if change == "history":
        data["/v1/products/owned-product/projects/owned-project/changes"]["items"][1][
            "event_id"
        ] = "other-event"
    elif change == "malformed_history":
        data["/v1/products/owned-product/projects/owned-project/changes"]["items"][
            1
        ] = None
    elif change == "receipt":
        owned["idempotency_receipt"] = digest({"product_id": "different"})
    elif change == "revision":
        data["/v1/products/owned-product/projects/owned-project/revisions/1"][
            "revision"
        ] = 2
    elif change == "edited":
        data["/v1/products/owned-product/projects/owned-project"]["product"][
            "name"
        ] = "other-name"
    elif change == "list":
        data["/v1/products"]["items"] = []
    else:
        original = request

        def request(*args, **kwargs):
            status, result = original(*args, **kwargs)
            if kwargs.get("body") is not None:
                result["product_id"] = "new-product"
            return status, result

    with pytest.raises(ports.PortError):
        readback.read_owned(
            {}, owned, "private-token", admissions=change == "replay", request=request
        )


def test_readback_refuses_foreign_owner_before_any_product_request(held, monkeypatch):
    owned, _, calls, request = held
    monkeypatch.setattr(readback.product_sessions, "verify", lambda *_: False)
    with pytest.raises(ports.PortError, match="authenticated"):
        readback.read_owned(
            {}, owned, "private-token", admissions=False, request=request
        )
    assert calls == []


def test_readback_refuses_unsafe_product_path_before_any_request(held):
    owned, _, calls, request = held
    owned["product_id"] = "another-product/../../other"
    with pytest.raises(ports.PortError, match="original held ownership"):
        readback.read_owned(
            {}, owned, "private-token", admissions=False, request=request
        )
    assert calls == []


@pytest.mark.parametrize("admissions", [None, "false", 0])
def test_readback_refuses_ambiguous_admissions_before_any_request(held, admissions):
    owned, _, calls, request = held
    with pytest.raises(ports.PortError, match="original held ownership"):
        readback.read_owned(
            {}, owned, "private-token", admissions=admissions, request=request
        )
    assert calls == []
