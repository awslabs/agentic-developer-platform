"""Exact accepted knowledge selection and claimed SetupWork authority."""

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import select

from l5_services.api.bindings import _intent_hash
from l5_services.devops.knowledge import KnowledgeAsset
from l5_services.devops.knowledge_admission import (
    admitted_selection,
    publication_selection,
)
from l5_services.devops.knowledge_contract import MAPPING_VERSION, native_contract
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.setup import BindingOperation, SetupIntent, SetupWork
from l5_services.workers.execution import AuthorityRevoked


def aware(value):
    return value.replace(tzinfo=value.tzinfo or UTC)


@dataclass(frozen=True)
class KnowledgeContext:
    intent: SetupIntent
    operation: BindingOperation
    binding: Binding
    work: SetupWork
    admission: dict


async def current_context(session, intent, claim, *, now=None, lock=False):
    """Use configuration -> binding -> work lock order for durable transitions."""
    now = now or datetime.now(UTC)
    denied = AuthorityRevoked("knowledge_work_authority_changed")
    if (
        intent.service_id != "devops"
        or intent.request.get("action") != "sync_knowledge"
        or intent.authorization.get("purpose") != "devops_knowledge_publication"
        or intent.tenant_id != claim.tenant_id
        or intent.operation_id != claim.operation_id
    ):
        raise denied

    async def get(model, condition):
        query = (
            select(model).where(*condition).execution_options(populate_existing=True)
        )
        return await session.scalar(query.with_for_update() if lock else query)

    config = await get(
        Configuration,
        (
            Configuration.tenant_id == intent.tenant_id,
            Configuration.configuration_id == intent.configuration_id,
        ),
    )
    binding = await get(
        Binding,
        (
            Binding.tenant_id == intent.tenant_id,
            Binding.binding_id == intent.request.get("binding_id"),
        ),
    )
    work = await get(
        SetupWork,
        (SetupWork.tenant_id == intent.tenant_id, SetupWork.work_id == claim.work_id),
    )
    original_hash = intent.intent_hash
    intent = await session.get(
        SetupIntent, (intent.tenant_id, intent.operation_id), populate_existing=True
    )
    operation = await get(
        BindingOperation,
        (
            BindingOperation.tenant_id == intent.tenant_id,
            BindingOperation.operation_id == intent.operation_id,
        ),
    )
    selected = intent.authorization.get("selection") or {}
    authority = intent.authorization
    version = await session.get(
        ConfigurationVersion,
        (
            intent.tenant_id,
            intent.configuration_id,
            intent.request.get("configuration_revision"),
        ),
        populate_existing=True,
    )
    if (
        intent.intent_hash != original_hash
        or intent.intent_hash != _intent_hash(intent)
        or config is None
        or version is None
        or binding is None
        or work is None
        or operation is None
        or (
            config.product_id,
            config.project_id,
            config.service_id,
            config.owner_id,
            config.current_revision,
            config.handoff_state,
        )
        != (
            intent.product_id,
            intent.project_id,
            "devops",
            intent.actor_id,
            version.revision,
            "linked",
        )
        or version.hash != selected.get("configuration_hash")
        or (
            binding.product_id,
            binding.project_id,
            binding.service_id,
            binding.configuration_id,
            binding.state,
            binding.generation,
            binding.revision,
        )
        != (
            intent.product_id,
            intent.project_id,
            "devops",
            intent.configuration_id,
            "ready",
            intent.binding_generation,
            intent.request["binding_revision"] + 1,
        )
        or binding.generation != intent.request["binding_generation"] + 1
        or binding.scope != intent.scope
        or binding.scope != selected.get("scope")
        or binding.connection_ref != selected.get("connection_ref")
        or binding.provider_identity != selected.get("provider_identity")
        or binding.native_resource_ref != selected.get("native_space_ref")
        or operation.binding_id != binding.binding_id
        or operation.action != "sync_knowledge"
        or operation.state not in {"accepted", "running", "waiting", "outcome_unknown"}
        or work.operation_id != intent.operation_id
        or work.generation != intent.binding_generation
        or work.generation != claim.generation
        or work.lease_owner != claim.owner
        or work.fence != claim.fence
        or work.fence < 1
        or work.completed_at is not None
        or work.blocked_at is not None
        or work.lease_until is None
        or aware(work.lease_until) <= now
        or aware(claim.lease_until) != aware(work.lease_until)
    ):
        raise denied
    try:
        deadline = datetime.fromisoformat(authority["expires_at"])
        if deadline.tzinfo is None or deadline <= now:
            raise ValueError("expired")
        admission = authority["knowledge_admission"]
        normalized = publication_selection(
            version.settings["devops"]["knowledge_policy"],
            intent.tenant_id,
            intent.project_id,
        )
        if admission != admitted_selection(
            normalized, admission.get("source_snapshots"), deadline
        ):
            raise ValueError("changed admission")
    except (KeyError, TypeError, ValueError) as error:
        raise denied from error
    return KnowledgeContext(intent, operation, binding, work, admission)


def source_request(context, claim):
    authority = context.intent.authorization
    return {
        "delegation_id": authority["delegation_id"],
        "delegation_revision": authority["delegation_revision"],
        "operation_id": context.intent.operation_id,
        "attempt_id": claim.work_id,
        "lease_owner": claim.owner,
        "lease_fence": claim.fence,
    }


async def resolve_admitted(context, claim, sources, check):
    """Read retained snapshots only; recovery can never repin a newer source."""
    intent, admission = context.intent, context.admission
    request = source_request(context, claim)
    result = []
    for publication, snapshot in zip(
        admission["publications"], admission["source_snapshots"], strict=True
    ):
        await check()
        receipt = await sources.read_source(
            intent.product_id, intent.project_id, snapshot["snapshot_id"], request
        )
        if receipt != snapshot:
            raise AuthorityRevoked("knowledge_source_receipt_changed")
        _, _, metadata, limit = native_contract(
            publication["kind"],
            publication["scope"],
            publication["active"],
            admission["visibility"],
            publication["native_metadata"],
        )
        content = bytearray()
        async for chunk in sources.stream_source(
            intent.product_id, intent.project_id, snapshot["snapshot_id"], request
        ):
            if not isinstance(chunk, bytes) or len(content) + len(chunk) > limit:
                raise AuthorityRevoked("knowledge_source_size_invalid")
            content.extend(chunk)
        artifact = snapshot["content"]
        if (
            len(content) != artifact["bytes"]
            or sha256(content).hexdigest() != artifact["sha256"]
        ):
            raise AuthorityRevoked("knowledge_source_content_changed")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AuthorityRevoked("knowledge_source_encoding_invalid") from error
        await check()
        if (
            await sources.read_source(
                intent.product_id, intent.project_id, snapshot["snapshot_id"], request
            )
            != snapshot
        ):
            raise AuthorityRevoked("knowledge_source_receipt_changed")
        result.append(
            KnowledgeAsset(
                publication["source_ref"],
                artifact["sha256"],
                MAPPING_VERSION,
                publication["kind"],
                publication["scope"],
                publication["active"],
                admission["visibility"],
                text,
                metadata,
            )
        )
    return result
