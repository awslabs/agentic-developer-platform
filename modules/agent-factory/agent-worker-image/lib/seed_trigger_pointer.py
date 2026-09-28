#!/usr/bin/env python3
"""Seed a correlation pointer for a CROSS-ISSUE agent->agent trigger (issue #1828).

Background: when an agent (e.g. operations) triggers another persona by posting
`@agent-developer ...` on a DIFFERENT issue, that comment is written by the
agent's own tooling (raw `gh issue comment <other-issue>`), which does NOT carry
the correlation marker. The inbound webhook then finds no pointer and no marker
on the target channel and starts a BRAND-NEW chain rooted at the bot — so the
spawned run is disconnected from the triggering chain and never shows under the
originating human in the UI.

Fix (server-side, no marker text, no race): BEFORE the trigger comment is posted,
write the correlation pointer for the TARGET issue's channel so the webhook
inherits the triggering run's chain (its correlation_id, human root, parent edge,
depth+1). The webhook's read_pointer uses ConsistentRead=True, so a write that
commits before the comment posts is guaranteed visible.

This is invoked synchronously by the gh-wrapper before exec'ing the real `gh`,
so it covers EVERY comment path (SDK Bash tool calls AND direct child_process),
unlike a PreToolUse hook which only sees SDK tool calls.

Usage:
    python3 -m lib.seed_trigger_pointer <repo> <issue_number> <persona>

Fail-soft: any error is logged and exits 0 — lineage seeding must NEVER block the
agent from posting its comment.
"""

from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("seed_trigger_pointer")


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        # Not enough args — nothing to do (fail-soft).
        return 0
    repo, issue_str, persona = argv[1], argv[2], argv[3]

    try:
        issue_number = int(issue_str)
    except (ValueError, TypeError):
        logger.warning("seed_trigger_pointer: bad issue number %r — skipping", issue_str)
        return 0

    # Read THIS run's chain context from the env the entrypoint exported.
    correlation_id = os.environ.get("ADP_CORRELATION_ID", "")
    root_human_id = os.environ.get("ADP_ROOT_HUMAN_ID", "")
    is_human_rooted = os.environ.get("ADP_IS_HUMAN_ROOTED", "false") == "true"
    own_message_id = os.environ.get("ADP_MESSAGE_ID", "")
    try:
        own_depth = int(os.environ.get("ADP_CHAIN_DEPTH", "0") or "0")
    except (ValueError, TypeError):
        own_depth = 0

    if not correlation_id or not root_human_id:
        # No chain context (e.g. run started without correlation) — nothing to
        # seed; the webhook will start a fresh chain as before.
        logger.info("seed_trigger_pointer: no correlation context in env — skipping")
        return 0

    try:
        from lib.correlation_store import channel_key, write_pointer

        key = channel_key("github", repo, "issue", issue_number)
        # The spawned run inherits THIS chain; its parent is this run's invocation;
        # its depth is this run's depth + 1.
        # Issue #2149: pass last_triggered_persona so the cross-persona loop guard
        # is pre-seeded on the target channel (the webhook reads it to block
        # immediate self-re-triggers).
        write_pointer(
            channel_key=key,
            correlation_id=correlation_id,
            root_human_id=root_human_id,
            is_human_rooted=is_human_rooted,
            triggering_invocation_id=own_message_id or None,
            chain_depth=own_depth + 1,
            last_triggered_persona=persona,
        )
        logger.info(
            "seed_trigger_pointer: seeded pointer channel=%s corr=%s parent=%s depth=%d "
            "(triggering persona=%s)",
            key,
            correlation_id,
            own_message_id,
            own_depth + 1,
            persona,
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft by design
        logger.warning("seed_trigger_pointer: write failed (non-fatal): %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
