"""The instance retains one verified Product intent for retries and retirement."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "remote"))
import common
import product_cleanup
from product_state import ProductState
from tests.e2e.product_onboarding.product_profile_api import (
    api_product_lifecycle as driver_lifecycle,
)
from tests.e2e.product_onboarding.product_recovery import InterruptedWrite
from tests.e2e.product_onboarding.test_product_profile_api import FixtureAPI


@pytest.fixture
def private_dir(tmp_path):
    tmp_path.chmod(0o700)
    return tmp_path


@pytest.fixture
def owned():
    manifest = {
        "run_id": "adp-e2e-20261019-000000-abcdef",
        "tenant_id": "tenant-one",
        "actor_id": "actor-one",
        "source_revision": "a" * 40,
        "idempotency_key": "product-create-original",
    }
    return {
        **manifest,
        "request": {"name": "owned"},
        "attempts": [],
        "receipt": None,
    }, manifest


def test_save_reloads_original_intent_and_exposes_cleanup_ownership(private_dir, owned):
    document, manifest = owned
    store = ProductState(private_dir, manifest["run_id"])
    key = f"lifecycle-{manifest['run_id']}"
    assert store.load(key) == (None, None)
    store.save(key, document, manifest=manifest)
    document["attempts"].append({"state": "create_started"})
    store.save(key, document, manifest=manifest)
    persisted = ProductState(private_dir, manifest["run_id"])
    assert persisted.load(key) == (document, manifest)
    document["receipt"] = {
        "product_id": "created-product",
        "project_id": "created-project",
    }
    manifest.update(product_id="created-product", project_id="created-project")
    persisted.save(key, document, manifest=manifest)
    record = json.loads(persisted.path.read_text())
    assert all(record[field] == value for field, value in manifest.items())
    assert record["product_id"] == "created-product"
    assert record["project_id"] == "created-project"
    assert persisted.path.stat().st_mode & 0o777 == 0o600
    assert persisted.load(key) == (document, manifest)


@pytest.mark.parametrize(
    "field", ["run_id", "tenant_id", "actor_id", "source_revision", "idempotency_key"]
)
def test_original_ownership_cannot_change_after_first_save(private_dir, owned, field):
    document, manifest = owned
    store = ProductState(private_dir, manifest["run_id"])
    key = f"lifecycle-{manifest['run_id']}"
    store.save(key, document, manifest=manifest)
    altered_manifest = {**manifest, field: "other-evaluation"}
    altered_document = {**document, field: "other-evaluation"}
    with pytest.raises(common.RemoteError, match="original ownership"):
        store.save(key, altered_document, manifest=altered_manifest)
    assert store.load(key) == (document, manifest)


@pytest.mark.parametrize(
    "corruption",
    [
        "symlink",
        "permissions",
        "invalid_json",
        "other_run",
        "other_actor",
        "other_product",
    ],
)
def test_corrupt_or_foreign_record_is_not_treated_as_new(
    private_dir, owned, corruption
):
    document, manifest = owned
    store = ProductState(private_dir, manifest["run_id"])
    key = f"lifecycle-{manifest['run_id']}"
    store.save(key, document, manifest=manifest)
    if corruption == "symlink":
        destination = private_dir / "different-file"
        store.path.rename(destination)
        store.path.symlink_to(destination)
    elif corruption == "permissions":
        store.path.chmod(0o644)
    elif corruption == "invalid_json":
        store.path.write_text("{")
    elif corruption == "other_product":
        document["receipt"] = {
            "product_id": "created-product",
            "project_id": "created-project",
        }
        manifest.update(product_id="created-product", project_id="created-project")
        store.save(key, document, manifest=manifest)
        record = json.loads(store.path.read_text())
        record["product_id"] = "different-product"
        store.path.write_text(json.dumps(record))
    else:
        record = json.loads(store.path.read_text())
        record["run_id" if corruption == "other_run" else "actor_id"] = (
            "other-evaluation"
        )
        store.path.write_text(json.dumps(record))
    with pytest.raises(common.RemoteError):
        store.load(key)
    with pytest.raises(common.RemoteError):
        store.save(key, document, manifest=manifest)


def test_requires_private_run_directory_and_exact_key(private_dir, owned):
    document, manifest = owned
    private_dir.chmod(0o755)
    with pytest.raises(common.RemoteError, match="private evaluation directory"):
        ProductState(private_dir, manifest["run_id"])
    private_dir.chmod(0o700)
    store = ProductState(private_dir, manifest["run_id"])
    with pytest.raises(common.RemoteError, match="key differs"):
        store.save("lifecycle-other", document, manifest=manifest)
    assert not store.path.exists()


def test_real_driver_resumes_committed_write_using_instance_ownership(private_dir):
    run_id = "adp-e2e-20261019-000000-abcdef"
    fixture = FixtureAPI(tenant_id="owned-tenant")
    fixture.response_lost_once = True
    arguments = {
        "tenant_id": "owned-tenant",
        "other_tenant_id": "other-tenant",
        "owner_token": "owner-token",
        "actor_id": "owner-id",
        "run_id": run_id,
        "source_revision": "a" * 40,
    }
    with pytest.raises(InterruptedWrite):
        driver_lifecycle(
            fixture.client, state_store=ProductState(private_dir, run_id), **arguments
        )
    initial = json.loads((private_dir / "product-ownership.json").read_text())
    assert initial["idempotency_key"] == fixture.create_key
    assert initial["document"]["attempts"][-1]["state"] == "response_unknown"
    assert "product_id" not in initial

    result = driver_lifecycle(
        fixture.client, state_store=ProductState(private_dir, run_id), **arguments
    )
    assert result["create_key"] == fixture.create_key
    assert fixture.posts == 3 and fixture.edits == 1
    document, manifest = ProductState(private_dir, run_id).load(f"lifecycle-{run_id}")
    assert manifest["product_id"] == result["product_id"]
    assert manifest["project_id"] == result["project_id"]
    assert document["attempts"][-1]["state"] == "fresh_readback_verified"


def test_cleanup_caller_accepts_driver_owned_record(private_dir, monkeypatch):
    run_id = "product-123456-2"
    fixture = FixtureAPI(tenant_id="owned-tenant")
    result = driver_lifecycle(
        fixture.client,
        tenant_id="owned-tenant",
        other_tenant_id="other-tenant",
        owner_token="owner-token",
        actor_id="owner-id",
        run_id=run_id,
        source_revision="a" * 40,
        state_store=ProductState(private_dir, run_id),
    )
    calls = []

    def bounded(argv, *, stdin=None, **_kwargs):
        calls.append((argv, stdin))
        if argv[0] == "aws":
            return (
                0,
                json.dumps(
                    {
                        "AccessKeyId": "ASIAEXAMPLE",
                        "SecretAccessKey": "secret+/=",
                        "SessionToken": "session+/=",
                    }
                ),
                "",
            )
        return (
            0,
            json.dumps(
                {
                    "run_id": run_id,
                    "product_id": result["product_id"],
                    "project_id": result["project_id"],
                    "cleanup_revision": 3,
                    "fixture_removed": True,
                }
            ),
            "",
        )

    monkeypatch.setattr(common, "bounded", bounded)
    evidence = {}
    product_cleanup.execute(
        {
            "work_dir": str(private_dir),
            "evaluation_id": "adp-e2e-20261019-000000-abcdef",
            "product_run_id": run_id,
            "workflow_run_id": run_id,
            "region": "us-east-1",
            "sts_endpoint": "https://sts.us-east-1.amazonaws.com",
            "internal_invoke_url": "https://example.execute-api.us-east-1.amazonaws.com/dev",
        },
        evidence,
    )
    assert evidence["success"] is True
    assert evidence["detail"]["product_id"] == result["product_id"]
    payload = json.loads(calls[1][0][-2])
    assert "document" not in payload
    assert payload["product_id"] == result["product_id"]
    assert set(payload) == {
        "tenant_id",
        "actor_id",
        "run_id",
        "source_revision",
        "idempotency_key",
        "product_id",
        "project_id",
    }
    assert "secret+/=" not in json.dumps(evidence)
