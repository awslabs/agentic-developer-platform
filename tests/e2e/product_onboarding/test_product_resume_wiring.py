"""Local wiring checks; these do not establish a live cross-client result."""

from copy import deepcopy
import hashlib

import pytest

from tests.e2e.new_ui.test_product_resume import checked_fixture, installed_cli, read_session

SOURCE = "a" * 40


@pytest.fixture
def owned_fixture():
    return {"source_revision": SOURCE, "tenant_id": "tenant", "actor_id": "owner",
            "product_id": "product", "project_id": "project", "run_id": "product-123-1",
            "state_store": object(), "protected_cleanup": object(),
            "owner": {"actor_id": "owner", "tenant_id": "tenant"},
            "observer": {"access_token": "test-only-token"},
            "evidence_context": {"deployment": {"observed": {"product_onboarding_enabled": True}}}}


def test_owned_resume_fixture_requires_matching_actor_revision_and_admission(owned_fixture):
    assert checked_fixture(owned_fixture, SOURCE) is owned_fixture
    for change in (
        {"source_revision": "b" * 40},
        {"actor_id": "different"},
        {"state_store": None},
        {"evidence_context": {"deployment": {"observed": {"product_onboarding_enabled": False}}}},
    ):
        invalid = deepcopy(owned_fixture)
        invalid.update(change)
        with pytest.raises(ValueError, match="run-owned"):
            checked_fixture(invalid, SOURCE)


def test_installed_cli_rejects_missing_or_mismatched_checksum_before_execution(tmp_path):
    binary = tmp_path / "adp"
    binary.write_text("exit 0\n")
    helper = tmp_path / "adp-onboard.py"
    helper.write_text("exit 0\n")
    with pytest.raises(ValueError, match="checksums"):
        installed_cli(binary, {"adp": "0" * 64, "adp-onboard.py": "1" * 64},
                      {}, "https://example.test", ["resume"])


def test_installed_cli_uses_pinned_binary_and_requires_json_success(tmp_path):
    binary = tmp_path / "adp"
    binary.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"status":"ok","detail":{"id":"saved-session"}}\'\nprintf \'%s\\n\' \'test-only-token\' >&2\n')
    binary.chmod(0o700)
    helper = tmp_path / "adp-onboard.py"
    helper.write_text("stubbed helper\n")
    checksums = {name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
                 for name in ("adp", "adp-onboard.py")}
    actor = {"access_token": "test-only-token", "id_token": "test-only-id-token",
             "expires_in": "3600", "tenant_id": "tenant"}
    record = type("Record", (), {"cli_events": []})()
    assert installed_cli(binary, checksums, actor, "https://example.test",
                         ["resume", "--session", "saved-session"], record=record) == {"id": "saved-session"}
    assert record.cli_events[0]["exit_code"] == 0
    assert record.cli_events[0]["stderr"].strip() == "[REDACTED]"
    assert record.cli_events[0]["installed_cli_sha256"] == checksums["adp"]
    helper.write_text("changed after pin\n")
    with pytest.raises(ValueError, match="checksums"):
        installed_cli(binary, checksums, actor, "https://example.test", ["resume"])


def test_independent_api_readback_requires_success_and_exact_session(owned_fixture):
    class Response:
        status = 200

        def json(self):
            return {"id": "session"}

    class Request:
        def get(self, path, *, headers, timeout):
            assert path == "https://example.test/api/v1/products/product/projects/project/onboarding-sessions/session"
            assert headers == {"Authorization": "Bearer test-only-token"}
            assert timeout == 30_000
            return Response()

    provider = type("Provider", (), {"request": Request()})()
    assert read_session(provider, "https://example.test", owned_fixture, "session") == {"id": "session"}
    Response.status = 404
    with pytest.raises(AssertionError, match="readback"):
        read_session(provider, "https://example.test", owned_fixture, "session")
