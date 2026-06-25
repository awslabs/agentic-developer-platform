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
    def test_inherits_chain_and_increments_depth(self):
        """Full context → pointer for target issue inherits corr/root, parent=own
        msg_id, depth=own+1."""
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
        assert kw["root_human_id"] == "human-1"
        assert kw["is_human_rooted"] is True
        assert kw["triggering_invocation_id"] == "ops-msg-1"
        assert kw["chain_depth"] == 1

    def test_no_correlation_context_skips(self):
        """No ADP_CORRELATION_ID/ROOT → no write (webhook will start fresh chain)."""
        with patch.dict(os.environ, {"ADP_CORRELATION_ID": "", "ADP_ROOT_HUMAN_ID": ""}, clear=False):
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

    def test_missing_args_skips(self):
        with patch.dict(os.environ, _FULL_ENV, clear=False):
            with patch("lib.correlation_store.write_pointer") as wp:
                rc = _run(["x", "aws-e/adp"])  # missing issue + persona
        assert rc == 0
        assert wp.call_count == 0
