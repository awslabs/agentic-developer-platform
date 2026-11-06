"""Owner-bound, non-token recovery publication and independent S3 readback."""

import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from tests.e2e.tools.atlassian_setup_live import GatewaySetupDriver, load_setup
from tests.e2e.tools.atlassian_setup_recovery import (
    SNAPSHOT,
    capture,
    restore_selected,
    verify_receipt,
)
from tests.e2e.tools.test_atlassian_setup_live import configuration


REVISION = "a" * 40


def test_pending_attempt_publishes_only_non_token_recovery_state(tmp_path, monkeypatch):
    path, config = configuration(tmp_path)
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_SETUP_FILE", path)
    fixture, config = load_setup(path, gateway_url=config["gateway_url"])
    fixture["ledger"].record("connection_begin", actor="machine", status="started")
    GatewaySetupDriver(config)._save_attempt(
        {
            "run_id": config["run_id"],
            "owner_id": config["owner_id"],
            "fixture_kind": config["fixture_kind"],
            "phase": "pending",
            "state": "browser-state-secret",
            "session_uri": "opaque-identity-secret",
            "authorization_url": "https://auth.example.test/private?secret=opaque",
            "connection_id": None,
            "revision": None,
        }
    )
    destination = tmp_path / "evidence"
    recovery_path = capture(config, destination)
    payload = recovery_path.read_text()
    assert recovery_path.stat().st_mode & 0o077 == 0
    assert destination.stat().st_mode & 0o077 == 0
    assert json.loads(payload)["phase"] == "pending"
    assert len(json.loads(payload)["actions"]) == 1
    assert all(
        secret not in payload
        for secret in (
            "browser-state-secret",
            "opaque-identity-secret",
            "private?secret",
        )
    )
    with pytest.raises(FileExistsError):
        capture(config, destination)


def s3_receipt(content, *, config, run_id="314", attempt="2", revision=REVISION):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
        info = tarfile.TarInfo(SNAPSHOT)
        info.size = len(content)
        bundle.addfile(info, io.BytesIO(content))
    artifact = archive.getvalue()
    bucket = f"adp-dev-ci-evidence-{config['account']}"
    prefix = f"artifacts/14/aws-e/adp/{run_id}/{attempt}/evaluate/tools-live-evaluation-{run_id}-{attempt}/"
    artifact_hash = hashlib.sha256(artifact).hexdigest()
    artifact_key = f"{prefix}{artifact_hash}.tar.gz"
    manifest = {
        "schema_version": 1,
        "repository": "aws-e/adp",
        "run_id": run_id,
        "run_attempt": attempt,
        "job": "evaluate",
        "name": f"tools-live-evaluation-{run_id}-{attempt}",
        "retention_days": 14,
        "checked_out_revision": revision,
        "workflow_revision": revision,
        "missing_path_count": 0,
        "artifact": {
            "bucket": bucket,
            "key": artifact_key,
            "sha256": artifact_hash,
            "version_id": "artifact-v1",
            "size": len(artifact),
        },
        "files": [
            {
                "path": SNAPSHOT,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
    }
    metadata = json.dumps(manifest).encode()
    key = f"{prefix}{hashlib.sha256(metadata).hexdigest()}.manifest.json"
    uri = f"s3://{bucket}/{key}"

    def fetch(selected_bucket, selected_key):
        assert selected_bucket == bucket
        data = (
            metadata
            if selected_key == key
            else artifact
            if selected_key == artifact_key
            else None
        )
        if data is None:
            raise ValueError("Unexpected evidence key")
        return data, {
            "VersionId": "manifest-v1" if selected_key == key else "artifact-v1",
            "ChecksumSHA256": base64.b64encode(hashlib.sha256(data).digest()).decode(),
            "ContentLength": len(data),
        }

    return uri, fetch


def test_independent_receipt_validates_version_owner_and_snapshot(
    tmp_path, monkeypatch
):
    path, config = configuration(tmp_path)
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_SETUP_FILE", path)
    _, config = load_setup(path, gateway_url=config["gateway_url"])
    content = capture(config, tmp_path / "evidence").read_bytes()
    uri, fetch = s3_receipt(content, config=config)
    assert (
        verify_receipt(
            uri,
            "manifest-v1",
            config=config,
            run_id="314",
            attempt="2",
            revision=REVISION,
            fetch=fetch,
        )[0]
        == content
    )
    with pytest.raises(ValueError):
        verify_receipt(
            uri,
            "wrong-v1",
            config=config,
            run_id="314",
            attempt="2",
            revision=REVISION,
            fetch=fetch,
        )
    with pytest.raises(ValueError):
        verify_receipt(
            uri,
            "manifest-v1",
            config={**config, "owner_id": "other-owner"},
            run_id="314",
            attempt="2",
            revision=REVISION,
            fetch=fetch,
        )
    with pytest.raises(ValueError):
        verify_receipt(
            uri,
            "manifest-v1",
            config=config,
            run_id="315",
            attempt="2",
            revision=REVISION,
            fetch=fetch,
        )


def test_workflow_retains_unverified_journal_and_never_uploads_tokens():
    workflow = Path(".github/workflows/tools-live-evaluation.yml").read_text()
    assert workflow.index("Capture non-token setup recovery") < workflow.index(
        "Publish result and failure evidence"
    )
    assert workflow.index("Verify private setup recovery publication") < workflow.index(
        "Remove owned evaluation configuration"
    )
    assert "ADP_TOOLS_ATLASSIAN_RECOVERY_VERIFIED:-" in workflow
    assert 'rm -f -- "$setup_root/admin.token"' in workflow
    assert "path: ${{ runner.temp }}/tools-eval-evidence/" in workflow
    assert "path: ${{ runner.temp }}/atlassian-setup-" not in workflow
    assert workflow.index("Reconcile previously retained setup") < workflow.index(
        "Exercise deployed gateway"
    )


def test_selected_receipt_resumes_only_matching_live_owner_connection(
    tmp_path, monkeypatch
):
    from tests.e2e.tools.atlassian_setup_recovery import projection

    old_path, config = configuration(tmp_path)
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_SETUP_FILE", old_path)
    old_fixture, old_config = load_setup(old_path, gateway_url=config["gateway_url"])
    old_fixture["ledger"].record("connection_begin", actor="machine", status="started")
    GatewaySetupDriver(old_config)._save_attempt(
        {
            "run_id": config["run_id"],
            "owner_id": config["owner_id"],
            "fixture_kind": config["fixture_kind"],
            "phase": "selected",
            "state": "private-consent-state",
            "session_uri": "private-identity-session",
            "authorization_url": "https://auth.example.test/consent",
            "connection_id": "owned-connection",
            "revision": "selected-revision",
        }
    )
    snapshot = projection(old_config)
    resumed = tmp_path / "resumed"
    resumed.mkdir()
    new_path, config = configuration(resumed)
    fixture, config = load_setup(new_path, gateway_url=config["gateway_url"])
    driver = GatewaySetupDriver(config)

    class Observer:
        def _gateway_status(self):
            return {
                "connection": {
                    "id": "owned-connection",
                    "revision": "selected-revision",
                    "selected_site_id": config["site_id"],
                }
            }

    restore_selected(
        config, snapshot, driver=driver, observer=Observer(), ledger=fixture["ledger"]
    )
    assert driver._attempt()["session_uri"] is None
    assert len(fixture["ledger"].state()["actions"]) == 1
    with pytest.raises(ValueError):
        restore_selected(
            config,
            snapshot,
            driver=driver,
            observer=Observer(),
            ledger=fixture["ledger"],
        )


def test_uncertain_or_changed_provider_state_never_resumes(tmp_path):
    path, config = configuration(tmp_path)
    fixture, config = load_setup(path, gateway_url=config["gateway_url"])
    driver = GatewaySetupDriver(config)

    class Observer:
        def _gateway_status(self):
            return {"connection": {"id": "different", "revision": "selected-revision"}}

    snapshot = {
        "phase": "selected",
        "connection_id": "owned-connection",
        "revision": "selected-revision",
        "actions": [
            {
                "action": "connection_begin",
                "actor": "machine",
                "status": "started",
                "evidence_sha256": None,
            }
        ],
    }
    with pytest.raises(ValueError):
        restore_selected(
            config,
            snapshot,
            driver=driver,
            observer=Observer(),
            ledger=fixture["ledger"],
        )
    with pytest.raises(ValueError):
        restore_selected(
            config,
            {**snapshot, "phase": "pending"},
            driver=driver,
            observer=Observer(),
            ledger=fixture["ledger"],
        )
    assert driver._attempt() is None
