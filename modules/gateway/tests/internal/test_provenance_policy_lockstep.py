"""Trigger-policy lockstep test — gateway authority gate ↔ webhook-ingress resolver.

Issue #4029: the gateway's provenance authority gate and the webhook-ingress
identity resolver both read the same ``trigger_policy`` vocabulary out of the same
admin-configured setting, but they held it as *independent bare string literals* in
two modules. They disagreed about the DEFAULT, and the result was an audit hole: the
resolver permitted a cross-tenant run to execute, then the gateway refused to record
its provenance.

The two sides cannot import a shared constant. ``package-lambdas.sh`` roots the
Lambda zip at ``lambda/`` (``cd "$handler_dir" && zip -r …`` / ``cd "$LAMBDA_DIR" &&
zip -r "$ZIP_FILE" common/``), so nothing outside that directory is importable — or
even readable — at Lambda runtime. Any "shared module" would silently ImportError in
production.

So this test is the drift guard, following the precedent of
``webhook-ingress/lambda/common/tests/test_marker_lockstep.py`` (issue #1696), which
imports the worker's marker writer and the Lambda's marker reader and asserts
round-trip fidelity. Same idea, applied to a shared constant instead of a format:
import BOTH modules and assert the vocabulary and the default agree.

If this test fails, do not "fix" it by editing one side's expected value — make the
two sides agree, or the #4029 audit hole reopens.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The Lambda package is not on the path in the gateway's test environment (the two
# modules are packaged separately). Add lambda/ so `common.identity_resolver` is
# importable, exactly as test_marker_lockstep.py does for its cross-module import.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_LAMBDA_ROOT = _REPO_ROOT / "modules" / "agent-factory" / "webhook-ingress" / "lambda"


@pytest.fixture(scope="module")
def resolver():
    """The webhook-ingress identity resolver module.

    boto3 is imported at resolver module scope but no client is constructed at import
    time, so this import needs no AWS credentials or network.
    """
    if not _LAMBDA_ROOT.is_dir():
        pytest.fail(
            f"Lambda package not found at {_LAMBDA_ROOT}. This lockstep test's path "
            "arithmetic is stale — fix the path rather than skipping, or the two "
            "sides of the trigger-policy contract stop being compared at all."
        )

    inserted = str(_LAMBDA_ROOT) not in sys.path
    if inserted:
        sys.path.insert(0, str(_LAMBDA_ROOT))
    try:
        from common import identity_resolver

        yield identity_resolver
    finally:
        if inserted:
            sys.path.remove(str(_LAMBDA_ROOT))


class TestTriggerPolicyLockstep:
    """The gateway and the Lambda must agree on the policy vocabulary and default."""

    def test_any_adp_user_value_matches(self, resolver):
        from src.internal import provenance_routes

        assert provenance_routes.TRIGGER_POLICY_ANY_ADP_USER == resolver.TRIGGER_POLICY_ANY_ADP_USER

    def test_home_tenant_only_value_matches(self, resolver):
        from src.internal import provenance_routes

        assert provenance_routes.TRIGGER_POLICY_HOME_TENANT_ONLY == resolver.TRIGGER_POLICY_HOME_TENANT_ONLY

    def test_default_policy_matches(self, resolver):
        """The load-bearing assertion — a mismatched default IS the #4029 bug.

        An org that never configured ``trigger_policy`` must get the same answer from
        both sides. When the resolver said "permitted" and the gateway said "denied",
        cross-tenant runs executed and then lost their audit row.
        """
        from src.internal import provenance_routes

        assert provenance_routes.DEFAULT_TRIGGER_POLICY == resolver.DEFAULT_TRIGGER_POLICY

    def test_default_is_permissive_on_both_sides(self, resolver):
        """Pins the *direction* of the default, not just that the two sides match.

        Both sides could be flipped to deny-by-default together and still satisfy the
        equality tests above, silently reintroducing the audit hole for every org on
        the implicit default. Changing this requires deciding to, not drifting into it.
        """
        from src.internal import provenance_routes

        assert resolver.DEFAULT_TRIGGER_POLICY == resolver.TRIGGER_POLICY_ANY_ADP_USER
        assert provenance_routes.DEFAULT_TRIGGER_POLICY == provenance_routes.TRIGGER_POLICY_ANY_ADP_USER

    def test_the_two_policies_are_distinct(self, resolver):
        """Guards a copy-paste that would make home_tenant_only a no-op."""
        from src.internal import provenance_routes

        assert provenance_routes.TRIGGER_POLICY_ANY_ADP_USER != provenance_routes.TRIGGER_POLICY_HOME_TENANT_ONLY


class TestProvenanceVocabularyLockstep:
    """#5664 (A10): the PROVEN-method vocabulary is duplicated for the same reason.

    The webhook resolver decides which platform user a GitHub sender IS, and the
    handler feeds that answer to ``agent_authority.VerifiedHumanEvent`` to mint human
    dispatch authority. So the resolver needs to know which verification methods are
    proof of account ownership — and it cannot import the gateway's
    ``PROVEN_METHODS`` for the packaging reason this module's docstring explains.

    A drift here is worse than a policy drift: if the Lambda's set is WIDER than the
    gateway's, the webhook path grants authority from a link the gateway itself
    considers unproven, which is the A10 finding reopening on the one path where it
    is reachable without authentication.
    """

    def test_proven_sets_are_identical(self, resolver):
        from src.shared.identity.verification import PROVEN_METHODS

        assert resolver.PROVEN_VERIFICATION_METHODS == PROVEN_METHODS

    def test_lambda_set_is_not_wider_than_the_gateway(self, resolver):
        """Stated separately from equality because this is the direction that bites.

        Equality already covers it today, but if this contract is ever relaxed to
        "the Lambda may lag the gateway", the subset direction is the half that must
        survive: a Lambda accepting a method the gateway rejects is a bypass, whereas
        a Lambda rejecting one the gateway accepts is merely conservative.
        """
        from src.shared.identity.verification import PROVEN_METHODS

        assert resolver.PROVEN_VERIFICATION_METHODS <= PROVEN_METHODS

    def test_unproven_methods_are_rejected_by_the_lambda(self, resolver):
        """The two sides must also agree on what is NOT proof.

        Asserted against the gateway's UNPROVEN set rather than hard-coded strings, so
        a method moved from unproven to proven on one side alone fails here.
        """
        from src.shared.identity.verification import UNPROVEN_METHODS

        for method in UNPROVEN_METHODS:
            assert not resolver._is_proven(method), f"{method!r} must not be proof in the Lambda"

    def test_unknown_provenance_is_not_proof(self, resolver):
        """Fail-closed on the values that actually occur in production today.

        ``""`` is what a DDB row carries (the attribute is not projected) and what a
        gateway predating the response field returns; ``None`` is a missing key. If
        either counted as proof, the gate would be decorative on every existing row.
        """
        assert not resolver._is_proven("")
        assert not resolver._is_proven(None)
        assert not resolver._is_proven("magic_link")
        assert not resolver._is_proven("self_asserted")
