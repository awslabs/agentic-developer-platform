"""Check manual scenario assertions, fixture bindings and repeatability controls."""

import json

from check_catalog_contract import FIXTURES, HOME, require


SCENARIOS = json.loads((HOME / "catalog-scenarios.json").read_text())
DOCUMENT = (HOME / "catalog-scenarios.md").read_text()
SCOPE = json.loads((HOME / "catalog-scope-examples.json").read_text())
ROLLOUT = json.loads((HOME / "catalog-rollout-evidence.json").read_text())


def main():
    controls = SCENARIOS["controls"]
    for field in ("fixtures", "as_of_clock", "revisions", "cleanup", "report"):
        require(controls.get(field), f"Shared scenario control absent: {field}")
    require(controls["poll_deadline_seconds"] == 120, "Bounded polling deadline changed")
    require(controls["poll_max_cadence_seconds"] == 2, "Bounded polling cadence changed")
    require(controls["page_size_max"] == 100, "Page size bound changed")
    require(FIXTURES["clock"] in controls["as_of_clock"], "Fixture clock drift")
    require("run-owned" in controls["fixtures"] and "durable manifest" in controls["fixtures"], "Run-owned fixtures not specified")
    require("verify absence" in controls["cleanup"] and "receipts/history" in controls["cleanup"], "Cleanup loses history")
    require("source/requirements revision" in controls["report"] and "run/attempt" in controls["report"], "Attempt report incomplete")
    assert_names = set()
    schema_cases = {case["name"]: case for case in FIXTURES["cases"]}
    transactions = {case["name"]: case for case in FIXTURES["transactions"]}
    for scenario in SCENARIOS["scenarios"]:
        scenario_id = scenario["id"]
        require(scenario_id not in assert_names, f"Duplicate scenario: {scenario_id}")
        assert_names.add(scenario_id)
        require(f"## {scenario_id} " in DOCUMENT, f"Undocumented scenario: {scenario_id}")
        require(scenario["acceptance_id"] in DOCUMENT, "Missing acceptance mapping")
        require(scenario["state"] == "planned manual; not executed", "Unobserved test claimed as executed")
        require(scenario["given"] and scenario["when"] and len(scenario["then"]) >= 3, "Case lacks independent assertions")
        require(scenario["independent_observation"] and scenario["evidence"], "Missing independent observation/evidence")
        if scenario_id == "SCN-01":
            require(scenario["acceptance_id"] == "AC-01", "SCN-01 acceptance drift")
            require(scenario["fixture_source_revision"] == FIXTURES["source_revision"], "Catalog fixture source revision drift")
            require(set(scenario["schema_cases"]) <= schema_cases.keys(), "Unknown wire fixture")
            require(set(scenario["transactions"]) <= transactions.keys(), "Unknown transaction fixture")
            require(all(not schema_cases[name]["valid"] for name in ("nested-portfolio-rejected", "extra-allocation-target-rejected")), "Missing invalid wire oracles")
            require(all(transactions[name]["expect"] != "accepted" for name in ("duplicate-pair-different-projection", "stale-supersedes", "invalid-interval", "duplicate-current-commitment")), "Missing negative transaction oracles")
            require(all(transactions[name]["expect"] == "accepted" for name in ("first-membership", "other-product-same-portfolio", "same-product-other-portfolio", "close-membership")), "Missing positive transaction oracles")
        if scenario_id == "SCN-02":
            require(scenario["acceptance_id"] == "AC-02", "SCN-02 acceptance drift")
            require(scenario["fixture_source_revision"] == SCOPE["source_revision"], "Scope source revision drift")
            require(SCOPE["clock"] in controls["as_of_clock"], "Scope fixture clock drift")
            scope_cases = {case["name"]: case for case in SCOPE["cases"]}
            require(set(scenario["scope_cases"]) == scope_cases.keys(), "Scope scenario misses a positive or negative fixture")
            for name in ("overlapping-projects-deduplicated", "second-page-no-duplicate", "valid-membership-mutation"):
                require(scope_cases[name]["expect"]["status"] in (200, 201), f"Positive scope oracle drift: {name}")
            for name, status, code in (("revoked-project-cursor", 400, "invalid_cursor"), ("expired-cursor", 400, "invalid_cursor"), ("foreign-id", 404, "not_found"), ("nonexistent-id", 404, "not_found"), ("foreign-mutation", 404, "not_found"), ("stale-expected-revision", 412, "stale_revision"), ("revoked-source", 404, "not_found"), ("revoked-project-mutation", 404, "not_found"), ("visible-but-no-edit-right", 403, "permission_denied")):
                require(scope_cases[name]["expect"] == {"status": status, "code": code}, f"Denial oracle drift: {name}")
            require(scope_cases["no-hidden-count"]["expect"] == {"status": 200, "items": ["commerce"]}, "Hidden count case drift")
            require(scope_cases["foreign-id"]["expect"] == scope_cases["nonexistent-id"]["expect"], "Foreign and nonexistent reads disclose different errors")
        if scenario_id == "SCN-03":
            require(scenario["acceptance_id"] == "AC-03", "SCN-03 acceptance drift")
            require(scenario["inspected_source_revision"] == ROLLOUT["inspected_source_revision"], "Rollout source revision drift")
            require(scenario["requirements_revision"] == ROLLOUT["requirements_revision"], "Requirements revision drift")
            require(set(scenario["workflow_paths"]) == {item["path"] for item in ROLLOUT["source_assertions"]}, "Rollout scenario lacks a pinned workflow/source path")
            require(set(scenario["live_evidence"]) == {"requirements SHA", "source SHA", "workflow definition SHA", "schema head", "gateway image digest", "CLI checksum", "frontend manifest", "deployment receipt", "independent API/source readback", "fixture cleanup receipt"}, "Live evidence pins or cleanup receipt missing")
            for required in ("pre-serving migration", "disable new admissions first", "durable manifest", "not-run", "separate", "under current authorization"):
                require(required in DOCUMENT, f"Rollout manual case lacks {required}")
    require(assert_names == {"SCN-01", "SCN-02", "SCN-03"}, "One or more catalog scenarios absent or unmapped")
    print(f"PASS: {len(assert_names)} planned catalog scenarios mapped to fixture oracles and bounded controls; no live claim")


if __name__ == "__main__":
    main()
