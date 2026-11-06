import json
from types import SimpleNamespace

import pytest

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


def test_exhausted_budget_uses_existing_stop_vocabulary_without_copying_payload():
    from lib.codex_failure import budget_stop_reason

    message = '402 Payment Required: {"error":"budget_exceeded","details":{"scope":"chain","scope_cap_usd":100}} token=secret'
    process = result(stdout=json.dumps({"status": "review_failed", "error": {"message": message}}))
    assert budget_stop_reason(process) == "chain_cap_exceeded"
    for text in ("budget_unavailable", "execution_policy_refused", "budget exceeded in source code"):
        assert budget_stop_reason(result(stderr=text)) is None
    for code in (0, -9, 124):
        assert budget_stop_reason(result(code=code, stderr=message)) is None


def denial(scope="chain", **body_fields):
    body = {"error": "budget_exceeded", "details": {"scope": scope, "scope_cap_usd": 150.0}, **body_fields}
    return "unexpected status 402 Payment Required: " + json.dumps(body) + ", url: http://127.0.0.1:9090/openai/v1/responses, request id: fixture"


def failed_envelope(message, status="agent_failed"):
    return json.dumps({"status": status, "error": {"message": message}})


@pytest.mark.parametrize("status", ["agent_failed", "review_failed"])
@pytest.mark.parametrize("scope,reason", [
    ("chain", "chain_cap_exceeded"), ("run", "run_cap_exceeded"),
    ("root_user", "root_user_cap_exceeded"), ("person", "person_cap_exceeded"),
    ("org", "hierarchy_cap_exceeded"), ("team", "hierarchy_cap_exceeded"),
    ("user", "hierarchy_cap_exceeded"), (None, "hierarchy_cap_exceeded"),
])
def test_native_terminal_budget_scope_is_exact(status, scope, reason):
    from lib.codex_failure import budget_stop_reason

    process = result(stdout="earlier activity\n" + failed_envelope(denial(scope), status))
    assert budget_stop_reason(process) == reason


def test_scope_comes_from_body_details_before_display_truncation():
    from lib.codex_failure import budget_stop_reason

    # Text mentioning another scope is not authority. The actual body may be
    # longer than failure_summary's display bound while within the SDK bound.
    body = {"error": "budget_exceeded", "message": 'example: "scope":"run" ' + "x" * 1100,
            "details": {"scope": "chain", "scope_cap_usd": 150.0}}
    process = result(stdout=failed_envelope("unexpected status 402 Payment Required: " + json.dumps(body)))
    assert "scope_cap_usd" not in failure_summary(process)
    assert budget_stop_reason(process) == "chain_cap_exceeded"


@pytest.mark.parametrize("message", [
    "tool output mentioned budget_exceeded",
    "request 402 failed; tool text: budget_exceeded",
    "model said: " + denial(),
    denial().replace("402 Payment", "403 Payment"),
    denial().replace('"budget_exceeded"', '"budget_unavailable"'),
    denial(scope="other"), denial(scope="CHAIN"), denial(scope=["chain"]),
    denial(details="scope: chain"),
    'unexpected status 402 Payment Required: {"error":"budget_exceeded","details":{"scope":"run","scope":"chain"}}',
    'unexpected status 402 Payment Required: {"error":"other","error":"budget_exceeded"}',
    "unexpected status 402 Payment Required: {broken",
    denial() + "x" * 8192,
])
def test_unverified_or_ambiguous_denials_remain_failures(message):
    from lib.codex_failure import budget_stop_reason

    assert budget_stop_reason(result(stdout=failed_envelope(message))) is None


@pytest.mark.parametrize("process", [
    result(stderr=denial()),
    result(stdout=denial()),
    result(stdout=failed_envelope(denial(), "tool_result")),
    result(stdout=failed_envelope(denial()) + "\nsubsequent unrelated failure"),
    result(stdout='{"status":"agent_failed","error":{"message":123}}'),
    result(stdout="[1,2,3]"),
    result(stdout="x" * 20000),
    result(stdout=None),
    *[result(code=code, stdout=failed_envelope(denial())) for code in (0, -9, 124)],
])
def test_only_the_bounded_native_terminal_envelope_can_stop_budget(process):
    from lib.codex_failure import budget_stop_reason

    assert budget_stop_reason(process) is None
