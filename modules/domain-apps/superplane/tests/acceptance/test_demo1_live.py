"""Offline preflight and durable checkpoint tests; never load a real browser."""

import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_demo1_cli import fixture_documents, identifier, run_cli, write_private

from superplane_acceptance.demo1_browser import CreationCheckpoint
from superplane_acceptance.demo1_evidence import DemoInput, EvidenceError
from superplane_acceptance.demo1_live import (
    PrivateCheckpoint,
    validate_browser_state,
)


def inputs():
    selected, _ = fixture_documents()
    now = datetime.now(UTC)
    authorized = now - timedelta(minutes=2)
    deadline = now + timedelta(hours=2)
    selected["authorized_at"] = authorized.isoformat()
    selected["deadline"] = deadline.isoformat()
    authority = {
        "version": "demo1-live-v1",
        "origin": "https://example.invalid",
        "broker_label": "example-connection",
        "authority_ref": identifier(21),
        "release_source": selected["release_source"],
        "image_digest": selected["image_digest"],
        "schema_revision": selected["schema_revision"],
        "connection_id": selected["connection_id"],
        "role": selected["role"],
        "account": selected["account"],
        "region": selected["region"],
        "org_id": selected["org_id"],
        "requester_id": selected["requester_id"],
        "approver_id": selected["approver_id"],
        "request_id": selected["request_id"],
        "plan_revision": selected["plan_revision"],
        "budget_usd": selected["budget_usd"],
        "authorized_at": selected["authorized_at"],
        "deadline": selected["deadline"],
        "cleanup_owner": selected["cleanup_owner"],
        "cleanup_deadline": (deadline + timedelta(hours=4)).isoformat(),
        "max_runtime_seconds": 900,
        "recovery_checkpoint": selected["recovery_checkpoint"],
    }
    session = {
        "cookies": [],
        "origins": [
            {
                "origin": "https://example.invalid",
                "localStorage": [
                    {
                        "name": "cognito_access_token",
                        "value": "synthetic-token-must-stay-private",
                    },
                ],
            },
        ],
    }
    return selected, authority, session


def launch(tmp_path: Path, selected, authority, session):
    paths = [
        tmp_path / name for name in ("selection.json", "authority.json", "session.json")
    ]
    for path, value in zip(paths, (selected, authority, session), strict=True):
        write_private(path, value)
    report = tmp_path / "report.json"
    checkpoint = tmp_path / "checkpoint.json"
    result = run_cli(
        "--mode",
        "live",
        "--private-input",
        str(paths[0]),
        "--authority",
        str(paths[1]),
        "--browser-state",
        str(paths[2]),
        "--checkpoint",
        str(checkpoint),
        "--report",
        str(report),
    )
    return result, report, checkpoint


def test_documented_preflight_command_is_runnable_but_never_accepts(tmp_path):
    selected, authority, session = inputs()
    tmp_path.chmod(0o700)
    for name, document in (
        ("selection.json", selected),
        ("authority.json", authority),
        ("requester-state.json", session),
    ):
        write_private(tmp_path / name, document)

    documentation = (Path(__file__).parent / "README.md").read_text()
    match = re.search(
        r"### Guarded live-selection preflight \(no effects\).*?```sh\n(.*?)\n```",
        documentation,
        re.DOTALL,
    )
    assert match is not None
    repository = Path(__file__).resolve().parents[5]
    result = subprocess.run(
        ["bash", "-c", match.group(1)],
        cwd=repository,
        env={**os.environ, "DEMO1_PRIVATE_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    report = tmp_path / "report.json"
    assert result.returncode == 2
    assert "Live selection preflight: BLOCKED" in result.stdout
    assert not result.stderr
    document = json.loads(report.read_text())
    assert report.stat().st_mode & 0o777 == 0o600
    assert document["live_acceptance"] is False
    assert document["status"] == "BLOCKED"
    assert document["evidence_mode"] == "live-selection-unverified"
    assert not (tmp_path / "checkpoint.json").exists()
    assert session["origins"][0]["localStorage"][0]["value"] not in report.read_text()


def test_cli_records_private_preflight_without_launching_or_claiming_success(tmp_path):
    selected, authority, session = inputs()
    result, report, checkpoint = launch(tmp_path, selected, authority, session)
    assert result.returncode == 2
    assert "BLOCKED" in result.stdout
    assert report.stat().st_mode & 0o777 == 0o600
    document = json.loads(report.read_text())
    assert document["status"] == "BLOCKED"
    assert document["live_acceptance"] is False
    assert document["evidence_mode"] == "live-selection-unverified"
    assert document["criteria"]["AC-02"] == "BLOCKED"
    assert not checkpoint.exists()
    for private in (
        session["origins"][0]["localStorage"][0]["value"],
        selected["account"],
        selected["request_id"],
        authority["broker_label"],
    ):
        assert private not in result.stdout + result.stderr + report.read_text()


@pytest.mark.parametrize(
    "field",
    [
        "release_source",
        "image_digest",
        "schema_revision",
        "connection_id",
        "role",
        "account",
        "region",
        "org_id",
        "requester_id",
        "approver_id",
        "request_id",
        "plan_revision",
        "budget_usd",
        "cleanup_owner",
    ],
)
def test_foreign_authority_is_rejected_before_creating_any_report(tmp_path, field):
    selected, authority, session = inputs()
    authority[field] = "example-swapped-value"
    result, report, checkpoint = launch(tmp_path, selected, authority, session)
    assert result.returncode == 2 and not report.exists() and not checkpoint.exists()
    assert "example-swapped-value" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "expired",
        "too-long",
        "missing-approval",
        "wrong-origin",
        "cross-origin-cookie",
        "reused-report",
    ],
)
def test_expired_missing_authority_and_unsafe_browser_state_refuse(tmp_path, mutation):
    selected, authority, session = inputs()
    if mutation == "expired":
        selected["deadline"] = authority["deadline"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
    elif mutation == "too-long":
        authority["max_runtime_seconds"] = 14_401
    elif mutation == "missing-approval":
        authority["authority_ref"] = ""
    elif mutation == "wrong-origin":
        session["origins"][0]["origin"] = "https://another.invalid"
    elif mutation == "cross-origin-cookie":
        session["cookies"] = [
            {"domain": "another.invalid", "name": "example", "value": "secret"}
        ]
    elif mutation == "reused-report":
        (tmp_path / "report.json").write_text("existing")
    result, report, checkpoint = launch(tmp_path, selected, authority, session)
    assert result.returncode == 2 and not checkpoint.exists()
    if mutation != "reused-report":
        assert not report.exists()
    else:
        assert report.read_text() == "existing"


def test_checkpoint_is_private_durable_and_rejects_replay_and_swapped_scope(tmp_path):
    selected, authority, _ = inputs()
    parsed = DemoInput.parse(selected)
    path = str(tmp_path / "checkpoint.json")
    state = CreationCheckpoint(
        parsed.request_id,
        identifier(6),
        parsed.plan_revision,
        identifier(8),
        identifier(9),
    )
    with PrivateCheckpoint(path, parsed, authority["origin"]) as store:
        assert store.load() is None
        store.save(state)
        assert os.stat(path).st_mode & 0o777 == 0o600
        store.save(CreationCheckpoint(**{**vars(state), "submitted": True}))
        with pytest.raises(EvidenceError, match="replay"):
            store.save(state)
        with pytest.raises(EvidenceError, match="identity change"):
            store.save(
                CreationCheckpoint(**{**vars(state), "workspace_id": identifier(40)})
            )
    with PrivateCheckpoint(path, parsed, authority["origin"]) as resumed:
        assert resumed.load().submitted is True
        with (
            pytest.raises(EvidenceError, match="runner"),
            PrivateCheckpoint(path, parsed, authority["origin"]),
        ):
            pytest.fail("second runner must not enter")
    with (
        PrivateCheckpoint(path, parsed, "https://another.invalid") as foreign,
        pytest.raises(EvidenceError, match="selection differs"),
    ):
        foreign.load()


def test_cli_reports_only_hashes_of_saved_checkpoint(tmp_path):
    selected, authority, session = inputs()
    parsed = DemoInput.parse(selected)
    path = str(tmp_path / "checkpoint.json")
    with PrivateCheckpoint(path, parsed, authority["origin"]) as store:
        store.save(
            CreationCheckpoint(
                parsed.request_id,
                identifier(6),
                parsed.plan_revision,
                identifier(8),
                identifier(9),
                submitted=True,
            )
        )
    result, report, _ = launch(tmp_path, selected, authority, session)
    assert result.returncode == 2
    document = json.loads(report.read_text())
    assert document["checkpoint"]["submitted"] is True
    for private in (
        parsed.request_id,
        identifier(6),
        identifier(8),
        session["origins"][0]["localStorage"][0]["value"],
    ):
        assert private not in report.read_text()


def test_malformed_checkpoint_fields_refuse_without_echoing_private_values(tmp_path):
    selected, authority, session = inputs()
    parsed = DemoInput.parse(selected)
    path = tmp_path / "checkpoint.json"
    state = CreationCheckpoint(
        parsed.request_id,
        identifier(6),
        parsed.plan_revision,
        identifier(8),
        identifier(9),
    )
    with PrivateCheckpoint(str(path), parsed, authority["origin"]) as store:
        payload = {
            "version": "demo1-checkpoint-v1",
            "scope": store.scope,
            "checkpoint": {**vars(state), "approval_id": {"private": "do-not-echo"}},
        }
        write_private(path, payload)
        with pytest.raises(EvidenceError):
            store.load()
    result, report, _ = launch(tmp_path, selected, authority, session)
    assert result.returncode == 2 and not report.exists()
    assert "do-not-echo" not in result.stdout + result.stderr


def test_symlinked_checkpoint_and_missing_browser_credentials_refuse(tmp_path):
    selected, authority, session = inputs()
    parsed = DemoInput.parse(selected)
    original = tmp_path / "original.json"
    write_private(original, {"some": "private state"})
    (tmp_path / "checkpoint.json").symlink_to(original)
    result, report, _ = launch(tmp_path, selected, authority, session)
    assert result.returncode == 2 and not report.exists()
    assert original.read_text() == json.dumps({"some": "private state"})
    session["origins"][0]["localStorage"] = []
    with pytest.raises(EvidenceError, match="authenticated requester"):
        validate_browser_state(session, authority["origin"])
    assert parsed.request_id == selected["request_id"]
