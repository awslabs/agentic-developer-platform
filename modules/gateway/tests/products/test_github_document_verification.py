"""Product documents have source integrity, independent of financial import."""

import base64
import hashlib

import pytest

from src.product_onboarding.financial_documents import read_document
from src.products.verification import observe_binding
from tests.products.test_financial_documents import source


def architecture_source(content=b"# Architecture\n\nAPI -> queue -> worker.\n"):
    binding, connection, github, _ = source()
    binding.native_id = "123:docs/architecture.md"
    digest = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    github.installation_request.return_value.update(
        path="docs/architecture.md", content=base64.b64encode(content).decode(), size=len(content), sha=digest
    )
    return binding, connection, github, digest


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b"# Architecture\n\nAPI -> queue -> worker.\n", b'{"services": ["api", "worker"]}'])
async def test_product_document_verification_accepts_architecture_content(content):
    binding, connection, github, digest = architecture_source(content)
    result = await observe_binding(binding, connection, github_client=github)
    assert result["status"] == "healthy"
    assert result["observed"]["source_revision"] == digest
    assert result["observed"]["native_id"] == binding.native_id
    github.installation_request.assert_awaited_once_with(789, "GET", "/repos/example/product/contents/docs/architecture.md")
    # The finance-specific action must still require financial proposals.
    with pytest.raises(ValueError):
        await read_document(binding, connection, client=github)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["tenant", "repository", "blob", "symlink", "oversize"])
async def test_generic_document_verification_retains_source_authority(failure):
    binding, connection, github, _ = architecture_source()
    if failure == "tenant":
        connection.org_id = "foreign-tenant"
    elif failure == "repository":
        github.get_installation_repository.return_value["id"] = 124
    elif failure == "blob":
        github.installation_request.return_value["sha"] = "0" * 40
    elif failure == "symlink":
        github.installation_request.return_value["type"] = "symlink"
    else:
        github.installation_request.return_value["size"] = 65537
    result = await observe_binding(binding, connection, github_client=github)
    assert result["status"] != "healthy"
    assert "observed" not in result
    if failure == "tenant":
        github.installation_request.assert_not_awaited()
