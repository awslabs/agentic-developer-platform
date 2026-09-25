"""Inherited authority is bound to the job's OWN context — Issue #5663 (A09).

Finding ``f-7c46ead6-06bf-4726-94a7-b09ea88efbb5``.

#4129 stopped the agent pod from supplying the *values* of the authority a new run
inherits: ``root_human_id`` / ``is_human_rooted`` / ``chain_depth`` are read from the
``correlation-index`` GSI on ``webhook-events``, which only this Lambda writes. What
it did not stop is the pod choosing WHICH row those values come from. The pointer row
is pod-writable (``dynamodb:UpdateItem`` on ``adp-*-correlation-pointers``) and its
``correlation_id`` is what selects the chain — and a correlation id is not a secret:
it is printed in PR bodies, issue comments, marker HTML and agent logs. So an agent
worker, whose instructions can be shaped by text an outsider pasted into an issue,
could write a pointer naming ANOTHER tenant's human-rooted chain, trigger its own
channel, and have the webhook persist that tenant's human as this run's
``authorized_user_id`` — which the credential broker later reads as licence to
release that human's vault secrets.

What this module asserts (the issue's Validation wording):

  * "authority is never inherited from a lineage record whose tenant, installation
    or repository does not match the new job's own context" — ``TestTenantPredicate``,
    ``TestInstallationPredicate``, ``TestRepositoryPredicate``.
  * "a job whose lineage was influenced by a worker-writable pointer starts with no
    inherited human authority" — ``TestEndToEndVaultAuthority``, asserted through
    ``_compute_authorized_user_id``, the function that actually persists the column,
    not merely through the intermediate correlation context.
  * "add a test asserting the credential broker refuses to release material for a
    human the run's own record does not legitimately name" — that is the GATEWAY
    half of the same finding and is asserted against the real broker dependency in
    ``modules/gateway/tests/internal/test_broker_user_binding.py``, since
    ``broker_identity`` is not importable from this Lambda's zip (rooted at
    ``lambda/``).

And the regression half, which matters just as much because getting it wrong is a
silent outage rather than a visible failure:

  * legitimate same-tenant continuation still inherits everything
    (``TestLegitimateCallersUnaffected``),
  * legitimate #1828 cross-REPO continuation inside one tenant still inherits
    (``TestRepositoryPredicate.test_sibling_repo_in_the_same_tenant_still_inherits``),
  * lineage (chain id + parent edge) and the depth counter survive a refusal
    (``TestRefusalNarrowsWithoutBreakingLineage``) — dropping the chain id would
    fragment #1828 lineage, and resetting the depth would WIDEN the recursion bound
    the refusal is supposed to narrow.

CI path: under ``lambda/`` so ``webhook-ingress-ci.yml``'s
``pytest lambda/ -m "not integration"`` executes it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from common.spawn_persona import _compute_authorized_user_id  # noqa: E402
from handler import (  # noqa: E402
    _emit_lineage_binding_metric as _REAL_EMITTER,
    _job_lineage_context,
    _lineage_context_mismatch,
    _pr_marker_text_with_issue_fallback,
    _resolve_pointer_provenance,
    determine_correlation,
)

# The job under test belongs to tenant "acme" and runs on acme/app.
TENANT = "acme"
REPO = "acme/app"
INSTALLATION = "1111"

# The chain it must not be able to borrow authority from.
OTHER_TENANT = "globex"
OTHER_REPO = "globex/secrets"
OTHER_INSTALLATION = "2222"

VICTIM = "globex-human-id"
OWN_HUMAN = "acme-human-id"
BOT = "bot-sender-id"
CHANNEL = "github:repo=acme/app,issue=7"


class _Identity:
    """Minimal ``ResolvedIdentity`` stand-in.

    ``tenant_id`` is the field the identity index resolves from the installation id,
    i.e. the one value in the job context that does not come from the payload.
    """

    def __init__(self, *, tenant_id=TENANT, user_kind="bot", user_id=BOT):
        self.tenant_id = tenant_id
        self.user_kind = user_kind
        self.user_id = user_id


def _payload(*, repo=REPO, installation=INSTALLATION) -> dict:
    """A webhook payload, as it looks AFTER HMAC signature verification."""
    return {"repository": {"full_name": repo}, "installation": {"id": installation}}


def _job_context(*, tenant=TENANT, repo=REPO, installation=INSTALLATION) -> dict:
    return {"tenant_id": tenant, "repo": repo, "installation_id": installation}


def _pointer(correlation_id="corr-VICTIM-CHAIN", **overrides) -> dict:
    """The row a worker can write today — chain id + parent edge only (post-#4129).

    Note what is NOT forged here: no ``root_human_id``, no ``is_human_rooted``, no
    ``chain_depth``. #4129 already made those inert. The only lever left is the
    ``correlation_id``, which is the lever this module is about.
    """
    row: dict[str, Any] = {
        "correlation_id": correlation_id,
        "triggering_invocation_id": "inv-parent",
        "last_triggered_persona": None,
        "recent_triggered_personas": set(),
        "recent_trigger_count": 0,
    }
    row.update(overrides)
    return row


def _victim_chain(**overrides) -> dict:
    """A real, server-written, human-rooted chain belonging to ANOTHER tenant."""
    row: dict[str, Any] = {
        "event_id": "inv-parent",
        "correlation_id": "corr-VICTIM-CHAIN",
        "root_human_id": VICTIM,
        "is_human_rooted": True,
        "chain_depth": 1,
        "tenant_id": OTHER_TENANT,
        "installation_id": OTHER_INSTALLATION,
        "repo": OTHER_REPO,
    }
    row.update(overrides)
    return row


def _own_chain(**overrides) -> dict:
    """A real, server-written, human-rooted chain belonging to THIS job's tenant."""
    row: dict[str, Any] = {
        "event_id": "inv-parent",
        "correlation_id": "corr-OWN-CHAIN",
        "root_human_id": OWN_HUMAN,
        "is_human_rooted": True,
        "chain_depth": 1,
        "tenant_id": TENANT,
        "installation_id": INSTALLATION,
        "repo": REPO,
    }
    row.update(overrides)
    return row


def _resolve(chain, *, pointer=None, job_context=None, fallback=BOT):
    """Run the lineage-authority resolution against a stubbed chain row."""
    with patch("handler._resolve_chain_record", return_value=chain):
        return _resolve_pointer_provenance(
            pointer if pointer is not None else _pointer(),
            fallback,
            _job_context() if job_context is None else job_context,
        )


def _determine(chain, *, pointer=None, payload=None, identity=None):
    store = MagicMock()
    store.read_pointer.return_value = pointer if pointer is not None else _pointer()
    with patch("handler._get_correlation_store", return_value=store):
        with patch("handler._resolve_chain_record", return_value=chain):
            return determine_correlation(
                payload if payload is not None else _payload(),
                identity or _Identity(),
                CHANNEL,
            )


@pytest.fixture(autouse=True)
def _no_real_metrics(monkeypatch):
    """Keep the CloudWatch emitter out of every case except the ones about it."""
    monkeypatch.setattr("handler._emit_lineage_binding_metric", lambda name: None)


@pytest.fixture(autouse=True)
def _default_config(monkeypatch):
    """Assert the SHIPPED default, don't configure one.

    The issue requires the check to hold "on the default deployment configuration,
    not only when an optional hardening flag is switched on", so these tests must
    not set ``LINEAGE_CONTEXT_BINDING``. Deleting it proves absence is enforcement.
    """
    monkeypatch.delenv("LINEAGE_CONTEXT_BINDING", raising=False)


# =============================================================================
# The escalation itself
# =============================================================================


class TestTenantPredicate:
    """A chain belonging to another tenant lends this job nothing."""

    def test_cross_tenant_chain_does_not_lend_its_human_root(self):
        """THE ESCALATION: naming another tenant's chain must not borrow its human."""
        root, rooted, _ = _resolve(_victim_chain())
        assert root != VICTIM
        assert rooted is False

    def test_chain_without_a_tenant_lends_nothing(self):
        """Absence must narrow, never widen.

        Mirrors ``agent_trigger``'s #4128 rule ("chain record has no tenant_id" →
        refuse). If a missing attribute were treated as "no objection", omitting it
        would be the trivial way around the check — and rows written before tenant
        stamping are exactly the rows we know least about.
        """
        root, rooted, _ = _resolve(_own_chain(tenant_id=""))
        assert root != OWN_HUMAN
        assert rooted is False

    def test_job_without_a_resolved_tenant_inherits_nothing(self):
        """A job we cannot place in a tenant cannot be given a human's authority."""
        root, rooted, _ = _resolve(_own_chain(), job_context=_job_context(tenant=""))
        assert root != OWN_HUMAN
        assert rooted is False

    def test_mismatch_reason_names_the_tenants_and_no_secrets(self):
        reason = _lineage_context_mismatch(_victim_chain(), _job_context())
        assert reason is not None
        assert OTHER_TENANT in reason and TENANT in reason


class TestInstallationPredicate:
    """The installation is compared when both sides have one, and only then."""

    def test_different_installation_in_the_same_tenant_is_a_mismatch(self):
        root, rooted, _ = _resolve(_own_chain(installation_id=OTHER_INSTALLATION))
        assert root != OWN_HUMAN
        assert rooted is False

    def test_chain_without_an_installation_still_inherits(self):
        """``log_event`` writes ``installation_id`` conditionally.

        Rows from producers that have no GitHub App installation simply lack the
        attribute, so requiring it would silently strip authority from legitimate
        non-installation-rooted chains. Absence widens nothing here because the
        tenant predicate above already had to match EXACTLY, and installations are
        per-tenant.
        """
        chain = _own_chain()
        chain.pop("installation_id")
        root, rooted, _ = _resolve(chain)
        assert root == OWN_HUMAN
        assert rooted is True

    def test_installation_compared_as_text_not_as_number(self):
        """DDB numbers arrive as ``Decimal``/``int``; the payload's is an ``int``.

        A raw ``!=`` between ``Decimal("1111")`` and ``"1111"`` would report every
        legitimate chain as a mismatch — an authority outage that no security test
        would catch because it errs "safe".
        """
        from decimal import Decimal

        root, rooted, _ = _resolve(_own_chain(installation_id=Decimal("1111")))
        assert rooted is True
        assert root == OWN_HUMAN


class TestRepositoryPredicate:
    """Repo binding reuses ``agent_trigger._repo_in_tenant`` — one rule, one place."""

    def test_repo_outside_the_chains_tenant_is_a_mismatch(self):
        """Same tenant string on both sides is not enough if the repo is foreign.

        Constructed so the tenant predicate PASSES and only the repo predicate can
        refuse, otherwise this test would pass for the previous class's reason.
        """
        chain = _own_chain(repo=OTHER_REPO)
        with patch("common.installation_resolver.resolve_installation_for_tenant", return_value=None):
            root, rooted, _ = _resolve(
                chain, job_context=_job_context(repo="unrelated-owner/app")
            )
        assert root != OWN_HUMAN
        assert rooted is False

    def test_same_repo_still_inherits(self):
        root, rooted, _ = _resolve(_own_chain())
        assert root == OWN_HUMAN
        assert rooted is True

    def test_sibling_repo_in_the_same_tenant_still_inherits(self):
        """#1828 cross-REPO continuation inside one org must keep working.

        This is the case a blunt repo-equality check would break: an issue in
        ``acme/app`` continuing into ``acme/infra`` is normal platform behaviour, and
        breaking it would look like chains quietly stopping rather than like an error.
        """
        root, rooted, _ = _resolve(
            _own_chain(repo="acme/infra"), job_context=_job_context(repo="acme/app")
        )
        assert root == OWN_HUMAN
        assert rooted is True

    def test_chain_without_a_repo_still_inherits(self):
        """``repo`` is written conditionally too — absence is not a claim."""
        chain = _own_chain()
        chain.pop("repo")
        root, rooted, _ = _resolve(chain)
        assert rooted is True

    def test_repo_predicate_failure_is_a_mismatch_not_a_crash(self):
        """An unresolvable owner must refuse authority, not 500 the webhook.

        A 500 here would drop the delivery entirely: a denial-of-service on normal
        dispatch, which the issue's impact table lists as its own bug class.
        """
        chain = _own_chain(repo=OTHER_REPO)
        with patch(
            "common.installation_resolver.resolve_installation_for_tenant",
            side_effect=RuntimeError("DDB unavailable"),
        ):
            root, rooted, _ = _resolve(
                chain, job_context=_job_context(repo="unrelated-owner/app")
            )
        assert rooted is False
        assert root == BOT


# =============================================================================
# End-to-end: the column the credential broker actually reads
# =============================================================================


class TestEndToEndVaultAuthority:
    """Asserted through the function that persists ``authorized_user_id``."""

    def test_cross_tenant_pointer_yields_no_vault_authority(self):
        ctx = _determine(_victim_chain())
        assert ctx["root_human_id"] != VICTIM
        assert ctx["is_human_rooted"] is False
        authorized = _compute_authorized_user_id(
            correlation_ctx=ctx,
            cognito_sub="",  # bot sender — no human at the door
            max_credential_chain_depth=5,
        )
        assert authorized != VICTIM
        assert authorized == ""

    def test_same_tenant_pointer_still_yields_the_real_humans_authority(self):
        """The legitimate counterpart — without this the fix is indistinguishable
        from simply breaking vault delivery."""
        ctx = _determine(_own_chain(), pointer=_pointer("corr-OWN-CHAIN"))
        authorized = _compute_authorized_user_id(
            correlation_ctx=ctx,
            cognito_sub="",
            max_credential_chain_depth=5,
        )
        assert authorized == OWN_HUMAN

    def test_pr_fallback_marker_cannot_launder_a_cross_tenant_root(self):
        """The synthesized PR marker is TRUSTED by construction (#4128).

        So if the predicate did not run on this path, the cross-tenant root would be
        re-emitted inside a marker the next hop trusts without verification — the
        "incomplete rollout" failure mode, where the fix looks done in review while
        one endpoint keeps the route open.
        """
        store = MagicMock()
        store.channel_key.side_effect = lambda prov, repo, kind, num: (
            f"{prov}:repo={repo},{kind}={num}"
        )
        store.read_pointer.return_value = _pointer()
        with patch("handler._resolve_chain_record", return_value=_victim_chain()):
            marker, trusted = _pr_marker_text_with_issue_fallback(
                store, REPO, "## Summary", "agent/issue-7", BOT, _job_context()
            )
        assert trusted is True
        assert VICTIM not in marker
        assert "adp-is-human-rooted:false" in marker


# =============================================================================
# The MARKER paths — Rules 2 and 4 (the second half of the same finding)
# =============================================================================


def _signed_marker(correlation_id="corr-VICTIM-CHAIN", root_human_id=VICTIM) -> dict:
    """A marker carrying a VALID signature — the case the first fix missed.

    The signature is stubbed as verified rather than computed, because verification
    is not what these tests are about and because a real one is trivially mintable by
    any worker: the signed input is
    ``"{correlation_id}:{root_human_id}:{is_human_rooted}:{invocation_id}:{chain_depth}"``
    (no tenant, repo or installation) and the key is a single fleet-wide secret
    readable by every worker in every tenant. That combination is why "signed" cannot
    stand in for "authorized" here.
    """
    return {
        "correlation_id": correlation_id,
        "root_human_id": root_human_id,
        "is_human_rooted": True,
        "invocation_id": "inv-parent",
        "chain_depth": 1,
        "signature": "a-valid-looking-signature",
    }


def _determine_with_marker(chain, *, pointer=None, marker=None, verified=True):
    """Drive ``determine_correlation`` down a MARKER branch (Rule 2 or Rule 4).

    ``pointer=None`` selects Rule 4 (marker only); a pointer whose correlation_id
    DIFFERS from the marker's selects Rule 2 (cross-channel hop).
    """
    store = MagicMock()
    store.read_pointer.return_value = pointer
    with patch("handler._get_correlation_store", return_value=store):
        with patch("handler._resolve_chain_record", return_value=chain):
            with patch(
                "common.marker_parse.parse_marker",
                return_value=marker if marker is not None else _signed_marker(),
            ):
                with patch("common.marker_verify.verify_marker", return_value=verified):
                    return determine_correlation(
                        _payload(),
                        _Identity(),
                        CHANNEL,
                        marker_text="<!-- adp-correlation:... -->",
                    )


class TestMarkerPathsAreBoundToo:
    """Rules 2 and 4 returned marker-borne authority without consulting the chain.

    The first revision of this fix bound only :func:`_resolve_pointer_provenance`.
    These two branches read ``root_human_id`` / ``is_human_rooted`` straight out of
    the marker dict and never called the predicate at all — so the finding was half
    closed, with a VALID signature as the only thing in the way, and a valid
    signature is mintable by any worker in any tenant (see :func:`_signed_marker`).
    """

    def test_rule4_signed_marker_cannot_borrow_another_tenants_human(self):
        """Marker only, no pointer, valid signature, another tenant's chain."""
        ctx = _determine_with_marker(_victim_chain())
        assert ctx["root_human_id"] != VICTIM
        assert ctx["is_human_rooted"] is False

    def test_rule2_signed_marker_cannot_borrow_another_tenants_human(self):
        """Pointer present but naming a DIFFERENT chain, so the marker wins."""
        ctx = _determine_with_marker(
            _victim_chain(), pointer=_pointer("corr-LOCAL-CHAIN")
        )
        assert ctx["root_human_id"] != VICTIM
        assert ctx["is_human_rooted"] is False

    def test_rule4_marker_naming_an_unknown_chain_gets_no_authority(self):
        """No server-written row at all — fail closed, same as the pointer path.

        This is also the case that proves the marker's own claim is inert: the marker
        says ``is_human_rooted=True`` and names a human, and the result is neither.
        """
        ctx = _determine_with_marker(None)
        assert ctx["root_human_id"] != VICTIM
        assert ctx["is_human_rooted"] is False

    def test_a_legitimate_marker_hop_in_the_right_tenant_still_inherits(self):
        """The regression half. Without this the fix is indistinguishable from
        breaking cross-channel lineage outright."""
        ctx = _determine_with_marker(
            _own_chain(), marker=_signed_marker(correlation_id="corr-OWN-CHAIN")
        )
        assert ctx["root_human_id"] == OWN_HUMAN
        assert ctx["is_human_rooted"] is True

    def test_the_marker_still_selects_the_chain_and_the_parent_edge(self):
        """A refusal narrows authority only — lineage must survive it intact."""
        ctx = _determine_with_marker(_victim_chain())
        assert ctx["correlation_id"] == "corr-VICTIM-CHAIN"
        assert ctx["parent_invocation_id"] == "inv-parent"
        assert ctx["is_new_chain"] is False
        # Depth is still inherited: resetting it would WIDEN the recursion bound.
        assert ctx["chain_depth"] == 1

    def test_the_marker_cannot_raise_authority_above_its_own_chain(self):
        """A same-tenant chain that is NOT human-rooted cannot be made so.

        The tenant predicate passes here, so this isolates the other half of the
        rule: values come from the row, and the marker's claim never upgrades them.
        """
        service_chain = _own_chain(
            correlation_id="corr-OWN-CHAIN", root_human_id="", is_human_rooted=False
        )
        ctx = _determine_with_marker(
            service_chain, marker=_signed_marker(correlation_id="corr-OWN-CHAIN")
        )
        assert ctx["is_human_rooted"] is False
        assert ctx["root_human_id"] != VICTIM

    def test_an_unsigned_marker_is_still_refused(self):
        """#3179's fail-closed policy is preserved, not replaced."""
        ctx = _determine_with_marker(_victim_chain(), verified=None)
        assert ctx["is_human_rooted"] is False
        assert ctx["root_human_id"] != VICTIM

    def test_marker_authority_yields_no_vault_access(self):
        """End-to-end through the function that persists ``authorized_user_id``."""
        ctx = _determine_with_marker(_victim_chain())
        authorized = _compute_authorized_user_id(
            correlation_ctx=ctx,
            cognito_sub="",
            max_credential_chain_depth=5,
        )
        assert authorized != VICTIM
        assert authorized == ""


# =============================================================================
# A refusal must narrow authority — and nothing else
# =============================================================================


class TestRefusalNarrowsWithoutBreakingLineage:
    def test_chain_id_and_parent_edge_survive_a_refusal(self):
        """Lineage is not authority. Dropping the chain id would fragment #1828
        lineage into one-chain-per-event and take the self-re-trigger guard with it —
        a silent degradation, not a visible failure."""
        ctx = _determine(_victim_chain())
        assert ctx["correlation_id"] == "corr-VICTIM-CHAIN"
        assert ctx["is_new_chain"] is False
        assert ctx["parent_invocation_id"] == "inv-parent"

    def test_depth_is_still_taken_from_the_chain_row_on_a_refusal(self):
        """A refusal must not reset the counter that bounds recursion.

        Returning "unknown depth" here would read as depth 0 downstream — so a
        mismatch would WIDEN the runaway-chain and credential-horizon guards, turning
        this control into the depth reset #4129 exists to prevent. The narrowing is
        confined to the human root.
        """
        _, rooted, depth = _resolve(_victim_chain(chain_depth=9))
        assert rooted is False
        assert depth == 9

    def test_loop_tracking_values_still_round_trip(self):
        """#1716/#2149: not authority-bearing, so a refusal must not disturb them."""
        pointer = _pointer(
            last_triggered_persona="developer",
            recent_triggered_personas={"developer"},
            recent_trigger_count=2,
        )
        ctx = _determine(_victim_chain(), pointer=pointer)
        assert ctx["last_triggered_persona"] == "developer"
        assert ctx["recent_trigger_count"] == 2


# =============================================================================
# The job context is server-derived, and the check is on by default
# =============================================================================


class TestJobContextIsServerDerived:
    def test_context_comes_from_the_identity_and_the_signed_payload(self):
        ctx = _job_lineage_context(_payload(), _Identity())
        assert ctx == {"tenant_id": TENANT, "repo": REPO, "installation_id": INSTALLATION}

    def test_tenant_comes_from_the_resolved_identity_not_the_payload(self):
        """The payload is attacker-authored content that GitHub merely signs; the
        tenant must come from the identity index instead."""
        payload = dict(_payload(), tenant_id="attacker-chosen", organization={"login": "globex"})
        ctx = _job_lineage_context(payload, _Identity(tenant_id=TENANT))
        assert ctx["tenant_id"] == TENANT

    def test_absent_payload_fields_become_empty_not_missing(self):
        """A KeyError here would be a 500 on a real event shape (e.g. a ping)."""
        ctx = _job_lineage_context({}, _Identity(tenant_id=""))
        assert ctx == {"tenant_id": "", "repo": "", "installation_id": ""}

    def test_determine_correlation_derives_the_context_itself(self):
        """No caller can pass the wrong context, because no caller passes one.

        Asserted by giving the PAYLOAD a foreign repo while the chain row is
        otherwise this tenant's: if the context were not derived from the payload the
        mismatch could not be seen at all.
        """
        ctx = _determine(
            _own_chain(repo=OTHER_REPO),
            payload=_payload(repo="unrelated-owner/app"),
            identity=_Identity(tenant_id=TENANT),
        )
        assert ctx["is_human_rooted"] is False


class TestShippedDefaultEnforces:
    """The acceptance criterion is "holds on the default deployment configuration"."""

    def test_enforced_with_no_environment_variable_set(self, monkeypatch):
        monkeypatch.delenv("LINEAGE_CONTEXT_BINDING", raising=False)
        from handler import _lineage_binding_enforced

        assert _lineage_binding_enforced() is True

    def test_log_only_is_the_documented_rollback(self, monkeypatch):
        """Rollback is a configuration change with no redeploy (#5663 Deployment)."""
        monkeypatch.setenv("LINEAGE_CONTEXT_BINDING", "log_only")
        root, rooted, _ = _resolve(_victim_chain())
        assert rooted is True
        assert root == VICTIM  # allowed, but counted — see the metric test below

    def test_an_unrecognized_value_enforces(self, monkeypatch):
        """A typo must not silently disable the control."""
        monkeypatch.setenv("LINEAGE_CONTEXT_BINDING", "log-only")  # hyphen, not underscore
        from handler import _lineage_binding_enforced

        assert _lineage_binding_enforced() is True


class TestDecisionsAreCounted:
    """Without a denominator, a deny count of zero is indistinguishable from
    telemetry that never arrived — which is how the pre-enforcement reading in the
    issue's rollout plan is supposed to be judged."""

    def _emitted(self, monkeypatch) -> list[str]:
        seen: list[str] = []
        monkeypatch.setattr("handler._emit_lineage_binding_metric", seen.append)
        return seen

    def test_refusal_is_counted_as_denied(self, monkeypatch):
        seen = self._emitted(monkeypatch)
        _resolve(_victim_chain())
        assert seen == ["LineageContextMismatchDenied"]

    def test_log_only_counts_would_deny_while_allowing(self, monkeypatch):
        seen = self._emitted(monkeypatch)
        monkeypatch.setenv("LINEAGE_CONTEXT_BINDING", "log_only")
        _, rooted, _ = _resolve(_victim_chain())
        assert rooted is True
        assert seen == ["LineageContextMismatchWouldDeny"]

    def test_legitimate_inheritance_is_counted_too(self, monkeypatch):
        seen = self._emitted(monkeypatch)
        _resolve(_own_chain())
        assert seen == ["LineageContextMatched"]

    def test_unresolvable_chain_emits_nothing_from_this_control(self, monkeypatch):
        """#4129 already refuses an unresolvable chain, so counting it here would
        inflate this control's numbers with decisions it did not make."""
        seen = self._emitted(monkeypatch)
        _resolve(None)
        assert seen == []

    def test_a_metric_failure_cannot_change_the_decision(self, monkeypatch):
        """Telemetry must never be able to authorize or refuse.

        Asserted against the REAL emitter with a broken CloudWatch client, not
        against a stub — the property is that the emitter swallows its own failure,
        and stubbing the emitter would assert nothing about the emitter.
        """
        monkeypatch.setattr("handler._emit_lineage_binding_metric", _REAL_EMITTER)
        monkeypatch.setattr("handler._get_metrics", MagicMock(side_effect=RuntimeError("down")))
        root, rooted, _ = _resolve(_own_chain())
        assert rooted is True
        assert root == OWN_HUMAN


# =============================================================================
# Legitimate supported callers, unchanged
# =============================================================================


class TestLegitimateCallersUnaffected:
    def test_human_sender_is_untouched_by_this_control(self):
        """A human event always starts a new chain and never inherits, so the
        predicate must not be able to affect it."""
        ctx = _determine(
            _victim_chain(), identity=_Identity(user_kind="human", user_id=OWN_HUMAN)
        )
        assert ctx["is_new_chain"] is True
        assert ctx["is_human_rooted"] is True
        assert ctx["root_human_id"] == OWN_HUMAN

    def test_normal_same_channel_continuation_inherits_everything(self):
        ctx = _determine(_own_chain(chain_depth=3), pointer=_pointer("corr-OWN-CHAIN"))
        assert ctx["correlation_id"] == "corr-OWN-CHAIN"
        assert ctx["root_human_id"] == OWN_HUMAN
        assert ctx["is_human_rooted"] is True
        assert ctx["chain_depth"] == 3

    def test_service_rooted_chain_in_the_right_tenant_is_unchanged(self):
        """An EventBridge/scheduled chain has no human root to withhold; the control
        must neither grant nor break anything for it."""
        chain = _own_chain(root_human_id="", is_human_rooted=False)
        root, rooted, _ = _resolve(chain)
        assert rooted is False
        assert root == BOT

    def test_no_mismatch_reason_for_a_fully_matching_context(self):
        assert _lineage_context_mismatch(_own_chain(), _job_context()) is None
