"""Disabled-by-default chat admission, workload exchange and scoped history access."""

import asyncio
import functools
import os
import time
from functools import lru_cache
from typing import Annotated
from urllib.parse import quote

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
from src.agentauth.chat_admission import admit, renew_lease, root_identity
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
from src.agentauth.chat_authority import ChatRuntimeAuthority, chat_capabilities, current_chat_member
from src.agentauth.chat_capability import (
    ChatAuthorizationRefusedError,
    ChatAuthorizationUnavailableError,
    ChatCapabilityExpiredError,
    ChatCapabilityInvalidError,
    Identifier,
)
from src.agentauth.chat_draft import ChatDraftConflictError, ChatDraftStore, DraftWrite
from src.agentauth.chat_history_compaction import ChatHistoryCompactor, HistoryCompaction
from src.agentauth.chat_history_store import ChatHistoryExpiredError, ChatHistoryStore
from src.agentauth.chat_history_summary import ChatSummaryWriter, SummaryAppend
from src.agentauth.chat_history_write import AssistantAppend, ChatHistoryConflictError, ChatHistoryWriter
from src.agentauth.chat_memory import ChatMemoryConflictError, ChatMemoryStore, MemoryId, MemorySearch, MemoryWrite
from src.agentauth.chat_session_acl import AclWrite, ChatSessionAclConflictError, ChatSessionAclWriter
from src.agentauth.external_roots import root_bindings, root_store
from src.agentauth.store import AuthorityStoreError
from src.agentauth.work_routes import PROOF_HEADER, verify_producer
from src.agentauth.workload import WORKLOAD_HEADER, KubernetesWorkloadVerifier, WorkloadRefusedError, WorkloadUnavailableError
from src.orchestration.intake_wiring import _get_context_table
from src.shared.database import get_db, get_session_factory
from src.shared.identity.workspaces import primary_team_for_workspace
from src.shared.models.organization import User

router = APIRouter(tags=["chat-data"])


def clock():
    return int(time.time())


def enabled():
    if os.environ.get("ADP_CHAT_DATA_ENABLED") != "true":
        raise HTTPException(503, detail={"error": "chat_data_disabled"}, headers={"Cache-Control": "no-store"})


@lru_cache(maxsize=1)
def _runtime():
    table = _get_context_table()
    if table is None:
        raise ChatAuthorizationUnavailableError("chat context unconfigured")
    authority = ChatRuntimeAuthority(root_store(), table, KubernetesWorkloadVerifier.in_cluster(chat=True))
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
        except ChatHistoryConflictError:
            raise HTTPException(409, detail={"error": "chat_history_conflict"}, headers={"Cache-Control": "no-store"}) from None
        except ChatMemoryConflictError:
            raise HTTPException(409, detail={"error": "chat_memory_conflict"}, headers={"Cache-Control": "no-store"}) from None
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
        except (ChatAuthorizationUnavailableError, WorkloadUnavailableError, AuthorityStoreError, BotoCoreError, ClientError, SQLAlchemyError):
            raise HTTPException(503, detail={"error": "chat_authority_unavailable"}, headers={"Cache-Control": "no-store"}) from None
        # Authentication failures are 401 so the sandbox knows to refresh or re-bootstrap;
        # a genuine capability that lacks authority for a resource stays a non-enumerating 404.
        except ChatCapabilityExpiredError:
            raise HTTPException(401, detail={"error": "capability_expired"}, headers={"Cache-Control": "no-store"}) from None
        except ChatCapabilityInvalidError:
            raise HTTPException(401, detail={"error": "capability_invalid"}, headers={"Cache-Control": "no-store"}) from None
        except (ChatAuthorizationRefusedError, BootstrapRefusedError, WorkloadRefusedError):
            raise HTTPException(404, detail={"error": "chat_scope_refused"}, headers={"Cache-Control": "no-store"}) from None
        except ValidationError:
            # A stored record that no longer matches its contract is unavailable data, not a caller error.
            raise HTTPException(503, detail={"error": "chat_authority_unavailable"}, headers={"Cache-Control": "no-store"}) from None

    return wrapped


class AdmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: Identifier
    envelope_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    pod_name: str = Field(min_length=1, max_length=253)
    pod_uid: Identifier


class ExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


Reference = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:-]+$")]


class HistoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: Identifier
    session_id: Identifier


class HistoryPageRequest(HistoryRequest):
    limit: int = Field(default=100, strict=True, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)


class HistoryMessagesRequest(HistoryRequest):
    ids: list[Reference] = Field(max_length=100)


class HistorySummaryRequest(HistoryRequest):
    summary_id: Reference


class HistoryAppendRequest(HistoryRequest, AssistantAppend):
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


class DraftWriteRequest(HistoryRequest, DraftWrite):
    pass


class AclWriteRequest(HistoryRequest, AclWrite):
    pass


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


Capability = Annotated[str, Depends(bearer)]


@router.post("/v1/chat/data/history/read", dependencies=[Depends(enabled)])
@contract_errors
async def read_history(body: HistoryPageRequest, token: Capability, services=Depends(runtime)):
    authority, capabilities = services
    history = ChatHistoryStore(authority.context_table, capabilities)
    result = await run_in_threadpool(history.read_page, token, **body.model_dump(), now=clock())
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/chat/data/history/messages", dependencies=[Depends(enabled)])
@contract_errors
async def read_messages(body: HistoryMessagesRequest, token: Capability, services=Depends(runtime)):
    authority, capabilities = services
    history = ChatHistoryStore(authority.context_table, capabilities)
    result = await run_in_threadpool(history.get_messages, token, **body.model_dump(), now=clock())
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


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


@router.post("/internal/v1/agent/chat/data/admit", dependencies=[Depends(enabled)])
@contract_errors
async def admit_chat(body: AdmissionRequest, request: Request, services=Depends(runtime), db: AsyncSession = Depends(get_db)):
    authority, capabilities = services
    bindings = [binding for binding in root_bindings() if binding.source == "chat"]
    role = await verify_producer(
        request.headers.get(PROOF_HEADER, ""), envelope_digest(body.model_dump()), allowed_roles={binding.producer_role for binding in bindings}
    )
    now = clock()
    execution, grant, session_id = await run_in_threadpool(root_identity, authority, body.run_id, body.envelope_digest, now)
    metadata = await run_in_threadpool(authority.store._read, f"TENANT#{execution.tenant_id}", f"EXEC#{body.run_id}")
    if not any(
        binding.producer_role == role and binding.tenant_id == execution.tenant_id and metadata.get("persona", {}).get("S") in binding.personas
        for binding in bindings
    ):
        raise ChatAuthorizationRefusedError("chat producer scope refused")
    user = await db.scalar(select(User).where(User.id == grant.authority.human_id, User.org_id == execution.tenant_id, User.user_kind == "human"))
    if user is None:
        raise ChatAuthorizationRefusedError("chat member unavailable")
    team = await primary_team_for_workspace(db, user, execution.tenant_id)
    team_id = team.id if team else ""
    if not await current_chat_member(db, execution.tenant_id, user.id, team_id):
        raise ChatAuthorizationRefusedError("chat membership refused")
    pod = await run_in_threadpool(authority.workloads.verify_bound, name=body.pod_name, uid=body.pod_uid)
    launch = await run_in_threadpool(admit, authority, run_id=body.run_id, digest=body.envelope_digest, pod=pod, team_id=team_id, now=now)
    await run_in_threadpool(capabilities.issue, launch.run_id, pod, now=now)
    return JSONResponse(
        {"run_id": launch.run_id, "session_id": session_id, "lease_generation": launch.lease_generation}, headers={"Cache-Control": "no-store"}
    )


@router.post("/v1/chat/data/bootstrap", dependencies=[Depends(enabled)])
@contract_errors
async def exchange_chat(body: ExchangeRequest, request: Request, services=Depends(runtime)):
    authority, capabilities = services
    pod = await run_in_threadpool(authority.workloads.verify, request.headers.get(WORKLOAD_HEADER, ""))
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
    now = clock()
    await run_in_threadpool(capabilities.issue, launch.run_id, pod, now=now)
    await run_in_threadpool(renew_lease, authority, launch, now=now)
    token = await run_in_threadpool(capabilities.issue, launch.run_id, pod, now=now)
    return JSONResponse(
        {"capability": token, "run_id": launch.run_id, "session_id": launch.session_id, "expires_at": min(now + 300, launch.expires_at)},
        headers={"Cache-Control": "no-store"},
    )
