"""Configuration storage and Profile population are composed by the gateway host."""

import json
from hashlib import sha256
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from l5_services.configuration_population import populate_settings
from l5_services.configuration_readiness import assess_readiness
from l5_services.models.configuration import Configuration, ConfigurationReceipt, ConfigurationVersion
from l5_services.wa_fence import active_generation
from l5_services.well_architected_sources import canonical_sources


def view(configuration: Configuration, version: ConfigurationVersion) -> dict:
    return {
        "configuration_id": configuration.configuration_id, "revision": version.revision,
        "scope": {"tenant_id": configuration.tenant_id, "product_id": configuration.product_id,
                  "project_id": configuration.project_id, "relationship_revision": configuration.relationship_revision,
                  "owner_id": configuration.owner_id, "visibility": version.settings["visibility"]},
        "state": version.state, "settings": version.settings,
        "inherited_fields": version.inherited_fields, "gaps": version.gaps,
        "handoff_state": configuration.handoff_state, "hash": version.hash,
    }


class ConfigurationStore:
    def __init__(self, session, tenant_id: str, owner_id: str, *, activation_authority=None):
        self.session = session
        self.tenant_id = tenant_id
        self.owner_id = owner_id
        self.activation_authority = activation_authority

    async def _readiness(self, settings, configuration_id, bindings, *, delegation_active, service_id):
        if service_id == "security" and configuration_id != "unassigned":
            from l5_services.models.binding import Binding

            # A configuration edit may invalidate revision-bound consent. Read
            # its actual local bindings for structural validation; save remains
            # needs_input until execution independently verifies fresh consent.
            rows = await self.session.scalars(select(Binding).where(
                Binding.tenant_id == self.tenant_id,
                Binding.configuration_id == configuration_id,
                Binding.service_id == "security",
            ))
            bindings = [{"binding_id": row.binding_id, "configuration_id": row.configuration_id,
                         "state": row.state, "connection_ref": row.connection_ref} for row in rows]
        state, gaps, policy = assess_readiness(settings, configuration_id, bindings,
                                              delegation_active=delegation_active, service_id=service_id)
        if service_id == "devops" and settings.get("enabled") is True:
            if self.activation_authority is None or configuration_id == "unassigned":
                raise HTTPException(409, detail={"code": "setup_activation_required",
                    "message": "Save a disabled draft, complete its retained native setup, then explicitly activate manual investigation."})
            await self.activation_authority(configuration_id, settings)
            state, gaps = "ready", []
        return state, gaps, policy

    async def current(self, product_id: str, project_id: str, service_id: str):
        configuration = await self.session.scalar(select(Configuration).where(
            Configuration.tenant_id == self.tenant_id, Configuration.product_id == product_id,
            Configuration.project_id == project_id, Configuration.service_id == service_id,
        ))
        if configuration is None:
            raise HTTPException(404, detail={"code": "not_found"})
        version = await self.session.get(ConfigurationVersion, (
            self.tenant_id, configuration.configuration_id, configuration.current_revision,
        ))
        if configuration.owner_id != self.owner_id and version.settings.get("visibility") != "product_team":
            raise HTTPException(404, detail={"code": "not_found"})
        return configuration, version

    def _receipt_key(self, product_id, project_id, service_id, idempotency_key):
        return (self.tenant_id, product_id, project_id, service_id, self.owner_id, "create", idempotency_key)

    async def replay_create(self, product_id, project_id, service_id, idempotency_key, choices):
        receipt = await self.session.get(ConfigurationReceipt, self._receipt_key(
            product_id, project_id, service_id, idempotency_key,
        ))
        if receipt is None:
            return None
        digest = sha256(json.dumps({"settings": choices, "if_none_match": "*"}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if receipt.request_hash != digest:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        configuration = await self.session.get(Configuration, (self.tenant_id, receipt.configuration_id))
        version = await self.session.get(ConfigurationVersion, (self.tenant_id, receipt.configuration_id, receipt.revision))
        if configuration is None or version is None:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        return configuration, version

    async def create(self, product_id, project_id, service_id, relationship_revision, profile, profile_ref, authorized_sources, choices, idempotency_key=None, request_settings=None):
        request_settings = choices if request_settings is None else request_settings
        if idempotency_key is not None:
            replay = await self.replay_create(product_id, project_id, service_id, idempotency_key, request_settings)
            if replay is not None:
                return replay
        existing = await self.session.scalar(select(Configuration.configuration_id).where(
            Configuration.tenant_id == self.tenant_id, Configuration.product_id == product_id,
            Configuration.project_id == project_id, Configuration.service_id == service_id,
        ))
        if existing is not None:
            raise HTTPException(412, detail={"code": "stale_revision"})
        try:
            settings, inherited_fields = populate_settings(profile, profile_ref, authorized_sources, choices, service_id)
            state, gaps, policy = await self._readiness(settings, "unassigned", [], delegation_active=False, service_id=service_id)
        except ValueError as error:
            raise HTTPException(422, detail={"code": "validation_error", "message": str(error)}) from None
        settings["monitoring"] = policy
        identifier = str(uuid4())
        digest = sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        configuration = Configuration(
            tenant_id=self.tenant_id, configuration_id=identifier, product_id=product_id,
            project_id=project_id, service_id=service_id, owner_id=self.owner_id,
            relationship_revision=str(relationship_revision),
            current_revision=1, handoff_state="pending",
        )
        version = ConfigurationVersion(
            tenant_id=self.tenant_id, configuration_id=identifier, revision=1, settings=settings,
            inherited_fields=inherited_fields, gaps=gaps, state=state, hash=digest,
        )
        self.session.add_all([configuration, version])
        if idempotency_key is not None:
            self.session.add(ConfigurationReceipt(
                tenant_id=self.tenant_id, product_id=product_id, project_id=project_id,
                service_id=service_id, principal_id=self.owner_id, operation="create",
                idempotency_key=idempotency_key,
                request_hash=sha256(json.dumps({"settings": request_settings, "if_none_match": "*"}, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                configuration_id=identifier, revision=1,
            ))
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            if idempotency_key is not None:
                replay = await self.replay_create(product_id, project_id, service_id, idempotency_key, request_settings)
                if replay is not None:
                    return replay
            raise HTTPException(412, detail={"code": "stale_revision"}) from None
        return configuration, version

    async def refresh(self, configuration, previous, profile, profile_ref, authorized_sources, if_match, *, choices=None):
        if configuration.owner_id != self.owner_id:
            raise HTTPException(404, detail={"code": "not_found"})
        if if_match != f'"{previous.hash}"':
            raise HTTPException(412, detail={"code": "stale_revision"})
        if choices is not None and not isinstance(choices, dict):
            raise HTTPException(422, detail={"code": "validation_error"})
        if profile_ref == previous.settings["profile_ref"] and not choices:
            return configuration, previous
        inherited = set(previous.inherited_fields)
        explicit = {
            field: json.loads(json.dumps(value)) for field, value in previous.settings.items()
            if field != "profile_ref" and field not in inherited
        }
        explicit.update(json.loads(json.dumps(choices or {})))
        try:
            settings, inherited_fields = populate_settings(
                profile, profile_ref, authorized_sources, explicit, configuration.service_id, refreshing=True,
            )
        except ValueError as error:
            raise HTTPException(409, detail={"code": "source_scope_conflict", "message": str(error)}) from None
        source_change = False
        if "source_refs" in inherited and "source_refs" not in (choices or {}):
            old_sources = previous.settings["source_refs"]
            if configuration.service_id == "well_architected":
                old_sources = canonical_sources(old_sources)
            new_sources = settings["source_refs"]
            settings["source_refs"] = [source for source in old_sources if source in new_sources]
            source_change = new_sources != old_sources
        try:
            state, gaps, policy = await self._readiness(
                settings, configuration.configuration_id, [], delegation_active=False, service_id=configuration.service_id
            )
        except ValueError as error:
            raise HTTPException(422, detail={"code": "validation_error", "message": str(error)}) from None
        if source_change:
            gaps.append("source_scope_changed")
            if state == "ready":
                state = "needs_input"
        settings["monitoring"] = policy
        if (settings, inherited_fields, gaps, state) == (
            previous.settings, previous.inherited_fields, previous.gaps, previous.state
        ):
            return configuration, previous
        return await self.append(configuration, previous, settings, inherited_fields, gaps, state, if_match)

    async def edit(self, configuration, previous, profile, authorized_sources, changes, if_match, *, idempotency_key=None):
        if configuration.owner_id != self.owner_id:
            raise HTTPException(404, detail={"code": "not_found"})
        if if_match != f'"{previous.hash}"':
            raise HTTPException(412, detail={"code": "stale_revision"})
        pinned_ref = previous.settings["profile_ref"]
        if "profile_ref" in changes and changes["profile_ref"] != pinned_ref:
            raise HTTPException(409, detail={"code": "refresh_required"})
        explicit = {
            field: json.loads(json.dumps(value)) for field, value in previous.settings.items()
            if field != "profile_ref" and field not in previous.inherited_fields
        }
        for field, value in changes.items():
            if field == "profile_ref":
                continue
            if field in previous.inherited_fields and value == previous.settings.get(field):
                continue
            explicit[field] = value
        try:
            settings, inherited_fields = populate_settings(profile, pinned_ref, authorized_sources, explicit, configuration.service_id)
            state, gaps, policy = await self._readiness(
                settings, configuration.configuration_id, [], delegation_active=False, service_id=configuration.service_id
            )
        except ValueError as error:
            raise HTTPException(422, detail={"code": "validation_error", "message": str(error)}) from None
        if configuration.service_id == "well_architected" and "source_refs" in previous.inherited_fields:
            old_sources = canonical_sources(previous.settings["source_refs"])
            selected_sources = settings["source_refs"]
            if not any(override["field"] == "source_refs" for override in settings["overrides"]):
                if any(source not in selected_sources for source in old_sources):
                    raise HTTPException(409, detail={"code": "source_scope_conflict"})
                settings["source_refs"] = json.loads(json.dumps(old_sources))
                if selected_sources != old_sources or "source_scope_changed" in previous.gaps:
                    gaps.append("source_scope_changed")
                    if state == "ready":
                        state = "needs_input"
        settings["monitoring"] = policy
        if (settings, inherited_fields, gaps, state) == (
            previous.settings, previous.inherited_fields, previous.gaps, previous.state,
        ):
            return configuration, previous
        receipt = None
        if idempotency_key is not None:
            receipt = ConfigurationReceipt(
                tenant_id=self.tenant_id, product_id=configuration.product_id, project_id=configuration.project_id,
                service_id=configuration.service_id, principal_id=self.owner_id, operation="edit",
                idempotency_key=idempotency_key, configuration_id=configuration.configuration_id,
                revision=previous.revision + 1,
                request_hash=sha256(json.dumps({"settings": changes, "if_match": if_match},
                                                sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            )
        return await self.append(configuration, previous, settings, inherited_fields, gaps, state, if_match, receipt=receipt)

    async def append(self, configuration, previous, settings, inherited_fields, gaps, state, if_match, *, receipt=None):
        if if_match != f'"{previous.hash}"':
            raise HTTPException(412, detail={"code": "stale_revision"})
        digest = sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        applied = await self.session.execute(update(Configuration).where(
            Configuration.tenant_id == self.tenant_id,
            Configuration.configuration_id == configuration.configuration_id,
            Configuration.current_revision == previous.revision,
        ).values(current_revision=previous.revision + 1, handoff_state="pending"))
        if applied.rowcount != 1:
            await self.session.rollback()
            raise HTTPException(412, detail={"code": "stale_revision"})
        if (configuration.service_id == "well_architected"
                and any(settings.get(field) != previous.settings.get(field) for field in (
                    "source_refs", "connection_ref", "region", "well_architected", "profile_ref"))
                and await active_generation(self.session, self.tenant_id,
                                            configuration_id=configuration.configuration_id)):
            await self.session.rollback()
            raise HTTPException(409, detail={"code": "native_generation_active"})
        version = ConfigurationVersion(
            tenant_id=self.tenant_id, configuration_id=configuration.configuration_id,
            revision=previous.revision + 1, settings=settings,
            inherited_fields=inherited_fields, gaps=gaps, state=state, hash=digest,
        )
        self.session.add(version)
        if receipt is not None:
            self.session.add(receipt)
            capture = getattr(self, "capture_edit_commit", None)
            if capture is not None:
                # The host may retain a local canonical effect in this SAME
                # transaction. A rejected capture rolls back the edit too.
                try:
                    await capture(self.session, configuration, version, receipt)
                except BaseException:
                    await self.session.rollback()
                    raise
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise HTTPException(412, detail={"code": "stale_revision"}) from None
        await self.session.refresh(configuration)
        return configuration, version
