"""Local Product owners authorize local evidence; no foreign flow is read here."""

from datetime import UTC, datetime

from l5_services.api.bindings import view_binding
from l5_services.api.configuration import view
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.run import view_run
from sqlalchemy import select

from src.auth.native_launch_revocation import require_native_launch_session_digest
from src.auth.security_fixture_credential import fingerprint, require_human_subject
from src.orchestration.l5_observation_routes import human_session
from src.orchestration.l5_observation_scope import aware, digest, require
from src.products.models import Product, ProductProjectBinding, Project
from src.shared.identity.product_scope import owner_scope, product_member, product_visibility_scope
from src.shared.identity.workflow_access import is_workflow_member

from .security_evidence_contract import Selection, timestamp
from .security_evidence_model import SecurityEvidenceEvent
from .security_evidence_store import FLAGS, append, event, event_id, reference

SLOTS = ("api", "mcp", "cli", "legacy_ui")


async def interactive(backend, request):
    return await human_session(request, backend.actor, backend.db)


async def request_row(backend, product_id, project_id, request_id, *, current=True, lock=False):
    statement = (
        select(SecurityEvidenceEvent)
        .where(
            SecurityEvidenceEvent.tenant_id == backend.actor.org_id,
            SecurityEvidenceEvent.id == event_id(request_id, "request"),
            SecurityEvidenceEvent.kind == "request",
        )
        .execution_options(populate_existing=True)
    )
    row = await backend.db.scalar(statement.with_for_update() if lock else statement)
    require(row is not None and row.request_id == request_id, "not_found", 404)
    selected = Selection.model_validate(row.document["selection"])
    require((selected.tenant_id, selected.product_id, selected.project_id) == (backend.actor.org_id, product_id, project_id), "not_found", 404)
    if current:
        require(timestamp(row.document["expires_at"]) > datetime.now(UTC), "security_evidence_request_expired")
    return row, selected


async def member_access(db, tenant, product_id, project_id, user_id, *, owner=False):
    member = await product_member(db, user_id=user_id, org_id=tenant)
    require(member is not None and await is_workflow_member(db, user_id=user_id, org_id=tenant), "not_found", 404)
    product = await db.scalar(
        select(Product)
        .where(Product.id == product_id, owner_scope(member, tenant) if owner else product_visibility_scope(member, tenant))
        .execution_options(populate_existing=True)
    )
    project = await db.get(Project, (tenant, project_id), populate_existing=True)
    relationship = await db.scalar(
        select(ProductProjectBinding)
        .where(
            ProductProjectBinding.tenant_id == tenant,
            ProductProjectBinding.product_id == product_id,
            ProductProjectBinding.project_id == project_id,
            ProductProjectBinding.valid_to.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    require(
        product is not None and product.archived_at is None and project is not None and project.archived_at is None and relationship is not None,
        "not_found",
        404,
    )
    return product, relationship


async def snapshot(backend, selected):
    product, relationship = await member_access(backend.db, selected.tenant_id, selected.product_id, selected.project_id, backend.actor.user_id)
    config, version = await backend.current(selected.product_id, selected.project_id, "security")
    binding = await backend.get_binding(selected.product_id, selected.project_id, "security", selected.binding_id)
    configuration = view(config, version)
    binding_view = view_binding(binding)
    require(
        config.configuration_id == selected.configuration_id
        and config.owner_id == product.owner_user_id
        and config.relationship_revision == str(relationship.relationship_revision)
        and (binding.configuration_id, binding.product_id, binding.project_id, binding.service_id)
        == (selected.configuration_id, selected.product_id, selected.project_id, "security")
        and binding.generation == selected.binding_generation
        and binding.revision == selected.binding_revision
        and digest(binding.connection_ref) == selected.connection_sha256
        and digest(binding.provider_identity) == selected.provider_sha256
        and digest(version.settings.get("source_refs")) == selected.source_sha256,
        "security_evidence_scope_changed",
    )
    result = {"configuration": configuration, "binding": binding_view, "run": None}
    if selected.run_id:
        run = await backend.get_run(selected.product_id, selected.project_id, "security", selected.run_id)
        require(
            run.configuration_id == selected.configuration_id
            and run.configuration_revision == selected.configuration_revision
            and run.binding_id == selected.binding_id
            and run.revision == selected.run_revision
            and digest(run.input) == selected.run_input_sha256,
            "security_evidence_scope_changed",
        )
        result["run"] = view_run(run)
    return result


async def register(backend, http, product_id, project_id, request_id, slot):
    row, selected = await request_row(backend, product_id, project_id, request_id, lock=True)
    # This endpoint only designates the authenticated credential as automated.
    # Designation grants no access and cannot create human observations.
    await member_access(backend.db, selected.tenant_id, product_id, project_id, backend.actor.user_id)
    require(backend.actor.expires_at is not None and aware(backend.actor.expires_at) > datetime.now(UTC), "security_evidence_session_required", 401)
    require(
        backend.actor.account_type == "human" and backend.actor.auth_source == "jwt" and backend.actor.token_use == "access",
        "security_evidence_fixture_session_required",
        403,
    )
    proof = {
        "fingerprint": fingerprint(http),
        "user_id": backend.actor.user_id,
        "expires_at": aware(backend.actor.expires_at).isoformat(),
        "account_type": backend.actor.account_type,
    }
    existing = await event(backend.db, selected.tenant_id, request_id, "credential_" + slot, required=False)
    if existing is not None:
        require(all(existing.document.get(key) == value for key, value in proof.items()), "security_evidence_reader_changed", 403)
        return {"credential": proof, "receipt": reference(existing), **FLAGS}
    require(await event(backend.db, selected.tenant_id, request_id, "grant", required=False) is None, "security_evidence_grant_already_fixed")
    await append(backend.db, selected.tenant_id, event_id(proof["fingerprint"], "credential"), "fixture", backend.actor.user_id, "service", proof)
    registered = await append(backend.db, selected.tenant_id, request_id, "credential_" + slot, backend.actor.user_id, "service", proof)
    return {"credential": proof, "receipt": reference(registered), **FLAGS}


async def preview(backend, http, product_id, project_id, request_id, observer_id=None):
    human = await interactive(backend, http)
    row, selected = await request_row(backend, product_id, project_id, request_id)
    product, _ = await member_access(backend.db, selected.tenant_id, product_id, project_id, human.user_id, owner=True)
    require(human.user_id == product.owner_user_id, "not_found", 404)
    observer_id = selected.observer_id or observer_id or human.user_id
    await member_access(backend.db, selected.tenant_id, product_id, project_id, observer_id)
    await require_human_subject(http, observer_id)
    credentials = {slot: (await event(backend.db, selected.tenant_id, request_id, "credential_" + slot)).document for slot in SLOTS}
    require(all(item["fingerprint"] != fingerprint(http) for item in credentials.values()), "security_evidence_interactive_session_required", 403)
    state = await snapshot(backend, selected)
    require(
        observer_id == human.user_id or state["configuration"]["scope"]["visibility"] == "product_team",
        "security_evidence_observer_access_revoked",
        403,
    )
    historical = None
    if row.document.get("export_only"):
        origin, original_grant, records = await historical_records(backend.db, row)
        expected = records[-1].document["after"] if selected.phase == "edit" else original_grant.document["snapshot"]
        require(
            original_grant.actor_id == human.user_id and observer_id == original_grant.document["observer_id"] and state == expected,
            "security_evidence_scope_changed",
        )
        historical = {"origin_request_id": origin.request_id, "receipts": [reference(item) for item in records]}
    else:
        require(
            (state["configuration"]["revision"], state["configuration"]["hash"]) == (selected.configuration_revision, selected.configuration_hash),
            "security_evidence_scope_changed",
        )
    value = {
        "request": row.document,
        "snapshot": state,
        "owner_id": human.user_id,
        "observer_id": observer_id,
        "credentials": credentials,
        "owner_fingerprint": fingerprint(http),
        "maximum_expires_at": min(human.expires_at, timestamp(row.document["expires_at"])).isoformat(),
        "historical": historical,
    }
    return {**value, "snapshot_sha256": digest(value), **FLAGS}


async def endorse(backend, http, product_id, project_id, request_id, body):
    await request_row(backend, product_id, project_id, request_id, lock=True)
    value = await preview(backend, http, product_id, project_id, request_id, body.observer_id)
    require(value["snapshot_sha256"] == body.snapshot_sha256, "security_evidence_scope_changed", 412)
    now = datetime.now(UTC)
    require(await event(backend.db, backend.actor.org_id, request_id, "revoked", required=False) is None, "security_evidence_grant_revoked")
    require(
        now < body.expires_at <= min(timestamp(value["request"]["expires_at"]), aware(backend.actor.expires_at)),
        "security_evidence_expiry_invalid",
        422,
    )
    granted = await append(
        backend.db, backend.actor.org_id, request_id, "grant", backend.actor.user_id, "human", {**value, "expires_at": body.expires_at.isoformat()}
    )
    return {"grant": reference(granted), **FLAGS}


async def validate_export_authority(db, request, reader=None):
    # Local revoke, export, effect and release import serialize on the same
    # tenant-local request. No imported content can win after revocation.
    await db.scalar(
        select(SecurityEvidenceEvent)
        .where(SecurityEvidenceEvent.tenant_id == request.tenant_id, SecurityEvidenceEvent.id == request.id)
        .with_for_update()
    )
    origin_id = request.document.get("origin_request_id") or request.request_id
    if origin_id != request.request_id:
        await db.scalar(
            select(SecurityEvidenceEvent)
            .where(SecurityEvidenceEvent.tenant_id == request.tenant_id, SecurityEvidenceEvent.id == event_id(origin_id, "request"))
            .with_for_update()
        )
    selected = Selection.model_validate(request.document["selection"])
    grant = await event(db, request.tenant_id, request.request_id, "grant")
    require(await event(db, request.tenant_id, request.request_id, "revoked", required=False) is None, "security_evidence_grant_revoked")
    require(timestamp(grant.document["expires_at"]) > datetime.now(UTC), "security_evidence_grant_expired")
    await require_native_launch_session_digest(grant.document["owner_fingerprint"])
    product, relationship = await member_access(db, request.tenant_id, selected.product_id, selected.project_id, grant.actor_id, owner=True)
    config = await db.get(Configuration, (request.tenant_id, selected.configuration_id), populate_existing=True)
    require(
        product.owner_user_id == grant.actor_id
        and config is not None
        and config.owner_id == grant.actor_id
        and config.relationship_revision == str(relationship.relationship_revision),
        "security_evidence_owner_changed",
    )
    await member_access(db, request.tenant_id, selected.product_id, selected.project_id, grant.document["observer_id"])
    current_version = await db.get(
        ConfigurationVersion, (request.tenant_id, config.configuration_id, config.current_revision), populate_existing=True
    )
    require(
        current_version is not None
        and (grant.document["observer_id"] == config.owner_id or current_version.settings.get("visibility") == "product_team"),
        "security_evidence_observer_access_revoked",
        403,
    )
    origin_id = request.document.get("origin_request_id") or request.request_id
    prepared = await event(db, request.tenant_id, origin_id, "prepared", required=False)
    if prepared is not None:
        await require_native_launch_session_digest(prepared.document["fingerprint"])
    if request.document.get("export_only"):
        await historical_records(db, request)
    if reader is not None:
        expected = grant.document["credentials"]["api"]
        require(reader == expected and timestamp(reader["expires_at"]) > datetime.now(UTC), "security_evidence_reader_changed")
        await member_access(db, request.tenant_id, selected.product_id, selected.project_id, reader["user_id"])
        await require_native_launch_session_digest(reader["fingerprint"])
    # Re-evaluate ordinary Product/source rights using the retained canonical
    # owner and current membership, never caller-authored role or view fields.
    from src.l5_integration import GatewayConfigurationBackend
    from src.shared.schemas.auth import TokenContext

    member = await product_member(db, user_id=grant.actor_id, org_id=request.tenant_id)
    actor = TokenContext(
        user_id=member.id,
        org_id=request.tenant_id,
        team_id=member.team_id or "",
        department_id="",
        account_type="human",
        expires_at=timestamp(grant.document["expires_at"]),
        token_use="access",
    )
    current = await snapshot(GatewayConfigurationBackend(db, actor), selected)
    after = await event(db, request.tenant_id, request.request_id, "effect_reconciled", required=False)
    after = after or await event(db, request.tenant_id, request.request_id, "effect_after", required=False)
    expected = after.document["after"] if selected.phase == "edit" and after is not None else grant.document["snapshot"]
    require(current == expected, "security_evidence_scope_changed")
    if grant.document["observer_id"] != grant.actor_id:
        observer = await product_member(db, user_id=grant.document["observer_id"], org_id=request.tenant_id)
        observer_actor = actor.model_copy(update={"user_id": observer.id, "team_id": observer.team_id or ""})
        require(
            await snapshot(GatewayConfigurationBackend(db, observer_actor), selected) == expected, "security_evidence_observer_access_revoked", 403
        )
    return grant


async def prepare(backend, http, product_id, project_id, request_id):
    human = await interactive(backend, http)
    row, selected = await request_row(backend, product_id, project_id, request_id, lock=True)
    require(not row.document.get("export_only"), "security_evidence_export_only")
    grant = await validate_export_authority(backend.db, row)
    require(grant.document["observer_id"] == human.user_id, "not_found", 404)
    require(await snapshot(backend, selected) == grant.document["snapshot"], "security_evidence_scope_changed")
    existing = await event(backend.db, selected.tenant_id, request_id, "prepared", required=False)
    if existing is None:
        existing = await append(
            backend.db,
            selected.tenant_id,
            request_id,
            "prepared",
            human.user_id,
            "human",
            {
                "snapshot_sha256": grant.document["snapshot_sha256"],
                "fingerprint": fingerprint(http),
                "expires_at": min(human.expires_at, timestamp(grant.document["expires_at"])).isoformat(),
            },
        )
    require(
        existing.actor_id == human.user_id
        and existing.document["fingerprint"] == fingerprint(http)
        and timestamp(existing.document["expires_at"]) > datetime.now(UTC),
        "security_evidence_challenge_expired",
    )
    return {
        "challenge_id": request_id,
        "observer_id": grant.document["observer_id"],
        "snapshot": grant.document["snapshot"],
        "snapshot_sha256": grant.document["snapshot_sha256"],
        "request": row.document,
        "expires_at": existing.document["expires_at"],
        "receipt": reference(existing),
        **FLAGS,
    }


async def observe(backend, http, product_id, project_id, request_id, body):
    human = await interactive(backend, http)
    row, selected = await request_row(backend, product_id, project_id, request_id, lock=True)
    require(not row.document.get("export_only"), "security_evidence_export_only")
    grant = await validate_export_authority(backend.db, row)
    challenge = await event(backend.db, selected.tenant_id, request_id, "prepared")
    require(
        challenge.actor_id == human.user_id
        and challenge.document["fingerprint"] == fingerprint(http)
        and timestamp(challenge.document["expires_at"]) > datetime.now(UTC),
        "security_evidence_challenge_expired",
    )
    require(selected.phase == "display" and grant.document["observer_id"] == human.user_id, "not_found", 404)
    state = await snapshot(backend, selected)
    require(state == grant.document["snapshot"] and body.snapshot_sha256 == grant.document["snapshot_sha256"], "security_evidence_scope_changed")
    require(
        (body.configuration_revision, body.binding_revision, body.run_id, body.run_revision)
        == (selected.configuration_revision, selected.binding_revision, selected.run_id, selected.run_revision),
        "security_evidence_display_mismatch",
    )
    existing = await event(backend.db, selected.tenant_id, request_id, "observed", required=False)
    if existing is not None:
        require(existing.actor_id == human.user_id and existing.document["observation"] == body.model_dump(), "security_evidence_already_attempted")
        return {"observation": reference(existing), **FLAGS}
    recorded = await append(
        backend.db,
        selected.tenant_id,
        request_id,
        "observed",
        human.user_id,
        "human",
        {
            "observation": body.model_dump(),
            "snapshot_sha256": body.snapshot_sha256,
            "recorded_at": datetime.now(UTC).isoformat(),
            "session_expires_at": human.expires_at.isoformat(),
        },
    )
    return {"observation": reference(recorded), **FLAGS}


async def revoke(backend, http, product_id, project_id, request_id):
    human = await interactive(backend, http)
    row, selected = await request_row(backend, product_id, project_id, request_id, current=False, lock=True)
    product, _ = await member_access(backend.db, selected.tenant_id, product_id, project_id, human.user_id, owner=True)
    require(product.owner_user_id == human.user_id, "not_found", 404)
    revoked = await append(backend.db, selected.tenant_id, row.request_id, "revoked", human.user_id, "human", {})
    return {"receipt": reference(revoked), **FLAGS}


async def read_status(backend, http, product_id, project_id, request_id):
    # Forensic history remains readable after expiry/revocation by a current
    # local Product member. It never grants export or repeat-effect authority.
    row, selected = await request_row(backend, product_id, project_id, request_id, current=False)
    await member_access(backend.db, selected.tenant_id, product_id, project_id, backend.actor.user_id)
    await backend.current(product_id, project_id, "security")
    records = (
        await backend.db.scalars(
            select(SecurityEvidenceEvent)
            .where(
                SecurityEvidenceEvent.tenant_id == selected.tenant_id,
                SecurityEvidenceEvent.request_id == request_id,
            )
            .order_by(SecurityEvidenceEvent.created_at, SecurityEvidenceEvent.id)
        )
    ).all()
    kinds = {item.kind for item in records}
    by_kind = {item.kind: item for item in records}
    if "revoked" in kinds:
        status = "revoked"
    elif timestamp(row.document["expires_at"]) <= datetime.now(UTC):
        status = "expired"
    elif "effect_before" in kinds:
        after = by_kind.get("effect_reconciled") or by_kind.get("effect_after")
        status = "ready_for_export" if after and after.document.get("outcome") in {"returned", "reconciled"} else "outcome_unknown"
    elif "observed" in kinds:
        status = "ready_for_export" if by_kind["observed"].document["observation"]["observation"] == "displayed" else "observation_mismatch"
    elif (row.document.get("export_only") and "grant" in kinds) or (selected.phase == "readiness" and "prepared" in kinds):
        status = "ready_for_export"
    else:
        status = "awaiting_human_action" if "grant" in kinds else "awaiting_owner_consent"
    next_action = {
        "outcome_unknown": "Use Reconcile original save. Do not save again while its canonical receipt is uncertain.",
        "ready_for_export": "The same current release can request a fresh protected transfer; the original evaluator still decides completion.",
        "observation_mismatch": "Review the reported configuration, binding and run mismatch before collecting a fresh exact observation.",
        "expired": "Ask the same current release for an exact renewal within its original lease; reconcile a committed save first.",
        "revoked": "Consent was revoked. Preserve history; this request cannot authorize another disclosure.",
        "awaiting_human_action": "The selected human must prepare and complete this exact phase in the Security configuration page.",
        "awaiting_owner_consent": "The actual Product owner must preview and approve this exact snapshot, observer and expiry.",
    }[status]
    return {
        "status": status,
        "next_action": next_action,
        "request": row.document,
        "receipts": [reference(item) for item in records],
        "events": [{"kind": item.kind, "document": item.document} for item in records],
        **FLAGS,
    }


async def export_local(backend, http, product_id, project_id, ticket_id):
    ticket = await event(backend.db, backend.actor.org_id, ticket_id, "ticket")
    require(timestamp(ticket.document["expires_at"]) > datetime.now(UTC), "security_evidence_ticket_expired")
    row, selected = await request_row(backend, product_id, project_id, ticket.document["request_id"], lock=True)
    require(row.document["release"] == ticket.document["release"], "security_evidence_release_changed")
    grant = await event(backend.db, selected.tenant_id, row.request_id, "grant")
    reader = grant.document["credentials"]["api"]
    require(reader["user_id"] == backend.actor.user_id and reader["fingerprint"] == fingerprint(http), "security_evidence_reader_changed", 403)
    await validate_export_authority(backend.db, row, reader)
    origin, original_grant, records = await historical_records(backend.db, row)
    facts = {
        "request": row.document,
        "origin_request": origin.document,
        "origin_request_id": origin.request_id,
        "grant": grant.document,
        "grant_receipt": reference(grant),
        "original_grant_receipt": reference(original_grant),
        "events": [
            {"kind": item.kind, "actor_id": item.actor_id, "actor_kind": item.actor_kind, "document": item.document, "receipt": reference(item)}
            for item in records
        ],
        **FLAGS,
    }
    exported = await append(backend.db, selected.tenant_id, ticket_id, "export", backend.actor.user_id, "service", {"reader": reader, "facts": facts})
    return {"receipt": reference(exported), **FLAGS}


async def historical_records(db, request):
    """Validate timely local facts; this alone never grants a new disclosure."""
    origin_id = request.document.get("origin_request_id") or request.request_id
    row = await event(db, request.tenant_id, origin_id, "request")
    selected = Selection.model_validate(row.document["selection"])
    require(
        row.document["release"] == request.document["release"] and row.document["selection"] == request.document["selection"],
        "security_evidence_release_changed",
    )
    require(await event(db, request.tenant_id, origin_id, "revoked", required=False) is None, "security_evidence_grant_revoked")
    original_grant = await event(db, request.tenant_id, origin_id, "grant")
    prepared = await event(db, request.tenant_id, origin_id, "prepared")
    capture_bound = min(
        timestamp(row.document["expires_at"]), timestamp(original_grant.document["expires_at"]), timestamp(prepared.document["expires_at"])
    )
    require(prepared.actor_id == original_grant.document["observer_id"] and prepared.actor_kind == "human", "security_evidence_invalid")
    reconciled = await event(db, selected.tenant_id, row.request_id, "effect_reconciled", required=False)
    kinds = (
        ("effect_before", "effect_committed", "effect_reconciled" if reconciled else "effect_after")
        if selected.phase == "edit"
        else ("prepared" if selected.phase == "readiness" else "observed",)
    )
    records = [await event(db, selected.tenant_id, row.request_id, kind) for kind in kinds]
    for retained in records:
        require(
            retained.document.get("release_check_completed") is False
            and retained.document.get("native_qualified") is False
            and timestamp(row.document["created_at"]) <= aware(retained.created_at)
            and (retained.kind in {"effect_reconciled", "effect_after"} or aware(retained.created_at) <= capture_bound)
            and aware(retained.created_at) <= timestamp(row.document["initial_claim_lease_expires_at"]),
            "security_evidence_invalid",
        )
    if selected.phase == "edit":
        require(
            all(
                item.actor_id == original_grant.document["observer_id"] and item.actor_kind == "human"
                for item in records
                if item.kind != "effect_reconciled"
            ),
            "security_evidence_invalid",
        )
        require(
            records[-1].document.get("outcome") == "reconciled"
            or (records[-1].document.get("outcome") == "returned" and records[-1].document.get("http_status") == 200),
            "security_evidence_effect_not_successful",
        )
    else:
        require(records[0].actor_id == original_grant.document["observer_id"] and records[0].actor_kind == "human", "security_evidence_invalid")
        await require_native_launch_session_digest((await event(db, selected.tenant_id, row.request_id, "prepared")).document["fingerprint"])
    return row, original_grant, records
