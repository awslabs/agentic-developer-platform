"""Canonical skip/block reason enum for non-dispatching webhook deliveries.

Issue #4020: A skipped trigger's reason existed only in CloudWatch logs, metrics
and the HTTP response body — never on the DynamoDB row Agent Activity reads. The
UI rendered a bare "✗ No-op" badge with no explanation, and the guard paths in
``spawn_persona`` wrote no row at all. Operators asked "why didn't my review run"
and had to go spelunking in Lambda logs to find out.

This module is the single source of truth for those reason strings so that the
producers (``intent_parser``, ``spawn_persona``, the worker entrypoint) and the
consumer (the Activity UI's human-readable mapping) cannot drift apart.

**Security invariant (issue #4020 impact analysis).** Every value here is a
static enum string. Reasons are NEVER built by interpolating webhook payload
content — no repo names, logins, label text, or IDs. A reason is rendered in the
Activity feed, so echoing payload data would be a cross-tenant info-disclosure
surface. Add new constants here; do not format strings at the call site.
"""

from __future__ import annotations

# --- Intent-parser no-ops (github/intent_parser.py) ---------------------------
# The delivery was authentic and authorized, but nothing in it asks for work.

#: Human comment with no ``@agent-<persona>`` mention.
NO_MENTION = "no_mention"

#: Bot comment containing a bare ``@agent-X`` mention but no ``adp-dispatch``
#: marker. Prose (status headers, plans) rather than a dispatch (#2149).
BOT_MENTION_NO_DISPATCH_MARKER = "bot_mention_no_dispatch_marker"

#: Bot dispatch marker present but no correlation context available, so the
#: loop guards cannot be evaluated — blocked as a safe default.
BOT_DISPATCH_NO_CORRELATION = "bot_dispatch_no_correlation"

#: ``issues.labeled`` whose label has no persona mapping in LABEL_TO_PERSONA.
LABEL_UNMAPPED = "label_unmapped"

#: PR head branch does not match ``agent/issue-*``, so no reviewer is due.
PR_BRANCH_NOT_AGENT = "pr_branch_not_agent"

#: Draft PRs are incomplete; review starts only once the author marks them ready.
PR_DRAFT = "pr_draft"

#: Bot-sent ``pull_request.synchronize`` — suppressed so an agent pushing fix
#: commits to its own PR branch cannot spawn a fresh reviewer each time (#1696).
BOT_SYNCHRONIZE_DEDUP = "bot_synchronize_dedup"

#: ``issues.opened`` without the ``aidlc-intent`` label (#3169). Prevents a
#: dispatch storm from every newly filed issue.
NO_AIDLC_LABEL = "no_aidlc_label"

#: Bot-generated non-comment/non-PR event, suppressed to prevent self-trigger
#: loops (#1696).
BOT_EVENT_IGNORED = "bot_event_ignored"

#: ``installation`` lifecycle event — recorded for audit, never dispatched.
INSTALLATION_EVENT = "installation_event"

#: The event type / action pair has no handler at all (e.g. ``check_run``).
EVENT_TYPE_UNHANDLED = "event_type_unhandled"

#: ``issue_comment`` action other than ``created`` (e.g. ``edited``) from a bot
#: sender — most commonly the agent editing its own status comment in place.
#: Distinguished from EVENT_TYPE_UNHANDLED so webhook-delivery logs read as
#: "the agent's own comment activity, ignored" rather than an unexplained no-op.
BOT_COMMENT_ACTION_UNHANDLED = "bot_comment_action_unhandled"

#: ``@agent-engine <command>`` comment (#4527). Addressed to the orchestration
#: engine, not to a persona, so this Lambda spawns NO pod and enqueues nothing:
#: it marks the event row and the engine tick consumes it on its next wake.
#:
#: This is a no-op *for this Lambda*, not a no-op for the platform — the row it
#: marks is the whole delivery mechanism. It is a skip reason rather than an
#: ``Intent`` because an ``Intent`` means "spawn an agent pod", and the engine
#: path deliberately spawns none.
ENGINE_COMMAND = "engine_command"

# --- Worker-side skips (agent-worker-image/entrypoint.py) --------------------

#: SQS redelivery for work that already landed: the issue's agent branch has a
#: merged PR, so a prior run completed successfully (#1864).
IDEMPOTENCY_MERGED_PR = "idempotency_merged_pr"

# NOTE: spawn_persona's guard reasons (invalid_installation_id, unknown_persona,
# self_mention, self_re_trigger, cross_persona_loop, chain_depth_exceeded) are
# NOT redefined here. They already exist as SpawnResult.block_reason values and
# are written to the row verbatim — the issue is explicit that we reuse those
# strings rather than invent parallel ones.
