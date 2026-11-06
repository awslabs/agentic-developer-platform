"""Verify pinned offline Transform installation and scoped configuration readbacks."""

import io
import json
import re
import zipfile
from hashlib import sha256

from l5_services.configuration_readiness import transform_capability_readiness


RUNTIME_ENTRIES = {
    "l5_services/workers/execution.py", "l5_services/workers/recovery.py",
    "l5_services/adapters/transform/execution.py",
    "l5_services/adapters/transform/retention.py",
    "l5_services/migrations/versions/l5_transform_cleanup.py", "l5_services/run_input.py",
}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _digest(value, size):
    return isinstance(value, str) and re.fullmatch(rf"[a-f0-9]{{{size}}}", value) is not None


async def verify_ec01(driver, *, source_revision, schema_head):
    _require(_digest(source_revision, 40) and isinstance(schema_head, str) and schema_head,
             "pinned source and migration head are required")
    release = await driver.read_release()
    _require(isinstance(release, dict) and release.get("service") == "transform"
             and release.get("phase") == "artifacts"
             and release.get("source_revision") == source_revision
             and isinstance(release.get("manifest"), dict)
             and isinstance(release.get("artifacts"), dict)
             and set(release["artifacts"]) == {"wheel", "archive"}
             and all(isinstance(item, dict) and bool(item.get("key"))
                     and bool(item.get("version_id")) and _digest(item.get("sha256"), 64)
                     for item in release["artifacts"].values()),
             "release is not a pinned Transform artifact handoff")
    wheel_ref = release["artifacts"]["wheel"]
    archive_ref = release["artifacts"]["archive"]
    wheel = await driver.read_artifact(wheel_ref)
    archive = await driver.read_artifact(archive_ref)
    _require(isinstance(wheel, bytes) and isinstance(archive, bytes)
             and sha256(wheel).hexdigest() == wheel_ref["sha256"]
             and sha256(archive).hexdigest() == archive_ref["sha256"],
             "versioned release bytes do not match their digests")
    try:
        with zipfile.ZipFile(io.BytesIO(wheel)) as package:
            _require(RUNTIME_ENTRIES <= set(package.namelist()),
                     "Transform entry points are absent from the release wheel")
        with zipfile.ZipFile(io.BytesIO(archive)) as workers:
            packaged = json.loads(workers.read("release/wheel.json"))
            _require(isinstance(packaged, dict) and packaged.get("wheel_sha256") == wheel_ref["sha256"]
                     and workers.read(f"release/{packaged['wheel_filename']}") == wheel,
                     "worker archive does not embed the selected wheel")
    except (zipfile.BadZipFile, KeyError, ValueError, TypeError):
        raise ValueError("versioned release archive is unreadable") from None

    migration = await driver.read_migration()
    _require(isinstance(migration, dict) and migration.get("head") == schema_head
             and migration.get("cleanup_column") is True
             and migration.get("before_rows") == migration.get("after_rows")
             and isinstance(migration.get("before_rows"), list)
             and {row.get("service_id") for row in migration["before_rows"]
                  if isinstance(row, dict)} == {"security", "devops"}
             and bool(migration.get("evidence_ref")),
             "migration lost old service rows or cleanup receipts")
    profiles = {}
    for selection, expected_status in (("ready", "ready"), ("missing_input", "missing_input")):
        observation = await driver.read_configuration(selection)
        _require(isinstance(observation, dict) and observation.get("status") == 200
                 and bool(observation.get("evidence_ref"))
                 and isinstance(observation.get("configuration"), dict)
                 and isinstance(observation.get("sql_settings"), dict)
                 and observation["configuration"].get("settings") == observation["sql_settings"]
                 and observation.get("etag") == f'"{observation["configuration"].get("hash")}"'
                 and isinstance(observation.get("scope"), dict)
                 and observation["configuration"].get("scope") == observation["scope"]
                 and isinstance(observation.get("options"), dict)
                 and observation["options"].get("service_id") == "transform"
                 and observation.get("check", {}).get("provider_contacted") is False
                 and observation["check"].get("ready") is False,
                 "configuration API differs from scoped SQL or claims native readiness")
        settings = observation["sql_settings"]
        sources = settings.get("source_refs")
        _require(settings.get("service_id") == "transform"
                 and settings.get("capabilities") == ["refactor"]
                 and isinstance(sources, list) and len(sources) == 1
                 and sources[0] in observation["options"].get("sources", [])
                 and sources[0].get("tenant_id") == observation["scope"].get("tenant_id")
                 and sources[0].get("project_id") == observation["scope"].get("project_id")
                 and _digest(sources[0].get("record_revision"), 40),
                 "selected repository revision is not scoped and pinned")
        readiness = transform_capability_readiness(
            settings, qualified_executor_ref=observation.get("qualified_executor_ref"))
        _require(observation["check"].get("readiness") == readiness
                 and len(readiness) == 1 and readiness[0]["status"] == expected_status
                 and (expected_status != "missing_input"
                      or "test_policy_ref_missing" in {gap["code"] for gap in readiness[0]["gaps"]}),
                 "readiness gaps do not match the selected Transform capability")
        profiles[selection] = {"scope": observation["scope"], "readiness": readiness,
                               "evidence_ref": observation["evidence_ref"]}
    _require(profiles["ready"]["scope"]["project_id"] != profiles["missing_input"]["scope"]["project_id"],
             "ready and missing-input controls must use separate projects")
    return {"phase": "offline-install-compatibility", "live_qualified": False,
            "wheel_sha256": wheel_ref["sha256"], "archive_sha256": archive_ref["sha256"],
            "migration_ref": migration["evidence_ref"], "profiles": profiles}
