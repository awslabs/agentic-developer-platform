"""My conversations (#9026, epic #9023): one entry per logical conversation, newest activity first.

Groups the owner's ``usage_logs`` rows by the conversation identity the proxy records (#9024):
``harness`` + ``conversation_id``. Rows the harness gave no id are kept visible as one
"untracked" bucket per harness per day. Identity spans the caller's own rows and the rows a Dev
Box wrote for them under the guest identity (``owner_user_id``), and both user records a GitHub
login can carry today (the JWT subject and the tenant member row).

The Dev Box's 15-minute backups are the second source (#9069): Kiro Crew chats and the owner's own
Claude Code and Codex sessions on the box, indexed in ``crew_conversations`` with their turns in the
archive. They are merged into the same newest-first page; a gateway-derived Dev Box entry of the same
harness that names the same conversation or overlaps it in time is absorbed into the backup entry.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import String, and_, func, literal_column, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user
from src.chat_logging.config import get_chat_logging_settings
from src.crew.checkpoint_objects import InvalidCheckpointObjectsError
from src.crew.conversation_index import codex_turns_extract
from src.crew.targets import CrewOwner
from src.proxy.client_tool import KNOWN_CLIENT_TOOLS
from src.proxy.conversation_identity import ORIGIN_DEV_BOX, ORIGIN_DIRECT
from src.shared.database import get_db
from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/me/conversations", tags=["conversations"])
NO_CACHE = {"Cache-Control": "no-store"}
UNTRACKED_PREFIX = "untracked:"
CHAT_LOG_MAX_BYTES = 1024 * 1024
#: Without `since`, the list looks this far back; older conversations are reachable with an explicit `since`.
DEFAULT_WINDOW_DAYS = 30
TURN_TEXT_CHARS = 20_000
SOURCE_GATEWAY, SOURCE_BACKUP = "gateway", "backup"
#: Backup index source → harness name the proxy records, and back.
BACKUP_HARNESS = {"kiro_crew": "kiro_crew", "claude_code": "claude_code", "codex": "codex_cli"}
BACKUP_SOURCE = {harness: source for source, harness in BACKUP_HARNESS.items()}
#: A gateway-derived Dev Box entry this close in time to a backup entry of the same harness is the same conversation
#: (Claude Code in Bedrock mode carries no session id, so its gateway rows are grouped by a derived id).
ABSORB_SLACK = timedelta(minutes=10)
BACKUP_TURNS_LIMIT = 200


def conversation_key():
    """The grouping key: the recorded id, else one bucket per day of untracked turns.

    The prefix is an inline literal, not a bind parameter: Postgres compares a GROUP BY expression with the
    SELECT expression textually, and two bound copies of the same string ($1 vs $4) do not match
    ("must appear in the GROUP BY clause", dev 2026-10-27). Callers also reuse one instance per query.
    """
    return func.coalesce(UsageLog.conversation_id, literal_column(f"'{UNTRACKED_PREFIX}'") + func.cast(func.date(UsageLog.timestamp), String))


async def crew_owner(db: AsyncSession, context: TokenContext) -> CrewOwner | None:
    """The caller's tenant member identity (the Dev Box owner), or None when they have no member row yet."""
    try:
        from src.crew.proxy import resolve_crew_owner

        return await resolve_crew_owner(db, context)
    except HTTPException:
        return None


async def owner_identities(db: AsyncSession, context: TokenContext) -> set[str]:
    """The user ids whose rows belong to this caller: the JWT subject and the tenant member row."""
    identities = {context.user_id}
    owner = await crew_owner(db, context)
    if owner is not None:
        identities.add(owner.user_id)
    return identities


def backup_store():
    from src.crew.checkpoint_routes import get_store

    return get_store()


def backup_objects():
    from src.crew.checkpoint_routes import get_objects

    return get_objects()


async def backup_conversations(
    owner: CrewOwner | None, *, limit: int, before: datetime | None, harness: str | None, origin: str | None
) -> list[dict[str, Any]]:
    """The owner's Dev Box backup conversations as list entries, newest first; empty when none apply or the store is down."""
    if owner is None or origin not in (None, ORIGIN_DEV_BOX) or (harness is not None and harness not in BACKUP_SOURCE):
        return []
    try:
        rows = await backup_store().list_conversations(
            owner, source=BACKUP_SOURCE[harness] if harness else None, limit=limit, before=before, owner_shell=True
        )
    except Exception:  # noqa: BLE001 - the gateway's own records still answer
        logger.warning("conversation backups unavailable for %s", owner.user_id, exc_info=True)
        return []
    return [
        {
            "harness": BACKUP_HARNESS[row["source"]],
            "conversation_id": row["conversation_id"],
            "untracked": False,
            "origin": ORIGIN_DEV_BOX,
            "title": row["title"],
            "started_at": row["started_at"],
            "last_activity_at": row["last_activity_at"],
            "turns": int(row["turn_count"]),
            "requests": 0,
            "models": [],
            "cost_usd": 0.0,
            "source": SOURCE_BACKUP,
            "partial": _prompts_only(row["source"], row["archive_path"]),
            "unreadable": bool(row["unreadable"]),
        }
        for row in rows
        if row["last_activity_at"] is not None
    ]


def _prompts_only(source: str, archive_path: str) -> bool:
    """Codex backups before #9073 hold the owner's prompts only; the turns extract holds both sides."""
    return source == "codex" and not codex_turns_extract(archive_path or "")


def _when(entry: dict[str, Any], key: str = "last_activity_at") -> datetime | None:
    return _parse(entry.get(key))


def _absorbing_backup(entry: dict[str, Any], backups: list[dict[str, Any]]) -> dict[str, Any] | None:
    if entry["origin"] != ORIGIN_DEV_BOX:
        return None
    start, last = _when(entry, "started_at"), _when(entry)
    for backup in backups:
        if backup["harness"] != entry["harness"]:
            continue
        if backup["conversation_id"] == entry["conversation_id"]:
            return backup
        backup_start, backup_last = _when(backup, "started_at"), _when(backup)
        if None in (start, last, backup_start, backup_last):
            continue
        if start <= backup_last + ABSORB_SLACK and last >= backup_start - ABSORB_SLACK:
            return backup
    return None


def merge_pages(gateway: list[dict[str, Any]], backups: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], str | None]:
    """One newest-first page from both sources with one cursor; absorbed gateway entries move their cost and models."""
    remaining = []
    for entry in gateway:
        twin = _absorbing_backup(entry, backups)
        if twin is None:
            remaining.append(entry)
            continue
        twin["cost_usd"] = round(twin["cost_usd"] + entry["cost_usd"], 6)
        twin["requests"] += entry["turns"]
        twin["models"] = twin["models"] + [model for model in entry["models"] if model not in twin["models"]]
    merged = sorted(remaining + backups, key=lambda item: _when(item) or datetime.min.replace(tzinfo=UTC), reverse=True)
    # k-way merge paging: a source that filled its page may hold older entries, so nothing older than its last one is shown yet.
    full = [_when(page[-1]) for page in (gateway, backups) if len(page) >= limit and _when(page[-1]) is not None]
    cursor = min(full) if full else None
    if cursor is not None:
        merged = [item for item in merged if (_when(item) or cursor) >= cursor]
    if len(merged) > limit:
        merged = merged[:limit]
        cursor = _when(merged[-1])
    return merged, _iso(cursor)


def _owned(identities: set[str], context: TokenContext):
    """Rows that belong to the caller.

    Their own rows carry their tenant. Rows a Dev Box wrote for them carry the guest identity's tenant
    (``__platform__`` on dev, 2026-10-27) with the owner's member id in ``owner_user_id`` — that id is a
    tenant-scoped ``users.id``, so matching it needs no org predicate. Rows the proxy never labelled
    (before the capture shipped) have no harness and are not conversations.
    """
    ids = sorted(identities)
    return (
        UsageLog.harness.is_not(None),
        or_(and_(UsageLog.org_id == context.org_id, UsageLog.user_id.in_(ids)), UsageLog.owner_user_id.in_(ids)),
    )


def _valid_harness(value: str | None) -> bool:
    return value is None or value in KNOWN_CLIENT_TOOLS


@router.get("")
async def list_conversations(
    context: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before: Annotated[datetime | None, Query(description="Return conversations whose last activity is before this instant")] = None,
    harness: Annotated[str | None, Query()] = None,
    origin: Annotated[str | None, Query()] = None,
    since: Annotated[datetime | None, Query(description="Ignore conversations whose last activity is before this instant")] = None,
) -> JSONResponse:
    if not _valid_harness(harness) or origin not in (None, ORIGIN_DEV_BOX, ORIGIN_DIRECT):
        raise HTTPException(400, detail={"error": "invalid_filter"})
    owner = await crew_owner(db, context)
    identities = {context.user_id} | ({owner.user_id} if owner else set())
    backups = await backup_conversations(owner, limit=limit, before=before, harness=harness, origin=origin)
    key_expr = conversation_key()
    key = key_expr.label("key")
    last = func.max(UsageLog.timestamp).label("last_activity_at")
    grouped = (
        select(
            UsageLog.harness.label("harness"),
            key,
            func.min(UsageLog.timestamp).label("started_at"),
            last,
            func.count().label("turns"),
            func.sum(UsageLog.cost_usd).label("cost_usd"),
            func.max(UsageLog.origin).label("origin"),
        )
        .where(*_owned(identities, context))
        .group_by(UsageLog.harness, key_expr)
        .order_by(last.desc())
        .limit(limit)
    )
    if harness:
        grouped = grouped.where(UsageLog.harness == harness)
    if origin:
        grouped = grouped.where(UsageLog.origin == origin)
    window_start = since or datetime.now(UTC) - timedelta(days=DEFAULT_WINDOW_DAYS)
    # A row filter, not a HAVING: it is what lets the partial (user_id, timestamp) index bound the scan.
    grouped = grouped.where(UsageLog.timestamp >= window_start)
    if before is not None:
        grouped = grouped.having(last < before)
    rows = (await db.execute(grouped)).mappings().all()
    if not rows:
        conversations, next_before = merge_pages([], backups, limit)
        return JSONResponse({"conversations": conversations, "next_before": next_before}, headers=NO_CACHE)
    keys = [(row["harness"], row["key"]) for row in rows]
    page_key = conversation_key()
    # Bound both follow-up queries to the page's own time span so the same index serves them.
    in_page = (tuple_(UsageLog.harness, page_key).in_(keys), UsageLog.timestamp >= min(row["started_at"] for row in rows))
    models = (
        await db.execute(
            select(UsageLog.harness, page_key.label("key"), UsageLog.model, func.count().label("n"))
            .where(*_owned(identities, context), *in_page)
            .group_by(UsageLog.harness, page_key, UsageLog.model)
        )
    ).all()
    # One title per conversation — its earliest — chosen in SQL instead of shipping every row.
    ranked = (
        select(
            UsageLog.harness.label("harness"),
            page_key.label("key"),
            UsageLog.conversation_title.label("title"),
            func.row_number().over(partition_by=(UsageLog.harness, page_key), order_by=UsageLog.timestamp.asc()).label("rank"),
        )
        .where(*_owned(identities, context), *in_page, UsageLog.conversation_title.is_not(None))
        .subquery()
    )
    titles = (await db.execute(select(ranked.c.harness, ranked.c.key, ranked.c.title).where(ranked.c.rank == 1))).all()
    models_by: dict[tuple, list[tuple[str, int]]] = {}
    for h, k, model, n in models:
        models_by.setdefault((h, k), []).append((model, n))
    title_by: dict[tuple, str] = {(h, k): title for h, k, title in titles}
    conversations = []
    for row in rows:
        pair = (row["harness"], row["key"])
        untracked = str(row["key"]).startswith(UNTRACKED_PREFIX)
        conversations.append(
            {
                "harness": row["harness"],
                "conversation_id": row["key"],
                "untracked": untracked,
                "origin": row["origin"],
                "title": title_by.get(pair) or ("Untracked turns" if untracked else "Untitled conversation"),
                "started_at": _iso(row["started_at"]),
                "last_activity_at": _iso(row["last_activity_at"]),
                "turns": int(row["turns"]),
                "models": [m for m, _n in sorted(models_by.get(pair, []), key=lambda item: -item[1])],
                "cost_usd": float(row["cost_usd"] or 0),
                "source": SOURCE_GATEWAY,
            }
        )
    conversations, next_before = merge_pages(conversations, backups, limit)
    return JSONResponse({"conversations": conversations, "next_before": next_before}, headers=NO_CACHE)


@router.get("/{harness}/{conversation_id}/turns")
async def conversation_turns(
    harness: str,
    conversation_id: str,
    context: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    source: Annotated[str | None, Query(description="`backup` reads the Dev Box backup instead of the gateway's logs")] = None,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=BACKUP_TURNS_LIMIT)] = BACKUP_TURNS_LIMIT,
) -> JSONResponse:
    if harness not in KNOWN_CLIENT_TOOLS or not 1 <= len(conversation_id) <= 255 or source not in (None, SOURCE_GATEWAY, SOURCE_BACKUP):
        raise HTTPException(404, detail={"error": "conversation_not_found"})
    if source == SOURCE_BACKUP:
        return await backup_turns(await crew_owner(db, context), harness, conversation_id, offset, limit)
    identities = await owner_identities(db, context)
    rows = (
        await db.execute(
            select(
                UsageLog.request_id,
                UsageLog.timestamp,
                UsageLog.model,
                UsageLog.chat_log_s3_key,
                UsageLog.conversation_title,
                UsageLog.input_tokens,
                UsageLog.output_tokens,
                UsageLog.cost_usd,
            )
            .where(*_owned(identities, context), UsageLog.harness == harness, conversation_key() == conversation_id)
            .order_by(UsageLog.timestamp.desc())
            .limit(200)
        )
    ).all()
    if not rows:
        raise HTTPException(404, detail={"error": "conversation_not_found"})
    latest_logged = next((row for row in rows if row.chat_log_s3_key), None)
    turns: list[dict[str, Any]] = []
    content_available = False
    # Why the content is missing, so the page can say so instead of showing an empty drawer:
    #   not_logged — chat logging kept no object for these requests;
    #   not_retained — the log exists but the scrubber dropped the body (size limit) or the passthrough
    #                  did not map it (dev 2026-10-27: Claude Code logs hit scrub_limit, Codex logs had 0 messages);
    #   unreadable — the object could not be fetched.
    reason = "not_logged"
    if latest_logged is not None:
        log = await run_in_threadpool(read_chat_log, latest_logged.chat_log_s3_key)
        if log is None:
            reason = "unreadable"
        else:
            turns = turns_from_chat_log(log)
            content_available = bool(turns)
            reason = "ok" if content_available else "not_retained"
    if not content_available:
        # A request timeline: one entry per request, oldest first, with what the gateway did keep.
        turns = [
            {
                "role": "request",
                "time": _iso(row.timestamp),
                "text": row.conversation_title if index == 0 and row.conversation_title else "",
                "collapsed": False,
                "model": row.model,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "cost_usd": float(row.cost_usd or 0),
            }
            for index, row in enumerate(reversed(rows))
        ]
    return JSONResponse(
        {
            "turns": turns,
            "content_available": content_available,
            "content_reason": reason,
            "request_count": len(rows),
            "latest_request_id": rows[0].request_id,
        },
        headers=NO_CACHE,
    )


async def backup_turns(owner: CrewOwner | None, harness: str, conversation_id: str, offset: int, limit: int) -> JSONResponse:
    """Turns stored in the owner's Dev Box backup; Codex backups hold the owner's prompts only (``partial``)."""
    backup_source = BACKUP_SOURCE.get(harness)
    if owner is None or backup_source is None:
        raise HTTPException(404, detail={"error": "conversation_not_found"})
    unavailable = HTTPException(503, detail={"error": "conversation_unavailable"}, headers=NO_CACHE)
    try:
        found = await backup_store().conversation_for_owner(owner, backup_source, conversation_id)
    except Exception as error:  # noqa: BLE001 - store or database down
        logger.warning("conversation backup lookup failed", exc_info=True)
        raise unavailable from error
    if found is None:
        raise HTTPException(404, detail={"error": "conversation_not_found"})
    row, checkpoint = found
    body: dict[str, Any] = {"source": SOURCE_BACKUP, "request_count": 0, "latest_request_id": None, "has_more": False}
    if row.unreadable:
        return JSONResponse({**body, "turns": [], "content_available": False, "content_reason": "unreadable"}, headers=NO_CACHE)
    try:
        storage = backup_objects()
        turns, has_more = await run_in_threadpool(
            storage.conversation_turns, checkpoint, row.archive_path, backup_source, conversation_id, offset, limit
        )
    except (InvalidCheckpointObjectsError, ClientError, BotoCoreError) as error:
        raise unavailable from error
    reason = "partial" if _prompts_only(backup_source, row.archive_path) else ("ok" if turns else "unreadable")
    return JSONResponse({**body, "turns": turns, "has_more": has_more, "content_available": bool(turns), "content_reason": reason}, headers=NO_CACHE)


def read_chat_log(key: str) -> dict | None:
    """The scrubbed chat log the gateway wrote for one request, or None when unavailable."""
    settings = get_chat_logging_settings()
    if not settings.chat_logging_bucket or not key:
        return None
    try:
        client = boto3.client("s3")
        head = client.head_object(Bucket=settings.chat_logging_bucket, Key=key)
        if int(head.get("ContentLength") or 0) > CHAT_LOG_MAX_BYTES:
            return None
        body = client.get_object(Bucket=settings.chat_logging_bucket, Key=key)["Body"].read(CHAT_LOG_MAX_BYTES + 1)
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else None
    except Exception:  # noqa: BLE001 - a missing or unreadable log degrades to the fallback
        return None


def _block_text(content: Any) -> tuple[str, bool]:
    """(text, collapsed): text blocks joined; tool, image or reasoning blocks noted as collapsed."""
    if isinstance(content, str):
        return content, False
    if not isinstance(content, list):
        return "", False
    texts, collapsed = [], False
    for block in content:
        if isinstance(block, dict) and block.get("type") in ("text", "input_text", "output_text") and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif isinstance(block, dict):
            collapsed = True
    return "\n".join(texts), collapsed


def turns_from_chat_log(log: dict) -> list[dict[str, Any]]:
    request = log.get("request") if isinstance(log.get("request"), dict) else {}
    turns: list[dict[str, Any]] = []
    for message in request.get("messages") or []:
        if not isinstance(message, dict):
            continue
        text, collapsed = _block_text(message.get("content"))
        turns.append({"role": str(message.get("role", "user")), "time": None, "text": text[:TURN_TEXT_CHARS], "collapsed": collapsed})
    response = log.get("response") if isinstance(log.get("response"), dict) else None
    if response is not None:
        text, collapsed = _block_text(response.get("content"))
        if text or collapsed:
            turns.append({"role": "assistant", "time": _iso(_parse(log.get("timestamp"))), "text": text[:TURN_TEXT_CHARS], "collapsed": collapsed})
    return turns


def _parse(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value if isinstance(value, datetime) else None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()
