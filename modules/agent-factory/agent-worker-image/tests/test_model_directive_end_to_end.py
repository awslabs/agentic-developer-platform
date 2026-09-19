"""PMM-07: a /model directive must reach the agent process unchanged.

This is a *handler-to-worker* regression test, not a unit test of either side.
It runs the real webhook-ingress resolution and envelope builder, hands the
resulting envelope to the real worker ``entrypoint.main()``, and asserts on the
``ANTHROPIC_MODEL`` in the environment the worker actually hands to the Node
agent subprocess. Nothing is re-implemented in the test: both halves are
imported from their shipping modules, so a change to either one that reopens the
silent-substitution gap fails here.

Why this exists (the defect it pins):
    PMM-07 added a strict, catalogue-backed resolution at the edge. Because the
    edge returned ``None`` for a model the authority had not published,
    ``entrypoint`` read that as "no /model directive was given" and fell back to
    ``ANTHROPIC_MODEL`` / its own hardcoded default -- so a user who asked for
    ``us.anthropic.claude-opus-4-6-v1`` silently ran a *different* model. The
    approved design forbids that twice over: policy may refuse a selection but
    must never substitute another one (§3 decision 1), and in ``report_only``
    the legacy assignment is still the one that executes (§8).

    The fix keeps the two answers separate -- the legacy (executed) assignment
    and the strict *proposed* one -- and only the proposed one is allowed to
    refuse. These tests assert the executed half.

Containment: every external effect is mocked (SQS receive/delete, GitHub check
runs, token minting, Vault, git/gh subprocesses, the Node agent invocation
itself). No model is ever invoked, no queue message is received or deleted for
real, and no credential is minted.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

WORKER_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = WORKER_ROOT.parents[2]
WEBHOOK_LAMBDA = REPO_ROOT / "modules/agent-factory/webhook-ingress/lambda"

sys.path.insert(0, str(WORKER_ROOT))
# The real webhook-ingress code, imported rather than copied. These two modules
# are the actual producers of the envelope the worker consumes; a test-local
# envelope literal would keep passing after the producer regressed.
sys.path.insert(0, str(WEBHOOK_LAMBDA))

from common.model_validate import (  # noqa: E402
    resolve_canonical_override,
    resolve_legacy_assignment,
)
from common.spawn_persona import _build_envelope  # noqa: E402

# The pod-level default, i.e. what the worker would run with no directive at
# all. Deliberately different from every model requested below so a
# substitution cannot hide behind a coincidental match.
POD_DEFAULT_MODEL = "global.anthropic.claude-opus-5"

SENDER = {"id": 12345678, "login": "jane-dev", "type": "User"}
PAYLOAD = {
    "issue": {"number": 42},
    "repository": {"id": 55501, "full_name": "acme-corp/flagship-app"},
}
CORRELATION_CTX = {
    "correlation_id": "corr-pmm07-1",
    "root_human_id": "cognito-sub-jane-123",
    "is_human_rooted": True,
    "parent_invocation_id": None,
    "chain_depth": 0,
}


def _webhook_envelope(requested: str | None) -> dict:
    """Build an envelope the way the real handler does for a /model directive.

    Mirrors github/handler.py step 12: the legacy assignment is what executes,
    the canonical override is the separate *proposed* answer. Keeping both calls
    here (rather than passing literals) is what makes this a handler-side test.
    """
    model_resolved = None
    model_canonical = None
    if requested:
        model_resolved = resolve_legacy_assignment(requested)
        model_canonical = resolve_canonical_override(requested)
    return _build_envelope(
        persona="developer",
        tenant_id="acme-corp",
        cognito_sub="cognito-sub-jane-123",
        actor_user_id="cognito-sub-jane-123",
        actor_org_id="org-acme",
        sender=SENDER,
        installation_id=99887766,
        repo="acme-corp/flagship-app",
        payload=PAYLOAD,
        correlation_ctx=CORRELATION_CTX,
        intent_trigger="mentioned",
        intent_label=None,
        model_requested=requested,
        model_resolved=model_resolved,
        model_canonical=model_canonical,
    )


def _subprocess_side_effect(*args, **kwargs):
    """`git ls-remote` reports no existing branch; everything else succeeds."""
    cmd = args[0] if args else kwargs.get("args", [])
    if cmd and cmd[0:2] == ["git", "ls-remote"]:
        return MagicMock(returncode=2, stdout="", stderr="")
    return MagicMock(returncode=0, stdout="", stderr="")


@pytest.fixture(autouse=True)
def hermetic_process_env():
    """Restore ``os.environ`` exactly, because the worker mutates it in place.

    ``entrypoint`` calls ``os.environ.update(env_vars)`` so the agent's
    environment also becomes the pod's. That is correct for a pod that handles
    one message and exits, but in-process it leaks ``ADP_MODEL_REQUESTED`` from
    one test into the next -- and monkeypatch cannot undo a mutation it never
    saw. Without this, "no directive given" would silently inherit the previous
    test's directive and the absence assertions would be meaningless.
    """
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@pytest.fixture(autouse=True)
def contained_worker(monkeypatch):
    """Neutralise every external effect of a worker run.

    The proxy, bootstrap logger and durable-receipt writes are replaced rather
    than allowed to reach AWS; the authority and token-broker paths stay off so
    this test covers model plumbing only.
    """
    for leaked in ("ADP_MODEL_REQUESTED", "ADP_MODEL_RESOLVED"):
        monkeypatch.delenv(leaked, raising=False)
    monkeypatch.setattr("entrypoint._start_sigv4_proxy", MagicMock())
    monkeypatch.setattr("entrypoint._stop_sigv4_proxy", MagicMock())
    monkeypatch.setattr("entrypoint.BootstrapLogger", MagicMock())
    monkeypatch.setattr("entrypoint.is_delivery_completed", MagicMock(return_value=False))
    monkeypatch.setattr("entrypoint.record_delivery_completed", MagicMock())
    # Status and provenance reporting are fail-soft by design, so an unmocked
    # run still passes -- but it makes real outbound HTTP/DDB attempts. Mock
    # them so this test performs no external mutation at all.
    monkeypatch.setattr("entrypoint.update_invocation_status", MagicMock())
    monkeypatch.setattr("entrypoint.post_provenance", MagicMock())
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "false")
    monkeypatch.setenv("ADP_GH_TOKEN_BROKER_ENABLED", "0")
    monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123456789012/q.fifo")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    # The posture under test is the actual one: report_only, no enforcement.
    monkeypatch.setenv("ADP_MODEL_POLICY_POSTURE", "report_only")


def _run_worker(envelope, monkeypatch, tmp_path, *, pod_model=POD_DEFAULT_MODEL):
    """Run the real ``entrypoint.main()`` and return the agent subprocess env.

    Returns the ``env`` mapping passed to the final ``subprocess.run`` -- the
    environment the Node agent would genuinely have started with.
    """
    import entrypoint
    from entrypoint import main

    if pod_model is None:
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_MODEL", pod_model)

    monkeypatch.setattr(entrypoint, "_setup_agent_control", lambda *_: False)
    monkeypatch.setattr("lib.run_identity.bootstrap_run_identity", lambda *_: None)

    work_dir = tmp_path / "repo"
    work_dir.mkdir(parents=True)
    monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
    monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
    monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

    with (
        patch("entrypoint._receive_one_message") as receive,
        patch("entrypoint._delete_message") as delete,
        patch("entrypoint.create_check_run") as create_cr,
        patch("entrypoint.update_check_run"),
        patch("entrypoint.run_cmd") as run_cmd,
        patch("entrypoint.mint_installation_token") as mint,
        patch("entrypoint.VaultClient") as vault_cls,
        patch("entrypoint.shutil.copytree"),
        patch("entrypoint.subprocess.run") as subprocess_run,
    ):
        receive.return_value = (json.dumps(envelope), "receipt-pmm07")
        vault_cls.return_value.get_secret.return_value = {
            "app_id": "123",
            "private_key": "k",
        }
        mint.return_value = "ghs_test"
        run_cmd.return_value = MagicMock(stdout="abc123\n", returncode=0)
        create_cr.return_value = {"id": 1, "html_url": "http://example.invalid/cr"}
        subprocess_run.side_effect = _subprocess_side_effect

        exit_code = main()

        # A clean run: the envelope was processed and acked, not dropped by the
        # poison guard. `_delete_message` is mocked, so no real queue mutation
        # happens -- but asserting the ack is what proves the worker actually
        # reached the agent launch rather than bailing out early with an env
        # that would coincidentally still hold the pod default.
        assert exit_code == 0
        assert delete.call_count == 1
        call = subprocess_run.call_args
        agent_env = call.kwargs.get("env") or call[1].get("env")
        assert agent_env is not None, "worker did not launch the agent subprocess"
        return agent_env


class TestRequestedModelReachesTheAgentUnchanged:
    """The regression: a requested model must not become a different model."""

    @pytest.mark.parametrize(
        "requested",
        [
            # Both were reproduced as silent substitutions: pattern-valid,
            # regionally routed, and absent from the published catalogue.
            "us.anthropic.claude-opus-4-6-v1",
            "eu.anthropic.claude-sonnet-4-6",
        ],
    )
    def test_unpublished_regional_request_still_executes_as_requested(
        self, requested, monkeypatch, tmp_path
    ):
        envelope = _webhook_envelope(requested)
        # The proposed answer refuses -- that is correct and must stay true.
        assert envelope.get("model_canonical") is None
        agent_env = _run_worker(envelope, monkeypatch, tmp_path)

        assert agent_env["ANTHROPIC_MODEL"] == requested
        assert agent_env["ANTHROPIC_MODEL"] != POD_DEFAULT_MODEL
        assert agent_env["ADP_MODEL_REQUESTED"] == requested
        assert agent_env["ADP_MODEL_RESOLVED"] == requested

    @pytest.mark.parametrize(
        ("requested", "expected"),
        [
            # Controls: published, and unaffected by the regression. They must
            # behave identically before and after the split, which is what
            # makes the two cases above attributable to the fix.
            ("sonnet46", "global.anthropic.claude-sonnet-4-6"),
            ("us.anthropic.claude-sonnet-4-6", "us.anthropic.claude-sonnet-4-6"),
        ],
    )
    def test_published_control_models_are_unchanged(
        self, requested, expected, monkeypatch, tmp_path
    ):
        envelope = _webhook_envelope(requested)
        # A published value proposes the same thing it executes.
        assert envelope["model_canonical"] == expected
        agent_env = _run_worker(envelope, monkeypatch, tmp_path)
        assert agent_env["ANTHROPIC_MODEL"] == expected


class TestPodDefaultAndOverridePrecedence:
    """Explicit directive > pod default; no directive leaves the pod default."""

    def test_directive_overrides_the_pod_model(self, monkeypatch, tmp_path):
        envelope = _webhook_envelope("sonnet46")
        agent_env = _run_worker(
            envelope, monkeypatch, tmp_path, pod_model="global.anthropic.claude-opus-4-8"
        )
        assert agent_env["ANTHROPIC_MODEL"] == "global.anthropic.claude-sonnet-4-6"

    def test_no_directive_keeps_the_pod_model(self, monkeypatch, tmp_path):
        envelope = _webhook_envelope(None)
        assert "model_resolved" not in envelope
        agent_env = _run_worker(
            envelope, monkeypatch, tmp_path, pod_model="global.anthropic.claude-opus-4-8"
        )
        assert agent_env["ANTHROPIC_MODEL"] == "global.anthropic.claude-opus-4-8"
        assert "ADP_MODEL_REQUESTED" not in agent_env

    def test_no_directive_and_no_pod_model_uses_the_built_in_default(
        self, monkeypatch, tmp_path
    ):
        envelope = _webhook_envelope(None)
        agent_env = _run_worker(envelope, monkeypatch, tmp_path, pod_model=None)
        assert agent_env["ANTHROPIC_MODEL"] == POD_DEFAULT_MODEL


class TestInvalidDirectiveIsLenientNotSubstituted:
    """An unusable directive falls back to the pod default *and says so*.

    The lenient fallback is pre-existing, deliberate behaviour for input that
    resolves to nothing at all (the run proceeds, and the requested value is
    exported so the agent can tell the user). What must not happen is the same
    fallback for input that *did* resolve -- that is the defect above.
    """

    @pytest.mark.parametrize(
        "requested",
        ["not-a-real-model", "gpt-4o", "meta.llama3-70b-instruct-v1:0", "opus"],
    )
    def test_unresolvable_directive_runs_the_pod_default(
        self, requested, monkeypatch, tmp_path
    ):
        envelope = _webhook_envelope(requested)
        assert "model_resolved" not in envelope
        agent_env = _run_worker(envelope, monkeypatch, tmp_path)

        assert agent_env["ANTHROPIC_MODEL"] == POD_DEFAULT_MODEL
        # The raw request is still visible to the agent so the user can be told
        # their directive was not usable.
        assert agent_env["ADP_MODEL_REQUESTED"] == requested
        assert "ADP_MODEL_RESOLVED" not in agent_env


class TestProposedResolutionNeverMovesExecution:
    """The proposal is recorded, never applied (design §3 decision 1, §8)."""

    def test_a_refused_proposal_does_not_change_the_executed_model(
        self, monkeypatch, tmp_path
    ):
        requested = "us.anthropic.claude-opus-4-6-v1"
        envelope = _webhook_envelope(requested)
        assert envelope["model_requested"] == requested
        assert envelope["model_resolved"] == requested
        assert "model_canonical" not in envelope  # refused, so nothing proposed

        agent_env = _run_worker(envelope, monkeypatch, tmp_path)
        assert agent_env["ANTHROPIC_MODEL"] == requested

    def test_a_proposal_is_not_read_as_the_execution_input(
        self, monkeypatch, tmp_path
    ):
        """Even a *present* proposal must not be what the worker executes.

        Constructed deliberately inconsistent: if the worker ever preferred
        ``model_canonical``, this would run Sonnet instead of the Opus the
        legacy assignment chose, and the substitution bug would be back by a
        different route.
        """
        envelope = _webhook_envelope("us.anthropic.claude-opus-4-6-v1")
        envelope["model_canonical"] = "global.anthropic.claude-sonnet-4-6"
        agent_env = _run_worker(envelope, monkeypatch, tmp_path)
        assert agent_env["ANTHROPIC_MODEL"] == "us.anthropic.claude-opus-4-6-v1"
