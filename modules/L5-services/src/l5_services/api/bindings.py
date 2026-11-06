"""Security Agent binding operations and durable setup receipts."""

import logging
import re
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import rfc8785
import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from l5_services.adapters.well_architected.binding import ProfileIdentityError
from l5_services.models.binding import Binding, RetainedAgentScope
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.evidence import AssessmentResult
from l5_services.models.profile_claim import ProfileClaim
from l5_services.models.run import Run
from l5_services.models.setup import BindingOperation, SetupIntent, SetupResourceReceipt, SetupWork
from l5_services.wa_fence import active_generation, uncertain_start
from l5_services.setup_authority import setup_authorization

logger = logging.getLogger(__name__)


def _digest(value):
    return sha256(rfc8785.dumps(value)).hexdigest()


def _request_key_conflict(operation_id):
    return HTTPException(409, detail={
        "code": "idempotency_conflict", "operation_id": operation_id,
        "message": "A setup request with this key is already retained. Read that operation and reuse its original request. "
                   "Do not change the key to repair a failed request.",
    })


def native_agent_digest(provider_identity, native_resource_ref):
    if (not isinstance(provider_identity, dict) or not isinstance(native_resource_ref, dict)
            or native_resource_ref.get("record_type") != "finops_agent"
            or any(not isinstance(provider_identity.get(field), str) or not provider_identity[field].strip()
                   for field in ("account_id", "region"))
            or not isinstance(native_resource_ref.get("record_id"), str)
            or not native_resource_ref["record_id"].strip()):
        raise HTTPException(409, detail={"code": "native_identity_unconfirmed"})
    return _digest({"account_id": provider_identity["account_id"],
                    "region": provider_identity["region"],
                    "agent_id": native_resource_ref["record_id"]})


async def claim_retained_agent_scope(session, *, tenant_id, product_id, project_id, binding_id,
                                     provider_identity, native_resource_ref):
    digest = native_agent_digest(provider_identity, native_resource_ref)
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError("retained agent scope requires an atomic database claim")
    claim = {"service_id": "finops", "identity_digest": digest, "tenant_id": tenant_id,
             "product_id": product_id, "project_id": project_id, "binding_id": binding_id,
             "conflicted": False}
    result = await session.execute(insert(RetainedAgentScope).values(**claim).on_conflict_do_nothing(
        index_elements=["service_id", "identity_digest"]))
    if result.rowcount == 1:
        return
    existing = await session.get(RetainedAgentScope, ("finops", digest), populate_existing=True)
    if existing is None or existing.conflicted or any(
        getattr(existing, field) != claim[field]
        for field in ("tenant_id", "product_id", "project_id", "binding_id")
    ):
        raise HTTPException(409, detail={"code": "native_scope_conflict"})


def _intent_hash(intent):
    payload = {"operation_id": intent.operation_id, "scope": intent.scope,
               "configuration_id": intent.configuration_id, "request": intent.request,
               "authorization": intent.authorization}
    if intent.binding_generation is not None:
        payload["binding_generation"] = intent.binding_generation
    if intent.owned_resource_receipt is not None:
        payload["owned_resource_receipt"] = intent.owned_resource_receipt
    return _digest(payload)


def view_binding(binding):
    result = {"binding_id": binding.binding_id, "scope": binding.scope,
              "configuration_id": binding.configuration_id, "revision": binding.revision,
              "generation": binding.generation, "state": binding.state, "ownership": binding.ownership,
              "connection_ref": binding.connection_ref, "gaps": binding.gaps,
              "readiness": binding.readiness}
    if binding.native_resource_ref is not None:
        result["native_resource_ref"] = binding.native_resource_ref
    if binding.provider_identity is not None:
        result["provider_identity"] = binding.provider_identity
    if getattr(binding, "service_id", None) == "finops":
        from l5_services.adapters.finops.binding import binding_readback

        record = binding_readback(binding)
        result["readiness"] = []
        if record is not None:
            result["native_agent_display_name"] = record["native_agent_display_name"]
            result["billing_scope"] = record["billing_scope"]
            result["native_configuration_revision"] = record["configuration_revision"]
    return result


def view_operation(operation):
    result = {"operation_id": operation.operation_id, "action": operation.action,
              "state": operation.state, "revision": operation.revision, "gaps": operation.gaps}
    if operation.binding_id is not None:
        result["binding_id"] = operation.binding_id
    if operation.readback_ref is not None:
        result["readback_ref"] = operation.readback_ref
    if operation.cleanup_receipt is not None:
        result["cleanup_receipt"] = operation.cleanup_receipt
    if operation.action == "sync_knowledge" and "knowledge_worker_unqualified" in operation.gaps:
        from l5_services.devops.knowledge_admission import WORKER_ACTION

        result["next_action"] = WORKER_ACTION
    if operation.action == "sync_knowledge":
        actions = {
            "knowledge_owned_asset_busy": "An earlier operation still reserves this native Asset. Inspect that operation and its retained effects; an abandoned or uncertain publication does not release ownership. Native remediation needs separate authorization before another publication can use it.",
            "knowledge_publication_abandoned": "This publication was abandoned by its current owner. Native changes may remain; retained ownership reservations prevent duplicate publication. Inspect the retained effects and use separately authorized remediation for native changes. No native cleanup was performed.",
            "knowledge_publication_cancelled": "This publication was cancelled before native writes. Its original sources and history remain retained; review current consent before a new explicit admission. No native cleanup was performed.",
            "knowledge_native_outcome_unknown": "Reconcile this original operation and its retained asset identity. Do not start another publication; recovery performs readback only.",
            "knowledge_native_use_unqualified": "Content was written and independently read back. Use this operation's knowledge evidence process to prepare a source-dependent scenario, record a real human-observed new native session, and review every selected source before completing the operation.",
            "knowledge_native_consent_required": "The Product owner must review native asset publication consent for this original operation. Source-read consent alone cannot authorize native writes.",
            "knowledge_publication_failed": "Inspect this original operation and its retained effect history. Restore source/native authority before reconciling; do not submit another publication.",
            "knowledge_work_authority_changed": "Review the original operation, current owner/source access and binding generation. Resume only with a current work claim and unchanged selection.",
            "knowledge_source_access_revoked": "The original source is no longer available to the consenting owner. Restore authorized access to that exact source before resuming this operation.",
            "knowledge_native_authority_denied": "Review this operation's source/native consent, connection and work claim. Renew native consent if needed, then explicitly resume the original operation.",
            "knowledge_native_session_expired": "The bounded native session ended. Review current consent and explicitly resume the same operation; uncertain writes will be read back only.",
            "knowledge_instruction_target_occupied": "The selected native agent already has instructions owned by another source. Review that target with its owner; this operation will not replace it.",
        }
        for gap in operation.gaps:
            if gap.startswith("knowledge_") and gap != "knowledge_worker_unqualified":
                result["next_action"] = actions.get(gap, actions["knowledge_publication_failed"])
                break
    return result


class BindingStore:
    def __init__(self, session, tenant_id, product_id, project_id, service_id):
        self.session = session
        self.tenant_id = tenant_id
        self.product_id = product_id
        self.project_id = project_id
        self.service_id = service_id

    async def _persist_setup(self, intent, operation, work):
        # These mappers have no ORM relationships, so add_all does not order
        # their INSERTs by the database foreign keys. Keep the parent flush and
        # both children in one transaction; a child failure rolls back all of it.
        request_key = {field: getattr(intent, field) for field in (
            "tenant_id", "product_id", "project_id", "service_id", "actor_id", "idempotency_key")}
        self.session.add(intent)
        try:
            await self.session.flush()
            self.session.add_all((operation, work))
            await self.session.commit()
        except IntegrityError as error:
            await self.session.rollback()
            sqlstate = getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)
            sqlite_code = getattr(error.orig, "sqlite_errorcode", None)
            if sqlstate == "23505" or sqlite_code in {1555, 2067}:
                # Other unique constraints protect primary IDs/work generations.
                # Only an actually retained request key is an idempotency conflict.
                original = await self.session.scalar(select(SetupIntent).where(*(
                    getattr(SetupIntent, field) == value for field, value in request_key.items())))
                if original is not None:
                    raise _request_key_conflict(original.operation_id) from None
            constraint = (getattr(error.orig, "constraint_name", None)
                          or getattr(getattr(error.orig, "__cause__", None), "constraint_name", None)
                          or getattr(getattr(error.orig, "diag", None), "constraint_name", None))
            if not isinstance(constraint, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,128}", constraint):
                constraint = None
            logger.error("L5 setup persistence failed", extra={
                "event": "l5_setup_persistence_failed", "service_id": self.service_id,
                "operation_id": intent.operation_id, "database_error_type": sqlstate or type(error.orig).__name__,
                "database_constraint": constraint,
            })
            raise HTTPException(500, detail={
                "code": "setup_persistence_failed",
                "message": "The setup request could not be saved. Keep the original request key and ask platform support "
                           "to inspect l5_setup_persistence_failed. Retry after the storage problem is repaired.",
            }) from None

    def _scope(self, model):
        if (model.product_id, model.project_id, model.service_id) != (self.product_id, self.project_id, self.service_id):
            raise HTTPException(404, detail={"code": "not_found"})
        return model

    async def binding(self, binding_id):
        binding = await self.session.get(Binding, (self.tenant_id, binding_id))
        if binding is None:
            raise HTTPException(404, detail={"code": "not_found"})
        self._scope(binding)
        await self.session.refresh(binding)
        return binding

    async def operation(self, operation_id):
        intent = await self.session.get(SetupIntent, (self.tenant_id, operation_id))
        if intent is None:
            raise HTTPException(404, detail={"code": "not_found"})
        self._scope(intent)
        if intent.intent_hash != _intent_hash(intent):
            raise HTTPException(409, detail={"code": "setup_intent_changed"})
        return await self.session.get(BindingOperation, (self.tenant_id, operation_id))

    async def list_bindings(self):
        return (await self.session.scalars(select(Binding).where(
            Binding.tenant_id == self.tenant_id, Binding.product_id == self.product_id,
            Binding.project_id == self.project_id, Binding.service_id == self.service_id,
        ).order_by(Binding.binding_id).limit(100))).all()

    async def _claim_profile(self, reference, verified):
        if self.service_id != "well_architected":
            return
        if reference.get("record_type") != "agent_profile" or not reference.get("record_id"):
            raise HTTPException(404, detail={"code": "not_found"})
        identity = _digest((verified["account_id"], verified["region"], reference["record_id"]))
        claim = await self.session.get(ProfileClaim, (self.service_id, identity))
        scope = (self.tenant_id, self.product_id, self.project_id)
        if claim is not None:
            if (claim.tenant_id, claim.product_id, claim.project_id) != scope:
                raise HTTPException(404, detail={"code": "not_found"})
            return
        self.session.add(ProfileClaim(service_id=self.service_id, native_identity_key=identity,
                                      tenant_id=self.tenant_id, product_id=self.product_id,
                                      project_id=self.project_id))
        try:
            await self.session.flush()
        except IntegrityError:
            await self.session.rollback()
            raise HTTPException(404, detail={"code": "not_found"}) from None

    async def _wa_cleanup_safe(self, binding, operation_id=None):
        others = (await self.session.scalars(select(Binding).where(
            Binding.tenant_id == self.tenant_id, Binding.product_id == self.product_id,
            Binding.project_id == self.project_id, Binding.service_id == "well_architected",
        ))).all()
        profile_arn = binding.native_resource_ref["record_id"]
        identity = binding.provider_identity
        claim = await self.session.get(ProfileClaim, ("well_architected", _digest((
            identity["account_id"], identity["region"], profile_arn))))
        if (claim is None or (claim.tenant_id, claim.product_id, claim.project_id)
                != (self.tenant_id, self.product_id, self.project_id)):
            raise ValueError("native_owned_claim_unconfirmed")
        if any(other.binding_id != binding.binding_id and other.state != "cleaned"
               and (other.native_resource_ref or {}).get("record_id") == profile_arn for other in others):
            raise ValueError("native_profile_still_referenced")
        pending = (await self.session.scalars(select(BindingOperation).where(
            BindingOperation.tenant_id == self.tenant_id, BindingOperation.binding_id == binding.binding_id,
            BindingOperation.state.in_(("accepted", "running", "waiting", "outcome_unknown")),
        ))).all()
        if any(item.operation_id != operation_id for item in pending):
            raise ValueError("native_setup_unresolved")
        runs = (await self.session.scalars(select(Run).where(
            Run.tenant_id == self.tenant_id, Run.binding_id == binding.binding_id,
        ).with_for_update())).all()
        for run in runs:
            if run.provider_state == "not_started" and run.native_job_ref is None and run.state in {
                    "cancelled", "failed", "blocked"}:
                if await uncertain_start(self.session, run):
                    raise ValueError("native_run_not_terminal")
                continue
            if (run.state not in {"succeeded", "failed", "cancelled", "partial"}
                    or run.provider_state not in {"completed", "stopped"}
                    or not run.provider_readback_ref or not run.native_job_ref
                    or not await self.session.scalar(select(AssessmentResult.result_id).where(
                        AssessmentResult.tenant_id == self.tenant_id,
                        AssessmentResult.run_id == run.run_id).limit(1))):
                raise ValueError("native_run_not_terminal")

    async def _wa_cleanup_pending(self, configuration_id):
        return await self.session.scalar(select(Binding.binding_id).where(
            Binding.tenant_id == self.tenant_id, Binding.configuration_id == configuration_id,
            Binding.service_id == "well_architected", Binding.state == "cleanup_pending",
        ).limit(1)) is not None

    async def start(self, request, key, verified):
        if verified.get("owned_cleanup") is not None or verified.get("authorization", {}).get("control_action") == "cleanup":
            raise HTTPException(403, detail={"code": "cleanup_consent_required"})
        if self.service_id == "well_architected" and not isinstance(verified.get("source_role_qualification"), dict):
            raise HTTPException(409, detail={"code": "provider_unqualified"})
        action = request.get("action")
        if action in {"check", "update", "suspend", "resume", "unlink", "cleanup"}:
            raise HTTPException(422, detail={"code": "precondition_required"})
        if self.service_id == "finops":
            if tuple(verified.get("scope", {}).get(field) for field in
                     ("tenant_id", "product_id", "project_id")) != (
                         self.tenant_id, self.product_id, self.project_id):
                raise HTTPException(404, detail={"code": "not_found"})
            if action == "create" and set(request) == {"action", "configuration_revision"}:
                reference = None
            elif (action == "link" and set(request) == {
                    "action", "configuration_revision", "native_resource_ref"
            } and isinstance(request["native_resource_ref"], dict)):
                reference = request["native_resource_ref"]
            else:
                raise HTTPException(422, detail={"code": "validation_error"})
            selection = verified.get("finops") or {}
            if (selection.get("agent_mode") != ("adopt" if action == "link" else "create")
                    or not selection.get("billing_scope") or not selection.get("data_role_ref")
                    or (action == "link" and not selection.get("native_agent_ref"))):
                raise HTTPException(409, detail={"code": "binding_selection_incomplete"})
        elif (action == "link" and set(request) == {"action", "configuration_revision", "native_resource_ref"}
                and isinstance(request["native_resource_ref"], dict)):
            reference = request["native_resource_ref"]
        elif action == "create" and set(request) == {"action", "configuration_revision"}:
            reference = None
        else:
            raise HTTPException(422, detail={"code": "validation_error"})
        if reference is not None and (set(reference) != {
                "tenant_id", "project_id", "source_connection_id", "record_type", "record_id", "record_revision"
        } or not all(isinstance(value, str) and value for value in reference.values())):
            raise HTTPException(422, detail={"code": "validation_error"})
        if (self.service_id == "finops" and reference is not None and (
                reference.get("record_type") != "finops_agent"
                or reference.get("record_id") != selection["native_agent_ref"]
                or reference.get("record_revision") in {"HEAD", "latest", "null"})):
            raise HTTPException(404, detail={"code": "not_found"})
        connection = verified["connection_ref"]
        if self.service_id == "transform":
            expected = verified.get("definition_ref") or verified.get("executor_binding_ref")
            if action == "link" and (reference is None or reference != expected):
                raise HTTPException(404, detail={"code": "not_found"})
            if action == "create" and (verified.get("definition_ref") is not None
                                       or verified.get("transform_definition_create_qualified") is not True):
                raise HTTPException(503, detail={"code": "provider_unqualified"})
        elif (reference is not None and (reference["tenant_id"], reference["project_id"], reference["source_connection_id"])
              != (self.tenant_id, self.project_id, connection["source_connection_id"])):
            raise HTTPException(404, detail={"code": "not_found"})
        if self.service_id == "well_architected" and reference is not None and (
                reference["record_type"] != "agent_profile" or not re.fullmatch(
                    rf"arn:aws:wellarchitected:{re.escape(verified['region'])}:{verified['account_id']}:agent-profile/[A-Za-z0-9_-]+",
                    reference["record_id"],
                )):
            raise HTTPException(404, detail={"code": "not_found"})
        if request.get("configuration_revision") != verified["configuration_revision"]:
            raise HTTPException(412, detail={"code": "precondition_failed"})
        original = await self.session.scalar(select(SetupIntent).where(
            SetupIntent.tenant_id == self.tenant_id, SetupIntent.product_id == self.product_id,
            SetupIntent.project_id == self.project_id, SetupIntent.service_id == self.service_id,
            SetupIntent.actor_id == verified["authorization"]["actor_id"], SetupIntent.idempotency_key == key,
        ))
        if original is not None:
            authority = await setup_authorization(self.session, original)
            if (original.request != request or original.configuration_id != verified["configuration_id"]
                    or original.scope != verified["scope"]
                    or authority["delegation_id"] != verified["authorization"]["delegation_id"]
                    or authority["delegation_revision"] != verified["authorization"]["delegation_revision"]):
                raise _request_key_conflict(original.operation_id)
            return await self.operation(original.operation_id)

        if self.service_id == "well_architected":
            if await self._wa_cleanup_pending(verified["configuration_id"]):
                raise HTTPException(409, detail={"code": "binding_cleanup_in_progress"})
            if await active_generation(self.session, self.tenant_id,
                                       configuration_id=verified["configuration_id"]):
                raise HTTPException(409, detail={"code": "native_generation_active"})
        operation_id = str(uuid4())
        authorization = verified["authorization"]
        if self.service_id == "well_architected":
            authorization = {**authorization, "role_qualification_hash": _digest(verified["source_role_qualification"])}
        intent = SetupIntent(tenant_id=self.tenant_id, operation_id=operation_id,
                             product_id=self.product_id, project_id=self.project_id, service_id=self.service_id,
                             actor_id=verified["authorization"]["actor_id"], idempotency_key=key,
                             configuration_id=verified["configuration_id"], scope=verified["scope"],
                             request=request, binding_generation=1, authorization=authorization,
                             intent_hash="")
        intent.intent_hash = _intent_hash(intent)
        operation = BindingOperation(tenant_id=self.tenant_id, operation_id=operation_id,
                                     action=action, state="blocked" if self.service_id == "finops" and action != "link" else "accepted",
                                     revision=1, gaps=["native_unqualified"] if self.service_id == "finops" and action != "link" else [],
                                     attempts=0)
        work = SetupWork(tenant_id=self.tenant_id, work_id=str(uuid4()), operation_id=operation_id,
                         generation=1, due_at=datetime.now(UTC), attempts=0)
        await self._persist_setup(intent, operation, work)
        return operation

    async def admit_knowledge(self, request, key, etag, verified, admission):
        """Retain the admitted source set; keep unqualified work explicitly blocked."""
        from l5_services.devops.knowledge_admission import WORKER_GAP, admitted_selection, publication_selection
        from l5_services.models.preparation import AdmissionPreparation

        expected = {"action", "configuration_revision", "binding_id", "binding_revision", "binding_generation"}
        if (self.service_id != "devops" or set(request) != expected or request.get("action") != "sync_knowledge"
                or any(type(request.get(field)) is not int or request[field] < 1
                       for field in ("configuration_revision", "binding_revision", "binding_generation"))):
            raise HTTPException(422, detail={"code": "knowledge_selection_invalid"})
        if etag is None:
            raise HTTPException(428, detail={"code": "precondition_required"})
        authorization = verified.get("authorization") or {}
        if authorization.get("purpose") != "devops_knowledge_publication":
            raise HTTPException(403, detail={"code": "knowledge_publication_consent_required"})
        try:
            expiry = datetime.fromisoformat(authorization["expires_at"])
            if expiry.tzinfo is None or expiry <= datetime.now(UTC):
                raise ValueError("expired")
        except (KeyError, TypeError, ValueError):
            raise HTTPException(403, detail={"code": "knowledge_publication_consent_required"}) from None
        async def replay():
            original = await self.session.scalar(select(SetupIntent).where(
                SetupIntent.tenant_id == self.tenant_id, SetupIntent.product_id == self.product_id,
                SetupIntent.project_id == self.project_id, SetupIntent.service_id == self.service_id,
                SetupIntent.actor_id == authorization["actor_id"], SetupIntent.idempotency_key == key
            ).execution_options(populate_existing=True))
            if original is None:
                return None
            if (original.request != request or original.configuration_id != verified["configuration_id"]
                    or original.authorization.get("selection") != verified
                    or original.authorization.get("delegation_id") != authorization["delegation_id"]
                    or original.authorization.get("delegation_revision") != authorization["delegation_revision"]):
                raise _request_key_conflict(original.operation_id)
            return await self.operation(original.operation_id)

        original = await replay()
        if original is not None:
            return original
        config = await self.session.scalar(select(Configuration).where(
            Configuration.tenant_id == self.tenant_id, Configuration.configuration_id == verified["configuration_id"]
        ).with_for_update().execution_options(populate_existing=True))
        original = await replay()
        if original is not None:
            return original
        binding = await self.binding(request["binding_id"])
        version = await self.session.get(ConfigurationVersion, (self.tenant_id, verified["configuration_id"], request["configuration_revision"]), populate_existing=True)
        if (config is None or version is None or config.current_revision != request["configuration_revision"]
                or version.hash != verified["configuration_hash"] or binding.configuration_id != config.configuration_id
                or binding.scope != verified["scope"] or binding.connection_ref != verified["connection_ref"]
                or binding.provider_identity != verified["provider_identity"] or binding.native_resource_ref != verified["native_space_ref"]
                or binding.state != "ready" or config.owner_id != authorization["actor_id"]):
            raise HTTPException(409, detail={"code": "knowledge_selection_changed"})
        if (etag != f'"{binding.revision}"' or request["binding_revision"] != binding.revision
                or request["binding_generation"] != binding.generation):
            raise HTTPException(412, detail={"code": "precondition_failed"})
        preparation = await self.session.get(AdmissionPreparation, (
            self.tenant_id, self.product_id, self.project_id, self.service_id,
            authorization["actor_id"], "sync_knowledge", key), populate_existing=True)
        now = datetime.now(UTC)
        if (preparation is None or preparation.request_hash != _digest(request) or preparation.verified != verified
                or preparation.deadline_at.replace(tzinfo=preparation.deadline_at.tzinfo or UTC) <= now):
            raise HTTPException(409, detail={"code": "knowledge_source_admission_missing"})
        try:
            selection = publication_selection(version.settings["devops"]["knowledge_policy"], self.tenant_id, self.project_id)
            if admission != admitted_selection(selection, admission.get("source_snapshots"), preparation.deadline_at.replace(tzinfo=preparation.deadline_at.tzinfo or UTC)):
                raise ValueError("knowledge_source_admission_changed")
        except (ValueError, KeyError, TypeError) as error:
            raise HTTPException(409, detail={"code": str(error)}) from None
        pending = await self.session.scalar(select(BindingOperation.operation_id).where(
            BindingOperation.tenant_id == self.tenant_id, BindingOperation.binding_id == binding.binding_id,
            BindingOperation.state.in_(("accepted", "running", "waiting", "outcome_unknown", "blocked")),
            BindingOperation.action == "sync_knowledge").limit(1))
        if pending is not None:
            raise HTTPException(409, detail={"code": "knowledge_operation_retained", "operation_id": pending,
                                "message": "Read and resume the original knowledge operation; do not submit a duplicate."})
        generation = binding.generation + 1
        changed = await self.session.execute(update(Binding).where(
            Binding.tenant_id == self.tenant_id, Binding.binding_id == binding.binding_id,
            Binding.revision == binding.revision, Binding.generation == binding.generation,
        ).values(revision=Binding.revision + 1, generation=generation), execution_options={"synchronize_session": False})
        if changed.rowcount != 1:
            await self.session.rollback()
            original = await replay()
            if original is not None:
                return original
            raise HTTPException(412, detail={"code": "precondition_failed"})
        operation_id = str(uuid4())
        intent = SetupIntent(tenant_id=self.tenant_id, operation_id=operation_id, product_id=self.product_id,
                             project_id=self.project_id, service_id=self.service_id, actor_id=authorization["actor_id"],
                             idempotency_key=key, configuration_id=config.configuration_id, scope=verified["scope"],
                             request=request, binding_generation=generation,
                             authorization={**authorization, "knowledge_admission": admission, "selection": verified}, intent_hash="")
        intent.intent_hash = _intent_hash(intent)
        operation = BindingOperation(tenant_id=self.tenant_id, operation_id=operation_id, binding_id=binding.binding_id,
                                     action="sync_knowledge", state="blocked", revision=1, gaps=[WORKER_GAP], attempts=0)
        work = SetupWork(tenant_id=self.tenant_id, work_id=str(uuid4()), operation_id=operation_id,
                         generation=generation, due_at=now, blocked_at=now, last_error=WORKER_GAP, attempts=0)
        try:
            await self._persist_setup(intent, operation, work)
        except HTTPException as error:
            if error.status_code != 409:
                raise
            original = await replay()
            if original is None:
                raise
            return original
        return operation

    async def control(self, request, key, etag, verified):
        cleanup_only = (request.get("action") == "cleanup" and verified.get("authorization", {}).get("control_action") == "cleanup"
                        and isinstance(verified.get("owned_cleanup"), dict))
        if verified.get("owned_cleanup") is not None and not cleanup_only:
            raise HTTPException(403, detail={"code": "cleanup_consent_required"})
        if self.service_id == "well_architected" and not cleanup_only and not isinstance(verified.get("source_role_qualification"), dict):
            raise HTTPException(409, detail={"code": "provider_unqualified"})
        action = request.get("action")
        if self.service_id == "finops" and action not in {"unlink", "cleanup"}:
            raise HTTPException(422, detail={"code": "validation_error"})
        expected = {"action", "configuration_revision", "binding_id"}
        if action == "cleanup":
            expected.add("owned_cleanup_confirmed")
        if self.service_id == "devops" and "resource_selection" in request:
            expected.add("resource_selection")
            if (action != "check" or verified.get("authorization", {}).get("purpose") != "devops_resource_verification"
                    or request["resource_selection"] != verified.get("resource_selection")):
                raise HTTPException(403, detail={"code": "resource_verification_consent_required"})
        if self.service_id == "devops" and verified.get("authorization", {}).get("purpose") == "devops_resource_verification" and "resource_selection" not in request:
            raise HTTPException(403, detail={"code": "resource_verification_consent_required"})
        if self.service_id == "security" and action == "check" and "native_model_ref" in request:
            expected.add("native_model_ref")
            selected = request["native_model_ref"]
            if (not isinstance(selected, dict)
                    or set(selected) != {"tenant_id", "project_id", "source_connection_id",
                                         "record_type", "record_id", "record_revision"}
                    or any(not isinstance(value, str) or not value for value in selected.values())
                    or selected["record_type"] != "threat_model"):
                raise HTTPException(422, detail={"code": "validation_error"})
        if (action not in {"check", "update", "suspend", "resume", "unlink", "cleanup"}
                or set(request) != expected or (action == "cleanup" and request["owned_cleanup_confirmed"] is not True)
                or not isinstance(request.get("binding_id"), str) or not request["binding_id"]):
            raise HTTPException(422, detail={"code": "validation_error"})
        if etag is None:
            raise HTTPException(428, detail={"code": "precondition_required"})
        binding = await self.binding(request["binding_id"])
        if (binding.configuration_id != verified["configuration_id"] or binding.scope != verified["scope"]
                or binding.connection_ref != verified["connection_ref"] or binding.provider_identity is None
                or any(binding.provider_identity.get(field) != verified[field]
                       for field in ("account_id", "role_arn", "region"))):
            raise HTTPException(404, detail={"code": "not_found"})
        original = await self.session.scalar(select(SetupIntent).where(
            SetupIntent.tenant_id == self.tenant_id, SetupIntent.product_id == self.product_id,
            SetupIntent.project_id == self.project_id, SetupIntent.service_id == self.service_id,
            SetupIntent.actor_id == verified["authorization"]["actor_id"], SetupIntent.idempotency_key == key))
        if original is not None:
            if original.request != request or original.configuration_id != verified["configuration_id"]:
                raise _request_key_conflict(original.operation_id)
            return await self.operation(original.operation_id)
        if etag != f'"{binding.revision}"' or request["configuration_revision"] != verified["configuration_revision"]:
            raise HTTPException(412, detail={"code": "precondition_failed"})
        if binding.state in {"cleaned", "cleanup_pending"} or (binding.state == "unlinked" and action != "cleanup"):
            raise HTTPException(409, detail={"code": "binding_not_active"})
        if (self.service_id == "well_architected" and action != "cleanup"
                and await active_generation(self.session, self.tenant_id, binding_id=binding.binding_id)):
            raise HTTPException(409, detail={"code": "native_generation_active"})
        receipt = None
        if action == "cleanup":
            receipt = binding.creation_receipt
            if binding.ownership != "adp_created" or not isinstance(receipt, dict):
                raise HTTPException(403, detail={"code": "ownership_required"})
            source = await self.session.get(SetupIntent, (self.tenant_id, receipt.get("operation_id")))
            if (source is None or source.configuration_id != binding.configuration_id
                    or source.scope != binding.scope or source.intent_hash != _intent_hash(source)
                    or source.intent_hash != receipt.get("intent_hash") or source.request.get("action") != "create"
                    or receipt.get("native_resource_ref") != binding.native_resource_ref
                    or receipt.get("provider_identity") != binding.provider_identity):
                raise HTTPException(403, detail={"code": "ownership_required"})
            if cleanup_only and any(verified["owned_cleanup"].get(field) != value for field, value in (
                    ("native_resource_ref", binding.native_resource_ref), ("provider_identity", binding.provider_identity),
                    ("creation_receipt", receipt))):
                raise HTTPException(403, detail={"code": "ownership_required"})
            if self.service_id == "well_architected":
                try:
                    await self._wa_cleanup_safe(binding)
                except ValueError as error:
                    raise HTTPException(409, detail={"code": str(error)}) from None
        authorization = verified["authorization"]
        if self.service_id == "well_architected" and not cleanup_only:
            authorization = {**authorization, "role_qualification_hash": _digest(verified["source_role_qualification"])}
        if cleanup_only:
            authorization = {**authorization, "owned_cleanup_hash": _digest(verified["owned_cleanup"])}
        operation_id = str(uuid4())
        generation = binding.generation + 1
        changed = await self.session.execute(update(Binding).where(
            Binding.tenant_id == self.tenant_id, Binding.binding_id == binding.binding_id,
            Binding.revision == binding.revision, Binding.generation == binding.generation,
        ).values(revision=Binding.revision + 1, generation=generation,
                 state={"suspend": "suspended", "unlink": "unlinking", "cleanup": "cleanup_pending"}.get(action, "checking")),
            execution_options={"synchronize_session": False})
        if changed.rowcount != 1:
            await self.session.rollback()
            raise HTTPException(412, detail={"code": "precondition_failed"})
        intent = SetupIntent(tenant_id=self.tenant_id, operation_id=operation_id,
                             product_id=self.product_id, project_id=self.project_id, service_id=self.service_id,
                             actor_id=verified["authorization"]["actor_id"], idempotency_key=key,
                             configuration_id=binding.configuration_id, scope=binding.scope,
                             request=request, binding_generation=generation, authorization=authorization,
                             owned_resource_receipt=receipt, intent_hash="")
        intent.intent_hash = _intent_hash(intent)
        operation = BindingOperation(tenant_id=self.tenant_id, operation_id=operation_id,
                                     binding_id=binding.binding_id, action=action, state="accepted", revision=1,
                                     gaps=[], attempts=0)
        if self.service_id == "finops":
            operation.state = "blocked"
            operation.gaps = ["native_unqualified"]
        work = SetupWork(tenant_id=self.tenant_id, work_id=str(uuid4()), operation_id=operation_id,
                         generation=generation, due_at=datetime.now(UTC), attempts=0)
        await self._persist_setup(intent, operation, work)
        return operation

    async def _pinned_intent(self, operation_id, verified):
        await self.operation(operation_id)
        intent = await self.session.get(SetupIntent, (self.tenant_id, operation_id))
        authority = await setup_authorization(self.session, intent)
        if (intent.configuration_id != verified["configuration_id"]
                or intent.scope != verified["scope"]
                or intent.request["configuration_revision"] != verified["configuration_revision"]
                or authority["delegation_id"] != verified["authorization"]["delegation_id"]
                or authority["delegation_revision"] != verified["authorization"]["delegation_revision"]
                or (intent.authorization.get("control_action") == "cleanup" and (
                    intent.request.get("action") != "cleanup" or intent.authorization.get("owned_cleanup_hash") != _digest(verified.get("owned_cleanup"))))
                or (self.service_id == "well_architected" and intent.authorization.get("control_action") != "cleanup" and intent.authorization.get("role_qualification_hash")
                    != _digest(verified.get("source_role_qualification")))
                or (intent.request["action"] == "link" and (
                    intent.request["native_resource_ref"] !=
                    (verified.get("definition_ref") or verified.get("executor_binding_ref"))
                    if self.service_id == "transform" else
                    intent.request["native_resource_ref"]["source_connection_id"] !=
                    verified["connection_ref"]["source_connection_id"]))):
            raise HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
        return intent

    async def _check_retirement(self, intent, receipt):
        if receipt.ownership != "owned":
            raise HTTPException(403, detail={"code": "ownership_required"})
        operation = await self.operation(intent.operation_id)
        if operation.state not in {"blocked", "failed", "succeeded"}:
            raise HTTPException(409, detail={"code": "setup_step_reconcile_required"})
        bindings = (await self.session.scalars(select(Binding).where(
            Binding.service_id == "finops"))).all()
        for binding in bindings:
            if (binding.tenant_id == intent.tenant_id
                    and isinstance(binding.creation_receipt, dict)
                    and binding.creation_receipt.get("operation_id") == intent.operation_id
                    and binding.state not in {"cleanup_pending", "cleanup_failed", "cleaned"}):
                raise HTTPException(409, detail={"code": "resource_still_referenced"})
            if (receipt.native_resource_ref is not None and binding.native_resource_ref is not None
                    and binding.native_resource_ref.get("record_type") == receipt.native_resource_ref["record_type"]
                    and binding.native_resource_ref.get("record_id") == receipt.native_resource_ref["record_id"]
                    and binding.state not in {"cleaned", "unlinked"}
                    and (binding.tenant_id, binding.creation_receipt and binding.creation_receipt.get("operation_id"))
                    != (intent.tenant_id, intent.operation_id)):
                raise HTTPException(409, detail={"code": "resource_still_referenced"})
        receipts = (await self.session.scalars(select(SetupResourceReceipt).where(
            SetupResourceReceipt.resource_kind == receipt.resource_kind))).all()
        for other in receipts:
            if (other.tenant_id, other.operation_id) == (receipt.tenant_id, receipt.operation_id):
                continue
            if (receipt.native_resource_ref is not None and other.native_resource_ref is not None
                    and other.native_resource_ref.get("record_id") == receipt.native_resource_ref["record_id"]
                    and other.state != "absent"):
                raise HTTPException(409, detail={"code": "resource_still_referenced"})
        if receipt.resource_kind != "agent":
            agent = await self.session.get(SetupResourceReceipt, (
                intent.tenant_id, intent.operation_id, "agent"))
            if agent is not None and agent.state != "absent":
                raise HTTPException(409, detail={"code": "agent_retirement_required"})
        if receipt.native_resource_ref is not None and receipt.resource_kind in {"billing_view", "data_role"}:
            versions = (await self.session.execute(select(Configuration, ConfigurationVersion).join(
                ConfigurationVersion,
                (Configuration.tenant_id == ConfigurationVersion.tenant_id)
                & (Configuration.configuration_id == ConfigurationVersion.configuration_id)
                & (Configuration.current_revision == ConfigurationVersion.revision)
            ).where(Configuration.service_id == "finops",
                    Configuration.configuration_id != intent.configuration_id))).all()
            for _, version in versions:
                selection = version.settings.get("finops", {})
                selected = (selection.get("billing_scope") or {}).get("view_ref") if receipt.resource_kind == "billing_view" else selection.get("data_role_ref")
                if selected == receipt.native_resource_ref["record_id"]:
                    raise HTTPException(409, detail={"code": "resource_still_referenced"})

    async def record_setup_resource(self, operation_id, verified, resource_kind, ownership, state,
                                    *, native_resource_ref=None, evidence_digest=None):
        intent = await self._pinned_intent(operation_id, verified)
        if (self.service_id != "finops" or resource_kind not in {"billing_view", "data_role", "agent"}
                or ownership not in {"owned", "adopted"}
                or state not in {"planned", "unknown", "observed", "absent"}):
            raise HTTPException(422, detail={"code": "validation_error"})
        if state in {"observed", "absent"} and (
                not isinstance(evidence_digest, str)
                or re.fullmatch(r"[a-f0-9]{64}", evidence_digest) is None):
            raise HTTPException(409, detail={"code": "readback_required"})
        if native_resource_ref is not None and (
                not isinstance(native_resource_ref, dict)
                or set(native_resource_ref) != {
                    "tenant_id", "project_id", "source_connection_id", "record_type",
                    "record_id", "record_revision"
                } or any(not isinstance(value, str) or not value or value in {"HEAD", "latest", "null"}
                         for value in native_resource_ref.values())
                or (native_resource_ref["tenant_id"], native_resource_ref["project_id"],
                    native_resource_ref["source_connection_id"]) != (
                        self.tenant_id, self.project_id,
                        verified["connection_ref"]["source_connection_id"])):
            raise HTTPException(404, detail={"code": "not_found"})
        if native_resource_ref is not None and (
                native_resource_ref["record_type"] != {
                    "billing_view": "billing_view", "data_role": "data_role", "agent": "finops_agent"
                }[resource_kind] or (resource_kind == "agent" and intent.request["action"] == "link"
                    and native_resource_ref["record_id"] != intent.request["native_resource_ref"]["record_id"])):
            raise HTTPException(404, detail={"code": "not_found"})
        if resource_kind == "agent" and ownership != (
                "adopted" if intent.request["action"] == "link" else "owned"):
            raise HTTPException(409, detail={"code": "setup_step_conflict"})
        if state == "observed" and native_resource_ref is None:
            raise HTTPException(409, detail={"code": "readback_required"})
        existing = await self.session.get(SetupResourceReceipt, (
            self.tenant_id, operation_id, resource_kind))
        if existing is None:
            if state != "planned" or native_resource_ref is not None or evidence_digest is not None:
                raise HTTPException(409, detail={"code": "setup_step_unrecorded"})
            receipt = SetupResourceReceipt(tenant_id=self.tenant_id, operation_id=operation_id,
                                           resource_kind=resource_kind, ownership=ownership,
                                           state="planned", revision=1)
            self.session.add(receipt)
            try:
                await self.session.commit()
            except IntegrityError:
                await self.session.rollback()
                raise HTTPException(409, detail={"code": "setup_step_conflict"}) from None
            return receipt
        if (existing.ownership != ownership or (existing.native_resource_ref is not None
                and native_resource_ref is not None and existing.native_resource_ref != native_resource_ref)):
            raise HTTPException(409, detail={"code": "setup_step_conflict"})
        if (existing.state, existing.native_resource_ref, existing.evidence_digest) == (
                state, native_resource_ref, evidence_digest):
            return existing
        allowed = {
            "planned": {"unknown", "observed", "absent"},
            "unknown": {"observed", "absent"},
            "observed": {"unknown", "absent"},
            "absent": set(),
        }
        if state not in allowed[existing.state] or (state == "absent" and ownership != "owned"):
            raise HTTPException(409, detail={"code": "setup_step_reconcile_required"})
        if state == "absent":
            await self._check_retirement(intent, existing)
        if state == "absent" and existing.native_resource_ref is not None and (
                native_resource_ref != existing.native_resource_ref):
            raise HTTPException(409, detail={"code": "setup_step_conflict"})
        changed = await self.session.execute(update(SetupResourceReceipt).where(
            SetupResourceReceipt.tenant_id == self.tenant_id,
            SetupResourceReceipt.operation_id == operation_id,
            SetupResourceReceipt.resource_kind == resource_kind,
            SetupResourceReceipt.revision == existing.revision,
        ).values(state=state, revision=existing.revision + 1,
                 native_resource_ref=native_resource_ref or existing.native_resource_ref,
                 evidence_digest=evidence_digest), execution_options={"synchronize_session": False})
        if changed.rowcount != 1:
            await self.session.rollback()
            raise HTTPException(409, detail={"code": "setup_step_conflict"})
        await self.session.commit()
        return await self.session.get(SetupResourceReceipt, (
            self.tenant_id, operation_id, resource_kind), populate_existing=True)

    async def finish_link(self, operation_id, verified, provider):
        intent = await self._pinned_intent(operation_id, verified)
        operation = await self.operation(operation_id)
        if operation.state not in {"accepted", "running", "waiting"}:
            return operation
        claimed = await self.session.execute(update(BindingOperation).where(
            BindingOperation.tenant_id == self.tenant_id, BindingOperation.operation_id == operation_id,
            BindingOperation.state.in_(("accepted", "running", "waiting")),
        ).values(state="running", revision=BindingOperation.revision + 1,
                 attempts=BindingOperation.attempts + 1), execution_options={"synchronize_session": False})
        await self.session.commit()
        if claimed.rowcount != 1:
            return await self.operation(operation_id)
        operation = await self.operation(operation_id)
        reference = intent.request["native_resource_ref"]
        try:
            native = await provider.read(reference, verified)
        except (ConnectionError, TimeoutError, httpx.TransportError):
            operation.state = "waiting"
            operation.gaps = ["native_readback_unavailable"]
            operation.revision += 1
            await self.session.commit()
            return operation
        except ProfileIdentityError:
            if self.service_id != "well_architected":
                raise
            operation.state = "blocked"
            operation.gaps = ["native_scope_mismatch"]
            operation.revision += 1
            await self.session.commit()
            return operation
        if (not isinstance(native, dict) or native.get("native_resource_ref") != reference
                or native.get("provider_identity", {}).get("region") != verified["region"]
                or native.get("provider_identity", {}).get("account_id") != verified["account_id"]
                or native.get("provider_identity", {}).get("role_arn") != verified["role_arn"]):
            operation.state = "blocked"
            operation.gaps = ["native_scope_mismatch"]
            operation.revision += 1
            await self.session.commit()
            raise HTTPException(404, detail={"code": "not_found"})

        if self.service_id == "well_architected":
            if (await self._wa_cleanup_pending(intent.configuration_id)
                    or await active_generation(self.session, self.tenant_id,
                                               configuration_id=intent.configuration_id)):
                operation.state = "blocked"
                operation.gaps = ["native_scope_change_fenced"]
                operation.revision += 1
                work = await self.session.scalar(select(SetupWork).where(
                    SetupWork.tenant_id == self.tenant_id, SetupWork.operation_id == operation_id))
                work.blocked_at = datetime.now(UTC)
                work.last_error = "native_scope_change_fenced"
                await self.session.commit()
                return operation
            if native.get("state") != "ready":
                operation.state = "blocked"
                operation.gaps = native.get("gaps") or ["native_scope_unverified"]
                operation.revision += 1
                await self.session.commit()
                return operation
            await self._claim_profile(reference, verified)
        if self.service_id == "finops":
            records = native.get("readiness") or []
            record = records[0].get("native_readback") if len(records) == 1 and isinstance(records[0], dict) else None
            if (native.get("state") != "ready" or native.get("gaps") or not isinstance(record, dict)
                    or record.get("scope") != intent.scope or record.get("native_resource_ref") != reference
                    or record.get("provider_identity") != native["provider_identity"]
                    or record.get("configuration_revision") != verified["configuration_revision"]
                    or record.get("binding_generation") != intent.binding_generation
                    or not record.get("native_agent_display_name")
                    or not (record.get("billing_scope") or {}).get("snapshot_artifact")):
                operation.state = "blocked"
                operation.gaps = ["native_scope_unverified"]
                operation.revision += 1
                await self.session.commit()
                raise HTTPException(409, detail={"code": "native_scope_unverified"})
        existing = await self.session.scalar(select(Binding).where(
            Binding.tenant_id == self.tenant_id, Binding.configuration_id == intent.configuration_id,
            Binding.connection_key == _digest(verified["connection_ref"]), Binding.native_key == _digest(reference),
        ))
        connection = verified["connection_ref"]
        if existing is None:
            existing = Binding(tenant_id=self.tenant_id, binding_id=verified["binding_id"] if self.service_id == "devops" else str(uuid4()),
                               product_id=self.product_id, project_id=self.project_id, service_id=self.service_id,
                               configuration_id=intent.configuration_id, scope=intent.scope,
                               revision=1, generation=1, state=native["state"], ownership="customer_linked",
                               connection_ref=connection, connection_key=_digest(connection),
                               native_resource_ref=native["native_resource_ref"], native_key=_digest(native["native_resource_ref"]),
                               provider_identity=native["provider_identity"], readiness=native.get("readiness", []),
                               gaps=native.get("gaps", []))
            self.session.add(existing)
        if self.service_id == "devops":
            from l5_services.devops.setup import record_setup

            await record_setup(self.session, intent, existing, native)
        elif self.service_id == "finops":
            await claim_retained_agent_scope(self.session, tenant_id=self.tenant_id, product_id=self.product_id,
                                            project_id=self.project_id, binding_id=existing.binding_id,
                                            provider_identity=native["provider_identity"], native_resource_ref=reference)
            existing.state = native["state"]
            existing.readiness = native["readiness"]
            existing.gaps = []
            existing.provider_identity = native["provider_identity"]
            existing.revision += 1
            version = await self.session.get(ConfigurationVersion, (self.tenant_id, intent.configuration_id,
                                                                   verified["configuration_revision"]))
            from l5_services.configuration_readiness import assess_readiness

            version.state, version.gaps, _ = assess_readiness(version.settings, intent.configuration_id,
                [view_binding(existing)], delegation_active=True, service_id="finops")
        operation.binding_id = existing.binding_id
        operation.state = "succeeded"
        operation.gaps = []
        operation.revision += 1
        work = await self.session.scalar(select(SetupWork).where(
            SetupWork.tenant_id == self.tenant_id, SetupWork.operation_id == operation_id))
        work.completed_at = datetime.now(UTC)
        await self.session.commit()
        return operation

    async def finish_create(self, operation_id, verified, provider):
        intent = await self._pinned_intent(operation_id, verified)
        operation = await self.operation(operation_id)
        if intent.request["action"] != "create" or operation.state == "succeeded":
            return operation
        if self.service_id == "transform" and verified.get("transform_definition_create_qualified") is not True:
            raise HTTPException(503, detail={"code": "provider_unqualified"})
        if operation.state == "accepted":
            claimed = await self.session.execute(update(BindingOperation).where(
                BindingOperation.tenant_id == self.tenant_id,
                BindingOperation.operation_id == operation_id,
                BindingOperation.state == "accepted",
            ).values(state="running", revision=BindingOperation.revision + 1,
                     attempts=BindingOperation.attempts + 1),
                execution_options={"synchronize_session": False})
            await self.session.commit()
            if claimed.rowcount != 1:
                return await self.operation(operation_id)
            operation.state = "running"
            if (self.service_id == "well_architected"
                    and await active_generation(self.session, self.tenant_id,
                                                configuration_id=intent.configuration_id)):
                operation.state = "blocked"
                operation.gaps = ["native_scope_change_fenced"]
                operation.revision += 1
                work = await self.session.scalar(select(SetupWork).where(
                    SetupWork.tenant_id == self.tenant_id, SetupWork.operation_id == operation_id))
                work.blocked_at = datetime.now(UTC)
                work.last_error = "native_scope_change_fenced"
                await self.session.commit()
                return operation
            try:
                native = await provider.create(intent, verified)
            except (ConnectionError, TimeoutError, httpx.TransportError):
                native = None
            except ProfileIdentityError:
                if self.service_id != "well_architected":
                    raise
                native = None
        elif operation.state in {"running", "outcome_unknown", "waiting"}:
            if operation.reconcile_checks >= 3:
                return operation
            operation.reconcile_checks += 1
            operation.state = "outcome_unknown"
            await self.session.commit()
            try:
                native = await provider.reconcile(intent, verified, operation.native_candidate_id)
            except (ConnectionError, TimeoutError, httpx.TransportError):
                native = None
            except ProfileIdentityError:
                if self.service_id != "well_architected":
                    raise
                native = None
        else:
            return operation

        receipt = native.get("creation_receipt") if isinstance(native, dict) else None
        reference = native.get("native_resource_ref") if isinstance(native, dict) else None
        if isinstance(native, dict) and isinstance(native.get("candidate_id"), str):
            operation.native_candidate_id = native["candidate_id"]
        if (not isinstance(receipt, dict) or not isinstance(reference, dict)
                or receipt.get("intent_hash") != intent.intent_hash
                or receipt.get("operation_id") != intent.operation_id
                or receipt.get("native_resource_ref") != reference
                or native.get("provider_identity") != receipt.get("provider_identity")
                or any(native.get("provider_identity", {}).get(field) != verified[field]
                       for field in ("account_id", "role_arn", "region"))
                or (reference.get("tenant_id"), reference.get("project_id"),
                    reference.get("source_connection_id")) != (self.tenant_id, self.project_id,
                                                               verified["connection_ref"]["source_connection_id"])
                or (self.service_id == "transform" and reference.get("record_type") != "transform_definition")):
            operation.state = "outcome_unknown"
            operation.revision += 1
            operation.gaps = ["native_identity_unconfirmed"]
            await self.session.commit()
            return operation

        if self.service_id == "well_architected":
            await self._claim_profile(reference, verified)
            if native.get("state") != "ready":
                operation.native_resource_ref = reference
                operation.creation_receipt = receipt
                operation.state = "blocked"
                operation.gaps = native.get("gaps") or ["native_scope_unverified"]
                operation.revision += 1
                await self.session.commit()
                return operation
        connection = verified["connection_ref"]
        existing = await self.session.scalar(select(Binding).where(
            Binding.tenant_id == self.tenant_id, Binding.configuration_id == intent.configuration_id,
            Binding.connection_key == _digest(connection), Binding.native_key == _digest(reference),
        ))
        if existing is not None and existing.creation_receipt != receipt:
            operation.state = "outcome_unknown"
            operation.revision += 1
            operation.gaps = ["native_ownership_conflict"]
            await self.session.commit()
            return operation
        if existing is None:
            existing = Binding(tenant_id=self.tenant_id, binding_id=verified["binding_id"] if self.service_id == "devops" else str(uuid4()),
                               product_id=self.product_id, project_id=self.project_id, service_id=self.service_id,
                               configuration_id=intent.configuration_id, scope=intent.scope,
                               revision=1, generation=1, state=native["state"], ownership="adp_created",
                               connection_ref=connection, connection_key=_digest(connection),
                               native_resource_ref=reference, native_key=_digest(reference),
                               provider_identity=native["provider_identity"], creation_receipt=receipt,
                               readiness=native.get("readiness", []), gaps=native.get("gaps", []))
            self.session.add(existing)
        if self.service_id == "devops":
            from l5_services.devops.setup import record_setup

            await record_setup(self.session, intent, existing, native)
        operation.binding_id = existing.binding_id
        operation.native_resource_ref = reference
        operation.creation_receipt = receipt
        operation.state = "succeeded"
        operation.gaps = []
        operation.revision += 1
        work = await self.session.scalar(select(SetupWork).where(
            SetupWork.tenant_id == self.tenant_id, SetupWork.operation_id == operation_id))
        work.completed_at = datetime.now(UTC)
        await self.session.commit()
        return operation

    async def request_reconcile(self, operation_id, reason, key, verified):
        intent = await self._pinned_intent(operation_id, verified)
        operation = await self.operation(operation_id)
        if self.service_id == "finops" and operation.state == "blocked" and operation.gaps == ["native_unqualified"]:
            if operation.reconcile_key is not None:
                if operation.reconcile_key != key or operation.reconcile_reason != reason:
                    raise HTTPException(409, detail={"code": "idempotency_conflict"})
                return operation
            operation.reconcile_key = key
            operation.reconcile_reason = reason
            operation.revision += 1
            await self.session.commit()
            return operation
        if intent.request["action"] != "create" or operation.state == "succeeded":
            raise HTTPException(409, detail={"code": "reconcile_not_available"})
        if operation.reconcile_key == key:
            if operation.reconcile_reason != reason:
                raise HTTPException(409, detail={"code": "idempotency_conflict"})
            return operation
        if operation.state not in {"running", "outcome_unknown"} or operation.reconcile_checks >= 3:
            raise HTTPException(409, detail={"code": "reconcile_not_available"})
        current_work = await self.session.scalar(select(SetupWork).where(
            SetupWork.tenant_id == self.tenant_id, SetupWork.operation_id == operation_id,
        ).order_by(SetupWork.generation.desc()))
        now = datetime.now(UTC)
        if (current_work.completed_at is None and current_work.lease_until is not None
                and current_work.lease_until.replace(tzinfo=UTC) > now):
            raise HTTPException(409, detail={"code": "setup_attempt_in_progress"})
        current_work.completed_at = now
        self.session.add(SetupWork(tenant_id=self.tenant_id, work_id=str(uuid4()), operation_id=operation_id,
                                   generation=current_work.generation + 1, due_at=now, attempts=0))
        operation.reconcile_key = key
        operation.reconcile_reason = reason
        operation.state = "waiting"
        operation.revision += 1
        await self.session.commit()
        return operation

    async def finish_control(self, operation_id, verified, provider):
        intent = await self._pinned_intent(operation_id, verified)
        operation = await self.operation(operation_id)
        binding = await self.binding(operation.binding_id)
        if (binding.generation != intent.binding_generation or binding.connection_ref != verified["connection_ref"]
                or binding.provider_identity is None
                or any(binding.provider_identity.get(field) != verified[field]
                       for field in ("account_id", "role_arn", "region"))):
            raise HTTPException(409, detail={"code": "stale_binding_generation"})
        if operation.state != "accepted":
            return operation
        claimed = await self.session.execute(update(BindingOperation).where(
            BindingOperation.tenant_id == self.tenant_id, BindingOperation.operation_id == operation_id,
            BindingOperation.state == "accepted",
        ).values(state="running", revision=BindingOperation.revision + 1,
                 attempts=BindingOperation.attempts + 1), execution_options={"synchronize_session": False})
        await self.session.commit()
        if claimed.rowcount != 1:
            return await self.operation(operation_id)
        operation = await self.operation(operation_id)
        action = intent.request["action"]
        target_state = binding.state
        readiness = binding.readiness
        gaps = binding.gaps
        try:
            if (self.service_id == "well_architected" and action != "cleanup"
                    and await active_generation(self.session, self.tenant_id, binding_id=binding.binding_id)):
                raise ValueError("native_generation_active")
            if action in {"check", "update", "resume"}:
                if self.service_id == "devops" and intent.request.get("resource_selection") is not None:
                    native = await provider.verify_resource(binding, verified)
                elif self.service_id == "security":
                    native = await provider.read(binding.native_resource_ref, verified, binding=binding,
                                                 selected_model=intent.request.get("native_model_ref"))
                else:
                    native = await provider.read(binding.native_resource_ref, verified)
                if (native.get("native_resource_ref") != binding.native_resource_ref
                        or any(native.get("provider_identity", {}).get(field) != verified[field]
                               for field in ("account_id", "role_arn", "region"))):
                    raise ValueError("native_scope_mismatch")
                target_state = native["state"]
                readiness = native.get("readiness", [])
                gaps = native.get("gaps", [])
            elif action == "cleanup":
                if intent.owned_resource_receipt != binding.creation_receipt:
                    raise ValueError("ownership_required")
                if self.service_id == "well_architected":
                    await self._wa_cleanup_safe(binding, operation_id)
                if self.service_id == "transform":
                    receipt = await provider.cleanup(binding.native_resource_ref, verified,
                                                     intent.owned_resource_receipt, intent)
                    evidence = receipt.get("absence_evidence_ref") if isinstance(receipt, dict) else None
                    reference = binding.native_resource_ref
                    if (not isinstance(receipt, dict) or receipt.get("absent") is not True
                        or receipt.get("intent_hash") != intent.intent_hash
                        or receipt.get("operation_id") != intent.operation_id
                        or receipt.get("creation_receipt") != intent.owned_resource_receipt
                        or receipt.get("native_resource_ref") != reference
                        or receipt.get("provider_identity") != binding.provider_identity
                        or not isinstance(evidence, dict)
                        or evidence.get("tenant_id") != self.tenant_id
                        or evidence.get("project_id") != self.project_id
                        or evidence.get("record_type") != "transform_absence"
                        or evidence.get("record_id") != reference.get("record_id")
                        or not evidence.get("record_revision")):
                        raise ValueError("native_absence_unverified")
                    operation.cleanup_receipt = receipt
                else:
                    absent = await provider.cleanup(binding.native_resource_ref, verified, intent.owned_resource_receipt)
                    if not absent:
                        raise ValueError("native_absence_unverified")
                    if self.service_id == "well_architected":
                        operation.cleanup_receipt = {
                            "schema_version": 1, "source": "installed-worker", "absent": True,
                            "operation_id": intent.operation_id, "intent_hash": intent.intent_hash,
                            "creation_receipt": intent.owned_resource_receipt,
                            "native_resource_ref": binding.native_resource_ref,
                            "provider_identity": binding.provider_identity,
                        }
                target_state = "cleaned"
            elif action == "unlink":
                target_state = "unlinked"
        except (ConnectionError, TimeoutError, httpx.TransportError, ValueError, PermissionError) as error:
            operation.state = "failed"
            operation.gaps = ([str(error)] if isinstance(error, ValueError) else
                              ["native_scope_mismatch"] if isinstance(error, PermissionError) else
                              ["native_readback_unavailable"])
            if action == "cleanup":
                target_state = "cleanup_failed"
            elif self.service_id == "well_architected" and str(error) == "native_generation_active":
                target_state = "suspended"
            operation.revision += 1
            work = await self.session.scalar(select(SetupWork).where(
                SetupWork.tenant_id == self.tenant_id, SetupWork.operation_id == operation_id))
            work.blocked_at = datetime.now(UTC)
            work.last_error = operation.gaps[0]
            changed = await self.session.execute(update(Binding).where(
                Binding.tenant_id == self.tenant_id, Binding.binding_id == binding.binding_id,
                Binding.generation == intent.binding_generation,
            ).values(state=target_state, revision=Binding.revision + 1),
                execution_options={"synchronize_session": False})
            if changed.rowcount != 1:
                operation.gaps = ["stale_binding_generation"]
            await self.session.commit()
            return operation
        changed = await self.session.execute(update(Binding).where(
            Binding.tenant_id == self.tenant_id, Binding.binding_id == binding.binding_id,
            Binding.generation == intent.binding_generation,
        ).values(state=target_state, readiness=readiness, gaps=gaps, revision=Binding.revision + 1),
            execution_options={"synchronize_session": False})
        operation.state = "succeeded" if changed.rowcount == 1 else "blocked"
        operation.gaps = [] if changed.rowcount == 1 else ["stale_binding_generation"]
        if changed.rowcount == 1 and self.service_id == "devops" and intent.request.get("resource_selection") and gaps:
            operation.state, operation.gaps = "blocked", gaps
        operation.revision += 1
        work = await self.session.scalar(select(SetupWork).where(
            SetupWork.tenant_id == self.tenant_id, SetupWork.operation_id == operation_id))
        if changed.rowcount == 1:
            if self.service_id == "devops" and action == "check":
                from l5_services.devops.setup import record_setup

                await record_setup(self.session, intent, binding, native)
            work.completed_at = datetime.now(UTC)
        else:
            work.blocked_at = datetime.now(UTC)
            work.last_error = "stale_binding_generation"
        await self.session.commit()
        return operation


def create_bindings_router(backend_dependency):
    router = APIRouter(tags=["l5-security"])

    @router.get("/bindings/{binding_id}/cleanup-status", operation_id="getOwnedCleanupStatus")
    async def cleanup_status(product_id: str, project_id: str, service_id: str, binding_id: str, response: Response,
                             backend=Depends(backend_dependency)):
        if service_id not in {"security", "well_architected"}:
            raise HTTPException(404, detail={"code": "not_found"})
        result = await backend.get_cleanup_status(product_id, project_id, service_id, binding_id)
        response.headers.update({"Cache-Control": "no-store", "ETag": f'"{result["revision"]}"'})
        return result

    @router.post("/binding-operations", operation_id="startBindingOperation", status_code=202)
    async def start_binding(product_id: str, project_id: str, service_id: str, body: dict, response: Response,
                            idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
                            if_match: str | None = Header(None, alias="If-Match"),
                            backend=Depends(backend_dependency)):
        if service_id not in {"security", "devops", "finops", "well_architected", "transform"}:
            raise HTTPException(404, detail={"code": "not_found"})
        if not idempotency_key or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", idempotency_key) is None:
            raise HTTPException(428 if not idempotency_key else 422, detail={"code": "precondition_required" if not idempotency_key else "validation_error"})
        if service_id == "finops" and body.get("action") not in {"create", "link", "unlink", "cleanup"}:
            raise HTTPException(422, detail={"code": "validation_error"})
        if service_id == "finops" and body.get("action") in {"unlink", "cleanup"}:
            required = {"action", "configuration_revision", "binding_id"}
            if body["action"] == "cleanup":
                required.add("owned_cleanup_confirmed")
            if set(body) != required or (body["action"] == "cleanup" and body["owned_cleanup_confirmed"] is not True):
                raise HTTPException(422, detail={"code": "validation_error"})
        operation = await backend.start_binding(product_id, project_id, service_id, body, idempotency_key, if_match)
        response.headers["ETag"] = f'"{operation.revision}"'
        response.headers["Cache-Control"] = "no-store"
        return view_operation(operation)

    @router.get("/binding-operations/{operation_id}", operation_id="getBindingOperation")
    async def get_operation(product_id: str, project_id: str, service_id: str, operation_id: str, response: Response,
                            backend=Depends(backend_dependency)):
        if service_id not in {"security", "devops", "finops", "well_architected", "transform"}:
            raise HTTPException(404, detail={"code": "not_found"})
        operation = await backend.get_binding_operation(product_id, project_id, service_id, operation_id)
        response.headers.update({"Cache-Control": "no-store", "ETag": f'"{operation.revision}"'})
        return view_operation(operation)

    @router.post("/binding-operations/{operation_id}/reconcile", operation_id="reconcileBindingOperation", status_code=202)
    async def reconcile_binding(product_id: str, project_id: str, service_id: str, operation_id: str,
                                body: dict, response: Response,
                                idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
                                backend=Depends(backend_dependency)):
        if service_id not in {"security", "devops", "finops", "well_architected", "transform"}:
            raise HTTPException(404, detail={"code": "not_found"})
        if not idempotency_key or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", idempotency_key) is None:
            raise HTTPException(428 if not idempotency_key else 422,
                                detail={"code": "precondition_required" if not idempotency_key else "validation_error"})
        if set(body) != {"reason"} or not isinstance(body["reason"], str) or not 1 <= len(body["reason"].strip()) <= 1000:
            raise HTTPException(422, detail={"code": "validation_error"})
        operation = await backend.reconcile_binding(product_id, project_id, service_id, operation_id,
                                                     body["reason"], idempotency_key)
        response.headers["ETag"] = f'"{operation.revision}"'
        response.headers["Cache-Control"] = "no-store"
        return view_operation(operation)

    @router.get("/bindings", operation_id="listBindings")
    async def list_bindings(product_id: str, project_id: str, service_id: str, response: Response,
                            backend=Depends(backend_dependency)):
        if service_id not in {"security", "devops", "finops", "well_architected", "transform"}:
            raise HTTPException(404, detail={"code": "not_found"})
        bindings = await backend.list_bindings(product_id, project_id, service_id)
        response.headers["ETag"] = f'"{_digest([(item.binding_id, item.revision) for item in bindings])}"'
        response.headers["Cache-Control"] = "no-store"
        return {"items": [view_binding(binding) for binding in bindings]}

    @router.get("/bindings/{binding_id}", operation_id="getBinding")
    async def get_binding(product_id: str, project_id: str, service_id: str, binding_id: str, response: Response,
                          backend=Depends(backend_dependency)):
        if service_id not in {"security", "devops", "finops", "well_architected", "transform"}:
            raise HTTPException(404, detail={"code": "not_found"})
        binding = await backend.get_binding(product_id, project_id, service_id, binding_id)
        response.headers["ETag"] = f'"{binding.revision}"'
        response.headers["Cache-Control"] = "no-store"
        return view_binding(binding)

    return router
