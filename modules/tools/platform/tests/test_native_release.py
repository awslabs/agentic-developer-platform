"""Exercise native release preflight and retained outcome, without live effects."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from modules.tools.platform.scripts import native_release as release
from modules.tools.platform.scripts import native_smoke_inputs as inputs
from modules.tools.platform.scripts.native_test_session import AdmissionUnavailable
from modules.tools.platform.tests.test_native_smoke import GATEWAY, Session
from modules.tools.platform.tests.test_native_smoke_inputs import S3, delivery  # noqa: F401


@pytest.fixture
def prepared(delivery, tmp_path, monkeypatch):  # noqa: F811
    env, bundle = delivery
    env.update(inputs.download(env, call=S3(bundle)))
    monkeypatch.setattr(
        "modules.tools.platform.tests.test_native_smoke.STARTED",
        bundle["approval_receipt"]["run_started_at"],
    )
    outputs, targets = tmp_path / "outputs.json", tmp_path / "targets.jsonl"
    outputs.write_text(json.dumps({"gateway_url": {"value": GATEWAY}}))
    targets.write_text(
        json.dumps(
            {"targetId": "offline", "lastSynchronizedAt": "2026-01-01T00:00:00Z"}
        )
    )
    return env, bundle, outputs, targets


@pytest.mark.parametrize(
    "name",
    [
        "ADP_TOOLS_SMOKE_APPROVAL_FILE",
        "ADP_TOOLS_SMOKE_FIXTURE_FILE",
        "ADP_TOOLS_SMOKE_NATIVE_LOGIN_FILE",
        "ADP_TOOLS_SMOKE_DELIVERY_FILE",
        "ADP_TOOLS_SMOKE_EVIDENCE_DIR",
    ],
)
def test_missing_input_preflight_is_actionable_and_has_no_network(prepared, name):
    env, _, _, _ = prepared
    env.pop(name)
    with patch.object(release.NativeSession, "exchange") as exchange:
        with pytest.raises(AdmissionUnavailable, match=name):
            release.preflight(env)
        exchange.assert_not_called()


def test_expired_original_approval_refuses_preflight(prepared):
    env, bundle, _, _ = prepared
    with pytest.raises(AdmissionUnavailable, match="expired"):
        release.preflight(env, now=bundle["approval_receipt"]["run_started_at"] + 300)


def test_success_retains_original_cleanup_with_pending_funds(prepared):
    env, _, outputs, targets = prepared
    _, fixture = release.preflight(env)
    session = Session(fixture)
    result = release.run(outputs, targets, env, exchange=lambda _: session)
    assert result["behavioral_status"] == "passed"
    assert result["cleanup_status"] == "confirmed"
    assert result["funding_status"] == "held"
    assert result["accounting_status"] == "pending"
    assert result["governance_complete"] is False
    evidence = Path(env["ADP_TOOLS_SMOKE_EVIDENCE_DIR"]) / "native-smoke.json"
    assert evidence.stat().st_mode & 0o777 == 0o600
    assert evidence.stat().st_size <= 16384
    assert fixture.query not in evidence.read_text()
    assert "fixture.access.signature" not in evidence.read_text()
    assert len(session.calls) == 4 and session.status_reads == 1
    with pytest.raises(AdmissionUnavailable, match="already"):
        release.run(
            outputs, targets, env, exchange=lambda _: pytest.fail("must not re-enroll")
        )


@pytest.mark.parametrize("failure", ["exchange", "search", "cleanup", "status"])
def test_failures_retain_evidence_and_never_publish(prepared, failure):
    env, _, outputs, targets = prepared
    _, fixture = release.preflight(env)
    session = Session(
        fixture,
        fail_search=failure == "search",
        fail_close=failure == "cleanup",
        status_change=(lambda status: status.update(funding_status="unknown"))
        if failure == "status"
        else None,
    )

    def exchange(_):
        if failure == "exchange":
            raise AdmissionUnavailable("private-provider-sentinel")
        return session

    with pytest.raises(AdmissionUnavailable, match="publication refused") as error:
        release.run(outputs, targets, env, exchange=exchange)
    path = Path(env["ADP_TOOLS_SMOKE_EVIDENCE_DIR"]) / "native-smoke.json"
    report = json.loads(path.read_text())
    assert report["behavioral_status"] == "failed"
    assert report["governance_complete"] is False
    assert "private-provider-sentinel" not in path.read_text() + str(error.value)
    assert sum(call[1] == "/api/agent/tools/call" for call in session.calls) <= 1
    assert sum("/close?" in call[1] for call in session.calls) <= 1


def test_workflow_loads_inputs_before_deploy_and_retains_only_bounded_receipts():
    root = Path(__file__).resolve().parents[4]
    workflow = (root / ".github/workflows/tools-platform-deploy.yml").read_text()
    assert (
        "SMOKE_REFRESH_TOKEN" not in workflow
        and "TLS_EVAL_REFRESH_TOKEN" not in workflow
    )
    assert "secrets.ADP_TOOLS_SMOKE_NATIVE_ACCESS_TOKEN" in workflow
    assert workflow.index("native_smoke_inputs download") < workflow.index(
        "Assume environment-scoped deployment role"
    )
    assert workflow.index("native_evidence") < workflow.index(
        "Publish verified tools platform release"
    )
    assert "github.run_attempt == 1" in workflow
    assert "timeout-minutes: 12" in workflow and "timeout-minutes: 2" in workflow
    assert 'rm -f -- "$RUNNER_TEMP/adp-native-tools-inputs/login.jwt"' in workflow


def test_authenticated_status_cannot_move_the_original_approval_start(prepared):
    env, _, outputs, targets = prepared
    _, fixture = release.preflight(env)

    def move_start(status):
        status["run_started_at"] += 1
        for key in (
            "session_deadline_at",
            "cleanup_deadline_at",
            "cleanup_confirmed_at",
        ):
            status["executions"][0][key] += 1

    session = Session(fixture, status_change=move_start)
    with pytest.raises(AdmissionUnavailable, match="publication refused"):
        release.run(outputs, targets, env, exchange=lambda _: session)
    report = json.loads(
        (Path(env["ADP_TOOLS_SMOKE_EVIDENCE_DIR"]) / "native-smoke.json").read_text()
    )
    assert report["behavioral_status"] == "failed"
    assert "original_approval_start_mismatch" in report["failures"]


@pytest.mark.parametrize(
    "fault", [None, "missing", "digest", "run", "manifest", "settlement"]
)
def test_only_bound_retained_evidence_can_qualify_selected_smoke(prepared, fault):
    env, _, outputs, targets = prepared
    _, fixture = release.preflight(env)
    result = release.run(outputs, targets, env, exchange=lambda _: Session(fixture))
    path = outputs.parent / "release.json"
    document = {
        "component": "tools-platform",
        "source_revision": env["ADP_SOURCE_REVISION"],
        "definition_revision": env["ADP_DEFINITION_REVISION"],
        "qualification_status": "smoke_passed_evidence_pending",
        "native_smoke": result,
    }
    if fault == "run":
        document["native_smoke"]["native_run_id"] = "foreign-run"
    if fault == "settlement":
        document["native_smoke"]["governance_complete"] = True
    inputs.private_write(path, json.dumps(document).encode())
    manifest = {
        "bucket": "adp-dev-ci-evidence-123456789012",
        "version_id": "original-version",
        "sha256": "d" * 64,
        "size": 1000,
        "key": "artifacts/14/aws-e/adp/42/1/release/native-tools-smoke/"
        + "d" * 64
        + ".manifest.json",
    }
    if fault == "manifest":
        manifest["version_id"] = "null"
    retention = {
        "version": 1,
        "manifest": manifest,
        "native_smoke_sha256": "e" * 64
        if fault == "digest"
        else result["native_evidence_sha256"],
    }
    if fault != "missing":
        inputs.private_write(
            Path(env["ADP_TOOLS_SMOKE_EVIDENCE_DIR"]) / "retention.json",
            json.dumps(retention).encode(),
        )
    if fault:
        with pytest.raises((AdmissionUnavailable, OSError)):
            release.qualify(path, env)
        assert (
            json.loads(path.read_text())["qualification_status"]
            == "smoke_passed_evidence_pending"
        )
    else:
        release.qualify(path, env)
        qualified = json.loads(path.read_text())
        assert (
            qualified["qualification_status"]
            == "selected_smoke_verified_evidence_retained"
        )
        assert qualified["native_evidence_manifest"] == manifest
        assert qualified["native_smoke"]["governance_complete"] is False
