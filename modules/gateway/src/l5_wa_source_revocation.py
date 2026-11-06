"""Read an actual Product source unlink against an earlier owner selection."""

from copy import deepcopy
from datetime import UTC, datetime

from l5_services.models.evidence import AssessmentResult
from l5_services.models.result import ResultDetail
from l5_services.models.run import Run, view_run
from sqlalchemy import select

from src.auth.aws_connection_authority import owned_aws_connection, verified_connection_evidence
from src.auth.l5_owner_delegation import _aware, _digest, _error
from src.product_onboarding.routes import fingerprint
from src.products.models import Product, ProductChange, ProductOperation, ProductSourceBinding
from src.products.source_routes import connection_access


def identity(binding):
    return {
        key: getattr(binding, key)
        for key in (
            "tenant_id",
            "id",
            "project_id",
            "kind",
            "provider",
            "installation_id",
            "credential_id",
            "namespace",
            "native_id",
            "desired",
            "observed",
        )
    }


def source_reference(binding, sources):
    revision = (binding.observed or {}).get("source_revision") or binding.desired.get("source_revision")
    matches = [
        source
        for source in sources
        if isinstance(source, dict)
        and source.get("tenant_id") == binding.tenant_id
        and source.get("project_id") == binding.project_id
        and source.get("source_connection_id") == (binding.installation_id or binding.credential_id)
        and source.get("record_type") == ("repository_commit" if binding.kind == "repository" else binding.kind)
        and source.get("record_id") == binding.native_id
        and source.get("record_revision") == revision
    ]
    return matches[0] if len(matches) == 1 else None


async def result_identity(backend, run):
    rows = (
        await backend.db.execute(
            select(AssessmentResult, ResultDetail)
            .outerjoin(
                ResultDetail,
                (ResultDetail.tenant_id == AssessmentResult.tenant_id) & (ResultDetail.result_id == AssessmentResult.result_id),
            )
            .where(AssessmentResult.tenant_id == backend.actor.org_id, AssessmentResult.run_id == run.run_id)
            .order_by(AssessmentResult.version.desc())
            .limit(2)
            .execution_options(populate_existing=True)
        )
    ).all()
    if not rows:
        return None
    if len(rows) == 2 and rows[0][0].version == rows[1][0].version:
        raise _error(409, "qualification_result_ambiguous", "Select a run with one exact latest retained result.")
    result, detail = rows[0]
    payload = detail.payload if detail is not None else {}
    if (
        run.state != "succeeded"
        or payload.get("run_id") != run.run_id
        or payload.get("result_id") != result.result_id
        or payload.get("revision") != result.version
        or payload.get("kind") != "well_architected_result"
        or any(payload.get("scope", {}).get(key) != getattr(run, key) for key in ("tenant_id", "product_id", "project_id"))
    ):
        raise _error(409, "qualification_result_changed", "The retained result does not match this selected terminal run.")
    return {
        "result_id": result.result_id,
        "revision": result.version,
        "result_digest": _digest(payload),
        "assessment_digest": _digest(result.payload),
        "run_revision": run.revision,
        "native_job_ref_digest": _digest(run.native_job_ref),
        "source_manifest_digest": _digest(view_run(run).get("source_manifest")),
    }


async def freeze_source(backend, scope, run, source_binding_id, settings):
    """Freeze a currently accessible source; this does not authorize its removal."""
    binding = await backend.db.get(ProductSourceBinding, (backend.actor.org_id, source_binding_id), populate_existing=True)
    if (
        run is None
        or binding is None
        or binding.project_id != scope["project_id"]
        or binding.status != "healthy"
        or binding.unlinked_at is not None
        or binding.generation < 1
        or binding.revision < 1
        or await connection_access(backend.db, backend.actor, binding) is None
    ):
        raise _error(404, "not_found", "Selected source observation not found.")
    reference = source_reference(binding, run.input.get("sources") or [])
    if reference is None or reference not in scope["sources"]:
        raise _error(404, "not_found", "Select one of this run's current pinned sources.")
    product = await backend.db.get(Product, (backend.actor.org_id, scope["product_id"]), populate_existing=True)
    return {
        "binding_id": binding.id,
        "generation": binding.generation,
        "revision": binding.revision,
        "created_at": _aware(binding.created_at).isoformat(),
        "identity_digest": _digest(identity(binding)),
        "source_ref": deepcopy(reference),
        "product_revision": product.revision,
        "result": await result_identity(backend, run),
        "review_settings_digest": _digest(settings.get("well_architected")),
    }


async def transition(backend, product_id, project_id, service_id, experiment_id, operation_id):
    from src.l5_wa_qualification import load, owner, verified_native_binding, view

    configuration, version = await owner(backend, product_id, project_id, service_id)
    row = await load(backend, product_id, project_id, service_id, experiment_id)
    frozen = row.selection.get("source_revocation")
    scope = row.selection["scope"]
    denied = _error(409, "source_unlink_not_observed", "No matching completed source unlink is proved. Read its original Product operation.")
    if not view(row)["active"]:
        raise _error(410, "qualification_inactive", "This observation selection is revoked or expired.")
    if not isinstance(frozen, dict):
        raise _error(404, "not_found", "This selection did not retain a source revocation target.")
    if (
        configuration.configuration_id != scope["configuration_id"]
        or configuration.current_revision != scope["configuration_revision"]
        or configuration.owner_id != scope["created_by"]
        or version.hash != scope["configuration_hash"]
        or _digest(version.settings.get("well_architected")) != frozen["review_settings_digest"]
    ):
        raise _error(409, "qualification_scope_changed", "The selected owner or configuration changed.")
    native = await verified_native_binding(backend, scope)
    credential = await owned_aws_connection(backend.db, scope["credential_id"], backend.actor.user_id, backend.actor.org_id)
    verified_connection_evidence(credential)
    expected_connection = {
        "tenant_id": backend.actor.org_id,
        "project_id": project_id,
        "source_connection_id": credential.id,
        "record_type": "connection",
        "record_id": credential.id,
        "record_revision": credential.aws_verified_version_id,
    }
    if (
        version.settings.get("connection_ref") != expected_connection
        or native.connection_ref != expected_connection
        or native.native_resource_ref != scope["native_resource_ref"]
        or native.provider_identity != scope["provider_identity"]
        or _digest(native.creation_receipt) != scope["creation_receipt_digest"]
        or (credential.scopes or {}).get("account_id") != scope["provider_account_id"]
        or (credential.scopes or {}).get("role_arn") != scope["provider_role_arn"]
        or version.settings.get("region") != scope["region"]
        or (version.settings.get("source_refs") or []) != scope["sources"][1:]
    ):
        raise _error(409, "qualification_scope_changed", "The selected native binding, connection or source scope changed.")
    db, tenant = backend.db, backend.actor.org_id
    run = await db.get(Run, (tenant, row.selection["targets"]["run_id"]), populate_existing=True)
    if (
        run is None
        or (run.product_id, run.project_id, run.service_id, run.configuration_id, run.binding_id, run.binding_generation)
        != (product_id, project_id, service_id, scope["configuration_id"], scope["binding_id"], scope["binding_generation"])
        or _digest(run.input) != row.selection["run_input_digest"]
        or _digest(run.admission) != row.selection["run_admission_digest"]
        or frozen["source_ref"] not in run.input.get("sources", [])
    ):
        raise _error(409, "qualification_scope_changed", "The selected run or pinned source changed.")
    if frozen.get("result") is not None and await result_identity(backend, run) != frozen["result"]:
        raise _error(409, "qualification_result_changed", "The originally selected terminal result changed.")
    binding = await db.get(ProductSourceBinding, (tenant, frozen["binding_id"]), populate_existing=True)
    operation = await db.get(ProductOperation, (tenant, operation_id), populate_existing=True)
    if (
        binding is None
        or binding.project_id != project_id
        or binding.status != "unlinked"
        or binding.unlinked_at is None
        or binding.generation != frozen["generation"] + 1
        or binding.revision != frozen["revision"] + 1
        or _aware(binding.created_at).isoformat() != frozen["created_at"]
        or _digest(identity(binding)) != frozen["identity_digest"]
        or _aware(binding.unlinked_at) < _aware(row.created_at)
        or operation is None
        or operation.operation != "unlinkBinding"
        or operation.state != "succeeded"
        or (operation.product_id, operation.project_id, operation.actor_id, operation.binding_id, operation.binding_generation)
        != (product_id, project_id, backend.actor.user_id, binding.id, frozen["generation"])
        or operation.expected_revision is None
        or operation.expected_revision < frozen["product_revision"]
        or operation.request_hash
        != fingerprint(
            {"binding_id": binding.id, "product_id": product_id, "project_id": project_id, "expected_revision": operation.expected_revision}
        )
        or operation.request != {"binding_id": binding.id}
        or operation.receipt != {"binding_id": binding.id, "revision": operation.expected_revision + 1}
        or operation.lease_owner is not None
        or operation.lease_until is not None
        or _aware(operation.created_at) < _aware(binding.unlinked_at)
    ):
        raise denied
    changes = (
        await db.scalars(
            select(ProductChange)
            .where(
                ProductChange.tenant_id == tenant,
                ProductChange.operation_id == operation.id,
            )
            .limit(2)
            .execution_options(populate_existing=True)
        )
    ).all()
    if len(changes) != 1:
        raise denied
    change = changes[0]
    if (
        (change.product_id, change.project_id, change.actor_id, change.revision)
        != (product_id, project_id, backend.actor.user_id, operation.expected_revision + 1)
        or change.changed_paths != ["/bindings"]
        or _aware(change.recorded_at) < _aware(operation.created_at)
        or any(
            change.snapshot.get(key) != value
            for key, value in (("tenant_id", tenant), ("product_id", product_id), ("project_id", project_id), ("revision", change.revision))
        )
    ):
        raise denied
    # A replacement binding must not borrow the removed binding's proof. Current
    # source authorization must also deny this original run after the unlink.
    replacement = await db.scalar(
        select(ProductSourceBinding.id)
        .where(
            ProductSourceBinding.tenant_id == tenant,
            ProductSourceBinding.project_id == project_id,
            ProductSourceBinding.kind == binding.kind,
            ProductSourceBinding.native_id == binding.native_id,
            ProductSourceBinding.unlinked_at.is_(None),
        )
        .limit(1)
    )
    if replacement is not None or run in await backend.run_source_rights(project_id, [run]):
        raise _error(409, "source_access_still_active", "Current source access does not prove revocation of this run's selected source.")
    return {
        "schema_version": 1,
        "source": "durable-product-source-transition",
        "scope": {key: scope[key] for key in ("tenant_id", "product_id", "project_id")},
        "actor_id": backend.actor.user_id,
        "experiment_id": experiment_id,
        "selection_digest": row.selection_digest,
        "run_id": run.run_id,
        "result": deepcopy(frozen.get("result")),
        "source_ref_digest": _digest(frozen["source_ref"]),
        "source_binding_id": binding.id,
        "generation_before": frozen["generation"],
        "generation_after": binding.generation,
        "selected_at": _aware(row.created_at).isoformat(),
        "unlinked_at": _aware(binding.unlinked_at).isoformat(),
        "operation_id": operation.id,
        "operation_kind": operation.operation,
        "operation_state": operation.state,
        "operation_digest": _digest({"request": operation.request, "receipt": operation.receipt, "request_hash": operation.request_hash}),
        "change_id": change.id,
        "change_revision": change.revision,
        "change_digest": _digest({"snapshot": change.snapshot, "operation_id": operation.id, "revision": change.revision}),
        "recorded_at": _aware(change.recorded_at).isoformat(),
        "current_source_access": "denied",
        "execution_authorized": False,
        "provider_effect_count": None,
    }


async def observe_source_revocation(backend, product_id, project_id, service_id, experiment_id, operation_id):
    before = await transition(backend, product_id, project_id, service_id, experiment_id, operation_id)
    after = await transition(backend, product_id, project_id, service_id, experiment_id, operation_id)
    if after != before:
        raise _error(409, "qualification_scope_changed", "Source authority or retained unlink evidence changed during observation.")
    return {**after, "observed_at": datetime.now(UTC).isoformat()}
