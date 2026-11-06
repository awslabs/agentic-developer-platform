import unittest

from fastapi import HTTPException
from l5_services.api.configuration import ConfigurationStore
from l5_services.models.base import Base
from l5_services.models.configuration import Configuration, ConfigurationVersion
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.document = self.reference("document", "doc-a", "rev-a")
        self.added = self.reference("document", "doc-b", "rev-b")
        self.profile = {
            "tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a", "revision": 7,
            "product": {"purpose": "Review"}, "configuration": {
                "basic": {"documents": [self.document]}, "governance": {"data_classification": "internal"},
            },
        }
        self.sources = self.available([self.document])
        self.profile_ref = self.reference("product", "product-a", "7", "adp.profile")
        async with self.sessions() as session:
            store = ConfigurationStore(session, "tenant-a", "user-a")
            configuration, version = await store.create(
                "product-a", "project-a", "security", 1, self.profile, self.profile_ref,
                self.sources, {"capabilities": ["threat_model"], "exclusions": ["legacy/"]},
            )
            self.configuration_id = configuration.configuration_id
            self.etag = f'"{version.hash}"'

    async def asyncTearDown(self):
        await self.engine.dispose()

    def reference(self, record_type, record_id, revision, connection="connection-a"):
        return {
            "tenant_id": "tenant-a", "project_id": "project-a", "source_connection_id": connection,
            "record_type": record_type, "record_id": record_id, "record_revision": revision,
        }

    def available(self, sources):
        return {
            "coverage": "complete", "next_cursor": None,
            "items": [{"connection_ref": {"kind": "credential", "id": "connection-a"}, "resources": sources}],
        }

    async def test_refresh_advances_inheritance_and_preserves_exclusions_without_broadening(self):
        newer = {**self.profile, "revision": 8, "configuration": {
            "basic": {"documents": [self.document, self.added]},
            "governance": {"data_classification": "restricted"},
        }}
        new_ref = self.reference("product", "product-a", "8", "adp.profile")
        async with self.sessions() as session:
            store = ConfigurationStore(session, "tenant-a", "user-a")
            configuration, previous = await store.current("product-a", "project-a", "security")
            refreshed, current = await store.refresh(
                configuration, previous, newer, new_ref, self.available([self.document, self.added]), self.etag,
            )
            self.assertEqual(refreshed.current_revision, 2)
            self.assertEqual(current.settings["profile_ref"], new_ref)
            self.assertEqual(current.settings["classification"], "restricted")
            self.assertEqual(current.settings["exclusions"], ["legacy/"])
            self.assertEqual(current.settings["source_refs"], [self.document])
            self.assertIn("source_scope_changed", current.gaps)
        async with self.sessions() as reader:
            old_version = await reader.get(ConfigurationVersion, ("tenant-a", self.configuration_id, 1))
            new_version = await reader.get(ConfigurationVersion, ("tenant-a", self.configuration_id, 2))
            self.assertEqual(old_version.settings["classification"], "internal")
            self.assertEqual(old_version.settings["source_refs"], [self.document])
            self.assertEqual(old_version.settings["exclusions"], new_version.settings["exclusions"])
            self.assertNotEqual(old_version.hash, new_version.hash)
            self.assertEqual(await reader.scalar(select(func.count()).select_from(ConfigurationVersion)), 2)

    async def test_changed_source_revision_is_not_silently_selected(self):
        newer = {**self.profile, "revision": 8, "configuration": {
            "basic": {"documents": [self.added]}, "governance": {"data_classification": "internal"},
        }}
        async with self.sessions() as session:
            store = ConfigurationStore(session, "tenant-a", "user-a")
            configuration, previous = await store.current("product-a", "project-a", "security")
            _, current = await store.refresh(configuration, previous, newer, self.reference(
                "product", "product-a", "8", "adp.profile"), self.available([self.added]), self.etag)
            self.assertEqual(current.settings["source_refs"], [])
            self.assertIn("source_scope_changed", current.gaps)

    async def test_stale_precondition_preserves_both_tables(self):
        async with self.sessions() as session:
            store = ConfigurationStore(session, "tenant-a", "user-a")
            configuration, previous = await store.current("product-a", "project-a", "security")
            with self.assertRaises(HTTPException) as caught:
                await store.refresh(configuration, previous, {**self.profile, "revision": 8}, self.reference(
                    "product", "product-a", "8", "adp.profile"), self.sources, '"stale"')
            self.assertEqual(caught.exception.status_code, 412)
        async with self.sessions() as reader:
            self.assertEqual((await reader.get(Configuration, ("tenant-a", self.configuration_id))).current_revision, 1)
            self.assertEqual(await reader.scalar(select(func.count()).select_from(ConfigurationVersion)), 1)

    async def test_refresh_requires_configuration_owner_even_without_choices(self):
        async with self.sessions() as session:
            owner = ConfigurationStore(session, "tenant-a", "user-a")
            configuration, previous = await owner.current("product-a", "project-a", "security")
            other = ConfigurationStore(session, "tenant-a", "user-b")
            for choices in (None, {"exclusions": ["other/"]}):
                with self.assertRaises(HTTPException) as caught:
                    await other.refresh(configuration, previous, self.profile, self.profile_ref,
                                        self.sources, self.etag, choices=choices)
                self.assertEqual(caught.exception.status_code, 404)
            self.assertEqual(configuration.current_revision, 1)
            self.assertEqual(await session.scalar(select(func.count()).select_from(ConfigurationVersion)), 1)

    async def test_conditional_edit_race_keeps_one_complete_version(self):
        newer = {**self.profile, "revision": 7}
        async with self.sessions() as first_session, self.sessions() as second_session:
            first_store = ConfigurationStore(first_session, "tenant-a", "user-a")
            second_store = ConfigurationStore(second_session, "tenant-a", "user-a")
            first_config, first_version = await first_store.current("product-a", "project-a", "security")
            second_config, second_version = await second_store.current("product-a", "project-a", "security")
            self.assertEqual(first_version.hash, second_version.hash)
            winner_config, winner = await first_store.edit(
                first_config, first_version, newer, self.sources,
                {"exclusions": ["winner/"]}, self.etag,
            )
            self.assertEqual(winner.revision, 2)
            self.assertEqual(winner_config.current_revision, 2)
            with self.assertRaises(HTTPException) as loser:
                await second_store.edit(
                    second_config, second_version, newer, self.sources,
                    {"exclusions": ["loser/"]}, self.etag,
                )
            self.assertEqual(loser.exception.status_code, 412)
        async with self.sessions() as reader:
            current = await reader.get(ConfigurationVersion, ("tenant-a", self.configuration_id, 2))
            old = await reader.get(ConfigurationVersion, ("tenant-a", self.configuration_id, 1))
            self.assertEqual(current.settings["exclusions"], ["winner/"])
            self.assertEqual(old.settings["exclusions"], ["legacy/"])
            self.assertEqual(await reader.scalar(select(func.count()).select_from(ConfigurationVersion)), 2)

    async def test_full_readback_edit_keeps_inherited_profile_fields(self):
        async with self.sessions() as session:
            store = ConfigurationStore(session, "tenant-a", "user-a")
            configuration, previous = await store.current("product-a", "project-a", "security")
            submitted = {**previous.settings, "exclusions": ["no-secret/"]}
            _, edited = await store.edit(configuration, previous, self.profile, self.sources, submitted, self.etag)
            self.assertEqual(edited.settings["classification"], "internal")
            self.assertEqual(edited.inherited_fields, ["purpose", "classification", "source_refs"])
            self.assertEqual(edited.settings["exclusions"], ["no-secret/"])

    async def test_devops_refresh_retains_overrides_and_does_not_inherit_added_environment(self):
        environment = self.reference("environment", "environment-a", "environment-revision-a")
        extra_environment = self.reference("environment", "environment-b", "environment-revision-b")
        devops_profile = {**self.profile, "configuration": {
            "selected_capabilities": [{"capability": "devops", "ownership_mode": "owned"}],
            "advanced": {"environments": [environment]},
            "governance": {"data_classification": "internal"},
        }}
        choices = {"service_id": "devops", "capabilities": ["investigation"], "enabled": False,
                   "triggers": [], "classification": "internal", "overrides": [
                       {"field": "classification", "value": "internal", "reason": "Keep restricted review"}],
                   "devops": {"space_mode": "create", "accounts": [], "integrations": []}}
        async with self.sessions() as session:
            store = ConfigurationStore(session, "tenant-a", "user-a")
            _, first = await store.create("product-a", "project-a", "devops", 1, devops_profile,
                                          self.profile_ref, self.available([environment]), choices)
            original_etag = f'"{first.hash}"'
            newer = {**devops_profile, "revision": 8, "configuration": {
                **devops_profile["configuration"], "advanced": {"environments": [environment, extra_environment]},
                "governance": {"data_classification": "restricted"},
            }}
            new_ref = self.reference("product", "product-a", "8", "adp.profile")
            configuration, previous = await store.current("product-a", "project-a", "devops")
            _, updated = await store.refresh(configuration, previous, newer, new_ref,
                                             self.available([environment, extra_environment]), original_etag)
            self.assertEqual(updated.revision, 2)
            self.assertEqual(updated.settings["profile_ref"], new_ref)
            self.assertEqual(updated.settings["classification"], "internal")
            self.assertEqual(updated.settings["overrides"], choices["overrides"])
            self.assertEqual(updated.settings["source_refs"], [environment])
            self.assertEqual(updated.settings["devops"], choices["devops"])
            self.assertIn("source_scope_changed", updated.gaps)
            self.assertEqual(updated.state, "disabled")
        async with self.sessions() as reader:
            original = await reader.get(ConfigurationVersion, ("tenant-a", configuration.configuration_id, 1))
            current = await reader.get(ConfigurationVersion, ("tenant-a", configuration.configuration_id, 2))
            self.assertEqual(original.settings["source_refs"], current.settings["source_refs"])
            self.assertEqual(original.settings["profile_ref"], self.profile_ref)
            self.assertNotEqual(original.hash, current.hash)

    async def test_devops_stale_write_and_implicit_widening_preserve_current_version(self):
        environment = self.reference("environment", "environment-a", "environment-revision-a")
        extra_environment = self.reference("environment", "environment-b", "environment-revision-b")
        devops_profile = {**self.profile, "configuration": {
            "selected_capabilities": [{"capability": "devops", "ownership_mode": "owned"}],
            "advanced": {"environments": [environment]},
        }}
        choices = {"service_id": "devops", "capabilities": ["investigation"], "enabled": False,
                   "triggers": [], "devops": {"space_mode": "create", "accounts": [], "integrations": []}}
        newer = {**devops_profile, "revision": 8, "configuration": {
            **devops_profile["configuration"], "advanced": {"environments": [environment, extra_environment]},
        }}
        new_ref = self.reference("product", "product-a", "8", "adp.profile")
        available = self.available([environment, extra_environment])
        async with self.sessions() as session:
            store = ConfigurationStore(session, "tenant-a", "user-a")
            configuration, old = await store.create("product-a", "project-a", "devops", 1, devops_profile,
                                                    self.profile_ref, self.available([environment]), choices)
            old_etag = f'"{old.hash}"'
            configuration, old = await store.current("product-a", "project-a", "devops")
            _, current = await store.refresh(configuration, old, newer, new_ref, available, old_etag)
            self.assertEqual(current.settings["source_refs"], [environment])
            configuration_id = configuration.configuration_id
            current_hash = current.hash
            with self.assertRaises(HTTPException) as widened:
                await store.edit(configuration, current, newer, available,
                                 {"source_refs": [environment, extra_environment]}, f'"{current.hash}"')
            self.assertEqual(widened.exception.status_code, 422)
            selection = [environment, extra_environment]
            _, selected = await store.edit(configuration, current, newer, available,
                {"source_refs": selection, "overrides": [{"field": "source_refs", "value": selection,
                    "reason": "Explicitly add the second environment"}]}, f'"{current.hash}"')
            self.assertEqual(selected.settings["source_refs"], selection)
            with self.assertRaises(HTTPException) as stale:
                await store.refresh(configuration, old, newer, new_ref, available, old_etag)
            self.assertEqual(stale.exception.status_code, 412)
        async with self.sessions() as reader:
            saved = await reader.get(ConfigurationVersion, ("tenant-a", configuration_id, 2))
            self.assertEqual(saved.settings["source_refs"], [environment])
            self.assertEqual(saved.hash, current_hash)
            selected_version = await reader.get(ConfigurationVersion, ("tenant-a", configuration_id, 3))
            self.assertEqual(selected_version.settings["source_refs"], [environment, extra_environment])
            self.assertEqual(await reader.scalar(select(func.count()).select_from(ConfigurationVersion)), 4)
