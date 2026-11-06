"""Pinned architecture-source packaging and lifecycle regressions."""

import io
import json
from copy import deepcopy
import subprocess
import stat
import zipfile
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from l5_services.admission import persist_admission
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.receipt import RunReceipt
from l5_services.models.source_retention import SourceRetention
from l5_services.models.work import Work
from l5_services.source_manifest import (SourceIntegrityError, materialize_inputs,
                                         package_architecture_sources, verify_provider_source_receipt)
from l5_services.source_retention import (RetentionError, pin_source_hold, release_source_hold,
                                          revoke_source_hold, source_cleanup_allowed, source_reuse_allowed)
from l5_services.models.base import Base
from l5_services.models.run import Run
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


SCOPE = {"tenant_id": "tenant-example", "project_id": "project-example"}


def _git(repository, *args):
    return subprocess.check_output(["git", "-C", str(repository), *args]).decode().strip()


def _git_bytes(repository, *args):
    return subprocess.check_output(["git", "-C", str(repository), *args])


def _selected_sources(tmp_path, document=b"Goal: review network resilience.\n"):
    repository = tmp_path / "architecture"
    repository.mkdir()
    _git(repository, "init", "-q")
    paths = {
        "README.md": b"Review the architecture.\n",
        "deploy/main.tf": b'module "network" { source = "git::https://example.invalid/network" }\n',
        "build/build.yml": b"steps: []\n",
        "legacy/old.tf": b"resource \"old\" \"unused\" {}\n",
    }
    for path, content in paths.items():
        destination = repository / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    _git(repository, "add", ".")
    subprocess.run(["git", "-C", str(repository), "-c", "user.name=Example",
                    "-c", "user.email=example@example.invalid", "commit", "-qm", "pin"], check=True)
    revision = _git(repository, "rev-parse", "HEAD")
    tracked = _git(repository, "ls-tree", "-r", "--name-only", revision).splitlines()
    files = [{"path": path, "content": paths[path],
              "component": None if path.startswith("legacy/") else "platform",
              "exclusion": "out of review scope" if path.startswith("legacy/") else None,
              "entry_point": path in {"build/build.yml", "deploy/main.tf"}}
             for path in reversed(tracked)]
    sources = [
        {"source_ref": {**SCOPE, "record_type": "repository_commit", "record_revision": revision},
         "snapshot_id": "repo-snapshot", "revision": revision,
         "selected_paths": tracked,
         "selected_hashes": {path: sha256(_git_bytes(repository, "show", f"{revision}:{path}")).hexdigest()
                             for path in tracked}, "files": files,
         "unresolved": ["remote module: git::https://example.invalid/network"]},
        {"source_ref": {**SCOPE, "record_type": "document",
                        "record_revision": sha256(document).hexdigest()},
         "snapshot_id": "document-snapshot", "revision": sha256(document).hexdigest(),
         "selected_paths": ["goals.txt"],
         "selected_hashes": {"goals.txt": sha256(document).hexdigest()},
         "files": [{"path": "goals.txt", "content": document, "component": "platform",
                    "exclusion": None, "entry_point": False}]},
    ]
    return repository, sources, document


def test_ac_01(tmp_path):
    repository, sources, document = _selected_sources(tmp_path)
    first, manifest = package_architecture_sources(sources, scope=SCOPE, max_files=8, max_bytes=8192)
    second, repeated = package_architecture_sources(sources, scope=SCOPE, max_files=8, max_bytes=8192)
    assert first == second and manifest == repeated
    assert sha256(first).hexdigest() == sha256(second).hexdigest()
    assert manifest["sources"][0]["selected_paths"] == _git(
        repository, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert json.loads(archive.read("manifest.json")) == manifest
        for entry in manifest["files"]:
            if entry["archive_path"]:
                assert sha256(archive.read(entry["archive_path"])).hexdigest() == entry["sha256"]
            else:
                assert entry["exclusion"] == "out of review scope"
            if entry["snapshot_id"] == "repo-snapshot":
                assert sha256(_git_bytes(repository, "show", f"HEAD:{entry['path']}")).hexdigest() == entry[
                    "sha256"]
    assert next(entry for entry in manifest["files"] if entry["path"] == "goals.txt")[
        "sha256"] == sha256(document).hexdigest()
    assert {entry["path"] for entry in manifest["files"] if entry["entry_point"]} == {
        "build/build.yml", "deploy/main.tf"}
    assert "remote module" in manifest["sources"][0]["unresolved"][0]


def test_architecture_package_rejects_changed_or_missing_selected_path(tmp_path):
    repository, sources, _ = _selected_sources(tmp_path)
    sources[0]["files"][0]["content"] = b"changed source bytes"
    with pytest.raises(SourceIntegrityError, match="digest"):
        package_architecture_sources(sources, scope=SCOPE, max_files=8, max_bytes=8192)
    sources[0]["files"][0]["content"] = _git_bytes(
        repository, "show", f"HEAD:{sources[0]['files'][0]['path']}")
    sources[0]["files"][0]["path"] = "changed.tf"
    with pytest.raises(SourceIntegrityError, match="coverage"):
        package_architecture_sources(sources, scope=SCOPE, max_files=8, max_bytes=8192)
    sources[0]["files"].pop()
    with pytest.raises(SourceIntegrityError, match="coverage"):
        package_architecture_sources(sources, scope=SCOPE, max_files=8, max_bytes=8192)


@pytest.mark.parametrize("unsafe_path", ["../secrets.txt", "/absolute.tf", "nested/../../secret.tf"])
def test_architecture_package_rejects_traversal(tmp_path, unsafe_path):
    _, sources, _ = _selected_sources(tmp_path)
    sources[0]["files"][0]["path"] = unsafe_path
    with pytest.raises(SourceIntegrityError, match="unsafe source path"):
        package_architecture_sources(sources, scope=SCOPE, max_files=8, max_bytes=8192)


def test_architecture_package_rejects_symlinks_and_duplicates(tmp_path):
    _, sources, _ = _selected_sources(tmp_path)
    sources[0]["files"][0]["mode"] = stat.S_IFLNK
    with pytest.raises(SourceIntegrityError, match="unsafe"):
        package_architecture_sources(sources, scope=SCOPE, max_files=8, max_bytes=8192)
    sources[0]["files"][0].pop("mode")
    sources[0]["files"][0] = sources[0]["files"][1].copy()
    with pytest.raises(SourceIntegrityError, match="duplicate"):
        package_architecture_sources(sources, scope=SCOPE, max_files=8, max_bytes=8192)


@pytest.mark.parametrize("content", [b"AKIA1234567890123456", b"-----BEGIN PRIVATE KEY-----\nsecret",
                                      b"PK\x03\x04not-allowed", b"password=1234567890abcdef"])
def test_architecture_package_rejects_secret_and_nested_archive(tmp_path, content):
    _, sources, _ = _selected_sources(tmp_path)
    entry = sources[0]["files"][0]
    entry["content"] = content
    sources[0]["selected_hashes"][entry["path"]] = sha256(content).hexdigest()
    with pytest.raises(SourceIntegrityError, match="unsafe or secret"):
        package_architecture_sources(sources, scope=SCOPE, max_files=8, max_bytes=8192)


@pytest.mark.parametrize("key", ["aws_secret_access_key", "api_key", "api-key", "apikey", "password"])
@pytest.mark.parametrize("syntax", ["json", "single_quoted_yaml", "double_quoted_yaml"])
def test_architecture_package_rejects_quoted_secret_keys(tmp_path, key, syntax):
    secret = "A" * 40
    if syntax == "json":
        document = json.dumps({key: secret}).encode()
    else:
        quote = "'" if syntax == "single_quoted_yaml" else '"'
        document = f"{quote}{key}{quote}: {quote}{secret}{quote}\n".encode()
    _, sources, _ = _selected_sources(tmp_path, document)
    with pytest.raises(SourceIntegrityError, match="unsafe or secret"):
        package_architecture_sources(sources, scope=SCOPE, max_files=8, max_bytes=8192)


def test_architecture_package_bounds_expanded_archive_including_manifest(tmp_path):
    _, sources, _ = _selected_sources(tmp_path, b"A" * 1000)
    sources = [sources[1]]
    archived, _ = package_architecture_sources(sources, scope=SCOPE, max_files=1, max_bytes=8192)
    with zipfile.ZipFile(io.BytesIO(archived)) as archive:
        expanded_bytes = sum(len(archive.read(entry)) for entry in archive.namelist())
    assert len(archived) < 1024 < expanded_bytes
    for limit in (1024, expanded_bytes - 1):
        with pytest.raises(SourceIntegrityError, match="expanded source exceeds byte limit"):
            package_architecture_sources(sources, scope=SCOPE, max_files=1, max_bytes=limit)
    bounded, _ = package_architecture_sources(sources, scope=SCOPE, max_files=1, max_bytes=expanded_bytes)
    assert bounded == archived


def test_architecture_package_rejects_oversize_and_foreign_source(tmp_path):
    _, sources, _ = _selected_sources(tmp_path)
    with pytest.raises(SourceIntegrityError, match="file count"):
        package_architecture_sources(sources, scope=SCOPE, max_files=2, max_bytes=8192)
    with pytest.raises(SourceIntegrityError, match="byte limit"):
        package_architecture_sources(sources, scope=SCOPE, max_files=8, max_bytes=20)
    sources[1]["source_ref"]["tenant_id"] = "foreign-tenant"
    with pytest.raises(SourceIntegrityError, match="pinned source"):
        package_architecture_sources(sources, scope=SCOPE, max_files=8, max_bytes=8192)


@pytest.mark.parametrize("readback_mode", ["valid", "tampered", "revoked", "missing", "receipt_mismatch",
                                         "json_secret", "yaml_secret"])
async def test_ac_02(tmp_path, readback_mode):
    secret_documents = {
        "json_secret": json.dumps({"aws_secret_access_key": "A" * 40}).encode(),
        "yaml_secret": b"'password': '" + b"A" * 40 + b"'\n",
    }
    _, selections, document = _selected_sources(
        tmp_path, secret_documents.get(readback_mode, b"Goal: review network resilience.\n"))
    now = datetime(2026, 1, 1, tzinfo=UTC)
    snapshots, listings, payloads, decisions = [], [], [], {}

    def artifact(content):
        return {"artifact_id": "l5/well_architected/work/run-example/private-snapshot", "version": "1",
                "sha256": sha256(content).hexdigest(), "bytes": len(content)}

    for index, selection in enumerate(selections):
        source = selection["source_ref"]
        if index == 0:
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as archive:
                for entry in selection["files"]:
                    archive.writestr(entry["path"], entry["content"])
            payload = output.getvalue()
        else:
            payload = document
        listing = json.dumps({"source_ref": source, "exclusions": [], "files": [
            {"source_ref": source, "path": entry["path"], "bytes": len(entry["content"]),
             "sha256": sha256(entry["content"]).hexdigest()} for entry in selection["files"]
        ]}).encode()
        snapshots.append({"source_ref": source, "snapshot_id": selection["snapshot_id"],
                          "content": artifact(payload), "file_manifest": artifact(listing),
                          "file_count": len(selection["files"]), "exclusions": [],
                          "expires_at": (now + timedelta(hours=1)).isoformat()})
        listings.append(listing)
        payloads.append(payload)
        decisions[selection["snapshot_id"]] = {
            "files": {entry["path"]: {key: entry[key] for key in ("component", "exclusion", "entry_point")}
                      for entry in selection["files"]},
            "unresolved": selection.get("unresolved", []),
        }

    class SourceClient:
        async def read_source(self, product, project, snapshot_id, request):
            assert product == "product-example" and project == "project-example"
            assert request["delegation_id"] == "delegation-example"
            return snapshots[0 if snapshot_id == "repo-snapshot" else 1]

        async def read_source_manifest(self, product, project, snapshot_id, request):
            return listings[0 if snapshot_id == "repo-snapshot" else 1]

        async def stream_source(self, product, project, snapshot_id, request):
            yield payloads[0 if snapshot_id == "repo-snapshot" else 1]

    class ArchiveStore:
        bucket = "fictional-wa-bucket"

        def __init__(self):
            self.uploaded = None
            self.reads = []

        async def put_archive(self, run_id, content):
            assert run_id == "run-example"
            self.uploaded = content
            return {**artifact(content), "sha256": "0" * 64} if readback_mode == "receipt_mismatch" else artifact(content)

        async def get_archive(self, run_id, reference):
            assert run_id == "run-example" and reference == artifact(self.uploaded)
            self.reads.append(reference)
            if readback_mode == "revoked":
                raise PermissionError("reader revoked")
            if readback_mode == "missing":
                return None
            if readback_mode == "tampered":
                return self.uploaded + b"changed"
            return self.uploaded

    admission = {"source_snapshots": snapshots, "architecture_coverage": decisions,
                 "delegation_id": "delegation-example", "delegation_version": "1", "exclusions": [],
                 "scope": {"tenant_id": "tenant-example", "project_id": "project-example"},
                 "configuration_hash": "a" * 64, "profile_ref": {"record_id": "profile-example"},
                 "adapter_revision": "wa-1"}
    run = SimpleNamespace(service_id="well_architected", product_id="product-example",
                          project_id="project-example", run_id="run-example", configuration_revision=1,
                          binding_generation=1, admission=admission,
                          input={"capability": "architecture_review", "sources": [item["source_ref"]
                                  for item in selections]}, capture_limits={"max_bytes": 8192, "max_files": 8})
    attempt = SimpleNamespace(work_id="work-example", fence=1)
    store = ArchiveStore()
    if readback_mode in secret_documents:
        with pytest.raises(SourceIntegrityError, match="unsafe or secret"):
            await materialize_inputs(run, attempt, SourceClient(), archive_store=store, now=now)
        assert store.uploaded is None and store.reads == []
        return
    if readback_mode != "valid":
        error = PermissionError if readback_mode == "revoked" else SourceIntegrityError
        with pytest.raises(error):
            await materialize_inputs(run, attempt, SourceClient(), archive_store=store, now=now)
        assert store.reads == ([] if readback_mode == "receipt_mismatch" else [artifact(store.uploaded)])
        return
    manifest, source_bytes = await materialize_inputs(run, attempt, SourceClient(),
                                                     archive_store=store, now=now)
    assert source_bytes == payloads
    assert manifest["coverage_mode"] == "selected_snapshot"
    assert manifest["source_snapshot_ids"] == ["repo-snapshot", "document-snapshot"]
    assert manifest["archive"]["sha256"] == sha256(store.uploaded).hexdigest()
    assert manifest["archive_readback"] == {"sha256": sha256(store.uploaded).hexdigest(),
                                            "bytes": len(store.uploaded), "artifact": manifest["archive"]}
    assert store.reads == [manifest["archive"]]
    assert manifest["provider_coverage"] == "provider_coverage_unverified"
    traversal = deepcopy(selections)
    traversal[0]["files"][0]["path"] = "../outside.tf"
    with pytest.raises(SourceIntegrityError, match="unsafe source path"):
        package_architecture_sources(traversal, scope=SCOPE, max_files=8, max_bytes=8192)
    secret = deepcopy(selections)
    secret[0]["files"][0]["content"] = b"AKIA1234567890123456"
    secret[0]["selected_hashes"][secret[0]["files"][0]["path"]] = sha256(
        secret[0]["files"][0]["content"]).hexdigest()
    with pytest.raises(SourceIntegrityError, match="secret"):
        package_architecture_sources(secret, scope=SCOPE, max_files=8, max_bytes=8192)
    provider_receipt = {"state": "consumed", "scope": admission["scope"], "artifact": manifest["archive"],
                        "sha256": manifest["archive"]["sha256"], "bytes": len(store.uploaded),
                        "paths": sorted(entry["archive_path"] for entry in manifest["architecture"]["files"]
                                        if entry["archive_path"])}

    class ProviderReader:
        def __init__(self, receipt):
            self.receipt = receipt

        async def read_source_receipt(self, reference):
            assert reference == manifest["archive"]
            return self.receipt

    checked = await verify_provider_source_receipt(manifest, ProviderReader(provider_receipt))
    assert checked == {"provider_coverage": "verified", "provider_source_receipt": provider_receipt}
    assert manifest["provider_coverage"] == "provider_coverage_unverified"
    for change in ({"paths": []}, {"state": "uploaded"}, {"scope": {"tenant_id": "another"}},
                   {"sha256": "0" * 64}, {"artifact": {"artifact_id": "another"}}):
        with pytest.raises(SourceIntegrityError, match="consumption unverified"):
            await verify_provider_source_receipt(manifest, ProviderReader({**provider_receipt, **change}))
    with pytest.raises(SourceIntegrityError, match="consumption unverified"):
        await verify_provider_source_receipt(manifest, None)
    with zipfile.ZipFile(io.BytesIO(store.uploaded)) as archive:
        assert json.loads(archive.read("manifest.json")) == manifest["architecture"]
        assert archive.read("sources/1/goals.txt") == document
    decisions["repo-snapshot"]["files"].pop("deploy/main.tf")
    with pytest.raises(SourceIntegrityError, match="coverage"):
        await materialize_inputs(run, attempt, SourceClient(), archive_store=ArchiveStore(), now=now)


async def test_ac_03(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'holds.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        artifact = {"artifact_id": "private-immutable-object", "version": "v1", "sha256": "a" * 64}
        objects = {artifact["artifact_id"]: b"pinned source bytes"}
        async with sessions() as session:
            run = Run(tenant_id=SCOPE["tenant_id"], run_id="run-example", product_id="product-example",
                      project_id=SCOPE["project_id"], service_id="well_architected",
                      configuration_id="configuration-example", configuration_revision=1,
                      binding_id="binding-example", binding_generation=1, trigger_id="manual",
                      input={"capability": "architecture_review"}, deadline_at=now + timedelta(hours=1))
            session.add(run)
            await session.flush()
            hold = await pin_source_hold(session, run, artifact, required_until=now + timedelta(days=2),
                                         policy_expires_at=now + timedelta(days=4), now=now)
            await session.commit()
            run.state, run.provider_state = "expired", "unknown"
            revoke_source_hold(hold, now=now + timedelta(hours=2))
            assert not source_reuse_allowed(hold, now=now + timedelta(hours=2))
            assert objects[artifact["artifact_id"]] == b"pinned source bytes"
            assert not await source_cleanup_allowed(session, SCOPE["tenant_id"], artifact,
                                                    now=now + timedelta(days=3))
            with pytest.raises(RetentionError, match="terminal proof"):
                release_source_hold(hold, run, None, now=now + timedelta(days=3))
            run.native_job_ref = {"generation_id": "native-example"}
            proof = {"tenant_id": SCOPE["tenant_id"], "run_id": run.run_id,
                     "generation_id": "native-example", "provider_state": "completed", "revision": 3}
            with pytest.raises(RetentionError, match="terminal proof"):
                release_source_hold(hold, run, {**proof, "generation_id": "foreign"},
                                    now=now + timedelta(days=3))
            receipt = release_source_hold(hold, run, proof, now=now + timedelta(days=3))
            assert receipt["artifact"] == artifact and receipt["proof_revision"] == 3
            assert await source_cleanup_allowed(session, SCOPE["tenant_id"], artifact,
                                                now=now + timedelta(days=3))
            objects.pop(artifact["artifact_id"])
            assert artifact["artifact_id"] not in objects
            await session.commit()
    finally:
        await engine.dispose()


async def test_source_cleanup_is_scoped_to_immutable_version(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'version-holds.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        required_until = now + timedelta(days=2)
        first = {"artifact_id": "private-immutable-object", "version": "v1", "sha256": "a" * 64}
        second = {**first, "version": "v2", "sha256": "b" * 64}
        async with sessions() as session:
            held_runs = []
            for index, artifact in enumerate((first, first, second)):
                run = Run(tenant_id=SCOPE["tenant_id"], run_id=f"run-{index}", product_id="product-example",
                          project_id=SCOPE["project_id"], service_id="well_architected",
                          configuration_id="configuration-example", configuration_revision=1,
                          binding_id="binding-example", binding_generation=1, trigger_id="manual",
                          input={"capability": "architecture_review"}, deadline_at=now + timedelta(hours=1),
                          native_job_ref={"generation_id": f"native-{index}"})
                session.add(run)
                await session.flush()
                hold = await pin_source_hold(session, run, artifact, required_until=required_until,
                                             policy_expires_at=now + timedelta(days=4), now=now)
                held_runs.append((run, hold))
            await session.commit()
            for index, (run, hold) in enumerate(held_runs):
                assert not await source_cleanup_allowed(session, SCOPE["tenant_id"], hold.artifact_ref,
                                                        now=required_until)
                proof = {"tenant_id": SCOPE["tenant_id"], "run_id": run.run_id,
                         "generation_id": run.native_job_ref["generation_id"],
                         "provider_state": "completed", "revision": 1}
                release_source_hold(hold, run, proof, now=now + timedelta(days=1))
                await session.commit()
                assert not await source_cleanup_allowed(session, SCOPE["tenant_id"], hold.artifact_ref,
                                                        now=now + timedelta(days=1))
                assert await source_cleanup_allowed(session, SCOPE["tenant_id"], first,
                                                    now=required_until) == (index >= 1)
                assert await source_cleanup_allowed(session, SCOPE["tenant_id"], second,
                                                    now=required_until) == (index == 2)
            for missing_or_conflicting in ({**first, "version": "v3"}, {**first, "sha256": "c" * 64},
                                           {"artifact_id": first["artifact_id"]}):
                assert not await source_cleanup_allowed(session, SCOPE["tenant_id"], missing_or_conflicting,
                                                        now=required_until)
            assert not await source_cleanup_allowed(session, "another-tenant", first, now=required_until)
            held_runs[0][1].artifact_ref = {**first, "sha256": "c" * 64}
            await session.commit()
            assert not await source_cleanup_allowed(session, SCOPE["tenant_id"], first, now=required_until)
            assert await source_cleanup_allowed(session, SCOPE["tenant_id"], second, now=required_until)
    finally:
        await engine.dispose()


async def test_conflicting_mandatory_source_retention_rejects_before_start(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'policy.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        artifact = {"artifact_id": "private-immutable-object", "version": "v1", "sha256": "a" * 64}
        snapshot = {"content": artifact, "expires_at": (now + timedelta(hours=1)).isoformat()}
        run = SimpleNamespace(tenant_id=SCOPE["tenant_id"], run_id="run-example",
                              project_id=SCOPE["project_id"], service_id="well_architected")
        async with sessions() as session:
            with pytest.raises(RetentionError, match="before Start"):
                await pin_source_hold(session, run, artifact, snapshot=snapshot,
                                      required_until=now + timedelta(days=1),
                                      policy_expires_at=now + timedelta(days=2), now=now)
            with pytest.raises(RetentionError, match="before Start"):
                await pin_source_hold(session, run, artifact, snapshot=snapshot,
                                      required_until=now + timedelta(minutes=30),
                                      policy_expires_at=now + timedelta(minutes=5), now=now)
            assert not await source_cleanup_allowed(session, SCOPE["tenant_id"], artifact, now=now)
    finally:
        await engine.dispose()


async def test_admission_pins_product_artifacts_before_work_and_rejects_short_policy(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admission-holds.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(UTC)
        artifacts = [{"artifact_id": f"source-object-{index}", "version": "v1", "sha256": "a" * 64}
                     for index in range(2)]
        snapshot = {"snapshot_id": "snapshot-example", "content": artifacts[0],
                    "file_manifest": artifacts[1], "expires_at": (now + timedelta(days=2)).isoformat()}
        async with sessions() as session:
            session.add(Configuration(tenant_id=SCOPE["tenant_id"], configuration_id="config-example",
                                      product_id="product-example", project_id=SCOPE["project_id"],
                                      service_id="well_architected", relationship_revision="1",
                                      owner_id="owner-example", current_revision=1, handoff_state="linked"))
            session.add(ConfigurationVersion(tenant_id=SCOPE["tenant_id"], configuration_id="config-example",
                                             revision=1, settings={}, inherited_fields=[], gaps=[],
                                             state="ready", hash="a" * 64))
            session.add(Binding(tenant_id=SCOPE["tenant_id"], binding_id="binding-example",
                                configuration_id="config-example", product_id="product-example",
                                project_id=SCOPE["project_id"], service_id="well_architected",
                                scope=SCOPE, revision=1, generation=1, state="ready",
                                ownership="customer_linked", connection_ref={}, connection_key="connection-example",
                                readiness=[], gaps=[]))
            await session.commit()

            def admission(label, policy_until):
                run = Run(tenant_id=SCOPE["tenant_id"], run_id=f"run-{label}", product_id="product-example",
                          project_id=SCOPE["project_id"], service_id="well_architected",
                          configuration_id="config-example", configuration_revision=1,
                          binding_id="binding-example", binding_generation=1, trigger_id="manual",
                          input={"sources": []}, deadline_at=now + timedelta(hours=1),
                          admission={"source_snapshots": [snapshot], "source_retention": {
                              "required_until": (now + timedelta(days=1)).isoformat(),
                              "policy_expires_at": policy_until.isoformat()}})
                receipt = RunReceipt(tenant_id=SCOPE["tenant_id"], product_id="product-example",
                                     project_id=SCOPE["project_id"], service_id="well_architected",
                                     actor_id="owner-example", operation="submit", idempotency_key=label,
                                     request_hash="a" * 64, run_id=run.run_id,
                                     expires_at=now + timedelta(days=2))
                work = Work(tenant_id=SCOPE["tenant_id"], work_id=f"work-{label}", run_id=run.run_id,
                            kind="execute", generation=1, step=0, due_at=now)
                return run, receipt, work

            await persist_admission(session, *admission("valid", now + timedelta(days=2)))
            holds = (await session.scalars(select(SourceRetention).where(
                SourceRetention.tenant_id == SCOPE["tenant_id"]))).all()
            assert {hold.artifact_id for hold in holds} == {item["artifact_id"] for item in artifacts}
            assert all(hold.run_id == "run-valid" for hold in holds)
            assert not await source_cleanup_allowed(session, SCOPE["tenant_id"], artifacts[0],
                                                    now=now + timedelta(days=3))
            admitted = await session.get(Run, (SCOPE["tenant_id"], "run-valid"))
            admitted.state = "blocked"
            await session.commit()
            with pytest.raises(HTTPException) as denied:
                await persist_admission(session, *admission("conflict", now + timedelta(hours=2)))
            assert denied.value.detail == {"code": "source_retention_unavailable"}
            assert await session.get(Run, (SCOPE["tenant_id"], "run-conflict")) is None
            assert (await session.scalars(select(SourceRetention))).all() == holds
    finally:
        await engine.dispose()
