"""Pinned offline TF07 scenarios; installed evaluation remains a separate phase."""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from datetime import UTC, datetime, timedelta
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from fastapi import FastAPI, Header, HTTPException
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services import migrations
from l5_services.adapters.transform.dependency_oracle import compare_dependency_upgrade
from l5_services.adapters.transform.provenance import compare_source_trees
from l5_services.api.configuration import ConfigurationStore, view
from l5_services.api.router import create_router
from l5_services.events.schedules import receive_schedule_tick
from l5_services.configuration_population import configuration_options
from l5_services.configuration_readiness import transform_capability_readiness
from l5_services.migrations.versions import l5_transform_cleanup
from l5_services.models.base import Base
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.event import EventReceipt
from l5_services.models.run import Run, view_run
from l5_services.transform_boundary_probe import SURFACES
from l5_services.transform_cleanup_scenario import verify_ec05
from l5_services.transform_client_scenario import verify_ec06
from l5_services.transform_boundary_scenario import verify_ec02
from l5_services.transform_native_scenario import verify_ec03
from l5_services.transform_portability_scenario import verify_ec08
from l5_services.transform_recovery_probe import FAULT_ACTIONS
from l5_services.transform_recovery_scenario import verify_ec04
from l5_services.transform_release_scenario import RUNTIME_ENTRIES, verify_ec01
from l5_services.transform_reuse_scenario import verify_ec07
from transform.test_tf07_cleanup import (
    ADOPTED as EC05_ADOPTED,
    FixtureDriver as CleanupFixtureDriver,
    NOW as EC05_NOW,
    OWNED as EC05_OWNED,
    PATCH as EC05_PATCH,
    SCOPE as EC05_SCOPE,
)
from transform.test_tf07_clients import FixtureDriver as ClientFixtureDriver, SCOPE as CLIENT_SCOPE
from transform.test_tf07_native import FakeDriver, PINS
from transform.test_tf07_portability import FixtureDriver as PortabilityFixtureDriver
from transform.test_tf07_recovery import FixtureDriver as RecoveryFixtureDriver, SCOPE as RECOVERY_SCOPE
from transform.test_tf07_reuse import FixtureDriver as ReuseFixtureDriver, NOW as REUSE_NOW, SCOPE as REUSE_SCOPE


SOURCE = "a" * 40


def reference(name, kind, record_id, revision, connection="source-a"):
    return {"tenant_id": "tenant-example", "project_id": f"project-{name}",
            "source_connection_id": connection, "record_type": kind,
            "record_id": record_id, "record_revision": revision}


class ScenarioDriver:
    def __init__(self):
        wheel = io.BytesIO()
        with zipfile.ZipFile(wheel, "w") as package:
            for name in sorted(RUNTIME_ENTRIES):
                package.writestr(name, b"packaged offline entry point")
        self.wheel = wheel.getvalue()
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as workers:
            workers.writestr("release/offline-example.whl", self.wheel)
            workers.writestr("release/wheel.json", json.dumps({
                "wheel_filename": "offline-example.whl", "wheel_sha256": sha256(self.wheel).hexdigest()}))
        self.archive = archive.getvalue()
        self.release = {"phase": "artifacts", "service": "transform", "source_revision": SOURCE,
                        "manifest": {"key": "private/manifest", "version_id": "v1"},
                        "artifacts": {name: {"key": f"private/{name}", "version_id": "v1",
                                             "sha256": sha256(data).hexdigest()}
                                      for name, data in (("wheel", self.wheel), ("archive", self.archive))}}
        self.migration = self._migrate()
        self.engine = None
        self.calls = []

    def _migrate(self):
        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE l5_service_binding_operations "
                                        "(operation_id VARCHAR(36) PRIMARY KEY, service_id VARCHAR(32))"))
                connection.execute(text("INSERT INTO l5_service_binding_operations "
                                        "(operation_id, service_id) VALUES "
                                        "('security-old', 'security'), ('devops-old', 'devops')"))
                before = [dict(row._mapping) for row in connection.execute(text(
                    "SELECT operation_id, service_id FROM l5_service_binding_operations ORDER BY operation_id"))]
                with Operations.context(MigrationContext.configure(connection)):
                    l5_transform_cleanup.upgrade()
                after = [dict(row._mapping) for row in connection.execute(text(
                    "SELECT operation_id, service_id FROM l5_service_binding_operations ORDER BY operation_id"))]
                column = "cleanup_receipt" in {field["name"] for field in inspect(connection).get_columns(
                    "l5_service_binding_operations")}
            head = ScriptDirectory(str(Path(migrations.__file__).parent)).get_current_head()
            return {"head": head, "cleanup_column": column, "before_rows": before,
                    "after_rows": after, "evidence_ref": "offline/sql-migration"}
        finally:
            engine.dispose()

    async def setup(self, tmp_path):
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'configuration.db'}")
        async with self.engine.begin() as database:
            await database.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.profiles = {}
        self.available = {}
        self.qualified = {}
        async with self.sessions() as session:
            for name in ("ready", "missing_input"):
                project, product = f"project-{name}", f"product-{name}"
                repository = reference(name, "repository_commit", f"example/{name}", SOURCE)
                executor = reference(name, "executor_binding", "executor-a", "rev-1", "executor-control")
                settings = {"service_id": "transform", "capabilities": ["refactor"],
                            "enabled": False, "visibility": "only_me", "source_refs": [repository],
                            "transform": {"executor_binding_ref": executor,
                                          **({"test_policy_ref": reference(name, "test_policy", "policy-a", "rev-1")}
                                             if name == "ready" else {})}}
                self.qualified[name] = executor
                self.profiles[name] = {"tenant_id": "tenant-example", "product_id": product,
                                       "project_id": project, "configuration": {
                                           "selected_capabilities": [{"capability": "refactor", "ownership_mode": "managed"}]}}
                self.available[name] = {"coverage": "complete", "next_cursor": None,
                                        "items": [{"connection_ref": {"id": "source-a"},
                                                   "record_ref": reference(name, "connection", "source-a", "rev-1"),
                                                   "resources": [repository]}]}
                configuration = Configuration(tenant_id="tenant-example", configuration_id=f"config-{name}",
                                              product_id=product, project_id=project, service_id="transform",
                                              relationship_revision="1", owner_id="owner-example",
                                              current_revision=1, handoff_state="linked")
                version = ConfigurationVersion(tenant_id="tenant-example", configuration_id=f"config-{name}",
                                               revision=1, state="disabled", settings=settings,
                                               inherited_fields=[], gaps=[],
                                               hash=sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest())
                session.add_all((configuration, version))
            await session.commit()
        driver = self

        class Backend:
            async def options(self, product_id, project_id, service_id):
                driver.calls.append("options")
                name = project_id.removeprefix("project-")
                return configuration_options(driver.profiles[name], driver.available[name], service_id)

            async def get(self, product_id, project_id, service_id):
                driver.calls.append("configuration")
                async with driver.sessions() as session:
                    return view(*await ConfigurationStore(session, "tenant-example", "owner-example").current(
                        product_id, project_id, service_id))

            async def check(self, product_id, project_id, service_id):
                driver.calls.append("check")
                name = project_id.removeprefix("project-")
                async with driver.sessions() as session:
                    _, version = await ConfigurationStore(session, "tenant-example", "owner-example").current(
                        product_id, project_id, service_id)
                    return {"ready": False, "provider_contacted": False,
                            "readiness": transform_capability_readiness(
                                version.settings, qualified_executor_ref=driver.qualified[name])}

        app = FastAPI()
        app.include_router(create_router(lambda: Backend()))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline.test")

    async def close(self):
        await self.client.aclose()
        await self.engine.dispose()

    async def read_release(self):
        self.calls.append("release")
        return deepcopy(self.release)

    async def read_artifact(self, reference):
        self.calls.append("versioned-artifact")
        assert reference["version_id"] == "v1"
        return self.wheel if reference["key"] == "private/wheel" else self.archive

    async def read_migration(self):
        self.calls.append("sql-migration")
        return deepcopy(self.migration)

    async def read_configuration(self, name):
        path = f"/v1/products/product-{name}/projects/project-{name}/services/transform/configuration"
        options = await self.client.get(path + "/options")
        configuration = await self.client.get(path)
        check = await self.client.post(path + "/check")
        async with self.sessions() as session:
            _, version = await ConfigurationStore(session, "tenant-example", "owner-example").current(
                f"product-{name}", f"project-{name}", "transform")
            settings = deepcopy(version.settings)
        assert (options.status_code, check.status_code) == (200, 200)
        return {"status": configuration.status_code, "configuration": configuration.json(),
                "sql_settings": settings, "options": options.json(), "check": check.json(),
                "etag": configuration.headers["etag"], "scope": configuration.json()["scope"],
                "qualified_executor_ref": self.qualified[name],
                "evidence_ref": f"offline/{name}/api-and-sql"}


@pytest.fixture
async def scenario(tmp_path):
    driver = ScenarioDriver()
    await driver.setup(tmp_path)
    yield driver
    await driver.close()


@pytest.mark.asyncio
async def test_ec01(scenario):
    report = await verify_ec01(scenario, source_revision=SOURCE, schema_head=scenario.migration["head"])
    assert report["live_qualified"] is False
    assert report["profiles"]["ready"]["readiness"][0]["status"] == "ready"
    assert report["profiles"]["missing_input"]["readiness"][0]["status"] == "missing_input"
    assert scenario.calls == ["release", "versioned-artifact", "versioned-artifact", "sql-migration",
                              "options", "configuration", "check", "options", "configuration", "check"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["wrong_release", "changed_bytes", "lost_old_row", "unqualified", "wrong_profile"])
async def test_ec01_rejects_unpinned_or_misleading_readback(scenario, change):
    if change == "wrong_release":
        scenario.release["source_revision"] = "b" * 40
    elif change == "changed_bytes":
        scenario.wheel = b"changed wheel"
    elif change == "lost_old_row":
        scenario.migration["after_rows"].pop()
    elif change == "unqualified":
        scenario.qualified["ready"] = None
    elif change == "wrong_profile":
        original = scenario.read_configuration

        async def foreign(name):
            observation = await original(name)
            if name == "missing_input":
                observation["sql_settings"]["source_refs"][0]["project_id"] = "foreign"
            return observation

        scenario.read_configuration = foreign
    with pytest.raises(ValueError):
        await verify_ec01(scenario, source_revision=SOURCE, schema_head=scenario.migration["head"])


EC02_OWNER = {"tenant_id": "tenant-example", "product_id": "product-example",
              "project_id": "project-example"}
EC02_FOREIGN = {"tenant_id": "tenant-foreign", "product_id": "product-foreign",
                "project_id": "project-foreign"}
EC02_REFERENCE = {"tenant_id": "tenant-example", "project_id": "project-example",
                  "record_id": "result-example"}
EC02_DENIAL = b'{"detail":{"code":"not_found"}}'


class BoundaryScenarioDriver:
    def __init__(self):
        self.calls = []
        self.raw = []
        self.items = {"owned": [EC02_REFERENCE["record_id"]], "foreign": []}
        self.before = deepcopy(self.items)
        self.effects = 0
        self.report_patch = {}
        self.oracle_patch = None
        self.allow_hostile = False
        self.pod_credentials = False

        class Backend:
            def __init__(self, actor):
                self.actor = actor

            async def get(self, product_id, project_id, service_id):
                if (self.actor != EC02_OWNER["tenant_id"]
                        or (product_id, project_id, service_id) != (
                            EC02_OWNER["product_id"], EC02_OWNER["project_id"], "transform")):
                    raise HTTPException(404, detail={"code": "not_found"})
                return {"hash": "offline-example", "resource_id": EC02_REFERENCE["record_id"]}

        async def dependency(x_actor: str = Header(..., alias="X-Actor")):
            return Backend(x_actor)

        app = FastAPI()
        app.include_router(create_router(dependency))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline.test")

    def source_response(self, actor, path):
        allowed = actor == EC02_OWNER and path == f"{EC02_OWNER['project_id']}/{EC02_REFERENCE['record_id']}"
        if self.allow_hostile and path.startswith("../"):
            allowed = True
        return (200, json.dumps({"resource_id": EC02_REFERENCE["record_id"]}).encode()) if allowed else (
            403, b"")

    async def read(self, surface, actor, reference):
        self.calls.append((surface, actor))
        allowed = actor == EC02_OWNER
        if surface == "client":
            path = (f"/v1/products/{reference['project_id'].replace('project-', 'product-')}/"
                    f"projects/{reference['project_id']}/services/transform/configuration")
            response = await self.client.get(path, headers={"X-Actor": actor["tenant_id"]})
            status, body = response.status_code, response.content
        elif surface == "source":
            status, body = self.source_response(actor, f"{reference['project_id']}/{reference['record_id']}")
        else:
            status = 200 if allowed else 404
            body = (json.dumps({"resource_id": reference["record_id"]}).encode()
                    if allowed else EC02_DENIAL)
        evidence_ref = f"offline/{surface}/{'owned' if allowed else 'foreign'}"
        self.raw.append({"surface": surface, "actor": deepcopy(actor), "reference": deepcopy(reference),
                         "evidence_ref": evidence_ref, "status": status, "body": body,
                         "resource_ids": [reference["record_id"]] if status == 200 else [],
                         "effect_count": self.effects})
        report = {"actor": actor, "reference": reference, "source": SURFACES[surface],
                  "evidence_ref": evidence_ref, "status": status, "effect_count": self.effects}
        if status == 200:
            report["resource_id"] = reference["record_id"]
        else:
            report["body"] = {"detail": {"code": "not_found"}}
        return {**report, **self.report_patch.get((surface, allowed), {})}

    async def probe_executor(self, scope):
        self.calls.append(("executor", scope))
        probes = {name: True for name in (
            "allowed_source_read", "metadata_denied", "egress_denied", "credentials_denied",
            "source_escape_denied", "host_escape_denied")}
        if self.pod_credentials:
            probes["credentials_denied"] = False
        return {"source": "kubernetes-api", "scope": scope,
                "evidence_ref": "offline/pod/uid-example", "probes": probes,
                "pod": {"metadata": {"uid": "uid-example"}, "spec": {
                    "automountServiceAccountToken": False, "enableServiceLinks": False,
                    "hostNetwork": False, "hostPID": False, "hostIPC": False,
                    "volumes": [{"name": "source", "projected": {
                        "sources": [{"configMap": {"name": "source-example"}}]}}],
                    "containers": [{"env": [
                        {"name": "HOME", "value": "/tmp"},
                        {"name": "BG_CONFIG_DIR", "value": "/tmp/bg"},
                        {"name": "TMPDIR", "value": "/tmp"}],
                        "securityContext": {"readOnlyRootFilesystem": True,
                                            "allowPrivilegeEscalation": False,
                                            "capabilities": {"drop": ["ALL"]}}}]}}}

    async def read_denial_oracle(self, owner, foreign, reference):
        self.calls.append(("oracle", foreign))
        path = f"../{foreign['project_id']}/.credentials"
        status, body = self.source_response(owner, path)
        hostile = {"path": path, "source": "source-api", "status": status,
                   "body": body, "effect_count": self.effects,
                   "evidence_ref": "offline/source/hostile"}
        oracle = {"traces": deepcopy(self.raw), "hostile_source": hostile,
                  "inventory": {"before": deepcopy(self.before), "after": deepcopy(self.items),
                                "effect_count": self.effects, "evidence_ref": "offline/inventory"}}
        if self.oracle_patch:
            self.oracle_patch(oracle)
        return oracle


@pytest.fixture
async def ec02_driver():
    driver = BoundaryScenarioDriver()
    yield driver
    await driver.client.aclose()


@pytest.mark.asyncio
async def test_ec02(ec02_driver):
    result = await verify_ec02(ec02_driver, owner=EC02_OWNER, foreign=EC02_FOREIGN,
                               reference=EC02_REFERENCE)
    assert result["live_qualified"] is False
    assert len(result["boundary_refs"]) == 7
    assert [item["status"] for item in ec02_driver.raw] == [200, 404, 200, 404, 200, 403]
    assert ec02_driver.calls == [(surface, actor) for surface in SURFACES
                                 for actor in (EC02_OWNER, EC02_FOREIGN)] + [
                                     ("executor", EC02_OWNER), ("oracle", EC02_FOREIGN)]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "false_report", "foreign_payload", "foreign_effect", "wrong_actor", "missing_raw",
    "hostile_allowed", "hostile_leak", "pod_credentials", "inventory_changed", "reused_evidence",
])
async def test_ec02_rejects_denial_without_independent_oracle(ec02_driver, failure):
    if failure == "false_report":
        ec02_driver.report_patch[("broker", False)] = {"status": 200}
    elif failure == "foreign_payload":
        ec02_driver.oracle_patch = lambda oracle: oracle["traces"][1].update(body=b"private")
    elif failure == "foreign_effect":
        ec02_driver.oracle_patch = lambda oracle: oracle["traces"][3].update(effect_count=1)
    elif failure == "wrong_actor":
        ec02_driver.oracle_patch = lambda oracle: oracle["traces"][1].update(actor=EC02_OWNER)
    elif failure == "missing_raw":
        ec02_driver.oracle_patch = lambda oracle: oracle["traces"].pop()
    elif failure == "hostile_allowed":
        ec02_driver.allow_hostile = True
    elif failure == "hostile_leak":
        ec02_driver.oracle_patch = lambda oracle: oracle["hostile_source"].update(body=b"private")
    elif failure == "pod_credentials":
        ec02_driver.pod_credentials = True
    elif failure == "inventory_changed":
        ec02_driver.items["owned"].clear()
    elif failure == "reused_evidence":
        ec02_driver.oracle_patch = lambda oracle: oracle["inventory"].update(
            evidence_ref="offline/pod/uid-example")
    with pytest.raises(ValueError):
        await verify_ec02(ec02_driver, owner=EC02_OWNER, foreign=EC02_FOREIGN,
                          reference=EC02_REFERENCE)


@pytest.mark.asyncio
async def test_ec02_refuses_same_tenant_project_sibling_without_scoped_denial(ec02_driver):
    sibling = {**EC02_FOREIGN, "tenant_id": EC02_OWNER["tenant_id"]}
    with pytest.raises(ValueError):
        await verify_ec02(ec02_driver, owner=EC02_OWNER, foreign=sibling, reference=EC02_REFERENCE)
    assert ec02_driver.raw[1]["status"] == 200


class NativeScenarioDriver(FakeDriver):
    async def read_run(self, run_id):
        assert run_id == self.run["run_id"]
        return deepcopy(self.run)

    async def read_result(self, run_id):
        assert run_id == self.result["run_id"]
        return deepcopy(self.result)

    async def read_native(self, native):
        assert native == self.run["native_job_ref"]
        self.native_reads += 1
        return deepcopy(self.observed_native)

    async def read_trees(self, source, native):
        assert source == self.run["input"]["sources"][0] and native == self.run["native_job_ref"]
        return deepcopy(self.trees)

    async def read_artifact(self, reference):
        assert reference == self.result["retained_artifacts"]["original_patch"]
        return self.patch_bytes


class ProcessCheckRunner:
    def __init__(self, authority):
        self.authority = authority
        self.stages = []
        self.logs = {}
        self.fail_after = False

    async def run_check(self, *, stage, files, source_tree, file_modes, command,
                        image_digest, bundle_sha256, oracle_version, authority):
        assert authority is self.authority and set(files) == set(file_modes)
        assert command["argv"] == [sys.executable, "-m", "unittest", "discover", "-v"]
        with tempfile.TemporaryDirectory(prefix="tf07-native-") as checkout:
            for name, contents in files.items():
                destination = Path(checkout, name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(contents)
            if self.fail_after and stage == "after":
                Path(checkout, "test_fail.py").write_text(
                    "import unittest\nclass TestFailure(unittest.TestCase):\n"
                    "    def test_failure(self): self.fail('offline control')\n")
            completed = subprocess.run(command["argv"], cwd=checkout, capture_output=True,
                                       timeout=command["timeout_seconds"],
                                       env={"PATH": os.environ.get("PATH", ""),
                                            "PYTHONPATH": checkout, **command["environment"]}, check=False)
        self.stages.append(stage)
        count = re.search(rb"Ran (\d+) tests?", completed.stderr)
        receipts = {}
        for name, contents in (("stdout", completed.stdout), ("stderr", completed.stderr)):
            artifact_id = f"{stage}-{name}-{len(self.stages)}"
            self.logs[artifact_id] = contents
            receipts[name] = {"artifact_id": artifact_id, "version": "offline-1",
                              "sha256": sha256(contents).hexdigest(), "bytes": len(contents)}
        return {"stage": stage, "argv": command["argv"], "cwd": command["cwd"],
                "environment": command["environment"], "image_digest": image_digest,
                "source_tree": source_tree, "bundle_sha256": bundle_sha256,
                "lock_sha256": self.policy["lock_sha256"], "oracle_version": oracle_version,
                "started_at": "2026-01-01T00:00:00Z", "ended_at": "2026-01-01T00:00:01Z",
                "exit_code": completed.returncode, "timed_out": False,
                "test_count": int(count.group(1)) if count else 0, **receipts}

    async def read_log(self, *, reference, authority):
        assert authority is self.authority
        return self.logs[reference["artifact_id"]]


def git_fixture_patch(checkout, base, candidate):
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    for name, contents in base.items():
        destination = checkout / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(contents)
    subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(checkout), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", "commit", "-qm", "base"], check=True)
    revision = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"]).decode().strip()
    for name, contents in candidate.items():
        (checkout / name).write_bytes(contents)
    patch = subprocess.check_output(["git", "-C", str(checkout), "diff", "--binary"])
    return revision, patch


def upgrade_snapshot(version, artifact_hash, test_bytes):
    return {"pyproject.toml": (f'[project]\nname = "sample"\ndependencies = '
                               f'["sample-library=={version}"]\n').encode(),
            "uv.lock": (f'version = 1\n[[package]]\nname = "sample-library"\n'
                        f'version = "{version}"\nsdist = {{hash = "{artifact_hash}"}}\n').encode(),
            "test_manifest.py": test_bytes}


async def native_case(tmp_path, capability):
    name = "refactor" if capability == "refactor" else "upgrade"
    target_hash = "sha256:" + sha256(b"fictional-target").hexdigest()
    if capability == "refactor":
        test_bytes = (b"import unittest\nfrom src.app import total\n"
                      b"class TestApp(unittest.TestCase):\n"
                      b"    def test_total(self): self.assertEqual(total([1, 2]), 3)\n")
        base = {"src/__init__.py": b"",
                "src/app.py": b"def total(values):\n    return sum(values)\n",
                "test_app.py": test_bytes}
        candidate = {**base, "src/app.py": b"def total(values):\n"
                     b"    return sum(value for value in values)\n"}
    else:
        test_bytes = (b"import unittest\nimport tomllib\nfrom pathlib import Path\n"
                      b"class TestManifest(unittest.TestCase):\n"
                      b"    def test_lock_matches_manifest(self):\n"
                      b"        manifest = tomllib.loads(Path('pyproject.toml').read_text())\n"
                      b"        lock = tomllib.loads(Path('uv.lock').read_text())\n"
                      b"        version = manifest['project']['dependencies'][0].split('==')[1]\n"
                      b"        self.assertIn(version, ('1.0.0', '2.1.0'))\n"
                      b"        self.assertEqual(lock['package'][0]['version'], version)\n")
        base = upgrade_snapshot("1.0.0", "sha256:" + sha256(b"fictional-old").hexdigest(), test_bytes)
        candidate = upgrade_snapshot("2.1.0", target_hash, test_bytes)
    revision, patch = git_fixture_patch(tmp_path / name, base, candidate)
    driver = NativeScenarioDriver()
    driver.run = deepcopy(driver.run)
    driver.result = deepcopy(driver.result)
    scope = {"tenant_id": "tenant-example", "product_id": f"product-{name}",
             "project_id": f"project-{name}"}
    source = {**driver.run["input"]["sources"][0], "tenant_id": scope["tenant_id"],
              "project_id": scope["project_id"], "record_revision": revision,
              "record_id": f"repo-{name}"}
    native = {**driver.run["native_job_ref"], "tenant_id": scope["tenant_id"],
              "project_id": scope["project_id"], "native_id": f"session-{name}",
              "executor_uid": f"pod-{name}", "attempt_token": f"token-{name}"}
    driver.run.update(run_id=f"run-{name}", scope=scope, native_job_ref=native)
    driver.run["input"]["sources"] = [source]
    driver.run["admission"]["source_snapshots"] = [{"snapshot_id": f"snapshot-{name}"}]
    driver.result.update(run_id=f"run-{name}", scope=scope, source_ref=[source], native_job_ref=native)
    driver.observed_native = {**native, "state": "completed"}
    modes = {path: "100644" for path in base}
    trees = compare_source_trees(base, candidate, base_modes=modes, candidate_modes=modes)
    driver.trees = {"base_files": base, "candidate_files": candidate,
                    "base_modes": modes, "candidate_modes": modes}
    driver.result["source_trees"] = trees
    driver.patch_bytes = patch
    driver.result["original_patch"].update(sha256=sha256(patch).hexdigest(), bytes=len(patch),
                                           native_job_ref=native, provider_pins=PINS)
    driver.result["retained_artifacts"]["original_patch"].update(
        sha256=sha256(patch).hexdigest(), bytes=len(patch))
    if capability == "dependency_upgrade":
        target = {"kind": "dependency", "name": "sample-library", "version": "2.1.0"}
        driver.run["input"]["transform_input"]["goal"] = {
            "kind": capability, "summary": "Upgrade library", "target": target}
        driver.run["admission"]["transform_artifact_hashes"] = [target_hash]
        driver.result["dependency_comparison"] = {"status": "passed", **compare_dependency_upgrade(
            base, candidate, target, [target_hash])}
    policy = deepcopy(driver.run["admission"]["transform_test_policy"])
    command = policy["commands"][0]
    command["argv"] = [sys.executable, "-m", "unittest", "discover", "-v"]
    command["environment"] = {"PYTHONNOUSERSITE": "1"}
    driver.run["admission"]["transform_test_policy"] = policy
    runner = ProcessCheckRunner(driver.authority)
    runner.policy = policy
    driver.detached_runner = runner
    for stage, files in (("before", base), ("after", candidate)):
        receipt = await runner.run_check(
            stage=stage, files=files, source_tree=trees["base_tree" if stage == "before" else "candidate_tree"],
            file_modes=modes, command=command, image_digest=policy["image_digest"],
            bundle_sha256=policy["bundle_sha256"], oracle_version=policy["oracle_version"],
            authority=driver.authority)
        assert receipt["exit_code"] == 0 and receipt["test_count"] == 1
        driver.result["tests_" + stage] = [{"status": "passed", "receipt": receipt}]
    runner.stages.clear()
    return {"driver": driver, "run_id": driver.run["run_id"], "scope": scope,
            "source_revision": revision, "expected_native_id": native["native_id"],
            "expected_patch_sha256": sha256(patch).hexdigest(),
            "expected_base_tree": trees["base_tree"], "expected_candidate_tree": trees["candidate_tree"]}


@pytest.fixture
async def ec03_cases(tmp_path):
    return {name: await native_case(tmp_path, name) for name in ("refactor", "dependency_upgrade")}


@pytest.mark.asyncio
async def test_ec03(ec03_cases):
    evidence = await verify_ec03(ec03_cases)
    assert evidence["live_qualified"] is False
    assert set(evidence["cases"]) == {"refactor", "dependency_upgrade"}
    for name, case in ec03_cases.items():
        result = evidence["cases"][name]
        assert result["original_patch_sha256"] == case["expected_patch_sha256"]
        assert case["driver"].native_reads == 1
        assert case["driver"].detached_runner.stages == ["before", "after"]
        assert [result["checks"]["tests_" + stage][0]["status"] for stage in ("before", "after")] == [
            "passed", "passed"]
    assert evidence["cases"]["dependency_upgrade"]["candidate_tree"] != evidence["cases"]["refactor"][
        "candidate_tree"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "same_project", "reused_native", "bad_patch", "tampered_candidate",
    "unrequested_artifact", "failed_detached_after", "false_reported_check", "wrong_revision",
])
async def test_ec03_rejects_unproven_native_or_fixture(ec03_cases, failure):
    refactor = ec03_cases["refactor"]
    upgrade = ec03_cases["dependency_upgrade"]
    if failure == "same_project":
        upgrade["scope"] = refactor["scope"]
    elif failure == "reused_native":
        upgrade["expected_native_id"] = refactor["expected_native_id"]
    elif failure == "bad_patch":
        refactor["driver"].patch_bytes = b"provider summary"
    elif failure == "tampered_candidate":
        refactor["driver"].trees["candidate_files"] = refactor["driver"].trees["base_files"]
    elif failure == "unrequested_artifact":
        upgrade["driver"].run["admission"]["transform_artifact_hashes"] = [
            "sha256:" + sha256(b"different").hexdigest()]
    elif failure == "failed_detached_after":
        refactor["driver"].detached_runner.fail_after = True
    elif failure == "false_reported_check":
        refactor["driver"].result["tests_after"][0]["receipt"]["test_count"] = 0
    elif failure == "wrong_revision":
        refactor["source_revision"] = "c" * 40
    with pytest.raises(ValueError):
        await verify_ec03(ec03_cases)


EC04_START = datetime(2026, 1, 1, tzinfo=UTC)


class RecoveryScenarioDriver(RecoveryFixtureDriver):
    def __init__(self):
        super().__init__()
        original_effect = deepcopy(self.effects[0])
        original_effect["observed_at"] = EC04_START.isoformat()
        self.effects = []
        self.ledger = {"source": "native-provider-ledger", "run_id": "run-a",
                       "scope": deepcopy(RECOVERY_SCOPE), "evidence_ref": "offline/native-ledger",
                       "effects": [], "samples": []}
        for event, (fault, _) in zip(self.trace, FAULT_ACTIONS):
            if fault == "lost_ack":
                self.ledger["effects"].append(deepcopy(original_effect))
                self.effects.append(deepcopy(original_effect))
            self.ledger["samples"].append({
                "source": "native-provider-ledger", "fault": fault,
                "fault_ref": event["evidence_ref"], "native_id": original_effect["native_id"],
                "attempt_token": original_effect["attempt_token"],
                "effect_count": len(self.ledger["effects"]), "state": "running",
                "observed_at": (datetime.fromisoformat(event["observed_at"]) +
                                timedelta(milliseconds=100)).isoformat(),
                "evidence_ref": f"offline/native-snapshot-{event['step']}"})

    async def read_effect_ledger(self, run_id):
        assert run_id == self.ledger["run_id"]
        self.calls.append("native-effect-ledger")
        return deepcopy(self.ledger)


@pytest.mark.asyncio
async def test_ec04():
    driver = RecoveryScenarioDriver()
    report = await verify_ec04(driver, run_id="run-a", scope=RECOVERY_SCOPE,
                               started_at=EC04_START.isoformat(), deadline_seconds=6,
                               max_reconcile_checks=3)
    assert report["live_qualified"] is False
    assert report["native_id"] == "session-a" and report["effect_count"] == 1
    assert report["reconcile_checks"] == 2 and len(report["evidence_refs"]) == 12
    assert [sample["effect_count"] for sample in driver.ledger["samples"]] == [1] * 5
    assert driver.calls == ["sql-run", "sql-attempt", "fault-trace", "provider-effects",
                            "native-readback", "sql-attempt", "fault-trace", "native-effect-ledger"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "duplicate_effect", "lost_effect", "changed_token", "changed_native", "duplicate_replay",
    "late_replay", "early_snapshot", "reused_receipt", "regressed_state", "stale_effect",
    "false_sql_effect", "unknown_readback", "late_deadline", "unbounded_reconcile",
])
async def test_ec04_rejects_unbounded_or_duplicate_native_work(failure):
    driver = RecoveryScenarioDriver()
    deadline = 6
    reconcile = 3
    if failure == "duplicate_effect":
        driver.ledger["effects"].append({**driver.ledger["effects"][0], "evidence_ref": "offline/second"})
    elif failure == "lost_effect":
        driver.ledger["effects"].clear()
    elif failure == "changed_token":
        driver.ledger["effects"][0]["attempt_token"] = "replacement-token"
    elif failure == "changed_native":
        driver.ledger["samples"][2]["native_id"] = "replacement-session"
    elif failure == "duplicate_replay":
        driver.ledger["samples"][2]["effect_count"] = 2
    elif failure == "late_replay":
        driver.ledger["samples"][2]["observed_at"] = (EC04_START + timedelta(seconds=7)).isoformat()
    elif failure == "early_snapshot":
        driver.ledger["samples"][1]["observed_at"] = EC04_START.isoformat()
    elif failure == "reused_receipt":
        driver.ledger["samples"][2]["evidence_ref"] = driver.ledger["samples"][1]["evidence_ref"]
    elif failure == "regressed_state":
        driver.ledger["samples"][2]["state"] = "completed"
    elif failure == "stale_effect":
        driver.ledger["samples"][3]["effect_count"] = 2
    elif failure == "false_sql_effect":
        driver.effects.append({**driver.effects[0], "evidence_ref": "offline/sql-only"})
    elif failure == "unknown_readback":
        driver.native["state"] = "unknown"
    elif failure == "late_deadline":
        deadline = 4
    elif failure == "unbounded_reconcile":
        reconcile = 0
    with pytest.raises(ValueError):
        await verify_ec04(driver, run_id="run-a", scope=RECOVERY_SCOPE,
                          started_at=EC04_START.isoformat(), deadline_seconds=deadline,
                          max_reconcile_checks=reconcile)


class CleanupScenarioDriver(CleanupFixtureDriver):
    def __init__(self):
        super().__init__()
        self.oracle = {"source": "native-inventory", "run_id": "run-a",
                       "scope": deepcopy(EC05_SCOPE), "evidence_ref": "offline/inventory-oracle",
                       "termination_ref": self.termination["evidence_ref"],
                       "artifacts": deepcopy(self.contents),
                       "before": {reference["record_id"]: {
                           "native_resource_ref": deepcopy(reference), "present": True}
                           for reference in (EC05_OWNED, EC05_ADOPTED)},
                       "after": {reference["record_id"]: {
                           "native_resource_ref": deepcopy(reference),
                           "present": reference == EC05_ADOPTED}
                           for reference in (EC05_OWNED, EC05_ADOPTED)},
                       "creation_receipts": {EC05_OWNED["record_id"]: deepcopy(self.creation)}}

    async def read_cleanup_oracle(self, run_id):
        assert run_id == self.oracle["run_id"]
        self.calls.append("native-inventory-oracle")
        return deepcopy(self.oracle)


@pytest.mark.asyncio
async def test_ec05():
    driver = CleanupScenarioDriver()
    report = await verify_ec05(driver, run_id="run-a", scope=EC05_SCOPE,
                               owned=[EC05_OWNED], adopted=[EC05_ADOPTED], now=EC05_NOW)
    assert report["live_qualified"] is False
    assert report["artifact_hashes"]["original_patch"] == sha256(EC05_PATCH).hexdigest()
    assert len(report["artifact_hashes"]) == 5
    assert report["owned_absent"] == report["adopted_retained"] == 1
    assert driver.oracle["before"][EC05_OWNED["record_id"]]["present"] is True
    assert driver.oracle["after"][EC05_OWNED["record_id"]]["present"] is False
    assert driver.oracle["after"][EC05_ADOPTED["record_id"]]["present"] is True
    assert driver.calls == ["result", "run", "termination"] + ["artifact"] * 5 + [
        "identity", "cleanup", "inventory", "cleanup", "inventory", "scm",
        "native-inventory-oracle", "cleanup"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "changed_retained_bytes", "missing_retained_bytes", "owned_still_present", "adopted_deleted",
    "unowned_before", "forged_creation_receipt", "missing_creation_receipt", "foreign_scope",
    "reused_evidence", "adopted_cleanup", "absent_termination", "changed_scm", "expired_patch",
])
async def test_ec05_refuses_unproven_cleanup_or_missing_retention(failure):
    driver = CleanupScenarioDriver()
    if failure == "changed_retained_bytes":
        driver.oracle["artifacts"]["original_patch"] = b"changed offline storage"
    elif failure == "missing_retained_bytes":
        driver.oracle["artifacts"].pop("tests_after-0-stderr")
    elif failure == "owned_still_present":
        driver.oracle["after"][EC05_OWNED["record_id"]]["present"] = True
    elif failure == "adopted_deleted":
        driver.oracle["after"][EC05_ADOPTED["record_id"]]["present"] = False
    elif failure == "unowned_before":
        driver.oracle["before"][EC05_OWNED["record_id"]]["present"] = False
    elif failure == "forged_creation_receipt":
        driver.cleanup["creation_receipt"]["intent_hash"] = "forged"
    elif failure == "missing_creation_receipt":
        driver.oracle["creation_receipts"].clear()
    elif failure == "foreign_scope":
        driver.oracle["scope"]["project_id"] = "project-foreign"
    elif failure == "reused_evidence":
        driver.oracle["evidence_ref"] = driver.termination["evidence_ref"]
    elif failure == "adopted_cleanup":
        driver.adopted_cleanup = {"native_resource_ref": EC05_ADOPTED, "absent": True}
    elif failure == "absent_termination":
        driver.termination["observedTermination"] = False
    elif failure == "changed_scm":
        driver.scm["after"]["pull_requests"] = 1
    elif failure == "expired_patch":
        driver.result["retained_artifacts"]["original_patch"]["expires_at"] = "2025-01-01T00:00:00Z"
    with pytest.raises(ValueError):
        await verify_ec05(driver, run_id="run-a", scope=EC05_SCOPE,
                          owned=[EC05_OWNED], adopted=[EC05_ADOPTED], now=EC05_NOW)


EC06_NOW = datetime(2026, 1, 1, tzinfo=UTC)
EC06_EVENT = "schedule:2026-01-01T00:00:00Z:revision:1"


class ClientScenarioDriver(ClientFixtureDriver):
    def __init__(self, sessions):
        super().__init__()
        self.sessions = sessions
        self.scm_audit = {"source": "scm-audit", "evidence_ref": "offline/scm-audit",
                          "before": deepcopy(self.scm["before"]), "after": deepcopy(self.scm["after"]),
                          "actions": [], "authorization_ref": None}
        self.oracle_patch = None

    async def read_schedule_tick(self, trigger_id, *, replay):
        self.calls.append("replay" if replay else "tick")
        async with self.sessions() as session:
            receipt = await receive_schedule_tick(
                session, tenant_id=CLIENT_SCOPE["tenant_id"], product_id=CLIENT_SCOPE["product_id"],
                project_id=CLIENT_SCOPE["project_id"], service_id="transform",
                trigger_id=trigger_id, scheduled_at=EC06_NOW, now=EC06_NOW)
            await session.commit()
            return {key: getattr(receipt, key) for key in (
                "tenant_id", "product_id", "project_id", "service_id", "trigger_id",
                "event_id", "event_kind", "idempotency_key", "run_id", "state", "reason")} | {
                    "evidence_ref": f"offline/sql-event/{receipt.event_id}"}

    async def read_event_runs(self, receipt):
        self.calls.append("event-runs")
        async with self.sessions() as session:
            rows = (await session.scalars(select(Run).where(
                Run.tenant_id == receipt["tenant_id"], Run.product_id == receipt["product_id"],
                Run.project_id == receipt["project_id"], Run.service_id == "transform",
                Run.trigger_id == receipt["trigger_id"]))).all()
            return [view_run(row) for row in rows if (row.admission or {}).get("event_id") == receipt["event_id"]]

    async def read_event_oracle(self, trigger_id):
        self.calls.append("sql-oracle")
        async with self.sessions() as session:
            events = (await session.scalars(select(EventReceipt).where(
                EventReceipt.tenant_id == CLIENT_SCOPE["tenant_id"],
                EventReceipt.product_id == CLIENT_SCOPE["product_id"],
                EventReceipt.project_id == CLIENT_SCOPE["project_id"],
                EventReceipt.service_id == "transform", EventReceipt.trigger_id == trigger_id))).all()
            runs = (await session.scalars(select(Run).where(
                Run.tenant_id == CLIENT_SCOPE["tenant_id"],
                Run.product_id == CLIENT_SCOPE["product_id"],
                Run.project_id == CLIENT_SCOPE["project_id"],
                Run.service_id == "transform", Run.trigger_id == trigger_id))).all()
            configuration = await session.get(Configuration, (CLIENT_SCOPE["tenant_id"], "config-example"))
            scope = {"tenant_id": CLIENT_SCOPE["tenant_id"], "product_id": CLIENT_SCOPE["product_id"],
                     "project_id": CLIENT_SCOPE["project_id"],
                     "relationship_revision": configuration.relationship_revision}
            oracle = {"source": "sql-event-receipt", "scope": scope, "trigger_id": trigger_id,
                      "evidence_ref": "offline/sql-oracle", "run_ids": [run.run_id for run in runs],
                      "events": [{"event_id": event.event_id, "run_id": event.run_id,
                                  "scope": {"tenant_id": event.tenant_id,
                                            "product_id": event.product_id,
                                            "project_id": event.project_id,
                                            "relationship_revision": configuration.relationship_revision},
                                  "state": event.state,
                                  "configuration_revision": event.configuration_revision,
                                  "idempotency_key": event.idempotency_key,
                                  "evidence_ref": f"offline/sql-event/{event.event_id}"}
                                 for event in events]}
        if self.oracle_patch:
            self.oracle_patch(oracle)
        return oracle

    async def read_scm_audit(self):
        self.calls.append("scm-audit")
        return deepcopy(self.scm_audit)


@pytest.fixture
async def ec06_driver(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    async with engine.begin() as database:
        await database.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add_all([
            Configuration(tenant_id=CLIENT_SCOPE["tenant_id"], configuration_id="config-example",
                          product_id=CLIENT_SCOPE["product_id"], project_id=CLIENT_SCOPE["project_id"],
                          service_id="transform", relationship_revision="1", owner_id="owner-example",
                          current_revision=1, handoff_state="linked"),
            ConfigurationVersion(tenant_id=CLIENT_SCOPE["tenant_id"], configuration_id="config-example",
                                 revision=1, settings={"enabled": True, "triggers": []},
                                 inherited_fields=[], gaps=[], state="ready", hash="a" * 64),
            EventReceipt(tenant_id=CLIENT_SCOPE["tenant_id"], product_id=CLIENT_SCOPE["product_id"],
                         project_id=CLIENT_SCOPE["project_id"], service_id="transform",
                         trigger_id="trigger-example", event_id=EC06_EVENT, idempotency_key="tick-example",
                         event_kind="schedule", configuration_id="config-example",
                         configuration_revision=1, target={}, run_input={}, state="admitted",
                         reason=None, run_id="scheduled-example", received_at=EC06_NOW),
            Run(tenant_id=CLIENT_SCOPE["tenant_id"], run_id="scheduled-example",
                product_id=CLIENT_SCOPE["product_id"], project_id=CLIENT_SCOPE["project_id"],
                service_id="transform", configuration_id="config-example",
                configuration_revision=1, binding_id="binding-example", binding_generation=1,
                trigger_id="trigger-example", deadline_at=EC06_NOW, input={},
                admission={"scope": CLIENT_SCOPE, "event_id": EC06_EVENT}, scope=CLIENT_SCOPE),
        ])
        await session.commit()
    yield ClientScenarioDriver(sessions)
    await engine.dispose()


@pytest.mark.asyncio
async def test_ec06(ec06_driver):
    result = await verify_ec06(ec06_driver, scope=CLIENT_SCOPE, run_id="run-example",
                               result_id="result-example", trigger_id="trigger-example",
                               scheduled_at=EC06_NOW, configuration_revision=1)
    assert result["live_qualified"] is False
    assert result["occurrence_count"] == 1 and result["event_id"] == EC06_EVENT
    assert set(result["client_refs"]) == {"ui", "cli", "mcp"}
    assert ec06_driver.calls == ["ui", "cli", "mcp", "tick", "replay", "event-runs", "scm",
                                 "sql-oracle", "scm-audit", "scm"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "foreign_client", "rejected_tick", "duplicate_event", "duplicate_run", "hidden_scm_action",
    "unauthorized_pr", "changed_audit", "reused_audit_ref", "wrong_tick", "wrong_revision",
    "foreign_sql_row", "different_sql_run", "implicit_pr",
])
async def test_ec06_refuses_unscoped_or_duplicated_occurrence(ec06_driver, failure):
    scheduled_at, revision = EC06_NOW, 1
    if failure == "foreign_client":
        ec06_driver.traces["mcp"]["result"]["scope"] = {**CLIENT_SCOPE, "tenant_id": "foreign"}
    elif failure == "rejected_tick":
        async with ec06_driver.sessions() as session:
            receipt = await session.get(EventReceipt, (
                CLIENT_SCOPE["tenant_id"], CLIENT_SCOPE["product_id"], CLIENT_SCOPE["project_id"],
                "transform", "trigger-example", EC06_EVENT))
            receipt.state = "rejected"
            await session.commit()
    elif failure == "duplicate_event":
        async with ec06_driver.sessions() as session:
            session.add(EventReceipt(tenant_id=CLIENT_SCOPE["tenant_id"],
                                     product_id=CLIENT_SCOPE["product_id"],
                                     project_id=CLIENT_SCOPE["project_id"], service_id="transform",
                                     trigger_id="trigger-example", event_id=EC06_EVENT + ":other",
                                     idempotency_key="another-tick", event_kind="schedule",
                                     configuration_id="config-example", configuration_revision=1,
                                     target={}, run_input={}, state="admitted", reason=None,
                                     run_id="another-run", received_at=EC06_NOW))
            await session.commit()
    elif failure == "duplicate_run":
        async with ec06_driver.sessions() as session:
            session.add(Run(tenant_id=CLIENT_SCOPE["tenant_id"], run_id="another-run",
                            product_id=CLIENT_SCOPE["product_id"], project_id=CLIENT_SCOPE["project_id"],
                            service_id="transform", configuration_id="config-example",
                            configuration_revision=1, binding_id="binding-example", binding_generation=1,
                            trigger_id="trigger-example", deadline_at=EC06_NOW, input={},
                            admission={"scope": CLIENT_SCOPE, "event_id": EC06_EVENT}, scope=CLIENT_SCOPE))
            await session.commit()
    elif failure == "hidden_scm_action":
        ec06_driver.scm_audit["actions"] = ["opened_pr"]
    elif failure == "unauthorized_pr":
        ec06_driver.scm_audit["authorization_ref"] = "unverified"
    elif failure == "changed_audit":
        ec06_driver.scm_audit["before"]["revision"] = "other"
        ec06_driver.scm_audit["after"]["revision"] = "other"
    elif failure == "reused_audit_ref":
        ec06_driver.scm_audit["evidence_ref"] = ec06_driver.scm["evidence_ref"]
    elif failure == "wrong_tick":
        scheduled_at += timedelta(days=1)
    elif failure == "wrong_revision":
        revision = 2
    elif failure == "foreign_sql_row":
        ec06_driver.oracle_patch = lambda oracle: oracle["events"][0]["scope"].update(tenant_id="foreign")
    elif failure == "different_sql_run":
        ec06_driver.oracle_patch = lambda oracle: oracle.update(run_ids=["another-run"])
    elif failure == "implicit_pr":
        ec06_driver.scm["pr_actions"] = ["opened"]
    with pytest.raises(ValueError):
        await verify_ec06(ec06_driver, scope=CLIENT_SCOPE, run_id="run-example",
                          result_id="result-example", trigger_id="trigger-example",
                          scheduled_at=scheduled_at, configuration_revision=revision)


class ReuseScenarioDriver(ReuseFixtureDriver):
    def __init__(self):
        super().__init__()
        self.oracle = {
            "source": "consumer-audit", "scope": deepcopy(REUSE_SCOPE),
            "run_id": "run-example", "result_id": "result-example",
            "evidence_ref": "offline/reuse-oracle",
            "receipts": {row["destination"]: {
                "receipt_id": row["receipt"]["receipt_id"],
                "contribution_id": row["publication_id"],
                "contribution_hash": row["contribution_hash"],
                "record_refs": deepcopy(row["receipt"]["record_refs"]),
                "evidence_ref": f"offline/{row['destination']}-receipt-oracle",
            } for row in self.rows},
            "later_task": {
                "task_id": self.later_task["task_id"],
                "run_id": self.later_task["run_id"],
                "citation": deepcopy(self.later_task["input"]["citation"]),
                "candidate_tree": self.later_task["input"]["candidate_tree"],
                "entitlement_ref": self.later_task["entitlement"]["evidence_ref"],
                "evidence_ref": self.later_task["evidence_ref"],
            },
            "denials": {kind: {
                "actor_id": denial["actor_id"], "status": denial["status"],
                "effect_count": denial["effect_count"],
                "entitlement_ref": denial["entitlement"]["evidence_ref"],
                "evidence_ref": denial["evidence_ref"],
                "audit_ref": f"offline/{kind}-denial-oracle",
            } for kind, denial in self.denials.items()},
        }

    async def read_reuse_oracle(self, result_id):
        assert result_id == self.oracle["result_id"]
        self.calls.append("reuse-oracle")
        return deepcopy(self.oracle)


async def check_ec07(driver):
    return await verify_ec07(driver, run_id="run-example", result_id="result-example",
                             scope=REUSE_SCOPE, now=REUSE_NOW)


@pytest.mark.asyncio
async def test_ec07():
    driver = ReuseScenarioDriver()
    report = await check_ec07(driver)
    assert report["live_qualified"] is False
    assert report["phase"] == "offline-consumer-reuse"
    assert report["audit_ref"] == "offline/reuse-oracle"
    assert len(report["consumer_refs"]) == 4
    assert report["later_task_ref"] == "private/later-task"
    assert report["denials"] == ["foreign", "revoked"]
    assert driver.calls == ["run", "result", "sql-publications",
                            "product-context-receipt", "product-context-projection",
                            "engineering-insights-receipt", "engineering-insights-projection",
                            "later-task", "foreign-denial", "revoked-denial", "reuse-oracle",
                            "sql-publications", "later-task", "foreign-denial", "revoked-denial"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "foreign_audit", "missing_consumer", "wrong_digest", "wrong_receipt",
    "wrong_record_ref", "missing_audit_ref", "reused_receipt_ref",
    "wrong_citation", "wrong_later_run", "wrong_candidate", "wrong_entitlement",
    "foreign_reuse_effect", "foreign_reuse_status", "revoked_actor",
    "reused_denial_ref", "missing_denial",
])
async def test_ec07_refuses_false_receipts_or_reuse_audit(failure):
    driver = ReuseScenarioDriver()
    receipt = driver.oracle["receipts"]["product-context"]
    if failure == "foreign_audit":
        driver.oracle["scope"]["tenant_id"] = "foreign"
    elif failure == "missing_consumer":
        driver.oracle["receipts"].pop("product-context")
    elif failure == "wrong_digest":
        receipt["contribution_hash"] = "0" * 64
    elif failure == "wrong_receipt":
        receipt["receipt_id"] = "another-receipt"
    elif failure == "wrong_record_ref":
        receipt["record_refs"][0]["record_id"] = "foreign-result"
    elif failure == "missing_audit_ref":
        driver.oracle["evidence_ref"] = ""
    elif failure == "reused_receipt_ref":
        receipt["evidence_ref"] = "private/product-context-receipt"
    elif failure == "wrong_citation":
        driver.oracle["later_task"]["citation"]["result_ref"]["record_id"] = "unrelated"
    elif failure == "wrong_later_run":
        driver.oracle["later_task"]["run_id"] = "run-example"
    elif failure == "wrong_candidate":
        driver.oracle["later_task"]["candidate_tree"] = "d" * 40
    elif failure == "wrong_entitlement":
        driver.oracle["later_task"]["entitlement_ref"] = "offline/unrelated-fence"
    elif failure == "foreign_reuse_effect":
        driver.oracle["denials"]["foreign"]["effect_count"] = 1
    elif failure == "foreign_reuse_status":
        driver.oracle["denials"]["foreign"]["status"] = 200
    elif failure == "revoked_actor":
        driver.oracle["denials"]["revoked"]["actor_id"] = "other-actor"
    elif failure == "reused_denial_ref":
        driver.oracle["denials"]["revoked"]["audit_ref"] = driver.oracle["evidence_ref"]
    elif failure == "missing_denial":
        driver.oracle["denials"].pop("foreign")
    with pytest.raises(ValueError):
        await check_ec07(driver)


class PortabilityScenarioDriver(PortabilityFixtureDriver):
    def __init__(self):
        super().__init__()
        snapshots = {}
        for fixture_id, entry in self.fixtures.items():
            contents = f"fictional retained patch for {fixture_id}\n".encode()
            digest = sha256(contents).hexdigest()
            entry["result"]["original_patch_sha256"] = digest
            entry["cleanup"]["retained_patch_sha256"] = digest
            cleanup = entry["cleanup"]
            inventory = {item["reference"]: {"present": False,
                                             "receipt_ref": item["receipt_ref"]}
                         for item in cleanup["owned_inventory"]}
            inventory.update({item["reference"]: {"present": True, "receipt_ref": None}
                              for item in cleanup["adopted_inventory"]})
            snapshots[fixture_id] = {
                "evidence_ref": f"offline/{fixture_id}-inventory-oracle",
                "manifest": deepcopy(entry["manifest"]),
                "run_id": entry["result"]["run_id"],
                "native_id": entry["result"]["native_id"],
                "retained_patch_bytes": contents,
                "inventory": inventory,
                "cost": deepcopy(entry["cost"]),
            }
        release = self.fixtures["library-python"]["release"]
        self.oracle = {
            "source": "release-inventory-ledger", "evidence_ref": "offline/release-ledger",
            "deployment_history": deepcopy(self.history),
            "release": {key: release[key] for key in (
                "source_revision", "wheel_sha256", "worker_sha256", "provider_image_sha256",
                "validation_image_sha256", "schema_sha256", "gateway_image_sha256")},
            "fixtures": snapshots,
        }

    async def read_portability_oracle(self):
        self.calls.append("release-ledger")
        return deepcopy(self.oracle)


async def check_ec08(driver):
    return await verify_ec08(driver, fixture_ids=("library-python", "library-javascript"))


@pytest.mark.asyncio
async def test_ec08():
    driver = PortabilityScenarioDriver()
    report = await check_ec08(driver)
    assert report["live_qualified"] is False and report["acceptance_complete"] is False
    assert report["phase"] == "offline-two-fixture-portability"
    assert set(report["fixture_ids"]) == {"library-python", "library-javascript"}
    assert set(report["cost_statuses"].values()) == {"unknown"}
    assert report["ledger_ref"] == "offline/release-ledger"
    assert report["release_digests"] == driver.oracle["release"]
    assert report["retained_patch_hashes"]["library-python"] != report["retained_patch_hashes"]["library-javascript"]
    assert driver.calls == ["deployment"] + [
        (fixture_id, kind) for fixture_id in ("library-python", "library-javascript")
        for kind in ("manifest", "release", "result", "cleanup", "cost", "outcomes")
    ] + ["release-ledger"]


@pytest.mark.asyncio
async def test_ec08_keeps_fictional_measured_and_unknown_cost_separate():
    driver = PortabilityScenarioDriver()
    measured = {"status": "actual", "amount_usd": "0.25", "currency": "USD",
                "evidence_ref": "offline/library-python/cost-receipt"}
    driver.fixtures["library-python"]["cost"] = deepcopy(measured)
    driver.oracle["fixtures"]["library-python"]["cost"] = deepcopy(measured)
    report = await check_ec08(driver)
    assert report["cost_statuses"] == {"library-python": "actual", "library-javascript": "unknown"}
    assert report["acceptance_complete"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "one_snapshot", "wrong_history", "platform_edit", "wrong_release", "changed_second_release",
    "same_repository", "same_tests", "foreign_manifest", "other_run", "other_native",
    "tampered_bytes", "missing_bytes", "owned_still_present", "adopted_removed",
    "unproven_receipt", "unknown_as_zero", "changed_cost", "reused_reference", "missing_ledger",
])
async def test_ec08_refuses_unproven_release_cleanup_or_cost(failure):
    driver = PortabilityScenarioDriver()
    python = driver.fixtures["library-python"]
    javascript = driver.fixtures["library-javascript"]
    snapshot = driver.oracle["fixtures"]["library-python"]
    if failure == "one_snapshot":
        driver.oracle["fixtures"].pop("library-javascript")
    elif failure == "wrong_history":
        driver.oracle["deployment_history"]["deployment_count"] = 2
    elif failure == "platform_edit":
        driver.history["platform_edits_between"] = 1
    elif failure == "wrong_release":
        driver.oracle["release"]["wheel_sha256"] = "4" * 64
    elif failure == "changed_second_release":
        javascript["release"]["worker_sha256"] = "4" * 64
    elif failure == "same_repository":
        javascript["manifest"]["repository_ref"] = deepcopy(python["manifest"]["repository_ref"])
    elif failure == "same_tests":
        javascript["manifest"]["test_commands"] = deepcopy(python["manifest"]["test_commands"])
    elif failure == "foreign_manifest":
        snapshot["manifest"]["scope"]["tenant_id"] = "foreign"
    elif failure == "other_run":
        snapshot["run_id"] = "unrelated-run"
    elif failure == "other_native":
        snapshot["native_id"] = "unrelated-native"
    elif failure == "tampered_bytes":
        snapshot["retained_patch_bytes"] = b"modified patch"
    elif failure == "missing_bytes":
        snapshot["retained_patch_bytes"] = None
    elif failure == "owned_still_present":
        snapshot["inventory"]["owned-library-python"]["present"] = True
    elif failure == "adopted_removed":
        snapshot["inventory"]["adopted-library-python"]["present"] = False
    elif failure == "unproven_receipt":
        snapshot["inventory"]["owned-library-python"]["receipt_ref"] = "offline/other-creation"
    elif failure == "unknown_as_zero":
        python["cost"]["amount_usd"] = "0.00"
        snapshot["cost"]["amount_usd"] = "0.00"
    elif failure == "changed_cost":
        snapshot["cost"]["status"] = "actual"
    elif failure == "reused_reference":
        snapshot["evidence_ref"] = python["manifest"]["evidence_ref"]
    elif failure == "missing_ledger":
        driver.oracle["evidence_ref"] = ""
    with pytest.raises(ValueError):
        await check_ec08(driver)
