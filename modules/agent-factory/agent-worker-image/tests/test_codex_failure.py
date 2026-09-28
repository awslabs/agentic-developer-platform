import json
from types import SimpleNamespace

from lib.codex_failure import failure_summary


def result(code=1, stdout="", stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def test_structured_cause_survives_noisy_token_logs():
    output = "worker log\n" + json.dumps({"status": "review_failed", "error": {"message": "git check failed: code.ts:3"}})
    assert failure_summary(result(stdout=output, stderr="[TokenManager] refreshed\n" * 200)) == (
        "Codex reviewer exit 1: git check failed: code.ts:3"
    )


def test_signal_is_reported_even_when_only_token_output_survives():
    assert failure_summary(result(-15, stderr="[TokenManager] refreshed")).startswith("Codex reviewer signal SIGTERM:")
    assert "signal SIGKILL" in failure_summary(result(-9))


def test_legacy_error_header_is_kept_before_long_stack_and_refresh_tail():
    message = failure_summary(result(stderr="agent-codex-reviewer failed Error: Git validation failed\n" + "stack\n" * 500))
    assert "Git validation failed" in message
    assert len(message) <= 1024


def test_malformed_output_and_success_receipts_are_not_failure_diagnostics():
    output = 'not json\n[]\n{"status":"review_failed","error":null}\n{"status":"engine_reviewed"}'
    assert "provider unavailable" in failure_summary(result(stdout=output, stderr="provider unavailable"))


def test_failure_categories_do_not_copy_secrets_or_authorize_retries():
    from lib.codex_failure import failure_details

    for text, category in [("Bad credentials", "authentication"), ("policy_expired wall_clock", "policy"),
                           ("high-risk cyber", "provider_refusal"), ("AbortError", "transport")]:
        details = failure_details(result(stderr=text + " token=secret"))
        assert details == {"category": category, "exit_code": 1}
    assert failure_details(result(124))["category"] == "deadline"
    assert failure_details(result(-9))["category"] == "signal"
    assert failure_details(result(1), aborted=True)["category"] == "cancelled"
    assert "TokenManager" not in failure_summary(result(stderr="[TokenManager] refreshed"))


def test_developer_structured_failure_is_classified_from_original_cause():
    from lib.codex_failure import failure_details

    process = result(stdout=json.dumps({"status": "agent_failed", "error": {"message": "402 budget_exceeded"}}))
    assert "402 budget_exceeded" in failure_summary(process)
    assert failure_details(process) == {"category": "policy", "exit_code": 1}
