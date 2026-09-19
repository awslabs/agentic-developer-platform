"""A failed verification cannot downgrade an enforcing response into legacy."""

import pytest

from lib.run_identity import RunIdentityError
from tests.test_run_identity import identity, policy_reply, reply  # noqa: F401
from tests.test_model_directive_end_to_end import (
    _run_worker,
    _webhook_envelope,
    contained_worker,  # noqa: F401
    hermetic_process_env,  # noqa: F401
)


@pytest.mark.parametrize("outer_posture", ["report_only", "disabled", None, "future-posture"])
def test_tampered_outer_posture_cannot_launch_legacy_after_signed_enforcing_reply(identity, monkeypatch, tmp_path, outer_posture):
    session, _ = identity
    # The signed body is an enforcing decision. Change only the outer metadata,
    # which the signature does not cover, and use the real refresh/error handling.
    policy = policy_reply(runtime_posture="enforcing", reply_changes={"posture": outer_posture})
    monkeypatch.setattr(session, "_request", lambda: reply(model_policy=policy))
    try:
        session.refresh()
    except RunIdentityError:
        return  # Refusal before launch is valid.
    assert session.model_policy_report is not None
    # External token issuance is outside this policy test; identity's fixture
    # enables the protected-worker token path, so contain that path too.
    monkeypatch.setattr("entrypoint._broker_installation_token", lambda **_: ("test-token", "123", "2099-01-01T00:00:00Z"))
    envelope = _webhook_envelope("us.anthropic.claude-opus-4-6-v1")
    envelope["message_id"] = "run-a"
    envelope["tenant_id"] = "tenant"
    envelope.setdefault("correlation", {})["correlation_id"] = "chain-a"
    assert _run_worker(envelope, monkeypatch, tmp_path, policy_report=session.model_policy_report, expect_exit=1) is None
