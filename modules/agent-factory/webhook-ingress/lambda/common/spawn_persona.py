"""Shared spawn_persona() — the ONE place loop guards + publish live.

Issue #2151: Extracted from handler.py steps 12-15 and intent_parser.py guard
logic. Every trigger adapter (GitHub comment, agent HTTP, EventBridge) calls
this single function. Guards exist in ONE place so no spawn path can drift.

The function performs, in order:
  1. Persona validation (against VALID_PERSONAS)
  2. Self-mention guard (bot dispatching to its own persona)
  3. Self-re-trigger guard (same as last_triggered_persona on channel)
  4. Cross-persona loop guard (A->B->A->B alternation detection)
  5. Depth guard (MAX_CHAIN_DEPTH)
  6. Pointer write (with recent_triggered_personas merge)
  7. Provenance write
  8. Envelope build
  9. Webhook-events capture (DDB row BEFORE SQS)
  10. SQS publish

Returns SpawnResult indicating success (with message_id) or block (with reason).

Issue #4020: a blocked spawn now also writes a ``status="blocked"`` webhook-events
row carrying the block reason, so the Activity UI can explain the block instead of
showing nothing at all. The guards evaluate exactly as before — only bookkeeping
was added, and it is best-effort so it can never fail the webhook.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass

from common.personas import VALID_PERSONAS

logger = logging.getLogger(__name__)

# Maximum chain depth before blocking bot-to-bot triggers (issue #1696).
MAX_CHAIN_DEPTH = int(os.environ.get("MAX_CHAIN_DEPTH", "8"))

# Issue #3174: Default max credential chain depth (tenant-configurable).
# A human's credential authority propagates through depths 0..4 (the root human
# plus four spawn hops); depth 5 and beyond get authorized_user_id="" (no vault).
DEFAULT_MAX_CREDENTIAL_CHAIN_DEPTH = 5

# Issue #2149: Cross-persona loop threshold.
CROSS_PERSONA_LOOP_THRESHOLD = int(os.environ.get("CROSS_PERSONA_LOOP_THRESHOLD", "4"))


@dataclass
class SpawnResult:
    """Outcome of a spawn_persona() call."""

    success: bool
    message_id: str | None = None
    block_reason: str | None = None


def spawn_persona(
    *,
    persona: str,
    correlation_ctx: dict,
    channel_key: str,
    resolved_identity,
    tenant_id: str,
    actor_user_id: str,
    actor_org_id: str,
    sender: dict,
    event_type: str,
    action: str,
    installation_id: int,
    repo: str,
    payload: dict,
    intent_trigger: str,
    intent_label: str | None = None,
    model_requested: str | None = None,
    model_resolved: str | None = None,
    aws_label: str | None = None,
    token_source: str | None = None,
) -> SpawnResult:
    """Validate guards, write lineage, build envelope, publish to SQS.

    This is the ONLY place spawn logic lives. All trigger adapters call this.

    Args:
        persona: Target persona to spawn (must be in VALID_PERSONAS).
        correlation_ctx: From determine_correlation() — contains chain_depth,
            last_triggered_persona, recent_triggered_personas, etc.
        channel_key: Canonical channel key for pointer writes.
        resolved_identity: ResolvedIdentity from identity resolver.
        tenant_id: Resolved tenant ID.
        actor_user_id: Platform user_id of the sender.
        actor_org_id: Platform org_id.
        sender: Raw sender dict from payload (github_id, login, type).
        event_type: Webhook event type (e.g. "issue_comment").
        action: Webhook action (e.g. "created").
        installation_id: GitHub App installation ID.
        repo: Full repo name (e.g. "org/repo").
        payload: Full webhook payload dict.
        intent_trigger: Trigger string (e.g. "mentioned", "issue_labeled").
        intent_label: Optional label that triggered this (for issues.labeled).
        model_requested: Raw alias the user typed in /model directive (issue #2279).
        model_resolved: Validated Bedrock model ID, or None if rejected/absent.
        aws_label: Validated AWS credential label from /aws-label directive
            (issue #3574).
        token_source: Issue #3385 (C3) — "pat" when the tenant's identity-index
            row carries token_source_override="pat"; None otherwise (App default).

    Returns:
        SpawnResult with success=True and message_id, or success=False and
        block_reason explaining why the spawn was blocked.
    """
    # --- Guards 0-5 ---
    # Each guard records a block_reason rather than returning directly, so every
    # blocked spawn funnels through the SINGLE Activity-row write below.
    # Issue #4020: these returns previously happened before
    # _capture_invocation_event, so a guard-blocked trigger produced NO Activity
    # row at all — the operator's @agent-... comment simply vanished.
    block_reason: str | None = None

    # --- Guard 0: installation_id validation (Issue #2336) ---
    # Reject messages with installation_id=0/None before they reach SQS.
    # A dispatch with no valid installation will deterministically crash the
    # worker at token-mint time and jam the FIFO queue.
    if installation_id in (0, None) and event_type != "test":
        logger.warning(
            "spawn_persona: invalid installation_id=%r for persona=%s "
            "event_type=%s — blocking",
            installation_id,
            persona,
            event_type,
        )
        _emit_metric("InvalidInstallationIdBlocked", {"persona": persona})
        block_reason = "invalid_installation_id"

    # --- Guard 1: Persona validation ---
    elif persona not in VALID_PERSONAS:
        logger.warning("spawn_persona: unknown persona %r — blocking", persona)
        _emit_metric("UnknownPersonaBlocked", {"persona": persona})
        block_reason = "unknown_persona"

    # --- Guards 2-5: Only apply to bot senders ---
    elif _is_bot_sender(sender):
        block = _apply_bot_guards(
            persona=persona,
            correlation_ctx=correlation_ctx,
            resolved_identity=resolved_identity,
            sender=sender,
        )
        if block is not None:
            block_reason = block.block_reason

    if block_reason is not None:
        # Issue #4020: record the block in Activity so "why didn't my agent run"
        # is answerable from the UI.
        #
        # Wrapped HERE, at the call site, and not only inside the helper. The
        # property that matters is "a guard block cannot become a webhook 500" —
        # a guard block is benign and returns 200, and GitHub retries 5xx, so a
        # transient DDB problem would produce a redelivery storm (the issue's
        # impact analysis calls this out explicitly). That property belongs where
        # the response is decided, rather than depending on a helper's internals
        # staying exhaustively guarded through future edits. The helper's own
        # try/except remains, for the specific-reason log line.
        try:
            _capture_blocked_event(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                sender=sender,
                event_type=event_type,
                action=action,
                installation_id=installation_id,
                repo=repo,
                persona=persona,
                payload=payload,
                correlation_ctx=correlation_ctx,
                block_reason=block_reason,
            )
        except Exception as e:  # noqa: BLE001 — bookkeeping must never fail the webhook
            logger.warning(
                "spawn_persona: blocked-row write raised for reason=%s (non-fatal): %s",
                block_reason,
                e,
            )
        return SpawnResult(success=False, block_reason=block_reason)

    # --- Step 5.5: the chain hop actually happened — advance the depth ---
    # Issue #4268: THE single increment point. Everything below describes the run
    # being spawned, not the run that asked, so it gets its own depth.
    spawned_ctx = _advance_chain_depth(correlation_ctx)

    # --- Step 6: Write pointer + provenance (fail-soft) ---
    _write_pointer_and_provenance(
        persona=persona,
        correlation_ctx=spawned_ctx,
        channel_key=channel_key,
        resolved_identity=resolved_identity,
        actor_user_id=actor_user_id,
        event_type=event_type,
        action=action,
        repo=repo,
        payload=payload,
    )

    # --- Step 7: Build envelope ---
    cognito_sub = actor_user_id if resolved_identity.user_kind == "human" else ""
    envelope = _build_envelope(
        persona=persona,
        tenant_id=tenant_id,
        cognito_sub=cognito_sub,
        actor_user_id=actor_user_id,
        actor_org_id=actor_org_id,
        sender=sender,
        installation_id=installation_id,
        repo=repo,
        payload=payload,
        correlation_ctx=spawned_ctx,
        intent_trigger=intent_trigger,
        intent_label=intent_label,
        model_requested=model_requested,
        model_resolved=model_resolved,
        aws_label=aws_label,
        token_source=token_source,
    )

    # --- Step 8: Capture invocation event to DDB BEFORE SQS ---
    # Issue #3174: read tenant credential chain depth policy (fail-soft).
    max_cred_depth = _get_max_credential_chain_depth(installation_id)
    _capture_invocation_event(
        envelope=envelope,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        sender=sender,
        event_type=event_type,
        action=action,
        installation_id=installation_id,
        repo=repo,
        persona=persona,
        payload=payload,
        correlation_ctx=spawned_ctx,
        max_credential_chain_depth=max_cred_depth,
    )

    # --- Step 9: Publish to SQS ---
    from common.sqs_publisher import publish_envelope

    message_id = publish_envelope(envelope)
    if not message_id:
        logger.error("spawn_persona: SQS publish failed for persona=%s", persona)
        return SpawnResult(success=False, block_reason="sqs_publish_failed")

    logger.info(
        "spawn_persona: published persona=%s sqs_message_id=%s",
        persona,
        message_id,
    )
    return SpawnResult(success=True, message_id=message_id)


def _advance_chain_depth(correlation_ctx: dict) -> dict:
    """Return a copy of ``correlation_ctx`` holding the SPAWNED run's depth (#4268).

    This is the one place ``chain_depth`` advances. It is called only after every
    guard has passed, which is precisely the condition the counter is supposed to
    measure: one agent has actually caused another agent to start.

    Why it moved here. Depth used to be incremented per webhook EVENT on the
    chain, in ``determine_correlation``. That value is persisted on the row for
    every outcome — including the ``no_op`` rows the ingest Lambda writes and then
    discards — and the next event inherits the newest row's depth. So events that
    started nothing advanced the counter that gates starting things: an
    orchestrator posting routine status comments drove its own chain to
    ``chain_depth`` 290 against ``MAX_CHAIN_DEPTH`` 8 with two real generations,
    and was then refused with ``chain_depth_exceeded``. 679 of those rows were
    ``event_type_unhandled`` — event types with no handler at all.

    Semantics, unchanged from what Guard 5 and the #3174 credential policy already
    assume: the depth on a run's row is the run's own generation. A run spawned
    directly by a human/service (``is_new_chain``) is generation 0 — the existing
    "depth 0 == human-initiated" convention ``_compute_authorized_user_id`` is
    written against. Every subsequent hop is caller + 1, so a chain of N genuine
    generations reports depth N-1 at its head and the cap still bounds recursion
    at ``MAX_CHAIN_DEPTH`` generations.

    A caller cannot use this to reset depth. The inherited value still comes from
    server-written state only (``handler._resolve_pointer_provenance`` reads the
    ``webhook-events`` GSI per #4129, ``agent_trigger._resolve_chain_depth`` 422s
    on absent/malformed/negative per #4128), and ``is_new_chain`` is False on
    every chain-continuation branch — a bot cannot present a continuation as a
    fresh root.

    Returns a shallow copy so the caller's context (used for the ``blocked`` row,
    where no run started and the depth must NOT advance) is left untouched.
    """
    advanced = dict(correlation_ctx)
    if correlation_ctx.get("is_new_chain"):
        # This spawn IS the root generation — nothing spawned it.
        advanced["chain_depth"] = 0
        return advanced

    caller_depth = correlation_ctx.get("chain_depth", 0)
    if not isinstance(caller_depth, int) or isinstance(caller_depth, bool):
        # Non-int depth reaching here would silently disable Guard 5 on the next
        # hop. The upstream resolvers reject these (#4128), so this is a
        # defensive floor, not a supported input.
        logger.warning(
            "spawn_persona: non-integer chain_depth=%r in correlation_ctx — "
            "treating the spawned run as a root generation",
            caller_depth,
        )
        caller_depth = 0
    advanced["chain_depth"] = caller_depth + 1
    return advanced


def _is_bot_sender(sender: dict) -> bool:
    """Check if the sender is a bot (GitHub App or bot user)."""
    if sender.get("type") == "Bot":
        return True
    login = sender.get("login", "")
    if login.endswith("[bot]"):
        return True
    return False


def _apply_bot_guards(
    *,
    persona: str,
    correlation_ctx: dict,
    resolved_identity,
    sender: dict,
) -> SpawnResult | None:
    """Apply bot loop guards. Returns SpawnResult if blocked, else None."""
    sender_login = sender.get("login", "unknown")

    # Guard 2: Self-mention — bot dispatching to its own persona
    if resolved_identity is not None and hasattr(resolved_identity, "bot_kind"):
        if persona == resolved_identity.bot_kind:
            logger.info(
                "spawn_persona: self-mention blocked — %s to %s",
                sender_login,
                persona,
            )
            _emit_metric("SelfMentionBlocked", {"persona": persona})
            return SpawnResult(success=False, block_reason="self_mention")

    # Guard 3: Self-re-trigger — same as last_triggered_persona on channel
    last_persona = correlation_ctx.get("last_triggered_persona")
    if last_persona and persona == last_persona:
        logger.info(
            "spawn_persona: self-re-trigger blocked — %s targeting %s "
            "(last triggered in chain %s)",
            sender_login,
            persona,
            correlation_ctx.get("correlation_id", "unknown"),
        )
        _emit_metric("SelfReTriggerBlocked", {"persona": persona})
        return SpawnResult(success=False, block_reason="self_re_trigger")

    # Guard 4: Cross-persona loop — A->B->A->B alternation detection
    recent_personas = correlation_ctx.get("recent_triggered_personas", set())
    recent_count = correlation_ctx.get("recent_trigger_count", 0)
    if persona in recent_personas and recent_count >= CROSS_PERSONA_LOOP_THRESHOLD:
        logger.info(
            "spawn_persona: cross-persona loop blocked — %s already in recent set %s "
            "(count=%d, threshold=%d) in chain %s",
            persona,
            recent_personas,
            recent_count,
            CROSS_PERSONA_LOOP_THRESHOLD,
            correlation_ctx.get("correlation_id", "unknown"),
        )
        _emit_metric("CrossPersonaLoopBlocked", {"persona": persona})
        return SpawnResult(success=False, block_reason="cross_persona_loop")

    # Guard 5: Depth cap
    chain_depth = correlation_ctx.get("chain_depth", 0)
    if chain_depth >= MAX_CHAIN_DEPTH:
        logger.info(
            "spawn_persona: depth guard blocked — %s at depth %d >= max %d",
            persona,
            chain_depth,
            MAX_CHAIN_DEPTH,
        )
        _emit_metric(
            "ChainDepthExceeded", {"persona": persona, "depth": str(chain_depth)}
        )
        return SpawnResult(success=False, block_reason="chain_depth_exceeded")

    # All guards passed
    source_bot = ""
    if resolved_identity is not None and hasattr(resolved_identity, "bot_kind"):
        source_bot = resolved_identity.bot_kind
    _emit_metric(
        "BotToBotTrigger",
        {"source_bot": source_bot, "target_persona": persona},
    )
    return None


def _write_pointer_and_provenance(
    *,
    persona: str,
    correlation_ctx: dict,
    channel_key: str,
    resolved_identity,
    actor_user_id: str,
    event_type: str,
    action: str,
    repo: str,
    payload: dict,
) -> None:
    """Write correlation pointer + provenance record (fail-soft)."""
    if not channel_key:
        return

    # Provenance write
    try:
        from common.gateway_client import post_provenance

        post_provenance(
            actor_user_id=actor_user_id,
            triggered_by=correlation_ctx.get("triggered_by"),
            root_human_id=correlation_ctx["root_human_id"],
            is_human_rooted=correlation_ctx["is_human_rooted"],
            action_kind="webhook_trigger",
            source_event={
                "event_type": event_type,
                "action": action,
                "repo": repo,
                "issue": payload.get("issue", {}).get("number"),
            },
            correlation_id=correlation_ctx["correlation_id"],
            org_id=resolved_identity.org_id,
            parent_invocation_id=correlation_ctx.get("parent_invocation_id"),
        )
    except Exception as e:
        logger.warning("spawn_persona: post_provenance failed (fail-soft): %s", e)

    # Pointer write — merge persona into recent set
    try:
        from common.correlation_store import write_pointer

        existing_recent = correlation_ctx.get("recent_triggered_personas", set())
        if not isinstance(existing_recent, set):
            existing_recent = set(existing_recent) if existing_recent else set()
        updated_recent = existing_recent | {persona}
        updated_count = correlation_ctx.get("recent_trigger_count", 0) + 1

        write_pointer(
            key=channel_key,
            correlation_id=correlation_ctx["correlation_id"],
            root_human_id=correlation_ctx["root_human_id"],
            is_human_rooted=correlation_ctx["is_human_rooted"],
            last_triggered_persona=persona,
            recent_triggered_personas=updated_recent,
            recent_trigger_count=updated_count,
        )
    except Exception as e:
        logger.warning("spawn_persona: write_pointer failed (fail-soft): %s", e)


def _build_envelope(
    *,
    persona: str,
    tenant_id: str,
    cognito_sub: str,
    actor_user_id: str,
    actor_org_id: str,
    sender: dict,
    installation_id: int,
    repo: str,
    payload: dict,
    correlation_ctx: dict,
    intent_trigger: str,
    intent_label: str | None,
    model_requested: str | None = None,
    model_resolved: str | None = None,
    aws_label: str | None = None,
    token_source: str | None = None,
) -> dict:
    """Build the normalized webhook envelope for SQS."""
    envelope = {
        "version": "1.0",
        "channel": "github",
        "tenant_id": tenant_id,
        "cognito_sub": cognito_sub,
        "persona": persona,
        "actor": {
            "user_id": actor_user_id,
            "org_id": actor_org_id,
            "github_id": sender.get("id", 0),
            "github_login": sender.get("login", ""),
            "is_bot": sender.get("type") == "Bot",  # Deprecated
        },
        "source_ref": {
            "installation_id": installation_id,
            "repo": repo,
            "issue": payload.get("issue", {}).get("number")
            if "issue" in payload
            else payload.get("pull_request", {}).get("number"),
            "pr": payload.get("pull_request", {}).get("number")
            if "pull_request" in payload
            else None,
            "sha": payload.get("pull_request", {}).get("head", {}).get("sha")
            if "pull_request" in payload
            else None,
        },
        "intent": {
            "trigger": intent_trigger,
            "label": intent_label,
            "persona": persona,
        },
        "correlation": {
            "correlation_id": correlation_ctx.get("correlation_id", ""),
            "root_human_id": correlation_ctx.get("root_human_id", ""),
            "is_human_rooted": correlation_ctx.get("is_human_rooted", True),
            "parent_invocation_id": correlation_ctx.get("parent_invocation_id"),
            "chain_depth": correlation_ctx.get("chain_depth", 0),
        },
        "payload": payload,
        "arrived_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # Issue #2279: Thread caller-chosen model through the envelope.
    # model_requested = raw alias the user typed; model_resolved = validated
    # Bedrock model ID (or None if rejected). Worker reads model_resolved to
    # override ANTHROPIC_MODEL; uses model_requested for the warning message.
    if model_requested is not None:
        envelope["model_requested"] = model_requested
    if model_resolved is not None:
        envelope["model_resolved"] = model_resolved
    # Issue #3574: Thread /aws-label through the envelope. The worker uses this
    # to pass label= to the gateway's assume-role endpoint, selecting a specific
    # linked account within the authorized user's vault.
    if aws_label is not None:
        envelope["aws_label"] = aws_label
    # Issue #3385 (C3): Thread token_source through the envelope. The worker
    # reads this to decide PAT vs App execution path. Only set when explicitly
    # "pat" — absent/None = legacy App behavior (backward compatible).
    if token_source is not None:
        envelope["token_source"] = token_source
    envelope["message_id"] = str(uuid.uuid4())
    return envelope


def _get_max_credential_chain_depth(installation_id: int | str) -> int:
    """Read max_credential_chain_depth from tenant-registry DDB (fail-soft).

    Issue #3174: Tenant-configurable depth limit for credential chain
    propagation. Defaults to DEFAULT_MAX_CREDENTIAL_CHAIN_DEPTH (5) if:
      - TENANT_REGISTRY_TABLE env var not set
      - DDB read fails
      - Attribute absent on tenant row

    Args:
        installation_id: GitHub App installation ID (PK of tenant-registry).

    Returns:
        The configured max depth, or the default.
    """
    table_name = os.environ.get("TENANT_REGISTRY_TABLE", "")
    if not table_name:
        return DEFAULT_MAX_CREDENTIAL_CHAIN_DEPTH

    try:
        import boto3

        region = os.environ.get(
            "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        )
        dynamodb = boto3.resource("dynamodb", region_name=region)
        table = dynamodb.Table(table_name)
        resp = table.get_item(
            Key={"installation_id": str(installation_id)},
            ProjectionExpression="max_credential_chain_depth",
        )
        item = resp.get("Item")
        if item and "max_credential_chain_depth" in item:
            return int(item["max_credential_chain_depth"])
    except Exception as e:
        logger.warning(
            "spawn_persona: failed to read max_credential_chain_depth "
            "for installation_id=%s (defaulting to %d): %s",
            installation_id,
            DEFAULT_MAX_CREDENTIAL_CHAIN_DEPTH,
            e,
        )

    return DEFAULT_MAX_CREDENTIAL_CHAIN_DEPTH


def _compute_authorized_user_id(
    *,
    correlation_ctx: dict,
    cognito_sub: str,
    max_credential_chain_depth: int,
) -> str:
    """Compute authorized_user_id per §Q3 chain policy (#3174).

    Policy table:
      - Human-initiated (depth 0, human sender) → cognito_sub
      - Human-rooted chain under depth limit → root_human_id
      - Human-rooted chain at/over depth, OR bot-rooted → "" (no vault)

    Args:
        correlation_ctx: Chain context with is_human_rooted, root_human_id,
            chain_depth.
        cognito_sub: The cognito_sub for this spawn (non-empty only for
            human senders).
        max_credential_chain_depth: Tenant-configurable depth limit.

    Returns:
        The authorized_user_id string ("" means no vault access).
    """
    is_human_rooted = correlation_ctx.get("is_human_rooted", False)
    if not is_human_rooted:
        return ""

    chain_depth = correlation_ctx.get("chain_depth", 0)
    if chain_depth >= max_credential_chain_depth:
        return ""

    # Human-initiated (cognito_sub set) takes precedence over root_human_id
    # for the root of the chain (depth 0).
    if cognito_sub:
        return cognito_sub

    # Chain path: inherit from root human
    root_human_id = correlation_ctx.get("root_human_id", "")
    return root_human_id


def _capture_invocation_event(
    *,
    envelope: dict,
    tenant_id: str,
    actor_user_id: str,
    sender: dict,
    event_type: str,
    action: str,
    installation_id: int,
    repo: str,
    persona: str,
    payload: dict,
    correlation_ctx: dict,
    max_credential_chain_depth: int = DEFAULT_MAX_CREDENTIAL_CHAIN_DEPTH,
) -> None:
    """Write enriched invocation row to DynamoDB (best-effort).

    Issue #2042: attribute the run to the chain's HUMAN ROOT for human-rooted
    chains so it appears in the originating human's Activity view.
    Issue #3174: compute and persist authorized_user_id per chain policy.
    """
    try:
        from common.webhook_events import WebhookEventLogger

        table_name = os.environ.get("EVENTS_TABLE", "")
        if not table_name:
            return

        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        event_logger = WebhookEventLogger(table_name=table_name, region=region)

        # Issue #2042: attribute to human root for human-rooted chains
        root_human = correlation_ctx.get("root_human_id")
        is_human_rooted = correlation_ctx.get("is_human_rooted")
        effective_user_id = (
            root_human if (is_human_rooted and root_human) else actor_user_id
        )

        # Issue #3174: compute authorized_user_id per chain policy (§Q3).
        cognito_sub = envelope.get("cognito_sub", "")
        authorized_user_id = _compute_authorized_user_id(
            correlation_ctx=correlation_ctx,
            cognito_sub=cognito_sub,
            max_credential_chain_depth=max_credential_chain_depth,
        )

        # Derive topic from issue/PR title
        issue_title = payload.get("issue", {}).get("title", "")
        pr_title = payload.get("pull_request", {}).get("title", "")
        topic = (issue_title or pr_title or "(untitled)")[:120]

        # Derive source_url
        issue_url = payload.get("issue", {}).get("html_url", "")
        pr_url = payload.get("pull_request", {}).get("html_url", "")
        source_url = issue_url or pr_url or None

        # Derive issue_number
        issue_number = payload.get("issue", {}).get("number")
        if issue_number is None:
            issue_number = payload.get("pull_request", {}).get("number")

        event_logger.log_event(
            event_id=envelope["message_id"],
            arrived_at=envelope["arrived_at"],
            tenant_id=tenant_id,
            channel="github",
            event_type=event_type,
            action=action,
            installation_id=str(installation_id),
            repo=repo,
            status="webhook_received",
            user_id=effective_user_id or "unattributed",
            github_login=sender.get("login", "") or None,
            persona=persona,
            topic=topic,
            source_url=source_url,
            issue_number=issue_number,
            correlation_id=correlation_ctx.get("correlation_id"),
            parent_invocation_id=correlation_ctx.get("parent_invocation_id"),
            chain_depth=correlation_ctx.get("chain_depth"),
            root_human_id=root_human,
            is_human_rooted=is_human_rooted,
            authorized_user_id=authorized_user_id,
        )
    except Exception as e:
        logger.warning("spawn_persona: capture_invocation_event failed: %s", e)


def _capture_blocked_event(
    *,
    tenant_id: str,
    actor_user_id: str,
    sender: dict,
    event_type: str,
    action: str,
    installation_id: int,
    repo: str,
    persona: str,
    payload: dict,
    correlation_ctx: dict,
    block_reason: str,
) -> None:
    """Write a ``blocked`` Activity row for a guard-blocked spawn. Issue #4020.

    The guard returns above used to fire BEFORE ``_capture_invocation_event``, so
    a blocked trigger left no trace anywhere the operator could see — the Activity
    feed showed nothing at all, and the only record was a CloudWatch log line and
    a metric datapoint. "Why didn't my review run?" was unanswerable from the UI.

    The row carries ``status="blocked"`` plus the guard's existing
    ``block_reason`` verbatim (the reason strings are reused, not reinvented).

    Best-effort by construction, and deliberately so: a guard block is a benign,
    expected outcome that returns HTTP 200. If bookkeeping could raise, a DDB
    blip would convert every blocked delivery into a 500 and GitHub would retry
    it — turning an observability improvement into a redelivery storm. There is
    no envelope, so the row gets an auto-generated event_id like the other
    terminal-at-ingress statuses (no_op, rate_limited).
    """
    try:
        from common.webhook_events import WebhookEventLogger

        table_name = os.environ.get("EVENTS_TABLE", "")
        if not table_name:
            return

        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        event_logger = WebhookEventLogger(table_name=table_name, region=region)

        # Issue #2042: attribute to the human root for human-rooted chains so the
        # row lands in the originating human's Activity view, matching the
        # successful-dispatch path.
        root_human = correlation_ctx.get("root_human_id")
        is_human_rooted = correlation_ctx.get("is_human_rooted")
        effective_user_id = (
            root_human if (is_human_rooted and root_human) else actor_user_id
        )

        issue_title = payload.get("issue", {}).get("title", "")
        pr_title = payload.get("pull_request", {}).get("title", "")
        topic = (issue_title or pr_title or "(untitled)")[:120]

        issue_url = payload.get("issue", {}).get("html_url", "")
        pr_url = payload.get("pull_request", {}).get("html_url", "")
        source_url = issue_url or pr_url or None

        issue_number = payload.get("issue", {}).get("number")
        if issue_number is None:
            issue_number = payload.get("pull_request", {}).get("number")

        event_logger.log_event(
            tenant_id=tenant_id,
            channel="github",
            event_type=event_type,
            action=action,
            installation_id=str(installation_id),
            repo=repo,
            status="blocked",
            skip_reason=block_reason,
            user_id=effective_user_id or "unattributed",
            github_login=sender.get("login", "") or None,
            persona=persona,
            topic=topic,
            source_url=source_url,
            issue_number=issue_number,
            correlation_id=correlation_ctx.get("correlation_id"),
            parent_invocation_id=correlation_ctx.get("parent_invocation_id"),
            chain_depth=correlation_ctx.get("chain_depth"),
            root_human_id=root_human,
            is_human_rooted=is_human_rooted,
        )
    except Exception as e:
        logger.warning(
            "spawn_persona: capture_blocked_event failed for reason=%s (non-fatal): %s",
            block_reason,
            e,
        )


def _emit_metric(metric_name: str, dimensions: dict[str, str]) -> None:
    """Emit a CloudWatch metric under WebhookIngress namespace (fail-soft)."""
    try:
        import boto3

        cw = boto3.client("cloudwatch", region_name="us-east-1")
        cw.put_metric_data(
            Namespace="WebhookIngress",
            MetricData=[
                {
                    "MetricName": metric_name,
                    "Dimensions": [
                        {"Name": k, "Value": v} for k, v in dimensions.items()
                    ],
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception as e:
        logger.debug("Failed to emit metric %s: %s", metric_name, e)
