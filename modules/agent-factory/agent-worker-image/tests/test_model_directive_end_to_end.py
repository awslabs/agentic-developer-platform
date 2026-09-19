"""PMM-07: a /model directive must reach the agent process unchanged.

This is a *handler-to-worker* regression test, not a unit test of either side.
It posts an HMAC-signed ``issue_comment`` delivery to the real
``github.handler``, lets the real intent parser find the ``/model`` line, the
real ``spawn_persona`` apply its guards and the real ``_build_envelope``
construct the message, intercepts that message at the SQS publisher, hands it to
the real worker ``entrypoint.main()``, and asserts on the ``ANTHROPIC_MODEL`` in
the environment the worker hands to the Node agent subprocess.

Nothing on that path is re-implemented or stubbed. An earlier version of this
file built the envelope by calling ``resolve_legacy_assignment`` and
``resolve_canonical_override`` itself and passing the results to
``_build_envelope`` -- which reproduced the two lines of handler code the
regression was *in*. It would have kept passing if the handler stopped calling
the resolvers, called them in the wrong order, or assigned either answer to the
wrong envelope field, because the test was making those calls rather than
observing them. Driving the signed delivery instead means the handler has to get
it right for this file to be green.

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

Containment: every external effect is mocked (identity resolution, rate-limit
and correlation storage, the invocation-event and provenance writes, the SQS
publish, then on the worker side SQS receive/delete, GitHub check runs, token
minting, Vault, git/gh subprocesses and the Node agent invocation itself). The
signature is real because it is cheap and local -- an HMAC over the body with a
test-only secret. No model is ever invoked, no queue message is sent, received
or deleted for real, and no credential is minted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

WORKER_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = WORKER_ROOT.parents[2]
WEBHOOK_LAMBDA = REPO_ROOT / "modules/agent-factory/webhook-ingress/lambda"

sys.path.insert(0, str(WORKER_ROOT))
# The real webhook-ingress code, imported rather than copied. `github/` is on the
# path as well as `lambda/` because the handler imports its sibling modules
# (`intent_parser`) by bare name, exactly as it does inside the Lambda bundle.
sys.path.insert(0, str(WEBHOOK_LAMBDA))
sys.path.insert(0, str(WEBHOOK_LAMBDA / "github"))

# A test-only HMAC key. Not a credential: it authenticates nothing outside this
# process, and the handler reads it from the env only because WEBHOOK_SECRET_ARN
# is blank (its documented local-dev fallback).
WEBHOOK_SECRET = "pmm07-local-test-secret"
os.environ.setdefault("WEBHOOK_SECRET", WEBHOOK_SECRET)
os.environ.setdefault("WEBHOOK_SECRET_ARN", "")
os.environ.setdefault(
    "SUBMIT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123456789012/adp-test-submit.fifo"
)
os.environ.setdefault("IDENTITY_INDEX_TABLE", "adp-test-identity-index")
os.environ.setdefault("RATE_LIMITS_TABLE", "adp-test-rate-limits")
os.environ.setdefault("AWS_REGION", "us-east-1")

# The pod-level default, i.e. what the worker would run with no directive at
# all. Deliberately different from every model requested below so a
# substitution cannot hide behind a coincidental match.
POD_DEFAULT_MODEL = "global.anthropic.claude-opus-5"

# What a verified gateway decision resolves to in the enforcing tests below.
# Distinct from both the pod default and every requested model, so "the decision
# was consumed" cannot be satisfied by any other code path producing a match.
GATEWAY_MODEL = "us.anthropic.claude-sonnet-4-7-v1"

SENDER = {"id": 12345678, "login": "jane-dev", "type": "User"}
REPO = "acme-corp/flagship-app"
ISSUE_NUMBER = 42
INSTALLATION_ID = 99887766


def _signed_delivery(comment_body: str) -> dict:
    """An ``issue_comment`` delivery signed the way GitHub signs one.

    The signature is computed over the exact bytes in ``body``, so the handler's
    real ``verify_github_signature`` accepts it. Verification is deliberately NOT
    mocked: the resolution under test happens after that gate, and a test that
    stubbed it could pass on a delivery the production handler would reject.
    """
    body = json.dumps(
        {
            "action": "created",
            "issue": {
                "number": ISSUE_NUMBER,
                "title": "Story: persona model mapping",
                "html_url": f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}",
            },
            "comment": {"body": comment_body},
            "repository": {"id": 55501, "full_name": REPO, "owner": {"login": "acme-corp"}},
            "installation": {"id": INSTALLATION_ID},
            "sender": SENDER,
        }
    )
    signature = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {
        "headers": {
            "x-github-event": "issue_comment",
            "content-type": "application/json",
            "x-hub-signature-256": f"sha256={signature}",
        },
        "body": body,
        "isBase64Encoded": False,
    }


def _webhook_envelope(requested: str | None) -> dict:
    """Return the envelope the **real handler** publishes for this directive.

    Drives one signed delivery through `github.handler` and captures what
    `spawn_persona` hands to the SQS publisher. The handler, the intent parser,
    the resolvers, the guards and the envelope builder are all the shipping code;
    only storage, identity and the queue are doubled.

    The `/model` line is on its own line because that is the only form the
    directive regex accepts (`^/model \\s+(\\S+)$`, MULTILINE) -- a detail worth
    exercising here rather than assuming, since a test that passed the alias
    straight to the builder never had to produce a parseable comment at all.
    """
    from common.identity_resolver import ResolvedIdentity

    comment = "@agent-developer please implement this"
    if requested:
        comment = f"{comment}\n/model {requested}"

    resolved = ResolvedIdentity(
        tenant_id="acme-corp",
        org_id="org-acme",
        user_id="cognito-sub-jane-123",
        user_provisioning_mode="strict",
        user_kind="human",
    )
    rate_decision = MagicMock()
    rate_decision.allowed = True
    rate_decision.retry_after_seconds = 0

    correlation_store = MagicMock()
    correlation_store.channel_key.side_effect = lambda provider, repo, kind, number: (
        f"{provider}:repo={repo},{kind}={number}"
    )
    correlation_store.read_pointer.return_value = {
        "correlation_id": "corr-pmm07-1",
        "triggering_invocation_id": None,
        "last_triggered_persona": None,
        "recent_triggered_personas": set(),
        "recent_trigger_count": 0,
    }

    published: dict[str, dict] = {}

    def _capture(envelope: dict) -> str:
        published["envelope"] = envelope
        return "sqs-message-pmm07"

    import handler as github_handler

    with (
        patch("handler._get_events_log") as events_log,
        patch("handler._get_rate_limiter") as rate_limiter,
        patch("handler._get_identity_resolver") as identity_resolver,
        patch("handler._get_metrics") as metrics,
        patch("handler._get_correlation_store", return_value=correlation_store),
        patch("handler._resolve_chain_record", return_value=None),
        # Durable side effects of a successful spawn. Mocked because they write to
        # DynamoDB; the envelope itself is built before any of them.
        patch("common.spawn_persona._write_pointer_and_provenance"),
        patch("common.spawn_persona._capture_invocation_event"),
        patch("common.spawn_persona._get_max_credential_chain_depth", return_value=3),
        # The one seam that stands in for the queue. Patched at its definition so
        # `spawn_persona`'s deferred import of it is intercepted too.
        patch("common.sqs_publisher.publish_envelope", side_effect=_capture),
    ):
        events_log.return_value.log_event = MagicMock()
        rate_limiter.return_value.check_and_increment.return_value = rate_decision
        identity_resolver.return_value.resolve.return_value = (resolved, "ok")
        identity_resolver.return_value.last_tenant_item = None
        metrics.return_value.record_rejected = MagicMock()
        metrics.return_value.flush = MagicMock()

        response = github_handler.handler(_signed_delivery(comment), None)

    # 202 means the delivery was authenticated, parsed, guarded and published. A
    # 200 `no_op` here would mean the mention or the directive never parsed, and
    # every downstream assertion would be vacuous.
    assert response["statusCode"] == 202, response
    assert "envelope" in published, "the handler accepted the delivery but published nothing"
    return published["envelope"]


def _subprocess_side_effect(*args, **kwargs):
    """`git ls-remote` reports no existing branch; everything else succeeds."""
    cmd = args[0] if args else kwargs.get("args", [])
    if cmd and cmd[0:2] == ["git", "ls-remote"]:
        return MagicMock(returncode=2, stdout="", stderr="")
    return MagicMock(returncode=0, stdout="", stderr="")


def _commands(subprocess_run) -> list[list[str]]:
    """Every argv this mock was asked to run, in order.

    The command is positional in every `subprocess.run` call the worker makes,
    including the agent launch itself (`subprocess.run(command, **run_options)`).
    """
    return [call.args[0] for call in subprocess_run.call_args_list if call.args]


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
    monkeypatch.setattr(
        "lib.run_service_client.own_marker_fields",
        lambda: {
            "correlation_id": "chain-a",
            "root_human_id": "human-a",
            "is_human_rooted": "true",
            "invocation_id": "run-a",
            "chain_depth": "0",
            "signature": "a" * 43,
        },
    )
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "false")
    monkeypatch.setenv("ADP_GH_TOKEN_BROKER_ENABLED", "0")
    monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123456789012/q.fifo")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    # The posture under test is the actual one: report_only, no enforcement.
    monkeypatch.setenv("ADP_MODEL_POLICY_POSTURE", "report_only")


def _run_worker(
    envelope,
    monkeypatch,
    tmp_path,
    *,
    pod_model=POD_DEFAULT_MODEL,
    policy_report=None,
    expect_exit=0,
):
    """Run the real ``entrypoint.main()`` and return the agent subprocess env.

    Returns the ``env`` mapping passed to the final ``subprocess.run`` -- the
    environment the Node agent would genuinely have started with.

    ``policy_report`` attaches a verified gateway decision to the run identity, so
    the enforcing path can be exercised through the real entrypoint. When
    ``expect_exit`` is non-zero the worker is expected to refuse before launching,
    and ``None`` is returned instead of an environment -- the assertion that the
    agent never started is the point of those cases.
    """
    import entrypoint
    from entrypoint import main

    if pod_model is None:
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_MODEL", pod_model)

    monkeypatch.setattr(entrypoint, "_setup_agent_control", lambda *_: False)
    identity = None if policy_report is None else SimpleNamespace(model_policy_report=policy_report)
    monkeypatch.setattr("lib.run_identity.bootstrap_run_identity", lambda *_: identity)

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

        if expect_exit != 0:
            # The refusal path: the worker must stop before the harness starts.
            # Asserting on ``subprocess.run`` is what proves no agent was ever
            # launched -- checking only the exit code would pass even if the
            # model had already been invoked.
            #
            # Not `call_args is None`: this same mock also serves the `gh pr list`
            # idempotency probe and the `git ls-remote` branch check, which run
            # legitimately *before* step 4 where the policy is consumed. Asserting
            # zero calls therefore failed for the wrong reason, and "loosen it to
            # any call" would have asserted nothing at all. The property that
            # actually matters is narrower and exact: no invocation of the agent
            # harness itself, which `worker_command` always starts with `node`.
            assert exit_code == expect_exit
            launches = [cmd for cmd in _commands(subprocess_run) if cmd[:1] == ["node"]]
            assert launches == [], f"worker launched the agent despite refusing: {launches}"
            return None

        # A clean run: the envelope was processed and acked, not dropped by the
        # poison guard. `_delete_message` is mocked, so no real queue mutation
        # happens -- but asserting the ack is what proves the worker actually
        # reached the agent launch rather than bailing out early with an env
        # that would coincidentally still hold the pod default.
        assert exit_code == 0
        assert delete.call_count == 1
        # Select the agent launch explicitly rather than trusting the last call:
        # the same mock serves the pre-launch `gh`/`git` probes, so "the most
        # recent call" is only incidentally the harness and would start reading
        # some other command's environment the moment ordering changed.
        launches = [
            call
            for call in subprocess_run.call_args_list
            if call.args and call.args[0][:1] == ["node"]
        ]
        assert len(launches) == 1, f"expected exactly one agent launch, got {len(launches)}"
        agent_env = launches[0].kwargs.get("env")
        assert agent_env is not None, "worker launched the agent with no explicit environment"
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

    def test_no_directive_and_no_pod_model_uses_the_built_in_default(self, monkeypatch, tmp_path):
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
    def test_unresolvable_directive_runs_the_pod_default(self, requested, monkeypatch, tmp_path):
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

    def test_a_refused_proposal_does_not_change_the_executed_model(self, monkeypatch, tmp_path):
        requested = "us.anthropic.claude-opus-4-6-v1"
        envelope = _webhook_envelope(requested)
        assert envelope["model_requested"] == requested
        assert envelope["model_resolved"] == requested
        assert "model_canonical" not in envelope  # refused, so nothing proposed

        agent_env = _run_worker(envelope, monkeypatch, tmp_path)
        assert agent_env["ANTHROPIC_MODEL"] == requested

    def test_a_proposal_is_not_read_as_the_execution_input(self, monkeypatch, tmp_path):
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


class TestEnforcingPostureIsConsumedByTheActualEntrypoint:
    """PMM-07: the real entrypoint, not a ConfigLoader stand-in.

    ``enforcing`` is supported in source so the path is provable end to end. No
    configured environment is set to it -- the autouse fixture above keeps the
    actual posture at ``report_only`` -- and each test here constructs the posture
    locally. Every inference and network effect is mocked; no model is invoked.
    """

    @staticmethod
    def _report(**changes):
        from lib.run_identity import ModelPolicyReport

        return ModelPolicyReport(
            **{
                "status": "proposed",
                "posture": "enforcing",
                "posture_verified": True,
                "resolved_model_id": GATEWAY_MODEL,
                "resolution_source": "principal-mapping",
                "snapshot_digest": "b" * 64,
                "policy_revision": "policy-7",
                "catalogue_revision": "catalogue-4",
                "allowlist_policy_drift": False,
                "posture_revision": 7,
                "assertion_key_id": "policy-key",
                **changes,
            }
        )

    def test_bootstrap_does_not_replace_the_legacy_input_before_fresh_sdk_admission(
        self, monkeypatch, tmp_path
    ):
        """A rollback during setup must still recover the original legacy model.

        Actual enforcing substitution is tested at createPolicyQuery and the
        resilientQuery retry boundary, after a fresh online decision.
        """
        envelope = _webhook_envelope("us.anthropic.claude-opus-4-6-v1")
        agent_env = _run_worker(envelope, monkeypatch, tmp_path, policy_report=self._report())
        assert agent_env["ANTHROPIC_MODEL"] == "us.anthropic.claude-opus-4-6-v1"
        assert agent_env["ADP_MODEL_POLICY_POSTURE"] == "enforcing"
        assert agent_env["ADP_MODEL_POLICY_ENFORCED"] == "false"
        assert agent_env["ADP_MODEL_POLICY_EXECUTION_PENDING"] == "true"
        assert agent_env["ADP_MODEL_REQUESTED"] == "us.anthropic.claude-opus-4-6-v1"

    @pytest.mark.parametrize("posture", ["disabled", "report_only"])
    def test_non_enforcing_postures_leave_the_legacy_model_untouched(
        self, monkeypatch, tmp_path, posture
    ):
        """The actual configured behaviour: the decision is recorded, not applied.

        Same gateway decision as the enforcing test, differing only in posture, so
        this isolates the posture as the single thing that changes execution.
        """
        envelope = _webhook_envelope("us.anthropic.claude-opus-4-6-v1")
        agent_env = _run_worker(
            envelope, monkeypatch, tmp_path, policy_report=self._report(posture=posture)
        )

        assert agent_env["ANTHROPIC_MODEL"] == "us.anthropic.claude-opus-4-6-v1"
        assert agent_env["ANTHROPIC_MODEL"] != GATEWAY_MODEL
        assert agent_env["ADP_MODEL_POLICY_POSTURE"] == posture
        assert agent_env["ADP_MODEL_POLICY_ENFORCED"] == "false"
        # The proposal is still recorded as comparison evidence for PMM-08.
        assert agent_env["ADP_MODEL_POLICY_PROPOSED_MODEL"] == GATEWAY_MODEL

    @pytest.mark.parametrize(
        "report_changes",
        [
            {"status": "unavailable", "reason": "evidence_stale", "resolved_model_id": None},
            {"posture_verified": False},
            {"resolved_model_id": None},
        ],
        ids=["unavailable-decision", "unverified-posture", "no-resolved-model"],
    )
    def test_an_unsatisfiable_enforcing_posture_never_launches_the_agent(
        self, monkeypatch, tmp_path, report_changes
    ):
        """The anti-bypass property, proven against the real launch path.

        Each case is an enforcing posture the gateway could not satisfy. The run
        must stop *before* the harness starts -- it must not fall back to the
        legacy assignment, which is what exception handling or a default
        substitution would silently do. ``_run_worker`` asserts the agent
        subprocess was never invoked.
        """
        envelope = _webhook_envelope("us.anthropic.claude-opus-4-6-v1")

        assert (
            _run_worker(
                envelope,
                monkeypatch,
                tmp_path,
                policy_report=self._report(**report_changes),
                expect_exit=1,
            )
            is None
        )

    def test_editable_environment_variables_cannot_force_enforcement(self, monkeypatch, tmp_path):
        """Posture comes from the verified decision, never from the environment.

        ``ADP_MODEL_POLICY_*`` are telemetry this worker writes. Anything in the
        pod can set them, so honouring them as input would let a compromised or
        misconfigured pod both force and defeat enforcement. Here the environment
        screams ``enforcing`` while the verified report says ``report_only``: the
        report must win, and the legacy model must run.
        """
        monkeypatch.setenv("ADP_MODEL_POLICY_POSTURE", "enforcing")
        monkeypatch.setenv("ADP_MODEL_POLICY_ENFORCED", "true")
        monkeypatch.setenv("ADP_MODEL_POLICY_PROPOSED_MODEL", GATEWAY_MODEL)
        envelope = _webhook_envelope("us.anthropic.claude-opus-4-6-v1")

        agent_env = _run_worker(
            envelope,
            monkeypatch,
            tmp_path,
            policy_report=self._report(posture="report_only"),
        )

        assert agent_env["ANTHROPIC_MODEL"] == "us.anthropic.claude-opus-4-6-v1"
        # The telemetry is overwritten with the truth, not inherited from the pod.
        assert agent_env["ADP_MODEL_POLICY_POSTURE"] == "report_only"
        assert agent_env["ADP_MODEL_POLICY_ENFORCED"] == "false"

    def test_no_gateway_report_at_all_preserves_the_legacy_model(self, monkeypatch, tmp_path):
        """Mixed-version: an older gateway sends no policy, and nothing changes."""
        envelope = _webhook_envelope("us.anthropic.claude-opus-4-6-v1")
        agent_env = _run_worker(envelope, monkeypatch, tmp_path)

        assert agent_env["ANTHROPIC_MODEL"] == "us.anthropic.claude-opus-4-6-v1"
        assert "ADP_MODEL_POLICY_ENFORCED" not in agent_env


@pytest.fixture
def gateway_session(tmp_path, monkeypatch):
    """A real ``RunIdentitySession`` wired to the signed-reply helpers.

    Reuses ``test_run_identity``'s fixture rather than re-declaring the endpoint,
    key material and envelope here: those are the real gateway response shape, and
    a second copy would drift from it. Named distinctly because ``identity`` is
    already a local in this module.
    """
    from tests.test_run_identity import identity as _identity

    yield from _identity.__wrapped__(tmp_path, monkeypatch)


class TestUnsignedPostureCannotDowngradeASignedDecision:
    """An unverifiable response is never permission to run the legacy model.

    The gateway signs the *decision body*, which contains ``runtime_posture``. The
    outer ``posture`` summary beside it is not covered by that signature. So a
    response can be internally inconsistent, and the worker's own verification
    rejects it -- but it must not then read the enforcement setting back out of the
    response it just rejected.

    The defect this pins: ``_unconsumable_report`` salvaged that unsigned outer
    posture unconditionally. A correctly signed *enforcing* decision whose outer
    posture said ``report_only``, ``disabled``, nothing at all, or an unrecognised
    word produced a report with no enforcement failure, and the real entrypoint
    exited 0 having launched the agent on its legacy model. Anyone able to alter
    the response could therefore strip enforcement by editing one unsigned word --
    exception handling converting an enforcing failure into legacy execution, which
    is the exact bypass this stage exists to close.

    Driven through the real ``RunIdentitySession.refresh()`` error handling and the
    real ``entrypoint.main()``, because the property is "the run stops before
    inference": only asserting that the agent subprocess was never invoked can tell
    refusing apart from proceeding. Inspecting a variable cannot.
    """

    @pytest.mark.parametrize(
        "outer_posture",
        ["report_only", "disabled", None, "future-posture"],
        ids=["claims-report-only", "claims-disabled", "claims-nothing", "claims-unknown"],
    )
    def test_tampered_outer_posture_refuses_instead_of_launching_legacy(
        self, gateway_session, monkeypatch, tmp_path, outer_posture
    ):
        from lib.run_identity import RunIdentityError
        from tests.test_run_identity import policy_reply, reply

        session, _ = gateway_session
        # The signed body is an enforcing decision for this run/tenant/chain. Only
        # the outer metadata -- which the signature does not cover -- is changed.
        policy = policy_reply(runtime_posture="enforcing", reply_changes={"posture": outer_posture})
        monkeypatch.setattr(session, "_request", lambda: reply(model_policy=policy))
        try:
            session.refresh()
        except RunIdentityError:
            return  # Refusing at refresh is also a valid outcome.

        report = session.model_policy_report
        assert report is not None
        # The unsigned claim is not believed in either direction: the worker does
        # not conclude "report_only" from it, and does not conclude "enforcing"
        # either -- it records that the posture could not be determined.
        assert report.verification_failed is True
        assert report.posture is None
        assert report.enforcement_failure is not None
        assert report.enforced is False

        monkeypatch.setattr(
            "entrypoint._broker_installation_token",
            lambda **_: ("test-token", "123", "2099-01-01T00:00:00Z"),
        )
        envelope = _webhook_envelope("us.anthropic.claude-opus-4-6-v1")
        envelope["message_id"] = "run-a"
        envelope["tenant_id"] = "tenant"
        envelope.setdefault("correlation", {})["correlation_id"] = "chain-a"
        assert (
            _run_worker(
                envelope,
                monkeypatch,
                tmp_path,
                policy_report=report,
                expect_exit=1,
            )
            is None
        )

    def test_an_authenticated_but_unexecutable_decision_keeps_its_signed_posture(
        self, gateway_session, monkeypatch, tmp_path
    ):
        """The one case where the posture *may* be believed, and must be.

        A newer gateway's decision with an unsupported ``schema_version`` is
        rejected only *after* its assertion verified, so its ``runtime_posture`` is
        signed platform state rather than an unsigned claim. Believing it is what
        keeps mixed-version report-only rollout behaving exactly as it does today:
        the legacy model runs and nothing is blocked.

        This is the boundary that stops the fix above from over-reaching into
        ordinary unavailability -- without it, every skewed-but-honest gateway
        response would stop runs while the platform was merely observing.
        """
        from tests.test_run_identity import policy_reply, reply

        session, _ = gateway_session
        monkeypatch.setattr(
            session, "_request", lambda: reply(model_policy=policy_reply(schema_version=2))
        )
        session.refresh()

        report = session.model_policy_report
        assert report.verification_failed is False
        assert report.posture == "report_only"
        assert report.reason == "snapshot_unsupported_revision"
        assert report.enforcement_failure is None

        monkeypatch.setattr(
            "entrypoint._broker_installation_token",
            lambda **_: ("test-token", "123", "2099-01-01T00:00:00Z"),
        )
        envelope = _webhook_envelope("us.anthropic.claude-opus-4-6-v1")
        agent_env = _run_worker(envelope, monkeypatch, tmp_path, policy_report=report)
        assert agent_env["ANTHROPIC_MODEL"] == "us.anthropic.claude-opus-4-6-v1"

    def test_the_same_skew_under_a_signed_enforcing_posture_still_refuses(
        self, gateway_session, monkeypatch, tmp_path
    ):
        """Authenticating the posture must not become a way to proceed.

        Same authenticated-but-unexecutable response as above, except the signed
        posture is ``enforcing``. Believing the posture is correct here too -- and
        believing it means refusing, not admitting.
        """
        from tests.test_run_identity import policy_reply, reply

        session, _ = gateway_session
        monkeypatch.setattr(
            session,
            "_request",
            lambda: reply(model_policy=policy_reply(schema_version=2, runtime_posture="enforcing")),
        )
        session.refresh()

        report = session.model_policy_report
        assert report.verification_failed is False
        assert report.posture == "enforcing"
        # ``posture_verified`` stays False because that flag is itself unsigned --
        # the signed body proves what the posture *was*, not that the gateway
        # proved it against committed state. So the refusal lands on
        # ``posture_unverified``, which is the stricter of the two available
        # reasons and refuses for the right cause either way.
        assert report.posture_verified is False
        assert report.enforcement_failure == "posture_unverified"

        monkeypatch.setattr(
            "entrypoint._broker_installation_token",
            lambda **_: ("test-token", "123", "2099-01-01T00:00:00Z"),
        )
        envelope = _webhook_envelope("us.anthropic.claude-opus-4-6-v1")
        assert (
            _run_worker(envelope, monkeypatch, tmp_path, policy_report=report, expect_exit=1)
            is None
        )


def test_worker_passes_envelope_dispatch_context_to_fresh_sdk_telemetry(
    contained_worker, hermetic_process_env, monkeypatch, tmp_path
):
    envelope = _webhook_envelope(None)
    launched = _run_worker(envelope, monkeypatch, tmp_path)
    assert launched["ADP_DISPATCH_CHANNEL"] == envelope["channel"]
    assert launched["ADP_DISPATCH_TRIGGER"] == envelope["intent"]["trigger"]
