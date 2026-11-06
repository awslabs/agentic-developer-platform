"""Validate private two-project Transform observations without declaring live acceptance."""

import re
from decimal import Decimal, InvalidOperation


RELEASE_DIGESTS = ("wheel_sha256", "worker_sha256", "provider_image_sha256",
                   "validation_image_sha256", "schema_sha256", "gateway_image_sha256")
EPIC_OUTCOMES = tuple(f"EC-{index:02}" for index in range(1, 9))


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _digest(value, length=64):
    return isinstance(value, str) and re.fullmatch(rf"[a-f0-9]{{{length}}}", value) is not None


def _evidence(value):
    return isinstance(value, str) and bool(value.strip())


def _cost(cost):
    _require(isinstance(cost, dict) and cost.get("status") in {"actual", "unknown"}
             and _evidence(cost.get("evidence_ref")), "cost readback is missing")
    if cost["status"] == "unknown":
        _require(cost.get("amount_usd") is None and cost.get("currency") is None,
                 "unknown cost cannot be reported as zero")
    else:
        amount = cost.get("amount_usd")
        _require(isinstance(amount, str) and re.fullmatch(r"(?:0|[1-9][0-9]*)\.[0-9]{2}", amount),
                 "actual cost requires a measured decimal amount")
        try:
            _require(Decimal(amount).is_finite() and Decimal(amount) >= 0
                     and cost.get("currency") == "USD", "actual cost is not a USD measurement")
        except InvalidOperation:
            raise ValueError("actual cost is not a USD measurement") from None


async def build_portability_report(driver, *, fixture_ids):
    _require(isinstance(fixture_ids, (list, tuple)) and len(fixture_ids) == 2
             and all(isinstance(fixture_id, str) and fixture_id for fixture_id in fixture_ids)
             and len(set(fixture_ids)) == 2, "two different private fixtures are required")
    history = await driver.read_deployment_history()
    _require(isinstance(history, dict) and type(history.get("deployment_count")) is int
             and history["deployment_count"] == 1
             and type(history.get("platform_edits_between")) is int
             and history["platform_edits_between"] == 0
             and type(history.get("redeployments_between")) is int
             and history["redeployments_between"] == 0
             and _evidence(history.get("evidence_ref")),
             "same-release deployment continuity is unproven")
    entries = []
    for fixture_id in fixture_ids:
        manifest = await driver.read_fixture(fixture_id)
        release = await driver.read_release(fixture_id)
        result = await driver.read_result(fixture_id)
        cleanup = await driver.read_cleanup(fixture_id)
        cost = await driver.read_cost(fixture_id)
        outcomes = await driver.read_outcomes(fixture_id)
        _require(isinstance(manifest, dict) and manifest.get("fixture_id") == fixture_id
                 and manifest.get("is_adp") is False
                 and manifest.get("language") in {"python", "javascript"}
                 and isinstance(manifest.get("scope"), dict)
                 and all(_evidence(manifest["scope"].get(key))
                         for key in ("tenant_id", "product_id", "project_id"))
                 and isinstance(manifest.get("repository_ref"), dict)
                 and manifest["repository_ref"].get("tenant_id") == manifest["scope"]["tenant_id"]
                 and manifest["repository_ref"].get("project_id") == manifest["scope"]["project_id"]
                 and manifest["repository_ref"].get("record_type") == "repository_commit"
                 and _evidence(manifest["repository_ref"].get("source_connection_id"))
                 and _evidence(manifest["repository_ref"].get("record_id"))
                 and _digest(manifest["repository_ref"].get("record_revision"), 40)
                 and isinstance(manifest.get("test_commands"), list)
                 and bool(manifest["test_commands"])
                 and all(isinstance(command, list) and bool(command)
                         and all(_evidence(argument) for argument in command)
                         for command in manifest["test_commands"])
                 and _evidence(manifest.get("evidence_ref")),
                 "non-ADP repository and independent test fixture are unproven")
        _require(isinstance(release, dict) and _digest(release.get("source_revision"), 40)
                 and all(_digest(release.get(key)) for key in RELEASE_DIGESTS)
                 and _evidence(release.get("evidence_ref")),
                 "installed release digests are incomplete")
        _require(isinstance(result, dict) and result.get("scope") == manifest["scope"]
                 and _evidence(result.get("run_id")) and _evidence(result.get("attempt_id"))
                 and _evidence(result.get("native_id"))
                 and _evidence(result.get("evidence_ref"))
                 and result.get("source_revision") == manifest["repository_ref"]["record_revision"]
                 and result.get("tests_before") == "observed" and result.get("tests_after") == "observed",
                 "native result or independent tests are not attributable to this fixture")
        owned = cleanup.get("owned_inventory") if isinstance(cleanup, dict) else None
        adopted = cleanup.get("adopted_inventory") if isinstance(cleanup, dict) else None
        _require(isinstance(owned, list) and bool(owned)
                 and isinstance(adopted, list) and bool(adopted)
                 and all(isinstance(item, dict) and _evidence(item.get("reference"))
                         and _evidence(item.get("receipt_ref")) and item.get("absent") is True
                         for item in owned)
                 and all(isinstance(item, dict) and _evidence(item.get("reference"))
                         and item.get("present") is True for item in adopted)
                 and len({item["reference"] for item in owned + adopted}) == len(owned + adopted),
                 "owned and adopted cleanup inventories are incomplete")
        _require(isinstance(cleanup, dict) and cleanup.get("run_id") == result["run_id"]
                 and cleanup.get("scope") == manifest["scope"]
                 and cleanup.get("owned_absent") is True
                 and cleanup.get("adopted_retained") is True
                 and cleanup.get("scm_unchanged") is True
                 and _evidence(cleanup.get("termination_ref"))
                 and _digest(cleanup.get("retained_patch_sha256"))
                 and cleanup.get("retained_patch_sha256") == result.get("original_patch_sha256")
                 and _evidence(cleanup.get("evidence_ref")),
                 "post-cleanup retained evidence or ownership readback is missing")
        _cost(cost)
        _require(isinstance(outcomes, dict) and set(outcomes) == set(EPIC_OUTCOMES),
                 "each epic outcome needs an explicit state")
        for outcome in outcomes.values():
            _require(isinstance(outcome, dict) and outcome.get("status") in {"verified", "blocked", "not_run"}
                     and (outcome["status"] != "verified" or (
                         _evidence(outcome.get("evidence_ref"))
                         and outcome.get("run_id") == result["run_id"]
                         and outcome.get("attempt_id") == result["attempt_id"]
                         and _evidence(outcome.get("revision"))))
                     and (outcome["status"] == "verified" or _evidence(outcome.get("reason"))),
                     "epic outcome lacks status or direct evidence")
        entries.append({"fixture": manifest, "release": release, "result": result,
                        "cleanup": cleanup, "cost": cost, "outcomes": outcomes})
    first, second = entries
    _require(first["fixture"]["language"] != second["fixture"]["language"]
             and first["fixture"]["test_commands"] != second["fixture"]["test_commands"]
             and first["fixture"]["repository_ref"] != second["fixture"]["repository_ref"]
             and first["fixture"]["scope"]["project_id"] != second["fixture"]["scope"]["project_id"]
             and first["result"]["run_id"] != second["result"]["run_id"]
             and first["result"]["native_id"] != second["result"]["native_id"]
             and all(first["release"][field] == second["release"][field]
                     for field in ("source_revision", *RELEASE_DIGESTS))
             and history.get("source_revision") == first["release"]["source_revision"],
             "two distinct projects must use one immutable release without redeployment")
    return {"phase": "portability-observations", "live_qualified": False,
            "acceptance_complete": False, "deployment_history": history, "fixtures": entries}
