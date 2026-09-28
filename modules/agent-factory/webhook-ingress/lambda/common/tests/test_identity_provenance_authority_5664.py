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
        "method", ["oauth", "org_placement", "admin_attested", "magic_link_confirmed"]
    )
    def test_proven_methods_are_proven(self, method):
        """Legitimate linking must keep working — the preserve-behaviour half."""
        from common import agent_authority, identity_resolver  # noqa: F401

        assert _identity(verification_method=method).identity_proven is True

    @pytest.mark.parametrize(
        "method",
        [
            "admin_manual",  # historical automatic and manual writers are ambiguous
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
            "admin_attested",
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
    """``from_verified_webhook`` is where a resolution becomes authority.

    The check is UNCONDITIONAL. A previous slice staged it behind
    ``REQUIRE_PROVEN_IDENTITY_FOR_AUTHORITY``, defaulting to allow-and-count,
    because ``verification_method`` was not projected onto the DynamoDB rows the
    resolver reads, so enforcing would have denied every human dispatch. That
    projection now exists end to end (gateway writers -> both identity tables ->
    resolver), so there is no longer a rollout reason to permit unproven links and
    no env var can re-permit them. The flag, its parser and its tests are gone
    deliberately: a switch that turns a security check off is itself the
    vulnerability once the data gap it covered for is closed.
    """

    SENDER = {"type": "User"}

    def _mint(self, resolved, *, tenant_id="org-acme"):
        from common import agent_authority, identity_resolver  # noqa: F401

        return agent_authority.VerifiedHumanEvent.from_verified_webhook(
            body=b'{"action":"created"}',
            event_type="issue_comment",
            resolved=resolved,
            sender=self.SENDER,
            tenant_id=tenant_id,
            repo="acme/widgets",
        )

    @pytest.mark.parametrize(
        "method", ["oauth", "org_placement", "admin_attested", "magic_link_confirmed"]
    )
    def test_proven_identity_mints_authority(self, method):
        """The legitimate path. Every method the policy calls proof must still mint
        authority with no flag set — this is the "legitimate current provider proof
        has a tested route through the new contract" half, and it is parametrized
        over the whole proven set so closing the hole cannot silently narrow it."""
        event = self._mint(_identity(verification_method=method))
        assert event.human_id == "user-alice"
        assert event.tenant_id == "org-acme"

    @pytest.mark.parametrize(
        "method",
        [
            "admin_manual",  # historical automatic and manual writers are ambiguous
            "self_asserted",  # the squatting path: user asserted it, nobody checked
            "magic_link",  # legacy/ambiguous: could be either, so it is not proof
            "",  # un-backfilled DDB row / gateway predating the response field
            None,  # attribute absent entirely
            "oauth_",  # near-miss, guards a prefix/substring match
            "OAUTH",  # case variation, guards a casefolded comparison
            "totally_new_scheme",  # never classified: inert until declared proven
        ],
    )
    def test_unproven_or_unknown_provenance_is_refused(self, method):
        """The finding, closed by default. No env var is set in this test: refusal is
        the behaviour of the shipped configuration, which is what the previous slice
        did not deliver."""
        from common import agent_authority, identity_resolver  # noqa: F401

        with pytest.raises(agent_authority.AuthorityProvisionError):
            self._mint(_identity(verification_method=method))

    def test_a_resolver_without_the_property_is_refused(self):
        """Mixed-version deploy: a resolution object from an older Lambda layer has
        no ``identity_proven`` at all. Absent must read as unproven, not as pass."""
        from common import agent_authority, identity_resolver  # noqa: F401

        class LegacyResolved:
            user_kind = "human"
            user_id = "user-alice"
            tenant_id = "org-acme"
            org_id = "org-acme"

        with pytest.raises(agent_authority.AuthorityProvisionError):
            self._mint(LegacyResolved())

    def test_refusal_is_counted(self, monkeypatch):
        """The metric moved to the DENY path. A non-zero count means real senders are
        being refused because their rows lack provenance — the signal to check the
        backfill, which is only actionable if the refusal is actually counted."""
        from common import agent_authority, identity_resolver  # noqa: F401

        emitted: list[str] = []
        monkeypatch.setattr(
            agent_authority,
            "_emit_unproven_identity_metric",
            lambda tenant: emitted.append(tenant),
        )

        with pytest.raises(agent_authority.AuthorityProvisionError):
            self._mint(_identity(verification_method="self_asserted"))

        assert emitted == ["org-acme"]

    def test_proven_identity_emits_nothing(self, monkeypatch):
        """The metric must mean what its name says, or it cannot be alerted on."""
        from common import agent_authority, identity_resolver  # noqa: F401

        emitted: list[str] = []
        monkeypatch.setattr(
            agent_authority,
            "_emit_unproven_identity_metric",
            lambda tenant: emitted.append(tenant),
        )

        self._mint(_identity(verification_method="oauth"))

        assert emitted == []

    def test_real_metric_helper_swallows_failures(self, monkeypatch):
        """Observability must not raise into the authorization path: on the deny path
        an exception here would replace a clean refusal with an unhandled error."""
        from common import agent_authority, identity_resolver  # noqa: F401

        monkeypatch.setattr(
            agent_authority.boto3,
            "client",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no credentials")),
        )
        # Must not raise.
        agent_authority._emit_unproven_identity_metric("org-acme")

    def test_metric_failure_still_yields_a_clean_refusal(self, monkeypatch):
        """End-to-end of the above: CloudWatch down during a refusal still produces
        AuthorityProvisionError, not RuntimeError. Asserted through the real helper
        rather than a stub, so it tests the shipped swallow."""
        from common import agent_authority, identity_resolver  # noqa: F401

        monkeypatch.setattr(
            agent_authority.boto3,
            "client",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cloudwatch is down")),
        )
        with pytest.raises(agent_authority.AuthorityProvisionError):
            self._mint(_identity(verification_method=""))

    def test_pre_existing_gates_still_apply(self):
        """The provenance check is additive. A bot with a PROVEN link is still
        refused human authority — proving ownership of a bot account does not make
        the bot a human, and the two checks must not be collapsed."""
        from common import agent_authority, identity_resolver  # noqa: F401

        with pytest.raises(agent_authority.AuthorityProvisionError):
            self._mint(_identity(verification_method="oauth", user_kind="bot"))

    def test_no_env_var_can_re_permit_unproven_links(self, monkeypatch):
        """The removed escape hatch stays removed.

        Setting the old flag name — in either direction — must not change the
        outcome. This is the regression guard for re-introducing the vulnerability
        by configuration, and it also fails loudly if someone restores the parser.
        """
        from common import agent_authority, identity_resolver  # noqa: F401

        assert not hasattr(agent_authority, "REQUIRE_PROVEN_IDENTITY_ENV")
        assert not hasattr(agent_authority, "_require_proven_identity")

        for value in ("false", "true", ""):
            monkeypatch.setenv("REQUIRE_PROVEN_IDENTITY_FOR_AUTHORITY", value)
            with pytest.raises(agent_authority.AuthorityProvisionError):
                self._mint(_identity(verification_method="self_asserted"))


class TestProvenanceMustBelongToThisResolution:
    """A proven link proves control of an account in ONE tenant.

    ``user_identities`` is unique per (provider, provider_user_id, org_id), so the
    same GitHub account can hold a proven link in tenant A and none in tenant B.
    Checking only "is this resolution proven" would let provenance earned in A mint
    authority in B. The tenant the authority is minted for is therefore re-checked
    against the resolution that carried the provenance, not taken solely from the
    caller's argument.
    """

    SENDER = {"type": "User"}

    def _mint(self, resolved, *, tenant_id):
        from common import agent_authority, identity_resolver  # noqa: F401

        return agent_authority.VerifiedHumanEvent.from_verified_webhook(
            body=b'{"action":"created"}',
            event_type="issue_comment",
            resolved=resolved,
            sender=self.SENDER,
            tenant_id=tenant_id,
            repo="acme/widgets",
        )

    def test_matching_tenant_is_allowed(self):
        event = self._mint(_identity(verification_method="oauth"), tenant_id="org-acme")
        assert event.tenant_id == "org-acme"

    def test_resolved_tenant_mismatch_is_refused(self):
        from common import agent_authority, identity_resolver  # noqa: F401

        resolved = _identity(
            verification_method="oauth",
            tenant_id="org-victim",
            org_id="org-victim",
        )
        with pytest.raises(agent_authority.AuthorityProvisionError):
            self._mint(resolved, tenant_id="org-attacker")

    def test_resolved_org_mismatch_is_refused(self):
        """``org_id`` is checked independently of ``tenant_id``: a resolution whose
        two fields disagree must not pass by satisfying only the one that happens to
        be compared first."""
        from common import agent_authority, identity_resolver  # noqa: F401

        resolved = _identity(
            verification_method="oauth",
            tenant_id="org-attacker",
            org_id="org-victim",
        )
        with pytest.raises(agent_authority.AuthorityProvisionError):
            self._mint(resolved, tenant_id="org-attacker")
