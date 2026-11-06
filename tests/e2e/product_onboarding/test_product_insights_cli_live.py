"""Offline checks for the protected V13 installed-CLI/API live adapter."""

import hashlib
import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from tests.e2e.product_onboarding.product_insights_cli_live import verify_cli_checkpoint


def setup(tmp_path):
    binary = tmp_path / "adp"
    binary.write_text("installed command")
    gateway = "https://example.test/api"
    fixture = {"run_id": "product-42-1", "source_revision": "a" * 40,
               "project_id": "project", "product_id": "product", "as_of": "2026-10-22T00:00:00Z",
               "portfolio_ids": ["commerce", "shared"], "gateway_url": gateway,
               "authorized_home": str(tmp_path / "owner"), "revoked_home": str(tmp_path / "revoked"),
               "authorized_token": "private-owner", "revoked_token": "private-revoked"}
    for home in (fixture["authorized_home"], fixture["revoked_home"]):
        directory = Path(home) / ".bedrock-gateway"
        directory.mkdir(parents=True)
        (directory / "config.json").write_text(json.dumps({"gateway_url": gateway}))
    release = {"source_revision": "a" * 40, "read_service_receipt": "private-readback",
               "installed_cli_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
    return fixture, release, binary


def test_real_command_arguments_and_independent_authorized_revoked_reads(tmp_path):
    fixture, release, binary = setup(tmp_path)
    commands = []
    reads = []
    output = {"coverage": "complete", "cost_by_currency": {"USD": "10.00"},
              "work_count": 1, "project_id": "project", "product_id": "product"}

    def run(command, **kwargs):
        commands.append((command, kwargs))
        revoked = kwargs["env"]["HOME"] == fixture["revoked_home"]
        return CompletedProcess(command, 5 if revoked else 0,
                                json.dumps({"status": "failed", "error": {"code": "not_found"}, "detail": {}}
                                           if revoked else {"status": "ok", "detail": output}), "")

    def read(url, token):
        reads.append((url, token))
        return (404, {"error": {"code": "not_found"}}) if token == "private-revoked" else (200, output)

    assert verify_cli_checkpoint(fixture, release, binary, run=run, read=read)
    assert len(commands) == len(reads) == 2
    assert commands[0][0][:4] == [str(binary), "insights", "context", "show"]
    assert commands[0][0].count("--portfolio") == 2
    assert reads[0][0] == reads[1][0]
    assert "portfolio_id=commerce&portfolio_id=shared" in reads[0][0]


@pytest.mark.parametrize("change", ["source_revision", "installed_cli_sha256", "gateway_url", "read_service_receipt"])
def test_release_or_gateway_mismatch_fails_before_command(tmp_path, change):
    fixture, release, binary = setup(tmp_path)
    if change == "source_revision":
        release[change] = "b" * 40
    elif change == "gateway_url":
        fixture[change] = "https://other.example.test/api"
    else:
        release[change] = None
    with pytest.raises(ValueError):
        verify_cli_checkpoint(fixture, release, binary, run=lambda *args, **kwargs: pytest.fail("command ran"))


def test_protected_release_input_refuses_public_and_symlinked_files(tmp_path):
    from tests.e2e.product_onboarding.product_insights_cli_live import protected_input

    release = tmp_path / "release.json"
    release.write_text(json.dumps({"source_revision": "a" * 40}))
    release.chmod(0o600)
    assert protected_input(release)["source_revision"] == "a" * 40
    release.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        protected_input(release)
    release.chmod(0o600)
    alias = tmp_path / "alias.json"
    alias.symlink_to(release)
    with pytest.raises(ValueError, match="absolute private"):
        protected_input(alias)


def test_cli_and_api_disagreement_is_never_accepted(tmp_path):
    fixture, release, binary = setup(tmp_path)
    output = {"coverage": "complete", "cost_by_currency": {"USD": "10.00"},
              "work_count": 1, "project_id": "project", "product_id": "product"}

    def run(command, **kwargs):
        return CompletedProcess(command, 0, json.dumps({"status": "ok", "detail": output}), "")

    with pytest.raises(AssertionError, match="independent readback"):
        verify_cli_checkpoint(fixture, release, binary, run=run,
                              read=lambda url, token: (200, {**output, "work_count": 2}))
