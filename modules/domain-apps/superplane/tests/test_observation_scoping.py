"""Workspace scoping: authenticated is not sufficient, in either direction.

Issue #5043 (U8), EPIC #4910. Design §2 line 113; §7 line 454.

This file is the story's smoke check:

    python3 -m pytest modules/domain-apps/superplane/tests/test_observation_scoping.py -q

The two required negatives are `test_cannot_submit_about_another_workspaces_cluster`
and `test_cannot_read_another_workspaces_observations`. They are separate tests
because they are separate holes: a system that scopes writes and not reads has
stopped cross-tenant tampering and left cross-tenant *disclosure* open, and a
fleet observation is an operational map of a tenant's estate — which clusters
exist, which are unreachable, what they cost.

Every submitter in this file is fully authenticated. That is the point: scoping is
asserted on top of a valid identity, so none of these refusals can be passing
because authentication happened to fail.
"""

from __future__ import annotations

import _contracts_path  # noqa: F401  (imported for its sys.path side effect)
import pytest
from conftest import OBSERVED_AT, TEST_SIGNING_KEY, W1, W2
from superplane_contracts import (
    canonical_body,
    AUTH_HEADER,
    CONTRACT_VERSION,
    SIGNATURE_HEADER,
    VERSION_HEADER,
    BudgetUsage,
    ClusterRef,
    Observation,
    Submitter,
    authorize_read,
    authorize_submit as _authorize_submit,
    compute_signature,
    verify_submission,
    visible_workspaces,
)

# The single refusal string. Asserted by name in several tests below, because the
# fact that every scoping refusal is *identical* is itself the property being
# protected — see TestRefusalsRevealNothing.
_OUT_OF_SCOPE = "workspace not in submitter scope"


def authorize_submit(submitter, observation):
    ownership = {"cluster-w1-a": W1, "cluster-w2-a": W2}
    return _authorize_submit(
        submitter,
        observation,
        cluster_workspace=ownership.get(observation.subject.cluster_id),
    )


def _submitter(*workspaces: str) -> Submitter:
    return Submitter(submitter_id="monitor-1", workspaces=frozenset(workspaces))


class TestSubmitScoping:
    """A submitter authenticated for W1 cannot write W2's observations."""

    def test_can_submit_about_its_own_workspace(self, w1_observation) -> None:
        """The positive case, so the negatives below are meaningful."""
        decision = authorize_submit(_submitter(W1), w1_observation)
        assert decision.allowed
        assert decision.reason == ""

    def test_cannot_submit_about_another_workspaces_cluster(
        self, w2_observation
    ) -> None:
        """REQUIRED NEGATIVE — cross-workspace submission is refused.

        The submitter is authenticated and holds a real grant; the grant just
        does not cover the subject. Authentication alone would have accepted
        this, which is why scoping is a separate decision.
        """
        decision = authorize_submit(_submitter(W1), w2_observation)
        assert not decision.allowed
        assert decision.reason == _OUT_OF_SCOPE

    def test_submitter_with_no_grant_can_submit_nothing(self, w1_observation) -> None:
        """An empty grant authorizes nothing.

        Fail-closed matters here specifically because an empty grant usually
        means a resolver could not determine one — and treating "unknown" as
        "all" is how a misconfigured token becomes a tenant boundary failure.
        """
        decision = authorize_submit(_submitter(), w1_observation)
        assert not decision.allowed
        assert decision.reason == _OUT_OF_SCOPE

    def test_multi_workspace_submitter_is_scoped_to_its_grant(
        self, w1_observation, w2_observation
    ) -> None:
        """A monitor watching several workspaces is allowed in each of them.

        Holding two workspaces is normal for a monitor, so the grant is a set —
        but it is still exactly a set, not a wildcard.
        """
        both = _submitter(W1, W2)
        assert authorize_submit(both, w1_observation).allowed
        assert authorize_submit(both, w2_observation).allowed

        third = Observation(
            kind="fleet_health",
            subject=ClusterRef(cluster_id="cluster-w3-a", workspace="ws-w3"),
            reported_at=w1_observation.reported_at,
            reporter="platform-monitor",
            checks=w1_observation.checks,
        )
        assert not authorize_submit(both, third).allowed

    def test_grant_is_not_taken_from_the_payload(self, w2_observation) -> None:
        """The payload cannot supply its own authority.

        `authorize_submit` takes the authenticated submitter and the observation
        as separate arguments, so there is no call shape in which the subject's
        claimed workspace becomes the grant it is checked against.
        """
        assert w2_observation.subject.workspace == W2
        assert not authorize_submit(_submitter(W1), w2_observation).allowed

    def test_cross_workspace_submission_is_refused_not_rewritten(
        self, w2_observation
    ) -> None:
        """A refusal, never a silent correction to the submitter's own workspace.

        Rewriting the subject to match the grant would turn a cross-workspace
        write into an accepted same-workspace one — data loss presented as
        success.
        """
        decision = authorize_submit(_submitter(W1), w2_observation)
        assert not decision.allowed
        assert w2_observation.subject.workspace == W2

    def test_budget_observation_is_scoped_the_same_way(self) -> None:
        """Budget submissions get the same scoping as health submissions.

        Spend is tenant-sensitive in both directions, so it is not a lesser
        payload for scoping purposes.
        """
        usage = BudgetUsage(
            workspace=W2,
            window_start=OBSERVED_AT,
            window_end=OBSERVED_AT.replace(hour=13),
            observed_spend_usd=99.0,
        )
        observation = Observation(
            kind="budget_usage",
            subject=ClusterRef(cluster_id="cluster-w2-a", workspace=W2),
            reported_at=OBSERVED_AT,
            reporter="cost-monitor",
            budget=usage,
        )
        assert not authorize_submit(_submitter(W1), observation).allowed
        assert authorize_submit(_submitter(W2), observation).allowed


class TestReadScoping:
    """A submitter authenticated for W1 cannot read W2's observations."""

    def test_can_read_its_own_workspace(self) -> None:
        assert authorize_read(_submitter(W1), W1).allowed

    def test_cannot_read_another_workspaces_observations(self) -> None:
        """REQUIRED NEGATIVE — cross-workspace read is refused.

        No write is involved, which is exactly why this needs its own test: read
        scoping is easy to treat as cosmetic, but a fleet observation discloses a
        tenant's cluster inventory, reachability and spend.
        """
        decision = authorize_read(_submitter(W1), W2)
        assert not decision.allowed
        assert decision.reason == _OUT_OF_SCOPE

    def test_read_with_no_grant_is_refused(self) -> None:
        assert not authorize_read(_submitter(), W1).allowed

    def test_blank_workspace_is_never_readable(self) -> None:
        """A blank workspace is not covered even by a grant containing one.

        So a malformed grant cannot combine with a malformed request into an
        accidental allow.
        """
        assert not authorize_read(_submitter(""), "").allowed
        assert not authorize_read(_submitter(W1), "   ").allowed

    def test_write_authorization_does_not_confer_read(self, w1_observation) -> None:
        """The two decisions are independent functions on the same grant.

        Asserted so neither can later be implemented in terms of the other,
        which is how one of them ends up unchecked.
        """
        submitter = _submitter(W1)
        assert authorize_submit(submitter, w1_observation).allowed
        assert not authorize_read(submitter, W2).allowed


class TestVisibleWorkspaces:
    """List filtering never returns a workspace outside the grant."""

    def test_filters_to_the_grant(self) -> None:
        assert visible_workspaces(_submitter(W1), (W1, W2)) == (W1,)

    def test_preserves_requested_order(self) -> None:
        """Order follows the request so a caller's paging stays stable."""
        submitter = _submitter(W1, W2, "ws-w3")
        assert visible_workspaces(submitter, ("ws-w3", W1, W2)) == ("ws-w3", W1, W2)

    def test_empty_grant_sees_nothing(self) -> None:
        assert visible_workspaces(_submitter(), (W1, W2)) == ()

    def test_out_of_scope_entries_are_dropped_silently(self) -> None:
        """Dropped, not reported.

        Reporting which requested workspaces were refused would confirm they
        exist, which is the enumeration this contract avoids everywhere else too.
        """
        assert visible_workspaces(_submitter(W1), ("ws-does-not-exist", W2, W1)) == (
            W1,
        )


class TestRefusalsRevealNothing:
    """Every scoping refusal is indistinguishable from every other."""

    def test_nonexistent_and_forbidden_workspaces_refuse_identically(self) -> None:
        """ "Exists but not yours" and "does not exist" give the same answer.

        The difference is itself information about another tenant's estate, so an
        endpoint that reveals it is an enumeration oracle. Same choice the MCP
        tool surface's authz module makes, for the same reason.
        """
        submitter = _submitter(W1)
        forbidden = authorize_read(submitter, W2)
        nonexistent = authorize_read(submitter, "ws-no-such-workspace")
        assert forbidden.reason == nonexistent.reason == _OUT_OF_SCOPE

    def test_refusal_does_not_echo_the_requested_workspace(self) -> None:
        """A refusal reason carries no caller-supplied value.

        Otherwise the reason string reflects arbitrary input into the receiver's
        logs and the caller's error surface.
        """
        decision = authorize_read(_submitter(W1), "ws-attacker-supplied-name")
        assert not decision.allowed
        assert "ws-attacker-supplied-name" not in decision.reason

    def test_submit_and_read_refusals_are_the_same_string(self, w2_observation) -> None:
        """Which direction was refused is not disclosed either."""
        submitter = _submitter(W1)
        assert (
            authorize_submit(submitter, w2_observation).reason
            == authorize_read(submitter, W2).reason
        )


class TestAuthenticationAndScopingCompose:
    """The end-to-end shape a receiver implements: authenticate, then scope."""

    class _Resolver:
        def __init__(self, submitter: Submitter) -> None:
            self._submitter = submitter

        def resolve(self, credential: str) -> Submitter | None:
            return self._submitter if credential == "Bearer valid" else None

    def _headers(self, observation: Observation) -> dict[str, str]:
        return {
            VERSION_HEADER: CONTRACT_VERSION,
            AUTH_HEADER: "Bearer valid",
            SIGNATURE_HEADER: compute_signature(observation, TEST_SIGNING_KEY),
        }

    def test_authenticated_but_out_of_scope_submission_is_refused(
        self, w2_observation
    ) -> None:
        """The gap authentication alone leaves, closed by the second check.

        The submission authenticates cleanly — valid credential, valid signature
        — and is then refused on scope. A receiver that stopped after
        `verify_submission` would have written W2's fleet state on W1's authority.
        """
        auth = verify_submission(
            canonical_body(w2_observation),
            self._headers(w2_observation),
            self._Resolver(_submitter(W1)),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert auth.authenticated
        assert auth.submitter is not None

        scope = authorize_submit(auth.submitter, w2_observation)
        assert not scope.allowed
        assert scope.reason == _OUT_OF_SCOPE

    def test_authenticated_and_in_scope_submission_is_accepted(
        self, w1_observation
    ) -> None:
        auth = verify_submission(
            canonical_body(w1_observation),
            self._headers(w1_observation),
            self._Resolver(_submitter(W1)),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert auth.authenticated
        assert auth.submitter is not None
        assert authorize_submit(auth.submitter, w1_observation).allowed

    def test_scoping_is_not_reachable_without_authentication(
        self, w1_observation
    ) -> None:
        """There is no `Submitter` to scope with until authentication produced one.

        `AuthResult.submitter` is None on refusal, so a receiver cannot call
        `authorize_submit` with an identity it never established — the type makes
        the ordering hard to get wrong rather than merely documented.
        """
        auth = verify_submission(
            canonical_body(w1_observation),
            {VERSION_HEADER: CONTRACT_VERSION},
            self._Resolver(_submitter(W1)),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert not auth.authenticated
        assert auth.submitter is None
        with pytest.raises(AttributeError):
            authorize_submit(auth.submitter, w1_observation)  # type: ignore[arg-type]
