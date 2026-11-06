"""Atomic admission of verified immutable inputs and owed execution work."""

import json
import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from l5_services.devops.release_prerequisites import release_eligible, selected_repository
from l5_services.devops.test_profiles import require_qualified_test_profile, selected_test_profile
from l5_services.devops.verification_targets import (
    verification_description,
    verify_target,
)
from l5_services.dispatcher import dispatch_work
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration
from l5_services.models.preparation import AdmissionPreparation
from l5_services.models.receipt import RunReceipt
from l5_services.models.run import Run, view_run
from l5_services.models.work import Work
from l5_services.run_access import RunStore
from l5_services.run_input import validate_run_input
from l5_services.wa_fence import active_generation


FINOPS_AUTHORITY_FIELDS = frozenset({
    "scope", "service_id", "actor_id", "actor_type", "configuration_id", "configuration_revision",
    "configuration_hash", "configuration_enabled", "binding_id", "binding_generation",
    "binding_state", "connection_ref", "binding_connection_ref", "delegation_id",
    "delegation_version", "delegation_active", "native_qualified", "native_agent_ref",
    "native_agent_display_name", "provider_identity", "billing_scope", "profile_ref", "trigger", "adapter_revision", "visibility", "classification",
    "causal_origin", "exclusions", "max_bytes", "max_files", "monitoring_policy", "limits", "event_id",
})


async def persist_admission(session: AsyncSession, run: Run, receipt: RunReceipt, work: Work) -> None:
    if not (run.tenant_id == receipt.tenant_id == work.tenant_id
            and run.run_id == receipt.run_id == work.run_id):
        raise ValueError("run, receipt and work must describe the same scoped admission")
    try:
        if run.service_id == "well_architected":
            locked = await session.execute(update(Configuration).where(
                Configuration.tenant_id == run.tenant_id,
                Configuration.configuration_id == run.configuration_id,
                Configuration.product_id == run.product_id,
                Configuration.project_id == run.project_id,
                Configuration.service_id == run.service_id,
                Configuration.current_revision == run.configuration_revision,
            ).values(current_revision=Configuration.current_revision))
            if locked.rowcount != 1:
                raise HTTPException(409, detail={"code": "stale_revision"})
            locked = await session.execute(update(Binding).where(
                Binding.tenant_id == run.tenant_id,
                Binding.binding_id == run.binding_id,
                Binding.configuration_id == run.configuration_id,
                Binding.service_id == run.service_id,
                Binding.generation == run.binding_generation,
                Binding.state == "ready",
            ).values(generation=Binding.generation))
            if locked.rowcount != 1:
                raise HTTPException(409, detail={"code": "stale_binding_generation"})
            if await active_generation(session, run.tenant_id, configuration_id=run.configuration_id):
                raise HTTPException(409, detail={"code": "native_generation_active"})
        session.add(run)
        await session.flush()
        if run.service_id == "well_architected" and (run.admission or {}).get("source_snapshots"):
            from l5_services.source_retention import RetentionError, pin_source_hold

            retention = run.admission.get("source_retention") or {}
            try:
                required_until = _instant(retention.get("required_until"))
                policy_expires_at = _instant(retention.get("policy_expires_at"))
                for snapshot in run.admission["source_snapshots"]:
                    for name in ("content", "file_manifest"):
                        await pin_source_hold(session, run, snapshot[name], snapshot=snapshot,
                                              required_until=required_until, policy_expires_at=policy_expires_at,
                                              now=datetime.now(UTC))
            except (RetentionError, HTTPException, KeyError, TypeError) as error:
                raise HTTPException(409, detail={"code": "source_retention_unavailable"}) from error
        if receipt.operation == "retry":
            receipt.response = view_run(run)
        session.add_all((receipt, work))
        await session.commit()
    except Exception:
        await session.rollback()
        raise


def _reject_source():
    raise HTTPException(422, detail={"code": "immutable_source_required"})


def _instant(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC)
    except (AttributeError, TypeError, ValueError):
        pass
    _reject_source()


def _exact_sources(run_input, scope):
    if run_input.get("capability") == "release_readiness" and run_input.get("service_id") == "devops":
        options = run_input["devops_input"]
        head = options["repository_ref"]
        base = {**head, "record_revision": options["base_revision"]}
        if any(source["tenant_id"] != scope["tenant_id"] or source["project_id"] != scope["project_id"]
               for source in (base, head)):
            _reject_source()
        return [base, head]
    if run_input.get("service_id") == "devops":
        return []
    if run_input.get("service_id") == "finops" and not run_input.get("sources"):
        return []
    sources = run_input.get("sources")
    capability = run_input.get("capability")
    source_type = {"threat_model": "document", "code_review": "repository_commit"}.get(capability)
    if run_input.get("service_id") == "transform":
        source_type = "repository_commit"
    if run_input.get("service_id") == "well_architected":
        source_type = {"document", "repository_commit"}
    finops_context = run_input.get("service_id") == "finops" and capability == "cost_inquiry"
    if (source_type is None and not finops_context
            or not isinstance(sources, list) or not 1 <= len(sources) <= 100):
        _reject_source()
    keys = ("tenant_id", "project_id", "source_connection_id", "record_type", "record_id", "record_revision")
    for source in sources:
        if (not isinstance(source, dict) or set(source) != set(keys)
                or any(not isinstance(source[key], str) or not source[key].strip() for key in keys)
                or source["tenant_id"] != scope["tenant_id"] or source["project_id"] != scope["project_id"]
                or source["record_type"] not in ({"document", "repository_commit"} if finops_context else
                                                 source_type if isinstance(source_type, set) else {source_type})
                or source["record_revision"].lower() in {"head", "latest", "null"}):
            _reject_source()
    if len({json.dumps(source, sort_keys=True) for source in sources}) != len(sources):
        _reject_source()
    pr_base, pr_head = run_input.get("pr_base"), run_input.get("pr_head")
    if (pr_base is None) != (pr_head is None) or (pr_base is not None and (
        capability != "code_review" or pr_base not in {source["record_revision"] for source in sources}
        or pr_head not in {source["record_revision"] for source in sources}
    )):
        _reject_source()
    return sources


def _verify_snapshot(snapshot, source, exclusions, deadline):
    if (not isinstance(snapshot, dict) or snapshot.get("source_ref") != source
            or snapshot.get("exclusions") != exclusions or not snapshot.get("snapshot_id")
            or _instant(snapshot.get("expires_at")) < deadline):
        _reject_source()
    for field in ("content", "file_manifest"):
        artifact = snapshot.get(field)
        if (not isinstance(artifact, dict) or not artifact.get("artifact_id") or not artifact.get("version")
                or not artifact.get("media_type") or not isinstance(artifact.get("bytes"), int)
                or artifact["bytes"] < 0 or not isinstance(artifact.get("sha256"), str)
                or len(artifact["sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in artifact["sha256"])):
            _reject_source()
    if not isinstance(snapshot.get("file_count"), int) or not 1 <= snapshot["file_count"] <= 100000:
        _reject_source()


def _validate_target(run_input, verified):
    trigger = verified.get("trigger")
    scope = verified.get("scope") or {}
    if verified.get("service_id") == "devops":
        options = run_input.get("devops_input") or {}
        if run_input.get("capability") == "release_verification" and "test_profile_ref" in options:
            selected_test_profile(options["test_profile_ref"], verified)
        if run_input.get("capability") == "release_readiness" and isinstance(trigger, dict):
            if (not selected_repository(trigger, options.get("repository_ref"))
                    or not release_eligible(options, verified.get("release_eligibility"), scope,
                                            verified.get("connection_ref") or {}, verified.get("native_space_ref") or {})):
                raise HTTPException(409, detail={"code": "release_prerequisite_unavailable"})
        resources = options.get("resource_refs") or []
        telemetry = options.get("telemetry_refs") or []
        if (run_input.get("service_id") != "devops"
                or any((reference["tenant_id"], reference["project_id"])
                       != (scope.get("tenant_id"), scope.get("project_id"))
                       for reference in resources + telemetry)
                or not isinstance(trigger, dict)
                or (run_input.get("capability") in {"investigation", "release_verification"}
                    and trigger.get("resource_refs") != resources)
                or (run_input.get("capability") == "release_readiness" and not selected_repository(trigger, options.get("repository_ref")))
                or (run_input.get("capability") in {"investigation", "release_verification"}
                    and trigger.get("telemetry_refs") != telemetry)):
            raise HTTPException(404, detail={"code": "not_found"})
    elif verified.get("service_id") == "well_architected":
        profile = verified.get("native_profile_ref") or {}
        if (run_input.get("service_id") != "well_architected"
                or run_input.get("capability") != "architecture_review"
                or profile.get("record_type") != "agent_profile"
                or profile.get("tenant_id") != scope.get("tenant_id")
                or profile.get("project_id") != scope.get("project_id")
                or profile.get("record_revision") != verified.get("profile_hash")
                or not isinstance(profile.get("record_id"), str)
                or not profile["record_id"]
                or verified.get("generation_scope", {}).get("pillars") is None
                or not verified.get("generation_model_hash")
                or not verified.get("native_creator")
                or not isinstance(verified.get("architecture_coverage"), dict)
                or not isinstance(verified.get("source_retention"), dict)
                or run_input.get("sources") != verified.get("source_refs")):
            raise HTTPException(409, detail={"code": "native_unqualified"})
    elif verified.get("service_id") == "finops":
        agent = verified.get("native_agent_ref") or {}
        if (run_input.get("service_id") != "finops" or run_input.get("capability") != "cost_inquiry"
                or verified.get("native_qualified") is not True
                or agent.get("record_type") != "finops_agent"
                or agent.get("tenant_id") != scope.get("tenant_id")
                or agent.get("project_id") != scope.get("project_id")
                or not agent.get("record_id")
                or "native_agent_display_name" in verified and (
                    not isinstance(verified["native_agent_display_name"], str)
                    or not verified["native_agent_display_name"].strip())):
            raise HTTPException(409, detail={"code": "native_unqualified"})
    elif verified.get("service_id") == "transform":
        from pydantic import ValidationError

        from l5_services.adapters.transform.qualification import ProviderQualification
        from l5_services.transform_contract import TransformSettings

        try:
            qualification = ProviderQualification.model_validate(verified.get("transform_qualification"))
            settings = TransformSettings.model_validate(verified.get("transform_settings"))
        except (ValidationError, TypeError, ValueError):
            raise HTTPException(409, detail={"code": "native_unqualified"}) from None
        if not qualification.ready_for_submission:
            raise HTTPException(409, detail={"code": "native_unqualified"})
        identity = verified.get("provider_identity")
        if (not isinstance(identity, dict)
                or not isinstance(identity.get("account_id"), str)
                or not re.fullmatch(r"[0-9]{12}", identity["account_id"])
                or not isinstance(identity.get("role_arn"), str)
                or not re.fullmatch(rf"arn:aws:iam::{identity['account_id']}:role/.+", identity["role_arn"])
                or identity.get("region") != settings.region):
            raise HTTPException(409, detail={"code": "native_unqualified"})
        from l5_services.adapters.transform.baseline import pinned_test_policy

        try:
            pinned_test_policy(verified)
        except (ValueError, TypeError):
            raise HTTPException(409, detail={"code": "transform_test_policy_unavailable"}) from None
        if (run_input["sources"][0] not in verified.get("source_refs", [])
                or settings.connection_ref.model_dump() != verified.get("connection_ref")
                or settings.executor_binding_ref.model_dump() != verified.get("qualified_executor_ref")
                or run_input["capability"] not in verified.get("capabilities", [])
                or trigger.get("source_refs") != run_input["sources"]):
            raise HTTPException(409, detail={"code": "transform_readiness_unavailable"})
    elif verified.get("service_id") != "security" or run_input.get("service_id", "security") != "security":
        raise HTTPException(404, detail={"code": "not_found"})
    if verified.get("configuration_enabled") is not True:
        raise HTTPException(409, detail={"code": "configuration_disabled"})
    if not isinstance(trigger, dict) or trigger.get("enabled") is not True:
        raise HTTPException(409, detail={"code": "trigger_disabled"})
    if (trigger.get("capability") != run_input.get("capability")
            or trigger.get("binding_id") != run_input.get("binding_id")
            or (verified.get("service_id") == "security"
                and trigger.get("source_refs") != run_input.get("sources"))):
        raise HTTPException(404, detail={"code": "not_found"})
    kind = trigger.get("kind")
    if (kind not in ({"manual", "pull_request", "document_revision", "schedule"}
                    | ({"native_event"} if verified.get("service_id") == "devops" else set()))
            or (kind == "manual") != (verified.get("event_id") is None)
            or verified.get("actor_type") != ("human" if kind == "manual" else "service")):
        raise HTTPException(409, detail={"code": "trigger_mismatch"})
    if verified.get("binding_state") != "ready" or not verified.get("delegation_active"):
        raise HTTPException(409, detail={"code": "binding_or_delegation_unavailable"})
    if verified.get("connection_ref") != verified.get("binding_connection_ref"):
        raise HTTPException(409, detail={"code": "binding_or_delegation_unavailable"})
    if run_input.get("capability") in {"code_review", "threat_model"}:
        space = verified.get("native_space_ref") or {}
        identity = verified.get("provider_identity") or {}
        registration = verified.get("source_registration") or {}
        if (kind not in {"manual", "pull_request", "schedule"}
                or kind == "pull_request" and (verified.get("event_kind") != kind
                or verified.get("event_source_ref") != run_input["sources"][-1])
                or kind == "schedule" and (verified.get("event_kind") != kind
                                           or verified.get("event_source_ref") is not None)
                or kind == "manual" and verified.get("event_source_ref") is not None):
            raise HTTPException(404, detail={"code": "not_found"})
        if run_input["capability"] == "threat_model":
            model = verified.get("native_model_ref") or {}
            if (kind not in {"manual", "document_revision", "schedule"}
                    or run_input["sources"][0].get("record_type") != "document"
                    or model.get("record_type") != "threat_model" or not model.get("record_id")
                    or not model.get("record_revision")
                    or any(model.get(field) != space.get(field) or not model.get(field)
                           for field in ("tenant_id", "project_id", "source_connection_id"))):
                raise HTTPException(409, detail={"code": "native_model_unavailable"})
        if (space.get("tenant_id") != scope.get("tenant_id")
                or space.get("project_id") != scope.get("project_id")
                or space.get("source_connection_id") != verified["connection_ref"]["source_connection_id"]
                or space.get("record_type") != "security_space" or not space.get("record_id")
                or identity.get("region") != "us-east-1"
                or not all(identity.get(field) for field in ("account_id", "role_arn"))
                or not registration.get("service_role") or not registration.get("bucket")):
            raise HTTPException(409, detail={"code": "source_readiness_unverified"})


async def admit_run(
    session: AsyncSession, source_client, run_input: dict, verified: dict, idempotency_key: str,
    *, now: datetime | None = None, retry_of: str | None = None, reason: str | None = None,
    budget_guard=None, verification_reader=None, replay_only=False,
) -> Run:
    """Admit a request after the caller has verified scope, binding and delegation."""
    verification_clock = None if now is not None else lambda: datetime.now(UTC)
    now = now or datetime.now(UTC)
    validate_run_input(run_input, allow_historical=True)
    if run_input.get("service_id") == "finops":
        agent = verified.get("native_agent_ref") or {}
        billing = verified.get("billing_scope") or {}
        identity = verified.get("provider_identity")
        if (set(verified) - FINOPS_AUTHORITY_FIELDS
                or not isinstance(identity, dict)
                or set(identity) != {"account_id", "role_arn", "region"}
                or any(not isinstance(value, str) for value in identity.values())
                or re.fullmatch(r"[0-9]{12}", identity["account_id"]) is None
                or re.fullmatch(rf"arn:aws:iam::{identity['account_id']}:role/[A-Za-z0-9_+=,.@/-]+", identity["role_arn"]) is None
                or re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", identity["region"]) is None
                or not isinstance(agent, dict)
                or set(agent) - {"tenant_id", "project_id", "source_connection_id",
                                 "record_type", "record_id", "record_revision"}
                or any(not isinstance(value, str) or not value for value in agent.values())
                or not isinstance(billing, dict)
                or set(billing) - {"account_ids", "view_ref", "filter_digest", "role_policy_digest",
                                   "observed_at", "scope_generation", "scope_digest", "snapshot_artifact"}
                or type(billing.get("scope_generation")) is not int
                or any(not isinstance(value, str) or not value for key, value in billing.items()
                       if key not in {"scope_generation", "account_ids", "snapshot_artifact"})
                or ("snapshot_artifact" in billing and (
                    not isinstance(billing["snapshot_artifact"], dict)
                    or set(billing["snapshot_artifact"]) != {"artifact_id", "version", "sha256"}
                    or any(not isinstance(value, str) or not value
                           for value in billing["snapshot_artifact"].values())
                    or len(billing["snapshot_artifact"]["sha256"]) != 64
                    or any(character not in "0123456789abcdef"
                           for character in billing["snapshot_artifact"]["sha256"])))
                or ("account_ids" in billing and (not isinstance(billing["account_ids"], list)
                    or any(not isinstance(account, str) or not account for account in billing["account_ids"])))):
            raise HTTPException(409, detail={"code": "native_unqualified"})
    scope = verified["scope"]
    tenant_id, product_id, project_id = (scope[field] for field in ("tenant_id", "product_id", "project_id"))
    service_id, actor_id = verified["service_id"], verified["actor_id"]
    operation = "retry" if retry_of is not None else "submit"
    receipt_key = (tenant_id, product_id, project_id, service_id, actor_id, operation, idempotency_key)
    request_hash = sha256(json.dumps({
        "input": run_input, "event_id": verified.get("event_id"),
        "exclusions": verified.get("exclusions", []),
        **({"run_id": retry_of, "reason": reason} if retry_of is not None else {}),
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    async def replay():
        receipt = await session.get(RunReceipt, receipt_key)
        if receipt is None:
            return None
        if receipt.request_hash != request_hash:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        run = await session.get(Run, (tenant_id, receipt.run_id))
        if run is None:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        return run

    previous = await replay()
    if previous is None:
        if (run_input.get("capability") == "release_verification"
                and "test_profile_ref" not in run_input["devops_input"]):
            raise HTTPException(409, detail={"code": "native_test_profile_required",
                "message": "Select the native API test profile and exact version in the manual trigger before creating a new verification run."})
        validate_run_input(run_input)
    elif run_input.get("capability") == "release_verification":
        if (previous.input != run_input or previous.admission["scope"] != scope
                or previous.configuration_id != verified["configuration_id"]
                or verified.get("configuration_enabled") is not True
                or verified.get("binding_state") != "ready"
                or verified.get("delegation_active") is not True
                or verified.get("actor_type") != "human" or actor_id != scope.get("owner_id")):
            raise HTTPException(404, detail={"code": "not_found"})
    if replay_only and previous is None:
        raise HTTPException(409, detail={"code": "l5_effect_existing_replay_required"})
    preparation = await session.get(AdmissionPreparation, receipt_key)
    if preparation is not None and preparation.request_hash != request_hash:
        raise HTTPException(409, detail={"code": "idempotency_conflict"})
    _validate_target(run_input, verified)
    if retry_of is not None:
        store = RunStore(session, tenant_id, product_id, project_id, service_id, actor_id,
                         scope["relationship_revision"])
        original = await store.owned(retry_of)
        if (original.input != run_input or original.configuration_id != verified["configuration_id"]
                or original.binding_generation != verified["binding_generation"]
                or any(original.admission[field] != verified[field] for field in (
                    "connection_ref", "delegation_id", "delegation_version", "exclusions",
                ))):
            raise HTTPException(409, detail={"code": "stale_revision"})
        if previous is None:
            await store.retryable(retry_of)
    if previous is not None:
        return previous
    run_id = str(uuid4())
    verification = None
    if run_input.get("capability") == "release_verification":
        verification = await verify_target(
            run_input["devops_input"], scope, verification_reader, now=now,
            deadline=now + timedelta(seconds=verified["limits"]["deadline_seconds"]),
            clock=verification_clock,
        )
        try:
            verification_description(scope, verification, run_input["devops_input"]["scenarios"],
                                     run_id=run_id, attempt_id=str(uuid4()), token="0" * 64,
                                     test_profile_ref=run_input["devops_input"]["test_profile_ref"])
        except ValueError:
            raise HTTPException(422, detail={"code": "verification_description_too_large"}) from None
        require_qualified_test_profile()
    sources = _exact_sources(run_input, scope)
    if service_id == "security" and run_input.get("capability") == "code_review":
        from l5_services.source_archive import verify_review_selection
        from l5_services.source_manifest import SourceIntegrityError

        try:
            verify_review_selection(SimpleNamespace(input=run_input, tenant_id=tenant_id, project_id=project_id))
        except SourceIntegrityError:
            raise HTTPException(422, detail={"code": "immutable_source_required"}) from None
    if (run_input.get("configuration_revision") != verified["configuration_revision"]
            or run_input.get("binding_id") != verified["binding_id"]):
        raise HTTPException(409, detail={"code": "stale_revision"})
    if preparation is None:
        preparation = AdmissionPreparation(
            tenant_id=tenant_id, product_id=product_id, project_id=project_id, service_id=service_id,
            actor_id=actor_id, operation=operation, idempotency_key=idempotency_key,
            request_hash=request_hash, verified=verified,
            deadline_at=now + timedelta(seconds=verified["limits"]["deadline_seconds"]),
        )
        session.add(preparation)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            preparation = await session.get(AdmissionPreparation, receipt_key)
            if preparation is None:
                raise
    if preparation.request_hash != request_hash:
        raise HTTPException(409, detail={"code": "idempotency_conflict"})
    if preparation.verified != verified:
        raise HTTPException(409, detail={"code": "stale_revision"})
    # Explicit human investigations need no organization-budget setup. Identity,
    # workload consent, trigger, deadline and replay checks above remain required.
    manual_investigation = (service_id == "devops" and run_input.get("capability") == "investigation"
                            and verified["trigger"].get("kind") == "manual")
    if service_id in {"devops", "finops", "transform", "well_architected"} and not manual_investigation:
        if budget_guard is None:
            raise HTTPException(503, detail={"code": "budget_authority_unavailable"})
        previous = await budget_guard(session, verified["limits"], scope, now, replay)
        if previous is not None:
            await session.commit()
            return previous
    deadline = preparation.deadline_at.replace(tzinfo=preparation.deadline_at.tzinfo or UTC)
    if deadline <= now:
        raise HTTPException(410, detail={"code": "source_expired"})
    exclusions = verified.get("exclusions", [])
    snapshots = []
    for index, source in enumerate(sources):
        capture_request = {
            "delegation_id": verified["delegation_id"],
            "delegation_revision": verified["delegation_version"],
            "source_ref": source, "exclusions": exclusions,
            "max_bytes": verified["max_bytes"], "max_files": verified["max_files"],
            "retain_until": (verified["source_retention"]["required_until"] if service_id == "well_architected"
                             else deadline.isoformat().replace("+00:00", "Z")),
        }
        snapshot = await source_client.pin_source(
            product_id, project_id, capture_request,
            idempotency_key=sha256(f"{receipt_key}:{index}".encode()).hexdigest(),
        )
        _verify_snapshot(snapshot, source, exclusions, deadline)
        snapshots.append(snapshot)
    fields = (
        "scope", "profile_ref", "connection_ref", "delegation_id", "delegation_version",
        "binding_generation", "actor_id", "configuration_hash", "adapter_revision", "limits",
        "visibility", "classification", "causal_origin", "exclusions", "monitoring_policy",
    )
    admission = {field: verified[field] for field in fields}
    if service_id == "devops":
        admission["service_id"] = "devops"
        admission["native_space_ref"] = verified["native_space_ref"]
        admission["provider_identity"] = verified["provider_identity"]
        if verified.get("event_id") is not None:
            admission["event_source_ref"] = verified["event_source_ref"]
            admission["event_kind"] = verified["event_kind"]
        if verification is not None:
            admission["verification"] = verification
        if run_input["capability"] == "release_readiness":
            admission["release_eligibility"] = verified["release_eligibility"]
    elif service_id == "well_architected":
        if verified.get("event_kind") == "schedule":
            admission.update(actor_type="service", event_kind="schedule")
        coverage_reader = getattr(source_client, "architecture_coverage", None)
        coverage = (await coverage_reader(snapshots) if callable(coverage_reader)
                    else verified["architecture_coverage"])
        admission.update(service_id=service_id, native_profile_ref=verified["native_profile_ref"],
                         profile_hash=verified["profile_hash"], source_retention=verified["source_retention"],
                         architecture_coverage=coverage,
                         provider_identity=verified.get("provider_identity"),
                         goal_refs=verified.get("goal_refs", []),
                         generation_scope=verified["generation_scope"],
                         generation_context=verified["generation_context"],
                         generation_model_hash=verified["generation_model_hash"],
                         generation_creator=verified["native_creator"])
    elif service_id == "finops":
        admission["service_id"] = "finops"
        admission["provider_identity"] = verified["provider_identity"]
        admission["native_agent_ref"] = verified["native_agent_ref"]
        if "native_agent_display_name" in verified:
            admission["native_agent_display_name"] = verified["native_agent_display_name"]
        admission["billing_scope"] = verified["billing_scope"]
    elif service_id == "transform":
        admission["service_id"] = "transform"
        admission["actor_type"] = verified["actor_type"]
        admission["qualified_executor_ref"] = verified["qualified_executor_ref"]
        if verified.get("event_kind") == "schedule":
            admission["event_kind"] = "schedule"
        admission["provider_identity"] = verified["provider_identity"]
        admission["transform_settings"] = verified["transform_settings"]
        admission["transform_qualification"] = verified["transform_qualification"]
        admission["transform_test_policy"] = verified["transform_test_policy"]
        if run_input["transform_input"]["goal"]["kind"] == "dependency_upgrade":
            admission["transform_artifact_hashes"] = verified.get("transform_artifact_hashes")
    elif service_id == "security":
        admission["native_space_ref"] = verified["native_space_ref"]
        admission["provider_identity"] = verified["provider_identity"]
        if run_input["capability"] == "code_review":
            admission["source_registration"] = verified["source_registration"]
            admission["event_kind"] = verified["trigger"]["kind"]
            admission["event_source_ref"] = verified.get("event_source_ref")
        else:
            admission["native_model_ref"] = verified["native_model_ref"]
    admission.update(admitted_at=now.isoformat().replace("+00:00", "Z"),
                     expires_at=deadline.isoformat().replace("+00:00", "Z"), source_snapshots=snapshots)
    if verified.get("event_id") is not None:
        admission["event_id"] = verified["event_id"]
    run = Run(
        tenant_id=tenant_id, run_id=run_id, product_id=product_id, project_id=project_id, service_id=service_id,
        configuration_id=verified["configuration_id"], configuration_revision=verified["configuration_revision"],
        # Activation permits one manual investigation trigger without requiring
        # callers to manufacture an internal identifier. Keep this default local
        # to persistence so existing admission preparations remain replayable.
        binding_id=verified["binding_id"],
        trigger_id=(verified["trigger"].get("trigger_id") or "manual-investigation")
        if manual_investigation else verified["trigger"]["trigger_id"],
        binding_generation=verified["binding_generation"],
        input=run_input, admission=admission, deadline_at=deadline, retry_of=retry_of,
        capture_limits={"max_bytes": verified["max_bytes"], "max_files": verified["max_files"]},
    )
    receipt = RunReceipt(
        tenant_id=tenant_id, product_id=product_id, project_id=project_id, service_id=service_id,
        actor_id=actor_id, operation=operation, idempotency_key=idempotency_key,
        request_hash=request_hash, run_id=run_id, expires_at=deadline,
        request={"run_id": retry_of, "reason": reason} if retry_of is not None else None,
    )
    work = Work(tenant_id=tenant_id, work_id=str(uuid4()), run_id=run_id, kind="execute",
                generation=1, step=0, due_at=now)
    if retry_of is not None:
        original = await store.retryable(retry_of)
        if original.input != run_input:
            raise HTTPException(409, detail={"code": "stale_revision"})
    try:
        await persist_admission(session, run, receipt, work)
    except IntegrityError:
        previous = await replay()
        if previous is None:
            raise
        return previous
    return run


async def retry_and_dispatch(
    session, sessions, source_client, publish, verified, run_id, reason, idempotency_key, *, now=None,
    budget_guard=None, verification_reader=None,
):
    scope = verified["scope"]
    store = RunStore(session, scope["tenant_id"], scope["product_id"], scope["project_id"],
                     verified["service_id"], verified["actor_id"], scope["relationship_revision"])
    original = await store.owned(run_id)
    await submit_and_dispatch(session, sessions, source_client, publish, original.input, verified,
                              idempotency_key, now=now, retry_of=run_id, reason=reason,
                              budget_guard=budget_guard, verification_reader=verification_reader)
    receipt = await session.get(RunReceipt, store.receipt_key("retry", idempotency_key))
    return receipt.response


async def cancel_and_dispatch(store, sessions, publish, run_id, reason, idempotency_key, *, now=None):
    """Publish active cancellation after its durable intent commits."""
    response = await store.cancel_queued(run_id, reason, idempotency_key, now=now)
    try:
        async with sessions() as independent:
            work_id = await independent.scalar(select(Work.work_id).where(
                Work.tenant_id == store.tenant_id, Work.run_id == run_id,
                Work.kind == "cancel", Work.generation == 1,
                Work.step == response["revision"],
            ))
        if work_id is not None:
            await dispatch_work(sessions, store.tenant_id, work_id, publish, now=now)
    except Exception:
        pass
    return response


async def reconcile_and_dispatch(store, sessions, publish, run_id, reason, idempotency_key, *, now=None,
                                 recovery_authorization=None, recovery_request=None):
    extra = ({"recovery_authorization": recovery_authorization, "recovery_request": recovery_request}
             if recovery_request is not None else {})
    response = await store.reconcile(run_id, reason, idempotency_key, now=now, **extra)
    try:
        async with sessions() as independent:
            work_id = await independent.scalar(select(Work.work_id).where(
                Work.tenant_id == store.tenant_id, Work.run_id == run_id, Work.kind.in_(("reconcile", "execute", "poll", "collect")),
                Work.generation == 1, Work.step == response["revision"],
            ))
        if work_id is not None:
            await dispatch_work(sessions, store.tenant_id, work_id, publish, now=now)
    except Exception:
        pass
    return response


async def submit_and_dispatch(
    session: AsyncSession, sessions, source_client, publish, run_input: dict, verified: dict,
    idempotency_key: str, *, now: datetime | None = None,
    retry_of: str | None = None, reason: str | None = None, budget_guard=None, verification_reader=None,
    replay_only=False,
) -> Run:
    """Attempt immediate publication only after a durable admission or replay."""
    run = await admit_run(session, source_client, run_input, verified, idempotency_key, now=now,
                          retry_of=retry_of, reason=reason, budget_guard=budget_guard,
                          verification_reader=verification_reader, replay_only=replay_only)
    if replay_only:
        return run
    try:
        async with sessions() as independent:
            work_id = await independent.scalar(select(Work.work_id).where(
                Work.tenant_id == run.tenant_id, Work.run_id == run.run_id,
                Work.kind == "execute", Work.generation == 1, Work.step == 0,
            ))
        if work_id is not None:
            await dispatch_work(sessions, run.tenant_id, work_id, publish, now=now)
    except Exception:
        pass
    return run
