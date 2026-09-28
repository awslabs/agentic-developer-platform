"""Tests for lib.seed_trigger_pointer — cross-issue lineage seeding (#1828).

When an agent triggers another persona by posting `@agent-X` on a DIFFERENT
issue, the gh-wrapper calls this helper BEFORE the comment posts, writing the
correlation pointer for the target issue so the inbound webhook inherits the
triggering run's chain instead of starting a fresh bot-rooted one.
"""

from __future__ import annotations

import importlib
import os
from unittest.mock import patch

_FULL_ENV = {
    "ADP_CORRELATION_ID": "corr-OPS",
    "ADP_ROOT_HUMAN_ID": "human-1",
    "ADP_IS_HUMAN_ROOTED": "true",
    "ADP_MESSAGE_ID": "ops-msg-1",
    "ADP_CHAIN_DEPTH": "0",
}


def _run(argv):
    import lib.seed_trigger_pointer as m

    importlib.reload(m)
    return m.main(argv)


class TestSeedTriggerPointer:
    def test_seeds_chain_and_parent_edge(self):
        """Full context → pointer for target issue carries corr + parent=own msg_id
        + last_triggered_persona=target persona.

        Issue #4129: root_human_id / is_human_rooted / chain_depth are NOT sent.
        The webhook resolves all three from its own webhook-events rows keyed on
        this correlation_id, so the seeded row carries lineage without carrying
        authority — which is what makes a pod-forged row inert.
        """
        with patch.dict(os.environ, _FULL_ENV, clear=False):
            with (
                patch("lib.correlation_store.write_pointer") as wp,
                patch(
                    "lib.correlation_store.channel_key",
                    side_effect=lambda p, r, k, n: f"{p}:repo={r},{k}={n}",
                ),
            ):
                rc = _run(["x", "aws-e/adp", "1777", "developer"])
        assert rc == 0
        wp.assert_called_once()
        kw = wp.call_args.kwargs
        assert kw["channel_key"] == "github:repo=aws-e/adp,issue=1777"
        assert kw["correlation_id"] == "corr-OPS"
        assert kw["triggering_invocation_id"] == "ops-msg-1"
        # Issue #2149: pre-seeds the self-re-trigger guard
        assert kw["last_triggered_persona"] == "developer"
        # Issue #4129: the pod cannot name a root human or reset the depth.
        assert "root_human_id" not in kw
        assert "is_human_rooted" not in kw
        assert "chain_depth" not in kw

    def test_no_correlation_context_skips(self):
        """No ADP_CORRELATION_ID → no write (webhook will start a fresh chain).

        Issue #4129: the gate is now correlation_id ALONE. It previously also
        required ADP_ROOT_HUMAN_ID, which the pod no longer sends.
        """
        with patch.dict(os.environ, {"ADP_CORRELATION_ID": ""}, clear=False):
            with patch("lib.correlation_store.write_pointer") as wp:
                rc = _run(["x", "aws-e/adp", "1777", "developer"])
        assert rc == 0
        assert wp.call_count == 0

    def test_bad_issue_number_skips(self):
        with patch.dict(os.environ, _FULL_ENV, clear=False):
            with patch("lib.correlation_store.write_pointer") as wp:
                rc = _run(["x", "aws-e/adp", "not-a-number", "developer"])
        assert rc == 0
        assert wp.call_count == 0

    def test_write_error_is_fail_soft(self):
        """A DDB error must NEVER block the agent's comment — exit 0 regardless."""
        with patch.dict(os.environ, _FULL_ENV, clear=False):
            with (
                patch("lib.correlation_store.write_pointer", side_effect=Exception("ddb down")),
                patch("lib.correlation_store.channel_key", side_effect=lambda p, r, k, n: "k"),
            ):
                rc = _run(["x", "aws-e/adp", "1777", "developer"])
        assert rc == 0

    def test_passes_reviewer_persona(self):
        """last_triggered_persona matches the target persona, not the triggering one."""
        with patch.dict(os.environ, _FULL_ENV, clear=False):
            with (
                patch("lib.correlation_store.write_pointer") as wp,
                patch(
                    "lib.correlation_store.channel_key",
                    side_effect=lambda p, r, k, n: f"{p}:repo={r},{k}={n}",
                ),
            ):
                rc = _run(["x", "aws-e/adp", "1777", "reviewer"])
        assert rc == 0
        wp.assert_called_once()
        kw = wp.call_args.kwargs
        assert kw["last_triggered_persona"] == "reviewer"

    def test_missing_args_skips(self):
        with patch.dict(os.environ, _FULL_ENV, clear=False):
            with patch("lib.correlation_store.write_pointer") as wp:
                rc = _run(["x", "aws-e/adp"])  # missing issue + persona
        assert rc == 0
        assert wp.call_count == 0
