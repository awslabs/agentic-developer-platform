"""Chain provenance is server-sourced, not read off the pointer row — Issue #4129.

The agent pod holds ``dynamodb:UpdateItem`` on ``adp-*-correlation-pointers``, so
every attribute on a pointer row is attacker-controlled from the platform's point
of view. Three of them decide **whose authority a run holds** and **how deep the
chain has gone**: ``root_human_id``, ``is_human_rooted``, ``chain_depth``.

The escalation this closes is one hop through that row: a compromised pod writes
``root_human_id=<victim> is_human_rooted=true chain_depth=0``, triggers the
channel, and the webhook persists the victim's id as a *legitimate server-side*
``authorized_user_id`` — the reset depth keeping it under the credential depth
limit. The resulting row is indistinguishable from a real one.

``determine_correlation`` now resolves all three from the ``correlation-index``
GSI on ``webhook-events`` (written only by this Lambda) via
``handler._resolve_chain_record`` → ``agent_trigger._resolve_chain``. The pointer
supplies only lineage: ``correlation_id`` and the parent edge, neither of which
grants anything.

Both halves are asserted here, because either one alone is a silent outage:
  - the forgery is inert (the security property), AND
  - legitimate #1828 cross-issue lineage still connects when the pointer row has
    NO provenance at all, which is the shape ``seed_trigger_pointer.py`` now
    writes (the regression property).

CI path: under ``lambda/`` so ``webhook-ingress-ci.yml``'s
``pytest lambda/ -m "not integration"`` executes it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from common.spawn_persona import _compute_authorized_user_id  # noqa: E402
from handler import (  # noqa: E402
    _pr_marker_text_with_issue_fallback,
    determine_correlation,
)

VICTIM = "victim-human-id"
BOT = "bot-sender-id"
REAL_HUMAN = "real-human-id"
CHANNEL = "github:repo=org/repo,issue=1"


def _patch_chain(chain):
    """Stub the server-written chain lookup.

    ``create=True`` so this file fails pre-fix with real ASSERTION failures
    (``authorized_user_id == VICTIM``) rather than an ``AttributeError`` at patch
    time — an import/patch error proves only that a symbol is missing, not that
    the escalation was live.
    """
    return patch("handler._resolve_chain_record", return_value=chain, create=True)


def _resolve(pointer, fallback):
    """Call ``handler._resolve_pointer_provenance`` (imported at call time).

    Deliberately not a module-level import: the classes above assert the
    end-to-end escalation through ``determine_correlation`` and must still
    COLLECT-and-FAIL against pre-fix code, which has no such symbol.
    """
    import handler

    return handler._resolve_pointer_provenance(pointer, fallback)


class _Identity:
    def __init__(self, user_kind="bot", user_id=BOT):
        self.user_kind = user_kind
        self.user_id = user_id


def _forged_pointer(**overrides) -> dict:
    """The row a compromised pod can write today via its UpdateItem grant."""
    row: dict[str, Any] = {
        "correlation_id": "corr-BOT-CHAIN",
        "root_human_id": VICTIM,  # forged
        "is_human_rooted": True,  # forged
        "chain_depth": 0,  # forged (reset, to stay under the cred depth limit)
        "triggering_invocation_id": "inv-pod",
        "last_triggered_persona": None,
        "recent_triggered_personas": set(),
        "recent_trigger_count": 0,
    }
    row.update(overrides)
    return row


def _bare_pointer(**overrides) -> dict:
    """The row the post-#4129 worker actually writes — no provenance at all."""
    row: dict[str, Any] = {
        "correlation_id": "corr-REAL-CHAIN",
        "triggering_invocation_id": "inv-parent",
        "last_triggered_persona": None,
        "recent_triggered_personas": set(),
        "recent_trigger_count": 0,
    }
    row.update(overrides)
    return row


def _chain_row(**overrides) -> dict:
    """A server-written ``webhook-events`` row as the GSI returns it."""
    row: dict[str, Any] = {
        "event_id": "inv-parent",
        "correlation_id": "corr-REAL-CHAIN",
        "root_human_id": REAL_HUMAN,
        "is_human_rooted": True,
        "chain_depth": 2,
    }
    row.update(overrides)
    return row


def _determine(pointer, chain, *, marker_text=None, identity=None):
    store = MagicMock()
    store.read_pointer.return_value = pointer
    with patch("handler._get_correlation_store", return_value=store):
        with _patch_chain(chain):
            return determine_correlation(
                {}, identity or _Identity(), CHANNEL, marker_text=marker_text
            )


# =============================================================================
# The exploit itself
# =============================================================================


class TestForgedPointerIsInert:
    """A pod-forged pointer row cannot mint authority it wasn't granted."""

    def test_forged_root_human_does_not_become_authorized_user(self):
        """THE EXPLOIT: forged root_human_id=victim → authorized_user_id != victim.

        This is the end-to-end property, asserted through the SAME function that
        persists the field (``_compute_authorized_user_id``), not just through the
        intermediate correlation context — the escalation is only closed if the
        value that reaches the vault-authorization column is not the victim's.
        """
        # The chain this correlation ACTUALLY belongs to: a bot-rooted chain,
        # which is why the pod had to forge a human root to escalate at all.
        chain = _chain_row(
            correlation_id="corr-BOT-CHAIN",
            root_human_id="",
            is_human_rooted=False,
            chain_depth=3,
        )
        ctx = _determine(_forged_pointer(), chain)

        assert ctx["root_human_id"] != VICTIM
        assert ctx["is_human_rooted"] is False

        authorized = _compute_authorized_user_id(
            correlation_ctx=ctx,
            cognito_sub="",  # bot sender — no human at the door
            max_credential_chain_depth=5,
        )
        assert authorized != VICTIM
        assert authorized == ""  # bot-rooted → no vault access at all

    def test_forged_depth_reset_does_not_evade_the_credential_depth_limit(self):
        """Forged chain_depth=0 on a deep chain → the server's depth still governs.

        Resetting the counter is the other half of the escalation: it keeps a
        chain that has run past ``max_credential_chain_depth`` eligible for vault
        credentials. Depth must come from the server-written row.
        """
        chain = _chain_row(
            correlation_id="corr-BOT-CHAIN",
            root_human_id=REAL_HUMAN,
            is_human_rooted=True,
            chain_depth=9,  # server says: way past the limit
        )
        ctx = _determine(_forged_pointer(chain_depth=0), chain)

        assert ctx["chain_depth"] == 10  # 9 + 1, not 1
        authorized = _compute_authorized_user_id(
            correlation_ctx=ctx,
            cognito_sub="",
            max_credential_chain_depth=5,
        )
        assert authorized == ""  # over the limit → no vault

    def test_forged_human_rooted_on_a_bot_chain_is_stripped(self):
        """is_human_rooted comes from the chain row, never from the pointer."""
        chain = _chain_row(is_human_rooted=False, root_human_id="")
        ctx = _determine(_forged_pointer(correlation_id="corr-REAL-CHAIN"), chain)
        assert ctx["is_human_rooted"] is False

    def test_pointer_only_path_is_closed_too(self):
        """The same forgery via the pointer-only branch (no marker) is also inert.

        Rule 1 and Rule 3 are separate code paths; a fix applied to one and not
        the other leaves the escalation fully open through the other.
        """
        chain = _chain_row(
            correlation_id="corr-BOT-CHAIN",
            root_human_id="",
            is_human_rooted=False,
            chain_depth=1,
        )
        ctx = _determine(_forged_pointer(), chain, marker_text=None)
        assert ctx["root_human_id"] != VICTIM
        assert ctx["is_human_rooted"] is False

    def test_forged_pointer_reaching_the_pr_fallback_is_inert(self):
        """The synthesized PR marker is trusted by construction (#4128).

        So if IT read authority off the pointer row, the forgery would be
        forwarded into a trusted marker and land on the PR channel — the fix has
        to cover this path, not just determine_correlation's.
        """
        store = MagicMock()
        store.channel_key.side_effect = lambda prov, repo, kind, num: (
            f"{prov}:repo={repo},{kind}={num}"
        )
        store.read_pointer.return_value = _forged_pointer()
        chain = _chain_row(
            correlation_id="corr-BOT-CHAIN",
            root_human_id="",
            is_human_rooted=False,
            chain_depth=4,
        )
        with _patch_chain(chain):
            marker, trusted = _pr_marker_text_with_issue_fallback(
                store, "org/repo", "## Summary", "agent/issue-77", BOT
            )
        assert trusted is True
        assert VICTIM not in marker
        assert "adp-is-human-rooted:false" in marker
        assert "adp-chain-depth:4" in marker  # server depth, not the forged 0


# =============================================================================
# The regression half — lineage must survive provenance being absent
# =============================================================================


class TestLineageSurvivesAbsentProvenance:
    """#1828 cross-issue dispatch still connects on a provenance-less row.

    This is the failure mode the issue calls out as silent: strip the fields
    without server-side resolution and chains quietly stop connecting, with the
    self-re-trigger guard going down alongside them and nothing raising.
    """

    def test_bare_pointer_still_inherits_the_chain(self):
        ctx = _determine(_bare_pointer(), _chain_row())
        assert ctx["correlation_id"] == "corr-REAL-CHAIN"
        assert ctx["is_new_chain"] is False
        assert ctx["parent_invocation_id"] == "inv-parent"

    def test_bare_pointer_resolves_the_human_root_from_the_chain(self):
        ctx = _determine(_bare_pointer(), _chain_row())
        assert ctx["root_human_id"] == REAL_HUMAN
        assert ctx["is_human_rooted"] is True

    def test_bare_pointer_still_increments_depth_from_the_chain(self):
        """The smoke test in the issue asserts exactly this: depth increments."""
        ctx = _determine(_bare_pointer(), _chain_row(chain_depth=2))
        assert ctx["chain_depth"] == 3

    def test_human_rooted_chain_still_grants_the_root_human_vault_access(self):
        """The legitimate counterpart of the exploit test: real chains still work."""
        ctx = _determine(_bare_pointer(), _chain_row(chain_depth=1))
        authorized = _compute_authorized_user_id(
            correlation_ctx=ctx,
            cognito_sub="",
            max_credential_chain_depth=5,
        )
        assert authorized == REAL_HUMAN

    def test_self_re_trigger_guard_value_still_round_trips(self):
        """#1716/#2149: the guard reads last_triggered_persona off the pointer.

        It is not authority-bearing, so it stays on the pointer — but if
        read_pointer or this branch dropped it, the guard would die silently.
        """
        pointer = _bare_pointer(
            last_triggered_persona="developer",
            recent_triggered_personas={"developer"},
            recent_trigger_count=2,
        )
        ctx = _determine(pointer, _chain_row())
        assert ctx["last_triggered_persona"] == "developer"
        assert ctx["recent_triggered_personas"] == {"developer"}
        assert ctx["recent_trigger_count"] == 2


# =============================================================================
# _resolve_pointer_provenance unit behaviour
# =============================================================================


class TestResolvePointerProvenance:
    def test_unresolvable_chain_fails_closed(self):
        """No chain row → authority dropped, NOT inherited from the pointer.

        Failing open here would reopen the whole hole: an attacker who can make
        the GSI query miss (a correlation_id the webhook has never written) would
        get the pointer's forged values back.
        """
        with _patch_chain(None):
            root, rooted, depth = _resolve(_forged_pointer(), BOT)
        assert root == BOT
        assert rooted is False
        assert depth is None

    def test_unresolvable_chain_still_lets_the_caller_inherit_lineage(self):
        """Fail-closed on authority must not mean fail-closed on the chain id.

        A dropped chain would fragment #1828 lineage into new-chain-per-event —
        the exact silent degradation the issue's ordering constraint exists to
        avoid.
        """
        ctx = _determine(_bare_pointer(), None)
        assert ctx["correlation_id"] == "corr-REAL-CHAIN"
        assert ctx["is_new_chain"] is False
        assert ctx["is_human_rooted"] is False

    def test_human_rooted_true_with_no_root_human_is_not_trusted(self):
        """An inconsistent chain row cannot yield rooted-but-anonymous authority.

        ``_compute_authorized_user_id`` returns ``root_human_id`` verbatim for a
        human-rooted chain under the depth limit, so ``is_human_rooted=True`` with
        an empty root would persist "" as an authorized user rather than denying.
        """
        chain = _chain_row(is_human_rooted=True, root_human_id="")
        with _patch_chain(chain):
            root, rooted, _ = _resolve(_bare_pointer(), BOT)
        assert rooted is False
        assert root == BOT

    def test_malformed_depth_is_unknown_not_zero(self):
        chain = _chain_row(chain_depth="not-a-number")
        with _patch_chain(chain):
            _, _, depth = _resolve(_bare_pointer(), BOT)
        assert depth is None

    def test_negative_depth_is_rejected(self):
        """A negative depth would evade the runaway-chain guard on increment."""
        chain = _chain_row(chain_depth=-5)
        with _patch_chain(chain):
            _, _, depth = _resolve(_bare_pointer(), BOT)
        assert depth is None

    def test_decimal_depth_from_boto3_resource_is_coerced(self):
        """The DDB resource API returns numbers as Decimal, not int."""
        from decimal import Decimal

        chain = _chain_row(chain_depth=Decimal("4"))
        with _patch_chain(chain):
            _, _, depth = _resolve(_bare_pointer(), BOT)
        assert depth == 4

    def test_pointer_provenance_is_ignored_even_when_it_agrees(self):
        """The pointer's own fields are never consulted — not even as a fallback.

        Asserted by making the pointer and the chain DISAGREE and pinning the
        result to the chain. A "prefer chain, fall back to pointer" implementation
        would pass the other tests here but still be exploitable whenever the GSI
        query comes up empty.
        """
        pointer = _forged_pointer(correlation_id="corr-REAL-CHAIN")
        with _patch_chain(_chain_row()):
            root, rooted, depth = _resolve(pointer, BOT)
        assert root == REAL_HUMAN
        assert rooted is True
        assert depth == 2

    def test_empty_correlation_id_short_circuits(self):
        """No chain id → nothing to resolve; fail closed without a GSI query."""
        with patch("handler.logger"):
            root, rooted, depth = _resolve({"correlation_id": ""}, BOT)
        assert (root, rooted, depth) == (BOT, False, None)

    def test_gsi_failure_fails_closed_rather_than_raising(self):
        """A DDB outage must not 500 the webhook (GitHub retries 5xx → storm).

        Patches the real ``_resolve_chain`` (not the wrapper) so this exercises
        ``_resolve_chain_record``'s own except path.
        """
        with patch("agent_trigger._resolve_chain", side_effect=Exception("ddb down")):
            root, rooted, depth = _resolve(_bare_pointer(), BOT)
        assert (root, rooted, depth) == (BOT, False, None)
