"""The fail-closed error a routed Bedrock call raises, and its reason vocabulary.

Issue #4744 (#4692 · R3 · routing enforcement), per the design note
``docs/design-notes/4692-per-principal-bedrock-account-routing.md`` §2.6, §5.2.

Operator ruling 1, in full: *"If the mapped account cannot serve the call (model not
enabled there, assume-role failure, account unlinked), the call fails with a clear
reason naming the mapped account AND how to fix it. No silent fallback to the
platform account."*

That ruling makes this module the **only** outcome a broken mapping can have. There
is no fallback branch anywhere in the signing path to reach instead — see the module
docstring of :mod:`src.proxy.bedrock_signing`, and note that ruling 1 forbids such a
branch *existing*, not merely being taken. A single ``except: use_platform_account()``
would void the feature, because the mapping's entire purpose voids itself precisely
when it matters and does so with a 200 response.

Three requirements from §2.6, all structural here rather than conventional:

**1. ``reason`` is a stable machine code, distinct per cause.** Ruling 1 names three
causes that need three *different* fixes, so one generic code cannot carry them: the
remediation for "enable the model in the console" is not the remediation for
"re-validate the role". The vocabulary is shared with R4's save-time validation
(§6.7), which is why it lives in its own module rather than inside the signing path
that happens to raise it first — R4 imports these names, and a copy in two places is
a vocabulary that drifts.

**2. The remediation is audience-correct** (:func:`_remediation_for`). A member whose
call fails on a *team* mapping cannot fix that mapping — under ruling 4 authoring is
platform-admin-only, so telling them to "change your account mapping" is a dead end
that generates a support ticket. Team and org rungs point at a platform admin; the
user rung, which ruling 2 makes self-service, points at the user's own credentials
screen.

**3. The role ARN and ExternalId never appear in a user-facing error.** The account id
is fine — ruling 1 *requires* it. This split already has precedent in the repo:
``assume_role_routes.py`` writes ``role_arn`` to the audit row while explicitly keeping
it out of the user-facing error ("do NOT include role_arn in user-facing error").

Here that rule is enforced by **construction, not by review**:
:class:`BedrockAccountUnavailableError` has no parameter for a role ARN or an
ExternalId, so there is no argument to pass one through. A future edit that wants to
leak one has to change this class's signature, which is a visible diff in a file whose
docstring says why not to — rather than an innocuous-looking addition at a call site.
Callers that need the ARN for diagnosis log it server-side (the signing path does
exactly that) and pass only the account id here.
"""

from __future__ import annotations

from typing import Literal

from src.shared.exceptions import BedrockGatewayError

# The stable machine-readable causes. Shared with R4's save-time validation (§2.6,
# §6.7): the admin authoring UI renders the same vocabulary when a test assume
# fails at save time that the proxy returns when one fails at request time, so an
# operator sees one set of names for one set of problems.
#
# Values are wire contract. They appear in the `reason` field of a 502 body that
# clients and the R4 UI branch on, so renaming one is a breaking change — add a new
# code instead.
REASON_ASSUME_ROLE_FAILED = "assume_role_failed"
REASON_MODEL_NOT_ENABLED = "model_not_enabled"
REASON_ACCOUNT_UNLINKED = "account_unlinked"
REASON_ROLE_MISSING_BEDROCK_PERMISSION = "role_missing_bedrock_permission"

BedrockUnavailableReason = Literal[
    "assume_role_failed",
    "model_not_enabled",
    "account_unlinked",
    "role_missing_bedrock_permission",
]

# Every code above, for validation and for tests that assert the set is covered.
ALL_REASONS: frozenset[str] = frozenset(
    {
        REASON_ASSUME_ROLE_FAILED,
        REASON_MODEL_NOT_ENABLED,
        REASON_ACCOUNT_UNLINKED,
        REASON_ROLE_MISSING_BEDROCK_PERMISSION,
    }
)

# The scope whose mapping produced the destination — i.e. the rung the resolver
# matched. `platform` is deliberately absent: the platform rung is the *absence* of a
# mapping, and a call that resolves to it is never routed, so it can never raise this
# error. If one ever did, the fail-closed rule would be failing calls that main would
# have served.
BedrockUnavailableScope = Literal["user", "team", "org"]

# Where each audience is sent. Team and org rungs are platform-admin-authored
# (ruling 4), so their remediation cannot be "fix your mapping" — the user has no
# authority over that rung and no surface on which to act.
_ADMIN_REMEDIATION = (
    "Ask a platform admin to re-validate the Bedrock account routing mapping for your {scope}, "
    "or to remove it so calls return to the platform account."
)
_SELF_REMEDIATION = (
    "You selected this account for your own Bedrock calls: open Settings → Credentials to "
    "re-verify the connection, or clear the selection to return to the platform account."
)


def _remediation_for(scope: BedrockUnavailableScope) -> str:
    """The fix, written for whoever can actually apply it (§2.6 requirement 2).

    The scope is already known at raise time, so choosing the text from it costs
    nothing and is the difference between a 5-minute self-service fix and a support
    escalation.
    """
    if scope == "user":
        return _SELF_REMEDIATION
    return _ADMIN_REMEDIATION.format(scope=scope)


def _message_for(
    reason: BedrockUnavailableReason,
    account_id: str,
    scope: BedrockUnavailableScope,
    model_id: str | None,
) -> str:
    """The human-readable half: what went wrong, in which account, and the fix.

    Ruling 1's own example is the template for the model case — *"model X is not
    enabled in AWS account …1234 — enable it in the Bedrock console, or
    change/remove your account mapping"* — and the other causes follow its shape:
    name the account, name the cause, then the audience-correct remediation.
    """
    remediation = _remediation_for(scope)

    if reason == REASON_MODEL_NOT_ENABLED:
        model = model_id or "the requested model"
        return (
            f"{model} is not enabled in AWS account {account_id}, which your {scope} Bedrock "
            f"routing mapping points at. Enable it in that account's Bedrock console "
            f"(Model access), or change the mapping. {remediation}"
        )
    if reason == REASON_ROLE_MISSING_BEDROCK_PERMISSION:
        return (
            f"ADP assumed the role in AWS account {account_id}, but that role is not allowed to "
            f"invoke Bedrock models. The account needs to be reconnected with the routing-capable "
            f"template. {remediation}"
        )
    if reason == REASON_ACCOUNT_UNLINKED:
        return (
            f"AWS account {account_id} is mapped to serve your Bedrock calls, but its connection to "
            f"ADP is missing or no longer verified, so ADP has no credentials for it. {remediation}"
        )
    return (
        f"ADP could not assume the configured role in AWS account {account_id} to serve this "
        f"request. The role may have been deleted, or its trust configuration changed. {remediation}"
    )


class BedrockAccountUnavailableError(BedrockGatewayError):
    """A mapped Bedrock account could not serve this call. The call fails (ruling 1).

    **502, not 503 or 500** (§2.6): upstream credential acquisition or upstream model
    access failed while the client's own request was well-formed. A 4xx would blame the
    caller for an operator's misconfiguration, and a 503 would read as "retry shortly"
    when every retry fails identically until someone fixes the mapping.

    ``model_id`` is optional because only the model-enablement cause has one — an
    assume failure happens before any model is named, and inventing a model in that
    payload would misdirect the reader to the model access page for a trust-policy
    problem.

    **There is deliberately no parameter for ``role_arn`` or ``external_id``.** See the
    module docstring: the redaction rule is enforced by this signature rather than by
    reviewer vigilance.
    """

    def __init__(
        self,
        *,
        reason: BedrockUnavailableReason,
        account_id: str,
        scope: BedrockUnavailableScope,
        model_id: str | None = None,
    ) -> None:
        self.reason = reason
        self.account_id = account_id
        self.scope = scope
        self.model_id = model_id
        super().__init__(
            error="bedrock_account_unavailable",
            message=_message_for(reason, account_id, scope, model_id),
            status_code=502,
            details={
                # The account id is required by ruling 1 — the user must be able to
                # tell the operator *which* account to look at.
                "account_id": account_id,
                # Stable machine code so clients and the R4 UI can branch per cause
                # rather than string-matching the message.
                "reason": reason,
                "scope": scope,
                "model_id": model_id,
                "remediation": _remediation_for(scope),
            },
        )
