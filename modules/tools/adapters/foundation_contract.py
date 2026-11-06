"""Validate measured foundation evidence, not a count of passing unit tests."""

import hashlib
import json
from xml.etree import ElementTree

CASES = {
    "setup": "test_atlassian_automatic_setup_retains_single_provider_after_restart",
    "readiness": "test_atlassian_admin_readiness_isolated_role_policy_and_forged_callback",
    "recovery": "test_packaged_setup_consumer_survives_failed_repair_and_restart",
    "lease": "test_shared_release_scope_fences_different_surfaces_and_isolates_phase_fixtures",
}
TESTS = [
    "tests/auth/test_oauth_app_setup.py::" + CASES["setup"],
    "tests/auth/test_oauth_app_setup.py::" + CASES["readiness"],
    "tests/migrations/test_atlassian_packaged_restart.py::" + CASES["recovery"],
    "tests/orchestration/test_environment_leases_postgres.py::" + CASES["lease"],
]
DEPENDENCIES = {
    "pytest",
    "pytest-asyncio",
    "pytest-timeout",
    "pluggy",
    "iniconfig",
    "packaging",
    "pygments",
    "aiosqlite",
    "psycopg2-binary",
}


def require(condition):
    if not condition:
        raise ValueError("foundation_evidence_unverified")


def observations(junit):
    require(isinstance(junit, bytes) and 0 < len(junit) <= 2 * 1024 * 1024)
    require(b"<!DOCTYPE" not in junit and b"<!ENTITY" not in junit)
    root = ElementTree.fromstring(junit)
    require(root.tag in {"testsuite", "testsuites"})
    tests = list(root.iter("testcase"))
    require(len(tests) == len(CASES))
    require(not any(list(root.iter(tag)) for tag in ("failure", "error", "skipped")))
    result = {}
    for kind, name in CASES.items():
        matches = [test for test in tests if test.get("name") == name]
        require(len(matches) == 1)
        properties = matches[0].findall("properties/property")
        values = [item.get("value") for item in properties if item.get("name") == "foundation_observation"]
        require(len(values) == 1)
        result[kind] = json.loads(values[0])
        if kind == "recovery":
            require(
                [item.get("value") for item in properties if item.get("name") == "foundation_ledger_remaining"]
                == ["false"]
            )
    setup, readiness, recovery, lease = (result[name] for name in CASES)
    require(
        setup["provider_registrations"] == 1 and setup["secret_creates"] == 0 and setup["ledger_remaining"] is False
    )
    require(setup["first"] == setup["second"] and setup["first"]["callback_url"] != setup["first"]["return_url"])
    require(
        readiness["regular_status"] == readiness["blocked_status"] == 403
        and readiness["callback_statuses"] == [303, 303, 405]
    )
    require(
        readiness["callback_locations"]
        == ["/settings/connections?oauth_consent=error", "/settings/connections?oauth_consent=error", None]
        and readiness["callback_payload_reflected"] is False
        and readiness["callback_effect_calls"] == 0
    )
    require(readiness["provider_registrations"] == 1 and readiness["policy"]["status"] == "policy_blocked")
    require(readiness["policy"]["owner"] == "atlassian_organization_admin")
    require(
        recovery
        == {
            "provider_creates": 1,
            "registration_requests": 1,
            "failed_repair_exit": 1,
            "failed_repair_state": "leased",
            "restart_exit": 0,
            "job_state": "done",
            "setup_phase": "prepared",
            "attempts": 3,
        }
    )
    require(
        lease["competing"] == lease["expired"] == lease["stale"] == "conflict" and lease["successor_applied"] is True
    )
    require(bool(lease["fixture_one"]) and bool(lease["fixture_two"]) and lease["fixture_one"] != lease["fixture_two"])
    return result


def qualified(report, *, source, gateway_source, gateway_image, adapter_image, build_id):
    require(report["schema_version"] == 1)
    require(report["source_revision"] == source and report["gateway_revision"] == gateway_source)
    require(
        report["gateway_image"] == gateway_image
        and report["adapter_image"] == adapter_image
        and report["build_id"] == build_id
    )
    measured = observations(report["junit"].encode())
    require(report["test_exit"] == 0 and report["database_names_before"] == report["database_names_after"])
    require(report["remaining_containers"] == [] and report["remaining_networks"] == [])
    require(report["gateway_import"] == "/app/src/shared/identity/oauth_setup.py")
    require(report["adapter_import"] == "/opt/adapter-venv/lib/python3.13/site-packages/tool_adapters/server.py")
    require(
        report["forbidden_imports"] == []
        and report["adapter_statuses"] == {"/healthz": 200, "/contract": 200, "/readyz": 200, "/internal/observe": 403}
    )
    require(report["gateway_source_sha256"] == report["expected_gateway_source_sha256"])
    require(report["adapter_source_sha256"] == report["expected_adapter_source_sha256"])
    require(all(len(report[key]) == 64 for key in ("gateway_source_sha256", "adapter_source_sha256")))
    return {"measurements": measured, "junit_sha256": hashlib.sha256(report["junit"].encode()).hexdigest()}


def summary(report, *, native, **pins):
    measured = qualified(report, **pins)
    require(native["before"] == native["after"] and bool(native["before"]))
    require(native["lease_checks"] >= 2 and native["run_attempt"] == 1)
    return {
        "schema_version": 1,
        "stage_id": "foundation",
        "issue_number": 8447,
        "source_revision": pins["source"],
        "gateway_revision": pins["gateway_source"],
        "workflow_revision": native["workflow_revision"],
        "definition_revision": native["workflow_revision"],
        "run_id": str(native["run_id"]),
        "run_attempt": str(native["run_attempt"]),
        "criteria": {
            f"8447.AC-{index:02}": {"phase_status": "pass", "evidence_verified": True} for index in range(1, 5)
        },
        "runtime_verified": True,
        "independent_verification": "confirmed",
        "cleanup_status": "confirmed",
        "unresolved_operations": [],
        "verification_level": "controlled_integration",
        "fixture_source": "generated",
        "provider_live_attestation": False,
        "test_runtime_artifacts_match": True,
        "shared_release_lease_verified": True,
        "fixture_isolation_verified": True,
        "build_id": pins["build_id"],
        "gateway_image": pins["gateway_image"],
        "adapter_image": pins["adapter_image"],
        **measured,
    }
