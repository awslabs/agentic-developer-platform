"""Guard the maintained Product selector before any target reads."""

import json
from types import SimpleNamespace

import pytest

from tests.e2e.product_onboarding import live

SOURCE = "a" * 40


@pytest.mark.parametrize(
    "cases",
    [
        "api_denial_oracle,api_product_lifecycle",
        "api_recovery_cleanup,api_product_lifecycle",
        "api_recovery_cleanup,api_denial_oracle",
    ],
)
def test_unordered_live_selection_refuses_before_target_reads(
    cases, tmp_path, monkeypatch
):
    monkeypatch.setenv("GITHUB_RUN_ID", "123456")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "boundary"))
    monkeypatch.setattr(
        live, "resolved_config", lambda *_: pytest.fail("target configuration read")
    )
    report_path = tmp_path / "dispatch.json"
    with pytest.raises(ValueError, match="canonical case order"):
        live.main(
            [
                "--cases",
                cases,
                "--expected-revision",
                SOURCE,
                "--dispatch-report",
                str(report_path),
            ]
        )
    report = json.loads(report_path.read_text())
    assert report["selected_cases"] == cases.split(",")
    assert report["status"] == "incomplete"
    assert report["observed_revision"] is None
    assert report["cleanup_verified"] is False
    assert (tmp_path / "boundary").read_text() == "cleanup_ok=false\n"


def test_denial_selection_refuses_native_gap_before_product_mutation(
    tmp_path, monkeypatch
):
    from tests.e2e.cli_uplift import ports
    from tests.e2e.product_onboarding import product_execution, product_inventory

    monkeypatch.setenv("GITHUB_RUN_ID", "123456")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "boundary"))
    monkeypatch.setenv("GITHUB_SHA", "b" * 40)
    monkeypatch.delenv("PRODUCT_API_FIXTURES_JSON", raising=False)
    monkeypatch.setattr(product_execution, "recovery_available", lambda: True)
    monkeypatch.setattr(
        live,
        "resolved_config",
        lambda revision: {
            "expected_revision": revision,
            "platform_account": "000000000000",
        },
    )
    monkeypatch.setattr(
        ports,
        "default_ports",
        lambda _cfg: {
            "aws": SimpleNamespace(
                call=lambda *_args: {
                    "Account": "000000000000",
                    "Arn": "arn:aws:sts::000000000000:assumed-role/adp-cli-uplift-eval-orchestrator/fixture",
                }
            )
        },
    )
    monkeypatch.setattr(
        product_inventory,
        "observe",
        lambda *_: {"source_revision": SOURCE, "pods": [{"uid": "owned-pod"}]},
    )
    monkeypatch.setattr(
        product_execution.statestore,
        "default_store",
        lambda *_: pytest.fail("durable Product create intent began"),
    )
    output = tmp_path / "dispatch.json"
    with pytest.raises(
        ports.PortError, match="Native migration-head and tick observation"
    ):
        live.main(
            [
                "--cases",
                "api_product_lifecycle,api_denial_oracle",
                "--expected-revision",
                SOURCE,
                "--dispatch-report",
                str(output),
            ]
        )
    report = json.loads(output.read_text())
    assert report["selected_cases"] == ["api_product_lifecycle", "api_denial_oracle"]
    assert report["observed_revision"] == SOURCE
    assert report["status"] == "incomplete"
    assert report["cleanup_verified"] is False
    assert (tmp_path / "boundary").read_text() == "cleanup_ok=false\n"


@pytest.mark.parametrize("wrong", ["account", "role", "missing_arn"])
def test_selected_product_refuses_wrong_evaluator_before_release_reads(
    tmp_path, monkeypatch, wrong
):
    from tests.e2e.cli_uplift import ports
    from tests.e2e.product_onboarding import product_inventory

    monkeypatch.setenv("GITHUB_RUN_ID", "123456")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "boundary"))
    monkeypatch.delenv("PRODUCT_API_FIXTURES_JSON", raising=False)
    monkeypatch.setattr(
        live,
        "resolved_config",
        lambda revision: {
            "expected_revision": revision,
            "platform_account": "000000000000",
        },
    )
    identity = {
        "Account": "000000000001" if wrong == "account" else "000000000000",
        "Arn": "arn:aws:sts::000000000000:assumed-role/"
        + ("another-role" if wrong == "role" else "adp-cli-uplift-eval-orchestrator")
        + "/fixture",
    }
    if wrong == "missing_arn":
        identity.pop("Arn")
    calls = []

    def caller(service, operation):
        calls.append((service, operation))
        return identity

    monkeypatch.setattr(
        ports,
        "default_ports",
        lambda _cfg: {"aws": SimpleNamespace(call=caller)},
    )
    monkeypatch.setattr(
        product_inventory,
        "observe",
        lambda *_args: pytest.fail("Release read started with wrong evaluator"),
    )
    output = tmp_path / "dispatch.json"
    with pytest.raises(ValueError, match="evaluator identity differs"):
        live.main(
            [
                "--cases",
                "api_product_lifecycle",
                "--expected-revision",
                SOURCE,
                "--dispatch-report",
                str(output),
            ]
        )
    assert calls == [("sts", "get_caller_identity")]
    assert json.loads(output.read_text())["cleanup_verified"] is False
    assert (tmp_path / "boundary").read_text() == "cleanup_ok=false\n"
