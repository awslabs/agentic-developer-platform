"""The one probe — Issue #4745 (#4692 · R4), §5.0, §6.7 items 1–3.

  P1  the invoke verdict table: which AWS error means what
  P2  both probes must pass, and the cheap one runs first
  P3  the probe never raises into its caller
  P4  the probe body cannot bill anybody
  P5  the model id is an inference profile, and not the EOL one

P1 is the load-bearing part. The invoke probe's entire correctness is the mapping
from error code to conclusion, and it is the part most likely to be "tidied" into
``except ClientError: return False`` — which would report a working destination as
lacking Bedrock permission, because the *success* signal here is an error.

P5 pins two constants that look like arbitrary choices and are not. Both were real
dead ends during dev validation: a bare foundation-model id fails for a role whose
policy covers only inference profiles, and the EOL haiku id returns
``ResourceNotFoundException`` — which reads exactly like a permissions failure and
sends the reader to debug IAM that is already correct.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import ClientError

from src.internal.sts_assume_service import STSAssumeError
from src.proxy.bedrock_routing_errors import (
    REASON_ASSUME_ROLE_FAILED,
    REASON_ROLE_MISSING_BEDROCK_PERMISSION,
)
from src.shared.services import routing_probe

ROLE_ARN = "arn:aws:iam::111111114821:role/ADP-Agent-acme-prod"

PROBE_KWARGS = {
    "role_arn": ROLE_ARN,
    "external_id": "ext-probe",
    "default_region": "us-east-1",
    "user_id": "47450000-0000-4000-8000-000000000003",
    "label": "acme-prod",
}


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "probe"}}, "InvokeModel")


# ===========================================================================
# P1 — the verdict table
# ===========================================================================


@pytest.mark.parametrize("code", ["AccessDeniedException", "AccessDenied", "UnauthorizedOperation"])
def test_p1_an_iam_denial_means_not_capable(code):
    """§5.0: the role assumes fine and cannot invoke Bedrock — the inert mapping.

    Every role the *original* connect flow created attached only ``ReadOnlyAccess``,
    which excludes ``bedrock:InvokeModel``. Those roles pass a naive assume-only gate
    and fail every model call, which is the #4511 defect class. The reason code is R3's
    so the admin sees the same name at save time and at runtime.
    """
    capable, reason = routing_probe._invoke_probe_verdict(code, ROLE_ARN)

    assert capable is False
    assert reason == REASON_ROLE_MISSING_BEDROCK_PERMISSION


def test_p1b_a_validation_error_is_the_success_signal():
    """The counter-intuitive one: an error means the probe PASSED.

    IAM is evaluated before the request body, so reaching body validation proves the
    call was permitted. It is the only pass signal available without generating
    billable tokens — which is why the probe can be re-run on demand (§6.7 item 5).
    """
    capable, reason = routing_probe._invoke_probe_verdict("ValidationException", ROLE_ARN)

    assert capable is True
    assert reason is None


@pytest.mark.parametrize("code", ["ResourceNotFoundException", "ThrottlingException", "ServiceUnavailable", ""])
def test_p1c_anything_else_is_inconclusive_not_a_denial(code):
    """Inconclusive gets its own reason, because the remediation differs.

    ``ResourceNotFoundException`` means the model is absent from that account or
    region and says nothing whatever about IAM. Filing it as
    ``role_missing_bedrock_permission`` would send an operator to fix a permission
    policy that is already correct — the exact trap the dev-validation notes hit.
    Still *not capable*, though: nothing gets routed to an unproven role.
    """
    capable, reason = routing_probe._invoke_probe_verdict(code, ROLE_ARN)

    assert capable is False
    assert reason == routing_probe.ROUTING_REASON_PROBE_INCONCLUSIVE


def test_p1d_inconclusive_is_distinguishable_from_a_denial():
    """The two failures must not collapse into one code.

    "Re-run the v2 template" and "re-run the probe" are different instructions, and a
    single generic failure reason cannot express either.
    """
    denied = routing_probe._invoke_probe_verdict("AccessDeniedException", ROLE_ARN)[1]
    unknown = routing_probe._invoke_probe_verdict("ResourceNotFoundException", ROLE_ARN)[1]

    assert denied != unknown


# ===========================================================================
# P2 — both probes, cheap one first
# ===========================================================================


async def test_p2_a_destination_is_routable_only_if_both_probes_pass(monkeypatch):
    """§6.7 item 3: assumable AND able to invoke."""
    monkeypatch.setattr(routing_probe, "probe_assumable_for_any_principal", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(routing_probe, "probe_bedrock_invoke", AsyncMock(return_value=(True, None)))

    assert await routing_probe.probe_routing_destination(**PROBE_KWARGS) == (True, None)


async def test_p2b_a_failed_assume_short_circuits_the_invoke_probe(monkeypatch):
    """Not merely an optimisation — it is why the reason code is right.

    A role that cannot be assumed for an arbitrary principal has nothing to invoke
    *with*, so running the invoke probe anyway would return a second, misleading
    reason ("missing Bedrock permission") for a cause that is entirely the trust
    policy.
    """
    invoke = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(
        routing_probe,
        "probe_assumable_for_any_principal",
        AsyncMock(return_value=(False, routing_probe.ROUTING_REASON_USER_PINNED)),
    )
    monkeypatch.setattr(routing_probe, "probe_bedrock_invoke", invoke)

    capable, reason = await routing_probe.probe_routing_destination(**PROBE_KWARGS)

    assert (capable, reason) == (False, routing_probe.ROUTING_REASON_USER_PINNED)
    invoke.assert_not_awaited()


async def test_p2c_an_assumable_role_that_cannot_invoke_is_refused(monkeypatch):
    """The whole reason R4 added a second probe. A v2 role with ReadOnlyAccess."""
    monkeypatch.setattr(routing_probe, "probe_assumable_for_any_principal", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(
        routing_probe,
        "probe_bedrock_invoke",
        AsyncMock(return_value=(False, REASON_ROLE_MISSING_BEDROCK_PERMISSION)),
    )

    capable, reason = await routing_probe.probe_routing_destination(**PROBE_KWARGS)

    assert capable is False
    assert reason == REASON_ROLE_MISSING_BEDROCK_PERMISSION


async def test_p2d_the_v1_pin_is_reported_as_needing_the_v2_template(monkeypatch):
    """#4742's classifier, reached through the combined entry point.

    An untagged assume denied means the v1 trust policy's ``aws:RequestTag`` condition
    is still there — the account must re-run the v2 template before any team or org
    rule can name it.
    """

    def _denied(**kwargs):
        raise STSAssumeError("STS AssumeRole failed: AccessDenied", code="AccessDenied")

    monkeypatch.setattr(routing_probe, "assume_role", _denied)

    capable, reason = await routing_probe.probe_assumable_for_any_principal(**PROBE_KWARGS)

    assert capable is False
    assert reason == routing_probe.ROUTING_REASON_USER_PINNED


async def test_p2e_the_assume_probe_sends_no_session_tags(monkeypatch):
    """The probe IS the untagged assume — tags would make it test nothing.

    With session tags, a v1 role succeeds too, and every destination would classify
    as routing-capable. The kwarg is the entire experiment.
    """
    captured = {}

    def _assume(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(routing_probe, "assume_role", _assume)

    await routing_probe.probe_assumable_for_any_principal(**PROBE_KWARGS)

    assert captured["send_session_tags"] is False


# ===========================================================================
# P3 — never raise into the caller
# ===========================================================================


async def test_p3_a_failed_assume_inside_the_invoke_probe_reports_the_assume_reason(monkeypatch):
    """The remediation is the trust policy, not the permission policy.

    Conflating the two sends the admin to edit an IAM policy that is fine while the
    trust relationship stays broken.
    """

    def _denied(**kwargs):
        raise STSAssumeError("STS AssumeRole failed: AccessDenied", code="AccessDenied")

    monkeypatch.setattr(routing_probe, "assume_role", _denied)

    capable, reason = await routing_probe.probe_bedrock_invoke(**PROBE_KWARGS)

    assert capable is False
    assert reason == REASON_ASSUME_ROLE_FAILED


async def test_p3b_a_non_client_error_does_not_escape(monkeypatch):
    """A probe must never turn into a 500 on the admin's PUT.

    A DNS failure or a botocore config problem is not evidence about the destination,
    so it reports inconclusive — the request gets a 422 explaining the probe could not
    reach a verdict rather than an unhandled exception.
    """
    monkeypatch.setattr(routing_probe, "assume_role", lambda **kwargs: _FakeCredentials())
    monkeypatch.setattr(routing_probe.boto3, "client", _raising_client(OSError("dns")))

    capable, reason = await routing_probe.probe_bedrock_invoke(**PROBE_KWARGS)

    assert capable is False
    assert reason == routing_probe.ROUTING_REASON_PROBE_INCONCLUSIVE


async def test_p3c_the_probe_reads_the_error_code_out_of_a_real_client_error(monkeypatch):
    """End to end through botocore's error shape, not just the helper.

    ``exc.response["Error"]["Code"]`` is the coupling between the verdict table and
    boto3; a table that is right on strings and wrong on where it reads them from
    would classify every destination as inconclusive.
    """
    monkeypatch.setattr(routing_probe, "assume_role", lambda **kwargs: _FakeCredentials())
    monkeypatch.setattr(routing_probe.boto3, "client", _raising_client(_client_error("ValidationException")))

    assert await routing_probe.probe_bedrock_invoke(**PROBE_KWARGS) == (True, None)


async def test_p3d_a_missing_error_code_is_inconclusive_not_a_crash(monkeypatch):
    """A malformed error response must not KeyError its way into the caller."""
    error = ClientError({}, "InvokeModel")
    monkeypatch.setattr(routing_probe, "assume_role", lambda **kwargs: _FakeCredentials())
    monkeypatch.setattr(routing_probe.boto3, "client", _raising_client(error))

    capable, reason = await routing_probe.probe_bedrock_invoke(**PROBE_KWARGS)

    assert capable is False
    assert reason == routing_probe.ROUTING_REASON_PROBE_INCONCLUSIVE


async def test_p3e_the_invoke_probe_targets_the_destinations_region(monkeypatch):
    """A role may be permitted in one region and not another.

    Probing the gateway's region would verify a path the routed calls never take.
    """
    captured = {}

    def _client(service, **kwargs):
        captured["service"] = service
        captured.update(kwargs)
        raise _client_error("ValidationException")

    monkeypatch.setattr(routing_probe, "assume_role", lambda **kwargs: _FakeCredentials(region="eu-west-1"))
    monkeypatch.setattr(routing_probe.boto3, "client", _client)

    await routing_probe.probe_bedrock_invoke(**{**PROBE_KWARGS, "default_region": "us-east-1"})

    assert captured["service"] == "bedrock-runtime"
    assert captured["region_name"] == "eu-west-1"


# ===========================================================================
# P4 / P5 — the probe cannot bill anybody, and names a model that exists
# ===========================================================================


def test_p4_the_probe_body_is_invalid_on_purpose():
    """No ``messages``, no ``anthropic_version`` — so no tokens on any branch.

    A valid body would make the probe a billable model call run on somebody else's
    account every time an admin saves a rule or clicks re-verify.
    """
    import json

    body = json.loads(routing_probe._PROBE_BODY)

    assert "messages" not in body
    assert "anthropic_version" not in body


def test_p4b_the_probe_does_not_retry_and_times_out_quickly():
    """It is a save-time gate on an interactive request.

    Botocore's default retries would hold an admin's PUT open for a minute or more
    against an unreachable account, and retrying tells us nothing new about IAM.
    """
    assert routing_probe._PROBE_MAX_ATTEMPTS == 1
    assert routing_probe._PROBE_TIMEOUT_SECONDS <= 10

    config = routing_probe._probe_client_config()
    assert config.retries["max_attempts"] == 1
    assert config.read_timeout == routing_probe._PROBE_TIMEOUT_SECONDS


def test_p5_the_probe_model_is_an_inference_profile():
    """Invocation goes through ``us.anthropic.*`` profiles, not bare model ARNs.

    A role whose policy covers only ``foundation-model/*`` would be denied on the
    shape the gateway actually uses while the probe reported it fine — which is why
    R1's v2 template covers ``inference-profile/*``.
    """
    assert routing_probe._PROBE_MODEL_ID.startswith("us.anthropic.")


def test_p5b_the_probe_model_is_not_the_end_of_life_one():
    """``claude-3-5-haiku-20241022`` is EOL here and returns ResourceNotFoundException.

    Which the verdict table correctly calls inconclusive — meaning every destination
    would fail verification with a reason that reads like an IAM problem. Pinned as a
    test because the id is a plausible-looking thing to swap in for a "cheaper" probe,
    and the resulting failure blames the wrong subsystem.
    """
    assert "claude-3-5-haiku-20241022" not in routing_probe._PROBE_MODEL_ID


def test_p5c_the_reason_vocabulary_is_r3s(monkeypatch):
    """§6.7 item 2: authoring-time and runtime failures speak the same names.

    An admin who sees ``assume_role_failed`` in a save refusal and
    ``assume_role_failed`` in a 502 can connect them. A private vocabulary here would
    make the same cause look like two unrelated bugs.
    """
    from src.proxy import bedrock_routing_errors

    shared = {value for value in vars(bedrock_routing_errors).values() if isinstance(value, str)}

    assert REASON_ROLE_MISSING_BEDROCK_PERMISSION in shared
    assert REASON_ASSUME_ROLE_FAILED in shared


def test_the_connect_route_delegates_to_this_module():
    """§6.7 item 1: *"do not write a second assume probe."*

    #4742 shipped the classifier inside the connect-verify route; R4 needs the same
    check at save time. Asserting identity — not equivalent behaviour — is what keeps
    a future edit to one path from silently applying to only one of the two callers.
    """
    from src.auth import aws_connect_routes

    assert aws_connect_routes._probe_routing_capability is routing_probe.probe_assumable_for_any_principal
    assert aws_connect_routes.ROUTING_REASON_USER_PINNED is routing_probe.ROUTING_REASON_USER_PINNED


class _FakeCredentials:
    """Minimal stand-in for ``sts_assume_service``'s assumed-credentials object."""

    def __init__(self, region: str = "us-east-1"):
        self.region = region
        self.access_key_id = "AKIAPROBE"
        self.secret_access_key = "probe-secret-not-real"
        self.session_token = "probe-token-not-real"


def _raising_client(error: Exception):
    """A ``boto3.client`` replacement whose ``invoke_model`` raises ``error``."""

    class _Client:
        def invoke_model(self, **kwargs):
            raise error

    def _factory(service, **kwargs):
        if isinstance(error, OSError):
            raise error
        return _Client()

    return _factory
