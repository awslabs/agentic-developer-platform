"""Offline design checks: structural contracts, examples, matrix and local links."""

import json
import re
import runpy
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate

from catalog import HOME, OPERATIONS, artifacts


def walk_refs(value, definitions):
    if isinstance(value, dict):
        if "$ref" in value:
            reference = value["$ref"]
            assert reference.split("#")[0] in {"", "./schemas.json"}, reference
            assert reference.split("/")[-1] in definitions, reference
        for child in value.values():
            walk_refs(child, definitions)
    elif isinstance(value, list):
        for child in value:
            walk_refs(child, definitions)


def references_in_scope(value, tenant_id, project_id):
    if isinstance(value, dict):
        if {"tenant_id", "project_id", "source_connection_id", "record_type", "record_id", "record_revision"} <= value.keys():
            if (value["tenant_id"], value["project_id"]) != (tenant_id, project_id):
                return False
        return all(references_in_scope(child, tenant_id, project_id) for child in value.values())
    if isinstance(value, list):
        return all(references_in_scope(child, tenant_id, project_id) for child in value)
    return True

def main():
    for filename, expected in artifacts().items():
        assert (HOME / filename).read_text() == expected, f"Regenerate {filename}"
    schema = json.loads((HOME / "schemas.json").read_text())
    definitions = schema["$defs"]
    Draft202012Validator.check_schema(schema)
    for name, definition in definitions.items():
        Draft202012Validator.check_schema(definition)
        if definition.get("type") == "object":
            assert definition.get("additionalProperties") is False, name
    walk_refs(schema, definitions)
    specification = json.loads((HOME / "openapi.json").read_text())
    walk_refs(specification, definitions)
    validate(specification, base_uri=(HOME / "openapi.json").as_uri())
    examples = json.loads((HOME / "examples.json").read_text())
    for index, example in enumerate(examples):
        validator = Draft202012Validator({**schema, "$ref": "#/$defs/" + example["schema"]}, format_checker=FormatChecker())
        assert validator.is_valid(example["value"]) == example["valid"], f"Example {index}"
    names = [operation[0] for operation in OPERATIONS]
    assert len(names) == len(set(names))
    tools = json.loads((HOME / "mcp-tools.json").read_text())
    assert {tool["x-http-operation"] for tool in tools} == set(names) - {"launchNativeBrowser"}
    for tool in tools:
        assert re.fullmatch(r"[a-z][a-z0-9_]{0,63}___[a-z][a-z0-9_]{0,63}", tool["name"])
        Draft202012Validator.check_schema(tool["inputSchema"])
        Draft202012Validator.check_schema(tool["outputSchema"])
        for direction in ("inputSchema", "outputSchema"):
            walk_refs(tool[direction], tool[direction].get("$defs", {}))
        assert not ({"tenant_id", "root_human_id", "run_id", "user_id", "adp_assertion"} & tool["inputSchema"]["properties"].keys())
    assert set(definitions["BindingAction"]["enum"]) == {"create", "link", "check", "update", "suspend", "resume", "unlink", "cleanup", "sync_knowledge"}
    scenarios = json.loads((HOME / "scenarios.json").read_text())
    owners = {"Configuration": {"configuration_api"}, "Operation": {"setup_worker"}, "Binding": {"setup_worker", "lifecycle_api"}, "Run": {"execution_worker"}, "Publication": {"publication_worker"}}
    for scenario in scenarios:
        kind = scenario["kind"]
        if kind == "scope":
            permitted = scenario["tenant"] == scenario["source_tenant"] and scenario["project"] == scenario["source_project"] and not scenario["revoked"]
            assert permitted == scenario["expected"], scenario["id"]
        elif kind == "pin":
            assert (scenario["admitted_revision"] == scenario["materialized_revision"]) == scenario["expected"], scenario["id"]
        elif kind == "contract_fixture":
            indexed = {"SCN-01": ("security-fixtures.json", "test_security_contracts"),
                       "SCN-02": ("lifecycle-fixtures.json", "test_lifecycle_boundaries"),
                       "SCN-03": ("compatibility-fixtures.json", "test_compatibility_fixtures")}
            assert (scenario["fixture"], scenario["selector"]) == indexed[scenario["id"]]
            assert json.loads((HOME / scenario["fixture"]).read_text())["scenario"] == scenario["id"]
        elif kind == "retry":
            assert (scenario["state"] == "failed" and scenario["no_effect_proven"]) == scenario["expected"], scenario["id"]
        else:
            assert kind == "lifecycle"
            assert scenario["owner"] in owners[scenario["entity"]]
            assert set(scenario["states"]) <= set(definitions[scenario["entity"]]["properties"]["state"]["enum"])
            assert not scenario["binding_deleted"]
            assert scenario["provider_submits"] == (1 if scenario["entity"] == "Run" else 0)
    documentation = HOME.parents[1]
    root = documentation.parents[1]
    module_readme = root / "modules/L5-services/README.md"
    checked_links = 0
    for document in [*documentation.rglob("*.md"), module_readme]:
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", document.read_text()):
            if "://" in target or target.startswith("mailto:"):
                continue
            path, _, fragment = target.partition("#")
            resolved = (document.parent / path).resolve() if path else document
            assert resolved.exists(), f"Broken link {document}: {target}"
            if fragment and resolved.suffix == ".md":
                headings = re.findall(r"^#+\s+(.+)$", resolved.read_text(), re.M)
                anchors = {re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-") for heading in headings}
                assert fragment in anchors, f"Broken anchor {document}: {target}"
            checked_links += 1
    print(f"Validated {len(definitions)} schemas, {len(names)} operations, {len(examples)} schema fixtures, {len(scenarios)} specification scenarios and {checked_links} local links")
    from test_contracts import ReviewContractChecks

    result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(ReviewContractChecks))
    assert result.wasSuccessful(), "Contract review regression examples failed"
    scanner = runpy.run_path(str(root / "scripts/check-public-docs.py"))["findings"]
    checked_files = 0
    for path in [*documentation.rglob("*"), module_readme]:
        if path.is_file() and "__pycache__" not in path.parts:
            findings = list(scanner(path.read_text()))
            assert not findings, f"Public-data pattern findings in {path}: {findings}"
            checked_files += 1
    print(f"Supplemental public-data scan: {checked_files} L5 artifact files; no findings")


if __name__ == "__main__":
    main()
