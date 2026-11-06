import unittest

import httpx
from fastapi import FastAPI
from l5_services.api.configuration import ConfigurationStore, view
from l5_services.api.router import create_router
from l5_services.models.base import Base
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.profile_handoff import link_configuration
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class MountedConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.source_ref = {
            "tenant_id": "tenant-a", "project_id": "project-a", "source_connection_id": "connection-a",
            "record_type": "document", "record_id": "document-a", "record_revision": "sha-a",
        }
        self.profile_ref = {
            **self.source_ref, "source_connection_id": "adp.profile", "record_type": "product",
            "record_id": "product-a", "record_revision": "7",
        }
        self.profile = {
            "tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a", "revision": 7,
            "product": {"purpose": "Review system"},
            "configuration": {
                "basic": {"documents": [self.source_ref]},
                "governance": {"data_classification": "confidential"},
                "selected_capabilities": [
                    {"capability": "threat_model", "ownership_mode": "managed"},
                    {"capability": "Delivery", "ownership_mode": "observed"},
                ],
            },
        }
        self.available = {
            "coverage": "complete", "next_cursor": None,
            "items": [{"connection_ref": {"kind": "credential", "id": "connection-a"}, "resources": [self.source_ref]},
                      {"connection_ref": {"kind": "credential", "id": "connection-b"}, "resources": []}],
        }
        self.provider_attempts = []
        owner = self

        class Backend:
            async def get(self, product_id, project_id, service_id):
                async with owner.sessions() as session:
                    store = ConfigurationStore(session, "tenant-a", "user-a")
                    configuration, version = await store.current(product_id, project_id, service_id)
                    return view(configuration, version)

            async def save(self, product_id, project_id, service_id, settings, idempotency_key, if_none_match, if_match):
                async with owner.sessions() as session:
                    store = ConfigurationStore(session, "tenant-a", "user-a")
                    configuration, version = await store.create(
                        product_id, project_id, service_id, 1, owner.profile, owner.profile_ref,
                        owner.available, settings,
                    )
                    reference = {
                        **owner.profile_ref, "record_type": "l5_configuration",
                        "record_id": configuration.configuration_id, "record_revision": "1",
                    }

                    async def read_profile():
                        return owner.profile

                    async def patch_profile(patch, revision, key):
                        owner.profile["configuration"] = patch["configuration"]
                        owner.profile["revision"] = revision + 1

                    configuration.handoff_state = await link_configuration(
                        read_profile, patch_profile, reference, ["threat_model"], idempotency_key,
                    )
                    await session.commit()
                    return view(configuration, version)

        app = FastAPI()
        app.include_router(create_router(lambda: Backend()))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test")
        self.path = "/v1/products/product-a/projects/project-a/services/security/configuration"

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()

    async def test_put_and_get_retain_profile_provenance_without_provider_effect(self):
        created = await self.client.put(self.path, json={"capabilities": ["threat_model"]}, headers={
            "Idempotency-Key": "first-create-00001", "If-None-Match": "*",
        })
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["settings"]["source_refs"], [self.source_ref])
        self.assertEqual(created.json()["settings"]["classification"], "confidential")
        self.assertEqual(created.json()["handoff_state"], "linked")
        self.assertEqual(created.json()["settings"]["monitoring"], {
            "poll_interval_seconds": 600, "poll_grace_seconds": 300,
        })
        self.assertEqual(created.headers["etag"], f'"{created.json()["hash"]}"')
        read = await self.client.get(self.path)
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json(), created.json())
        async with self.sessions() as session:
            stored_config = await session.scalar(select(Configuration))
            stored_version = await session.scalar(select(ConfigurationVersion))
            self.assertEqual(stored_config.current_revision, 1)
            self.assertEqual(stored_version.settings, created.json()["settings"])
            self.assertEqual(stored_version.inherited_fields, ["purpose", "classification", "source_refs"])
        self.assertEqual(self.profile["configuration"]["selected_capabilities"][1], {
            "capability": "Delivery", "ownership_mode": "observed",
        })
        self.assertEqual(self.provider_attempts, [])

    async def test_rejects_other_service_without_creating_configuration(self):
        response = await self.client.put(self.path.replace("/security/", "/other/"), json={
            "capabilities": ["threat_model"]}, headers={"If-None-Match": "*", "Idempotency-Key": "wrong_service_00001"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual((await self.client.get(self.path.replace("/security/", "/other/"))).status_code, 404)
        async with self.sessions() as session:
            self.assertIsNone(await session.scalar(select(Configuration)))

    async def test_requires_create_preconditions(self):
        response = await self.client.put(self.path, json={"capabilities": ["threat_model"]})
        self.assertEqual(response.status_code, 428)
        conflicting = await self.client.put(self.path, json={"capabilities": ["threat_model"]}, headers={
            "Idempotency-Key": "conflicting_headers_00001", "If-Match": '"old"', "If-None-Match": "*",
        })
        self.assertEqual(conflicting.status_code, 422)
        self.assertEqual((await self.client.get(self.path)).status_code, 404)
