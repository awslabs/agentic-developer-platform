"""POST /agent/trigger handler — IAM-authenticated agent-to-agent spawn.

Issue #2152: Agents spawn other personas via authenticated HTTP call with
mandatory server-resolved lineage. The body provides correlation_id +
parent_invocation_id; the handler resolves the chain from the webhook-events
table's correlation-index GSI and cross-checks trust properties. An agent
CANNOT forge root_human_id or tenant — those come from the chain record.

Issue #4128 (#4073 findings #18 + #1b): provenance on this route is VERIFIED,
not trusted. Five fail-open behaviours are closed:
  - a body-supplied provenance marker is signature-verified (forged -> 403)
  - chain_depth never silently resets to 0 (malformed -> 422)
  - parent_invocation_id must actually belong to the claimed chain
  - target.repo must resolve to the chain's tenant
  - an omitted body tenant_id no longer skips the cross-tenant check
  - is_human_rooted defaults to False, not True

Reject rules:
  - Missing correlation_id        -> 400 missing_lineage
  - Unknown/expired chain         -> 422 unknown_chain (NEVER mint new root)
  - Cross-tenant mismatch         -> 403 cross_tenant
  - Chain has no tenant           -> 403 cross_tenant  (#4128)
  - Forged/unsigned marker        -> 403 unverified_provenance  (#4128)
  - Malformed chain_depth         -> 422 invalid_chain_depth  (#4128)
  - Parent not in chain           -> 422 unknown_parent_invocation  (#4128)
  - target.repo outside tenant    -> 403 cross_tenant_target  (#4128)
  - Guard rejections              -> 422 guard_rejected
  - Missing required fields       -> 400 invalid_body
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# GSI propagation retry: a single retry after 200ms handles the common case
# where the parent just wrote the event row and the GSI hasn't replicated yet.
_GSI_RETRY_DELAY_S = 0.2
_GSI_MAX_ATTEMPTS = 2

# Issue #4128: how many rows of the chain to examine when validating a claimed
# parent_invocation_id. The correlation-index GSI is ordered by arrived_at, so
# this bounds the read while still spanning a realistic chain. Legitimate
# cross-issue lineage (#1828) points at a recent ancestor, not an arbitrary one.
#
# NOTE: no longer used to validate parent_invocation_id — see _parent_in_chain.
# A recency window silently rejected valid parents once a chain outgrew it.
_CHAIN_SCAN_LIMIT = 50

# Required body fields
_REQUIRED_FIELDS = ("correlation_id", "parent_invocation_id", "persona", "target")


def _require_signed_provenance() -> bool:
    """Whether an UNVERIFIABLE marker (indeterminate) is rejected.

    Issue #4128 / #4073 adopted decision 5: strict rejection of indeterminate
    provenance ships behind a flag, defaulted OFF, and is enabled per-env only
    after an operator confirms ``marker_signing_key`` holds a real generated
    value rather than the Terraform placeholder. Enabling it while no real key
    exists would 403 every agent-to-agent dispatch — the ALLOW_OPEN_SIGNUP
    failure mode of code and config landing out of step.

    A FORGED marker (verify_marker -> False) is rejected regardless of this
    flag: no legitimate caller ever sends a signature that fails to verify.
    """
    return os.environ.get("REQUIRE_SIGNED_PROVENANCE", "").lower() in (
        "1",
        "true",
        "yes",
    )


def handle_agent_trigger(event: dict, context) -> dict:
    """Handle POST /agent/trigger — IAM-authenticated agent spawn.

    The caller is authenticated by API Gateway AWS_IAM authorization. The
    Lambda receives the caller's IAM identity in event.requestContext.identity.

    Body schema:
        {
            "correlation_id": "<chain id>",
            "parent_invocation_id": "<caller's invocation id>",
            "persona": "<target persona>",
            "target": {"repo": "org/repo", "issue": 123},
            "reason": "<optional spawn reason>"
        }

    Returns:
        API Gateway response dict (202 on success, 4xx on reject).
    """
    start_time = time.time()

    # Parse body
    raw_body = event.get("body", "")
    if event.get("isBase64Encoded", False):
        import base64

        raw_body = base64.b64decode(raw_body).decode("utf-8")

    try:
        body = json.loads(raw_body) if raw_body else {}
    except (json.JSONDecodeError, ValueError):
        return _response(400, {"error": "invalid_json"})

    # Validate required fields
    missing = [f for f in _REQUIRED_FIELDS if not body.get(f)]
    if missing:
        if "correlation_id" in missing:
            return _response(
                400, {"error": "missing_lineage", "detail": "correlation_id is required"}
            )
        return _response(400, {"error": "invalid_body", "detail": f"missing fields: {missing}"})

    correlation_id = body["correlation_id"]
    parent_invocation_id = body["parent_invocation_id"]
    persona = body["persona"]
    target = body["target"]
    reason = body.get("reason", "")

    # Validate target shape
    if not isinstance(target, dict) or not target.get("repo") or not target.get("issue"):
        return _response(
            400, {"error": "invalid_body", "detail": "target must have repo and issue"}
        )

    target_repo = target["repo"]
    target_issue = int(target["issue"])

    # Resolve chain from correlation-index GSI (mandatory retry for GSI propagation)
    chain_record = _resolve_chain(correlation_id)
    if chain_record is None:
        return _response(
            422, {"error": "unknown_chain", "detail": "correlation_id not found in chain index"}
        )

    # Issue #4128: verify a body-supplied provenance marker BEFORE any of it is
    # read. The route previously accepted caller-declared lineage on trust, so
    # anything that could reach this internal plane could claim to be a fresh
    # chain rooted at any human — and authorized_user_id is computed server-side
    # from that claim, which makes the forgery indistinguishable from a real
    # human-rooted run downstream.
    marker, marker_error = _verified_marker(body)
    if marker_error is not None:
        return marker_error

    # Extract trust properties from chain record (server-resolved, not from body)
    chain_tenant_id = chain_record.get("tenant_id")
    chain_root_human_id = chain_record.get("root_human_id")
    # Issue #4128: default False, not True. A chain row missing the attribute is
    # a chain we cannot show is human-rooted — defaulting to True handed the
    # spawned run a human's authority on the strength of an ABSENT field.
    chain_is_human_rooted = bool(chain_record.get("is_human_rooted", False))

    # Issue #4128: chain_depth must never silently reset to 0. A reset makes the
    # runaway-chain guard (MAX_CHAIN_DEPTH in spawn_persona) stop bounding
    # recursion, because every hop re-enters the chain at depth 0.
    chain_depth, depth_error = _resolve_chain_depth(chain_record, marker, correlation_id)
    if depth_error is not None:
        return depth_error

    # Cross-tenant check (Issue #4128: absence is no longer a bypass).
    #
    # Previously guarded by `if body_tenant and ...`, so simply OMITTING
    # tenant_id skipped the check entirely — the trivial way around it. The
    # check now always runs: the chain must HAVE a tenant, and a body-supplied
    # tenant must match it.
    body_tenant = body.get("tenant_id")
    if not chain_tenant_id:
        logger.warning(
            "agent_trigger: chain has no tenant_id correlation=%s — refusing "
            "dispatch (cannot enforce tenant isolation)",
            correlation_id,
        )
        return _response(
            403,
            {"error": "cross_tenant", "detail": "chain record has no tenant_id"},
        )
    if body_tenant and body_tenant != chain_tenant_id:
        logger.warning(
            "agent_trigger: cross-tenant rejected body_tenant=%s chain_tenant=%s",
            body_tenant,
            chain_tenant_id,
        )
        return _response(
            403, {"error": "cross_tenant", "detail": "body tenant does not match chain"}
        )

    # Issue #4128: the claimed parent must actually be part of this chain.
    # Without this, parent_invocation_id is a free-text field that fabricates a
    # lineage edge the Activity chain view then renders as real.
    if not _parent_in_chain(correlation_id, parent_invocation_id, chain_record):
        logger.warning(
            "agent_trigger: parent_invocation_id=%s not found in chain "
            "correlation=%s — rejecting forged lineage edge",
            parent_invocation_id,
            correlation_id,
        )
        return _response(
            422,
            {
                "error": "unknown_parent_invocation",
                "detail": "parent_invocation_id does not belong to this chain",
            },
        )

    # Issue #4128: target.repo must belong to the chain's tenant. The channel
    # key and the spawned run's repo are both built from this body value, so an
    # unchecked repo points a legitimately-verified chain at another tenant.
    if not _repo_in_tenant(target_repo, chain_tenant_id, chain_record):
        logger.warning(
            "agent_trigger: target repo=%s outside chain tenant=%s — rejecting",
            target_repo,
            chain_tenant_id,
        )
        return _response(
            403,
            {
                "error": "cross_tenant_target",
                "detail": "target.repo does not belong to the chain's tenant",
            },
        )

    # Resolve caller identity from IAM context
    request_context = event.get("requestContext", {})
    caller_arn = request_context.get("identity", {}).get("userArn", "")

    # Build correlation context for spawn_persona
    correlation_ctx = {
        "correlation_id": correlation_id,
        "root_human_id": chain_root_human_id or "",
        "triggered_by": caller_arn,
        "is_human_rooted": chain_is_human_rooted,
        "is_new_chain": False,
        "parent_invocation_id": parent_invocation_id,
        "chain_depth": chain_depth + 1,
        # Carry forward loop-tracking from pointer if available
        "last_triggered_persona": chain_record.get("last_triggered_persona"),
        "recent_triggered_personas": set(chain_record.get("recent_triggered_personas") or []),
        "recent_trigger_count": _safe_int(chain_record.get("recent_trigger_count"), 0),
    }

    # Build channel key for pointer writes
    from common.correlation_store import channel_key

    channel_key_str = channel_key("github", target_repo, "issue", target_issue)

    # Build a synthetic resolved_identity for spawn_persona
    resolved_identity = _SyntheticIdentity(
        tenant_id=chain_tenant_id or "",
        org_id=chain_tenant_id or "",
        user_id=caller_arn,
        user_kind="bot",
        bot_kind=_extract_bot_kind_from_arn(caller_arn),
    )

    # Build minimal sender dict
    sender = {
        "login": f"iam:{caller_arn}",
        "id": 0,
        "type": "Bot",
    }

    # Resolve installation_id for the chain's tenant (Issue #2336)
    from common.installation_resolver import resolve_installation_for_tenant

    installation_id = resolve_installation_for_tenant(chain_tenant_id or "")
    if installation_id is None:
        logger.warning(
            "agent_trigger: no installation_id for tenant=%r — cannot dispatch",
            chain_tenant_id,
        )
        return _response(422, {"error": "no_installation_for_tenant"})

    # Build minimal payload for envelope (target context)
    payload = {
        "action": "agent_trigger",
        "issue": {
            "number": target_issue,
            "title": reason or "(agent-triggered)",
            "html_url": f"https://github.com/{target_repo}/issues/{target_issue}",
        },
        "repository": {"full_name": target_repo},
        "sender": sender,
        "installation": {"id": installation_id},
    }

    # Call spawn_persona (the single enforcement point)
    from common.spawn_persona import spawn_persona

    spawn_result = spawn_persona(
        persona=persona,
        correlation_ctx=correlation_ctx,
        channel_key=channel_key_str,
        resolved_identity=resolved_identity,
        tenant_id=chain_tenant_id or "",
        actor_user_id=caller_arn,
        actor_org_id=chain_tenant_id or "",
        sender=sender,
        event_type="agent_trigger",
        action="trigger",
        installation_id=installation_id,
        repo=target_repo,
        payload=payload,
        intent_trigger="agent_trigger",
        intent_label=None,
    )

    if not spawn_result.success:
        block_reason = spawn_result.block_reason or "error"
        logger.info(
            "agent_trigger: spawn blocked reason=%s persona=%s correlation=%s",
            block_reason,
            persona,
            correlation_id,
        )
        if block_reason == "sqs_publish_failed":
            return _response(500, {"error": "enqueue_failed"})
        return _response(422, {"error": "guard_rejected", "detail": block_reason})

    latency_ms = (time.time() - start_time) * 1000
    logger.info(
        "agent_trigger: spawned persona=%s correlation=%s message_id=%s latency_ms=%.1f",
        persona,
        correlation_id,
        spawn_result.message_id,
        latency_ms,
    )
    return _response(
        202,
        {
            "status": "accepted",
            "message_id": spawn_result.message_id,
            "correlation_id": correlation_id,
        },
    )


def _query_chain(correlation_id: str, limit: int) -> list[dict[str, Any]]:
    """Query the webhook-events correlation-index GSI for a chain's rows.

    Issue #4128: factored out of :func:`_resolve_chain` so the parent-invocation
    check reuses the SAME query — the reuse table in the issue is explicit that
    there must not be a second GSI query implementation to drift from this one.

    Uses mandatory single retry with 200ms backoff to handle GSI eventual
    consistency (the parent may have just written the row).

    Returns the most recent rows first, or an empty list when the chain is
    unknown/expired or the table is unconfigured.
    """
    import boto3
    from boto3.dynamodb.conditions import Key

    table_name = os.environ.get("EVENTS_TABLE", "")
    if not table_name:
        logger.error("agent_trigger: EVENTS_TABLE not configured")
        return []

    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)

    for attempt in range(_GSI_MAX_ATTEMPTS):
        try:
            resp = table.query(
                IndexName="correlation-index",
                KeyConditionExpression=Key("correlation_id").eq(correlation_id),
                ScanIndexForward=False,  # Most recent first
                Limit=limit,
            )
            items = resp.get("Items", [])
            if items:
                return items
        except Exception as e:
            logger.warning(
                "agent_trigger: GSI query failed attempt=%d error=%s",
                attempt + 1,
                e,
            )

        # Retry with backoff (only if not last attempt)
        if attempt < _GSI_MAX_ATTEMPTS - 1:
            time.sleep(_GSI_RETRY_DELAY_S)  # nosemgrep: arbitrary-sleep

    return []


def _query_event_row(event_id: str) -> list[dict[str, Any]]:
    """Query the webhook-events base table for one row by ``event_id``.

    ``event_id`` is the table's partition key (range key ``arrived_at``), so this
    is a bounded primary-key query rather than a scan. Used by
    :func:`_parent_in_chain` to resolve a claimed parent directly instead of
    hoping it falls inside a recency window — see that function for why the
    window approach broke long-lived chains (#4245).

    Mirrors :func:`_query_chain`'s retry policy: the parent may have written its
    row moments ago, and a read that races the write must not be read as "the
    row does not exist".

    Returns the matching row(s), or an empty list when the row does not exist,
    the query failed, or the table is unconfigured. The caller treats empty as
    fail-closed.
    """
    import boto3
    from boto3.dynamodb.conditions import Key

    table_name = os.environ.get("EVENTS_TABLE", "")
    if not table_name:
        logger.error("agent_trigger: EVENTS_TABLE not configured")
        return []

    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)

    for attempt in range(_GSI_MAX_ATTEMPTS):
        try:
            resp = table.query(
                KeyConditionExpression=Key("event_id").eq(event_id),
                Limit=1,
            )
            items = resp.get("Items", [])
            if items:
                return items
        except Exception as e:
            logger.warning(
                "agent_trigger: event row query failed attempt=%d error=%s",
                attempt + 1,
                e,
            )

        # Retry with backoff (only if not last attempt)
        if attempt < _GSI_MAX_ATTEMPTS - 1:
            time.sleep(_GSI_RETRY_DELAY_S)  # nosemgrep: arbitrary-sleep

    return []


def _resolve_chain(correlation_id: str) -> dict[str, Any] | None:
    """Return the most recent event row for this correlation_id, or None.

    None means the chain is unknown/expired — the caller MUST NOT mint a new
    root from it.
    """
    items = _query_chain(correlation_id, limit=1)
    return items[0] if items else None


def _verified_marker(body: dict) -> tuple[dict[str, Any] | None, dict | None]:
    """Verify a body-supplied provenance marker (Issue #4128).

    The signed-marker scheme already exists and is exercised in lockstep by
    ``test_marker_lockstep.py``; this route simply never invoked it. Reuses
    ``common/marker_verify.verify_marker`` rather than building a second
    verifier — the canonical signing input and key-rotation grace live there.

    A marker may be supplied either as raw text under ``provenance_marker``
    (the same ``<!-- adp-* -->`` form the worker writes into comment bodies) or
    as already-parsed marker fields under ``provenance``.

    Returns:
        ``(marker, None)`` when provenance is acceptable — ``marker`` is the
        verified marker dict, or None when the body carried no marker at all.
        ``(None, response)`` when the request must be rejected; ``response`` is
        the API Gateway 403 to return.

    Policy, and why it is split:
      - ``False`` (forged/tampered) -> 403 ALWAYS. Strip-and-continue is not an
        option on a credential-bearing plane, and no legitimate caller sends a
        signature that fails to verify, so this needs no flag.
      - ``None`` (unsigned, or no usable key — including the placeholder-key
        case #4128 introduces) -> 403 only when REQUIRE_SIGNED_PROVENANCE is
        enabled. The ``adp-trigger`` client does not sign yet, so rejecting
        unconditionally would be a total dispatch outage.
      - No marker in the body -> same treatment as an unsigned marker.
    """
    raw_marker = body.get("provenance_marker")
    parsed = body.get("provenance")

    marker: dict[str, Any] | None = None
    if raw_marker:
        from common.marker_parse import parse_marker

        marker = parse_marker(raw_marker)
    elif isinstance(parsed, dict) and parsed:
        marker = dict(parsed)

    if marker is None:
        # No provenance claim at all. Nothing forged; everything downstream is
        # server-resolved from the chain record.
        if _require_signed_provenance():
            logger.warning(
                "agent_trigger: no signed provenance supplied and "
                "REQUIRE_SIGNED_PROVENANCE is enabled — rejecting"
            )
            return None, _response(
                403,
                {
                    "error": "unverified_provenance",
                    "detail": "signed provenance marker required",
                },
            )
        return None, None

    from common.marker_verify import verify_marker

    verdict = verify_marker(marker)

    if verdict is False:
        logger.warning(
            "agent_trigger: provenance marker signature FAILED (forged) "
            "correlation=%s claimed_root_human=%s — rejecting",
            marker.get("correlation_id"),
            marker.get("root_human_id"),
        )
        return None, _response(
            403,
            {
                "error": "unverified_provenance",
                "detail": "provenance marker signature is invalid",
            },
        )

    if verdict is None:
        # Indeterminate: unsigned marker, no key configured, or the signing
        # secret still holds the Terraform placeholder. Never reported as
        # verified — that is the point of the placeholder guard.
        logger.warning(
            "agent_trigger: provenance marker is UNVERIFIABLE (unsigned or no "
            "usable signing key) correlation=%s — %s",
            marker.get("correlation_id"),
            "rejecting" if _require_signed_provenance() else "accepting (flag off)",
        )
        if _require_signed_provenance():
            return None, _response(
                403,
                {
                    "error": "unverified_provenance",
                    "detail": "provenance marker could not be verified",
                },
            )
        # Flag off: the marker continues to carry NO authority — trust values
        # below are still read from the chain record, never from this marker.
        return None, None

    return marker, None


def _resolve_chain_depth(
    chain_record: dict[str, Any],
    marker: dict[str, Any] | None,
    correlation_id: str,
) -> tuple[int, dict | None]:
    """Resolve this chain's depth, refusing to silently reset it (Issue #4128).

    Depth is taken from the VERIFIED marker when one was supplied (it reflects
    the calling run's own position), otherwise from the chain record.

    The previous code had two fallbacks to 0 — one for a malformed value and one
    for an absent one. Either turns the depth counter into a reset button, so
    the runaway-chain guard stops bounding recursion. Both are now errors.

    Returns ``(depth, None)`` or ``(0, response)`` with a 422 to return.
    """
    if marker is not None and marker.get("chain_depth") is not None:
        source = "marker"
        raw = marker.get("chain_depth")
    else:
        source = "chain"
        raw = chain_record.get("chain_depth")

    if raw is None:
        logger.warning(
            "agent_trigger: chain_depth absent on chain correlation=%s — "
            "refusing to reset depth to 0",
            correlation_id,
        )
        return 0, _response(
            422,
            {"error": "invalid_chain_depth", "detail": "chain_depth is missing"},
        )

    try:
        depth = int(raw)
    except (ValueError, TypeError):
        logger.warning(
            "agent_trigger: chain_depth=%r is malformed (source=%s) "
            "correlation=%s — refusing to reset depth to 0",
            raw,
            source,
            correlation_id,
        )
        return 0, _response(
            422,
            {"error": "invalid_chain_depth", "detail": "chain_depth is not an integer"},
        )

    if depth < 0:
        logger.warning(
            "agent_trigger: chain_depth=%d is negative (source=%s) correlation=%s "
            "— refusing (would evade the depth guard)",
            depth,
            source,
            correlation_id,
        )
        return 0, _response(
            422,
            {"error": "invalid_chain_depth", "detail": "chain_depth is negative"},
        )

    return depth, None


def _parent_in_chain(
    correlation_id: str,
    parent_invocation_id: str,
    chain_record: dict[str, Any],
) -> bool:
    """Whether the claimed parent_invocation_id belongs to this chain.

    Issue #4128: requires the claimed parent's ``event_id`` to appear under this
    ``correlation_id``. Reuses :func:`_query_chain` (the same correlation-index
    GSI query :func:`_resolve_chain` uses).

    Deliberately NOT stricter than that: legitimate cross-issue lineage (#1828)
    points at an ancestor that is not the newest row in the chain, so requiring
    "parent == the latest event" would fragment real chains. Matching ANY row of
    the chain is what the issue's design asks for.

    Resolved by PRIMARY-KEY LOOKUP, not by scanning a recency window. The
    previous implementation read the newest ``_CHAIN_SCAN_LIMIT`` (50) rows of
    the correlation-index GSI and required the parent to be among them, which
    made validity depend on how busy the chain had been rather than on whether
    the lineage edge was real. A long-lived orchestrator run dispatching several
    children is the pathological case: every child writes chain rows, so the
    orchestrator's OWN row slides out of the window and its later, entirely
    legitimate dispatches start failing closed with 422. Observed on issue #4245
    at 477 rows, where the caller's row was position 477/477.

    ``event_id`` is the base table's partition key, so "is this row in this
    chain?" is one bounded ``query`` on the row itself — strictly cheaper than
    the 50-row GSI read it replaces, and correct for chains of any length and
    any age. The chain membership assertion is preserved in full: the row must
    exist AND carry this ``correlation_id``.
    """
    if chain_record.get("event_id") == parent_invocation_id:
        # Fast path: the parent is the row we already read. Avoids a second query
        # for the common agent-just-wrote-its-row case.
        return True

    rows = _query_event_row(parent_invocation_id)
    if not rows:
        # Either the row genuinely does not exist (forged lineage — the case this
        # check exists to stop) or the query failed. Both are unverifiable, so
        # both fail closed.
        logger.warning(
            "agent_trigger: parent row lookup returned nothing for "
            "parent_invocation_id=%s correlation=%s — failing closed",
            parent_invocation_id,
            correlation_id,
        )
        return False

    # The row exists; it belongs to this chain only if its correlation matches.
    # Without this comparison the check would accept any real invocation id from
    # any chain, which is precisely the forged-lineage edge #4128 closed.
    return any(row.get("correlation_id") == correlation_id for row in rows)


def _repo_in_tenant(
    target_repo: str,
    chain_tenant_id: str,
    chain_record: dict[str, Any],
) -> bool:
    """Whether ``target_repo`` belongs to ``chain_tenant_id`` (Issue #4128).

    Accepts on the first of three signals to match, cheapest first:

      1. The target repo IS the chain's own repo (same-repo dispatch, the
         overwhelmingly common case — no lookup needed).
      2. The repo owner equals the chain's tenant id. Tenants are keyed by org
         login throughout the identity index, so this is the normal match for a
         sibling repo in the same org.
      3. ``resolve_installation_for_tenant(owner)`` — reused from
         ``common/installation_resolver.py`` per the issue's reuse table —
         resolves the owner to the SAME installation as the chain's tenant. This
         covers a tenant id that is not literally the org login.

    Fail-closed: an owner we cannot tie to the chain's tenant is rejected.
    """
    if not target_repo or "/" not in target_repo:
        return False

    chain_repo = chain_record.get("repo")
    if chain_repo and target_repo == chain_repo:
        return True

    owner = target_repo.split("/", 1)[0]
    if owner == chain_tenant_id:
        return True

    try:
        from common.installation_resolver import resolve_installation_for_tenant

        owner_installation = resolve_installation_for_tenant(owner)
        if owner_installation is None:
            return False
        tenant_installation = resolve_installation_for_tenant(chain_tenant_id)
        return (
            tenant_installation is not None and owner_installation == tenant_installation
        )
    except Exception as e:  # noqa: BLE001 — an unresolvable owner is a rejection
        logger.warning(
            "agent_trigger: repo-tenant resolution failed for repo=%s tenant=%s: "
            "%s — failing closed",
            target_repo,
            chain_tenant_id,
            e,
        )
        return False


class _SyntheticIdentity:
    """Minimal identity object for spawn_persona compatibility."""

    def __init__(self, *, tenant_id: str, org_id: str, user_id: str, user_kind: str, bot_kind: str):
        self.tenant_id = tenant_id
        self.org_id = org_id
        self.user_id = user_id
        self.user_kind = user_kind
        self.bot_kind = bot_kind


def _extract_bot_kind_from_arn(caller_arn: str) -> str:
    """Extract a bot_kind hint from the caller's IAM role ARN.

    Example: arn:aws:sts::123:assumed-role/adp-dev-agent-worker-role/session
    -> returns "" (no persona inference from ARN — guards use the target persona).
    """
    # We don't infer bot_kind from ARN — it would be unreliable.
    # The self-mention guard compares persona vs bot_kind; returning ""
    # means the guard won't fire for IAM callers (correct — the caller is
    # authenticated, not a bot mentioning itself).
    return ""


def _safe_int(value: Any, default: int) -> int:
    """Safely convert to int with fallback."""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _response(status_code: int, body: dict) -> dict:
    """Build API Gateway response."""
    return {
        "statusCode": status_code,
        "body": json.dumps(body),
        "headers": {"Content-Type": "application/json"},
    }
