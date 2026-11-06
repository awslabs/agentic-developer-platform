"""Repository commit pins must be explicit immutable selections at admission."""

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.products.models import ProductSourceBinding
from src.products.source_routes import BindingInput
from src.products.verification import observe_binding
from tests.products.test_i08_api import mutation, setup_source
from tests.products.test_i08_verification import FakeGitHub


def selection(**overrides):
    return {
        "kind": "repository",
        "provider": "github",
        "connection_ref": {"kind": "github_installation", "id": "connection"},
        "namespace": "27",
        "native_id": "35",
        **overrides,
    }


@pytest.mark.parametrize(
    "revision", ["main", "release/branch", "a" * 39, "A" * 40, "g" * 40, "2026-10-31T00:00:00Z", "", " " + "a" * 40, "a" * 40 + "\n"]
)
def test_repository_pin_rejects_mutable_refs_and_discovery_timestamps(revision):
    with pytest.raises(ValidationError, match="full lowercase commit SHA"):
        BindingInput.model_validate(selection(source_revision=revision))


@pytest.mark.parametrize("revision", [None, "c" * 40])
def test_repository_pin_preserves_explicit_selection(revision):
    parsed = BindingInput.model_validate(selection(source_revision=revision))
    assert parsed.source_revision == revision
    assert parsed.model_dump()["source_revision"] == revision


def test_repository_without_pin_and_pipeline_selection_remain_supported():
    assert BindingInput.model_validate(selection()).source_revision is None
    pipeline = BindingInput.model_validate(selection(kind="pipeline", repository_id="35", source_revision="discovered-pipeline-version"))
    assert pipeline.source_revision == "discovered-pipeline-version"


@pytest.mark.asyncio
async def test_api_retains_explicit_commit_pin_through_verification(test_client, test_engine, db_session, source_member, monkeypatch):
    path, body = await setup_source(test_client, db_session, source_member)
    body["source_revision"] = "c" * 40
    created = await test_client.post(path, json=body, headers=mutation(1, "create_pinned_source_123"))
    assert created.status_code == 201, created.text
    binding_id = created.json()["id"]
    assert created.json()["selection"]["source_revision"] == "c" * 40
    provider = FakeGitHub(branch="unrelated-default", revision="c" * 40)

    async def read_source(binding, installation):
        return await observe_binding(binding, installation, github_client=provider)

    monkeypatch.setattr("src.products.source_routes.observe_binding", read_source)
    verified = await test_client.post(f"{path}/{binding_id}/verify", headers=mutation(2, "verify_pinned_source_123"))
    assert verified.status_code == 202, verified.text
    assert verified.json()["status"] == "succeeded"
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent:
        saved = await independent.get(ProductSourceBinding, (source_member.org_id, binding_id))
        assert saved.desired["source_revision"] == saved.observed["source_revision"] == "c" * 40
    assert provider.calls[-1][-1] == "/repos/example/app/commits/" + "c" * 40


@pytest.mark.asyncio
async def test_api_rejects_mutable_pin_without_creating_binding(test_client, db_session, source_member):
    path, body = await setup_source(test_client, db_session, source_member)
    body["source_revision"] = "main"
    rejected = await test_client.post(path, json=body, headers=mutation(1, "reject_mutable_pin_123"))
    assert rejected.status_code == 422
    listing = await test_client.get(path)
    assert listing.status_code == 200 and listing.json()["items"] == []
    assert listing.headers["Profile-Revision"] == "1"
