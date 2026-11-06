"""Compare two independent offline native Transform scenarios against pinned fixtures."""

import re

from l5_services.transform_native_probe import probe_native_result


CAPABILITIES = ("refactor", "dependency_upgrade")


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _digest(value, size):
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{size}}}", value) is not None


async def verify_ec03(cases):
    _require(isinstance(cases, dict) and set(cases) == set(CAPABILITIES),
             "both native fixture cases are required")
    observed = {}
    for capability in CAPABILITIES:
        case = cases[capability]
        _require(isinstance(case, dict) and set(case) == {
            "driver", "run_id", "scope", "source_revision", "expected_native_id",
            "expected_patch_sha256", "expected_base_tree", "expected_candidate_tree",
        } and isinstance(case["run_id"], str) and bool(case["run_id"])
                 and isinstance(case["expected_native_id"], str) and bool(case["expected_native_id"])
                 and isinstance(case["scope"], dict)
                 and set(case["scope"]) == {"tenant_id", "product_id", "project_id"}
                 and all(isinstance(value, str) and value for value in case["scope"].values())
                 and _digest(case["source_revision"], 40)
                 and _digest(case["expected_patch_sha256"], 64)
                 and _digest(case["expected_base_tree"], 40)
                 and _digest(case["expected_candidate_tree"], 40)
                 and case["expected_base_tree"] != case["expected_candidate_tree"],
                 "native scenario is not pinned to a separate source and patch")
    refactor, upgrade = (cases[name] for name in CAPABILITIES)
    _require(refactor["driver"] is not upgrade["driver"]
             and refactor["scope"]["project_id"] != upgrade["scope"]["project_id"]
             and refactor["run_id"] != upgrade["run_id"]
             and refactor["source_revision"] != upgrade["source_revision"]
             and refactor["expected_native_id"] != upgrade["expected_native_id"]
             and refactor["expected_patch_sha256"] != upgrade["expected_patch_sha256"],
             "native refactor and upgrade cannot reuse a session or fixture")
    for capability in CAPABILITIES:
        case = cases[capability]
        evidence = await probe_native_result(
            case["driver"], run_id=case["run_id"], scope=case["scope"],
            source_revision=case["source_revision"], capability=capability)
        _require(evidence["live_qualified"] is False
                 and evidence["native_id"] == case["expected_native_id"]
                 and evidence["original_patch_sha256"] == case["expected_patch_sha256"]
                 and evidence["base_tree"] == case["expected_base_tree"]
                 and evidence["candidate_tree"] == case["expected_candidate_tree"],
                 "independent native or original patch readback differs from pinned fixture")
        checks = evidence["checks"]
        _require(isinstance(checks, dict), "detached before/after checks are unavailable")
        for stage in ("before", "after"):
            entries = checks.get("tests_" + stage)
            tree = evidence["base_tree" if stage == "before" else "candidate_tree"]
            _require(isinstance(entries, list) and bool(entries)
                     and all(isinstance(entry, dict) and entry.get("status") == "passed"
                             and isinstance(entry.get("receipt"), dict)
                             and entry["receipt"].get("stage") == stage
                             and entry["receipt"].get("source_tree") == tree
                             and type(entry["receipt"].get("test_count")) is int
                             and entry["receipt"]["test_count"] >= 1 for entry in entries),
                     "independently rerun checks did not pass on both source trees")
        observed[capability] = evidence
    return {"phase": "offline-native-fixtures", "live_qualified": False, "cases": observed}
