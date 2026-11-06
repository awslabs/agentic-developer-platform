"""Fictional offline fixtures; private live project manifests are never committed."""

from copy import deepcopy

import pytest

from l5_services.transform_portability_report import EPIC_OUTCOMES, build_portability_report


class FixtureDriver:
    def __init__(self):
        self.history = {"deployment_count": 1, "redeployments_between": 0,
                        "platform_edits_between": 0, "source_revision": "a" * 40,
                        "evidence_ref": "private/release-timeline"}
        self.fixtures = {}
        for name, language, command, project in (
            ("library-python", "python", ["pytest", "tests/test_upgrade.py"], "product-python"),
            ("library-javascript", "javascript", ["npm", "test"], "product-javascript"),
        ):
            scope = {"tenant_id": "tenant-example", "product_id": project, "project_id": project}
            repository = {"tenant_id": scope["tenant_id"], "project_id": project,
                          "source_connection_id": "repository", "record_type": "repository_commit",
                          "record_id": name, "record_revision": ("b" if language == "python" else "c") * 40}
            manifest = {"fixture_id": name, "is_adp": False, "scope": scope,
                        "language": language, "repository_ref": repository,
                        "test_commands": [command], "evidence_ref": f"private/{name}/manifest"}
            release = {"source_revision": "a" * 40, "wheel_sha256": "d" * 64,
                       "worker_sha256": "e" * 64, "provider_image_sha256": "f" * 64,
                       "validation_image_sha256": "0" * 64, "schema_sha256": "1" * 64,
                       "gateway_image_sha256": "2" * 64,
                       "evidence_ref": f"private/{name}/installed-readback"}
            result = {"run_id": f"run-{name}", "attempt_id": f"attempt-{name}",
                      "native_id": f"native-{name}",
                      "scope": scope, "source_revision": repository["record_revision"],
                      "original_patch_sha256": "3" * 64,
                      "tests_before": "observed", "tests_after": "observed",
                      "evidence_ref": f"private/{name}/provider-tests"}
            cleanup = {"run_id": result["run_id"], "scope": scope,
                       "owned_absent": True, "adopted_retained": True,
                       "scm_unchanged": True, "retained_patch_sha256": "3" * 64,
                       "owned_inventory": [{"reference": f"owned-{name}", "absent": True,
                                            "receipt_ref": f"private/{name}/creation"}],
                       "adopted_inventory": [{"reference": f"adopted-{name}", "present": True}],
                       "termination_ref": f"private/{name}/termination",
                       "evidence_ref": f"private/{name}/cleanup"}
            cost = {"status": "unknown", "amount_usd": None, "currency": None,
                    "evidence_ref": f"private/{name}/cost-unavailable"}
            outcomes = {outcome: {"status": "blocked", "reason": "deployed-target not checked"}
                        for outcome in EPIC_OUTCOMES}
            self.fixtures[name] = {"manifest": manifest, "release": release,
                                   "result": result, "cleanup": cleanup, "cost": cost,
                                   "outcomes": outcomes}
        self.calls = []

    async def read_deployment_history(self):
        self.calls.append("deployment")
        return deepcopy(self.history)

    async def _read(self, kind, fixture_id):
        self.calls.append((fixture_id, kind))
        return deepcopy(self.fixtures[fixture_id][kind])

    async def read_fixture(self, fixture_id):
        return await self._read("manifest", fixture_id)

    async def read_release(self, fixture_id):
        return await self._read("release", fixture_id)

    async def read_result(self, fixture_id):
        return await self._read("result", fixture_id)

    async def read_cleanup(self, fixture_id):
        return await self._read("cleanup", fixture_id)

    async def read_cost(self, fixture_id):
        return await self._read("cost", fixture_id)

    async def read_outcomes(self, fixture_id):
        return await self._read("outcomes", fixture_id)


async def report(driver):
    return await build_portability_report(driver, fixture_ids=list(driver.fixtures))


@pytest.mark.asyncio
async def test_distinct_projects_preserve_one_release_and_unknown_cost():
    driver = FixtureDriver()
    result = await report(driver)
    assert result["live_qualified"] is False and result["acceptance_complete"] is False
    assert len(result["fixtures"]) == 2 and driver.calls[0] == "deployment"
    assert result["fixtures"][0]["fixture"]["test_commands"] != result["fixtures"][1]["fixture"]["test_commands"]
    assert all(entry["cost"]["amount_usd"] is None for entry in result["fixtures"])
    assert all(set(entry["outcomes"]) == set(EPIC_OUTCOMES) for entry in result["fixtures"])
    driver.fixtures["library-python"]["cost"] = {
        "status": "actual", "amount_usd": "0.25", "currency": "USD", "evidence_ref": "private/actual-cost"}
    actual = await report(driver)
    assert actual["fixtures"][0]["cost"]["amount_usd"] == "0.25"
    assert actual["fixtures"][1]["cost"]["status"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "one_fixture", "adp", "same_repo", "same_tests", "same_language", "missing_manifest",
    "different_release", "platform_edit", "redeploy", "missing_image_digest", "missing_provider",
    "wrong_revision", "different_patch_hash", "adopted_deleted", "missing_cleanup",
    "unknown_as_zero", "unmeasured_zero", "missing_cost", "missing_outcome",
    "empty_verified_evidence", "wrong_verified_run", "no_block_reason",
    "missing_owned_receipt", "adopted_aliased", "same_native", "boolean_deployment_count",
    "missing_attempt", "wrong_verified_attempt",
])
async def test_unproven_portability_and_false_acceptance_are_rejected(change):
    driver = FixtureDriver()
    python = driver.fixtures["library-python"]
    javascript = driver.fixtures["library-javascript"]
    if change == "missing_attempt":
        python["result"]["attempt_id"] = ""
    elif change == "wrong_verified_attempt":
        python["outcomes"]["EC-03"] = {"status": "verified", "run_id": python["result"]["run_id"],
                                       "attempt_id": "unrelated", "revision": "1",
                                       "evidence_ref": "private/unrelated"}
    elif change == "missing_owned_receipt":
        python["cleanup"]["owned_inventory"][0]["receipt_ref"] = ""
    elif change == "adopted_aliased":
        python["cleanup"]["adopted_inventory"][0]["reference"] = "owned-library-python"
    elif change == "same_native":
        javascript["result"]["native_id"] = python["result"]["native_id"]
    elif change == "boolean_deployment_count":
        driver.history["deployment_count"] = True
    elif change == "one_fixture":
        driver.fixtures.pop("library-javascript")
    elif change == "adp":
        python["manifest"]["is_adp"] = True
    elif change == "same_repo":
        javascript["manifest"]["repository_ref"] = python["manifest"]["repository_ref"]
    elif change == "same_tests":
        javascript["manifest"]["test_commands"] = python["manifest"]["test_commands"]
    elif change == "same_language":
        javascript["manifest"]["language"] = "python"
    elif change == "missing_manifest":
        python["manifest"]["evidence_ref"] = None
    elif change == "different_release":
        javascript["release"]["worker_sha256"] = "4" * 64
    elif change == "platform_edit":
        driver.history["platform_edits_between"] = 1
    elif change == "redeploy":
        driver.history["redeployments_between"] = 1
    elif change == "missing_image_digest":
        python["release"]["provider_image_sha256"] = ""
    elif change == "missing_provider":
        python["result"]["native_id"] = ""
    elif change == "wrong_revision":
        python["result"]["source_revision"] = "4" * 40
    elif change == "different_patch_hash":
        python["cleanup"]["retained_patch_sha256"] = "4" * 64
    elif change == "adopted_deleted":
        python["cleanup"]["adopted_retained"] = False
    elif change == "missing_cleanup":
        python["cleanup"]["evidence_ref"] = ""
    elif change == "unknown_as_zero":
        python["cost"]["amount_usd"] = "0.00"
    elif change == "unmeasured_zero":
        python["cost"].update(status="actual", amount_usd="0.00", currency="USD", evidence_ref="")
    elif change == "missing_cost":
        python["cost"]["evidence_ref"] = ""
    elif change == "missing_outcome":
        python["outcomes"].pop("EC-07")
    elif change == "empty_verified_evidence":
        python["outcomes"]["EC-03"] = {"status": "verified", "run_id": python["result"]["run_id"],
                                       "revision": "1", "evidence_ref": ""}
    elif change == "wrong_verified_run":
        python["outcomes"]["EC-03"] = {"status": "verified", "run_id": "foreign",
                                       "revision": "1", "evidence_ref": "private/unrelated"}
    elif change == "no_block_reason":
        python["outcomes"]["EC-03"] = {"status": "blocked", "reason": ""}
    with pytest.raises(ValueError):
        await report(driver)
