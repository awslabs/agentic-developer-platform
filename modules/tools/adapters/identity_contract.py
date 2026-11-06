"""Independently judge measured Identity outcomes from the isolated image run."""

import hashlib
import json
import re
from xml.etree import ElementTree

from foundation_contract import require

CASES = {
    "consent": "test_atlassian_mcp_consent_is_isolated_from_legacy_and_requires_observation",
    "denial": "test_atlassian_mcp_binding_denials_before_identity_or_provider",
    "admission": "test_two_user_callback_replay_and_release_fence_through_real_admission_routes",
    "retry": "test_older_observation_cannot_publish_after_new_attempt_starts",
    "disconnect": "test_disconnect_queues_each_grant_generation_without_identity_tokens",
    "cancel": "test_cancel_v2_migration_keeps_legacy_credential_and_binding",
    "repair": "test_packaged_restart_retries_cancelled_migration_after_failed_retirement",
}
TESTS = [
    "tests/auth/test_connections_routes.py::" + CASES["consent"],
    "tests/auth/test_connections_routes.py::" + CASES["denial"],
    "tests/internal/test_tool_adapter_authority.py::" + CASES["admission"],
    "tests/auth/test_atlassian_mcp_reconnect_postgres.py::" + CASES["retry"],
    "tests/auth/test_atlassian_mcp_revocation.py::" + CASES["disconnect"],
    "tests/auth/test_atlassian_mcp_revocation.py::" + CASES["cancel"],
    "tests/migrations/test_136_grant_revocation_worker.py::" + CASES["repair"],
]
ADAPTER_CASES = {
    "methods": "test_provider_observation_requires_pinned_schemas_and_two_real_method_replies",
    "forged": "test_adapter_rejects_forged_role_and_purpose_before_identity",
    "refresh": "test_observation_rechecks_owner_after_identity_refresh_before_provider",
}
ADAPTER_TESTS = ["/image_tests/test_observation.py::" + name for name in ADAPTER_CASES.values()]
GATEWAY_FILES = [
    "src/auth/atlassian_mcp_routes.py",
    "src/internal/tool_adapter_authority.py",
    "src/internal/tool_adapter_observation.py",
    "src/shared/identity/oauth_grant_worker.py",
    "src/shared/models/oauth_setup.py",
    "src/shared/models/vault.py",
]
ADAPTER_FILES = [
    "tool_adapters/server.py",
    "tool_adapters/config.py",
    "tool_adapters/observation.py",
    "tool_adapters/caller_identity.py",
    "tool_adapters/atlassian_observe.py",
    "tool_adapters/identity_access.py",
]


def _unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def observations(junit):
    require(isinstance(junit, str) and 0 < len(junit) <= 2 * 1024 * 1024)
    require("<!DOCTYPE" not in junit and "<!ENTITY" not in junit)
    root = ElementTree.fromstring(junit)
    require(root.tag in {"testsuites", "testsuite"})
    require(not any(list(root.iter(tag)) for tag in ("failure", "error", "skipped")))
    cases = list(root.iter("testcase"))
    require(len(cases) == 12)
    measured = {}
    for case in cases:
        name = case.get("name", "")
        kind = next((kind for kind, prefix in CASES.items() if name == prefix or name.startswith(prefix + "[")), None)
        require(kind is not None)
        properties = case.findall("properties/property")
        values = [item.get("value") for item in properties if item.get("name") == "identity_observation"]
        require(len(values) == 1 and values[0] is not None)
        value = json.loads(values[0], object_pairs_hook=_unique)
        require(isinstance(value, dict))
        measured.setdefault(kind, []).append(value)
    require(
        {kind: len(values) for kind, values in measured.items()}
        == {
            "consent": 1,
            "denial": 5,
            "admission": 1,
            "retry": 2,
            "disconnect": 1,
            "cancel": 1,
            "repair": 1,
        }
    )
    consent, admission, disconnect, cancel, repair = (
        measured[kind][0]
        for kind in (
            "consent",
            "admission",
            "disconnect",
            "cancel",
            "repair",
        )
    )
    require(
        consent["owner"] == disconnect["owner"] == "user-alice"
        and consent["tenant"] == disconnect["tenant"] == "org-acme"
    )
    require(consent["site"] == "site-2" and consent["provider_calls"] == 1)
    require(consent["foreign_site_status"] == 403 and consent["stale_site_status"] == 409)
    require(admission["owner"] == "alice" and admission["foreign_status"] == 403)
    require(admission["callback_status"] == 201 and admission["replay_status"] == 403)
    require(admission["old_dispatch_status"] == admission["fenced_dispatch_status"] == 403)
    require(admission["fresh_admission_status"] == 200 and admission["fenced_admission_status"] == 403)
    require(admission["provider_calls"] == 1 and admission["generation"] == 1 and bool(admission["old_attempt"]))
    require(
        {item["fault"] for item in measured["denial"]}
        == {
            "foreign_owner",
            "foreign_tenant",
            "changed_site",
            "callback_site",
            "wrong_session",
        }
    )
    for item in measured["denial"]:
        require(item["status"] == {"changed_site": 409, "callback_site": 422}.get(item["fault"], 403))
        require(item["provider_calls_before"] == item["provider_calls_after"] and item["observation_calls"] == 0)
        require(bool(item["attempt"]))
    require({item["retried"] for item in measured["retry"]} == {False, True})
    for item in measured["retry"]:
        require(item["old_attempt"] != item["new_attempt"] and item["owner"] == "user-alice")
        require(item["stale_status"] == 409 and item["current_status"] == 201 and item["identity_completions"] == 2)
        require(item["token_persisted"] is False)
    require(disconnect["pending"] == 2 and disconnect["wrong_revision_status"] == 409)
    require(disconnect["disconnect_status"] == cancel["disconnect_status"] == 200)
    require(disconnect["provider_calls"] == cancel["provider_calls"] == 0)
    require(len(disconnect["generations"]) == 2 and cancel["legacy_consent"] is True)
    require(cancel["legacy_revision"] == repair["legacy_revision"] == "legacy-revision")
    require(cancel["v2_jobs"] == 1 and cancel["v2_generation"] == repair["generation"])
    require(repair["failed_repair_exit"] != 0 and repair["restart_exit"] == 0)
    require(repair["worker_state"] == "done" and repair["attempts"] == 3 and repair["cancel_status"] == 200)
    require(repair["ledger"] == {"calls": [repair["generation"]] * 2, "effects": [repair["generation"]]})
    require(all(item["token_persisted"] is False for item in (consent, disconnect)))
    require(bool(consent["attempt"]) and bool(repair["job"]))
    return measured


def adapter_observations(junit):
    require(isinstance(junit, str) and 0 < len(junit) <= 1024 * 1024)
    require("<!DOCTYPE" not in junit and "<!ENTITY" not in junit)
    root = ElementTree.fromstring(junit)
    require(root.tag in {"testsuite", "testsuites"})
    require(not any(list(root.iter(tag)) for tag in ("failure", "error", "skipped")))
    cases = list(root.iter("testcase"))
    require(len(cases) == 6)
    measured = {}
    for case in cases:
        kind = next(
            (
                kind
                for kind, prefix in ADAPTER_CASES.items()
                if case.get("name", "") == prefix or case.get("name", "").startswith(prefix + "[")
            ),
            None,
        )
        require(kind is not None)
        values = [
            property.get("value")
            for property in case.findall("properties/property")
            if property.get("name") == "identity_observation"
        ]
        require(len(values) == 1 and values[0] is not None)
        measured.setdefault(kind, []).append(json.loads(values[0], object_pairs_hook=_unique))
    require({kind: len(values) for kind, values in measured.items()} == {"methods": 1, "forged": 1, "refresh": 4})
    methods, forged = measured["methods"][0], measured["forged"][0]
    require(methods["subject"] == "subject-1" and methods["site_count"] == 2 and methods["schema_rejected"] is True)
    require(methods["methods"] == ["initialize", "notifications/initialized", "tools/list", "tools/call", "tools/call"])
    require(
        forged["forged_role_status"] == 403
        and forged["wrong_purpose_status"] in (403, 422)
        and forged["identity_calls"] == 0
    )
    require(
        {value["change"] for value in measured["refresh"]} == {"unchanged", "reconnect", "disconnect", "substituted"}
    )
    for value in measured["refresh"]:
        require(value["status"] == {"unchanged": 200, "substituted": 403}.get(value["change"], 503))
        require(value["owner"] == "alice" and bool(value["attempt"]) and value["generation"] == 1)
        require(value["core_checks"] == 2 and value["identity_refreshes"] == 1)
        require(value["provider_calls"] == (1 if value["change"] == "unchanged" else 0))
        require(value["token_reflected"] is False)
    return measured


def summary(report, *, native, source, gateway_source, gateway_image, adapter_image, build_id):
    require(report.get("schema_version") == 1 and report.get("stage_id") == "identity")
    require(report.get("source_revision") == source and report.get("gateway_revision") == gateway_source == source)
    require(report.get("gateway_image") == gateway_image and report.get("adapter_image") == adapter_image)
    require(report.get("build_id") == build_id and report.get("test_exit") == 0)
    require(report.get("adapter_test_exit") == 0)
    require(
        report.get("database_names_before") == report.get("database_names_after")
        and bool(report.get("database_names_before"))
    )
    require(report.get("remaining_containers") == report.get("remaining_networks") == [])
    require(report.get("gateway_import") == "/app/src/auth/atlassian_mcp_routes.py")
    require(report.get("adapter_import") == "/opt/adapter-venv/lib/python3.13/site-packages/tool_adapters/server.py")
    require(report.get("gateway_source_sha256") == report.get("expected_gateway_source_sha256"))
    require(report.get("adapter_source_sha256") == report.get("expected_adapter_source_sha256"))
    require(
        all(
            isinstance(report.get(field), str) and re.fullmatch(r"[0-9a-f]{64}", report[field])
            for field in ("gateway_source_sha256", "adapter_source_sha256")
        )
    )
    require(report.get("forbidden_imports") == [])
    require(
        report.get("adapter_statuses")
        == {
            "/healthz": 200,
            "/contract": 200,
            "/readyz": 200,
            "/internal/observe": 403,
        }
    )
    require(native["before"] == native["after"] and bool(native["before"]))
    adapter_target = native["before"]["adapter"]
    require(bool(adapter_target["cluster"]) and bool(adapter_target["namespace"]))
    require(native["lease_checks"] >= 2 and native["run_attempt"] == 1)
    require(isinstance(native["run_id"], int) and native["run_id"] > 0)
    require(re.fullmatch(r"[0-9a-f]{40}", native["workflow_revision"]))
    require(re.fullmatch(r"orch:[0-9a-f-]{36}", native["correlation"]))
    measured = observations(report["junit"])
    adapter_measured = adapter_observations(report["adapter_junit"])
    return {
        "schema_version": 1,
        "stage_id": "identity",
        "issue_number": 8448,
        "source_revision": source,
        "gateway_revision": gateway_source,
        "workflow_revision": native["workflow_revision"],
        "definition_revision": native["workflow_revision"],
        "run_id": str(native["run_id"]),
        "run_attempt": str(native["run_attempt"]),
        "correlation": native["correlation"],
        "criteria": [
            {"criterion_id": f"8448.AC-{index:02}", "phase_status": "pass", "evidence_verified": True}
            for index in range(1, 5)
        ],
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
        "build_id": build_id,
        "gateway_image": gateway_image,
        "adapter_image": adapter_image,
        "resource_kind": "eks-namespace",
        "resource_id": adapter_target["cluster"] + "/" + adapter_target["namespace"],
        "measurements": {"gateway": measured, "adapter": adapter_measured},
        "junit_sha256": hashlib.sha256(report["junit"].encode()).hexdigest(),
        "adapter_junit_sha256": hashlib.sha256(report["adapter_junit"].encode()).hexdigest(),
    }
