"""Validate design artifacts only; does not exercise an ADP runtime."""

import json
import copy
import importlib.util
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate

from contract_catalog import OPERATIONS, generate


HOME = Path(__file__).resolve().parent
ROOT = HOME.parents[2]
REVISION = "56a8450aa41cc661c52ad90a1e421b8a16aca7f8"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def pointer(document, fragment):
    value = document
    for part in fragment.removeprefix("/").split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def slug(text):
    text = re.sub(r"[^\w\- ]", "", text.lower().replace("`", ""))
    return text.replace(" ", "-")


def validate_links():
    count = 0
    pinned = 0
    for path in sorted(HOME.glob("*.md")):
        columns = None
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if not line.startswith("|"):
                columns = None
                continue
            width = len(re.split(r"(?<!\\)\|", line)) - 2
            columns = width if columns is None else columns
            require(width == columns, f"Broken Markdown table: {path.name}:{number}")
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", path.read_text()):
            parsed = urlsplit(target)
            if parsed.scheme in {"http", "https"}:
                prefix = f"https://github.com/aws-e/adp/blob/{REVISION}/"
                if target.startswith(prefix):
                    relative = unquote(parsed.path.split(f"/blob/{REVISION}/", 1)[1])
                    content = subprocess.check_output(["git", "show", f"{REVISION}:{relative}"], cwd=ROOT)
                    if parsed.fragment.startswith("L"):
                        require(int(parsed.fragment[1:]) <= len(content.splitlines()), f"Invalid source line: {target}")
                    pinned += 1
                continue
            destination = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
            require(destination.is_file(), f"Missing local link: {path.name}: {target}")
            if parsed.fragment:
                headings = re.findall(r"^#{1,6}\s+(.+)$", destination.read_text(), re.MULTILINE)
                require(parsed.fragment in {slug(heading) for heading in headings}, f"Missing heading: {target}")
            count += 1
    return count, pinned


def validate_lineage_alignment(schemas, context, profile):
    """Use the owning contracts, including actual raw-lineage scope validation."""
    relation_validator = Draft202012Validator(
        {"$ref": "#/$defs/relationship", "$defs": context["$defs"]},
        format_checker=FormatChecker(),
    )
    examples = json.loads((HOME / "registry-relationship-examples.json").read_text())
    for relation in examples:
        relation_validator.validate(relation)
        Draft202012Validator({"$ref": "#/$defs/RegistryRelationship", "$defs": schemas["$defs"]}, format_checker=FormatChecker()).validate(relation)
        require(relation["from_ref"]["record_type"] == "product", "Binding source is not Product")
        require(relation["to_ref"]["record_type"] == "project", "Binding target is not Project")
        for endpoint in (relation["from_ref"], relation["to_ref"]):
            Draft202012Validator({"$ref": "#/$defs/RecordRef", "$defs": schemas["$defs"]}).validate(endpoint)
            require(endpoint["tenant_id"] == profile["tenant_id"] and endpoint["project_id"] == profile["project_id"], "Registry projection changed scope")
    original, closed = examples
    require(original["relationship_id"] == closed["relationship_id"], "Closing a binding minted a new relationship")
    require(closed["supersedes_revision"] == original["relationship_revision"], "Missing relationship revision chain")
    require(original["mapping_version"] == closed["mapping_version"], "Closing a binding changed mapping policy")
    require("valid_to" not in original and closed["valid_to"] > original["valid_from"], "Historical open version was rewritten")
    module_path = ROOT / "modules/insights/schemas/validate.py"
    spec = importlib.util.spec_from_file_location("profile_insights_validator", module_path)
    insights = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(insights)
    sample = json.loads((ROOT / "modules/insights/schemas/fixtures/v2/coverage_mapping.json").read_text())
    require(not insights.validate_record(sample, "coverage_mapping", "v2"), "Existing scoped lineage fixture failed")
    cross_project = copy.deepcopy(sample)
    cross_project["to_ref"]["project_id"] = "another-example-project"
    require(bool(insights.validate_record(cross_project, "coverage_mapping", "v2")), "Cross-Project raw reference was admitted")
    require(bool(insights.validate_record(profile, "story_revision", "v2")), "Profile incorrectly admitted as raw lineage")
    task = json.loads((ROOT / "modules/insights/schemas/fixtures/v1/task.json").read_text())
    task.pop("story_id")
    require(bool(insights.validate_record(task, "task", "v1")), "Existing Story-required producer contract silently relaxed")
    # Existing Context fixtures express one Initiative's contributions from two Products.
    snapshots = [json.loads((ROOT / "docs/product-context/examples/cross-product" / name).read_text()) for name in ("checkout.json", "accounts.json")]
    for snapshot in snapshots:
        Draft202012Validator(context, format_checker=FormatChecker()).validate(snapshot)
    contributions = [record for snapshot in snapshots for record in snapshot["records"] if record["record_type"] == "contribution"]
    require(len({record["data"]["product_ref"]["record_id"] for record in contributions}) > 1, "Cross-product Initiative example lost its product contributions")
    require(len({record["data"]["initiative_ref"]["record_id"] for record in contributions}) == 1, "Contribution example no longer uses a shared Initiative")
    # Portfolio membership is optional and many-to-many, with scoped catalog projections.
    context_validator = Draft202012Validator(context, format_checker=FormatChecker())
    minimal = json.loads((ROOT / "docs/product-context/examples/minimal-profile.json").read_text())
    context_validator.validate(minimal)
    require(not any(r["record_type"] == "portfolio" for r in minimal["records"]), "Portfolio became a first-save requirement")
    memberships = [link for snapshot in snapshots for link in snapshot["relationships"] if link["relationship_type"] == "portfolio_product"]
    portfolio_products, product_portfolios = {}, {}
    ref_fields = ("tenant_id", "project_id", "source_connection_id", "record_type", "record_id", "record_revision")
    for snapshot in snapshots:
        records = {tuple(record[field] for field in ref_fields) for record in snapshot["records"]}
        for link in (r for r in snapshot["relationships"] if r["relationship_type"] == "portfolio_product"):
            for endpoint in (link["from_ref"], link["to_ref"]):
                require(endpoint["project_id"] == snapshot["project_id"], "Portfolio fixture rewrote the member's Project scope")
                require(tuple(endpoint[field] for field in ref_fields) in records, "Portfolio membership has an unresolved endpoint revision")
    for link in memberships:
        portfolio_id, product_id = link["from_ref"]["record_id"], link["to_ref"]["record_id"]
        portfolio_products.setdefault(portfolio_id, set()).add(product_id)
        product_portfolios.setdefault(product_id, set()).add(portfolio_id)
    require(any(len(members) > 1 for members in portfolio_products.values()), "Portfolio fixture no longer groups Products")
    require(any(len(collections) > 1 for collections in product_portfolios.values()), "Product was restricted to one Portfolio")
    for endpoint, wrong_type in (("from_ref", "program"), ("to_ref", "portfolio")):
        invalid = copy.deepcopy(memberships[0])
        invalid[endpoint]["record_type"] = wrong_type
        require(bool(list(relation_validator.iter_errors(invalid))), "Portfolio membership admitted a wrong endpoint or nested collection")
    portfolio = next(r for r in snapshots[0]["records"] if r["record_type"] == "portfolio")
    require(bool(insights.validate_record(portfolio, "story_revision", "v2")), "Portfolio incorrectly admitted as raw lineage")
    print("PASS: registry relationship history; cross-product Initiative context; existing Insights same-Project rule and producer admission")
    print("PASS: optional Portfolio; many-to-many Product membership; exact endpoint revisions; invalid membership types rejected")


def main():
    schemas = json.loads((HOME / "schemas.json").read_text())
    Draft202012Validator.check_schema(schemas)
    for node in walk(schemas):
        if "$ref" in node:
            require(node["$ref"].startswith("#/"), "Schema references must resolve within the reviewed document")
            pointer(schemas, node["$ref"][1:])
    spec = json.loads((HOME / "openapi.json").read_text())
    require((HOME / "openapi.json").read_text() == generate(), "Operation catalog drift")
    validate(spec, base_uri=(HOME / "openapi.json").as_uri())
    examples = json.loads((HOME / "examples.json").read_text())
    names = set()
    positive = negative = 0
    for case in examples:
        require(case["name"] not in names, "Duplicate example name")
        names.add(case["name"])
        selected = {"$ref": f"#/$defs/{case['schema']}", "$defs": schemas["$defs"]}
        errors = list(Draft202012Validator(selected, format_checker=FormatChecker()).iter_errors(case["value"]))
        require(bool(errors) != case["valid"], f"Example mismatch {case['name']}: {[error.message for error in errors]}")
        positive += int(case["valid"])
        negative += int(not case["valid"])
        if "operation_id" in case:
            operation = next(item for item in OPERATIONS if item[2] == case["operation_id"])
            expected = operation[3] if case["direction"] == "request" else operation[4] if case["status"] < 400 else "Error"
            require(case["schema"] == expected, f"Example not bound to its operation: {case['name']}")
    profile = next(case["value"] for case in examples if case["name"] == "minimal-profile")
    require(profile["project_id"] == profile["project"]["id"], "Profile Project mismatch")
    require(profile["tenant_id"] == profile["project"]["tenant_id"], "Profile tenant mismatch")
    progress = profile["progress"]
    require(progress["percentage"] == 100 * progress["completed"] // progress["applicable"], "Example percentage mismatch")
    for provenance in profile["provenance"]:
        for path in provenance["paths"]:
            pointer(profile, path)
    context = json.loads((ROOT / "docs/product-context/context-model-v1.schema.json").read_text())
    require(schemas["$defs"]["Allocation"]["properties"]["approval_status"]["enum"] == context["$defs"]["approval_status"]["enum"], "Context allocation approval enum drift")
    allocation = dict(next(case["value"] for case in examples if case["name"] == "financial-allocation"))
    allocation.pop("id")
    context_schema = {"$ref": "#/$defs/allocation_data", "$defs": context["$defs"]}
    Draft202012Validator(context_schema, format_checker=FormatChecker()).validate(allocation)
    # Product-team mapping is local registry metadata, not a new Context field.
    product_data = dict(next(case["value"]["product"] for case in examples if case["name"] == "shared-team-profile"))
    product_data.pop("team_id")
    Draft202012Validator({"$ref": "#/$defs/product_data", "$defs": context["$defs"]}, format_checker=FormatChecker()).validate(product_data)
    redacted = {"id": "example-binding", "project_id": "example-project", "status": "restricted", "generation": 1, "revision": 2}
    binding_validator = Draft202012Validator({"$ref": "#/$defs/Binding", "$defs": schemas["$defs"]})
    binding_validator.validate(redacted)
    redacted["selection"] = next(case["value"] for case in examples if case["name"] == "repository-selection")
    require(not binding_validator.is_valid(redacted), "Restricted binding must not disclose source selection")
    experience = (HOME / "experience.md").read_text()
    api = (HOME / "api.md").read_text()
    require(len({operation[2] for operation in OPERATIONS}) == len(OPERATIONS), "Duplicate operation ID")
    for method, path, operation_id, request, response, status, command, conditional in OPERATIONS:
        require(operation_id in api, f"Undocumented operation: {operation_id}")
        if command and operation_id != "applyCallback":
            require(command.split(" --")[0] in experience, f"Missing client mapping: {command}")
        if method not in {"GET"} and operation_id != "applyCallback":
            parameters = spec["paths"][path][method.lower()]["parameters"]
            require(any(item["name"] == "Idempotency-Key" and item["required"] for item in parameters), f"Missing idempotency: {operation_id}")
            if conditional:
                require(any(item["name"] == "If-Profile-Revision" and item["required"] for item in parameters), f"Missing revision precondition: {operation_id}")
    coverage = (HOME / "requirements-coverage.md").read_text()
    rows = re.findall(r"^\| B(\d{2}) / (\d+) \|", coverage, re.MULTILINE)
    baseline = (ROOT / "docs/product-onboarding/requirements-2026-10-13.md").read_text().splitlines()
    expected_lines = [number for number, line in enumerate(baseline, 1) if re.match(r"- \[.\] ", line)]
    require(len(expected_lines) == 74, "Baseline obligation count changed")
    require(rows == [(f"{number:02}", str(line)) for number, line in enumerate(expected_lines, 1)], "Missing/reordered baseline obligation mapping")
    for prefix, count in (("EC", 15), ("PO", 13), ("PON", 13)):
        for number in range(1, count + 1):
            require(f"| {prefix}-{number:02} |" in coverage, f"Missing {prefix} mapping {number}")
    evaluation = (HOME / "evaluation.md").read_text()
    for prefix, count in (("AC", 5), ("SCN", 3)):
        for number in range(1, count + 1):
            require(f"| {prefix}-{number:02} |" in evaluation, f"Missing design criterion {prefix}-{number:02}")
    require(len(re.findall(r"^\| V\d{2} ", evaluation, re.MULTILINE)) == 15, "Scenario coverage drift")
    delivery = (HOME / "delivery.md").read_text()
    for prefix in "IDCU":
        for number in range(1, 6):
            require(f"| {prefix}{number} — " in delivery, f"Missing vertical checkpoint {prefix}{number}")
    summary = json.loads((HOME / "inventory-summary.json").read_text())
    require(summary["revision"] == REVISION and not summary["unclassified_paths"], "Unresolved inventory mapping")
    validate_lineage_alignment(schemas, context, profile)
    local_links, source_links = validate_links()
    print(f"PASS: OpenAPI {len(OPERATIONS)} operations; {len(schemas['$defs'])} schema definitions; {positive} positive / {negative} negative examples")
    print(f"PASS: {local_links} local links / {source_links} pinned source links; 74 obligations; EC/PO/PON and AC/SCN coverage; 5 I/D/C/U increments")
    print("PASS: Context product/allocation export compatibility, restricted-binding positive/negative assertions, and Markdown table structure")
    print("Design-only validation. No runtime, PostgreSQL migration, deployed UI/CLI or provider test was executed.")


if __name__ == "__main__":
    main()
