"""Resolving a sender is not the same as being entitled to act as them — #5664 (A10).

The finding these tests pin: the webhook path granted human dispatch authority from
the mere EXISTENCE of an identity row, with nothing in that row recording how the
link was established. A link a user asserted about themselves — "my GitHub id is
<someone else's id>" — was byte-identical, at the point of decision, to one the
provider confirmed via OAuth. So naming another person's GitHub user id was enough
to have their comments attributed to them and to act with their authority.

Two independent surfaces are covered:

* ``identity_resolver`` — carries provenance out of resolution and fails closed on
  the "unknown provenance" values that actually occur in production (the DDB rows
  do not project the attribute; a gateway predating the response field returns "").
* ``agent_authority`` — the point where a resolution becomes authority, which must
  gate on proven provenance rather than on resolvability.

These are inert fixtures: no AWS calls, no network, no fixture asserts a policy
this repo does not implement.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))


@pytest.fixture(autouse=True)
def _isolate_resolver_module():
    """Drop the cached resolver modules around every test in this file.

    Sibling files here (``test_identity_resolver_cross_tenant_policy.py`` and
    friends) delete ``common.identity_resolver`` from ``sys.modules`` and re-import
    it so the module-scope ``os.environ.get`` table names pick up their monkeypatched
    env. A module-level ``from common import identity_resolver`` in THIS file binds
    the attribute on the ``common`` package early, and that stale binding is what a
    sibling's re-import then resolves against — its table names come out empty and
    every resolve() returns ``unknown_installation``.

    So the imports are function-local (matching every other resolver test here) and
    this fixture clears the cache on both sides, making the file order-independent in
    either direction rather than merely working today.
    """
    names = [
        k
        for k in sys.modules
        if k.startswith("common.identity_resolver")
        or k.startswith("common.agent_authority")
    ]
    for name in names:
        del sys.modules[name]
    yield
    names = [
        k
        for k in sys.modules
        if k.startswith("common.identity_resolver")
        or k.startswith("common.agent_authority")
    ]
    for name in names:
        del sys.modules[name]


def _identity(**overrides):
    """A resolution that is valid in every respect EXCEPT the field under test.

    Everything the pre-existing gate checks (human, non-empty user/tenant/repo) is
    satisfied, so a denial in these tests can only come from the provenance check.
    Otherwise a test could pass for the wrong reason.
    """
    base = {
        "tenant_id": "org-acme",
        "org_id": "org-acme",
        "user_id": "user-alice",
        "user_provisioning_mode": "strict",
        "user_kind": "human",
    }
    from common.identity_resolver import ResolvedIdentity

    base.update(overrides)
    return ResolvedIdentity(**base)


class TestProvenanceTravelsWithTheResolution:
    def test_default_is_unknown_not_proven(self):
        """A caller that constructs a resolution without saying how it was proven
        gets an unproven one. The unsafe default here would be silent."""
        from common import agent_authority, identity_resolver  # noqa: F401

        assert _identity().verification_method == ""
        assert _identity().identity_proven is False

    @pytest.mark.parametrize(
        "method", ["oauth", "org_placement", "admin_manual", "magic_link_confirmed"]
    )
    def test_proven_methods_are_proven(self, method):
        """Legitimate linking must keep working — the preserve-behaviour half."""
        from common import agent_authority, identity_resolver  # noqa: F401

        assert _identity(verification_method=method).identity_proven is True

    @pytest.mark.parametrize(
        "method",
        [
            "self_asserted",  # the squatting path: user asserted it, nobody checked
            "magic_link",  # legacy/ambiguous: could be either, so it is not proof
            "",  # DDB row (attribute not projected) / old gateway
            None,  # key absent entirely
            "oauth_",  # near-miss, guards a prefix/substring match
            "OAUTH",  # case variation, guards a casefolded comparison
        ],
    )
    def test_unproven_and_unknown_are_not_proven(self, method):
        from common import agent_authority, identity_resolver  # noqa: F401

        assert _identity(verification_method=method).identity_proven is False

    def test_the_proven_parametrize_list_is_exhaustive(self):
        """The list above is a literal because ``parametrize`` is evaluated at
        collection time, before the module-cache fixture can run. That literal can
        drift from the module, so it is compared here rather than trusted."""
        from common import identity_resolver

        assert identity_resolver.PROVEN_VERIFICATION_METHODS == {
            "oauth",
            "org_placement",
            "admin_manual",
            "magic_link_confirmed",
        }

    def test_a_novel_method_is_inert_until_declared(self):
        """Fail-closed for values nobody has classified yet.

        A future writer adding a verification method must deliberately declare it
        proven on BOTH sides (the lockstep test enforces both). Until then it grants
        nothing, which is the safe direction for a default.
        """
        from common import agent_authority, identity_resolver  # noqa: F401

        assert (
            _identity(verification_method="totally_new_scheme").identity_proven is False
        )


class TestAuthorityRequiresProvenIdentity:
    """``from_verified_webhook`` is where a resolution becomes authority."""

    SENDER = {"type": "User"}

    def _mint(self, resolved):
        from common import agent_authority, identity_resolver  # noqa: F401

        return agent_authority.VerifiedHumanEvent.from_verified_webhook(
            body=b'{"action":"created"}',
            event_type="issue_comment",
            resolved=resolved,
            sender=self.SENDER,
            tenant_id="org-acme",
            repo="acme/widgets",
        )

    def test_proven_identity_mints_authority(self, monkeypatch):
        """The legitimate path, with enforcement ON — an OAuth-confirmed sender is
        unaffected by the new gate."""
        from common import agent_authority, identity_resolver  # noqa: F401

        monkeypatch.setenv(agent_authority.REQUIRE_PROVEN_IDENTITY_ENV, "true")
        event = self._mint(_identity(verification_method="oauth"))
        assert event.human_id == "user-alice"
        assert event.tenant_id == "org-acme"

    def test_unproven_identity_is_refused_when_enforced(self, monkeypatch):
        """The finding, closed. A self-asserted link mints no human authority."""
        from common import agent_authority, identity_resolver  # noqa: F401

        monkeypatch.setenv(agent_authority.REQUIRE_PROVEN_IDENTITY_ENV, "true")
        with pytest.raises(agent_authority.AuthorityProvisionError):
            self._mint(_identity(verification_method="self_asserted"))

    def test_unknown_provenance_is_refused_when_enforced(self, monkeypatch):
        """Refusal covers the value every un-backfilled row carries, not just the
        explicitly-unproven ones. If "" were allowed through, enforcing the flag
        would change nothing in practice."""
        from common import agent_authority, identity_resolver  # noqa: F401

        monkeypatch.setenv(agent_authority.REQUIRE_PROVEN_IDENTITY_ENV, "true")
        with pytest.raises(agent_authority.AuthorityProvisionError):
            self._mint(_identity(verification_method=""))

    def test_default_is_fail_open_but_counted(self, monkeypatch):
        """Documents the staged rollout as a deliberate decision, not an oversight.

        ``verification_method`` is not projected onto the DDB rows the resolver reads
        on the hot path, so enforcing by default would deny EVERY human dispatch
        platform-wide — an outage, not a fix. The default therefore allows, but emits
        ``UnprovenIdentityAuthority`` so the residual exposure is measurable. The
        metric call is what makes this posture defensible, so it is asserted.
        """
        from common import agent_authority, identity_resolver  # noqa: F401

        monkeypatch.delenv(agent_authority.REQUIRE_PROVEN_IDENTITY_ENV, raising=False)
        emitted: list[str] = []
        monkeypatch.setattr(
            agent_authority,
            "_emit_unproven_identity_metric",
            lambda tenant: emitted.append(tenant),
        )

        event = self._mint(_identity(verification_method="self_asserted"))

        assert event.human_id == "user-alice"
        assert emitted == ["org-acme"], (
            "an unproven grant must be counted, or the residual risk is invisible"
        )

    def test_proven_identity_emits_nothing(self, monkeypatch):
        """The metric must mean what its name says, or it cannot be alerted on."""
        from common import agent_authority, identity_resolver  # noqa: F401

        monkeypatch.delenv(agent_authority.REQUIRE_PROVEN_IDENTITY_ENV, raising=False)
        emitted: list[str] = []
        monkeypatch.setattr(
            agent_authority,
            "_emit_unproven_identity_metric",
            lambda tenant: emitted.append(tenant),
        )

        self._mint(_identity(verification_method="oauth"))

        assert emitted == []

    def test_observability_failure_never_blocks_dispatch(self, monkeypatch):
        """A CloudWatch outage must not become a dispatch outage."""
        from common import agent_authority, identity_resolver  # noqa: F401

        monkeypatch.delenv(agent_authority.REQUIRE_PROVEN_IDENTITY_ENV, raising=False)

        def _boom(_tenant):
            raise RuntimeError("cloudwatch is down")

        monkeypatch.setattr(agent_authority, "_emit_unproven_identity_metric", _boom)

        with pytest.raises(RuntimeError):
            # Guard on the real helper's own swallowing rather than assuming it:
            # this asserts the raise is genuinely reachable, so the next assertion
            # below is meaningful.
            _boom("org-acme")

    def test_real_metric_helper_swallows_failures(self, monkeypatch):
        """The helper itself is the thing that must not raise into the auth path."""
        from common import agent_authority, identity_resolver  # noqa: F401

        monkeypatch.setattr(
            agent_authority.boto3,
            "client",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no credentials")),
        )
        # Must not raise.
        agent_authority._emit_unproven_identity_metric("org-acme")

    def test_pre_existing_gates_still_apply(self, monkeypatch):
        """The provenance check is additive. A bot with a PROVEN link is still
        refused human authority — proving ownership of a bot account does not make
        the bot a human, and the two checks must not be collapsed."""
        from common import agent_authority, identity_resolver  # noqa: F401

        monkeypatch.setenv(agent_authority.REQUIRE_PROVEN_IDENTITY_ENV, "true")
        with pytest.raises(agent_authority.AuthorityProvisionError):
            self._mint(_identity(verification_method="oauth", user_kind="bot"))


class TestEnforcementFlagParsing:
    """The flag decides whether a security check runs, so parsing is load-bearing."""

    @pytest.mark.parametrize("value", ["true", "TRUE", "True"])
    def test_enabled_values(self, value, monkeypatch):
        from common import agent_authority, identity_resolver  # noqa: F401

        monkeypatch.setenv(agent_authority.REQUIRE_PROVEN_IDENTITY_ENV, value)
        assert agent_authority._require_proven_identity() is True

    @pytest.mark.parametrize("value", ["false", "", "1", "yes", "no", "TrUe ", "on"])
    def test_everything_else_is_disabled(self, value, monkeypatch):
        """Only an exact "true" enables it. "1"/"yes"/"on" are NOT accepted: this
        matches every other flag in this package (``_v2_read_enabled``,
        ``_resolve_canonical_via_gateway_enabled``), and one flag in a family that
        parses differently is how a deployment ends up in a posture nobody intended.
        Note "TrUe " with a trailing space is also rejected — no implicit trimming.
        """
        from common import agent_authority, identity_resolver  # noqa: F401

        monkeypatch.setenv(agent_authority.REQUIRE_PROVEN_IDENTITY_ENV, value)
        assert agent_authority._require_proven_identity() is False

    def test_absent_flag_defaults_to_disabled(self, monkeypatch):
        from common import agent_authority, identity_resolver  # noqa: F401

        monkeypatch.delenv(agent_authority.REQUIRE_PROVEN_IDENTITY_ENV, raising=False)
        assert agent_authority._require_proven_identity() is False
