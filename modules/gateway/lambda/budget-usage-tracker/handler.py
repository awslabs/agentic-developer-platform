"""
Budget Usage Tracker Lambda Handler.

Triggered by S3 PutObject events on the chat logs bucket. Extracts actual
token counts from Bedrock responses, calculates cost, and upserts usage
into the budget_usage table in RDS.

Issue #234: Budget Usage Tracking Lambda

Environment Variables:
    DB_HOST: RDS hostname
    DB_PORT: RDS port (default: 5432)
    DB_NAME: Database name (default: bedrockgateway)
    DB_USERNAME: Database username (default: bgadmin)
    AWS_REGION: AWS region (default: us-east-1)
"""

import hashlib
import json
import logging
import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import boto3

# Add shared module to path for local Lambda deployment
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))

from db import get_db_connection
from pricing_fallback import resolve_model_id
from pricing_legacy_reader import get_legacy_rates
from pricing_settlement import (
    InvalidPricingDecisionError,
    MissingUsageError,
    compatibility_snapshot,
    settle_chat_log,
)
from pricing_v2_reader import cache_failure_age_seconds, get_rate_state
from root_principal import unqualify_root_principal_id

from pricing_policy import is_openai_model

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# S3 client
s3_client = boto3.client("s3")

# Issue #4969: the former module-level pricing cache and its 1-hour TTL are gone.
# Caching now lives in `pricing_v2_reader`/`pricing_policy.storage`, which has the
# retention rule this one lacked: a failed read keeps the last known good
# generation rather than reverting to bundled rates, because the bundle is by
# definition older and reverting would silently reprice live traffic.

# Issue #4300: the settled-ledger entity_type for the human who initiated an
# agent chain.
#
# This MUST stay equal to `EntityType.ROOT_USER.value` in
# src/shared/schemas/budget.py — this Lambda is a separate deploy artifact and
# cannot import gateway `src`, so the agreement is pinned by a test rather than
# by the type system (see tests/lambda/test_budget_usage_tracker.py::T15).
#
# Why a named constant and not an inline literal like the ("user", ...) entry
# below: those hand-written literals are exactly how the org line drifted from
# the reader's "org" (fixed in #4322, see `_ORGANIZATION_ENTITY_TYPE`), where a
# writer/reader mismatch means enforcement silently reads an empty ledger and
# every cap passes. Do not inline this string.
_ROOT_USER_ENTITY_TYPE = "root_user"

# Issue #4322: the settled-ledger entity_type for an organization.
#
# This MUST stay equal to `EntityType.ORGANIZATION.value` in
# src/shared/schemas/budget.py — same separate-deploy-artifact reasoning as
# `_ROOT_USER_ENTITY_TYPE` above, pinned by the same test.
#
# It is "org", NOT "organization". This Lambda wrote the hand-written literal
# `"organization"` from #234 until #4322, while enforcement has always queried
# `EntityType.ORGANIZATION.value` == "org" — so `_check_entity_budget` read the
# org's accumulated spend as 0 on every request and the org cap never enforced
# against the persisted period total. Nothing raised and nothing logged; the cap
# was simply inert. Migration 032 merges the historical `"organization"` rows
# into their `"org"` counterparts.
#
# Do not "tidy" this to the longer word to match `src/ratelimit/models.py`'s
# EntityType.ORGANIZATION — that is a DIFFERENT enum keying rate_limit_configs,
# and the two tables genuinely disagree on this string.
_ORGANIZATION_ENTITY_TYPE = "org"


def get_period_starts(timestamp: datetime) -> dict[str, datetime]:
    """
    Calculate period start dates for daily, weekly, and monthly periods.

    Args:
        timestamp: The timestamp of the chat log

    Returns:
        Dict with period_type -> period_start mapping
    """
    # Ensure timezone-aware
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)

    date = timestamp.date()

    # Daily: start of the day
    daily_start = datetime(date.year, date.month, date.day, tzinfo=UTC)

    # Weekly: Monday of the week (weekday() returns 0 for Monday)
    days_since_monday = date.weekday()
    monday = date - timedelta(days=days_since_monday)
    weekly_start = datetime(monday.year, monday.month, monday.day, tzinfo=UTC)

    # Monthly: first of the month
    monthly_start = datetime(date.year, date.month, 1, tzinfo=UTC)

    return {
        "daily": daily_start,
        "weekly": weekly_start,
        "monthly": monthly_start,
    }


def emit_pricing_metrics(reasons=()):
    age = cache_failure_age_seconds()
    values = {"PricingCacheAgeSeconds": age}
    if age > 0:
        values["PricingCacheRefreshFailure"] = 1
    for reason, metric in (
        ("unknown_model", "UnknownModelPricing"),
        ("unsupported_variant", "PricingUnknownVariant"),
        ("stale_rate_source", "PricingStaleRate"),
    ):
        if reason in reasons:
            values[metric] = 1
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {"Namespace": "ADP/Gateway", "Dimensions": [[]], "Metrics": [{"Name": key, "Unit": "None"} for key in values]}
                    ],
                },
                **values,
            }
        )
    )


def get_rate_source(conn):
    """
    Get the rate rows to settle with, from the active V2 pricing generation.

    Issue #4969: replaces the former `model_pricing` flat-table load. That table
    is keyed on model_id alone, so it could not express geography, service tier,
    context tier or cache rates — a GovCloud long-context Priority request
    settled at the same rate as an in-region short-context Flex one. Its rows
    also included `source='fallback'` OpenAI prices that were simply wrong (up to
    5x for Luna). Reading V2 instead is what stops those rows affecting current
    settlement.

    The reader keeps its own process-wide cache with the retention rules in
    `pricing_policy.storage`, so the 1-hour TTL this function used to implement
    is gone from here on purpose: a read failure now retains the last known good
    generation instead of silently reverting to bundled rates.

    Returns:
        A `RateSourceState`: rows, provenance and any estimate reasons the way
        those rows were obtained implies.
    """
    state = get_rate_state(conn)
    emit_pricing_metrics(state.reasons)
    return state


def parse_chat_log(chat_log: dict[str, Any]) -> dict[str, Any] | None:
    """
    Parse and validate chat log JSON.

    Issue #249: Added support for agent usage tracking via account_type and
    agent_id fields in chat logs.

    Args:
        chat_log: Parsed chat log dictionary

    Returns:
        Parsed data dict or None if invalid
    """
    required_fields = ["org_id", "user_id", "model"]

    for field in required_fields:
        if field not in chat_log:
            logger.warning(f"Missing required field: {field}")
            return None

    # Extract usage from response
    response = chat_log.get("response", {})
    usage = response.get("usage", {})

    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")

    if input_tokens is None or output_tokens is None:
        logger.warning("Missing token counts in response.usage")
        return None

    # Issue #1486: Extract prompt-cache token counts for correct cost calculation
    cache_read_input_tokens = usage.get("cache_read_input_tokens", 0)
    cache_creation_input_tokens = usage.get("cache_creation_input_tokens", 0)

    # Parse timestamp
    timestamp_str = chat_log.get("timestamp")
    if timestamp_str:
        try:
            # Handle ISO format with Z suffix
            if timestamp_str.endswith("Z"):
                timestamp_str = timestamp_str[:-1] + "+00:00"
            timestamp = datetime.fromisoformat(timestamp_str)
        except ValueError:
            logger.warning(f"Invalid timestamp format: {timestamp_str}")
            timestamp = datetime.now(UTC)
    else:
        timestamp = datetime.now(UTC)

    return {
        "org_id": chat_log["org_id"],
        "user_id": chat_log["user_id"],
        "team_id": chat_log.get("team_id"),
        # Issue #4300: the initiating human. Deliberately `.get()` and
        # deliberately NOT in `required_fields` above — it is legitimately absent
        # on every non-human-rooted request and on every log written before #4300
        # shipped. Requiring it would make this Lambda drop those logs entirely
        # and stop recording ALL budget usage for them: a missing attribution
        # field would become a total metering outage. Same handling as `agent_id`.
        "root_human_id": chat_log.get("root_human_id"),
        "model": chat_log["model"],
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        # Issue #1486: Prompt-cache token counts for correct cost calculation
        "cache_read_input_tokens": int(cache_read_input_tokens or 0),
        "cache_creation_input_tokens": int(cache_creation_input_tokens or 0),
        "timestamp": timestamp,
        # Issue #1016: request_id for bridging cost back to usage_logs
        "request_id": chat_log.get("request_id"),
        # Issue #249: Agent-specific fields
        "account_type": chat_log.get("account_type"),  # "service" for agents
        "agent_id": chat_log.get("agent_id"),  # Agent UUID if IAM-authenticated
        "budget_config_id": chat_log.get("budget_config_id"),  # Agent's budget config ID
    }


def upsert_budget_usage(
    conn,
    org_id: str,
    entity_type: str,
    entity_id: str,
    period_start: datetime,
    period_type: str,
    cost: Decimal,
    tokens: int,
):
    """
    Upsert budget usage record.

    Uses PostgreSQL ON CONFLICT to atomically increment usage.

    Args:
        conn: Database connection
        org_id: Organization ID
        entity_type: Entity type (user/team/org/agent/root_user) — must be a
            value of `EntityType` in src/shared/schemas/budget.py, which is what
            enforcement queries this table with (#4322)
        entity_id: Entity ID
        period_start: Start of the period
        period_type: Period type (daily/weekly/monthly)
        cost: Cost in USD
        tokens: Total tokens (input + output)
    """
    usage_id = str(uuid.uuid4())

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO budget_usage (
                id, org_id, entity_type, entity_id, period_start, period_type,
                total_cost_usd, total_tokens, request_count
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1)
            ON CONFLICT (org_id, entity_type, entity_id, period_start, period_type)
            DO UPDATE SET
                total_cost_usd = budget_usage.total_cost_usd + EXCLUDED.total_cost_usd,
                total_tokens = budget_usage.total_tokens + EXCLUDED.total_tokens,
                request_count = budget_usage.request_count + 1
            """,
            (
                usage_id,
                org_id,
                entity_type,
                entity_id,
                period_start.date(),
                period_type,
                cost,
                tokens,
            ),
        )


def bridge_cost_to_usage_logs(
    conn, request_id: str, cost: Decimal, chat_log_s3_key: str | None = None, *, org_id: str, user_id: str, atomic: bool = False
) -> bool:
    """
    Bridge calculated cost back to the usage_logs table.

    Issue #1074: Updates usage_logs.cost_usd for the matching request_id so
    the admin dashboard cost tile (which reads SUM(usage_logs.cost_usd)) shows
    real numbers.

    Issue #1616: Also writes chat_log_s3_key (the S3 object key for the
    request/response payload). Uses COALESCE so the key is written even on
    retry when cost_usd is already set, but never overwrites an existing key.

    Args:
        conn: Database connection
        request_id: The request ID linking the chat log to usage_logs
        cost: Calculated cost in USD
        chat_log_s3_key: Optional S3 object key for the chat log payload

    Returns:
        True if a row was updated, False otherwise
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE usage_logs
                SET cost_usd = CASE WHEN cost_usd = 0 THEN %s ELSE cost_usd END,
                    chat_log_s3_key = COALESCE(chat_log_s3_key, %s)
                WHERE request_id = %s AND org_id = %s AND user_id = %s
                  AND (cost_usd = 0 OR chat_log_s3_key IS NULL)
                """,
                (cost, chat_log_s3_key, request_id, org_id, user_id),
            )
            updated = cur.rowcount > 0
            if updated:
                logger.info(f"Bridged cost_usd=${cost} to usage_logs for request_id={request_id}")
            else:
                logger.debug(f"No usage_logs row found for request_id={request_id} (or already populated)")
            return updated
    except Exception as e:
        logger.warning(f"Failed to bridge cost to usage_logs: {e}")
        # The failed statement aborted the transaction — roll back so
        # subsequent statements on this connection don't fail with
        # InFailedSqlTransaction.
        if atomic:
            raise
        conn.rollback()
        return False


def process_chat_log(conn, chat_log: dict[str, Any], rate_source, chat_log_s3_key: str | None = None):
    """
    Process a single chat log and record usage.

    Issue #249: Added agent-level usage tracking. When a chat log has
    account_type="service" and agent_id is present, usage is also recorded
    to the agent entity (entity_type="agent").

    Issue #1074: Bridges calculated cost back to usage_logs.cost_usd so
    the admin dashboard cost tile shows real spend.

    Issue #1616: Also bridges the S3 object key for per-run traceability.

    Args:
        conn: Database connection
        chat_log: Parsed chat log dictionary
        rate_source: `RateSourceState` from `get_rate_source` — the rows of the
            active V2 generation, their provenance, and the estimate reasons the
            way they were obtained implies (issue #4969)
        chat_log_s3_key: Optional S3 object key for the chat log payload
    """
    if type(chat_log.get("settlement_version")) is not int or chat_log["settlement_version"] != 1:
        raise ValueError("Legacy transcript requires explicit settlement reconciliation")
    parsed = parse_chat_log(chat_log)
    if not parsed:
        raise ValueError("Invalid chat log cannot be settled")

    org_id = parsed["org_id"]
    user_id = parsed["user_id"]
    team_id = parsed["team_id"]
    model_id = parsed["model"]
    input_tokens = parsed["input_tokens"]
    output_tokens = parsed["output_tokens"]
    # Issue #1486: Extract prompt-cache token counts
    cache_read_input_tokens = parsed.get("cache_read_input_tokens", 0)
    cache_creation_input_tokens = parsed.get("cache_creation_input_tokens", 0)
    timestamp = datetime.fromisoformat(chat_log["timestamp"].replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("Settlement timestamp requires an explicit timezone")
    timestamp = timestamp.astimezone(UTC)
    request_id = parsed.get("request_id")

    # Issue #249: Agent-specific fields
    account_type = parsed.get("account_type")
    agent_id = parsed.get("agent_id") or (user_id if account_type == "service" else None)

    # Issue #4300: the human who initiated this agent chain, if any.
    root_human_id = parsed.get("root_human_id")

    # Resolve cross-region model ID
    resolved_model_id = resolve_model_id(model_id)

    # Issue #4969: settle from the durable decision the gateway attached, or —
    # for a legacy OpenAI event that carries none — from the pinned bundle,
    # marked estimated. Never recompute a present decision: it was priced against
    # one immutable generation at response time, and re-pricing it here would make
    # the amount depend on when this S3 event happened to be delivered.
    #
    # A malformed decision raises. It must NOT degrade to a re-price, which would
    # silently defeat the reproducibility the decision exists to provide. It
    # propagates to the per-record handler in `lambda_handler`, which counts the
    # record as an error and leaves it for investigation without poisoning the
    # other records in the batch.
    settlement = settle_chat_log(
        parsed,
        chat_log=chat_log,
        rows=rate_source.rows,
        # The COMPATIBILITY snapshot, not the current one: it supplies the curated
        # non-OpenAI policy and the context threshold used to price a legacy event,
        # and pinning it means re-settling the same event after a new snapshot
        # ships cannot change its amount.
        snapshot=compatibility_snapshot(),
        generation_id=rate_source.generation_id,
        pointer_revision=rate_source.pointer_revision,
        source_reasons=rate_source.reasons,
        legacy_rates=get_legacy_rates(conn) if chat_log.get("pricing_decision") is None and not is_openai_model(model_id) else None,
    )
    emit_pricing_metrics(settlement.reasons)
    logger.info(
        "Pricing settlement: request=%s reused=%s estimated=%s reasons=%s", request_id, settlement.reused, settlement.estimated, settlement.reasons
    )
    cost = settlement.cost
    # Issue #1486: total_tokens includes cache tokens for accurate consumption tracking
    total_tokens = settlement.total_tokens

    logger.info(
        f"Processing: model={model_id}, resolved={resolved_model_id}, "
        f"input={input_tokens}, output={output_tokens}, "
        f"cache_read={cache_read_input_tokens}, cache_creation={cache_creation_input_tokens}, "
        f"cost=${cost}"
    )

    # Get period starts
    periods = get_period_starts(timestamp)

    # Record usage for each entity level and period type
    entities = [
        ("user", user_id),
        # Issue #4322: the constant, never the literal — see its definition.
        (_ORGANIZATION_ENTITY_TYPE, org_id),
    ]

    # Add team if present
    if team_id:
        entities.append(("team", team_id))

    if chat_log.get("department_id"):
        entities.append(("department", chat_log["department_id"]))

    # Issue #249: Add agent entity if this is an IAM-authenticated agent request
    if account_type == "service" and agent_id:
        entities.append(("agent", agent_id))
        logger.info(f"Including agent entity: {agent_id}")

    # Issue #4300: attribute the spend to the human who set this chain in motion,
    # so a person's budget is the envelope for everything they trigger and not
    # just their own first hop.
    #
    # Gated on presence: an empty/absent root_human_id must add NO row. A row
    # keyed on "" would collapse every non-human-rooted request in the tenant
    # into one shared bogus ledger line.
    #
    # This is a THIRD row, not a second debit on the org's — each
    # (entity_type, entity_id) is a distinct row under the table's
    # UniqueConstraint, so the org line still receives exactly `cost`.
    #
    # Issue #4391: ALSO gated on `!= user_id`, mirroring the reader's guard at
    # `src/budget/enforcement_service.py:437`. When the root principal IS the
    # caller, this cost is already on the ("user", user_id) row above, and
    # `root_user` is a distinct row under `uq_budget_usage` — so writing both
    # settles one dollar twice, x3 period types. Enforcement has always skipped
    # the entity in that case, so before this gate existed the tracker was
    # writing a line no cap ever read and only the spend dashboard summed: a
    # pure write/read asymmetry (same family as #4322's org-line mismatch),
    # surfacing as over-reported spend. The two skip conditions are now
    # co-extensive, which is why this cannot move any cap either way.
    #
    # Fires for the two cases enforcement documents at `:420-429`: the
    # service-rooted run (`user_id="k"`, `root_human_id="service:k"`) and the
    # direct human caller (`user_id == root_human_id`, both bare). It is a
    # no-op for the hosted agent run #4300 exists for — there `user_id` is the
    # worker identity and `root_human_id` a canonical `users.id`, which can
    # never be equal — so human attribution survives untouched.
    #
    # Issue #4344: the comparison UNQUALIFIES; the entity id stays QUALIFIED.
    # `root_human_id` carries the `service:` prefix for a service root while
    # `user_id` never does, so comparing them verbatim would report "different"
    # for exactly the service-rooted run this is meant to catch. The key written
    # to the ledger must stay the qualified one — that is the key enforcement
    # reads.
    #
    # Presence gate retained deliberately: see above, a "" root must add no row.
    if root_human_id and unqualify_root_principal_id(root_human_id) != user_id:
        entities.append((_ROOT_USER_ENTITY_TYPE, root_human_id))
        logger.info(f"Including root-human entity: {root_human_id}")

    entities = sorted(set(entities))
    day = timestamp.astimezone(UTC).date()
    allocation_key = hashlib.sha256(json.dumps([day.isoformat(), entities], separators=(",", ":")).encode()).hexdigest()
    if not request_id:
        raise ValueError("Legacy log has no settlement identity; explicit reconciliation required")
    # Claim and ALL additive debits share the caller's transaction. A failed fanout
    # rolls back the claim, so redelivery can finish without losing/doubling spend.
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO budget_settlement_receipts
            (org_id, request_id, user_id, cost_usd, total_tokens, allocation_key)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (org_id, request_id) DO NOTHING RETURNING request_id""",
            (org_id, request_id, user_id, cost, total_tokens, allocation_key),
        )
        if cur.fetchone() is None:
            cur.execute(
                """SELECT user_id, cost_usd, total_tokens, allocation_key FROM budget_settlement_receipts
                WHERE org_id = %s AND request_id = %s""",
                (org_id, request_id),
            )
            existing = cur.fetchone()
            if existing != (user_id, cost, total_tokens, allocation_key):
                raise ValueError("Conflicting settlement replay")
            if cost > 0 or chat_log_s3_key:
                bridge_cost_to_usage_logs(conn, request_id, cost, chat_log_s3_key=chat_log_s3_key, org_id=org_id, user_id=user_id, atomic=True)
            return

    if cost > 0 or chat_log_s3_key:
        bridge_cost_to_usage_logs(conn, request_id, cost, chat_log_s3_key=chat_log_s3_key, org_id=org_id, user_id=user_id, atomic=True)

    for entity_type, entity_id in entities:
        for period_type, period_start in periods.items():
            upsert_budget_usage(
                conn,
                org_id,
                entity_type,
                entity_id,
                period_start,
                period_type,
                cost,
                total_tokens,
            )

    logger.info(f"Recorded usage for {len(entities)} entities x {len(periods)} periods")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Lambda handler for S3 PutObject events.

    Processes chat log JSON files and records budget usage.

    Args:
        event: S3 event notification
        context: Lambda context

    Returns:
        Response dict with status
    """
    records = event.get("Records", [])
    logger.info(f"Received {len(records)} S3 event records")

    processed_count = 0
    error_count = 0

    try:
        with get_db_connection() as conn:
            # Issue #4969: read the active V2 generation once per invocation. The
            # reader caches across invocations in this container and retains the
            # last known good generation through a read failure, so this is not the
            # per-record cost it appears to be.
            rate_source = get_rate_source(conn)

            # Process each S3 record
            for record in event.get("Records", []):
                try:
                    # Extract S3 bucket and key
                    s3_info = record.get("s3", {})
                    bucket = s3_info.get("bucket", {}).get("name")
                    key = s3_info.get("object", {}).get("key")

                    if not bucket or not key:
                        logger.warning(f"Missing bucket or key in record: {record}")
                        error_count += 1
                        continue

                    from urllib.parse import unquote_plus

                    key = unquote_plus(key)
                    # Skip non-JSON files
                    if not key.endswith(".json"):
                        logger.info(f"Skipping non-JSON file: {key}")
                        continue

                    logger.info(f"Processing s3://{bucket}/{key}")

                    version_id = s3_info.get("object", {}).get("versionId")
                    # Versioned notifications must read exactly their own object.
                    response = s3_client.get_object(Bucket=bucket, Key=key, **({"VersionId": version_id} if version_id else {}))
                    body = response["Body"].read().decode("utf-8")
                    chat_log = json.loads(body)
                    expected_prefix = f"{chat_log.get('org_id')}/{chat_log.get('user_id') or 'anonymous'}/"
                    if not key.startswith(expected_prefix):
                        raise ValueError("Transcript key does not match settlement owner")
                    if chat_log.get("request_id") and key.rsplit("/", 1)[-1] != f"{chat_log['request_id']}.json":
                        raise ValueError("Transcript key does not match settlement request")

                    # Process the chat log (issue #1616: pass S3 key for traceability)
                    process_chat_log(conn, chat_log, rate_source, chat_log_s3_key=key)
                    # Per-record commit: one bad record must not poison the
                    # shared connection or roll back other records' writes.
                    conn.commit()
                    processed_count += 1

                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON in {key}: {e}")
                    error_count += 1
                except InvalidPricingDecisionError as e:
                    # Issue #4969: a decision was present and failed validation.
                    # Logged distinctly because the remedy is different from any
                    # other record error: the amount is NOT recoverable by retry
                    # (a retry validates the same bad payload), and it means either
                    # a gateway bug or a tampered settlement event. Never fall back
                    # to re-pricing the request — that would silently defeat the
                    # durable decision and bill an amount nobody quoted.
                    logger.error(f"Invalid pricing decision in {key}, record not settled: {e}", exc_info=True)
                    conn.rollback()
                    error_count += 1
                except MissingUsageError as e:
                    # Issue #4968 preserved: a log with no usable token counts is
                    # not settled and not counted as zero. A zero-cost row would
                    # look like a free request and permanently under-report that
                    # tenant's spend, so it is surfaced as an error instead.
                    logger.error(f"No usable token counts in {key}, record not settled: {e}")
                    conn.rollback()
                    error_count += 1
                except Exception as e:
                    logger.error(f"Error processing record: {e}", exc_info=True)
                    conn.rollback()
                    error_count += 1

    except Exception as e:
        logger.error(f"Database connection error: {e}", exc_info=True)
        raise

    if error_count:
        # S3 invokes Lambda asynchronously: returning HTTP 500 still acknowledges
        # the event. Raise so failed records retry; successful receipts deduplicate.
        raise RuntimeError(f"{error_count} settlement records failed; {processed_count} committed")

    result = {
        "statusCode": 200,
        "body": json.dumps(
            {
                "processed": processed_count,
                "errors": error_count,
            }
        ),
    }

    logger.info(f"Completed: {result['body']}")
    return result
