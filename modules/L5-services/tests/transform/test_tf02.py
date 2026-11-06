"""Deterministic Transform configuration API scenarios."""

import json
import unittest
from pathlib import Path
from datetime import UTC, datetime, timedelta

import httpx
from jsonschema import Draft202012Validator
from fastapi import FastAPI, HTTPException
from l5_services.api.configuration import ConfigurationStore, view
from l5_services.api.bindings import BindingStore, view_operation
from l5_services.adapters.transform.bindings import TransformDefinitionCleanup
from l5_services.api.router import create_router
from l5_services.configuration_population import configuration_options
from l5_services.configuration_readiness import transform_capability_readiness
from l5_services.models.base import Base
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.binding import Binding
from l5_services.models.setup import BindingOperation, SetupIntent, SetupWork
from l5_services.workers.bindings import execute_setup_work
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def reference(product, kind, record_id, revision, connection="source-a"):
    return {"tenant_id": "tenant-a", "project_id": f"project-{product}",
            "source_connection_id": connection, "record_type": kind,
            "record_id": record_id, "record_revision": revision}


class TransformConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.profiles, self.available, self.repositories = {}, {}, {}
        for product in ("one", "two"):
            repository = reference(product, "repository_commit", f"example/{product}", "a" * 40)
            self.repositories[product] = repository
            self.profiles[product] = {
                "tenant_id": "tenant-a", "product_id": f"product-{product}",
                "project_id": f"project-{product}", "revision": 7,
                "product": {"purpose": f"Modernize {product}"},
                "configuration": {"basic": {"repositories": [repository]},
                                  "selected_capabilities": [{"capability": "refactor", "ownership_mode": "managed"}]},
            }
            self.available[product] = {"coverage": "complete", "next_cursor": None,
                                       "items": [{"provider": "aws", "connection_ref": {"id": "source-a"},
                                                  "record_ref": reference(product, "connection", "source-a", "rev-1"),
                                                  "resources": [repository]}]}
        self.qualified_executor_refs = {}
        owner = self

        class Backend:
            async def options(self, product_id, project_id, service_id):
                product = product_id.removeprefix("product-")
                return configuration_options(owner.profiles[product], owner.available[product], service_id)

            async def check(self, product_id, project_id, service_id):
                async with owner.sessions() as session:
                    configuration, version = await ConfigurationStore(session, "tenant-a", "user-a").current(
                        product_id, project_id, service_id)
                    readiness = transform_capability_readiness(
                        version.settings, qualified_executor_ref=owner.qualified_executor_refs.get(product_id))
                    return {"ready": False, "gaps": version.gaps, "provider_contacted": False,
                            "readiness": readiness}

            async def get(self, product_id, project_id, service_id):
                async with owner.sessions() as session:
                    return view(*await ConfigurationStore(session, "tenant-a", "user-a").current(
                        product_id, project_id, service_id))

            async def version(self, product_id, project_id, service_id, revision):
                async with owner.sessions() as session:
                    store = ConfigurationStore(session, "tenant-a", "user-a")
                    configuration, _ = await store.current(product_id, project_id, service_id)
                    version = await session.get(ConfigurationVersion, ("tenant-a", configuration.configuration_id, revision))
                    return view(configuration, version)

            async def save(self, product_id, project_id, service_id, settings, key, if_none_match, if_match):
                product = product_id.removeprefix("product-")
                profile = owner.profiles[product]
                profile_ref = reference(product, "product", product_id, str(profile["revision"]), "adp.profile")
                async with owner.sessions() as session:
                    store = ConfigurationStore(session, "tenant-a", "user-a")
                    if if_none_match == "*":
                        configuration, version = await store.create(
                            product_id, project_id, service_id, "1", profile, profile_ref,
                            owner.available[product], settings, key)
                    else:
                        configuration, previous = await store.current(product_id, project_id, service_id)
                        configuration, version = await store.edit(
                            configuration, previous, profile, owner.available[product], settings, if_match)
                    return view(configuration, version)

            async def refresh(self, product_id, project_id, service_id, profile_ref, key, if_match, *, choices=None):
                product = product_id.removeprefix("product-")
                async with owner.sessions() as session:
                    store = ConfigurationStore(session, "tenant-a", "user-a")
                    configuration, previous = await store.current(product_id, project_id, service_id)
                    configuration, version = await store.refresh(
                        configuration, previous, owner.profiles[product], profile_ref,
                        owner.available[product], if_match, choices=choices)
                    return view(configuration, version)

        app = FastAPI()
        app.include_router(create_router(lambda: Backend()))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test")
        self.provider_launches = []

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()

    def path(self, product):
        return f"/v1/products/product-{product}/projects/project-{product}/services/transform/configuration"

    def choices(self, product):
        repository = self.repositories[product]
        return {"service_id": "transform", "capabilities": ["refactor"], "enabled": False,
                "source_refs": [repository],
                "overrides": [{"field": "source_refs", "value": [repository], "reason": "Pin this commit"}],
                "transform": {"connection_ref": reference(product, "connection", "source-a", "rev-1"),
                              "executor_binding_ref": reference(product, "executor_binding", "executor", "rev-1", "executor-control"),
                              "test_policy_ref": reference(product, "test_policy", "policy", "rev-1"),
                              "region": "example-region", "read_paths": ["src"],
                              "write_paths": ["src/app.py"], "excluded_paths": [".git"],
                              "goal_defaults": {"kind": "refactor", "summary": "Simplify the module"}}}

    async def test_draft_repository_is_selectable_without_authorizing_execution(self):
        from l5_services.transform_contract import TransformConfigurationDraftInput, TransformConfigurationInput
        from pydantic import ValidationError

        repository = {**self.repositories["one"], "record_type": "repository", "record_revision": "draft-revision"}
        self.profiles["one"]["configuration"]["basic"]["repositories"] = [repository]
        self.available["one"]["items"][0]["resources"] = [repository]
        options = await self.client.get(self.path("one") + "/options")
        self.assertEqual(options.status_code, 200)
        self.assertEqual(options.json()["sources"], [repository])

        draft = self.choices("one")
        draft["source_refs"] = [repository]
        draft["overrides"][0]["value"] = [repository]
        created = await self.client.put(self.path("one"), json=draft, headers={
            "Idempotency-Key": "draft_repository_selection_00001", "If-None-Match": "*"})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["settings"]["source_refs"], [repository])
        fields = ("service_id", "profile_ref", "source_refs", "capabilities", "enabled", "transform")
        selected = {field: created.json()["settings"][field] for field in fields}
        TransformConfigurationDraftInput.model_validate(selected)
        with self.assertRaises(ValidationError):
            TransformConfigurationInput.model_validate(selected)
        with self.assertRaises(ValidationError):
            TransformConfigurationDraftInput.model_validate({**selected, "enabled": True})

    async def test_explicit_source_revision_can_refresh_without_recreating_configuration(self):
        for product in ("one", "two"):
            path = self.path(product)
            created = await self.client.put(path, json=self.choices(product), headers={
                "Idempotency-Key": f"create-refresh-{product}", "If-None-Match": "*"})
            self.assertEqual(created.status_code, 201, created.text)
            selected = {**self.repositories[product], "record_revision": "b" * 40}
            self.profiles[product]["revision"] = 8
            self.profiles[product]["configuration"]["basic"]["repositories"] = [selected]
            self.available[product]["items"][0]["resources"] = [selected]
            new_ref = {**created.json()["settings"]["profile_ref"], "record_revision": "8"}
            headers = {"Idempotency-Key": f"refresh-source-{product}", "If-Match": created.headers["etag"]}
            implicit = await self.client.post(path + "/refresh", json={"profile_ref": new_ref}, headers=headers)
            self.assertEqual(implicit.status_code, 409)
            for invalid in ([], {"source_refs": [selected]}, {
                    "source_refs": [self.repositories["two" if product == "one" else "one"]],
                    "overrides": []}):
                refused = await self.client.post(path + "/refresh", json={"profile_ref": new_ref, "choices": invalid}, headers=headers)
                self.assertIn(refused.status_code, (409, 422))
            choices = {"source_refs": [selected], "overrides": [
                {"field": "source_refs", "value": [selected], "reason": "Select the current authorized commit"}]}
            refreshed = await self.client.post(path + "/refresh", json={"profile_ref": new_ref, "choices": choices}, headers=headers)
            self.assertEqual(refreshed.status_code, 200, refreshed.text)
            self.assertEqual(refreshed.json()["configuration_id"], created.json()["configuration_id"])
            self.assertEqual(refreshed.json()["revision"], 2)
            self.assertEqual(refreshed.json()["state"], "disabled")
            self.assertEqual(refreshed.json()["settings"]["profile_ref"], new_ref)
            self.assertEqual(refreshed.json()["settings"]["source_refs"], [selected])
            self.assertEqual((await self.client.get(path + "/versions/1")).json(), created.json())
            replay = await self.client.post(path + "/refresh", json={"profile_ref": new_ref, "choices": choices}, headers=headers)
            self.assertEqual(replay.status_code, 412)
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(ConfigurationVersion)), 4)
            self.assertEqual(await session.scalar(select(func.count()).select_from(SetupIntent)), 0)
        self.assertEqual(self.provider_launches, [])

    async def test_readiness_gaps(self):
        self.profiles["one"]["configuration"]["selected_capabilities"].append(
            {"capability": "dependency_upgrade", "ownership_mode": "managed"})
        contracts = json.loads((Path(__file__).resolve().parents[4]
                                / "docs/l5-services/contracts/v1/schemas.json").read_text())

        def accepts(name, value):
            return Draft202012Validator({**contracts, "$ref": "#/$defs/" + name}).is_valid(value)

        path = self.path("one")
        options = await self.client.get(path + "/options")
        self.assertEqual(options.status_code, 200)
        self.assertTrue(accepts("Options", options.json()))
        self.assertEqual([item["capability"] for item in options.json()["readiness"]],
                         ["refactor", "dependency_upgrade"])
        self.assertEqual({gap["code"] for gap in options.json()["readiness"][0]["gaps"]},
                         {"test_policy_ref_missing", "executor_binding_ref_missing"})
        self.assertNotIn("region_options_unavailable", options.json()["unavailable_reasons"])
        self.assertNotIn("connection_revision_unavailable", options.json()["unavailable_reasons"])

        draft = self.choices("one")
        draft["enabled"] = True
        draft["capabilities"] = ["refactor"]
        del draft["transform"]["test_policy_ref"]
        created = await self.client.put(path, json=draft, headers={
            "Idempotency-Key": "readiness-draft-00001", "If-None-Match": "*"})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertTrue(accepts("TransformConfigurationDraftInput", {
            field: created.json()["settings"][field]
            for field in ("service_id", "profile_ref", "source_refs", "capabilities", "enabled", "transform")
        }))
        from pydantic import ValidationError
        from l5_services.transform_contract import TransformConfigurationInput
        with self.assertRaises(ValidationError):
            TransformConfigurationInput.model_validate({
                field: created.json()["settings"][field]
                for field in ("service_id", "profile_ref", "source_refs", "capabilities", "enabled", "transform")
            })
        self.assertIn("test_policy_ref_missing", created.json()["gaps"])
        self.assertNotIn("test_policy_ref", created.json()["settings"]["transform"])
        check = await self.client.post(path + "/check")
        self.assertEqual(check.status_code, 200)
        self.assertTrue(accepts("Check", check.json()))
        self.assertFalse(check.json()["ready"])
        self.assertFalse(check.json()["provider_contacted"])
        self.assertEqual([item["capability"] for item in check.json()["readiness"]], ["refactor"])
        self.assertEqual(check.json()["readiness"][0]["status"], "missing_input")
        self.assertEqual(check.json()["readiness"][0]["gaps"][0], {
            "code": "test_policy_ref_missing", "field": "transform.test_policy_ref",
            "owner": "product", "affected_step": "test"})

        edited = await self.client.put(path, json={"transform": {
            **self.choices("one")["transform"]}}, headers={
                "Idempotency-Key": "readiness-edit-00001", "If-Match": created.headers["etag"]})
        self.assertEqual(edited.status_code, 200, edited.text)
        self.qualified_executor_refs["product-one"] = reference(
            "one", "executor_binding", "executor", "rev-2", "executor-control")
        stale = await self.client.post(path + "/check")
        self.assertTrue(accepts("Check", stale.json()))
        self.assertFalse(stale.json()["ready"])
        self.assertEqual(stale.json()["readiness"][0]["status"], "stale")
        self.assertEqual(stale.json()["readiness"][0]["gaps"], [{
            "code": "executor_qualification_stale", "field": "transform.executor_binding_ref",
            "owner": "executor", "affected_step": "execute"}])
        self.assertEqual(self.provider_launches, [])

    async def test_setup_reconciles_lost_create_acknowledgement(self):
        profile = self.profiles["one"]
        profile_ref = reference("one", "product", "product-one", "7", "adp.profile")
        async with self.sessions() as session:
            configuration, version = await ConfigurationStore(session, "tenant-a", "user-a").create(
                "product-one", "project-one", "transform", "1", profile, profile_ref,
                self.available["one"], self.choices("one"))
        selected = {
            "configuration_id": configuration.configuration_id, "configuration_revision": version.revision,
            "scope": view(configuration, version)["scope"], "connection_ref": self.choices("one")["transform"]["connection_ref"],
            "executor_binding_ref": self.choices("one")["transform"]["executor_binding_ref"],
            "definition_ref": None, "transform_definition_create_qualified": True,
            "account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/ExampleRole",
            "region": "example-region", "authorization": {"actor_id": "user-a", "delegation_id": "example-grant",
                                                     "delegation_revision": 1},
        }
        inventory = {}
        create_attempts = []

        class Provider:
            async def read(self, native_ref, verified):
                return {"native_resource_ref": native_ref, "provider_identity": {
                    "account_id": verified["account_id"], "role_arn": verified["role_arn"],
                    "region": verified["region"]}, "state": "blocked", "gaps": ["executor_unqualified"]}

            async def create(self, intent, verified):
                create_attempts.append(intent.operation_id)
                native_ref = reference("one", "transform_definition", intent.operation_id, "native-rev-1")
                identity = {"account_id": verified["account_id"], "role_arn": verified["role_arn"],
                            "region": verified["region"]}
                receipt = {"operation_id": intent.operation_id, "intent_hash": intent.intent_hash,
                           "native_resource_ref": native_ref, "provider_identity": identity}
                inventory[intent.operation_id] = {"native_resource_ref": native_ref, "provider_identity": identity,
                                                  "creation_receipt": receipt, "state": "blocked", "gaps": ["native_unqualified"]}
                raise TimeoutError("acknowledgement lost after native create")

            async def reconcile(self, intent, verified, candidate_id):
                return inventory.get(intent.operation_id)

        provider = Provider()

        async def authorize(intent, claim):
            self.assertEqual(intent.tenant_id, "tenant-a")
            return selected, provider

        async with self.sessions() as session:
            store = BindingStore(session, "tenant-a", "product-one", "project-one", "transform")
            adopted = await store.start({"action": "link", "configuration_revision": 1,
                                         "native_resource_ref": selected["executor_binding_ref"]},
                                        "adopt-executor-00001", selected)
            self.assertEqual(adopted.state, "accepted")
            with self.assertRaises(HTTPException) as unqualified:
                await store.start({"action": "create", "configuration_revision": 1},
                                  "unqualified-create-00001", {**selected, "transform_definition_create_qualified": False})
            self.assertEqual(unqualified.exception.status_code, 503)
            created = await store.start({"action": "create", "configuration_revision": 1},
                                        "qualified-create-00001", selected)
            self.assertEqual((await store.start({"action": "create", "configuration_revision": 1},
                                                 "qualified-create-00001", selected)).operation_id, created.operation_id)
            work_items = (await session.scalars(select(SetupWork))).all()
            self.assertEqual(len(work_items), 2)
            envelopes = {work.operation_id: {"schema_version": "1.0", "work_id": work.work_id,
                                              "generation": work.generation} for work in work_items}
        fixed = datetime.now(UTC) + timedelta(minutes=1)
        self.assertTrue(await execute_setup_work(self.sessions, envelopes[adopted.operation_id], authorize, now=fixed))
        self.assertTrue(await execute_setup_work(self.sessions, envelopes[created.operation_id], authorize, now=fixed))
        async with self.sessions() as session:
            pending = await session.get(BindingOperation, ("tenant-a", created.operation_id))
            self.assertEqual(pending.state, "outcome_unknown")
            self.assertEqual(len((await session.scalars(select(Binding))).all()), 1)
        self.assertTrue(await execute_setup_work(self.sessions, envelopes[created.operation_id], authorize,
                                                  now=fixed + timedelta(minutes=6)))
        async with self.sessions() as session:
            rows = (await session.scalars(select(Binding))).all()
            self.assertEqual({binding.ownership for binding in rows}, {"customer_linked", "adp_created"})
            owned = next(binding for binding in rows if binding.ownership == "adp_created")
            intent = await session.get(SetupIntent, ("tenant-a", created.operation_id))
            operation = await session.get(BindingOperation, ("tenant-a", created.operation_id))
            self.assertEqual(operation.state, "succeeded")
            self.assertEqual(owned.creation_receipt["intent_hash"], intent.intent_hash)
            self.assertEqual(owned.native_resource_ref, inventory[created.operation_id]["native_resource_ref"])
        self.assertEqual(create_attempts, [created.operation_id])

    async def test_owned_setup_cleanup(self):
        profile = self.profiles["one"]
        async with self.sessions() as session:
            configuration, version = await ConfigurationStore(session, "tenant-a", "user-a").create(
                "product-one", "project-one", "transform", "1", profile,
                reference("one", "product", "product-one", "7", "adp.profile"),
                self.available["one"], self.choices("one"))
        selected = {"configuration_id": configuration.configuration_id, "configuration_revision": version.revision,
                    "scope": view(configuration, version)["scope"],
                    "connection_ref": self.choices("one")["transform"]["connection_ref"],
                    "executor_binding_ref": self.choices("one")["transform"]["executor_binding_ref"],
                    "definition_ref": None, "transform_definition_create_qualified": True,
                    "account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/ExampleRole",
                    "region": "example-region", "authorization": {"actor_id": "user-a",
                                                               "delegation_id": "example-grant", "delegation_revision": 1}}
        identity = {field: selected[field] for field in ("account_id", "role_arn", "region")}
        resources = {"unrelated": {"native_resource_ref": reference("one", "transform_definition", "unrelated", "1"),
                                    "provider_identity": identity}}
        deleted = []
        absence_available = [False]

        class Native:
            async def read(self, resource, verified):
                record_id = resource["record_id"]
                if record_id in resources:
                    return resources[record_id]
                if absence_available[0]:
                    return {"native_resource_ref": resource, "absent": True,
                            "absence_evidence_ref": reference("one", "transform_absence", record_id, "readback-1")}
                return {"native_resource_ref": resource, "absent": True}

            async def delete(self, resource, verified):
                deleted.append(resource["record_id"])
                resources.pop(resource["record_id"], None)

        native = Native()

        class Provider:
            async def create(self, intent, verified):
                resource = reference("one", "transform_definition", intent.operation_id, "native-rev-1")
                receipt = {"operation_id": intent.operation_id, "intent_hash": intent.intent_hash,
                           "native_resource_ref": resource, "provider_identity": identity}
                resources[intent.operation_id] = {"native_resource_ref": resource, "provider_identity": identity,
                                                  "creation_receipt": receipt, "state": "blocked"}
                return resources[intent.operation_id]

            async def read(self, resource, verified):
                return {"native_resource_ref": resource, "provider_identity": identity,
                        "state": "blocked", "gaps": ["executor_unqualified"]}

            async def cleanup(self, resource, verified, receipt, intent):
                return await TransformDefinitionCleanup(native).cleanup(resource, verified, receipt, intent)

        provider = Provider()

        async def authorize(intent, claim):
            self.assertEqual(intent.scope, selected["scope"])
            return selected, provider

        async def perform(operation_id):
            async with self.sessions() as session:
                work = await session.scalar(select(SetupWork).where(
                    SetupWork.tenant_id == "tenant-a", SetupWork.operation_id == operation_id))
                envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation}
            self.assertTrue(await execute_setup_work(self.sessions, envelope, authorize,
                                                      now=datetime.now(UTC) + timedelta(minutes=1)))
            async with self.sessions() as session:
                return await session.get(BindingOperation, ("tenant-a", operation_id))

        async with self.sessions() as session:
            store = BindingStore(session, "tenant-a", "product-one", "project-one", "transform")
            owned_operation = await store.start({"action": "create", "configuration_revision": 1},
                                                "create-to-clean-0001", selected)
            adopted_operation = await store.start({"action": "link", "configuration_revision": 1,
                                                   "native_resource_ref": selected["executor_binding_ref"]},
                                                  "adopt-to-keep-00001", selected)
        owned_result = await perform(owned_operation.operation_id)
        adopted_result = await perform(adopted_operation.operation_id)
        async with self.sessions() as session:
            store = BindingStore(session, "tenant-a", "product-one", "project-one", "transform")
            owned = await store.binding(owned_result.binding_id)
            adopted = await store.binding(adopted_result.binding_id)
            self.assertIsNone(adopted.creation_receipt)
            adopted_request = {"action": "cleanup", "configuration_revision": 1,
                               "binding_id": adopted.binding_id, "owned_cleanup_confirmed": True}
            with self.assertRaises(HTTPException) as denied:
                await store.control(adopted_request, "cannot-delete-adopted", f'"{adopted.revision}"', selected)
            self.assertEqual(denied.exception.status_code, 403)
            owned_request = {**adopted_request, "binding_id": owned.binding_id}
            cleanup = await store.control(owned_request, "owned-cleanup-00001", f'"{owned.revision}"', selected)
            intent = await session.get(SetupIntent, ("tenant-a", cleanup.operation_id))
            self.assertEqual(intent.owned_resource_receipt, owned.creation_receipt)
        first = await perform(cleanup.operation_id)
        self.assertEqual(first.state, "failed")
        self.assertIsNone(first.cleanup_receipt)
        self.assertEqual(deleted, [owned_operation.operation_id])
        self.assertIn("unrelated", resources)
        absence_available[0] = True
        async with self.sessions() as session:
            store = BindingStore(session, "tenant-a", "product-one", "project-one", "transform")
            owned = await store.binding(owned_result.binding_id)
            self.assertEqual(owned.state, "cleanup_failed")
            retried = await store.control(owned_request, "owned-cleanup-retry-0001",
                                          f'"{owned.revision}"', selected)
        final = await perform(retried.operation_id)
        self.assertEqual(final.state, "succeeded")
        self.assertEqual(view_operation(final)["cleanup_receipt"]["absent"], True)
        self.assertEqual(deleted, [owned_operation.operation_id])
        async with self.sessions() as session:
            stored = await session.get(BindingOperation, ("tenant-a", retried.operation_id))
            binding = await session.get(Binding, ("tenant-a", owned_result.binding_id))
            adopted = await session.get(Binding, ("tenant-a", adopted_result.binding_id))
            work = await session.scalar(select(SetupWork).where(SetupWork.operation_id == retried.operation_id))
            self.assertEqual(binding.state, "cleaned")
            self.assertEqual(adopted.ownership, "customer_linked")
            self.assertEqual(adopted.state, "blocked")
            self.assertEqual(stored.cleanup_receipt["creation_receipt"], binding.creation_receipt)
            self.assertEqual(stored.cleanup_receipt["absence_evidence_ref"]["record_id"],
                             owned_operation.operation_id)
            self.assertIsNotNone(work.completed_at)
        self.assertIn("unrelated", resources)

    async def test_scope_denial(self):
        choices = self.choices("one")
        path = self.path("one")
        foreign = {**self.repositories["one"], "tenant_id": "tenant-other"}
        wrong_project = {**self.repositories["one"], "project_id": "project-two"}
        unlisted = {**self.repositories["one"], "record_id": "example/unlisted"}
        for source in (foreign, wrong_project, unlisted):
            attempt = {**choices, "source_refs": [source],
                       "overrides": [{"field": "source_refs", "value": [source], "reason": "Not a grant"}]}
            response = await self.client.put(path, json=attempt, headers={
                "Idempotency-Key": "denied-source-choice-001", "If-None-Match": "*"})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertNotIn("example/unlisted", response.text)
        for field in ("connection_ref", "executor_binding_ref", "test_policy_ref"):
            nested = {**choices["transform"], field: {**choices["transform"][field],
                                                        "tenant_id": "tenant-other"}}
            response = await self.client.put(path, json={**choices, "transform": nested}, headers={
                "Idempotency-Key": "denied-transform-ref-001", "If-None-Match": "*"})
            self.assertEqual(response.status_code, 422, response.text)
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(ConfigurationVersion)), 0)
        self.assertEqual(self.provider_launches, [])

    async def test_configuration_roundtrip(self):
        for product in ("one", "two"):
            path = self.path(product)
            options = await self.client.get(path + "/options")
            self.assertEqual(options.status_code, 200, options.text)
            self.assertEqual(options.json()["sources"], [self.repositories[product]])
            choices = self.choices(product)
            if product == "two":
                choices.pop("source_refs")
                choices.pop("overrides")
            created = await self.client.put(path, json=choices, headers={
                "Idempotency-Key": f"create-transform-{product}", "If-None-Match": "*"})
            self.assertEqual(created.status_code, 201, created.text)
            self.assertEqual(created.headers["etag"], f'"{created.json()["hash"]}"')
            self.assertEqual(created.json()["settings"]["source_refs"], [self.repositories[product]])
            self.assertEqual((await self.client.get(path)).json(), created.json())
            edited = await self.client.put(path, json={"transform": {
                **self.choices(product)["transform"], "write_paths": ["src/updated.py"]}}, headers={
                "Idempotency-Key": f"edit-transform-{product}", "If-Match": created.headers["etag"]})
            self.assertEqual(edited.status_code, 200, edited.text)
            self.assertEqual(edited.json()["revision"], 2)
            self.assertEqual((await self.client.get(path + "/versions/1")).json(), created.json())
            self.profiles[product] = {**self.profiles[product], "revision": 8,
                                      "product": {"purpose": f"Updated {product}"}}
            profile_ref = {**created.json()["settings"]["profile_ref"], "record_revision": "8"}
            refreshed = await self.client.post(path + "/refresh", json={"profile_ref": profile_ref}, headers={
                "Idempotency-Key": f"refresh-transform-{product}", "If-Match": edited.headers["etag"]})
            self.assertEqual(refreshed.status_code, 200, refreshed.text)
            self.assertEqual(refreshed.json()["revision"], 3)
            self.assertEqual(refreshed.json()["settings"]["transform"]["write_paths"], ["src/updated.py"])
            self.assertEqual(refreshed.json()["settings"]["purpose"], f"Updated {product}")
            self.assertNotEqual(refreshed.headers["etag"], edited.headers["etag"])
        async with self.sessions() as session:
            configurations = (await session.scalars(select(Configuration))).all()
            self.assertEqual(len(configurations), 2)
            self.assertEqual(await session.scalar(select(func.count()).select_from(ConfigurationVersion)), 6)
            for configuration in configurations:
                self.assertEqual(configuration.current_revision, 3)
                first = await session.get(ConfigurationVersion, ("tenant-a", configuration.configuration_id, 1))
                latest = await session.get(ConfigurationVersion, ("tenant-a", configuration.configuration_id, 3))
                self.assertEqual(first.settings["transform"]["write_paths"], ["src/app.py"])
                self.assertEqual(latest.settings["transform"]["write_paths"], ["src/updated.py"])
                self.assertNotEqual(first.hash, latest.hash)
        self.assertEqual(self.provider_launches, [])
