"""The one probe that decides whether a role may serve routed Bedrock calls.

Issue #4745 (#4692 · R4 · routing admin surface), per the design note
``docs/design-notes/4692-per-principal-bedrock-account-routing.md`` §5.0, §5.0b,
§6.7.

**This module exists so that there is exactly one probe.** §6.7 item 1 is explicit:
*"Reuse it; do not write a second assume probe. Two probes with different conditions
is how 'verified here, broken there' happens."* The v1/v2 classifier shipped in
#4742 inside ``src/auth/aws_connect_routes.py`` because the connect-verify flow was
its only caller. R4 adds a second caller — the mapping-authoring API, which must run
the same check at save time — so the probe moves here and
``aws_connect_routes._probe_routing_capability`` becomes a delegate. Nothing was
copied; a copy is the failure mode the design names.

Two independent properties are probed, and **both** are required before a
destination may serve a routed call. They fail for different reasons and have
different remediations, which is why they are two functions returning two reason
codes rather than one boolean:

**1. Is the role assumable on behalf of someone other than its creator?**
(:func:`probe_assumable_for_any_principal`, unchanged from #4742.) v1's trust policy
conditions on ``aws:RequestTag/adp:user_id``, so an *untagged* assume is denied; v2
drops that condition, so it succeeds. Repeating the assume without session tags is a
direct read of exactly that property — no template self-report to trust, no extra
IAM permission needed.

**2. May the role actually invoke Bedrock?** (:func:`probe_bedrock_invoke`, new
here.) §5.0 is a blocker precisely because every role the *original* connect flow
created attached only ``ReadOnlyAccess``, which excludes ``bedrock:InvokeModel``.
Such a role assumes perfectly and fails every model call — the inert mapping of the
#4511 class, which passes a naive gate. §6.7 item 3 requires this check at authoring
time for that reason.

**How the invoke check avoids billing anyone.** It calls ``invoke_model`` with a
deliberately malformed body and reads which error comes back, because IAM is
evaluated *before* the request body is:

===================================== ==========================================
Response                              Conclusion
===================================== ==========================================
``AccessDeniedException``             IAM refused. NOT capable.
``ValidationException``               IAM **allowed** it; only the body was bad.
                                      Capable.
``ResourceNotFoundException``         The model id is not available in that
                                      account/region. Says nothing about IAM —
                                      inconclusive, not a denial. (See the model
                                      constant below; this is the exact trap the
                                      #4745 dev-validation notes warned about.)
anything else                         Inconclusive.
===================================== ==========================================

No tokens are generated on any branch, so the check is free and can be re-run on
demand (§6.7 item 5).

**Failure is never silent and never optimistic.** Every inconclusive outcome
returns *not capable* with a reason distinguishing it from a real denial, so an
operator can tell "re-run the v2 template" from "re-run the probe". Defaulting the
other way would advertise an unproven role as a usable destination, which under
fail-closed (§2.5) is an outage waiting for its first request.

The reason codes are **R3's** (:mod:`src.proxy.bedrock_routing_errors`), not a
private vocabulary. §6.7 item 2 requires the authoring-time failure and the runtime
failure to speak the same names, so an admin who sees ``assume_role_failed`` at save
and ``assume_role_failed`` at runtime can connect them.
"""

from __future__ import annotations

import asyncio
import json
import logging

import boto3
from botocore.exceptions import ClientError

from src.internal.sts_assume_service import STSAssumeError, assume_role
from src.proxy.bedrock_routing_errors import (
    REASON_ASSUME_ROLE_FAILED,
    REASON_ROLE_MISSING_BEDROCK_PERMISSION,
)

logger = logging.getLogger("bedrockgateway.routing.probe")

#: Machine-readable reason a verified connection is not usable as a Bedrock routing
#: destination. Stable vocabulary — the admin UI renders these. Kept as R1 (#4742)
#: defined them so that module's callers and tests are unaffected by the move.
ROUTING_REASON_USER_PINNED = "role_user_pinned_needs_v2_template"
ROUTING_REASON_PROBE_INCONCLUSIVE = "routing_probe_inconclusive"

#: The model the invoke probe names. It is never actually invoked successfully — the
#: body is deliberately malformed — but the id must still be one that EXISTS in the
#: destination, or the call fails ``ResourceNotFoundException`` before IAM's verdict
#: is observable and the probe learns nothing.
#:
#: An **inference profile** id (``us.anthropic.*``), not a bare foundation-model id:
#: invocation in this organization goes through inference profiles, and a role whose
#: policy covers only ``foundation-model/*`` fails for the shape the gateway
#: actually uses. R1's v2 template covers ``inference-profile/*`` for this reason.
#:
#: Do NOT change this to ``us.anthropic.claude-3-5-haiku-20241022-v1:0``: that model
#: is end-of-life in this organization and returns ``ResourceNotFoundException``,
#: which reads exactly like a permissions failure and sends the reader to debug IAM
#: that is in fact correct.
_PROBE_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

#: Deliberately invalid: no ``messages``, no ``anthropic_version``. Bedrock validates
#: IAM first and the body second, so this reaches an ``AccessDeniedException`` when
#: the role cannot invoke and a ``ValidationException`` when it can — and generates
#: no tokens either way.
_PROBE_BODY = json.dumps({"adp_routing_probe": True})

#: The probe is a save-time gate on an interactive request. A role whose account is
#: unreachable must fail the gate quickly rather than hold the admin's PUT open.
_PROBE_TIMEOUT_SECONDS = 10
_PROBE_MAX_ATTEMPTS = 1


async def probe_assumable_for_any_principal(
    *,
    role_arn: str,
    external_id: str | None,
    default_region: str,
    user_id: str,
    label: str,
) -> tuple[bool, str | None]:
    """Classify a verified role as routing-capable (v2) or single-user-pinned (v1).

    Issue #4742, moved here verbatim by #4745. The property that matters for routing
    is not "which template did you launch" — we cannot see that from here, and a
    self-reported version would be a guess. It is the *behaviour*: can this role be
    assumed on behalf of someone other than whoever created the stack?

    We test it directly by repeating the assume **without session tags**. The v1
    trust policy conditions on ``aws:RequestTag/adp:user_id``, so an untagged assume
    is denied; v2 drops that condition, so it succeeds. That single call is a
    definitive read of the exact property, needs no extra IAM permission, and costs
    nothing.

    Returns ``(routing_capable, reason)`` where ``reason`` is None on success.
    Never raises — every caller has already proved the connection works, and a
    classification failure must not fail the operation that asked for it.
    """
    try:
        await asyncio.to_thread(
            assume_role,
            role_arn=role_arn,
            external_id=external_id,
            session_duration_seconds=900,
            default_region=default_region,
            user_id=user_id,
            agent_id="connect-verify",
            task_id="routing-probe",
            label=label,
            send_session_tags=False,
        )
    except STSAssumeError as exc:
        if exc.code in ("AccessDenied", "AccessDeniedException"):
            # The trust policy refused an untagged assume — the v1 single-user pin.
            # This is the expected, non-alarming outcome for every existing
            # connection; the account must re-run the v2 template to be routable.
            logger.info("Routing probe: role is user-pinned (v1 shape) role_arn=%s", role_arn)
            return False, ROUTING_REASON_USER_PINNED
        # Anything else (throttling, region disabled, transient STS failure) is not
        # evidence about the trust policy. Report not-capable so nothing is routed
        # to an unproven role, but with a distinct reason so an operator can tell
        # "re-run the template" from "re-run the probe".
        logger.warning("Routing probe inconclusive role_arn=%s code=%s", role_arn, exc.code)
        return False, ROUTING_REASON_PROBE_INCONCLUSIVE

    return True, None


def _invoke_probe_verdict(error_code: str, role_arn: str) -> tuple[bool, str | None]:
    """Read IAM's verdict out of the error Bedrock returned. See the module docstring.

    Split out from :func:`probe_bedrock_invoke` so the discrimination table is
    testable without a live AWS call — the mapping from error code to conclusion is
    the whole of this check's correctness, and it is the part most likely to be
    "simplified" into treating every error as a denial.
    """
    if error_code in ("AccessDeniedException", "AccessDenied", "UnauthorizedOperation"):
        # IAM refused before the body was even parsed. This is the §5.0 case: a role
        # that assumes fine and cannot invoke Bedrock — an inert mapping.
        logger.info("Invoke probe: role cannot invoke Bedrock role_arn=%s", role_arn)
        return False, REASON_ROLE_MISSING_BEDROCK_PERMISSION

    if error_code == "ValidationException":
        # IAM ALLOWED the call and Bedrock got as far as rejecting our deliberately
        # malformed body. That is the success signal — the only one available without
        # actually generating billable tokens.
        return True, None

    # Everything else — ResourceNotFoundException (model absent in that
    # account/region), ThrottlingException, an endpoint that cannot be reached — is
    # not evidence about IAM. Report not-capable, so nothing is routed to an
    # unproven role, with the reason that says "re-run the probe" rather than
    # "re-run the template".
    logger.warning("Invoke probe inconclusive role_arn=%s code=%s", role_arn, error_code)
    return False, ROUTING_REASON_PROBE_INCONCLUSIVE


async def probe_bedrock_invoke(
    *,
    role_arn: str,
    external_id: str | None,
    default_region: str,
    user_id: str,
    label: str,
) -> tuple[bool, str | None]:
    """May this role actually invoke Bedrock? (§5.0 implication 3, §6.7 item 3.)

    The check :func:`probe_assumable_for_any_principal` cannot make: a role can be
    perfectly assumable by any principal and still lack ``bedrock:InvokeModel``,
    which is exactly an inert mapping — it passes the naive gate and fails every
    call (#4511 class). §5.0 is a blocker for the *authoring* work, not only the
    signing work, for this reason.

    Assumes the role, then calls ``invoke_model`` with a malformed body and reads
    IAM's verdict out of the error code (see the module docstring's table). Bills
    nothing on any branch.

    Returns ``(capable, reason)`` where ``reason`` is None on success. Never raises:
    an inconclusive probe returns ``(False, routing_probe_inconclusive)`` so the
    caller refuses the destination rather than crashing the admin's request.
    """
    try:
        assumed = await asyncio.to_thread(
            assume_role,
            role_arn=role_arn,
            external_id=external_id,
            session_duration_seconds=900,
            default_region=default_region,
            user_id=user_id,
            agent_id="routing-validate",
            task_id="invoke-probe",
            label=label,
            send_session_tags=False,
        )
    except STSAssumeError as exc:
        # The assume itself failed. Report it with R3's assume code rather than an
        # invoke-flavoured one: the remediation is the trust policy, not the
        # permission policy, and conflating them sends the admin to the wrong page.
        logger.warning("Invoke probe could not assume role role_arn=%s code=%s", role_arn, exc.code)
        return False, REASON_ASSUME_ROLE_FAILED

    def _call() -> None:
        # Region from the destination, not the gateway's: a role may be permitted in
        # one region and not another, and the probe must test where calls will land.
        client = boto3.client(
            "bedrock-runtime",
            region_name=assumed.region or default_region,
            aws_access_key_id=assumed.access_key_id,
            aws_secret_access_key=assumed.secret_access_key,
            aws_session_token=assumed.session_token,
            config=_probe_client_config(),
        )
        client.invoke_model(modelId=_PROBE_MODEL_ID, body=_PROBE_BODY)

    try:
        await asyncio.to_thread(_call)
    except ClientError as exc:
        return _invoke_probe_verdict(exc.response.get("Error", {}).get("Code", ""), role_arn)
    except Exception as exc:  # noqa: BLE001 - a probe must never fail the caller's request
        logger.warning("Invoke probe raised a non-ClientError role_arn=%s error=%s", role_arn, exc)
        return False, ROUTING_REASON_PROBE_INCONCLUSIVE

    # A malformed body that SUCCEEDS should be impossible. Treat it as capable
    # anyway — the call was permitted, which is the only thing being asked — but log
    # it, because it means the probe body has stopped being invalid and the check
    # may be billing tokens.
    logger.warning("Invoke probe unexpectedly succeeded; the probe body may no longer be invalid role_arn=%s", role_arn)
    return True, None


def _probe_client_config():
    """Short-timeout, no-retry botocore config. Imported lazily-ish for test patching."""
    from botocore.config import Config

    return Config(
        connect_timeout=_PROBE_TIMEOUT_SECONDS,
        read_timeout=_PROBE_TIMEOUT_SECONDS,
        retries={"max_attempts": _PROBE_MAX_ATTEMPTS},
    )


async def probe_routing_destination(
    *,
    role_arn: str,
    external_id: str | None,
    default_region: str,
    user_id: str,
    label: str,
) -> tuple[bool, str | None]:
    """Run BOTH probes. A destination is routable only if both pass (§6.7 item 3).

    The order is not arbitrary: assumability is checked first because it is the
    cheaper call and because its failure explains the other's. A role that cannot be
    assumed for an arbitrary principal has nothing to invoke *with*, so running the
    invoke probe on it would produce a second, misleading reason code for one cause.

    Returns ``(capable, reason)``; ``reason`` is None only when both probes passed.
    Never raises — see the individual probes.

    **A pass is not a permanent guarantee** (§6.7 item 4). The customer can delete
    the role or rotate the ExternalId afterwards. This gate removes *never-worked*
    mappings; §2.5's fail-closed runtime error handles *worked-then-broke*. Both are
    needed — do not let this gate's existence argue away the runtime error path.
    """
    assumable, reason = await probe_assumable_for_any_principal(
        role_arn=role_arn,
        external_id=external_id,
        default_region=default_region,
        user_id=user_id,
        label=label,
    )
    if not assumable:
        return False, reason

    return await probe_bedrock_invoke(
        role_arn=role_arn,
        external_id=external_id,
        default_region=default_region,
        user_id=user_id,
        label=label,
    )
