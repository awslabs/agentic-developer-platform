"""Disabled-by-default chat admission, workload exchange and scoped history access."""

import asyncio
import functools
import hashlib
import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache, partial
from ipaddress import IPv4Address, IPv4Network
from types import SimpleNamespace
from typing import Annotated, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from anyio.to_thread import run_sync
from boto3.dynamodb.types import TypeDeserializer
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from src.activity.chat_work import read_work
from src.activity.routes import get_activity_service
from src.agentauth.agentcore_authority import shared_owner_header
from src.agentauth.agentcore_governance import MemoryGovernanceConflictError, advance_owner_state, mutation_retry_epoch, owner_state
from src.agentauth.agentcore_management import AgentCoreManagement, MemoryManagementUnavailableError, MemoryRevisionConflictError
from src.agentauth.agentcore_migration import LegacyMemoryMigration
from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
from src.agentauth.chat_admission import PERSISTENT_LEASE_SECONDS, admit, renew_lease, root_identity
from src.agentauth.chat_artifact import (
    MAX_ARTIFACT_BYTES,
    ArtifactCreate,
    ArtifactId,
    ArtifactList,
    ChatArtifactConflictError,
    ChatArtifactInputError,
    ChatArtifactMissingError,
    ChatArtifactStore,
)
from src.agentauth.chat_authority import ChatRuntimeAuthority, chat_capabilities, current_chat_member, tenant_member_user
from src.agentauth.chat_capability import (
    ChatAuthorizationRefusedError,
    ChatAuthorizationUnavailableError,
    ChatCapabilityExpiredError,
    ChatCapabilityInvalidError,
    ChatModelPolicyUnavailableError,
    Identifier,
)
from src.agentauth.chat_crew_run import CrewRunConflictError, CrewRunStore, current_crew_store
from src.agentauth.chat_draft import ChatDraftConflictError, ChatDraftStore, DraftWrite
from src.agentauth.chat_followup_activation import activate_followup
from src.agentauth.chat_followup_admission import claim_followup, pending_mailbox_root, previous_delivery_complete
from src.agentauth.chat_history_compaction import ChatHistoryCompactor, HistoryCompaction
from src.agentauth.chat_history_store import ChatHistoryExpiredError, ChatHistoryStore
from src.agentauth.chat_history_summary import ChatSummaryWriter, SummaryAppend
from src.agentauth.chat_history_write import AssistantAppend, ChatHistoryConflictError, ChatHistoryWriter
from src.agentauth.chat_memory import ChatMemoryConflictError, ChatMemoryStore, MemoryId, MemorySearch, MemoryWrite
from src.agentauth.chat_platform_tools import ToolsCallRequest, ToolsListRequest, execute_platform_tool, owner_tools, tool_listing
from src.agentauth.chat_qualification import (
    ChatQualification,
    QualificationClaim,
    QualificationLookup,
    QualificationRequest,
    QualificationResult,
    QualificationUnavailableError,
)
from src.agentauth.chat_session_acl import AclWrite, ChatSessionAclConflictError, ChatSessionAclWriter
from src.agentauth.chat_session_mailbox import ChatSessionMailbox
from src.agentauth.chat_supervisor_resume import resume_supervisor_turn
from src.agentauth.chat_teardown import ChatSandboxRemovalUnconfirmedError, ChatTeardown
from src.agentauth.chat_terminal_publication import publish_terminal_delivery
from src.agentauth.chat_turn_completion import complete_delivered_turn
from src.agentauth.chat_turn_finalization import ChatTurnFinalizer
from src.agentauth.chat_turn_result import ChatTurnResultWriter, TurnResult
from src.agentauth.external_roots import root_bindings, root_store
from src.agentauth.installation_failure import read_installation_failure as load_installation_failure
from src.agentauth.installation_reader import authorize_installation_reader
from src.agentauth.installation_status import read_installation_status as load_installation_status
from src.agentauth.installation_status import unavailable_installation_status
from src.agentauth.store import AuthorityStoreError
from src.agentauth.work_routes import PROOF_HEADER, verify_producer
from src.agentauth.workload import WORKLOAD_HEADER, KubernetesWorkloadVerifier, WorkloadRefusedError, WorkloadUnavailableError
from src.auth.dependencies import get_current_user
from src.crew.targets import CrewLease
from src.orchestration import chat_history
from src.orchestration.intake_wiring import _get_context_table
from src.shared.database import get_db, get_session_factory
from src.shared.identity.workspaces import primary_team_for_workspace
from src.shared.models.crew_executor import CrewExecutor
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat-data"])


def clock():
    return int(time.time())


def enabled():
    if os.environ.get("ADP_CHAT_DATA_ENABLED") != "true":
        raise HTTPException(503, detail={"error": "chat_data_disabled"}, headers={"Cache-Control": "no-store"})


def crew_session_limit():
    configured = os.environ.get("ADP_MAX_SESSIONS_PER_CREW", "20")
    try:
        limit = int(configured)
    except ValueError:
        limit = 0
    if limit < 1 or limit > 20 or configured != str(limit):
        raise HTTPException(503, detail={"error": "crew_capacity_unavailable"}, headers={"Cache-Control": "no-store"})
    return limit


@lru_cache(maxsize=1)
def _runtime():
    table = _get_context_table()
    if table is None:
        raise ChatAuthorizationUnavailableError("chat context unconfigured")
    authority = ChatRuntimeAuthority(root_store(), table, KubernetesWorkloadVerifier.in_cluster(chat_sandbox=True), crew_workloads=crew_workloads)
    return authority, chat_capabilities(authority=authority, session_factory=get_session_factory())


def runtime():
    enabled()
    try:
        return _runtime()
    except Exception:
        raise HTTPException(503, detail={"error": "chat_authority_unavailable"}, headers={"Cache-Control": "no-store"}) from None


def contract_errors(function):
    @functools.wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except QualificationUnavailableError as error:
            raise HTTPException(503, detail={"error": str(error)}, headers={"Cache-Control": "no-store"}) from None
        # Before the generic history conflict (its subclass): the supervisor waits for the pod's
        # removal on this code and treats every other 409 as a plain retry (#8047).
        except ChatSandboxRemovalUnconfirmedError as error:
            _log_conflict(function, error)
            raise HTTPException(409, detail={"error": "chat_sandbox_removal_unconfirmed"}, headers={"Cache-Control": "no-store"}) from None
        except ChatHistoryConflictError as error:
            _log_conflict(function, error)
            raise HTTPException(409, detail={"error": "chat_history_conflict"}, headers={"Cache-Control": "no-store"}) from None
        except ChatMemoryConflictError:
            raise HTTPException(409, detail={"error": "chat_memory_conflict"}, headers={"Cache-Control": "no-store"}) from None
        except (MemoryRevisionConflictError, MemoryGovernanceConflictError):
            raise HTTPException(409, detail={"error": "memory_revision_conflict"}, headers={"Cache-Control": "no-store"}) from None
        except MemoryManagementUnavailableError:
            raise HTTPException(503, detail={"error": "agentcore_memory_unavailable"}, headers={"Cache-Control": "no-store"}) from None
        except ChatDraftConflictError:
            raise HTTPException(409, detail={"error": "chat_draft_conflict"}, headers={"Cache-Control": "no-store"}) from None
        except ChatArtifactConflictError:
            raise HTTPException(409, detail={"error": "chat_artifact_conflict"}, headers={"Cache-Control": "no-store"}) from None
        except ChatSessionAclConflictError:
            raise HTTPException(409, detail={"error": "chat_acl_conflict"}, headers={"Cache-Control": "no-store"}) from None
        except ChatArtifactInputError:
            raise HTTPException(422, detail={"error": "chat_artifact_invalid"}, headers={"Cache-Control": "no-store"}) from None
        except ChatArtifactMissingError:
            raise HTTPException(404, detail={"error": "chat_artifact_missing"}, headers={"Cache-Control": "no-store"}) from None
        except ChatHistoryExpiredError:
            raise HTTPException(410, detail={"error": "chat_history_expired"}, headers={"Cache-Control": "no-store"}) from None
        # A refused or unavailable model-policy decision is an expected policy outcome, not an
        # anonymous outage: the body names the reason so the sandbox can end the turn visibly.
        except ChatModelPolicyUnavailableError as error:
            raise HTTPException(503, detail=error.detail(), headers={"Cache-Control": "no-store"}) from None
        except (
            ChatAuthorizationUnavailableError,
            WorkloadUnavailableError,
            AuthorityStoreError,
            BotoCoreError,
            ClientError,
            SQLAlchemyError,
        ) as error:
            _log_unavailable(function, error)
            raise HTTPException(503, detail={"error": "chat_authority_unavailable"}, headers={"Cache-Control": "no-store"}) from None
        # Authentication failures are 401 so the sandbox knows to refresh or re-bootstrap;
        # a genuine capability that lacks authority for a resource stays a non-enumerating 404.
        except ChatCapabilityExpiredError:
            raise HTTPException(401, detail={"error": "capability_expired"}, headers={"Cache-Control": "no-store"}) from None
        except ChatCapabilityInvalidError:
            raise HTTPException(401, detail={"error": "capability_invalid"}, headers={"Cache-Control": "no-store"}) from None
        except (ChatAuthorizationRefusedError, BootstrapRefusedError, WorkloadRefusedError) as error:
            # The body stays a non-enumerating 404; the operator log names which check
            # refused (class + bounded message, never a token or payload) — #7990.
            logger.warning(
                "Chat data route refused",
                extra={"route": getattr(function, "__name__", "?"), "error_class": type(error).__name__, "error_detail": str(error)[:300]},
            )
            raise HTTPException(404, detail={"error": "chat_scope_refused"}, headers={"Cache-Control": "no-store"}) from None
        except ValidationError as error:
            # A stored record that no longer matches its contract is unavailable data, not a caller error.
            _log_unavailable(function, error)
            raise HTTPException(503, detail={"error": "chat_authority_unavailable"}, headers={"Cache-Control": "no-store"}) from None

    return wrapped


def _log_conflict(function, error: BaseException) -> None:
    """Name the reason behind an anonymous 409 in the gateway log (#8061).

    The supervisor retries ``chat_history_conflict`` for its whole reconciliation window, so a
    conflict the gateway raises on every attempt (for example "chat reserved pod exit not
    confirmed") used to be invisible without the API Gateway execution log. Like refusals
    (#8035), only the exception class and its bounded message are logged.
    """
    logger.warning(
        "Chat data route conflict",
        extra={"route": getattr(function, "__name__", "?"), "error_class": type(error).__name__, "error_detail": str(error)[:300]},
    )


def _log_unavailable(function, error: BaseException) -> None:
    """Name the cause of an anonymous 503 in the gateway log (#7983).

    The response body stays ``chat_authority_unavailable`` so callers learn nothing
    about the store, but the operator reading the gateway log sees which exception
    (for example a DynamoDB ``AccessDeniedException`` on a transaction item) made the
    route unavailable. Only the exception class and its bounded message are logged;
    tokens and payloads never reach this path.
    """
    logger.warning(
        "Chat data route unavailable",
        extra={"route": getattr(function, "__name__", "?"), "error_class": type(error).__name__, "error_detail": str(error)[:300]},
    )


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: Identifier
    envelope_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class SupervisorResumeRequest(ResumeRequest):
    """Resume may name the reserved pod the supervisor created but never admitted (#8061).

    Only the resume route accepts these; they stay off ``ResumeRequest`` so the body-bound
    producer proof of every other supervisor route is unchanged.
    """

    pod_name: str | None = Field(default=None, min_length=1, max_length=253)
    pod_uid: Identifier | None = None


class AdmissionRequest(ResumeRequest):
    pod_name: str = Field(min_length=1, max_length=253)
    pod_uid: Identifier


class CreationRequest(ResumeRequest):
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class ExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


Reference = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:-]+$")]


class HistoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: Identifier
    session_id: Identifier


class SessionStateRequest(HistoryRequest):
    model_config = ConfigDict(extra="forbid", strict=True)


class SessionNextRequest(SessionStateRequest):
    after: int = Field(strict=True, ge=0, le=99_999_999)


class HistoryPageRequest(HistoryRequest):
    limit: int = Field(default=100, strict=True, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)


class HistoryMessagesRequest(HistoryRequest):
    ids: list[Reference] = Field(max_length=100)


class HistorySummaryRequest(HistoryRequest):
    summary_id: Reference


class HistoryAppendRequest(HistoryRequest, AssistantAppend):
    pass


class TurnResultRequest(HistoryRequest, TurnResult):
    pass


class SummaryAppendRequest(HistoryRequest, SummaryAppend):
    pass


class HistoryCompactionRequest(HistoryRequest, HistoryCompaction):
    pass


class MemoryReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: Identifier
    memory_id: MemoryId


class MemoryWriteRequest(MemoryWrite):
    run_id: Identifier


class MemorySearchRequest(MemorySearch):
    run_id: Identifier


class AgentCoreRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class AgentCoreCorrectionRequest(AgentCoreRevisionRequest):
    text: str = Field(min_length=1, max_length=2000)


class DraftWriteRequest(HistoryRequest, DraftWrite):
    pass


class AclWriteRequest(HistoryRequest, AclWrite):
    pass


class InstallationReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: Identifier
    installation_id: int = Field(gt=0, lt=10**20)


class ActivityWorkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: Identifier
    from_time: str = Field(alias="from", min_length=1, max_length=40)
    to_time: str = Field(alias="to", min_length=1, max_length=40)
    timezone: str = Field(min_length=1, max_length=64)
    page_size: int = Field(default=20, strict=True, ge=1, le=20)
    last_key: str | None = Field(default=None, min_length=1, max_length=8192)


def activity_window(body: ActivityWorkRequest) -> tuple[str, str]:
    try:
        zone = ZoneInfo(body.timezone)
        start = datetime.fromisoformat(body.from_time.replace("Z", "+00:00"))
        end = datetime.fromisoformat(body.to_time.replace("Z", "+00:00"))
        for stamp in (start, end):
            if stamp.tzinfo is None:
                first = stamp.replace(tzinfo=zone, fold=0)
                second = stamp.replace(tzinfo=zone, fold=1)
                if first.utcoffset() != second.utcoffset() or first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != stamp:
                    raise ValueError("ambiguous or nonexistent local time")
        if start.tzinfo is None:
            start = start.replace(tzinfo=zone)
        if end.tzinfo is None:
            end = end.replace(tzinfo=zone)
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        if not start < end <= start + timedelta(days=31):
            raise ValueError("invalid window")
    except (ValueError, OverflowError, ZoneInfoNotFoundError):
        raise HTTPException(422, detail={"error": "activity_window_invalid"}) from None
    return start.isoformat(timespec="microseconds").replace("+00:00", "Z"), end.isoformat(timespec="microseconds").replace("+00:00", "Z")


def memory_table():
    name = os.environ.get("MEMORY_TABLE", "").strip()
    if not name:
        raise HTTPException(503, detail={"error": "chat_memory_unconfigured"}, headers={"Cache-Control": "no-store"})
    import boto3

    return boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1")).Table(name)


def artifact_storage():
    table, bucket = (os.environ.get(name, "").strip() for name in ("ARTIFACTS_TABLE", "ARTIFACTS_BUCKET"))
    if not table or not bucket:
        raise HTTPException(503, detail={"error": "chat_artifacts_unconfigured"}, headers={"Cache-Control": "no-store"})
    import boto3

    region = os.environ.get("AWS_REGION", "us-east-1")
    storage = boto3.client("s3", region_name=region, config=Config(connect_timeout=3, read_timeout=15, retries={"total_max_attempts": 1}))
    return boto3.resource("dynamodb", region_name=region).Table(table), storage, bucket


def bearer(request: Request) -> str:
    """Shared capability extraction for every delegated route; verification happens in the stores."""
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 4096:
        raise HTTPException(401, detail={"error": "capability_invalid"}, headers={"Cache-Control": "no-store"})
    return token


def bound_bearer(request: Request, services=Depends(runtime)) -> str:
    token = bearer(request)
    authority, capabilities = services
    try:
        pod = authority.verify_workload(request.headers.get(WORKLOAD_HEADER, ""))
        capabilities.verify_pod(token, pod, now=clock())
    except (ChatCapabilityExpiredError, ChatCapabilityInvalidError) as error:
        code = "capability_expired" if isinstance(error, ChatCapabilityExpiredError) else "capability_invalid"
        raise HTTPException(401, detail={"error": code}, headers={"Cache-Control": "no-store"}) from None
    except (ChatAuthorizationUnavailableError, WorkloadUnavailableError, AuthorityStoreError, BotoCoreError, ClientError, ValidationError):
        raise HTTPException(503, detail={"error": "chat_authority_unavailable"}, headers={"Cache-Control": "no-store"}) from None
    except (ChatAuthorizationRefusedError, WorkloadRefusedError):
        raise HTTPException(404, detail={"error": "chat_scope_refused"}, headers={"Cache-Control": "no-store"}) from None
    return token


Capability = Annotated[str, Depends(bound_bearer)]


@router.post("/chat/sessions/{session_id}/qualification", dependencies=[Depends(enabled)])
@contract_errors
async def request_qualification(
    session_id: str,
    body: QualificationRequest,
    user: Annotated[TokenContext, Depends(get_current_user)],
    table=Depends(chat_history.store),
    services=Depends(runtime),
    db: AsyncSession = Depends(get_db),
):
    await chat_history.owned_session(session_id, user, table, db)
    result = await run_in_threadpool(ChatQualification(*services).issue, user, session_id, body, now=clock())
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/chat/qualification", dependencies=[Depends(enabled)])
@contract_errors
async def qualification_readiness(evaluation_id: Identifier, user: Annotated[TokenContext, Depends(get_current_user)], services=Depends(runtime)):
    result = await run_in_threadpool(ChatQualification(*services).readiness, user, evaluation_id, now=clock())
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/qualification/fixtures", dependencies=[Depends(enabled)])
@contract_errors
async def qualification_fixtures(
    body: QualificationClaim, token: Capability, services=Depends(runtime), memory=Depends(memory_table), storage=Depends(artifact_storage)
):
    from src.agentauth.chat_qualification_fixtures import QualificationFixtures

    result = await run_in_threadpool(QualificationFixtures(ChatQualification(*services), memory, *storage).capture, token, body, now=clock())
    return JSONResponse(jsonable_encoder(result), headers={"Cache-Control": "no-store"})


@router.post("/chat/sessions/{session_id}/qualification/cleanup", dependencies=[Depends(enabled)])
@contract_errors
async def cleanup_qualification(
    session_id: Identifier,
    body: QualificationRequest,
    user: Annotated[TokenContext, Depends(get_current_user)],
    services=Depends(runtime),
    memory=Depends(memory_table),
    storage=Depends(artifact_storage),
    table=Depends(chat_history.store),
    db: AsyncSession = Depends(get_db),
):
    from src.agentauth.chat_qualification_fixtures import QualificationFixtures

    await chat_history.owned_session(session_id, user, table, db)
    result = await run_in_threadpool(
        QualificationFixtures(ChatQualification(*services), memory, *storage).cleanup, user, session_id, body, now=clock()
    )
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/chat/sessions/{session_id}/qualification/cancel", dependencies=[Depends(enabled)])
@contract_errors
async def cancel_qualification(
    session_id: str,
    body: QualificationRequest,
    user: Annotated[TokenContext, Depends(get_current_user)],
    table=Depends(chat_history.store),
    services=Depends(runtime),
    db: AsyncSession = Depends(get_db),
):
    await chat_history.owned_session(session_id, user, table, db)
    result = await run_in_threadpool(ChatQualification(*services).cancel, user, session_id, body, now=clock())
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/qualification/claim", dependencies=[Depends(enabled)])
@contract_errors
async def claim_qualification(body: QualificationClaim, token: Capability, services=Depends(runtime)):
    result = await run_in_threadpool(ChatQualification(*services).claim, token, body, now=clock())
    return JSONResponse(jsonable_encoder(result), headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/qualification/pending", dependencies=[Depends(enabled)])
@contract_errors
async def pending_qualification(body: QualificationLookup, token: Capability, services=Depends(runtime)):
    result = await run_in_threadpool(ChatQualification(*services).pending, token, body, now=clock())
    return JSONResponse(jsonable_encoder(result), headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/qualification/active", dependencies=[Depends(enabled)])
@contract_errors
async def active_qualification(body: QualificationClaim, token: Capability, services=Depends(runtime)):
    result = await run_in_threadpool(ChatQualification(*services).active, token, body, now=clock())
    return JSONResponse(jsonable_encoder(result), headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/qualification/result", dependencies=[Depends(enabled)])
@contract_errors
async def collect_qualification(body: QualificationResult, token: Capability, services=Depends(runtime)):
    result = await run_in_threadpool(ChatQualification(*services).collect, token, body, now=clock())
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/chat/sessions/{session_id}/qualification/result", dependencies=[Depends(enabled)])
@contract_errors
async def qualification_result(
    session_id: str,
    body: QualificationRequest,
    user: Annotated[TokenContext, Depends(get_current_user)],
    table=Depends(chat_history.store),
    services=Depends(runtime),
    db: AsyncSession = Depends(get_db),
):
    await chat_history.owned_session(session_id, user, table, db)
    result = await run_in_threadpool(ChatQualification(*services).result, user, session_id, body, now=clock())
    return JSONResponse(jsonable_encoder(result), headers={"Cache-Control": "no-store"})


async def _read_installation(request: Request, token: str, services, db: AsyncSession, operation: str):
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 2048:
            raise HTTPException(422, detail={"error": "installation_request_invalid"}, headers={"Cache-Control": "no-store"})
        raw.extend(chunk)
    try:
        body = InstallationReadRequest.model_validate(json.loads(raw))
    except (ValueError, TypeError, ValidationError):
        raise HTTPException(422, detail={"error": "installation_request_invalid"}, headers={"Cache-Control": "no-store"}) from None
    _, capabilities = services
    launch = await run_in_threadpool(capabilities.verify_run, token, run_id=body.run_id, operation=operation, now=clock())
    scope = await authorize_installation_reader(db, tenant_id=launch.tenant_id, user_id=launch.user_id, installation_id=body.installation_id)
    if operation == "installation.status":
        status = await load_installation_status(db, scope)
        failure = await load_installation_failure(db, scope)
        if failure["status"] == "partial":
            if status["status"] == "unavailable":
                status = unavailable_installation_status(scope.installation_id)
                status["status"] = "partial"
                status.pop("reason")
                status["components"] = []
            status["failure"] = failure["failure"]
        return JSONResponse(status, headers={"Cache-Control": "no-store"})
    if operation == "installation.failure":
        return JSONResponse(await load_installation_failure(db, scope), headers={"Cache-Control": "no-store"})
    return JSONResponse(
        {"status": "unavailable", "installation_id": scope.installation_id, "reason": "diagnostic_record_unavailable"},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/v1/chat/data/installation/status", dependencies=[Depends(enabled)])
@contract_errors
async def read_installation_status(request: Request, token: Capability, services=Depends(runtime), db: AsyncSession = Depends(get_db)):
    return await _read_installation(request, token, services, db, "installation.status")


@router.post("/v1/chat/data/installation/failure", dependencies=[Depends(enabled)])
@contract_errors
async def read_installation_failure(request: Request, token: Capability, services=Depends(runtime), db: AsyncSession = Depends(get_db)):
    return await _read_installation(request, token, services, db, "installation.failure")


@router.post("/v1/chat/data/activity/work", dependencies=[Depends(enabled)])
@contract_errors
async def read_activity_work(
    body: ActivityWorkRequest,
    request: Request,
    token: Capability,
    services=Depends(runtime),
    activity_service=Depends(get_activity_service),
):
    _, capabilities = services
    launch = await run_in_threadpool(capabilities.verify_run, token, run_id=body.run_id, operation="activity.read", now=clock())
    since, until = activity_window(body)
    result = await read_work(
        request, launch, activity_service, since=since, until=until, page_size=body.page_size, last_key=body.last_key, timezone=body.timezone
    )
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/tools/list", dependencies=[Depends(enabled)])
@contract_errors
async def list_turn_tools(body: ToolsListRequest, token: Capability, services=Depends(runtime), db: AsyncSession = Depends(get_db)):
    """The first-party tools this turn may call: catalogue ∩ tenant entitlement ∩ owner consent (#8113).

    Evaluated for the launch's tenant principal, never for a caller-named user. Tools the
    owner has not consented to are named as withheld so the assistant can point them at
    Settings → Tools; tools the tenant has not entitled are not revealed at all.
    """
    authority, capabilities = services
    launch = await run_in_threadpool(capabilities.verify_run, token, run_id=body.run_id, operation="tools.invoke", now=clock())
    tools = await owner_tools(db, authority.store, launch)
    return JSONResponse(tool_listing(tools), headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/tools/call", dependencies=[Depends(enabled)])
@contract_errors
async def call_turn_tool(
    body: ToolsCallRequest,
    token: Capability,
    services=Depends(runtime),
    db: AsyncSession = Depends(get_db),
    activity_service=Depends(get_activity_service),
):
    """Execute one offered first-party tool server-side under the turn capability (#8113).

    The offer is re-evaluated at call time (consent may have been withdrawn since the
    listing); a tool outside it is refused with its reason, which the sandbox surfaces to
    the model as a tool error rather than a failed turn.
    """
    authority, capabilities = services
    launch = await run_in_threadpool(capabilities.verify_run, token, run_id=body.run_id, operation="tools.invoke", now=clock())
    tools = await owner_tools(db, authority.store, launch)
    if body.tool not in tools.offered:
        reason = next((entry["reason"] for entry in tools.withheld if entry["name"] == body.tool), "tool_not_entitled")
        raise HTTPException(403, detail={"error": f"tool_{reason}" if reason == "not_consented" else reason}, headers={"Cache-Control": "no-store"})
    result = await execute_platform_tool(body.tool, body.arguments, db=db, launch=launch, owner_id=tools.owner_id, activity_service=activity_service)
    return JSONResponse({**result, "request_id": body.request_id}, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/history/read", dependencies=[Depends(enabled)])
@contract_errors
async def read_history(body: HistoryPageRequest, token: Capability, services=Depends(runtime)):
    authority, capabilities = services
    history = ChatHistoryStore(authority.context_table, capabilities)
    result = await run_in_threadpool(history.read_page, token, **body.model_dump(), now=clock())
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/session/state", dependencies=[Depends(enabled)])
@contract_errors
async def read_session_state(body: SessionStateRequest, token: Capability, services=Depends(runtime)):
    authority, capabilities = services
    launch = await run_in_threadpool(capabilities.verify, token, run_id=body.run_id, session_id=body.session_id, operation="turn.next", now=clock())
    state = await run_in_threadpool(
        ChatSessionMailbox(authority.context_table).state,
        session_id=launch.session_id,
        owner=(launch.tenant_id, launch.team_id, launch.user_id),
        now=clock(),
    )
    await run_in_threadpool(capabilities.verify, token, run_id=body.run_id, session_id=body.session_id, operation="turn.next", now=clock())
    return JSONResponse(state, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/session/next", dependencies=[Depends(enabled)])
@contract_errors
async def next_session_turn(body: SessionNextRequest, token: Capability, services=Depends(runtime)):
    authority, capabilities = services
    deadline = time.monotonic() + 20
    mailbox = ChatSessionMailbox(authority.context_table)
    while True:
        now = clock()
        launch = await run_in_threadpool(capabilities.verify, token, run_id=body.run_id, session_id=body.session_id, operation="turn.next", now=now)
        turn = await run_in_threadpool(
            mailbox.next_turn,
            session_id=launch.session_id,
            owner=(launch.tenant_id, launch.team_id, launch.user_id),
            run_id=launch.run_id,
            sandbox_uid=launch.sandbox_uid,
            generation=launch.lease_generation,
            after=body.after,
            now=now,
        )
        if turn is not None and not await run_in_threadpool(previous_delivery_complete, authority, launch):
            turn = None
        if turn is not None:
            await run_in_threadpool(pending_mailbox_root, authority, launch, turn, now)
        if turn is not None or time.monotonic() >= deadline:
            await run_in_threadpool(capabilities.verify, token, run_id=body.run_id, session_id=body.session_id, operation="turn.next", now=clock())
            if turn is not None:
                current = await run_in_threadpool(
                    mailbox.next_turn,
                    session_id=launch.session_id,
                    owner=(launch.tenant_id, launch.team_id, launch.user_id),
                    run_id=launch.run_id,
                    sandbox_uid=launch.sandbox_uid,
                    generation=launch.lease_generation,
                    after=body.after,
                    now=clock(),
                )
                if current != turn:
                    raise ChatAuthorizationRefusedError("chat follow-up mailbox changed")
                await run_in_threadpool(claim_followup, authority, launch, turn, body.after, clock())
            return JSONResponse(
                {"turn": turn, "run_id": launch.run_id, "session_id": launch.session_id, "lease_generation": launch.lease_generation},
                headers={"Cache-Control": "no-store"},
            )
        await asyncio.sleep(min(0.5, max(0, deadline - time.monotonic())))


@router.post("/v1/chat/data/history/messages", dependencies=[Depends(enabled)])
@contract_errors
async def read_messages(body: HistoryMessagesRequest, token: Capability, services=Depends(runtime)):
    authority, capabilities = services
    history = ChatHistoryStore(authority.context_table, capabilities)
    result = await run_in_threadpool(history.get_messages, token, **body.model_dump(), now=clock())
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/session/admit", dependencies=[Depends(enabled)])
@contract_errors
async def admit_session_turn(body: SessionNextRequest, request: Request, token: Capability, services=Depends(runtime)):
    authority, capabilities = services
    now = clock()
    launch = await run_in_threadpool(capabilities.verify, token, run_id=body.run_id, session_id=body.session_id, operation="turn.next", now=now)
    pod = await run_in_threadpool(authority.verify_workload, request.headers.get(WORKLOAD_HEADER, ""))
    following = await run_in_threadpool(activate_followup, authority, capabilities, launch, after=body.after, now=now)
    capability = await run_in_threadpool(capabilities.issue, following.run_id, pod, now=clock())
    return JSONResponse(
        {
            "capability": capability,
            "run_id": following.run_id,
            "session_id": following.session_id,
            "attempt": following.attempt,
            "lease_generation": following.lease_generation,
            "session_mode": "persistent",
            "expires_at": min(clock() + 300, following.expires_at),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/v1/chat/data/history/summary", dependencies=[Depends(enabled)])
@contract_errors
async def read_summary(body: HistorySummaryRequest, token: Capability, services=Depends(runtime)):
    authority, capabilities = services
    history = ChatHistoryStore(authority.context_table, capabilities)
    result = await run_in_threadpool(history.get_summary, token, **body.model_dump(), now=clock())
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/history/turn", dependencies=[Depends(enabled)])
@contract_errors
async def accepted_user_turn(body: HistoryRequest, token: Capability, services=Depends(runtime)):
    authority, capabilities = services
    writer = ChatHistoryWriter(authority, ChatHistoryStore(authority.context_table, capabilities))
    result = await run_in_threadpool(writer.accepted_turn, token, **body.model_dump(), now=clock())
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/history/append", dependencies=[Depends(enabled)])
@contract_errors
async def append_history(body: HistoryAppendRequest, token: Capability, services=Depends(runtime)):
    authority, capabilities = services
    writer = ChatHistoryWriter(authority, ChatHistoryStore(authority.context_table, capabilities))
    write = AssistantAppend.model_validate(body.model_dump(exclude={"run_id", "session_id"}))
    result = await run_in_threadpool(writer.append, token, run_id=body.run_id, session_id=body.session_id, write=write, now=clock())
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/turn/result", dependencies=[Depends(enabled)])
@contract_errors
async def record_turn_result(body: TurnResultRequest, token: Capability, services=Depends(runtime)):
    authority, capabilities = services
    writer = ChatTurnResultWriter(authority, ChatHistoryStore(authority.context_table, capabilities))
    result = TurnResult.model_validate(body.model_dump(exclude={"run_id", "session_id"}))
    receipt = await run_in_threadpool(writer.commit, token, run_id=body.run_id, session_id=body.session_id, result=result, now=clock())
    return JSONResponse(receipt, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/history/summary/append", dependencies=[Depends(enabled)])
@contract_errors
async def append_summary(body: SummaryAppendRequest, token: Capability, services=Depends(runtime)):
    authority, capabilities = services
    writer = ChatSummaryWriter(authority, ChatHistoryStore(authority.context_table, capabilities))
    write = SummaryAppend.model_validate(body.model_dump(exclude={"run_id", "session_id"}))
    result = await run_in_threadpool(writer.append_summary, token, run_id=body.run_id, session_id=body.session_id, write=write, now=clock())
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/history/compact", dependencies=[Depends(enabled)])
@contract_errors
async def compact_history(body: HistoryCompactionRequest, token: Capability, services=Depends(runtime)):
    authority, capabilities = services
    writer = ChatHistoryCompactor(authority, ChatHistoryStore(authority.context_table, capabilities))
    write = HistoryCompaction.model_validate(body.model_dump(exclude={"run_id", "session_id"}))
    result = await run_in_threadpool(writer.compact, token, run_id=body.run_id, session_id=body.session_id, write=write, now=clock())
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


async def _agentcore_management(session_id, user, table, services, db):
    await chat_history.owned_session(session_id, user, table, db)
    authority, _ = services
    owner = type("MemoryOwner", (), {"tenant_id": user.org_id, "team_id": user.team_id or "", "user_id": user.user_id, "session_id": session_id})()
    await run_in_threadpool(shared_owner_header, authority.context_table, owner)
    from src.agentauth.chat_model import agentcore_recall

    provider = await run_in_threadpool(agentcore_recall)
    if provider is None:
        raise MemoryManagementUnavailableError("memory target unconfigured")
    return AgentCoreManagement(provider), provider.owner_namespace(owner)


@router.get("/chat/sessions/{session_id}/agentcore-memory/migration", dependencies=[Depends(enabled)])
@contract_errors
async def inspect_legacy_memory_migration(
    session_id: str,
    limit: int = Query(default=25, ge=1, le=25),
    cursor: str | None = None,
    user: TokenContext = Depends(get_current_user),
    table=Depends(chat_history.store),
    services=Depends(runtime),
    memory=Depends(memory_table),
    db: AsyncSession = Depends(get_db),
):
    manager, _ = await _agentcore_management(session_id, user, table, services, db)
    owner = SimpleNamespace(tenant_id=user.org_id, team_id=user.team_id or "", user_id=user.user_id, session_id=session_id)
    report = await run_in_threadpool(
        LegacyMemoryMigration(memory, services[0].context_table, manager.provider, services[1]).inventory,
        owner,
        now=clock(),
        limit=limit,
        cursor=cursor,
    )
    return JSONResponse(
        {"counts": report["counts"], "next_cursor": report["next_cursor"], "status": "dry_run"}, headers={"Cache-Control": "no-store"}
    )


def _advance_owner_memory_epoch(table, user, session_id):
    try:
        table.update_item(
            Key={"PK": f"session#{session_id}", "SK": "header"},
            UpdateExpression="ADD memoryEpoch :one",
            ConditionExpression=(
                "tenantId = :tenant AND teamId = :team AND ownerUserId = :user AND memoryPrivacy = :shared AND memoryConsent = :allowed"
            ),
            ExpressionAttributeValues={
                ":one": 1,
                ":tenant": user.org_id,
                ":team": user.team_id or "",
                ":user": user.user_id,
                ":shared": "shared",
                ":allowed": True,
            },
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ChatAuthorizationRefusedError("memory scope refused") from None
        raise


def _governed_mutation(table, user, session_id, kind, record_id, revision, text=None):
    owner = SimpleNamespace(tenant_id=user.org_id, team_id=user.team_id or "", user_id=user.user_id, session_id=session_id)
    shared_owner_header(table, owner)
    prior = owner_state(table, owner)["fences"].get(record_id)
    retry_epoch = mutation_retry_epoch(prior, record_id, revision, kind, text)
    if retry_epoch is not None:
        return retry_epoch
    _advance_owner_memory_epoch(table, user, session_id)
    return advance_owner_state(table, owner, record_id, revision, kind, text)


@router.get("/chat/sessions/{session_id}/agentcore-memory/captures/{receipt_id}", dependencies=[Depends(enabled)])
@contract_errors
async def agentcore_capture_status(
    session_id: Identifier,
    receipt_id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
    user: Annotated[TokenContext, Depends(get_current_user)],
    table=Depends(chat_history.store),
    services=Depends(runtime),
    db: AsyncSession = Depends(get_db),
):
    await chat_history.owned_session(session_id, user, table, db)
    authority, _ = services
    owner = SimpleNamespace(tenant_id=user.org_id, team_id=user.team_id or "", user_id=user.user_id, session_id=session_id)
    await run_in_threadpool(shared_owner_header, authority.context_table, owner)
    capture = await run_in_threadpool(
        lambda: authority.context_table.get_item(Key={"PK": f"memory-capture#{receipt_id}", "SK": "capture"}, ConsistentRead=True).get("Item")
    )
    if not capture or any(
        capture.get(field) != expected
        for field, expected in (
            ("receiptId", receipt_id),
            ("tenantId", user.org_id),
            ("teamId", user.team_id or ""),
            ("ownerUserId", user.user_id),
            ("sessionId", session_id),
        )
    ):
        raise ChatAuthorizationRefusedError("memory receipt refused")
    capture_epoch = capture.get("ownerEpoch")
    if isinstance(capture_epoch, bool) or not isinstance(capture_epoch, int | Decimal) or capture_epoch < 0 or int(capture_epoch) != capture_epoch:
        raise MemoryManagementUnavailableError("memory capture epoch unavailable")
    capture_epoch = int(capture_epoch)
    await chat_history.owned_session(session_id, user, table, db)
    await run_in_threadpool(shared_owner_header, authority.context_table, owner)
    state = capture.get("status")
    if state == "submitted" and (not isinstance(capture.get("serviceEventId"), str) or not capture["serviceEventId"]):
        raise MemoryManagementUnavailableError("memory service receipt unavailable")
    if state not in ("pending", "processing", "submitted", "failed"):
        raise MemoryManagementUnavailableError("memory receipt unavailable")
    phase = {"pending": "captured", "processing": "processing", "submitted": "event_accepted", "failed": "failed"}[state]
    result_status = "failed" if state == "failed" else "pending"
    coverage = "unverified"
    record_id = None
    if state == "submitted":
        from src.agentauth.chat_model import agentcore_recall

        try:
            provider = await asyncio.wait_for(run_sync(agentcore_recall, abandon_on_cancel=True), timeout=0.35)
            if provider is None:
                raise MemoryManagementUnavailableError("memory target unconfigured")
            record_id = await asyncio.wait_for(
                run_sync(
                    partial(AgentCoreManagement(provider).visible_receipt, provider.owner_namespace(owner), receipt_id, epoch=capture_epoch),
                    abandon_on_cancel=True,
                ),
                timeout=0.75,
            )
        except (TimeoutError, MemoryManagementUnavailableError, ChatAuthorizationUnavailableError):
            result_status, coverage = "unavailable", "unavailable"
        await chat_history.owned_session(session_id, user, table, db)
        current = await run_in_threadpool(shared_owner_header, authority.context_table, owner)
        if current.get("memoryEpoch", 0) != capture.get("memoryEpoch", 0) or owner_state(authority.context_table, owner)["epoch"] != capture_epoch:
            record_id = None
            phase, coverage = "fenced", "unverified"
        elif record_id:
            result_status, coverage, phase = "available", "verified", "available_for_recall"
    return JSONResponse(
        {
            "receipt_id": receipt_id,
            "status": result_status,
            "phase": phase,
            "service_event_id": capture.get("serviceEventId") if state == "submitted" else None,
            "record_id": record_id,
            "coverage": coverage,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/chat/sessions/{session_id}/agentcore-memory", dependencies=[Depends(enabled)])
@contract_errors
async def inspect_agentcore_memory(
    session_id: Identifier,
    user: Annotated[TokenContext, Depends(get_current_user)],
    table=Depends(chat_history.store),
    services=Depends(runtime),
    db: AsyncSession = Depends(get_db),
):
    manager, namespace = await _agentcore_management(session_id, user, table, services, db)
    owner = SimpleNamespace(tenant_id=user.org_id, user_id=user.user_id)
    result = await run_in_threadpool(manager.inspect, namespace, governance=partial(owner_state, services[0].context_table, owner))
    await _agentcore_management(session_id, user, table, services, db)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/chat/sessions/{session_id}/agentcore-memory/{record_id}/operations/{receipt_id}", dependencies=[Depends(enabled)])
@contract_errors
async def agentcore_memory_operation_status(
    session_id: Identifier,
    record_id: Identifier,
    receipt_id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
    user: Annotated[TokenContext, Depends(get_current_user)],
    table=Depends(chat_history.store),
    services=Depends(runtime),
    db: AsyncSession = Depends(get_db),
):
    manager, namespace = await _agentcore_management(session_id, user, table, services, db)
    owner = SimpleNamespace(tenant_id=user.org_id, team_id=user.team_id or "", user_id=user.user_id, session_id=session_id)
    state = await run_in_threadpool(owner_state, services[0].context_table, owner)
    fence = state["fences"].get(record_id)
    if not fence or receipt_id != manager.operation_receipt(namespace, record_id, fence["revision"], fence["kind"], fence.get("text")):
        raise ChatAuthorizationRefusedError("memory receipt refused")
    try:
        result = await asyncio.wait_for(
            run_sync(partial(manager.operation_status, namespace, record_id, receipt_id, fence), abandon_on_cancel=True), timeout=0.75
        )
    except (TimeoutError, MemoryManagementUnavailableError):
        result = {"status": "unavailable", "phase": "service_unavailable", "coverage": "unavailable", "service_record_id": None}
    await _agentcore_management(session_id, user, table, services, db)
    if (await run_in_threadpool(owner_state, services[0].context_table, owner))["epoch"] != state["epoch"]:
        result = {"status": "pending", "phase": "superseded", "coverage": "unverified", "service_record_id": None}
    return JSONResponse({"receipt_id": receipt_id, **result}, headers={"Cache-Control": "no-store"})


@router.get("/chat/sessions/{session_id}/agentcore-memory/export", dependencies=[Depends(enabled)])
@contract_errors
async def export_agentcore_memory(
    session_id: Identifier,
    user: Annotated[TokenContext, Depends(get_current_user)],
    table=Depends(chat_history.store),
    services=Depends(runtime),
    db: AsyncSession = Depends(get_db),
):
    manager, namespace = await _agentcore_management(session_id, user, table, services, db)
    owner = SimpleNamespace(tenant_id=user.org_id, user_id=user.user_id)
    result = await run_in_threadpool(manager.inspect, namespace, export=True, governance=partial(owner_state, services[0].context_table, owner))
    await _agentcore_management(session_id, user, table, services, db)
    return JSONResponse({**result, "format": "agentcore-memory-v1"}, headers={"Cache-Control": "no-store"})


@router.post("/chat/sessions/{session_id}/agentcore-memory/{record_id}/correct", dependencies=[Depends(enabled)])
@contract_errors
async def correct_agentcore_memory(
    session_id: Identifier,
    record_id: Identifier,
    body: AgentCoreCorrectionRequest,
    user: Annotated[TokenContext, Depends(get_current_user)],
    table=Depends(chat_history.store),
    services=Depends(runtime),
    db: AsyncSession = Depends(get_db),
):
    manager, namespace = await _agentcore_management(session_id, user, table, services, db)
    owner = SimpleNamespace(tenant_id=user.org_id, user_id=user.user_id)
    result = await run_in_threadpool(
        manager.correct,
        namespace,
        session_id,
        record_id,
        body.revision,
        body.text,
        fence=partial(_governed_mutation, services[0].context_table, user, session_id),
        governance=partial(owner_state, services[0].context_table, owner),
    )
    await _agentcore_management(session_id, user, table, services, db)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.delete("/chat/sessions/{session_id}/agentcore-memory/{record_id}", dependencies=[Depends(enabled)])
@contract_errors
async def forget_agentcore_memory(
    session_id: Identifier,
    record_id: Identifier,
    body: AgentCoreRevisionRequest,
    user: Annotated[TokenContext, Depends(get_current_user)],
    table=Depends(chat_history.store),
    services=Depends(runtime),
    db: AsyncSession = Depends(get_db),
):
    manager, namespace = await _agentcore_management(session_id, user, table, services, db)
    owner = SimpleNamespace(tenant_id=user.org_id, user_id=user.user_id)
    result = await run_in_threadpool(
        manager.forget,
        namespace,
        record_id,
        body.revision,
        fence=partial(_governed_mutation, services[0].context_table, user, session_id),
        governance=partial(owner_state, services[0].context_table, owner),
    )
    await _agentcore_management(session_id, user, table, services, db)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/memory/read", dependencies=[Depends(enabled)])
@contract_errors
async def read_memory(body: MemoryReadRequest, token: Capability, services=Depends(runtime), table=Depends(memory_table)):
    authority, capabilities = services
    memory = ChatMemoryStore(table, authority, capabilities)
    result = await run_in_threadpool(memory.read, token, **body.model_dump(), now=clock())
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/memory/search", dependencies=[Depends(enabled)])
@contract_errors
async def search_memory(body: MemorySearchRequest, token: Capability, services=Depends(runtime), table=Depends(memory_table)):
    authority, capabilities = services
    memory = ChatMemoryStore(table, authority, capabilities)
    search = MemorySearch.model_validate(body.model_dump(exclude={"run_id"}))
    result = await run_in_threadpool(memory.search, token, run_id=body.run_id, search=search, now=clock())
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/memory/write", dependencies=[Depends(enabled)])
@contract_errors
async def write_memory(body: MemoryWriteRequest, token: Capability, services=Depends(runtime), table=Depends(memory_table)):
    authority, capabilities = services
    memory = ChatMemoryStore(table, authority, capabilities)
    write = MemoryWrite.model_validate(body.model_dump(exclude={"run_id"}))
    result = await run_in_threadpool(memory.write, token, run_id=body.run_id, write=write, now=clock())
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/draft/read", dependencies=[Depends(enabled)])
@contract_errors
async def read_draft(body: HistoryRequest, token: Capability, services=Depends(runtime)):
    authority, capabilities = services
    drafts = ChatDraftStore(authority, capabilities)
    result = await run_in_threadpool(drafts.read, token, **body.model_dump(), now=clock())
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/draft/write", dependencies=[Depends(enabled)])
@contract_errors
async def write_draft(body: DraftWriteRequest, token: Capability, services=Depends(runtime)):
    authority, capabilities = services
    drafts = ChatDraftStore(authority, capabilities)
    write = DraftWrite.model_validate(body.model_dump(by_alias=True, exclude_none=True, exclude={"run_id", "session_id"}))
    result = await run_in_threadpool(drafts.write, token, run_id=body.run_id, session_id=body.session_id, write=write, now=clock())
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/session/acl/read", dependencies=[Depends(enabled)])
@contract_errors
async def read_session_acl(body: HistoryRequest, token: Capability, services=Depends(runtime)):
    authority, capabilities = services
    acl = ChatSessionAclWriter(authority, ChatHistoryStore(authority.context_table, capabilities))
    result = await run_in_threadpool(acl.read, token, **body.model_dump(), now=clock())
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/session/acl/write", dependencies=[Depends(enabled)])
@contract_errors
async def write_session_acl(body: AclWriteRequest, token: Capability, services=Depends(runtime)):
    authority, capabilities = services
    acl = ChatSessionAclWriter(authority, ChatHistoryStore(authority.context_table, capabilities))
    write = AclWrite.model_validate(body.model_dump(exclude={"run_id", "session_id"}))
    result = await run_in_threadpool(acl.write, token, run_id=body.run_id, session_id=body.session_id, write=write, now=clock())
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/artifact/list", dependencies=[Depends(enabled)])
@contract_errors
async def list_artifacts(body: ArtifactList, token: Capability, services=Depends(runtime), storage=Depends(artifact_storage)):
    authority, capabilities = services
    artifacts = ChatArtifactStore(authority, capabilities, *storage, clock)
    result = await run_in_threadpool(artifacts.list_page, token, body)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/artifact/create", dependencies=[Depends(enabled)])
@contract_errors
async def create_artifact(request: Request, token: Capability, services=Depends(runtime), storage=Depends(artifact_storage)):
    if request.headers.get("content-encoding") or request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise ChatArtifactInputError("artifact upload requires uncompressed JSON")
    limit = 4 * ((MAX_ARTIFACT_BYTES + 2) // 3) + 8192
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, detail={"error": "chat_artifact_too_large"}, headers={"Cache-Control": "no-store"})
    try:
        async with asyncio.timeout(30):
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > limit:
                    raise HTTPException(413, detail={"error": "chat_artifact_too_large"}, headers={"Cache-Control": "no-store"})
            try:
                upload = ArtifactCreate.model_validate_json(body)
            except ValidationError:
                raise ChatArtifactInputError("artifact upload schema invalid") from None
            authority, capabilities = services
            artifacts = ChatArtifactStore(authority, capabilities, *storage, clock)
            result = await run_in_threadpool(artifacts.create, token, upload)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except TimeoutError:
        raise HTTPException(503, detail={"error": "chat_artifact_timeout"}, headers={"Cache-Control": "no-store"}) from None


@router.get("/v1/chat/data/artifact/{session_id}/{artifact_id}", dependencies=[Depends(enabled)])
@contract_errors
async def download_artifact(
    session_id: Identifier,
    artifact_id: ArtifactId,
    run_id: Identifier,
    token: Capability,
    services=Depends(runtime),
    storage=Depends(artifact_storage),
):
    authority, capabilities = services
    artifacts = ChatArtifactStore(authority, capabilities, *storage, clock)
    content, reference = await run_in_threadpool(artifacts.download, token, run_id=run_id, session_id=session_id, artifact_id=artifact_id)
    return Response(
        content,
        media_type=reference["contentType"],
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(reference["filename"], safe=""),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            "X-Artifact-Scan-Status": reference["scanStatus"],
        },
    )


class CrewRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: Identifier
    target_kind: Literal["eks-pod", "ec2-instance"]
    target_ref: Identifier
    executor_scope: Literal["session", "user"] = "session"
    run_id: Identifier | None = None
    envelope_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None


class CrewActivationRequest(AdmissionRequest):
    model_config = ConfigDict(extra="forbid", strict=True)

    crew_run_id: Identifier
    lease_generation: int = Field(ge=1)


def crew_run_store():
    return current_crew_store()


@lru_cache(maxsize=1)
def crew_workloads():
    return KubernetesWorkloadVerifier.in_cluster(chat_crew=True)


@router.post("/internal/v1/agent/chat/crew-run", dependencies=[Depends(enabled)])
@contract_errors
async def mint_crew_run(
    body: CrewRunRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    store: CrewRunStore = Depends(crew_run_store),
    services=Depends(runtime),
):
    bindings = [binding for binding in root_bindings() if binding.source == "chat" and binding.chat_supervisor_role]
    role = await verify_producer(
        request.headers.get(PROOF_HEADER, ""),
        envelope_digest(body.model_dump(exclude_none=True, exclude_unset=True)),
        allowed_roles={binding.chat_supervisor_role for binding in bindings},
    )
    context_table = _get_context_table()
    if context_table is None:
        raise HTTPException(503, detail={"error": "chat_authority_unavailable"})
    header = await run_in_threadpool(
        lambda: context_table.get_item(Key={"PK": f"session#{body.session_id}", "SK": "header"}, ConsistentRead=True).get("Item")
    )
    now = clock()
    if (
        not header
        or header.get("status") != "active"
        or header.get("sessionMode") != "persistent"
        or header.get("sessionState") in {"ending", "ended", "recovering"}
        or not isinstance(header.get("tenantId"), str)
        or not isinstance(header.get("ownerUserId"), str)
        or isinstance(header.get("ttl"), bool)
        or not isinstance(header.get("ttl"), int | Decimal)
        or header["ttl"] <= now
    ):
        raise HTTPException(409, detail={"error": "chat_lease_unavailable"})
    tenant_id, user_id = header["tenantId"], header["ownerUserId"]
    if role not in {binding.chat_supervisor_role for binding in bindings if binding.tenant_id == tenant_id}:
        raise HTTPException(403, "forbidden")
    user = await tenant_member_user(db, tenant_id, user_id)
    if user is None:
        raise HTTPException(404, detail={"error": "chat_member_unavailable"})
    previous = header.get("chatLease")
    previous_generation = previous.get("generation") if isinstance(previous, dict) else 0
    if (
        isinstance(previous_generation, bool)
        or not isinstance(previous_generation, int | Decimal)
        or int(previous_generation) != previous_generation
        or previous_generation < 0
    ):
        raise HTTPException(409, detail={"error": "chat_lease_unavailable"})
    generation = int(previous_generation) + 1
    inventory = None
    if body.executor_scope == "user":
        inventory = await db.scalar(
            select(CrewExecutor)
            .where(CrewExecutor.tenant_id == tenant_id, CrewExecutor.user_id == user_id, CrewExecutor.state.in_(("ready", "provisioning")))
            .with_for_update()
        )
        if (
            inventory is None
            or not isinstance(inventory.owner_key, str)
            or len(inventory.owner_key) != 52
            or any(character not in "0123456789abcdef" for character in inventory.owner_key)
            or inventory.target_kind != body.target_kind
        ):
            raise HTTPException(409, detail={"error": "crew_target_unavailable"})
        if inventory.state == "provisioning":
            if (
                body.target_kind != "eks-pod"
                or body.run_id is None
                or inventory.generation != 1
                or inventory.latest_verified_checkpoint_id is not None
                or inventory.target_ref is not None
                or inventory.lease_generation is not None
                or inventory.lease_expires_at is not None
            ):
                raise HTTPException(409, detail={"error": "crew_target_unavailable"})
        elif (
            (body.target_kind != "eks-pod" and inventory.target_ref != body.target_ref)
            or inventory.lease_generation != inventory.generation
            or inventory.lease_expires_at is None
            or inventory.lease_expires_at <= now
        ):
            raise HTTPException(409, detail={"error": "crew_target_unavailable"})
        if inventory.state == "ready" and body.target_kind == "eks-pod":
            owner_target_ref = body.target_ref
            if body.run_id is not None:
                owner = await run_in_threadpool(
                    lambda: store.table.get_item(
                        Key={"event_id": f"crew-owner#{tenant_id}#{user_id}", "arrived_at": "crew"},
                        ConsistentRead=True,
                    ).get("Item")
                )
                if (
                    not owner
                    or owner.get("generation") != inventory.generation
                    or owner.get("executor_scope") != "user"
                    or owner.get("target_kind") != "eks-pod"
                    or owner.get("pod_uid") != inventory.target_ref
                    or not isinstance(owner.get("target_ref"), str)
                ):
                    raise HTTPException(409, detail={"error": "crew_target_unavailable"})
                owner_target_ref = owner["target_ref"]
            pod = await run_in_threadpool(
                crew_workloads().verify_bound,
                name=owner_target_ref,
                uid=inventory.target_ref,
            )
            if pod.owner_key != inventory.owner_key or pod.name != owner_target_ref or pod.uid != inventory.target_ref:
                raise HTTPException(409, detail={"error": "crew_target_unavailable"})
        generation = inventory.generation
    owner_lease_check = None
    owner_lease_expiry = None
    if inventory is not None and inventory.state == "ready":
        context_table = _get_context_table()
        if context_table is None:
            raise HTTPException(503, detail={"error": "chat_authority_unavailable"})
        lease_row = await run_in_threadpool(
            lambda: context_table.get_item(Key={"PK": f"crew#{tenant_id}#{user_id}", "SK": "executor"}, ConsistentRead=True).get("Item")
        )
        if not lease_row or lease_row.get("tenantId") != tenant_id or lease_row.get("ownerUserId") != user_id:
            raise HTTPException(409, detail={"error": "crew_lease_changed"})
        try:
            lease = CrewLease.model_validate(lease_row.get("crewLease"))
        except ValidationError:
            raise HTTPException(409, detail={"error": "crew_lease_changed"}) from None
        if (
            lease.tenant_id != tenant_id
            or lease.user_id != user_id
            or lease.target_kind != inventory.target_kind
            or lease.target_ref != inventory.target_ref
            or lease.generation != generation
            or lease.expires_at <= now
            or lease_row.get("crewLeaseState", "ready") != "ready"
        ):
            raise HTTPException(409, detail={"error": "crew_lease_changed"})
        owner_lease_expiry = lease.expires_at
        owner_lease_check = {
            "TableName": context_table.name,
            "Key": {"PK": lease_row["PK"], "SK": "executor"},
            "ConditionExpression": (
                "tenantId = :tenant AND ownerUserId = :user AND #lease = :lease AND #lease.expires_at > :now "
                "AND (attribute_not_exists(#state) OR #state = :ready)"
            ),
            "ExpressionAttributeNames": {"#lease": "crewLease", "#state": "crewLeaseState"},
            "ExpressionAttributeValues": {
                ":tenant": tenant_id,
                ":user": user_id,
                ":lease": lease_row["crewLease"],
                ":now": now,
                ":ready": "ready",
            },
        }
    if generation < 1:
        raise HTTPException(409, detail={"error": "chat_lease_unavailable"})
    lease_expires_at = min(now + PERSISTENT_LEASE_SECONDS, int(header["ttl"]))
    if inventory is not None and inventory.state == "ready":
        lease_expires_at = min(lease_expires_at, inventory.lease_expires_at, owner_lease_expiry)
    creation_update = None
    if (body.run_id is None) != (body.envelope_digest is None):
        raise ChatAuthorizationRefusedError("crew creation identity required")
    if body.run_id is not None:
        authority, capabilities = services
        resumed = await run_in_threadpool(resume_supervisor_turn, authority, capabilities, body, role, bindings, now=now)
        recovering_user = inventory is not None and resumed.get("crew_run_id")
        if (
            resumed.get("state") != "creation_reserved"
            or resumed.get("session_id") != body.session_id
            or resumed.get("pod_name") != body.target_ref
            or body.target_kind != "eks-pod"
            or (not recovering_user and (resumed.get("sandbox_uid") or resumed.get("crew_run_id")))
        ):
            raise ChatHistoryConflictError("crew creation already bound or changed")
        creation = await run_in_threadpool(authority.store._read, f"CHAT-LAUNCH#{body.run_id}", "CREATION")
        if not creation or any(
            creation["binding"]["M"].get(field) != {"S": value}
            for field, value in (
                ("tenant_id", tenant_id),
                ("user_id", user_id),
                ("session_id", body.session_id),
                ("pod_name", body.target_ref),
            )
        ):
            raise ChatAuthorizationRefusedError("crew creation owner changed")
        if recovering_user:
            existing_run = creation["crew_run_id"]["S"]
            row = await run_in_threadpool(store.read_run, existing_run)
            owner = await run_in_threadpool(
                lambda: store.table.get_item(Key={"event_id": f"crew-owner#{tenant_id}#{user_id}", "arrived_at": "crew"}, ConsistentRead=True).get(
                    "Item"
                )
            )
            if (
                not row
                or row.get("kind") != "crew"
                or row.get("executor_scope") != "user"
                or row.get("status") not in {"provisioning", "active"}
                or row.get("tenant_id") != tenant_id
                or row.get("user_id") != user_id
                or row.get("lease_generation") != generation
                or row.get("lease_expires_at", 0) <= now
                or creation.get("crew_lease_generation") != {"N": str(generation)}
                or not owner
                or owner.get("run_id") != existing_run
                or owner.get("generation") != generation
                or owner.get("executor_scope") != "user"
                or owner.get("target_kind") != "eks-pod"
                or owner.get("target_ref") != row.get("target_ref")
                or owner.get("pod_uid") != row.get("pod_uid")
                or owner.get("lease_expires_at", 0) <= now
            ):
                raise ChatHistoryConflictError("crew recovery binding changed")
            receipt = {"run_id": existing_run, "lease_generation": generation, "owner_key": inventory.owner_key}
            if inventory.state == "ready":
                if row.get("status") != "active" or row.get("pod_uid") != pod.uid or row.get("target_ref") != pod.name:
                    raise ChatHistoryConflictError("crew recovery pod changed")
                receipt.update(pod_name=pod.name, pod_uid=pod.uid)
            else:
                if row.get("session_id") != body.session_id or row.get("target_ref") != body.target_ref:
                    raise ChatHistoryConflictError("crew recovery owner changed")
                if resumed.get("sandbox_uid"):
                    recovered_pod = await run_in_threadpool(crew_workloads().verify_bound, name=body.target_ref, uid=resumed["sandbox_uid"])
                    if recovered_pod.owner_key != inventory.owner_key:
                        raise ChatHistoryConflictError("crew recovery pod changed")
                if row.get("status") == "active":
                    if not resumed.get("sandbox_uid") or row.get("pod_uid") != resumed["sandbox_uid"]:
                        raise ChatHistoryConflictError("crew recovery activation changed")
                    receipt["activated"] = True
                receipt["state"] = "provisioning"
            return JSONResponse(receipt, status_code=201, headers={"Cache-Control": "no-store"})
        creation_update = ChatTeardown(authority, capabilities.launches)._unchanged(creation)
        creation_update["ConditionExpression"] += " AND attribute_not_exists(crew_run_id)"
        for field in ("Key", "ExpressionAttributeValues"):
            creation_update[field] = TypeDeserializer().deserialize({"M": creation_update[field]})
    try:
        if inventory is not None and inventory.state == "ready":
            run_id = await run_in_threadpool(
                store.attach_session,
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=body.session_id,
                lease_generation=generation,
                target_kind=body.target_kind,
                target_ref=owner_target_ref if body.run_id is not None and body.target_kind == "eks-pod" else body.target_ref,
                now=now,
                creation_update=creation_update,
                owner_lease_check=owner_lease_check,
                pod_uid=pod.uid if body.target_kind == "eks-pod" else None,
                max_sessions=crew_session_limit(),
            )
        else:
            run_id = await run_in_threadpool(
                store.mint,
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=body.session_id,
                lease_generation=generation,
                lease_expires_at=lease_expires_at,
                target_kind=body.target_kind,
                target_ref=body.target_ref,
                executor_scope=body.executor_scope,
                now=now,
                creation_update=creation_update,
                provisioning=inventory is not None and inventory.state == "provisioning",
                owner_lease_check=owner_lease_check,
            )
    except CrewRunConflictError:
        raise HTTPException(409, detail={"error": "crew_lease_already_bound"}) from None
    receipt = {"run_id": run_id, "lease_generation": generation}
    if inventory is not None:
        receipt["owner_key"] = inventory.owner_key
        if inventory.state == "provisioning":
            receipt["state"] = "provisioning"
        elif body.target_kind == "eks-pod":
            receipt["pod_name"] = pod.name
            receipt["pod_uid"] = pod.uid
    return JSONResponse(receipt, status_code=201, headers={"Cache-Control": "no-store"})


@router.post("/internal/v1/agent/chat/crew-run/lease", dependencies=[Depends(enabled)])
@contract_errors
async def provision_crew_lease(
    body: CrewActivationRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    store: CrewRunStore = Depends(crew_run_store),
    services=Depends(runtime),
):
    bindings = [binding for binding in root_bindings() if binding.source == "chat" and binding.chat_supervisor_role]
    role = await verify_producer(
        request.headers.get(PROOF_HEADER, ""),
        envelope_digest(body.model_dump()),
        allowed_roles={binding.chat_supervisor_role for binding in bindings},
    )
    authority, _ = services
    now = clock()
    execution, grant, session_id = await run_in_threadpool(root_identity, authority, body.run_id, body.envelope_digest, now)
    tenant_id, user_id = execution.tenant_id, grant.authority.human_id
    metadata = await run_in_threadpool(authority.store._read, f"TENANT#{tenant_id}", f"EXEC#{body.run_id}") or {}
    if not any(
        binding.chat_supervisor_role == role and binding.tenant_id == tenant_id and metadata.get("persona", {}).get("S") in binding.personas
        for binding in bindings
    ):
        raise ChatAuthorizationRefusedError("Crew supervisor scope refused")
    header = await run_in_threadpool(
        lambda: authority.context_table.get_item(Key={"PK": f"session#{session_id}", "SK": "header"}, ConsistentRead=True).get("Item")
    )
    if (
        not header
        or header.get("tenantId") != tenant_id
        or header.get("ownerUserId") != user_id
        or header.get("status") != "active"
        or header.get("sessionMode") != "persistent"
        or header.get("sessionState") in {"ending", "ended", "recovering"}
        or header.get("sessionPendingMode")
        or header.get("ttl", 0) <= now
    ):
        raise HTTPException(409, detail={"error": "chat_lease_unavailable"})
    user = await tenant_member_user(db, tenant_id, user_id)
    if user is None or not await current_chat_member(db, tenant_id, user_id, header.get("teamId", "")):
        raise ChatAuthorizationRefusedError("chat member unavailable")
    inventory = await db.scalar(
        select(CrewExecutor)
        .where(
            CrewExecutor.tenant_id == tenant_id,
            CrewExecutor.user_id == user_id,
            CrewExecutor.state == "provisioning",
        )
        .with_for_update()
    )
    if (
        inventory is None
        or inventory.target_kind != "eks-pod"
        or inventory.generation != 1
        or body.lease_generation != inventory.generation
        or inventory.latest_verified_checkpoint_id is not None
        or inventory.target_ref is not None
        or inventory.lease_generation is not None
        or inventory.lease_expires_at is not None
        or not inventory.owner_key
    ):
        raise HTTPException(409, detail={"error": "crew_target_unavailable"})
    pod = await run_in_threadpool(crew_workloads().verify_bound, name=body.pod_name, uid=body.pod_uid)
    if (
        pod.uid != body.pod_uid
        or pod.name != body.pod_name
        or pod.owner_key != inventory.owner_key
        or pod.run_hash != hashlib.sha256(body.run_id.encode()).hexdigest()
        or pod.session_hash != hashlib.sha256(session_id.encode()).hexdigest()
    ):
        raise HTTPException(409, detail={"error": "crew_target_unavailable"})
    try:
        pod_ip = IPv4Address(pod.ip)
    except (TypeError, ValueError):
        raise HTTPException(409, detail={"error": "crew_target_unavailable"}) from None
    if not any(pod_ip in IPv4Network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise HTTPException(409, detail={"error": "crew_target_unavailable"})
    prepared = await run_in_threadpool(store.read_run, body.crew_run_id)
    creation = await run_in_threadpool(authority.store._read, f"CHAT-LAUNCH#{body.run_id}", "CREATION")
    binding = creation.get("binding", {}).get("M", {}) if creation else {}
    if (
        not prepared
        or prepared.get("status") != "provisioning"
        or prepared.get("kind") != "crew"
        or prepared.get("executor_scope") != "user"
        or prepared.get("tenant_id") != tenant_id
        or prepared.get("user_id") != user_id
        or prepared.get("session_id") != session_id
        or prepared.get("lease_generation") != inventory.generation
        or prepared.get("target_kind") != "eks-pod"
        or prepared.get("target_ref") != pod.name
        or prepared.get("lease_expires_at", 0) <= now
        or binding.get("tenant_id") != {"S": tenant_id}
        or binding.get("user_id") != {"S": user_id}
        or binding.get("session_id") != {"S": session_id}
        or binding.get("pod_name") != {"S": pod.name}
        or binding.get("image_digest") != {"S": pod.image_digest}
        or creation.get("crew_run_id") != {"S": body.crew_run_id}
        or creation.get("crew_lease_generation") != {"N": str(body.lease_generation)}
    ):
        raise HTTPException(409, detail={"error": "crew_creation_changed"})
    expires_at = min(
        int(prepared["lease_expires_at"]),
        int(header["ttl"]),
        int(grant.expires_at.timestamp()),
        now + PERSISTENT_LEASE_SECONDS,
    )
    if expires_at <= now:
        raise HTTPException(409, detail={"error": "crew_lease_changed"})
    lease = CrewLease(
        tenant_id=tenant_id,
        user_id=user_id,
        target_kind="eks-pod",
        target_ref=pod.uid,
        generation=inventory.generation,
        expires_at=expires_at,
        endpoint=f"http://{pod_ip}:5476",
    ).model_dump()
    key = {"PK": f"crew#{tenant_id}#{user_id}", "SK": "executor"}
    existing = await run_in_threadpool(lambda: authority.context_table.get_item(Key=key, ConsistentRead=True).get("Item"))
    if existing is not None:
        if (
            existing.get("tenantId") != tenant_id
            or existing.get("ownerUserId") != user_id
            or existing.get("crewLeaseState") != "provisioning"
            or existing.get("crewLease") != lease
        ):
            raise HTTPException(409, detail={"error": "crew_lease_changed"})
    else:
        creation_check = ChatTeardown(authority, None)._unchanged(creation)
        for field in ("Key", "ExpressionAttributeValues"):
            creation_check[field] = TypeDeserializer().deserialize({"M": creation_check[field]})
        session_check = {
            "TableName": authority.context_table.name,
            "Key": {"PK": f"session#{session_id}", "SK": "header"},
            "ConditionExpression": (
                "tenantId = :tenant AND ownerUserId = :user AND #status = :active "
                "AND sessionMode = :mode AND #ttl > :now AND attribute_not_exists(sessionPendingMode) "
                + ("AND sessionState = :state" if "sessionState" in header else "AND attribute_not_exists(sessionState)")
            ),
            "ExpressionAttributeNames": {"#status": "status", "#ttl": "ttl"},
            "ExpressionAttributeValues": {
                ":tenant": tenant_id,
                ":user": user_id,
                ":active": "active",
                ":mode": "persistent",
                ":now": now,
                **({":state": header["sessionState"]} if "sessionState" in header else {}),
            },
        }
        try:
            await run_in_threadpool(
                store.table.meta.client.transact_write_items,
                TransactItems=[
                    {"ConditionCheck": creation_check},
                    {"ConditionCheck": session_check},
                    {
                        "ConditionCheck": {
                            "TableName": store.table.name,
                            "Key": {"event_id": f"crew-owner#{tenant_id}#{user_id}", "arrived_at": "crew"},
                            "ConditionExpression": "run_id = :run AND generation = :generation AND lease_expires_at > :now",
                            "ExpressionAttributeValues": {
                                ":run": body.crew_run_id,
                                ":generation": body.lease_generation,
                                ":now": now,
                            },
                        }
                    },
                    {
                        "Put": {
                            "TableName": authority.context_table.name,
                            "Item": {**key, "tenantId": tenant_id, "ownerUserId": user_id, "crewLease": lease, "crewLeaseState": "provisioning"},
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ],
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise HTTPException(409, detail={"error": "crew_lease_changed"}) from None
            raise
    return JSONResponse(
        {"run_id": body.crew_run_id, "lease_generation": body.lease_generation, "state": "provisioning"},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/internal/v1/agent/chat/crew-run/activate", dependencies=[Depends(enabled)])
@contract_errors
async def activate_crew_run(
    body: CrewActivationRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    store: CrewRunStore = Depends(crew_run_store),
    services=Depends(runtime),
):
    bindings = [binding for binding in root_bindings() if binding.source == "chat" and binding.chat_supervisor_role]
    role = await verify_producer(
        request.headers.get(PROOF_HEADER, ""),
        envelope_digest(body.model_dump()),
        allowed_roles={binding.chat_supervisor_role for binding in bindings},
    )
    authority, _ = services
    now = clock()
    execution, grant, session_id = await run_in_threadpool(root_identity, authority, body.run_id, body.envelope_digest, now)
    tenant_id, user_id = execution.tenant_id, grant.authority.human_id
    metadata = await run_in_threadpool(authority.store._read, f"TENANT#{tenant_id}", f"EXEC#{body.run_id}")
    if not any(
        binding.chat_supervisor_role == role and binding.tenant_id == tenant_id and metadata.get("persona", {}).get("S") in binding.personas
        for binding in bindings
    ):
        raise ChatAuthorizationRefusedError("Crew supervisor scope refused")
    header = await run_in_threadpool(
        lambda: authority.context_table.get_item(Key={"PK": f"session#{session_id}", "SK": "header"}, ConsistentRead=True).get("Item")
    )
    if (
        not header
        or header.get("tenantId") != tenant_id
        or header.get("ownerUserId") != user_id
        or header.get("status") != "active"
        or header.get("sessionMode") != "persistent"
        or header.get("sessionState") in {"ending", "ended", "recovering"}
        or header.get("ttl", 0) <= now
    ):
        raise HTTPException(409, detail={"error": "chat_lease_unavailable"})
    user = await tenant_member_user(db, tenant_id, user_id)
    if user is None:
        raise HTTPException(404, detail={"error": "chat_member_unavailable"})
    inventory = await db.scalar(
        select(CrewExecutor)
        .where(
            CrewExecutor.tenant_id == tenant_id,
            CrewExecutor.user_id == user_id,
            CrewExecutor.state.in_(("ready", "provisioning")),
        )
        .with_for_update()
    )
    if (
        inventory is None
        or inventory.target_kind != "eks-pod"
        or inventory.generation != body.lease_generation
        or not inventory.owner_key
        or (
            inventory.state == "ready"
            and (
                inventory.lease_generation != body.lease_generation
                or inventory.lease_expires_at is None
                or inventory.lease_expires_at <= now
                or inventory.target_ref != body.pod_uid
            )
        )
        or (
            inventory.state == "provisioning"
            and (
                inventory.generation != 1
                or inventory.latest_verified_checkpoint_id is not None
                or inventory.target_ref is not None
                or inventory.lease_generation is not None
                or inventory.lease_expires_at is not None
            )
        )
    ):
        raise HTTPException(409, detail={"error": "crew_target_unavailable"})
    pod = await run_in_threadpool(crew_workloads().verify_bound, name=body.pod_name, uid=body.pod_uid)
    if pod.uid != body.pod_uid or pod.owner_key != inventory.owner_key or pod.name != body.pod_name:
        raise HTTPException(409, detail={"error": "crew_target_unavailable"})
    lease_row = await run_in_threadpool(
        lambda: authority.context_table.get_item(Key={"PK": f"crew#{tenant_id}#{user_id}", "SK": "executor"}, ConsistentRead=True).get("Item")
    )
    if not lease_row or lease_row.get("tenantId") != tenant_id or lease_row.get("ownerUserId") != user_id:
        raise HTTPException(409, detail={"error": "crew_lease_changed"})
    try:
        lease = CrewLease.model_validate(lease_row.get("crewLease"))
    except ValidationError:
        raise HTTPException(409, detail={"error": "crew_lease_changed"}) from None
    lease_state = lease_row.get("crewLeaseState", "ready")
    if lease_state not in ("ready", "provisioning") or (inventory.state == "ready" and lease_state != "ready"):
        raise HTTPException(409, detail={"error": "crew_lease_changed"})
    if (
        lease.tenant_id != tenant_id
        or lease.user_id != user_id
        or lease.target_kind != "eks-pod"
        or lease.target_ref != pod.uid
        or lease.generation != body.lease_generation
        or lease.expires_at <= now
    ):
        raise HTTPException(409, detail={"error": "crew_lease_changed"})
    creation = await run_in_threadpool(authority.store._read, f"CHAT-LAUNCH#{body.run_id}", "CREATION")
    binding = creation.get("binding", {}).get("M", {}) if creation else {}
    if (
        binding.get("tenant_id") != {"S": tenant_id}
        or binding.get("user_id") != {"S": user_id}
        or binding.get("session_id") != {"S": session_id}
        or binding.get("pod_name") != {"S": pod.name}
        or binding.get("image_digest") != {"S": pod.image_digest}
        or creation.get("crew_run_id") != {"S": body.crew_run_id}
        or creation.get("crew_lease_generation") != {"N": str(body.lease_generation)}
    ):
        raise HTTPException(409, detail={"error": "crew_creation_changed"})
    if inventory.state == "provisioning" and lease_state == "ready":
        inventory.state = "ready"
        inventory.target_ref = pod.uid
        inventory.lease_generation = lease.generation
        inventory.lease_expires_at = lease.expires_at
        inventory.last_started_at = datetime.fromtimestamp(now, UTC)
        await db.commit()
        inventory = await db.scalar(
            select(CrewExecutor)
            .where(
                CrewExecutor.tenant_id == tenant_id,
                CrewExecutor.user_id == user_id,
                CrewExecutor.state == "ready",
            )
            .with_for_update()
        )
        if (
            inventory is None
            or inventory.target_kind != "eks-pod"
            or inventory.owner_key != pod.owner_key
            or inventory.target_ref != pod.uid
            or inventory.generation != lease.generation
            or inventory.lease_generation != lease.generation
            or inventory.lease_expires_at is None
            or inventory.lease_expires_at <= now
        ):
            raise HTTPException(409, detail={"error": "crew_target_unavailable"})
    creation_check = ChatTeardown(authority, None)._unchanged(creation)
    for field in ("Key", "ExpressionAttributeValues"):
        creation_check[field] = TypeDeserializer().deserialize({"M": creation_check[field]})
    lease_check = {
        "TableName": authority.context_table.name,
        "Key": {"PK": lease_row["PK"], "SK": "executor"},
        "ConditionExpression": (
            "tenantId = :tenant AND ownerUserId = :user AND #lease = :lease AND #lease.expires_at > :now "
            + ("AND #state = :state" if "crewLeaseState" in lease_row else "AND attribute_not_exists(#state)")
        ),
        "ExpressionAttributeNames": {"#lease": "crewLease", "#state": "crewLeaseState"},
        "ExpressionAttributeValues": {
            ":tenant": tenant_id,
            ":user": user_id,
            ":lease": lease_row["crewLease"],
            ":now": now,
            **({":state": lease_state} if "crewLeaseState" in lease_row else {}),
        },
    }
    session_check = {
        "TableName": authority.context_table.name,
        "Key": {"PK": f"session#{session_id}", "SK": "header"},
        "ConditionExpression": (
            "tenantId = :tenant AND ownerUserId = :user AND #status = :active "
            "AND sessionMode = :mode AND #ttl > :now AND " + ("#state = :state" if "sessionState" in header else "attribute_not_exists(#state)")
        ),
        "ExpressionAttributeNames": {"#status": "status", "#ttl": "ttl", "#state": "sessionState"},
        "ExpressionAttributeValues": {
            ":tenant": tenant_id,
            ":user": user_id,
            ":active": "active",
            ":mode": "persistent",
            ":now": now,
            **({":state": header["sessionState"]} if "sessionState" in header else {}),
        },
    }
    try:
        await run_in_threadpool(
            store.activate_for_pod,
            run_id=body.crew_run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            lease_generation=body.lease_generation,
            target_ref=pod.name,
            pod_uid=pod.uid,
            now=now,
            owner_lease_expires_at=lease.expires_at,
            lease_check=lease_check,
            creation_check=creation_check,
            session_check=session_check,
        )
    except CrewRunConflictError:
        raise HTTPException(409, detail={"error": "crew_lease_changed"}) from None
    return JSONResponse({"run_id": body.crew_run_id, "lease_generation": body.lease_generation}, headers={"Cache-Control": "no-store"})


async def crew_native_healthy(endpoint: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
            async with client.stream("GET", f"{endpoint}/api/health") as response:
                if response.status_code != 200:
                    return False
                content = b""
                async for chunk in response.aiter_bytes():
                    content += chunk
                    if len(content) > 1024:
                        return False
                status = json.loads(content)
                return isinstance(status, dict) and status.get("ok") is True and status.get("app") == "kirocrew"
    except (httpx.RequestError, ValueError):
        return False


@router.post("/internal/v1/agent/chat/crew-run/ready", dependencies=[Depends(enabled)])
@contract_errors
async def ready_crew_run(
    body: CrewActivationRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    store: CrewRunStore = Depends(crew_run_store),
    services=Depends(runtime),
):
    bindings = [binding for binding in root_bindings() if binding.source == "chat" and binding.chat_supervisor_role]
    role = await verify_producer(
        request.headers.get(PROOF_HEADER, ""),
        envelope_digest(body.model_dump()),
        allowed_roles={binding.chat_supervisor_role for binding in bindings},
    )
    authority, capabilities = services
    now = clock()
    execution, grant, session_id = await run_in_threadpool(root_identity, authority, body.run_id, body.envelope_digest, now)
    tenant_id, user_id = execution.tenant_id, grant.authority.human_id
    metadata = await run_in_threadpool(authority.store._read, f"TENANT#{tenant_id}", f"EXEC#{body.run_id}") or {}
    if not any(
        binding.chat_supervisor_role == role and binding.tenant_id == tenant_id and metadata.get("persona", {}).get("S") in binding.personas
        for binding in bindings
    ):
        raise ChatAuthorizationRefusedError("Crew supervisor scope refused")
    header = await run_in_threadpool(
        lambda: authority.context_table.get_item(
            Key={"PK": f"session#{session_id}", "SK": "header"},
            ConsistentRead=True,
        ).get("Item")
    )
    if (
        not header
        or header.get("tenantId") != tenant_id
        or header.get("ownerUserId") != user_id
        or header.get("status") != "active"
        or header.get("sessionMode") != "persistent"
        or header.get("sessionState") != "active"
        or header.get("sessionPendingMode")
        or header.get("ttl", 0) <= now
    ):
        raise HTTPException(409, detail={"error": "chat_lease_unavailable"})
    user = await tenant_member_user(db, tenant_id, user_id)
    if user is None or not await current_chat_member(db, tenant_id, user_id, header.get("teamId", "")):
        raise ChatAuthorizationRefusedError("chat member unavailable")
    inventory = await db.scalar(
        select(CrewExecutor)
        .where(
            CrewExecutor.tenant_id == tenant_id,
            CrewExecutor.user_id == user_id,
            CrewExecutor.state.in_(("provisioning", "ready")),
        )
        .with_for_update()
    )
    if (
        inventory is None
        or inventory.target_kind != "eks-pod"
        or inventory.generation != body.lease_generation
        or (
            inventory.state == "provisioning"
            and (
                inventory.generation != 1
                or inventory.latest_verified_checkpoint_id is not None
                or inventory.target_ref is not None
                or inventory.lease_generation is not None
                or inventory.lease_expires_at is not None
            )
        )
        or (
            inventory.state == "ready"
            and (
                inventory.target_ref != body.pod_uid
                or inventory.lease_generation != body.lease_generation
                or inventory.lease_expires_at is None
                or inventory.lease_expires_at <= now
            )
        )
    ):
        raise HTTPException(409, detail={"error": "crew_target_unavailable"})
    pod = await run_in_threadpool(crew_workloads().verify_bound, name=body.pod_name, uid=body.pod_uid)
    if pod.uid != body.pod_uid or pod.name != body.pod_name or pod.owner_key != inventory.owner_key:
        raise HTTPException(409, detail={"error": "crew_target_unavailable"})
    try:
        pod_ip = IPv4Address(pod.ip)
    except (TypeError, ValueError):
        raise HTTPException(409, detail={"error": "crew_target_unavailable"}) from None
    if not any(pod_ip in IPv4Network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise HTTPException(409, detail={"error": "crew_target_unavailable"})
    lease_row = await run_in_threadpool(
        lambda: authority.context_table.get_item(
            Key={"PK": f"crew#{tenant_id}#{user_id}", "SK": "executor"},
            ConsistentRead=True,
        ).get("Item")
    )
    try:
        lease = CrewLease.model_validate(lease_row["crewLease"])
    except (TypeError, KeyError, ValidationError):
        raise HTTPException(409, detail={"error": "crew_lease_changed"}) from None
    if (
        lease_row.get("tenantId") != tenant_id
        or lease_row.get("ownerUserId") != user_id
        or lease_row.get("crewLeaseState") not in {"provisioning", "ready"}
        or lease.tenant_id != tenant_id
        or lease.user_id != user_id
        or lease.target_kind != "eks-pod"
        or lease.target_ref != pod.uid
        or lease.generation != body.lease_generation
        or lease.endpoint != f"http://{pod_ip}:5476"
        or lease.expires_at <= now
        or (inventory.state == "ready" and lease_row.get("crewLeaseState") != "ready")
    ):
        raise HTTPException(409, detail={"error": "crew_lease_changed"})
    row = await run_in_threadpool(store.read_run, body.crew_run_id)
    owner = await run_in_threadpool(
        lambda: store.table.get_item(
            Key={"event_id": f"crew-owner#{tenant_id}#{user_id}", "arrived_at": "crew"},
            ConsistentRead=True,
        ).get("Item")
    )
    creation = await run_in_threadpool(authority.store._read, f"CHAT-LAUNCH#{body.run_id}", "CREATION") or {}
    launch_row = await run_in_threadpool(authority.store._read, f"CHAT-LAUNCH#{body.run_id}", "LAUNCH")
    launch = await run_in_threadpool(capabilities.launches.load, body.run_id)
    if (
        row is None
        or row.get("status") != "active"
        or row.get("executor_scope") != "user"
        or row.get("tenant_id") != tenant_id
        or row.get("user_id") != user_id
        or row.get("session_id") != session_id
        or row.get("lease_generation") != lease.generation
        or row.get("pod_uid") != pod.uid
        or row.get("target_ref") != pod.name
        or row.get("lease_expires_at", 0) <= now
        or owner is None
        or owner.get("run_id") != body.crew_run_id
        or owner.get("pod_uid") != pod.uid
        or owner.get("generation") != lease.generation
        or owner.get("lease_expires_at", 0) <= now
        or creation.get("crew_run_id") != {"S": body.crew_run_id}
        or creation.get("crew_lease_generation") != {"N": str(lease.generation)}
        or creation.get("binding", {}).get("M", {}).get("pod_name") != {"S": pod.name}
        or creation.get("binding", {}).get("M", {}).get("image_digest") != {"S": pod.image_digest}
        or launch.sandbox_uid != pod.uid
        or launch.tenant_id != tenant_id
        or launch.user_id != user_id
        or launch.session_id != session_id
        or launch.image_digest != pod.image_digest
        or not await run_in_threadpool(authority.current, launch, now)
    ):
        raise HTTPException(409, detail={"error": "crew_lease_changed"})
    if not await crew_native_healthy(lease.endpoint):
        raise HTTPException(425, detail={"error": "crew_not_ready"})
    now = clock()
    if lease.expires_at <= now:
        raise HTTPException(409, detail={"error": "crew_lease_changed"})
    creation_check = ChatTeardown(authority, None)._unchanged(creation)
    launch_check = ChatTeardown(authority, None)._unchanged(launch_row)
    for check in (creation_check, launch_check):
        for field in ("Key", "ExpressionAttributeValues"):
            check[field] = TypeDeserializer().deserialize({"M": check[field]})
    try:
        await run_in_threadpool(
            store.table.meta.client.transact_write_items,
            TransactItems=[
                {"ConditionCheck": creation_check},
                {"ConditionCheck": launch_check},
                {
                    "ConditionCheck": {
                        "TableName": store.table.name,
                        "Key": {"event_id": row["event_id"], "arrived_at": row["arrived_at"]},
                        "ConditionExpression": (
                            "#status = :active AND pod_uid = :pod AND lease_generation = :generation "
                            "AND tenant_id = :tenant AND user_id = :user AND target_ref = :name AND lease_expires_at > :now"
                        ),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {
                            ":active": "active",
                            ":pod": pod.uid,
                            ":generation": lease.generation,
                            ":tenant": tenant_id,
                            ":user": user_id,
                            ":name": pod.name,
                            ":now": now,
                        },
                    }
                },
                {
                    "ConditionCheck": {
                        "TableName": store.table.name,
                        "Key": {"event_id": owner["event_id"], "arrived_at": "crew"},
                        "ConditionExpression": (
                            "run_id = :run AND pod_uid = :pod AND generation = :generation AND executor_scope = :scope "
                            "AND target_kind = :kind AND target_ref = :name AND lease_expires_at > :now"
                        ),
                        "ExpressionAttributeValues": {
                            ":run": body.crew_run_id,
                            ":pod": pod.uid,
                            ":generation": lease.generation,
                            ":scope": "user",
                            ":kind": "eks-pod",
                            ":name": pod.name,
                            ":now": now,
                        },
                    }
                },
                {
                    "ConditionCheck": {
                        "TableName": authority.context_table.name,
                        "Key": {"PK": f"session#{session_id}", "SK": "header"},
                        "ConditionExpression": "tenantId = :tenant AND ownerUserId = :user AND #status = :active AND sessionState = :active "
                        "AND sessionMode = :persistent AND #ttl > :now AND attribute_not_exists(sessionPendingMode) "
                        "AND chatLease = :sessionLease AND chatLease.expires_at > :now",
                        "ExpressionAttributeNames": {"#status": "status", "#ttl": "ttl"},
                        "ExpressionAttributeValues": {
                            ":tenant": tenant_id,
                            ":user": user_id,
                            ":active": "active",
                            ":persistent": "persistent",
                            ":sessionLease": header["chatLease"],
                            ":now": now,
                        },
                    }
                },
                {
                    "Update": {
                        "TableName": authority.context_table.name,
                        "Key": {"PK": lease_row["PK"], "SK": "executor"},
                        "UpdateExpression": "SET crewLeaseState = :ready",
                        "ConditionExpression": "tenantId = :tenant AND ownerUserId = :user AND crewLease = :lease AND crewLeaseState = :state "
                        "AND crewLease.expires_at > :now",
                        "ExpressionAttributeValues": {
                            ":tenant": tenant_id,
                            ":user": user_id,
                            ":lease": lease_row["crewLease"],
                            ":state": lease_row["crewLeaseState"],
                            ":ready": "ready",
                            ":now": now,
                        },
                    }
                },
            ],
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            raise HTTPException(409, detail={"error": "crew_lease_changed"}) from None
        raise
    if inventory.state == "provisioning":
        inventory.state = "ready"
        inventory.target_ref = pod.uid
        inventory.lease_generation = lease.generation
        inventory.lease_expires_at = lease.expires_at
        inventory.last_started_at = datetime.fromtimestamp(now, UTC)
        await db.commit()
    return JSONResponse(
        {"run_id": body.crew_run_id, "lease_generation": lease.generation, "state": "ready"},
        headers={"Cache-Control": "no-store"},
    )


@router.delete("/internal/v1/agent/chat/crew-run/{run_id}", dependencies=[Depends(enabled)])
@contract_errors
async def revoke_crew_run(run_id: Identifier, request: Request, store: CrewRunStore = Depends(crew_run_store)):
    bindings = [binding for binding in root_bindings() if binding.source == "chat" and binding.chat_supervisor_role]
    role = await verify_producer(
        request.headers.get(PROOF_HEADER, ""),
        envelope_digest({"run_id": run_id}),
        allowed_roles={binding.chat_supervisor_role for binding in bindings},
    )
    item = await run_in_threadpool(store.read_run, run_id)
    if not item or item.get("kind") != "crew":
        raise HTTPException(404, detail={"error": "unknown_run"})
    if role not in {binding.chat_supervisor_role for binding in bindings if binding.tenant_id == item.get("tenant_id")}:
        raise HTTPException(403, "forbidden")
    try:
        revoked = await run_in_threadpool(
            store.revoke_for_lease,
            tenant_id=item["tenant_id"],
            session_id=item["session_id"],
            lease_generation=int(item["lease_generation"]),
            expected_run_id=run_id,
        )
    except CrewRunConflictError:
        raise HTTPException(409, detail={"error": "crew_lease_changed"}) from None
    if not revoked:
        raise HTTPException(409, detail={"error": "crew_lease_changed"})
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.post("/internal/v1/agent/chat/data/admit", dependencies=[Depends(enabled)])
@contract_errors
async def admit_chat(body: AdmissionRequest, request: Request, services=Depends(runtime), db: AsyncSession = Depends(get_db)):
    authority, capabilities = services
    bindings = [binding for binding in root_bindings() if binding.source == "chat"]
    roles = {role for binding in bindings for role in (binding.producer_role, binding.chat_supervisor_role) if role is not None}
    role = await verify_producer(request.headers.get(PROOF_HEADER, ""), envelope_digest(body.model_dump()), allowed_roles=roles)
    now = clock()
    execution, grant, session_id = await run_in_threadpool(root_identity, authority, body.run_id, body.envelope_digest, now)
    metadata = await run_in_threadpool(authority.store._read, f"TENANT#{execution.tenant_id}", f"EXEC#{body.run_id}")
    if not any(
        role in (binding.producer_role, binding.chat_supervisor_role)
        and binding.tenant_id == execution.tenant_id
        and metadata.get("persona", {}).get("S") in binding.personas
        for binding in bindings
    ):
        raise ChatAuthorizationRefusedError("chat producer scope refused")
    # The root's human holds the chat tenant through tenant membership; their own
    # users.org_id is a personal org for GitHub-login users and never scopes this.
    user = await tenant_member_user(db, execution.tenant_id, grant.authority.human_id)
    if user is None:
        raise ChatAuthorizationRefusedError("chat member unavailable")
    team = await primary_team_for_workspace(db, user, execution.tenant_id)
    team_id = team.id if team else ""
    if not await current_chat_member(db, execution.tenant_id, user.id, team_id):
        raise ChatAuthorizationRefusedError("chat membership refused")
    verifier = await run_in_threadpool(authority.workloads_for_run, body.run_id)
    pod = await run_in_threadpool(verifier.verify_bound, name=body.pod_name, uid=body.pod_uid)
    creation = await run_in_threadpool(authority.store._read, f"CHAT-LAUNCH#{body.run_id}", "CREATION") or {}
    bound_run = creation.get("crew_run_id", {}).get("S")
    crew_owner_key = None
    if bound_run:
        row = await run_in_threadpool(crew_run_store().read_run, bound_run)
        if row and row.get("executor_scope") == "user":
            inventory = await db.scalar(
                select(CrewExecutor)
                .where(
                    CrewExecutor.tenant_id == execution.tenant_id,
                    CrewExecutor.user_id == user.id,
                    CrewExecutor.state.in_(("provisioning", "ready")),
                )
                .with_for_update()
            )
            if (
                inventory is None
                or inventory.target_kind != "eks-pod"
                or inventory.owner_key != pod.owner_key
                or inventory.generation != row.get("lease_generation")
                or (
                    inventory.state == "ready"
                    and (
                        inventory.target_ref != pod.uid
                        or inventory.lease_generation != inventory.generation
                        or inventory.lease_expires_at is None
                        or inventory.lease_expires_at <= now
                    )
                )
            ):
                raise ChatAuthorizationRefusedError("Crew owner inventory unavailable")
            crew_owner_key = inventory.owner_key
    launch = await run_in_threadpool(
        admit,
        authority,
        run_id=body.run_id,
        digest=body.envelope_digest,
        pod=pod,
        team_id=team_id,
        now=now,
        crew_store_factory=crew_run_store,
        crew_owner_key=crew_owner_key,
    )
    await run_in_threadpool(capabilities.issue, launch.run_id, pod, now=now)
    return JSONResponse(
        {"run_id": launch.run_id, "session_id": session_id, "lease_generation": launch.lease_generation}, headers={"Cache-Control": "no-store"}
    )


@router.post("/internal/v1/agent/chat/data/exit", dependencies=[Depends(enabled)])
@contract_errors
async def sandbox_exit(body: AdmissionRequest, request: Request, services=Depends(runtime)):
    terminated = await observe_teardown(body, request, services, removed=False)
    return JSONResponse({"run_id": body.run_id, "pod_uid": body.pod_uid, "terminated": terminated}, headers={"Cache-Control": "no-store"})


@router.post("/internal/v1/agent/chat/data/teardown", dependencies=[Depends(enabled)])
@contract_errors
async def sandbox_teardown(body: AdmissionRequest, request: Request, services=Depends(runtime)):
    removed = await observe_teardown(body, request, services, removed=True)
    return JSONResponse({"run_id": body.run_id, "pod_uid": body.pod_uid, "removed": removed}, headers={"Cache-Control": "no-store"})


@router.post("/internal/v1/agent/chat/data/finalize", dependencies=[Depends(enabled)])
@contract_errors
async def finalize_chat(body: AdmissionRequest, request: Request, services=Depends(runtime)):
    if not await observe_teardown(body, request, services, removed=True):
        raise ChatSandboxRemovalUnconfirmedError("chat sandbox removal not confirmed")
    authority, capabilities = services
    writer = ChatTurnFinalizer(authority, ChatHistoryStore(authority.context_table, capabilities))
    terminal = await run_in_threadpool(writer.finalize, body, now=clock())
    await run_in_threadpool(publish_terminal_delivery, authority, capabilities.launches, terminal)
    return JSONResponse(terminal, headers={"Cache-Control": "no-store"})


@router.post("/internal/v1/agent/chat/data/resume", dependencies=[Depends(enabled)])
@contract_errors
async def resume_chat(body: SupervisorResumeRequest, request: Request, services=Depends(runtime), db: AsyncSession = Depends(get_db)):
    bindings = [binding for binding in root_bindings() if binding.source == "chat" and binding.chat_supervisor_role is not None]
    role = await verify_producer(
        request.headers.get(PROOF_HEADER, ""),
        envelope_digest(body.model_dump(exclude_none=True)),
        allowed_roles={binding.chat_supervisor_role for binding in bindings},
    )
    authority, capabilities = services
    receipt = await run_in_threadpool(resume_supervisor_turn, authority, capabilities, body, role, bindings, now=clock())
    if "crew_readiness" in receipt:
        launch = await run_in_threadpool(capabilities.launches.load, body.run_id)
        inventory = await db.scalar(select(CrewExecutor).where(CrewExecutor.tenant_id == launch.tenant_id, CrewExecutor.user_id == launch.user_id))
        lease_row = await run_in_threadpool(
            lambda: authority.context_table.get_item(
                Key={"PK": f"crew#{launch.tenant_id}#{launch.user_id}", "SK": "executor"}, ConsistentRead=True
            ).get("Item", {})
        )
        lease = lease_row.get("crewLease", {})
        generation = receipt["crew_readiness"]["lease_generation"]
        ready = (
            inventory is not None
            and inventory.state == "ready"
            and inventory.target_kind == "eks-pod"
            and inventory.target_ref == launch.sandbox_uid
            and inventory.generation == inventory.lease_generation == generation
            and inventory.lease_expires_at is not None
            and inventory.lease_expires_at > clock()
            and lease_row.get("crewLeaseState") == "ready"
            and lease.get("tenant_id") == launch.tenant_id
            and lease.get("user_id") == launch.user_id
            and lease.get("target_kind") == "eks-pod"
            and lease.get("target_ref") == launch.sandbox_uid
            and lease.get("generation") == generation
            and lease.get("expires_at", 0) > clock()
        )
        receipt["crew_readiness"]["state"] = "ready" if ready else "provisioning"
    return JSONResponse(receipt, headers={"Cache-Control": "no-store"})


@router.post("/internal/v1/agent/chat/data/session/commit", dependencies=[Depends(enabled)])
@contract_errors
async def commit_persistent_turn(body: AdmissionRequest, request: Request, services=Depends(runtime)):
    if await observe_teardown(body, request, services, removed=False):
        raise ChatHistoryConflictError("chat sandbox exited; reconcile teardown")
    authority, capabilities = services
    writer = ChatTurnFinalizer(authority, ChatHistoryStore(authority.context_table, capabilities))
    terminal = await run_in_threadpool(writer.finalize, body, now=clock(), persistent=True)
    await run_in_threadpool(publish_terminal_delivery, authority, capabilities.launches, terminal)
    completion = await run_in_threadpool(complete_delivered_turn, authority, capabilities, body, now=clock(), persistent=True)
    return JSONResponse({"terminal": terminal, "completion": completion}, headers={"Cache-Control": "no-store"})


@router.post("/internal/v1/agent/chat/data/reserve", dependencies=[Depends(enabled)])
@contract_errors
async def reserve_chat(body: CreationRequest, request: Request, services=Depends(runtime)):
    bindings = [binding for binding in root_bindings() if binding.source == "chat" and binding.chat_supervisor_role is not None]
    role = await verify_producer(
        request.headers.get(PROOF_HEADER, ""),
        envelope_digest(body.model_dump()),
        allowed_roles={binding.chat_supervisor_role for binding in bindings},
    )
    authority, capabilities = services
    receipt = await run_in_threadpool(
        resume_supervisor_turn, authority, capabilities, body, role, bindings, now=clock(), creation_image=body.image_digest
    )
    return JSONResponse(receipt, headers={"Cache-Control": "no-store"})


@router.post("/internal/v1/agent/chat/data/complete", dependencies=[Depends(enabled)])
@contract_errors
async def complete_chat(body: AdmissionRequest, request: Request, services=Depends(runtime)):
    if not await observe_teardown(body, request, services, removed=True):
        raise ChatSandboxRemovalUnconfirmedError("chat sandbox removal not confirmed")
    authority, capabilities = services
    receipt = await run_in_threadpool(complete_delivered_turn, authority, capabilities, body, now=clock())
    return JSONResponse(receipt, headers={"Cache-Control": "no-store"})


async def observe_teardown(body, request, services, *, removed):
    authority, capabilities = services
    bindings = [binding for binding in root_bindings() if binding.source == "chat" and binding.chat_supervisor_role is not None]
    role = await verify_producer(
        request.headers.get(PROOF_HEADER, ""),
        envelope_digest(body.model_dump()),
        allowed_roles={binding.chat_supervisor_role for binding in bindings},
    )
    if removed and await run_in_threadpool(authority.store._read, f"CHAT-LAUNCH#{body.run_id}", "LAUNCH") is None:
        receipt = await run_in_threadpool(resume_supervisor_turn, authority, capabilities, body, role, bindings, now=clock(), removed=True)
        return receipt.get("removed") is True
    return await run_in_threadpool(
        ChatTeardown(authority, capabilities.launches, crew_store=current_crew_store() if removed else None).observe,
        body,
        role,
        bindings,
        removed=removed,
        now=clock(),
    )


@router.post("/v1/chat/data/bootstrap", dependencies=[Depends(enabled)])
@contract_errors
async def exchange_chat(body: ExchangeRequest, request: Request, services=Depends(runtime)):
    authority, capabilities = services
    pod = await run_in_threadpool(authority.verify_workload, request.headers.get(WORKLOAD_HEADER, ""))
    binding = await run_in_threadpool(authority.store._read, f"POD#{pod.uid}", "BINDING")
    if not binding:
        raise ChatAuthorizationRefusedError("chat workload not admitted")
    launch = await run_in_threadpool(capabilities.launches.load, binding.get("invocation_id", {}).get("S", ""))
    if (
        launch.sandbox_uid != pod.uid
        or launch.image_digest != pod.image_digest
        or binding.get("tenant_id") != {"S": launch.tenant_id}
        or binding.get("attempt") != {"N": str(launch.attempt)}
    ):
        raise ChatAuthorizationRefusedError("chat workload mismatch")
    if launch.session_run_id:
        header = await run_in_threadpool(
            authority.context_table.get_item, Key={"PK": f"session#{launch.session_id}", "SK": "header"}, ConsistentRead=True
        )
        current_run = header.get("Item", {}).get("chatLease", {}).get("run_id")
        current = await run_in_threadpool(capabilities.launches.load, current_run or "")
        if (
            current.session_run_id != launch.session_run_id
            or current.session_id != launch.session_id
            or current.tenant_id != launch.tenant_id
            or current.user_id != launch.user_id
            or current.team_id != launch.team_id
            or current.sandbox_uid != pod.uid
            or current.image_digest != pod.image_digest
        ):
            raise ChatAuthorizationRefusedError("chat session binding changed")
        launch = current
    now = clock()
    await run_in_threadpool(capabilities.issue, launch.run_id, pod, now=now)
    await run_in_threadpool(renew_lease, authority, launch, now=now)
    session = await run_in_threadpool(
        ChatSessionMailbox(authority.context_table).state,
        session_id=launch.session_id,
        owner=(launch.tenant_id, launch.team_id, launch.user_id),
        now=now,
    )
    token = await run_in_threadpool(capabilities.issue, launch.run_id, pod, now=now)
    qualification = await run_in_threadpool(ChatQualification(*services).for_runtime, launch, now=now)
    return JSONResponse(
        {
            "capability": token,
            **({"qualification_required": True} if qualification else {}),
            "run_id": launch.run_id,
            "session_id": launch.session_id,
            "attempt": launch.attempt,
            "lease_generation": launch.lease_generation,
            "session_mode": session["mode"],
            "expires_at": min(now + 300, launch.expires_at),
        },
        headers={"Cache-Control": "no-store"},
    )
