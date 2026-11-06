"""Capture the actual conditional Security UI save in independent transactions."""

from datetime import UTC, datetime

from fastapi import HTTPException

from src.auth.security_fixture_credential import fingerprint
from src.orchestration.l5_observation_scope import ObservationError, digest, require

from . import security_evidence_local as local
from .security_evidence_contract import timestamp
from .security_evidence_store import FLAGS, append, event, reference

HEADER = "X-ADP-Security-Evidence"
RECEIPT_HEADER = "X-ADP-Security-Evidence-Receipt"


def validate_choices(settings, before, selected):
    require(
        settings.get("region") == selected.ui_region and selected.ui_region != before["region"], "security_evidence_requested_choices_required", 422
    )
    expected = [item for item in before.get("overrides", []) if item["field"] != "region"]
    expected.append({"field": "region", "value": selected.ui_region, "reason": selected.ui_reason})
    require(settings.get("overrides") == expected, "security_evidence_requested_choices_required", 422)
    require(
        all(key in {"region", "overrides"} or value == before.get(key) for key, value in settings.items()), "security_evidence_unrelated_edit", 422
    )
    return {**before, "region": selected.ui_region, "overrides": expected}


class SecurityEffectBackend:
    def __init__(self, backend, request, response):
        self.backend, self.request, self.response = backend, request, response

    def __getattr__(self, name):
        return getattr(self.backend, name)

    async def save(self, product_id, project_id, service_id, settings, idempotency_key, if_none_match, if_match):
        from src.l5_integration import GatewayConfigurationBackend

        request_id = self.request.headers[HEADER]
        factory = self.backend.sessions
        try:
            async with factory() as db:
                context = GatewayConfigurationBackend(db, self.backend.actor)
                human = await local.interactive(context, self.request)
                row, selected = await local.request_row(context, product_id, project_id, request_id, lock=True)
                require(not row.document.get("export_only"), "security_evidence_export_only", 409)
                grant = await local.validate_export_authority(db, row)
                prepared = await event(db, human.tenant_id, request_id, "prepared")
                require(service_id == "security" and selected.phase == "edit" and grant.document["observer_id"] == human.user_id, "not_found", 404)
                require(
                    prepared.actor_id == human.user_id
                    and prepared.document["fingerprint"] == fingerprint(self.request)
                    and timestamp(prepared.document["expires_at"]) > datetime.now(UTC),
                    "security_evidence_challenge_expired",
                )
                require(await event(db, human.tenant_id, request_id, "effect_before", required=False) is None, "security_evidence_already_attempted")
                before = await local.snapshot(context, selected)
                require(
                    before == grant.document["snapshot"]
                    and if_match == '"' + selected.configuration_hash + '"'
                    and if_none_match is None
                    and idempotency_key == selected.ui_receipt,
                    "security_evidence_scope_changed",
                    412,
                )
                expected = validate_choices(settings, before["configuration"]["settings"], selected)
                marker = await append(
                    db,
                    human.tenant_id,
                    request_id,
                    "effect_before",
                    human.user_id,
                    "human",
                    {
                        "before": before,
                        "snapshot_sha256": grant.document["snapshot_sha256"],
                        "request_sha256": digest({"settings": settings, "if_match": if_match, "idempotency_key": idempotency_key}),
                        "canonical_request_sha256": digest({"settings": settings, "if_match": if_match}),
                        "idempotency_key": idempotency_key,
                        "expected_settings": expected,
                        "recorded_at": datetime.now(UTC).isoformat(),
                        **FLAGS,
                    },
                )
                before_receipt = reference(marker)
                await db.commit()
        except ObservationError as error:
            raise HTTPException(
                error.status,
                detail={
                    "code": error.code,
                    "next_action": (
                        "Read this Security request's retained receipts. Resolve the exact scope or choices; never repeat an uncertain save."
                    ),
                },
            ) from None

        async def finish(result, status):
            async with factory() as db:
                context = GatewayConfigurationBackend(db, self.backend.actor)
                after = await local.snapshot(context, selected)
                valid = (
                    result is not None
                    and after["configuration"] == result
                    and result["settings"] == expected
                    and result["revision"] == selected.configuration_revision + 1
                )
                recorded = await append(
                    db,
                    human.tenant_id,
                    request_id,
                    "effect_after",
                    human.user_id,
                    "human",
                    {
                        "before_receipt": before_receipt,
                        "after": after,
                        "returned_configuration": result,
                        "http_status": status,
                        "outcome": "returned" if valid else "unknown",
                        "recorded_at": datetime.now(UTC).isoformat(),
                        **FLAGS,
                    },
                )
                await db.commit()
                return reference(recorded)

        if self.response is not None:
            self.response.headers[RECEIPT_HEADER] = before_receipt["id"]

        async def committed(db, configuration, version, receipt):
            context = GatewayConfigurationBackend(db, self.backend.actor)
            current_request, _ = await local.request_row(context, product_id, project_id, request_id, lock=True)
            require(await event(db, selected.tenant_id, request_id, "revoked", required=False) is None, "security_evidence_grant_revoked")
            require(
                datetime.now(UTC) < min(timestamp(grant.document["expires_at"]), timestamp(prepared.document["expires_at"])),
                "security_evidence_challenge_expired",
            )
            await local.interactive(context, self.request)
            product, _ = await local.member_access(db, selected.tenant_id, product_id, project_id, grant.actor_id, owner=True)
            require(
                product.owner_user_id == grant.actor_id
                and configuration.owner_id == grant.actor_id
                and version.revision == selected.configuration_revision + 1,
                "security_evidence_owner_changed",
            )
            await append(
                db,
                selected.tenant_id,
                request_id,
                "effect_committed",
                human.user_id,
                "human",
                {
                    "before_receipt": before_receipt,
                    "configuration_id": configuration.configuration_id,
                    "revision": version.revision,
                    "hash": version.hash,
                    "idempotency_key": receipt.idempotency_key,
                    "canonical_request_sha256": receipt.request_hash,
                },
            )

        self.backend.store.capture_edit_commit = committed
        try:
            result = await self.backend.save(product_id, project_id, service_id, settings, idempotency_key, if_none_match, if_match)
        except BaseException as error:
            try:
                await finish(None, error.status_code if isinstance(error, HTTPException) else None)
            except Exception:
                pass  # A durable before marker remains unknown and prevents retry.
            raise
        receipt = await finish(result, 200)
        if self.response is not None:
            self.response.headers[RECEIPT_HEADER] = receipt["id"]
        return result


async def reconcile(backend, http, product_id, project_id, request_id):
    """Read the atomic edit receipt; append new evidence without repeating work."""
    from l5_services.models.configuration import ConfigurationReceipt, ConfigurationVersion

    row, selected = await local.request_row(backend, product_id, project_id, request_id, current=False, lock=True)
    await local.member_access(backend.db, selected.tenant_id, product_id, project_id, backend.actor.user_id)
    before = await event(backend.db, selected.tenant_id, request_id, "effect_before")
    committed = await event(backend.db, selected.tenant_id, request_id, "effect_committed")
    receipt = await backend.db.get(
        ConfigurationReceipt,
        (selected.tenant_id, product_id, project_id, "security", before.actor_id, "edit", before.document["idempotency_key"]),
        populate_existing=True,
    )
    require(
        receipt is not None
        and receipt.request_hash == before.document["canonical_request_sha256"]
        and receipt.configuration_id == selected.configuration_id
        and receipt.revision == selected.configuration_revision + 1,
        "security_evidence_effect_outcome_unknown",
    )
    version = await backend.db.get(ConfigurationVersion, (selected.tenant_id, receipt.configuration_id, receipt.revision))
    require(version is not None and version.settings == before.document["expected_settings"], "security_evidence_effect_outcome_unknown")
    require(
        committed.document["canonical_request_sha256"] == receipt.request_hash
        and committed.document["idempotency_key"] == receipt.idempotency_key
        and committed.document["hash"] == version.hash
        and local.aware(committed.created_at) <= timestamp(row.document["expires_at"]),
        "security_evidence_effect_outcome_unknown",
    )
    after = await local.snapshot(backend, selected)
    require(
        after["configuration"]["revision"] == version.revision and after["configuration"]["hash"] == version.hash, "security_evidence_scope_changed"
    )
    recorded = await append(
        backend.db,
        selected.tenant_id,
        request_id,
        "effect_reconciled",
        "system:security-reconciliation",
        "service",
        {
            "before_receipt": reference(before),
            "after": after,
            "returned_configuration": None,
            "outcome": "reconciled",
            "canonical_receipt": {
                "idempotency_key": receipt.idempotency_key,
                "principal_id": receipt.principal_id,
                "request_sha256": receipt.request_hash,
                "configuration_id": receipt.configuration_id,
                "revision": receipt.revision,
            },
        },
    )
    return {"receipt": reference(recorded), **FLAGS}
