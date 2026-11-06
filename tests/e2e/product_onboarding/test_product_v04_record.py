"""Controlled B03 recorder checks; no served UI or installed CLI is simulated."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.product_onboarding.product_v04_evidence import ISOLATION_SELECTORS
from tests.e2e.product_onboarding.product_v04_record import ScopeEvidence


@pytest.fixture
def collected(tmp_path):
    evidence = ScopeEvidence(
        tmp_path,
        {
            "GITHUB_RUN_ID": "42",
            "GITHUB_RUN_ATTEMPT": "1",
            "PRODUCT_EXPECTED_REVISION": "a" * 40,
            "PRODUCT_BROWSER_WORKFLOW_REVISION": "b" * 40,
            "GITHUB_SHA": "a" * 40,
        },
    )
    evidence.bind(
        {
            "run_id": "product-42-1",
            "source_revision": "a" * 40,
            "evidence_context": {
                "deployment": {"receipt_ref": "evidence/release"},
                "identities": {"source_revision": "a" * 40},
            },
        }
    )
    return evidence


def test_b03_records_separate_passed_api_and_browser_cases_and_cleanup(
    collected, tmp_path
):
    record = collected.case(ISOLATION_SELECTORS["api"])
    record.phases.update(setup="passed", call="passed", teardown="passed")
    record.reads.append({"status": 200, "body": {"product_id": "owned"}})
    scope = {
        "cleanup_pending": False,
        "cleanup_receipt": {
            "owned_absent": True,
            "shared_unchanged": True,
            "fixture_removed": True,
        },
    }
    provider = SimpleNamespace(state=SimpleNamespace(document={"scope": scope}))
    collected.record_cleanup(
        provider,
        {"run_id": "product-42-1", "cases": {"scope": scope["cleanup_receipt"]}},
    )
    assert collected.cleanup["verified"] is True
    report = Path(collected.write())
    assert report.exists() and report.stat().st_mode & 0o077 == 0
    import json

    value = json.loads(report.read_text())
    assert value["cases"]["AC-01"]["api"]["status"] == "pass"
    assert value["cases"]["AC-02"]["browser"]["status"] == "not_run"
    assert value["cases"]["AC-01"]["api"]["reads"] == 1
    assert json.loads(Path(value["cleanup_ref"]).read_text())["verified"] is True


@pytest.mark.parametrize(
    "change",
    [
        lambda scope: scope["scope"].update(cleanup_pending=True),
        lambda scope: scope.update(scope_editor_restore_required=True),
        lambda scope: scope["scope"].update(cleanup_receipt={"owned_absent": False}),
    ],
)
def test_b03_refuses_unrestored_or_unconfirmed_cleanup(collected, change):
    receipt = {"owned_absent": True, "shared_unchanged": True, "fixture_removed": True}
    state = {"scope": {"cleanup_pending": False, "cleanup_receipt": receipt}}
    change(state)
    collected.record_cleanup(
        SimpleNamespace(state=SimpleNamespace(document=state)),
        {"run_id": "product-42-1", "cases": {"scope": receipt}},
    )
    assert collected.cleanup["verified"] is False


def test_b03_rejects_other_run_or_unverified_release(tmp_path):
    collected = ScopeEvidence(
        tmp_path,
        {
            "GITHUB_RUN_ID": "42",
            "GITHUB_RUN_ATTEMPT": "1",
            "PRODUCT_EXPECTED_REVISION": "a" * 40,
            "PRODUCT_BROWSER_WORKFLOW_REVISION": "b" * 40,
            "GITHUB_SHA": "a" * 40,
        },
    )
    with pytest.raises(ValueError, match="another workflow attempt"):
        collected.bind({"run_id": "product-41-1", "source_revision": "a" * 40})
    with pytest.raises(ValueError, match="signed release"):
        collected.bind(
            {
                "run_id": "product-42-1",
                "source_revision": "a" * 40,
                "evidence_context": {},
            }
        )


@pytest.mark.parametrize("history_state", ["restored", "pending", "missing_result"])
def test_cli_cleanup_requires_preserved_history_and_independent_success(
    collected, history_state
):
    receipt = {"owned_absent": True, "shared_unchanged": True, "fixture_removed": True}
    scope = {
        "cleanup_pending": False,
        "cleanup_receipt": receipt,
        "cli_history": {
            "status": "restored" if history_state == "missing_result" else history_state
        },
    }
    collected.api_attempts = {
        "AC-03": [
            {"result": "interrupted" if history_state == "missing_result" else "pass"}
        ]
    }
    collected.cli_attempts = {"AC-03": [{"result": "pass"}]}
    collected.record_cleanup(
        SimpleNamespace(state=SimpleNamespace(document={"scope": scope})),
        {"run_id": collected.run_id, "cases": {"scope": receipt}},
    )
    assert collected.cleanup["verified"] is True
    assert collected.cleanup["original_projects_preserved"] is (
        history_state == "restored"
    )
    assert collected.cleanup["reused_assets_preserved"] is (history_state == "restored")
